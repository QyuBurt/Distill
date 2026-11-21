"""Whether each conversion / download tool is ready, as a word and a colour, and the one-line overview of them all.

The rule is the one the sidebar dots and the footer markers already use (a tool is ready when its path points at a file; else
it is still being looked for, or it is missing); a copy being downloaded right now shows as updating whatever else is true.
No Qt.
"""
from __future__ import annotations

from .orgreview import ACCENT, BAD, OK, WARN, Chip

READY, CHECKING, UPDATING, MISSING = "ready", "checking", "updating", "missing"
LABELS = {READY: ("Ready", OK), CHECKING: ("Checking", WARN), UPDATING: ("Updating", WARN), MISSING: ("Missing", BAD)}


def tool_state(ready: bool, pending: bool, installing: bool) -> str:
    """`ready`: its path points at a file that runs. `pending`: the start-up check has not finished. `installing`: a copy is
    being downloaded now."""
    if installing:
        return UPDATING
    if ready:
        return READY
    return CHECKING if pending else MISSING


def describe(state: str) -> tuple[str, str]:
    """(the word on the tool's tag, its tone)."""
    return LABELS.get(state, LABELS[MISSING])


def overview(states: dict[str, str]) -> list[Chip]:
    """The tags over the Tools page: how many are ready, how many are busy, how many are missing."""
    if not states:
        return []
    values = list(states.values())
    ready, busy, missing = values.count(READY), values.count(CHECKING) + values.count(UPDATING), values.count(MISSING)
    chips = [Chip(f"{ready} of {len(values)} ready", OK if ready == len(values) else ACCENT)]
    if busy:
        chips.append(Chip(f"{busy} checking or updating", WARN))
    if missing:
        chips.append(Chip(f"{missing} missing", BAD))
    return chips
