"""Download rules: a global speed limit, a weekly schedule, and per-site limits (files at once, speed, retries).

No Qt in here. The runner asks `GATE.try_acquire(host)` before it starts a file and calls `THROTTLE.consume(...)` after
every chunk it reads. Rules live in distill_rules.json next to the app and take effect on running downloads at once."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable
from urllib.parse import urlsplit

from .constants import APP_DIR

RULES_FILE = APP_DIR / "distill_rules.json"
MAX_SEGMENTS = 16
UNLIMITED, PAUSED = 0, -1  # values of a speed limit in KB/s; anything above is a real limit
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(slots=True)
class HostRule:
    host: str  # 'example.com' also covers 'cdn.example.com'
    max_files: int = 0  # files downloading from this site at once; 0 = no limit beyond the global setting
    kbps: int = 0  # speed cap for this site in KB/s; 0 = none
    attempts: int = 0  # tries per file; 0 = the app default
    segments: int = 0  # connections per file for this site; 0 = the overall setting
    proxy: str = ""  # '' = the overall proxy, 'direct' = no proxy for this site, else an http(s) proxy address


@dataclass(slots=True)
class ScheduleRule:
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # 0 = Monday
    start: int = 0  # minutes after midnight
    end: int = 24 * 60
    kbps: int = 0  # 0 = full speed in this window, -1 = don't download in this window, else a cap in KB/s

    def covers(self, now: datetime) -> bool:
        minute, day = now.hour * 60 + now.minute, now.weekday()
        if self.start == self.end:
            return day in self.days  # the whole day
        if self.start < self.end:
            return day in self.days and self.start <= minute < self.end
        # a window that crosses midnight belongs to the day it starts on
        return (day in self.days and minute >= self.start) or ((day - 1) % 7 in self.days and minute < self.end)


@dataclass(slots=True)
class Rules:
    global_kbps: int = 0  # applies whenever no schedule window matches; 0 = unlimited
    schedule: list[ScheduleRule] = field(default_factory=list)
    hosts: list[HostRule] = field(default_factory=list)
    segments: int = 1  # connections per file (1 = one connection, the safest); big files are split between them
    proxy: str = ""  # an http(s) proxy for every download ('' = none; this computer's proxy settings still apply)

    def active_kbps(self, now: datetime | None = None) -> int:
        """The limit in force right now: the first matching schedule window, else the global limit."""
        now = now or datetime.now()
        for rule in self.schedule:
            if rule.covers(now):
                return rule.kbps
        return self.global_kbps

    def proxy_for(self, host: str) -> str:
        """'' (no rule: this computer's own proxy settings apply), 'direct' (never use a proxy) or the proxy address."""
        rule = self.host_rule(host)
        return (rule.proxy if rule and rule.proxy else self.proxy) or ""

    def segments_for(self, host: str) -> int:
        rule = self.host_rule(host)
        return min(MAX_SEGMENTS, max(1, (rule.segments if rule and rule.segments else self.segments)))

    def host_rule(self, host: str) -> HostRule | None:
        """The most specific rule whose site is this host or a parent of it."""
        best = None
        for rule in self.hosts:
            h = rule.host.strip().lower()
            if h and (host == h or host.endswith("." + h)) and (best is None or len(h) > len(best.host)):
                best = rule
        return best

    def to_json(self) -> dict:
        return {"global_kbps": self.global_kbps, "segments": self.segments, "proxy": self.proxy,
                "schedule": [{**asdict(r), "days": list(r.days)} for r in self.schedule],
                "hosts": [asdict(r) for r in self.hosts]}

    @classmethod
    def from_json(cls, data: object) -> "Rules":
        rules = cls()
        if not isinstance(data, dict):
            return rules

        def num(v, lo=-1, hi=10**7) -> int:
            try:
                return min(hi, max(lo, int(v)))
            except (TypeError, ValueError):
                return 0
        rules.global_kbps = max(0, num(data.get("global_kbps")))
        rules.proxy = normalize_proxy(str(data.get("proxy", "")))[0]
        rules.segments = min(MAX_SEGMENTS, max(1, num(data.get("segments"), 1, MAX_SEGMENTS) or 1))
        for r in data.get("schedule", []) if isinstance(data.get("schedule"), list) else []:
            if isinstance(r, dict):
                days = tuple(sorted({d for d in r.get("days", []) if isinstance(d, int) and 0 <= d <= 6}))
                rules.schedule.append(ScheduleRule(days or (0, 1, 2, 3, 4, 5, 6), num(r.get("start"), 0, 1439),
                                                   num(r.get("end"), 0, 1440), num(r.get("kbps"))))
        for r in data.get("hosts", []) if isinstance(data.get("hosts"), list) else []:
            if isinstance(r, dict) and str(r.get("host", "")).strip():
                rules.hosts.append(HostRule(str(r["host"]).strip().lower(), max(0, num(r.get("max_files"), 0, 64)),
                                            max(0, num(r.get("kbps"))), max(0, num(r.get("attempts"), 0, 20)),
                                            max(0, num(r.get("segments"), 0, MAX_SEGMENTS)),
                                            normalize_proxy(str(r.get("proxy", "")), allow_direct=True)[0]))
        return rules


RULES = Rules()  # live rules, replaced in place by load_rules() / the Settings page


def load_rules() -> None:
    try:
        loaded = Rules.from_json(json.loads(RULES_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        loaded = Rules()
    set_rules(loaded)


def set_rules(new: Rules) -> None:
    RULES.global_kbps, RULES.segments, RULES.schedule, RULES.hosts = new.global_kbps, new.segments, new.schedule, new.hosts
    RULES.proxy = new.proxy


def save_rules() -> None:
    try:
        if RULES.global_kbps or RULES.segments > 1 or RULES.proxy or RULES.schedule or RULES.hosts:
            RULES_FILE.write_text(json.dumps(RULES.to_json(), indent=1), encoding="utf-8")
        else:
            RULES_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def normalize_proxy(text: str, allow_direct: bool = False) -> tuple[str, str]:
    """(clean proxy address, problem). Only http(s) proxies work (SOCKS needs a library Distill doesn't ship)."""
    text = text.strip()
    if not text:
        return "", ""
    if text.lower() == "direct":
        return ("direct", "") if allow_direct else ("", "'direct' only makes sense for a single site")
    if text.lower().startswith(("socks", "ss://")):
        return "", "SOCKS proxies aren't supported: use an http(s) proxy"
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "", "Enter the proxy as host:port or http://host:port"
    return text, ""


def host_of(url: str) -> str:
    """The site a download counts against: its host name ('torbox:' entries count as TorBox)."""
    if url.startswith("torbox:"):
        return "torbox.app"
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def describe_schedule(rule: ScheduleRule) -> str:
    days = "every day" if len(rule.days) == 7 else ", ".join(DAYS[d] for d in rule.days)
    limit = {UNLIMITED: "full speed", PAUSED: "paused"}.get(rule.kbps, f"{rule.kbps} KB/s")
    return f"{days}  {rule.start // 60:02d}:{rule.start % 60:02d}-{rule.end // 60 % 24:02d}:{rule.end % 60:02d}: {limit}"


class HostGate:
    """Counts files in flight per site so a per-site 'files at once' limit holds across the whole run."""

    def __init__(self, rules: Rules = RULES):
        self._rules, self._busy, self._lock = rules, {}, threading.Lock()

    def try_acquire(self, host: str) -> bool:
        rule = self._rules.host_rule(host)
        cap = rule.max_files if rule else 0
        with self._lock:
            if cap and self._busy.get(host, 0) >= cap:
                return False
            self._busy[host] = self._busy.get(host, 0) + 1
            return True

    def release(self, host: str) -> None:
        with self._lock:
            self._busy[host] = max(0, self._busy.get(host, 0) - 1)


class Throttle:
    """Paces reads to the global limit and the site's limit together. Each limit is a running clock: bytes are charged
    to it and the caller sleeps until its turn, which averages out to the limit however the chunks fall."""

    def __init__(self, rules: Rules = RULES, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] = datetime.now):
        self._rules, self._clock, self._sleep, self._now = rules, clock, sleep, now
        self._global = 0.0
        self._hosts: dict[str, float] = {}
        self._lock = threading.Lock()

    def consume(self, nbytes: int, host: str, abort: Callable[[], bool]) -> bool:
        """Wait as long as the limits ask. False if `abort()` turned true while waiting (stop the download)."""
        while (kbps := self._rules.active_kbps(self._now())) == PAUSED:  # a schedule window that forbids downloading
            if abort():
                return False
            self._sleep(1.0)
        rule = self._rules.host_rule(host)
        wait = 0.0
        with self._lock:
            now = self._clock()
            if kbps > 0:
                turn = max(self._global, now)
                self._global = turn + nbytes / (kbps * 1024)
                wait = turn - now
            if rule and rule.kbps > 0:
                turn = max(self._hosts.get(host, 0.0), now)
                self._hosts[host] = turn + nbytes / (rule.kbps * 1024)
                wait = max(wait, turn - now)
        while wait > 0.001:
            if abort():
                return False
            step = min(wait, 0.25)
            self._sleep(step)
            wait -= step
        return not abort()


GATE = HostGate()
THROTTLE = Throttle()
