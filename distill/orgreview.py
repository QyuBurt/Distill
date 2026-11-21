"""What the Organise review bar says about the selection: a few short chips ("12 of 40 selected", "frees 3.20 GB").

The scan results themselves are Findings (organize.py); this only reads them. Nothing here imports Qt.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import fmt_size, plural

ACCENT, OK, WARN, BAD, MUTED = "accent", "ok", "warn", "bad", "muted"  # which colour a chip is drawn in


@dataclass(frozen=True, slots=True)
class Chip:
    text: str
    tone: str


def review_chips(findings: list, checked: set[int]) -> list[Chip]:
    """The selection on a scan page: how many rows are ticked, what applying them frees, what can't be applied."""
    if not findings:
        return []
    ticked = [findings[i] for i in checked if 0 <= i < len(findings)]
    chips = [Chip(f"{len(ticked)} of {len(findings)} selected", ACCENT if ticked else MUTED)]
    if freed := sum(f.size for f in ticked):
        chips.append(Chip(f"frees {fmt_size(freed)}", OK))
    if blocked := sum(1 for f in findings if f.problem):
        chips.append(Chip(f"{blocked} can't be applied", BAD))
    return chips


def patch_chips(patches: int, games: int, made: int) -> list[Chip]:
    """The same for the Patch page, which ticks patches under games rather than rows."""
    if not patches:
        return [Chip("nothing selected", MUTED)]
    return [Chip(f"{plural(patches, 'patch', 'patches')} on {plural(games, 'game')}", ACCENT),
            Chip(f"{plural(made, 'patched file')} will be made", OK)]


def note_tone(note: str) -> str | None:
    """The colour a detail row's Status deserves, so what stays and what goes reads at a glance: KEEP in green, anything that
    would be removed or deleted in red. Everything else keeps the list's ordinary colour (None)."""
    text = note.strip().lower()
    if text == "keep":
        return OK
    if text.startswith(("remove", "delete", "duplicate - removed")):
        return BAD
    return None


def is_destination(note: str) -> bool:
    """A row whose Status says where it is going ("->  New Name.chd", "->  PlayStation 2/"): drawn in the accent colour so the
    'after' of a rename or a sort stands out from the 'before' beside it."""
    return note.lstrip().startswith("->")
