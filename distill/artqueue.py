"""A small pool of background threads that fetch poster art for what is on screen. Nothing here imports Qt.

The PC Recomps grid and the Games library both used to carry their own copy of this: a newest-first queue, a lock, a count of running
threads, and a loop that backs off when SteamGridDB says it is busy. It lives here once, so the two pages can't drift apart.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable


class ArtQueue:
    """`add(item, *args)` queues one fetch; up to `workers` threads drain the queue, newest first (what was painted last is what is on
    screen now). For each item `fetch(item, *args)` is called off the UI thread and `deliver(item, found)` gets the result: `found` is
    None when the fetch failed, so one bad item never stops the rest. If `fetch` raises one of the `busy` exceptions (a rate limit),
    `on_busy(item)` is called so the item can be asked about again, and the worker eases off for `backoff` seconds."""

    def __init__(self, fetch: Callable[..., Any], deliver: Callable[[Any, Any], None], *, workers: int = 6,
                 busy: type[BaseException] | tuple[type[BaseException], ...] = (), on_busy: Callable[[Any], None] | None = None,
                 pause: float = 0.05, backoff: float = 3.0, sleep: Callable[[float], None] = time.sleep,
                 on_start: Callable[[Any], None] | None = None, max_pending: int = 0, on_drop: Callable[[list[Any]], None] | None = None):
        self._fetch, self._deliver, self._on_busy = fetch, deliver, on_busy
        self._on_start, self._max_pending, self._on_drop = on_start, max_pending, on_drop
        self._workers, self._busy, self._pause, self._backoff, self._sleep = workers, busy, pause, backoff, sleep
        self._items: deque[tuple[Any, tuple]] = deque()
        self._lock = threading.Lock()
        self._running = 0

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def running(self) -> int:
        with self._lock:
            return self._running

    def add(self, item: Any, *args: Any) -> None:
        dropped: list[Any] = []
        with self._lock:
            self._items.append((item, args))
            while self._max_pending and len(self._items) > self._max_pending:  # scrolled far past: the oldest asks (the least likely on screen) go
                dropped.append(self._items.popleft()[0])
            start = self._running < self._workers
            if start:
                self._running += 1
        self._dropped(dropped)
        if start:
            threading.Thread(target=self._work, daemon=True).start()

    def clear(self) -> list[Any]:
        """Take everything that has not begun off the queue (what is being fetched this moment finishes). Returns the items, so a caller can
        forget it asked for them and ask again if they are still wanted."""
        with self._lock:
            gone = [item for item, _args in self._items]
            self._items.clear()
        self._dropped(gone)
        return gone

    def _dropped(self, items: list[Any]) -> None:
        if items and self._on_drop is not None:
            try:
                self._on_drop(items)
            except Exception:
                pass

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._items:
                    self._running -= 1
                    return
                item, args = self._items.pop()
            if self._on_start is not None:
                try:
                    self._on_start(item)
                except Exception:
                    pass
            try:
                found = self._fetch(item, *args)
            except self._busy:
                if self._on_busy is not None:
                    self._on_busy(item)
                found = None
                self._sleep(self._backoff)
            except Exception:
                found = None
            try:
                self._deliver(item, found)
            except Exception:  # a failed hand-over must not kill the worker and leave the pool a thread short
                pass
            self._sleep(self._pause)
