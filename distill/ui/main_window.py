"""The main window: layout, sidebar navigation and theming."""
from __future__ import annotations

import sys
import os
import queue
import shutil
import threading
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEasingCurve, QEvent, QRect, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QLineEdit,
    QApplication, QButtonGroup, QCheckBox, QFrame,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSplitter,
    QVBoxLayout, QWidget,
)

from ..constants import APP_DIR, APP_NAME, APP_VERSION, RVZ_MAX_LEVEL
from ..downloads import DownloadRunner, load_logins, LOGINS
from ..netrules import load_rules
from ..ytdlp import load_settings as load_ytdlp_settings
from ..orgpages import ORG_PAGES
from ..runview import DL, RunFacts, bar_targets, controls, view_of
from .update_prompt import UpdatePromptMixin
from ..torbox import torbox_key
from .. import debrid
from ..system import resolve_tool_path
from ..tools import (
    AnyPS5Manager, ChdmanManager, DolphinToolManager, FfmpegManager, MaxCsoManager,
    HactoolnetManager, NscbManager, NupManager, PkgExtractorManager, PkgToAnyPs5Manager, Ps3QddManager,
    XdvdfsManager, YtdlpManager, ZArchiveManager,
)
from .dialogs import TorBoxDownloadsDialog
from .download_page import DownloadMixin
from .dropglow import DropGlowFilter
from .dropper import DropMixin
from .feedback import FeedbackMixin
from .icon import make_app_icon
from .models import DlModel, DlProxy
from .monitor import ResourceMonitor
from .now_playing import NowPlaying
from .emu_cards import TwoColumn
from .nav import NAV_GROUPS, PAGE_CHIP, PAGE_DETAIL, PAGE_INFO
from .organize_page import OrganizeMixin
from .patch_view import PatchViewMixin
from .emulator_page import EmulatorMixin
from .games_page import GamesMixin
from .recomp_page import RecompMixin
from . import persist, msgbox
from .persist import PersistMixin
from .soundtrack_page import SoundtrackMixin
from .switch_page import SwitchMixin
from .run_panel import RunMixin
from .settings_page import SettingsMixin
from .state import _which_any, DOWNLOAD_TOOLS, LABEL_W, OrgState, TabUI, TOOLS
from .theme import T
from .tooltips import TooltipWrapper
from . import icons, motion
from .widgets import (ThemeWipe, AuraButton, Banner, Card, EdgeHotZone, Expander, GearButton, hint, IconButton, Logo, NavButton,
                      NavHeader, PathRow, PressButton, RunBar, SideHost, SideShadow, SizedStack, ToolMarkers, ToolRow)

if TYPE_CHECKING:
    from ..batch import BatchRunner
    from ..models import Config


# the Start button's text for each conversion page: "Start" and what the page does, in the same words as its title
START_LABELS = {
    "chd": "Start CHD Compression", "cso": "Start CSO Compression", "ps3": "Start PS3 Conversion",
    "pkg": "Start PS4 PKG Extraction", "pkg5": "Start PS5 PKG Extraction", "rvz": "Start RVZ Compression",
    "xbox": "Start Xbox Repack", "zar": "Start ZAR Compression",
}
# ...and what it says while that page's run is going (the button is greyed out then, so it reads as a status)
BUSY_LABELS = {
    "chd": "Compressing...", "cso": "Compressing...", "rvz": "Compressing...", "zar": "Compressing...",
    "ps3": "Converting...", "pkg": "Extracting...", "pkg5": "Extracting...", "xbox": "Repacking...",
    "dl": "Downloading...", "sound": "Saving...",
}


class DistillWindow(DropMixin, RunMixin, OrganizeMixin, PatchViewMixin, DownloadMixin, SoundtrackMixin, GamesMixin, RecompMixin, EmulatorMixin, SwitchMixin, SettingsMixin, FeedbackMixin, PersistMixin, UpdatePromptMixin,
                     QMainWindow):
    MODES = ("chd", "rvz", "cso", "pkg", "pkg5", "zar", "ps3", "xbox")  # pkg5: the PS5 page, which runs the "pkg" engine mode
    SIDEBAR_WIDTH = 268
    CARD_GAP = 12  # space around the floating sidebar and command bar
    FOOTER_H = 64  # sidebar footer and command bar share this height so their top borders line up
    PAGE_MARGIN = 28

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(make_app_icon())
        self._base_title = self.windowTitle()  # the title bar shows progress in front of this while a run is going
        self._tray = None
        self._persist_setup()
        self._why_setup()
        self._aura_setup()
        self._tip_filter = TooltipWrapper(self)  # long tooltips wrap instead of running across the screen
        QApplication.instance().installEventFilter(self._tip_filter)
        self.resize(1240, 880)
        self.setMinimumSize(1000, 600)
        self.setAcceptDrops(True)  # .torrent files and magnet links dropped on the Download page
        self._drop_glow = DropGlowFilter(self)  # the glowing, softened window while something is dragged over it

        self.is_processing = False
        self.is_paused = False
        self._stopping = False
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.runner: BatchRunner | None = None  # the conversion (or other tool job) run: one at a time
        self._last_cfg: Config | None = None
        self._work = (0, 0)  # (bytes done, bytes to do) from the runner
        self._samples: deque[tuple[float, int]] = deque(maxlen=12)  # (active seconds, bytes done)
        self._t0: float | None = None
        self._paused_total, self._pause_t, self._eta_tick = 0.0, None, 0.0
        # The download run (games, soundtracks) has a state of its own, so a download and a conversion can go at the same time.
        self.dl_runner: DownloadRunner | None = None
        self.dl_paused = False
        self.dl_stopping = False
        self.dl_pause_event = threading.Event()
        self.dl_pause_event.set()
        self._dl_t0: float | None = None
        self._dl_paused_total, self._dl_pause_t, self._dl_eta_tick = 0.0, None, 0.0
        self._dl_page, self._dl_label, self._dl_prog_text = "dl", "Downloading...", "Download Progress: 0 / 0"
        self._settings_visible = True
        self._theming = False
        self._qss = ""
        self.ui_q: queue.SimpleQueue = queue.SimpleQueue()
        self.sys_threads = os.cpu_count() or 4
        self._tasks: dict[int, list[int]] = {}  # log task id -> [first block, line count]

        # tool paths: one setting per tool, mirrored in every page that shows it
        self._tool_rows: dict[str, list[ToolRow]] = {key: [] for key in TOOLS}
        self._auto: dict[str, str] = {key: _which_any(names) for key, (_n, names) in TOOLS.items()}  # last value the app (not the user) put in the box
        self._auto_ps3_keys = ""
        self._pending_tools = set(TOOLS) - set(DOWNLOAD_TOOLS)  # the conversion tools the Start button waits for
        self._pending_dl = set(DOWNLOAD_TOOLS)  # yt-dlp / ffmpeg still being checked
        self._installing: set[str] = set()  # ... and those being downloaded right now

        self.current_page = "recomps"  # opens on PC Recomps; a saved page from the last run still wins
        self.tabs: dict[str, TabUI] = {}
        self._make_settings_widgets()
        self._sw_make_widgets()  # before Settings is built: the key files are shown there
        self.nav_items: dict[str, NavButton] = {}
        self.nav_groups: dict[str, dict] = {}

        # Organisation pages share one folder; each tool keeps its own results and options
        self.org: dict[str, OrgState] = {}
        self.org_busy = False
        self._org_stop = threading.Event()
        self._org_sources: list[PathRow] = []
        self._org_recursive: list[QCheckBox] = []

        # Download page
        self.dl_remember = load_logins()  # logins are asked for when a download starts
        load_rules()  # speed limit, schedule and per-site limits
        load_ytdlp_settings()  # video-site quality
        self._dl_token = 0  # bumped by every Fetch, so late replies for an old list are ignored
        self._st_token = 0  # same for the Soundtracks page's searches
        self._st_rows: list = []  # its games, one SoundRow each
        self._st_player = None  # the preview player, made on first use
        self._st_output = None
        self._st_queue: list = []  # (title, game, url) of the tracks being stepped through
        self._st_index = -1  # which of them is playing; -1 when nothing is
        self._st_art_cache: dict = {}  # cover address -> QPixmap
        self._st_art_loading: set = set()
        self._dl_next: list[int] = []  # rows lined up to start when the current run ends
        self._dl_epoch = 0  # bumped whenever rows are renumbered, so a slow start-up disk scan can't mark the wrong ones
        self._dl_cleared = False  # the person emptied the list (so the saved copy goes too)
        self._dl_samples: deque[tuple[float, int]] = deque()
        self.dl_model, self.dl_proxy = DlModel(), DlProxy()
        self.dl_proxy.setSourceModel(self.dl_model)
        self._dl_filter_timer = QTimer(self)
        self._dl_filter_timer.setSingleShot(True)
        self._dl_filter_timer.setInterval(250)  # debounce: one rebuild after typing pauses
        self._dl_filter_timer.timeout.connect(self._dl_apply_filter)
        self._dl_save_timer = QTimer(self)
        self._dl_save_timer.setSingleShot(True)
        self._dl_save_timer.setInterval(1500)
        self._dl_save_timer.timeout.connect(self._dl_save_session)

        self._build_ui()
        self._apply_theme()
        self._dl_restore_session()
        self._first_run = not persist.SETTINGS_FILE.exists() and not any(APP_DIR.glob("distill_*.json"))
        self._settings_restore()
        QTimer.singleShot(600, self._maybe_whats_new)
        QTimer.singleShot(2500, self._update_check_start)  # asks GitHub in the background; prompts only if a newer release exists
        self.start_btn.setEnabled(False)  # enabled once the tool checks have finished
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_events)
        self._poll.start(50)
        QApplication.styleHints().colorSchemeChanged.connect(lambda *_: QTimer.singleShot(60, self._apply_theme))
        for manager in (ChdmanManager, DolphinToolManager, MaxCsoManager, PkgExtractorManager,
                        PkgToAnyPs5Manager, ZArchiveManager, Ps3QddManager, XdvdfsManager, AnyPS5Manager,
                        NscbManager, HactoolnetManager, NupManager, YtdlpManager, FfmpegManager):
            threading.Thread(target=manager(self.ui_q.put).run, daemon=True).start()

    def _apply_theme(self, palette: bool = True) -> None:
        if self._theming:
            return
        self._theming = True
        try:
            T.refresh()
            if palette:
                self._apply_palette()
            qss = self._qss_cache.get(T.dark) or self._qss_cache.setdefault(T.dark, T.qss())  # built once per look
            if qss != self._qss:
                self._qss = qss
                QApplication.instance().setStyleSheet(qss)
            T.restyle()  # widgets that were given colours of their own when they were built
            self._remap_item_colours()  # text colours stored on list rows when they were made
            for nav in self.nav_items.values():
                nav.update()
            if hasattr(self, "settings_nav"):
                self.settings_nav.update()
            for header in (g["header"] for g in self.nav_groups.values()):
                header.update()
            self.centralWidget().update()
            if hasattr(self, "run_bar"):
                self.run_bar.update()
            if hasattr(self, "logo"):
                self.logo.refresh_theme()  # the bright icon in the light look, the dark one in the dark look
            self.setWindowIcon(make_app_icon())
            if hasattr(self, "_log_hl"):
                self._log_hl.refresh_colours()
            if hasattr(self, "now_playing"):
                self.now_playing.refresh_theme()
        finally:
            self._theming = False

    def _remap_item_colours(self) -> None:
        """Rows in the lists carry text colours of their own (a game's name, a status, a Disc header), set when the row was
        made. Move each from the old look's colour to the new look's, or light-grey dark-theme text stays on a white list."""
        from PySide6.QtGui import QBrush
        from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItemIterator
        for tree in self.findChildren(QTreeWidget):
            was = tree.blockSignals(True)  # recolouring is not an edit: keep itemChanged handlers out of it
            try:
                it = QTreeWidgetItemIterator(tree)
                while (item := it.value()) is not None:
                    for col in range(tree.columnCount()):
                        brush = item.foreground(col)
                        if brush.style() != Qt.BrushStyle.NoBrush and (new := T.remap(brush.color())) is not None:
                            item.setForeground(col, QBrush(new))
                    it += 1
            finally:
                tree.blockSignals(was)
            tree.viewport().update()

    _qss_cache: dict = {}

    def _apply_palette(self) -> None:
        """The Qt palette for native pieces (message boxes, file dialogs, menus) and for the areas the style sheet leaves to it,
        such as the background behind a scrolled page. It must go in BEFORE the style sheet: the sheet saves each widget's
        palette as it styles it, so a palette set afterwards leaves those widgets on the old theme's colours."""
        if QApplication.instance().palette() != (pal := T.palette()):
            QApplication.instance().setPalette(pal)

    def _sync_logo_tip(self) -> None:
        self.logo.setToolTip("Switch to the light look" if T.dark else "Switch to the dark look")

    def _toggle_theme(self) -> None:
        """Dark <-> light. The window as it is now is photographed and laid over the new look, then wiped away from the top left
        to the bottom right (no wipe when motion is reduced)."""
        if getattr(self, "_wipe", None) is not None:
            return  # one change at a time
        wipe = None
        if motion.on() and self.isVisible() and not self.isMinimized():
            wipe = ThemeWipe(self, self.grab(), self._wipe_done)
            self._wipe = wipe
            wipe.show()
            wipe.raise_()
        T.dark = not T.dark
        self.setUpdatesEnabled(False)  # one repaint at the end, not one per restyled widget
        try:
            self._apply_theme()
            self._tint_title_bar()
        finally:
            self.setUpdatesEnabled(True)
        self._sync_logo_tip()
        self._persist_soon()
        if wipe is not None:
            wipe.start()
        else:
            self._theme_refill()

    def _theme_refill(self) -> None:
        for refill in ("_em_fill", "_em_refresh_detail", "_rc_fill", "_rc_refresh_detail"):  # cards coloured from the theme when filled
            try:
                getattr(self, refill)()
            except Exception:
                pass

    def _wipe_done(self) -> None:
        self._wipe = None
        self._theme_refill()  # after the sweep, so it starts at once

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not getattr(self, "_title_tinted", False):
            self._title_tinted = True
            self._tint_title_bar()

    def _tint_title_bar(self) -> None:
        """Windows 10/11: ask for a title bar to match the theme, and on Windows 11 colour it like the sidebar. Best effort: a
        failure leaves the normal title bar."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = wintypes.HWND(int(self.winId()))
            dwm = ctypes.windll.dwmapi

            def set_attr(attr: int, value: int) -> None:
                v = ctypes.c_int(value)
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

            set_attr(20, 1 if T.dark else 0)  # DWMWA_USE_IMMERSIVE_DARK_MODE
            c = T.bg  # the same colour as the page, so the title bar melts into it
            set_attr(35, c.red() | (c.green() << 8) | (c.blue() << 16))  # DWMWA_CAPTION_COLOR (Windows 11)
            set_attr(36, T.fg.red() | (T.fg.green() << 8) | (T.fg.blue() << 16))  # DWMWA_TEXT_COLOR
        except Exception:
            pass

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ThemeChange) and not self._theming:
            QTimer.singleShot(0, self._apply_theme)
        super().changeEvent(event)

    def _info(self, title: str, text: str) -> None:
        msgbox.information(self, title, text)

    def _warn(self, title: str, text: str) -> None:
        msgbox.warning(self, title, text)

    def _error(self, title: str, text: str) -> None:
        msgbox.critical(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        return msgbox.question(self, title, text, yes | no, yes) == yes

    @staticmethod
    def _clean(text: str) -> str:
        return text.strip(" \"'")

    def closeEvent(self, event) -> None:
        if self.org_busy and not self._ask(
                "Confirm Exit", "Games are still being moved into folders.\n\nExit anyway? (Use Undo Last Run "
                                "next time to reverse a half-finished run.)"):
            event.ignore()
            return
        if self.is_processing and not self._ask(
                "Confirm Exit", "A batch is currently running!\n\nClosing now will instantly abort it. "
                                "Active conversions are stopped and partial files deleted. Are you sure?"):
            event.ignore()
            return
        if self.dl_runner is not None and not self._ask(
                "Confirm Exit", "A download is currently running!\n\nClosing now will stop it. "
                                "Partly downloaded files are kept and resume next time. Are you sure?"):
            event.ignore()
            return
        if self.is_processing and self.runner:  # both questions are asked first, so saying no to either leaves everything running
            self.runner.shutdown()
        if self.dl_runner is not None:
            self.dl_runner.shutdown()
        self._settings_save()
        self._dl_save_session()
        event.accept()
        self.hide()
        QApplication.processEvents()
        os._exit(0)  # worker threads may still be inside a (non-interruptible) extraction

    def _build_ui(self) -> None:
        self._build_settings_dialog()  # tool paths and download options live here, not on each page
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = self._body = SideHost()  # the sidebar and the page; the run bar sits under both
        outer.addWidget(body, 1)
        self.run_bar = RunBar()  # while a job runs and another page is showing: what it is and how far it has got
        self.run_bar.clicked.connect(self._run_bar_clicked)
        self.run_bar.hide()
        outer.addWidget(self.run_bar)
        root = QHBoxLayout(body)
        root.setContentsMargins(0, self.CARD_GAP, 0, self.CARD_GAP)
        root.setSpacing(0)
        self._side_dock = QWidget()  # keeps the sidebar's room open beside the page; closes up in compact mode
        self._side_dock.setFixedWidth(self.CARD_GAP + self.SIDEBAR_WIDTH)
        root.addWidget(self._side_dock)
        self._side = self._build_sidebar()  # a floating card, placed by hand over the body (see _place_sidebar)
        self._side.setParent(body)
        self._side_setup(body)

        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        root.addWidget(content, 1)

        # header: page title and blurb, and the settings fold-away
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(self.PAGE_MARGIN, 22, self.PAGE_MARGIN, 10)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.page_chip = QLabel()  # the small label above the title: what kind of tool this is
        self.page_chip.setObjectName("chip")
        titles.addWidget(self.page_chip, 0, Qt.AlignmentFlag.AlignLeft)
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        self.page_blurb = QLabel()
        self.page_blurb.setObjectName("muted")
        self.page_blurb.setWordWrap(True)
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_blurb)
        hl.addLayout(titles, 1)
        self.settings_btn = QPushButton("Hide options")
        self.settings_btn.setToolTip("Fold away this page's options to give the log more room")
        self.settings_btn.clicked.connect(lambda: self._set_settings_visible(not self._settings_visible))
        hl.addWidget(self.settings_btn, 0, Qt.AlignmentFlag.AlignTop)
        col.addWidget(header)
        self.banner = Banner()  # notices that only need a glance; shown between the title and the page
        banner_wrap = QWidget()
        bw = QHBoxLayout(banner_wrap)
        bw.setContentsMargins(self.PAGE_MARGIN, 0, self.PAGE_MARGIN, 8)
        bw.addWidget(self.banner)
        col.addWidget(banner_wrap)

        # settings (top) and the live run panel (bottom) share the space through a splitter
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)
        self.stack = SizedStack()
        self.perf_slots: dict[str, QVBoxLayout] = {}  # each split page's right column: the shared Performance card moves into the one showing
        self.pages: dict[str, QWidget] = {mode: self._build_tab(mode) for mode in self.MODES}
        for org_page in ORG_PAGES:
            self.pages[org_page.key] = self._build_org_page(org_page)
        self.pages["dl"] = self._build_dl_page()
        self.pages["sound"] = self._build_sound_page()  # after the download page: it shares that page's Save-to folder
        self.pages["switch"] = self._build_switch_page()
        self.pages["games"] = self._build_games_page()
        self.pages["recomps"] = self._build_recomp_page()
        self.pages["emus"] = self._build_emulator_page()
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.perf = self._build_perf_card()
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 10, 4)
        il.setSpacing(10)
        il.addWidget(self.stack)
        self.perf.setParent(inner)  # lives inside the showing page's right column (see _place_perf)
        self.perf.hide()
        self.perf.header.setChecked(True)
        il.addStretch(1)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setWidget(inner)
        self.splitter.addWidget(self.settings_scroll)
        self.splitter.addWidget(self._build_run_panel())
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([480, 320])
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(self.PAGE_MARGIN, 0, self.PAGE_MARGIN - 8, 14)
        wl.addWidget(self.splitter)
        col.addWidget(wrap, 1)

        # command bar
        bar = QWidget()
        bar.setObjectName("commandBar")
        bl = QHBoxLayout(bar)
        bar.setFixedHeight(self.FOOTER_H)
        bl.setContentsMargins(16 - AuraButton.GLOW, 2, 16, 2)  # the glow needs room
        bl.setSpacing(10)
        self.start_btn = AuraButton("Start Batch Conversion")  # drifting gradient and glow while a batch runs
        self.start_btn.clicked.connect(self._start_processing)
        self.pause_btn = PressButton("Pause")
        self.pause_btn.setFixedHeight(40)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn = PressButton("Stop", danger=True)
        self.stop_btn.setFixedHeight(40)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_run)
        bl.addWidget(self.start_btn, 3)
        bl.addWidget(self.pause_btn, 1, Qt.AlignmentFlag.AlignVCenter)
        bl.addWidget(self.stop_btn, 1, Qt.AlignmentFlag.AlignVCenter)
        self.markers = ToolMarkers()  # yt-dlp / ffmpeg / default downloader: red, blinking amber, green
        self.markers.clicked.connect(self._marker_clicked)  # TorBox, once it has a key, is a chip that lists the account
        bl.addWidget(self.markers, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        self._marker_timer = QTimer(self)  # picks up a debrid key being added or a tool path being edited
        self._marker_timer.setInterval(1000)
        self._marker_timer.timeout.connect(self._refresh_markers)
        self._marker_timer.start()
        self._refresh_markers()
        bar_wrap = QWidget()  # the bar floats as a card, as wide as the page's own cards
        bar_lay = QHBoxLayout(bar_wrap)
        bar_lay.setContentsMargins(self.PAGE_MARGIN, 0, self.PAGE_MARGIN, self.CARD_GAP)
        bar_lay.addWidget(bar)
        self.bar_wrap = bar_wrap  # the PC Recomps page has no batch to start, so it hides the bar
        col.addWidget(bar_wrap)
        for seq, button in (("Ctrl+Return", self.start_btn), ("Ctrl+P", self.pause_btn), ("Ctrl+.", self.stop_btn)):
            QShortcut(QKeySequence(seq), self, activated=button.click)  # click() is a no-op while disabled
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self._open_settings())
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self._open_goto)
        for n in range(1, 10):  # Ctrl+1 ... Ctrl+9 open the first nine pinned pages, in sidebar order
            QShortcut(QKeySequence(f"Ctrl+{n}"), self, activated=lambda n=n: self._nav_nth(n))
        QShortcut(QKeySequence("Ctrl+B"), self, activated=lambda: self.compact_box.toggle())  # compact sidebar on / off
        self._explain(self.pause_btn, "Ctrl+P: finish active jobs, start no new ones", "Nothing is running.")
        self._explain(self.stop_btn, "Ctrl+.: abort the run (finished items are kept)", "Nothing is running.")
        self.start_btn.setToolTip("Ctrl+Enter")

        self._refresh_nav_dots()
        self._apply_pins()
        self._place_sidebar()
        self._side_raise()
        self._show_page(self.current_page)

    def _set_settings_visible(self, visible: bool) -> None:
        if visible == self._settings_visible or (not visible and not self._log_shown):
            return  # pages without a log keep their options on screen
        self._settings_visible = visible
        self.settings_scroll.setVisible(visible)
        self.settings_btn.setText("Hide options" if visible else "Show options")

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(self.SIDEBAR_WIDTH)
        outer = QVBoxLayout(side)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(10, 20 - Logo.GLOW, 4, 0)
        lay.setSpacing(0)
        outer.addWidget(content, 1)

        brand = QHBoxLayout()
        brand.setContentsMargins(8 - Logo.GLOW, 0, 0, 12 - Logo.GLOW)  # the logo keeps room around itself for its glow
        brand.setSpacing(12 - Logo.GLOW)
        self.logo = Logo()  # click it to change between the dark and light looks
        self.logo.clicked.connect(self._toggle_theme)
        self._sync_logo_tip()
        brand.addWidget(self.logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel(APP_NAME)
        name.setObjectName("brandName")
        count = len([k for k in self.pages_order() if not k.startswith("act_")])
        sub = self._brand_sub = QLabel(f"{APP_VERSION}  |  {count} tools")
        sub.setObjectName("hint")
        names.addWidget(name)
        names.addWidget(sub)
        brand.addLayout(names)
        brand.addStretch(1)
        self.side_toggle = IconButton("sidebar")  # compact mode on / off
        self.side_toggle.clicked.connect(lambda: self.compact_box.toggle())
        self._sync_compact_ui()
        brand.addWidget(self.side_toggle, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(brand)

        self.nav_search = QLineEdit()  # filters the tools below; Enter opens the first match
        self.nav_search.setPlaceholderText("Quick search tools...")
        self.nav_search.setClearButtonEnabled(True)
        search_action = self.nav_search.addAction(icons.icon("search", T.fg3, 16), QLineEdit.ActionPosition.LeadingPosition)
        T.bind(self.nav_search, lambda: search_action.setIcon(icons.icon("search", T.fg3, 16)))
        self.nav_search.textChanged.connect(self._filter_nav)
        self.nav_search.returnPressed.connect(self._nav_search_open)
        self.settings_nav = GearButton("")
        self.settings_nav.setToolTip("Settings: tools, downloads, accounts and torrent privacy (Ctrl+,)")
        self.settings_nav.clicked.connect(lambda: self._open_settings())
        search_row = QHBoxLayout()
        search_row.setSpacing(4)
        search_row.addWidget(self.nav_search, 1)
        search_row.addWidget(self.settings_nav)
        lay.addLayout(search_row)
        lay.addSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(6, 6, 6, 6)  # breathing room inside the rounded well the list sits in
        il.setSpacing(1)
        for group, items in NAV_GROUPS:
            header = NavHeader(group)
            body = QWidget()
            bl = QVBoxLayout(body)
            bl.setContentsMargins(0, 0, 0, 4)
            bl.setSpacing(1)
            self.nav_groups[group] = {"header": header, "body": body}
            header.clicked.connect(lambda _c=False, g=group: self._toggle_group(g))
            il.addWidget(header)
            il.addWidget(body)
            for key, text in items:
                if key.startswith("act_"):  # Logins / Torrent privacy now live in Settings
                    continue
                item = NavButton(key, text, "accent")
                item.clicked.connect(lambda _c=False, k=key: self._nav_click(k))
                self.nav_items[key] = item
                bl.addWidget(item)
        il.addStretch(1)
        scroll.setWidget(inner)
        inner.setAutoFillBackground(False)  # QScrollArea.setWidget switches it on, which paints a square block over the card
        well = QWidget()  # the list sits in a rounded well, like the cards, instead of a square-cornered block
        well.setObjectName("navWell")
        wl = QVBoxLayout(well)
        wl.setContentsMargins(1, 1, 1, 1)  # inside the well's 1px border
        wl.setSpacing(0)
        wl.addWidget(scroll)
        lay.addWidget(well, 1)
        lay.addSpacing(8)  # so the well's lower corners clear the footer line
        footer = QWidget()
        footer.setObjectName("sideFooter")
        footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        footer.setFixedHeight(48)
        bottom = QHBoxLayout(footer)
        bottom.setContentsMargins(10, 0, 10, 0)
        bottom.setSpacing(2)
        bottom.addWidget(ResourceMonitor(), 1)
        self.now_playing = NowPlaying()  # shown only while a soundtrack preview plays
        self.now_playing.setVisible(False)
        self.now_playing.skipBack.connect(lambda: self._st_step(-1))
        self.now_playing.skipForward.connect(lambda: self._st_step(1))
        self.now_playing.togglePlay.connect(self._st_toggle_pause)
        self.now_playing.stopped.connect(self._st_stop)
        self.now_playing.volumeChanged.connect(self._st_set_volume)
        self.now_playing.scratchStarted.connect(self._st_scratch_start)  # hold the disc to stop it, turn it to wind the track
        self.now_playing.scratched.connect(self._st_scratch_move)
        self.now_playing.scratchEnded.connect(self._st_scratch_end)
        self.now_playing.seekTo.connect(self._st_seek)
        self.now_playing.opened.connect(lambda: self._nav_click("sound"))
        outer.addWidget(self.now_playing)
        outer.addWidget(footer)
        self._refresh_nav_dots()
        return side

    @staticmethod
    def pages_order() -> list[str]:
        return [key for _group, items in NAV_GROUPS for key, _text in items]


    # ---- the sidebar: pinned beside the page, or compact (out of the way until the mouse touches the left edge) ----------
    def _side_setup(self, body: QWidget) -> None:
        """The sidebar floats over `body` rather than sitting in its layout. A spacer (the dock) holds its room open while it is
        pinned and closes up when it is compact, so the page grows to fill the window; the hover reveal then slides the
        sidebar out over the page without moving it."""
        self._compact = False
        self._side_open = 1.0  # how far out the sidebar is: 0 = away to the left, 1 = fully showing
        self._away_ticks = 0
        self._side_anim = QVariantAnimation(self)
        self._side_anim.valueChanged.connect(self._side_step)
        self._dock_anim = QVariantAnimation(self)
        self._dock_anim.valueChanged.connect(lambda v: self._side_dock.setFixedWidth(int(v)))
        self._shadow = SideShadow(body)
        self._shadow.hide()
        self._hot = EdgeHotZone(body)
        self._hot.hide()
        self._hot_timer = QTimer(self)  # a short dwell, so brushing past the edge doesn't pop it open
        self._hot_timer.setSingleShot(True)
        self._hot_timer.setInterval(90)
        self._hot_timer.timeout.connect(self._reveal_side)
        self._hot.entered.connect(lambda: self._hot_timer.start())
        self._hot.left.connect(lambda: self._hot_timer.stop())
        self._watch = QTimer(self)  # while the sidebar is out: has the mouse gone away from it?
        self._watch.setInterval(120)
        self._watch.timeout.connect(self._side_watch_tick)
        body.resized.connect(self._place_sidebar)

    def _side_raise(self) -> None:
        for widget in (self._hot, self._shadow, self._side):  # in this order: the hot strip lowest, the sidebar on top
            widget.raise_()

    def _place_sidebar(self) -> None:
        if not hasattr(self, "_side_anim"):
            return
        gap, width, body, side = self.CARD_GAP, self.SIDEBAR_WIDTH, self._body, self._side
        x = round(gap + (self._side_open - 1.0) * (width + gap * 2))
        height = max(0, body.height() - 2 * gap)
        side.setGeometry(x, gap, width, height)
        shown = self._side_open > 0.001
        if side.isHidden() == shown:
            side.setVisible(shown)
            if shown:
                self._side_raise()
        floating = shown and self._compact
        self._shadow.setGeometry(x + width - 2, gap + 12, 24, max(0, height - 24))
        if self._shadow.isHidden() == floating:
            self._shadow.setVisible(floating)
            if floating:
                self._side_raise()
        self._hot.setGeometry(0, 0, 8, body.height())
        if self._hot.isHidden() == self._compact:
            self._hot.setVisible(self._compact)
            if self._compact:
                self._side_raise()

    def _side_step(self, value) -> None:
        self._side_open = float(value)
        self._place_sidebar()

    def _glide_side(self, goal: float, duration: int = 240) -> None:
        """Ease the sidebar to `goal` (0 away, 1 out): out with a quick start that settles, away with a gentler one."""
        anim = self._side_anim
        anim.stop()
        if not motion.on() or not self.isVisible() or self._side_open == goal:
            self._side_open = goal
            self._place_sidebar()
            return
        anim.setDuration(duration)
        anim.setStartValue(float(self._side_open))
        anim.setEndValue(float(goal))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic if goal > self._side_open else QEasingCurve.Type.InOutCubic)
        anim.start()

    def _glide_dock(self, goal: int) -> None:
        anim, dock = self._dock_anim, self._side_dock
        anim.stop()
        if not motion.on() or not self.isVisible() or dock.width() == goal:
            dock.setFixedWidth(goal)
            return
        anim.setDuration(260)
        anim.setStartValue(dock.width())
        anim.setEndValue(goal)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.start()

    def _set_compact(self, on: bool) -> None:
        """Compact mode on or off (Settings > Sidebar, Ctrl+B, or the button beside the logo)."""
        on = bool(on)
        if on == self._compact:
            return
        self._compact = on
        self._hot_timer.stop()
        self._watch.stop()
        self._glide_dock(0 if on else self.CARD_GAP + self.SIDEBAR_WIDTH)
        self._glide_side(0.0 if on else 1.0, 260)
        self._place_sidebar()
        self._sync_compact_ui()

    def _sync_compact_ui(self) -> None:
        self.side_toggle.setToolTip("Keep the sidebar open (Ctrl+B)" if getattr(self, "_compact", False) else "Hide the sidebar until the mouse touches the left edge (Ctrl+B)")

    def _reveal_side(self) -> None:
        if not self._compact:
            return
        self._away_ticks = 0
        self._glide_side(1.0, 240)
        self._watch.start()

    def _conceal_side(self) -> None:
        self._watch.stop()
        if self._compact:
            self._glide_side(0.0, 220)

    def _side_watch_tick(self) -> None:
        """The sidebar stays out while the mouse is over it (or a menu is open, or a button is held, as when dragging a scrollbar)
        and eases away a moment after the mouse leaves."""
        if not self._compact or self._side_open <= 0.0:
            self._watch.stop()
            return
        pos = self._body.mapFromGlobal(QCursor.pos())
        zone = QRect(0, 0, self.CARD_GAP + self.SIDEBAR_WIDTH + 14, self._body.height())
        held = QApplication.activePopupWidget() is not None or QApplication.mouseButtons() != Qt.MouseButton.NoButton
        if (zone.contains(pos) and self.isActiveWindow()) or held:
            self._away_ticks = 0
            return
        self._away_ticks += 1
        if self._away_ticks >= 3:
            self._conceal_side()

    # ---- pinning tools to the sidebar --------------------------------------------------------------------------
    def _is_pinned(self, key: str) -> bool:
        box = getattr(self, "_pin_boxes", {}).get(key)
        return True if box is None else box.isChecked()

    def _group_has_pins(self, group: str) -> bool:
        return any(self._is_pinned(k) for g, items in NAV_GROUPS if g == group for k, _t in items if k in self.nav_items)

    def _apply_pins(self, *_args) -> None:
        """Show the pinned tools in the sidebar and hide the rest (a group with nothing pinned goes too)."""
        if not self.nav_groups or not hasattr(self, "nav_search"):
            return
        self._filter_nav(self.nav_search.text())
        count = sum(1 for k in self.nav_items if self._is_pinned(k))
        self._brand_sub.setText(f"{APP_VERSION}  |  {count} tools")
        self._refresh_nav_tips()

    def _pinned_pages(self) -> list[str]:
        return [k for k in self.pages_order() if k in self.pages and self._is_pinned(k)]

    def _refresh_nav_tips(self) -> None:
        numbered = {k: n for n, k in enumerate(self._pinned_pages()[:9], 1)}
        for key, item in self.nav_items.items():
            n = numbered.get(key)
            item.setToolTip(f"{PAGE_INFO[key][0]}  (Ctrl+{n}; Ctrl+K finds any page)" if n else "")

    def _nav_nth(self, n: int) -> None:
        """Ctrl+1 ... Ctrl+9: the nth pinned tool, in sidebar order."""
        pages = self._pinned_pages()
        if 0 < n <= len(pages):
            self._nav_click(pages[n - 1])

    def _run_bar_clicked(self) -> None:
        if jobs := bar_targets(self._tool_target(), self._dl_target(), self.current_page):
            self._show_page(jobs[0][1])  # the job the bar is showing (a conversion first)

    def _nav_click(self, key: str) -> None:
        self._show_page(key)  # a run carries on in the background: its bar along the bottom leads back to it

    def _filter_nav(self, text: str) -> None:
        """Show only the tools whose names contain every typed word (their groups open up to show them)."""
        terms = text.lower().split()
        for group, items in NAV_GROUPS:
            g = self.nav_groups[group]
            hits = 0
            shown = []
            for key, label in items:
                item = self.nav_items.get(key)
                if item is None:
                    continue
                show = self._is_pinned(key) and all(t in label.lower() for t in terms)
                item.setVisible(show)  # at once: the body around it is what animates, and it clips them
                shown.append(show)
                hits += show
            want_header, want_body = hits > 0, hits > 0 and (bool(terms) or g["header"].open)  # a group with nothing pinned is left out
            if (tuple(shown), want_header, want_body) != g.get("filtered"):  # only a real change moves anything
                g["filtered"] = (tuple(shown), want_header, want_body)
                if want_header != g["header"].isVisibleTo(self):
                    motion.slide(g["header"], want_header)
                motion.slide(g["body"], want_body)  # opens, closes or resizes to fit the tools that are left

    def _nav_search_open(self) -> None:
        first = next((k for _g, items in NAV_GROUPS for k, _t in items if k in self.nav_items
                      and self.nav_items[k].isVisibleTo(self)), None)
        if first:
            self._nav_click(first)

    def _toggle_group(self, group: str) -> None:
        g = self.nav_groups[group]
        g["header"].open = not g["header"].open
        motion.slide(g["body"], g["header"].open)  # the list eases open and shut
        g["header"].update()

    def _set_badge(self, key: str, count: int | str) -> None:
        self.nav_items[key].badge = str(count) if count else ""
        self.nav_items[key].update()

    def _refresh_nav_dots(self) -> None:
        """Green when a conversion tool is ready, amber while it is still being checked, red when it is missing."""
        if not self.nav_items:
            return
        for mode, tool in (("chd", "chdman"), ("rvz", "dolphin"), ("cso", "maxcso"), ("pkg", "shadplus"),
                           ("pkg5", "anyps5"), ("switch", "nscb"), ("zar", "zarchive"), ("ps3", "ps3qdd"), ("xbox", "xdvdfs")):
            path = self._tool_text(tool)
            ready = bool(path) and (Path(resolve_tool_path(path)).is_file() or bool(shutil.which(path)))
            if mode in self.nav_items:
                self.nav_items[mode].state = "ok" if ready else "warn" if tool in self._pending_tools else "bad"
                self.nav_items[mode].update()
        self._sync_dot_blink()
        if hasattr(self, "_sw_refresh_note"):
            self._sw_refresh_note()

    def _sync_dot_blink(self) -> None:
        """Amber dots (a tool is being downloaded or checked) blink; the timer runs only while one is showing."""
        waiting = any(item.state == "warn" for item in self.nav_items.values()) or self.markers.blinking()
        timer = getattr(self, "_blink_timer", None)
        if timer is None:
            timer = self._blink_timer = QTimer(self)
            timer.setInterval(550)
            timer.timeout.connect(self._blink_dots)
        if waiting and motion.on():
            if not timer.isActive():
                timer.start()
        else:
            timer.stop()
            NavButton.blink_on = True
            for item in self.nav_items.values():
                if item.state == "warn":
                    item.update()
            self.markers.update()

    def _blink_dots(self) -> None:
        NavButton.blink_on = not NavButton.blink_on
        for item in self.nav_items.values():
            if item.state == "warn":
                item.update()
        self.markers.update()

    def _refresh_markers(self) -> None:
        """Footer markers: green when installed, blinking amber while being checked or downloaded, red when missing."""
        if not hasattr(self, "markers"):
            return
        states = []
        for key, name in (("ytdlp", "yt-dlp"), ("ffmpeg", "ffmpeg")):
            path = self._tool_text(key)
            ready = bool(path) and (Path(resolve_tool_path(path)).is_file() or bool(shutil.which(path)))
            if key in self._installing:
                state, note = "warn", "downloading or updating..."
            elif ready:
                state, note = "ok", "installed"
            elif key in self._pending_dl:
                state, note = "warn", "checking..."
            else:
                state, note = "bad", "not installed (see Settings > Tools)"
            states.append((name, state, note))
        provider = next(iter(debrid.active(LOGINS)), None)  # the downloader links go to first
        has_torbox = bool(torbox_key())
        click = "  Click to see what is on your account." if has_torbox else ""
        states.append((provider.name, "ok", "default downloader: key saved" + (click if provider.key == "torbox" else ""))
                      if provider else ("Debrid", "bad", "no downloader: add a TorBox or other debrid key under Settings > API keys > Downloads"))
        if has_torbox and provider is not None and provider.key != "torbox":  # TorBox has a key but isn't first in line
            states.append(("TorBox", "ok", "key saved." + click))
        self.markers.set_states(states)
        self.markers.set_clickable({"TorBox"} if has_torbox else ())
        self._sync_dot_blink()

    def _marker_clicked(self, name: str) -> None:
        """A footer chip was clicked: TorBox lists what is on the account (one window; clicking again refreshes it)."""
        if name != "TorBox" or not torbox_key():
            return
        dialog = getattr(self, "_torbox_dialog", None)
        try:
            if dialog is not None and dialog.isVisible():
                dialog.raise_()
                dialog.activateWindow()
                dialog.refresh()
                return
        except RuntimeError:  # the window was closed and deleted since
            pass
        self._torbox_dialog = TorBoxDownloadsDialog(self)
        self._torbox_dialog.addFiles.connect(self._dl_add_account_files)
        self._torbox_dialog.show()

    def _show_page(self, key: str) -> None:
        if key not in self.pages:
            return
        order, before = self.pages_order(), self.current_page
        self.current_page = key
        # the old page drifts up when the new one is further down the sidebar, down when it is further up
        further_up = key in order and before in order and order.index(key) < order.index(before)
        self._place_perf(key)
        self.stack.setCurrentWidget(self.pages[key], 1 if further_up else -1)
        motion.set_visible(self.markers, key == "dl", axis="width")  # only Files & Torrents uses yt-dlp, ffmpeg and a debrid service
        title, blurb = PAGE_INFO[key]
        retitled = self.page_title.text() != title
        self.page_title.setText(title)
        self.page_blurb.setText(blurb)
        if retitled:  # the header is outside the stack, so the page dissolve doesn't cover it
            motion.reveal(self.page_title, 180)
            motion.reveal(self.page_blurb, 180)
        self.page_blurb.setToolTip(PAGE_DETAIL.get(key, ""))  # what the page really does, in the app's own words
        self.page_chip.setText(PAGE_CHIP.get(key, "").upper())
        motion.fade(self.page_chip, bool(PAGE_CHIP.get(key)))
        for group, items in NAV_GROUPS:  # make sure the group holding this page is expanded
            if any(k == key for k, _t in items) and not self.nav_groups[group]["header"].open and self._group_has_pins(group):
                self._toggle_group(group)
        for k, item in self.nav_items.items():
            item.selected = k == key
            item.update()
        self._refresh_start_label()
        self._runbar_sync()  # the bar along the bottom shows while the running job's page is not the one on screen
        self._gm_sync_scrollbar()  # the Games page hides its scrollbar while it has no games; every other page keeps its own
        if key == "games":
            self._gm_on_show()
        elif key == "recomps":
            self._rc_on_show()
        elif key == "emus":
            self._em_on_show()

    def _place_perf(self, key: str) -> None:
        """Move the one Performance card into the right column of the page being shown (below that page's own settings)."""
        slot = self.perf_slots.get(key)
        if slot is None or slot.indexOf(self.perf) >= 0:
            return
        slot.insertWidget(max(0, slot.count() - 1), self.perf)  # above the trailing stretch

    def _current_mode(self) -> str:
        page = self.current_page
        return (page if page in self.MODES else "dl" if page in ("dl", "sound") else "recomps" if page == "recomps"
                else "games" if page == "games" else "emus" if page == "emus" else "switch" if page == "switch" else "org")

    def _busy_label(self, page: str) -> str:
        """What the Start button says while the run begun from this page is going."""
        if page == "switch":
            return self._sw_busy_label()
        return BUSY_LABELS.get(page, "Working...")

    def _begin_run_label(self, page: str | None = None) -> None:
        """A run is starting: remember its page and wording, so the button keeps saying it whichever page is showing."""
        self._run_page = page or self.current_page
        self._run_label = self._busy_label(self._run_page)
        self._update_start_text()

    def _update_start_text(self) -> None:
        """The Start button's words: what the page does when idle, what is happening while a run or an Organise job goes."""
        if not hasattr(self, "start_btn"):
            return
        page, mode = self.current_page, self._current_mode()
        dl = view_of(mode) == DL  # the Download and Soundtrack pages speak for the download run, every other page for the tool run
        if dl and self.dl_runner is not None:
            text = self._dl_label
        elif not dl and self.is_processing:
            text = getattr(self, "_run_label", "Working...")
        elif self.org_busy:
            text = getattr(self, "_org_label", "Working...")
        else:
            text = ("Save Selected Soundtracks" if page == "sound" else "Start Download" if mode == "dl"
                    else self._sw_start_label() if mode == "switch"
                    else self.org[page].tool.apply_label if mode == "org"
                    else START_LABELS.get(mode, "Start Batch"))
        self.start_btn.setText(text)
        target = self._dl_target() if dl else self._tool_target()
        if target is None and self.org_busy:  # an Organise job blocks Start on every page
            target = self._tool_target()
        away = target is not None and target[0] != page
        tip = (f"{PAGE_INFO[target[0]][0]} is running. Click the bar along the bottom to go back to it."
               if away and target[0] in PAGE_INFO else "Ctrl+Enter")
        if self.start_btn.toolTip() != tip:
            self.start_btn.setToolTip(tip)

    def _refresh_start_label(self) -> None:
        page, mode = self.current_page, self._current_mode()
        self._update_start_text()
        if not self.is_processing and not self.org_busy and mode != "dl":  # the idle label follows the page
            self.lbl_total_prog.setText("Organise Progress: 0 / 0" if mode == "org" else "Batch Progress: 0 / 0")
        motion.set_visible(self.perf, mode not in ("dl", "org", "games", "recomps", "emus"))
        motion.set_visible(self.bar_wrap, mode not in ("games", "recomps", "emus"))
        self._apply_panel_mode(mode)
        self._sync_run_controls()

    def _run_facts(self) -> RunFacts:
        stop = getattr(self, "_org_stop", None)
        return RunFacts(tool=self.is_processing, tool_paused=self.is_paused, tool_stopping=self._stopping,
                        dl=self.dl_runner is not None, dl_paused=self.dl_paused, dl_stopping=self.dl_stopping,
                        org=self.org_busy, org_stopping=bool(stop is not None and stop.is_set()), pending_tools=bool(self._pending_tools))

    def _sync_run_controls(self) -> None:
        """Start, Pause and Stop belong to the run of the page on screen (see runview), so they are set from the facts, not nudged one by one."""
        if not (hasattr(self, "start_btn") and hasattr(self, "pause_btn") and hasattr(self, "stop_btn")):
            return
        c = controls(view_of(self._current_mode()), self._run_facts())
        self.start_btn.setEnabled(c.start)
        self.pause_btn.setEnabled(c.pause)
        self.stop_btn.setEnabled(c.stop)
        if self.pause_btn.text() != c.pause_text:
            self.pause_btn.setText(c.pause_text)
        self._update_start_text()

    def _build_tab(self, mode: str) -> QWidget:
        is_pkg_page = mode in ("pkg", "pkg5")
        always_checked = is_pkg_page or mode == "ps3"  # these pages check every result, so the Verify box is fixed
        tv = self.tabs[mode] = TabUI(mode, always_checked)
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        dirs = Expander("Folders", expanded=True)
        dirs.add(tv.source)
        dirs.add(tv.dest)
        dirs.add(hint("When Destination folder is left blank, results are organised by platform inside the source folder",
                      tip="Each result goes in '<source folder>/<Platform>/' (PlayStation 2, Dreamcast, Game Boy ...), told "
                          "from the file itself. If the platform can't be told, or the file is already in a folder for it, "
                          "the result is saved next to its source."))
        extra = {
            "pkg": "Only PS4 .pkg files (and archives containing them) are picked up; PS5, PS3 and Vita packages "
                   "are skipped.",
            "pkg5": "Only PS5 debug .pkg files (and archives containing them) are picked up. Retail or encrypted "
                    "packages can't be read: they are flagged and left untouched.",
            "zar": "Every game, update or DLC folder under the source (one with sce_sys/param.sfo or eboot.bin) "
                   "becomes its own .zar named after the folder. Other folders are skipped.",
            "xbox": "Only Xbox and Xbox 360 .iso discs (and archives containing them) are picked up; other .iso "
                    "files are skipped.",
            "ps3": "Only PS3 .iso discs (and archives containing them) are picked up.",
            "cso": "Only PSP .iso discs (and archives containing them) are picked up here; other .iso files are "
                   "skipped.",
        }.get(mode)
        if extra:
            dirs.add(hint(extra))
        if mode == "ps3":
            dirs.add(self.ps3_keys)  # a folder pick: full width with the others
        dirs.add(self._drop_note_row(mode))  # 'only this dropped file will run', when a drop picked one out

        main = QWidget()  # below the folders: this tool's workflow on the left, its settings and the performance budget on the right
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(14)
        side = QWidget()
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 0, 0, 0)
        side_lay.setSpacing(14)
        self.perf_slots[mode] = side_lay
        main_lay.addWidget(dirs)  # the folders card is half width like the rest

        opts = Expander("Workflow", expanded=True)
        if mode != "zar":  # folders are packed as they are
            opts.add(tv.extract)
        tv.verify.setText({
            "chd": "Verify each CHD right after conversion (runs in parallel)",
            "rvz": "Verify each RVZ right after conversion",
            "cso": "Verify each CSO after conversion (decompressed and compared with the source file)",
            "pkg": "Check each extraction (title files present) - always on",
            "pkg5": "Check each extraction (app0/ or source/ written) - always on",
            "zar": "Verify each .zar after packing (unpacked again and every file compared with the folder; slow)",
            "ps3": "Check each result (PARAM.SFO readable, files complete) - always on",
            "xbox": "Verify each XISO right after conversion (content checksum compared with the source ISO)",
        }[mode])
        tv.delete.setText("Delete the original folders after they are packed successfully" if mode == "zar"
                          else "Delete original files after successful conversion/verification")
        opts.add(tv.verify)
        opts.add(tv.delete)
        main_lay.addWidget(opts)

        def form(card: Card, label: str, widget: QWidget, note: str = "", label_w: int = LABEL_W) -> None:
            v = QVBoxLayout()  # label over the control, note under it: a half-width column has no room for them in a row
            v.setSpacing(6)
            lbl = QLabel(label)
            lbl.setObjectName("pathLabel")
            v.addWidget(lbl)
            v.addWidget(widget)
            if note:
                v.addWidget(hint(note))
            card.add(v)

        if mode == "chd":
            card = Expander("CHD Settings", expanded=True)
            form(card, "Compression", self.chd_compression)
            side_lay.addWidget(card)
        if mode == "cso":
            card = Expander("CSO Settings", expanded=True)
            form(card, "Compression", self.cso_speed)
            side_lay.addWidget(card)
        if mode == "rvz":
            card = Expander("RVZ Settings", expanded=True)
            card.add(self.rvz_scrub)
            card.add(hint("With scrubbing on, verification only checks the RVZ reads back cleanly."))
            form(card, "Compression level", self.rvz_level)
            card.add(hint(f"zstd 1-{RVZ_MAX_LEVEL}: step right for smaller files, left for faster conversion."))
            side_lay.addWidget(card)
        if mode == "ps3":
            card = Expander("PS3 Settings", expanded=True)
            form(card, "Output", self.ps3_output)
            card.add(hint("A .dkey beside the ISO wins, else one with the same name in the Keys folder. Results are named 'Game Name - Title ID'.", tip="Keys: a .dkey beside the ISO wins, else one with the same name in the Keys folder (PS3QDD's own Keys folder is used automatically).  Results are named 'Game Name - Title ID' from PARAM.SFO.  A loose folder needs room for the decrypted ISO as well while it is unpacked (a temp folder from Performance is used if set)."))
            side_lay.addWidget(card)
        if mode == "pkg":
            card = Expander("PS4 PKG Settings", expanded=True)
            form(card, "Folder layout", self.pkg_layout)
            card.add(hint('One folder per PKG, named after the file. shadPS4 layout: game, -UPDATE folder, and addcont for DLC.', tip='One folder per PKG: named after each file.  |  shadPS4 layout: a game in CUSAxxxxx, its update in CUSAxxxxx-UPDATE, DLC in addcont/CUSAxxxxx/<label>.'))
            card.add(self.pkg_merge)
            card.add(hint('A game and its updates (same title ID) become one patched game folder, updates applied in version order.', tip="A game and the updates for it in the source folder (same title ID) become one patched game folder: each update is laid over the game in version order and the game's param.sfo takes the newest version. The update .pkg files are used up (deleted with the game if you ticked that); an update with no game in the folder is extracted on its own."))
            card.add(self.pkg_zar)
            card.add(hint('Each extraction is checked and packed into one <name>.zar (shadPS4 0.17+), then the folder is removed.', tip='Each extraction is checked, packed into one <name>.zar with ZArchive (shadPS4 0.17+ reads it directly) and the extracted folder is removed.'))
            side_lay.addWidget(card)
        if mode == "pkg5":
            card = Expander("PS5 PKG Settings", expanded=True)
            lbl = QLabel("What is this extraction for?")
            lbl.setObjectName("cardTitle")
            card.add(lbl)
            card.add(self.ps5_anyps5)
            card.add(hint("AnyPS5's relinker makes app.exe and copies its libraries into libs/. A failed relink keeps the extraction.", tip="AnyPS5's relinker makes app.exe from source/eboot.bin and its system libraries are copied into libs/. If the relink fails the extraction is kept and the .pkg is not deleted."))
            card.add(self.ps5_kyty)
            card.add(hint('Kyty reads a .zar directly: app0/ is packed into <name>.zar and the extraction folder is removed.', tip='Kyty reads a .zar directly. app0/ is packed into <name>.zar and the extraction folder is removed; source/ is not kept. Room is needed for the full extraction and the .zar at once.'))
            group = QButtonGroup(self)
            group.addButton(self.ps5_anyps5)
            group.addButton(self.ps5_kyty)
            side_lay.addWidget(card)
        main_lay.addStretch(1)
        side_lay.addStretch(1)
        lay.addWidget(TwoColumn(main, side, wide_at=760, equal=True))
        lay.addStretch(1)  # spare height goes below the cards, never into them
        page.setProperty("compact", True)  # SizedStack pins the page's height to its cards
        return page

    DL_COLS = {"size": 2, "source": 3, "status": 4, "speed": 5}
