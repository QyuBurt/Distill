"""Write Distill's library out for other front ends, so it can be used beside them: RetroArch playlists (.lpl) today.

One playlist is written per platform, named the way RetroArch names its own (so the thumbnails it downloads for that platform line up), plus
'Distill Favourites' and one for each of the player's collections. A playlist that already exists is never replaced: what is in it stays exactly
as it is (including the core the player picked for a game), and only games it doesn't have yet are added. Nothing is ever removed.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import games as gm

# Distill's platform -> the name of RetroArch's database for it (which is also what its own playlist for the platform is called)
LPL_DB = {
    "NES": "Nintendo - Nintendo Entertainment System", "SNES": "Nintendo - Super Nintendo Entertainment System", "Nintendo 64": "Nintendo - Nintendo 64",
    "Game Boy": "Nintendo - Game Boy", "Game Boy Color": "Nintendo - Game Boy Color", "Game Boy Advance": "Nintendo - Game Boy Advance",
    "Nintendo DS": "Nintendo - Nintendo DS", "Nintendo 3DS": "Nintendo - Nintendo 3DS", "GameCube": "Nintendo - GameCube", "Wii": "Nintendo - Wii",
    "Virtual Boy": "Nintendo - Virtual Boy", "Famicom Disk System": "Nintendo - Family Computer Disk System",
    "Mega Drive": "Sega - Mega Drive - Genesis", "Master System": "Sega - Master System - Mark III", "Game Gear": "Sega - Game Gear",
    "Sega CD": "Sega - Mega-CD - Sega CD", "Sega Saturn": "Sega - Saturn", "Dreamcast": "Sega - Dreamcast",
    "PlayStation": "Sony - PlayStation", "PlayStation 2": "Sony - PlayStation 2", "PSP": "Sony - PlayStation Portable",
    "PC Engine": "NEC - PC Engine - TurboGrafx 16", "PC Engine CD": "NEC - PC Engine CD - TurboGrafx-CD",
    "Atari 2600": "Atari - 2600", "Atari 7800": "Atari - 7800", "Atari Lynx": "Atari - Lynx", "Neo Geo Pocket": "SNK - Neo Geo Pocket",
    "WonderSwan": "Bandai - WonderSwan", "3DO": "The 3DO Company - 3DO",
}
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def playlist_name(title: str) -> str:
    """A file name (without .lpl) from a playlist's title: no characters a file name can't have."""
    return re.sub(r"\s+", " ", _BAD_NAME.sub(" ", title)).strip(" .") or "Playlist"


def db_name(system: str) -> str:
    return LPL_DB.get(gm.canonical_system(system), "")


@dataclass(slots=True)
class PlaylistResult:
    name: str  # the file's name
    added: int = 0
    kept: int = 0  # games it already had
    skipped: str = ""  # why it was left alone ('' = written)


@dataclass(slots=True)
class ExportReport:
    folder: str = ""
    playlists: list[PlaylistResult] = field(default_factory=list)
    left_out: dict[str, int] = field(default_factory=dict)  # why games weren't included -> how many

    @property
    def written(self) -> list[PlaylistResult]:
        return [p for p in self.playlists if not p.skipped]

    def summary(self) -> str:
        if not self.written and not any(p.skipped for p in self.playlists):
            lines = ["Nothing to export: no games here can be played in RetroArch."]
        else:
            added = sum(p.added for p in self.written)
            lines = [f"Wrote {len(self.written)} playlist{'s' if len(self.written) != 1 else ''} to {self.folder}: {added} game{'s' if added != 1 else ''} "
                     f"added, {sum(p.kept for p in self.written)} already there."]
            lines += [f"{p.name} was left alone: {p.skipped}" for p in self.playlists if p.skipped]
        if self.left_out:
            lines.append("Left out: " + ", ".join(f"{n} {why}" for why, n in self.left_out.items()) + ".")
        return "\n\n".join(lines)


# ---- one playlist -------------------------------------------------------------------------------------------------------
def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)).replace("\\", "/").casefold()


def _item(g: gm.Game, core_path: str, core_name: str) -> dict:
    """One entry as RetroArch writes it. The checksum is left blank (RetroArch fills it in itself if it ever scans the game)."""
    return {"path": str(g.path), "label": g.custom_title or Path(g.path).stem, "core_path": core_path or "DETECT", "core_name": core_name or "DETECT",
            "crc32": "00000000|crc", "db_name": (db_name(g.system) + ".lpl") if db_name(g.system) else ""}


def _read(path: Path) -> dict | None:
    """An existing playlist, or None when it can't be read as RetroArch's JSON playlist (the old plain-text kind, or a damaged file)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items", []), list) else None


def _empty() -> dict:
    return {"version": "1.5", "default_core_path": "", "default_core_name": "", "label_display_mode": 0, "right_thumbnail_mode": 0,
            "left_thumbnail_mode": 0, "sort_mode": 0, "items": []}


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def write_playlist(path: Path, entries: list[tuple[gm.Game, str, str]]) -> PlaylistResult:
    """Make or extend one .lpl with (game, core path, core name) entries. Existing entries are left as they are; only new games are appended."""
    result = PlaylistResult(path.name)
    if path.exists():
        data = _read(path)
        if data is None:
            result.skipped = "it isn't in a format Distill can add to (RetroArch's older plain-text playlists)"
            return result
    else:
        data = _empty()
    items = data.setdefault("items", [])
    have = {_key(str(i.get("path", ""))) for i in items if isinstance(i, dict)}
    for g, core_path, core_name in entries:
        if _key(g.path) in have:
            result.kept += 1
            continue
        have.add(_key(g.path))
        items.append(_item(g, core_path, core_name))
        result.added += 1
    if result.added or not path.exists():
        _write_atomic(path, data)
    return result


# ---- the whole library ------------------------------------------------------------------------------------------------
def retro_entry(g: gm.Game, setup: gm.RetroSetup | None, default: str = "") -> tuple[str, str] | None:
    """(core path, core name) RetroArch would play a game with, ('', '') to let RetroArch pick, or None when the game isn't played in RetroArch
    (it is set to an emulator of its own, or no core plays its platform)."""
    emu, core = gm.parse_choice(g.emulator or default)
    if emu not in ("", gm.RETROARCH):
        return None
    if setup is None:
        return ("", "")
    options = gm.cores_for(g.system, setup.cores, setup.infos)
    core = core if core in setup.cores else (options[0] if options else "")
    if not core:
        return None
    return str(setup.cores[core]), setup.core_label(core)


def export_retroarch(store: gm.GameStore, folder: Path | str, setup: gm.RetroSetup | None, progress: Callable[[str], None] | None = None) -> ExportReport:
    """Write the library's playlists into `folder` (RetroArch's playlist folder, usually)."""
    folder = Path(folder)
    report = ExportReport(str(folder))
    left = report.left_out

    def bump(why: str) -> None:
        left[why] = left.get(why, 0) + 1

    def entries(games: Iterable[gm.Game], count: bool) -> list[tuple[gm.Game, str, str]]:
        out = []
        for g in games:
            if not g.installed or not g.path:
                if count:
                    bump("not downloaded yet")
                continue
            pick = retro_entry(g, setup, store.default_for(g.system))
            if pick is None:
                if count:
                    bump("play in another emulator")
                continue
            out.append((g, *pick))
        return out

    everything = store.games()
    by_system: dict[str, list] = {}
    for item in entries(everything, True):
        by_system.setdefault(gm.canonical_system(item[0].system), []).append(item)
    for system, items in sorted(by_system.items(), key=lambda kv: kv[0].casefold()):
        name = playlist_name(db_name(system) or system) + ".lpl"
        if progress:
            progress(name)
        report.playlists.append(write_playlist(folder / name, items))
    if favourites := entries([g for g in everything if g.favorite], False):
        report.playlists.append(write_playlist(folder / "Distill Favourites.lpl", favourites))
    for cid, cname, _n in store.collections():
        members = entries(store.games(collection=cid), False)
        if members:
            report.playlists.append(write_playlist(folder / (playlist_name(f"Distill - {cname}") + ".lpl"), members))
    return report
