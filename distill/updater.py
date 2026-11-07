"""Checks GitHub for a newer Distill release and installs it (Windows builds). Qt-free; the prompt lives in ui/update_prompt.py."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import update_splash
from .constants import APP_DIR, APP_NAME, APP_VERSION
from .github_auth import github_headers

REPO = "QyuBurt/Distill"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_ENABLED = os.environ.get("DISTILL_NO_UPDATE_CHECK") != "1"  # tests and CI switch this off
PREVIEW = os.environ.get("DISTILL_UPDATE_PREVIEW") == "1"  # show the update screen with a made-up download (no release needed)
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


def _script(kind: str, payload: Path, tag: str = "", splash: Path | None = None) -> str:
    """The helper batch file. With `splash` (the staged spec file, see _stage_splash) it first runs the install window in
    update_splash.py, which does the whole swap itself and exits 0; if that fails in any way the plain path below takes over,
    so the install never depends on PowerShell behaving. The plain path waits until the running exe can be written to (so it
    works for the single-file build, whose launcher outlives the program by a moment), swaps the files, then starts Distill
    again. `timeout` is avoided: it fails when there is no input, which turned every wait into no wait at all."""
    exe = Path(sys.executable).resolve()
    if kind == "portable":  # tar ships with Windows 10+ and is far faster than Expand-Archive; PowerShell stays as the fallback
        swap = (f"tar -xf \"{payload}\" -C \"{APP_DIR}\" >>\"%LOG%\" 2>&1 || powershell -NoProfile -Command "
                f"\"Expand-Archive -Force -LiteralPath '{payload}' -DestinationPath '{APP_DIR}'\" >>\"%LOG%\" 2>&1\n")
    else:
        swap = f"move /y \"{payload}\" \"{exe}\" >>\"%LOG%\" 2>&1\n"
    name = f"Distill {tag}" if tag else "Distill"
    window = ""
    if splash is not None:
        window = (f"powershell -NoProfile -ExecutionPolicy Bypass -File \"{splash.with_suffix('.ps1')}\" -Spec \"{splash}\" >>\"%LOG%\" 2>&1\n"
                  "if not errorlevel 1 goto restart\n"
                  "echo %date% %time% install window failed, installing without it >> \"%LOG%\"\n")
    return (
        "@echo off\nsetlocal\ntitle Updating Distill\n"
        "set \"LOG=%TEMP%\\distill-update.log\"\n"
        "echo %date% %time% waiting for Distill to exit > \"%LOG%\"\n"
        f"echo Updating to {name}. Please wait: Distill reopens by itself when this is done.\n"
        + window +
        "set n=0\n"
        ":wait\n"
        f"(>>\"{exe}\" (call;)) 2>nul && goto swap\n"
        "set /a n+=1\n"
        "if %n% gtr 120 (echo gave up waiting >> \"%LOG%\" & goto restart)\n"
        "ping -n 2 127.0.0.1 >nul\n"
        "goto wait\n"
        ":swap\n"
        "echo Installing...\n"
        "echo %date% %time% installing >> \"%LOG%\"\n"
        + swap +
        ":restart\n"
        f"cd /d \"{APP_DIR}\"\n"
        f"start \"\" \"{exe}\"\n"
        "echo %date% %time% restarted >> \"%LOG%\"\n"
        "del \"%~f0\"\n"
    ).replace("\n", "\r\n")


def _stage_splash(spec: dict, payload: Path, kind: str) -> tuple[Path, Path] | None:
    """Put everything the install window needs next to the download (the bundled folder disappears with a single-file build
    once it quits): the logo, the script and its spec. Returns (spec file, 'window is up' marker file), or None
    if anything is missing, in which case the update just installs without the window."""
    try:
        dest = payload.parent
        out = dict(spec)
        src = Path(str(spec.get("logo") or ""))
        if not src.is_file():
            return None
        shutil.copyfile(src, dest / "update-logo.png")
        out["logo"] = str(dest / "update-logo.png")
        ready = dest / "splash-ready.txt"
        ready.unlink(missing_ok=True)
        out.update(kind=kind, payload=str(payload), app_dir=str(APP_DIR), exe=str(Path(sys.executable).resolve()), ready=str(ready))
        (dest / "apply_update.ps1").write_text(update_splash.SCRIPT, encoding="ascii")
        spec_path = dest / "apply_update.json"
        spec_path.write_text(json.dumps(out), encoding="utf-8")
        return spec_path, ready
    except Exception:
        return None


def apply(payload: Path, tag: str = "", splash: dict | None = None) -> Path | None:
    """Start the helper that swaps the files once Distill has exited. The caller must quit the app right after.
    `splash` describes where and how the app's own update screen was drawn (see UpdateScreen.handoff_spec) so the install
    window can carry on from it. Returns the marker file that appears once that window is on screen (the app waits for it
    before quitting, so there is no gap), or None when there is no install window."""
    kind = install_kind()
    if kind == "source":
        raise RuntimeError("Self-update works in the packaged Windows builds only.")
    staged = _stage_splash(splash, payload, kind) if splash else None
    spec_path, ready = staged if staged else (None, None)
    script = payload.parent / "apply_update.bat"
    script.write_text(_script(kind, payload, tag, spec_path), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # the new single-file exe must unpack itself, not reuse this one's temp folder
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if spec_path else getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # the window replaces the console
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True, cwd=str(payload.parent), env=env)
    return ready
