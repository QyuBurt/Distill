"""What the Files & Torrents list says about itself, in a few short words: how many are ticked, how many are going, done or
failed, and a one-line summary of what happens when the downloads end. Reads the rows; changes nothing. No Qt.
"""
from __future__ import annotations

from .models import fmt_size
from .orgreview import ACCENT, BAD, MUTED, OK, Chip

IDLE, ACTIVE, DONE, FAILED, STOPPED = "idle", "active", "done", "failed", "stopped"
DEFAULT_FINISHED = ("Done", "Already downloaded")  # the statuses of a row whose file is complete (ui.models.FINISHED)


def classify(status: str, finished: tuple[str, ...] = DEFAULT_FINISHED) -> str:
    """Which group a row's status text puts it in. Anything the engines write while a file is on the move ("45.2%",
    "Looking for peers...", "Paused - VPN dropped") counts as active."""
    text = (status or "").strip()
    if not text:
        return IDLE
    if text in finished:
        return DONE
    if text.startswith("Failed"):
        return FAILED
    if text.startswith("Stopp"):  # "Stopped", "Stopping..."
        return STOPPED
    return ACTIVE


def list_chips(sizes: list[int | None], checked: set[int], removed: set[int], statuses: list[str],
               finished: tuple[str, ...] = DEFAULT_FINISHED) -> list[Chip]:
    """The chips over the list. `sizes[i]` is row i's size in bytes (None: not known yet); a removed row is not counted."""
    shown = len(sizes) - len(removed)
    if shown <= 0:
        return []
    picks = [i for i in checked if 0 <= i < len(sizes) and i not in removed]
    total = sum(sizes[i] or 0 for i in picks)
    more = "+" if any(sizes[i] is None for i in picks) else ""
    text = f"{len(picks)} of {shown} selected" + (f" \u00b7 {fmt_size(total)}{more}" if picks else "")
    chips = [Chip(text, ACCENT if picks else MUTED)]
    counts = {ACTIVE: 0, DONE: 0, FAILED: 0}
    for i, status in enumerate(statuses):
        if i not in removed and (group := classify(status, finished)) in counts:
            counts[group] += 1
    for group, word, tone in ((ACTIVE, "downloading", ACCENT), (DONE, "done", OK), (FAILED, "failed", BAD)):
        if counts[group]:
            chips.append(Chip(f"{counts[group]} {word}", tone))
    return chips


def after_summary(unpack: str, organise: bool, command: str, power: str, resume: bool) -> tuple[str, bool]:
    """One line for the folded "After downloads" settings, and whether it deserves a warning colour (the computer will sleep
    or shut down). `unpack` and `power` are the choices as the boxes spell them ("Nothing more", "Nothing")."""
    parts = []
    if unpack and not unpack.startswith("Nothing"):
        parts.append(unpack[0].lower() + unpack[1:])
    if organise:
        parts.append("open Sort by System")
    if command.strip():
        parts.append("run a command")
    armed = bool(power) and power != "Nothing"
    if armed:
        parts.append(f"then {power.lower()} the computer")
    if resume:
        parts.append("resume on startup")
    return (", ".join(parts)[:1].upper() + ", ".join(parts)[1:] if parts else "Nothing happens when they finish"), armed
