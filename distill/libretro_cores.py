"""RetroArch cores: the catalogue on the libretro buildbot, what is installed in a RetroArch's cores folder, and install / update / remove.

No Qt in here (the Emulators page lays it out; see ui/retroarch_cores.py). The folder is the source of truth for what is installed: Distill
only keeps a small sidecar (`.distill_cores.json`, beside the cores) of which build it put there, so it can say "update available" and nothing
else, and RetroArch's own Core Downloader keeps working on the same folder.

Assumptions about the buildbot (all in one place so they are cheap to correct):
  * every core is a zip named `<name>_libretro.<dll|so|dylib>.zip` in a directory that also serves `.index-extended` (date, crc, file),
  * individual cores live under `nightly/<platform>/latest/`, whatever the RetroArch version: that is where RetroArch's own Core Downloader
    reads them from too (the `stable/<version>/` folders hold the program and one big cores bundle, not a per-core listing),
  * `assets/frontend/info.zip` holds one `<name>_libretro.info` per core (display name, systems, extensions, firmware).
"""
from __future__ import annotations

import io
import json
import os
import platform as _platform
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import emulators as em
from . import recomps as rc

BUILDBOT = "https://buildbot.libretro.com/"
HOST = "buildbot.libretro.com"
INFO_URL = BUILDBOT + "assets/frontend/info.zip"
RECORD_NAME = ".distill_cores.json"
INDEX_TTL = 6 * 3600.0
INFO_TTL = 3 * 86400.0
MAX_CORE_BYTES = 1 << 30  # no core is anywhere near this; a bigger "core" is a mistake or an attack
Progress = Callable[[str, int, int], None]


# ---- what to ask the buildbot for -----------------------------------------------------------------------------
def target(platform: str | None = None, machine: str | None = None) -> tuple[str, str]:
    """(the platform's folder on the buildbot, the suffix of a core's file) for this PC."""
    platform = platform or sys.platform
    machine = (machine or _platform.machine()).lower()
    x64, arm = machine in ("amd64", "x86_64", "x64"), machine in ("arm64", "aarch64")
    if platform.startswith("win"):
        if x64:
            return "windows/x86_64", ".dll"
    elif platform == "darwin":
        if arm:
            return "apple/osx/arm64", ".dylib"
        if x64:
            return "apple/osx/x86_64", ".dylib"
    elif x64:
        return "linux/x86_64", ".so"
    raise rc.RecompError("RetroArch's build server has no ready-made cores for this PC's system and CPU.")


def base_url(sub: str) -> str:
    return f"{BUILDBOT}nightly/{sub}/latest/"


def check_url(url: str) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or (p.hostname or "").lower() != HOST:
        raise rc.RecompError(f"Refusing to download from {p.hostname or url}: cores only come from {HOST}.")


# ---- the catalogue --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Core:
    name: str  # "fceumm"
    file: str  # "fceumm_libretro.dll"
    display: str
    systems: str = ""
    extensions: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    firmware: tuple[tuple[str, str, bool], ...] = ()  # (what it is, path RetroArch looks for, optional)
    license: str = ""
    authors: str = ""
    description: str = ""
    date: str = ""
    crc: str = ""
    remote: bool = True  # False: a file in the folder the server knows nothing about

    @property
    def is_emulator(self) -> bool:  # cores with no category are kept: better shown than hidden
        return not self.categories or any(c.lower() == "emulator" for c in self.categories)

    @property
    def needs_firmware(self) -> bool:
        return any(not opt for _d, _p, opt in self.firmware)


@dataclass(frozen=True, slots=True)
class Catalog:
    cores: tuple[Core, ...]
    base: str
    suffix: str
    sub: str


def _nice(name: str) -> str:
    return name.replace("_", " ").strip().title()


_INFO_LINE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=\s*"?(.*?)"?\s*$')  # numbers (firmware_count = 1) are written without quotes
_SPLIT = re.compile(r"\s*[|,;]\s*")


def parse_info(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if m := _INFO_LINE.match(line):
            out[m.group(1)] = m.group(2)
    return out


def _from_info(name: str, file: str, info: dict[str, str], date: str, crc: str, remote: bool = True) -> Core:
    fw = []
    count = info.get("firmware_count", "0")
    for i in range(min(int(count) if count.isdigit() else 0, 64)):
        path = info.get(f"firmware{i}_path", "")
        if path:
            fw.append((info.get(f"firmware{i}_desc", path), path, info.get(f"firmware{i}_opt", "false").lower() == "true"))
    return Core(name, file, info.get("display_name") or _nice(name), info.get("systemname", ""),
                tuple(x for x in _SPLIT.split(info.get("supported_extensions", "")) if x),
                tuple(x for x in _SPLIT.split(info.get("categories", "")) if x), tuple(fw), info.get("license", ""),
                info.get("authors", ""), info.get("description", "") or info.get("notes", ""), date, crc, remote)


_INDEX_LINE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\S*\s+(?:\d{1,2}:\d{2}(?::\d{2})?\s+)?(?P<crc>[0-9a-fA-F]{8})\s+(?P<file>\S+)\s*$")


def parse_index(text: str, suffix: str) -> dict[str, tuple[str, str]]:
    """`.index-extended` (or a plain `.index`, or an HTML directory listing): core file -> (date, crc)."""
    want = f"_libretro{suffix}.zip"
    found: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if m := _INDEX_LINE.match(line):
            if m.group("file").endswith(want):
                found[m.group("file")[:-4]] = (m.group("date"), m.group("crc").lower())
        elif line.endswith(want) and " " not in line:
            found.setdefault(line[:-4], ("", ""))
    if not found:
        for href in re.findall(r'href="(?:[^"?#]*/)?([^"/?#]+)"', text):
            if href.endswith(want):
                found.setdefault(href[:-4], ("", ""))
    return found


def _find_index(sub: str, suffix: str) -> tuple[str, dict[str, tuple[str, str]]]:
    base = base_url(sub)
    last: Exception | None = None
    for leaf in (".index-extended", ".index", ""):
        try:
            found = parse_index(rc._get(base + leaf, timeout=30).decode("utf-8", "replace"), suffix)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            continue
        if found:
            return base, found
    raise rc.RecompError(f"Couldn't read the list of cores from {base}" + (f" ({em._why(last).strip(' ()')})." if last else " (it listed no cores)."))


def fetch_info(store: rc.Store | None, force: bool = False) -> dict[str, dict[str, str]]:
    """core name -> the fields of its .info file. Cached for days; the last copy is used if the server can't be reached."""
    if store and not force and (hit := store.cache_get("lrinfo", INFO_TTL)) is not None:
        return hit  # type: ignore[return-value]
    try:
        out = {}
        with zipfile.ZipFile(io.BytesIO(rc._get(INFO_URL, timeout=60))) as z:
            for n in z.namelist():
                base = Path(n).name
                if base.endswith("_libretro.info"):
                    out[base[: -len("_libretro.info")]] = parse_info(z.read(n).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, zipfile.BadZipFile):
        return (store.cache_stale("lrinfo") if store else None) or {}  # type: ignore[return-value]
    if store:
        store.cache_put("lrinfo", out)
    return out


def _core_from_dict(d: dict) -> Core:
    d = dict(d)
    d["extensions"], d["categories"] = tuple(d.get("extensions") or ()), tuple(d.get("categories") or ())
    d["firmware"] = tuple((str(a), str(b), bool(c)) for a, b, c in d.get("firmware") or ())
    return Core(**d)


def fetch_catalog(store: rc.Store | None, *, force: bool = False, platform: str | None = None, machine: str | None = None) -> Catalog:
    sub, suffix = target(platform, machine)
    key = f"lrcores:{sub}"
    if store and not force and (hit := store.cache_get(key, INDEX_TTL)) is not None:
        return Catalog(tuple(_core_from_dict(d) for d in hit["cores"]), hit["base"], suffix, sub)  # type: ignore[index]
    try:
        base, index = _find_index(sub, suffix)
    except rc.RecompError:
        if store and (stale := store.cache_stale(key)) is not None:
            return Catalog(tuple(_core_from_dict(d) for d in stale["cores"]), stale["base"], suffix, sub)  # type: ignore[index]
        raise
    info = fetch_info(store, force)
    tail = f"_libretro{suffix}"
    cores = []
    for file, (date, crc) in index.items():
        name = file[: -len(tail)]
        cores.append(_from_info(name, file, info.get(name, {}), date, crc))
    cores.sort(key=lambda c: c.display.lower())
    if store:
        store.cache_put(key, {"base": base, "cores": [asdict(c) for c in cores]})
    return Catalog(tuple(cores), base, suffix, sub)


# ---- where RetroArch keeps its cores --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Location:
    cores: Path | None
    info: Path | None = None
    config: Path | None = None
    reason: str = ""  # why Distill can't manage them (empty when it can)

    @property
    def ok(self) -> bool:
        return self.cores is not None and not self.reason


def read_cfg(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f.read(4_000_000).splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def _resolve(value: str, exe_dir: Path, home: Path) -> Path | None:
    value = (value or "").strip()
    if value in ("", "default"):
        return None
    if value.startswith(":"):  # ":\cores" means "beside the program"
        return exe_dir / value[1:].lstrip("/\\").replace("\\", "/")
    if value.startswith("~"):
        return home / value[1:].lstrip("/\\")
    return Path(value)


def _writable(path: Path) -> bool:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return os.access(probe, os.W_OK)


def locate(folder: str, exe: str, linked: str = "", *, platform: str | None = None, home: str | Path | None = None) -> Location:
    """Find the cores (and .info) folders of an installed RetroArch: the ones its retroarch.cfg names, else where that build keeps them."""
    platform = platform or sys.platform
    home = Path(home) if home else Path.home()
    exe_path = Path(folder) / exe
    bundle = exe_path.suffix.lower() == ".app"
    exe_dir = exe_path if bundle else exe_path.parent
    homes: list[Path]  # per-user data folders a RetroArch of this kind may use, most likely first
    if platform == "darwin":
        homes = [home / "Library" / "Application Support" / "RetroArch"]
        cfgs = [homes[0] / "config" / "retroarch.cfg", homes[0] / "retroarch.cfg"]
    elif platform.startswith("win"):
        homes = [exe_dir]
        cfgs = [exe_dir / "retroarch.cfg"]
    else:
        bundled = exe_path.with_name(exe_path.name + ".home") / ".config" / "retroarch"  # an AppImage uses this when its ".home" folder exists
        homes = ([bundled] if bundled.parent.parent.is_dir() else []) + [home / ".config" / "retroarch",
                 home / ".var" / "app" / "org.libretro.RetroArch" / "config" / "retroarch"]
        cfgs = [exe_dir / "retroarch.cfg"] + [h / "retroarch.cfg" for h in homes]
    if not bundle and (exe_dir / "retroarch.cfg") not in cfgs:
        cfgs.insert(0, exe_dir / "retroarch.cfg")  # a cfg beside the program always wins: that is the portable layout
    config = next((c for c in cfgs if c.is_file()), None)
    cfg = read_cfg(config) if config else {}
    cores = _resolve(cfg.get("libretro_directory", ""), exe_dir, home)
    info = _resolve(cfg.get("libretro_info_path", ""), exe_dir, home)
    if cores is None:
        if config is not None and config.parent in homes + [exe_dir]:
            cores = config.parent / "cores"
        else:
            cores = next((h / "cores" for h in homes if (h / "cores").is_dir()), homes[0] / "cores")  # else where a fresh install would put them
    if info is None:
        info = cores.parent / "info" if cores.name == "cores" else None
    reason = ""
    if not _writable(cores):
        reason = (f"Distill can't write to {cores}. This looks like a system-wide install; manage its cores from RetroArch's own "
                  "Core Downloader, or link a copy of RetroArch that lives in your own folders.")
    return Location(cores, info, config, reason)


# ---- what is in the folder ------------------------------------------------------------------------------------
def scan_installed(cores: Path | None, suffix: str) -> dict[str, Path]:
    if cores is None or not cores.is_dir():
        return {}
    tail = f"_libretro{suffix}"
    return {p.name[: -len(tail)]: p for p in sorted(cores.iterdir()) if p.is_file() and p.name.endswith(tail) and not p.name.startswith(".")}


def load_record(cores: Path | None) -> dict[str, dict]:
    if cores is None:
        return {}
    try:
        data = json.loads((cores / RECORD_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_record(cores: Path, record: dict[str, dict]) -> None:
    tmp = cores / (RECORD_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, cores / RECORD_NAME)
    except OSError:
        tmp.unlink(missing_ok=True)  # losing the sidecar only costs "update available" hints


def with_local_only(catalog: Catalog, installed: dict[str, Path]) -> list[Core]:
    """The catalogue plus any core in the folder the server doesn't list (your own build, a retired core), so it can still be seen and removed."""
    known = {c.name for c in catalog.cores}
    extra = [Core(n, p.name, _nice(n), remote=False) for n, p in installed.items() if n not in known]
    return sorted([*catalog.cores, *extra], key=lambda c: c.display.lower())


def status_of(core: Core, installed: bool, record: dict | None) -> str:
    """missing | current | update | unknown (installed, but not by Distill so which build is a guess) | local (the server has no such core)."""
    if not installed:
        return "missing"
    if not core.remote:
        return "local"
    if not record:
        return "unknown"
    if core.crc and record.get("crc") != core.crc:
        return "update"
    if not core.crc and core.date and record.get("date") != core.date:
        return "update"
    return "current"


# ---- our pick for each system ---------------------------------------------------------------------------------
# The buildbot says nothing about which core is better, and there is no benchmark that settles it (accuracy, speed and compatibility pull
# different ways), so this is a judgment call drawn from the libretro documentation and what emulation communities usually recommend. It
# is a short, readable table so it is easy to disagree with: (system, core names best first, why). The first one the server has wins.
PICKS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("NES / Famicom", ("mesen", "nestopia", "fceumm"), "Cycle-accurate and the most compatible."),
    ("SNES / Super Famicom", ("snes9x", "bsnes"), "Runs on almost any PC with excellent compatibility. bsnes is more exact, but needs a faster one."),
    ("Game Boy / Color", ("sameboy", "gambatte"), "The most accurate Game Boy emulation, and light enough for any PC."),
    ("Game Boy Advance", ("mgba", "vbam"), "Accurate, fast and actively developed."),
    ("Nintendo DS", ("melondsds", "melonds", "desmume"), "The best compatibility and accuracy for DS games."),
    ("Nintendo 3DS", ("azahar", "citra"), "The only 3DS emulator in libretro that is worth using."),
    ("Nintendo 64", ("mupen64plus_next", "parallel_n64"), "Broad compatibility, with upscaling."),
    ("GameCube / Wii", ("dolphin",), "The same emulator as the standalone Dolphin."),
    ("Mega Drive / Genesis", ("genesis_plus_gx", "picodrive"), "Accurate, and covers all of Sega's 8- and 16-bit systems."),
    ("Master System / Game Gear", ("genesis_plus_gx", "picodrive"), "Accurate, and covers all of Sega's 8- and 16-bit systems."),
    ("Sega CD", ("genesis_plus_gx", "picodrive"), "Accurate, and covers all of Sega's 8- and 16-bit systems."),
    ("Sega 32X", ("picodrive",), "The only good 32X core."),
    ("Sega Saturn", ("mednafen_saturn", "kronos", "yabause"), "The most accurate Saturn core, but it needs a fast CPU."),
    ("Dreamcast", ("flycast",), "The standard Dreamcast core, with upscaling."),
    ("PlayStation", ("mednafen_psx_hw", "swanstation", "pcsx_rearmed", "mednafen_psx"), "Accurate, with hardware rendering and upscaling."),
    ("PSP", ("ppsspp",), "The same emulator as the standalone PPSSPP."),
    ("PC Engine / TurboGrafx-16", ("mednafen_pce", "mednafen_pce_fast"), "The most accurate PC Engine core."),
    ("SuperGrafx", ("mednafen_supergrafx",), "The SuperGrafx build of the same accurate core."),
    ("Arcade, Neo Geo and CPS", ("fbneo", "mame"), "Wide arcade support, with Neo Geo and Capcom boards that work out of the box."),
    ("Neo Geo Pocket", ("mednafen_ngp", "race"), "Accurate, and covers colour and mono games."),
    ("WonderSwan", ("mednafen_wswan",), "Accurate, and covers WonderSwan Colour."),
    ("Virtual Boy", ("mednafen_vb",), "The only Virtual Boy core worth using."),
    ("Atari 2600", ("stella", "stella2014"), "The reference Atari 2600 emulator."),
    ("Atari 5200", ("atari800",), "Covers the Atari 5200 and 8-bit computers."),
    ("Atari 7800", ("prosystem",), "The standard Atari 7800 core."),
    ("Atari Lynx", ("handy", "mednafen_lynx"), "Accurate and light."),
    ("Atari Jaguar", ("virtualjaguar",), "The only Jaguar core."),
    ("Amiga", ("puae",), "The libretro version of the leading Amiga emulator."),
    ("Commodore 64", ("vice_x64sc", "vice_x64"), "The cycle-accurate VICE build."),
    ("MS-DOS", ("dosbox_pure",), "Runs games straight from a folder or zip, with little setup."),
    ("MSX", ("bluemsx",), "Covers the whole MSX family."),
    ("ColecoVision", ("gearcoleco", "bluemsx"), "Accurate and simple."),
    ("Intellivision", ("freeintv",), "The standard Intellivision core."),
    ("Vectrex", ("vecx",), "The standard Vectrex core."),
    ("3DO", ("opera",), "The only 3DO core."),
    ("ScummVM", ("scummvm",), "Plays classic adventure games with ScummVM itself."),
    ("PC-98", ("np2kai",), "The leading PC-98 emulator."),
    ("Sharp X68000", ("px68k",), "The libretro X68000 core."),
    ("ZX Spectrum", ("fuse",), "The libretro version of the Fuse emulator."),
    ("Amstrad CPC", ("cap32",), "Accurate Amstrad CPC emulation."),
)


def recommendations(cores: "list[Core] | tuple[Core, ...]") -> dict[str, list[tuple[str, str]]]:
    """core name -> [(system, why)] for every system this core is our pick for, taking the first core of each list that the server has."""
    have = {c.name for c in cores}
    out: dict[str, list[tuple[str, str]]] = {}
    for system, names, why in PICKS:
        if best := next((n for n in names if n in have), None):
            out.setdefault(best, []).append((system, why))
    return out


# ---- which library systems a core plays -----------------------------------------------------------------------
_SYSTEM_PATTERNS = {
    "nes": r"(?<!super )nintendo entertainment system|famicom", "snes": r"super nintendo|super famicom|\bsnes\b",
    "game boy": r"game ?boy(?! advance)", "game boy color": r"game ?boy(?! advance)", "game boy advance": r"game ?boy advance",
    "nintendo 64": r"nintendo 64", "nintendo ds": r"nintendo ds", "nintendo 3ds": r"3ds", "gamecube": r"gamecube", "wii": r"\bwii\b(?! u)",
    "wii u": r"wii u", "mega drive": r"mega drive|genesis", "master system": r"master system", "sega cd": r"sega cd|mega-?cd",
    "sega saturn": r"saturn", "dreamcast": r"dreamcast", "pc engine": r"pc engine|turbografx", "neo geo pocket": r"neo geo pocket",
    "wonderswan": r"wonderswan", "atari jaguar": r"jaguar", "atari jaguar cd": r"jaguar", "playstation": r"playstation(?! ?(2|3|4|5|portable|vita))",
    "psp": r"playstation portable|\bpsp\b", "playstation 2": r"playstation 2", "playstation 3": r"playstation 3", "playstation vita": r"vita",
    "xbox": r"\bxbox\b(?! 360)", "xbox 360": r"xbox 360", "switch": r"\bswitch\b",
}


def system_matches(core_systems: str, library_system: str) -> bool:
    """Whether a core's `systemname` (\"Nintendo - Game Boy Advance\") covers a system as Distill's library names it (\"Game Boy Advance\")."""
    s, want = core_systems.lower(), library_system.strip().lower()
    if not s or not want:
        return False
    if pat := _SYSTEM_PATTERNS.get(want):
        return re.search(pat, s) is not None
    return re.search(r"(?<![a-z0-9])" + re.escape(want) + r"(?![a-z0-9])", s) is not None


# ---- changing what is installed -------------------------------------------------------------------------------
@dataclass(slots=True)
class BatchResult:
    installed: list[str]
    removed: list[str]
    failed: dict[str, str]
    cancelled: bool = False

    def summary(self) -> str:
        bits = []
        if self.installed:
            bits.append(f"Installed {len(self.installed)}")
        if self.removed:
            bits.append(f"removed {len(self.removed)}")
        if self.failed:
            first = next(iter(self.failed.items()))
            bits.append(f"{len(self.failed)} failed ({first[0]}: {first[1]})")
        if self.cancelled:
            bits.append("cancelled")
        text = ", ".join(bits) or "Nothing to do"
        return text[0].upper() + text[1:] + "."


def _install_one(store: rc.Store | None, loc: Location, cat: Catalog, core: Core, progress: Progress, cancel: threading.Event) -> None:
    assert loc.cores is not None
    url = cat.base + core.file + ".zip"
    check_url(url)
    loc.cores.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".distill-", dir=loc.cores))  # beside the target: the final rename stays on one filesystem
    try:
        archive = tmp / (core.file + ".zip")
        rc.Installer(store or rc.Store(tmp / "unused.json"), progress, cancel).download({"name": core.display, "url": url}, archive)  # type: ignore[arg-type]
        try:
            with zipfile.ZipFile(archive) as z:
                if z.testzip():
                    raise rc.RecompError(f"{core.display}'s download is damaged.")
                member = next((i for i in z.infolist() if Path(i.filename).name == core.file), None)
                if member is None:
                    raise rc.RecompError(f"{core.display}'s download doesn't contain {core.file}.")
                if member.file_size > MAX_CORE_BYTES:
                    raise rc.RecompError(f"{core.display}'s download is unreasonably large; it was not unpacked.")
                staged = tmp / core.file
                with z.open(member) as src, staged.open("wb") as out:
                    shutil.copyfileobj(src, out)
        except zipfile.BadZipFile as exc:
            raise rc.RecompError(f"{core.display}'s download isn't a valid zip.") from exc
        if sys.platform != "win32":
            staged.chmod(0o755)
        try:
            os.replace(staged, loc.cores / core.file)
        except PermissionError as exc:
            raise rc.RecompError(f"{core.file} is in use. Close RetroArch and try again.") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write_info(loc: Location, names: list[str]) -> None:
    """Best effort: RetroArch shows a core's proper name and systems from its .info; a core still loads without one."""
    if not names or loc.info is None or not _writable(loc.info):
        return
    try:
        with zipfile.ZipFile(io.BytesIO(rc._get(INFO_URL, timeout=60))) as z:
            have = {Path(n).name: n for n in z.namelist()}
            loc.info.mkdir(parents=True, exist_ok=True)
            for name in names:
                if (member := have.get(f"{name}_libretro.info")) is not None:
                    (loc.info / f"{name}_libretro.info").write_bytes(z.read(member))
    except (urllib.error.URLError, OSError, ValueError, zipfile.BadZipFile):
        pass


def apply_changes(store: rc.Store | None, loc: Location, cat: Catalog, install: list[Core], remove: list[Core], *,
                  progress: Progress = lambda *_: None, cancel: threading.Event | None = None) -> BatchResult:
    cancel = cancel or threading.Event()
    result = BatchResult([], [], {})
    if loc.cores is None:
        result.failed["cores folder"] = loc.reason or "unknown"
        return result
    record = load_record(loc.cores)
    total = len(install) + len(remove)
    step = 0
    try:
        for core in remove:
            step += 1
            progress(f"Removing {core.display}  ({step}/{total})", step - 1, total)
            try:
                (loc.cores / core.file).unlink(missing_ok=True)
                record.pop(core.name, None)
                result.removed.append(core.name)
            except PermissionError:
                result.failed[core.display] = "in use: close RetroArch and try again"
            except OSError as exc:
                result.failed[core.display] = str(exc)
        for core in install:
            if cancel.is_set():
                result.cancelled = True
                break
            step += 1
            label = f"{core.display}  ({step}/{total})"
            try:
                _install_one(store, loc, cat, core, lambda text, done, size, label=label: progress(f"{label}: {text}", done, size), cancel)
            except rc.Cancelled:
                result.cancelled = True
                break
            except rc.RecompError as exc:
                result.failed[core.display] = str(exc)
                continue
            except OSError as exc:
                result.failed[core.display] = str(exc)
                continue
            record[core.name] = {"crc": core.crc, "date": core.date, "when": time.time()}
            result.installed.append(core.name)
        if result.installed:
            progress("Adding core info...", 0, 0)
            _write_info(loc, result.installed)
    finally:
        save_record(loc.cores, record)
    return result
