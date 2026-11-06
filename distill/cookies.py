"""Cookies for sites that need a browser sign-in: read a cookies.txt export (Netscape format) or Firefox's cookie store,
and turn the ones you pick into per-site 'Cookie' logins. Also signs in through a plain web form. No Qt in here.

Chrome, Edge and Brave encrypt their cookies in a way other programs can't read, so use a cookies.txt export
extension for those. Cookies from a browser session expire: re-import when a site starts refusing."""
from __future__ import annotations

import glob
import http.cookiejar
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .constants import APP_NAME, APP_VERSION


@dataclass(slots=True)
class Cookie:
    domain: str  # without the leading dot
    name: str
    value: str
    expires: int = 0  # unix seconds, 0 = session cookie
    secure: bool = False

    @property
    def expired(self) -> bool:
        return bool(self.expires) and self.expires < time.time()


def parse_netscape(text: str) -> list[Cookie]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _sub, _path, secure, expires, name, value = parts[:7]
        try:
            exp = int(float(expires or 0))
        except ValueError:
            exp = 0
        if exp > 10**11:  # some exporters write milliseconds
            exp //= 1000
        if domain.strip(".") and name:
            out.append(Cookie(domain.strip().lstrip(".").lower(), name, value, exp, secure.upper() == "TRUE"))
    return out


def firefox_profiles() -> list[Path]:
    home = Path.home()
    roots = {"win32": [Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"],
             "darwin": [home / "Library" / "Application Support" / "Firefox" / "Profiles"]}.get(
        sys.platform, [home / ".mozilla" / "firefox", home / "snap" / "firefox" / "common" / ".mozilla" / "firefox"])
    found = [Path(p) for root in roots for p in glob.glob(str(root / "*" / "cookies.sqlite"))]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)  # the profile used most recently first


def read_firefox(db: Path) -> list[Cookie]:
    """Copy the store first: Firefox keeps it locked while it runs."""
    with tempfile.TemporaryDirectory(prefix="distill_ck_") as tmp:
        copy = Path(tmp) / "cookies.sqlite"
        shutil.copyfile(db, copy)
        for extra in ("-wal", "-shm"):
            if (db.parent / (db.name + extra)).exists():
                shutil.copyfile(db.parent / (db.name + extra), Path(tmp) / ("cookies.sqlite" + extra))
        con = sqlite3.connect(str(copy))
        try:
            rows = con.execute("SELECT host, name, value, expiry, isSecure FROM moz_cookies").fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"Can't read Firefox cookies: {exc}") from exc
        finally:
            con.close()
    out = []
    for host, name, value, expiry, secure in rows:
        exp = int(expiry or 0)
        if exp > 10**11:
            exp //= 1000
        out.append(Cookie(str(host).lstrip(".").lower(), str(name), str(value), exp, bool(secure)))
    return out


def group_by_domain(cookies: list[Cookie]) -> dict[str, list[Cookie]]:
    groups: dict[str, list[Cookie]] = {}
    for c in cookies:
        if not c.expired:
            groups.setdefault(c.domain, []).append(c)
    return dict(sorted(groups.items()))


def to_logins(groups: dict[str, list[Cookie]], chosen: set[str]) -> list[dict]:
    """Custom-login entries ('Cookie' kind) for the chosen domains."""
    return [{"host": d, "kind": "Cookie", "user": "", "secret": "; ".join(f"{c.name}={c.value}" for c in cs)}
            for d, cs in groups.items() if d in chosen and cs]


def merge_logins(existing: list[dict], new: list[dict]) -> list[dict]:
    """New entries replace old ones for the same host; the list stays most-specific-site first, which is the order the
    downloader looks them up in (so 'dl.example.com' wins over 'example.com')."""
    hosts = {e["host"] for e in new}
    merged = [e for e in existing if e.get("host") not in hosts] + new
    return sorted(merged, key=lambda e: -len(e.get("host", "")))


def form_login(login_url: str, fields: dict[str, str], timeout: float = 30) -> tuple[str, str]:
    """Sign in through a plain HTML form: load the page (for its starting cookies), POST the fields, and return
    (host, 'a=1; b=2') for the cookies the site set. Sites with CSRF tokens, captchas or JavaScript logins won't work."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"User-Agent": f"{APP_NAME}/{APP_VERSION}"}
    host = (urllib.parse.urlsplit(login_url).hostname or "").lower()
    if not host:
        raise ValueError("Enter the full login address (https://...)")
    try:
        with opener.open(urllib.request.Request(login_url, headers=headers), timeout=timeout):
            pass
        body = urllib.parse.urlencode(fields).encode("utf-8")
        with opener.open(urllib.request.Request(login_url, data=body, headers=headers), timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ValueError(f"The site answered HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise ValueError(str(exc) or type(exc).__name__) from None
    pairs = [f"{c.name}={c.value}" for c in jar if host.endswith(c.domain.lstrip(".")) or c.domain.lstrip(".").endswith(host)]
    if not pairs:
        raise ValueError("The site set no cookies: the details were refused, or it doesn't use a plain form login")
    return host, "; ".join(pairs)
