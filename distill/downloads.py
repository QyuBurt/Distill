"""Downloads: archive.org items and plain file listings, logins, link resolving, and the download runner."""
from __future__ import annotations

import base64
import contextlib
import glob
import hashlib
import http.client
import json
import os
import posixpath
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from . import debrid, secretstore, ytdlp
from .constants import APP_DIR, APP_NAME, APP_VERSION, SUBPROCESS_FLAGS
from .models import fmt_size
from .netrules import GATE, PAUSED, RULES, THROTTLE, host_of
from .system import _Aborted


ARCHIVE_URL = "https://archive.org"


ARCHIVE_HOSTS = ("archive.org", "www.archive.org")


USER_AGENT = f"{APP_NAME}/{APP_VERSION}"


DL_CHUNK = 256 * 1024


DL_ATTEMPTS = 4  # per file; only attempts that made no progress are counted


DL_MAX_PARALLEL = 16
SEG_BLOCK = 8 * 1024 * 1024  # a segmented download is fetched in blocks this size, each its own ranged request
SEG_MIN = 32 * 1024 * 1024  # smaller files aren't worth splitting


ARCHIVE_EXTRA_SOURCES = frozenset({"derivative", "metadata"})  # files archive.org generates itself


LINKS_NEED_DEBRID = ("mega.nz", "mega.co.nz", "mega.io")  # hosts that encrypt in the browser: no direct handler


class LinkError(Exception):
    """A pasted link can't be turned into a file list (the message is shown to the user)."""


class _Short(Exception):
    """The connection ended before the whole file arrived (resumable)."""


class _NoRanges(Exception):
    """The server answered a ranged request with the whole file: it can't serve parts."""


class _BadHash(Exception):
    """The finished download doesn't match the checksum the server published."""


@dataclass(eq=False, slots=True)
class RemoteFile:
    name: str  # path relative to the item / folder, '/'-separated
    url: str
    size: int | None
    group: str = ""  # archive.org identifier or folder name
    md5: str | None = None
    extra: bool = False  # archive.org's own metadata / thumbnail / derivative files
    preselect: bool = False  # the link pointed straight at this file
    source: str = ""  # "TorBox" (cached there), "Torrent" (the swarm) or "" (a direct web download)
    fmt: str = ""  # a video / audio site row's own choice from the picker ('mp4:1080', 'mp3:320'); blank = Settings


SESSION_FILE = APP_DIR / "distill_download_session.json"  # the Download tab's list, ticks, filter and options


LOGIN_FILE = APP_DIR / "distill_logins.json"  # only written when "Remember" is ticked in the Logins window


# Sites with their own sign-in: key -> (name, hostnames, ((field, label, hidden), ...), where to find the key, required)
LOGIN_SITES = {
    "archive.org": ("archive.org", ("archive.org",),
                    (("access", "Access key", False), ("secret", "Secret key", True)),
                    "https://archive.org/account/s3.php", False),
    "1fichier": ("1fichier", ("1fichier.com",), (("apikey", "API key", True),),
                 "https://1fichier.com/console/params.pl", True),
    "torbox": ("TorBox", ("torbox.app",), (("apikey", "API key", True),),
               "https://torbox.app/settings", False),  # torrents are looked up in TorBox's cache before the swarm
    # Prowlarr (optional): more torrent sites for Search torrents. The address is the person's own server, so the
    # host below is a reserved one that no real download link can ever match (the key is never sent with a download).
    "prowlarr": ("Prowlarr", ("prowlarr.invalid",), (("url", "Address", False), ("apikey", "API key", True)),
                 "https://wiki.servarr.com/prowlarr/faq", False),
    # Debrid services: file-host links they support are exchanged for fast direct links (see debrid.py)
    **{cls.key: (cls.name, (cls.site,), (("apikey", "API key", True),), cls.where, False)
       for cls in debrid.PROVIDERS.values() if cls.links},
}


CUSTOM_KINDS = ("Basic (user + password)", "Bearer token", "Cookie")



LOGINS: dict = {}  # live logins: {"archive.org": {...}, "1fichier": {...}, "custom": [{host, kind, user, secret}]}


LOGIN_PROBLEM = ""  # set when a remembered-logins file exists but could not be unlocked (moved to another account?)


SKIPPED_LOGINS: set[str] = set()  # sites the user chose to carry on without, until the app is closed


def _host_matches(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def site_for(url: str) -> str:
    """The login this address belongs to: a LOGIN_SITES key, 'custom:<host>', or '' for everything else."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    for key, (_name, hosts, _fields, _where, _req) in LOGIN_SITES.items():
        if _host_matches(host, hosts):
            return key
    for entry in LOGINS.get("custom", []):
        if entry.get("host") and _host_matches(host, (entry["host"],)):
            return "custom:" + entry["host"]
    return ""


def login_for(url: str) -> dict | None:
    """The saved login for this address, if it has a complete one."""
    key = site_for(url)
    if key.startswith("custom:"):
        return next((e for e in LOGINS.get("custom", []) if "custom:" + e.get("host", "") == key and e.get("secret")), None)
    cred = LOGINS.get(key)
    fields = LOGIN_SITES[key][2] if key in LOGIN_SITES else ()
    return cred if cred and fields and all(cred.get(f) for f, _label, _hidden in fields) else None


def auth_headers(url: str) -> dict[str, str]:
    """Credentials are only ever built for the site they belong to (see _KeepKeysOnSite for redirects)."""
    cred, key = login_for(url), site_for(url)
    if not cred:
        return {}
    if key == "archive.org":  # S3-style keys: "Authorization: LOW access:secret"
        return {"Authorization": f"LOW {cred['access']}:{cred['secret']}"}
    if key.startswith("custom:"):
        if cred.get("kind") == CUSTOM_KINDS[1]:
            return {"Authorization": f"Bearer {cred['secret']}"}
        if cred.get("kind") == CUSTOM_KINDS[2]:
            return {"Cookie": cred["secret"]}
        token = base64.b64encode(f"{cred.get('user', '')}:{cred['secret']}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {}  # 1fichier: the API key is only used against its own API, never on file downloads


def load_logins() -> bool:
    """Read remembered logins. True if anything was remembered."""
    global LOGIN_PROBLEM
    LOGINS.clear()
    LOGINS["custom"] = []
    LOGIN_PROBLEM = ""
    try:
        data = json.loads(LOGIN_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("v") == 2:  # sealed: see secretstore.py; older files are plain and still load
            data = json.loads(secretstore.unseal(data).decode("utf-8"))
    except secretstore.SealError as exc:
        LOGIN_PROBLEM, data = str(exc), {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in LOGIN_SITES:
        if isinstance(data.get(key), dict):
            LOGINS[key] = {str(k): str(v) for k, v in data[key].items()}
    for entry in data.get("custom", []) if isinstance(data.get("custom"), list) else []:
        if isinstance(entry, dict) and entry.get("host") and entry.get("secret"):
            LOGINS["custom"].append({k: str(entry.get(k, "")) for k in ("host", "kind", "user", "secret")})
    LOGINS["custom"].sort(key=lambda e: -len(e["host"]))  # 'dl.example.com' is looked up before 'example.com'
    return any(LOGINS.get(k) for k in (*LOGIN_SITES, "custom"))


def save_logins(remember: bool) -> None:
    """Keep the logins on disk (plain text, next to the app) only while 'Remember' is ticked."""
    try:
        if remember and any(LOGINS.get(k) for k in (*LOGIN_SITES, "custom")):
            sealed = secretstore.seal(json.dumps(LOGINS).encode("utf-8"))  # plain text only where nothing better exists
            LOGIN_FILE.write_text(json.dumps(sealed), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(LOGIN_FILE, 0o600)  # owner-only where the OS supports it
        else:
            LOGIN_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def logins_missing(urls: list[str], required_only: bool = False) -> list[str]:
    """Sites among these addresses that have a sign-in option but no login yet, and weren't skipped."""
    out: list[str] = []
    for url in urls:
        key = site_for(url if "://" in url else "https://" + url)
        if (key in LOGIN_SITES and key not in out and key not in SKIPPED_LOGINS and login_for(f"https://{LOGIN_SITES[key][1][0]}/") is None
                and (LOGIN_SITES[key][4] or not required_only)):
            out.append(key)
    return out


class _KeepKeysOnSite(urllib.request.HTTPRedirectHandler):
    """urllib forwards every header on a redirect; credentials must never leave the site they belong to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            def where(u: str) -> str:
                return site_for(u) or (urllib.parse.urlsplit(u).hostname or "").lower()
            if where(newurl) != where(req.full_url):
                for name in ("Authorization", "Cookie"):
                    new.remove_header(name)
        return new


_OPENER = urllib.request.build_opener(_KeepKeysOnSite)
_PROXY_OPENERS: dict[str, urllib.request.OpenerDirector] = {}


def opener_for(url: str) -> urllib.request.OpenerDirector:
    """The opener for this address: the shared one, or one that goes through the proxy a rule names for the site
    ('direct' = no proxy at all, even if this computer has one configured)."""
    proxy = RULES.proxy_for(host_of(url))
    if not proxy:
        return _OPENER
    if proxy not in _PROXY_OPENERS:
        handler = urllib.request.ProxyHandler({} if proxy == "direct" else {"http": proxy, "https": proxy})
        _PROXY_OPENERS[proxy] = urllib.request.build_opener(_KeepKeysOnSite, handler)
    return _PROXY_OPENERS[proxy]


def http_open(url: str, headers: dict[str, str] | None = None, method: str = "GET", timeout: float = 30):
    hdrs = {"User-Agent": USER_AGENT, **(headers or {}), **auth_headers(url)}
    return opener_for(url).open(urllib.request.Request(url, headers=hdrs, method=method), timeout=timeout)


def _site_name(url: str) -> str:
    key = site_for(url)
    if key in LOGIN_SITES:
        return LOGIN_SITES[key][0]
    return key.removeprefix("custom:") or (urllib.parse.urlsplit(url).hostname or "this site")


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        url = getattr(exc, "url", "") or ""
        name, have = _site_name(url), login_for(url) is not None
        hint = {401: f"login rejected - check your {name} login (Logins...)" if have else
                f"needs a login - add one for {name} under Logins...",
                403: "access denied - this account may not have access to it" if have else
                f"access denied - it may need a {name} login (Logins...)",
                404: "not found", 410: "gone",
                429: "rate limited - try fewer parallel downloads"}.get(exc.code, "")
        return f"HTTP {exc.code}" + (f" - {hint}" if hint else "")
    if isinstance(exc, urllib.error.URLError):
        return f"Network error: {exc.reason}"
    if isinstance(exc, TimeoutError):
        return "Timed out"
    return str(exc) or type(exc).__name__


def parse_archive_url(text: str) -> tuple[str, str] | None:
    """('identifier', 'path inside the item') for an archive.org item link; None if it isn't archive.org."""
    u = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
    if (u.hostname or "").lower() not in ARCHIVE_HOSTS:
        return None
    segs = [urllib.parse.unquote(seg) for seg in u.path.split("/") if seg]
    if len(segs) < 2 or segs[0] not in ("details", "download", "metadata", "stream", "compress"):
        raise LinkError(f"{text}: paste an item link such as https://archive.org/details/<item>")
    return segs[1], "/".join(segs[2:])


def _archive_item_url(quoted_id: str, name: str) -> str:
    return f"{ARCHIVE_URL}/download/{quoted_id}/{urllib.parse.quote(name, safe='/')}"


def _fetch_archive_metadata(identifier: str) -> dict:
    quoted = urllib.parse.quote(identifier, safe="")
    try:
        with http_open(f"{ARCHIVE_URL}/metadata/{quoted}") as resp:
            data = json.load(resp)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise LinkError(f"{identifier}: {describe_error(exc)}") from exc
    return data if isinstance(data, dict) else {}


def _crawl_archive_folder(identifier: str, sub: str, limit: int = 50000) -> tuple[list[str], str]:
    """(names inside the item, diagnostic) for every file under a folder, read from archive.org's own folder
    pages (the same pages a browser shows at /download/<item>/<folder>/). Sub-folders are followed.
    Links are matched by their path inside the item, so relative, absolute and protocol-relative links
    (the listing may point at a different data server than the one it was served from) all work."""
    quoted = urllib.parse.quote(identifier, safe="")
    markers = (f"/items/{identifier}/", f"/download/{identifier}/")
    found: list[str] = []
    seen_dirs: set[str] = set()
    todo = [sub.strip("/")]
    problems: list[str] = []
    hrefs_seen, sample = 0, ""
    while todo and len(found) < limit:
        folder = todo.pop()
        if folder in seen_dirs:
            continue
        seen_dirs.add(folder)
        page_url = _archive_item_url(quoted, folder) + "/"
        try:
            with http_open(page_url) as resp:
                ctype = resp.headers.get("Content-Type", "")
                final = resp.geturl().split("#")[0].split("?")[0]
                page = resp.read(64 << 20).decode(resp.headers.get_content_charset() or "utf-8", "replace")
        except (OSError, http.client.HTTPException) as exc:
            problems.append(f"{folder or '/'}: {describe_error(exc)}")
            continue
        if "html" not in ctype.lower():
            problems.append(f"{folder or '/'}: not a folder page ({ctype or 'unknown type'})")
            continue
        final += "" if final.endswith("/") else "/"
        parser = _LinkCollector()
        parser.feed(page)
        hrefs_seen += len(parser.hrefs)
        for href in parser.hrefs:
            if href.startswith(("?", "#", "javascript:", "mailto:")):
                continue
            full = urllib.parse.urljoin(final, href).split("#")[0].split("?")[0]
            path = urllib.parse.unquote(urllib.parse.urlsplit(full).path)
            name = None
            for marker in markers:
                if marker in path:
                    name = path.split(marker, 1)[1]
                    break
            if name is None and full.startswith(final):  # no recognisable item path: relative to the page itself
                rel = urllib.parse.unquote(urllib.parse.urlsplit(full).path)[len(urllib.parse.urlsplit(final).path):]
                name = f"{folder}/{rel}" if folder else rel
            if name is None:
                continue
            if "/./" in "/" + name or ".." in name.split("/"):  # collapse ./ and ../ so a parent link can't pass as a sub-folder
                name = posixpath.normpath(name) + ("/" if name.endswith("/") else "")
            prefix = folder + "/" if folder else ""
            if not name.startswith(prefix) or name == prefix or name.rstrip("/") == folder:
                continue  # the parent link, the folder itself, and anything outside it
            if name.endswith("/"):
                todo.append(name.rstrip("/"))
            else:
                found.append(name)
            sample = sample or href
    diag = f"{hrefs_seen} link(s) on the folder page" + (f", e.g. {sample!r}" if sample else "")
    if problems:
        diag += "; " + "; ".join(problems[:3])
    return found, diag


def list_archive_item(identifier: str, subpath: str = "") -> list[RemoteFile]:
    quoted = urllib.parse.quote(identifier, safe="")
    data = _fetch_archive_metadata(identifier)
    if (data.get("metadata") or {}).get("mediatype") == "collection":
        raise LinkError(f"{identifier} is a collection, not a single item. Open one of its items and paste that link.")
    if not data.get("files"):
        if str((data.get("metadata") or {}).get("access-restricted-item", "")).lower() == "true":
            raise LinkError(f"{identifier} is access-restricted. " + (
                "Your account doesn't appear to have access to its files." if login_for(ARCHIVE_URL) else
                "Add your archive.org keys under Logins... and fetch again."))
        raise LinkError(f"{identifier}: no downloadable files found (item missing, empty or not public)")
    meta: dict[str, dict] = {f["name"]: f for f in data["files"] if f.get("name")}

    def make(name: str, f: dict | None, preselect: bool) -> RemoteFile:
        f = f or {}
        try:
            size = int(f["size"])
        except (KeyError, TypeError, ValueError):
            size = None
        return RemoteFile(name, _archive_item_url(quoted, name), size, identifier, f.get("md5"),
                          f.get("source") in ARCHIVE_EXTRA_SOURCES, preselect)

    sub = subpath.strip("/")
    if sub and sub not in meta:  # the link points at a folder: list just that folder, every level of it
        crawled, diag = _crawl_archive_folder(identifier, sub)
        names = set(crawled)
        names |= {n for n in meta if n.startswith(sub + "/") and not n.endswith("/")}  # whatever the metadata knows
        if names:
            # the folder page is public, so it wins over the metadata's "private" flag
            return [make(n, meta.get(n), True) for n in sorted(names, key=str.lower)]
        raise LinkError(f"{identifier}: nothing found in the folder '{sub}' ({diag})")
    files = []
    for name, f in meta.items():
        if name.endswith("/"):
            continue  # "private" files are kept: a download that is really restricted reports its own HTTP error
        files.append(make(name, f, bool(sub) and name == sub))
    return files


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href" and v)


def list_open_directory(url: str) -> list[RemoteFile]:
    """Files linked from a plain 'Index of /...' style page, or a single direct file link."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise LinkError(f"{url}: not a web link")
    last = u.path.rsplit("/", 1)[-1]
    if last and "." in last:  # a direct link to one file
        return [RemoteFile(urllib.parse.unquote(last), url, None, preselect=True)]
    base = url.split("#")[0].split("?")[0]
    base += "" if base.endswith("/") else "/"
    try:
        with http_open(base) as resp:
            if "html" not in resp.headers.get("Content-Type", "").lower():
                raise LinkError(f"{url}: that address isn't a folder listing")
            page = resp.read(8 << 20).decode(resp.headers.get_content_charset() or "utf-8", "replace")
    except (OSError, http.client.HTTPException) as exc:
        raise LinkError(f"{url}: {describe_error(exc)}") from exc
    group = urllib.parse.unquote(base.rstrip("/").rsplit("/", 1)[-1]) or (u.hostname or "")
    parser, files, seen = _LinkCollector(), [], set()
    parser.feed(page)
    for href in parser.hrefs:
        if href.startswith(("?", "#", "javascript:", "mailto:")):
            continue
        full = urllib.parse.urljoin(base, href).split("#")[0]
        if not full.startswith(base) or full == base or full.endswith("/") or full in seen:
            continue  # parent links, sort links, sub-folders (not followed), repeats
        seen.add(full)
        files.append(RemoteFile(urllib.parse.unquote(full[len(base):].split("?")[0]), full, None, group))
    if not files:
        raise LinkError(f"{url}: no files found at that address")
    return files


FICHIER_API = "https://api.1fichier.com/v1"


def _fichier_call(endpoint: str, payload: dict) -> dict:
    key = (LOGINS.get("1fichier") or {}).get("apikey", "")
    if not key:
        raise LinkError("1fichier links need your API key - add it under Logins...")
    req = urllib.request.Request(
        f"{FICHIER_API}/{endpoint}", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            message = str(json.loads(exc.read(4096).decode("utf-8", "replace")).get("message", ""))
        except (OSError, ValueError, AttributeError):
            message = ""
        exc.close()
        raise LinkError(f"1fichier: {message or describe_error(exc)}") from None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise LinkError(f"1fichier: {describe_error(exc)}") from None


def list_1fichier(url: str) -> list[RemoteFile]:
    """A 1fichier folder (its public JSON listing) or a single file link (looked up through the API)."""
    if "/dir/" in urllib.parse.urlsplit(url).path:
        listing = url + ("&" if "?" in url else "?") + "json=1"
        try:
            with http_open(listing) as resp:
                data = json.loads(resp.read(8 << 20).decode("utf-8", "replace"))
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise LinkError(f"1fichier folder: {describe_error(exc)}") from None
        rows = data if isinstance(data, list) else (data.get("items") or data.get("files") or []) if isinstance(data, dict) else []
        group = "1fichier-" + urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        out = [RemoteFile(str(r["filename"]), str(r["url"]), int(r["size"]) if str(r.get("size", "")).isdigit() else None, group)
               for r in rows if isinstance(r, dict) and r.get("filename") and r.get("url")]
        if not out:
            raise LinkError("1fichier folder: no files found (private folder, or the listing format changed)")
        return out
    info = _fichier_call("file/info.cgi", {"url": url})
    if not info.get("filename"):
        raise LinkError(f"1fichier: {info.get('message') or 'file not found'}")
    size = info.get("size")
    return [RemoteFile(str(info["filename"]), url, int(size) if str(size).isdigit() else None, "1fichier", None, False, True)]


def direct_url(url: str) -> str:
    """1fichier and TorBox links are exchanged for a short-lived direct link right before each download (and again on
    every retry, so a link can't go stale while it waits in the queue); others pass through."""
    if url.startswith("torbox:"):
        from .torbox import torbox_link  # deferred: torbox imports this module
        try:  # torbox:<torrent id>:<file id>, or torbox:<usenet | web>:<id>:<file id> for the account's other lists
            _scheme, *parts = url.split(":")
            if len(parts) == 2:
                return torbox_link(int(parts[0]), int(parts[1]))
            kind, ident, file_id = parts
            return torbox_link(int(ident), int(file_id), kind)
        except ValueError:
            raise LinkError("Not a TorBox file entry") from None
    if url.startswith("debrid:"):
        try:
            _scheme, service, handle, file_id = url.split(":")
            provider = next(p for p in debrid.active(LOGINS) if p.key == service)
            return provider.torrent_link(handle, file_id)
        except (ValueError, StopIteration):
            raise LinkError("Not a debrid torrent file entry (is the service's key still set?)") from None
        except debrid.DebridError as exc:
            raise LinkError(str(exc)) from None
    key = site_for(url)
    has_fichier_key = bool((LOGINS.get("1fichier") or {}).get("apikey"))
    if not (key == "1fichier" and has_fichier_key) and url.startswith(("http://", "https://")):
        try:  # a debrid service that supports this site turns the link into a direct one
            unlocked = debrid.unlock(url, host_of(url), LOGINS)
        except debrid.DebridError as exc:
            raise LinkError(str(exc)) from None
        if unlocked:
            return unlocked.url
    if key != "1fichier":
        from .hosters import hoster_for  # noqa: PLC0415  (hosters imports this module)
        handler = hoster_for(url)
        return handler.direct(url) if handler else url
    got = _fichier_call("download/get_token.cgi", {"url": url, "inline": 0, "cdn": 0, "restrict_ip": 0, "no_ssl": 0})
    if not got.get("url"):
        raise LinkError(f"1fichier: {got.get('message') or 'no download link returned (free-account wait or limit?)'}")
    return str(got["url"])


@dataclass(frozen=True, slots=True)
class DownloadSummary:
    total: int
    done: int
    skipped: int
    failures: tuple[tuple[int, str, str], ...]  # (row index, name, reason)
    bytes_received: int
    seconds: float
    stopped: int = 0  # files the person stopped one by one during the run


class DownloadRunner:
    """Parallel, resumable downloader. Like BatchRunner it never touches Tk: it posts events to the UI."""

    KEEPS_PARTIALS = True  # an aborted download leaves its .part file to resume from
    _BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

    def __init__(self, jobs: list[tuple[int, RemoteFile]], dest: Path, threads: int, verify: bool,
                 per_item_folder: bool, emit: Callable[[tuple], None], pause_event: threading.Event):
        self.jobs, self.dest, self.threads = jobs, dest, threads
        self.verify, self.per_item = verify, per_item_folder
        self._emit, self.pause_event = emit, pause_event
        self._stop, self._lock = threading.Event(), threading.Lock()
        self._counted: dict[int, int] = {}  # bytes of each file already reflected in bytes_done
        self.bytes_done = 0
        self.bytes_total = sum(rf.size or 0 for _i, rf in jobs)
        self.bytes_received = 0
        self.files_total = len(jobs)
        self.files_finished = 0
        self.done = self.skipped = 0
        self.failures: list[tuple[int, str, str]] = []
        self._queue = deque(jobs)  # files not started yet
        self._gate = threading.Condition()  # guards _queue, _active, _running, _workers, _closed and threads
        self._active = 0  # files in flight right now
        # Per-file control. Lock order is always _gate, then _lock.
        self._live = {i for i, _rf in jobs}  # rows that are queued or running (under _lock)
        self._running: set[int] = set()  # rows a worker holds right now (under _gate)
        self._cancelled: set[int] = set()  # rows the person stopped that haven't wound down yet (under _lock)
        self._result: dict[int, str] = {}  # row -> done / skipped / failed / stopped (under _lock)
        self.stopped = 0
        self._workers: list[threading.Thread] = []
        self._closed = False  # the run is over: add() turns files away
        self._told_paused = False
        self.post_action = ""  # '' | 'extract' | 'extract_delete': what to do with archives once everything has finished
        self.completed_paths: list[Path] = []  # files that finished (or were already there), for the post step
        self._finals: dict[int, Path] = {}  # where a file really ended up, when the name had no extension (yt-dlp)
        self._no_ranges: set[str] = set()  # sites that ignored a Range request: downloaded on one connection

    def prioritize(self, indexes: list[int]) -> int:
        """Move these still-waiting files to the front of the queue, keeping their order. Returns how many moved;
        files already downloading (or finished) are unaffected."""
        wanted = set(indexes)
        with self._gate:
            front = [j for j in self._queue if j[0] in wanted]
            if not front:
                return 0
            rest = [j for j in self._queue if j[0] not in wanted]
            self._queue.clear()
            self._queue.extend([*front, *rest])
            self._gate.notify_all()
        return len(front)

    def set_threads(self, n: int) -> None:
        """Change how many files download at once, while the batch is running."""
        n = min(DL_MAX_PARALLEL, max(1, n))
        with self._gate:
            if n == self.threads:
                return
            self.threads = n
            self._gate.notify_all()
        self._emit(("log", f"Parallel downloads set to {n} (files already running finish normally)"))

    def shutdown(self) -> None:
        self._stop.set()
        self.pause_event.set()  # wake paused workers so they can exit
        with self._gate:
            self._gate.notify_all()

    # ---- per-file control (the right-click menu) -------------------------------
    def is_live(self, idx: int) -> bool:
        """True while this row is waiting its turn or downloading in this run."""
        return idx in self._live

    def is_busy(self, idx: int) -> bool:
        """True while something may still have this row's files open (running, or told to stop and not yet let go)."""
        return idx in self._running or idx in self._cancelled

    def cancel(self, idxs) -> list[int]:
        """Stop these files without touching the rest of the run. Files still waiting are dropped at once; running
        ones abort at their next chunk and keep their .part to resume from. Returns the rows that were part of the run."""
        dropped: list[tuple[int, RemoteFile]] = []
        with self._gate:
            with self._lock:
                hit = [i for i in dict.fromkeys(idxs) if i in self._live]
                if not hit:
                    return []
                mine = set(hit)
                waiting = deque()
                for job in self._queue:
                    (dropped if job[0] in mine else waiting).append(job)
                self._queue = waiting
                for idx, rf in dropped:  # never started, so nothing was counted for it yet
                    self._live.discard(idx)
                    self._result[idx] = "stopped"
                    self.files_total -= 1
                    self.bytes_total -= rf.size or 0
                    self.stopped += 1
                self._cancelled |= mine - {i for i, _rf in dropped}  # the rest wind down in their own thread
                for idx in self._cancelled & mine:  # emitted under the lock, so a finishing file's own status wins
                    self._emit(("dl_row", idx, "Stopping..."))
            self._gate.notify_all()
        for idx, rf in dropped:
            self._emit(("dl_row", idx, "Stopped"))
            self._emit(("log", f"Stopped: {rf.name}"))
        return hit

    def add(self, jobs: list[tuple[int, RemoteFile]]) -> list[int] | None:
        """Put more files into the run while it is going; each starts as soon as a slot is free. Rows already in the
        run, or finished in it, are left alone. Returns the rows taken, or None if the run can't take any (it is over)."""
        taken: list[tuple[int, RemoteFile]] = []
        with self._gate:
            if self._closed or self._stop.is_set():
                return None
            with self._lock:
                for idx, rf in jobs:
                    prev = self._result.get(idx)
                    if idx in self._live or prev in ("done", "skipped"):
                        continue
                    self._result.pop(idx, None)
                    if prev == "failed":  # counted as finished and forgotten from the bar: undo both
                        self.failures = [f for f in self.failures if f[0] != idx]
                        self.files_finished -= 1
                    else:  # new to this run, or stopped earlier (which took it out of the total)
                        self.files_total += 1
                    self.bytes_total += rf.size or 0
                    self._live.add(idx)
                    taken.append((idx, rf))
            self.jobs.extend(taken)
            self._queue.extend(taken)
            self._spawn(len(taken))
            self._gate.notify_all()
        return [idx for idx, _rf in taken]

    def _spawn(self, n: int) -> None:  # call with _gate held
        for _ in range(n):
            worker = threading.Thread(target=self._worker, daemon=True, name=f"download-{len(self._workers)}")
            self._workers.append(worker)
            worker.start()

    def _join_workers(self) -> None:
        """Wait for every worker, including ones add() started later. Once this returns the run is closed."""
        while True:
            with self._gate:
                self._workers = [w for w in self._workers if w.is_alive()]
                if not self._workers:
                    self._closed = True
                    return
                waiting_for = self._workers[0]
            waiting_for.join()

    def _worker(self) -> None:
        """Take the next file whenever fewer than `threads` are running and its site has room (a per-site limit can
        leave a file waiting while later ones from other sites go ahead); surplus workers just wait."""
        while True:
            with self._gate:
                job = None
                while not job:
                    if self._stop.is_set() or not self._queue:
                        return
                    if RULES.active_kbps() == PAUSED:  # a schedule window that forbids downloading: start nothing new
                        if not self._told_paused:
                            self._told_paused = True
                            self._emit(("log", "Downloads are paused by the schedule; they carry on when its window opens."))
                    elif self._active < self.threads:
                        self._told_paused = False
                        for k, cand in enumerate(self._queue):
                            if GATE.try_acquire(host_of(cand[1].url)):
                                job = cand
                                del self._queue[k]
                                break
                    if not job:
                        self._gate.wait(1.0)  # woken early by a finished file; the timeout re-reads changed rules
                self._active += 1
                self._running.add(job[0])
            try:
                self._job(job)
            finally:
                GATE.release(host_of(job[1].url))
                with self._gate:
                    self._active -= 1
                    self._running.discard(job[0])
                    self._gate.notify_all()

    def _checkpoint(self, idx: int | None = None) -> bool:
        """Wait out a pause, then say whether this file (or the whole run) should carry on."""
        while not self.pause_event.wait(0.25):  # paused: stay alert, a stop must not wait for Resume
            if self._stop.is_set() or (idx is not None and idx in self._cancelled):
                break
        return not self._stop.is_set() and not (idx is not None and idx in self._cancelled)

    def _nap(self, idx: int, secs: float) -> bool:
        """Sleep between retries; True if the run or this file was stopped meanwhile."""
        end = time.monotonic() + secs
        while (left := end - time.monotonic()) > 0:
            if self._stop.wait(min(0.25, left)) or idx in self._cancelled:
                return True
        return False

    def _settle(self, idx: int, result: str) -> None:
        """This row is finished with, one way or another."""
        with self._lock:
            self._live.discard(idx)
            self._cancelled.discard(idx)
            self._result[idx] = result

    def _finish_cancel(self, idx: int, rf: RemoteFile) -> None:
        """A running file has let go after being stopped: take it out of the totals."""
        self._forget(idx, rf)
        with self._lock:
            self.files_total -= 1
            self.stopped += 1
        self._settle(idx, "stopped")
        self._emit(("dl_row", idx, "Stopped"))
        self._emit(("log", f"Stopped: {rf.name}"))

    # ---- progress bookkeeping -------------------------------------------------
    def _add(self, idx: int, delta: int, received: bool = False) -> None:
        with self._lock:
            self._counted[idx] = self._counted.get(idx, 0) + delta
            self.bytes_done += delta
            if received:
                self.bytes_received += delta

    def _sync(self, idx: int, have: int) -> None:
        self._add(idx, have - self._counted.get(idx, 0))

    def _learn(self, idx: int, rf: RemoteFile, size: int) -> None:
        if rf.size is None:
            rf.size = size
            with self._lock:
                self.bytes_total += size
            self._emit(("dl_size_run", idx, size))

    def _forget(self, idx: int, rf: RemoteFile) -> None:
        """A failed file no longer counts toward the progress bar."""
        with self._lock:
            self.bytes_done -= self._counted.pop(idx, 0)
            self.bytes_total -= rf.size or 0

    @staticmethod
    def _pct(got: int, size: int | None) -> str:
        return f"{got * 100 // size}%  {fmt_size(got)} / {fmt_size(size)}" if size else fmt_size(got)

    # ---- one file ---------------------------------------------------------------
    @classmethod
    def locate(cls, rf: RemoteFile, dest: Path, per_item: bool) -> Path:
        """Where this file is (or will be) saved under `dest`. Raises ValueError for a name that would escape it."""
        def clean(seg: str) -> str:
            return cls._BAD_CHARS.sub("_", seg).rstrip(" .") or "_"

        parts = [seg for seg in rf.name.replace("\\", "/").split("/") if seg not in ("", ".")]
        if not parts or ".." in parts:
            raise ValueError("Unsafe file name in the listing")
        base = dest / clean(rf.group) if (per_item and rf.group) else dest
        path = base.joinpath(*(clean(seg) for seg in parts))
        if not path.resolve().is_relative_to(dest.resolve()):
            raise ValueError("Unsafe file path in the listing")
        return path

    def _target(self, rf: RemoteFile) -> Path:
        return self.locate(rf, self.dest, self.per_item)

    @staticmethod
    def _retryable(exc: BaseException) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in (408, 425, 429) or exc.code >= 500
        return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException,
                                _Short, _BadHash))

    def _replace(self, part: Path, dest: Path) -> None:
        for attempt in range(5):
            try:
                os.replace(part, dest)
                return
            except PermissionError:  # antivirus / indexer briefly holding the new file
                if attempt == 4:
                    raise
                time.sleep(1)

    def _fetch(self, idx: int, rf: RemoteFile, dest: Path) -> str:
        if rf.url.startswith(ytdlp.PREFIX):
            return self._fetch_ytdlp(idx, rf, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        if dest.exists() and (rf.size is None or dest.stat().st_size == rf.size):
            if rf.size is None:
                self._learn(idx, rf, dest.stat().st_size)
            self._sync(idx, dest.stat().st_size)
            return "skipped"

        seg_file = part.with_name(part.name + ".segs")
        site = host_of(rf.url)
        workers = RULES.segments_for(site)
        if (rf.size and rf.url.startswith(("http://", "https://", "torbox:")) and site not in self._no_ranges
                and (seg_file.exists() or (workers > 1 and rf.size >= SEG_MIN and not part.exists()))):
            try:
                return self._fetch_segmented(idx, rf, dest, part, seg_file, max(1, workers))
            except _NoRanges:
                self._no_ranges.add(site)
                seg_file.unlink(missing_ok=True)
                part.unlink(missing_ok=True)
                self._sync(idx, 0)
                self._emit(("log", f"{site} can't serve parts of a file: downloading {rf.name} on one connection."))

        have = part.stat().st_size if part.exists() else 0
        if rf.size is not None and have > rf.size:
            part.unlink()
            have = 0
        digest = hashlib.md5() if (self.verify and rf.md5) else None
        if digest and have:  # resuming: the checksum has to cover the bytes we already have
            self._emit(("dl_row", idx, "Checking partial download..."))
            with open(part, "rb") as fh:
                while chunk := fh.read(1 << 20):
                    if not self._checkpoint(idx):
                        raise _Aborted
                    digest.update(chunk)

        resp = None
        try:
            resp = http_open(direct_url(rf.url), {"Range": f"bytes={have}-"} if have else None)
        except urllib.error.HTTPError as exc:
            if exc.code != 416 or not have:
                raise
            if rf.size is None or have != rf.size:  # asked for bytes past the end: the .part is bogus
                part.unlink(missing_ok=True)
                self._sync(idx, 0)
                raise _Short("Partial file didn't match the server; restarting") from None
        if resp is not None:
            with resp:
                if have and resp.status != 206:  # the server ignored the Range header: start over
                    have = 0
                    digest = hashlib.md5() if digest is not None else None
                length = resp.headers.get("Content-Length", "")
                total = have + int(length) if length.isdigit() else rf.size
                if total is not None:
                    self._learn(idx, rf, total)
                self._sync(idx, have)
                got, last = have, 0.0
                site = host_of(rf.url)
                mark_t, mark_got, speed = time.monotonic(), have, 0.0
                with open(part, "ab" if have else "wb") as out:
                    while True:
                        if not self._checkpoint(idx):
                            raise _Aborted
                        chunk = resp.read(DL_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        if digest:
                            digest.update(chunk)
                        got += len(chunk)
                        self._add(idx, len(chunk), received=True)
                        if not THROTTLE.consume(len(chunk), site, lambda: self._stop.is_set() or idx in self._cancelled):
                            raise _Aborted
                        now = time.monotonic()
                        if now - last >= 0.5:
                            last = now
                            if now - mark_t >= 0.25:  # smoothed per-file speed
                                inst = (got - mark_got) / (now - mark_t)
                                speed = inst if speed == 0 else speed * 0.6 + inst * 0.4
                                mark_t, mark_got = now, got
                                self._emit(("dl_speed", idx, f"{fmt_size(int(speed))}/s"))
                            self._emit(("dl_row", idx, self._pct(got, rf.size)))

        on_disk = part.stat().st_size
        if rf.size is not None and on_disk != rf.size:
            raise _Short("Connection dropped before the file finished")
        if digest and digest.hexdigest().lower() != (rf.md5 or "").lower():
            part.unlink(missing_ok=True)
            self._sync(idx, 0)
            raise _BadHash("Checksum mismatch - the download was corrupted")
        self._replace(part, dest)
        self._learn(idx, rf, on_disk)
        return "done"

    @staticmethod
    def _attempts(rf: RemoteFile) -> int:
        rule = RULES.host_rule(host_of(rf.url))
        return rule.attempts if rule and rule.attempts else DL_ATTEMPTS

    # ---- a video or audio site, through yt-dlp ------------------------------------------------------------------
    def _fetch_ytdlp(self, idx: int, rf: RemoteFile, dest: Path) -> str:
        """Run yt-dlp for one video and mirror its progress. Pausing stops it and starts it again on Resume (it carries on
        from its partial file); stopping kills it."""
        exe = ytdlp.find_exe()
        if not exe:
            raise LinkError("yt-dlp isn't installed: it is downloaded in Settings > Tools > Download Tools")
        dest.parent.mkdir(parents=True, exist_ok=True)
        existing = [p for p in dest.parent.glob(glob.escape(dest.name) + ".*")
                    if p.suffix.lower() not in (".part", ".ytdl", ".temp", ".tmp") and p.is_file()]
        if (picked := ytdlp.parse_choice(rf.fmt)) is not None:  # an MP3 is not "already there" because the MP4 is
            existing = [p for p in existing if p.suffix.lower() == "." + picked[0]]
        if existing:
            self._learn(idx, rf, existing[0].stat().st_size)
            self._sync(idx, existing[0].stat().st_size)
            return "skipped"
        kbps = RULES.active_kbps()
        site = RULES.host_rule(host_of(rf.url[len(ytdlp.PREFIX):]))
        caps = [k for k in (kbps, site.kbps if site else 0) if k > 0]
        cmd = ytdlp.command(exe, rf.url[len(ytdlp.PREFIX):], dest, ytdlp.SETTINGS.preset, min(caps) if caps else 0,
                            RULES.proxy_for(host_of(rf.url[len(ytdlp.PREFIX):])), rf.fmt)
        final: Path | None = None
        problems: list[str] = []
        while True:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1, creationflags=SUBPROCESS_FLAGS)
            lines: queue.Queue = queue.Queue()

            def pump(p=proc, q=lines) -> None:
                try:
                    for text in p.stdout:  # type: ignore[union-attr]
                        q.put(text)
                except (OSError, ValueError):  # the pipe was closed under us after a stop
                    pass
                q.put(None)
            threading.Thread(target=pump, daemon=True).start()
            paused = False
            try:
                while True:
                    if self._stop.is_set() or idx in self._cancelled:
                        raise _Aborted
                    if not self.pause_event.is_set():
                        paused = True
                        break
                    try:
                        text = lines.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    if text is None:
                        break
                    if (prog := ytdlp.parse_progress(text)) is not None:
                        got, total, speed = prog
                        self._emit(("dl_row", idx, f"{fmt_size(got)} / {fmt_size(total)}" if total else fmt_size(got)))
                        if speed:
                            self._emit(("dl_speed", idx, f"{fmt_size(int(speed))}/s"))
                    elif (path := ytdlp.final_path(text)) is not None:
                        final = path
                    elif text.strip():
                        problems.append(text.strip())
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                if proc.stdout:
                    with contextlib.suppress(OSError, ValueError):
                        proc.stdout.close()
            if not paused:
                break
            if not self._checkpoint(idx):  # waits out the pause, False if told to stop meanwhile
                raise _Aborted
        if proc.returncode != 0:
            message = next((p for p in problems if p.startswith("ERROR")), problems[-1] if problems else "yt-dlp failed")
            message = message.removeprefix("ERROR: ")
            if ytdlp.is_permanent_failure(message):
                raise LinkError(message)
            raise _Short(message)
        if final is None or not final.exists():
            found = [p for p in dest.parent.glob(glob.escape(dest.name) + ".*") if p.suffix.lower() not in (".part", ".ytdl")]
            final = found[0] if found else None
        if final is None:
            raise LinkError("yt-dlp finished but no file was saved")
        size = final.stat().st_size
        self._finals[idx] = final
        self._learn(idx, rf, size)
        self._sync(idx, size)
        return "done"

    # ---- one file over several connections ---------------------------------------------------------------------
    def _fetch_segmented(self, idx: int, rf: RemoteFile, dest: Path, part: Path, seg_file: Path, workers: int) -> str:
        """Fetch the file in fixed blocks, `workers` at a time, each block its own ranged request written straight to its
        place in a pre-sized .part file. Finished blocks are listed in a .segs file, so a stop, a crash or a dropped
        connection resumes from the blocks that are done. The link is resolved once and shared by every block."""
        size = rf.size or 0
        blocks = -(-size // SEG_BLOCK)
        done: set[int] = set()
        try:
            saved = json.loads(seg_file.read_text(encoding="utf-8")) if seg_file.exists() else {}
            if saved.get("size") == size and saved.get("block") == SEG_BLOCK and part.exists() and part.stat().st_size == size:
                done = {b for b in saved.get("done", []) if isinstance(b, int) and 0 <= b < blocks}
        except (OSError, ValueError, AttributeError):
            pass
        if not done:
            with open(part, "wb") as fh:
                fh.truncate(size)

        def length(b: int) -> int:
            return min(SEG_BLOCK, size - b * SEG_BLOCK)

        def save_state() -> None:
            with contextlib.suppress(OSError):
                seg_file.write_text(json.dumps({"size": size, "block": SEG_BLOCK, "done": sorted(done)}), encoding="utf-8")

        self._sync(idx, sum(length(b) for b in done))
        todo = deque(b for b in range(blocks) if b not in done)
        gate, errors = threading.Lock(), []
        link = {"url": direct_url(rf.url)}

        def abort() -> bool:
            return self._stop.is_set() or idx in self._cancelled

        def block(out, b: int) -> None:
            start, end = b * SEG_BLOCK, b * SEG_BLOCK + length(b) - 1
            for attempt in range(1, self._attempts(rf) + 1):
                got = 0
                try:
                    with http_open(link["url"], {"Range": f"bytes={start}-{end}"}) as resp:
                        if resp.status != 206:
                            raise _NoRanges
                        out.seek(start)
                        while got < length(b):
                            if not self._checkpoint(idx):
                                raise _Aborted
                            chunk = resp.read(min(DL_CHUNK, length(b) - got))
                            if not chunk:
                                raise _Short("Connection dropped before the block finished")
                            out.write(chunk)
                            got += len(chunk)
                            self._add(idx, len(chunk), received=True)
                            if not THROTTLE.consume(len(chunk), host_of(rf.url), abort):
                                raise _Aborted
                    return
                except (_Aborted, _NoRanges):
                    raise
                except Exception as exc:
                    self._add(idx, -got)  # this block starts over
                    if isinstance(exc, urllib.error.HTTPError):
                        code = exc.code
                        exc.close()
                        if code in (401, 403, 404, 410) and attempt < self._attempts(rf):  # a short-lived link went stale
                            with gate:
                                link["url"] = direct_url(rf.url)
                            continue
                    if not self._retryable(exc) or attempt >= self._attempts(rf):
                        raise
                    if self._nap(idx, min(2 ** attempt, 15)):
                        raise _Aborted from None

        def worker() -> None:
            try:
                with open(part, "r+b") as out:
                    while True:
                        with gate:
                            if errors or not todo:
                                return
                            b = todo.popleft()
                        block(out, b)
                        with gate:
                            done.add(b)
            except BaseException as exc:  # reported by the loop below
                with gate:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, daemon=True, name=f"segment-{n}") for n in range(min(workers, len(todo)))]
        for t in threads:
            t.start()
        mark_t, mark_got, speed, last_save = time.monotonic(), self._counted.get(idx, 0), 0.0, time.monotonic()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.25)
            now = time.monotonic()
            got = self._counted.get(idx, 0)
            if now - mark_t >= 0.5:
                inst = (got - mark_got) / (now - mark_t)
                speed = inst if speed == 0 else speed * 0.6 + inst * 0.4
                mark_t, mark_got = now, got
                self._emit(("dl_speed", idx, f"{fmt_size(int(max(speed, 0)))}/s"))
                self._emit(("dl_row", idx, self._pct(got, size)))
            if now - last_save >= 2.0:
                last_save = now
                with gate:
                    save_state()
        with gate:
            save_state()
        if errors:
            for kind in (_Aborted, _NoRanges):
                if any(isinstance(e, kind) for e in errors):
                    raise next(e for e in errors if isinstance(e, kind))
            raise errors[0]
        if len(done) != blocks or part.stat().st_size != size:
            raise _Short("Connection dropped before the file finished")
        if self.verify and rf.md5:
            self._emit(("dl_row", idx, "Checking download..."))
            digest = hashlib.md5()
            with open(part, "rb") as fh:
                while chunk := fh.read(1 << 20):
                    if not self._checkpoint(idx):
                        raise _Aborted
                    digest.update(chunk)
            if digest.hexdigest().lower() != rf.md5.lower():
                part.unlink(missing_ok=True)
                seg_file.unlink(missing_ok=True)
                self._sync(idx, 0)
                raise _BadHash("Checksum mismatch - the download was corrupted")
        self._replace(part, dest)
        seg_file.unlink(missing_ok=True)
        self._learn(idx, rf, size)
        return "done"

    def _fail(self, idx: int, rf: RemoteFile, reason: str) -> None:
        self._forget(idx, rf)
        with self._lock:
            self.failures.append((idx, rf.name, reason))
            self.files_finished += 1
        self._settle(idx, "failed")
        self._emit(("dl_row", idx, f"Failed: {reason}"))
        self._emit(("log", f"Download failed: {rf.name} - {reason}"))

    def _one(self, idx: int, rf: RemoteFile) -> bool:
        """Download one file. True once it has been counted (done, skipped or failed); False if it was cut short by a
        stop of the whole run or of just this file."""
        if not self._checkpoint(idx):
            return False
        try:
            dest = self._target(rf)
        except ValueError as exc:
            self._fail(idx, rf, str(exc))
            return True
        attempt = bad_copies = 0
        while True:
            if not self._checkpoint(idx):
                return False
            before = self._counted.get(idx, 0)
            try:
                outcome = self._fetch(idx, rf, dest)
                break
            except _Aborted:
                return False
            except Exception as exc:
                if self._stop.is_set() or idx in self._cancelled:
                    return False
                reason = describe_error(exc)
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()  # an error response still holds its socket open until closed
                # Progress resets the count: a pause or a flaky link mid-file shouldn't use up the retries.
                attempt = 1 if self._counted.get(idx, 0) > before else attempt + 1
                bad_copies += isinstance(exc, _BadHash)  # a second corrupt copy means the source file is bad
                limit = self._attempts(rf)
                if not self._retryable(exc) or attempt >= limit or bad_copies > 1:
                    self._fail(idx, rf, reason)
                    return True
                self._emit(("dl_speed", idx, ""))
                self._emit(("dl_row", idx, f"Retrying ({attempt}/{limit - 1}): {reason}"))
                if self._nap(idx, (2, 5, 15)[min(attempt, 3) - 1]):
                    return False
        with self._lock:
            self.files_finished += 1
            if outcome == "skipped":
                self.skipped += 1
            else:
                self.done += 1
        self._settle(idx, outcome)
        with self._lock:
            self.completed_paths.append(self._finals.pop(idx, dest))
        if outcome == "skipped":
            self._emit(("dl_row", idx, "Already downloaded"))
        else:
            self._emit(("dl_row", idx, "Done"))
            self._emit(("log", f"Downloaded {rf.name} ({fmt_size(rf.size or 0)})"))
        return True

    def _post_process(self) -> None:
        from .postprocess import unpack_all  # noqa: PLC0415
        try:
            unpack_all(self.completed_paths, self.post_action == "extract_delete",
                       lambda m: self._emit(("log", m)), self._stop)
        except Exception as exc:  # unpacking is a bonus: it must never lose the download summary
            self._emit(("log", f"Unpacking stopped: {exc}"))

    def _job(self, job: tuple[int, RemoteFile]) -> None:
        settled = False
        try:
            settled = self._one(*job)
        except Exception as exc:  # one bad file must never take the batch down
            if not self._stop.is_set():
                self._fail(job[0], job[1], f"Unexpected error: {exc}")
                settled = True
        finally:
            if not settled and job[0] in self._cancelled and not self._stop.is_set():
                self._finish_cancel(*job)  # stopped on its own: leave the totals and say so
            self._emit(("dl_speed", job[0], ""))  # the row is no longer transferring

    def run(self) -> None:
        summary, started = None, time.monotonic()
        try:
            self._emit(("log", f"\n--- Download started: {len(self.jobs)} file(s) -> {self.dest} "
                               f"({self.threads} at a time) ---"))
            with self._gate:
                self._spawn(DL_MAX_PARALLEL)  # the limit, not the thread count, sets the parallelism
            self._join_workers()
            if self._stop.is_set():
                return
            took = time.monotonic() - started
            if self.post_action:
                self._post_process()
            summary = DownloadSummary(len(self.jobs), self.done, self.skipped, tuple(self.failures),
                                      self.bytes_received, took, self.stopped)
            speed = f", {fmt_size(int(self.bytes_received / took))}/s average" if took > 1 and self.bytes_received else ""
            lost = f", {self.stopped} stopped" if self.stopped else ""
            self._emit(("log", f"--- Download complete: {self.done} downloaded ({fmt_size(self.bytes_received)}"
                               f"{speed}), {self.skipped} already present, {len(self.failures)} failed{lost} ---"))
        except Exception as exc:
            self._emit(("log", f"Fatal download error: {exc!r}"))
            self._emit(("notice", f"Download error: {exc}"))
        finally:
            self._emit(("dl_done", summary))
