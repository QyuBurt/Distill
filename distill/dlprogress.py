"""What a row of the Files & Torrents list says about its own progress, read from its Status text ("45%  12.00 MB / 26.00 MB").

The downloader writes that text (downloads.py _pct); this only reads it, to draw a thin line under the cell. No Qt.
"""
from __future__ import annotations

import re

from .dltally import ACTIVE, DONE, FAILED, classify

_PERCENT = re.compile(r"^\s*(\d{1,3})%")


def row_look(status: str) -> tuple[str, float | None]:
    """(kind, fraction) for a row's status: ("active", 0..1) while it downloads with a known size, ("done", 1.0), ("failed", 1.0);
    ("none", None) for everything else (no size known yet, "Looking for peers...", paused, stopped, a blank)."""
    group = classify(status)
    if group == DONE:
        return "done", 1.0
    if group == FAILED:
        return "failed", 1.0
    if group == ACTIVE and (m := _PERCENT.match(status)) is not None:
        return "active", min(100, int(m.group(1))) / 100
    return "none", None
