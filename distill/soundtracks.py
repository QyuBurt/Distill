"""Soundtrack pulling: a game's name -> candidate albums -> a track list laid out as
`<Game>/soundtrack/01 - Title.flac`, ready for the Download page's runner (and for RomM to find next to the game).

Sources are pluggable `Provider`s. A provider only has to find albums and list their tracks; naming, numbering,
disc folders, matching and the folder layout are done here once. Shipped providers:

  KHInsiderProvider   searches downloads.khinsider.com (streams and downloads; FLAC when the album has it)
  ArchiveOrgProvider  searches archive.org's audio items for the game's soundtrack
  LinkProvider        a link you paste yourself (archive.org item, open directory, ...), re-filed into the game's folder

No Qt in here. Everything that touches the network goes through `fetch_json` / `resolve_links`, so tests swap those out.
"""
from __future__ import annotations

import dataclasses
import concurrent.futures
import difflib
import html
import html.parser
import http.client
import json
import posixpath
import re
import threading
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from . import fuzzy
from . import paths
from .netrules import RULES
from .downloads import ARCHIVE_URL, describe_error, http_open, LinkError, parse_archive_url, RemoteFile
from .formats import associated_files
from .organize import ANY_TAG_RE, DISC_RE as DISC_MARKER_RE, ROMAN_RE, parse_disc, title_core
from .systems import system_of_name


SOUNDTRACK_FOLDER = "soundtrack"  # the folder inside each game's folder; this is the name RomM looks for
STANDARD_ROOT = "Soundtracks"  # Standard mode: <Save to>/Soundtracks/<Game>/01 - Title.flac
SAVE_STANDARD, SAVE_ROMM = "standard", "romm"

AUDIO_EXTS = (".flac", ".wav", ".m4a", ".mp3", ".ogg", ".opus", ".aac", ".wma", ".ape", ".wv")

LOSSLESS = (".flac", ".wav", ".ape", ".wv")  # .m4a can be either; it ranks between lossless and lossy

MATCH_MIN = 0.75  # below this a candidate is shown but never ticked for you

NOISE_WORDS = frozenset({
    "soundtrack", "ost", "original", "score", "music", "official", "complete", "collection", "game", "video",
    "videogame", "from", "of", "the", "and", "a", "edition", "disc", "cd", "vol", "volume",
})

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_LEADING_NUMBER = re.compile(r"^\s*(?:(?:track|tr|disc|cd)\s*)?(\d{1,3})(?:\s*[-_.)\]]\s*|\s+)", re.IGNORECASE)

_LOW_BITRATE_DERIVATIVE = re.compile(r"(?:_|\b)(?:64kb|ogg|vbr)\.|\.ogg$", re.IGNORECASE)


# --- data ------------------------------------------------------------------------------------------------
@dataclass(eq=False, slots=True)
class Album:
    provider: str  # which provider found it
    ident: str  # whatever that provider needs to list its tracks (an archive.org identifier, a link)
    title: str
    detail: str = ""  # creator / track count / ... shown next to the title
    score: float = 0.0  # how well the title matches the game's name, 0..1
    art: str = ""  # address of its cover image, when the source has one
    playable: int | None = None  # individual audio tracks it was found to have (None: not checked / unknown)
    cache: dict = field(default_factory=dict)  # track lists already fetched, by quality preference


@dataclass(eq=False, slots=True)
class Track:
    url: str
    ext: str  # with the dot, lower case
    title: str
    number: int | None = None
    size: int | None = None
    md5: str | None = None
    sub: str = ""  # folder inside the album ("CD1/"), kept so multi-disc releases stay apart
    lossless: bool = False
    disc: int | None = None  # which disc, when the name or tags say so and no folder already does
    seconds: int | None = None  # its length, when the source lists it


@dataclass(eq=False, slots=True)
class GameRequest:
    name: str  # also the folder name, so it should match the game's own folder
    link: str | None = None  # a link pinned by the person: skips the search
    search: str | None = None  # search under this name instead (the folder keeps `name`)


class Provider(Protocol):
    name: str

    def search(self, game: str) -> list[Album]: ...

    def tracks(self, album: Album, prefer: str = "best") -> list[Track]: ...


# --- names -----------------------------------------------------------------------------------------------
def safe_name(text: str) -> str:
    """A string that is safe as one file or folder name on Windows, macOS and Linux."""
    cleaned = _BAD_CHARS.sub("_", text).strip().rstrip(" .")
    return re.sub(r"\s+", " ", cleaned) or "_"


def search_name(game: str) -> str:
    """The game's name without region / disc / revision tags: what you'd actually type in a search box."""
    return re.sub(r"\s+", " ", ANY_TAG_RE.sub("", game).replace("_", " ")).strip(" -.,") or game.strip()


def _words(text: str) -> list[str]:
    core, _ = title_core(fuzzy.strip_accents(text))
    return [w for w in fuzzy.merge_letters(core.split()) if w not in NOISE_WORDS]


def _contains_run(big: list[str], small: list[str]) -> bool:
    """True when `small` appears in `big` as one unbroken run of words."""
    n = len(small)
    return n > 0 and any(big[i:i + n] == small for i in range(len(big) - n + 1))


def match_score(game: str, title: str) -> float:
    """0..1: how well an album title names this game. Whole words only (case never matters), so 'Final Fantasy VII'
    never matches VIII, and a title with the game's words as one unbroken phrase ('Super Mario 64' for 'Mario 64')
    always beats one that merely contains them apart ('Mario Kart 64')."""
    want, have = _words(search_name(game)), _words(title)
    if not want or not have:
        return 0.0
    wset, hset = set(want), set(have)
    hit = wset & hset
    coverage, precision = len(hit) / len(wset), len(hit) / len(hset)
    ratio = difflib.SequenceMatcher(None, " ".join(want), " ".join(have)).ratio()
    phrase = _contains_run(have, want) or _contains_run(want, have)
    score = 0.4 * coverage + 0.2 * precision + 0.15 * ratio + (0.25 if phrase else 0.0)
    if any(_is_number(w) for w in wset - hset):  # a missing sequel number / numeral means a different game
        score = min(score, 0.6)
    if len(want) > 1 and not phrase:  # the words are there but split up by others: another game's title
        score = min(score, 0.6)
    return round(score, 3)


def _is_number(word: str) -> bool:
    return word.isdigit() or bool(ROMAN_RE.match(word))


_SMALL_WORDS = frozenset({"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of", "on", "or", "the", "to", "vs", "vs."})
_ROMAN = re.compile(r"^(?:ii|iii|iv|vi|vii|viii|ix|xi|xii|xiii|xiv|xv|xvi|xvii|xviii|xix|xx)$")


def title_case(name: str) -> str:
    """'super mario sunshine' -> 'Super Mario Sunshine'. Only words typed wholly in lower case are changed, so a name
    that already has its own capitals (NES, HD, iPhone, a proper 'Zelda II') is left as the person wrote it. Small words
    (of, the, and ...) stay lower case unless they open the name or follow a colon or dash; roman numerals go upper."""
    out, start = [], True  # start: the next word opens the name or a subtitle
    for token in re.split(r"(\s+)", name):
        if not token or token.isspace():
            out.append(token)
            continue
        if token != token.lower():  # has capitals of its own: leave it
            out.append(token)
        else:
            core = token.strip("()[]\"'.,:;!?-")
            if _ROMAN.match(core):
                word = token.upper()
            elif re.fullmatch(r"\d+d", core):  # 3d, 2d
                word = token.upper()
            elif not start and core in _SMALL_WORDS:
                word = token
            else:  # capitalise each part of hyphenated or slashed words (spider-man), and after an apostrophe-free start
                word = re.sub(r"(^|[-/(\[\"])([a-z])", lambda m: m.group(1) + m.group(2).upper(), token)
            out.append(word)
        start = token.endswith((":", "-", "\u2013", "\u2014"))
    return "".join(out)


def title_case_lines(text: str) -> str:
    """The games box with every game name in Title Case. Only the name part of a line (before any '=') is touched;
    comments, blank lines, search aliases and pinned links stay exactly as typed."""
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "://" in stripped.partition("=")[0]:
            lines.append(raw)
            continue
        name, eq, rest = raw.partition("=")
        lead = name[:len(name) - len(name.lstrip())]
        trail = name[len(name.rstrip()):] if eq else ""
        lines.append(lead + title_case(name.strip()) + trail + eq + rest)
    return "\n".join(lines)


def parse_game_lines(text: str) -> list[GameRequest]:
    """One game per line: `Game name`, `Game name = other name to search for`, `Game name = <link>` (a pinned link
    skips the search), or `Game name = other name = <link>`. Blank lines and #comments are ignored; repeated names
    are kept once."""
    seen, out = set(), []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = (part.strip() for part in line.partition("="))
        if "://" in name:  # a bare link on its own line: no name to file it under
            continue
        link = alias = ""
        if "://" in rest:
            eq = rest.rfind("=", 0, rest.index("://"))  # a link may hold '=' itself; only one before it counts
            link = rest[eq + 1:].strip() if eq >= 0 else rest
            alias = rest[:eq].strip() if eq >= 0 else ""
        else:
            alias = rest
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(GameRequest(title_case(name), link or None, alias or None))
    return out


def _visible(p: Path) -> bool:
    return not p.name.startswith((".", "$"))


NOT_GAME_EXTS = frozenset(AUDIO_EXTS) | {".jpg", ".jpeg", ".png", ".webp", ".gif", ".txt", ".nfo", ".xml", ".json",
                                         ".ini", ".db", ".srm", ".sav", ".state", ".url", ".pdf", ".log", ".bak"}


@dataclass(eq=False, slots=True)
class GameEntry:
    """One game found in a games directory: its own folder, or loose file(s) that would get one."""
    key: str  # normalized title (see title_core)
    folder: Path  # the game's folder (for loose files: the folder that would be made)
    is_dir: bool
    files: tuple[Path, ...] = ()  # loose files that belong to it

    @property
    def name(self) -> str:
        return self.folder.name


def _children(folder: Path) -> list[Path]:
    try:
        return sorted((p for p in folder.iterdir() if _visible(p)), key=lambda p: p.name.casefold())
    except OSError:
        return []


def _is_container(folder: Path) -> bool:
    """A platform folder (holds games) rather than a game's own folder."""
    if system_of_name(folder.name):
        return True
    return any(p.is_dir() and p.name.casefold() != SOUNDTRACK_FOLDER for p in _children(folder))


def _loose_entries(folder: Path, files: list[Path]) -> list[GameEntry]:
    """Group loose game files (a .cue and its .bin, every disc of a game) into one entry per game."""
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(title_core(f.stem)[0], []).append(f)
    out = []
    for key, group in groups.items():
        if not key:
            continue
        members = set(group)
        for f in group:
            if f.suffix.lower() in (".cue", ".gdi"):
                members |= {t for t in associated_files(f) if t.parent == folder}
        parsed = parse_disc(group[0].stem)
        name = (parsed[0] if parsed else group[0].stem).strip(" .") or group[0].stem
        out.append(GameEntry(key, folder / name, False, tuple(sorted(members))))
    return out


def scan_games(root: Path) -> list[GameEntry]:
    """Games inside a games directory: game folders and loose game files, either directly inside it or one platform
    folder down (`roms/snes/Chrono Trigger/`)."""
    entries: list[GameEntry] = []

    def read(folder: Path, top: bool) -> None:
        loose: list[Path] = []
        for p in _children(folder):
            if p.is_dir():
                if p.name.casefold() == SOUNDTRACK_FOLDER:
                    continue
                if top and _is_container(p):
                    read(p, False)
                else:
                    entries.append(GameEntry(title_core(p.name)[0], p, True))
            elif p.suffix.lower() not in NOT_GAME_EXTS:
                loose.append(p)
        entries.extend(_loose_entries(folder, loose))

    read(root, True)
    return entries


def game_names_from_folder(root: Path) -> list[str]:
    """The game names inside a games directory (folders and loose files), for the Games box."""
    seen, names = set(), []
    for e in scan_games(root):
        if e.key and e.key not in seen:
            seen.add(e.key)
            names.append(e.name)
    return sorted(names, key=str.casefold)


def find_matches(game: str, entries: list[GameEntry]) -> list[GameEntry]:
    """The entries that are this game. A game folder beats loose files of the same name."""
    key = title_core(game)[0]
    hits = [e for e in entries if key and e.key == key]
    return [e for e in hits if e.is_dir] or hits


# --- layout ----------------------------------------------------------------------------------------------
def _unique(name: str, used: set[str]) -> str:
    stem, dot, ext = name.rpartition(".")
    candidate, n = name, 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"
        n += 1
    used.add(candidate.casefold())
    return candidate


def _natural(text: str) -> list:
    """A sort key that puts 'Disc 2' before 'Disc 10'."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text.casefold())]


def layout(game: str, tracks: list[Track], folder: str = SOUNDTRACK_FOLDER) -> list[RemoteFile]:
    """RemoteFiles named `<Game>/<Soundtrack>/[CD1/]NN - Title.ext`, in disc then track order. The Download page's
    runner saves them at exactly that path under the Save-to folder. Tracks without a number are numbered by their
    position. When one folder holds several discs' tracks (their names say which disc), each disc gets a `Disc N`
    folder of its own, so the track numbers do not collide."""
    base = f"{safe_name(game)}/{safe_name(folder)}"
    ordered = sorted(enumerate(tracks), key=lambda it: (_natural(it[1].sub), it[1].disc is None, it[1].disc or 0,
                                                        it[1].number is None, it[1].number or 0, it[0]))
    discs_in: dict[str, set[int]] = {}
    for _, t in ordered:
        if t.disc is not None:
            discs_in.setdefault(t.sub, set()).add(t.disc)
    by_sub: dict[str, list[Track]] = {}
    for _, t in ordered:
        place = t.sub + (f"Disc {t.disc}/" if t.disc is not None and len(discs_in[t.sub]) > 1 else "")
        by_sub.setdefault(place, []).append(t)
    files = []
    for sub, group in by_sub.items():
        used: set[str] = set()  # names are only unique within one disc folder
        width = max(2, len(str(max([t.number or 0 for t in group] + [len(group)]))))
        folder_part = "".join(safe_name(seg) + "/" for seg in sub.split("/") if seg)
        for pos, t in enumerate(group, 1):
            number = t.number if t.number is not None else pos
            name = _unique(f"{number:0{width}d} - {safe_name(t.title)}{t.ext}", used)
            files.append(RemoteFile(f"{base}/{folder_part}{name}", t.url, t.size, "", t.md5, seconds=t.seconds))
    return files


def tidy_title(stem: str) -> tuple[int | None, str]:
    """('01 - Main Theme' -> (1, 'Main Theme')); names with no leading number give (None, the name)."""
    m = _LEADING_NUMBER.match(stem)
    if m and stem[m.end():].strip():
        return int(m[1]), stem[m.end():].strip()
    return None, stem.strip()


def _int_prefix(value) -> int | None:
    m = re.match(r"\s*(\d+)", str(value or ""))
    return int(m[1]) if m else None


# a disc named at the very start of a file name: "Disc 1 - 05 Title", "CD2 05 Title", "d1t05 Title", "1-05 Title"
_LEADING_DISC = re.compile(r"^\s*(?:(?:disc|disk|cd)\s*0*(\d{1,2})\s*[-_.:)]*\s*"
                           r"|d0*(\d{1,2})\s*[-_. ]?\s*(?=t\d)t?"
                           r"|0*(\d{1,2})\s*[-.]\s*(?=\d{2,3}(?:\s|[-_.)]|$)))", re.IGNORECASE)
_DISC_DASH_TRACK = re.compile(r"^\s*(\d{1,2})\s*[-.]\s*(\d{1,3})\s*$")  # a track tag of "2-05": disc 2, track 5


def split_disc(name: str) -> tuple[int | None, str]:
    """(the disc, the name without its disc marker): 'Disc 2 - 05 Boss' -> (2, '05 Boss'). A disc marker anywhere
    else in the name ('Game (Disc 2) 05 Boss') still gives the disc, and leaves the name as it was."""
    m = _LEADING_DISC.match(name)
    if m and name[m.end():].strip():
        return int(m[1] or m[2] or m[3]), name[m.end():].strip()
    found = DISC_MARKER_RE.search(name)
    return (int(found[1] or found[3]), name) if found else (None, name)


def _disc_and_track(value) -> tuple[int | None, int | None]:
    """A track tag as (disc, track): '2-05' -> (2, 5), '5/22' -> (None, 5), '7' -> (None, 7)."""
    if m := _DISC_DASH_TRACK.match(str(value or "")):
        return int(m[1]), int(m[2])
    return None, _int_prefix(value)


def _rank(ext: str, derivative: bool, prefer: str) -> tuple[int, int]:
    if prefer == "mp3":
        order = {".mp3": 0}
    else:
        order = {".flac": 0, ".wav": 1, ".ape": 1, ".wv": 1, ".m4a": 2, ".mp3": 3}
    return order.get(ext, 9), int(derivative)


# --- archive.org -----------------------------------------------------------------------------------------
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")
COVER_MAX_BYTES = 3_000_000  # bigger than this is not worth streaming just for a 52px picture
_COVER_NAMES = frozenset({"cover", "folder", "front", "albumart", "artwork"})
_NOT_COVER = frozenset({"back", "rear", "inlay", "tray", "spine", "obi", "booklet", "insert", "label", "disc", "disk",
                        "cd"})


def pick_cover(files: Iterable[tuple[str, int | None]]) -> str | None:
    """The name of the best cover image among (name, size) pairs, or None. In order of preference: a file named like
    a cover (cover / front / folder / artwork), the item's own image, then archive.org's auto thumbnail. Backs, discs,
    booklets and anything too big to be worth streaming are never picked."""
    best: tuple[int, str] | None = None
    for name, size in files:
        stem, ext = posixpath.splitext(posixpath.basename(name).lower())
        if ext not in COVER_EXTS or (size is not None and size > COVER_MAX_BYTES):
            continue
        tokens = [t for t in re.split(r"[^a-z0-9]+", stem) if t]
        if any(t in _NOT_COVER or re.fullmatch(r"(?:cd|disc|disk)\d+", t) for t in tokens):
            continue
        if _COVER_NAMES & set(tokens) or stem == "album":
            rank = 0
        elif stem.endswith("_itemimage"):
            rank = 1
        elif stem == "__ia_thumb":
            rank = 2
        else:
            continue  # some other picture (a screenshot, a scan): not safely a cover
        if best is None or (rank, name) < best:
            best = (rank, name)
    return best[1] if best else None


# khinsider's lists (Top 40, the Top 100s, the home page, search) show each album's tiny `thumbs_small` picture. The same file is kept at
# a medium size in `thumbs` (what an album's own page shows) and in full with no folder at all, so a list's cover can be fetched sharper.
_KHI_THUMB = re.compile(r"^(?P<base>https?://[^?#]+/soundtracks/[^/?#]+)/(?P<dir>thumbs_small|thumbs)/(?P<file>[^/?#]+)(?P<tail>[?#].*)?$", re.IGNORECASE)


class CoverTooBig(Exception):
    """A cover bigger than the most worth fetching (see COVER_MAX_BYTES)."""


def cover_candidates(url: str) -> list[str]:
    """The addresses to try for a cover, sharpest first, ending with `url` itself. A khinsider list's `thumbs_small` picture is tried as the
    full picture, then as the medium `thumbs` one; any other address (archive.org, a file on this computer, a picture that is already
    full-size) is just itself."""
    if not url:
        return []
    m = _KHI_THUMB.match(url)
    if not m:
        return [url]
    base, folder, name, tail = m["base"], m["dir"].lower(), m["file"], m["tail"] or ""
    out = [f"{base}/{name}{tail}"]
    if folder == "thumbs_small":
        out.append(f"{base}/thumbs/{name}{tail}")
    out.append(url)
    return out


def image_mime(data: bytes) -> str:
    """The picture type of `data` by its first bytes ('' when it is not a picture Distill knows)."""
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def read_cover(url: str, limit: int = COVER_MAX_BYTES) -> bytes:
    """One cover's bytes: from the net or from a file on this computer. CoverTooBig when it is bigger than `limit` (a big one is refused
    from its header, before it is downloaded)."""
    if is_local_path(url):
        data = Path(url).read_bytes()
    else:
        with http_open(url, timeout=15) as resp:
            size = str(resp.headers.get("Content-Length") or "")
            if size.isdigit() and int(size) > limit:
                raise CoverTooBig(url)
            data = resp.read(limit + 1)
    if len(data) > limit:
        raise CoverTooBig(url)
    return data


def fetch_cover(url: str, limit: int = COVER_MAX_BYTES, reader: Callable[[str, int], bytes] | None = None) -> bytes:
    """The sharpest cover that can be had for `url` (see cover_candidates). A sharper address that is missing, too big or not a picture is
    passed over for the next; the address that was given is read as it always was. b"" when it is too big; any other failure is raised."""
    reader = reader or read_cover
    tries = cover_candidates(url)
    for i, address in enumerate(tries):
        if i == len(tries) - 1:
            try:
                return reader(address, limit)
            except CoverTooBig:
                return b""
        try:
            data = reader(address, limit)
        except Exception:  # (not there, not allowed, too big: the next one down)
            continue
        if image_mime(data):
            return data
    return b""


def archive_art_url(identifier: str) -> str:
    """archive.org makes a cover image for every item (its own thumbnail, or the uploader's cover)."""
    return f"{ARCHIVE_URL}/services/img/{urllib.parse.quote(identifier, safe='')}"


def fetch_json(url: str, timeout: float = 30):
    """GET a URL and parse the JSON. LinkError (with a readable message) on any network or format problem."""
    try:
        with http_open(url, timeout=timeout) as resp:
            return json.load(resp)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise LinkError(describe_error(exc)) from exc


class ArchiveOrgProvider:
    name = "archive.org"

    def __init__(self, rows: int = 50):
        self.rows = rows  # per query

    def _query(self, title_clause: str) -> list[dict]:
        query = f'title:({title_clause}) AND mediatype:audio AND (soundtrack OR ost OR "original score" OR "game music")'
        params = [("q", query), ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "creator"),
                  ("fl[]", "downloads"), ("sort[]", "downloads desc"), ("rows", str(self.rows)), ("output", "json")]
        data = fetch_json(f"{ARCHIVE_URL}/advancedsearch.php?" + urllib.parse.urlencode(params))
        return ((data or {}).get("response") or {}).get("docs") or []

    def search(self, game: str) -> list[Album]:
        """Two searches, merged: the name as an exact phrase, then all of its words in any order. Both are
        case-insensitive on archive.org's side; scoring then ranks the merged list."""
        text = search_name(game).replace('"', " ").strip()
        words = [w for w in re.split(r"[^\w]+", text) if w]
        clauses = [f'"{text}"'] + ([" AND ".join(words)] if len(words) > 1 else [])
        docs: list[dict] = []
        errors: list[LinkError] = []
        for clause in clauses:
            try:
                docs += self._query(clause)
            except LinkError as exc:
                errors.append(exc)
        if errors and len(errors) == len(clauses):
            raise errors[0]
        albums, seen = [], set()
        for d in docs:
            ident, title = d.get("identifier"), d.get("title")
            if not ident or not title or ident in seen:
                continue
            seen.add(ident)
            creator = d.get("creator")
            creator = ", ".join(creator) if isinstance(creator, list) else (creator or "")
            albums.append(Album(self.name, ident, str(title), creator, match_score(game, str(title)),
                                archive_art_url(ident)))
        return albums

    def tracks(self, album: Album, prefer: str = "best") -> list[Track]:
        if prefer in album.cache:
            return album.cache[prefer]
        quoted = urllib.parse.quote(album.ident, safe="")
        data = fetch_json(f"{ARCHIVE_URL}/metadata/{quoted}")
        files = [f for f in (data or {}).get("files") or [] if isinstance(f, dict) and f.get("name")]
        if not files:
            raise LinkError(f"{album.ident}: no files (item missing, empty or restricted)")
        best: dict[str, tuple[tuple[int, int], dict, str]] = {}
        for f in files:
            name = str(f["name"])
            ext = posixpath.splitext(name)[1].lower()
            if ext not in AUDIO_EXTS or name.endswith("/"):
                continue
            if "64kbps" in str(f.get("format", "")).lower() or _LOW_BITRATE_DERIVATIVE.search(name) and ext != ".ogg":
                continue
            rank = _rank(ext, f.get("source") == "derivative", prefer)
            stem = posixpath.splitext(name)[0]
            if rank[0] < 9 and (stem not in best or rank < best[stem][0]):
                best[stem] = (rank, f, ext)
        tracks = []
        for stem, (_, f, ext) in best.items():
            sub = posixpath.dirname(stem)
            name_disc, rest = split_disc(posixpath.basename(stem))
            fallback_number, fallback_title = tidy_title(rest)
            title = str(f.get("title") or "").strip() or fallback_title
            tag_disc, number = _disc_and_track(f.get("track"))
            disc = _int_prefix(f.get("disc") or f.get("discnumber")) or tag_disc or name_disc
            if number is None:
                number = fallback_number
            if title.casefold() == posixpath.basename(stem).casefold():
                number, title = (fallback_number if number is None else number), fallback_title
            try:
                size = int(f["size"])
            except (KeyError, TypeError, ValueError):
                size = None
            url = f"{ARCHIVE_URL}/download/{quoted}/{urllib.parse.quote(str(f['name']), safe='/')}"
            tracks.append(Track(url, ext, title, number, size, f.get("md5"), (sub + "/") if sub else "",
                                ext in LOSSLESS, disc, to_seconds(f.get("length"))))
        if not tracks:
            raise LinkError(f"{album.ident}: no audio files found in it")
        sizes = []
        for f in files:
            try:
                sizes.append((str(f["name"]), int(f["size"])))
            except (KeyError, TypeError, ValueError):
                sizes.append((str(f["name"]), None))
        if cover := pick_cover(sizes):  # the album's own cover beats archive.org's generic item image
            album.art = f"{ARCHIVE_URL}/download/{quoted}/{urllib.parse.quote(cover, safe='/')}"
        album.cache[prefer] = tracks
        return tracks


def to_seconds(value) -> int | None:
    """A length as sources write it ('215.42', '3:35', 215) in whole seconds; None when it is not one."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if ":" in text:
            total = 0
            for part in text.split(":"):
                total = total * 60 + int(float(part))
            return total or None
        return int(round(float(text))) or None
    except ValueError:
        return None


def fmt_clock(seconds: int | None) -> str:
    """3:05 or 1:02:03; blank when unknown."""
    if not seconds or seconds < 0:
        return ""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_total(seconds: int | None) -> str:
    """'1 h 19 min', '13 min 54 s' or ''."""
    if not seconds or seconds < 0:
        return ""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h} h {m} min"
    return f"{m} min {s} s" if m else f"{s} s"


# --- khinsider -------------------------------------------------------------------------------------------
KHI_HOST = "downloads.khinsider.com"
KHI_URL = "https://" + KHI_HOST
_KHI_ALBUM_PATH = "/game-soundtracks/album/"
_KHI_AUDIO = re.compile(r"\.(?:mp3|flac|m4a|ogg|opus|wav)(?:$|\?)", re.IGNORECASE)
_KHI_SIZE = re.compile(r"^\s*([\d.,]+)\s*(KB|MB|GB)\s*$", re.IGNORECASE)
_KHI_TIME = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*$")


# khinsider turned away the app's own User-Agent with HTTP 403 (reported from a real run), so its pages are asked for
# the way a browser asks. A rule the person has set for the host (Settings > network rules) wins over these.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": KHI_URL + "/",
}


def khinsider_headers(url: str) -> dict[str, str]:
    """Browser-like headers for khinsider addresses (pages and covers); nothing for any other site."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host != KHI_HOST and not host.endswith(".khinsider.com"):
        return {}
    return {k: v for k, v in BROWSER_HEADERS.items() if k not in RULES.headers_for(url)}


class ChallengeError(LinkError):
    """khinsider answered with a Cloudflare 'Just a moment...' check, which only a real browser can pass."""


CHALLENGE_COOLDOWN = 300.0  # seconds khinsider is left alone after a refusal: asking again at once only repeats it
_KHI_BLOCK = {"until": 0.0}
_CHALLENGE_TEXT = "just a moment"
CHALLENGE_MESSAGE = ("khinsider is behind a Cloudflare check that plain Python cannot pass. Install the optional "
                     "'curl_cffi' package (pip install curl_cffi) and restart Distill to get past it. Until then "
                     "khinsider is skipped and archive.org is searched.")
CHALLENGE_STILL_MESSAGE = ("khinsider still refused Distill with a browser-like connection (Cloudflare check), so it is "
                           "skipped for a few minutes. archive.org is still searched.")


def is_challenge(headers, body: bytes | str = b"") -> bool:
    """A Cloudflare managed-challenge response: its header says so, or the page is the 'Just a moment...' one."""
    try:
        if str(headers.get("cf-mitigated", "")).lower() == "challenge":
            return True
    except AttributeError:
        pass
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    return _CHALLENGE_TEXT in text[:4000].lower() and "cloudflare" in text.lower()


_CFFI_LOCAL = threading.local()


def _cffi_session():
    """A Chrome-impersonating HTTP session (one per thread), or None when the optional `curl_cffi` package is not
    installed. Cloudflare's check turns plain Python away by the way it connects (its TLS fingerprint); this connects
    the way Chrome does."""
    session = getattr(_CFFI_LOCAL, "session", None)
    if session is not None:
        return session
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None
    session = cffi_requests.Session(impersonate="chrome")
    _CFFI_LOCAL.session = session
    return session


def curl_cffi_available() -> bool:
    return _cffi_session() is not None


def _fetch_via_cffi(session, url: str, timeout: float) -> str:
    # The browser identity comes from `impersonate`; only a rule the person set for the host is added to it.
    try:
        resp = session.get(url, headers=dict(RULES.headers_for(url)) or None, timeout=timeout)
        body = resp.content[:6_000_000]
        status, headers = resp.status_code, resp.headers
    except Exception as exc:  # noqa: BLE001 - curl_cffi raises its own error types
        raise LinkError(f"khinsider: {exc}") from exc
    if is_challenge(headers, body[:8000]):
        _KHI_BLOCK["until"] = time.time() + CHALLENGE_COOLDOWN
        raise ChallengeError(CHALLENGE_STILL_MESSAGE)
    if status == 403:
        raise LinkError("khinsider refused the request (HTTP 403)")
    if status == 429:
        raise LinkError("khinsider is rate limiting requests (HTTP 429): wait a bit and try again")
    if status >= 400:
        raise LinkError(f"khinsider: HTTP {status}")
    return body.decode("utf-8", errors="replace")


def khinsider_wait_seconds() -> int:
    """How long khinsider is being left alone after a refusal (0 when it is not)."""
    return max(0, int(_KHI_BLOCK["until"] - time.time() + 0.999))


def reset_khinsider_block() -> None:
    """Ask khinsider again at once (after the person has added a cookie rule, say)."""
    _KHI_BLOCK["until"] = 0.0


def fetch_text(url: str, timeout: float = 30) -> str:
    """GET a page as text. LinkError (with a readable message) on any network problem."""
    khi = bool(khinsider_headers(url))
    if khi and time.time() < _KHI_BLOCK["until"]:
        raise ChallengeError(CHALLENGE_MESSAGE if _cffi_session() is None else CHALLENGE_STILL_MESSAGE)
    if khi and (session := _cffi_session()) is not None:
        return _fetch_via_cffi(session, url, timeout)
    try:
        with http_open(url, headers=khinsider_headers(url) or None, timeout=timeout) as resp:
            raw = resp.read(6_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        body = b""
        if exc.code == 403 and khi:
            try:
                body = exc.read(8000)
            except Exception:  # noqa: BLE001
                pass
            if is_challenge(exc.headers or {}, body):
                _KHI_BLOCK["until"] = time.time() + CHALLENGE_COOLDOWN
                raise ChallengeError(CHALLENGE_MESSAGE) from exc
            raise LinkError("khinsider refused the request (HTTP 403)") from exc
        if exc.code == 429:
            raise LinkError("khinsider is rate limiting requests (HTTP 429): wait a bit and try again") from exc
        raise LinkError(describe_error(exc)) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise LinkError(describe_error(exc)) from exc
    if khi and is_challenge({}, raw[:4000]):  # a challenge page served with a 200
        _KHI_BLOCK["until"] = time.time() + CHALLENGE_COOLDOWN
        raise ChallengeError(CHALLENGE_MESSAGE)
    return raw.decode(charset, errors="replace")


class _TableReader(html.parser.HTMLParser):
    """Reads every table of a page into rows of cells: each cell keeps its text, its links and its pictures."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []  # {"id", "class", "rows": [[cell, ...], ...]}
        self._table: dict | None = None
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._link: list | None = None  # [href, text parts] of the link being read

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._table = {"id": a.get("id", ""), "class": a.get("class", ""), "rows": []}
            self.tables.append(self._table)
        elif tag == "tr" and self._table is not None:
            self._row = []
            self._table["rows"].append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": [], "links": [], "imgs": [], "th": tag == "th"}
            self._row.append(self._cell)
        elif self._cell is not None and tag == "a" and a.get("href"):
            self._link = [a["href"], []]
            self._cell["links"].append(self._link)
        elif self._cell is not None and tag == "img" and a.get("src"):
            self._cell["imgs"].append(a["src"])

    def handle_endtag(self, tag):
        if tag == "a":
            self._link = None
        elif tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = None
        elif tag == "table":
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"].append(data)
            if self._link is not None:
                self._link[1].append(data)


def _cell_text(cell: dict) -> str:
    return re.sub(r"\s+", " ", "".join(cell["text"])).strip()


def _read_tables(page: str) -> list[dict]:
    reader = _TableReader()
    try:
        reader.feed(page)
        reader.close()
    except Exception:  # a broken page gives what it gave so far
        pass
    return reader.tables


def _khi_bytes(text: str) -> int | None:
    m = _KHI_SIZE.match(text)
    if not m:
        return None
    try:
        return int(float(m[1].replace(",", "")) * {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[m[2].upper()])
    except ValueError:
        return None


def _khi_seconds(text: str) -> int | None:
    m = _KHI_TIME.match(text)
    return int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3]) if m else None


def _khi_abs(href: str, base: str = KHI_URL) -> str:
    url = urllib.parse.urljoin(base, html.unescape(href.strip()))
    return urllib.parse.quote(url, safe=":/%?&=+~#@!$',;[]()*")  # raw spaces etc. only; existing %xx stays


def is_khinsider_page(url: str) -> bool:
    """A khinsider track page (not yet a file): it has to be opened to find the real file address."""
    parts = urllib.parse.urlsplit(url)
    return (parts.hostname or "").lower() == KHI_HOST and _KHI_ALBUM_PATH in parts.path and bool(_KHI_AUDIO.search(parts.path))


def parse_khinsider_search(page: str) -> list[dict]:
    """[{slug, title, detail, art}] from a khinsider search page, in the order the site lists them."""
    out, seen = [], set()
    for table in _read_tables(page):
        if "albumList" not in table["class"]:
            continue
        for row in table["rows"]:
            link = next(((h, t) for cell in row for h, t in cell["links"] if _KHI_ALBUM_PATH in h
                         and "".join(t).strip()), None)
            if link is None:
                continue
            slug = urllib.parse.urlsplit(link[0]).path.rstrip("/").rpartition("/")[2]
            if not slug or slug in seen:
                continue
            seen.add(slug)
            art = next((src for cell in row for src in cell["imgs"]), "")
            others = [_cell_text(c) for c in row if not any(_KHI_ALBUM_PATH in h for h, _ in c["links"])]
            detail = " \u00b7 ".join(t for t in others if t)
            out.append({"slug": slug, "title": html.unescape("".join(link[1])).strip(), "detail": detail,
                        "art": _khi_abs(art) if art else ""})
    return out


def parse_khinsider_album(page: str, slug: str, prefer: str = "best") -> tuple[list[Track], str, dict]:
    """(tracks, cover address, info) from an album page. Tracks keep their khinsider page address as the url: the
    file address is only found when a track is played or saved (`resolve_url`). FLAC is chosen when the album
    has it, unless `prefer` is 'mp3'."""
    base = f"{KHI_URL}{_KHI_ALBUM_PATH}{slug}"
    heads: list[str] = []
    rows: list[list[dict]] = []
    for table in _read_tables(page):
        if table["id"] == "songlist":
            for row in table["rows"]:
                if row and all(c["th"] for c in row):
                    if not heads:
                        heads = [_cell_text(c).lower() for c in row]
                else:
                    rows.append(row)
            break
    has_flac = any("flac" in h for h in heads)
    tracks: list[Track] = []
    for row in rows:
        song = next(((h, t) for cell in row for h, t in cell["links"] if _KHI_AUDIO.search(h)), None)
        if song is None:
            continue
        title = html.unescape("".join(song[1])).strip() or posixpath.basename(urllib.parse.unquote(song[0]))
        texts = [_cell_text(c) for c in row]
        nums = [int(t.rstrip(".")) for t in texts if re.fullmatch(r"\d{1,3}\.?", t)]
        sizes = [b for t in texts if (b := _khi_bytes(t)) is not None]
        if not has_flac and len(sizes) >= 2:
            has_flac = True
        mp3 = sizes[0] if sizes else None
        flac = sizes[1] if len(sizes) > 1 else None
        seconds = next((sec for t in texts if (sec := _khi_seconds(t)) is not None), None)
        use_flac = has_flac and prefer != "mp3"
        disc, number = (nums[0], nums[1]) if len(nums) >= 2 else (None, nums[0] if nums else None)
        ext = ".flac" if use_flac else ".mp3"
        tracks.append(Track(_khi_abs(song[0], base + "/"), ext, title, number, flac if use_flac else mp3, None, "",
                            use_flac, disc, seconds))
    cover = ""
    if m := re.search(r'class="albumImage"[^>]*>.*?<a[^>]+href="([^"]+)"', page, re.DOTALL | re.IGNORECASE):
        cover = _khi_abs(m[1])
    elif m := re.search(r'class="albumImage"[^>]*>.*?<img[^>]+src="([^"]+)"', page, re.DOTALL | re.IGNORECASE):
        cover = _khi_abs(m[1])
    info = {"title": "", "year": "", "platforms": "", "publisher": "", "type": "", "added": "", "related": []}
    start = re.search(r'id="pageContent"', page, re.IGNORECASE)  # the album's own heading, not one in the site's menus
    content = page[start.start():] if start else page
    if m := re.search(r"<h2[^>]*>(.*?)</h2>", content, re.DOTALL | re.IGNORECASE):
        info["title"] = html.unescape(re.sub(r"<[^>]+>", "", m[1])).strip()
    if block := re.search(r'<p align="left">(.*?)</p>', content, re.DOTALL | re.IGNORECASE):
        meta = block[1]
        if m := re.search(r"Platforms?:(.*?)<br", meta, re.DOTALL | re.IGNORECASE):
            names = re.findall(r"<a[^>]*>(.*?)</a>", m[1], re.DOTALL)
            info["platforms"] = ", ".join(html.unescape(n).strip() for n in names) or html.unescape(re.sub(r"<[^>]+>", "", m[1])).strip()
        for key, pattern in (("year", r"Year:\s*<b>\s*(\d{4})"), ("publisher", r"Published by:\s*<a[^>]*>(.*?)</a>"),
                             ("type", r"Album type:\s*<b>\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\s*</b>"),
                             ("added", r"Date Added:\s*<b>(.*?)</b>")):
            if m := re.search(pattern, meta, re.DOTALL | re.IGNORECASE):
                info[key] = html.unescape(re.sub(r"<[^>]+>", "", m[1])).strip()
    info["related"] = parse_khinsider_tiles(page, after="People who viewed this also viewed")
    return tracks, cover, info


_TILE = re.compile(r'<td class="albumIconLarge">(.*?)</td>', re.DOTALL | re.IGNORECASE)


def parse_khinsider_tiles(page: str, after: str = "") -> list[dict]:
    """[{slug, title, art}] from the cover tiles khinsider shows under 'Top 12 Albums' and 'People who viewed this also
    viewed'. `after`: only the tiles after this heading text."""
    if after:
        at = page.find(after)
        if at < 0:
            return []
        page = page[at:]
    out, seen = [], set()
    for tile in _TILE.findall(page):
        link = re.search(r'href="[^"]*?/game-soundtracks/album/([^"/?#]+)"', tile)
        if not link or link[1] in seen:
            continue
        seen.add(link[1])
        img = re.search(r'<img[^>]+src="([^"]+)"', tile)
        label = re.search(r"<p>(.*?)</p>", tile, re.DOTALL)
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", label[1] if label else ""))).strip()
        out.append({"slug": link[1], "title": title, "art": _khi_abs(img[1]) if img else ""})
    return out


def parse_khinsider_toplist(page: str) -> list[dict]:
    """[{rank, slug, title, art}] from a ranked list (/top40, /all-time-top-1000, ...): table#top40."""
    out = []
    for table in _read_tables(page):
        if table["id"] != "top40":
            continue
        for row in table["rows"]:
            link = next(((h, t) for cell in row for h, t in cell["links"] if _KHI_ALBUM_PATH in h and "".join(t).strip()), None)
            if link is None:
                continue
            rank = next((int(t.rstrip(".")) for t in (_cell_text(c) for c in row) if re.fullmatch(r"\d{1,5}\.?", t)), len(out) + 1)
            art = next((src for cell in row for src in cell["imgs"]), "")
            out.append({"rank": rank, "slug": urllib.parse.urlsplit(link[0]).path.rstrip("/").rpartition("/")[2],
                        "title": html.unescape("".join(link[1])).strip(), "art": _khi_abs(art) if art else ""})
    return out


def parse_khinsider_pages(page: str) -> tuple[int, int]:
    """(this page, how many pages) from the 'Page 1 of 620' counter; (1, 1) when there is none."""
    m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", page)
    return (int(m[1]), int(m[2])) if m else (1, 1)


def parse_khinsider_consoles(page: str) -> list[tuple[str, str, int]]:
    """(slug, name, album count) of every platform on /console-list."""
    return [(m[1], html.unescape(m[2]).strip(), int(m[3])) for m in
            re.finditer(r'<a href="/game-soundtracks/([a-z0-9-]+)">([^<]+)</a>\s*\((\d+)\)', page)]


_KHI_FILE_LINK = re.compile(r'(?:href|src)="(https?://[^"]+?\.(?:flac|mp3|m4a|ogg|opus|wav)(?:\?[^"]*)?)"', re.IGNORECASE)


def parse_khinsider_track(page: str, prefer: str = "best") -> str | None:
    """The file address on a track page: the FLAC link when there is one (and `prefer` allows it), else the MP3."""
    links = [html.unescape(m[1]) for m in _KHI_FILE_LINK.finditer(page)]
    if not links:
        return None
    flac = next((u for u in links if urllib.parse.urlsplit(u).path.lower().endswith(".flac")), None)
    mp3 = next((u for u in links if urllib.parse.urlsplit(u).path.lower().endswith(".mp3")), None)
    pick = (mp3 or links[0]) if prefer == "mp3" else (flac or mp3 or links[0])
    return urllib.parse.quote(pick, safe=":/%?&=+~#@!$',;[]()*")


_RESOLVED: dict[tuple[str, str], str] = {}
_RESOLVE_LOCK = threading.Lock()


def resolve_url(url: str, prefer: str = "best") -> str:
    """The address a track really streams or downloads from. Anything that is not a khinsider track page is
    already one. Raises LinkError when the page has no file on it."""
    if not is_khinsider_page(url):
        return url
    key = (url, prefer)
    with _RESOLVE_LOCK:
        if key in _RESOLVED:
            return _RESOLVED[key]
    direct = parse_khinsider_track(fetch_text(url), prefer)
    if not direct:
        raise LinkError("khinsider: no file on that track's page")
    with _RESOLVE_LOCK:
        _RESOLVED[key] = direct
    return direct


def resolve_files(files: list[RemoteFile], prefer: str = "best", workers: int = 4,
                  should_stop: Callable[[], bool] | None = None) -> tuple[list[RemoteFile], list[str]]:
    """The same files with their real addresses. A few at a time (kind to the site). (files, problems): a file that
    could not be resolved is left out and named in problems."""
    todo = [f for f in files if is_khinsider_page(f.url)]
    if not todo:
        return list(files), []
    direct: dict[int, str] = {}
    problems: list[str] = []

    def one(f: RemoteFile) -> None:
        if should_stop and should_stop():
            return
        for attempt in range(3):
            try:
                direct[id(f)] = resolve_url(f.url, prefer)
                return
            except LinkError as exc:
                last = str(exc)
                time.sleep(0.5 * (attempt + 1))
        problems.append(f"{posixpath.basename(f.name)}: {last}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, todo))
    out = []
    for f in files:
        if not is_khinsider_page(f.url):
            out.append(f)
        elif id(f) in direct:
            out.append(dataclasses.replace(f, url=direct[id(f)]))
    return out, problems


def snapshot_for_quality(tracks: list[Track], prefer: str) -> list[Track] | None:
    """A saved track list is kept in Best form. For MP3 only, khinsider tracks are turned into MP3 ones (the file
    address is looked up at play time, so only the label and size change); a list from anywhere else cannot be turned
    and None says to list the album afresh."""
    if prefer != "mp3":
        return tracks
    if not tracks or not all(is_khinsider_page(t.url) for t in tracks):
        return None
    return [dataclasses.replace(t, ext=".mp3", lossless=False, size=None) for t in tracks]


class KHInsiderProvider:
    name = "khinsider"

    def search(self, game: str) -> list[Album]:
        text = search_name(game)
        page = fetch_text(f"{KHI_URL}/search?" + urllib.parse.urlencode({"search": text}))
        albums = []
        for d in parse_khinsider_search(page):
            score = max(match_score(game, d["title"]), 0.1)  # the site already judged it a hit
            albums.append(Album(self.name, d["slug"], d["title"], d["detail"], score, d["art"]))
        return albums

    def tracks(self, album: Album, prefer: str = "best") -> list[Track]:
        if prefer in album.cache:
            return album.cache[prefer]
        slug = urllib.parse.quote(album.ident, safe="")
        tracks, cover, info = parse_khinsider_album(fetch_text(f"{KHI_URL}{_KHI_ALBUM_PATH}{slug}"), album.ident, prefer)
        if not tracks:
            raise LinkError(f"{album.title}: no audio files found in it")
        if cover:
            album.art = cover
        if not album.detail:
            album.detail = " \u00b7 ".join(x for x in (info["platforms"], info["type"], info["year"]) if x)
        album.cache[prefer] = tracks
        album.cache["info"] = info  # publisher, type, date added, and the 'also viewed' albums
        return tracks

    # ---- browsing (the Media page's Home tab) ----
    LISTS = {"top40": ("Top 40 this week", "/top40"), "newly": ("Top 100 newly added", "/top-100-newly-added"),
             "recent": ("Top 100 of the last 6 months", "/last-6-months-top-100"),
             "alltime": ("All-time top 1000", "/all-time-top-1000")}

    def ranked(self, kind: str, page: int = 1) -> tuple[list[Album], int]:
        # (named 'ranked', not 'top': Python's symtable treats a scope called 'top' as the module, which fools the
        # static-names test)
        """(albums, pages) of one ranked list, best first."""
        path = self.LISTS[kind][1] + (f"?page={page}" if page > 1 else "")
        text = fetch_text(KHI_URL + path)
        albums = [Album(self.name, d["slug"], d["title"], "", 1.0, d["art"]) for d in parse_khinsider_toplist(text)]
        return albums, parse_khinsider_pages(text)[1]

    def latest(self, page: int = 1) -> tuple[list[Album], int]:
        """(albums, pages) of the newest uploads, newest first."""
        return self.browse("/", page)

    def browse(self, path: str, page: int = 1) -> tuple[list[Album], int]:
        """(albums, pages) of a list page: the home page, a platform, a year, a publisher or a letter."""
        sep = "&" if "?" in path else "?"
        text = fetch_text(KHI_URL + path + (f"{sep}page={page}" if page > 1 else ""))
        albums = [Album(self.name, d["slug"], d["title"], d["detail"], 1.0, d["art"]) for d in parse_khinsider_search(text)]
        return albums, parse_khinsider_pages(text)[1]

    def consoles(self) -> list[tuple[str, str, int]]:
        """(slug, name, album count) of every platform khinsider has, from its console list."""
        return parse_khinsider_consoles(fetch_text(KHI_URL + "/console-list"))

    def related(self, album: Album) -> list[Album]:
        """Albums people who viewed this one also viewed (from its page; listed once and kept)."""
        if "info" not in album.cache:
            self.tracks(album)
        return [Album(self.name, d["slug"], d["title"], "", 1.0, d["art"]) for d in album.cache["info"]["related"]]


# --- matching an album to a game's platform and year -------------------------------------------------------
_GENERIC_PLATFORM_WORDS = frozenset({"nintendo", "sony", "sega", "microsoft", "entertainment", "system", "console", "the",
                                     "video", "game", "computer"})
_PLATFORM_ALIASES = (  # (what the name contains, what it stands for)
    ("super nintendo", "snes"), ("super famicom", "snes"), ("super nes", "snes"),
    ("nintendo entertainment system", "nes"), ("famicom", "nes"),
    ("mega drive", "genesis"), ("megadrive", "genesis"), ("sega genesis", "genesis"),
    ("game boy advance", "gba"), ("gameboy advance", "gba"), ("game boy color", "gbc"), ("game boy", "gb"),
    ("gamecube", "gamecube"), ("game cube", "gamecube"),
    ("playstation portable", "psp"), ("playstation vita", "vita"), ("psx", "playstation"), ("ps1", "playstation"),
    ("ps2", "playstation 2"), ("ps3", "playstation 3"), ("ps4", "playstation 4"), ("ps5", "playstation 5"),
    ("playstation one", "playstation"), ("nintendo switch", "switch"), ("microsoft windows", "pc"), ("windows", "pc"),
)
PLATFORM_BONUS = 0.10  # added to the match score when the album is for the game's platform
YEAR_BONUS = 0.05  # ... and when its year is within a year of the game's


def platform_tokens(name: str) -> frozenset[str]:
    """The words that say which platform this is: 'Sony PlayStation 4' and 'PS4' both give {playstation, 4}."""
    low = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    low = f" {re.sub(r'  +', ' ', low).strip()} "
    for text, stands_for in _PLATFORM_ALIASES:
        low = low.replace(f" {text} ", f" {stands_for} ")
    return frozenset(w for w in low.split() if w not in _GENERIC_PLATFORM_WORDS)


def same_platform(a: str, b: str) -> bool:
    ta, tb = platform_tokens(a), platform_tokens(b)
    if not ta or not tb:
        return False
    nums_a, nums_b = {w for w in ta if w.isdigit()}, {w for w in tb if w.isdigit()}
    if nums_a and nums_b and nums_a != nums_b:  # PlayStation 2 is not PlayStation 4
        return False
    return bool((ta - nums_a) & (tb - nums_b)) or (ta == tb)


def split_detail(detail: str) -> tuple[str, int]:
    """(platform, year) out of an album's detail line ('PS3, PS4 \u00b7 Arrangement \u00b7 2020'). The platform keeps
    its commas when the album is for several."""
    parts = [p.strip() for p in (detail or "").split("\u00b7") if p.strip()]
    year = next((int(p) for p in parts if re.fullmatch(r"(?:19|20)\d\d", p)), 0)
    platform = next((p for p in parts if not re.fullmatch(r"(?:19|20)\d\d", p)), "")
    return platform, year


def rank_for_game(albums: list[Album], platform: str = "", year: str | int = "") -> list[Album]:
    """The same albums with the ones for the game's platform (and release year) moved up. Only a small bonus, so a
    better title match on another platform still wins over a poor one on the right platform."""
    try:
        want_year = int(year)
    except (TypeError, ValueError):
        want_year = 0
    if not platform and not want_year:
        return list(albums)

    def key(a: Album) -> float:
        album_platform, album_year = split_detail(a.detail)
        listed = [p.strip() for p in album_platform.split(",") if p.strip()]
        bonus = PLATFORM_BONUS if platform and any(same_platform(platform, p) for p in listed) else 0.0
        if want_year and album_year and abs(album_year - want_year) <= 1:
            bonus += YEAR_BONUS
        return a.score + bonus

    return sorted(albums, key=key, reverse=True)


_MERGE_NOISE = frozenset({"original", "soundtrack", "ost", "game", "music", "official", "complete", "the", "of", "video"})


def album_merge_key(title: str) -> str:
    """Titles that name the same release collapse to one key ('Celeste Original Soundtrack' and 'Celeste' match)."""
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split() if w not in _MERGE_NOISE]
    return " ".join(words)


# --- albums saved on disk ---------------------------------------------------------------------------------
AUDIO_EXTS = frozenset({".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"})
LOCAL_SOURCE = "local"
_LOCAL_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg")
_DISC_FOLDER = re.compile(r"(?:cd|dis[ck])\s*0*(\d+)", re.IGNORECASE)


def is_local_path(url: str) -> bool:
    """A file on this computer rather than an address on the net."""
    return bool(url) and not url.lower().startswith(("http://", "https://"))


def _audio_files(folder: Path, depth: int = 2) -> list[Path]:
    found: list[Path] = []
    try:
        for entry in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            if entry.is_file() and entry.suffix.lower() in AUDIO_EXTS:
                found.append(entry)
            elif entry.is_dir() and depth > 0:
                found += _audio_files(entry, depth - 1)
    except OSError:
        pass
    return found


def _local_album(folder: Path, title: str) -> Album | None:
    files = _audio_files(folder)
    if not files:
        return None
    art = next((str(folder / n) for n in _LOCAL_COVER_NAMES if (folder / n).is_file()), "")
    album = Album(LOCAL_SOURCE, str(folder), title, "", 1.0, art)
    album.playable = len(files)
    return album


def scan_local_albums(standard_dir: Path | None, games_dir: Path | None = None, max_depth: int = 4) -> list[Album]:
    """Albums already saved: each folder of <standard_dir>/Soundtracks, and each 'soundtrack' folder under the games
    directory (named for its game). Albums are listed by title."""
    seen: set[str] = set()
    albums: list[Album] = []

    def add(folder: Path, title: str) -> None:
        key = str(folder.resolve()).casefold()
        if key in seen:
            return
        seen.add(key)
        if album := _local_album(folder, title):
            albums.append(album)

    if standard_dir is not None:
        root = standard_dir / STANDARD_ROOT
        try:
            for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
                if child.is_dir():
                    add(child, child.name)
        except OSError:
            pass
    if games_dir is not None and games_dir.is_dir():
        def walk(folder: Path, depth: int) -> None:
            try:
                children = [c for c in folder.iterdir() if c.is_dir()]
            except OSError:
                return
            for child in children:
                if child.name.casefold() == SOUNDTRACK_FOLDER:
                    add(child, folder.name)
                elif depth < max_depth:
                    walk(child, depth + 1)
        walk(games_dir, 1)
    albums.sort(key=lambda a: a.title.casefold())
    return albums


class LocalProvider:
    """Albums saved on disk. They are found by `scan_local_albums`, never by a search."""
    name = LOCAL_SOURCE

    def search(self, game: str) -> list[Album]:
        return []

    def tracks(self, album: Album, prefer: str = "best") -> list[Track]:
        folder = Path(album.ident)
        files = _audio_files(folder)
        if not files:
            raise LinkError(f"{album.title}: no audio files in {folder}")
        tracks = []
        for path in files:
            number, title = tidy_title(path.stem)
            disc_match = _DISC_FOLDER.search(path.parent.name) if path.parent != folder else None
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            tracks.append(Track(str(path), path.suffix.lower(), title, number, size, None, "",
                                path.suffix.lower() == ".flac", int(disc_match[1]) if disc_match else None))
        return tracks


# --- pasted links ----------------------------------------------------------------------------------------
class LinkProvider:
    """Not searched: the person pastes the link. Anything the Download page understands works (archive.org item or
    folder, an open directory listing); audio files in it are re-filed under the game's folder."""
    name = "link"

    def __init__(self, resolver: Callable[[str], tuple[list[RemoteFile], list[str]]] | None = None):
        self._resolver = resolver

    def search(self, game: str) -> list[Album]:
        return []

    def album_for(self, link: str) -> Album:
        try:
            parsed = parse_archive_url(link)
        except LinkError:
            parsed = None
        return Album(self.name, link, link, "pasted link", 1.0, archive_art_url(parsed[0]) if parsed else "")

    def tracks(self, album: Album, prefer: str = "best") -> list[Track]:
        resolver = self._resolver
        if resolver is None:
            from .links import resolve_links  # deferred: links pulls in the torrent engine
            resolver = resolve_links
        files, errors = resolver(album.ident)
        if errors and not files:
            raise LinkError(errors[0])
        if cover := pick_cover((rf.name, rf.size) for rf in files if not rf.extra):
            album.art = next(rf.url for rf in files if rf.name == cover and not rf.extra)
        tracks = []
        for rf in files:
            ext = posixpath.splitext(rf.name)[1].lower()
            if ext not in AUDIO_EXTS or rf.extra:
                continue
            sub = posixpath.dirname(rf.name)
            disc, rest = split_disc(posixpath.splitext(posixpath.basename(rf.name))[0])
            number, title = tidy_title(rest)
            tracks.append(Track(rf.url, ext, title, number, rf.size, rf.md5, (sub + "/") if sub else "",
                                ext in LOSSLESS, disc))
        if not tracks:
            raise LinkError("No audio files found at that link")
        if prefer == "mp3" and any(t.ext == ".mp3" for t in tracks):
            tracks = [t for t in tracks if t.ext == ".mp3"]
        return tracks


# --- putting it together ---------------------------------------------------------------------------------
DEFAULT_PROVIDERS: tuple[Provider, ...] = (KHInsiderProvider(), ArchiveOrgProvider())  # streaming and first choice for saving: khinsider


@dataclass(eq=False, slots=True)
class GameResult:
    request: GameRequest
    albums: list[Album] = field(default_factory=list)  # best match first
    error: str = ""

    @property
    def best(self) -> Album | None:
        """The album worth ticking for the person: only when it matches well."""
        return self.albums[0] if self.albums and self.albums[0].score >= MATCH_MIN else None


PLAYABLE_BONUS = 0.08  # ranking nudge for albums with separate tracks (expandable and previewable), and against ones without
VERIFY_TOP = 3  # how many good matches per game get their track list checked


def _rank_playable(albums: list[Album], providers) -> list[Album]:
    """Check the track lists of the best few matches and favour the ones that are split into separate tracks, since
    those can be expanded and previewed. A single-file album or a bare zip is ranked below a track-by-track one of
    about the same match. Never reorders anything below MATCH_MIN, so a poor match is still never chosen for you."""
    good = [a for a in albums if a.score >= MATCH_MIN]
    for a in good[:VERIFY_TOP]:
        provider = next((p for p in providers if p.name == a.provider), None)
        if provider is None:
            continue
        try:
            a.playable = len(provider.tracks(a))
        except LinkError as exc:
            text = str(exc).lower()
            a.playable = 0 if ("no audio" in text or "no files" in text) else None
        except Exception:  # a surprise in one item must not sink the whole search
            a.playable = None

    def key(a: Album) -> float:
        if a.playable is None:
            return a.score
        return a.score + (PLAYABLE_BONUS if a.playable >= 2 else -PLAYABLE_BONUS)

    head = sorted(good[:VERIFY_TOP], key=key, reverse=True)
    return head + good[VERIFY_TOP:] + [a for a in albums if a.score < MATCH_MIN]


CACHE_FILE = paths.cache_dir() / "distill_soundtrack_cache.json"
CACHE_DAYS = 3
CACHE_MAX = 400


def _cache_key(query: str, providers) -> str:
    return query.casefold().strip() + "|" + "+".join(sorted(p.name for p in providers))


def _load_cache() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    oldest = time.time() - CACHE_DAYS * 86400
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("t", 0) >= oldest}


def _save_cache(cache: dict) -> None:
    if len(cache) > CACHE_MAX:  # keep the newest
        cache = dict(sorted(cache.items(), key=lambda kv: kv[1].get("t", 0), reverse=True)[:CACHE_MAX])
    try:
        paths.ensure_parent(CACHE_FILE)
        CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass  # a convenience; never interrupt a search for it


def clear_search_cache() -> None:
    try:
        CACHE_FILE.unlink()
    except OSError:
        pass


def _albums_to_json(albums: list[Album]) -> list[dict]:
    return [{"provider": a.provider, "ident": a.ident, "title": a.title, "detail": a.detail, "score": a.score,
             "art": a.art, "playable": a.playable} for a in albums]


def _albums_from_json(rows: list) -> list[Album]:
    out = []
    for d in rows:
        try:
            out.append(Album(d["provider"], d["ident"], d["title"], d.get("detail", ""), float(d["score"]),
                             d.get("art", ""), d.get("playable")))
        except (KeyError, TypeError, ValueError):
            return []  # a damaged entry: search again instead
    return out


def m3u_text(entries: list[tuple[str, int | None, str]]) -> str:
    """An extended M3U playlist from (display title, length in seconds, file path or address) entries."""
    out = ["#EXTM3U"]
    for title, seconds, location in entries:
        clean = " ".join(str(title).split())
        out.append(f"#EXTINF:{seconds if seconds else -1},{clean}")
        out.append(location)
    return "\n".join(out) + "\n"


def browse_path(kind: str, value: str) -> str:
    """The khinsider list page for a platform ('nintendo-switch'), a year ('2016') or a letter ('A', or '0-9')."""
    if kind == "platform":
        return f"/game-soundtracks/{urllib.parse.quote(value, safe='-')}"
    if kind == "year":
        return f"/game-soundtracks/year/{int(value)}"
    if kind == "letter":
        return f"/game-soundtracks/browse/{'0-9' if value in ('0-9', '#') else value.upper()[:1]}"
    raise ValueError(kind)


def related_albums(album: Album) -> list[Album]:
    """Albums people who viewed this one also viewed: khinsider's own list, read from its page. Empty for any other source."""
    if album.provider != KHInsiderProvider.name:
        return []
    try:
        return KHInsiderProvider().related(album)
    except LinkError:
        return []


def find_albums(requests: list[GameRequest], providers=DEFAULT_PROVIDERS,
                on_result: Callable[[int, GameResult], None] | None = None,
                should_stop: Callable[[], bool] | None = None, pause: float = 0.4,
                use_cache: bool = True) -> list[GameResult]:
    """Search every provider for every game, one game at a time (kind to the sites), best matches first.
    Finished searches are remembered for a few days (`use_cache=False` searches afresh and refreshes them)."""
    results = []
    link = LinkProvider()
    cache = _load_cache()  # always loaded, so a fresh search refreshes its entry without losing the others
    dirty = False
    for i, req in enumerate(requests):
        if should_stop and should_stop():
            break
        res = GameResult(req)
        searched = False
        if req.link:
            res.albums = [link.album_for(req.link)]
        else:
            query = req.search or req.name
            key = _cache_key(query, providers)
            cached = _albums_from_json(cache[key]["albums"]) if use_cache and key in cache else []
            if cached:
                res.albums = cached
            else:
                searched = True
                problems = []
                for p in providers:
                    try:
                        res.albums += p.search(query)
                    except LinkError as exc:
                        problems.append(f"{p.name}: {exc}")
                if not any(a.score > 0 for a in res.albums) and not problems:  # nothing: try other spellings
                    for variant in fuzzy.query_variants(query, fuzzy.HISTORY.titles()):
                        for p in providers:
                            try:
                                res.albums += p.search(variant)
                            except LinkError as exc:
                                problems.append(f"{p.name}: {exc}")
                        if any(a.score > 0 for a in res.albums):
                            break
                if res.albums:  # real titles are kept for suggestions
                    fuzzy.HISTORY.add_many([fuzzy.clean_title(a.title) for a in res.albums[:3] if a.score > 0])
                res.albums.sort(key=lambda a: a.score, reverse=True)
                res.albums = [a for a in res.albums if a.score > 0]
                res.albums = _rank_playable(res.albums, providers)
                if problems:  # said even when another source did find albums: the person should know one is missing
                    res.error = "; ".join(problems)
                if res.albums and not problems:  # a partial answer is not kept: the missing source gets asked again
                    cache[key] = {"t": time.time(), "albums": _albums_to_json(res.albums)}
                    dirty = True
        results.append(res)
        if on_result:
            on_result(i, res)
        if searched and pause and i + 1 < len(requests):  # only real searches need the courtesy pause
            time.sleep(pause)
    if dirty:
        _save_cache(cache)
    return results


def files_for(game: str, album: Album, providers=DEFAULT_PROVIDERS, prefer: str = "best",
              folder: str = SOUNDTRACK_FOLDER) -> list[RemoteFile]:
    """The download-ready file list for one chosen album: `<Game>/Soundtrack/NN - Title.ext`."""
    if album.provider == LinkProvider.name:
        provider: Provider = LinkProvider()
    elif album.provider == LOCAL_SOURCE:
        provider = LocalProvider()  # type: ignore[assignment]
    else:
        provider = next((p for p in providers if p.name == album.provider), None)  # type: ignore[assignment]
        if provider is None:
            raise LinkError(f"No source called {album.provider}")
    return layout(game, provider.tracks(album, prefer), folder)


def archive_fallback(game: str, album_title: str, prefer: str = "best") -> tuple[Album, list[RemoteFile]] | None:
    """When an album cannot be saved from khinsider: the best archive.org match for the same title, with its files
    laid out for `game`. None when archive.org has nothing that matches well."""
    provider = ArchiveOrgProvider()
    try:
        hits = [a for a in provider.search(album_title) if a.score >= MATCH_MIN]
    except LinkError:
        return None
    for album in hits[:VERIFY_TOP]:
        try:
            return album, layout(game, provider.tracks(album, prefer))
        except LinkError:
            continue
    return None


def already_there(dest: Path | None, game: str, mode: str = SAVE_STANDARD, entries: list[GameEntry] | None = None) -> int:
    """How many audio files the game's soundtrack folder already holds (so a batch can skip finished games)."""
    root = target_folder(dest, game, mode, entries)
    if root is None:
        return 0
    try:
        return sum(1 for p in root.rglob("*") if p.suffix.lower() in AUDIO_EXTS)
    except OSError:
        return 0


def target_folder(dest: Path | None, game: str, mode: str, entries: list[GameEntry] | None = None) -> Path | None:
    """Where this game's tracks go (None when unknown: RomM mode with no single matching game)."""
    if dest is None:
        return None
    if mode == SAVE_ROMM:
        hits = find_matches(game, entries if entries is not None else scan_games(dest))
        return hits[0].folder / SOUNDTRACK_FOLDER if len(hits) == 1 else None
    return dest / STANDARD_ROOT / safe_name(game)


# --- save plans ------------------------------------------------------------------------------------------
@dataclass(eq=False, slots=True)
class SavePlan:
    dest: Path
    files: list[RemoteFile] = field(default_factory=list)
    by_game: dict[str, list[RemoteFile]] = field(default_factory=dict)
    moves: dict[str, list[tuple]] = field(default_factory=dict)  # game -> organize ops (loose game files into a folder)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (game, why)
    ambiguous: dict[str, list[GameEntry]] = field(default_factory=dict)

    def drop(self, game: str) -> None:
        gone = self.by_game.pop(game, [])
        self.files = [f for f in self.files if not any(f is g for g in gone)]
        self.moves.pop(game, None)


def _rest(name: str) -> str:
    """'<Game>/soundtrack/CD1/01 - A.flac' -> 'CD1/01 - A.flac'"""
    parts = name.split("/", 2)
    return parts[2] if len(parts) == 3 else parts[-1]


def plan_save(items: list[tuple[str, list[RemoteFile]]], mode: str, dest: Path, entries: list[GameEntry] | None = None,
              picks: dict[str, GameEntry] | None = None) -> SavePlan:
    """Where each game's tracks go, and which loose games must first be moved into a folder of their own.

    Standard: `<dest>/Soundtracks/<Game>/NN - Title.ext`.
    RomM: `<games dir>/.../<Game>/soundtrack/NN - Title.ext`, beside the game. A loose game file gets a folder named
    like it first. No match, several matches, or a folder already in the way: that game is skipped, never guessed.
    `dest` is the Save-to folder (Standard) or the games directory (RomM). Nothing is touched on disk here."""
    plan = SavePlan(dest)
    if mode == SAVE_ROMM and entries is None:
        entries = scan_games(dest)
    made: set[str] = set()
    for game, files in items:
        if mode != SAVE_ROMM:
            prefix = f"{STANDARD_ROOT}/{safe_name(game)}"
        else:
            hits = [picks[game]] if picks and game in picks else find_matches(game, entries or [])
            if not hits:
                plan.skipped.append((game, "no game with this name in the games directory"))
                continue
            if len(hits) > 1:
                plan.skipped.append((game, f"{len(hits)} games match this name"))
                plan.ambiguous[game] = hits
                continue
            entry = hits[0]
            if not entry.is_dir:
                if entry.folder.exists() or str(entry.folder).casefold() in made:
                    plan.skipped.append((game, f"'{entry.folder.name}' already exists there"))
                    continue
                made.add(str(entry.folder).casefold())
                plan.moves[game] = [("move", str(f), str(entry.folder / f.name)) for f in entry.files]
            try:
                prefix = f"{entry.folder.relative_to(dest).as_posix()}/{SOUNDTRACK_FOLDER}"
            except ValueError:
                plan.skipped.append((game, "it is outside the games directory"))
                plan.moves.pop(game, None)
                continue
        mapped = [dataclasses.replace(f, name=f"{prefix}/{_rest(f.name)}") for f in files]
        plan.by_game[game] = mapped
        plan.files += mapped
    return plan


# --- remembered choices ----------------------------------------------------------------------------------
PREFS_FILE = paths.config_dir() / "distill_soundtracks.json"


def load_prefs() -> dict:
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict) -> None:
    try:
        paths.ensure_parent(PREFS_FILE)
        PREFS_FILE.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError:
        pass  # a convenience; never interrupt a save for it


# --- undoing a save --------------------------------------------------------------------------------------
SAVES_FILE = paths.state_dir() / "distill_soundtrack_saves.json"
SAVES_KEEP = 10


def _read_saves() -> list[dict]:
    try:
        data = json.loads(SAVES_FILE.read_text(encoding="utf-8"))
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_saves(saves: list[dict]) -> None:
    try:
        paths.ensure_parent(SAVES_FILE)
        SAVES_FILE.write_text(json.dumps(saves[-SAVES_KEEP:]), encoding="utf-8")
    except OSError:
        pass


def record_save(tracks: list[Path], root: Path, moved: bool) -> None:
    """Remember what one save created (only files that did not exist before), so it can be undone."""
    saves = _read_saves()
    saves.append({"root": str(root), "tracks": [str(p) for p in tracks], "moved": moved})
    _write_saves(saves)


def add_to_last_save(paths: list[Path]) -> None:
    """Add files made after the save started (a cover, say) to the last record."""
    saves = _read_saves()
    if saves:
        saves[-1]["tracks"] += [str(p) for p in paths]
        _write_saves(saves)


def saves_depth() -> int:
    return len(_read_saves())


def undo_last_save() -> tuple[int, int, list[str]] | None:
    """Undo the most recent save: delete the files it created (and folders left empty), then put back any games it
    moved into folders. Returns (files removed, games put back, problems), or None when there is nothing to undo."""
    saves = _read_saves()
    if not saves:
        return None
    last = saves.pop()
    _write_saves(saves)
    root, removed, problems = Path(last.get("root", "")), 0, []
    folders: set[Path] = set()
    for raw in last.get("tracks", []):
        path = Path(raw)
        for victim in (path, path.with_name(path.name + ".part")):
            try:
                if victim.is_file():
                    victim.unlink()
                    removed += victim == path
            except OSError as exc:
                problems.append(f"{victim.name}: {exc}")
        folders.add(path.parent)
    for folder in sorted(folders, key=lambda f: len(f.parts), reverse=True):  # deepest first
        while folder != root and root in folder.parents:
            try:
                folder.rmdir()  # only succeeds when empty
            except OSError:
                break
            folder = folder.parent
    restored = 0
    if last.get("moved"):
        from . import organize

        res = organize.undo_last("soundtrack")
        if res is not None:
            restored, more = res
            problems += list(more)
    return removed, restored, problems
