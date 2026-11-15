"""The Media page's local data: the library of albums saved for streaming, playlists, favourites, play history, and two
disk budgets the person sets (saved track lists, and an optional cache of streamed audio).

One SQLite file. Nothing here touches the network or Qt. A track is stored as (source, album ident, position), and an
album's track list is a *snapshot*: it lets the library open offline, and it is the first thing dropped when the
snapshot budget is full (the album stays in the library and is listed again the next time it is opened).
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import re
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .soundtracks import Album, LOCAL_SOURCE, split_detail, Track  # noqa: F401  (split_detail is re-exported)

DB_FILE = paths.data_dir() / "distill_media.db"
SCHEMA_VERSION = 1

MB = 1024 * 1024
DEFAULT_SNAPSHOT_MB = 50  # saved track lists: ~2 KB each, so this is thousands of albums
DEFAULT_AUDIO_CACHE_MB = 0  # off until the person gives it room
SORTS = {
    "added": "l.added_at", "title": "a.title COLLATE NOCASE", "played": "l.last_played_at",
    "platform": "a.platform COLLATE NOCASE", "year": "a.year", "plays": "l.play_count",
}
DESCENDING = frozenset({"added", "played", "plays"})  # newest / most first unless asked otherwise

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, ident TEXT NOT NULL, title TEXT NOT NULL, detail TEXT DEFAULT '',
    platform TEXT DEFAULT '', year INTEGER DEFAULT 0, art TEXT DEFAULT '', track_count INTEGER DEFAULT 0,
    UNIQUE (source, ident));
CREATE TABLE IF NOT EXISTS snapshots (
    album INTEGER PRIMARY KEY REFERENCES albums(id) ON DELETE CASCADE, body TEXT NOT NULL, size INTEGER NOT NULL,
    saved_at REAL NOT NULL, used_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS library (
    album INTEGER PRIMARY KEY REFERENCES albums(id) ON DELETE CASCADE, added_at REAL NOT NULL,
    last_played_at REAL DEFAULT 0, play_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS favourites (
    album INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE, position INTEGER NOT NULL, liked_at REAL NOT NULL,
    PRIMARY KEY (album, position));
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS playlist_items (
    id INTEGER PRIMARY KEY, playlist INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL, album INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE, position INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS playlist_order ON playlist_items (playlist, ord);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY, album INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE, position INTEGER NOT NULL,
    played_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS history_time ON history (played_at);
CREATE TABLE IF NOT EXISTS game_albums (
    game TEXT PRIMARY KEY, album INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE, chosen_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS web_cache (
    key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audio_cache (
    key TEXT PRIMARY KEY, file TEXT NOT NULL, size INTEGER NOT NULL, used_at REAL NOT NULL);
"""


@dataclass(slots=True)
class LibraryEntry:
    id: int
    source: str
    ident: str
    title: str
    detail: str
    platform: str
    year: int
    art: str
    track_count: int
    added_at: float
    last_played_at: float
    play_count: int
    has_snapshot: bool

    def album(self) -> Album:
        return Album(self.source, self.ident, self.title, self.detail, 1.0, self.art)


class MediaStore:
    def __init__(self, path: Path | str | None = None):
        self.path = str(path or DB_FILE)
        with contextlib.suppress(OSError):  # (a folder that can't be made shows as SQLite's own error just below)
            paths.ensure_parent(Path(self.path))
        self._lock = threading.RLock()  # one connection shared by the page and its worker threads
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.executescript(_SCHEMA)
            if self._get("schema") is None:
                self._set("schema", str(SCHEMA_VERSION))
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ---- settings (the budgets live here) -----------------------------------------------------
    def _get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value))

    def setting(self, key: str, default: str = "") -> str:
        with self._lock:
            value = self._get("s:" + key)
        return default if value is None else value

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._set("s:" + key, str(value))
            self.db.commit()

    def snapshot_limit(self) -> int:
        """Bytes the saved track lists may take (0 keeps none: the library still lists albums, each opened online)."""
        return max(0, int(float(self.setting("snapshot_limit_mb", str(DEFAULT_SNAPSHOT_MB))) * MB))

    def audio_cache_limit(self) -> int:
        """Bytes the cache of streamed audio may take; 0 means the cache is off."""
        return max(0, int(float(self.setting("audio_cache_limit_mb", str(DEFAULT_AUDIO_CACHE_MB))) * MB))

    def cover_cache_limit(self) -> int:
        """Bytes the folder of kept covers may take (0 keeps none)."""
        return max(0, int(float(self.setting("cover_cache_mb", "100")) * MB))

    def set_cover_cache_limit_mb(self, mb: float) -> None:
        self.set_setting("cover_cache_mb", str(max(0.0, mb)))

    def cover_dir(self) -> Path:
        return Path(self.path).with_name("distill_covers")

    def set_snapshot_limit_mb(self, mb: float) -> int:
        self.set_setting("snapshot_limit_mb", str(max(0.0, mb)))
        return self.trim_snapshots()

    def set_audio_cache_limit_mb(self, mb: float) -> list[str]:
        self.set_setting("audio_cache_limit_mb", str(max(0.0, mb)))
        return self.trim_audio_cache()

    # ---- albums -------------------------------------------------------------------------------
    def upsert_album(self, album: Album, track_count: int | None = None, update: bool = True) -> int:
        """Remember an album. `update=False` only adds a missing one: what is already known about it is kept (a
        play, a heart or a playlist entry may carry less detail than the library does)."""
        platform, year = split_detail(album.detail)
        with self._lock:
            row = self.db.execute("SELECT id, track_count FROM albums WHERE source = ? AND ident = ?",
                                  (album.provider, album.ident)).fetchone()
            count = track_count if track_count is not None else (row["track_count"] if row else 0)
            if row and not update:
                album_id = row["id"]
            elif row:
                self.db.execute("UPDATE albums SET title = ?, detail = ?, platform = ?, year = ?, art = ?, track_count = ? "
                                "WHERE id = ?", (album.title, album.detail, platform, year, album.art, count, row["id"]))
                album_id = row["id"]
            else:
                album_id = self.db.execute(
                    "INSERT INTO albums (source, ident, title, detail, platform, year, art, track_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (album.provider, album.ident, album.title, album.detail, platform, year, album.art, count)).lastrowid
            self.db.commit()
        return album_id

    def album_id(self, source: str, ident: str) -> int | None:
        with self._lock:
            row = self.db.execute("SELECT id FROM albums WHERE source = ? AND ident = ?", (source, ident)).fetchone()
        return row["id"] if row else None

    def album_by_id(self, album_id: int) -> Album | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
        return Album(row["source"], row["ident"], row["title"], row["detail"], 1.0, row["art"]) if row else None

    def _prune_album(self, album_id: int) -> None:
        """Forget an album row nothing refers to any more."""
        used = self.db.execute(
            "SELECT 1 FROM library WHERE album = ? UNION SELECT 1 FROM playlist_items WHERE album = ? "
            "UNION SELECT 1 FROM favourites WHERE album = ? UNION SELECT 1 FROM history WHERE album = ? "
            "UNION SELECT 1 FROM game_albums WHERE album = ?",
            (album_id,) * 5).fetchone()
        if not used:
            self.db.execute("DELETE FROM albums WHERE id = ?", (album_id,))

    # ---- library ------------------------------------------------------------------------------
    def add_to_library(self, album: Album, tracks: list[Track] | None = None) -> bool:
        """Save an album for streaming. True when it was new to the library."""
        album_id = self.upsert_album(album, len(tracks) if tracks else None)
        with self._lock:
            cur = self.db.execute("INSERT OR IGNORE INTO library (album, added_at) VALUES (?, ?)", (album_id, time.time()))
            self.db.commit()
            new = cur.rowcount > 0
        if tracks:
            self.save_snapshot(album, tracks)
        return new

    def remove_from_library(self, album: Album) -> None:
        album_id = self.album_id(album.provider, album.ident)
        if album_id is None:
            return
        with self._lock:
            self.db.execute("DELETE FROM library WHERE album = ?", (album_id,))
            self.db.execute("DELETE FROM snapshots WHERE album = ? AND NOT EXISTS "
                            "(SELECT 1 FROM playlist_items WHERE album = ?)", (album_id, album_id))
            self._prune_album(album_id)
            self.db.commit()

    def in_library(self, album: Album) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM library l JOIN albums a ON a.id = l.album WHERE a.source = ? AND a.ident = ?",
                                   (album.provider, album.ident)).fetchone() is not None

    def library(self, sort: str = "added", descending: bool | None = None, text: str = "",
                platform: str = "") -> list[LibraryEntry]:
        column = SORTS.get(sort, SORTS["added"])
        desc = (sort in DESCENDING) if descending is None else descending
        where, args = [], []
        if text.strip():
            where.append("(a.title LIKE ? OR a.platform LIKE ?)")
            args += [f"%{text.strip()}%"] * 2
        if platform:
            where.append("a.platform = ? COLLATE NOCASE")
            args.append(platform)
        sql = ("SELECT a.*, l.added_at, l.last_played_at, l.play_count, "
               "EXISTS(SELECT 1 FROM snapshots s WHERE s.album = a.id) AS snap FROM library l JOIN albums a ON a.id = l.album"
               + (" WHERE " + " AND ".join(where) if where else "")
               + f" ORDER BY {column} {'DESC' if desc else 'ASC'}, a.title COLLATE NOCASE")
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [LibraryEntry(r["id"], r["source"], r["ident"], r["title"], r["detail"], r["platform"], r["year"], r["art"],
                             r["track_count"], r["added_at"], r["last_played_at"], r["play_count"], bool(r["snap"]))
                for r in rows]

    def library_platforms(self) -> list[str]:
        with self._lock:
            rows = self.db.execute("SELECT DISTINCT a.platform FROM library l JOIN albums a ON a.id = l.album "
                                   "WHERE a.platform <> '' ORDER BY a.platform COLLATE NOCASE").fetchall()
        return [r["platform"] for r in rows]

    # ---- snapshots (an album's track list, kept so it opens offline) --------------------------
    def save_snapshot(self, album: Album, tracks: list[Track]) -> bool:
        """Keep the track list. False when the snapshot budget cannot hold it (the album itself is still kept)."""
        body = json.dumps([dataclasses.asdict(t) for t in tracks], separators=(",", ":"))
        size = len(body.encode("utf-8"))
        limit = self.snapshot_limit()
        if size > limit:
            return False
        album_id = self.upsert_album(album, len(tracks))
        now = time.time()
        with self._lock:
            self.db.execute("INSERT INTO snapshots (album, body, size, saved_at, used_at) VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(album) DO UPDATE SET body = excluded.body, size = excluded.size, "
                            "saved_at = excluded.saved_at, used_at = excluded.used_at", (album_id, body, size, now, now))
            self.db.commit()
        self.trim_snapshots(keep=album_id)
        return True

    def load_snapshot(self, album: Album, max_age_days: float | None = None) -> list[Track] | None:
        """The saved track list, or None when there is none (or it is older than `max_age_days`)."""
        album_id = self.album_id(album.provider, album.ident)
        if album_id is None:
            return None
        with self._lock:
            row = self.db.execute("SELECT body, saved_at FROM snapshots WHERE album = ?", (album_id,)).fetchone()
            if row is None or (max_age_days is not None and time.time() - row["saved_at"] > max_age_days * 86400):
                return None
            self.db.execute("UPDATE snapshots SET used_at = ? WHERE album = ?", (time.time(), album_id))
            self.db.commit()
        try:
            return [Track(**d) for d in json.loads(row["body"])]
        except (ValueError, TypeError):
            return None

    def snapshot_bytes(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COALESCE(SUM(size), 0) AS n FROM snapshots").fetchone()["n"]

    def trim_snapshots(self, keep: int | None = None) -> int:
        """Drop the least recently used snapshots until the budget holds. Returns how many were dropped."""
        limit, dropped = self.snapshot_limit(), 0
        with self._lock:
            total = self.snapshot_bytes()
            for row in self.db.execute("SELECT album, size FROM snapshots ORDER BY used_at ASC").fetchall():
                if total <= limit:
                    break
                if row["album"] == keep:
                    continue
                self.db.execute("DELETE FROM snapshots WHERE album = ?", (row["album"],))
                total -= row["size"]
                dropped += 1
            self.db.commit()
        return dropped

    # ---- the album chosen for a game, and albums found on disk --------------------------------
    def set_game_album(self, game: str, album: Album) -> None:
        """Remember which album is this game's soundtrack (`game` is the Games library's id for it)."""
        album_id = self.upsert_album(album, update=False)
        with self._lock:
            old = self.db.execute("SELECT album FROM game_albums WHERE game = ?", (game,)).fetchone()
            self.db.execute("INSERT INTO game_albums (game, album, chosen_at) VALUES (?, ?, ?) "
                            "ON CONFLICT(game) DO UPDATE SET album = excluded.album, chosen_at = excluded.chosen_at",
                            (game, album_id, time.time()))
            if old and old["album"] != album_id:
                self._prune_album(old["album"])
            self.db.commit()

    def game_album(self, game: str) -> Album | None:
        with self._lock:
            row = self.db.execute("SELECT a.* FROM game_albums g JOIN albums a ON a.id = g.album WHERE g.game = ?", (game,)).fetchone()
        return Album(row["source"], row["ident"], row["title"], row["detail"], 1.0, row["art"]) if row else None

    def clear_game_album(self, game: str) -> None:
        with self._lock:
            row = self.db.execute("SELECT album FROM game_albums WHERE game = ?", (game,)).fetchone()
            self.db.execute("DELETE FROM game_albums WHERE game = ?", (game,))
            if row:
                self._prune_album(row["album"])
            self.db.commit()

    def sync_local(self, found: list[tuple[Album, int]]) -> tuple[int, int]:
        """Make the library's disk albums match what a scan found: new ones are added, ones whose folder is gone are
        dropped. `found` is (album, track count). Returns (added, removed)."""
        keep = {a.ident for a, _n in found}
        added = removed = 0
        for album, count in found:
            if self.album_id(LOCAL_SOURCE, album.ident) is None or not self.in_library(album):
                added += 1
            self.add_to_library(album)
            self.upsert_album(album, count)
        with self._lock:
            gone = [r for r in self.db.execute(
                "SELECT a.id, a.ident FROM library l JOIN albums a ON a.id = l.album WHERE a.source = ?", (LOCAL_SOURCE,))
                    if r["ident"] not in keep]
            for r in gone:
                self.db.execute("DELETE FROM library WHERE album = ?", (r["id"],))
                self._prune_album(r["id"])
            self.db.commit()
        return added, len(gone)

    # ---- favourites ---------------------------------------------------------------------------
    def set_favourite(self, album: Album, position: int, on: bool = True) -> None:
        album_id = self.upsert_album(album, update=False)
        with self._lock:
            if on:
                self.db.execute("INSERT OR IGNORE INTO favourites (album, position, liked_at) VALUES (?, ?, ?)",
                                (album_id, position, time.time()))
            else:
                self.db.execute("DELETE FROM favourites WHERE album = ? AND position = ?", (album_id, position))
                self._prune_album(album_id)
            self.db.commit()

    def is_favourite(self, album: Album, position: int) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM favourites f JOIN albums a ON a.id = f.album WHERE a.source = ? AND "
                                   "a.ident = ? AND f.position = ?", (album.provider, album.ident, position)).fetchone() is not None

    def favourite_positions(self, album: Album) -> set[int]:
        """Which tracks of this album are liked."""
        with self._lock:
            rows = self.db.execute("SELECT f.position FROM favourites f JOIN albums a ON a.id = f.album WHERE a.source = ? "
                                   "AND a.ident = ?", (album.provider, album.ident)).fetchall()
        return {r["position"] for r in rows}

    def favourites(self) -> list[tuple[Album, int]]:
        """Liked tracks, newest first, as (album, position)."""
        with self._lock:
            rows = self.db.execute("SELECT a.*, f.position FROM favourites f JOIN albums a ON a.id = f.album "
                                   "ORDER BY f.liked_at DESC").fetchall()
        return [(Album(r["source"], r["ident"], r["title"], r["detail"], 1.0, r["art"]), r["position"]) for r in rows]

    # ---- playlists ----------------------------------------------------------------------------
    def create_playlist(self, name: str) -> int:
        now = time.time()
        with self._lock:
            pid = self.db.execute("INSERT INTO playlists (name, created_at, updated_at) VALUES (?, ?, ?)",
                                  (name.strip() or "Untitled", now, now)).lastrowid
            self.db.commit()
        return pid

    def rename_playlist(self, pid: int, name: str) -> None:
        with self._lock:
            self.db.execute("UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?", (name.strip() or "Untitled", time.time(), pid))
            self.db.commit()

    def delete_playlist(self, pid: int) -> None:
        with self._lock:
            albums = [r["album"] for r in self.db.execute("SELECT DISTINCT album FROM playlist_items WHERE playlist = ?", (pid,))]
            self.db.execute("DELETE FROM playlists WHERE id = ?", (pid,))
            for album_id in albums:
                self._prune_album(album_id)
            self.db.commit()

    def playlist_name(self, pid: int) -> str:
        with self._lock:
            row = self.db.execute("SELECT name FROM playlists WHERE id = ?", (pid,)).fetchone()
        return row["name"] if row else ""

    def playlists(self) -> list[tuple[int, str, int]]:
        """(id, name, track count), most recently changed first."""
        with self._lock:
            rows = self.db.execute("SELECT p.id, p.name, (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist = p.id) AS n "
                                   "FROM playlists p ORDER BY p.updated_at DESC, p.id DESC").fetchall()
        return [(r["id"], r["name"], r["n"]) for r in rows]

    def add_to_playlist(self, pid: int, album: Album, positions: list[int]) -> None:
        album_id = self.upsert_album(album, update=False)
        with self._lock:
            end = self.db.execute("SELECT COALESCE(MAX(ord), -1) AS m FROM playlist_items WHERE playlist = ?", (pid,)).fetchone()["m"]
            for offset, position in enumerate(positions, 1):
                self.db.execute("INSERT INTO playlist_items (playlist, ord, album, position) VALUES (?, ?, ?, ?)",
                                (pid, end + offset, album_id, position))
            self.db.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (time.time(), pid))
            self.db.commit()

    def playlist_items(self, pid: int) -> list[tuple[int, Album, int]]:
        """(item id, album, position) in playlist order."""
        with self._lock:
            rows = self.db.execute("SELECT i.id AS item, i.position, a.* FROM playlist_items i JOIN albums a ON a.id = i.album "
                                   "WHERE i.playlist = ? ORDER BY i.ord", (pid,)).fetchall()
        return [(r["item"], Album(r["source"], r["ident"], r["title"], r["detail"], 1.0, r["art"]), r["position"]) for r in rows]

    def remove_from_playlist(self, pid: int, item: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM playlist_items WHERE id = ? AND playlist = ?", (item, pid))
            self._renumber(pid)
            self.db.commit()

    def move_in_playlist(self, pid: int, item: int, to_index: int) -> None:
        with self._lock:
            ids = [r["id"] for r in self.db.execute("SELECT id FROM playlist_items WHERE playlist = ? ORDER BY ord", (pid,))]
            if item not in ids:
                return
            ids.remove(item)
            ids.insert(max(0, min(to_index, len(ids))), item)
            for order, i in enumerate(ids):
                self.db.execute("UPDATE playlist_items SET ord = ? WHERE id = ?", (order, i))
            self.db.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (time.time(), pid))
            self.db.commit()

    def _renumber(self, pid: int) -> None:
        for order, r in enumerate(self.db.execute("SELECT id FROM playlist_items WHERE playlist = ? ORDER BY ord", (pid,)).fetchall()):
            self.db.execute("UPDATE playlist_items SET ord = ? WHERE id = ?", (order, r["id"]))

    # ---- history ------------------------------------------------------------------------------
    def record_play(self, album: Album, position: int, when: float | None = None) -> None:
        album_id = self.upsert_album(album, update=False)
        now = when if when is not None else time.time()
        with self._lock:
            self.db.execute("INSERT INTO history (album, position, played_at) VALUES (?, ?, ?)", (album_id, position, now))
            self.db.execute("UPDATE library SET play_count = play_count + 1, last_played_at = ? WHERE album = ?", (now, album_id))
            self.db.commit()

    def recent_albums(self, limit: int = 20) -> list[Album]:
        """Albums played lately, each once, most recent first."""
        with self._lock:
            rows = self.db.execute("SELECT a.*, MAX(h.played_at) AS last FROM history h JOIN albums a ON a.id = h.album "
                                   "GROUP BY a.id ORDER BY last DESC LIMIT ?", (limit,)).fetchall()
        return [Album(r["source"], r["ident"], r["title"], r["detail"], 1.0, r["art"]) for r in rows]

    def clear_history(self) -> None:
        with self._lock:
            albums = [r["album"] for r in self.db.execute("SELECT DISTINCT album FROM history")]
            self.db.execute("DELETE FROM history")
            self.db.execute("UPDATE library SET play_count = 0, last_played_at = 0")
            for album_id in albums:
                self._prune_album(album_id)
            self.db.commit()

    # ---- web lists (the Home rows), kept for a few hours so the page opens at once ------------
    def web_get(self, key: str, max_age: float) -> object | None:
        """What `web_put` kept under this key, if it is younger than `max_age` seconds."""
        with self._lock:
            row = self.db.execute("SELECT body, fetched_at FROM web_cache WHERE key = ?", (key,)).fetchone()
        if row is None or time.time() - row["fetched_at"] > max_age:
            return None
        try:
            return json.loads(row["body"])
        except ValueError:
            return None

    def web_put(self, key: str, value: object) -> None:
        with self._lock:
            self.db.execute("INSERT INTO web_cache (key, body, fetched_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                            "body = excluded.body, fetched_at = excluded.fetched_at",
                            (key, json.dumps(value, separators=(",", ":")), time.time()))
            self.db.commit()

    def web_clear(self) -> None:
        with self._lock:
            self.db.execute("DELETE FROM web_cache")
            self.db.commit()

    # ---- the audio cache (files in a folder, indexed here, trimmed to the budget) -------------
    def cache_dir(self) -> Path:
        return Path(self.setting("audio_cache_dir", "")) if self.setting("audio_cache_dir") else Path(self.path).with_name("distill_audio_cache")

    def cache_path(self, key: str) -> Path | None:
        """The cached file for this track, or None. (A hit counts as a use.)"""
        with self._lock:
            row = self.db.execute("SELECT file FROM audio_cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            path = self.cache_dir() / row["file"]
            if not path.is_file():
                self.db.execute("DELETE FROM audio_cache WHERE key = ?", (key,))
                self.db.commit()
                return None
            self.db.execute("UPDATE audio_cache SET used_at = ? WHERE key = ?", (time.time(), key))
            self.db.commit()
        return path

    def cache_put(self, key: str, data: bytes, ext: str = ".mp3") -> Path | None:
        """Keep a streamed track. None when the cache is off or the file is bigger than the whole budget."""
        limit = self.audio_cache_limit()
        if limit <= 0 or len(data) > limit:
            return None
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:80] + f"_{zlib.crc32(key.encode('utf-8'))}" + (ext if ext.startswith(".") else "." + ext)
        folder = self.cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        with self._lock:
            self.db.execute("INSERT INTO audio_cache (key, file, size, used_at) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET file = excluded.file, size = excluded.size, used_at = excluded.used_at",
                            (key, name, len(data), time.time()))
            self.db.commit()
        self.trim_audio_cache(keep=key)
        return path

    def audio_cache_bytes(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COALESCE(SUM(size), 0) AS n FROM audio_cache").fetchone()["n"]

    def trim_audio_cache(self, keep: str | None = None) -> list[str]:
        """Delete the least recently used cached files until the budget holds. Returns the keys removed."""
        limit, removed = self.audio_cache_limit(), []
        with self._lock:
            total = self.audio_cache_bytes()
            for row in self.db.execute("SELECT key, file, size FROM audio_cache ORDER BY used_at ASC").fetchall():
                if total <= limit and limit > 0:
                    break
                if row["key"] == keep and limit > 0:
                    continue
                try:
                    (self.cache_dir() / row["file"]).unlink(missing_ok=True)
                except OSError:
                    pass
                self.db.execute("DELETE FROM audio_cache WHERE key = ?", (row["key"],))
                total -= row["size"]
                removed.append(row["key"])
            self.db.commit()
        return removed

    def clear_audio_cache(self) -> int:
        """Delete every cached file (the budget setting stays as it is). Returns how many files went."""
        with self._lock:
            rows = self.db.execute("SELECT key, file FROM audio_cache").fetchall()
            for row in rows:
                try:
                    (self.cache_dir() / row["file"]).unlink(missing_ok=True)
                except OSError:
                    pass
            self.db.execute("DELETE FROM audio_cache")
            self.db.commit()
        return len(rows)

    # ---- backup: everything personal in one file, and back -------------------------------------
    BACKUP_FORMAT = "distill-media-backup"
    BACKUP_SETTINGS = ("snapshot_limit_mb", "audio_cache_limit_mb", "cover_cache_mb")

    @staticmethod
    def _album_dict(row) -> dict:
        return {"source": row["source"], "ident": row["ident"], "title": row["title"], "detail": row["detail"], "art": row["art"]}

    def export_data(self) -> dict:
        """The library, favourites, playlists, play history and the album chosen for each game, as plain data. Albums
        saved on this computer are left out (a rescan finds them again) and so are track lists and cached files."""
        with self._lock:
            local = LOCAL_SOURCE
            albums = {r["id"]: self._album_dict(r) for r in self.db.execute("SELECT * FROM albums WHERE source <> ?", (local,))}
            key = lambda i: f"{albums[i]['source']}|{albums[i]['ident']}"  # noqa: E731
            lib = [{"album": key(r["album"]), "added_at": r["added_at"], "last_played_at": r["last_played_at"],
                    "play_count": r["play_count"]} for r in self.db.execute("SELECT * FROM library") if r["album"] in albums]
            fav = [{"album": key(r["album"]), "position": r["position"], "liked_at": r["liked_at"]}
                   for r in self.db.execute("SELECT * FROM favourites") if r["album"] in albums]
            lists = []
            for p in self.db.execute("SELECT * FROM playlists ORDER BY id"):
                items = [{"album": key(i["album"]), "position": i["position"]} for i in self.db.execute(
                    "SELECT album, position FROM playlist_items WHERE playlist = ? ORDER BY ord", (p["id"],)) if i["album"] in albums]
                lists.append({"name": p["name"], "created_at": p["created_at"], "items": items})
            hist = [{"album": key(r["album"]), "position": r["position"], "played_at": r["played_at"]} for r in self.db.execute(
                "SELECT * FROM history ORDER BY played_at DESC LIMIT 2000") if r["album"] in albums]
            games = {r["game"]: key(r["album"]) for r in self.db.execute("SELECT game, album FROM game_albums") if r["album"] in albums}
            used = {a for a in [*(x["album"] for x in lib), *(x["album"] for x in fav), *(x["album"] for x in hist), *games.values(),
                                *(i["album"] for pl in lists for i in pl["items"])]}
            settings = {k: v for k in self.BACKUP_SETTINGS if (v := self._get("s:" + k)) is not None}
        return {"format": self.BACKUP_FORMAT, "version": 1, "exported_at": time.time(),
                "albums": {k: next(a for i, a in albums.items() if key(i) == k) for k in sorted(used)},
                "library": lib, "favourites": fav, "playlists": lists, "history": hist, "game_albums": games, "settings": settings}

    def import_data(self, data: dict) -> dict[str, int]:
        """Merge a backup into this library: nothing already here is replaced or removed. Returns how many of each were
        added. ValueError when `data` is not a backup of this kind."""
        if not isinstance(data, dict) or data.get("format") != self.BACKUP_FORMAT or not isinstance(data.get("version"), int):
            raise ValueError("That is not a Distill media backup.")
        if data["version"] > 1:
            raise ValueError("That backup is from a newer version of Distill.")
        added = {"library": 0, "favourites": 0, "playlists": 0, "playlist tracks": 0, "history": 0, "games": 0}
        ids: dict[str, int] = {}
        for key, a in (data.get("albums") or {}).items():
            if not isinstance(a, dict) or not a.get("source") or not a.get("ident"):
                continue
            ids[key] = self.upsert_album(Album(str(a["source"]), str(a["ident"]), str(a.get("title", "")), str(a.get("detail", "")), 1.0,
                                               str(a.get("art", ""))), update=False)
        with self._lock:
            for x in data.get("library") or []:
                if (aid := ids.get(x.get("album"))) is not None:
                    cur = self.db.execute("INSERT OR IGNORE INTO library (album, added_at, last_played_at, play_count) VALUES (?, ?, ?, ?)",
                                          (aid, float(x.get("added_at") or time.time()), float(x.get("last_played_at") or 0),
                                           int(x.get("play_count") or 0)))
                    added["library"] += cur.rowcount > 0
            for x in data.get("favourites") or []:
                if (aid := ids.get(x.get("album"))) is not None:
                    cur = self.db.execute("INSERT OR IGNORE INTO favourites (album, position, liked_at) VALUES (?, ?, ?)",
                                          (aid, int(x.get("position", 0)), float(x.get("liked_at") or time.time())))
                    added["favourites"] += cur.rowcount > 0
            for pl in data.get("playlists") or []:
                name = str(pl.get("name") or "Untitled")
                row = self.db.execute("SELECT id FROM playlists WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
                if row is None:
                    now = time.time()
                    pid = self.db.execute("INSERT INTO playlists (name, created_at, updated_at) VALUES (?, ?, ?)",
                                          (name, float(pl.get("created_at") or now), now)).lastrowid
                    added["playlists"] += 1
                else:
                    pid = row["id"]
                have = {(r["album"], r["position"]) for r in self.db.execute("SELECT album, position FROM playlist_items WHERE playlist = ?", (pid,))}
                end = self.db.execute("SELECT COALESCE(MAX(ord), -1) AS m FROM playlist_items WHERE playlist = ?", (pid,)).fetchone()["m"]
                for it in pl.get("items") or []:
                    aid = ids.get(it.get("album"))
                    if aid is None or (aid, int(it.get("position", 0))) in have:
                        continue
                    end += 1
                    self.db.execute("INSERT INTO playlist_items (playlist, ord, album, position) VALUES (?, ?, ?, ?)",
                                    (pid, end, aid, int(it.get("position", 0))))
                    added["playlist tracks"] += 1
            for x in data.get("history") or []:
                if (aid := ids.get(x.get("album"))) is not None:
                    dup = self.db.execute("SELECT 1 FROM history WHERE album = ? AND position = ? AND played_at = ?",
                                          (aid, int(x.get("position", 0)), float(x.get("played_at") or 0))).fetchone()
                    if not dup:
                        self.db.execute("INSERT INTO history (album, position, played_at) VALUES (?, ?, ?)",
                                        (aid, int(x.get("position", 0)), float(x.get("played_at") or 0)))
                        added["history"] += 1
            for game, key in (data.get("game_albums") or {}).items():
                if (aid := ids.get(key)) is not None and not self.db.execute("SELECT 1 FROM game_albums WHERE game = ?", (game,)).fetchone():
                    self.db.execute("INSERT INTO game_albums (game, album, chosen_at) VALUES (?, ?, ?)", (game, aid, time.time()))
                    added["games"] += 1
            for k, v in (data.get("settings") or {}).items():
                if k in self.BACKUP_SETTINGS and self._get("s:" + k) is None:
                    self._set("s:" + k, str(v))
            self.db.commit()
        return added
