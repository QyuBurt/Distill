"""Backing up a game's saves, and putting them back: the save states and battery saves RetroArch keeps for a game (so a core or emulator
update, a bad save, or a cloud-sync mishap doesn't cost hours of play).

A backup is one zip in Distill's own folder, per game, holding copies of the files plus a manifest saying where each came from, so restoring
puts every file back where its emulator expects it. Restoring first backs up whatever it is about to overwrite. Only RetroArch's saves are
known (where its states and .srm files go is in its settings); an emulator of its own keeps them its own way.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import games as gm
from . import paths

BACKUP_DIR = paths.data_dir() / "save_backups"
MAX_BACKUPS = 20  # older ones of a game are dropped beyond this
SAVE_NAME = re.compile(r"\.(state\d*|state\.auto|srm|rtc)$", re.IGNORECASE)  # the only files a restore will ever write


@dataclass(frozen=True, slots=True)
class SaveFile:
    path: Path
    kind: str  # 'state' or 'save'


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    created: float
    reason: str
    files: int
    size: int  # bytes in the zip
    names: tuple[str, ...]


@dataclass(slots=True)
class Restored:
    restored: int = 0
    skipped: list[str] = None  # type: ignore[assignment]  # files that could not be put back, with why
    safety: Path | None = None  # the backup of what was overwritten

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []


def _folder(game_id: str) -> Path:
    return BACKUP_DIR / re.sub(r"[^\w.-]", "_", game_id)


# ---- finding a game's saves -------------------------------------------------------------------------------------------
def find_saves(setup: gm.RetroSetup, game: gm.Game) -> list[SaveFile]:
    """Every save state and battery save RetroArch has for the game, wherever its settings put them (beside the game, in its own folder, or in
    sub-folders sorted by core or by the game's folder), across every core that could have played it."""
    rom = Path(game.path)
    stem = re.escape(rom.stem)
    states = re.compile(rf"^{stem}\.state(\d*|\.auto)$", re.IGNORECASE)
    saves = re.compile(rf"^{stem}\.(srm|rtc)$", re.IGNORECASE)
    found: dict[Path, SaveFile] = {}
    for core in gm.cores_for(game.system, setup.cores, setup.infos) or [""]:
        for root, pattern, kind in ((..., states, "state"), (setup.save_dir, saves, "save")):
            for folder in gm._state_folders(setup, core, rom, root):
                with contextlib.suppress(OSError):
                    for f in folder.iterdir():
                        if pattern.match(f.name) and f.is_file():
                            found[f] = SaveFile(f, kind)
    return sorted(found.values(), key=lambda s: (s.kind, s.path.name.lower()))


# ---- backups ----------------------------------------------------------------------------------------------------------
def create_backup(game_id: str, title: str, system: str, files: list[SaveFile], reason: str = "", now: float | None = None) -> Path | None:
    """Copy these files into a new backup of the game; None when there is nothing to back up (or the backup could not be written)."""
    files = [f for f in files if f.path.is_file()]
    if not files:
        return None
    when = time.time() if now is None else now
    folder = _folder(game_id)
    base = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    dest, n = folder / f"{base}.zip", 2
    while dest.exists():
        dest, n = folder / f"{base}-{n}.zip", n + 1
    manifest = {"version": 1, "game": title, "system": system, "created": when, "reason": reason,
                "files": [{"arc": f"{i:02d}_{f.path.name}", "origin": str(f.path), "kind": f.kind, "mtime": f.path.stat().st_mtime} for i, f in enumerate(files)]}
    part = dest.with_name(dest.name + ".part")
    try:
        paths.ensure_dir(folder)
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            for entry, f in zip(manifest["files"], files):
                z.write(f.path, entry["arc"])
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
        part.replace(dest)
    except (OSError, zipfile.BadZipFile):
        part.unlink(missing_ok=True)
        return None
    prune(game_id)
    return dest


def read_manifest(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path) as z:
            data = json.loads(z.read("manifest.json"))
        return data if isinstance(data, dict) and isinstance(data.get("files"), list) else None
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def list_backups(game_id: str) -> list[BackupInfo]:
    """The game's backups, newest first (a file that isn't a readable backup is left out)."""
    out: list[BackupInfo] = []
    with contextlib.suppress(OSError):
        for p in _folder(game_id).glob("*.zip"):
            if (m := read_manifest(p)) is not None:
                out.append(BackupInfo(p, float(m.get("created", 0) or 0), str(m.get("reason", "")), len(m["files"]), p.stat().st_size,
                                      tuple(Path(str(f.get("origin", ""))).name for f in m["files"])))
    return sorted(out, key=lambda b: -b.created)


def delete_backup(path: Path) -> None:
    path.unlink(missing_ok=True)


def prune(game_id: str, keep: int = MAX_BACKUPS) -> int:
    """Drop the oldest backups of a game beyond `keep`; returns how many went."""
    old = list_backups(game_id)[keep:]
    for b in old:
        delete_backup(b.path)
    return len(old)


def restore(path: Path, game_id: str = "", title: str = "", system: str = "") -> Restored:
    """Put a backup's files back where they came from. What they would overwrite is backed up first (reason 'before restore'), so a restore
    can itself be undone. Only files that look like saves are ever written, whatever the backup says."""
    out = Restored()
    manifest = read_manifest(path)
    if manifest is None:
        out.skipped.append("That backup can't be read.")
        return out
    entries = [e for e in manifest["files"] if isinstance(e, dict) and e.get("origin") and e.get("arc")]
    targets = [(e, Path(str(e["origin"]))) for e in entries]
    for e, dest in targets:
        if not SAVE_NAME.search(dest.name):
            out.skipped.append(f"{dest.name}: not a save file, left alone")
    ok = [(e, dest) for e, dest in targets if SAVE_NAME.search(dest.name)]
    existing = [SaveFile(dest, str(e.get("kind", "save"))) for e, dest in ok if dest.is_file()]
    if existing and game_id:
        out.safety = create_backup(game_id, title or str(manifest.get("game", "")), system or str(manifest.get("system", "")), existing, "before restore")
    with zipfile.ZipFile(path) as z:
        for e, dest in ok:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_name(dest.name + ".part")
                with z.open(str(e["arc"])) as src, open(part, "wb") as dst:
                    dst.write(src.read())
                part.replace(dest)
                if e.get("mtime"):
                    with contextlib.suppress(OSError):
                        os.utime(dest, (float(e["mtime"]), float(e["mtime"])))
                out.restored += 1
            except (OSError, KeyError, zipfile.BadZipFile) as exc:
                out.skipped.append(f"{dest.name}: {getattr(exc, 'strerror', None) or exc}")
    return out


# ---- many games ---------------------------------------------------------------------------------------------------------
def backup_game(setup: gm.RetroSetup, game: gm.Game, reason: str = "") -> Path | None:
    return create_backup(game.id, game.name, game.system, find_saves(setup, game), reason)


def backup_all(setup: gm.RetroSetup, games: list[gm.Game], reason: str, on_progress: Callable[[int, int, str], None] | None = None,
               cancelled: Callable[[], bool] = lambda: False) -> tuple[int, int]:
    """Back up the saves of every game that has any (a game with none is skipped). Returns (games backed up, files copied)."""
    done = files = 0
    for i, game in enumerate(games):
        if on_progress:
            on_progress(i, len(games), game.name)
        if cancelled():  # (asked after reporting, so a Stop pressed in answer to the report takes effect before this game)
            break
        saves = find_saves(setup, game)
        if saves and create_backup(game.id, game.name, game.system, saves, reason):
            done, files = done + 1, files + len(saves)
    if on_progress:
        on_progress(len(games), len(games), "")
    return done, files
