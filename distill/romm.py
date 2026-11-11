"""RomM (a self-hosted ROM manager) as a source of games for the Games page. No Qt in here.

What it does: list every game on the player's RomM server through its REST API, turn each into a catalog entry the Games page can
merge with the local library (`GameStore.sync_remote`), and download one to disk when the player installs it.

Sign-in is one text box: a RomM client API token (sent as a Bearer token), a `user:password` pair (Basic), or a session cookie written as
`name=value` (sent as a Cookie). The server address and the credential are kept by apikeys.py, in the sealed keys file.

Every call here blocks, so the page runs them on its own worker threads; nothing in this module touches the window.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .constants import APP_NAME

SOURCE = "romm"  # what a game from RomM has as its source
PAGE_SIZE = 500  # games asked for at a time
CHUNK = 1 << 20  # bytes read at a time while downloading
TIMEOUT = 30
ENTRY_ORDER = (".m3u", ".cue", ".gdi", ".chd", ".cso", ".rvz", ".iso", ".pbp", ".bin")  # what starts a multi-file game, best first
_SLUGS = {  # RomM's platform slugs the name lookup may not know
    "snes": "SNES", "sfam": "SNES", "nes": "NES", "famicom": "NES", "n64": "Nintendo 64", "gba": "Game Boy Advance", "gb": "Game Boy",
    "gbc": "Game Boy Color", "nds": "Nintendo DS", "nintendo-ds": "Nintendo DS", "3ds": "Nintendo 3DS", "ngc": "GameCube", "wii": "Wii",
    "genesis": "Mega Drive", "genesis-slash-megadrive": "Mega Drive", "sega-mega-drive": "Mega Drive", "megadrive": "Mega Drive",
    "sms": "Master System", "gamegear": "Game Gear", "segacd": "Sega CD", "saturn": "Sega Saturn", "dc": "Dreamcast", "psx": "PlayStation",
    "ps": "PlayStation", "ps2": "PlayStation 2", "psp": "PSP", "pce": "PC Engine", "tg16": "PC Engine", "ngp": "Neo Geo Pocket",
    "lynx": "Atari Lynx", "atari2600": "Atari 2600", "atari7800": "Atari 7800", "wonderswan": "WonderSwan", "virtualboy": "Virtual Boy",
}


class RommError(Exception):
    """Something the player can act on, in words."""


# ---- the address and the sign-in ------------------------------------------------------------------------------------
def normalize_url(text: str) -> str:
    """'romm.local:8080/' -> 'http://romm.local:8080'. Without a scheme, https, except for a name that only works on a home network
    (localhost, an IP address, .local, .lan, .home), which gets http. A trailing slash or '/api' is dropped."""
    text = (text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        host = text.split("/")[0].rsplit("@", 1)[-1].split(":")[0].lower()
        local = host == "localhost" or bool(re.fullmatch(r"[\d.]+", host)) or host.endswith((".local", ".lan", ".home", ".internal")) or "." not in host
        text = ("http://" if local else "https://") + text
    parts = urllib.parse.urlsplit(text)
    if not parts.netloc:
        return ""
    path = re.sub(r"/api/?$", "", parts.path.rstrip("/"))
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, "", "")).rstrip("/")


def server_key(url: str) -> str:
    """What names the server in the library (so two servers never share a game id): its host and port."""
    return urllib.parse.urlsplit(normalize_url(url)).netloc.lower()


def auth_headers(credential: str) -> dict[str, str]:
    """The header that signs a request in. 'rmm_...' (a client API token) is Bearer, 'user:password' is Basic, 'name=value' is a session cookie."""
    cred = (credential or "").strip()
    if not cred:
        return {}
    if re.fullmatch(r"[\w.-]+=[^;\s]+(;\s*[\w.-]+=[^;\s]*)*", cred) and not cred.startswith("rmm_"):
        return {"Cookie": cred}
    if ":" in cred and not cred.startswith("rmm_"):
        return {"Authorization": "Basic " + base64.b64encode(cred.encode("utf-8")).decode("ascii")}
    return {"Authorization": f"Bearer {cred}"}


# ---- what a game on the server looks like -----------------------------------------------------------------------------
@dataclass(slots=True)
class RemoteEntry:
    """One RomM game as the Games page keeps it."""
    remote_id: str
    title: str
    system: str
    path: str  # where it goes (or is) on this computer
    kind: str = ""
    region: str = ""
    size: int = 0
    folder: str = ""
    discs: int = 0
    cover_url: str = ""
    year: str = ""
    genre: str = ""
    developer: str = ""
    remote_name: str = ""  # the file name on the server (what the download address needs)
    remote_files: int = 1  # more than 1: the download is a .zip of the game's files


@dataclass(slots=True)
class RommRom:
    id: int
    name: str
    fs_name: str
    platform: str
    platform_slug: str = ""
    fs_slug: str = ""
    size: int = 0
    files: list[tuple[str, int]] = field(default_factory=list)  # (file name, size) of each file of the game
    cover: str = ""
    year: str = ""
    genres: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    multi: bool = False


def _text(value) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _names(value) -> list[str]:
    return [t for t in (_text(v.get("name") if isinstance(v, dict) else v) for v in (value if isinstance(value, list) else [])) if t]


def parse_rom(d: dict) -> RommRom | None:
    """A game from /api/roms; None when it has no id or file name to ask for."""
    if not isinstance(d, dict) or not d.get("id"):
        return None
    fs_name = _text(d.get("fs_name"))
    if not fs_name:
        return None
    files = []
    for f in d.get("files") or ():
        if isinstance(f, dict) and _text(f.get("file_name")):
            files.append((_text(f["file_name"]), int(f.get("file_size_bytes") or 0)))
    meta = d.get("metadatum") if isinstance(d.get("metadatum"), dict) else {}
    igdb = d.get("igdb_metadata") if isinstance(d.get("igdb_metadata"), dict) else {}
    released = meta.get("first_release_date") or igdb.get("first_release_date")
    year = ""
    if isinstance(released, (int, float)) and released > 0:
        import datetime  # noqa: PLC0415
        try:
            year = str(datetime.datetime.fromtimestamp(released / 1000 if released > 1e11 else released, datetime.timezone.utc).year)
        except (OverflowError, OSError, ValueError):
            year = ""
    cover = _text(d.get("url_cover")) or _text(d.get("path_cover_l")) or _text(d.get("path_cover_s"))
    return RommRom(int(d["id"]), _text(d.get("name")) or Path(fs_name).stem, fs_name,
                   _text(d.get("platform_display_name")) or _text(d.get("platform_name")) or _text(d.get("platform_slug")),
                   _text(d.get("platform_slug")), _text(d.get("platform_fs_slug")), int(d.get("fs_size_bytes") or 0), files, cover, year,
                   _names(meta.get("genres") or igdb.get("genres")), _names(meta.get("companies") or igdb.get("companies")), _names(d.get("regions")),
                   bool(d.get("has_multiple_files")) or len(files) > 1)


def system_of(rom: RommRom) -> str:
    """Distill's name for the rom's platform."""
    from . import games as gm  # noqa: PLC0415
    for text in (rom.platform, rom.platform_slug.replace("-", " ")):
        name = gm.canonical_system(text)
        if name and name != " ".join(text.split()):
            return name
    return _SLUGS.get(rom.platform_slug.lower()) or _SLUGS.get(rom.fs_slug.lower()) or gm.canonical_system(rom.platform) or gm.UNKNOWN


def safe_name(text: str) -> str:
    """A name that is safe as one file or folder name on any system."""
    out = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text or "").strip(" .")
    return out or "_"


def entry_name(rom: RommRom) -> str:
    """The file inside a multi-file game that starts it: a playlist or cue sheet if there is one, else the biggest file."""
    names = [n for n, _s in rom.files] or [rom.fs_name]
    for ext in ENTRY_ORDER:
        for n in names:
            if n.lower().endswith(ext):
                return n
    sizes = dict(rom.files)
    return max(names, key=lambda n: sizes.get(n, 0))


def destination(rom: RommRom, folder: Path) -> Path:
    """Where the game's file is (or will be) on this computer: <folder>/<platform>/<file>, or for a game of several files <folder>/<platform>/<game>/<entry>."""
    platform = folder / safe_name(rom.fs_slug or rom.platform_slug or rom.platform)
    if rom.multi:
        return platform / safe_name(Path(rom.fs_name).stem if "." in rom.fs_name else rom.fs_name) / safe_name(entry_name(rom))
    return platform / safe_name(rom.fs_name)


def to_entry(rom: RommRom, base_url: str, folder: Path) -> RemoteEntry:
    dest = destination(rom, folder)
    cover = rom.cover
    if cover and not cover.startswith(("http://", "https://")):
        cover = base_url.rstrip("/") + "/" + cover.lstrip("/")
    ext = Path(entry_name(rom) if rom.multi else rom.fs_name).suffix.lstrip(".").upper()
    return RemoteEntry(str(rom.id), rom.name, system_of(rom), str(dest), ext, ", ".join(rom.regions[:1]), rom.size, str(dest.parent), 0, cover,
                       rom.year, ", ".join(rom.genres[:3]), rom.companies[0] if rom.companies else "", rom.fs_name, max(1, len(rom.files)) if rom.multi else 1)


# ---- talking to the server ---------------------------------------------------------------------------------------------
class Client:
    def __init__(self, url: str, credential: str):
        self.base = normalize_url(url)
        self.headers = {"User-Agent": f"{APP_NAME}-romm", "Accept": "application/json", **auth_headers(credential)}
        if not self.base:
            raise RommError("Enter your RomM server's address first (Settings > API keys > Games & artwork).")
        if not credential.strip():
            raise RommError("Enter your RomM API token first (Settings > API keys > Games & artwork).")

    def _open(self, path: str, params: dict | None = None, timeout: int = TIMEOUT):
        url = self.base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=self.headers), timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RommError("RomM didn't accept that token. Make a client API token in RomM (your profile > Client API tokens) and paste it here.") from exc
            if exc.code == 404:
                raise RommError("RomM answered 404: check the address (it should be the one you open RomM at, without /api).") from exc
            raise RommError(f"RomM answered {exc.code}.") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RommError(f"Couldn't reach RomM at {self.base}.") from exc

    def get_json(self, path: str, params: dict | None = None):
        with self._open(path, params) as resp:
            try:
                return json.loads(resp.read().decode("utf-8", "replace"))
            except ValueError as exc:
                raise RommError("That address answered, but it doesn't look like RomM.") from exc

    def test(self) -> str:
        """'' when the server is RomM and takes the sign-in, else a sentence saying what is wrong."""
        try:
            self.get_json("/api/heartbeat")
            platforms = self.get_json("/api/platforms")
        except RommError as exc:
            return str(exc)
        return "" if isinstance(platforms, list) else "That address answered, but it doesn't look like RomM."

    def roms(self, note: Callable[[str], None] | None = None, stop: threading.Event | None = None) -> list[RommRom]:
        """Every game on the server, a page at a time."""
        out: list[RommRom] = []
        offset, total = 0, None
        while total is None or offset < total:
            if stop is not None and stop.is_set():
                break
            data = self.get_json("/api/roms", {"limit": PAGE_SIZE, "offset": offset, "order_by": "id", "order_dir": "asc"})
            if isinstance(data, dict):
                items, total = data.get("items") or [], int(data.get("total") or 0)
            elif isinstance(data, list):  # older servers answer with the bare list
                items, total = data, offset + len(data)
            else:
                raise RommError("That address answered, but it doesn't look like RomM.")
            out.extend(r for r in (parse_rom(d) for d in items) if r is not None)
            offset += len(items)
            if note:
                note(f"Reading RomM: {len(out)}" + (f" of {total}" if total else "") + " games...")
            if not items:
                break
        return out

    def cover(self, url: str) -> bytes | None:
        """A cover picture, or None (a cover is never worth an error)."""
        try:
            req = urllib.request.Request(url, headers={k: v for k, v in self.headers.items() if k != "Accept"})
            if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(self.base).netloc:
                req = urllib.request.Request(url, headers={"User-Agent": self.headers["User-Agent"]})  # never send the sign-in to another host
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read(12_000_000)
            return data if len(data) > 1024 else None
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def download(self, remote_id: str, remote_name: str, files: int, dest: Path, progress: Callable[[int, int], None], stop: threading.Event,
                 total_hint: int = 0) -> Path:
        """Save a game to `dest` (a .zip of several files is unpacked beside it). Written under a temporary name and moved into place when
        whole, so a failed or cancelled download never leaves half a game that looks installed. Returns the file the game starts from."""
        folder = dest.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RommError(f"Couldn't make the folder {folder}: {exc}") from exc
        target = folder / (safe_name(remote_name) + ".zip") if files > 1 else dest
        part = target.with_name(target.name + ".part")
        done, total = 0, total_hint
        try:
            with self._open(f"/api/roms/{urllib.parse.quote(str(remote_id))}/content/{urllib.parse.quote(remote_name)}", timeout=60) as resp, part.open("wb") as out:
                total = int(resp.headers.get("Content-Length") or 0) or total_hint
                progress(0, total)
                while chunk := resp.read(CHUNK):
                    if stop.is_set():
                        raise RommError("Cancelled.")
                    out.write(chunk)
                    done += len(chunk)
                    progress(done, max(total, done))
            if done == 0:
                raise RommError("RomM sent an empty file.")
            part.replace(target)
        except RommError:
            part.unlink(missing_ok=True)
            raise
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise RommError(f"Couldn't save the game: {exc}") from exc
        if files > 1:
            try:
                unpack(target, folder)
            finally:
                target.unlink(missing_ok=True)
            if not dest.exists():  # the entry file wasn't where the listing said: take the best file there is
                found = pick_entry(folder)
                if found is None:
                    raise RommError("The download had no game file in it.")
                return found
        return dest


def unpack(archive: Path, folder: Path) -> None:
    """Unzip into `folder`, refusing anything that would land outside it."""
    root = folder.resolve()
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                out = (folder / info.filename).resolve()
                if out != root and root not in out.parents:
                    raise RommError("The download had a file that wanted to go outside its folder, so it was not unpacked.")
            z.extractall(folder)
    except zipfile.BadZipFile as exc:
        raise RommError("The download wasn't a complete zip file.") from exc
    except OSError as exc:
        raise RommError(f"Couldn't unpack the game: {exc}") from exc


def pick_entry(folder: Path) -> Path | None:
    """The file in a folder that starts the game: a playlist or cue sheet, else the biggest."""
    files = [p for p in folder.rglob("*") if p.is_file()]
    for ext in ENTRY_ORDER:
        for p in sorted(files):
            if p.suffix.lower() == ext:
                return p
    return max(files, key=lambda p: p.stat().st_size, default=None)


def fetch_library(url: str, credential: str, folder: Path, note: Callable[[str], None] | None = None,
                  stop: threading.Event | None = None) -> list[RemoteEntry]:
    """Every game on the server as catalog entries, with the paths they would be saved at."""
    client = Client(url, credential)
    return [to_entry(r, client.base, folder) for r in client.roms(note, stop)]
