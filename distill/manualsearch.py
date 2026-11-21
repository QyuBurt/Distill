"""Finding a game's manual online, and downloading it.

The source is the Internet Archive, which holds a great many scanned game manuals and has a public search and download interface (no key,
no account). A search looks for items about the game that are manuals or instruction booklets, ranks them by how closely the title matches
(with the platform's name counting for the right one and guides, walkthroughs and catalogues counting against), and finds the PDF in each.
Two kinds of thing are searched on the Archive: single items that are one manual, and the "manual packs" (collection manual_packs: one item per
platform holding a PDF for every game, named the way ROM sets are, e.g. "Legend of Zelda, The - A Link to the Past (USA).pdf"). The packs are
the better source when they have the game, because their file names can be matched to the game's own name exactly. More sources can be added:
`search` just collects `Manual` results from each. Nothing is downloaded until the player picks one.

Network access is passed in (`get`), so the searching and ranking are tested here without the network; `download` streams a file with
progress, a size limit and cancelling, and refuses anything that isn't a PDF. No Qt here.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from . import games as gm
from .constants import APP_NAME

SEARCH_URL = "https://archive.org/advancedsearch.php"
META_URL = "https://archive.org/metadata/"
DOWNLOAD_URL = "https://archive.org/download/"
DETAILS_URL = "https://archive.org/details/"
USER_AGENT = f"{APP_NAME}-manuals"
PACKS_QUERY = "collection:manual_packs"
PACK_LISTING_DAYS = 7  # how long a pack's list of files is kept before it is read again
MAX_BYTES = 250 << 20  # a manual bigger than this is not offered
MAX_RESULTS = 10  # manuals offered
MIN_SCORE = 0.42  # below this a result is probably another game
SEARCH_ROWS = 25  # items asked for per search; the best few are looked into for their PDF

# Other names a platform goes by, as people write them on a scan of its manual.
PLATFORM_WORDS = {
    "NES": ["nintendo entertainment system", "nes", "famicom"], "SNES": ["super nintendo", "snes", "super famicom"],
    "Game Boy": ["game boy", "gameboy"], "Game Boy Color": ["game boy color", "gameboy color"], "Game Boy Advance": ["game boy advance", "gba"],
    "Nintendo 64": ["nintendo 64", "n64"], "GameCube": ["gamecube", "game cube"], "Wii": ["wii"], "Nintendo DS": ["nintendo ds", "nds"],
    "Nintendo 3DS": ["nintendo 3ds", "3ds"], "Mega Drive": ["genesis", "mega drive", "megadrive"], "Master System": ["master system"],
    "Game Gear": ["game gear"], "Sega CD": ["sega cd", "mega cd"], "Sega Saturn": ["saturn"], "Dreamcast": ["dreamcast"],
    "PlayStation": ["playstation", "psx", "ps1"], "PlayStation 2": ["playstation 2", "ps2"], "PlayStation 3": ["playstation 3", "ps3"],
    "PSP": ["psp", "playstation portable"], "PC Engine": ["turbografx", "pc engine"], "Atari 2600": ["atari 2600"], "Atari 7800": ["atari 7800"],
    "Atari Lynx": ["lynx"], "Xbox": ["xbox"], "Xbox 360": ["xbox 360"], "3DO": ["3do"], "Neo Geo Pocket": ["neo geo pocket"],
}
NOISE = {"manual", "manuals", "instruction", "instructions", "booklet", "game", "games", "the", "for", "of", "and", "a", "an", "video", "owner", "owners",
         "user", "guide", "usa", "us", "en", "english", "pdf", "scan", "scans", "complete", "original", "version"}
GUIDE_WORDS = ("strategy", "walkthrough", "cheat", "catalog", "catalogue", "poster", "advert", "magazine", "player's guide", "players guide", "hint book",
               "official guide", "prima", "brady", "soundtrack", "box art", "cover art", "review")


class ManualError(Exception):
    """Something went wrong finding or fetching a manual, in words the player can read."""


@dataclass(frozen=True, slots=True)
class Manual:
    title: str
    source: str  # 'Internet Archive'
    identifier: str
    url: str  # the PDF itself
    size: int  # bytes (0 when not known)
    score: float  # 0..1: how likely this is the right manual
    page: str  # a web page about it, to check before using it

    def size_text(self) -> str:
        if self.size <= 0:
            return ""
        return f"{self.size / (1 << 20):.1f} MB" if self.size >= (1 << 20) else f"{max(1, self.size // 1024)} KB"


# ---- words --------------------------------------------------------------------------------------------------------------
def _words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold().replace("&", " and ")).strip()


def clean_name(name: str) -> str:
    """A game's name as a person would search for it: no file extension, no (USA) or [!] tags, one space between words."""
    stem = gm.clean_title(name) if name else ""
    return " ".join(re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", stem).split())


def platform_words(system: str, platform: str = "") -> list[str]:
    """How the platform is written on its manuals (several ways), for searching and for ranking."""
    out = list(PLATFORM_WORDS.get(system, []))
    for extra in (system, platform):
        w = _words(extra) if extra and extra != gm.UNKNOWN else ""
        if w and w not in out:
            out.append(w)
    return out


def _strip_noise(text: str, platform_terms: list[str]) -> str:
    w = f" {_words(text)} "
    for term in sorted(platform_terms, key=len, reverse=True):
        w = w.replace(f" {_words(term)} ", " ")
    return " ".join(t for t in w.split() if t not in NOISE)


def score(game: str, platform_terms: list[str], title: str, subjects: str = "") -> float:
    """How likely an item (its title and subjects) is the manual of `game`, 0..1."""
    want, have = _strip_noise(game, platform_terms), _strip_noise(title, platform_terms)
    if not want or not have:
        return 0.0
    wanted, present = set(want.split()), set(have.split())
    cover = len(wanted & present) / len(wanted)  # how many of the game's own words the title has: "Super Mario World" is not "Super Metroid"
    if cover < 0.67:
        return 0.0
    ratio = SequenceMatcher(None, want, have).ratio() * cover**2
    if want in have:  # the game's whole name is in it (plus a region or a subtitle): as good as an exact match
        ratio = max(ratio, 0.92 if len(want) >= 0.6 * len(have) else 0.8)
    text = f" {_words(title)} {_words(subjects)} "
    bonus = 0.12 if any(f" {_words(t)} " in text for t in platform_terms if t) else 0.0
    bonus += 0.08 if re.search(r"\b(manual|instruction|booklet|owner)", text) else 0.0
    penalty = 0.25 if any(g in text for g in GUIDE_WORDS) else 0.0
    return max(0.0, min(1.0, 0.8 * ratio + bonus - penalty))


# ---- searching ----------------------------------------------------------------------------------------------------------
def build_query(game: str, platform_terms: list[str] | None = None) -> str:
    """The Internet Archive search for a game's manual (Lucene syntax)."""
    phrase = game.replace('"', " ").strip()
    q = f'mediatype:texts AND "{phrase}" AND (manual OR "instruction booklet" OR instructions OR booklet)'
    if platform_terms:
        q += " AND (" + " OR ".join(f'"{t}"' for t in platform_terms[:4]) + ")"
    return q


def _search_url(query: str) -> str:
    params = [("q", query), ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "subject"), ("rows", str(SEARCH_ROWS)), ("page", "1"),
              ("output", "json"), ("sort[]", "downloads desc")]
    return SEARCH_URL + "?" + urllib.parse.urlencode(params)


def parse_search(text: str) -> list[dict]:
    """The items in an advanced-search reply ({identifier, title, subject}); an unreadable reply is no items."""
    try:
        docs = json.loads(text)["response"]["docs"]
    except (ValueError, KeyError, TypeError):
        return []
    out = []
    for d in docs if isinstance(docs, list) else []:
        if isinstance(d, dict) and d.get("identifier"):
            subject = d.get("subject") or ""
            out.append({"identifier": str(d["identifier"]), "title": str(d.get("title") or d["identifier"]),
                        "subject": " ".join(subject) if isinstance(subject, list) else str(subject)})
    return out


def pick_pdf(files: list[dict]) -> dict | None:
    """The best PDF among an item's files: not too big, the uploader's own file before one made from it, a text PDF before a bare scan."""
    pdfs = []
    for f in files if isinstance(files, list) else []:
        name = str(f.get("name", ""))
        try:
            size = int(f.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if name.lower().endswith(".pdf") and not name.lower().endswith("_text.pdf") and size <= MAX_BYTES:
            pdfs.append((f.get("source") == "original", "text pdf" in str(f.get("format", "")).lower(), -size, name, size))
    if not pdfs:
        return None
    best = max(pdfs)
    return {"name": best[3], "size": best[4]}


def search(game: str, system: str = "", platform: str = "", get: Callable[[str], bytes] | None = None, region: str = "") -> list[Manual]:
    """Manuals online that might be `game`'s, best first: the platform's manual packs first (their file names match the game's exactly), then
    items that are one manual. `get(url)` fetches a web address (the default asks the Internet Archive); `region` is the game's, if known."""
    game = clean_name(game)
    if not game:
        return []
    fetch = get or _http_get
    terms = platform_words(system, platform)
    try:
        in_packs = search_packs(game, terms, fetch, region.casefold())
    except (urllib.error.URLError, OSError) as exc:
        raise ManualError("Couldn't reach the Internet Archive. Check your connection and try again.") from exc
    except ValueError:
        in_packs = []
    found: dict[str, dict] = {}
    try:
        for query in ([build_query(game, terms)] if terms else []) + [build_query(game)]:
            for item in parse_search(fetch(_search_url(query)).decode("utf-8", "replace")):
                found.setdefault(item["identifier"], item)
            if len(found) >= 6:  # the narrow search found enough: the wide one is for when it didn't
                break
    except (urllib.error.URLError, OSError) as exc:
        raise ManualError("Couldn't reach the Internet Archive. Check your connection and try again.") from exc
    ranked = sorted(((score(game, terms, it["title"], it["subject"]), it) for it in found.values()), key=lambda p: -p[0])
    ranked = [(s, it) for s, it in ranked if s >= MIN_SCORE][:MAX_RESULTS + 4]

    def look(pair: tuple[float, dict]) -> Manual | None:
        s, it = pair
        try:
            files = json.loads(fetch(META_URL + urllib.parse.quote(it["identifier"])).decode("utf-8", "replace")).get("files", [])
        except (ValueError, urllib.error.URLError, OSError):
            return None
        pdf = pick_pdf(files)
        if pdf is None:
            return None
        return Manual(it["title"], "Internet Archive", it["identifier"],
                      f"{DOWNLOAD_URL}{urllib.parse.quote(it['identifier'])}/{urllib.parse.quote(pdf['name'])}", pdf["size"], s,
                      DETAILS_URL + urllib.parse.quote(it["identifier"]))

    with ThreadPoolExecutor(max_workers=4) as pool:
        found_manuals = [m for m in pool.map(look, ranked) if m is not None]
    seen: set[str] = set()
    both = [m for m in in_packs + sorted(found_manuals, key=lambda m: -m.score) if not (m.url in seen or seen.add(m.url))]
    return both[:MAX_RESULTS]


# ---- the manual packs ---------------------------------------------------------------------------------------------------
_ARTICLE_SUFFIX = re.compile(r"^(?P<main>.+?), (?P<art>The|A|An)(?P<rest>(?: - .*)?)$")


def nointro_title(stem: str) -> str:
    """A ROM-set style name as a person would write it: no (USA) / (Rev 1) tags, "Legend of Zelda, The - A Link to the Past" read as
    "The Legend of Zelda - A Link to the Past"."""
    name = " ".join(re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", stem).split())
    m = _ARTICLE_SUFFIX.match(name)
    return f"{m['art']} {m['main']}{m['rest']}" if m else name


def region_of(stem: str) -> str:
    """The first (USA) / (Europe) / (Japan, USA) style tag of a ROM-set name, lower case ('' when there is none)."""
    for tag in re.findall(r"\(([^)]*)\)", stem):
        if re.fullmatch(r"(?:USA|Europe|Japan|World|Australia|Canada|Brazil|Korea|France|Germany|Spain|Italy|Asia|China|Sweden|,|\s)+", tag, re.I):
            return tag.casefold()
    return ""


def same_game(a: str, b: str) -> bool:
    """Are these two titles the same game? The main titles must be the same words; a subtitle may be missing from either ("Donkey Kong
    Country 2" is "Donkey Kong Country 2 - Diddy's Kong Quest"), but two different subtitles are two games."""
    def split(title: str) -> tuple[str, str]:
        main, _dash, sub_title = title.replace(": ", " - ").partition(" - ")
        return _words(main), _words(sub_title)

    (ma, sa), (mb, sb) = split(a), split(b)
    if ma != mb:
        return False
    return not sa or not sb or sa == sb or SequenceMatcher(None, sa, sb).ratio() >= 0.7


def match_pack(game: str, terms: list[str], files: list[dict], pack_title: str, identifier: str, region: str = "") -> list[Manual]:
    """The PDFs of a manual pack that are `game`'s manual, best first. An exact name beats a sequel that merely contains it, and a revision
    ("(Rev 1)") or another region ranks just below the plain one. `region` ('usa', 'europe'...) is the game's own, if known."""
    game = clean_name(game)
    scored: list[tuple[float, str, int]] = []
    for f in files if isinstance(files, list) else []:
        name = str(f.get("name", ""))
        if not name.lower().endswith(".pdf") or "/" in name:
            continue
        try:
            size = int(f.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MAX_BYTES:
            continue
        stem = name[:-4]
        if not same_game(game, nointro_title(stem)):
            continue  # (another game: a sequel, or one that merely shares some words)
        sc = score(game, terms, nointro_title(stem), f"manual {pack_title}")  # (the pack's own title says which platform it is for)
        if sc < MIN_SCORE:
            continue
        if want_tokens(game) != want_tokens(nointro_title(stem)):
            sc -= 0.06  # (not the same words exactly: a sequel, or an extra subtitle)
        if re.search(r"\(Rev \w+\)|\(Beta|\(Proto|\(Unl\)", stem, re.I):
            sc -= 0.03
        tag = region_of(stem)
        if region and tag and region not in tag:
            sc -= 0.04
        scored.append((sc, name, size))
    if not scored:
        return []
    best = max(s for s, _n, _z in scored)
    keep = [t for t in scored if t[0] >= best - 0.12] if best >= 0.8 else scored  # (a good one found: the sequels that contain its name are not offered)
    keep.sort(key=lambda t: -t[0])
    page = DETAILS_URL + urllib.parse.quote(identifier)
    return [Manual(name[:-4], pack_title or "Internet Archive", identifier, f"{DOWNLOAD_URL}{urllib.parse.quote(identifier)}/{urllib.parse.quote(name)}",
                   size, max(0.0, min(1.0, sc)), page) for sc, name, size in keep[:4]]


def want_tokens(title: str) -> tuple[str, ...]:
    return tuple(sorted(_words(re.sub(r"\([^)]*\)", " ", title)).split()))


def _pack_listing(identifier: str, fetch: Callable[[str], bytes]) -> list[dict]:
    """The files of a pack. Kept for a week in the temporary folder: a pack holds hundreds of files and rarely changes."""
    cache = temp_dir() / "packs"
    path = cache / (re.sub(r"[^\w.-]", "_", identifier) + ".json")
    try:
        if path.is_file() and time.time() - path.stat().st_mtime < PACK_LISTING_DAYS * 86400:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except (OSError, ValueError):
        pass
    files = json.loads(fetch(META_URL + urllib.parse.quote(identifier)).decode("utf-8", "replace")).get("files", [])
    slim = [{"name": f.get("name"), "size": f.get("size")} for f in files if isinstance(f, dict) and str(f.get("name", "")).lower().endswith(".pdf")]
    try:
        cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(slim), encoding="utf-8")
    except OSError:
        pass
    return slim


def search_packs(game: str, terms: list[str], fetch: Callable[[str], bytes], region: str = "") -> list[Manual]:
    """Look for the game's manual in the manual packs of its platform."""
    if not terms:
        return []
    query = f"{PACKS_QUERY} AND (" + " OR ".join(f'"{t}"' for t in terms[:4]) + ")"
    packs = parse_search(fetch(_search_url(query)).decode("utf-8", "replace"))[:3]
    found: list[Manual] = []
    for pack in packs:
        try:
            files = _pack_listing(pack["identifier"], fetch)
        except (ValueError, urllib.error.URLError, OSError):
            continue  # (a pack that can't be read is just not searched)
        found += match_pack(game, terms, files, pack["title"], pack["identifier"], region)
    return found


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=25) as resp:
        return resp.read()


# ---- downloading --------------------------------------------------------------------------------------------------------
def temp_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "distill_manuals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sweep_temp(max_age: float = 86400.0) -> None:
    """Remove downloaded manuals nobody used, a day after they were fetched."""
    try:
        for f in temp_dir().iterdir():
            if f.is_file() and time.time() - f.stat().st_mtime > max_age:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def download(url: str, dest: Path, progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] = lambda: False,
             limit: int = MAX_BYTES) -> Path:
    """Fetch a PDF to `dest`, reporting (bytes so far, bytes in all or 0). Raises ManualError when it can't be had, is too big, isn't a PDF, or
    `cancelled()` says stop (nothing is left behind in any of those cases)."""
    part = dest.with_name(dest.name + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            if total > limit:
                raise ManualError(f"That manual is too big ({total / (1 << 20):.0f} MB).")
            done, first = 0, True
            while chunk := resp.read(256 * 1024):
                if cancelled():
                    raise ManualError("Cancelled.")
                if first:
                    first = False
                    if not chunk.startswith(b"%PDF-"):
                        raise ManualError("That wasn't a PDF, so it wasn't kept.")
                done += len(chunk)
                if done > limit:
                    raise ManualError(f"That manual is too big (over {limit >> 20} MB).")
                out.write(chunk)
                if progress:
                    progress(done, total)
            if first:
                raise ManualError("The download was empty.")
        part.replace(dest)
        return dest
    except ManualError:
        part.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise ManualError("Couldn't download it. Check your connection and try again.") from exc
