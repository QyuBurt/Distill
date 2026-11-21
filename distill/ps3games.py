"""PlayStation 3 games kept as loose folders (a disc dump, or a PSN game): the game's name comes from its PARAM.SFO and the file that
boots it is USRDIR/EBOOT.BIN. The library scan used to see only the .pkg inside (INSTALL.PKG, a few MB of installer data) and list that as
the game, under the name 'INSTALL'."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .formats import parse_sfo

# The third letter of a title id (BLUS30591, BCES00510, NPJA00039 ...) says where the game was sold.
_REGIONS = {"U": "USA", "E": "Europe", "J": "Japan", "A": "Asia", "K": "Korea", "H": "Hong Kong"}
_TITLE_ID = re.compile(r"^[A-Z]{4}\d{5}$")
_MARKS = re.compile(r"[\u2122\u00ae\u00a9]")  # (TM) (R) (C): they make a poor search term
_MAX_SFO = 1 << 20


@dataclass(slots=True)
class Ps3Game:
    title: str
    title_id: str
    region: str
    eboot: Path  # what the emulator boots
    root: Path  # the game's folder (the one that holds PS3_GAME, or the PS3_GAME folder itself)
    modified: float


def _child(folder: Path, name: str) -> Path | None:
    """`name` inside `folder`, whatever the case it is written in on disk (a disc dump may say Eboot.bin or eboot.bin)."""
    direct = folder / name
    if direct.exists():
        return direct
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.upper() == name.upper():
                    return Path(entry.path)
    except OSError:
        pass
    return None


def _tidy(text: object) -> str:
    return re.sub(r"\s+", " ", _MARKS.sub("", str(text or ""))).strip()  # (a title can hold a newline)


def region_of_title_id(title_id: str) -> str:
    tid = title_id.upper()
    return _REGIONS.get(tid[2], "") if _TITLE_ID.match(tid) else ""


def is_ps3_folder(folder: Path, subdirs: list[str], names: list[str]) -> bool:
    """Cheap test for the scan, from the names already read: the disc's root (PS3_GAME beside it), or the PS3_GAME folder itself
    (a PARAM.SFO and a USRDIR)."""
    upper_dirs = {d.upper() for d in subdirs}
    if "PS3_GAME" in upper_dirs:
        return True
    return "USRDIR" in upper_dirs and any(n.upper() == "PARAM.SFO" for n in names)


def read_ps3_folder(folder: Path) -> Ps3Game | None:
    """The game in a PS3 folder, or None when there is nothing to boot (no EBOOT.BIN) - such a folder is left to the scan's other rules."""
    base = folder
    if inner := _child(folder, "PS3_GAME"):
        if inner.is_dir():
            base = inner
    usrdir = _child(base, "USRDIR")
    eboot = _child(usrdir, "EBOOT.BIN") if usrdir is not None and usrdir.is_dir() else None
    if eboot is None or not eboot.is_file():
        return None
    sfo_path = _child(base, "PARAM.SFO")
    info: dict = {}
    modified = 0.0
    try:
        if sfo_path is not None and sfo_path.is_file():
            with open(sfo_path, "rb") as f:
                info = parse_sfo(f.read(_MAX_SFO)) or {}
        modified = max(eboot.stat().st_mtime, sfo_path.stat().st_mtime if sfo_path is not None else 0.0)
    except OSError:
        pass
    title_id = str(info.get("TITLE_ID") or "").strip().upper()
    # The folder's own name is the fallback ('inFAMOUS [BLUS30000]'); inside a PS3_GAME folder it is the folder above that names the game.
    named = folder.parent if folder.name.upper() == "PS3_GAME" else folder
    title = _tidy(info.get("TITLE")) or _tidy(re.sub(r"\s*[\[(][^\])]*[\])]", "", named.name)) or named.name
    return Ps3Game(title, title_id, region_of_title_id(title_id), eboot, named, modified)
