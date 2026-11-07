"""Watch folder: drop a .txt, .torrent, .magnet or .url file into a folder and its links join the Download tab's list.
No Qt in here (the page polls `FolderWatcher.poll()` on a timer).

A .txt (or .url / .magnet) can hold any number of links, one anywhere on a line, including the `scan:` form that reads a
web page (see crawl.py). A .torrent is read from disk. Each file that has been taken moves into a `queued` folder beside
the others, so it is never queued twice and the person can see what went in; a file that can't be moved is remembered
instead. A file still being written (changed in the last couple of seconds) is left for the next look."""
from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .constants import APP_DIR

WATCH_FOLDER = APP_DIR / "Watch"  # where the Settings option looks; made when the option is switched on
WATCH_EXTENSIONS = (".txt", ".torrent", ".magnet", ".url")
QUEUED_DIR = "queued"
SETTLE_SECONDS = 2.0  # a file younger than this may still be copying in
MAX_FILE = 1 << 20  # a link list is small; anything bigger is not one
_LINK = re.compile(r"(?:scan(?:\[[^\]]*\])?:)?(?:https?://|magnet:\?)[^\s<>\"']+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Drop:
    name: str  # the file's name, for the log
    links: tuple[str, ...]  # what to hand to resolve_links


def links_in_file(path: Path) -> list[str]:
    """The links one dropped file stands for ([] if there are none, or it can't be read)."""
    ext = path.suffix.lower()
    if ext == ".torrent":
        try:
            return [path.resolve().as_uri()] if path.stat().st_size > 0 else []  # a file: address has no spaces in it
        except OSError:
            return []
    try:
        if path.stat().st_size > MAX_FILE:
            return []
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for raw in _LINK.findall(text):
        link = raw.rstrip(".,;)]}")
        if urllib.parse.unquote(link) and link not in out:
            out.append(link)
    return out


class FolderWatcher:
    def __init__(self, folder: Path | str):
        self.folder = Path(folder)
        self._seen: set[tuple[str, float, int]] = set()  # files that gave nothing, or could not be moved: not looked at again

    def poll(self, now: float | None = None) -> list[Drop]:
        """The new drops since the last look (and the files are moved into `queued` as they are taken)."""
        now = time.time() if now is None else now
        try:
            entries = sorted(p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() in WATCH_EXTENSIONS)
        except OSError:
            return []
        drops: list[Drop] = []
        for path in entries:
            try:
                st = path.stat()
            except OSError:
                continue
            key = (path.name, st.st_mtime, st.st_size)
            if key in self._seen or now - st.st_mtime < SETTLE_SECONDS:
                continue
            links = links_in_file(path)
            if not links:
                self._seen.add(key)
                continue
            moved = self._move_away(path)
            if moved is None:
                self._seen.add(key)  # could not be moved aside: take it this once and never again
            elif path.suffix.lower() == ".torrent":
                links = [moved.resolve().as_uri()]  # the address made before the move must point where the file now is
            drops.append(Drop(path.name, tuple(links)))
        return drops

    def _move_away(self, path: Path) -> Path | None:
        done = self.folder / QUEUED_DIR
        try:
            done.mkdir(exist_ok=True)
            target = done / path.name
            n = 2
            while target.exists():
                target = done / f"{path.stem} ({n}){path.suffix}"
                n += 1
            path.replace(target)
            return target
        except OSError:
            return None
