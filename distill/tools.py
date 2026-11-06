"""Tool managers: find, download and update chdman, DolphinTool, maxcso, the PKG extractors and the rest."""
from __future__ import annotations

import hashlib
import json
import mmap
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .constants import APP_DIR, APP_NAME, SUBPROCESS_FLAGS, TOOLS_DIR_NAME
from .github_auth import github_headers
from .system import _py7zr, find_7zip, portable_path, resolve_dolphintool


@dataclass(frozen=True, slots=True)
class Release:
    tag: str
    name: str
    url: str
    size: int
    sha256: str | None


def mame_version(tag: str) -> str:
    m = re.fullmatch(r"mame0(\d+)", tag)
    return f"0.{m[1]}" if m else tag


class ReleaseUnavailable(RuntimeError):
    """A tool has no prebuilt download (the message tells the user how to get it)."""


class ToolManager:
    """Keeps a private, auto-updated copy of a command-line tool next to the app (Windows only).

    Subclasses say where the tool comes from (class attributes + _latest_release), how to unpack a
    download (_unpack) and how to tell it works (_probe). Everything else is shared: the tool folder,
    the version cache, the update check, the download, and the rollback if a new copy doesn't run.
    """

    # event prefix, tool name, origin, its folder under ./tools, its pre-v8.5 folder, release-tag regex, exe name
    KEY = NAME = SOURCE = SUBDIR = TAG_RE = EXE_NAME = ""
    WINDOWS_ONLY = True  # False: the tool has official builds for every platform, so it is downloaded everywhere
    USER_AGENT = f"{APP_NAME}-updater"

    def __init__(self, emit: Callable[[tuple], None]):
        self._emit = emit
        self.root = self._pick_root()

    @classmethod
    def _pick_root(cls) -> Path:
        """./tools/<tool> beside the app, or under %LOCALAPPDATA% when the app folder isn't writable."""
        bases = [APP_DIR]
        if local := os.environ.get("LOCALAPPDATA"):
            bases.append(Path(local) / APP_NAME)
        for base in bases:
            target = base / TOOLS_DIR_NAME / cls.SUBDIR
            try:
                target.mkdir(parents=True, exist_ok=True)
                probe = target / ".write_test"
                probe.write_text("x")
                probe.unlink()
                return target
            except OSError:
                continue
        return Path(tempfile.gettempdir()) / f"{APP_NAME.lower()}_{cls.SUBDIR}"

    def _label(self, tag: str) -> str:
        return tag

    def _status(self, text: str) -> None:
        self._emit((f"{self.KEY}_status", text))

    def _shown(self, exe: Path) -> str:
        """How a path appears in the tool box: ./<tool folder>/... rather than the full absolute path."""
        return portable_path(exe)

    def cached(self) -> tuple[str, Path] | None:
        """(release tag, exe) of the copy this app downloaded last, if it is still on disk."""
        try:
            tag = (self.root / "current.txt").read_text().strip()
        except OSError:
            return None
        if not re.fullmatch(self.TAG_RE, tag):
            return None
        folder = self.root / tag
        exe = next(folder.glob(self.EXE_NAME), None) or next(folder.glob(f"*/{self.EXE_NAME}"), None)
        return (tag, exe) if exe else None

    def _local_copy(self) -> Path | None:
        """A copy of the tool the user placed in the tool folder themselves (chdman has none)."""
        return None

    def run(self) -> None:
        """Background thread entry point. Always ends with a <key>_ready event."""
        try:
            self._run()
        except Exception as exc:
            self._status(f"{self.NAME} update failed: {exc}")
        finally:
            self._emit((f"{self.KEY}_ready",))

    def _run(self) -> None:
        if local := self._local_copy():
            self._emit((f"{self.KEY}_path", self._shown(local)))
            try:
                self._probe(local)
                note = ""
            except Exception as exc:
                note = f" - WARNING: {exc}"
            self._status(f"{self.NAME} found in {portable_path(local.parent)} - using your copy, "
                         f"auto-update skipped{note}")
            return
        cached = self.cached()
        if cached:
            self._emit((f"{self.KEY}_path", self._shown(cached[1])))
            self._status(f"{self.NAME} from {self.SOURCE} {self._label(cached[0])} - checking for updates...")
        if self.WINDOWS_ONLY and sys.platform != "win32":
            if not cached:
                self._status(f"Auto-download is Windows-only: install {self.NAME} with your package manager "
                             "(or select it manually).")
            return
        try:
            release = self._latest_release()
        except ReleaseUnavailable as exc:
            self._status(str(exc))
            return
        except Exception:
            self._status(f"Offline - using the downloaded {self.NAME}." if cached
                         else f"Couldn't reach the download server - select {self.NAME} manually.")
            return
        if cached and cached[0] == release.tag:
            self._status(f"{self.NAME} from {self.SOURCE} {self._label(release.tag)} (up to date)")
            return
        self._install(release)

    # ---- what a subclass provides ----------------------------------------------------------------
    def _latest_release(self) -> Release:
        raise NotImplementedError

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        """Leave a working tool at dest / EXE_NAME (tmp is scratch space that is deleted afterwards)."""
        raise NotImplementedError

    @staticmethod
    def _probe(exe: Path) -> str:
        """Run the binary once; raise if it doesn't behave like the real tool."""
        raise NotImplementedError

    def _extract_all(self, archive: Path, dest: Path) -> None:
        """Unpack a whole .7z / .zip package into dest."""
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(dest)
            return
        if seven := find_7zip():
            rc = subprocess.run(
                [seven, "x", "-y", f"-o{dest}", str(archive)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=SUBPROCESS_FLAGS,
            ).returncode
            if rc in (0, 1):
                return
        with _py7zr().SevenZipFile(archive) as z:
            z.extractall(path=dest)

    # ---- shared download / install ---------------------------------------------------------------
    def _download(self, rel: Release, dest: Path) -> None:
        req = urllib.request.Request(rel.url, headers={"User-Agent": self.USER_AGENT})
        sha, done, last = hashlib.sha256(), 0, -1
        with urllib.request.urlopen(req, timeout=30) as resp, dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or rel.size or 0)
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if (pct := done * 100 // total if total else done >> 20) != last:
                    last = pct
                    size = f"{done >> 20} / {total >> 20} MB" if total else f"{done >> 20} MB"
                    self._status(f"Downloading {self.SOURCE} {self._label(rel.tag)} for {self.NAME}... {size}")
        if rel.size and done != rel.size:
            raise RuntimeError("download was incomplete")
        if rel.sha256 and sha.hexdigest().lower() != rel.sha256.lower():
            raise RuntimeError("download checksum mismatch")

    def _prune(self, keep: str) -> None:
        for old in self.root.iterdir():  # old versions may be in use; failures are retried next launch
            if old.is_dir() and re.fullmatch(self.TAG_RE, old.name) and old.name != keep:
                shutil.rmtree(old, ignore_errors=True)

    def _install(self, rel: Release) -> None:
        self._emit((f"{self.KEY}_busy", True))  # the window blinks this tool's marker while it downloads
        tmp = Path(tempfile.mkdtemp(prefix="dl_", dir=self.root))
        try:
            archive = tmp / rel.name
            self._download(rel, archive)
            self._status(f"Extracting {self.NAME} from {self.SOURCE} {self._label(rel.tag)}...")
            dest = self.root / rel.tag
            self._unpack(archive, tmp, dest)
            try:
                self._probe(dest / self.EXE_NAME)
            except Exception:  # a copy that doesn't run is removed, so the previous good version stays active
                shutil.rmtree(dest, ignore_errors=True)
                raise
            (self.root / "current.txt").write_text(rel.tag)
            self._emit((f"{self.KEY}_path", self._shown(dest / self.EXE_NAME)))
            self._status(f"{self.NAME} from {self.SOURCE} {self._label(rel.tag)} (just updated)")
            self._prune(rel.tag)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            self._emit((f"{self.KEY}_busy", False))


class ChdmanManager(ToolManager):
    """chdman comes from the official MAME release: one small GitHub API call per launch, and a download
    (~85 MB, the full MAME package) only when MAME has published a newer release."""

    KEY, NAME, SOURCE = "chdman", "chdman", "MAME"
    SUBDIR, TAG_RE, EXE_NAME = "chdman", r"mame\d+", "chdman.exe"
    API_URL = "https://api.github.com/repos/mamedev/mame/releases/latest"
    SFX_SIGNATURE = b"7z\xbc\xaf\x27\x1c"

    def _label(self, tag: str) -> str:
        return mame_version(tag)

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        arch = ("arm64",) if platform.machine().lower() in ("arm64", "aarch64") else ("x64", "64bit")
        pattern = re.compile(rf"mame\d+b_(?:{'|'.join(arch)})\.exe", re.IGNORECASE)
        for asset in data["assets"]:
            if pattern.fullmatch(asset["name"]):
                digest = asset.get("digest") or ""
                sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
                return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)
        raise RuntimeError("no Windows binary found in the latest MAME release")

    def _extract_chdman(self, sfx: Path, out_dir: Path) -> Path:
        """Pull just chdman.exe out of MAME's self-extracting 7-Zip package."""
        target = out_dir / "chdman.exe"
        if seven := find_7zip():
            rc = subprocess.run(
                [seven, "e", "-y", "-r", f"-o{out_dir}", str(sfx), "chdman.exe"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=SUBPROCESS_FLAGS,
            ).returncode
            if rc in (0, 1) and target.is_file():
                return target
        # Pure-Python path: an SFX is just an exe stub followed by an ordinary .7z archive.
        py7zr = _py7zr()
        blob = out_dir / "payload.7z"
        with sfx.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            offset = mm.find(self.SFX_SIGNATURE)
            while offset != -1:
                blob.write_bytes(mm[offset:])
                try:
                    with py7zr.SevenZipFile(blob) as z:
                        names = [n for n in z.getnames() if n.lower().rsplit("/", 1)[-1] == "chdman.exe"]
                    if names:
                        with py7zr.SevenZipFile(blob) as z:
                            z.extract(path=out_dir, targets=names[:1])
                        found = out_dir / names[0]
                        if found.is_file():
                            return found
                except Exception:
                    pass
                offset = mm.find(self.SFX_SIGNATURE, offset + 1)
        raise RuntimeError("could not extract chdman.exe from the MAME package")

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        extracted = self._extract_chdman(archive, tmp)
        dest.mkdir(exist_ok=True)
        os.replace(extracted, dest / self.EXE_NAME)

    @staticmethod
    def _probe(exe: Path) -> str:
        """chdman with no arguments just prints its usage."""
        res = subprocess.run([str(exe)], capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                             creationflags=SUBPROCESS_FLAGS)
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "chdman" not in text.lower():
            raise RuntimeError("the downloaded chdman did not run")
        return text


class DolphinToolManager(ToolManager):
    """The same idea for DolphinTool, taken from the newest official Dolphin release (Windows only).

    The download page lists no checksums, so this copy is protected by HTTPS plus a test run of the
    tool before it is used. Dolphin's 7z is extracted in full so DolphinTool keeps any files beside it.
    A DolphinTool the user dropped into ./tools/dolphintool by hand is used as is (see _local_copy).
    """

    KEY, NAME, SOURCE = "dolphin", "DolphinTool", "Dolphin"
    SUBDIR, TAG_RE, EXE_NAME = "dolphintool", r"\d{4}[a-z]?", "DolphinTool.exe"
    PAGE_URL = "https://dolphin-emu.org/download/?nocr=true"

    def _local_copy(self) -> Path | None:
        """DolphinTool dropped into ./tools/dolphintool by hand: directly in it, or inside an unzipped Dolphin
        folder (up to two levels down). Folders the updater manages (release tags, dl_* temp) are skipped,
        so a copy the user placed always wins over an auto-downloaded one."""
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            level1 = sorted(d for d in self.root.iterdir() if unmanaged(d))
            level2 = sorted(g for d in level1 for g in d.iterdir() if unmanaged(g))
        except OSError:
            level1 = level2 = []
        for folder in (self.root, *level1, *level2):
            if found := resolve_dolphintool(str(folder)):
                return Path(found)
        return None

    def _latest_release(self) -> Release:
        req = urllib.request.Request(self.PAGE_URL, headers={"User-Agent": self.USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", "replace")
        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        # The releases table comes before the development builds and lists the newest release first.
        m = re.search(rf"https://dl\.dolphin-emu\.org/releases/(\d{{4}}[a-z]?)/dolphin-\1-{arch}\.7z",
                      html, re.IGNORECASE)
        if not m:
            raise RuntimeError("no Windows download found on Dolphin's download page")
        return Release(m[1], f"dolphin-{m[1]}-{arch}.7z", m[0], 0, None)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        found = next(staging.rglob(self.EXE_NAME), None)
        if not found:
            raise RuntimeError("DolphinTool.exe is not in this Dolphin package")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)  # keep the whole folder: DolphinTool may need files beside it

    @staticmethod
    def _probe(exe: Path) -> str:
        res = subprocess.run([str(exe), "convert", "-h"], capture_output=True, timeout=30,
                             stdin=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "convert" not in text.lower():
            raise RuntimeError("DolphinTool did not run (is the 64-bit Visual C++ redistributable installed?)")
        return text


class MaxCsoManager(ToolManager):
    """maxcso (PSP ISO -> CSO) from the newest release of its GitHub project (Windows only).

    The Windows package ships maxcso.exe (64-bit) beside maxcso32.exe; the 64-bit one is used. A maxcso
    dropped into ./tools/maxcso by hand is used as is (see _local_copy).
    """

    KEY, NAME, SOURCE = "maxcso", "maxcso", "maxcso"
    SUBDIR, TAG_RE, EXE_NAME = "maxcso", r"v?\d+(?:\.\d+)+", "maxcso.exe"
    API_URL = "https://api.github.com/repos/unknownbrackets/maxcso/releases/latest"

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("maxcso.exe", "maxcso"):
                if (folder / name).is_file():
                    return folder / name
        return None

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        packages = [a for a in data["assets"] if a["name"].lower().endswith((".7z", ".zip"))]
        # Prefer the package that says it is for Windows; a lone package is taken as is.
        windows = [a for a in packages if "win" in a["name"].lower()] or (packages if len(packages) == 1 else [])
        if not windows:
            raise RuntimeError("no Windows package found in the latest maxcso release")
        asset = windows[0]
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        found = next((p for p in staging.rglob("*") if p.is_file() and p.name.lower() == self.EXE_NAME), None)
        if not found:
            raise RuntimeError("maxcso.exe is not in this maxcso package")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)  # keep the whole folder, in case anything sits beside the exe

    @staticmethod
    def _probe(exe: Path) -> str:
        res = subprocess.run([str(exe), "--help"], capture_output=True, timeout=30,
                             stdin=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "maxcso" not in text.lower():
            raise RuntimeError("the downloaded maxcso did not run")
        return text


class PkgExtractorManager(ToolManager):
    """ShadPS4Plus PKG_Extractor (the PKG code that used to live in shadPS4, as a standalone command-line tool)
    from the newest "PKG Extractor" release of AzaharPlus/shadPS4Plus (Windows only).

    That repository also publishes emulator builds, so the release list is searched for the newest tag that
    starts with PKG_EXTRACTOR_ instead of trusting /releases/latest. The release holds the extractor, an
    AppImage and install scripts: only the Windows program is taken. A copy dropped into ./tools/pkg-extractor
    by hand is used as is. The tool is never started just to test it.
    """

    KEY, NAME, SOURCE = "shadplus", "ShadPS4Plus PKG_Extractor", "AzaharPlus/shadPS4Plus"
    SUBDIR, TAG_RE, EXE_NAME = "pkg-extractor", r"PKG_EXTRACTOR_\d+(?:_\d+)+", "pkg_extractor.exe"
    API_URL = "https://api.github.com/repos/AzaharPlus/shadPS4Plus/releases?per_page=30"

    def _label(self, tag: str) -> str:
        return "v" + tag.removeprefix("PKG_EXTRACTOR_").replace("_", ".")

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("pkg_extractor.exe", "pkg_extractor"):
                if (folder / name).is_file():
                    return folder / name
        return None

    @staticmethod
    def _windows_asset(assets: list[dict]) -> dict | None:
        """The Windows download: a lone pkg_extractor .exe, else a .zip / .7z that isn't the Linux package."""
        def usable(a: dict, suffixes: tuple[str, ...]) -> bool:
            n = a["name"].lower()
            return n.endswith(suffixes) and not any(w in n for w in ("linux", "appimage", "install_pkg"))

        exes = [a for a in assets if usable(a, (".exe",)) and "extract" in a["name"].lower()]
        packs = [a for a in assets if usable(a, (".zip", ".7z"))]
        packs.sort(key=lambda a: "win" not in a["name"].lower())  # a "win" package first
        return (exes or packs or [None])[0]

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            releases = json.load(resp)
        for data in releases:
            if data.get("draft") or data.get("prerelease") or not re.fullmatch(self.TAG_RE, data.get("tag_name", "")):
                continue
            if asset := self._windows_asset(data.get("assets", [])):
                digest = asset.get("digest") or ""
                sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
                return Release(data["tag_name"], asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)
        raise RuntimeError("no Windows PKG Extractor package found in the shadPS4Plus releases")

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        if archive.suffix.lower() == ".exe":  # the release is the bare program
            staging = tmp / "extracted"
            staging.mkdir()
            found = staging / self.EXE_NAME
            shutil.copy2(archive, found)
        else:
            staging = tmp / "extracted"
            staging.mkdir()
            self._extract_all(archive, staging)
            exes = [p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() == ".exe"]
            found = (next((p for p in exes if p.name.lower() == self.EXE_NAME), None)
                     or next((p for p in exes if "extractor" in p.name.lower()), None))
            if not found:
                raise RuntimeError("pkg_extractor.exe is not in this PKG Extractor package")
            if found.name != self.EXE_NAME:  # the app looks for one fixed name
                target = found.with_name(self.EXE_NAME)
                if found.name.lower() == self.EXE_NAME:
                    found.rename(target)  # only the casing differs
                else:
                    shutil.copy2(found, target)
                found = target
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)  # keep the whole folder: the tool may need DLLs beside it

    @staticmethod
    def _probe(exe: Path) -> str:
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF") or exe.stat().st_size < 32 * 1024:
            raise RuntimeError("that file doesn't look like a program")
        return "ok"


class PkgToAnyPs5Manager(ToolManager):
    """pkg-to-anyps5 (extracts PS5 debug packages into the layout the AnyPS5 relinker expects) from the newest
    release of its GitHub project (Windows x86-64 only). Its own advice is to build it yourself, so this looks, in
    order, for: a copy in ./tools/pkg-to-anyps5, one installed by cargo (~/.cargo/bin) and one on PATH, and only then
    downloads the release. The download is checked against GitHub's digest for the file, or failing that against
    the release's SHA256SUMS.txt, and the binary is test-run (--version) before it is used."""

    KEY, NAME, SOURCE = "anyps5", "pkg-to-anyps5", "awake-devel/pkg-to-anyps5"
    SUBDIR, TAG_RE, EXE_NAME = "pkg-to-anyps5", r"v?\d+(?:\.\d+)+", "pkg-to-anyps5.exe"
    API_URL = "https://api.github.com/repos/awake-devel/pkg-to-anyps5/releases/latest"

    def _local_copy(self) -> Path | None:
        names = ("pkg-to-anyps5.exe", "pkg-to-anyps5")

        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        try:
            folders.append(Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo") / "bin")
        except RuntimeError:  # no home folder to look in
            pass
        for folder in folders:
            for name in names:
                if (folder / name).is_file():
                    return folder / name
        found = shutil.which("pkg-to-anyps5")
        return Path(found) if found else None

    def _published_sha256(self, data: dict, asset: dict) -> str | None:
        """The asset's SHA-256: GitHub's own digest if it gives one, else the release's SHA256SUMS.txt."""
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            return digest.split(":", 1)[1]
        sums = next((a for a in data.get("assets", []) if a["name"].lower() == "sha256sums.txt"), None)
        if not sums:
            return None
        try:
            req = urllib.request.Request(sums["browser_download_url"], headers={"User-Agent": self.USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read(1 << 20).decode("utf-8", "replace")
        except Exception:
            return None
        for line in text.splitlines():  # "<hash>  <file name>" (a leading * marks binary mode)
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip().lstrip("*") == asset["name"] and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                return parts[0]
        return None

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        windows = [a for a in data.get("assets", [])
                   if a["name"].lower().endswith(".zip") and "windows" in a["name"].lower() and "x86_64" in a["name"].lower()]
        if not windows:
            raise RuntimeError("no Windows package found in the latest pkg-to-anyps5 release")
        asset = windows[0]
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0),
                       self._published_sha256(data, asset))

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        exes = [p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() == ".exe"]
        found = (next((p for p in exes if p.name.lower() == self.EXE_NAME), None)
                 or next((p for p in exes if p.name.lower().startswith("pkg-to-anyps5")), None))
        if not found:
            raise RuntimeError("pkg-to-anyps5.exe is not in this package")
        if found.name != self.EXE_NAME:
            target = found.with_name(self.EXE_NAME)
            found.rename(target)
            found = target
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)

    @staticmethod
    def _probe(exe: Path) -> str:
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        try:  # a plain command-line tool, so asking its version is safe
            res = subprocess.run([str(exe), "--version"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if res.returncode != 0 and "pkg-to-anyps5" not in text.lower():
            raise RuntimeError("the downloaded pkg-to-anyps5 did not run")
        return text


class ZArchiveManager(ToolManager):
    """ZArchive (zarchive.exe, packs a folder into a .zar and unpacks one) from the newest release of Exzap's
    GitHub project (Windows only). A copy in ./tools/zarchive (or on PATH) is used as is. The release's file
    names are not assumed: a package or program for Windows is taken, or the one program there is."""

    KEY, NAME, SOURCE = "zarchive", "ZArchive", "Exzap/ZArchive"
    SUBDIR, TAG_RE, EXE_NAME = "zarchive", r"v?\d+(?:\.\d+)+", "zarchive.exe"
    API_URL = "https://api.github.com/repos/Exzap/ZArchive/releases/latest"

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("zarchive.exe", "zarchive"):
                if (folder / name).is_file():
                    return folder / name
        found = shutil.which("zarchive")
        return Path(found) if found else None

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        usable = [a for a in data.get("assets", []) if a["name"].lower().endswith((".zip", ".7z", ".exe"))]
        windows = ([a for a in usable if re.search(r"win", a["name"].lower())]
                   or [a for a in usable if a["name"].lower().endswith(".exe")])
        if not windows:
            raise ReleaseUnavailable("No Windows download found in the latest ZArchive release. Build it from "
                                     "github.com/Exzap/ZArchive, or select zarchive.exe yourself.")
        asset = windows[0]
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        if archive.suffix.lower() == ".exe":  # the release is the program itself
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, dest / self.EXE_NAME)
            return
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        exes = [p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() == ".exe"]
        found = next((p for p in exes if p.name.lower() == self.EXE_NAME), None)
        if not found:
            raise RuntimeError("zarchive.exe is not in this ZArchive package")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)  # keep the whole folder: the program may need DLLs beside it

    @staticmethod
    def _probe(exe: Path) -> str:
        """With no arguments zarchive prints its usage text and exits; it never starts packing."""
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        try:
            res = subprocess.run([str(exe)], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "zarchive" not in text.lower():
            raise RuntimeError("the downloaded zarchive did not run")
        return text


class Ps3QddManager(ToolManager):
    """PS3Dec (PS3Dec.exe, the PS3 disc decryptor inside ElektroStudios' PS3 Quick Disc Decryptor) from the newest
    release of that GitHub project (Windows only). The package is kept whole, so its Keys folder of Redump .dkey
    files is there for the PS3 tab. A PS3Dec.exe you put in ./tools/ps3qdd (or an unpacked PS3QDD folder there) is
    used as is. The release's file names are not assumed: the package with keys is taken if there is one."""

    KEY, NAME, SOURCE = "ps3qdd", "PS3QDD", "ElektroStudios/PS3-Quick-Disc-Decryptor"
    SUBDIR, TAG_RE, EXE_NAME = "ps3qdd", r"v?\d+(?:\.\d+)+", "PS3Dec.exe"
    API_URL = "https://api.github.com/repos/ElektroStudios/PS3-Quick-Disc-Decryptor/releases/latest"

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("PS3Dec.exe", "ps3dec.exe", "PS3Dec", "ps3dec"):
                if (folder / name).is_file():
                    return folder / name
        found = shutil.which("ps3dec")
        return Path(found) if found else None

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        usable = [a for a in data.get("assets", []) if a["name"].lower().endswith((".zip", ".7z"))]
        if not usable:
            raise ReleaseUnavailable("No download found in the latest PS3QDD release. Get it from "
                                     "github.com/ElektroStudios/PS3-Quick-Disc-Decryptor, or select PS3Dec.exe yourself.")
        asset = next((a for a in usable if "key" in a["name"].lower()), usable[0])  # the package with the keys
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        found = next((p for p in staging.rglob("*") if p.is_file() and p.name.lower() == self.EXE_NAME.lower()), None)
        if not found:
            raise RuntimeError("PS3Dec.exe is not in this PS3QDD package")
        root = staging
        while (kids := list(root.iterdir())) and len(kids) == 1 and kids[0].is_dir():  # a single wrapper folder
            root = kids[0]
        rel = found.relative_to(root)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(root, dest)  # keep everything: the Keys folder sits beside the program
        if not (dest / self.EXE_NAME).is_file():
            shutil.copy2(dest / rel, dest / self.EXE_NAME)

    @staticmethod
    def _probe(exe: Path) -> str:
        """PS3Dec waits for a key press, so it is not started here: the file just has to be a program."""
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        return ""


class XdvdfsManager(ToolManager):
    """xdvdfs (xdvdfs.exe, packs and checksums Xbox ISOs) from the newest release of antangelo's GitHub project
    (Windows only). A copy in ./tools/xdvdfs (or on PATH) is used as is. The release's file names are not assumed:
    the Windows package is the one with "windows" in its name, else the one program there is."""

    KEY, NAME, SOURCE = "xdvdfs", "xdvdfs", "antangelo/xdvdfs"
    SUBDIR, TAG_RE, EXE_NAME = "xdvdfs", r"v?\d+(?:\.\d+)+", "xdvdfs.exe"
    API_URL = "https://api.github.com/repos/antangelo/xdvdfs/releases/latest"

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in ("xdvdfs.exe", "xdvdfs"):
                if (folder / name).is_file():
                    return folder / name
        found = shutil.which("xdvdfs")
        return Path(found) if found else None

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        usable = [a for a in data.get("assets", []) if a["name"].lower().endswith((".zip", ".7z", ".exe"))]
        windows = [a for a in usable if "win" in a["name"].lower()] or [a for a in usable if a["name"].lower().endswith(".exe")]
        if not windows:
            raise ReleaseUnavailable("No Windows download found in the latest xdvdfs release. Get it from "
                                     "github.com/antangelo/xdvdfs, or select xdvdfs.exe yourself.")
        asset = windows[0]
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        if archive.suffix.lower() == ".exe":  # the release is the program itself
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, dest / self.EXE_NAME)
            return
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        exes = [p for p in staging.rglob("*") if p.is_file() and p.suffix.lower() == ".exe"]
        found = next((p for p in exes if p.name.lower() == self.EXE_NAME), None) or (exes[0] if len(exes) == 1 else None)
        if not found:
            raise RuntimeError("xdvdfs.exe is not in this xdvdfs package")
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        shutil.copy2(found, dest / self.EXE_NAME)

    @staticmethod
    def _probe(exe: Path) -> str:
        """`xdvdfs -h` only prints its usage text."""
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        try:
            res = subprocess.run([str(exe), "-h"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "xdvdfs" not in text.lower() and "xiso" not in text.lower():
            raise RuntimeError("the downloaded xdvdfs did not run")
        return text


class AnyPS5Manager(ToolManager):
    """AnyPS5's relinker (relinker.exe) and its system libraries (libs/*.prx) from the newest GitHub release that
    has both for Windows. They are taken from the SAME release, as AnyPS5 requires, into ./tools/anyps5/<tag>/.
    A relinker.exe you put in ./tools/anyps5 yourself (e.g. built from source) is used as is; its libraries are found
    beside it or in the source build's build/core/libs/libs."""

    KEY, NAME, SOURCE = "relinker", "AnyPS5 relinker", "boykopovar/AnyPS5"
    SUBDIR, TAG_RE, EXE_NAME = "anyps5", r"v?\d+(?:\.\d+)+", "relinker.exe"
    API_URL = "https://api.github.com/repos/boykopovar/AnyPS5/releases?per_page=10"

    def __init__(self, emit: Callable[[tuple], None]):
        super().__init__(emit)
        self._libs_release: Release | None = None  # the libraries' download, found alongside the relinker's

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            try:
                found = next(iter(sorted(folder.glob("relinker*.exe"))), None)
            except OSError:
                found = None
            if found:
                return found
        return None

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            releases = json.load(resp)

        def asset(a: dict, tag: str) -> Release:
            digest = a.get("digest") or ""
            sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
            return Release(tag, a["name"], a["browser_download_url"], a.get("size", 0), sha)

        for rel in releases:
            tag = rel.get("tag_name", "")
            if rel.get("draft") or not re.fullmatch(self.TAG_RE, tag):
                continue
            assets = rel.get("assets", [])
            exe = next((a for a in assets if re.fullmatch(r"relinker.*\.exe", a["name"], re.I)), None)
            libs = next((a for a in assets if re.fullmatch(r"prx-windows.*\.zip", a["name"], re.I)), None)
            if exe and libs:
                self._libs_release = asset(libs, tag)
                return asset(exe, tag)
        raise ReleaseUnavailable("No AnyPS5 release with a Windows relinker and system libraries was found. Build "
                                 "AnyPS5 from github.com/boykopovar/AnyPS5, or select relinker.exe yourself.")

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        libs_release = self._libs_release
        if libs_release is None:
            raise RuntimeError("the libraries' download details were lost")
        shutil.rmtree(dest, ignore_errors=True)
        try:
            dest.mkdir(parents=True)
            shutil.copy2(archive, dest / self.EXE_NAME)  # relinker-vX.Y.Z.exe -> relinker.exe
            zip_path = tmp / libs_release.name
            self._download(libs_release, zip_path)
            staging = tmp / "libs_extracted"
            staging.mkdir()
            self._extract_all(zip_path, staging)
            holders = [staging, *sorted(d for d in staging.rglob("*") if d.is_dir())]
            libs = next((d for d in holders if any(d.glob("*.prx"))), None)  # the .prx files (and the DLLs beside them)
            if libs is None:
                raise RuntimeError("no .prx libraries are in the AnyPS5 package")
            shutil.copytree(libs, dest / "libs")
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise

    @staticmethod
    def _probe(exe: Path) -> str:
        """--help prints the usage text and exits 0; nothing is relinked."""
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF"):
            raise RuntimeError("that file doesn't look like a program")
        try:
            res = subprocess.run([str(exe), "--help"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "relinker" not in text.lower() and "usage" not in text.lower():
            raise RuntimeError("the downloaded relinker did not run")
        return text


class FlatToolManager(ToolManager):
    """A tool kept loose in its folder (./tools/ytdlp/yt-dlp.exe) rather than in a folder per release, so other
    tools can sit beside it: yt-dlp finds an ffmpeg next to itself with no extra settings. The installed release
    is remembered in <key>_version.txt. Subclasses fill FILES (what ends up in the folder) in _unpack."""

    FILES: tuple[str, ...] = ()  # file names _unpack leaves in its destination; all are moved into the folder

    def cached(self) -> tuple[str, Path] | None:
        try:
            tag = (self.root / f"{self.KEY}_version.txt").read_text().strip()
        except OSError:
            return None
        exe = self.root / self.EXE_NAME
        return (tag, exe) if re.fullmatch(self.TAG_RE, tag) and exe.is_file() else None

    def _install(self, rel: Release) -> None:
        self._emit((f"{self.KEY}_busy", True))
        tmp = Path(tempfile.mkdtemp(prefix="dl_", dir=self.root))
        try:
            archive = tmp / rel.name
            self._download(rel, archive)
            self._status(f"Extracting {self.NAME} {self._label(rel.tag)}...")
            stage = tmp / "stage"
            stage.mkdir()
            self._unpack(archive, tmp, stage)
            self._probe(stage / self.EXE_NAME)  # a copy that doesn't run never replaces the working one
            for name in self.FILES or (self.EXE_NAME,):
                if (stage / name).is_file():
                    os.replace(stage / name, self.root / name)
            (self.root / f"{self.KEY}_version.txt").write_text(rel.tag)
            self._emit((f"{self.KEY}_path", self._shown(self.root / self.EXE_NAME)))
            self._status(f"{self.NAME} {self._label(rel.tag)} (just updated)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            self._emit((f"{self.KEY}_busy", False))

    def _run(self) -> None:
        cached = self.cached()
        if cached:
            self._emit((f"{self.KEY}_path", self._shown(cached[1])))
            self._status(f"{self.NAME} {self._label(cached[0])} - checking for updates...")
        if self.WINDOWS_ONLY and sys.platform != "win32":
            if not cached:
                self._status(f"Auto-download is Windows-only: install {self.NAME} with your package manager.")
            return
        try:
            release = self._latest_release()
        except ReleaseUnavailable as exc:
            self._status(str(exc))
            return
        except Exception:
            self._status(f"Offline - using the downloaded {self.NAME}." if cached
                         else f"Couldn't reach the download server for {self.NAME}.")
            return
        if cached and cached[0] == release.tag:
            self._status(f"{self.NAME} {self._label(release.tag)} (up to date)")
            return
        self._install(release)

    @staticmethod
    def _github(url: str, agent: str) -> dict:
        headers = github_headers({"User-Agent": agent, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as resp:
            return json.load(resp)

    @staticmethod
    def _release_of(asset: dict, tag: str) -> Release:
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), sha)


class YtdlpManager(FlatToolManager):
    """yt-dlp, from the newest official release (its own single-file build for this platform). Lives in
    ./tools/ytdlp beside ffmpeg."""

    KEY, NAME, SOURCE = "ytdlp", "yt-dlp", "yt-dlp"
    SUBDIR, TAG_RE = "ytdlp", r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?"
    EXE_NAME = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    WINDOWS_ONLY = False
    API_URL = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"

    @staticmethod
    def asset_name() -> str:
        return {"win32": "yt-dlp.exe", "darwin": "yt-dlp_macos"}.get(sys.platform, "yt-dlp_linux")

    def _latest_release(self) -> Release:
        data = self._github(self.API_URL, self.USER_AGENT)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        asset = next((a for a in data["assets"] if a["name"] == self.asset_name()), None)
        if asset is None:
            raise RuntimeError("no build for this system in the latest yt-dlp release")
        return self._release_of(asset, tag)

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        shutil.copy2(archive, dest / self.EXE_NAME)
        if sys.platform != "win32":
            (dest / self.EXE_NAME).chmod(0o755)

    @staticmethod
    def _probe(exe: Path) -> str:
        res = subprocess.run([str(exe), "--version"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                             creationflags=SUBPROCESS_FLAGS)
        text = res.stdout.decode(errors="replace").strip()
        if not re.match(r"\d{4}\.\d{2}\.\d{2}", text):
            raise RuntimeError("the downloaded yt-dlp did not run")
        return text


class FfmpegManager(FlatToolManager):
    """ffmpeg and ffprobe (Windows builds from BtbN's FFmpeg-Builds, the build yt-dlp itself recommends), put in
    ./tools/ytdlp beside yt-dlp. The download is a rolling "latest" build, so its upload date is the version."""

    KEY, NAME, SOURCE = "ffmpeg", "ffmpeg", "BtbN"
    SUBDIR, TAG_RE, EXE_NAME = "ytdlp", r"\d{8}", "ffmpeg.exe"
    FILES = ("ffmpeg.exe", "ffprobe.exe")
    API_URL = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"

    def _latest_release(self) -> Release:
        data = self._github(self.API_URL, self.USER_AGENT)
        arm = platform.machine().lower() in ("arm64", "aarch64")
        wanted = f"ffmpeg-master-latest-{'winarm64' if arm else 'win64'}-gpl.zip"
        asset = next((a for a in data["assets"] if a["name"] == wanted), None)
        if asset is None:
            raise RuntimeError("no Windows build in the latest FFmpeg-Builds release")
        stamp = re.sub(r"\D", "", str(asset.get("updated_at") or asset.get("created_at") or ""))[:8]
        if len(stamp) != 8:
            raise RuntimeError("the ffmpeg build has no date")
        return self._release_of(asset, stamp)

    def _label(self, tag: str) -> str:
        return f"{tag[:4]}-{tag[4:6]}-{tag[6:]}"

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        wanted = {name: None for name in self.FILES}
        with zipfile.ZipFile(archive) as z:  # only the two programs, not the whole package
            for member in z.namelist():
                base = member.rsplit("/", 1)[-1].lower()
                if base in wanted and "/bin/" in member.lower() and wanted[base] is None:
                    with z.open(member) as src, (dest / base).open("wb") as out:
                        shutil.copyfileobj(src, out)
                    wanted[base] = True
        if not wanted["ffmpeg.exe"]:
            raise RuntimeError("ffmpeg.exe is not in this ffmpeg package")

    @staticmethod
    def _probe(exe: Path) -> str:
        res = subprocess.run([str(exe), "-version"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                             creationflags=SUBPROCESS_FLAGS)
        text = res.stdout.decode(errors="replace")
        if "ffmpeg version" not in text.lower():
            raise RuntimeError("the downloaded ffmpeg did not run")
        return text


def _published_digest(asset: dict) -> str | None:
    digest = asset.get("digest") or ""
    return digest.split(":", 1)[1] if digest.startswith("sha256:") else None


class _OwnCopyMixin:
    """_local_copy for the tools a person may have put in their folder by hand (or installed on PATH)."""

    LOCAL_NAMES: tuple[str, ...] = ()
    PATH_NAMES: tuple[str, ...] = ()

    def _local_copy(self) -> Path | None:
        def unmanaged(d: Path) -> bool:
            return d.is_dir() and not d.name.startswith("dl_") and not re.fullmatch(self.TAG_RE, d.name)

        try:
            folders = [self.root, *sorted(d for d in self.root.iterdir() if unmanaged(d))]
        except OSError:
            folders = [self.root]
        for folder in folders:
            for name in self.LOCAL_NAMES:
                if (folder / name).is_file():
                    return folder / name
        for name in self.PATH_NAMES:
            if found := shutil.which(name):
                return Path(found)
        return None


class NscbManager(_OwnCopyMixin, ToolManager):
    """nscb_rust (the Rust port of NSC_BUILDER's core: compress / decompress / merge / convert Switch files) from the
    newest release of cxfcxf/nscb_rust. Its releases hold one program per system (nscb_rust.exe, nscb_rust-linux-amd64,
    nscb_rust-macos-arm64). The download is checked against GitHub's digest when it has one, and the program is test-run
    (--help) before it is used. A copy in ./tools/nscb-rust (or an `nscb_rust` on PATH) is used as is."""

    KEY, NAME, SOURCE = "nscb", "nscb_rust", "cxfcxf/nscb_rust"
    SUBDIR, TAG_RE = "nscb-rust", r"v?\d+(?:\.\d+)*"
    EXE_NAME = "nscb_rust.exe" if sys.platform == "win32" else "nscb_rust"
    WINDOWS_ONLY = False
    API_URL = "https://api.github.com/repos/cxfcxf/nscb_rust/releases/latest"
    LOCAL_NAMES = ("nscb_rust.exe", "nscb_rust", "nscb.exe", "nscb")
    PATH_NAMES = ("nscb_rust", "nscb")

    @staticmethod
    def asset_name() -> str | None:
        arm = platform.machine().lower() in ("arm64", "aarch64")
        if sys.platform == "win32":
            return "nscb_rust.exe"
        if sys.platform == "darwin":
            return "nscb_rust-macos-arm64" if arm else None
        return None if arm else "nscb_rust-linux-amd64"

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        wanted = self.asset_name()
        asset = next((a for a in data.get("assets", []) if wanted and a["name"] == wanted), None)
        if asset is None:
            raise ReleaseUnavailable("No nscb_rust download for this system. Build it with  cargo build --release  from "
                                     "github.com/cxfcxf/nscb_rust, then select the program yourself.")
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), _published_digest(asset))

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, dest / self.EXE_NAME)
        if sys.platform != "win32":
            (dest / self.EXE_NAME).chmod(0o755)

    @staticmethod
    def _probe(exe: Path) -> str:
        with exe.open("rb") as f:
            magic = f.read(4)
        if not (magic[:2] == b"MZ" or magic == b"\x7fELF" or magic[:2] == b"\xcf\xfa" or magic[:4] == b"\xcf\xfa\xed\xfe"):
            raise RuntimeError("that file doesn't look like a program")
        try:
            res = subprocess.run([str(exe), "--help"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "nscb" not in text.lower() and "usage" not in text.lower():
            raise RuntimeError("the downloaded nscb_rust did not run")
        return text


class HactoolnetManager(_OwnCopyMixin, ToolManager):
    """hactoolnet (LibHac's command-line tool: opens NSP / XCI / NCA files and extracts their ExeFS and RomFS with your
    keys) from the newest release of Thealexbarney/LibHac. It is a .NET program, so the .NET runtime must be installed.
    The package is kept whole, DLLs and all. A copy in ./tools/hactoolnet (or on PATH) is used as is."""

    KEY, NAME, SOURCE = "hactoolnet", "hactoolnet", "Thealexbarney/LibHac"
    SUBDIR, TAG_RE = "hactoolnet", r"v?\d+(?:\.\d+)+"
    EXE_NAME = "hactoolnet.exe" if sys.platform == "win32" else "hactoolnet"
    WINDOWS_ONLY = False
    API_URL = "https://api.github.com/repos/Thealexbarney/LibHac/releases/latest"
    LOCAL_NAMES = ("hactoolnet.exe", "hactoolnet")
    PATH_NAMES = ("hactoolnet",)

    @staticmethod
    def _wanted(asset: dict) -> bool:
        name = asset["name"].lower()
        if not (name.startswith("hactoolnet") and name.endswith(".zip")):
            return False
        token = {"win32": "win", "darwin": "osx"}.get(sys.platform, "linux")
        return token in name or (sys.platform == "darwin" and "mac" in name)

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(urllib.request.Request(self.API_URL, headers=headers), timeout=10) as resp:
            data = json.load(resp)
        tag = data["tag_name"]
        if not re.fullmatch(self.TAG_RE, tag):
            raise RuntimeError(f"unexpected release tag {tag!r}")
        arm = platform.machine().lower() in ("arm64", "aarch64")
        found = [a for a in data.get("assets", []) if self._wanted(a)]
        found.sort(key=lambda a: (("arm" in a["name"].lower()) != arm, a["name"]))  # this machine's architecture first
        if not found:
            raise ReleaseUnavailable("No hactoolnet download for this system. Get it from "
                                     "github.com/Thealexbarney/LibHac/releases, or select hactoolnet yourself.")
        asset = found[0]
        return Release(tag, asset["name"], asset["browser_download_url"], asset.get("size", 0), _published_digest(asset))

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        found = next((p for p in staging.rglob("*") if p.is_file() and p.name.lower() in (self.EXE_NAME.lower(), "hactoolnet.dll")), None)
        if not found:
            raise RuntimeError("hactoolnet is not in this package")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(found.parent, dest)  # keep the whole folder: it needs the DLLs beside it
        if not (dest / self.EXE_NAME).is_file() and (dest / "hactoolnet.dll").is_file():  # .NET builds without a native launcher
            script = dest / self.EXE_NAME
            script.write_text('#!/bin/sh\nexec dotnet "$(dirname "$0")/hactoolnet.dll" "$@"\n')
            script.chmod(0o755)
        elif sys.platform != "win32":
            (dest / self.EXE_NAME).chmod(0o755)

    @staticmethod
    def _probe(exe: Path) -> str:
        try:
            res = subprocess.run([str(exe), "--version"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"the program didn't start: {exc}") from exc
        text = (res.stdout + res.stderr).decode(errors="replace")
        if "hactoolnet" not in text.lower() and "libhac" not in text.lower():
            raise RuntimeError("hactoolnet did not run (it needs the .NET runtime from dotnet.microsoft.com/download)")
        return text


NUP_LAUNCHER = '''"""Starts nup (github.com/a55uka/nup) from the copy Distill downloaded."""
import os
import runpy
import sys

here = os.path.dirname(os.path.abspath(__file__))
for extra in (os.path.join(here, "src"), os.path.join(here, "deps")):  # deps first on the path: it is added last
    if os.path.isdir(extra):
        sys.path.insert(0, extra)
sys.argv[0] = "nup"
runpy.run_module("nup.cli", run_name="__main__", alter_sys=True)
'''


class NupManager(_OwnCopyMixin, ToolManager):
    """nup (the NX Package format: packs decrypted Switch / Switch 2 dumps into one compressed .nup) from the newest
    commit of a55uka/nup. The project is a few days old, publishes no releases and is plain Python, so what is fetched
    is the source of one exact commit (its first 7 characters are the version shown), with a small launcher beside it.
    Running it needs Python; a module it asks for that isn't installed (a zstd binding, say) is installed into this
    tool's own folder with pip, nowhere else. A `nup` on PATH, or a nup.py launcher in ./tools/nup, is used as is."""

    KEY, NAME, SOURCE = "nup", "nup", "a55uka/nup"
    SUBDIR, TAG_RE, EXE_NAME = "nup", r"[0-9a-f]{7}", "nup.py"
    WINDOWS_ONLY = False
    REPO_API = "https://api.github.com/repos/a55uka/nup"
    LOCAL_NAMES = ("nup.py",)
    PATH_NAMES = ("nup",)

    def _latest_release(self) -> Release:
        headers = github_headers({"User-Agent": self.USER_AGENT, "Accept": "application/vnd.github+json"})

        def get(url: str) -> dict:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as resp:
                return json.load(resp)

        branch = get(self.REPO_API).get("default_branch") or "main"
        sha = get(f"{self.REPO_API}/commits/{urllib.parse.quote(branch)}")["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise RuntimeError("GitHub gave no commit for nup")
        return Release(sha[:7], f"nup-{sha[:7]}.zip", f"https://github.com/a55uka/nup/archive/{sha}.zip", 0, None)

    @staticmethod
    def _missing_module(text: str) -> str | None:
        m = re.search(r"No module named '([A-Za-z0-9_]+)", text)
        return m[1] if m else None

    @classmethod
    def _run_launcher(cls, launcher: Path) -> tuple[int, str]:
        from .switchconv import find_python
        python = find_python()
        if not python:
            raise RuntimeError("nup is a Python program and no Python was found on this PC (install Python 3 from python.org)")
        res = subprocess.run([python, str(launcher), "--help"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                             creationflags=SUBPROCESS_FLAGS)
        return res.returncode, (res.stdout + res.stderr).decode(errors="replace")

    def _unpack(self, archive: Path, tmp: Path, dest: Path) -> None:
        staging = tmp / "extracted"
        staging.mkdir()
        self._extract_all(archive, staging)
        package = next((p.parent for p in staging.rglob("cli.py") if p.parent.name == "nup" and (p.parent / "__init__.py").is_file()), None)
        if package is None:
            raise RuntimeError("the nup package (nup/cli.py) is not in this download")
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        shutil.copytree(package.parent, dest / "src")
        launcher = dest / self.EXE_NAME
        launcher.write_text(NUP_LAUNCHER, encoding="utf-8")
        from .switchconv import find_python
        for _attempt in range(4):  # each pass can reveal one more module that is missing
            rc, text = self._run_launcher(launcher)
            module = self._missing_module(text)
            if rc == 0 or not module:
                break
            self._status(f"Installing {module} for nup...")
            res = subprocess.run([find_python(), "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "--target",
                                  str(dest / "deps"), module], capture_output=True, timeout=300, stdin=subprocess.DEVNULL,
                                 creationflags=SUBPROCESS_FLAGS)
            if res.returncode != 0:
                raise RuntimeError(f"could not install {module} with pip")

    @classmethod
    def _probe(cls, exe: Path) -> str:
        rc, text = cls._run_launcher(exe)
        if rc != 0 or "nup" not in text.lower():
            raise RuntimeError(f"nup did not run: {(text.strip().splitlines() or [''])[-1]}")
        return text
