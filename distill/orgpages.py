"""The Organize pages: which tools exist, how a page scans its steps, and what the UI needs to know about each.

Kept apart from organize.py (the scanners and the apply / undo machinery) so that module can stay a sane size;
this one only wires the scanners together."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .frontends import PRESET_NONE, PRESETS
from .models import fmt_size
from .onegr import DEFAULT_PRIORITY
from .organize import (
    DISC_ACTIONS, DUPE_POLICIES, Finding, HOLDING_DIR, MATCH_MODES, UNKNOWN_CHOICES, _confirm_sort, _load_runs,
    find_bios, find_by_system, find_dat_verify, find_discfix, find_duplicates, find_leftovers, find_library,
    find_multidisc, find_onegr, find_patch_games, find_playlist_fixes, find_tidy_renames,
)


# ---- registry: the Organize tools and what the UI needs to know about each --------------------------------
def _confirm_removal(picks: list[Finding], opts: dict) -> str:
    how = ("PERMANENTLY DELETED (this can't be undone)" if opts.get("permanent")
           else f"moved into a '{HOLDING_DIR}' folder (Undo Last Run restores them)")
    return f"{len(picks)} item(s), {fmt_size(sum(f.size for f in picks))}, will be {how}.\n\nContinue?"


@dataclass(frozen=True, slots=True)
class OrgTool:
    key: str
    nav: str  # sidebar label
    title: str
    blurb: str
    headings: tuple[str, str]  # the two tree columns after the tick box
    apply_label: str
    empty: str  # status text when a scan finds nothing
    scan: Callable[[Path, bool, dict, threading.Event], tuple[list[Finding], str]]
    options: tuple[tuple, ...] = ()  # (name, "check" | "choice" | "folder" | "file" | "text", label, default[, choices | placeholder[, file filter]])
    confirm: Callable[[list[Finding], dict], str] | None = None
    export: bool = False  # the apply button saves a report of the ticked rows instead of changing files

    @property
    def busy(self) -> str:
        """What the Start button says while this tool's apply is running."""
        return APPLY_BUSY.get(self.key, "Working...")


APPLY_BUSY = {
    "cleanup": "Cleaning up...", "dupes": "Removing...", "onegr": "Moving aside...", "discfix": "Repairing...",
    "patch": "Patching...", "tidy": "Renaming...", "multi": "Organising...", "playlists": "Repairing...",
    "bysystem": "Sorting...", "datcheck": "Renaming...", "bioscheck": "Checking...", "report": "Saving...",
}

_PERMANENT = ("permanent", "check", "Delete permanently instead of moving aside (can't be undone)", False)


ORG_TOOLS: dict[str, OrgTool] = {t.key: t for t in (
    OrgTool("cleanup", "Clean up", "Clean up leftovers",
            "Find source files that already have a converted copy, plus partial downloads, stray tracks and empty folders.", ("Leftover", "Files"), "Clean Up Selected", "Nothing to clean up.",
            find_leftovers, (_PERMANENT,), _confirm_removal),
    OrgTool("dupes", "Duplicates", "Duplicate games",
            "Find the same game stored more than once and keep a single copy.",
            ("Game", "Copies"), "Remove Selected Duplicates", "No duplicates found.", find_duplicates,
            (("match", "choice", "Match by:", MATCH_MODES[0], MATCH_MODES),
             ("keep", "choice", "Keep:", DUPE_POLICIES[0], DUPE_POLICIES),
             ("other_files", "check", "Include ROMs & other game files (.gba, .nes, .zip), not just disc images",
              False),
             _PERMANENT), _confirm_removal),
    OrgTool("onegr", "One game, one ROM", "One game, one ROM (1G1R)",
            "Keep one release of each game: best region first, then newest revision. Betas, prototypes and demos "
            "lose, and the rest are moved aside.", ("Game", "Releases"),
            "Move Aside Selected", "Every title already has one release.", find_onegr,
            (("regions", "text", "Region priority, best first:", ", ".join(DEFAULT_PRIORITY), "USA, Europe, Japan"),
             ("other_files", "check", "Include ROMs & other game files, not just disc images", True),
             _PERMANENT), _confirm_removal),
    OrgTool("discfix", "Repair discs", "Repair disc images",
            "Merge or split BIN/CUE tracks, or convert CloneCD images to BIN/CUE. Originals are moved aside.",
            ("Disc", "Files"), "Repair Selected Discs", "No discs need repair for that action.", find_discfix,
            (("action", "choice", "Action:", DISC_ACTIONS[0], DISC_ACTIONS),
             ("replace", "check", "Replace the originals with the result (off keeps both, in a '(converted)' folder)", True))),
    OrgTool("patch", "Apply patches", "Apply patches",
            "Match UPS and BPS patches to ROMs by checksum, and IPS and xdelta by name (check those). "
            "The original ROM is never changed.", ("Game", "Patches"), "Apply Selected Patches",
            "No games with patches found.", find_patch_games,
            (("stack", "switch", "Multiple patches", False, "Separate copies", "Stacked",
              "Separate copies:  one patched copy per ticked patch, e.g. Game (Translation).gba and Game (Bugfix).gba\n\n"
              "Stacked:  the ticked patches are applied in the order listed into one copy, "
              "e.g. Game (Translation + Bugfix).gba"),
             ("show_all", "check", "Include games with no patch", False))),
    OrgTool("tidy", "Tidy filenames", "Tidy filenames",
            "Fix disc markers, spacing and other filename quirks so games group and sort correctly. Playlists are updated to match.", ("File", "Files"), "Rename Selected Files",
            "All filenames already look tidy.", find_tidy_renames,
            (("discs", "check", "Standardise disc markers:  [CD2], (Disk 2), Disc 2  →  (Disc 2)", True),
             ("spaces", "check", "Tidy filename spacing & convert underscores to spaces", True),
             ("articles", "check", "Move trailing articles to the front:  Legend of Zelda, The  →  The Legend of Zelda",
              False),
             ("tags", "check", "Remove dump tags such as [!] or [b1]", False),
             ("template", "text", "Rename to a pattern (blank = just tidy).  Parts: {title} {region} {tags} {disc} {system} {name}", ""))),
    OrgTool("multi", "Multi-disc games", "Multi-disc games",
            "Move each multi-disc game's discs into a folder of their own, with an .m3u playlist.",
            ("Game  ->  folder", "Discs"), "Organise Selected Games",
            "No multi-disc games found that still need organizing.", find_multidisc,
            (("loose", "check", "Also pair discs with similar names (left unticked for you to check)", False),
             ("only_missing", "check", "Only list games with a missing disc (e.g. Disc 1 and Disc 3, no Disc 2)", False))),
    OrgTool("playlists", "Playlist repair", "Playlist repair",
            "Fix .m3u playlists that point to renamed, converted or moved discs.",
            ("Playlist", "Entries"), "Repair Selected Playlists", "All playlists look fine.", find_playlist_fixes,
            (("add_missing", "check", "Add discs that are in the folder but missing from the playlist", True),)),
    OrgTool("bysystem", "Sort by system", "Sort by system",
            "Move each game into a folder for its console, identified from the file itself. Discs, tracks and "
            "playlists move together, and a game's own folder is kept.",
            ("System", "Files"), "Sort Selected Systems", "Everything is already sorted.", find_by_system,
            (("preset", "choice", "Folder names for:", PRESET_NONE, PRESETS),
             ("dest", "folder", "Sort into", "", "Blank = inside the scanned folder"),
             ("unknown", "choice", "If the system is unknown:", UNKNOWN_CHOICES[0], UNKNOWN_CHOICES)),
            _confirm_sort),
    OrgTool("datcheck", "Verify with DAT", "Verify against a DAT",
            "Check files against a No-Intro, Redump or TOSEC DAT and list what is bad, unknown or missing. Matches "
            "can be renamed to the official names. Nothing changes until you apply.",
            ("Result", "Files"), "Rename Selected Files", "Nothing to report.", find_dat_verify,
            (("dat", "file", "DAT file", "", "Blank = skip this step",
              "DAT files (*.dat *.xml);;All files (*)"),)),
    OrgTool("bioscheck", "Check BIOS", "Check BIOS files",
            "Check your BIOS files against a list you supply (JSON: system → files with MD5). Missing or wrong files are listed; nothing is changed.", ("System", "Found"), "Nothing to apply",
            "Nothing to report.", find_bios,
            (("biosdb", "file", "BIOS list (.json)", "", "Blank = skip this step",
              "BIOS lists (*.json);;All files (*)"),
             ("biosdir", "folder", "BIOS folder", "", "Blank = the scanned folder"))),
    OrgTool("report", "Library report", "Library report",
            "List every game with its system, format, region, size and folder, then save it as a web page or "
            "spreadsheet. Untick a system to leave it out.",
            ("System", "Files"), "Save Report...", "No games found.", find_library, export=True),
)}


# ---- Pages: one or more tools that belong together ----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OrgPage:
    """A sidebar page. A page with several steps scans them in order and shows the first one that has something to
    do; apply it and the next scan moves on to the following step, so steps never act on the same files at once."""
    key: str
    nav: str  # sidebar label
    title: str
    blurb: str  # under the page title: what it is for, and when to use it
    steps: tuple[OrgTool, ...]

    @property
    def multi(self) -> bool:
        return len(self.steps) > 1


ORG_PAGES: tuple[OrgPage, ...] = (
    OrgPage("fix", "Fix Names & Discs", "Fix Names & Discs",
            "Use this first. One step at a time, in order: tidy filenames, group multi-disc games with a playlist, "
            "fix broken playlists, repair disc images.",
            (ORG_TOOLS["tidy"], ORG_TOOLS["multi"], ORG_TOOLS["playlists"], ORG_TOOLS["discfix"])),
    OrgPage("extras", "Remove Extras / Junk", "Remove Extras & Junk",
            "Use this to free space. One step at a time, in order: leftovers, duplicate games, extra releases. "
            "Nothing is removed until you tick it and confirm.",
            (ORG_TOOLS["cleanup"], ORG_TOOLS["dupes"], ORG_TOOLS["onegr"])),
    OrgPage("patch", "Patch ROMs", "Patch ROMs",
            ORG_TOOLS["patch"].blurb, (ORG_TOOLS["patch"],)),
    OrgPage("bysystem", "Sort by System", "Sort by System",
            ORG_TOOLS["bysystem"].blurb + " Best done last.", (ORG_TOOLS["bysystem"],)),
    OrgPage("report", "Library Report", "Library Report",
            ORG_TOOLS["report"].blurb + " Changes nothing. Add a DAT or BIOS list to check against; "
            "with no path, that step is skipped.", (ORG_TOOLS["datcheck"], ORG_TOOLS["bioscheck"], ORG_TOOLS["report"])),
)

PAGES_BY_KEY: dict[str, OrgPage] = {p.key: p for p in ORG_PAGES}


def scan_page(page: OrgPage, root: Path, recursive: bool, opts: dict,
              stop: threading.Event) -> tuple[list[Finding], str, str]:
    """Scan the page's steps in order (only those named in opts['_steps'], default all) and return the first step's
    findings that has any, as (findings, message, key of that step). When no step has anything to do the findings are
    empty and the key is the last step scanned."""
    wanted = opts.get("_steps")
    steps = [t for t in page.steps if wanted is None or t.key in wanted]
    if not steps:
        return [], "Tick at least one step to scan.", page.steps[0].key
    skipped: list[str] = []
    message = ""
    for step in steps:
        if stop.is_set():
            break
        findings, message = step.scan(root, recursive, opts, stop)
        if findings:
            if skipped:
                message = (message + "  |  " if message else "") + "Nothing to do: " + ", ".join(skipped)
            return findings, message, step.key
        skipped.append(step.title.lower())
    return [], ("Checked: " + ", ".join(skipped)) if page.multi and skipped else message, steps[-1].key


def newest_undo_step(keys: list[str]) -> str | None:
    """Of these tools, the one whose most recent run is the newest (so one Undo button can cover a whole page)."""
    newest: tuple[str, str] | None = None
    for key in keys:
        runs = _load_runs(key)
        if runs and (newest is None or str(runs[-1].get("made", "")) > newest[0]):
            newest = (str(runs[-1].get("made", "")), key)
    return newest[1] if newest else None
