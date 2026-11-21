"""One-line summaries of a folded card's settings, so a card can be folded away without hiding what it is set to.

A card header asks for its summary when it paints while folded, so the words are always the controls' current state and there is
nothing to keep in step. No Qt.
"""
from __future__ import annotations

SEP = "  \u00b7  "


def workflow_summary(extract: bool | None, verify: bool, delete: bool, always_verified: bool = False) -> tuple[str, bool]:
    """The converter pages' Workflow card: ("Unpack archives  ·  Verify results  ·  Keep originals", warn). `extract` is None on a
    page with no such box (ZAR packs folders as they are). It warns (amber) when the originals will be deleted."""
    parts = []
    if extract:
        parts.append("Unpack archives")
    if verify or always_verified:
        parts.append("Checks each result" if always_verified else "Verify results")
    parts.append("Deletes originals" if delete else "Keeps originals")
    return SEP.join(parts), delete


def perf_summary(level: str, threads, total: int, disk: str, temp_dir: str = "") -> tuple[str, bool]:
    """The Performance card: "High  ·  12 of 16 threads  ·  Disk: Auto", and "Own temp folder" when one is set. `level` and `disk` are the
    switches' own words ("Low" ... "All", "Auto" / "HDD" / "SSD"); `threads` the count the level means."""
    parts = [level or "Default"]
    if isinstance(threads, int) and total:
        parts.append(f"{threads} of {total} threads")
    if disk:
        parts.append(f"Disk: {disk}")
    if temp_dir.strip():
        parts.append("Own temp folder")
    return SEP.join(parts), False


def changed_options(options: list[tuple], values: dict) -> list[str]:
    """The labels of the options whose current value differs from their default. `options` are orgpages' tuples
    (name, kind, label, default, ...); `values[name]` is a function returning the control's current value."""
    changed = []
    for name, kind, label, default, *_rest in options:
        read = values.get(name)
        if read is None:
            continue
        try:
            now = read()
        except RuntimeError:  # the control's widget is gone
            continue
        same = (bool(now) == bool(default)) if kind in ("check", "switch") else (str(now).strip() == str(default).strip())
        if not same:
            changed.append(label)
    return changed


def options_summary(options: list[tuple], values: dict) -> tuple[str, bool]:
    """The Organise pages' Options card: "Defaults", or how many have been changed from them."""
    changed = changed_options(options, values)
    if not changed:
        return "Defaults", False
    return f"{len(changed)} changed from the defaults", False


# The calmer layout (cards that hold the options sit folded, with a line saying what they are set to) is applied once to a settings
# file that has not seen it; after that, whatever the person opens or closes is remembered as ever.
LAYOUT = "calm1"
FOLD_TITLES = ("Workflow", "Options")  # the converter pages' Workflow card and the Organise pages' Options card


def calm_pending(data: dict) -> bool:
    """Whether this settings file has yet to get the calmer layout (a fresh install has not either, which comes to the same)."""
    return data.get("layout") != LAYOUT
