"""Which run the window is showing, and what its Start / Pause / Stop buttons should be doing. Nothing here imports Qt.

Conversions (and the other tool jobs) go one at a time. Downloads have a run of their own, so one can be going while a conversion
is. The buttons belong to the page on screen: the Download and Soundtrack pages drive the download run, every other page drives the
tool run. These two functions are the whole rule, so it can be tested without a window.
"""
from __future__ import annotations

from dataclasses import dataclass

TOOL, DL = "tool", "dl"


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
