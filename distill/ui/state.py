"""Definitions shared by the window and its page mixins: tool table, per-page state."""
from __future__ import annotations

import shutil
from typing import Callable, TYPE_CHECKING

from PySide6.QtWidgets import QLabel, QPushButton

from .treeslide import TreeSlide
from .widgets import AnimCheckBox, CheckTree, PathRow, RevealDelegate, StepCard, SwitchToggle

if TYPE_CHECKING:
    from ..organize import Finding
    from ..orgpages import OrgPage, OrgTool


# --- The window -----------------------------------------------------------------------------------
# The conversion tools the window knows about: event prefix -> (display name, names to look for on PATH).
TOOLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chdman": ("chdman", ("chdman",)),
    "dolphin": ("DolphinTool", ("dolphin-tool", "DolphinTool")),
    "maxcso": ("maxcso", ("maxcso",)),
    "shadplus": ("ShadPS4Plus PKG_Extractor", ("pkg_extractor",)),
    "anyps5": ("pkg-to-anyps5", ("pkg-to-anyps5",)),
    "zarchive": ("ZArchive", ("zarchive",)),
    "ps3qdd": ("PS3QDD (PS3Dec)", ("ps3dec",)),
    "xdvdfs": ("xdvdfs", ("xdvdfs",)),
    "relinker": ("the AnyPS5 relinker", ()),
    "nscb": ("nscb_rust", ("nscb_rust", "nscb")),
    "hactoolnet": ("hactoolnet", ("hactoolnet",)),
    "nup": ("nup", ("nup",)),
    "ytdlp": ("yt-dlp", ("yt-dlp",)),
    "ffmpeg": ("ffmpeg", ("ffmpeg",)),
}

# Downloaded in the background like the rest, but nothing waits for them: a batch conversion can start while they
# are still coming down, and the footer markers (not the sidebar dots) show how they are doing.
DOWNLOAD_TOOLS = ("ytdlp", "ffmpeg")


# The Settings window lists every tool once, in two groups: tool key -> label.
TOOL_GROUPS: dict[str, dict[str, str]] = {
    "Disc Image Tools": {"chdman": "CHDMan", "dolphin": "DolphinTool", "maxcso": "maxcso", "xdvdfs": "xdvdfs"},
    "PlayStation Tools": {"shadplus": "ShadPS4Plus PKG_Extractor", "anyps5": "pkg-to-anyps5",
                          "relinker": "AnyPS5 relinker", "zarchive": "ZArchive (.zar)", "ps3qdd": "PS3Dec (PS3QDD)"},
    "Switch Tools": {"nscb": "nscb_rust (NSZ / XCZ / merge)", "hactoolnet": "hactoolnet (decrypts NSP / XCI)",
                     "nup": "nup (.nup packer)"},
    "Download Tools": {"ytdlp": "yt-dlp", "ffmpeg": "ffmpeg"},
}


LABEL_W = 150  # one label column for every form, so fields line up from page to page


def _which_any(names: tuple[str, ...]) -> str:
    for name in names:
        if found := shutil.which(name):
            return found
    return ""


class TabUI:
    """The widgets of one conversion page, so CHD, RVZ, CSO ... each remember their own folders and options."""

    def __init__(self, mode: str, always_checked: bool):
        self.source = PathRow("Source folder", label_width=LABEL_W)
        self.dest = PathRow("Destination folder", label_width=LABEL_W)
        self.extract = AnimCheckBox("Extract archives first")
        self.extract.setChecked(True)
        self.verify = AnimCheckBox()
        self.verify.setChecked(True)
        if always_checked:
            self.verify.setEnabled(False)
            self.verify.setToolTip("This page always verifies its results, so the box is fixed.")
        self.delete = AnimCheckBox()


class OrgState:
    """One Organize page: its last scan, what is ticked, its options and its widgets."""

    def __init__(self, page: OrgPage):
        self.page = page
        self.tool: OrgTool = page.steps[0]  # the step whose findings are showing (a multi-step page moves along)
        self.rows: dict[str, PathRow] = {}  # option name -> its file / folder box (a dropped DAT or BIOS list fills one)
        self.step_boxes: dict[str, SwitchToggle] = {}  # step key -> the switch on its card (multi-step pages only)
        self.step_cards: dict[str, StepCard] = {}  # step key -> its card
        self.steps_label: QLabel | None = None
        self.findings: list[Finding] = []
        self.checked: set[int] = set()
        self.patch_ticks: set[tuple[int, int]] = set()  # patch page: (game row, patch number) pairs that are ticked
        self.patch_names: dict[tuple[str, ...], str] = {}  # patch page: output names chosen by hand
        self.manual: list[tuple[str, str]] = []  # patch page: (patch, ROM) pairs chosen by hand; '' ROM = left unmatched
        self.token = 0
        self.message = ""
        self.opts: dict[str, Callable[[], object]] = {}
        self.tree: CheckTree | None = None
        self.reveal: RevealDelegate | None = None  # new scan results ripple in, row by row
        self.slide: TreeSlide | None = None  # a finding's details are written in when it is opened
        self.status: QLabel | None = None
        self.scan_btn: QPushButton | None = None
        self.undo_btn: QPushButton | None = None
        self.report_btn: QPushButton | None = None
