"""'Drop it on the window': which page a dropped file, folder or archive belongs to. No Qt in here.

`route_drop` looks at what was dropped (extensions, a few bytes of the file, a folder's layout, the names inside a zip,
and nothing is extracted) and answers with a `DropTarget`: the page, the folder to fill in as its source, and the
files to limit the run to when only some of a folder were dropped. The window applies it (see ui/dropper.py).

The page keys are the sidebar's: chd, cso, ps3, pkg, pkg5, rvz, xbox, zar, switch, dl, and the Organize pages
fix, patch and report."""
from __future__ import annotations

import os
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .constants import CHD_EXTENSIONS, DOLPHIN_ONLY_EXTENSIONS
from .formats import (
    is_gamecube_wii_image, is_ps3_game_folder, is_ps3_image, is_ps4_game_folder, is_ps4_pkg, is_ps5_pkg, is_psp_image, is_xbox_image, PKG_MAGIC,
    PS5_PKG_MAGIC,
)
from .patcher import PATCH_EXTENSIONS
from .router import _looks_like_bios, _looks_like_dat
from .switchconv import _drop_kind, classify_switch_drop, is_nx_dump, SwitchDrop

DISC_SHEETS = CHD_EXTENSIONS - {".iso"}  # .cue .gdi .cdi .toc .nrg: a disc described by a sheet
CLONECD = frozenset({".ccd", ".img", ".sub"})  # repaired on the Fix Names & Discs page
CONVERSION_PAGES = ("chd", "rvz", "cso", "ps3", "xbox", "pkg", "pkg5", "switch", "zar")  # what a folder of games is counted as
SNIFF_BUDGET = 12  # files of a folder whose first bytes are read to tell them apart (the rest of its files are not looked at)


@dataclass(frozen=True, slots=True)
class DropTarget:
    page: str
    source: Path | None  # the folder to put in the page's source box (None: leave it as it is)
    only: tuple[Path, ...] = ()  # run just these (files or archives), not everything in the source folder
    label: str = ""  # what was dropped, for the message
    reason: str = ""  # what it was recognised as
    archive: bool = False  # a zip / 7z: the page's 'Extract archives first' has to be on
    guess: bool = False  # the page is the likeliest, not a certainty (an ISO inside a 7z)
    switch: SwitchDrop | None = None  # the Switch page's own reading of the drop
    links: tuple[str, ...] = ()  # dl: .torrent files to add
    option: tuple[str, Path] | None = None  # an Organize page option to fill: (option name, file)
    step: str = ""  # an Organize step to tick
    step_exclusive: bool = False  # ... and to untick the others


@dataclass(frozen=True, slots=True)
class _Hit:
    page: str
    reason: str
    tag: str = ""  # "dat" | "bios" for a list file
    guess: bool = False
    archive: bool = False


# ---- one file ----------------------------------------------------------------------------------------------------
def _file_hit(path: Path, budget: list[int] | None = None) -> _Hit | None:
    """What one file is, from its extension and, for the ambiguous ones (.iso, .pkg), its first bytes. `budget` (a one-item
    list) limits how many such files are opened; once it is spent they count as unrecognised."""
    ext = path.suffix.lower()

    def sniff() -> bool:
        if budget is None:
            return True
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        return True

    if ext == ".torrent":
        return _Hit("dl", "Torrent file")
    if ext in PATCH_EXTENSIONS:
        return _Hit("patch", "Patch file")
    if ext in CLONECD:
        return _Hit("fix", "CloneCD image: convert it to BIN/CUE")
    if ext == ".dat" or (ext == ".xml" and _looks_like_dat(path)):
        return _Hit("report", "DAT file", "dat")
    if ext == ".json" and _looks_like_bios(path):
        return _Hit("report", "BIOS list", "bios")
    if _drop_kind([path.name]):
        return _Hit("switch", "Switch file")
    if ext in DISC_SHEETS:
        return _Hit("chd", "Disc image")
    if ext in DOLPHIN_ONLY_EXTENSIONS:
        return _Hit("rvz", "GameCube / Wii disc")
    if ext == ".pkg":
        if not sniff():
            return None
        if is_ps4_pkg(path):
            return _Hit("pkg", "PS4 package")
        if is_ps5_pkg(path):
            return _Hit("pkg5", "PS5 package")
        return None  # PS3 and Vita packages have no page
    if ext == ".iso":
        if not sniff():
            return None
        return _iso_hit(path)
    return None


def _iso_hit(path, xbox: bool = True) -> _Hit:
    """An .iso is told apart by what is in it: a Dolphin disc, a PSP disc, a PS3 disc, an Xbox disc; anything else is a plain
    disc image for CHD (PS1, PS2, Saturn, Mega CD ...). `xbox=False` skips the Xbox check, which can mean reading far into the file."""
    if is_gamecube_wii_image(path):
        return _Hit("rvz", "GameCube / Wii disc")
    if is_psp_image(path):
        return _Hit("cso", "PSP disc")
    if is_ps3_image(path):
        return _Hit("ps3", "PS3 disc")
    if xbox and is_xbox_image(path):
        return _Hit("xbox", "Xbox disc")
    return _Hit("chd", "Disc image")


# ---- archives ----------------------------------------------------------------------------------------------------
class _Member:
    """A file inside a zip, shaped enough like a Path for the format readers (they only need .suffix and .open())."""

    def __init__(self, zf: zipfile.ZipFile, name: str):
        self._zf, self.name, self.suffix = zf, name, Path(name).suffix.lower()

    def open(self, _mode: str = "rb"):
        return self._zf.open(self.name)


def _archive_hit(path: Path) -> _Hit | None:
    """What a zip or 7z holds, from its member names (and, for a zip, the first bytes of an .iso or .pkg inside it)."""
    zf = None
    try:
        if path.suffix.lower() == ".zip":
            zf = zipfile.ZipFile(path)
            names = zf.namelist()
        else:
            import py7zr
            with py7zr.SevenZipFile(path) as z:
                names = z.getnames()
    except Exception:  # not an archive, damaged, encrypted or py7zr missing: not for us to place
        return None
    try:
        return _names_hit(names, zf)
    except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        return None
    finally:
        if zf is not None:
            zf.close()


def _names_hit(names: list[str], zf: zipfile.ZipFile | None) -> _Hit | None:
    if kind := _drop_kind(names):
        return _Hit("switch", "Archive of Switch files", archive=True, tag=kind[0])
    by_ext: dict[str, str] = {}
    for n in names:
        by_ext.setdefault(Path(n).suffix.lower(), n)
    if ".pkg" in by_ext:
        if zf is None:  # a 7z can't be read in place
            return _Hit("pkg", "Archive of PlayStation packages", archive=True, guess=True)
        with zf.open(by_ext[".pkg"]) as f:
            magic = f.read(4)
        if magic == PS5_PKG_MAGIC:
            return _Hit("pkg5", "Archive of a PS5 package", archive=True)
        if magic == PKG_MAGIC:
            return _Hit("pkg", "Archive of a PS4 package", archive=True)
        return None
    if ".iso" in by_ext:
        if zf is None:
            return _Hit("chd", "Archive of a disc image", archive=True, guess=True)
        hit = _iso_hit(_Member(zf, by_ext[".iso"]), xbox=False)  # Xbox discs hide their marker too deep to read from a zip
        return _Hit(hit.page, f"Archive of a {hit.reason[0].lower()}{hit.reason[1:]}", archive=True, guess=hit.page == "chd")
    if DISC_SHEETS & by_ext.keys():
        return _Hit("chd", "Archive of a disc image", archive=True)
    if DOLPHIN_ONLY_EXTENSIONS & by_ext.keys():
        return _Hit("rvz", "Archive of a GameCube / Wii disc", archive=True)
    return None


# ---- a folder ----------------------------------------------------------------------------------------------------
def _folder_target(folder: Path) -> DropTarget | None:
    if is_nx_dump(folder):
        return _switch_target([folder])
    if is_ps3_game_folder(folder):  # an unpacked PS3 game belongs on the PS3 page (RPCS3 can't read a .zar)
        return DropTarget("ps3", folder, (), folder.name, "Unpacked PS3 game folder; this page reads PS3 ISOs")
    if is_ps4_game_folder(folder):  # one game folder: pack it alone, from its parent
        return DropTarget("zar", folder.parent, (folder,), folder.name, "PlayStation game folder")
    tally: Counter[str] = Counter()
    budget = [SNIFF_BUDGET]
    seen = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith((".", ".distill_nx_"))]
        here = Path(root)
        if here != folder and is_ps3_game_folder(here):
            tally["ps3"] += 1  # taken whole, so the EBOOT.BIN in its USRDIR isn't mistaken for a PS4 game
            dirs[:] = []
            continue
        if here != folder and is_ps4_game_folder(here):
            tally["zar"] += 1
            dirs[:] = []
            continue
        for name in files:
            seen += 1
            if hit := _file_hit(here / name, budget):
                if hit.page in CONVERSION_PAGES:
                    tally[hit.page] += 1
        if seen >= 400:
            break
    if not tally:
        return None
    page = max(CONVERSION_PAGES, key=lambda p: tally[p])  # most files wins; a tie goes to the earlier page in the list
    if page == "switch":
        return _switch_target([folder])
    return DropTarget(page, folder, (), folder.name, f"Folder of {_PLURAL[page]}")


_PLURAL = {"chd": "disc images", "rvz": "GameCube / Wii discs", "cso": "PSP discs", "ps3": "PS3 discs", "xbox": "Xbox discs",
           "pkg": "PS4 packages", "pkg5": "PS5 packages", "zar": "game folders", "switch": "Switch games"}


def _switch_target(paths: list[Path]) -> DropTarget | None:
    drop = classify_switch_drop(paths)
    if drop is None:
        return None
    return DropTarget("switch", drop.source, drop.only, drop.label, "Switch " + ("2 dump" if drop.side == "s2" else "1 file"),
                      archive=drop.archive, switch=drop)


# ---- the whole drop ----------------------------------------------------------------------------------------------
def route_drop(paths: list[Path]) -> DropTarget | None:
    """The page this drop belongs to, or None when it isn't something Distill has a page for, or when the items disagree
    (different kinds, or files from different folders)."""
    if not paths:
        return None
    if len(paths) == 1 and paths[0].is_dir():
        return _folder_target(paths[0])
    files = [p for p in paths if p.is_file()]
    hits: list[tuple[Path, _Hit]] = []
    for p in files:
        hit = _archive_hit(p) if p.suffix.lower() in (".zip", ".7z", ".7zip") else _file_hit(p)
        if hit:
            hits.append((p, hit))
    if not hits:
        return None  # unknown files dropped alongside (a .bin beside its .cue) don't matter when something is recognised
    if len({h.page for _p, h in hits}) != 1 or len({p.parent for p, _h in hits}) != 1:
        return None
    page, first = hits[0][1].page, hits[0][1]
    picked = tuple(p for p, _h in hits)
    parent = picked[0].parent
    label = picked[0].name if len(picked) == 1 else f"{len(picked)} files"
    if page == "switch":
        return _switch_target(list(picked))
    if page == "dl":
        return DropTarget("dl", None, (), label, first.reason, links=tuple(p.resolve().as_uri() for p in picked))
    if page == "report":  # a DAT or a BIOS list: fills the step's file box, the library folder stays as it is
        if len(picked) != 1:
            return None
        name, step = ("dat", "datcheck") if first.tag == "dat" else ("biosdb", "bioscheck")
        return DropTarget("report", None, (), label, first.reason, option=(name, picked[0]), step=step)
    if page in ("fix", "patch"):
        return DropTarget(page, parent, (), label, first.reason, step="discfix" if page == "fix" else "", step_exclusive=page == "fix")
    return DropTarget(page, parent, picked, label, first.reason, archive=any(h.archive for _p, h in hits),
                      guess=any(h.guess for _p, h in hits))
