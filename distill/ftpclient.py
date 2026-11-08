"""FTP and FTPS (explicit TLS) support: list a folder tree, and stream a file with resume. No Qt, and nothing here imports
the rest of the app, so downloads.py and links.py can both use it.

Addresses are `ftp://[user:password@]host[:port]/path` and `ftps://...`. Without a user the login is anonymous. A path that
ends in `/` (or turns out to be a folder) is listed, folders and all; a path to a file is one file. Downloads use passive
mode, and resume with REST where the server allows it. FTP does not go through the HTTP proxy rules."""
from __future__ import annotations

import ftplib
import posixpath
import re
import ssl
import urllib.parse
from dataclasses import dataclass
from typing import Callable

SCHEMES = ("ftp://", "ftps://")
TIMEOUT = 30
MAX_FILES = 5000
MAX_DEPTH = 6
ANONYMOUS = ("anonymous", "distill@example.com")


class FtpError(OSError):
    """An FTP failure with a plain message. `permanent` = retrying will not help (bad login, no such file)."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class Target:
    secure: bool
    host: str
    port: int
    user: str
    password: str
    path: str  # unquoted, always starts with '/'
    netloc: str  # as written in the address (user:password@host:port), used to build the files' own addresses


def is_ftp(url: str) -> bool:
    return url.lower().startswith(SCHEMES)


def parse(url: str, creds: tuple[str, str] | None = None) -> Target:
    """Split an ftp:// or ftps:// address. `creds` (a saved login) is used when the address has none of its own."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("ftp", "ftps") or not parts.hostname:
        raise FtpError(f"{url}: not an FTP address", permanent=True)
    secure = parts.scheme.lower() == "ftps"
    try:
        port = parts.port or 21
    except ValueError:
        raise FtpError(f"{url}: bad port number", permanent=True) from None
    if parts.username:
        user, password = urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or "")
    else:
        user, password = creds or ANONYMOUS
    path = urllib.parse.unquote(parts.path) or "/"
    return Target(secure, parts.hostname, port, user, password, path if path.startswith("/") else "/" + path, parts.netloc)


def _message(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return "FTP: " + text.splitlines()[0][:160]


def connect(t: Target, timeout: float = TIMEOUT) -> ftplib.FTP:
    """A logged-in, passive, binary connection. Raises FtpError."""
    ftp: ftplib.FTP = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=timeout) if t.secure else ftplib.FTP(timeout=timeout)
    ftp.encoding = "utf-8"
    try:
        ftp.connect(t.host, t.port)
        ftp.login(t.user, t.password)
        if t.secure:
            ftp.prot_p()  # encrypt the data connection too, not just the login
        ftp.set_pasv(True)
        ftp.voidcmd("TYPE I")
        return ftp
    except ftplib.error_perm as exc:
        _quiet_close(ftp)
        raise FtpError(_message(exc), permanent=True) from None
    except (ftplib.Error, OSError, EOFError, ssl.SSLError) as exc:
        _quiet_close(ftp)
        raise FtpError(_message(exc)) from None


def _quiet_close(ftp: ftplib.FTP) -> None:
    try:
        ftp.close()
    except Exception:  # noqa: BLE001  (closing a half-open connection can fail in any number of ways)
        pass


# ---- listing -------------------------------------------------------------------------------------------------------
_DOS = re.compile(r"^\d\d-\d\d-\d{2,4}\s+\d\d:\d\d\s*[AP]?M?\s+(<DIR>|\d+)\s+(.+)$", re.IGNORECASE)


def parse_list_line(line: str) -> tuple[str, bool, int | None] | None:
    """One line of a LIST reply (Unix or DOS style) -> (name, is_folder, size), or None for anything else."""
    line = line.rstrip("\r\n")
    if m := _DOS.match(line):
        return m.group(2), m.group(1).upper() == "<DIR>", None if m.group(1).upper() == "<DIR>" else int(m.group(1))
    cols = line.split(None, 8)
    if len(cols) == 9 and cols[0][:1] in "-dl" and len(cols[0]) >= 10 and cols[4].isdigit():
        if cols[0][0] == "l":
            return None  # a link: it may point anywhere, so it is not followed
        return cols[8], cols[0][0] == "d", None if cols[0][0] == "d" else int(cols[4])
    return None


def _entries(ftp: ftplib.FTP, folder: str) -> list[tuple[str, bool, int | None]]:
    """(name, is_folder, size) for what is in a folder: MLSD when the server has it, LIST when it doesn't."""
    try:
        out = []
        for name, facts in ftp.mlsd(folder, facts=["type", "size"]):
            kind = facts.get("type", "").lower()
            if kind in ("cdir", "pdir") or name in (".", ".."):
                continue
            size = facts.get("size", "")
            out.append((name, kind == "dir", int(size) if size.isdigit() else None))
        return out
    except ftplib.error_perm:
        pass  # no MLSD here
    lines: list[str] = []
    try:
        ftp.retrlines("LIST " + folder if folder not in ("", "/") else "LIST", lines.append)
    except ftplib.Error as exc:
        raise FtpError(_message(exc), permanent=isinstance(exc, ftplib.error_perm)) from None
    out = []
    for line in lines:
        if (row := parse_list_line(line)) and row[0] not in (".", ".."):
            out.append(row)
    return out


def _is_folder(ftp: ftplib.FTP, path: str) -> bool:
    try:
        ftp.cwd(path)
    except ftplib.error_perm:
        return False
    try:
        ftp.cwd("/")
    except ftplib.Error:
        pass
    return True


def _size(ftp: ftplib.FTP, path: str) -> int | None:
    try:
        return ftp.size(path)
    except (ftplib.Error, OSError):
        return None


def _quote(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def list_files(url: str, creds: tuple[str, str] | None = None, max_files: int = MAX_FILES, max_depth: int = MAX_DEPTH,
               should_stop: Callable[[], bool] = lambda: False) -> tuple[str, list[tuple[str, str, int | None]]]:
    """(a name for the group, [(name relative to the listed folder, the file's own address, size)]) for a file or a folder
    tree. Raises FtpError."""
    t = parse(url, creds)
    scheme = "ftps" if t.secure else "ftp"
    ftp = connect(t)
    try:
        if not (t.path.endswith("/") or t.path == "/" or _is_folder(ftp, t.path)):
            return t.host, [(posixpath.basename(t.path), url, _size(ftp, t.path))]
        root = t.path.rstrip("/") or ""
        found: list[tuple[str, str, int | None]] = []
        todo = [(root, 0)]
        while todo and len(found) < max_files and not should_stop():
            folder, depth = todo.pop(0)
            for name, is_dir, size in _entries(ftp, folder or "/"):
                full = f"{folder}/{name}"
                if is_dir:
                    if depth < max_depth:
                        todo.append((full, depth + 1))
                else:
                    found.append((full[len(root) + 1:], f"{scheme}://{t.netloc}{_quote(full)}", size))
                    if len(found) >= max_files:
                        break
        group = posixpath.basename(root) or t.host
        return group, found
    except ftplib.Error as exc:
        raise FtpError(_message(exc), permanent=isinstance(exc, ftplib.error_perm)) from None
    except (OSError, EOFError) as exc:
        if isinstance(exc, FtpError):
            raise
        raise FtpError(_message(exc)) from None
    finally:
        _quiet_close(ftp)


# ---- downloading ---------------------------------------------------------------------------------------------------
class FtpStream:
    """A file being sent, shaped like the HTTP response the download code already reads: .status (206 when it resumed),
    .headers.get('Content-Length'), .read(n) (b'' at the end) and use as a context manager."""

    def __init__(self, url: str, rest: int = 0, creds: tuple[str, str] | None = None):
        t = parse(url, creds)
        self._ftp = connect(t)
        self._conn = None
        self._finished = False
        try:
            size = _size(self._ftp, t.path)
            try:
                self._conn = self._ftp.transfercmd("RETR " + t.path, rest=rest or None)
            except ftplib.error_perm as exc:
                if rest and str(exc).startswith(("501", "502", "504", "554")):
                    raise FtpError("FTP: this server can't resume a download", permanent=False) from None
                raise FtpError(_message(exc), permanent=True) from None
        except ftplib.Error as exc:
            self.close()
            raise FtpError(_message(exc)) from None
        except BaseException:
            self.close()
            raise
        self.status = 206 if rest else 200
        self.headers = {"Content-Length": str(max(0, size - rest))} if size is not None else {}
        self.size = size

    def read(self, n: int = 65536) -> bytes:
        try:
            data = self._conn.recv(n)  # type: ignore[union-attr]
        except (OSError, ssl.SSLError) as exc:
            raise FtpError(_message(exc)) from None
        if not data:
            self._finished = True
        return data

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            if self._finished:
                try:
                    self._ftp.voidresp()  # the server's "226 transfer complete"
                except Exception:  # noqa: BLE001
                    pass
            self._conn = None
        _quiet_close(self._ftp)

    def __enter__(self) -> "FtpStream":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
