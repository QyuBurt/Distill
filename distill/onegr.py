"""1G1R ("one game, one ROM"): choosing a single release per title by region priority. No Qt in here."""
from __future__ import annotations

import re

DEFAULT_PRIORITY = ("USA", "Europe", "Japan")
_ALIASES = {"ntsc-u": "usa", "ntsc-j": "japan", "pal": "europe", "pal-e": "europe", "uk": "europe"}
_BAD_RE = re.compile(r"\((?:[^)]*\b(?:beta|proto(?:type)?|demo|sample|unl|pirate|promo|kiosk)\b[^)]*)\)|"
                     r"\[(?:b|h|o|f|t|p)\d*[^\]]*\]", re.IGNORECASE)
_REV_RE = re.compile(r"\((?:rev|v)\s*([0-9a-z.]+)\)", re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^()]*)\)")


def parse_priority(text: str) -> tuple[str, ...]:
    """'USA, Europe, Japan' -> ('usa', 'europe', 'japan'), normalised for comparing."""
    return tuple(_ALIASES.get(p.strip().casefold(), p.strip().casefold()) for p in text.replace(";", ",").split(",")
                 if p.strip())


def is_bad_release(stem: str) -> bool:
    """Beta, prototype, demo, sample, unlicensed, pirate or a bad-dump / hack tag: only kept when nothing else exists."""
    return bool(_BAD_RE.search(stem))


def revision(stem: str) -> tuple:
    """Comparable revision: 'Rev 2' > 'Rev 1' > none; 'Rev A' < 'Rev B'; 'v1.10' > 'v1.2'. Plain tuples so max() works."""
    m = _REV_RE.search(stem)
    if not m:
        return ()
    return tuple((0, int(p)) if p.isdigit() else (1, ord(p[0].lower())) for p in m[1].split(".") if p)


def region_rank(stem: str, priority: tuple[str, ...]) -> int:
    """Higher is better. A multi-region tag ('USA, Europe') scores by its best region; no listed region scores 0."""
    best = 0
    for tag in _PAREN_RE.findall(stem):
        for part in tag.split(","):
            key = _ALIASES.get(part.strip().casefold(), part.strip().casefold())
            if key in priority:
                best = max(best, len(priority) - priority.index(key))
            elif key == "world" and best == 0:
                best = 1  # "World" is playable anywhere: better than nothing, below any named choice
    return best


def rank(stem: str, priority: tuple[str, ...]) -> tuple:
    return (not is_bad_release(stem), region_rank(stem, priority), revision(stem), -len(stem))


def choose(stems: list[str], priority: tuple[str, ...]) -> str:
    """The one stem to keep."""
    return max(stems, key=lambda s: rank(s, priority))
