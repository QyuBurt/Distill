"""The link crawler: look through any web page and pick out the download links on it. No Qt in here.

Pasting `scan:https://example.com/roms` (or `scan[zip,7z]:https://...` to say which file types) makes the Download tab
fetch that page and list everything on it that looks like a download: links to files with a wanted extension, magnet
links, and links to hosts Distill can handle (Drive, MediaFire, Dropbox, OneDrive, Pixeldrain, 1fichier, archive.org).
An ordinary open directory listing needs no prefix; this is for the pages that aren't one.

Only the one page is read (links to other pages are not followed), and nothing is downloaded here."""
from __future__ import annotations

import http.client
import re
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Iterable

from . import hosters, ytdlp
from .clipwatch import FILE_EXTENSIONS, KNOWN_HOSTS
from .downloads import LinkError, describe_error, http_open, parse_archive_url, site_for

MAX_LINKS = 5000
MAX_PAGE = 8 << 20
ANY = "*"  # as an extension: every link that ends in some file name, whatever it is
SCAN = re.compile(r"^scan(?:\[([^\]]*)\])?:(\S+)$", re.IGNORECASE)
_TEXT_URL = re.compile(r"(?:https?://|ftps?://|magnet:\?)[^\s<>\"'\\]+", re.IGNORECASE)
# Pages and page furniture, not files: what ANY must not pick up
_PAGE_EXTENSIONS = (".html", ".htm", ".xhtml", ".php", ".asp", ".aspx", ".jsp", ".cgi", ".shtml", ".css", ".js",
                    ".json", ".xml", ".rss", ".atom", ".txt", ".ico", ".svg", ".map", ".woff", ".woff2", ".ttf")
_URL_ATTRS = {"a": ("href",), "area": ("href",), "source": ("src",), "video": ("src",), "audio": ("src",),
              "embed": ("src",), "img": ("src",), "object": ("data",)}
_DATA_ATTRS = ("data-href", "data-url", "data-src", "data-download", "data-link")


@dataclass(frozen=True, slots=True)
class ScanRequest:
    url: str
    exts: tuple[str, ...] | None  # None: the usual download types (clipwatch.FILE_EXTENSIONS)


def normalize_exts(exts: Iterable[str] | str | None) -> tuple[str, ...] | None:
    """'zip, .7z' or ['zip', '.7Z'] -> ('.zip', '.7z'); empty or None -> None (the usual types); '*' -> ('*',)."""
    if exts is None:
        return None
    parts = re.split(r"[\s,;|]+", exts) if isinstance(exts, str) else list(exts)
    out: list[str] = []
    for part in parts:
        part = str(part).strip().lower()
        if not part:
            continue
        part = ANY if part in (ANY, "all", "any") else (part if part.startswith(".") else "." + part)
        if part not in out:
            out.append(part)
    return tuple(out) or None


def parse_scan_token(token: str) -> ScanRequest | None:
    """'scan:URL' or 'scan[zip,7z]:URL' -> a ScanRequest; anything else -> None."""
    m = SCAN.match(token.strip("\"'"))
    if not m:
        return None
    url = m.group(2)
    return ScanRequest(url if "://" in url else "https://" + url, normalize_exts(m.group(1)))


def _last_segment(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])


def _extension(path: str) -> str:
    last = path.rstrip("/").rsplit("/", 1)[-1].lower()
    return "." + last.rsplit(".", 1)[-1] if "." in last.strip(".") else ""


def is_direct_file(url: str, exts: tuple[str, ...] | None = None) -> bool:
    """A web link to a file of a wanted type (judged by the end of its path)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https", "ftp", "ftps") or not parts.netloc or parts.path.endswith("/"):
        return False
    ext = _extension(parts.path)
    if not ext:
        return False
    wanted = exts or FILE_EXTENSIONS
    if ANY in wanted:
        return ext not in _PAGE_EXTENSIONS
    return ext in wanted


def _worth_taking(url: str, exts: tuple[str, ...] | None, videos: bool) -> bool:
    if url.lower().startswith("magnet:"):
        return "xt=urn:bt" in url.lower()
    if is_direct_file(url, exts):
        return True
    parts = urllib.parse.urlsplit(url)
    if parts.scheme in ("ftp", "ftps"):
        return bool(parts.netloc and parts.path.strip("/"))  # a file or folder on an FTP server (not just the server)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if not parts.path.strip("/") and not parts.query and not parts.fragment:
        return False  # a site's front page
    if videos and ytdlp.is_video_url(url):
        return True
    host = (parts.hostname or "").lower()
    if host.endswith("archive.org"):
        try:
            return parse_archive_url(url) is not None
        except LinkError:
            return False  # an archive.org link that isn't an item: the site's own navigation
    if hosters.hoster_for(url) or site_for(url) == "1fichier":
        return True
    return any(host == h or host.endswith("." + h) for h in KNOWN_HOSTS)


def _drop_fragment(url: str) -> str:
    """'page.zip#top' -> 'page.zip'. Mega keeps its key after the '#', so those and magnet links are left whole."""
    if url.lower().startswith("magnet:") or "mega." in (urllib.parse.urlsplit(url).hostname or ""):
        return url
    return url.split("#")[0]


class _Page(HTMLParser):
    """Every address a page points at, in the order it first does, plus its <title> and <base href>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.base = ""
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        d = {k: v for k, v in attrs if v}
        if tag == "base" and d.get("href") and not self.base:
            self.base = d["href"]
        if tag == "title":
            self._in_title = True
        for attr in _URL_ATTRS.get(tag, ()):
            if d.get(attr):
                self.urls.append(d[attr])
        for attr in _DATA_ATTRS:
            if d.get(attr):
                self.urls.append(d[attr])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 200:
            self.title += data


def find_links(page: str, page_url: str, exts: tuple[str, ...] | None = None, videos: bool = False) -> tuple[list[str], str]:
    """(download links in the page, in order and without repeats, the page's title). Relative links are made absolute."""
    parser = _Page()
    try:
        parser.feed(page)
        parser.close()
    except Exception:  # a page with broken markup still has links in the part that parsed
        pass
    base = urllib.parse.urljoin(page_url, parser.base) if parser.base else page_url
    candidates = [urllib.parse.urljoin(base, u.strip()) if not u.lower().startswith("magnet:") else u.strip() for u in parser.urls]
    candidates += [m.group(0).rstrip(".,;)]}") for m in _TEXT_URL.finditer(page)]  # addresses written out as text or in scripts
    out: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        url = _drop_fragment(url)
        if url in seen or not _worth_taking(url, exts, videos):
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= MAX_LINKS:
            break
    return out, " ".join(parser.title.split())


def scan_page(url: str, exts: tuple[str, ...] | None = None, videos: bool = False,
              fetch: Callable[[str], tuple[str, str]] | None = None) -> tuple[list[str], str]:
    """Fetch `url` and return (its download links, its title). Raises LinkError with a plain reason.
    `fetch(url)` -> (page text, final url) lets tests run without a network."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise LinkError(f"{url}: not a web link")
    try:
        page, final = (fetch or _fetch)(url)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise LinkError(f"{url}: {describe_error(exc)}") from exc
    links, title = find_links(page, final, exts, videos)
    if not links:
        wanted = ", ".join(e.lstrip(".") for e in (exts or ())) or "download"
        raise LinkError(f"{url}: no {wanted} links found on that page")
    return links, title


def _fetch(url: str) -> tuple[str, str]:
    with http_open(url) as resp:
        kind = resp.headers.get("Content-Type", "").lower()
        if kind and "html" not in kind and "text" not in kind:
            raise LinkError(f"{url}: that address is a file, not a web page")
        data = resp.read(MAX_PAGE)
        return data.decode(resp.headers.get_content_charset() or "utf-8", "replace"), getattr(resp, "url", url) or url


def file_name(url: str) -> str:
    """A list-row name for a direct link: the last part of its path."""
    return _last_segment(url) or "download"


def unique_names(urls: list[str]) -> list[str]:
    """One name per link, kept as the file's own name unless two links share it: then the folder it sits in is put in
    front ('a/disc.zip', 'b/disc.zip'), and failing that a number, so one never overwrites the other."""
    names: list[str] = []
    used: set[str] = set()
    for url in urls:
        name = file_name(url)
        if name.lower() in used:
            segs = [urllib.parse.unquote(s) for s in urllib.parse.urlsplit(url).path.split("/") if s]
            if len(segs) > 1:
                name = f"{segs[-2]}/{name}"
        stem, dot, ext = name.rpartition(".")
        n = 2
        while name.lower() in used:
            name = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"
            n += 1
        used.add(name.lower())
        names.append(name)
    return names
