"""What the Games page is fetching in the background (covers, and the LaunchBox match that gives a game its details), counted so the bar
along the bottom of the window can say what is being grabbed and how long it will take. Nothing here imports Qt.

A game is one unit of work: it is `add`ed when its fetch is queued, `begin`s when a worker takes it (and says whether it is looking the
game up or fetching its cover), and `finish`es when the worker is done with it, found or not. The counts belong to a batch: the
first game queued after everything has settled starts a new one, so "12 / 340" always means this stretch of work.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

MIN_DONE_FOR_ETA = 3  # no estimate from fewer finished games than this: the first few say nothing about the rest
MIN_SECONDS_FOR_ETA = 4.0
SHOWN_NAMES = 2  # game names spelt out on the bar; any more are counted


@dataclass(frozen=True)
class Snapshot:
    running: bool
    total: int = 0
    done: int = 0
    covers: int = 0  # covers found in this batch
    matched: int = 0  # games linked to their LaunchBox entry in this batch
    active: tuple[tuple[str, str], ...] = ()  # (game, "info" | "cover") being worked on right now
    waiting: int = 0
    eta: float | None = None  # seconds, None while it cannot tell yet

    @property
    def fraction(self) -> float:
        return min(1.0, self.done / self.total) if self.total else 0.0


def fmt_eta(seconds: float) -> str:
    """'~40s', '~3m', '~1h 5m'."""
    s = max(1, round(seconds))
    if s < 90:
        return f"~{s}s" if s >= 10 else "~10s"
    m = round(s / 60)
    if m < 60:
        return f"~{m}m"
    h, m = divmod(m, 60)
    return f"~{h}h {m}m" if m else f"~{h}h"


STAGE_WORDS = {"info": "details", "cover": "cover"}


def detail_line(snap: Snapshot, note: str = "") -> str:
    """\"Getting covers and details  |  12 / 340  |  Sonic (cover), Zelda (details) +3  |  ~3m left\" for the bar."""
    bits = ["Getting covers and details", f"{snap.done} / {snap.total}"]
    if snap.active:
        names = [f"{name} ({STAGE_WORDS.get(stage, stage)})" for name, stage in snap.active[:SHOWN_NAMES]]
        more = len(snap.active) - SHOWN_NAMES
        bits.append(", ".join(names) + (f" +{more}" if more > 0 else ""))
    if note:
        bits.append(note)
    elif snap.eta is not None:
        bits.append(f"{fmt_eta(snap.eta)} left")
    return "  |  ".join(bits)


def tooltip(snap: Snapshot, note: str = "") -> str:
    """The whole picture for the bar's tooltip: every game being worked on, and what has been found so far."""
    lines = [f"{snap.done} of {snap.total} games done  ({snap.covers} covers found, {snap.matched} matched on LaunchBox)"]
    if snap.waiting:
        lines.append(f"{snap.waiting} waiting")
    lines += [f"Now: {name} ({STAGE_WORDS.get(stage, stage)})" for name, stage in snap.active[:12]]
    if snap.eta is not None:
        lines.append(f"About {fmt_eta(snap.eta).lstrip('~')} to go")
    if note:
        lines.append(note)
    lines.append("Click to go to the Games page")
    return "\n".join(lines)


class ArtProgress:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._open: dict[str, str] = {}  # game id -> its name, for every game queued or being worked on
        self._active: dict[str, str] = {}  # game id -> "info" | "cover", for the ones a worker has taken
        self._names: dict[str, str] = {}
        self._total = self._done = self._covers = self._matched = 0
        self._started = 0.0

    def add(self, gid: str, name: str) -> None:
        """A game's fetch was queued (asking twice about one game while it is still open changes nothing)."""
        with self._lock:
            if gid in self._open:
                return
            if not self._open:  # everything had settled: this is a new batch
                self._reset()
                self._started = self._clock()
            self._open[gid] = name
            self._total += 1

    def begin(self, gid: str, stage: str) -> None:
        with self._lock:
            if gid in self._open:
                self._active[gid] = stage

    def covers_found(self, gid: str) -> None:
        with self._lock:
            if gid in self._open:
                self._covers += 1

    def matched(self, gid: str) -> None:
        with self._lock:
            if gid in self._open:
                self._matched += 1

    def finish(self, gid: str) -> None:
        with self._lock:
            if self._open.pop(gid, None) is not None:
                self._active.pop(gid, None)
                self._done += 1

    def drop(self, gids) -> None:
        """Games whose fetch was taken off the queue before it began: they were never part of the work."""
        with self._lock:
            for gid in gids:
                if self._open.pop(gid, None) is not None:
                    self._active.pop(gid, None)
                    self._total -= 1

    def running(self) -> bool:
        with self._lock:
            return bool(self._open)

    def snapshot(self) -> Snapshot:
        with self._lock:
            if not self._open:
                return Snapshot(False, self._total, self._done, self._covers, self._matched)
            elapsed = self._clock() - self._started
            left = self._total - self._done
            eta = None
            if self._done >= MIN_DONE_FOR_ETA and elapsed >= MIN_SECONDS_FOR_ETA:
                eta = elapsed / self._done * left
            active = tuple((self._open[g], st) for g, st in self._active.items() if g in self._open)
            return Snapshot(True, self._total, self._done, self._covers, self._matched, active, max(0, left - len(active)), eta)
