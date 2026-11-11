"""Bring a player's history across from another front end, so they can switch without losing it: favourites, play time and count, the last time
a game was played, their stars, progress and notes, and their collections.

Two sources are read: a LaunchBox folder (its Data/Platforms and Data/Playlists XML files) and a Playnite export (a JSON file; Playnite's own
database is a binary LiteDB file that can't be read without it, see PLAYNITE_COMMAND for how to make the JSON).

Only games Distill already has are touched: the library comes from scanning folders, so a game in the other program that isn't in a scanned
folder is counted and listed, not added. Nothing is overwritten that the player did in Distill: a favourite is only ever switched on, play
time and counts only go up, and the other details only fill what is empty. Importing twice therefore changes nothing the second time.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from . import games as gm

PLAYNITE_COMMAND = (
    "$PlayniteApi.Database.Games | Select-Object Name,@{n='Platforms';e={@($_.Platforms.Name)}},Favorite,PlayCount,Playtime,LastActivity,UserScore,"
    "@{n='CompletionStatus';e={$_.CompletionStatus.Name}},@{n='Categories';e={@($_.Categories.Name)}},@{n='Roms';e={@($_.Roms.Path)}},GameImagePath,"
    "InstallDirectory,Notes | ConvertTo-Json | Out-File -Encoding utf8 \"$env:USERPROFILE\\Desktop\\playnite-games.json\""
)  # run in Playnite's SDK console: it writes the file read_playnite takes (to the desktop)


@dataclass(slots=True)
class Imported:
    """One game as the other program had it."""
    title: str
    system: str = ""
    paths: list[str] = field(default_factory=list)  # where its file was, resolved to full paths where that could be done
    favorite: bool = False
    play_seconds: float = 0.0
    play_count: int = 0
    last_played: float = 0.0
    rating: int = 0  # whole stars, 0 = none
    status: str = ""  # a key of games.STATUSES
    developer: str = ""
    publisher: str = ""
    year: str = ""
    genre: str = ""
    notes: str = ""
    collections: list[str] = field(default_factory=list)
    source_id: str = ""  # LaunchBox's game id (how its playlists point at the game)


class ImportProblem(Exception):
    """Something the player can act on: the folder isn't a LaunchBox, the file isn't an export."""


# ---- reading values ---------------------------------------------------------------------------------------------------
def _epoch(text) -> float:
    """A date as the other program wrote it ('2023-05-01T12:30:00-04:00', '2023-05-01 12:30:00', '/Date(1682944200000)/') as an epoch; 0 for none."""
    s = str(text or "").strip()
    if not s or s.startswith("0001"):
        return 0.0
    if m := re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", s):  # PowerShell's older JSON dates
        return max(0.0, int(m[1]) / 1000.0)
    try:
        return max(0.0, datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0.0


def _num(text, default: float = 0.0) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def _yes(text) -> bool:
    return str(text).strip().lower() in ("true", "1", "yes")


def _status(text: str) -> str:
    """Distill's progress key for how another program words progress ('Completed', 'Beaten', 'Plan to Play', 'Abandoned'), '' for none."""
    t = re.sub(r"[^a-z]+", " ", str(text or "").lower()).strip()
    if not t or t in ("not played", "played", "on hold", "none"):
        return ""
    if "plan" in t or "want" in t or "backlog" in t:
        return "plan"
    if "playing" in t:
        return "playing"
    if "complete" in t or "beaten" in t or "finished" in t:
        return "completed"
    if "abandon" in t or "dropped" in t:
        return "dropped"
    return ""


def _year(text) -> str:
    m = re.match(r"\s*(\d{4})", str(text or ""))
    return m[1] if m and m[1] != "0001" else ""


def _stars(value: float) -> int:
    """0 to 5 stars from a 0-5 rating that may be fractional (4.5 -> 5), with anything above zero at least one star."""
    return gm.clamp_rating(max(1, int(value + 0.5))) if value > 0 else 0


def _resolve(path: str, base: Path) -> str:
    """A path as the other program stored it ('..\\Roms\\a.sfc', relative to its own folder) as a full path."""
    p = (path or "").strip().strip('"')
    if not p:
        return ""
    p = p.replace("\\", os.sep) if os.sep == "/" else p.replace("/", os.sep)
    return os.path.normpath(p if os.path.isabs(p) or re.match(r"^[A-Za-z]:", p) else str(base / p))


# ---- LaunchBox ------------------------------------------------------------------------------------------------------
def launchbox_data(folder: Path | str) -> tuple[Path, Path]:
    """(LaunchBox's own folder, its Data folder) from whatever the player picked: LaunchBox itself, its Data folder or its Platforms folder."""
    f = Path(folder)
    for root, data in ((f, f / "Data"), (f.parent, f), (f.parent.parent, f.parent)):
        if (data / "Platforms").is_dir():
            return root, data
    raise ImportProblem("That doesn't look like a LaunchBox folder: it has no Data\\Platforms folder in it. Pick the folder that holds LaunchBox.exe.")


def _text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def read_launchbox(folder: Path | str) -> list[Imported]:
    """Every game in a LaunchBox library with its favourite, play time and count, last played, stars, progress and notes, and the playlists it is in."""
    root, data = launchbox_data(folder)
    games: list[Imported] = []
    by_id: dict[str, Imported] = {}
    for xml in sorted((data / "Platforms").glob("*.xml")):
        try:
            tree = ET.parse(xml)
        except (ET.ParseError, OSError):
            continue  # one broken file must not stop the rest
        for node in tree.getroot().iter("Game"):
            title = _text(node, "Title")
            if not title:
                continue
            platform = _text(node, "Platform") or xml.stem
            path = _resolve(_text(node, "ApplicationPath"), root)
            status = "completed" if _yes(_text(node, "Completed")) else _status(_text(node, "Status"))
            g = Imported(
                title=title, system=gm.canonical_system(platform), paths=[path] if path else [], favorite=_yes(_text(node, "Favorite")),
                play_seconds=_num(_text(node, "PlayTime")), play_count=int(_num(_text(node, "PlayCount"))), last_played=_epoch(_text(node, "LastPlayedDate")),
                rating=_stars(_num(_text(node, "StarRating"))), status=status, developer=_text(node, "Developer"), publisher=_text(node, "Publisher"),
                year=_year(_text(node, "ReleaseDate")), genre=_text(node, "Genre").replace(";", ", ").replace(" ,", ","), notes=_text(node, "Notes"),
                source_id=_text(node, "ID"))
            games.append(g)
            if g.source_id:
                by_id[g.source_id] = g
    playlists = data / "Playlists"
    for xml in sorted(playlists.glob("*.xml")) if playlists.is_dir() else ():
        try:
            top = ET.parse(xml).getroot()
        except (ET.ParseError, OSError):
            continue
        name = next((_text(p, "Name") for p in top.iter("Playlist") if _text(p, "Name")), "") or xml.stem
        for member in top.iter("PlaylistGame"):
            g = by_id.get(_text(member, "GameId"))
            if g is not None and name not in g.collections:
                g.collections.append(name)
    return games


# ---- Playnite ------------------------------------------------------------------------------------------------------
def _get(d: dict, *names: str):
    """A value from a dictionary whatever the case of its key ('Name', 'name')."""
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in lower and lower[n.lower()] is not None:
            return lower[n.lower()]
    return None


def _names(value) -> list[str]:
    """A list of names from a value that may be a string, a {'Name': ...} object, or a list of either (PowerShell turns a list of one into a bare value)."""
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        return [str(value)] if str(value).strip() else []
    if isinstance(value, dict):
        n = _get(value, "Name", "Path")
        return [str(n)] if n else []
    out: list[str] = []
    for v in value if isinstance(value, list) else [value]:
        out += _names(v)
    return out


def read_playnite(path: Path | str) -> list[Imported]:
    """The games in a Playnite export (a JSON list of games, or an object with a 'Games' list)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ImportProblem(f"Couldn't read that file as JSON: {exc}") from exc
    if isinstance(data, dict):
        data = _get(data, "Games", "value") or [data]
    if not isinstance(data, list):
        raise ImportProblem("That file isn't a list of games.")
    out: list[Imported] = []
    for item in data:
        if not isinstance(item, dict) or not (title := str(_get(item, "Name", "Title") or "").strip()):
            continue
        install = str(_get(item, "InstallDirectory") or "")
        paths = []
        for raw in [*_names(_get(item, "Roms")), *_names(_get(item, "GameImagePath"))]:
            raw = raw.replace("{InstallDir}", install).replace("{InstallDirectory}", install)
            if "{" in raw or not raw.strip():  # a Playnite variable that can't be filled in here: the name will have to do
                continue
            paths.append(_resolve(raw, Path(install) if install else Path(".")))
        score = _num(_get(item, "UserScore"))
        platforms = _names(_get(item, "Platforms", "Platform"))
        out.append(Imported(
            title=title, system=gm.canonical_system(platforms[0]) if platforms else "", paths=list(dict.fromkeys(paths)),
            favorite=_yes(_get(item, "Favorite")), play_seconds=_num(_get(item, "Playtime", "PlayTime")), play_count=int(_num(_get(item, "PlayCount"))),
            last_played=_epoch(_get(item, "LastActivity", "LastPlayed")), rating=_stars(score / 20.0) if score > 0 else 0,
            status=_status(next(iter(_names(_get(item, "CompletionStatus"))), "")), notes=str(_get(item, "Notes") or "").strip(),
            collections=list(dict.fromkeys(_names(_get(item, "Categories"))))))
    return out


# ---- matching and applying ---------------------------------------------------------------------------------------------
def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", gm.clean_title(title).casefold())


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)).replace("\\", "/").casefold()


class Index:
    """The library's games, looked up by file, then by name on the same platform, then by name alone (when only one game has it)."""

    def __init__(self, games: Iterable[gm.Game]):
        self.by_path: dict[str, gm.Game] = {}
        self.by_name_system: dict[tuple[str, str], list[gm.Game]] = {}
        self.by_name: dict[str, list[gm.Game]] = {}
        for g in games:
            self.by_path.setdefault(_path_key(g.path), g)
            key = _title_key(g.name)
            self.by_name_system.setdefault((key, gm.canonical_system(g.system).casefold()), []).append(g)
            self.by_name.setdefault(key, []).append(g)

    @staticmethod
    def _best(found: list[gm.Game]) -> gm.Game:
        return min(found, key=lambda g: (gm._region_rank(g.region), g.id))  # of several versions of one game, the one a poster would show first

    def find(self, imp: Imported) -> gm.Game | None:
        for p in imp.paths:
            if (g := self.by_path.get(_path_key(p))) is not None:
                return g
        key = _title_key(imp.title)
        if imp.system and (found := self.by_name_system.get((key, imp.system.casefold()))):
            return self._best(found)
        found = self.by_name.get(key, [])
        return found[0] if len(found) == 1 else None


@dataclass(slots=True)
class Report:
    source: str = ""
    seen: int = 0  # games in the other program
    matched: int = 0  # ... that are in the Distill library
    favourites: int = 0  # newly switched on
    playtime: int = 0  # games that gained play time, count or a last-played date
    ratings: int = 0
    statuses: int = 0
    details: int = 0  # games that had a blank developer, publisher, year, genre or note filled
    collections: list[str] = field(default_factory=list)  # names created or added to
    unmatched: list[str] = field(default_factory=list)  # titles not in the library

    def summary(self) -> str:
        if not self.seen:
            return f"No games found in the {self.source} library."
        lines = [f"Found {self.seen} games in {self.source}; {self.matched} are in your Distill library."]
        bits = [(self.favourites, "favourite"), (self.playtime, "game with play time or history"), (self.ratings, "star rating"), (self.statuses, "progress status")]
        done = [f"{n} {w}{'s' if n != 1 else ''}" for n, w in bits if n]
        if self.details:
            done.append(f"details filled in on {self.details} game{'s' if self.details != 1 else ''}")
        if self.collections:
            done.append(f"{len(self.collections)} collection{'s' if len(self.collections) != 1 else ''}")
        lines.append("Brought across: " + ", ".join(done) + "." if done else "Nothing new to bring across: your Distill library already has all of it.")
        if self.unmatched:
            shown = ", ".join(self.unmatched[:6]) + (f" and {len(self.unmatched) - 6} more" if len(self.unmatched) > 6 else "")
            lines.append(f"Not in your library, so left out: {shown}. Add the folder that holds them under Libraries and import again.")
        return "\n\n".join(lines)


def apply(store: gm.GameStore, imported: Iterable[Imported], source: str = "") -> Report:
    """Match what was read to the library and bring the history across (see the module's note on what is and isn't overwritten)."""
    imported = list(imported)
    report = Report(source=source, seen=len(imported))
    index = Index(store.games())
    made: dict[str, int] = {}  # collection name (any case) -> its id
    shown: dict[str, str] = {}  # ... -> the name as the other program wrote it
    for imp in imported:
        g = index.find(imp)
        if g is None:
            report.unmatched.append(imp.title)
            continue
        report.matched += 1
        if imp.favorite and not g.favorite:
            store.set_favorite(g.id, True)
            report.favourites += 1
        if store.import_stats(g.id, imp.play_seconds, imp.play_count, imp.last_played):
            report.playtime += 1
        fill: dict = {}
        if imp.rating and not g.rating:
            fill["rating"] = imp.rating
            report.ratings += 1
        if imp.status in gm.STATUSES and not g.status:
            fill["status"] = imp.status
            report.statuses += 1
        extra = {k: v for k, v in (("developer", imp.developer), ("publisher", imp.publisher), ("year", imp.year), ("genre", imp.genre), ("notes", imp.notes))
                 if v and not getattr(g, k)}
        if extra:
            fill.update(extra)
            report.details += 1
        if fill:
            store.update_details(g.id, **fill)
        for name in imp.collections:
            key = name.casefold()
            cid = made.get(key) or store.create_collection(name)
            if cid:
                made[key], shown[key] = cid, shown.get(key, name)
                store.add_to_collection(cid, [g.id])
    report.collections = sorted(shown.values(), key=str.casefold)
    return report
