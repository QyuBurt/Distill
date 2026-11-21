"""One shared, adaptive limit on how hard Distill asks the LaunchBox Games Database for things. Nothing here imports Qt.

LaunchBox publishes no rate limit, so none is assumed. Instead the limiter feels its way: it starts with a few requests in flight, adds
one after every run of good answers (up to a ceiling), and when the site says "slow down" (HTTP 429, or 503 with or without a
Retry-After) it halves what it allows and holds every request back for the time the site asked for, then climbs again. So a
library of thousands of games is fetched as fast as the site tolerates, and never faster.

`Throttle.slot()` is a context manager around ONE request. `Throttle.busy(wait)` is what a caller says on a 429 / 503.
"""
from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator

START = 3  # requests in flight at first
CEILING = 8  # the most that are ever in flight, however well it is going
FLOOR = 1  # ...and the fewest, however badly
GOOD_RUN = 25  # this many good answers in a row earn one more place
MIN_GAP = 0.08  # seconds between two requests starting (so a burst of workers does not all fire in the same instant)
COOL_DOWN = 4.0  # how long everything waits after a "slow down" that said nothing about how long
MAX_COOL_DOWN = 30.0  # ...and the longest it ever waits, whatever the site asks
HOSTS = ("launchbox-app.com", "launchbox.gg")  # the Games Database's pages and its picture servers


def is_launchbox(host: str | None) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


class Throttle:
    def __init__(self, *, start: int = START, ceiling: int = CEILING, floor: int = FLOOR, good_run: int = GOOD_RUN, min_gap: float = MIN_GAP,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._ceiling, self._floor, self._good_run, self._min_gap = ceiling, floor, good_run, min_gap
        self._clock, self._sleep = clock, sleep
        self._cond = threading.Condition()
        self._limit = max(floor, min(start, ceiling))
        self._in_flight = 0
        self._good = 0
        self._resume_at = 0.0  # nothing starts before this (a cool-down after "slow down")
        self._next_start = 0.0
        self.slowed = 0  # how many times the site has said "slow down" (for the progress line and the tests)

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    def cooling(self) -> float:
        """Seconds left of a cool-down (0 when requests are flowing)."""
        with self._cond:
            return max(0.0, self._resume_at - self._clock())

    @contextlib.contextmanager
    def slot(self) -> Iterator[None]:
        """Wait for a place (and for any cool-down to end), then hold it for the length of one request."""
        self._enter()
        try:
            yield
        finally:
            self._leave()

    def _enter(self) -> None:
        with self._cond:
            while True:
                now = self._clock()
                if self._in_flight < self._limit and now >= self._resume_at and now >= self._next_start:
                    self._in_flight += 1
                    self._next_start = now + self._min_gap
                    return
                wait = max(self._resume_at - now, self._next_start - now, 0.0) if self._in_flight < self._limit else 0.5
                self._cond.wait(timeout=max(0.01, min(wait or 0.05, 1.0)))

    def _leave(self) -> None:
        with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._cond.notify_all()

    def good(self) -> None:
        """A request was answered properly: a long enough run of these earns one more place."""
        with self._cond:
            self._good += 1
            if self._good >= self._good_run and self._limit < self._ceiling:
                self._limit += 1
                self._good = 0
                self._cond.notify_all()

    def busy(self, wait: float = 0.0) -> float:
        """The site said "slow down": halve what is allowed, and hold every request back for `wait` seconds (or a default). Returns how long."""
        wait = min(MAX_COOL_DOWN, wait if wait and wait > 0 else COOL_DOWN)
        with self._cond:
            self.slowed += 1
            self._good = 0
            self._limit = max(self._floor, self._limit // 2)
            self._resume_at = max(self._resume_at, self._clock() + wait)
            self._cond.notify_all()
        return wait


_shared: dict[str, Throttle] = {}
_shared_lock = threading.Lock()


def for_host(host: str | None) -> Throttle | None:
    """The throttle for this host, or None for a host that is not LaunchBox's. The pages and the picture servers are limited separately: the
    pictures come off a CDN and are asked for far more often than the pages."""
    host = (host or "").lower()
    if not is_launchbox(host):
        return None
    key = "images" if host.startswith("images.") or host.startswith("gamesdb-images.") else "pages"
    with _shared_lock:
        if key not in _shared:
            _shared[key] = Throttle(start=4, ceiling=10) if key == "images" else Throttle()
        return _shared[key]


def pages() -> Throttle:
    """The throttle on the Games Database's pages (the one that matters for rate limits)."""
    t = for_host("gamesdb.launchbox-app.com")
    assert t is not None
    return t


def retry_after(headers) -> float:
    """Seconds from a Retry-After header (the number form; a date is not worth parsing here), or 0."""
    try:
        return float(headers.get("Retry-After", "")) if headers else 0.0
    except (ValueError, TypeError, AttributeError):
        return 0.0
