"""Distill Resonance: a media player. One search bar; the albums found are laid out like a music app's album page
(cover on the left, tracks on the right). Tracks stream from KHInsider (or archive.org); selected tracks and albums are
saved through the Files & torrents download list, from KHInsider when it has them and archive.org otherwise."""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QSize, QTimer, Qt, QUrl
from PySide6.QtGui import QBrush, QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

try:  # previews need Qt's media player; without it the page works the same, just without the play buttons
    from PySide6.QtMultimedia import QAudio, QAudioOutput, QMediaPlayer, QSoundEffect
    AUDIO_OK = True
except ImportError:  # pragma: no cover - depends on the install
    AUDIO_OK = False

from .. import organize as org
from .. import soundtrack_tags as tags
from .. import soundtracks as st
from ..covercache import CoverCache
from ..downloads import DownloadRunner, http_open, LinkError, RemoteFile
from ..models import fmt_size, plural
from ..mediaqueue import QueueItem, REPEAT_ALL, REPEAT_OFF, REPEAT_ONE
from ..mediastore import MB
from .albumview import AlbumGrid, AlbumTile, AlbumView, clock, EmptyState, FIRST_SHOWN, HomeStrip, QueueBody, QueueRow, TrackInfo
from .assets import asset
from .sourcechips import has_chip, source_chip
from .tabs import MediaTabs
from .emu_cards import hairline
from .state import LABEL_W
from .theme import T
from . import flight, icons, motion, msgbox, scratchmath
from .widgets import accent_button, AnimCheckBox, Card, hint, PathRow, StageSwitch, SwitchToggle, symbolize

ART_CACHE_MAX = 64  # covers kept in memory
ART_MAX_BYTES = st.COVER_MAX_BYTES
ART_PIXELS = 800  # a cover kept in memory is no bigger than this on its longest side (the sharper ones fetched for the grids can be huge)
_ART_SLOTS = threading.BoundedSemaphore(4)  # covers downloaded at once: a Top 100 asks for a hundred, and the sharper ones are bigger
SHOWN_ALBUMS = 10  # albums drawn per search; the rest are one click away
HOME_TTL = 6 * 3600  # Home rows are kept this long
HOME_ROWS = (("recent", "Recently played"), ("new", "New additions"), ("top40", "Top 40 this week"),
             ("newly", "Top 100 newly added"), ("recent6", "Top 100 of the last 6 months"), ("alltime", "All-time top 1000"))
HOME_TILES = 30  # albums shown per row
SNAPSHOT_MAX_AGE_DAYS = 30  # a saved track list older than this is listed again from the source
BROWSE_TILES = 60  # albums drawn per step on the Browse tab
BROWSE_TTL = 6 * 3600
CONSOLES_TTL = 7 * 24 * 3600
HISTORY_AFTER_MS = 30_000  # a track is logged as played once it has run this long (or ended)
FAVOURITES = "favourites"  # the built-in playlist of liked tracks
CACHE_MAX_FILE = 200 * MB  # one streamed track bigger than this is never cached
LIBRARY_SORTS = (("Date added", "added"), ("Title", "title"), ("Last played", "played"), ("Platform", "platform"),
                 ("Year", "year"), ("Most played", "plays"))

PROVIDER_NAMES = {"khinsider": "KHInsider", "archive.org": "archive.org", st.LOCAL_SOURCE: "On this computer"}  # as a person reads them


def provider_name(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, provider)


def meta_source(provider: str) -> str:
    """The source's name for a line of facts, unless it has a credit chip of its own (KHInsider, archive.org)."""
    return "" if has_chip(provider) else provider_name(provider)


def meta_source(provider: str) -> str:
    """The source's name as plain text, unless it has a credit chip of its own (KHInsider, archive.org)."""
    return "" if has_chip(provider) else provider_name(provider)


# (word in the status, symbol, colour) - the first that fits wins
STATUS_STYLE = (
    ("failed", "\u2717", "bad"), ("nothing found", "\u2717", "bad"), ("no tracks", "\u2717", "bad"),
    ("skipped", "\u26a0", "warn"), ("already", "\u2713", "ok"), ("done", "\u2713", "ok"), ("saved", "\u2713", "ok"),
    ("queued", "\u2026", "busy"), ("downloaded", "\u2026", "busy"), ("finding", "\u2026", "busy"),
)


class SoundRow:
    """One album on the page: what was found, its tracks, its widget and where it is in saving."""

    def __init__(self, album: st.Album, query: str):
        self.album = album
        self.query = query  # what was searched for: the game's name, used to find its folder in RomM mode
        self.view: AlbumView | None = None
        self.tile: AlbumTile | None = None  # its place in the results grid
        self.rule: QFrame | None = None
        self.files: list[RemoteFile] = []  # in the order the view lists them
        self.started = False  # its tracks have been handed to the download list
        self.saved: list[RemoteFile] = []  # the files as last handed over (named for where they go)
        self.removed = False
        self.match_folder: Path | None = None  # a game picked by hand (RomM mode)
        self.finalized = True  # nothing waiting on a finished run (tags, cover, summary)


def _track_infos(files: list[RemoteFile]) -> list[TrackInfo]:
    infos = []
    for i, f in enumerate(files, 1):
        rest = st._rest(f.name)
        _folder, _, base = rest.rpartition("/")
        stem, dot, ext = base.rpartition(".")
        number, title = st.tidy_title(stem if dot else base)
        bits = [(ext or "").upper(), fmt_size(f.size) if f.size is not None else ""]  # (the folder, the album's name, is not repeated)
        infos.append(TrackInfo(str(number if number is not None else i), title, "  \u00b7  ".join(b for b in bits if b),
                               duration=clock(getattr(f, "seconds", None))))
    return infos


class SoundtrackMixin:
    """Distill Resonance page. Methods only; the state lives on DistillWindow, which mixes this in."""

    def _build_sound_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 18, 0, 0)
        self.media_tabs = MediaTabs()  # every music screen is a tab of this page (the underline slides, the pages dissolve)
        self.media_tabs.setDocumentMode(True)
        self.media_tabs.tabBar().setExpanding(False)
        self.media_tabs.tabBar().setDrawBase(False)
        self.media_tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        T.bind_css(self.media_tabs, self._media_tabs_css)
        name = QLabel("Distill Resonance")  # the page names itself here: it has no title block of its own
        name.setObjectName("dialogHead")
        name.setContentsMargins(0, 0, 22, 0)
        self.media_tabs.setCornerWidget(name, Qt.Corner.TopLeftCorner)
        outer.addWidget(self.media_tabs)
        search_tab = QWidget()
        lay = QVBoxLayout(search_tab)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)
        self._st_autoplay: str | None = None
        self._st_search_note = ""  # 'Some sources failed: ...' for the status line
        self._st_game_ctx: tuple[str, str, str, str] | None = None  # (game id, name, platform, year) of a game-started search
        self._st_logged_token = -1  # the play already logged to the history
        self._st_covers = CoverCache(self.media_store.cover_dir(), self.media_store.cover_cache_limit)  # covers kept between runs
        self._st_art_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="covers")  # a few at a time, never a thread per cover
        self._br_albums: list[st.Album] = []
        self._br_shown = 0
        self._br_token = 0
        self._br_page, self._br_pages = 1, 1
        self._br_consoles: list[tuple[str, str, int]] = []
        self._br_tiles: list[tuple[str, AlbumTile]] = []
        self._st_cache_key = ""
        self._st_pending: dict = {}
        self._st_resolve_token = 0
        self._st_stream_token = 0
        self._st_extra: list[st.Album] = []
        self._st_query = ""
        self._st_detail: SoundRow | None = None  # the album opened from the results grid (None: the grid is showing)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.st_search = QLineEdit()
        self.st_search.setPlaceholderText("Search game soundtracks...  e.g. Persona 5, Chrono Trigger")
        self.st_search.setClearButtonEnabled(True)
        self._search_field(self.st_search, 40)
        self.st_search.returnPressed.connect(self._st_search)
        self.st_search_btn = accent_button("Search")
        self.st_search_btn.setToolTip("Shift-click to ignore results remembered from the last few days")
        self.st_search_btn.clicked.connect(self._st_search)
        symbolize(self.st_search_btn, "search", "Search. Shift-click to ignore results remembered from the last few days", width=48)
        bar.addWidget(self.st_search, 1)
        bar.addWidget(self.st_search_btn)
        for provider in ("khinsider", "archive.org"):  # where the results come from
            bar.addWidget(source_chip(provider))
        lay.addLayout(bar)

        opts = Card("Saving")
        self.st_options = opts
        self.st_dest = PathRow("Save to", label_width=LABEL_W)
        self.st_dest.edit.setAcceptDrops(False)
        self.st_dest.setText(self.dl_dest.text())
        self._sync_group([self.dl_dest.edit, self.st_dest.edit], "text", "setText", "textChanged")  # one setting
        opts.add(self.st_dest)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.st_mode = SwitchToggle()  # off = Standard, on = RomM
        self.st_mode.setToolTip(
            "Off:  <Save to>/Soundtracks/<Album>/01 - Title.flac\n\n"
            "RomM:  <Games directory>/<platform>/<Game>/soundtrack/01 - Title.flac\n"
            "The game is found by the name you searched for (right-click an album > Match to game... to pick it).")
        self.st_mode_off, self.st_mode_on = QLabel("Off"), QLabel("RomM")
        for lab in (self.st_mode_off, self.st_mode_on):
            lab.setCursor(Qt.CursorShape.PointingHandCursor)
        self.st_mode_off.mousePressEvent = lambda _e: self.st_mode.setChecked(False)
        self.st_mode_on.mousePressEvent = lambda _e: self.st_mode.setChecked(True)
        label = QLabel("RomM support")
        label.setFixedWidth(LABEL_W)
        for w in (label, self.st_mode_off, self.st_mode, self.st_mode_on):
            mode_row.addWidget(w)
        self.st_folder_btn = QPushButton("Games directory...")
        self.st_folder_btn.clicked.connect(self._st_pick_folder)
        mode_row.addSpacing(10)
        mode_row.addWidget(self.st_folder_btn)
        mode_row.addStretch(1)
        opts.add(mode_row)
        self.st_mode_hint = hint("")
        opts.add(self.st_mode_hint)
        self.st_quality = StageSwitch()
        self.st_quality.addItem("Best (FLAC first)", "best")
        self.st_quality.addItem("MP3 only", "mp3")
        self.st_quality.setToolTip("Best uses FLAC when the album has it and MP3 otherwise; MP3 only skips FLAC.")
        qrow = QHBoxLayout()
        qrow.addStretch(1)
        qrow.addWidget(self.st_quality)
        qrow.addStretch(1)
        opts.add(qrow)
        self.st_tag = AnimCheckBox("Tag the files and save a cover.jpg beside them")
        self.st_tag.setToolTip("Fills in missing title, album, artist, track and disc numbers and the cover. Existing "
                               "tags are never overwritten.\n"
                               + ("" if tags.available() else "Needs the 'mutagen' package: only cover.jpg is saved without it."))
        opts.add(self.st_tag)
        tools = QHBoxLayout()
        self.st_undo_btn = QPushButton("Undo last save")
        self.st_undo_btn.clicked.connect(lambda _c=False: self._st_undo_save())
        symbolize(self.st_undo_btn, "undo", "Undo last save")
        self.st_log_btn = QPushButton("Open move log")
        self.st_log_btn.clicked.connect(lambda _c=False: self._st_open_log())
        self.st_cache_btn = QPushButton("Forget remembered searches")
        self.st_cache_btn.clicked.connect(lambda _c=False: (st.clear_search_cache(),
                                                            self._toast("Remembered searches cleared.", "ok", 4000)))
        self.st_hist_btn = QPushButton("Clear play history")
        self.st_hist_btn.clicked.connect(self._st_clear_history)
        for b in (self.st_undo_btn, self.st_log_btn, self.st_cache_btn, self.st_hist_btn):
            tools.addWidget(b)
        tools.addStretch(1)
        opts.add(tools)
        settings = QWidget()
        sl = QVBoxLayout(settings)
        sl.setContentsMargins(0, 12, 0, 0)
        sl.setSpacing(12)
        sl.addWidget(opts)
        sl.addWidget(self._build_budget_card())
        sl.addWidget(self._build_source_card())
        sl.addWidget(self._build_backup_card())
        sl.addStretch(1)
        settings_scroll = QScrollArea()  # three cards no longer fit a short window
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setWidget(settings)

        prefs = st.load_prefs()
        self.st_tag.setChecked(prefs.get("tag", True))
        self.st_tag.toggled.connect(lambda *_: self._st_mode_changed())
        self._st_games_dir = Path(prefs["games_dir"]) if prefs.get("games_dir") else None
        self.st_mode.setChecked(prefs.get("mode") == st.SAVE_ROMM)
        self.st_mode.toggled.connect(lambda *_: self._st_mode_changed())
        if prefs.get("quality") == "mp3":
            self.st_quality.setCurrentIndex(1)
        self.st_quality.currentIndexChanged.connect(lambda *_: self._st_prefs_update({"quality": self.st_quality.currentData()}))

        self.st_scroll = QScrollArea()
        self.st_scroll.setWidgetResizable(True)
        self.st_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.st_scroll.setMinimumHeight(320)
        body = QWidget()
        self.st_results = QVBoxLayout(body)
        self.st_results.setContentsMargins(0, 0, 8, 0)
        self.st_results.setSpacing(0)
        self.st_empty = EmptyState("search", "Search for a game to see its soundtrack albums.\nPlay a track to stream it; "
                                             "hover a track to save it or select it.")
        self.st_grid = AlbumGrid()  # the albums found, as covers; click one to open it
        self.st_grid.setVisible(False)
        self.st_results.addWidget(self.st_grid)
        self.st_results.addWidget(self.st_empty)
        self.st_more_results = QPushButton("Show more results")
        self.st_more_results.setVisible(False)
        self.st_more_results.clicked.connect(self._st_show_more)
        self.st_results.addWidget(self.st_more_results, 0, Qt.AlignmentFlag.AlignHCenter)
        self.st_results.addStretch(1)
        self.st_scroll.setWidget(body)
        self.st_back = QPushButton("Search results")  # shown while one album of several is open
        self.st_back.setFlat(True)
        self.st_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.st_back.setVisible(False)
        T.bind(self.st_back, lambda: self.st_back.setIcon(icons.icon("chevron-left", T.fg2, 16)))
        self.st_back.clicked.connect(self._st_show_grid)
        lay.addWidget(self.st_back, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.st_scroll, 1)

        self.st_status = hint("Search for a game above.", live=True)
        lay.addWidget(self.st_status)
        self._st_failed_rows: list[int] = []
        self._st_notes: list[tuple[str, str]] = []
        self._st_build_dock()
        self._explain(self.st_undo_btn, "Delete the tracks the last save created and put back any game files it moved "
                                         "into their own folders", "Nothing to undo yet.")
        self._st_cascading = False
        self._st_volume, self._st_muted = float(prefs.get("volume", 0.8)), bool(prefs.get("muted", False))
        self.now_playing.set_volume(self._st_volume, self._st_muted)
        self._st_mode_changed(save=False)
        self.media_tabs.addTab(self._build_home_tab(), "Home")
        self.media_tabs.addTab(search_tab, "Search")
        self.media_tabs.addTab(self._build_browse_tab(), "Browse")
        self.media_tabs.addTab(self._build_library_tab(), "Library")
        self.media_tabs.addTab(self._build_playlists_tab(), "Playlists")
        self.media_tabs.addTab(self._build_queue_tab(), "Queue")
        self.media_tabs.addTab(settings_scroll, "Settings")
        self.media_tabs.currentChanged.connect(lambda i: self._media_tab_shown(i))
        self._st_shortcuts = []  # keys that work anywhere on this page (and never fight typing or a focused button)
        for keys, action in ((QKeySequence("Ctrl+Space"), self._st_key_pause), (QKeySequence("Ctrl+Right"), lambda: self._st_step(1)),
                             (QKeySequence("Ctrl+Left"), lambda: self._st_step(-1)), (QKeySequence("Ctrl+F"), self._st_focus_search),
                             (QKeySequence(Qt.Key.Key_MediaTogglePlayPause), self._st_key_pause),
                             (QKeySequence(Qt.Key.Key_MediaNext), lambda: self._st_step(1)),
                             (QKeySequence(Qt.Key.Key_MediaPrevious), lambda: self._st_step(-1))):
            shortcut = QShortcut(keys, page)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
            self._st_shortcuts.append(shortcut)
        return page

    def _st_key_pause(self) -> None:
        if self._st_index >= 0:
            self._st_toggle_pause()

    def _st_focus_search(self) -> None:
        self._media_show_tab("Search")
        self.st_search.setFocus()
        self.st_search.selectAll()

    def _st_build_dock(self) -> None:
        """The bar along the bottom of the window while Distill Resonance shows (main_window puts it in line with the bottom of
        the resource monitor): Select all and Save selected above the download's progress, so neither needs a scroll to find."""
        dock = self.st_dock = QWidget()
        lay = QVBoxLayout(dock)
        lay.setContentsMargins(self.PAGE_MARGIN, 4, self.PAGE_MARGIN, self.CARD_GAP)
        lay.setSpacing(8)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.st_retry_btn = QPushButton("Retry failed")
        self.st_retry_btn.setVisible(False)
        self.st_retry_btn.clicked.connect(self._st_retry_failed)
        symbolize(self.st_retry_btn, "refresh", "Retry the tracks that failed")
        self.st_select_btn = QPushButton("Select all")  # "Deselect all" once anything is selected
        self.st_select_btn.setToolTip("Select every track of the open album (or of every album found, when none is open)")
        self.st_select_btn.clicked.connect(self._st_toggle_select_all)
        self.st_summary = hint("")
        self.st_save_selected = accent_button("Save selected")
        self.st_save_selected.setToolTip("Save every selected track (hover a track to select it) to your Soundtracks folder")
        self.st_save_selected.setEnabled(False)
        self.st_save_selected.clicked.connect(self._st_download)
        buttons.addWidget(self.st_retry_btn)
        buttons.addWidget(self.st_select_btn)
        buttons.addWidget(self.st_summary, 1)
        buttons.addWidget(self.st_save_selected)
        lay.addLayout(buttons)
        dock.hide()

    # ---- look: tab strip, search boxes, chips, queue toggles ----------------------------------------
    @staticmethod
    def _media_tabs_css() -> str:
        """Text tabs on a hairline, the current one in white with an accent underline that slides: the way the rest of the app marks
        'you are here', instead of the system's raised tab strip."""
        return (f"QTabWidget::pane {{ border: none; border-top: 1px solid {T.card_border.name()}; }} "
                f"QTabWidget::tab-bar {{ left: 2px; }} "
                f"QTabBar {{ background: transparent; }} "
                f"QTabBar::tab {{ background: transparent; color: {T.fg3.name()}; border: none; "
                f"padding: 9px 18px; margin: 0; font-weight: 600; }} "  # (the underline is drawn by the strip: tabs.SlidingTabBar)
                f"QTabBar::tab:hover {{ color: {T.fg.name()}; }} "
                f"QTabBar::tab:selected {{ color: {T.title.name()}; }}")

    @staticmethod
    def _chip_css() -> str:
        return (f"QPushButton {{ background: {T.btn[0].name()}; border: 1px solid {T.btn[1].name()}; border-radius: 14px; "
                f"padding: 4px 12px; color: {T.fg2.name()}; font-size: 9pt; font-weight: 500; }} "
                f"QPushButton:hover {{ background: {T.btn_hover[0].name()}; border-color: {T.btn_hover[1].name()}; "
                f"color: {T.title.name()}; }}")

    @staticmethod
    def _search_field(edit: QLineEdit, height: int) -> None:
        """A pill-shaped search box with a magnifier in it, as a music app has."""
        edit.setMinimumHeight(height)
        action = edit.addAction(icons.icon("search", T.fg3, 16), QLineEdit.ActionPosition.LeadingPosition)

        def style() -> None:
            action.setIcon(icons.icon("search", T.fg3, 16))
            edit.setStyleSheet(f"QLineEdit {{ border-radius: {height // 2}px; padding: 4px 12px 4px 14px; }}")
        T.bind(edit, style)

    def _queue_style_buttons(self) -> None:
        """Shuffle and Repeat: an icon each, and lit while they are on (a checkable button has no look of its own for it)."""
        if not hasattr(self, "q_repeat") or not hasattr(self, "pq"):
            return
        repeat = self.pq.repeat
        for button, name, lit in ((self.q_shuffle, "shuffle", self.q_shuffle.isChecked()),
                                  (self.q_repeat, "repeat-one" if repeat == REPEAT_ONE else "repeat", repeat != REPEAT_OFF)):
            button.setIcon(icons.icon(name, T.accent_text if lit else T.fg2, 16))
            button.setIconSize(QSize(16, 16))
            button.setStyleSheet(f"QPushButton {{ background: {T.chip_bg.name()}; border-color: {T.chip_edge.name()}; "
                                 f"color: {T.accent_text.name()}; }} QPushButton:hover {{ background: {T.chip_edge.name()}; }}"
                                 if lit else "")

    # ---- the library tab ------------------------------------------------------------------------
    def _build_library_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.lib_filter = QLineEdit()
        self.lib_filter.setPlaceholderText("Filter your library...")
        self.lib_filter.setClearButtonEnabled(True)
        self._search_field(self.lib_filter, 36)
        self.lib_filter.textChanged.connect(lambda *_: self._lib_refresh())
        rescan = QPushButton("Rescan")
        rescan.setToolTip("Look again for soundtracks saved on this computer")
        rescan.clicked.connect(lambda _c=False: self._st_scan_local())
        symbolize(rescan, "refresh", "Rescan: look again for soundtracks saved on this computer")
        self.lib_sort = QComboBox()
        for label, key in LIBRARY_SORTS:
            self.lib_sort.addItem(label, key)
        self.lib_sort.currentIndexChanged.connect(lambda *_: self._lib_refresh())
        self.lib_platform = QComboBox()
        self.lib_platform.addItem("All platforms", "")
        self.lib_platform.currentIndexChanged.connect(lambda *_: self._lib_refresh(keep_platforms=True))
        bar.addWidget(self.lib_filter, 1)
        bar.addWidget(self.lib_sort)
        bar.addWidget(self.lib_platform)
        bar.addWidget(rescan)
        lay.addLayout(bar)
        self.lib_recent_box = QWidget()
        rl = QHBoxLayout(self.lib_recent_box)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        recent = QLabel("Recently played")
        T.bind_css(recent, lambda: f"color: {T.fg3.name()}; font-weight: 600;")
        rl.addWidget(recent)
        rl.addStretch(1)
        self.lib_recent_lay = rl
        self._lib_recent_buttons: list[QPushButton] = []
        lay.addWidget(self.lib_recent_box)
        self.lib_scroll = QScrollArea()
        self.lib_scroll.setWidgetResizable(True)
        self.lib_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self.lib_list = QVBoxLayout(body)
        self.lib_list.setContentsMargins(0, 0, 0, 0)
        self.lib_list.setSpacing(0)
        self.lib_empty = EmptyState("music", "Your library is empty.\nPress the star on an album to keep it here: it streams "
                                             "from the source and takes almost no disk space.")
        self.lib_list.addWidget(self.lib_empty)
        self.lib_list.addStretch(1)
        self.lib_grid = AlbumGrid()
        shelf = QWidget()
        sl = QVBoxLayout(shelf)
        sl.setContentsMargins(0, 4, 8, 0)
        sl.setSpacing(0)
        sl.addWidget(self.lib_grid)
        sl.addWidget(body, 1)
        self.lib_scroll.setWidget(shelf)
        lay.addWidget(self.lib_scroll, 1)
        self._lib_widgets: list[tuple[str, AlbumTile]] = []  # (cover address, tile)
        self._lib_refresh()
        self._lib_recent_refresh()
        QTimer.singleShot(1500, self._st_scan_local)  # once the window is up: find what is already saved
        return tab

    def _st_scan_local(self) -> None:
        """Look for soundtracks already on disk (Soundtracks folder and RomM 'soundtrack' folders), off the UI thread."""
        raw = self._clean(self.st_dest.text())
        standard = Path(raw) if raw else self._st_default_dir()
        games = self._st_games_dir if self._st_games_dir and self._st_games_dir.is_dir() else None
        threading.Thread(target=self._st_local_worker, args=(standard, games), daemon=True).start()

    def _st_local_worker(self, standard: Path, games: Path | None) -> None:  # worker thread
        try:
            found = [(a, a.playable or 0) for a in st.scan_local_albums(standard, games)]
        except Exception:  # an unreadable drive must not break the page
            return
        self.ui_q.put(("st_local", found))

    def _st_on_local(self, found: list) -> None:
        added, removed = self.media_store.sync_local(found)
        if added or removed:
            self._lib_refresh()

    def _lib_recent_refresh(self) -> None:
        """The albums played lately, as buttons above the library list."""
        if not hasattr(self, "lib_recent_lay"):
            return
        for b in self._lib_recent_buttons:
            b.setParent(None)
            b.deleteLater()
        self._lib_recent_buttons = []
        albums = self.media_store.recent_albums(8)
        self.lib_recent_box.setVisible(bool(albums))
        for album in albums:
            b = QPushButton(album.title if len(album.title) <= 28 else album.title[:27] + "\u2026")
            b.setToolTip(f"Open {album.title} and play it")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            T.bind_css(b, self._chip_css)
            b.clicked.connect(lambda _c=False, a=album: self._st_open_album(a, play=True))
            self.lib_recent_lay.insertWidget(self.lib_recent_lay.count() - 1, b)
            self._lib_recent_buttons.append(b)


    def _lib_refresh(self, keep_platforms: bool = False) -> None:
        store = self.media_store
        if not keep_platforms:  # the platform list follows what the library holds
            current = self.lib_platform.currentData() or ""
            self.lib_platform.blockSignals(True)
            self.lib_platform.clear()
            self.lib_platform.addItem("All platforms", "")
            for name in store.library_platforms():
                self.lib_platform.addItem(name, name)
            at = self.lib_platform.findData(current)
            self.lib_platform.setCurrentIndex(max(at, 0))
            self.lib_platform.blockSignals(False)
        entries = store.library(self.lib_sort.currentData() or "added", text=self.lib_filter.text(),
                                platform=self.lib_platform.currentData() or "")
        self.lib_grid.clear()
        self._lib_widgets = []
        everything = bool(store.library())
        self.lib_empty.setVisible(not entries)
        self.lib_empty.setText("Nothing matches that filter." if everything and not entries else
                               "Your library is empty.\nPress the star on an album to keep it here: it streams from the "
                               "source and takes almost no disk space.")
        key = lambda e: st.album_merge_key(e.title) or e.title.casefold()  # noqa: E731
        saved = {key(e) for e in entries if e.source == st.LOCAL_SOURCE}
        streamed: dict[str, list] = {}
        for e in entries:
            if e.source != st.LOCAL_SOURCE:
                streamed.setdefault(key(e), []).append(e)
        for e in entries:
            k = key(e)
            local = e.source == st.LOCAL_SOURCE
            if not local and k in saved:
                continue  # the same release is saved on disk: one tile for both
            remotes = streamed.get(k, []) if local else [e]
            badge = ("Saved + streaming" if remotes else "Saved on disk") if local else "Streaming"
            facts = [x for x in (e.platform, str(e.year) if e.year else "", plural(e.track_count, "song") if e.track_count else "") if x]
            tile = AlbumTile(e.title, "  \u00b7  ".join(facts), badge, e.art, AUDIO_OK)
            tile.setToolTip(e.title + (f"\nPlayed {plural(e.play_count, 'time')}" if e.play_count else ""))
            album = e.album()
            tile.set_meta(badge, lit=local)
            tile.opened.connect(lambda a=album: self._st_open_album(a))
            tile.played.connect(lambda a=album: self._st_open_album(a, play=True))
            tile.moreClicked.connect(lambda w, a=album, rs=[x.album() for x in remotes]: self._lib_menu(w, a, rs))
            self.lib_grid.add_tile(tile)
            self._lib_widgets.append((e.art, tile))
            if e.art:
                self._st_want_art(e.art)
                tile.set_cover(self._st_art_cache.get(e.art))

    def _lib_menu(self, anchor, album: st.Album, remotes: list[st.Album]) -> None:
        """A library tile's menu: play it, open it, take it out of the library."""
        menu = QMenu(self)
        play = menu.addAction("Play")
        play.setEnabled(AUDIO_OK)
        play.triggered.connect(lambda: self._st_open_album(album, play=True))
        menu.addAction("Open").triggered.connect(lambda: self._st_open_album(album))
        if remotes:
            menu.addSeparator()
            menu.addAction("Remove from library").triggered.connect(lambda: self._lib_remove_many(remotes))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        menu.deleteLater()

    def _lib_remove_many(self, albums: list[st.Album]) -> None:
        for album in albums:
            self.media_store.remove_from_library(album)
            for r in self._st_rows:
                if (r.album.provider, r.album.ident) == (album.provider, album.ident):
                    self._st_mark_library(r, False)
        self._lib_refresh()
        self._media_refresh_budgets()

    def _lib_remove(self, album: st.Album) -> None:
        self._lib_remove_many([album])

    def _media_tab_shown(self, index: int) -> None:
        if self.media_tabs.tabText(index) == "Queue":
            self._queue_refresh()
        elif self.media_tabs.tabText(index) == "Playlists":
            self._pl_reload_picker()
        elif self.media_tabs.tabText(index) == "Library":
            self._lib_refresh()
        elif self.media_tabs.tabText(index) == "Browse":
            if not self._br_consoles:
                self._br_load_consoles()
        elif self.media_tabs.tabText(index) == "Settings":
            self._media_refresh_budgets()
            self._khi_refresh_status()

    def _st_toggle_library(self, r: SoundRow) -> None:
        store = self.media_store
        if store.in_library(r.album):
            store.remove_from_library(r.album)
            self._st_mark_library(r, False)
            self.st_status.setText(f"Removed {r.album.title} from your library.")
        else:
            tracks = r.album.cache.get("best")  # the list as listed for Best quality; MP3 mode derives from it
            store.add_to_library(r.album, tracks)
            self._st_mark_library(r, True)
            self.st_status.setText(f"Added {r.album.title} to your library.")
        self._lib_refresh()
        self._media_refresh_budgets()

    def _st_open_album(self, album: st.Album, play: bool = False, keep_ctx: bool = False) -> None:
        """Show one album (from the library, Home or a game) on the Search tab, optionally starting it."""
        self._media_show_tab("Search")
        self._st_token += 1
        token = self._st_token
        self._st_clear_results()
        self._st_query = album.title
        if not keep_ctx:
            self._st_game_ctx = None  # opened by hand: not for any game
        self.st_search_btn.setEnabled(True)  # a search it interrupted never reports back (its token is stale)
        self.st_empty.setVisible(False)
        self._st_show_detail(self._st_add_album(album))
        self._st_autoplay = album.ident if play else None
        self.st_status.setText(f"{album.title}")
        threading.Thread(target=self._st_list_worker, args=(token, 0, [album], album.title, self.st_quality.currentData()),
                         daemon=True).start()

    def _media_show_tab(self, name: str) -> None:
        for i in range(self.media_tabs.count()):
            if self.media_tabs.tabText(i) == name:
                self.media_tabs.setCurrentIndex(i)
                return

    def _st_show_albums(self, albums: list[st.Album], label: str) -> None:
        """Show these albums as the Search tab's results: tiles to choose from (their tracks are listed as for a search)."""
        self._media_show_tab("Search")
        self._st_token += 1
        token = self._st_token
        self._st_clear_results()
        self._st_query = label
        self._st_game_ctx = None  # opened by hand: not for any game
        self.st_search_btn.setEnabled(True)  # a search it interrupted never reports back (its token is stale)
        self._st_search_note = ""
        self.st_empty.setVisible(False)
        shown = albums[:SHOWN_ALBUMS]
        for album in shown:
            self._st_add_album(album)
        self._st_extra = albums[SHOWN_ALBUMS:]
        self._st_show_more_button()
        self.st_status.setText(label)
        threading.Thread(target=self._st_list_worker, args=(token, 0, shown, label, self.st_quality.currentData()),
                         daemon=True).start()

    def _st_related(self, r: SoundRow) -> None:
        """'More like this': the albums people who viewed this one also viewed (khinsider lists them on the album's page)."""
        related = st.related_albums(r.album)
        if related:
            self._st_show_albums(related, f"Also viewed with {r.album.title}")
        else:
            self.st_status.setText("No related albums are listed for that one.")

    # ---- the home tab ----------------------------------------------------------------------------
    def _build_home_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(6)
        bar = QHBoxLayout()
        self.home_status = hint("")
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Fetch the lists again (they are kept for a few hours)")
        refresh.clicked.connect(lambda _c=False: self._home_load(force=True))
        symbolize(refresh, "refresh", "Refresh: fetch the lists again (they are kept for a few hours)")
        bar.addWidget(self.home_status, 1)
        bar.addWidget(source_chip("khinsider"))  # the lists are KHInsider's
        bar.addWidget(refresh)
        lay.addLayout(bar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(4)
        self._home_strips: dict[str, HomeStrip] = {}
        for key, title in HOME_ROWS:
            strip = HomeStrip(title, can_see_all=key != "recent", can_play=AUDIO_OK)
            strip.opened.connect(lambda album: self._st_open_album(album))
            strip.played.connect(lambda album: self._st_open_album(album, play=True))
            strip.moreClicked.connect(lambda w, album: self._lib_menu(w, album, []))
            strip.seeAll.connect(lambda k=key: self._home_see_all(k))
            col.addWidget(strip)
            self._home_strips[key] = strip
        col.addStretch(1)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        self._home_albums: dict[str, list[st.Album]] = {}
        QTimer.singleShot(1200, self._home_load)  # once the window is up
        return tab

    def _home_load(self, force: bool = False) -> None:
        recent = self.media_store.recent_albums(HOME_TILES)  # local: no waiting
        self._home_albums["recent"] = recent
        if recent:
            self._home_strips["recent"].set_albums(recent, subtitle=lambda a: a.detail)
        else:
            self._home_strips["recent"].set_message("Nothing played yet.")
        self._home_art(recent)
        self.home_status.setText("Loading the lists...")
        threading.Thread(target=self._home_worker, args=(force,), daemon=True).start()

    def _home_worker(self, force: bool) -> None:  # worker thread
        provider = st.KHInsiderProvider()
        for key, _title in HOME_ROWS:
            if key == "recent":
                continue
            cache_key = f"home:{key}"
            albums, error = None, ""
            if not force and (cached := self.media_store.web_get(cache_key, HOME_TTL)):
                albums = st._albums_from_json(cached)
            if not albums:
                try:
                    if key == "new":
                        albums, _pages = provider.latest(1)
                    else:
                        albums, _pages = provider.ranked({"recent6": "recent"}.get(key, key))
                    albums = albums[:HOME_TILES * 4]  # 'See all' shows the rest of the page
                    if albums:
                        self.media_store.web_put(cache_key, st._albums_to_json(albums))
                except LinkError as exc:
                    albums, error = [], str(exc)
                except Exception as exc:  # a surprise must not leave the row on 'Loading...'
                    albums, error = [], f"{type(exc).__name__}: {exc}"
                time.sleep(0.4)
            self.ui_q.put(("st_home", key, albums, error))
        self.ui_q.put(("st_home", "", [], ""))  # the end

    def _st_on_home(self, key: str, albums: list, error: str) -> None:
        if not key:  # every row is in
            if self.home_status.text().startswith("Loading"):
                self.home_status.setText("")
            return
        strip = self._home_strips.get(key)
        if strip is None:
            return
        self._home_albums[key] = albums
        if albums:
            strip.set_albums(albums[:HOME_TILES], subtitle=lambda a: a.detail)
            self._home_art(albums[:HOME_TILES])
        else:
            strip.set_message(error or "Nothing here yet.")
            if error:
                self.home_status.setText(error)

    def _home_art(self, albums: list) -> None:
        for album in albums:
            if album.art:
                self._st_want_art(album.art)
                pix = self._st_art_cache.get(album.art)
                if pix is not None:
                    self._home_cover(album.art, pix)

    def _home_cover(self, url: str, pix) -> None:
        for strip in getattr(self, "_home_strips", {}).values():
            for tile in strip.tiles():
                if tile.art == url:
                    tile.set_cover(pix)

    def _home_see_all(self, key: str) -> None:
        albums = self._home_albums.get(key, [])
        if albums:
            self._st_show_albums(albums, dict(HOME_ROWS)[key])

    # ---- the queue tab --------------------------------------------------------------------------
    def _build_queue_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.q_shuffle = QPushButton("Shuffle")
        self.q_shuffle.setCheckable(True)
        self.q_shuffle.toggled.connect(self._queue_set_shuffle)
        self.q_repeat = QPushButton("Repeat: off")
        self.q_repeat.clicked.connect(self._queue_cycle_repeat)
        clear = QPushButton("Clear queue")
        clear.clicked.connect(self._queue_clear)
        symbolize(clear, "trash", "Clear the queue")
        for b in (self.q_shuffle, self.q_repeat, clear):
            bar.addWidget(b)
        T.bind(self.q_repeat, self._queue_style_buttons)  # (and again whenever the queue is redrawn)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.q_scroll = QScrollArea()
        self.q_scroll.setWidgetResizable(True)
        self.q_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QueueBody()
        body.dropped.connect(lambda index, slot: QTimer.singleShot(0, lambda: self._queue_drop(index, slot)))
        self.q_list = QVBoxLayout(body)
        self.q_list.setContentsMargins(0, 0, 8, 0)
        self.q_list.setSpacing(0)
        self.q_empty = EmptyState("list", "The queue is empty.\nPlay an album, or use Play next / Add to queue in an album or "
                                          "track menu.")
        self.q_list.addWidget(self.q_empty)
        self.q_list.addStretch(1)
        self.q_scroll.setWidget(body)
        lay.addWidget(self.q_scroll, 1)
        self._q_widgets: list[QWidget] = []
        self._st_restore_queue()
        self._queue_refresh()
        return tab

    def _queue_refresh(self) -> None:
        if not hasattr(self, "q_list"):
            return
        pq = self.pq
        for w in self._q_widgets:
            w.setParent(None)
            w.deleteLater()
        self._q_widgets = []
        self.q_shuffle.blockSignals(True)
        self.q_shuffle.setChecked(pq.shuffle)
        self.q_shuffle.blockSignals(False)
        self.q_repeat.setText({REPEAT_OFF: "Repeat: off", REPEAT_ALL: "Repeat: all", REPEAT_ONE: "Repeat: one"}[pq.repeat])
        self._queue_style_buttons()
        self._st_sync_player_controls()
        self.q_empty.setVisible(not pq.items)

        def add(widget: QWidget) -> None:
            self.q_list.insertWidget(len(self._q_widgets), widget)
            self._q_widgets.append(widget)

        def header(text: str) -> None:
            label = QLabel(text)
            T.bind_css(label, lambda: f"color: {T.fg3.name()}; font-weight: 600; padding: 10px 0 4px 8px;")
            add(label)

        def row(i: int, current: bool = False, movable: bool = False) -> None:
            item = pq.items[i]
            if item.art:
                self._st_want_art(item.art)
            r = QueueRow(item.title, item.album, current, can_move=movable, can_remove=not current, item_index=i,
                         movable=movable, art=self._st_art_cache.get(item.art), art_url=item.art)
            r.playClicked.connect(lambda i=i: self._queue_play(i))
            r.upClicked.connect(lambda i=i: self._queue_shift(i, -1))
            r.downClicked.connect(lambda i=i: self._queue_shift(i, 1))
            r.removeClicked.connect(lambda i=i: self._queue_remove(i))
            add(r)

        if pq.current_index >= 0:
            header("Now playing")
            row(pq.current_index, True)
        up = pq.upcoming()
        if up:
            header("Up next")
            for i in up:
                row(i, movable=True)
        elif pq.items and pq.current_index < 0:  # restored from last time: nothing is playing yet
            header("Queue")
            for i in pq.order:
                row(i, movable=True)
        done = pq.played()
        if done:
            header("Played")
            for i in done:
                row(i)

    def _queue_changed(self) -> None:
        """The queue was edited: redraw it, keep it for next time, and refresh what plays after this track."""
        self._queue_refresh()
        self._st_save_queue()
        self._st_sync_icons()
        if self._st_index >= 0:
            self.now_playing.set_can_skip(self.pq.can_step(-1), self.pq.can_step(1))
            self._st_prefetch_next()

    def _queue_play(self, index: int) -> None:
        if self.pq.jump(index):
            self._st_play_index(index)

    def _queue_shift(self, index: int, delta: int) -> None:
        """The up / down buttons: one place earlier or later among what is still to play."""
        up = self.pq.upcoming()
        if index in up:
            self._queue_drop(index, up.index(index) + delta + (1 if delta > 0 else 0))

    def _queue_drop(self, index: int, slot: int) -> None:
        """A row was dropped (or nudged) to `slot` among the upcoming tracks. `slot` counts the dragged row itself
        while it is still in its old place, so a slot after it is one too many."""
        up = self.pq.upcoming()
        if index in up and slot > up.index(index):
            slot -= 1
        if self.pq.move_upcoming(index, slot):
            self._queue_changed()

    def _queue_remove(self, index: int) -> None:
        if self.pq.remove(index):
            self._queue_changed()

    def _queue_clear(self) -> None:
        playing = self._st_index >= 0
        if playing:  # the playing track stays: only what is queued around it goes
            cur = self.pq.current()
            self.pq.set([cur], 0)
        else:
            self.pq.clear()
        self._queue_changed()

    def _queue_set_shuffle(self, on: bool) -> None:
        self.pq.set_shuffle(on)
        self._queue_changed()

    def _queue_cycle_repeat(self) -> None:
        self.pq.cycle_repeat()
        self._queue_changed()

    def _st_save_queue(self) -> None:
        import json
        self.media_store.set_setting("queue_state", json.dumps(self.pq.state(), separators=(",", ":")))

    def _st_restore_queue(self) -> None:
        import json
        try:
            state = json.loads(self.media_store.setting("queue_state", "") or "null")
        except ValueError:
            return
        if isinstance(state, dict):
            self.pq.restore(state)

    def _st_items_for(self, r: SoundRow, indexes: list[int]) -> list[QueueItem]:
        out = []
        for i in indexes:
            if 0 <= i < len(r.files):
                f = r.files[i]
                title = st.tidy_title(st._rest(f.name).rpartition("/")[2].rpartition(".")[0])[1]
                out.append(QueueItem(title, r.album.title, f.url, r.album.art or "", r.album.provider, r.album.ident, i,
                                     r.album.detail, int(f.seconds or 0)))
        return out

    def _st_enqueue(self, r: SoundRow, indexes: list[int], play_next: bool) -> None:
        """Play next / Add to queue. With nothing playing the tracks simply start."""
        items = self._st_items_for(r, indexes)
        if not items:
            return
        if self._st_index < 0:
            self.pq.set(items, 0)
            self._st_play_index(self.pq.current_index)
            return
        (self.pq.insert_next if play_next else self.pq.append)(items)
        self._queue_changed()
        self.st_status.setText(f"{'Playing next' if play_next else 'Added to the queue'}: {plural(len(items), 'track')}.")

    # ---- the player's like / queue buttons --------------------------------------------------------
    def _st_sync_player_controls(self) -> None:
        """Tell the sidebar player whether the playing track is liked and whether shuffle and repeat are on."""
        if not hasattr(self, "now_playing"):
            return
        cur = self.pq.current()
        liked = cur is not None and cur.position in self.media_store.favourite_positions(st.Album(cur.source, cur.ident, cur.album))
        self.now_playing.set_controls(liked, self.pq.shuffle, self.pq.repeat)

    def _st_like_current(self) -> None:
        """The heart in the player: like (or un-like) the track that is playing."""
        cur = self.pq.current()
        if cur is None:
            return
        for r in self._st_rows:  # the album is on the page: the same path as the heart on its track
            if (r.album.provider, r.album.ident) == (cur.source, cur.ident):
                self._st_toggle_favourite(r, cur.position)
                return
        album = st.Album(cur.source, cur.ident, cur.album, cur.detail, 1.0, cur.art)
        self.media_store.set_favourite(album, cur.position, cur.position not in self.media_store.favourite_positions(album))
        self._pl_reload_picker(keep=True)
        self._st_sync_player_controls()

    def _st_show_queue(self) -> None:
        """The queue button in the player: go to the Queue tab."""
        self._nav_click("sound")
        for i in range(self.media_tabs.count()):
            if self.media_tabs.tabText(i) == "Queue":
                self.media_tabs.setCurrentIndex(i)

    # ---- favourites and playlists ---------------------------------------------------------------
    def _st_toggle_favourite(self, r: SoundRow, index: int) -> None:
        on = index not in self.media_store.favourite_positions(r.album)
        self.media_store.set_favourite(r.album, index, on)
        if r.view is not None:
            r.view.set_liked(index, on)
        if tracks := r.album.cache.get("best"):  # so the favourites list can open without listing the album again
            self.media_store.save_snapshot(r.album, tracks)
        self._pl_reload_picker(keep=True)
        self._st_sync_player_controls()

    def _build_playlists_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.pl_pick = QComboBox()
        self.pl_pick.setMinimumWidth(220)
        self.pl_pick.currentIndexChanged.connect(lambda *_: self._pl_refresh())
        self.pl_new = QPushButton("New")
        self.pl_new.clicked.connect(self._pl_create)
        symbolize(self.pl_new, "plus", "New playlist")
        self.pl_rename = QPushButton("Rename")
        self.pl_rename.clicked.connect(self._pl_rename)
        symbolize(self.pl_rename, "edit", "Rename this playlist")
        self.pl_delete = QPushButton("Delete")
        self.pl_delete.clicked.connect(self._pl_delete)
        symbolize(self.pl_delete, "trash", "Delete this playlist", danger=True)
        self.pl_play = accent_button("Play")
        self.pl_play.clicked.connect(lambda: self._pl_play(0))
        self.pl_shuffle = QPushButton("Shuffle play")
        self.pl_shuffle.clicked.connect(lambda: self._pl_play(0, shuffle=True))
        symbolize(self.pl_shuffle, "shuffle", "Shuffle play")
        self.pl_export = QPushButton("Export...")
        self.pl_export.setToolTip("Save this list as an .m3u8 playlist other players can open")
        self.pl_export.clicked.connect(self._pl_export)
        symbolize(self.pl_export, "save", "Export this list as an .m3u8 playlist other players can open")
        for w in (self.pl_pick, self.pl_new, self.pl_rename, self.pl_delete, self.pl_export):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.pl_shuffle)
        bar.addWidget(self.pl_play)
        lay.addLayout(bar)
        self.pl_scroll = QScrollArea()
        self.pl_scroll.setWidgetResizable(True)
        self.pl_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self.pl_list = QVBoxLayout(body)
        self.pl_list.setContentsMargins(0, 0, 8, 0)
        self.pl_list.setSpacing(0)
        self.pl_empty = EmptyState("heart")
        self.pl_list.addWidget(self.pl_empty)
        self.pl_list.addStretch(1)
        self.pl_scroll.setWidget(body)
        lay.addWidget(self.pl_scroll, 1)
        self._pl_widgets: list[QWidget] = []
        self._pl_files: dict[tuple[str, str], list[RemoteFile]] = {}  # album -> its listing, to name and play tracks
        self._pl_loading: set[tuple[str, str]] = set()
        self._pl_entries: list = []
        self._pl_reload_picker()
        return tab

    def _pl_current(self):
        """FAVOURITES, or the id of the chosen playlist."""
        return self.pl_pick.currentData() if self.pl_pick.count() else FAVOURITES

    def _pl_reload_picker(self, keep: bool = False) -> None:
        chosen = self._pl_current() if self.pl_pick.count() else FAVOURITES
        self.pl_pick.blockSignals(True)
        self.pl_pick.clear()
        self.pl_pick.addItem(f"\u2665 Favourites ({len(self.media_store.favourites())})", FAVOURITES)
        for pid, name, n in self.media_store.playlists():
            self.pl_pick.addItem(f"{name} ({n})", pid)
        at = self.pl_pick.findData(chosen)
        self.pl_pick.setCurrentIndex(max(at, 0))
        self.pl_pick.blockSignals(False)
        if not keep or self.media_tabs.tabText(self.media_tabs.currentIndex()) == "Playlists":
            self._pl_refresh()

    def _pl_collect(self) -> list[tuple[int | None, st.Album, int]]:
        """(playlist item id or None, album, track position) of what the chosen list holds."""
        cur = self._pl_current()
        if cur == FAVOURITES:
            return [(None, a, p) for a, p in self.media_store.favourites()]
        return [(iid, a, p) for iid, a, p in self.media_store.playlist_items(cur)]

    def _pl_track(self, album: st.Album, position: int) -> tuple[str, st.RemoteFile | None]:
        files = self._pl_files.get((album.provider, album.ident))
        if files and 0 <= position < len(files):
            f = files[position]
            return st.tidy_title(st._rest(f.name).rpartition("/")[2].rpartition(".")[0])[1], f
        return f"Track {position + 1}", None

    def _pl_refresh(self) -> None:
        for w in self._pl_widgets:
            w.setParent(None)
            w.deleteLater()
        self._pl_widgets = []
        cur = self._pl_current()
        user_list = cur != FAVOURITES
        entries = self._pl_entries = self._pl_collect()
        for b in (self.pl_rename, self.pl_delete):
            b.setEnabled(user_list)
        for b in (self.pl_play, self.pl_shuffle):
            b.setEnabled(bool(entries) and AUDIO_OK)
        self.pl_empty.setVisible(not entries)
        self.pl_empty.setText("No favourites yet.\nPress the heart on a track to keep it here." if not user_list else
                              "This playlist is empty.\nUse Add to playlist in an album or track menu.")
        now = self.pq.current()
        missing: list[st.Album] = []
        for k, (iid, album, pos) in enumerate(entries):
            title, f = self._pl_track(album, pos)
            if f is None:
                missing.append(album)
            is_now = now is not None and (now.source, now.ident, now.position) == (album.provider, album.ident, pos)
            if album.art:
                self._st_want_art(album.art)
            r = QueueRow(title, album.title if f else f"{album.title}  (loading...)", is_now, can_move=user_list,
                         can_remove=True, art=self._st_art_cache.get(album.art), art_url=album.art)
            r.playClicked.connect(lambda k=k: self._pl_play(k))
            r.upClicked.connect(lambda k=k: self._pl_move(k, -1))
            r.downClicked.connect(lambda k=k: self._pl_move(k, 1))
            r.removeClicked.connect(lambda k=k: self._pl_remove(k))
            self.pl_list.insertWidget(len(self._pl_widgets), r)
            self._pl_widgets.append(r)
        self._pl_load(missing)

    def _pl_load(self, albums: list[st.Album]) -> None:
        todo = []
        for a in albums:
            key = (a.provider, a.ident)
            if key not in self._pl_files and key not in self._pl_loading and a not in todo:
                self._pl_loading.add(key)
                todo.append(a)
        if todo:
            threading.Thread(target=self._pl_load_worker, args=(todo, self.st_quality.currentData()), daemon=True).start()

    def _pl_load_worker(self, albums: list[st.Album], prefer: str) -> None:  # worker thread
        for album in albums:
            try:
                files, error = self._st_files(album.title, album, prefer), ""
            except Exception as exc:  # LinkError or a surprise: the rows then say the tracks are unavailable
                files, error = [], str(exc)
            self.ui_q.put(("st_files", (album.provider, album.ident), files, error))

    def _st_on_files(self, key: tuple[str, str], files: list[RemoteFile], error: str) -> None:
        self._pl_loading.discard(key)
        self._pl_files[key] = files
        if self.media_tabs.tabText(self.media_tabs.currentIndex()) == "Playlists":
            self._pl_refresh()

    def _pl_play(self, start: int, shuffle: bool = False) -> None:
        items, first = [], 0
        for k, (_iid, album, pos) in enumerate(self._pl_entries):
            title, f = self._pl_track(album, pos)
            if f is None:
                continue
            if k == start:
                first = len(items)
            items.append(QueueItem(title, album.title, f.url, album.art or "", album.provider, album.ident, pos, album.detail,
                                   int(f.seconds or 0)))
        if not items:
            self.st_status.setText("Those tracks are still loading, or their albums are unavailable.")
            return
        self.pq.set_shuffle(shuffle)
        self.pq.set(items, first)
        self._st_play_index(self.pq.current_index)

    def _pl_move(self, k: int, delta: int) -> None:
        cur = self._pl_current()
        if cur != FAVOURITES and 0 <= k < len(self._pl_entries):
            self.media_store.move_in_playlist(cur, self._pl_entries[k][0], k + delta)
            self._pl_refresh()

    def _pl_remove(self, k: int) -> None:
        if not 0 <= k < len(self._pl_entries):
            return
        iid, album, pos = self._pl_entries[k]
        cur = self._pl_current()
        if cur == FAVOURITES:
            self.media_store.set_favourite(album, pos, False)
            for r in self._st_rows:
                if r.view is not None and (r.album.provider, r.album.ident) == (album.provider, album.ident):
                    r.view.set_liked(pos, False)
        else:
            self.media_store.remove_from_playlist(cur, iid)
        self._pl_reload_picker()

    def _pl_ask_name(self, title: str, label: str, text: str = "") -> str:
        name, ok = QInputDialog.getText(self, title, label, text=text)
        return name.strip() if ok else ""

    def _pl_create(self) -> None:
        if name := self._pl_ask_name("New Playlist", "Name:"):
            pid = self.media_store.create_playlist(name)
            self._pl_reload_picker()
            self.pl_pick.setCurrentIndex(max(self.pl_pick.findData(pid), 0))

    def _pl_rename(self) -> None:
        cur = self._pl_current()
        if cur != FAVOURITES and (name := self._pl_ask_name("Rename Playlist", "Name:", self.media_store.playlist_name(cur))):
            self.media_store.rename_playlist(cur, name)
            self._pl_reload_picker()

    def _pl_delete(self) -> None:
        cur = self._pl_current()
        if cur == FAVOURITES:
            return
        name = self.media_store.playlist_name(cur)
        if msgbox.question(self, "Delete Playlist", f"Delete the playlist '{name}'? The albums stay as they are.") \
                == QMessageBox.StandardButton.Yes:
            self.media_store.delete_playlist(cur)
            self._pl_reload_picker()

    def _pl_menu(self, menu: QMenu, r: SoundRow, indexes: list[int], enabled: bool) -> None:
        """'Add to playlist' submenu: each playlist, and a new one."""
        sub = menu.addMenu("Add to playlist")
        sub.setEnabled(enabled)
        for pid, name, _n in self.media_store.playlists():
            sub.addAction(name).triggered.connect(lambda _c=False, pid=pid: self._pl_add(r, indexes, pid))
        if sub.actions():
            sub.addSeparator()
        sub.addAction("New playlist...").triggered.connect(lambda: self._pl_add_new(r, indexes))

    def _pl_add_new(self, r: SoundRow, indexes: list[int]) -> None:
        if name := self._pl_ask_name("New Playlist", "Name:", r.album.title):
            self._pl_add(r, indexes, self.media_store.create_playlist(name))

    def _pl_add(self, r: SoundRow, indexes: list[int], pid: int) -> None:
        self.media_store.add_to_playlist(pid, r.album, indexes)
        if tracks := r.album.cache.get("best"):  # so the playlist can name and play its tracks without listing again
            self.media_store.save_snapshot(r.album, tracks)
        self._pl_files.setdefault((r.album.provider, r.album.ident), list(r.files))
        self.st_status.setText(f"Added {plural(len(indexes), 'track')} to {self.media_store.playlist_name(pid)}.")
        self._pl_reload_picker(keep=True)

    # ---- settings: the two disk allocations -----------------------------------------------------
    # ---- the Browse tab: platform, year or letter ----------------------------------------------
    def _build_browse_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.br_mode = QComboBox()
        for label, key in (("Platform", "platform"), ("Year", "year"), ("Letter", "letter")):
            self.br_mode.addItem(label, key)
        self.br_value = QComboBox()
        self.br_value.setMinimumWidth(260)
        self.br_value.setMaxVisibleItems(18)
        self.br_page_label = QLabel("")
        T.bind_css(self.br_page_label, lambda: f"color: {T.fg3.name()};")
        self.br_prev = QPushButton("Previous page")
        self.br_next = QPushButton("Next page")
        for b in (self.br_prev, self.br_next):
            b.setEnabled(False)
        self.br_prev.clicked.connect(lambda: self._br_load(self._br_page - 1))
        self.br_next.clicked.connect(lambda: self._br_load(self._br_page + 1))
        bar.addWidget(self.br_mode)
        bar.addWidget(self.br_value)
        bar.addStretch(1)
        bar.addWidget(self.br_page_label)
        bar.addWidget(self.br_prev)
        bar.addWidget(self.br_next)
        bar.addWidget(source_chip("khinsider"))
        lay.addLayout(bar)
        self.br_status = hint("")
        lay.addWidget(self.br_status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 4, 8, 0)
        col.setSpacing(0)
        self.br_grid = AlbumGrid()
        self.br_empty = EmptyState("music", "Pick a platform, a year or a letter to browse KHInsider's albums.")
        self.br_more = QPushButton("Show more")
        self.br_more.setVisible(False)
        self.br_more.clicked.connect(lambda: self._br_render(more=True))
        col.addWidget(self.br_grid)
        col.addWidget(self.br_empty)
        col.addWidget(self.br_more, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addStretch(1)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        self.br_mode.currentIndexChanged.connect(lambda *_: self._br_fill_values())
        self.br_value.currentIndexChanged.connect(lambda *_: self._br_load(1) if self.br_value.currentIndex() >= 0 else None)
        self._br_fill_values()
        return tab

    def _br_fill_values(self) -> None:
        mode = self.br_mode.currentData()
        self.br_value.blockSignals(True)
        self.br_value.clear()
        if mode == "platform":
            for slug, name, count in self._br_consoles:
                self.br_value.addItem(f"{name}  ({count})", slug)
            self.br_value.setPlaceholderText("Choose a platform" if self._br_consoles else "Loading platforms...")
        elif mode == "year":
            for year in range(time.localtime().tm_year, 1969, -1):
                self.br_value.addItem(str(year), str(year))
            self.br_value.setPlaceholderText("Choose a year")
        else:
            for letter in ["0-9", *"ABCDEFGHIJKLMNOPQRSTUVWXYZ"]:
                self.br_value.addItem(letter, letter)
            self.br_value.setPlaceholderText("Choose a letter")
        self.br_value.setCurrentIndex(-1)
        self.br_value.blockSignals(False)
        if mode == "platform" and not self._br_consoles:
            self._br_load_consoles()

    def _br_load_consoles(self) -> None:
        threading.Thread(target=self._br_consoles_worker, daemon=True).start()

    def _br_consoles_worker(self) -> None:  # worker thread
        cached = self.media_store.web_get("browse:consoles", CONSOLES_TTL)
        error = ""
        if cached:
            consoles = [tuple(c) for c in cached]
        else:
            try:
                consoles = st.KHInsiderProvider().consoles()
                if consoles:
                    self.media_store.web_put("browse:consoles", [list(c) for c in consoles])
            except LinkError as exc:
                consoles, error = [], str(exc)
            except Exception as exc:  # a surprise must not leave the box on 'Loading...'
                consoles, error = [], f"{type(exc).__name__}: {exc}"
        self.ui_q.put(("st_browse", "consoles", 0, consoles, 1, error))

    def _br_load(self, page: int) -> None:
        mode, value = self.br_mode.currentData(), self.br_value.currentData()
        if not value or page < 1:
            return
        self._br_token += 1
        self.br_status.setText("Loading...")
        for b in (self.br_prev, self.br_next):
            b.setEnabled(False)
        threading.Thread(target=self._br_worker, args=(self._br_token, st.browse_path(mode, str(value)), page), daemon=True).start()

    def _br_worker(self, token: int, path: str, page: int) -> None:  # worker thread
        key = f"browse:{path}:{page}"
        error = ""
        cached = self.media_store.web_get(key, BROWSE_TTL)
        if cached:
            albums, pages = st._albums_from_json(cached["albums"]), int(cached.get("pages", 1))
        else:
            try:
                albums, pages = st.KHInsiderProvider().browse(path, page)
                if albums:
                    self.media_store.web_put(key, {"albums": st._albums_to_json(albums), "pages": pages})
            except LinkError as exc:
                albums, pages, error = [], 1, str(exc)
            except Exception as exc:
                albums, pages, error = [], 1, f"{type(exc).__name__}: {exc}"
        self.ui_q.put(("st_browse", "albums", token, albums, (page, pages), error))

    def _st_on_browse(self, kind: str, token: int, payload: list, pages, error: str) -> None:
        if kind == "consoles":
            if payload:
                self._br_consoles = payload
                if self.br_mode.currentData() == "platform":
                    self._br_fill_values()
            elif error:
                self.br_status.setText(error)
            return
        if token != self._br_token:
            return  # another choice was made meanwhile
        page, total = pages
        self._br_albums, self._br_page, self._br_pages, self._br_shown = payload, page, total, 0
        self._br_render()
        self.br_page_label.setText(f"Page {page} of {total}" if total > 1 else "")
        self.br_prev.setEnabled(page > 1)
        self.br_next.setEnabled(page < total)
        self.br_status.setText(error or (plural(len(payload), "album") if payload else "Nothing listed there."))

    def _br_render(self, more: bool = False) -> None:
        if not more:
            self.br_grid.clear()
            self._br_tiles = []
        albums = self._br_albums[self._br_shown:self._br_shown + BROWSE_TILES]
        for album in albums:
            tile = AlbumTile(album.title, album.detail, "", album.art, AUDIO_OK)
            tile.opened.connect(lambda a=album: self._st_open_album(a))
            tile.played.connect(lambda a=album: self._st_open_album(a, play=True))
            tile.moreClicked.connect(lambda w, a=album: self._lib_menu(w, a, []))
            self.br_grid.add_tile(tile)
            self._br_tiles.append((album.art, tile))
            if album.art:
                self._st_want_art(album.art)
                tile.set_cover(self._st_art_cache.get(album.art))
        self._br_shown += len(albums)
        self.br_empty.setVisible(not self._br_albums)
        self.br_more.setVisible(self._br_shown < len(self._br_albums))
        if self.br_more.isVisible():
            self.br_more.setText(f"Show more ({len(self._br_albums) - self._br_shown} left on this page)")

    # ---- the sources card: credit, and whether KHInsider is reachable -----------------------------
    def _build_source_card(self) -> Card:
        card = Card("Sources")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(source_chip("khinsider"))
        self.khi_status = QLabel("")
        self.khi_status.setWordWrap(True)
        T.bind_css(self.khi_status, lambda: f"color: {T.fg2.name()};")
        row.addWidget(self.khi_status, 1)
        card.add(row)
        buttons = QHBoxLayout()
        self.khi_check_btn = QPushButton("Check now")
        self.khi_check_btn.setToolTip("Ask KHInsider for one page to see whether it answers")
        self.khi_check_btn.clicked.connect(self._khi_check)
        clear = QPushButton("Clear Home and Browse lists")
        clear.setToolTip("Forget the lists kept for a few hours, so they are fetched again")
        clear.clicked.connect(self._media_clear_lists)
        buttons.addWidget(self.khi_check_btn)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        card.add(buttons)
        card.add(hint("Albums, covers and tracks credited to KHInsider come from its Video Game Music database. Distill asks "
                      "for about one page a second and keeps what it fetches for hours, to be kind to a volunteer-run site."))
        arch = QHBoxLayout()
        arch.setSpacing(10)
        arch.addWidget(source_chip("archive.org"))
        arch.addWidget(QLabel("Needs nothing extra. It is searched too, and takes over when KHInsider cannot."), 1)
        card.add(arch)
        self._khi_refresh_status()
        return card

    def _khi_status_text(self) -> str:
        if not st.curl_cffi_available():
            return ("curl_cffi is not installed, so KHInsider is skipped and only archive.org is used. Install it with "
                    "python -m pip install curl_cffi, then restart Distill.")
        wait = st.khinsider_wait_seconds()
        if wait:
            return f"KHInsider refused the last request (a Cloudflare check). Trying again in {max(1, round(wait / 60))} min, or press Check now."
        return "Connected the way Chrome connects (curl_cffi)."

    def _khi_refresh_status(self) -> None:
        if hasattr(self, "khi_status"):
            self.khi_status.setText(self._khi_status_text())

    def _khi_check(self) -> None:
        self.khi_check_btn.setEnabled(False)
        self.khi_status.setText("Asking KHInsider...")
        st.reset_khinsider_block()
        threading.Thread(target=self._khi_check_worker, daemon=True).start()

    def _khi_check_worker(self) -> None:  # worker thread
        try:
            st.fetch_text(st.KHI_URL + "/console-list")
            self.ui_q.put(("st_khi", True, "KHInsider answered: it is working."))
        except LinkError as exc:
            self.ui_q.put(("st_khi", False, str(exc)))
        except Exception as exc:
            self.ui_q.put(("st_khi", False, f"{type(exc).__name__}: {exc}"))

    def _st_on_khi(self, ok: bool, text: str) -> None:
        self.khi_check_btn.setEnabled(True)
        self.khi_status.setText(text)
        if ok:
            self._home_load(force=False)

    def _media_clear_lists(self) -> None:
        self.media_store.web_clear()
        self._br_consoles = []
        self._toast("Home and Browse lists cleared. They are fetched again when you open them.", "ok", 5000)
        self._home_load(force=True)

    # ---- backup and restore ---------------------------------------------------------------------
    def _build_backup_card(self) -> Card:
        card = Card("Backup")
        card.add(hint("The library, favourites, playlists, play history and the album chosen for each game, in one file. "
                      "Restoring adds to what is here and never removes anything."))
        buttons = QHBoxLayout()
        back = QPushButton("Back up library...")
        back.clicked.connect(self._media_backup)
        restore = QPushButton("Restore from backup...")
        restore.clicked.connect(self._media_restore)
        buttons.addWidget(back)
        buttons.addWidget(restore)
        buttons.addStretch(1)
        card.add(buttons)
        return card

    def _media_backup(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(self, "Back up the library", str(Path.home() / "distill-media-backup.json"),
                                                    "Backup (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self.media_store.export_data(), ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            self._toast(f"Couldn't write the backup: {exc}", "bad", 8000)
            return
        self._toast(f"Backed up to {path}", "ok", 8000)

    def _media_restore(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Restore from a backup", str(Path.home()), "Backup (*.json)")
        if not path:
            return
        try:
            added = self.media_store.import_data(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:  # json errors are ValueErrors too
            self._toast(f"Couldn't restore: {exc}", "bad", 8000)
            return
        self._lib_refresh()
        self._lib_recent_refresh()
        self._pl_reload_picker()
        self._media_refresh_budgets()
        parts = [f"{n} {name}" for name, n in added.items() if n]
        self._toast("Restored: " + (", ".join(parts) if parts else "nothing new, it was all here already") + ".", "ok", 9000)

    def _pl_export(self) -> None:
        if not self._pl_entries:
            self._toast("This list is empty, so there is nothing to export.", "info", 5000)
            return
        cur = self._pl_current()
        name = "Favourites" if cur == FAVOURITES else self.media_store.playlist_name(cur)
        safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "playlist"
        path, _filter = QFileDialog.getSaveFileName(self, "Export playlist", str(Path.home() / f"{safe}.m3u8"), "Playlist (*.m3u8 *.m3u)")
        if not path:
            return
        entries = []
        for _iid, album, pos in self._pl_entries:
            title, f = self._pl_track(album, pos)
            if f is not None:
                entries.append((f"{album.title} - {title}", f.seconds, f.url))
        self._toast("Exporting: finding each track's file address...", "info", 6000)
        threading.Thread(target=self._pl_export_worker, args=(Path(path), entries, self.st_quality.currentData()), daemon=True).start()

    def _pl_export_worker(self, path: Path, entries: list, prefer: str) -> None:  # worker thread
        out, skipped = [], 0
        for title, seconds, url in entries:
            try:
                out.append((title, seconds, url if st.is_local_path(url) else st.resolve_url(url, prefer)))
            except Exception:
                skipped += 1
        try:
            path.write_text(st.m3u_text(out), encoding="utf-8")
        except OSError as exc:
            self.ui_q.put(("st_export", str(path), 0, skipped, str(exc)))
            return
        self.ui_q.put(("st_export", str(path), len(out), skipped, ""))

    def _st_on_export(self, path: str, count: int, skipped: int, error: str) -> None:
        if error:
            self._toast(f"Couldn't write the playlist: {error}", "bad", 8000)
        else:
            self._toast(f"Exported {plural(count, 'track')} to {path}" + (f" ({skipped} could not be found)" if skipped else "")
                        + ". Streamed tracks are saved as web addresses.", "ok", 10000)

    def _media_set_cover_limit(self) -> None:
        self.media_store.set_cover_cache_limit_mb(self.cover_spin.value())
        self._st_covers.trim()
        self._media_refresh_budgets()

    def _media_clear_covers(self) -> None:
        n = self._st_covers.clear()
        self._toast(f"Removed {plural(n, 'kept cover')}.", "ok", 4000)
        self._media_refresh_budgets()

    def _build_budget_card(self) -> Card:
        card = Card("Disk space for streaming")
        card.add(hint("These are maximums. When one is full the least recently used items are removed first.",
                      tip="Saved track lists let your library open without a network request. The audio cache keeps "
                          "tracks you streamed so they replay without the network."))
        self.snap_spin = QSpinBox()
        self.snap_spin.setRange(0, 100_000)
        self.snap_spin.setSingleStep(10)
        self.snap_spin.setSuffix(" MB")
        self.snap_spin.setValue(int(self.media_store.snapshot_limit() / MB))
        self.snap_spin.editingFinished.connect(self._media_set_snapshot_limit)
        self.snap_use = QLabel("")
        T.bind_css(self.snap_use, lambda: f"color: {T.fg3.name()};")
        row = QHBoxLayout()
        label = QLabel("Saved track lists")
        label.setFixedWidth(LABEL_W)
        for w in (label, self.snap_spin, self.snap_use):
            row.addWidget(w)
        row.addStretch(1)
        card.add(row)
        self.cache_spin = QSpinBox()
        self.cache_spin.setRange(0, 1_000_000)
        self.cache_spin.setSingleStep(100)
        self.cache_spin.setSuffix(" MB")
        self.cache_spin.setSpecialValueText("Off")
        self.cache_spin.setValue(int(self.media_store.audio_cache_limit() / MB))
        self.cache_spin.editingFinished.connect(self._media_set_cache_limit)
        self.cache_use = QLabel("")
        T.bind_css(self.cache_use, lambda: f"color: {T.fg3.name()};")
        clear = QPushButton("Clear cache")
        clear.clicked.connect(self._media_clear_cache)
        row = QHBoxLayout()
        label = QLabel("Audio cache")
        label.setFixedWidth(LABEL_W)
        for w in (label, self.cache_spin, self.cache_use, clear):
            row.addWidget(w)
        row.addStretch(1)
        card.add(row)
        self.cover_spin = QSpinBox()
        self.cover_spin.setRange(0, 10_000)
        self.cover_spin.setSingleStep(50)
        self.cover_spin.setSuffix(" MB")
        self.cover_spin.setSpecialValueText("Off")
        self.cover_spin.setValue(int(self.media_store.cover_cache_limit() / MB))
        self.cover_spin.editingFinished.connect(self._media_set_cover_limit)
        self.cover_use = QLabel("")
        T.bind_css(self.cover_use, lambda: f"color: {T.fg3.name()};")
        clear_covers = QPushButton("Clear covers")
        clear_covers.clicked.connect(self._media_clear_covers)
        row = QHBoxLayout()
        label = QLabel("Covers")
        label.setFixedWidth(LABEL_W)
        for w in (label, self.cover_spin, self.cover_use, clear_covers):
            row.addWidget(w)
        row.addStretch(1)
        card.add(row)
        self._media_refresh_budgets()
        return card

    def _media_refresh_budgets(self) -> None:
        if not hasattr(self, "snap_use"):
            return
        self.snap_use.setText(f"{fmt_size(self.media_store.snapshot_bytes())} used")
        on = self.media_store.audio_cache_limit() > 0
        self.cache_use.setText(f"{fmt_size(self.media_store.audio_cache_bytes())} used" if on else "not caching")
        if hasattr(self, "cover_use"):
            self.cover_use.setText(f"{fmt_size(self._st_covers.size())} used" if self.media_store.cover_cache_limit() > 0 else "not keeping covers")

    def _media_set_snapshot_limit(self) -> None:
        dropped = self.media_store.set_snapshot_limit_mb(self.snap_spin.value())
        if dropped:
            self._toast(f"Dropped {plural(dropped, 'saved track list')} to fit. Their albums stay in the library.", "info", 6000)
        self._media_refresh_budgets()
        self._lib_refresh()

    def _media_set_cache_limit(self) -> None:
        self.media_store.set_audio_cache_limit_mb(self.cache_spin.value())
        self._media_refresh_budgets()

    def _media_clear_cache(self) -> None:
        n = self.media_store.clear_audio_cache()
        self._toast(f"Removed {plural(n, 'cached track')}.", "ok", 4000)
        self._media_refresh_budgets()

    # ---- status ---------------------------------------------------------------------------------
    def _st_status(self, r: SoundRow, text: str) -> None:
        """An album's status line (under its buttons), with a coloured symbol in front."""
        if r.view is None:
            return
        low = text.lower()
        for needle, symbol, kind in STATUS_STYLE:
            if text and needle in low:
                r.view.set_status(f"{symbol} {text}", kind if kind in ("ok", "warn", "bad") else "")
                return
        r.view.set_status(text)

    def _st_update_cards(self) -> None:
        """Keep the options hint telling where things go."""
        self._st_mode_changed(save=False)

    def _persist_columns(self) -> dict[str, list[int]]:
        return {}

    def _restore_columns(self, widths: dict[str, list]) -> None:
        return

    # ---- search ---------------------------------------------------------------------------------
    def _st_pick_folder(self) -> None:
        start = str(self._st_games_dir or self._clean(self.st_dest.text()) or "")
        if folder := QFileDialog.getExistingDirectory(self, "Games directory (holds your games or game folders)", start):
            self._st_games_dir = Path(folder)
            self._st_mode_changed()

    def _st_clear_results(self) -> None:
        for r in self._st_rows:
            for w in (r.view, r.rule):
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
        self._st_cancel_flight()
        self._st_rows = []
        self._st_extra = []
        self._st_detail = None
        self.st_grid.clear()
        self.st_grid.setVisible(False)
        self.st_back.setVisible(False)
        self.st_more_results.setVisible(False)

    def _st_search(self, _checked: bool = False) -> None:
        """The Search button / Enter: search what is typed (no game behind it)."""
        self._st_run_search(self.st_search.text(), None)

    def _st_search_game(self, game_id: str, name: str, platform: str = "", year: str = "") -> None:
        """Search for a game's soundtrack: albums for its platform and year rank higher, and an album can be remembered
        for it (see 'Use as this game's soundtrack')."""
        self._media_show_tab("Search")
        self.st_search.setText(name)
        self._st_run_search(name, (game_id, name, platform, year))

    def _st_run_search(self, text: str, ctx: tuple[str, str, str, str] | None) -> None:
        query = st.title_case(st.search_name(text.strip()))
        if not query or not text.strip():
            self.st_status.setText("Type a game name first.")
            return
        self._st_game_ctx = ctx
        platform, year = (ctx[2], ctx[3]) if ctx else ("", "")
        self._st_token += 1
        token, prefer = self._st_token, self.st_quality.currentData()
        fresh = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._st_clear_results()
        self._st_query = query
        self._st_search_note = ""
        self.st_empty.setText(f"Searching for {query}...")
        self.st_empty.setVisible(True)
        self.st_search_btn.setEnabled(False)
        self.st_status.setText("Searching...")
        self._st_update_summary()
        threading.Thread(target=self._st_search_worker, args=(token, query, prefer, fresh, platform, year), daemon=True).start()

    def _st_search_worker(self, token: int, query: str, prefer: str, fresh: bool = False, platform: str = "",
                          year: str = "") -> None:  # worker thread
        def found(i: int, res: st.GameResult) -> None:
            res.albums = st.rank_for_game(res.albums, platform, year)  # the game's own platform first
            self.ui_q.put(("st_found", token, i, res))
            # listing the tracks takes a while: on its own thread, so the search itself finishes (and the Search button
            # comes back) as soon as the albums are known
            threading.Thread(target=self._st_list_worker, args=(token, 0, res.albums[:SHOWN_ALBUMS], query, prefer),
                             daemon=True).start()
        try:
            st.find_albums([st.GameRequest(query)], on_result=found, should_stop=lambda: token != self._st_token,
                           use_cache=not fresh)
        finally:
            self.ui_q.put(("st_searched", token))

    def _st_list_worker(self, token: int, first: int, albums: list[st.Album], query: str, prefer: str) -> None:  # worker thread
        """List the tracks of these albums one after another (kind to the sites); rows are numbered from `first`."""
        for offset, album in enumerate(albums):
            if token != self._st_token:
                return
            try:
                files, error = self._st_files(query, album, prefer), ""
            except LinkError as exc:
                files, error = [], str(exc)
            except Exception as exc:  # a surprise must not leave the album stuck on 'Loading tracks...'
                files, error = [], f"{type(exc).__name__}: {exc}"
            self.ui_q.put(("st_tracks", token, first + offset, album, files, error))

    def _st_files(self, query: str, album: st.Album, prefer: str) -> list[RemoteFile]:  # worker thread
        """An album's files: from its saved track list when the library has a fresh one, else from the source (and
        a library album's saved list is then refreshed)."""
        store = self.media_store
        if snap := store.load_snapshot(album, SNAPSHOT_MAX_AGE_DAYS):
            if (adjusted := st.snapshot_for_quality(snap, prefer)) is not None:
                return st.layout(query, adjusted)
        files = st.files_for(query, album, prefer=prefer)
        if prefer == "best" and store.in_library(album) and (tracks := album.cache.get("best")):
            store.save_snapshot(album, tracks)
        return files

    # ---- results --------------------------------------------------------------------------------
    def _st_add_album(self, album: st.Album) -> SoundRow:
        r = SoundRow(album, self._st_query)
        view = AlbumView(AUDIO_OK)
        r.view = view
        view.set_info(album.title, album.detail, "", meta_source(album.provider))
        view.set_source(album.provider)
        view.playAlbum.connect(lambda rr=r: self._st_play_album(rr))
        view.saveAlbum.connect(lambda rr=r: self._st_save([(rr, list(rr.files))]))
        view.playTrack.connect(lambda i, rr=r: self._st_play_track(rr, i))
        view.saveTrack.connect(lambda i, rr=r: self._st_save([(rr, [rr.files[i]])]) if i < len(rr.files) else None)
        view.trackMenu.connect(lambda i, pos, rr=r: self._st_track_menu(rr, i, pos))
        view.moreMenu.connect(lambda btn, rr=r: self._st_album_menu(rr, btn))
        view.tickChanged.connect(self._st_update_summary)
        view.libraryToggled.connect(lambda rr=r: self._st_toggle_library(rr))
        view.favouriteToggled.connect(lambda i, rr=r: self._st_toggle_favourite(rr, i))
        view.set_in_library(self.media_store.in_library(album))
        r.rule = hairline()
        at = self.st_results.count() - 2  # before the 'more' button and the stretch
        self.st_results.insertWidget(at, view)
        self.st_results.insertWidget(at + 1, r.rule)
        self._st_rows.append(r)
        view.hide()  # the grid shows first; _st_show_detail brings a view up when its tile is clicked
        r.rule.hide()
        r.tile = AlbumTile(album.title, album.detail, provider_name(album.provider), album.art, AUDIO_OK)
        r.tile.opened.connect(lambda rr=r: self._st_show_detail(rr))
        r.tile.played.connect(lambda rr=r: self._st_tile_play(rr))
        r.tile.moreClicked.connect(lambda w, rr=r: self._st_album_menu(rr, w))
        r.tile.set_badge(self.media_store.in_library(album))
        self.st_grid.add_tile(r.tile)
        self.st_grid.setVisible(self._st_detail is None)
        self._st_want_art(album.art)
        self._st_row_cover(r)
        if self._st_game_ctx and self._is_game_choice(album):
            view.set_status("\u2605 Your choice for this game", "ok")
            r.tile.set_meta("\u2605 Your choice for this game", lit=True)
        return r

    # ---- the results grid and one open album ----------------------------------------------------
    def _st_show_grid(self) -> None:
        """Back from one album to the albums found."""
        self._st_detail = None
        self.st_back.setVisible(False)
        for r in self._st_rows:
            for w in (r.view, r.rule):
                if w is not None:
                    w.setVisible(False)
        self.st_grid.setVisible(bool(self._st_rows))
        self._st_show_more_button()
        self.st_scroll.verticalScrollBar().setValue(0)
        self._st_update_summary()

    def _st_show_detail(self, r: SoundRow) -> None:
        """Open one album: its cover, buttons and every track. With several albums found, a button leads back."""
        self._st_cancel_flight()
        lifted = None  # the tile's cover and where it is, for it to glide to its place on the album's page
        if r.tile is not None and r.view is not None and r.tile.cover.isVisible() and self._st_detail is None:
            area = self.st_scroll.viewport()
            box = flight.box_of(r.tile.cover, area)
            if area.rect().intersects(box):
                lifted = (flight.picture_of(r.tile.cover), box)
        self._st_detail = r
        self.st_grid.setVisible(False)
        self.st_more_results.setVisible(False)
        for other in self._st_rows:
            if other.view is not None:
                other.view.setVisible(other is r and not other.removed)
            if other.rule is not None:
                other.rule.setVisible(False)
        self.st_back.setVisible(len([x for x in self._st_rows if not x.removed]) > 1)
        self.st_scroll.verticalScrollBar().setValue(0)
        self._st_update_summary()
        if lifted is not None:
            self._st_fly_cover(r, *lifted)

    def _st_cancel_flight(self) -> None:
        running = getattr(self, "_st_flight", None)
        if running is not None:
            running.finish()  # lets go of the cover it was holding hidden
            self._st_flight = None

    def _st_fly_cover(self, r: SoundRow, picture, start) -> None:
        """The cover on the tile glides to its place in the top left of the album's page; the page's own copy waits, hidden, until it lands."""
        area = self.st_scroll.viewport()
        cover = r.view.cover
        flight.hold(cover)
        self._st_flight = flight.Flight(area, picture, start, lambda: flight.box_of(cover, area),
                                        done=lambda: flight.release(cover)).go()

    def _st_tile_play(self, r: SoundRow) -> None:
        """The play button on a tile: start the album; if its tracks are not listed yet, open it and start when they arrive."""
        if r.files:
            self._st_play_album(r)
        else:
            self._st_autoplay = r.album.ident
            self._st_show_detail(r)

    def _st_mark_library(self, r: SoundRow, on: bool) -> None:
        if r.view is not None:
            r.view.set_in_library(on)
        if r.tile is not None:
            r.tile.set_badge(on)

    def _is_game_choice(self, album: st.Album) -> bool:
        chosen = self.media_store.game_album(self._st_game_ctx[0]) if self._st_game_ctx else None
        return chosen is not None and (chosen.provider, chosen.ident) == (album.provider, album.ident)

    def _st_use_for_game(self, r: SoundRow) -> None:
        if not self._st_game_ctx:
            return
        game_id, name, _platform, _year = self._st_game_ctx
        if self._is_game_choice(r.album):
            self.media_store.clear_game_album(game_id)
            r.view.set_status("")
            self.st_status.setText(f"Forgot the soundtrack chosen for {name}.")
        else:
            self.media_store.set_game_album(game_id, r.album)
            r.view.set_status("\u2605 Your choice for this game", "ok")
            self.st_status.setText(f"{r.album.title} is now {name}'s soundtrack: its Soundtrack button opens it.")

    def _st_on_found(self, row: int, res: st.GameResult) -> None:
        if not res.albums:
            self.st_empty.setText(f"Search failed.\n{res.error}" if res.error else
                                  f"Nothing found for {self._st_query}.\nTry the game's full name, or fewer words.")
            return
        self.st_empty.setVisible(False)
        self._st_search_note = f" Some sources failed: {res.error}" if res.error else ""
        added = [self._st_add_album(album) for album in res.albums[:SHOWN_ALBUMS]]
        if len(added) == 1:  # one album found: no grid to choose from
            self._st_show_detail(added[0])
        self._st_extra = res.albums[SHOWN_ALBUMS:]
        self._st_show_more_button()

    def _st_show_more_button(self) -> None:
        self.st_more_results.setVisible(bool(self._st_extra) and self._st_detail is None)
        self.st_more_results.setText(f"Show {min(len(self._st_extra), SHOWN_ALBUMS)} more of {len(self._st_extra)} results")

    def _st_show_more(self) -> None:
        batch, self._st_extra = self._st_extra[:SHOWN_ALBUMS], self._st_extra[SHOWN_ALBUMS:]
        first = len(self._st_rows)
        for album in batch:
            self._st_add_album(album)
        self._st_show_more_button()
        threading.Thread(target=self._st_list_worker, args=(self._st_token, first, batch, self._st_query,
                                                             self.st_quality.currentData()), daemon=True).start()

    def _st_on_tracks(self, row: int, album: st.Album, files: list[RemoteFile], error: str) -> None:
        if row >= len(self._st_rows) or self._st_rows[row].album is not album:
            return
        r = self._st_rows[row]
        r.files = files
        self._st_want_art(album.art)
        self._st_row_cover(r)
        r.view.set_info(album.title, album.detail, "", meta_source(album.provider))
        r.view.set_source(album.provider)
        if error or not files:
            r.view.set_tracks([])
            r.view.set_note(error or "No tracks found.")
            if r.tile is not None:
                r.tile.set_meta("No tracks found", lit=False)
            return
        known = [f.size for f in files if f.size is not None]
        length = st.fmt_total(sum(f.seconds or 0 for f in files))
        r.view.set_info(album.title, album.detail,
                        plural(len(files), "song") + (f"  \u00b7  {length}" if length else "")
                        + (f"  \u00b7  {fmt_size(sum(known))}" if known else ""),
                        meta_source(album.provider))  # (the source is a chip of its own, so the facts line stays short)
        r.view.set_source(album.provider)
        if r.tile is not None and not (self._st_game_ctx and self._is_game_choice(album)):
            r.tile.set_meta(plural(len(files), "song") + (f"  \u00b7  {fmt_size(sum(known))}" if known else ""))
        infos = _track_infos(files)
        liked = self.media_store.favourite_positions(album)
        for i, info in enumerate(infos):
            info.liked = i in liked
        r.view.set_tracks(infos)
        if have := self._st_existing(self._st_game(r)):
            self._st_status(r, f"Already have {plural(have, 'file')}")
        self._st_sync_icons()
        self._st_update_summary()
        if self._st_autoplay and self._st_autoplay == album.ident:
            self._st_autoplay = None
            self._st_play_track(r, 0)

    def _st_on_searched(self) -> None:
        self.st_search_btn.setEnabled(True)
        n = len([r for r in self._st_rows if not r.removed])
        if n:
            self.st_status.setText(f"{plural(n, 'album')} found. "
                                   + ("Click one to open it, or press its play button." if n > 1 and self._st_detail is None else
                                      "Play a track to stream it; hover a track to save it.") + self._st_search_note)
        elif not self.st_empty.isVisible():
            self.st_empty.setVisible(True)
        self._st_update_summary()

    def _st_row_cover(self, r: SoundRow) -> None:
        pix = self._st_art_cache.get(r.album.art)
        if r.view is not None:
            r.view.set_cover(pix)
        if r.tile is not None:
            r.tile.set_cover(pix)

    def _st_want_art(self, url: str) -> None:
        if url and url not in self._st_art_cache and url not in self._st_art_loading:
            self._st_art_loading.add(url)
            self._st_art_pool.submit(self._st_art_worker, url)

    # ---- ticking and saving ---------------------------------------------------------------------
    def _st_game(self, r: SoundRow) -> str:
        """The name its folder gets: the album's own title in Standard mode, the game searched for in RomM mode."""
        return r.query if self._st_mode() == st.SAVE_ROMM else r.album.title

    def _st_select(self, on: bool) -> None:
        open_one = self._st_detail if on and self._st_detail is not None else None  # selecting: the album being looked at
        for r in self._st_rows:
            if r.view is not None and r.files and not r.removed and (open_one is None or r is open_one):
                r.view.set_all_checked(on)

    def _st_toggle_select_all(self) -> None:
        """One button for both: Select all when nothing is selected, Deselect all when something is."""
        self._st_select(not self._st_ticked())

    def _st_ticked(self) -> list[SoundRow]:
        return [r for r in self._st_rows if r.files and not r.removed and r.view is not None and r.view.checked_indices()]

    def _st_checked_files(self, r: SoundRow) -> list[RemoteFile]:
        return [r.files[i] for i in r.view.checked_indices() if i < len(r.files)] if r.view is not None else []

    def _st_update_summary(self) -> None:
        picks = self._st_ticked()
        files = [f for r in picks for f in self._st_checked_files(r)]
        size = sum(f.size or 0 for f in files)
        self.st_save_selected.setEnabled(bool(files))
        something = bool(picks)
        self.st_select_btn.setText("Deselect all" if something else "Select all")
        self.st_select_btn.setEnabled(something or any(r.files and not r.removed for r in self._st_rows))
        self.st_summary.setText(f"{plural(len(files), 'track')} selected from {plural(len(picks), 'album')}" + (f"  |  {fmt_size(size)}" if size else "")
                                if picks else "")

    def _st_download(self) -> None:
        picks = self._st_ticked()
        if not picks:
            self.st_status.setText("Select some tracks first (hover a track), or press an album's save button.")
            return
        self._st_save([(r, self._st_checked_files(r)) for r in picks], batch=True)

    def _st_refresh_status(self) -> None:
        """While a download runs, show each started album's progress."""
        for r in self._st_rows:
            if not r.started or not r.saved:
                continue
            rows = [i for f in r.saved if (i := self.dl_model.row_of(f)) is not None]
            if not rows:
                continue
            done = sum(self.dl_model.is_finished(i) for i in rows)
            self._st_status(r, "Done" if done == len(rows) else f"{done} / {len(rows)} downloaded")

    # ---- save mode and places -------------------------------------------------------------------
    def _st_mode(self) -> str:
        return st.SAVE_ROMM if self.st_mode.isChecked() else st.SAVE_STANDARD

    def _st_default_dir(self) -> Path:
        return self._dl_default_dir()

    def _st_mode_changed(self, save: bool = True) -> None:
        romm = self._st_mode() == st.SAVE_ROMM
        for lab, active in ((self.st_mode_off, not romm), (self.st_mode_on, romm)):
            font = lab.font()
            font.setBold(active)
            lab.setFont(font)
        self.st_dest.setEnabled(not romm)
        self.st_folder_btn.setVisible(romm)
        if romm:
            where = str(self._st_games_dir) if self._st_games_dir else "not chosen yet - press Games directory..."
            self.st_mode_hint.setText(f"RomM: tracks go in a 'soundtrack' folder beside each game. Games directory: {where}")
        else:
            self.st_mode_hint.setText("Off: tracks go in <Save to>/Soundtracks/<Album>/. A blank Save to uses your Downloads folder.")
        self._st_update_undo()
        if save:
            self._st_prefs_update({"mode": self._st_mode(), "games_dir": str(self._st_games_dir or ""),
                                   "tag": self.st_tag.isChecked()})

    def _st_scan(self) -> list[st.GameEntry] | None:
        return st.scan_games(self._st_games_dir) if self._st_games_dir and self._st_games_dir.is_dir() else None

    def _st_existing(self, game: str) -> int:
        if self._st_mode() == st.SAVE_ROMM:
            entries = self._st_scan()
            return st.already_there(self._st_games_dir, game, st.SAVE_ROMM, entries) if entries is not None else 0
        raw = self._clean(self.st_dest.text())
        return st.already_there(Path(raw) if raw else self._st_default_dir(), game)

    def _st_update_undo(self) -> None:
        self.st_undo_btn.setEnabled(st.saves_depth() > 0 or org.undo_depth("soundtrack") > 0)

    def _st_undo_save(self) -> None:
        if self._dl_runner() is not None:
            self.st_status.setText("Wait for the running download to finish (or stop it), then undo.")
            return
        res = st.undo_last_save()
        if res is None and (moved := org.undo_last("soundtrack")) is not None:
            res = (0, moved[0], moved[1])
        if res is None:
            self.st_status.setText("Nothing to undo.")
        else:
            removed, restored, problems = res
            bits = [f"removed {plural(removed, 'track file')}"] + ([f"put back {plural(restored, 'game')}"] if restored else [])
            self.st_status.setText("Undid the last save: " + ", ".join(bits)
                                   + (f". {plural(len(problems), 'problem')}: {problems[0]}" if problems else "."))
        for r in self._st_rows:
            if r.started:
                r.started, r.saved = False, []
                self._st_status(r, "")
        self._st_update_undo()

    def _st_open_log(self) -> None:
        path = org.run_log_file()
        if not path.exists():
            self.st_status.setText("No game files have been moved yet, so there is no log.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ---- saving (an album, ticked tracks, or one track) -----------------------------------------
    def _st_save(self, entries: list[tuple[SoundRow, list[RemoteFile]]], batch: bool = False,
                 other_folder: bool = False, resolved: bool = False) -> None:
        """Save these (album row, files) pairs. khinsider tracks first have their real file addresses looked up
        (and an album that cannot be had there is taken from archive.org instead). Standard mode files them under
        Soundtracks/, RomM mode beside the game. `other_folder` asks where to save (Standard layout)."""
        on_disk = [r for r, _fs in entries if r.album.provider == st.LOCAL_SOURCE]
        for r in on_disk:
            self._st_status(r, "Already saved on this computer")
        entries = [(r, fs) for r, fs in entries if fs and r.album.provider != st.LOCAL_SOURCE]
        if not entries:
            self.st_status.setText("Already saved on this computer." if on_disk else
                                   "Nothing to save: those albums have no tracks listed yet.")
            return
        if not resolved and any(st.is_khinsider_page(f.url) for _r, fs in entries for f in fs):
            self._st_resolve_token += 1
            token = self._st_resolve_token
            self._st_pending[token] = (entries, batch, other_folder)
            prefer = self.st_quality.currentData()
            jobs = []
            for i, (r, fs) in enumerate(entries):
                self._st_status(r, "Finding files...")
                jobs.append((i, self._st_game(r), r.album, fs, len(fs) == len(r.files)))
            self.st_status.setText("Finding the files...")
            threading.Thread(target=self._st_resolve_worker, args=(token, jobs, prefer), daemon=True).start()
            return
        mode = st.SAVE_STANDARD if other_folder else self._st_mode()
        if mode == st.SAVE_ROMM:  # two albums of one game would share a folder and collide: the first goes, the rest wait
            seen, kept = set(), []
            for r, fs in entries:
                key = self._st_game(r).casefold()
                if key not in seen:
                    seen.add(key)
                    kept.append((r, fs))
            if len(kept) < len(entries):
                self.st_status.setText("RomM mode: one album per game at a time. Saved the first; save the other next.")
            entries = kept
        items = [(self._st_game(r), fs) for r, fs in entries]
        scan = None
        if mode == st.SAVE_ROMM:
            if self._st_games_dir is None or not self._st_games_dir.is_dir():
                self._st_pick_games_dir_only()
            scan = self._st_scan()
            if scan is None:
                self.st_status.setText("RomM mode needs a games directory: open Options and press Games directory...")
                return
            dest = self._st_games_dir
        elif other_folder:
            start = self._clean(self.st_dest.text()) or str(self._st_default_dir())
            folder = QFileDialog.getExistingDirectory(self, "Save Soundtracks to", start)
            if not folder:
                return
            dest = Path(folder)
        else:
            raw = self._clean(self.st_dest.text())
            dest = Path(raw) if raw else self._st_default_dir()
        picks: dict[str, st.GameEntry] = {}
        for r, _ in entries:
            if mode == st.SAVE_ROMM and r.match_folder and scan is not None:
                if hit := next((e for e in scan if e.folder == r.match_folder), None):
                    picks[self._st_game(r)] = hit
        plan = st.plan_save(items, mode, dest, scan, picks)

        if len(items) == 1 and plan.ambiguous:
            game, hits = next(iter(plan.ambiguous.items()))
            labels = [str(e.folder.relative_to(dest)) for e in hits]
            label, ok = msgbox.get_item(self, "Which Game?", f"'{game}' matches more than one game:", labels, 0, False)
            if not ok:
                return
            plan = st.plan_save(items, mode, dest, scan, {**picks, game: hits[labels.index(label)]})
        if not plan.files and mode == st.SAVE_ROMM and len(items) == 1:
            why = plan.skipped[0][1] if plan.skipped else "no match"
            ask = msgbox.question(self, "Game Not Found", f"'{items[0][0]}': {why}.\n\nSave it to your Downloads folder instead?")
            if ask != QMessageBox.StandardButton.Yes:
                return
            mode, dest = st.SAVE_STANDARD, self._st_default_dir()
            items = [(entries[0][0].album.title, items[0][1])]
            plan = st.plan_save(items, mode, dest)
        if not plan.files:
            self.st_status.setText("Nothing saved: " + "; ".join(f"{g}: {why}" for g, why in plan.skipped[:3]))
            return
        if mode == st.SAVE_ROMM and (batch or plan.moves or plan.skipped):
            if not self._st_confirm(plan, dest):
                return
        runner = self._dl_runner()
        if runner is not None and self._dl_dest_path() != dest:
            self.st_status.setText(f"A download to {self._dl_dest_path()} is still running. Wait for it to finish, then save again.")
            return
        failed = self._st_apply_moves(plan)
        if not plan.files:
            self.st_status.setText("Nothing saved: " + "; ".join(f"{g}: {why}" for g, why in failed[:3]))
            return

        self.dl_dest.setText(str(dest))
        if not other_folder and mode == st.SAVE_STANDARD and not self._clean(self.st_dest.text()):
            self.st_dest.setText(str(dest))
        notes = plan.skipped + failed
        self._st_notes, self._st_failed_rows = notes, []
        motion.fade(self.st_retry_btn, False)
        names = [g for g, _ in items]
        for (r, _), game in zip(entries, names):
            if game in plan.by_game:
                r.saved, r.started, r.finalized = plan.by_game[game], True, False
                self._st_status(r, "Queued")
            elif why := dict(notes).get(game):
                self._st_status(r, f"Skipped: {why}")

        index = {(rf.url, rf.name): i for i, rf in enumerate(self.dl_files)}
        new_files, redo = [], []
        for rf in plan.files:
            try:
                path = DownloadRunner.locate(rf, dest, False)
            except ValueError:
                continue
            if not path.exists() and not path.with_name(path.name + ".part").exists():
                new_files.append(path)
            if (rf.url, rf.name) in index and not path.exists():
                redo.append(index[(rf.url, rf.name)])
        rows = self._dl_append(plan.files, [], by_name=True) + [i for i in redo if i not in self.dl_model.removed]
        self._st_mode_changed(save=False)
        skipped = (f" Skipped {len(notes)}: " + "; ".join(f"{g} ({why})" for g, why in notes[:3])) if notes else ""
        if not rows:
            self.st_status.setText("Those tracks are already saved there (see Files & Torrents)." + skipped)
            for r, _ in entries:
                r.finalized = True
            return
        st.record_save(new_files, dest, moved=bool(plan.moves))
        self._st_update_undo()
        self.st_status.setText(f"Saving {plural(len(rows), 'track')} to {dest}. Progress is in the bar below." + skipped)
        self._dl_start_rows(rows)

    def _st_resolve_worker(self, token: int, jobs: list, prefer: str) -> None:  # worker thread
        out = []
        for i, game, album, files, whole in jobs:
            new, problems = st.resolve_files(files, prefer)
            note = ""
            if len(new) < len(files):
                fb = st.archive_fallback(game, album.title, prefer) if whole and album.provider == st.KHInsiderProvider.name else None
                if fb is not None:
                    new, note = fb[1], "Saved from archive.org instead"
                elif problems:
                    note = problems[0]
            out.append((i, new, note))
        self.ui_q.put(("st_resolved", token, out))

    def _st_on_resolved(self, token: int, out: list) -> None:
        pending = self._st_pending.pop(token, None)
        if pending is None:
            return
        entries, batch, other_folder = pending
        ready = []
        for i, files, note in out:
            r = entries[i][0]
            if files:
                ready.append((r, files))
                self._st_status(r, "")
            else:
                self._st_status(r, f"Failed: {note or 'no files found'}")
            if note and files:
                self.st_status.setText(f"{r.album.title}: {note}.")
        if ready:
            self._st_save(ready, batch, other_folder, resolved=True)
        else:
            self.st_status.setText("Couldn't find files for those tracks on khinsider or archive.org.")

    # ---- when the run ends: summary, retry, tags ------------------------------------------------
    def _st_on_run_done(self, sm) -> None:
        pending = [r for r in self._st_rows if r.started and not r.finalized and r.saved]
        if not pending:
            return
        saved_games = failed_games = tracks = 0
        self._st_failed_rows, jobs = [], []
        dest = self._dl_dest_path()
        for r in pending:
            rows = [(f, self.dl_model.row_of(f)) for f in r.saved]
            ok = [f for f, i in rows if i is not None and self.dl_model.is_finished(i)]
            bad = [i for _f, i in rows if i is not None and self.dl_model.status[i].startswith("Failed")]
            r.finalized = not bad
            tracks += len(ok)
            self._st_failed_rows += bad
            if bad:
                failed_games += 1
                self._st_status(r, f"{len(bad)} failed, {len(ok)} saved")
            else:
                saved_games += 1
                self._st_status(r, "Done")
            if ok and dest is not None and self.st_tag.isChecked():
                jobs.append((r.album, ok))
        parts = [f"Saved {plural(tracks, 'track')} from {plural(saved_games + failed_games, 'album')}"]
        if failed_games:
            parts.append(f"{plural(len(self._st_failed_rows), 'track')} failed")
        if self._st_notes:
            parts.append(f"{len(self._st_notes)} skipped")
        self.st_status.setText(", ".join(parts) + ".")
        motion.fade(self.st_retry_btn, bool(self._st_failed_rows))
        if saved_games and not failed_games and dest is not None:
            self._toast(f"Saved {plural(tracks, 'track')} from {plural(saved_games, 'album')}.", "ok", 20000,
                        ("Open folder", lambda d=dest: self._st_open_saved(d)))
        if jobs and dest is not None:
            threading.Thread(target=self._st_finish_worker, args=(jobs, dest), daemon=True).start()

    def _st_open_saved(self, dest: Path) -> None:
        target = dest / st.STANDARD_ROOT if self._st_mode() == st.SAVE_STANDARD and (dest / st.STANDARD_ROOT).is_dir() else dest
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _st_retry_failed(self) -> None:
        rows, self._st_failed_rows = self._st_failed_rows, []
        motion.fade(self.st_retry_btn, False)
        if rows:
            self.st_status.setText(f"Retrying {plural(len(rows), 'track')}...")
            self._dl_start_rows(rows)

    def _st_finish_worker(self, jobs: list[tuple[st.Album, list[RemoteFile]]], dest: Path) -> None:  # worker thread
        """Tag the saved files and save each album's cover beside its tracks."""
        made: list[str] = []
        done = skipped = 0
        for album, files in jobs:
            cover, mime = b"", "image/jpeg"
            if album.art:
                try:
                    cover = st.fetch_cover(album.art, ART_MAX_BYTES)  # (the sharpest one: a list's tiny picture is not what to tag the tracks with)
                    mime = st.image_mime(cover) or mime
                except Exception:
                    cover = b""
            info = tags.TagInfo(album.title, album.detail, cover or None, mime, len(files))
            folders = set()
            for rf in files:
                try:
                    path = DownloadRunner.locate(rf, dest, False)
                except ValueError:
                    continue
                folders.add(path.parent.parent if tags.DISC_RE.search(path.parent.name) else path.parent)
                outcome = tags.tag_file(path, info)
                done += outcome == "tagged"
                skipped += outcome != "tagged"
            if cover:
                for folder in folders:
                    if saved := tags.save_cover_file(folder, cover, mime):
                        made.append(str(saved))
        note = f"Tagged {plural(done, 'file')}." if done else ""
        if skipped and not tags.available():
            note = "Install the 'mutagen' package to tag files; covers were still saved."
        self.ui_q.put(("st_finished", note, made))

    def _st_on_finished(self, text: str, made: list[str]) -> None:
        self._st_scan_local()  # what was just saved shows up in the library as a saved album
        if made:
            st.add_to_last_save([Path(m) for m in made])
        if text:
            self.st_status.setText(self.st_status.text() + " " + text)

    def _st_pick_games_dir_only(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Games directory (holds your games or game folders)",
                                                  str(self._st_games_dir or ""))
        if folder:
            self._st_games_dir = Path(folder)
            self._st_mode_changed()

    def _st_confirm(self, plan: st.SavePlan, dest: Path) -> bool:
        moved = [g for g in plan.by_game if g in plan.moves]
        lines = [f"{plural(len(plan.by_game), 'soundtrack')} will be added in 'soundtrack' folders under {dest}."]
        if moved:
            lines.append(f"{plural(len(moved), 'game file')} will first be moved into a folder of their own (can be undone).")
        if plan.skipped:
            lines.append(f"{plural(len(plan.skipped), 'game')} will be skipped.")
        detail = []
        if moved:
            detail += ["Games moved into their own folder:"] + [f"  {g}" for g in moved] + [""]
        if plan.skipped:
            detail += ["Skipped:"] + [f"  {g}: {why}" for g, why in plan.skipped]
        choice, _tick = msgbox.ask(self, "question", "Save soundtracks beside games", "\n\n".join(lines),
                                   [("OK", "accept"), ("Cancel", "reject")], default=0, escape=1, details="\n".join(detail))
        return choice == 0

    def _st_apply_moves(self, plan: st.SavePlan) -> list[tuple[str, str]]:
        if not plan.moves:
            return []
        findings = [org.Finding(game, ops=ops) for game, ops in plan.moves.items()]
        _ok, failures = org.apply_findings("soundtrack", findings, lambda _m: None, lambda *_: None, threading.Event())
        failed = [(game, why) for game, why in failures]
        for game, _why in failed:
            plan.drop(game)
        return failed

    # ---- menus ----------------------------------------------------------------------------------
    def _st_album_menu(self, r: SoundRow, button) -> None:
        menu = QMenu(self)
        ready = bool(r.files)
        menu.addAction("Save album").triggered.connect(lambda: self._st_save([(r, list(r.files))]))
        menu.actions()[0].setEnabled(ready)
        other = menu.addAction("Save album to another folder...")
        other.setEnabled(ready)
        other.triggered.connect(lambda: self._st_save([(r, list(r.files))], other_folder=True))
        for text, nxt in (("Play next", True), ("Add to queue", False)):
            act = menu.addAction(text)
            act.setEnabled(ready and AUDIO_OK)
            act.triggered.connect(lambda _c=False, nxt=nxt: self._st_enqueue(r, list(range(len(r.files))), nxt))
        self._pl_menu(menu, r, list(range(len(r.files))), ready)
        menu.addAction("Open save folder").triggered.connect(lambda: self._st_open_folder(r))
        more = menu.addAction("More like this")
        more.setEnabled("info" in r.album.cache)  # known once its page has been read
        more.triggered.connect(lambda: self._st_related(r))
        if self._st_game_ctx:
            menu.addAction("Forget as this game's soundtrack" if self._is_game_choice(r.album)
                           else "Use as this game's soundtrack").triggered.connect(lambda: self._st_use_for_game(r))
        menu.addAction("Select all tracks").triggered.connect(lambda: r.view.set_all_checked(True))
        if self._st_mode() == st.SAVE_ROMM:
            menu.addAction("Match to game...").triggered.connect(lambda: self._st_match_game(r))
            if r.match_folder:
                menu.addAction("Clear the matched game").triggered.connect(lambda: self._st_clear_match(r))
        menu.addSeparator()
        menu.addAction("Remove from the list").triggered.connect(lambda: self._st_remove_games([r]))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        menu.deleteLater()

    def _st_track_menu(self, r: SoundRow, i: int, pos) -> None:
        if i >= len(r.files):
            return
        menu = QMenu(self)
        if AUDIO_OK:
            menu.addAction("Play next").triggered.connect(lambda: self._st_enqueue(r, [i], True))
            menu.addAction("Add to queue").triggered.connect(lambda: self._st_enqueue(r, [i], False))
        self._pl_menu(menu, r, [i], True)
        if AUDIO_OK:
            menu.addSeparator()
        menu.addAction("Save track").triggered.connect(lambda: self._st_save([(r, [r.files[i]])]))
        menu.addAction("Save track to another folder...").triggered.connect(
            lambda: self._st_save([(r, [r.files[i]])], other_folder=True))
        menu.exec(pos)
        menu.deleteLater()

    def _st_match_game(self, r: SoundRow) -> None:
        entries = self._st_scan()
        if not entries:
            self.st_status.setText("Choose a Games directory first (Options > Games directory...).")
            return
        labels = {}
        for e in sorted(entries, key=lambda e: e.folder.name.casefold()):
            rel = str(e.folder.relative_to(self._st_games_dir)) + ("" if e.is_dir else "  (loose file)")
            labels[rel] = e
        current = next((i for i, e in enumerate(labels.values()) if e.folder == r.match_folder), 0)
        label, ok = msgbox.get_item(self, "Match to Game", f"Which game is '{r.album.title}' the soundtrack of?",
                                    list(labels), current, False)
        if ok and label in labels:
            r.match_folder = labels[label].folder
            self._st_status(r, f"Matched: {labels[label].folder.name}")

    def _st_clear_match(self, r: SoundRow) -> None:
        r.match_folder = None
        self._st_status(r, "")

    def _st_remove_games(self, rows: list[SoundRow]) -> None:
        for r in rows:  # hidden, not deleted: workers report by row number
            r.removed = True
            for w in (r.view, r.rule, r.tile):
                if w is not None:
                    w.setVisible(False)
        if self._st_detail is not None and self._st_detail.removed:
            self._st_show_grid()
        self.st_grid.reflow()
        self._st_update_summary()

    def _st_open_folder(self, r: SoundRow) -> None:
        mode = self._st_mode()
        root = self._st_games_dir if mode == st.SAVE_ROMM else Path(self._clean(self.st_dest.text()) or self._st_default_dir())
        folder = st.target_folder(root, self._st_game(r), mode, self._st_scan() if mode == st.SAVE_ROMM else None)
        while folder is not None and not folder.exists() and folder != folder.parent:
            folder = folder.parent
        if folder is None or not folder.exists():
            self.st_status.setText("Nothing has been saved for that album yet.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


    # ---- previews -----------------------------------------------------------------------------
    # The queue holds plain (title, game, url) tuples, not tree rows, so playback carries on when the person changes
    # page or searches again; the sidebar's NowPlaying widget is the control surface.
    @property
    def _st_queue(self) -> list:
        return self.pq.items

    @property
    def _st_index(self) -> int:
        return self.pq.current_index

    def _st_url_playing(self) -> str | None:
        item = self.pq.current()
        return item.url if item else None

    def _st_is_playing(self) -> bool:
        return bool(AUDIO_OK and self._st_player is not None
                    and self._st_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)

    def _st_sync_icons(self) -> None:
        """Show which track (and album) is playing: its play button is a square, its row is highlighted."""
        if not AUDIO_OK:
            return
        playing = self._st_is_playing()
        cur = self.pq.current()
        for r in self._st_rows:
            if r.view is None:
                continue
            mine = cur is not None and cur.source == r.album.provider and cur.ident == r.album.ident
            r.view.set_playing(cur.position if mine else None, playing, mine)

    def _st_prefs_update(self, changes: dict) -> None:
        prefs = st.load_prefs()
        prefs.update(changes)
        st.save_prefs(prefs)

    def _st_output_level(self) -> float:
        """The linear level for the audio output: a slider is perceptual, so it is converted (and 0 when muted)."""
        level, muted = getattr(self, "_st_volume", 0.8), getattr(self, "_st_muted", False)
        if muted or level <= 0:
            return 0.0
        try:
            return float(QAudio.convertVolume(level, QAudio.VolumeScale.LogarithmicVolumeScale,
                                              QAudio.VolumeScale.LinearVolumeScale))
        except Exception:  # an older Qt without the converter: the slider is used as it is
            return level

    def _st_set_volume(self, level: float, muted: bool) -> None:
        self._st_volume, self._st_muted = level, muted
        if self._st_output is not None:
            self._st_output.setVolume(self._st_output_level())
        self._st_prefs_update({"volume": level, "muted": muted})

    def _st_player_get(self):
        """The media player, made the first time a preview is asked for."""
        if self._st_player is None:
            player = QMediaPlayer(self)
            output = QAudioOutput(self)
            output.setVolume(self._st_output_level())
            player.setAudioOutput(output)
            player.mediaStatusChanged.connect(self._st_media_status)
            player.errorOccurred.connect(self._st_player_error)
            player.playbackStateChanged.connect(lambda state: (self.now_playing.set_playing(
                state == QMediaPlayer.PlaybackState.PlayingState), self._st_sync_icons()))
            player.positionChanged.connect(self.now_playing.set_position)
            player.positionChanged.connect(self._st_position_tick)
            player.durationChanged.connect(self.now_playing.set_duration)
            self._st_player, self._st_output = player, output
            self._st_vinyl_get()  # the record sounds load now, so the first hold of the disc has no wait
        return self._st_player

    def _st_play_album(self, r: SoundRow) -> None:
        """The big play button: start the album, or pause / resume it when it is the one playing."""
        cur = self.pq.current()
        if cur is not None and cur.source == r.album.provider and cur.ident == r.album.ident:
            self._st_toggle_pause()
        else:
            self._st_play_track(r, 0)

    def _st_play_track(self, r: SoundRow, i: int) -> None:
        """Play this track (it streams) and queue the rest of its album; pressing the playing one pauses it."""
        if not r.files or not 0 <= i < len(r.files):
            return
        cur = self.pq.current()
        if cur is not None and cur.source == r.album.provider and cur.ident == r.album.ident and cur.position == i:
            self._st_toggle_pause()
            return
        self.pq.set(self._st_items_for(r, list(range(len(r.files)))), i)
        self._st_play_index(self.pq.current_index)

    def _st_play_index(self, index: int) -> None:
        if not 0 <= index < len(self._st_queue):
            self._st_stop()
            return
        title, game, url, art = self.pq.items[index][:4]
        player = self._st_player_get()
        player.stop()
        self._st_stream_token += 1
        token = self._st_stream_token
        np = self.now_playing
        np.show_track(title, game)
        np.set_can_skip(self.pq.can_step(-1), self.pq.can_step(1))
        np.set_duration(self.pq.items[index].seconds * 1000)  # the length the source gave, until the audio says its own
        np.set_position(0)
        np.set_playing(True)
        motion.slide(np, True)  # the player grows up out of the sidebar
        self._st_show_art(art)
        self._st_sync_icons()
        prefer = self.st_quality.currentData()
        self._st_cache_key = f"{prefer}|{url}"
        self._queue_refresh()
        self._st_save_queue()
        self._st_prefetch_next()
        if st.is_local_path(url):  # saved on this computer: no lookup, no cache
            self._st_start_stream(url, local=True)
            return
        if (cached := self.media_store.cache_path(self._st_cache_key)) is not None:  # streamed before: play from disk
            self._st_start_stream(str(cached), local=True)
            return
        if st.is_khinsider_page(url):  # the file address is on the track's page: look it up, then stream
            threading.Thread(target=self._st_stream_worker, args=(token, index, url, prefer), daemon=True).start()
        else:
            self._st_start_stream(url)

    def _st_prefetch_next(self) -> None:
        """Look up the next track's file address while this one plays, so the gap between them stays short."""
        nxt = self.pq.peek_next()
        if nxt < 0:
            return
        url = self.pq.items[nxt].url
        if st.is_khinsider_page(url):
            prefer = self.st_quality.currentData()
            threading.Thread(target=lambda: self._st_safe_resolve(url, prefer), daemon=True).start()

    @staticmethod
    def _st_safe_resolve(url: str, prefer: str) -> None:  # worker thread
        try:
            st.resolve_url(url, prefer)
        except Exception:  # only a head start: playing it later will report any problem
            pass

    def _st_stream_worker(self, token: int, index: int, url: str, prefer: str) -> None:  # worker thread
        try:
            direct, error = st.resolve_url(url, prefer), ""
        except LinkError as exc:
            direct, error = "", str(exc)
        except Exception as exc:
            direct, error = "", f"{type(exc).__name__}: {exc}"
        self.ui_q.put(("st_stream", token, index, direct, error))

    def _st_on_stream(self, token: int, index: int, url: str, error: str) -> None:
        if token != self._st_stream_token or index != self._st_index:
            return  # another track was chosen meanwhile
        if error or not url:
            self._st_stop()
            self.st_status.setText("Couldn't find that track's file" + (f": {error}" if error else "") + ".")
            return
        self._st_start_stream(url)

    def _st_start_stream(self, url: str, local: bool = False) -> None:
        player = self._st_player_get()
        player.setSource(QUrl.fromLocalFile(url) if local else QUrl(url))
        player.play()
        if not local and self._st_cache_key and self.media_store.audio_cache_limit() > 0:
            threading.Thread(target=self._st_cache_worker, args=(self._st_cache_key, url), daemon=True).start()

    def _st_cache_worker(self, key: str, url: str) -> None:  # worker thread
        """Keep a copy of the track being streamed, if the audio cache is on and has room for it."""
        try:
            if self.media_store.cache_path(key) is not None:
                return
            with http_open(url, timeout=30) as resp:
                data = resp.read(CACHE_MAX_FILE + 1)
            if len(data) <= CACHE_MAX_FILE:
                ext = Path(urllib.parse.urlsplit(url).path).suffix or ".mp3"
                self.media_store.cache_put(key, data, ext)
        except Exception:  # a cache is a convenience: never interrupt playback for it
            pass

    # ---- cover art ----------------------------------------------------------------------------
    def _st_art_url_playing(self) -> str:
        item = self.pq.current()
        return item.art if item else ""

    def _st_show_art(self, art: str) -> None:
        """Put the cover on the sidebar card: straight away if cached, otherwise fetch it in the background."""
        np = self.now_playing
        if not art:
            np.set_art(None)
        elif (pix := self._st_art_cache.get(art)) is not None:
            np.set_art(pix)
        else:
            np.set_art(None, pending=True)
            if art not in self._st_art_loading:
                self._st_art_loading.add(art)
                threading.Thread(target=self._st_art_worker, args=(art,), daemon=True).start()

    def _st_art_worker(self, url: str) -> None:  # worker thread
        try:
            if (kept := self._st_covers.get(url)) is not None:  # fetched before: no network
                data = kept
            else:
                with _ART_SLOTS:
                    data = st.fetch_cover(url, ART_MAX_BYTES)  # (a cover.jpg saved beside the tracks, or the sharpest picture of a listed album's cover)
                if data and not st.is_local_path(url):  # (a file on this computer is its own copy)
                    self._st_covers.put(url, data)
        except Exception:  # no cover art is fine, it just isn't shown
            data = b""
        self.ui_q.put(("st_art", url, data))

    def _st_on_art(self, url: str, data: bytes) -> None:
        self._st_art_loading.discard(url)
        pix = QPixmap()
        if data and pix.loadFromData(data):
            if max(pix.width(), pix.height()) > ART_PIXELS:
                pix = pix.scaled(ART_PIXELS, ART_PIXELS, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            if len(self._st_art_cache) >= ART_CACHE_MAX:
                self._st_art_cache.pop(next(iter(self._st_art_cache)))
            self._st_art_cache[url] = pix
            for row in self._st_rows:
                if row.album is not None and row.album.art == url:
                    self._st_row_cover(row)
            for art, lib_row in getattr(self, "_lib_widgets", []):
                if art == url:
                    lib_row.set_cover(pix)
            self._home_cover(url, pix)
            for w in getattr(self, "_q_widgets", []) + getattr(self, "_pl_widgets", []):  # queue and playlist rows
                if isinstance(w, QueueRow) and w.art == url:
                    w.set_cover(pix)
        elif url == self._st_art_url_playing():
            self.now_playing.set_art(None)  # nothing usable came back: drop the placeholder
            return
        if url == self._st_art_url_playing() and url in self._st_art_cache:
            self.now_playing.set_art(self._st_art_cache[url])

    def _st_toggle_pause(self) -> None:
        player = self._st_player
        if player is None or self._st_index < 0:
            return
        if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            player.pause()
        else:
            player.play()

    def _st_step(self, delta: int, auto: bool = False) -> None:
        """Next (+1) or previous (-1). Previous restarts a track that is well under way, as players usually do."""
        player = self._st_player
        if player is None or self._st_index < 0:
            return
        if delta < 0 and not auto and player.position() > 3000:
            player.setPosition(0)
            return
        if self.pq.step(delta, auto):
            self._st_play_index(self.pq.current_index)
        elif delta > 0:  # ran off the end of the queue
            self._st_stop()
        else:
            player.setPosition(0)

    def _st_seek(self, ms: int) -> None:
        if self._st_player is not None and self._st_index >= 0:
            self._st_player.setPosition(ms)

    # ---- the disc: hold it to stop the record, turn it to wind the track forward and back (a small easter egg) -------------------
    def _st_vinyl_get(self) -> dict:
        """The record sounds (bundled in assets/sounds), loaded the first time the disc is held."""
        if getattr(self, "_st_vinyl", None) is None:
            self._st_vinyl = {}
            if AUDIO_OK:
                for name in ("vinyl-stop", "vinyl-start", "scratch-fwd", "scratch-back"):
                    effect = QSoundEffect(self)
                    effect.setSource(QUrl.fromLocalFile(str(asset("sounds", name + ".wav"))))
                    self._st_vinyl[name] = effect
        return self._st_vinyl

    VINYL_START_MS = 500  # how long vinyl-start.wav runs: the music waits this long after letting go so the record "spins up" first
    VINYL_SOFTEN = 0.7  # the record sounds sit a little under the music's own volume

    def _st_vinyl_play(self, name: str, gain: float = 1.0) -> None:
        """One of the record sounds, at the player's own volume (silent when it is muted)."""
        level = min(self._st_output_level() * gain * self.VINYL_SOFTEN, 1.0)
        if level <= 0 or (effect := self._st_vinyl_get().get(name)) is None:
            return
        effect.setVolume(level)
        effect.play()

    def _st_scratch_start(self) -> None:
        """The disc was held: pause where it is, with the sound of a record being stopped."""
        player = self._st_player
        if player is None or self._st_index < 0:
            return
        self._st_scratch = {"was_playing": player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
                            "start": player.position(), "target": None, "applied": None, "grain": 0.0}
        if getattr(self, "_st_scratch_timer", None) is None:
            self._st_scratch_timer = QTimer(self)  # seeking a stream on every mouse move would stall it: at most one a tick
            self._st_scratch_timer.setSingleShot(True)
            self._st_scratch_timer.setInterval(50)
            self._st_scratch_timer.timeout.connect(self._st_scratch_apply)
        if getattr(self, "_st_resume_timer", None) is not None:
            self._st_resume_timer.stop()  # grabbed again before the spin-up finished: the music stays held
        player.pause()
        self._st_vinyl_play("vinyl-stop")

    def _st_scratch_apply(self) -> None:
        s = getattr(self, "_st_scratch", None)
        if s is not None and s["target"] is not None and s["target"] != s["applied"] and self._st_player is not None:
            s["applied"] = s["target"]
            self._st_player.setPosition(s["target"])

    def _st_scratch_move(self, turned: float, speed: float) -> None:
        """The disc was turned `turned` degrees from where it was grabbed: seek that far (clockwise is forward) and scratch."""
        s, player = getattr(self, "_st_scratch", None), self._st_player
        if s is None or player is None:
            return
        s["target"] = scratchmath.scrub_ms(s["start"], turned, player.duration())
        if not self._st_scratch_timer.isActive():
            self._st_scratch_apply()
            self._st_scratch_timer.start()
        now, gain = time.monotonic(), scratchmath.grain_gain(speed)
        if gain and now - s["grain"] >= scratchmath.grain_gap_ms(speed) / 1000:
            s["grain"] = now
            self._st_vinyl_play("scratch-fwd" if speed > 0 else "scratch-back", gain)

    def _st_scratch_end(self) -> None:
        """The disc was let go: carry on from where it was turned to, if the music was playing when it was held."""
        s, self._st_scratch = getattr(self, "_st_scratch", None), None
        player = self._st_player
        if getattr(self, "_st_scratch_timer", None) is not None:
            self._st_scratch_timer.stop()
        if s is None or player is None or self._st_index < 0:
            return
        if s["target"] is not None:
            player.setPosition(s["target"])
        if s["was_playing"]:
            self._st_vinyl_play("vinyl-start")  # the music starts only once the spin-up sound has played
            if getattr(self, "_st_resume_timer", None) is None:
                self._st_resume_timer = QTimer(self)
                self._st_resume_timer.setSingleShot(True)
                self._st_resume_timer.timeout.connect(self._st_scratch_resume)
            self._st_resume_timer.start(self.VINYL_START_MS)

    def _st_scratch_resume(self) -> None:
        """The spin-up sound is done: now the music carries on."""
        player = self._st_player
        if player is not None and self._st_index >= 0 and getattr(self, "_st_scratch", None) is None:
            player.play()

    def _st_stop(self) -> None:
        """Stop, hide the sidebar player and put every play icon back. What was queued stays in the Queue tab."""
        self.pq.stop()  # the queue stays for the Queue tab; nothing is playing
        self._st_stream_token += 1
        if self._st_player is not None:
            self._st_player.stop()
        if np := getattr(self, "now_playing", None):
            motion.slide(np, False)
        self._st_sync_icons()
        self._queue_refresh()

    def _st_position_tick(self, ms: int) -> None:
        if ms >= HISTORY_AFTER_MS and self._st_logged_token != self._st_stream_token:
            self._st_log_play()

    def _st_log_play(self) -> None:
        """Record the playing track in the history (once per play): it ran 30 s or it ended."""
        item = self.pq.current()
        if item is None or self._st_logged_token == self._st_stream_token:
            return
        self._st_logged_token = self._st_stream_token
        self.media_store.record_play(st.Album(item.source, item.ident, item.album, item.detail, 1.0, item.art), item.position)
        self._lib_recent_refresh()

    def _st_clear_history(self) -> None:
        self.media_store.clear_history()
        self._lib_recent_refresh()
        self._lib_refresh()
        self._toast("Play history cleared.", "ok", 4000)

    def _st_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._st_log_play()  # a track shorter than 30 s still counts when it ends
            self._st_step(1, auto=True)  # on to the next track (or the same again with repeat one), or stop after the last

    def _st_player_error(self, _error, message: str) -> None:
        self._st_stop()
        self.st_status.setText("Couldn't play that track" + (f": {message}" if message else "")
                               + ".")
