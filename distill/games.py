"""The Games page's engine, with no window code in it: a saved list of the games in the player's library (a small SQLite file), clean
titles made from file names, which RetroArch core plays which system and the command that starts it, and the details (overview,
developer, genre) and covers that come from the LaunchBox Games Database.

The list is filled from the same scan the Library Report uses. What the player does to a game (favourite, a title of their own, the
emulator they picked, when it was last played) is kept apart from what the scan found, so scanning again never loses it."""
from __future__ import annotations

import contextlib
import hashlib
import html as html_lib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from . import libretro_cores as lc
from . import recomps as rc
from . import systems as sysmod
from .formats import playlist_discs

UNKNOWN = "Unknown system"  # what the scan calls a game it couldn't place (organize.UNKNOWN_FOLDER)
RETROARCH = "retroarch"  # the id of RetroArch in the emulator catalog, and the first part of a launch choice ('retroarch:snes9x')


class GameError(Exception):
    """Something that stops a game from starting, worded for the player."""


# ---- titles ---------------------------------------------------------------------------------------------------------
_TAGS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_ARTICLE_LAST = re.compile(r"^(?P<head>.+?),\s*(?P<art>The|A|An)(?P<rest>\s+-\s+.*)?$")


def clean_title(filename: str) -> str:
    """A game's name from its file name: no extension, no (Region) / [tag] / (Disc 1) notes, and 'Legend of Zelda, The' put back as
    'The Legend of Zelda'. Falls back to the plain file name when nothing is left."""
    suffix = Path(filename).suffix
    stem = Path(filename).stem if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", suffix) else str(filename)
    text = _TAGS.sub("", stem.replace("_", " "))
    text = re.sub(r"\s+", " ", text).strip(" -.")
    if m := _ARTICLE_LAST.match(text):
        text = f"{m['art']} {m['head']}{m['rest'] or ''}"
    return text or stem.strip() or filename


def game_id(path: str) -> str:
    """A stable id for a file, from its path (a game that is moved is a new game)."""
    key = str(path).replace("\\", "/").casefold()
    return "g" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class Game:
    id: str
    title: str  # as cleaned from the file name
    system: str
    path: str
    kind: str = ""  # 'CHD', 'ISO', 'GBA' ... the file extension
    region: str = ""
    size: int = 0
    modified: float = 0.0
    folder: str = ""
    favorite: bool = False
    last_played: float = 0.0
    play_count: int = 0
    emulator: str = ""  # a launch choice the player made for this game ('retroarch:mgba'); '' = the system's default
    custom_title: str = ""
    developer: str = ""  # what the player (or a LaunchBox lookup in the edit window) said about the game; '' = not set
    publisher: str = ""
    year: str = ""
    genre: str = ""
    custom_system: str = ""  # the platform the player put the game on; `system` already shows it
    added: float = 0.0  # when a scan first found the game
    play_seconds: float = 0.0  # time spent playing, added up from the sessions Distill watched
    last_session: float = 0.0  # how long the latest session was, in seconds
    discs: int = 0  # more than 1 for a multi-disc game: its file is the .m3u playlist that lists the discs
    rating: int = 0  # the player's stars, 1 to 5; 0 = not rated
    status: str = ""  # a key of STATUSES ('' = no status)
    notes: str = ""  # the player's own notes about the game
    source: str = ""  # '' = a file the library scan found; 'romm' = a game on the player's RomM server (its file is where an install puts it)
    remote_id: str = ""  # the game's id on that server
    remote_name: str = ""  # its file name there
    remote_files: int = 0  # how many files it has there (more than 1: installing unpacks a zip)
    cover_url: str = ""  # where the server keeps its cover

    @property
    def remote(self) -> bool:
        return bool(self.source)

    @property
    def installed(self) -> bool:
        """A game from the library scan is always there to start; one from a server is once its file has been downloaded."""
        return not self.source or Path(self.path).is_file()

    @property
    def name(self) -> str:
        """What the player sees: their own title if they gave one."""
        return self.custom_title or self.title


# ---- the saved library ------------------------------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (id TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL, system TEXT NOT NULL, kind TEXT,
    region TEXT, size INTEGER, modified REAL, folder TEXT, root TEXT, added REAL, discs INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS user (id TEXT PRIMARY KEY, custom_title TEXT DEFAULT '', favorite INTEGER DEFAULT 0,
    last_played REAL DEFAULT 0, play_count INTEGER DEFAULT 0, emulator TEXT DEFAULT '', custom_system TEXT DEFAULT '', developer TEXT DEFAULT '',
    publisher TEXT DEFAULT '', year TEXT DEFAULT '', genre TEXT DEFAULT '', hidden INTEGER DEFAULT 0, play_seconds REAL DEFAULT 0, last_session REAL DEFAULT 0,
    rating INTEGER DEFAULT 0, status TEXT DEFAULT '', notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS collections (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_games (cid INTEGER NOT NULL, gid TEXT NOT NULL, PRIMARY KEY (cid, gid));
CREATE TABLE IF NOT EXISTS smart_collections (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, rules TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS games_system ON games (system);
CREATE INDEX IF NOT EXISTS games_root ON games (root);  -- a scan reads and replaces one library folder's games
CREATE INDEX IF NOT EXISTS collection_games_gid ON collection_games (gid);  -- a game's collections, and what remains of it when it is removed
"""
_USER_COLUMNS = {"custom_system": "TEXT DEFAULT ''", "developer": "TEXT DEFAULT ''", "publisher": "TEXT DEFAULT ''", "year": "TEXT DEFAULT ''",
                 "genre": "TEXT DEFAULT ''", "hidden": "INTEGER DEFAULT 0", "play_seconds": "REAL DEFAULT 0", "last_session": "REAL DEFAULT 0",
                 "rating": "INTEGER DEFAULT 0", "status": "TEXT DEFAULT ''", "notes": "TEXT DEFAULT ''"}  # added after the first release: older games.db files get them on open
_GAME_COLUMNS = {"discs": "INTEGER DEFAULT 0", "source": "TEXT DEFAULT ''", "remote_id": "TEXT DEFAULT ''", "remote_name": "TEXT DEFAULT ''",
                 "remote_files": "INTEGER DEFAULT 0", "cover_url": "TEXT DEFAULT ''"}  # the same for the scan's own table
_SYS = "COALESCE(NULLIF(u.custom_system, ''), g.system)"  # the platform the player chose, else the one the scan found
_VISIBLE = "COALESCE(u.hidden, 0) = 0"  # a game taken out of the library stays in the file (so a scan doesn't bring it back) but is never shown
_SELECT = (f"SELECT g.id, g.title, {_SYS}, g.path, g.kind, g.region, g.size, g.modified, g.folder, "
           "COALESCE(u.favorite, 0), COALESCE(u.last_played, 0), COALESCE(u.play_count, 0), COALESCE(u.emulator, ''), "
           "COALESCE(u.custom_title, ''), COALESCE(u.developer, ''), COALESCE(u.publisher, ''), COALESCE(u.year, ''), COALESCE(u.genre, ''), "
           "COALESCE(u.custom_system, ''), COALESCE(g.added, 0), COALESCE(u.play_seconds, 0), COALESCE(u.last_session, 0), COALESCE(g.discs, 0), "
           "COALESCE(u.rating, 0), COALESCE(u.status, ''), COALESCE(u.notes, ''), "
           "COALESCE(g.source, ''), COALESCE(g.remote_id, ''), COALESCE(g.remote_name, ''), COALESCE(g.remote_files, 0), COALESCE(g.cover_url, '') "
           "FROM games g LEFT JOIN user u ON u.id = g.id")


def _game(row: tuple) -> Game:
    return Game(row[0], row[1], row[2], row[3], row[4] or "", row[5] or "", int(row[6] or 0), float(row[7] or 0), row[8] or "",
                bool(row[9]), float(row[10]), int(row[11]), row[12], row[13], row[14], row[15], row[16], row[17], row[18], float(row[19]), float(row[20]), float(row[21]), int(row[22]),
                int(row[23] or 0), row[24] or "", row[25] or "", row[26] or "", row[27] or "", row[28] or "", int(row[29] or 0), row[30] or "")


class GameStore:
    """games.db beside state.json. Every call opens its own connection, so the page's threads can use one store freely."""

    def __init__(self, path: Path | None = None):
        self.path = path or rc.root() / "games.db"
        self._lock = threading.RLock()
        with self._db() as db:
            db.executescript(_SCHEMA)
            have = {r[1] for r in db.execute("PRAGMA table_info(user)")}
            for column, kind in _USER_COLUMNS.items():
                if column not in have:
                    db.execute(f"ALTER TABLE user ADD COLUMN {column} {kind}")
            have = {r[1] for r in db.execute("PRAGMA table_info(games)")}
            for column, kind in _GAME_COLUMNS.items():
                if column not in have:
                    db.execute(f"ALTER TABLE games ADD COLUMN {column} {kind}")

    @contextlib.contextmanager
    def _db(self):
        with self._lock:
            db = sqlite3.connect(str(self.path), timeout=15)
            try:
                yield db
                db.commit()
            finally:
                db.close()

    # ---- filling it from a scan ---------------------------------------------------------------------------
    def sync(self, entries: Iterable, root: str, fallback_system: str = "") -> tuple[int, int]:
        """Make the list under `root` match a scan (LibraryEntry objects). Returns (new games, games that are gone). The player's own
        changes to a game stay, even for one that is gone for now: put the file back and they are back too. `fallback_system` is the platform
        for a game the scan couldn't place (the library folder itself is named for a platform, see `folder_platform`)."""
        now = time.time()
        rows, seen = [], set()
        merged: dict[str, list[str]] = {}  # a playlist game's id -> the ids its discs had as games of their own
        for e in entries:
            path = getattr(e, "path", "")
            if not path:
                continue
            gid = game_id(path)
            if gid in seen:
                continue
            seen.add(gid)
            system = e.system or UNKNOWN
            if system == UNKNOWN and fallback_system:
                system = fallback_system
            rows.append((gid, path, clean_title(getattr(e, "name", "") or Path(path).name), system, e.kind, e.region, int(e.size),
                         float(e.modified), e.folder, root, now, int(getattr(e, "discs", 0) or 0)))
            if members := getattr(e, "members", ()):
                merged[gid] = [game_id(m) for m in members]
        with self._db() as db:
            before = {r[0] for r in db.execute("SELECT id FROM games WHERE root = ?", (root,))}
            db.executemany(
                "INSERT INTO games (id, path, title, system, kind, region, size, modified, folder, root, added, discs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET path=excluded.path, title=excluded.title, system=excluded.system, kind=excluded.kind, "
                "region=excluded.region, size=excluded.size, modified=excluded.modified, folder=excluded.folder, root=excluded.root, discs=excluded.discs", rows)
            for gid, old in merged.items():  # a game whose discs were separate games until now: it inherits what the player did to them
                if gid not in before:
                    self._inherit(db, gid, [o for o in old if o in before and o not in seen])
            gone = before - seen
            db.executemany("DELETE FROM games WHERE id = ?", [(g,) for g in gone])
        return len(seen - before), len(gone)

    def sync_remote(self, entries: Iterable, root: str, source: str = "romm") -> tuple[int, int]:
        """Make the games from one server (`root`, such as 'romm:host:port') match what it listed (romm.RemoteEntry objects). Returns (new games,
        games that are gone). Like `sync`, what the player did to a game stays. The year, genre and developer the server knows are filled in
        only where the player hasn't written their own. Local games are never touched, and a server that lists nothing removes nothing."""
        now = time.time()
        entries = [e for e in entries if getattr(e, "remote_id", "")]
        if not entries:
            return 0, 0
        key = root.split(":", 1)[-1]
        rows, seen = [], set()
        for e in entries:
            gid = game_id(f"{source}://{key}/{e.remote_id}")
            if gid in seen:
                continue
            seen.add(gid)
            rows.append((gid, e.path, e.title, e.system or UNKNOWN, e.kind, e.region, int(e.size), 0.0, e.folder, root, now,
                         int(e.discs), source, str(e.remote_id), e.remote_name, int(e.remote_files), e.cover_url, e))
        with self._db() as db:
            before = {r[0] for r in db.execute("SELECT id FROM games WHERE root = ?", (root,))}
            db.executemany(
                "INSERT INTO games (id, path, title, system, kind, region, size, modified, folder, root, added, discs, source, remote_id, remote_name, remote_files, cover_url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET path=excluded.path, title=excluded.title, system=excluded.system, kind=excluded.kind, region=excluded.region, "
                "size=excluded.size, folder=excluded.folder, root=excluded.root, discs=excluded.discs, source=excluded.source, remote_id=excluded.remote_id, "
                "remote_name=excluded.remote_name, remote_files=excluded.remote_files, cover_url=excluded.cover_url", [r[:-1] for r in rows])
            for r in rows:
                e = r[-1]
                if not (e.year or e.genre or e.developer):
                    continue
                self._touch(db, r[0])
                for column, value in (("year", e.year), ("genre", e.genre), ("developer", e.developer)):
                    if value:
                        db.execute(f"UPDATE user SET {column} = ? WHERE id = ? AND COALESCE({column}, '') = ''", (value, r[0]))
            gone = before - seen
            db.executemany("DELETE FROM games WHERE id = ?", [(g,) for g in gone])
        return len(seen - before), len(gone)

    @staticmethod
    def _inherit(db, gid: str, old: list[str]) -> None:
        """Give a game that now stands for several earlier ones (a playlist that replaced its discs) what the player had done to them: a
        favourite, the play time and count added up, the latest play, and a title, platform or details they set; and a place in the same
        collections."""
        rows = [r for o in old if (r := db.execute("SELECT custom_title, favorite, last_played, play_count, emulator, custom_system, developer, publisher, "
                                                    "year, genre, hidden, play_seconds, last_session, rating, status, notes FROM user WHERE id = ?", (o,)).fetchone())]
        if rows:
            first = lambda i: next((r[i] for r in rows if r[i]), "")  # noqa: E731
            db.execute("INSERT OR IGNORE INTO user (id) VALUES (?)", (gid,))
            db.execute("UPDATE user SET custom_title=?, favorite=?, last_played=?, play_count=?, emulator=?, custom_system=?, developer=?, publisher=?, "
                       "year=?, genre=?, hidden=?, play_seconds=?, last_session=?, rating=?, status=?, notes=? WHERE id = ?",
                       (first(0), max(r[1] or 0 for r in rows), max(r[2] or 0 for r in rows), sum(r[3] or 0 for r in rows), first(4), first(5), first(6),
                        first(7), first(8), first(9), max(r[10] or 0 for r in rows), sum(r[11] or 0 for r in rows),
                        max(rows, key=lambda r: r[2] or 0)[12] or 0, max(r[13] or 0 for r in rows), first(14), first(15), gid))
        for o in old:
            db.execute("INSERT OR IGNORE INTO collection_games (cid, gid) SELECT cid, ? FROM collection_games WHERE gid = ?", (gid, o))
            db.execute("DELETE FROM collection_games WHERE gid = ?", (o,))
            db.execute("DELETE FROM user WHERE id = ?", (o,))

    # ---- reading it ---------------------------------------------------------------------------------------
    def games(self, system: str = "", query: str = "", favorites: bool = False, recent: bool = False, sort: str = "name",
              filters: dict | None = None, collection: int = 0, smart: int = 0, added: bool = False) -> list[Game]:
        if smart:  # a smart collection's saved rules, with whatever is chosen now added on top
            rules = self.smart_rules(smart)
            system = system or rules.get("system", "")
            query = " ".join(x for x in (rules.get("query", ""), query) if x)
            favorites = favorites or bool(rules.get("favorites"))
            filters = {**rules.get("filters", {}), **(filters or {})}
        where, args = [_VISIBLE], []
        if collection:
            where.append("g.id IN (SELECT gid FROM collection_games WHERE cid = ?)")
            args.append(int(collection))
        if system:
            where.append(f"{_SYS} = ?")
            args.append(system)
        if favorites:
            where.append("COALESCE(u.favorite, 0) = 1")
        if recent:
            where.append("COALESCE(u.last_played, 0) > 0")
        if added:  # the Recently added pill: what a scan found in the last RECENT_ADD_DAYS days
            where.append("COALESCE(g.added, 0) > ?")
            args.append(time.time() - RECENT_ADD_DAYS * 86400)
        sql = _SELECT + " WHERE " + " AND ".join(where)
        with self._db() as db:
            games = [_game(r) for r in db.execute(sql, args)]
        if query.strip():
            words = [w for w in re.split(r"\s+", query.casefold().strip()) if w]
            games = [g for g in games if all(w in g.name.casefold() or w in g.system.casefold() or w in g.developer.casefold() or w in g.publisher.casefold() or w in g.notes.casefold()
                                             for w in words)]
        if filters:
            games = [g for g in games if matches_filters(g, filters)]
        return sort_games(games, "played" if recent and sort == "name" else "added" if added and sort == "name" else sort)

    def facets(self) -> dict[str, list[tuple[str, int]]]:
        """What the filters can offer: the genres, decades, developers, publishers and regions in the library, each with its game count."""
        return facets(self.games())

    # ---- collections ----------------------------------------------------------------------------------------
    def collections(self) -> list[tuple[int, str, int]]:
        """(id, name, games in it), alphabetical."""
        with self._db() as db:
            rows = db.execute("SELECT c.id, c.name, COUNT(CASE WHEN g.id IS NOT NULL AND COALESCE(u.hidden, 0) = 0 THEN 1 END) FROM collections c "
                              "LEFT JOIN collection_games cg ON cg.cid = c.id LEFT JOIN games g ON g.id = cg.gid LEFT JOIN user u ON u.id = g.id "
                              "GROUP BY c.id").fetchall()
        return sorted(((int(i), n, int(c)) for i, n, c in rows), key=lambda r: r[1].casefold())

    def create_collection(self, name: str) -> int:
        """Make a collection (or give back the id of the one with that name already). 0 for an empty name."""
        name = " ".join(name.split())
        if not name:
            return 0
        with self._db() as db:
            row = db.execute("SELECT id FROM collections WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            return int(row[0]) if row else int(db.execute("INSERT INTO collections (name) VALUES (?)", (name,)).lastrowid)

    def rename_collection(self, cid: int, name: str) -> None:
        name = " ".join(name.split())
        if name:
            with self._db() as db:
                db.execute("UPDATE collections SET name = ? WHERE id = ?", (name, int(cid)))

    def delete_collection(self, cid: int) -> None:
        """Delete the collection; the games in it stay in the library."""
        with self._db() as db:
            db.execute("DELETE FROM collection_games WHERE cid = ?", (int(cid),))
            db.execute("DELETE FROM collections WHERE id = ?", (int(cid),))

    def add_to_collection(self, cid: int, gids: Iterable[str]) -> None:
        with self._db() as db:
            db.executemany("INSERT OR IGNORE INTO collection_games (cid, gid) VALUES (?, ?)", [(int(cid), g) for g in gids])

    def remove_from_collection(self, cid: int, gids: Iterable[str]) -> None:
        with self._db() as db:
            db.executemany("DELETE FROM collection_games WHERE cid = ? AND gid = ?", [(int(cid), g) for g in gids])

    # ---- smart collections: a saved search that stays up to date -----------------------------------------------
    def smart_collections(self) -> list[tuple[int, str, int]]:
        """(id, name, games that match now), alphabetical."""
        with self._db() as db:
            rows = db.execute("SELECT id, name FROM smart_collections").fetchall()
        return sorted(((int(i), n, len(self.games(smart=int(i)))) for i, n in rows), key=lambda r: r[1].casefold())

    def smart_rules(self, sid: int) -> dict:
        """The rules saved for a smart collection ({} when there is none)."""
        with self._db() as db:
            row = db.execute("SELECT rules FROM smart_collections WHERE id = ?", (int(sid),)).fetchone()
        try:
            return clean_rules(json.loads(row[0])) if row else {}
        except (ValueError, TypeError):
            return {}

    def save_smart_collection(self, name: str, rules: dict) -> int:
        """Save rules under a name (the same name again replaces its rules). 0 for an empty name or rules that match everything."""
        name, rules = " ".join(name.split()), clean_rules(rules)
        if not name or not rules:
            return 0
        text = json.dumps(rules, sort_keys=True)
        with self._db() as db:
            row = db.execute("SELECT id FROM smart_collections WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if row:
                db.execute("UPDATE smart_collections SET rules = ? WHERE id = ?", (text, int(row[0])))
                return int(row[0])
            return int(db.execute("INSERT INTO smart_collections (name, rules) VALUES (?, ?)", (name, text)).lastrowid)

    def rename_smart_collection(self, sid: int, name: str) -> None:
        name = " ".join(name.split())
        if name:
            with self._db() as db:
                db.execute("UPDATE smart_collections SET name = ? WHERE id = ?", (name, int(sid)))

    def delete_smart_collection(self, sid: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM smart_collections WHERE id = ?", (int(sid),))

    def get(self, gid: str) -> Game | None:
        with self._db() as db:
            row = db.execute(_SELECT + " WHERE g.id = ?", (gid,)).fetchone()
        return _game(row) if row else None

    def systems(self) -> list[tuple[str, int]]:
        """(system, number of games), alphabetical with the unplaced ones last."""
        with self._db() as db:
            rows = db.execute(f"SELECT {_SYS}, COUNT(*) FROM games g LEFT JOIN user u ON u.id = g.id WHERE {_VISIBLE} GROUP BY 1").fetchall()
        return sorted(rows, key=lambda r: (r[0] == UNKNOWN, r[0].casefold()))

    def totals(self) -> tuple[int, int, int]:
        """(all games, favourites, ones played)."""
        with self._db() as db:
            total = db.execute(f"SELECT COUNT(*) FROM games g LEFT JOIN user u ON u.id = g.id WHERE {_VISIBLE}").fetchone()[0]
            fav = db.execute(f"SELECT COUNT(*) FROM games g JOIN user u ON u.id = g.id WHERE u.favorite = 1 AND {_VISIBLE}").fetchone()[0]
            played = db.execute(f"SELECT COUNT(*) FROM games g JOIN user u ON u.id = g.id WHERE u.last_played > 0 AND {_VISIBLE}").fetchone()[0]
        return total, fav, played

    def installed_count(self) -> int:
        """How many games can be started from this computer now: every game from the library scan, and the RomM games whose file has been downloaded."""
        with self._db() as db:
            local = db.execute(f"SELECT COUNT(*) FROM games g LEFT JOIN user u ON u.id = g.id WHERE {_VISIBLE} AND COALESCE(g.source, '') = ''").fetchone()[0]
            remote = [r[0] for r in db.execute(f"SELECT g.path FROM games g LEFT JOIN user u ON u.id = g.id WHERE {_VISIBLE} AND COALESCE(g.source, '') != ''")]
        return local + sum(1 for p in remote if Path(p).is_file())

    def recent_added(self) -> int:
        """How many games a scan first found in the last RECENT_ADD_DAYS days (the Recently added pill's count)."""
        with self._db() as db:
            return db.execute(f"SELECT COUNT(*) FROM games g LEFT JOIN user u ON u.id = g.id WHERE {_VISIBLE} AND COALESCE(g.added, 0) > ?",
                              (time.time() - RECENT_ADD_DAYS * 86400,)).fetchone()[0]

    # ---- what the player does -----------------------------------------------------------------------------
    def _touch(self, db, gid: str) -> None:
        db.execute("INSERT OR IGNORE INTO user (id) VALUES (?)", (gid,))

    def set_favorite(self, gid: str, on: bool) -> None:
        with self._db() as db:
            self._touch(db, gid)
            db.execute("UPDATE user SET favorite = ? WHERE id = ?", (1 if on else 0, gid))

    def set_title(self, gid: str, title: str) -> None:
        with self._db() as db:
            self._touch(db, gid)
            db.execute("UPDATE user SET custom_title = ? WHERE id = ?", (title.strip(), gid))

    def set_emulator(self, gid: str, choice: str) -> None:
        with self._db() as db:
            self._touch(db, gid)
            db.execute("UPDATE user SET emulator = ? WHERE id = ?", (choice, gid))

    def update_details(self, gid: str, *, title: str | None = None, system: str | None = None, developer: str | None = None,
                       publisher: str | None = None, year: str | None = None, genre: str | None = None, rating: int | None = None,
                       status: str | None = None, notes: str | None = None) -> None:
        """Save what the edit window collected. A field left as None is not touched; '' clears it (the title and platform then go back to what
        the file name and the scan say). The rating is clamped to 0-5 and an unknown status is stored as none."""
        fields = {"custom_title": title, "custom_system": system, "developer": developer, "publisher": publisher, "year": year, "genre": genre,
                  "status": None if status is None else (status.strip() if status.strip() in STATUSES else ""),
                  "notes": None if notes is None else notes.strip()}
        fields = {k: v.strip() if isinstance(v, str) and k != "notes" else v for k, v in fields.items() if v is not None}
        if "custom_system" in fields:
            fields["custom_system"] = canonical_system(fields["custom_system"])
        if rating is not None:
            fields["rating"] = clamp_rating(rating)
        if not fields:
            return
        with self._db() as db:
            self._touch(db, gid)
            db.execute(f"UPDATE user SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*fields.values(), gid])

    def set_system_many(self, ids: Iterable[str], system: str) -> None:
        """Put games on a platform ('' = back to the one the scan found)."""
        with self._db() as db:
            for gid in ids:
                self._touch(db, gid)
                db.execute("UPDATE user SET custom_system = ? WHERE id = ?", (canonical_system(system), gid))

    def update_details_many(self, ids: Iterable[str], *, developer: str | None = None, publisher: str | None = None, year: str | None = None,
                            genre: str | None = None) -> int:
        """Set the same developer, publisher, year or genre on many games at once. A field left as None is not touched; '' clears it. A year
        that isn't four digits is ignored. Returns how many games were written."""
        fields = {"developer": developer, "publisher": publisher, "year": year, "genre": genre}
        fields = {k: " ".join(v.split()) for k, v in fields.items() if v is not None}
        if "year" in fields and fields["year"] and not (len(fields["year"]) == 4 and fields["year"].isdigit()):
            del fields["year"]
        ids = list(dict.fromkeys(ids))
        if not fields or not ids:
            return 0
        sql = f"UPDATE user SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?"
        with self._db() as db:
            for gid in ids:
                self._touch(db, gid)
                db.execute(sql, [*fields.values(), gid])
        return len(ids)

    def set_favorite_many(self, ids: Iterable[str], on: bool) -> None:
        with self._db() as db:
            for gid in ids:
                self._touch(db, gid)
                db.execute("UPDATE user SET favorite = ? WHERE id = ?", (1 if on else 0, gid))

    def hide(self, ids: Iterable[str]) -> int:
        """Take games out of the library. Their files are never touched, and a scan doesn't bring them back."""
        ids = list(ids)
        with self._db() as db:
            for gid in ids:
                self._touch(db, gid)
                db.execute("UPDATE user SET hidden = 1 WHERE id = ?", (gid,))
        return len(ids)

    def record_play(self, gid: str, when: float | None = None) -> None:
        with self._db() as db:
            self._touch(db, gid)
            db.execute("UPDATE user SET last_played = ?, play_count = play_count + 1 WHERE id = ?", (when if when is not None else time.time(), gid))

    # ---- settings -----------------------------------------------------------------------------------------
    def setting(self, key: str, default: str = "") -> str:
        with self._db() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._db() as db:
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def default_for(self, system: str) -> str:
        """The launch choice the player set for a whole system ('' = none: the best installed core is used)."""
        return self.setting(f"default:{system}")

    def set_default(self, system: str, choice: str) -> None:
        self.set_setting(f"default:{system}", choice)

    def add_playtime(self, gid: str, seconds: float) -> None:
        """Add a finished session to the game's total."""
        with self._db() as db:
            self._touch(db, gid)
            db.execute("UPDATE user SET play_seconds = play_seconds + ?, last_session = ? WHERE id = ?", (float(seconds), float(seconds), gid))

    def import_stats(self, gid: str, play_seconds: float, play_count: int, last_played: float) -> bool:
        """Raise a game's play time, play count and last-played date to what another program had; never lower them. True when anything changed."""
        with self._db() as db:
            self._touch(db, gid)
            have = db.execute("SELECT COALESCE(play_seconds, 0), COALESCE(play_count, 0), COALESCE(last_played, 0) FROM user WHERE id = ?", (gid,)).fetchone()
            want = (max(float(have[0]), float(play_seconds)), max(int(have[1]), int(play_count)), max(float(have[2]), float(last_played)))
            if want == (float(have[0]), int(have[1]), float(have[2])):
                return False
            db.execute("UPDATE user SET play_seconds = ?, play_count = ?, last_played = ? WHERE id = ?", (*want, gid))
            return True

    # ---- library folders ------------------------------------------------------------------------------------
    def library_roots(self) -> list[str]:
        """The folders that hold the player's games, in the order they were added (an older single-folder setting counts as the first)."""
        raw = self.setting("library_roots")
        if raw:
            try:
                roots = [str(r) for r in json.loads(raw) if r]
            except (ValueError, TypeError):
                roots = []
            return roots
        old = self.setting("library_root")
        return [old] if old else []

    def _save_roots(self, roots: list[str]) -> None:
        self.set_setting("library_roots", json.dumps(roots))

    def add_library_root(self, path: str) -> bool:
        """Add a folder (False when it is already in the list, spelled any way)."""
        roots = self.library_roots()
        key = os.path.normcase(os.path.normpath(path))
        if any(os.path.normcase(os.path.normpath(r)) == key for r in roots):
            return False
        self._save_roots([*roots, path])
        return True

    def remove_library_root(self, path: str) -> int:
        """Drop a folder and the games found in it from the list (the files are not touched). Returns how many games went."""
        self._save_roots([r for r in self.library_roots() if r != path])
        with self._db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM games WHERE root = ?", (path,))]
            db.executemany("DELETE FROM games WHERE id = ?", [(i,) for i in ids])
        return len(ids)

    def root_counts(self) -> dict[str, int]:
        """How many games each folder has listed."""
        with self._db() as db:
            return {r: n for r, n in db.execute("SELECT root, COUNT(*) FROM games GROUP BY root")}

    def library_root(self) -> str:
        roots = self.library_roots()
        return roots[0] if roots else ""

    def set_library_root(self, path: str) -> None:
        self.add_library_root(path)


SORTS = {"name": "Name (A-Z)", "name_desc": "Name (Z-A)", "year_new": "Year (newest first)", "year_old": "Year (oldest first)",
         "added": "Recently added", "played": "Last played", "plays": "Most played (time)", "platform": "Platform",
         "rating": "Rating (highest first)", "status": "Status"}  # key -> what the sort box says
STATUSES = {"plan": "Plan to play", "playing": "Playing", "completed": "Completed", "dropped": "Dropped"}  # key -> what the player sees, in the order they are listed
MAX_RATING = 5
SOURCES = {"romm": "RomM"}  # a game's source -> what the player sees ('' = this computer; no label)
RECENT_ADD_DAYS = 30  # a game counts as recently added for this long after a scan first found it


def clamp_rating(value) -> int:
    """A rating as stored: a whole number of stars from 0 (none) to 5."""
    try:
        return max(0, min(MAX_RATING, int(value)))
    except (TypeError, ValueError):
        return 0


def sort_games(games: list[Game], key: str) -> list[Game]:
    """The games in the order asked for. Whatever the order, ties go alphabetical; games with no year go last in a year sort."""
    by_name = sorted(games, key=lambda g: (g.name.casefold(), g.region))
    if key == "name_desc":
        return by_name[::-1]
    if key in ("year_new", "year_old"):
        dated = [g for g in by_name if g.year.isdigit()]
        return sorted(dated, key=lambda g: int(g.year), reverse=key == "year_new") + [g for g in by_name if not g.year.isdigit()]
    if key == "added":
        return sorted(by_name, key=lambda g: -g.added)
    if key == "played":
        return sorted(by_name, key=lambda g: -g.last_played)
    if key == "plays":
        return sorted(by_name, key=lambda g: (-g.play_seconds, -g.play_count))
    if key == "platform":
        return sorted(by_name, key=lambda g: (g.system == UNKNOWN, g.system.casefold()))
    if key == "rating":  # unrated games go last
        return sorted(by_name, key=lambda g: (g.rating == 0, -g.rating))
    if key == "status":  # in the order the statuses are listed (Plan to play first); games with no status go last
        order = {k: i for i, k in enumerate(STATUSES)}
        return sorted(by_name, key=lambda g: order.get(g.status, len(order)))
    return by_name


def _split_genres(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,/;]", text or "") if p.strip()]


def decade_of(year: str) -> str:
    """'1994' -> '1990s' ('' when there is no year)."""
    return f"{int(year) // 10 * 10}s" if (year or "").isdigit() else ""


def matches_filters(g: Game, f: dict) -> bool:
    """Whether a game passes the filters: genre, decade ('1990s'), developer, publisher, region, played ('played' | 'unplayed'), status (a key of
    STATUSES, or 'none' for games with no status) and rating ('4' = four stars or more, 'unrated'). A filter that is empty doesn't count. Genre is matched against each of a game's genres."""
    if f.get("genre") and f["genre"].casefold() not in (x.casefold() for x in _split_genres(g.genre)):
        return False
    if f.get("decade") and decade_of(g.year) != f["decade"]:
        return False
    if f.get("developer") and g.developer.casefold() != f["developer"].casefold():
        return False
    if f.get("publisher") and g.publisher.casefold() != f["publisher"].casefold():
        return False
    if f.get("region") and g.region.casefold() != f["region"].casefold():
        return False
    if f.get("status") == "none" and g.status in STATUSES:
        return False
    if f.get("status") in STATUSES and g.status != f["status"]:
        return False
    if f.get("rating") == "unrated" and g.rating:
        return False
    if str(f.get("rating", "")).isdigit() and g.rating < int(f["rating"]):  # '4' = four stars or more
        return False
    if f.get("source") == "romm" and g.source != "romm":
        return False
    if f.get("source") == "local" and g.source:
        return False
    if f.get("installed") == "yes" and not g.installed:
        return False
    if f.get("installed") == "no" and g.installed:
        return False
    if f.get("played") == "played" and not g.play_count:
        return False
    if f.get("played") == "unplayed" and g.play_count:
        return False
    return True


_RULE_FILTERS = ("genre", "decade", "developer", "publisher", "region", "rating", "status", "played", "source", "installed")


def clean_rules(rules: dict | None) -> dict:
    """What a smart collection may remember: a platform, search words, favourites only, and the filters (not 'missing files', which is a check,
    not a property of a game). Empty values and unknown keys are dropped, so a rule set from a newer or older version still loads."""
    rules = rules if isinstance(rules, dict) else {}
    out: dict = {}
    if str(rules.get("system") or "").strip():
        out["system"] = str(rules["system"]).strip()
    if str(rules.get("query") or "").strip():
        out["query"] = " ".join(str(rules["query"]).split())
    if rules.get("favorites"):
        out["favorites"] = True
    filters = rules.get("filters") if isinstance(rules.get("filters"), dict) else {}
    kept = {k: str(filters[k]).strip() for k in _RULE_FILTERS if str(filters.get(k) or "").strip()}
    if kept:
        out["filters"] = kept
    return out


def facets(games: Iterable[Game]) -> dict[str, list[tuple[str, int]]]:
    """For each filter, the values the games have and how many games have each (most first; decades newest first)."""
    counts: dict[str, dict[str, int]] = {k: {} for k in ("genre", "decade", "developer", "publisher", "region")}

    def bump(kind: str, value: str) -> None:
        if value:
            counts[kind][value] = counts[kind].get(value, 0) + 1

    for g in games:
        for genre in _split_genres(g.genre):
            bump("genre", genre)
        bump("decade", decade_of(g.year))
        bump("developer", g.developer.strip())
        bump("publisher", g.publisher.strip())
        bump("region", g.region.strip())
    out = {k: sorted(v.items(), key=lambda kv: (-kv[1], kv[0].casefold())) for k, v in counts.items()}
    out["decade"] = sorted(counts["decade"].items(), key=lambda kv: kv[0], reverse=True)
    return out


# ---- small helpers for the grid and list ------------------------------------------------------------------------------
def pick_random(games: list[Game], avoid: str = "", rng=None) -> Game | None:
    """A game picked at random from `games` (the ones showing), never `avoid` (the last pick) unless it is the only one. None when empty."""
    import random  # noqa: PLC0415
    pool = [g for g in games if g.id != avoid] or list(games)
    return (rng or random).choice(pool) if pool else None


# The list view's columns: key -> (heading, what a click on the heading sorts by first: 'asc' or 'desc'). Numbers and dates start with the most.
LIST_COLUMNS: dict[str, tuple[str, str]] = {
    "name": ("Title", "asc"), "system": ("Platform", "asc"), "source": ("Source", "asc"), "year": ("Year", "asc"), "genre": ("Genre", "asc"), "rating": ("Rating", "desc"),
    "status": ("Progress", "asc"), "last_played": ("Last played", "desc"), "play_seconds": ("Time played", "desc"), "added": ("Added", "desc"),
}


def column_text(g: Game, column: str, now: float | None = None) -> str:
    """What a list cell says."""
    if column == "name":
        return g.name
    if column == "system":
        return g.system
    if column == "source":
        return SOURCES.get(g.source, "")
    if column == "year":
        return g.year
    if column == "genre":
        return g.genre
    if column == "rating":
        return "★" * g.rating if g.rating else ""
    if column == "status":
        return STATUSES.get(g.status, "")
    if column == "last_played":
        return fmt_ago(g.last_played, now) if g.last_played else ""
    if column == "play_seconds":
        return fmt_playtime(g.play_seconds) if g.play_seconds else ""
    if column == "added":
        return fmt_ago(g.added, now) if g.added else ""
    return ""


def sort_by_column(games: list[Game], column: str, descending: bool = False) -> list[Game]:
    """The games ordered by a list column. Games with nothing in that column always go last, whichever way it runs; ties go alphabetical."""
    by_name = sorted(games, key=lambda g: (g.name.casefold(), g.region))
    if column not in LIST_COLUMNS or column == "name":
        return by_name[::-1] if descending and column == "name" else by_name

    def value(g: Game):
        if column == "year":
            return int(g.year) if g.year.isdigit() else None
        if column == "rating":
            return g.rating or None
        if column == "status":
            order = {k: i for i, k in enumerate(STATUSES)}
            return order[g.status] if g.status in order else None
        if column in ("last_played", "play_seconds", "added"):
            return getattr(g, column) or None
        text = column_text(g, column).casefold().strip()
        return text or None

    have = [g for g in by_name if value(g) is not None]
    none = [g for g in by_name if value(g) is None]
    have = sorted(have, key=value, reverse=descending)  # reverse keeps equal rows in their order, so ties stay alphabetical
    return have + none


@dataclass(slots=True)
class PlatformTime:
    system: str
    seconds: float
    games: int  # games on the platform that have been played
    total: int  # games on the platform

    def share(self, whole: float) -> float:
        return self.seconds / whole if whole > 0 else 0.0


def playtime_by_platform(games: Iterable[Game]) -> list[PlatformTime]:
    """Time played on each platform, most first. Platforms with no time are left out; ties go alphabetical."""
    seconds: dict[str, float] = {}
    played: dict[str, int] = {}
    total: dict[str, int] = {}
    for g in games:
        total[g.system] = total.get(g.system, 0) + 1
        if g.play_seconds > 0:
            seconds[g.system] = seconds.get(g.system, 0.0) + g.play_seconds
            played[g.system] = played.get(g.system, 0) + 1
    rows = [PlatformTime(s, t, played[s], total[s]) for s, t in seconds.items()]
    return sorted(rows, key=lambda r: (-r.seconds, r.system.casefold()))


@dataclass(slots=True)
class DatCheck:
    """Games checked against a DAT: ids by what was found."""
    ok: list[str] = field(default_factory=list)  # file is exactly what the DAT lists
    renamed: dict[str, str] = field(default_factory=dict)  # of those, the ones whose file name differs from the DAT's: id -> the DAT's name
    bad: list[str] = field(default_factory=list)  # named like a DAT game but the contents differ (a bad dump, a patch or a hack)
    unknown: list[str] = field(default_factory=list)  # in no DAT entry
    unreadable: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # playlists and cue sheets: a DAT lists the tracks inside, which are checked in Library Report

    @property
    def checked(self) -> int:
        return len(self.ok) + len(self.bad) + len(self.unknown) + len(self.unreadable)

    def summary(self) -> str:
        parts = [f"{len(self.ok)} " + ("match" if len(self.ok) == 1 else "matches")]
        for n, what in ((len(self.bad), "differ from the DAT"), (len(self.unknown), "not in the DAT"), (len(self.unreadable), "unreadable"), (len(self.skipped), "skipped")):
            if n:
                parts.append(f"{n} {what}")
        return ", ".join(parts) + "."


_NOT_HASHED = {".m3u", ".cue", ".gdi", ".ccd"}  # files that only point at others


def verify_games(games: Iterable[Game], dat, stop: threading.Event, note: Callable[[str], None] | None = None) -> DatCheck:
    """Check the games' files against a loaded DAT (datverify.Dat) by hash."""
    from . import datverify as dv  # noqa: PLC0415
    out = DatCheck()
    by_path: dict[str, Game] = {}
    for g in games:
        if Path(g.path).suffix.lower() in _NOT_HASHED:
            out.skipped.append(g.id)
        else:
            by_path[g.path] = g
    rep = dv.verify([Path(p) for p in by_path], dat, stop, note)
    for v in rep.ok:
        g = by_path[str(v.path)]
        out.ok.append(g.id)
        if v.renamed:
            out.renamed[g.id] = v.canonical
    out.bad = [by_path[str(p)].id for p, _g in rep.bad]
    out.unknown = [by_path[str(p)].id for p in rep.unknown]
    out.unreadable = [by_path[str(p)].id for p in rep.unreadable]
    return out


# ---- versions of one game ---------------------------------------------------------------------------------------------
def version_key(g: Game) -> tuple[str, str]:
    """Games with the same key are versions of one game: the same name on the same platform (regions, revisions, formats)."""
    return re.sub(r"\W+", " ", g.name.casefold()).strip(), g.system


def _region_rank(region: str) -> int:
    r = (region or "").casefold()
    for rank, word in enumerate(("usa", "world", "europe", "uk", "japan")):
        if word in r:
            return rank
    return 9


def group_versions(games: list[Game]) -> list[tuple[Game, list[Game]]]:
    """(the version to show, all the versions) for each game, in the order the games came in. The one shown is a favourite, else the
    last played, else the best-known region (USA, World, Europe...), else the first."""
    groups: dict[tuple[str, str], list[Game]] = {}
    for g in games:
        groups.setdefault(version_key(g), []).append(g)
    out = []
    for versions in groups.values():
        shown = min(enumerate(versions), key=lambda iv: (not iv[1].favorite, -iv[1].last_played, _region_rank(iv[1].region), iv[0]))[1]
        out.append((shown, versions))
    return out


def version_label(g: Game) -> str:
    return "  ·  ".join(x for x in (g.region or "Region unknown", g.kind, Path(g.path).name) if x)


def missing_ids(games: Iterable[Game]) -> set[str]:
    """Games whose file isn't there any more (a drive that is unplugged, a file moved)."""
    return {g.id for g in games if not g.source and not Path(g.path).exists()}  # a game on a server that isn't installed isn't missing


def folder_platform(path: str) -> str:
    """The platform a folder's own name says ('PS2' -> 'PlayStation 2', 'Game Boy Advance'), or ''. Used for the library folder the player picks."""
    name = str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return sysmod.system_of_name(name) if name else ""


# ---- RetroArch: which core, which command ----------------------------------------------------------------------------
# a library system -> the label of its row in libretro_cores.PICKS, whose first names are the cores to prefer
_PICK_GROUP = {
    "NES": "NES / Famicom", "Famicom Disk System": "NES / Famicom", "SNES": "SNES / Super Famicom", "Game Boy": "Game Boy / Color",
    "Game Boy Color": "Game Boy / Color", "Game Boy Advance": "Game Boy Advance", "Nintendo DS": "Nintendo DS", "Nintendo 3DS": "Nintendo 3DS",
    "Nintendo 64": "Nintendo 64", "GameCube": "GameCube / Wii", "Wii": "GameCube / Wii", "Mega Drive": "Mega Drive / Genesis",
    "Master System": "Master System / Game Gear", "Game Gear": "Master System / Game Gear", "Sega CD": "Sega CD", "Sega Saturn": "Sega Saturn",
    "Dreamcast": "Dreamcast", "PlayStation": "PlayStation", "PSP": "PSP", "PC Engine": "PC Engine / TurboGrafx-16",
    "PC Engine CD": "PC Engine / TurboGrafx-16", "Neo Geo Pocket": "Neo Geo Pocket", "WonderSwan": "WonderSwan", "Virtual Boy": "Virtual Boy",
    "Atari 2600": "Atari 2600", "Atari 7800": "Atari 7800", "Atari Lynx": "Atari Lynx",
}


def canonical_system(name: str) -> str:
    """Distill's own name for a platform however it is spelled ('Sega Mega Drive', 'Genesis', 'Mega Drive / Genesis', 'SEGA - Mega Drive -
    Genesis' are all 'Mega Drive'). A name nothing recognises comes back as it was."""
    name = " ".join((name or "").split())
    if not name:
        return ""
    known = {n.casefold(): n for n in (*sysmod.SYSTEM_NAMES, *LB_PLATFORM_NAMES, "Switch", "PC")}
    pieces = [name, *(p.strip() for p in re.split(r"\s+/\s+|\s+-\s+|/", name) if p.strip())]
    for piece in pieces:
        for cand in (piece, re.sub(r"^sega\s+", "", piece, flags=re.IGNORECASE)):
            hit = known.get(cand.casefold()) or sysmod.system_of_name(cand) or _LB_TO_SYSTEM.get(cand.casefold())
            if hit:
                return hit
    return name


def preferred_cores(system: str) -> tuple[str, ...]:
    """The cores Distill recommends for a system, best first (whether or not they are installed)."""
    system = canonical_system(system)
    label = _PICK_GROUP.get(system)
    return next((names for group, names, _why in lc.PICKS if group == label), ()) if label else ()


def cores_for(system: str, installed: dict[str, Path], infos: dict[str, dict[str, str]] | None = None) -> list[str]:
    """Installed cores that can play `system`: the recommended ones first, then any other emulator core whose .info says it covers it."""
    infos = infos or {}
    system = canonical_system(system)
    first = [n for n in preferred_cores(system) if n in installed]
    rest = []
    for name in sorted(installed):
        info = infos.get(name, {})
        cats = [c.lower() for c in lc._SPLIT.split(info.get("categories", "")) if c]
        if name not in first and (not cats or "emulator" in cats) and lc.system_matches(info.get("systemname", ""), system):
            rest.append(name)
    return first + rest


def read_infos(info_dir: Path | None, names: Iterable[str]) -> dict[str, dict[str, str]]:
    """The .info file of each installed core (what it is called, which system it plays), for the ones RetroArch has."""
    out: dict[str, dict[str, str]] = {}
    if info_dir is None:
        return out
    for name in names:
        with contextlib.suppress(OSError):
            out[name] = lc.parse_info((info_dir / f"{name}_libretro.info").read_text(encoding="utf-8", errors="replace"))
    return out


@dataclass(slots=True)
class RetroSetup:
    """An installed RetroArch: its program and the cores found in its folder."""
    exe: Path
    cores: dict[str, Path]  # core name -> file
    infos: dict[str, dict[str, str]]
    system_dir: Path | None = None  # RetroArch's system folder: where BIOS files go
    state_dir: Path | None = None  # where its save states go; None = beside the game (RetroArch's "default")
    states_by_core: bool = False  # "Sort save states into folders by core name"
    states_by_content: bool = False  # "... by content directory"
    playlist_dir: Path | None = None  # where its playlists (.lpl) go

    def core_label(self, name: str) -> str:
        return self.infos.get(name, {}).get("display_name") or name.replace("_", " ").title()


def retro_setup(folder: str, exe: str, linked: str = "") -> RetroSetup | None:
    """RetroArch as installed in `folder` (or linked from where it already was), or None when its program is gone."""
    program = Path(folder) / exe
    if not program.exists():
        return None
    try:
        _sub, suffix = lc.target()
    except rc.RecompError:
        return None
    loc = lc.locate(folder, exe, linked)
    cores = lc.scan_installed(loc.cores, suffix)
    state_dir, by_core, by_content = retro_state_options(program, loc)
    return RetroSetup(program, cores, read_infos(loc.info, cores), retro_system_dir(program, loc), state_dir, by_core, by_content,
                      retro_playlist_dir(program, loc))


def retro_system_dir(program: Path, loc) -> Path:
    """RetroArch's system folder (where BIOS files live): the one its retroarch.cfg names, else 'system' beside its settings."""
    exe_dir = program if program.suffix.lower() == ".app" else program.parent
    cfg = lc.read_cfg(loc.config) if getattr(loc, "config", None) else {}
    named = lc._resolve(cfg.get("system_directory", ""), exe_dir, Path.home())
    return named or ((loc.config.parent if getattr(loc, "config", None) else exe_dir) / "system")


def retro_playlist_dir(program: Path, loc) -> Path:
    """RetroArch's playlist folder: the one its retroarch.cfg names, else 'playlists' beside its settings."""
    exe_dir = program if program.suffix.lower() == ".app" else program.parent
    cfg = lc.read_cfg(loc.config) if getattr(loc, "config", None) else {}
    named = lc._resolve(cfg.get("playlist_directory", ""), exe_dir, Path.home())
    return named or ((loc.config.parent if getattr(loc, "config", None) else exe_dir) / "playlists")


@dataclass(slots=True)
class Firmware:
    path: str  # relative to RetroArch's system folder, as the core's .info writes it
    desc: str
    optional: bool


def core_firmware(info: dict[str, str]) -> list[Firmware]:
    """The BIOS / firmware files a core's .info lists."""
    count = info.get("firmware_count", "0")
    out = []
    for i in range(min(int(count) if count.isdigit() else 0, 64)):
        path = info.get(f"firmware{i}_path", "")
        if path:
            out.append(Firmware(path.replace("\\", "/"), info.get(f"firmware{i}_desc", path), info.get(f"firmware{i}_opt", "false").lower() == "true"))
    return out


def missing_firmware(setup: RetroSetup, core: str) -> list[Firmware]:
    """The files the core's .info marks as required that aren't in RetroArch's system folder. (An .info sometimes lists several alternative
    BIOS dumps as required, so this says "may need", not "needs".)"""
    if setup.system_dir is None or core not in setup.infos:
        return []
    out = []
    for fw in core_firmware(setup.infos[core]):
        if fw.optional:
            continue
        found = (setup.system_dir / fw.path).is_file()
        if not found and setup.system_dir.is_dir():
            found = any(p.is_file() for p in setup.system_dir.rglob(Path(fw.path).name))  # put in a sub-folder by hand
        if not found:
            out.append(fw)
    return out


def retro_state_options(program: Path, loc) -> tuple[Path | None, bool, bool]:
    """(RetroArch's save state folder, sorted by core, sorted by content folder) as its retroarch.cfg says; the folder is None when states
    are kept beside the game (the cfg's "default", or no cfg at all)."""
    exe_dir = program if program.suffix.lower() == ".app" else program.parent
    cfg = lc.read_cfg(loc.config) if getattr(loc, "config", None) else {}
    named = lc._resolve(cfg.get("savestate_directory", ""), exe_dir, Path.home())
    yes = lambda key: cfg.get(key, "").lower() == "true"  # noqa: E731
    return named, yes("sort_savestates_enable"), yes("sort_savestates_by_content_enable")


@dataclass(frozen=True, slots=True)
class SaveState:
    """One save state RetroArch wrote for a game: slot 0 is the plain '.state' file, 1 and up are '.state1' ...; saved is its file time."""
    slot: int
    path: Path
    saved: float


def _state_folders(setup: RetroSetup, core: str, rom: Path) -> list[Path]:
    """Every folder RetroArch may have put this game's states in: the save state folder (or the game's own folder when none is set), and the
    sub-folders it makes when states are sorted by core and/or by the game's folder (matched without regard to case)."""
    roots = [setup.state_dir] if setup.state_dir is not None else []
    roots.append(rom.parent)
    info = setup.infos.get(core, {})
    names = {n.casefold() for n in (info.get("corename", ""), info.get("display_name", ""), core, setup.core_label(core)) if n}
    names.add(rom.parent.name.casefold())
    out: list[Path] = []
    for root in roots:
        level = [root]
        for _ in range(2):  # core folder, then content folder inside it
            nxt = []
            for folder in level:
                with contextlib.suppress(OSError):
                    nxt += [d for d in folder.iterdir() if d.is_dir() and d.name.casefold() in names]
            out += level
            level = nxt
        out += level
    return list(dict.fromkeys(out))


def find_save_states(setup: RetroSetup, core: str, rom: Path | str) -> list[SaveState]:
    """The save states RetroArch has for a game, the newest first. The automatic one ('.state.auto') is left out: RetroArch can't be told to load it."""
    rom = Path(rom)
    pattern = re.compile(rf"^{re.escape(rom.stem)}\.state(\d*)$", re.IGNORECASE)
    found: dict[Path, SaveState] = {}
    for folder in _state_folders(setup, core, rom):
        with contextlib.suppress(OSError):
            for f in folder.iterdir():
                m = pattern.match(f.name)
                if m and f.is_file():
                    found[f] = SaveState(int(m[1] or 0), f, f.stat().st_mtime)
    return sorted(found.values(), key=lambda st: -st.saved)


def retroarch_command(exe: Path, core: Path, rom: Path, fullscreen: bool = True, state_slot: int | None = None) -> list[str]:
    """RetroArch's command line for one game: `retroarch -L <core> <game> -f`, and `-e <slot>` to start from that save state."""
    program = exe / "Contents" / "MacOS" / "RetroArch" if exe.suffix.lower() == ".app" else exe
    cmd = [str(program), "-L", str(core), str(rom)]
    if fullscreen:
        cmd.append("-f")
    if state_slot is not None:
        cmd += ["-e", str(state_slot)]
    return cmd


def parse_choice(choice: str) -> tuple[str, str]:
    """('retroarch', 'snes9x') from 'retroarch:snes9x'; ('', '') for no choice."""
    emu, _sep, core = (choice or "").partition(":")
    return (emu, core) if emu else ("", "")


@dataclass(slots=True)
class LaunchPlan:
    cmd: list[str]
    cwd: str
    label: str  # 'RetroArch · mGBA'


@dataclass(slots=True)
class Standalone:
    """An emulator of its own (not RetroArch) that is installed and plays a platform."""
    id: str
    name: str
    exe: Path
    args: str = ""  # the launch options the player wrote for it; '' = Distill's own (STANDALONE_ARGS)


# What to put after the program to start a game: {rom} the game's file, {dir} its folder, {name} its file name without the extension. Each
# emulator's own command-line options, as its documentation gives them; the player can change one (Play with > Launch options) when a
# version wants something else. An emulator not listed here is simply given the game's file.
STANDALONE_ARGS = {
    # Sony
    "duckstation": "-batch -fullscreen -- {rom}", "pcsx2": "-batch -fullscreen -- {rom}", "rpcs3": "--no-gui {rom}",
    "ppsspp": "--fullscreen {rom}", "shadps4": "-g {rom} -f true",
    # Nintendo
    "dolphin": "-b -C Dolphin.Display.Fullscreen=True -e {rom}", "cemu": "-f -g {rom}", "ryujinx": "-f {rom}", "eden": "-f -g {rom}",
    "sudachi": "-f -g {rom}", "azahar": "-f {rom}", "lime3ds": "-f {rom}", "melonds": "-f {rom}", "mgba": "-f {rom}",
    "mesen2": "--fullscreen {rom}", "snes9x": "-fullscreen {rom}", "rmg": "-f {rom}",
    # Microsoft, Sega, Atari, multi-system, PC
    "xenia-canary": "--fullscreen=true {rom}", "xemu": "-full-screen -dvd_path {rom}", "flycast": "-config window:fullscreen=yes {rom}",
    "blastem": "-f {rom}", "ares": "--fullscreen {rom}", "mame": "{name} -rompath {dir} -nowindow -skip_gameinfo", "fbneo": "{name} -f",
    "dosbox-staging": "--fullscreen {rom}", "scummvm": "-f --auto-detect --path={dir}",
    # Left at the plain game file on purpose (no command-line switch for it that we could confirm): they start full screen from their own
    # settings, or can't be started with a game at all (see tests/test_launch_defaults.py for the reasons).
    # "project64", "bigpemu", "ymir", "vita3k", "kytyps5", "anyps5", "eglantine", "lichii", "bluestacks"
}


def launch_args(template: str, game: Game) -> list[str]:
    """The words to put after an emulator's program for a game. A template that never mentions the game (its file, its name or its folder) gets the file added."""
    path = Path(game.path)
    subs = {"{rom}": str(path), "{dir}": str(path.parent), "{name}": path.stem}
    try:
        parts = shlex.split(template or "{rom}")
    except ValueError:
        parts = ["{rom}"]  # unbalanced quotes: fall back to the plain file
    out, used = [], False
    for part in parts:
        used = used or any(k in part for k in ("{rom}", "{name}", "{dir}"))
        for key, value in subs.items():
            part = part.replace(key, value)
        out.append(part)
    return out if used else [*out, str(path)]


def standalone_for(system: str, catalog, installed: Callable[[str], Path | None], args_for: Callable[[str], str] | None = None) -> dict[str, Standalone]:
    """The installed emulators (RetroArch excluded) whose catalog entry says they play `system`, in catalog order. `installed(id)` gives an
    emulator's program or None."""
    out: dict[str, Standalone] = {}
    want = canonical_system(system).casefold()
    for e in catalog:
        # (an entry with no list of systems, like MAME, still plays the one platform its page names)
        if e.id == RETROARCH or want not in (canonical_system(s).casefold() for s in (*e.systems, getattr(e, "system", "") or "")):
            continue
        exe = installed(e.id)
        if exe is not None and e.id not in out:
            out[e.id] = Standalone(e.id, e.name, exe, (args_for(e.id) if args_for else "") or "")
    return out


PLAYLIST_EMULATORS = frozenset({"duckstation", "flycast", "mednafen"})  # standalone emulators that open an .m3u; the others are given the first disc


def standalone_plan(game: Game, s: Standalone) -> LaunchPlan:
    template = s.args or STANDALONE_ARGS.get(s.id, "{rom}")
    if Path(game.path).suffix.lower() == ".m3u" and s.id not in PLAYLIST_EMULATORS and (discs := playlist_discs(Path(game.path))):
        game = replace(game, path=str(discs[0]))  # (the other discs are swapped in the emulator's own disc menu)
    exe = s.exe
    if exe.suffix.lower() == ".app":
        return LaunchPlan(["open", "-a", str(exe), "--args", *launch_args(template, game)], str(exe.parent), s.name)
    return LaunchPlan([str(exe), *launch_args(template, game)], str(exe.parent), s.name)


def plan_launch(game: Game, choice: str, setup: RetroSetup | None, default: str = "", emus: dict[str, Standalone] | None = None,
                resume: bool = False) -> LaunchPlan:
    """What to run for a game. `choice` is the game's own pick, `default` the system's; with neither, the best installed RetroArch core, or
    else an installed emulator of its own that plays the platform. With `resume` the game starts from its newest save state (RetroArch only).
    Raises GameError with something the player can act on."""
    if not Path(game.path).exists():
        raise GameError(f"The file is missing: {game.path}\nScan your library again to update the list.")
    emus = emus or {}
    emu, core = parse_choice(choice or default)
    if resume and emu not in ("", RETROARCH):
        raise GameError("Only games played in RetroArch can continue from a save state. Pick RetroArch under Play with, or just press Play.")
    if emu not in ("", RETROARCH):
        s = emus.get(emu)
        if s is None:
            raise GameError(f"{emu} isn't installed, or doesn't play {game.system}. Install it on the Emulators page, or pick another to play with.")
        return standalone_plan(game, s)
    if setup is None:
        if not emu and emus:
            return standalone_plan(game, next(iter(emus.values())))
        raise GameError("RetroArch isn't set up. Install it, or link the copy you already have, on the Emulators page.")
    options = cores_for(game.system, setup.cores, setup.infos)
    if not emu and not options and emus:  # RetroArch has nothing for it, but an emulator of its own does
        return standalone_plan(game, next(iter(emus.values())))
    if core and core not in setup.cores:
        raise GameError(f"The core {core} isn't installed in RetroArch any more. Pick another one for this game.")
    if not core:
        if not options:
            want = ", ".join(preferred_cores(game.system)[:2])
            raise GameError(f"RetroArch has no core installed for {game.system}."
                            + (f" Install one on the Emulators page (RetroArch > Cores): {want} is a good choice." if want else
                               " Install one on the Emulators page (RetroArch > Cores)."))
        core = options[0]
    slot = None
    if resume:
        states = find_save_states(setup, core, game.path)
        if not states:
            raise GameError(f"There is no save state for {game.name} to continue from. Save one in RetroArch (Quick Menu > Save State), or press Play.")
        slot = states[0].slot
    return LaunchPlan(retroarch_command(setup.exe, setup.cores[core], Path(game.path), state_slot=slot),
                      str(setup.exe.parent if setup.exe.suffix.lower() != ".app" else setup.exe), f"RetroArch · {setup.core_label(core)}")


def resume_state(game: Game, choice: str, setup: RetroSetup | None, default: str = "", emus: dict[str, Standalone] | None = None) -> SaveState | None:
    """The save state a Continue button would start from, or None when it can't: the game isn't played in RetroArch, or has none. Picks the core
    the way `plan_launch` does, so the two always agree."""
    emu, core = parse_choice(choice or default)
    if setup is None or emu not in ("", RETROARCH):
        return None
    options = cores_for(game.system, setup.cores, setup.infos)
    if not emu and not options and emus:
        return None  # an emulator of its own plays it
    core = core if core else (options[0] if options else "")
    if not core or core not in setup.cores:
        return None
    states = find_save_states(setup, core, game.path)
    return states[0] if states else None


def fmt_ago(epoch: float, now: float | None = None) -> str:
    """'just now', '5 min ago', '3 h ago', 'yesterday', '4 days ago', '3 weeks ago', '2 months ago', '2 years ago' ('' when never)."""
    if epoch <= 0:
        return ""
    seconds = max(0.0, (time.time() if now is None else now) - epoch)
    minutes, hours, days = int(seconds // 60), int(seconds // 3600), int(seconds // 86400)
    if minutes < 1:
        return "just now"
    if hours < 1:
        return f"{minutes} min ago"
    if days < 1:
        return f"{hours} h ago"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    if days < 730:
        return f"{max(2, round(days / 30.4))} months ago"
    return f"{days // 365} years ago"


# ---- the health of a platform's setup -------------------------------------------------------------------------------------
@dataclass(slots=True)
class PlatformHealth:
    """What the player has set up for one platform, and whether a game on it would start: the emulator that will play it, its launch options and
    the BIOS files its core asks for."""
    system: str
    games: int = 0
    default: str = ""  # the launch choice saved for the platform ('' = automatic)
    emulator: str = ""  # what will play it now: 'retroarch', a standalone emulator's id, or '' when nothing can
    core: str = ""  # the RetroArch core, when RetroArch plays it
    label: str = ""  # 'RetroArch · mGBA', 'DuckStation' ('' when nothing can)
    problems: list[tuple[str, str]] = field(default_factory=list)  # (short, long): "BIOS missing", "The core may need BIOS files that ..."
    firmware: list[Firmware] = field(default_factory=list)  # the required BIOS files that are not there

    @property
    def level(self) -> str:
        """'bad' when no game on the platform can start, 'warn' when one may not, 'ok' otherwise."""
        if not self.emulator:
            return "bad"
        return "warn" if self.problems else "ok"

    @property
    def summary(self) -> str:
        """One line for the health overview: 'PlayStation: BIOS missing'."""
        return f"{self.system}: " + (", ".join(short for short, _long in self.problems) if self.problems else "ready")


def platform_choices(system: str, setup: RetroSetup | None, emus: dict[str, Standalone]) -> list[tuple[str, str]]:
    """What a platform can be played with: (launch choice, label) for each installed RetroArch core and each installed emulator of its own."""
    cores = cores_for(system, setup.cores, setup.infos) if setup else []
    return [*((f"{RETROARCH}:{c}", f"RetroArch · {setup.core_label(c)}") for c in cores), *((rid, s.name) for rid, s in emus.items())]


def platform_health(system: str, games: int, default: str, setup: RetroSetup | None, emus: dict[str, Standalone]) -> PlatformHealth:
    """Work out, without starting anything, what would play a platform's games and what is missing for it. `emus` are the installed emulators
    of their own that play it (see `standalone_for`)."""
    out = PlatformHealth(system, games, default)
    choices = dict(platform_choices(system, setup, emus))
    cores = cores_for(system, setup.cores, setup.infos) if setup else []
    if not choices:
        want = ", ".join(preferred_cores(system)[:2])
        out.problems.append(("nothing installed", f"Nothing installed can play {system}. Install an emulator for it on the Emulators page"
                             + (f", or a RetroArch core ({want} is a good choice)." if want else ".")))
        return out
    dem, dcore = parse_choice(default)
    valid = bool(default) and ((dem == RETROARCH and (dcore in cores if dcore else bool(cores))) or (dem != RETROARCH and dem in emus))
    if default and not valid:
        out.problems.append(("default emulator missing", f"The default for {system} ({dcore or dem}) isn't installed any more, so another one is used."))
    if valid and dem != RETROARCH:
        out.emulator, out.label = dem, emus[dem].name
    elif cores:
        out.emulator = RETROARCH
        out.core = dcore if valid and dcore in cores else cores[0]
        out.label = f"RetroArch · {setup.core_label(out.core)}"
    else:
        out.emulator, out.label = next(iter(emus)), next(iter(emus.values())).name
    if out.emulator == RETROARCH and setup is not None:
        out.firmware = missing_firmware(setup, out.core)
        if out.firmware:
            names = ", ".join(dict.fromkeys(f.path for f in out.firmware[:6]))
            out.problems.append(("BIOS missing", f"{setup.core_label(out.core)} may need BIOS files that aren't in {setup.system_dir}: {names}."))
    return out


def health_overview(counts: Iterable[tuple[str, int]], default_for: Callable[[str], str], setup: RetroSetup | None,
                    emus_for: Callable[[str], dict[str, Standalone]]) -> list[PlatformHealth]:
    """Every platform in the library with its health, the ones that need attention first (then by name)."""
    rows = [platform_health(system, n, default_for(system), setup, emus_for(system)) for system, n in counts if system != UNKNOWN]
    order = {"bad": 0, "warn": 1, "ok": 2}
    return sorted(rows, key=lambda h: (order[h.level], h.system.casefold()))


def launch_options(emu: str, saved: str) -> tuple[str, str]:
    """(what an emulator is started with, Distill's own default for it): the player's words when they wrote some."""
    default = STANDALONE_ARGS.get(emu, "{rom}")
    return saved or default, default


def fmt_playtime(seconds: float) -> str:
    """'42 min', '3 h 05 min', '12 h'."""
    minutes = int(seconds) // 60
    if seconds < 60:
        return "under a minute"
    hours, minutes = divmod(minutes, 60)
    return f"{minutes} min" if not hours else (f"{hours} h {minutes:02d} min" if minutes else f"{hours} h")


MIN_SESSION = 10.0  # seconds: shorter than this is a launcher handing over to another program, not a game being played


def watch_session(proc, on_done: Callable[[float], None]) -> threading.Thread:
    """Wait (in the background) for the emulator to close, then report how long it ran, unless that was too short to be a game."""
    start = time.time()

    def run() -> None:
        try:
            proc.wait()
        except Exception:  # a process that can't be waited on just isn't timed
            return
        took = time.time() - start
        if took >= MIN_SESSION:
            on_done(took)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def spawn(plan: LaunchPlan) -> subprocess.Popen:
    """Start the emulator on its own, so closing Distill doesn't close the game."""
    flags = 0
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(plan.cmd, cwd=plan.cwd, creationflags=flags, close_fds=True, start_new_session=(sys.platform != "win32"), **quiet)
    except OSError as exc:
        raise GameError(f"Couldn't start it: {exc}") from exc


# ---- details and covers (LaunchBox Games Database) -------------------------------------------------------------------
LB_PLATFORM_NAMES = {
    "NES": "Nintendo Entertainment System", "Famicom Disk System": "Nintendo Famicom Disk System", "SNES": "Super Nintendo Entertainment System",
    "Game Boy": "Nintendo Game Boy", "Game Boy Color": "Nintendo Game Boy Color", "Game Boy Advance": "Nintendo Game Boy Advance",
    "Nintendo 64": "Nintendo 64", "Nintendo DS": "Nintendo DS", "Nintendo 3DS": "Nintendo 3DS", "GameCube": "Nintendo GameCube",
    "Wii": "Nintendo Wii", "Switch": "Nintendo Switch", "Virtual Boy": "Nintendo Virtual Boy", "Mega Drive": "Sega Genesis",
    "Master System": "Sega Master System", "Game Gear": "Sega Game Gear", "Sega CD": "Sega CD", "Sega Saturn": "Sega Saturn",
    "Dreamcast": "Sega Dreamcast", "PlayStation": "Sony Playstation", "PlayStation 2": "Sony Playstation 2", "PlayStation 3": "Sony Playstation 3",
    "PSP": "Sony PSP", "PC Engine": "NEC TurboGrafx-16", "PC Engine CD": "NEC TurboGrafx-CD", "Neo Geo Pocket": "SNK Neo Geo Pocket",
    "WonderSwan": "WonderSwan", "Atari 2600": "Atari 2600", "Atari 7800": "Atari 7800", "Atari Lynx": "Atari Lynx", "3DO": "3DO Interactive Multiplayer",
    "Xbox": "Microsoft Xbox", "Xbox 360": "Microsoft Xbox 360",
}


@dataclass(slots=True)
class GameShim:
    """The few fields the cover and overview lookups read from a game (they were written for PC recomps, which have more)."""
    id: str
    game: str
    system: str  # the LaunchBox name of the console
    launchbox_id: str = ""
    cover: str = ""
    cover_path: str = ""
    repo: str = ""


def shim(game: Game) -> GameShim:
    return GameShim(game.id, game.name, LB_PLATFORM_NAMES.get(game.system, "" if game.system == UNKNOWN else game.system))


def fetch_cover(game: Game, sgdb_key: str = "", retry: bool = False) -> Path | None:
    """The game's cached cover, or find one: the LaunchBox Games Database's box art (the wider selection), else SteamGridDB with the player's
    key. A cover the player chose stays."""
    target = shim(game)
    if game.cover_url:  # a game from a server brings its own cover, which beats looking one up (unless the player chose or removed one)
        have, src = rc.cached_cover(target), rc.cover_source(target)  # type: ignore[arg-type]
        if src == rc.REMOVED_SRC or (have is not None and src in (ROMM_COVER_SRC, rc.CUSTOM_SRC)):
            return have if src != rc.REMOVED_SRC else None
        if (got := _server_cover(game)) is not None:
            return got
    found = rc.fetch_cover(target, sgdb_key="", retry=retry)  # type: ignore[arg-type]
    if found is None and sgdb_key:
        found = rc.fetch_cover(target, sgdb_key=sgdb_key, retry=True)  # type: ignore[arg-type]
    return found


ROMM_COVER_SRC = "romm"  # the cached poster came from the player's RomM server


def _server_cover(game: Game) -> Path | None:
    """Download the cover a RomM server has for the game and keep it as the game's poster; None when it has none or can't be reached."""
    from . import apikeys, romm  # noqa: PLC0415
    try:
        data = romm.Client(apikeys.get("romm_url"), apikeys.get("romm")).cover(game.cover_url)
        return rc.set_cover(game.id, data, ROMM_COVER_SRC) if data else None
    except (romm.RommError, rc.RecompError):
        return None


def forget_cover(game: Game) -> bool:
    """Throw away a cover Distill found by itself so that one is looked for again (after the game's name or platform changed). A cover the
    player picked, and the choice to have none, are kept. True when something was thrown away."""
    target = shim(game)
    if rc.cover_source(target) in (rc.CUSTOM_SRC, rc.REMOVED_SRC):  # type: ignore[arg-type]
        return False
    gone = False
    for f in rc.art_dir().glob(f"{game.id}.*"):
        f.unlink(missing_ok=True)
        gone = True
    return gone


def cover_path(game: Game) -> Path | None:
    return rc.cached_cover(shim(game))  # type: ignore[arg-type]


@dataclass(slots=True)
class Details:
    overview: str = ""
    developers: str = ""
    publishers: str = ""
    genres: str = ""
    released: str = ""

    def empty(self) -> bool:
        return not any((self.overview, self.developers, self.publishers, self.genres, self.released))

    def as_dict(self) -> dict:
        return {"overview": self.overview, "developers": self.developers, "publishers": self.publishers, "genres": self.genres, "released": self.released}


_BLOCKS = re.compile(r"(?i)</?(?:p|br|li|ul|ol|div|section|h[1-6]|tr|td|th|dt|dd|table|tbody|span)\b[^>]*>")
_LABELS = {"developers": "developers", "developer": "developers", "publishers": "publishers", "publisher": "publishers", "genres": "genres",
           "genre": "genres", "release date": "released", "released": "released", "first release": "released", "release year": "released"}
_STOPS = {"platform", "alternate names", "also known as", "media", "video", "wikipedia", "advertisement", "overview", "images", "edit"}


def _page_lines(page: str) -> list[str]:
    page = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", _BLOCKS.sub("\n", page)))
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def parse_launchbox_details(page: str) -> Details:
    """What a LaunchBox game page says about the game: the Overview, who made and published it, its genres and when it came out.
    A label (Developers, Genres...) is a line of its own with its values on the lines after it, up to the next label."""
    out = Details(overview=rc._lb_overview(page))
    lines = _page_lines(page)
    found: dict[str, list[str]] = {}
    i = 0
    while i < len(lines):
        key = _LABELS.get(lines[i].rstrip(":").strip().lower())
        if key and key not in found:
            values = []
            j = i + 1
            while j < len(lines) and len(values) < 8:
                low = lines[j].rstrip(":").strip().lower()
                is_stop = low in _STOPS or low.startswith("alternate names")
                if low in _LABELS or (is_stop and not (low == "platform" and key == "genres" and not values)):  # 'Platform' is also a genre
                    break
                values.append(lines[j])
                j += 1
            if values:
                found[key] = values
            i = j
            continue
        i += 1
    out.developers = ", ".join(found.get("developers", []))
    out.publishers = ", ".join(found.get("publishers", []))
    out.genres = ", ".join(v for v in found.get("genres", []))
    out.released = found.get("released", [""])[0]
    return out


def launchbox_details(game: Game, store: rc.Store | None = None, force: bool = False) -> Details:
    """The LaunchBox Games Database entry for a game: found by searching its title and console, read from its page. Cached for a week,
    a miss too when the site answered. Best effort: when the page layout changes this comes back empty, and the page shows what the scan knows."""
    key = f"lbg1:{game.id}"
    if store and not force and (hit := store.cache_get(key, rc.OVERVIEW_TTL)) is not None and isinstance(hit, dict):
        return Details(**{k: str(hit.get(k, "")) for k in Details.__slots__})
    s = shim(game)
    result, reached = Details(), False
    for title in rc._title_fallbacks(s.game):
        for q in rc._search_queries(title):
            try:
                page = rc._get(rc._LB_SEARCH + urllib.parse.quote(q), timeout=15).decode("utf-8", "replace")
                reached = True
            except (urllib.error.URLError, OSError, ValueError):
                continue
            ids = rc._lb_game_ids(page, title, s.system)
            for gid in ids[:3]:
                try:
                    detail = rc._get(rc._LB_DETAIL + gid, timeout=15).decode("utf-8", "replace")
                except (urllib.error.URLError, OSError, ValueError):
                    continue
                parsed = parse_launchbox_details(detail)
                if not parsed.empty():
                    result = parsed
                    break
            break  # one query that found games is enough
        if not result.empty():
            break
    if store and (not result.empty() or reached):
        store.cache_put(key, result.as_dict())
    if result.empty() and store and isinstance(stale := store.cache_stale(key), dict):
        return Details(**{k: str(stale.get(k, "")) for k in Details.__slots__})
    return result


# ---- looking a game up by name (the edit window) ---------------------------------------------------------------------
_LB_TO_SYSTEM = {name.casefold(): system for system, name in LB_PLATFORM_NAMES.items()}
_LB_TO_SYSTEM.update({"sega mega drive": "Mega Drive", "sega genesis": "Mega Drive", "nintendo entertainment system": "NES", "nec turbografx-16": "PC Engine",
                      "nintendo super nintendo entertainment system": "SNES", "sony playstation": "PlayStation", "sony psp": "PSP"})
_YEAR = re.compile(r"\b(19[6-9]\d|20[0-4]\d)\b")


def system_from_launchbox(platform: str) -> str:
    """Distill's name for a LaunchBox platform ('Sega Genesis' -> 'Mega Drive'); a platform Distill has no name for is kept as LaunchBox spells it."""
    platform = (platform or "").strip()
    return _LB_TO_SYSTEM.get(platform.casefold(), platform)


def system_choices() -> list[str]:
    """The platforms offered in the edit window, alphabetical."""
    return sorted({*sysmod.SYSTEM_NAMES, *LB_PLATFORM_NAMES, "Switch", "PC"}, key=str.casefold)


@dataclass(slots=True)
class Match:
    """One LaunchBox Games Database entry a name could mean."""
    title: str = ""
    system: str = ""  # Distill's name for the platform
    developer: str = ""
    publisher: str = ""
    year: str = ""
    genre: str = ""
    overview: str = ""
    lb_id: str = ""

    @property
    def label(self) -> str:
        return self.title + (f"  ·  {self.system}" if self.system else "") + (f"  ·  {self.year}" if self.year else "")


def _page_title(page: str) -> str:
    for rx in (r"(?is)<h1[^>]*>(.*?)</h1>", r'(?is)<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', r"(?is)<title[^>]*>(.*?)</title>"):
        if m := re.search(rx, page):
            text = html_lib.unescape(re.sub(r"<[^>]+>", "", m[1])).strip()
            text = re.sub(r"(?i)\s*[-|]\s*launchbox.*$", "", text).strip()
            if text:
                return text
    return ""


def parse_launchbox_match(page: str, lb_id: str = "", slug: str = "") -> Match:
    """A game's LaunchBox page as a Match: its name, platform, who made and published it, genres and year. Best effort, like the page parser."""
    d = parse_launchbox_details(page)
    platform = ""
    lines = _page_lines(page)
    for i, line in enumerate(lines[:-1]):  # the last one: 'Platform' can also be a genre, which comes earlier on the page
        if line.rstrip(":").strip().lower() == "platform":
            platform = lines[i + 1]
    title = _page_title(page) or slug.replace("-", " ").title()
    year = (m[1] if (m := _YEAR.search(d.released)) else "")
    return Match(title, system_from_launchbox(platform), d.developers, d.publishers, year, d.genres, d.overview, lb_id)


def lb_candidates(title: str, system: str = "") -> tuple[list[str], dict[str, str]]:
    """LaunchBox Games Database ids a title could mean (and each one's address name), best first. Every spelling of the title is searched
    (as written, letters only, roman numerals), and so is the title with its platform added, and the answers are put together: a game
    that is only listed as 'Resident Evil 2 - Dual Shock Ver.' still turns up for 'Resident Evil 2' on the platform it is on, instead of
    being pushed out by the many other games with the plain name. Raises GameError when LaunchBox can't be reached."""
    want = LB_PLATFORM_NAMES.get(system, system if system != UNKNOWN else "")
    plain = rc._search_queries(title)
    named = [f"{title} {want}", f"{title} {system}"] if want else []  # the same title with its platform added
    slugs: dict[str, str] = {}
    first: list[list[str]] = []  # answers to the searches that named the platform: their best results are the ones on it
    rest: list[list[str]] = []
    reached = False
    for q, is_named in [*((q, True) for q in dict.fromkeys(named)), *((q, False) for q in plain)]:
        if not q.strip():
            continue
        try:
            page = rc._get(rc._LB_SEARCH + urllib.parse.quote(q), timeout=15).decode("utf-8", "replace")
            reached = True
        except (urllib.error.URLError, OSError, ValueError):
            continue
        for m in rc._LB_LINK.finditer(page):
            slugs.setdefault(m[1], m[2] or "")
        (first if is_named else rest).append(rc._lb_game_ids(page, title, want or "\0"))
    if not reached:
        raise GameError("Couldn't reach the LaunchBox Games Database.")
    ids: list[str] = []
    lists = [*first, *rest]
    for rank in range(max((len(lst) for lst in lists), default=0)):  # each search's best answer, then each one's second best...
        for lst in lists:
            if rank < len(lst) and lst[rank] not in ids:
                ids.append(lst[rank])
    return ids, slugs


def launchbox_search(title: str, system: str = "", limit: int = 16) -> list[Match]:
    """The LaunchBox entries a game name could mean, the ones on `system` first. Raises GameError when LaunchBox can't be reached, so the
    window can say so instead of claiming nothing was found."""
    from concurrent.futures import ThreadPoolExecutor
    title = title.strip()
    if not title:
        return []
    ids, slugs = lb_candidates(title, system)

    def one(gid: str) -> Match | None:
        try:
            page = rc._get(rc._LB_DETAIL + gid, timeout=15).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return parse_launchbox_match(page, gid, slugs.get(gid, ""))

    with ThreadPoolExecutor(max_workers=4) as pool:
        found = [m for m in pool.map(one, ids[:limit]) if m is not None and m.title]
    found.sort(key=lambda m: m.system.casefold() != system.casefold())  # stable: LaunchBox's own order within each group
    return found


def launchbox_cover_urls(title: str, system: str = "", limit: int = 30) -> list[str]:
    """Box art for a game from the LaunchBox Games Database, a wider choice than one poster: the front covers (posters, box fronts, flyers) of
    the entries the title could mean, the ones on `system` first. Raises GameError when LaunchBox can't be reached."""
    ids, _slugs = lb_candidates(title.strip(), system)
    urls: list[str] = []
    for gid in ids[:5]:
        try:
            page = rc._get(rc._LB_IMAGES + gid, timeout=15).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        urls += [u for u in rc._lb_posters(page) if u not in urls]
    return urls[:limit]


# ---- handing games to Distill's other tools ----------------------------------------------------------------------------
def tool_files(games: Iterable[Game]) -> list[Path]:
    """The files to give the conversion tools for these games. A multi-disc game's .m3u playlist stands for the discs it lists."""
    out: list[Path] = []
    for g in games:
        path = Path(g.path)
        for f in (playlist_discs(path) if path.suffix.lower() == ".m3u" else []) or [path]:
            if f not in out:
                out.append(f)
    return out


def common_folder(games: Iterable[Game]) -> Path | None:
    """The folder that holds all these games (the one they share, or the closest above them); None when they are on different drives."""
    parents = [str(Path(g.path).parent) for g in games]
    if not parents:
        return None
    try:
        return Path(os.path.commonpath(parents))
    except ValueError:  # different drives
        return None


def soundtrack_names(games: Iterable[Game]) -> list[str]:
    """One name per game for the Soundtrack Grabber's box: versions and discs of the same game count once."""
    seen: set[str] = set()
    out: list[str] = []
    for g in sorted(games, key=lambda x: (x.name.casefold(), x.region)):
        key = version_key(g)[0]
        if key and key not in seen:
            seen.add(key)
            out.append(g.name)
    return out


def best_match(game: Game, search=None) -> Match | None:
    """The LaunchBox entry that is this game, or None. A game already on a platform only matches an entry for that platform, so it is
    never moved to a different one by a lookup; an unplaced game takes the first entry."""
    system = "" if game.system == UNKNOWN else game.system
    found = (search or launchbox_search)(game.name, system)
    if system:
        found = [m for m in found if m.system.casefold() == system.casefold()]
    return found[0] if found else None


def fill_from_match(store: GameStore, game: Game, m: Match) -> bool:
    """Put what a match knows into the details the game doesn't have yet (nothing the player already wrote is replaced); an unplaced game
    also gets the match's platform. True when anything was filled in."""
    changes = {f: getattr(m, f) for f in ("developer", "publisher", "year", "genre") if m and getattr(m, f) and not getattr(game, f)}
    if game.system == UNKNOWN and m.system:
        changes["system"] = m.system
    if changes:
        store.update_details(game.id, **changes)
    return bool(changes)
