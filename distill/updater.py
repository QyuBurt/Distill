"""Checks GitHub for a newer Distill release and installs it (Windows builds). Qt-free; the prompt lives in ui/update_prompt.py."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .constants import APP_DIR, APP_NAME, APP_VERSION, SUBPROCESS_FLAGS
from .github_auth import github_headers

REPO = "QyuBurt/Distill"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_ENABLED = os.environ.get("DISTILL_NO_UPDATE_CHECK") != "1"  # tests and CI switch this off
USER_AGENT = f"{APP_NAME}-updater"


@dataclass(frozen=True, slots=True)
class Update:
    tag: str
    notes: str
    page: str  # the release's web page
    asset_url: str  # "" when this kind of install can't update itself
    asset_name: str
    asset_size: int
    sha256: str | None


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.0.2' -> (1, 0, 2); anything unreadable -> ()."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(p) for p in m[1].split(".")) if m else ()


def is_newer(tag: str, current: str = APP_VERSION) -> bool:
    new, old = parse_version(tag), parse_version(current)
    if not new or not old:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def install_kind() -> str:
    """'portable' (folder build), 'onefile' (single exe) or 'source' (run from a checkout: no self-update)."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return "source"
    return "portable" if (APP_DIR / "_internal").is_dir() else "onefile"


def _wanted_asset(kind: str) -> str:
    return {"portable": "Distill-portable.zip", "onefile": "Distill.exe"}.get(kind, "")


def _get(url: str, accept: str = "application/vnd.github+json", timeout: int = 10) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if url.startswith("https://api.github.com/"):
        headers = github_headers(headers)  # the token goes to the API only
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
        return resp.read()


def check(current: str = APP_VERSION) -> Update | None:
    """The newest release if it is newer than `current`; None when up to date or when anything goes wrong (never raises)."""
    try:
        data = json.loads(_get(API_URL))
        tag = str(data.get("tag_name") or "")
        if data.get("draft") or data.get("prerelease") or not is_newer(tag, current):
            return None
        want = _wanted_asset(install_kind())
        assets = {a.get("name"): a for a in data.get("assets") or []}
        asset = assets.get(want) if want else None
        sha = None
        if asset and (sums := assets.get("SHA256SUMS.txt")):
            try:
                for line in _get(sums["browser_download_url"], "application/octet-stream").decode("utf-8", "replace").splitlines():
                    digest, _, name = line.strip().partition(" ")
                    if name.strip(" *") == want:
                        sha = digest.lower()
            except Exception:
                sha = None
        return Update(tag=tag, notes=str(data.get("body") or ""), page=str(data.get("html_url") or f"https://github.com/{REPO}/releases"),
                      asset_url=asset["browser_download_url"] if asset else "", asset_name=want,
                      asset_size=int(asset.get("size") or 0) if asset else 0, sha256=sha)
    except Exception:
        return None


def download(update: Update, progress=None) -> Path:
    """Fetch the release file into a temp folder, check its checksum, and return its path. Raises RuntimeError on a bad file."""
    dest_dir = Path(tempfile.mkdtemp(prefix="distill-update-"))
    dest = dest_dir / update.asset_name
    digest = hashlib.sha256()
    req = urllib.request.Request(update.asset_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp, dest.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or update.asset_size or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if update.sha256 and digest.hexdigest().lower() != update.sha256:
        raise RuntimeError("The download did not match its checksum, so it was not installed.")
    return dest


def _script(kind: str, payload: Path) -> str:
    exe = Path(sys.executable).resolve()
    pid = os.getpid()
    head = (f"@echo off\r\nsetlocal\r\n:wait\r\ntasklist /fi \"PID eq {pid}\" 2>nul | find \"{pid}\" >nul\r\n"
            "if not errorlevel 1 (timeout /t 1 /nobreak >nul & goto wait)\r\n")
    if kind == "portable":
        body = (f"powershell -NoProfile -Command \"Expand-Archive -Force -LiteralPath '{payload}' -DestinationPath '{APP_DIR}'\"\r\n"
                "if errorlevel 1 exit /b 1\r\n")
    else:
        body = (f"set n=0\r\n:swap\r\nmove /y \"{payload}\" \"{exe}\" >nul 2>nul\r\n"
                "if errorlevel 1 (set /a n+=1 & if %n% lss 20 (timeout /t 1 /nobreak >nul & goto swap) else exit /b 1)\r\n")
    return head + body + f"start \"\" \"{exe}\"\r\ndel \"%~f0\"\r\n"


def apply(payload: Path) -> None:
    """Start the helper that swaps the files once Distill has exited. The caller must quit the app right after."""
    kind = install_kind()
    if kind == "source":
        raise RuntimeError("Self-update works in the packaged Windows builds only.")
    script = payload.parent / "apply_update.bat"
    script.write_text(_script(kind, payload), encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=SUBPROCESS_FLAGS | getattr(subprocess, "DETACHED_PROCESS", 0),
                     close_fds=True, cwd=str(payload.parent))
