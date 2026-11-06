"""Turning what the person pasted (archive.org item, directory listing, 1fichier, magnet, .torrent) into
a list of downloadable files."""
from __future__ import annotations

import http.client
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import debrid, hosters, ytdlp
from .downloads import (
    http_open, LINKS_NEED_DEBRID, LinkError, list_1fichier, list_archive_item,
    list_open_directory, LOGINS, parse_archive_url, RemoteFile, site_for,
)
from .netrules import host_of
from .torbox import torbox_list_magnet
from .torrents import (
    ENGINE, import_libtorrent, is_torrent_token, load_torrent_settings,
    NEEDS_LIBTORRENT, torrent_files_from_bytes,
)


def resolve_links(text: str) -> tuple[list[RemoteFile], list[str]]:
    """Turn pasted text (any number of links) into one de-duplicated file list plus per-link problems."""
    files, errors, seen = [], [], set()
    for token in text.split():
        try:
            if is_torrent_token(token):
                found = list_torrent(token)
                for f in found:
                    if f.url not in seen:
                        seen.add(f.url)
                        files.append(f)
                continue
            if ytdlp.is_video_url(token) or token.lower().startswith(ytdlp.FORCE_PREFIXES):
                try:
                    found = ytdlp.list_entries(token)
                except ytdlp.YtdlpError as exc:
                    raise LinkError(str(exc)) from None
                for f in found:
                    if f.url not in seen:
                        seen.add(f.url)
                        files.append(f)
                continue
            parsed = parse_archive_url(token)
            full = token if "://" in token else "https://" + token
            if parsed:
                found = list_archive_item(*parsed)
            elif site_for(full) == "1fichier" and (LOGINS.get("1fichier") or {}).get("apikey"):
                found = list_1fichier(full)
            elif unlocked := _debrid_entry(full):  # a debrid service supports this site: name and size come from it
                found = [unlocked]
            elif site_for(full) == "1fichier":
                found = list_1fichier(full)
            elif handler := hosters.hoster_for(full):
                found = [hosters.entry(handler, full)]
            elif host_of(full).endswith(LINKS_NEED_DEBRID):
                raise LinkError(f"{host_of(full)}: this host needs a debrid service (Real-Debrid, AllDebrid or Premiumize "
                                "support it). Add one in Settings > Accounts.")
            else:
                found = list_open_directory(full)
        except LinkError as exc:
            errors.append(str(exc))
            continue
        for f in found:
            if f.url not in seen:
                seen.add(f.url)
                files.append(f)
    return files, errors


def _debrid_entry(url: str) -> RemoteFile | None:
    """A list row for a link a debrid service supports, with the file name and size the service reports."""
    host = host_of(url)
    try:
        got = debrid.unlock(url, host, LOGINS)
    except debrid.DebridError as exc:
        raise LinkError(str(exc)) from None
    if not got:
        return None
    name = got.name or urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]) or "download"
    return RemoteFile(name, url, got.size, host, None, False, True)


def probe_sizes(files: list[RemoteFile], on_size: Callable[[int, int], None], workers: int = 8) -> None:
    """HEAD every file whose size the listing didn't give, reporting each as it arrives."""
    def head(i: int) -> None:
        url = files[i].url
        if url.startswith(("torrent:", ytdlp.PREFIX)) or site_for(url) == "1fichier" or hosters.hoster_for(url):
            return  # sizes already known (or unknowable by HEAD), and these aren't plain web files
        try:
            with http_open(files[i].url, method="HEAD", timeout=15) as resp:
                n = resp.headers.get("Content-Length", "")
                if n.isdigit():
                    on_size(i, int(n))
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()

    todo = [i for i, f in enumerate(files) if f.size is None]
    with ThreadPoolExecutor(workers, thread_name_prefix="probe") as pool:
        list(pool.map(head, todo))


def _debrid_list_magnet(magnet: str) -> list[RemoteFile] | None:
    """The file list of a magnet a debrid service (Premiumize) has cached, or None: then the swarm is asked."""
    from .torbox import magnet_hash  # noqa: PLC0415
    service, ih = debrid.torrent_service(LOGINS), magnet_hash(magnet)
    if service is None or not ih:
        return None
    try:
        if ih not in service.torrent_cached([ih]):
            return None
        listed = service.torrent_list(ih, magnet)
    except debrid.DebridError:
        return None
    if not listed:
        return None
    group = magnet_name(magnet) or ih
    return [RemoteFile(f.name, f"torrent:{ih}:{i}", f.size, group, source=service.name) for i, f in enumerate(listed) if f.name]


def magnet_name(magnet: str) -> str:
    m = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query).get("dn")
    return m[0] if m else ""


def list_torrent(token: str) -> list[RemoteFile]:
    token = token.strip("\"'")
    is_magnet = token.lower().startswith("magnet:")
    if is_magnet and "xt=urn:bt" not in token.lower():
        raise LinkError("That magnet link isn't valid (no torrent id in it).")
    if is_magnet and (cached := torbox_list_magnet(token)):  # cached on TorBox: no swarm, no VPN needed
        return cached
    if is_magnet and (cached := _debrid_list_magnet(token)):  # cached on another debrid service
        return cached
    lt = import_libtorrent()
    if lt is None:
        raise LinkError(NEEDS_LIBTORRENT.replace("\n", " "))
    if is_magnet:
        ENGINE.ensure(load_torrent_settings())  # the VPN check happens before anything touches the network
        data = ENGINE.fetch_metadata(token)
    else:
        path = Path(urllib.request.url2pathname(urllib.parse.urlsplit(token).path)) if token.lower().startswith("file:") \
            else Path(token)
        try:
            if path.stat().st_size > 20 << 20:
                raise LinkError(f"{path.name} is too big to be a .torrent file.")
            data = path.read_bytes()
        except OSError as exc:
            raise LinkError(f"Can't read {path.name}: {exc.strerror or exc}") from None
    return torrent_files_from_bytes(lt, data)
