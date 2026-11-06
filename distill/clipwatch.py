"""Which copied text is worth capturing as download links. No Qt in here.

Copying any web address shouldn't fill the list, so only links that look like downloads are taken: magnets, links to
hosts Distill knows how to handle, and links to files with a download-type extension."""
from __future__ import annotations

import re
import urllib.parse

from . import hosters, ytdlp
from .torrents import is_torrent_token

_URL = re.compile(r"(?:https?://|magnet:\?)[^\s<>\"']+", re.IGNORECASE)
KNOWN_HOSTS = ("archive.org", "1fichier.com", "mega.nz", "mega.io", "mega.co.nz")
FILE_EXTENSIONS = (".zip", ".7z", ".rar", ".iso", ".bin", ".cue", ".chd", ".rvz", ".nsp", ".xci", ".pkg", ".torrent",
                   ".img", ".gba", ".nes", ".sfc", ".smc", ".gb", ".gbc", ".n64", ".z64", ".nds", ".3ds", ".cia", ".wbfs")
MAX_LINKS = 200


def links_in_text(text: str) -> list[str]:
    """Download-looking links in copied text, in order, without repeats."""
    out: list[str] = []
    for raw in _URL.findall(text or ""):
        url = raw.rstrip(".,;)]}")
        if _worth_taking(url) and url not in out:
            out.append(url)
        if len(out) >= MAX_LINKS:
            break
    return out


def _worth_taking(url: str) -> bool:
    if url.lower().startswith("magnet:") or is_torrent_token(url):
        return True
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in KNOWN_HOSTS):
        return True
    if hosters.hoster_for(url) or ytdlp.is_video_url(url):
        return True
    return parts.path.lower().endswith(FILE_EXTENSIONS)
