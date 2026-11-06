"""Soundtrack pulling: a game's name -> candidate albums -> a track list laid out as
`<Game>/soundtrack/01 - Title.flac`, ready for the Download page's runner (and for RomM to find next to the game).

Sources are pluggable `Provider`s. A provider only has to find albums and list their tracks; naming, numbering,
disc folders, matching and the folder layout are done here once. Shipped providers:

  ArchiveOrgProvider  searches archive.org's audio items for the game's soundtrack
  LinkProvider        a link you paste yourself (archive.org item, open directory, ...), re-filed into the game's folder

No Qt in here. Everything that touches the network goes through `fetch_json` / `resolve_links`, so tests swap those out.
"""
from __future__ import annotations

import dataclasses
import difflib
import http.client
import json
import posixpath
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from .constants import APP_DIR
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
    core, _ = title_core(text)
    return [w for w in core.split() if w not in NOISE_WORDS]


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
            files.append(RemoteFile(f"{base}/{folder_part}{name}", t.url, t.size, "", t.md5))
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
                                ext in LOSSLESS, disc))
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
DEFAULT_PROVIDERS: tuple[Provider, ...] = (ArchiveOrgProvider(),)


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


CACHE_FILE = APP_DIR / "distill_soundtrack_cache.json"
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
                res.albums.sort(key=lambda a: a.score, reverse=True)
                res.albums = [a for a in res.albums if a.score > 0]
                res.albums = _rank_playable(res.albums, providers)
                if not res.albums and problems:
                    res.error = "; ".join(problems)
                elif res.albums:
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
    else:
        provider = next((p for p in providers if p.name == album.provider), None)  # type: ignore[assignment]
        if provider is None:
            raise LinkError(f"No source called {album.provider}")
    return layout(game, provider.tracks(album, prefer), folder)


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
PREFS_FILE = APP_DIR / "distill_soundtracks.json"


def load_prefs() -> dict:
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict) -> None:
    try:
        PREFS_FILE.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError:
        pass  # a convenience; never interrupt a save for it


# --- undoing a save --------------------------------------------------------------------------------------
SAVES_FILE = APP_DIR / "distill_soundtrack_saves.json"
SAVES_KEEP = 10


def _read_saves() -> list[dict]:
    try:
        data = json.loads(SAVES_FILE.read_text(encoding="utf-8"))
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_saves(saves: list[dict]) -> None:
    try:
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
