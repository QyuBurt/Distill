"""Which run the window is showing, and what its Start / Pause / Stop buttons should be doing. Nothing here imports Qt.

Conversions (and the other tool jobs) go one at a time. Downloads have a run of their own, so one can be going while a conversion
is. The buttons belong to the page on screen: the Download and Soundtrack pages drive the download run, every other page drives the
tool run. These two functions are the whole rule, so it can be tested without a window.
"""
from __future__ import annotations

import re

from dataclasses import dataclass

TOOL, DL, ART = "tool", "dl", "art"  # ART: the Games page fetching covers and details in the background


def view_of(mode: str) -> str:
    """The run a page controls: the Download and Soundtrack pages (mode "dl") the download run, everything else the tool run."""
    return DL if mode == "dl" else TOOL


@dataclass(frozen=True)
class RunFacts:
    tool: bool = False            # a conversion (or other tool job) is running
    tool_paused: bool = False
    tool_stopping: bool = False
    dl: bool = False              # the download run is going
    dl_paused: bool = False
    dl_stopping: bool = False
    org: bool = False             # an Organise job is going (it has its own Stop, and blocks Start everywhere)
    org_stopping: bool = False
    pending_tools: bool = False   # tool checks or tool downloads are still under way


@dataclass(frozen=True)
class Controls:
    start: bool
    pause: bool
    stop: bool
    pause_text: str


def controls(view: str, f: RunFacts) -> Controls:
    """The buttons for the page on screen. A run on the other side never changes them: that is the point of the split."""
    running, paused, stopping = ((f.dl, f.dl_paused, f.dl_stopping) if view == DL else (f.tool, f.tool_paused, f.tool_stopping))
    return Controls(
        start=not running and not f.org and not f.pending_tools,
        pause=running and not stopping,
        stop=(f.org and not f.org_stopping) or (running and not stopping),
        pause_text="Resume" if running and paused else "Pause",
    )


def bar_targets(tool: tuple[str, str] | None, dl: tuple[str, str] | None, current_page: str) -> list[tuple[str, str, str]]:
    """The jobs the bar along the bottom could stand for: those running on a page other than the one on screen, a conversion first.
    Each is (view, page, what it is doing); `tool` and `dl` are (page, what it is doing) or None."""
    found = [(TOOL, *tool)] if tool is not None else []
    if dl is not None:
        found.append((DL, *dl))
    return [t for t in found if t[1] != current_page]


def bar_jobs(tool: tuple[str, str] | None, dl: tuple[str, str] | None, art: tuple[str, str] | None = None) -> list[tuple[str, str, str]]:
    """The jobs the bar along the bottom stands for: every one that is running, whichever page is on screen, a conversion first, then the
    download, then the Games page's cover and details fetch. Each is (view, page, what it is doing); `tool`, `dl` and `art` are (page, what it
    is doing) or None. The bar is the one progress bar in the window: it shows while any job runs, and clicking it goes to the page that is
    doing the work."""
    found = [(TOOL, *tool)] if tool is not None else []
    if dl is not None:
        found.append((DL, *dl))
    if art is not None:
        found.append((ART, *art))
    return found


_SPEED = re.compile(r"(\d[\d,]*(?:\.\d+)?\s[KMGT]?B/s)")
_LEFT = re.compile(r"Est\. remaining: ~([^|]+?)\s*(?:\||$)")


def eta_bits(text: str) -> str:
    """The short form of a run's time line for the bar along the bottom: "3.20 MB/s  |  ~12m left" out of "Elapsed: 4m  |  1.2 GB of
    9.8 GB  |  3.20 MB/s  |  Est. remaining: ~12m". Empty while it has nothing to say yet (still estimating, or paused)."""
    bits = []
    if speed := _SPEED.search(text):
        bits.append(speed.group(1))
    if left := _LEFT.search(text):
        bits.append(f"~{left.group(1).strip()} left")
    return "  |  ".join(bits)
