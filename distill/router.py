"""'Drop a file anywhere': which Distill tool a dropped file or folder belongs to. No Qt in here.

Returns (tool key, one-line reason); the UI opens that page with the path filled in."""
from __future__ import annotations

from pathlib import Path

from .patcher import PATCH_EXTENSIONS
from .system import read_head

_CONVERT = {".iso": "convert", ".cue": "convert", ".gdi": "convert", ".bin": "convert", ".cso": "convert",
            ".gcm": "convert", ".wbfs": "convert", ".rvz": "convert", ".chd": "convert", ".pkg": "convert",
            ".zar": "convert"}


def route(path: Path) -> tuple[str, str]:
    if path.is_dir():
        return "report", "Folder: open it in Library Report (or Sort by System)"
    ext = path.suffix.lower()
    if ext in PATCH_EXTENSIONS:
        return "patch", "Patch file: apply it to a ROM"
    if ext in (".nsp", ".xci"):
        return "nsz", "Switch dump: compress to NSZ / XCZ"
    if ext in (".nsz", ".xcz"):
        return "nsz", "Compressed Switch dump: verify or decompress"
    if ext in (".ccd", ".img", ".mds", ".mdf", ".sub"):
        return "cuebin", "CloneCD / Alcohol image: convert to BIN/CUE"
    if ext == ".dat" or (ext == ".xml" and _looks_like_dat(path)):
        return "datcheck", "DAT file: verify a library against it"
    if ext == ".json" and _looks_like_bios(path):
        return "bios", "BIOS list: check your BIOS folder"
    if ext in _CONVERT:
        return _CONVERT[ext], "Disc image: convert / compress"
    return "", "No matching tool"


def _looks_like_dat(path: Path) -> bool:
    try:
        return b"<datafile" in read_head(path, 4096)
    except OSError:
        return False


def _looks_like_bios(path: Path) -> bool:
    try:
        return b'"md5"' in read_head(path, 8192)
    except OSError:
        return False
