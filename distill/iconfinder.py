"""Finding a game's icon online, from the same place Eglantine's metadata editor looks first: switchiconshowdown.com. When
that has nothing alike, SteamGridDB's icons are tried (the same API key the Recomps page uses).

The site is a catalogue of Switch home-menu icons, searchable by name (it has no title IDs, so the title is what is matched).
`search.php?query=<name>` lists matches that link to `detail.php?id=<n>`, and the full icon of each is `images/<n>.png`. No Qt here.

A search is polite and bounded: one request per name tried, a hard deadline for the whole lookup, and a cancel flag the
caller can set. Nothing is cached beyond the downloaded picture, which the caller saves where it likes."""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .constants import APP_NAME, APP_VERSION

SITE = "https://www.switchiconshowdown.com"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
DEADLINE_SECONDS = 10.0  # the whole lookup, as in Eglantine's
MAX_BYTES = 8 << 20  # an icon is a few hundred KB at most
UNTITLED_LOOKUPS = 5  # detail pages read at most, to name results whose search page gave no title
MIN_SCORE = 0.6  # how alike a result's title and the wanted one must be to be taken

_LINK_RE = re.compile(r'<a\b([^>]*?href="[^"]*detail\.php\?id=(\d+)[^"]*"[^>]*)>(.*?)</a>', re.I | re.S)
_TITLE_ATTR_RE = re.compile(r'(?:title|alt)="([^"]*)"', re.I)
_PAGE_TITLE_RE = re.compile(r"<title>\s*(?:Switch Icon Showdown!)?\s*(.*?)\s*</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


SGDB_API = "https://www.steamgriddb.com/api/v2"
SOURCE_SHOWDOWN, SOURCE_SGDB = "Switch Icon Showdown", "SteamGridDB"
SGDB_ICON_QUERY = "types=static&mimes=image/png&nsfw=false&humor=false"  # /icons takes png or .ico only: image/jpeg is a 400
MIN_SGDB_SIDE = 128  # an icon smaller than this is only taken when nothing bigger exists


@dataclass(frozen=True, slots=True)
class IconHit:
    icon_id: int
    title: str
    score: float = 0.0
    source: str = SOURCE_SHOWDOWN
    image_url: str = ""  # where the picture is, when it is not on Switch Icon Showdown

    @property
    def url(self) -> str:
        return self.image_url or f"{SITE}/images/{self.icon_id}.png"

    @property
    def file_stem(self) -> str:
        """A name for the cached picture that cannot clash between the two sources."""
        return f"{'sgdb' if self.source == SOURCE_SGDB else 'showdown'}_{self.icon_id}"


class IconLookupError(Exception):
    """The site could not be reached or answered something unusable (the message is for the person)."""


def normalize(title: str) -> str:
    """A title as compared: lower case, trademark signs and punctuation dropped, '&' as 'and', spaces collapsed."""
    text = html.unescape(title).casefold().replace("&", " and ")
    text = re.sub(r"[™®©:'’`\-–—_!?.,()\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def score(wanted: str, found: str) -> float:
    """0..1: 1 for the same title, then by how much of the wanted words the result has (and the reverse), so 'Hades' does not
    take 'Hades II'. Words are compared as sets, which is enough for titles."""
    a, b = normalize(wanted), normalize(found)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    wa, wb = set(a.split()), set(b.split())
    common = len(wa & wb)
    if not common:
        return 0.0
    recall, precision = common / len(wa), common / len(wb)
    return 2 * recall * precision / (recall + precision) * 0.95  # never as good as an exact match


def parse_results(page: str) -> list[tuple[int, str]]:
    """(icon id, title) of every result on a search page, in page order, each id once. A result's title is the picture's
    title / alt text (or the link's own), else the link's text; '' when the page gives none (search() then asks the detail
    page). A later copy of an id fills in a title the first one lacked."""
    order: list[int] = []
    titles: dict[int, str] = {}
    for m in _LINK_RE.finditer(page):
        icon_id = int(m.group(2))
        attr = _TITLE_ATTR_RE.search(m.group(3)) or _TITLE_ATTR_RE.search(m.group(1))
        title = html.unescape(attr.group(1)).strip() if attr else html.unescape(_TAG_RE.sub("", m.group(3))).strip()
        if icon_id not in titles:
            order.append(icon_id)
            titles[icon_id] = title
        elif not titles[icon_id] and title:
            titles[icon_id] = title
    return [(i, titles[i]) for i in order]


def page_title(page: str) -> str:
    """The game's name from a detail page's <title> ('Switch Icon Showdown! <name>')."""
    m = _PAGE_TITLE_RE.search(page)
    return html.unescape(m.group(1)).strip() if m else ""


def _get(url: str, timeout: float, who: str = "switchiconshowdown.com") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            data = resp.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        raise IconLookupError(f"Could not reach {who} ({getattr(e, 'reason', e)}).") from e
    if len(data) > MAX_BYTES:
        raise IconLookupError("The icon was larger than expected, so it was not downloaded.")
    return data


def search(name: str, timeout: float = DEADLINE_SECONDS) -> list[IconHit]:
    """Matches for a name, best first (those that are not alike enough are left out). A result the search page gives no
    title for is named by its detail page (a few at most), so a page layout without alt text still works."""
    end = time.monotonic() + timeout
    page = _get(f"{SITE}/search.php?query={urllib.parse.quote(name)}", timeout).decode("utf-8", "replace")
    pairs = parse_results(page)
    untitled = [i for i, t in pairs if not t][:UNTITLED_LOOKUPS]
    named: dict[int, str] = {}
    for icon_id in untitled:
        if time.monotonic() >= end:
            break
        try:
            detail = _get(f"{SITE}/detail.php?id={icon_id}", end - time.monotonic()).decode("utf-8", "replace")
        except IconLookupError:
            continue
        named[icon_id] = page_title(detail)
    hits = [IconHit(i, t or named.get(i, ""), 0.0) for i, t in pairs]
    hits = [IconHit(h.icon_id, h.title, score(name, h.title)) for h in hits if h.title]
    hits = [h for h in hits if h.score >= MIN_SCORE]
    return sorted(hits, key=lambda h: -h.score)  # sorted() is stable: the site's own order breaks ties


_TAGS_RE = re.compile(r"\s*[\[(][^\])]*[\])]")  # '[0100...]', '[v0]', '(Update)'
_SEPARATORS_RE = re.compile(r"[:\-–—™®©]+")


def name_variants(names: list[str]) -> list[str]:
    """The names to search, in order: each as given, then without file tags ('[0100...][v0]'), then without punctuation that
    a catalogue may spell differently ('Zelda: Link' / 'Zelda Link'). The typed name comes first and stays first; no name is
    tried twice."""
    out: list[str] = []
    seen: set[str] = set()
    cleaned = [re.sub(r"\s+", " ", _TAGS_RE.sub("", n)).strip() for n in names]
    for group in (names, cleaned, [re.sub(r"\s+", " ", _SEPARATORS_RE.sub(" ", c)).strip() for c in cleaned]):
        for n in group:
            n = n.strip()
            key = n.casefold()
            if n and key not in seen:
                seen.add(key)
                out.append(n)
    return out


def sgdb_key() -> str:
    """The SteamGridDB key: the one saved from the Recomps page, else the built-in one, else $SGDB_API_KEY. '' = none."""
    try:
        from . import recomps
        return recomps.Store().sgdb_key()
    except Exception:  # a missing or unreadable store must never stop the lookup
        return os.environ.get("SGDB_API_KEY", "")


def _sgdb(path: str, key: str, timeout: float) -> list[dict]:
    req = urllib.request.Request(SGDB_API + path, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                           "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read(MAX_BYTES + 1)
        data = json.loads(raw)
    except urllib.error.HTTPError as e:  # before URLError, which it is a kind of
        if e.code in (401, 403):
            raise IconLookupError("SteamGridDB rejected the API key.") from e
        if e.code == 429:
            raise IconLookupError("SteamGridDB is busy right now. Try again in a minute.") from e
        detail = ""
        try:
            body = json.loads(e.read(2000) or b"{}")
            errs = body.get("errors") if isinstance(body, dict) else None
            detail = f": {'; '.join(map(str, errs))}" if errs else ""
        except Exception:  # the body is a courtesy; the code is what matters
            pass
        raise IconLookupError(f"SteamGridDB answered {e.code}{detail}.") from e
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        raise IconLookupError(f"Could not reach SteamGridDB ({getattr(e, 'reason', e)}).") from e
    out = data.get("data") if isinstance(data, dict) else None
    return [d for d in out if isinstance(d, dict)] if isinstance(out, list) else []


def _square(icon: dict) -> bool:
    w, h = icon.get("width") or 0, icon.get("height") or 0
    return bool(w and h and abs(w - h) <= max(2, 0.03 * max(w, h)))


def _is_picture(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"


def _find_showdown(names: list[str], cancel: threading.Event | None, deadline: float) -> tuple[bytes, IconHit] | None:
    """The best match on Switch Icon Showdown for the first name (typed name, then the game's) that finds one alike enough.
    A name whose search fails is skipped, not fatal; the failure is only raised when no name found anything."""
    end = time.monotonic() + deadline
    tried: set[str] = set()
    error: IconLookupError | None = None
    for name in name_variants(names):
        key = normalize(name)
        if not key or key in tried:
            continue
        tried.add(key)
        if (cancel and cancel.is_set()) or time.monotonic() >= end:
            break
        try:
            hits = search(name, end - time.monotonic())
            for best in hits[:3]:  # the best match may be a dead image: the next best
                if (cancel and cancel.is_set()) or time.monotonic() >= end:
                    return None
                try:
                    data = _get(best.url, end - time.monotonic())
                except IconLookupError:
                    continue
                if _is_picture(data):
                    return data, best
        except IconLookupError as e:
            error = error or e
    if error is not None:
        raise error
    return None


def _find_sgdb(names: list[str], key: str, cancel: threading.Event | None, deadline: float,
               min_score: float = MIN_SCORE) -> tuple[bytes, IconHit] | None:
    """SteamGridDB's icons for the first name that finds a game alike enough. Square png icons, the community's best-scored
    first, and a real size over a tiny one. A refused key or a busy server stops the search; any other failed request is
    skipped (and raised only when nothing was found)."""
    end = time.monotonic() + deadline
    tried: set[str] = set()
    error: IconLookupError | None = None

    def stop() -> bool:
        return bool((cancel and cancel.is_set()) or time.monotonic() >= end)

    def call(path: str) -> list[dict]:
        nonlocal error
        try:
            return _sgdb(path, key, end - time.monotonic())
        except IconLookupError as e:
            if "API key" in str(e) or "busy" in str(e):
                raise
            error = error or e
            return []

    for name in name_variants(names):
        norm = normalize(name)
        if not norm or norm in tried:
            continue
        tried.add(norm)
        if stop():
            return None
        games = call("/search/autocomplete/" + urllib.parse.quote(name, safe=""))
        ranked = sorted(((score(name, str(g.get("name", ""))), g) for g in games if isinstance(g.get("id"), int)),
                        key=lambda sg: (-sg[0], not sg[1].get("verified")))
        for sc, game in [r for r in ranked if r[0] >= min_score][:3]:
            if stop():
                return None
            icons = call(f"/icons/game/{game['id']}?{SGDB_ICON_QUERY}")
            icons = [x for x in icons if isinstance(x.get("url"), str) and isinstance(x.get("id"), int)]
            icons.sort(key=lambda x: (not _square(x), min(x.get("width") or 0, x.get("height") or 0) < MIN_SGDB_SIDE,
                                      -(x.get("score") or 0)))
            for icon in icons[:3]:
                if stop():
                    return None
                try:
                    data = _get(icon["url"], end - time.monotonic(), "SteamGridDB")
                except IconLookupError:
                    continue  # one bad picture: the next best
                if _is_picture(data):
                    return data, IconHit(icon["id"], str(game.get("name", name)), sc, SOURCE_SGDB, icon["url"])
    if error is not None:
        raise error
    return None


def find_icon(names: list[str], cancel: threading.Event | None = None,
              deadline: float = DEADLINE_SECONDS) -> tuple[bytes, IconHit] | None:
    """Try each name in turn (the typed name first, then the file's) on Switch Icon Showdown, within one overall deadline,
    then, when it has nothing alike (or can't be reached) and a SteamGridDB key is set, on SteamGridDB within a deadline of
    its own. Returns the picture of the best match with what matched; None when nothing alike was found. Raises
    IconLookupError only when nothing was found AND a site could not be used (unreachable, key refused)."""
    problems: list[str] = []
    try:
        if found := _find_showdown(names, cancel, deadline):
            return found
        showdown = "Switch Icon Showdown had nothing alike"
    except IconLookupError as e:
        showdown = f"Switch Icon Showdown: {e}"  # SteamGridDB may still have it
    if cancel and cancel.is_set():
        return None
    key = sgdb_key()
    if key:
        try:
            if found := _find_sgdb(names, key, cancel, deadline):
                return found
        except IconLookupError as e:
            problems.append(f"{showdown}, and {e}" if showdown.endswith("alike") else f"{showdown} {e}")
    if problems:
        raise IconLookupError(problems[0])
    if not showdown.endswith("alike"):
        raise IconLookupError(showdown)
    return None
