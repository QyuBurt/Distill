"""Direct handlers for the file hosts people actually paste: Google Drive, MediaFire, Dropbox and OneDrive.

Each handler turns a share link into the real download address (`direct`) at download time, so short-lived addresses
never go stale in a saved list, and `entry` builds the list row (file name and size from the server's own headers).
Google Drive and OneDrive folders, Mega, and anything needing a sign-in or captcha are not handled here: a debrid
service covers Mega, Google Drive and many more. No Qt in here."""
from __future__ import annotations

import base64
import binascii
import html
import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from .downloads import LinkError, RemoteFile, describe_error, http_open

_GDRIVE_ID = re.compile(r"/file/d/([\w-]{10,})|[?&]id=([\w-]{10,})")


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _is(host: str, *names: str) -> bool:
    return any(host == n or host.endswith("." + n) for n in names)


# ---- Dropbox, OneDrive, Google Drive: the address can be worked out without asking the site -------------------------
def dropbox_direct(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if "/scl/fo/" in parts.path or parts.path.startswith("/sh/"):
        raise LinkError("Dropbox folder: open it and paste the links of the files inside (or use a debrid service)")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "dl"]
    return urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc, parts.path, urllib.parse.urlencode(query + [("dl", "1")]), ""))


def onedrive_direct(url: str) -> str:
    token = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"https://api.onedrive.com/v1.0/shares/u!{token}/root/content"


def gdrive_direct(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if "/folders/" in parts.path or "/drive/folders" in parts.path:
        raise LinkError("Google Drive folder: folders can't be listed here. Paste the links of the files inside, "
                        "or use a debrid service")
    m = _GDRIVE_ID.search(parts.path + ("?" + parts.query if parts.query else ""))
    if not m:
        raise LinkError("Google Drive: couldn't find the file ID in that link")
    return "https://drive.usercontent.google.com/download?" + urllib.parse.urlencode(
        {"id": m.group(1) or m.group(2), "export": "download", "confirm": "t"})


# ---- MediaFire: the page holds the real address ------------------------------------------------------------------------
_MF_BUTTON = re.compile(r"<a\b[^>]*\bid=[\"']downloadButton[\"'][^>]*>", re.I)
_MF_HREF = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.I)
_MF_SCRAMBLED = re.compile(r"data-scrambled-url=[\"']([^\"']+)[\"']", re.I)
_MF_LABEL = re.compile(r"<a\b[^>]*aria-label=[\"']Download file[\"'][^>]*>", re.I)


def mediafire_find_link(page: str) -> str | None:
    """The direct address on a MediaFire file page, or None."""
    if m := _MF_SCRAMBLED.search(page):  # newer pages hide it in base64
        with_pad = m.group(1) + "=" * (-len(m.group(1)) % 4)
        try:
            url = base64.b64decode(with_pad).decode("utf-8")
            if url.startswith("http"):
                return url
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
    for pattern in (_MF_BUTTON, _MF_LABEL):
        if tag := pattern.search(page):
            if href := _MF_HREF.search(tag.group(0)):
                url = html.unescape(href.group(1))
                if url.startswith("http") and "javascript" not in url:
                    return url
    return None


def _fetch_page(url: str) -> str:
    try:
        with http_open(url) as resp:
            return resp.read(2 << 20).decode("utf-8", "replace")
    except (OSError, ValueError) as exc:
        raise LinkError(f"MediaFire: {describe_error(exc)}") from None


def mediafire_direct(url: str, fetch: Callable[[str], str] | None = None) -> str:
    if "/folder/" in urllib.parse.urlsplit(url).path:
        raise LinkError("MediaFire folder: paste the links of the files inside (or use a debrid service)")
    link = mediafire_find_link((fetch or _fetch_page)(url))
    if not link:
        raise LinkError("MediaFire: no download link on the page (file removed, or the page layout changed)")
    return link


@dataclass(frozen=True, slots=True)
class Hoster:
    name: str
    hosts: tuple[str, ...]
    direct: Callable[[str], str]


HOSTERS = (
    Hoster("Google Drive", ("drive.google.com", "drive.usercontent.google.com"), gdrive_direct),
    Hoster("MediaFire", ("mediafire.com",), mediafire_direct),
    Hoster("Dropbox", ("dropbox.com",), dropbox_direct),
    Hoster("OneDrive", ("1drv.ms", "onedrive.live.com"), onedrive_direct),
)


def hoster_for(url: str) -> Hoster | None:
    host = _host(url)
    return next((h for h in HOSTERS if _is(host, *h.hosts)), None)


# ---- the list row ----------------------------------------------------------------------------------------------------------
def parse_disposition(value: str) -> str:
    """The file name in a Content-Disposition header ('' if none)."""
    if m := re.search(r"filename\*\s*=\s*[\w-]+'[^']*'([^;]+)", value, re.I):
        return urllib.parse.unquote(m.group(1).strip().strip('"'))
    if m := re.search(r'filename\s*=\s*"([^"]+)"', value, re.I) or re.search(r"filename\s*=\s*([^;]+)", value, re.I):
        return m.group(1).strip()
    return ""


def probe(direct: str, who: str) -> tuple[str, int | None]:
    """(file name, size) from the server's headers, without downloading the file."""
    try:
        with http_open(direct, {"Range": "bytes=0-0"}) as resp:
            kind = (resp.headers.get("Content-Type") or "").lower()
            if kind.startswith("text/html"):
                raise LinkError(f"{who}: the site showed a web page instead of the file (private, quota exceeded, "
                                "or it needs a sign-in)")
            name = parse_disposition(resp.headers.get("Content-Disposition") or "")
            rng = resp.headers.get("Content-Range") or ""
            total = rng.rsplit("/", 1)[-1] if "/" in rng else resp.headers.get("Content-Length", "")
            return name, int(total) if str(total).isdigit() and int(total) > 1 else None
    except LinkError:
        raise
    except (OSError, ValueError) as exc:
        raise LinkError(f"{who}: {describe_error(exc)}") from None


def entry(hoster: Hoster, url: str) -> RemoteFile:
    direct = hoster.direct(url)
    name, size = probe(direct, hoster.name)
    if not name:
        name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]) or "download"
        if name == "file":  # MediaFire's /file/<id>/<name>/file
            name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 2)[-2])
    return RemoteFile(name, url, size, hoster.name, None, False, True)
