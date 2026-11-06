"""Debrid services: premium link generators that turn a file-host link into a direct download link.

One small interface (`Provider`) so a service is a few lines: where its host list is, how to unlock one link. A link is
only ever sent to a service when that service says it supports the link's site, so private or unrelated URLs
(your own servers, open directories) never leave your machine. TorBox is registered too, for torrents; its magnet
handling stays in torbox.py. No Qt in here.

The REST calls follow each service's published API but have not been run against live accounts here: if one changes or
misbehaves, the error shown names the service and what it said."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .constants import APP_NAME, APP_VERSION

AGENT = f"{APP_NAME}/{APP_VERSION}"
HOSTS_TTL = 6 * 3600  # how long a service's list of supported sites is trusted
# Sites with their own sign-in in Distill: never handed to a debrid service.
NEVER_DEBRID = ("archive.org", "torbox.app")


class DebridError(Exception):
    pass


@dataclass(slots=True)
class Unlocked:
    url: str
    name: str = ""
    size: int | None = None


@dataclass(slots=True)
class DebridFile:
    id: str
    name: str
    size: int | None = None


@dataclass(slots=True)
class TorrentHandle:
    id: str  # the service's own id for the torrent on this account
    files: list[DebridFile]


Fetch = Callable[[str, str, dict, bytes | None], object]  # (method, url, headers, body) -> parsed JSON


def _urllib_fetch(method: str, url: str, headers: dict, body: bytes | None) -> object:
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read(4 << 20).decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read(8192).decode("utf-8", "replace"))
        except (OSError, ValueError):
            data = None
        exc.close()
        raise DebridError(_api_message(data) or f"HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise DebridError(str(exc) or type(exc).__name__) from None


def _api_message(data: object) -> str:
    if isinstance(data, dict):
        err = data.get("error") or data.get("message")
        if isinstance(err, dict):
            err = err.get("message") or err.get("code")
        return str(err or "")
    return ""


def _form(**fields: str) -> bytes:
    return urllib.parse.urlencode(fields).encode("utf-8")


class Provider:
    key = ""  # the LOGINS key holding this service's API key
    name = ""
    site = ""  # the service's own web address (also what LOGIN_SITES lists it under)
    where = ""  # page where the API key is shown
    torrents = False  # can also fetch torrents / magnets (TorBox's own code handles those)
    torrent_api = False  # implements the torrent_* methods below, used by DebridTorrentRunner
    links = True  # can unlock file-host links

    def __init__(self, token: str, fetch: Fetch = _urllib_fetch):
        self.token, self._fetch = token, fetch
        self._hosts: set[str] | None = None
        self._hosts_at = 0.0
        self._lock = threading.Lock()

    def supported_hosts(self) -> set[str]:
        with self._lock:
            if self._hosts is None or time.monotonic() - self._hosts_at > HOSTS_TTL:
                self._hosts = {h.strip().lower().lstrip("*.") for h in self._load_hosts() if isinstance(h, str)}
                self._hosts_at = time.monotonic()
            return self._hosts

    def supports(self, host: str) -> bool:
        if not self.links or not host or any(host == n or host.endswith("." + n) for n in NEVER_DEBRID):
            return False
        return any(host == h or host.endswith("." + h) for h in self.supported_hosts())

    def _load_hosts(self) -> list: raise NotImplementedError

    def unlock(self, url: str) -> Unlocked: raise NotImplementedError

    # ---- torrents (only services with torrent_api) ----------------------------------------------------------------
    def torrent_cached(self, hashes: list[str]) -> set[str]:
        """The info-hashes this service can serve right away (a service with no cache check returns them all and lets
        torrent_open find out)."""
        raise NotImplementedError

    def torrent_list(self, ih: str, magnet: str) -> list[DebridFile] | None:
        """The file list of a cached torrent without adding anything to the account; None if it can't say."""
        return None

    def torrent_open(self, ih: str, magnet: str) -> TorrentHandle:
        """Make a cached torrent ready on the account and list its files. DebridError if it isn't cached."""
        raise NotImplementedError

    def torrent_link(self, handle: str, file_id: str) -> str:
        """A fresh direct link to one file of an opened torrent."""
        raise NotImplementedError


class RealDebrid(Provider):
    key, name, site = "realdebrid", "Real-Debrid", "real-debrid.com"
    where = "https://real-debrid.com/apitoken"
    API = "https://api.real-debrid.com/rest/1.0"

    def _load_hosts(self):
        data = self._fetch("GET", f"{self.API}/hosts/domains", {}, None)
        return data if isinstance(data, list) else []

    def unlock(self, url):
        data = self._fetch("POST", f"{self.API}/unrestrict/link", {"Authorization": f"Bearer {self.token}"}, _form(link=url))
        if not isinstance(data, dict) or not data.get("download"):
            raise DebridError(_api_message(data) or "no link returned")
        return Unlocked(str(data["download"]), str(data.get("filename") or ""), _int(data.get("filesize")))

    # torrents: no cache check exists, so a magnet is added and given a short time to become ready; one that isn't
    # ready is removed again, and the swarm handles it instead.
    torrent_api = True
    READY_SECONDS = 25.0
    _sleep = staticmethod(time.sleep)

    def torrent_cached(self, hashes):
        return set(hashes)

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def torrent_open(self, ih, magnet):
        added = self._fetch("POST", f"{self.API}/torrents/addMagnet", self._auth(), _form(magnet=magnet))
        tid = str(added.get("id") or "") if isinstance(added, dict) else ""
        if not tid:
            raise DebridError(_api_message(added) or "the magnet wasn't accepted")
        waited = 0.0
        while True:
            info = self._fetch("GET", f"{self.API}/torrents/info/{tid}", self._auth(), None)
            status = str(info.get("status") if isinstance(info, dict) else "")
            if status == "waiting_files_selection":
                self._fetch("POST", f"{self.API}/torrents/selectFiles/{tid}", self._auth(), _form(files="all"))
            elif status == "downloaded":
                break
            elif status in ("magnet_error", "error", "virus", "dead"):
                self._drop(tid)
                raise DebridError(f"torrent status: {status}")
            if waited >= self.READY_SECONDS:
                self._drop(tid)
                raise DebridError("not cached (it would have to download on their servers first)")
            self._sleep(1.5)
            waited += 1.5
        files = [f for f in info.get("files", []) if isinstance(f, dict) and f.get("selected")]
        links = info.get("links", [])
        if len(files) != len(links):
            raise DebridError("it came back as bundled archive links, which aren't supported")
        return TorrentHandle(tid, [DebridFile(str(i), str(f.get("path", "")).lstrip("/"), _int(f.get("bytes")))
                                   for i, f in enumerate(files)])

    def _drop(self, tid: str) -> None:
        try:
            self._fetch("DELETE", f"{self.API}/torrents/delete/{tid}", self._auth(), None)
        except DebridError:
            pass

    def torrent_link(self, handle, file_id):
        info = self._fetch("GET", f"{self.API}/torrents/info/{handle}", self._auth(), None)
        links = info.get("links", []) if isinstance(info, dict) else []
        try:
            return self.unlock(links[int(file_id)]).url
        except (IndexError, ValueError):
            raise DebridError("that file is no longer on the account") from None


class AllDebrid(Provider):
    key, name, site = "alldebrid", "AllDebrid", "alldebrid.com"
    where = "https://alldebrid.com/apikeys/"
    API = "https://api.alldebrid.com/v4"

    def _load_hosts(self):
        data = self._fetch("GET", f"{self.API}/hosts/domains?agent={urllib.parse.quote(AGENT)}", {}, None)
        inner = data.get("data", {}) if isinstance(data, dict) else {}
        return [*inner.get("hosts", []), *inner.get("redirectors", [])]

    def unlock(self, url):
        data = self._fetch("GET", f"{self.API}/link/unlock?agent={urllib.parse.quote(AGENT)}&link={urllib.parse.quote(url, safe='')}",
                           {"Authorization": f"Bearer {self.token}"}, None)
        inner = data.get("data") if isinstance(data, dict) and data.get("status") == "success" else None
        if not isinstance(inner, dict) or not inner.get("link"):
            raise DebridError(_api_message(data) or "no link returned")
        return Unlocked(str(inner["link"]), str(inner.get("filename") or ""), _int(inner.get("filesize")))


class Premiumize(Provider):
    key, name, site = "premiumize", "Premiumize", "premiumize.me"
    where = "https://www.premiumize.me/account"
    API = "https://www.premiumize.me/api"

    def _load_hosts(self):
        data = self._fetch("GET", f"{self.API}/services/list", {"Authorization": f"Bearer {self.token}"}, None)
        return list(data.get("directdl", [])) if isinstance(data, dict) else []

    def unlock(self, url):
        data = self._fetch("POST", f"{self.API}/transfer/directdl", {"Authorization": f"Bearer {self.token}"}, _form(src=url))
        content = data.get("content") if isinstance(data, dict) and data.get("status") == "success" else None
        if not content or not isinstance(content[0], dict) or not content[0].get("link"):
            raise DebridError(_api_message(data) or "no link returned")
        first = content[0]
        return Unlocked(str(first["link"]), str(first.get("path") or "").rsplit("/", 1)[-1], _int(first.get("size")))

    torrent_api = True

    def torrent_cached(self, hashes):
        query = "&".join(f"items[]={h}" for h in hashes)
        data = self._fetch("GET", f"{self.API}/cache/check?{query}", {"Authorization": f"Bearer {self.token}"}, None)
        flags = data.get("response") if isinstance(data, dict) and data.get("status") == "success" else None
        return {h for h, ok in zip(hashes, flags) if ok} if isinstance(flags, list) else set()

    def _direct(self, magnet: str) -> list[dict]:
        data = self._fetch("POST", f"{self.API}/transfer/directdl", {"Authorization": f"Bearer {self.token}"}, _form(src=magnet))
        content = data.get("content") if isinstance(data, dict) and data.get("status") == "success" else None
        if not isinstance(content, list) or not content:
            raise DebridError(_api_message(data) or "not cached")
        return [c for c in content if isinstance(c, dict)]

    def torrent_list(self, ih, magnet):
        return [DebridFile(str(i), str(c.get("path", "")).lstrip("/"), _int(c.get("size"))) for i, c in enumerate(self._direct(magnet))]

    def torrent_open(self, ih, magnet):
        return TorrentHandle(ih, self.torrent_list(ih, magnet))

    def torrent_link(self, handle, file_id):
        content = self._direct(f"magnet:?xt=urn:btih:{handle}")  # fresh links each time: they expire
        try:
            return str(content[int(file_id)]["link"])
        except (IndexError, ValueError, KeyError):
            raise DebridError("that file is no longer available") from None


class DebridLink(Provider):
    key, name, site = "debridlink", "Debrid-Link", "debrid-link.com"
    where = "https://debrid-link.com/webapp/apikey"
    API = "https://debrid-link.com/api/v2"

    def _load_hosts(self):
        data = self._fetch("GET", f"{self.API}/downloader/hostnames", {}, None)
        value = data.get("value") if isinstance(data, dict) else None
        return [h for h in value if isinstance(h, str)] if isinstance(value, list) else []

    def unlock(self, url):
        data = self._fetch("POST", f"{self.API}/downloader/add",
                           {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                           json.dumps({"url": url}).encode("utf-8"))
        value = data.get("value") if isinstance(data, dict) and data.get("success") else None
        if not isinstance(value, dict) or not value.get("downloadUrl"):
            raise DebridError(_api_message(data) or "no link returned")
        return Unlocked(str(value["downloadUrl"]), str(value.get("name") or ""), _int(value.get("size")))


class TorBoxService(Provider):
    """TorBox serves torrents and magnets (see torbox.py); it is listed here so every service lives in one place."""
    key, name, site = "torbox", "TorBox", "torbox.app"
    where = "https://torbox.app/settings"
    torrents, links = True, False


def _int(v) -> int | None:
    try:
        return int(v) if v is not None and int(v) > 0 else None
    except (TypeError, ValueError):
        return None


PROVIDERS: dict[str, type[Provider]] = {c.key: c for c in (RealDebrid, AllDebrid, Premiumize, DebridLink, TorBoxService)}
_LIVE: dict[tuple[str, str], Provider] = {}  # one instance per (service, key): its host list is cached


def active(keys: dict[str, dict]) -> list[Provider]:
    """The services that have an API key, in a fixed order (the order above)."""
    out = []
    for key, cls in PROVIDERS.items():
        token = (keys.get(key) or {}).get("apikey", "").strip()
        if token:
            out.append(_LIVE.setdefault((key, token), cls(token)))
    return out


def torrent_service(keys: dict[str, dict]) -> Provider | None:
    """The first service with a key that can serve torrents through this module (TorBox has its own code)."""
    return next((p for p in active(keys) if p.torrent_api), None)


def unlock(url: str, host: str, keys: dict[str, dict]) -> Unlocked | None:
    """Unlock `url` with the first service that supports its site; None when no service does (download it directly)."""
    errors = []
    for provider in active(keys):
        try:
            if not provider.supports(host):
                continue
            return provider.unlock(url)
        except DebridError as exc:
            errors.append(f"{provider.name}: {exc}")
    if errors:
        raise DebridError("; ".join(errors))
    return None
