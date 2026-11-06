"""Telling which console a game file belongs to: from its extension, its header and the folder it sits in.

Nothing here imports Qt. detect_system() returns "" when it genuinely can't tell, so callers never have to guess.
"""
from __future__ import annotations

import re
from pathlib import Path

from .constants import ILLEGAL_NAME_CHARS
from .frontends import _TABLE
from .system import read_head
from .formats import (
    XDVDFS_MAGIC, XDVDFS_OFFSETS, associated_files, is_ps4_pkg, is_ps5_pkg, parse_sfo,
)

# Extensions that name one system outright.
EXT_SYSTEMS = {
    ".gba": "Game Boy Advance", ".gbc": "Game Boy Color", ".gb": "Game Boy", ".nes": "NES", ".fds": "Famicom Disk System",
    ".sfc": "SNES", ".smc": "SNES", ".n64": "Nintendo 64", ".z64": "Nintendo 64", ".v64": "Nintendo 64",
    ".nds": "Nintendo DS", ".dsi": "Nintendo DS", ".3ds": "Nintendo 3DS", ".cia": "Nintendo 3DS",
    ".gen": "Mega Drive", ".md": "Mega Drive", ".smd": "Mega Drive", ".sms": "Master System", ".gg": "Game Gear",
    ".pce": "PC Engine", ".a26": "Atari 2600", ".a78": "Atari 7800", ".lnx": "Atari Lynx", ".ws": "WonderSwan",
    ".wsc": "WonderSwan", ".ngp": "Neo Geo Pocket", ".ngc": "Neo Geo Pocket", ".vb": "Virtual Boy",
    ".xci": "Switch", ".nsp": "Switch", ".wad": "Wii", ".cso": "PSP", ".zso": "PSP", ".pbp": "PSP",
    ".gdi": "Dreamcast", ".wbfs": "Wii", ".gcm": "GameCube",
}

# Folder names people use for each system, compared after lower-casing and dropping punctuation.
_HINT_NAMES = {
    "PlayStation": ("ps1", "psx", "ps one", "psone", "playstation", "playstation 1", "sony playstation"),
    "PlayStation 2": ("ps2", "playstation 2", "sony playstation 2"),
    "PlayStation 3": ("ps3", "playstation 3", "sony playstation 3"),
    "PlayStation 4": ("ps4", "playstation 4", "sony playstation 4"),
    "PlayStation 5": ("ps5", "playstation 5", "sony playstation 5"),
    "PSP": ("psp", "playstation portable", "sony playstation portable"),
    "GameCube": ("gamecube", "game cube", "gc", "ngc", "nintendo gamecube"),
    "Wii": ("wii", "nintendo wii"),
    "Sega Saturn": ("saturn", "sega saturn"),
    "Dreamcast": ("dreamcast", "sega dreamcast", "dc"),
    "Sega CD": ("sega cd", "segacd", "mega cd", "megacd"),
    "Xbox": ("xbox", "original xbox", "microsoft xbox"),
    "Xbox 360": ("xbox 360", "x360", "360", "microsoft xbox 360"),
    "3DO": ("3do",),
    "PC Engine CD": ("pc engine cd", "turbografx cd", "turbografx 16 cd", "pce cd"),
}
HINTS = {key: system for system, keys in _HINT_NAMES.items() for key in keys}
for _sys, _row in _TABLE.items():  # folder names a front-end preset creates are recognised again on the next run
    for _n in _row:
        HINTS.setdefault(re.sub(r"[^a-z0-9]+", " ", _n.casefold()).strip(), _sys)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


# A folder named exactly like a system ("Game Boy Advance", "PlayStation 2") counts too: it is what Sort by system makes.
for _system in {*EXT_SYSTEMS.values(), *_HINT_NAMES}:
    HINTS.setdefault(_norm(_system), _system)


SYSTEM_NAMES = tuple(sorted({*EXT_SYSTEMS.values(), *_HINT_NAMES}, key=str.casefold))  # every folder name sorting can make


def system_of_name(name: str) -> str:
    """The system a folder name says ('PS2', 'Dreamcast', 'Game Boy Advance'), or ''."""
    return HINTS.get(_norm(name), "")


def folder_hint(path: Path, root: Path | None = None) -> str:
    """The system a parent folder's name points to (nearest folder first), or ''."""
    parts = list(path.parts[:-1])
    if root is not None:
        try:
            parts = list(path.relative_to(root).parts[:-1])
        except ValueError:
            pass
    for part in reversed(parts[-4:]):
        if system := HINTS.get(_norm(part)):
            return system
    return ""


def leading_hint(path: Path, root: Path) -> str:
    """The system named by the first folder under root ('PS2/Game.iso' -> 'PlayStation 2'), or ''."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return ""
    return HINTS.get(_norm(parts[0]), "") if len(parts) > 1 else ""


def folder_name_for_system(system: str) -> str:
    return ILLEGAL_NAME_CHARS.sub("", system).strip(" .")


# ---- reading discs --------------------------------------------------------------------------------------------
_SECTOR_LAYOUTS = ((2048, 0), (2352, 16), (2352, 24), (2336, 8))  # (bytes per sector, where the 2048 data bytes start)


class _Disc:
    """Sector access to a data track, whether the image is plain 2048-byte sectors or raw CD sectors."""

    def __init__(self, f, size: int, offset: int):
        self.f, self.size, self.offset = f, size, offset

    def sector(self, n: int) -> bytes:
        self.f.seek(n * self.size + self.offset)
        return self.f.read(2048)

    def read(self, first: int, length: int) -> bytes:
        count = max(1, min((length + 2047) // 2048, 512))
        return b"".join(self.sector(first + i) for i in range(count))[:length]


def _open_disc(f) -> _Disc | None:
    for size, offset in _SECTOR_LAYOUTS:
        f.seek(16 * size + offset)
        if f.read(6)[1:6] == b"CD001":
            return _Disc(f, size, offset)
    return None


def _root_entries(disc: _Disc) -> dict[str, tuple[int, int]]:
    pvd = disc.sector(16)
    if len(pvd) < 190:
        return {}
    extent, size = int.from_bytes(pvd[158:162], "little"), int.from_bytes(pvd[166:170], "little")
    data, entries, pos = disc.read(extent, min(size, 1 << 18)), {}, 0
    while pos + 33 <= len(data):
        length = data[pos]
        if length == 0:
            pos = (pos // 2048 + 1) * 2048
            continue
        rec = data[pos:pos + length]
        name = rec[33:33 + rec[32]].decode("latin-1").split(";")[0].upper()
        entries[name] = (int.from_bytes(rec[2:6], "little"), int.from_bytes(rec[10:14], "little"))
        pos += length
    return entries


def _xbox_system(f) -> str:
    for offset in XDVDFS_OFFSETS:
        f.seek(offset + 32 * 2048)
        header = f.read(0x20)
        if header[:len(XDVDFS_MAGIC)] != XDVDFS_MAGIC:
            continue
        sector, size = int.from_bytes(header[0x14:0x18], "little"), int.from_bytes(header[0x18:0x1C], "little")
        f.seek(offset + sector * 2048)
        names = f.read(min(max(size, 2048), 1 << 16)).lower()
        if b"default.xex" in names:
            return "Xbox 360"
        if b"default.xbe" in names:
            return "Xbox"
        return "Xbox 360" if offset in (0x0FD90000, 0x02080000) else "Xbox"
    return ""


def _detect_image(path: Path) -> str:
    """The system of an uncompressed disc image (.iso / .bin / .img ...), read from its own bytes."""
    try:
        with path.open("rb") as f:
            head = f.read(0x20)
            if len(head) == 0x20:
                if head[0x18:0x1C] == b"\x5d\x1c\x9e\xa3":
                    return "Wii"
                if head[0x1C:0x20] == b"\xc2\x33\x9f\x3d":
                    return "GameCube"
            if (system := _xbox_system(f)):
                return system
            for size, offset in ((2048, 0), (2352, 16), (2352, 24)):  # a console's own boot header opens sector 0
                f.seek(offset)
                boot = f.read(16)
                if boot.startswith(b"SEGA SEGASATURN"):
                    return "Sega Saturn"
                if boot.startswith(b"SEGADISCSYSTEM"):
                    return "Sega CD"
                if boot.startswith(b"SEGA SEGAKATANA"):
                    return "Dreamcast"
                if boot.startswith(b"\x01ZZZZZ\x01"):
                    return "3DO"
            disc = _open_disc(f)
            if disc is None:
                return ""
            entries = _root_entries(disc)
            if "PSP_GAME" in entries:
                return "PSP"
            if "PS3_GAME" in entries:
                return "PlayStation 3"
            if cnf := entries.get("SYSTEM.CNF"):
                text = disc.read(cnf[0], min(cnf[1], 4096)).upper()
                return "PlayStation 2" if b"BOOT2" in text else "PlayStation" if b"BOOT" in text else ""
    except OSError:
        pass
    return ""


def _detect_dolphin(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        with path.open("rb") as f:
            head = f.read(0x50)
    except OSError:
        return ""
    if ext in (".rvz", ".wia") and len(head) >= 0x4C:
        return {1: "GameCube", 2: "Wii"}.get(int.from_bytes(head[0x48:0x4C], "big"), "")
    if ext == ".gcz" and len(head) >= 8 and int.from_bytes(head[:4], "little") == 0xB10BC001:
        return {0: "GameCube", 1: "Wii"}.get(int.from_bytes(head[4:8], "little"), "")
    return ""


_IMAGE_EXTENSIONS = frozenset({".iso", ".bin", ".img", ".mdf", ".ima"})  # plain data-track images


def detect_folder_system(folder: Path) -> str:
    """The console of an extracted game folder: PS4 / PS5 by the title id in sce_sys/param.sfo, Xbox 360 by default.xex."""
    try:
        if (folder / "default.xex").is_file():
            return "Xbox 360"
        sfo = folder / "sce_sys" / "param.sfo"
        if sfo.is_file():
            info = parse_sfo(read_head(sfo, 1 << 16)) or {}
            title_id = str(info.get("TITLE_ID") or info.get("CONTENT_ID") or "").upper()
            content = title_id.split("-")[1] if "-" in title_id else title_id
            if content.startswith("PPSA"):
                return "PlayStation 5"
            if content.startswith("CUSA"):
                return "PlayStation 4"
    except OSError:
        pass
    return ""


def detect_system(path: Path, root: Path | None = None) -> str:
    """The console a file is for ('PlayStation 2', 'Game Boy Advance', ...), or '' when it can't be told.

    The file's own contents beat the folder it sits in. CHDs hold their disc compressed, so for those (and for archives)
    only the folder name can help."""
    if path.is_dir():
        return detect_folder_system(path) or folder_hint(path, root)
    ext = path.suffix.lower()
    if system := EXT_SYSTEMS.get(ext):
        return system
    system = ""
    if ext in (".rvz", ".wia", ".gcz"):
        system = _detect_dolphin(path)
    elif ext in _IMAGE_EXTENSIONS:
        system = _detect_image(path)
    elif ext == ".cue":
        for track in sorted(associated_files(path) - {path}):
            if track.suffix.lower() in _IMAGE_EXTENSIONS and (system := _detect_image(track)):
                break
    elif ext == ".pkg":
        system = "PlayStation 5" if is_ps5_pkg(path) else "PlayStation 4" if is_ps4_pkg(path) else ""
    return system or folder_hint(path, root)


# ---- names -----------------------------------------------------------------------------------------------------
_REGION_RE = re.compile(r"^(?:usa|europe|japan|world|asia|korea|china|taiwan|hong kong|brazil|australia|canada|france|"
                        r"germany|spain|italy|uk|netherlands|sweden|russia|scandinavia|latin america)"
                        r"(?:\s*,\s*[a-z ]+)*$", re.IGNORECASE)


def region_of(stem: str) -> str:
    """'USA' for 'Game (USA) (Rev 1)', 'USA, Europe' for '(USA, Europe)'; '' when no region tag is present."""
    for tag in re.findall(r"[(\[]([^)\]]*)[)\]]", stem):
        if _REGION_RE.match(tag.strip()):
            return tag.strip()
    return ""
