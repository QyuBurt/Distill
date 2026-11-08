"""Torrent search: sources that need no setup (Knaben, torrents-csv) plus Prowlarr when the person has one.

Nothing here scrapes a web page. Every source answers with JSON, so a site redesign can't break it; if a source is
down, the others still answer and the failure is reported as a note. Results from all sources are merged by info-hash.
What a source sends back is untrusted: names are cleaned, numbers are clamped, and the only thing taken from a result
that is ever added is its 40-character info-hash, from which the magnet link is built here (never a link a source sent).
No Qt in here (tests/test_structure.py keeps it that way)."""
from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

from . import paths
from .constants import APP_DIR as APP_DIR  # retained for callers using the legacy app location
from .downloads import describe_error, http_open, LOGINS, opener_for, USER_AGENT

# Public sources. Tuples, so a mirror can be added without touching the code that uses them.
KNABEN_URLS = ("https://api.knaben.org/v1", "https://api.knaben.eu/v1")  # an aggregator of many trackers (older address last)
TORRENTS_CSV_URLS = ("https://torrents-csv.com",)  # open-source torrent database (also a Prowlarr indexer)

TRACKERS_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
TRACKERS_FILE = paths.cache_dir() / "distill_trackers.txt"  # the last copy of that list that was downloaded
TRACKERS_MAX_AGE = 7 * 24 * 3600  # seconds before it is fetched again
MAGNET_TRACKERS = 8  # how many trackers a magnet we build carries (DHT does the rest)
# Used until the list above has been fetched once (and whenever it can't be).
BUNDLED_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://explodie.org:6969/announce",
)
MAX_TORRENT_FILE = 8 * 1024 * 1024  # a .torrent bigger than this isn't one
MAX_REPLY = 16 * 1024 * 1024  # no search answer is bigger than this
MAX_NAME = 300  # characters of a title that are kept


class SearchError(Exception):
    """One source couldn't answer (the message is shown to the person)."""


@dataclass(eq=False, slots=True)
class SearchResult:
    name: str
    infohash: str = ""  # 40 hex characters, lower case ('' when the source gave only a .torrent link)
    size: int = 0
    seeders: int = 0
    leechers: int = 0
    added: int = 0  # unix time the torrent was published (0 = unknown)
    sources: list[str] = field(default_factory=list)  # every source that listed it
    magnet: str = ""  # the source's own magnet link, if it gave one
    torrent_url: str = ""  # a .torrent download link (Prowlarr), used only when there is no hash or magnet
    private: bool = False  # from a private tracker: its own tracker must stay the only one in its magnet


# ---- helpers ---------------------------------------------------------------------------------------------------
def _int(value) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _text(value, limit: int = MAX_NAME) -> str:
    """A title from a source: control characters removed, whitespace collapsed, cut to a sane length."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).split())[:limit]


def _hash_of(value: str) -> str:
    """Info-hash from a bare hash or a magnet link (hex or base-32 spelling); '' if there isn't a usable one."""
    value = (value or "").strip()
    if value.lower().startswith("magnet:"):
        m = re.search(r"xt=urn:btih:([^&]+)", value, re.I)
        value = m.group(1) if m else ""
    if re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", value):
        with contextlib.suppress(ValueError):
            return base64.b32decode(value.upper()).hex()
    return ""


def _json(url: str, headers: dict[str, str] | None = None, timeout: float = 15, body: dict | None = None):
    """GET (or, with `body`, POST as JSON) and decode the JSON answer. Goes through the app's proxy rules."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    try:
        with opener_for(url).open(urllib.request.Request(url, data=data, headers=hdrs), timeout=timeout) as resp:
            raw = resp.read(MAX_REPLY + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SearchError(describe_error(exc)) from None
    if len(raw) > MAX_REPLY:
        raise SearchError("sent a reply that was too big")
    try:
        return json.loads(raw)
    except ValueError:
        raise SearchError("sent a reply that couldn't be read") from None


# ---- trackers --------------------------------------------------------------------------------------------------
def parse_trackers(text: str) -> list[str]:
    seen: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if re.match(r"(udp|http|https)://", line, re.I) and line not in seen:
            seen.append(line)
    return seen


def trackers() -> list[str]:
    """The trackers to put in a magnet we build: ngosang/trackerslist if it has been fetched, else the bundled few."""
    with contextlib.suppress(OSError):
        if found := parse_trackers(TRACKERS_FILE.read_text(encoding="utf-8")):
            return found[:MAGNET_TRACKERS]
    return list(BUNDLED_TRACKERS[:MAGNET_TRACKERS])


def refresh_trackers() -> None:
    """Fetch the tracker list when the saved copy is missing or a week old. Never raises: it is only an extra."""
    with contextlib.suppress(Exception):
        if TRACKERS_FILE.exists() and time.time() - TRACKERS_FILE.stat().st_mtime < TRACKERS_MAX_AGE:
            return
        with http_open(TRACKERS_URL, timeout=10) as resp:
            text = resp.read(256 * 1024).decode("utf-8", "replace")
        if len(parse_trackers(text)) >= 3:
            paths.ensure_parent(TRACKERS_FILE)
            TRACKERS_FILE.write_text(text, encoding="utf-8")


def build_magnet(infohash: str, name: str = "", extra_trackers: bool = True) -> str:
    parts = [f"magnet:?xt=urn:btih:{infohash}"]
    if name:
        parts.append("dn=" + urllib.parse.quote(name, safe=""))
    if extra_trackers:
        parts += ["tr=" + urllib.parse.quote(t, safe="") for t in trackers()]
    return "&".join(parts)


# ---- the sources: a parser (pure) and a fetcher each ---------------------------------------------------------------
def parse_torrents_csv(data) -> list[SearchResult]:
    rows = data.get("torrents") if isinstance(data, dict) else data  # older servers answer with a bare list
    out: list[SearchResult] = []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        ih = _hash_of(str(item.get("infohash", "")))
        if not ih:
            continue
        out.append(SearchResult(name=_text(item.get("name")) or ih, infohash=ih,
                                size=_int(item.get("size_bytes")), seeders=_int(item.get("seeders")),
                                leechers=_int(item.get("leechers")), added=_int(item.get("created_unix")),
                                sources=["Torrents-CSV"]))
    return out


def parse_prowlarr(data) -> list[SearchResult]:
    out: list[SearchResult] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or str(item.get("protocol", "torrent")).lower() != "torrent":
            continue
        magnet = str(item.get("magnetUrl") or "")
        link = str(item.get("downloadUrl") or "")
        if not magnet.lower().startswith("magnet:"):
            magnet = link if link.lower().startswith("magnet:") else ""
        ih = _hash_of(str(item.get("infoHash") or "")) or _hash_of(magnet)
        if not (ih or magnet or link):
            continue
        added = 0
        with contextlib.suppress(ValueError, TypeError):
            added = int(datetime.fromisoformat(str(item.get("publishDate", "")).replace("Z", "+00:00")).timestamp())
        flags = [str(f).lower() for f in item.get("indexerFlags") or []]
        out.append(SearchResult(name=_text(item.get("title")) or ih or "(untitled)", infohash=ih,
                                size=_int(item.get("size")), seeders=_int(item.get("seeders")),
                                leechers=_int(item.get("leechers")), added=max(0, added),
                                sources=[_text(item.get("indexer"), 40) or "Prowlarr"], magnet=magnet,
                                torrent_url="" if magnet or ih else link,
                                private=str(item.get("privacy", "")).lower() == "private" or "internal" in flags))
    return out


def parse_knaben(data) -> list[SearchResult]:
    """Knaben's answer is {"hits": [...]}. Only rows with a real info-hash are kept: some of its sources are listed
    with a download link that goes through Knaben itself, and those can't be checked, so they are not offered."""
    hits = data.get("hits") if isinstance(data, dict) else None
    out: list[SearchResult] = []
    for item in hits if isinstance(hits, list) else []:
        if not isinstance(item, dict):
            continue
        ih = _hash_of(str(item.get("hash") or "")) or _hash_of(str(item.get("magnetUrl") or ""))
        if not ih or set(ih) == {"0"}:
            continue
        added = 0
        with contextlib.suppress(ValueError, TypeError):
            added = int(datetime.fromisoformat(str(item.get("date", "")).replace("Z", "+00:00")).timestamp())
        out.append(SearchResult(name=_text(item.get("title")) or ih, infohash=ih, size=_int(item.get("bytes")),
                                seeders=_int(item.get("seeders")), leechers=_int(item.get("peers")),
                                added=max(0, added), sources=["Knaben"]))
    return out


def _first_working(urls: tuple[str, ...], path: str = "", body: dict | None = None) -> object:
    last = "no address"
    for base in urls:
        try:
            return _json(base + path, body=body)
        except SearchError as exc:
            last = str(exc)
    raise SearchError(last)


def search_knaben(query: str) -> list[SearchResult]:
    return parse_knaben(_first_working(KNABEN_URLS, body={
        "search_type": "100%", "search_field": "title", "query": query,
        "order_by": "seeders", "order_direction": "desc", "size": 100}))


def search_torrents_csv(query: str) -> list[SearchResult]:
    if len(query) < 3:  # the server refuses anything shorter
        return []
    return parse_torrents_csv(_first_working(TORRENTS_CSV_URLS,
                                             "/service/search?" + urllib.parse.urlencode({"q": query, "size": 100})))


def prowlarr_config() -> tuple[str, str] | None:
    """(address, API key) from the Accounts page, or None when Prowlarr isn't set up."""
    cred = LOGINS.get("prowlarr") or {}
    url, key = str(cred.get("url", "")).strip(), str(cred.get("apikey", "")).strip()
    if not (url and key):
        return None
    if "://" not in url:
        url = "http://" + url
    return url.rstrip("/"), key


def search_prowlarr(query: str, url: str, key: str) -> list[SearchResult]:
    path = "/api/v1/search?" + urllib.parse.urlencode({"query": query, "type": "search", "indexerIds": -2})
    try:
        data = _json(url + path, {"X-Api-Key": key}, timeout=60)  # it queries every indexer, so it can be slow
    except SearchError as exc:
        text = str(exc)
        if "401" in text or "403" in text:
            raise SearchError("rejected the API key (Settings > Accounts)") from None
        raise
    return parse_prowlarr(data)


# ---- putting it together -------------------------------------------------------------------------------------------
def merge(results: list[SearchResult]) -> list[SearchResult]:
    """One row per torrent: the same info-hash from several sources is joined (the highest counts win)."""
    merged: dict[str, SearchResult] = {}
    for r in results:
        key = r.infohash or f"{r.name.lower()}|{r.size}"
        have = merged.get(key)
        if have is None:
            merged[key] = r
            continue
        have.seeders, have.leechers = max(have.seeders, r.seeders), max(have.leechers, r.leechers)
        have.added = have.added or r.added
        have.size = have.size or r.size
        have.magnet = have.magnet or r.magnet
        have.torrent_url = have.torrent_url or r.torrent_url
        have.private = have.private or r.private
        have.sources += [s for s in r.sources if s not in have.sources]
    return sorted(merged.values(), key=lambda r: (-r.seeders, -r.leechers, r.name.lower()))


def search(query: str) -> tuple[list[SearchResult], list[str]]:
    """Ask every source at once -> (merged results, one note per source that failed). Blocking: run it on a thread."""
    query = " ".join(query.split())
    if not query:
        return [], []
    threading.Thread(target=refresh_trackers, daemon=True).start()  # for the next search; this one uses what it has
    jobs = {"Knaben": lambda: search_knaben(query), "Torrents-CSV": lambda: search_torrents_csv(query)}
    if cfg := prowlarr_config():
        jobs["Prowlarr"] = lambda: search_prowlarr(query, *cfg)
    found: list[SearchResult] = []
    notes: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(fn): name for name, fn in jobs.items()}
        for fut in concurrent.futures.as_completed(futures):
            try:
                found += fut.result()
            except SearchError as exc:
                notes.append(f"{futures[fut]}: {exc}")
            except Exception as exc:  # a source must never take the others down with it
                notes.append(f"{futures[fut]}: {exc}")
    return merge(found), sorted(notes)


# ---- turning a result into something the download list takes ---------------------------------------------------------
def torrent_infohash(data: bytes) -> str:
    """The info-hash of a .torrent file's bytes (SHA-1 of its bencoded 'info' dictionary)."""
    span: list[int] = []

    def parse(i: int, top: bool = False) -> int:  # returns the index just after the value that starts at i
        c = data[i:i + 1]
        if c == b"i":
            return data.index(b"e", i) + 1
        if c in (b"l", b"d"):
            i += 1
            while data[i:i + 1] != b"e":
                if c == b"d":
                    n = data.index(b":", i)
                    start = n + 1 + int(data[i:n])
                    key, i = data[n + 1:start], start
                    begin = i
                    i = parse(i)
                    if top and key == b"info":
                        span[:] = [begin, i]
                else:
                    i = parse(i)
            return i + 1
        if c.isdigit():
            n = data.index(b":", i)
            return n + 1 + int(data[i:n])
        raise ValueError("not bencode")

    try:
        if data[:1] != b"d":
            raise ValueError("not a torrent")
        parse(0, top=True)
    except (ValueError, IndexError):
        raise SearchError("that isn't a valid .torrent file") from None
    if not span:
        raise SearchError("that .torrent file has no info section")
    return hashlib.sha1(data[span[0]:span[1]]).hexdigest()


def to_magnet(result: SearchResult) -> str:
    """The magnet link to add for this result. A result that only has a .torrent link (Prowlarr) is downloaded here to
    read its hash; the indexer may also answer with a magnet redirect, which is accepted too. Blocking (network)."""
    if result.magnet:
        return result.magnet  # the indexer's own (a private tracker's already carries its tracker; public ones are never added)
    if result.infohash:
        return build_magnet(result.infohash, result.name, extra_trackers=not result.private)
    if not result.torrent_url:
        raise SearchError(f"{result.name}: no link to add")
    try:
        with http_open(result.torrent_url, timeout=30) as resp:
            data = resp.read(MAX_TORRENT_FILE + 1)
    except urllib.error.HTTPError as exc:
        where = exc.headers.get("Location", "") if exc.headers else ""
        exc.close()
        if where.lower().startswith("magnet:"):  # urllib refuses to follow that, but it is exactly what we want
            return where
        raise SearchError(describe_error(exc)) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SearchError(describe_error(exc)) from None
    if len(data) > MAX_TORRENT_FILE:
        raise SearchError("that .torrent file is too big")
    if data.lower().startswith(b"magnet:"):
        return data.decode("utf-8", "replace").strip()
    return build_magnet(torrent_infohash(data), result.name, extra_trackers=not result.private)
