"""The Games page: the player's own library as a front end. A thin sidebar of platforms (with how many games each has), posters for the games
in the one picked, and a page for each game with its cover, what is known about it and a Play button that starts it in RetroArch.

Methods only; the state lives on DistillWindow (set up by `_gm_init`). The widgets it uses are in games_widgets.py, the engine in
`distill/games.py`."""
from __future__ import annotations

import math
import threading
import time
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QSizePolicy, QSlider,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import apikeys
from .. import gamemedia as mm
from .. import games as gm
from .. import romm
from .. import recomps as rc
from ..droproute import DropTarget, route_drop
from ..models import fmt_size
from ..organize import find_library
from . import flight, icons, motion, msgbox
from .games_dialog import ClickLabel, EditGameDialog, ImagePreview, LibrariesDialog, PlatformSetupDialog, RetroAchievementsDialog, StatsDialog
from .emu_cards import LAUNCHBOX_COLOUR, FactList, SourceChip, chip_css, clear_layout, pill_css, section_header, tint, TwoColumn
from .motion import smooth_scroll
from .nav import PAGE_INFO
from ..artqueue import ArtQueue
from .recomp_page import ART_CACHE_MAX, ART_WORKERS, DETAIL_H, DETAIL_W, ID_ROLE, CoverFrame, cover_pixmap, rounded_pixmap
from .theme import T
from .widgets import accent_button, Card, detail_shell, ModalStack, hint
from .games_widgets import (BackdropView, BADGE, ButtonFill, ROMM_COLOUR, cell_metrics, COUNT_ROLE, EmptyLibrary, GameDelegate, GameGrid, GridPage,
                            HEALTH_ROLE, HStrip, LinkChip, NotesEdit, PAGE_MIN_H, Pill, PILL_COLOURS, PlatformLogo, poster_rect, RA_COLOUR,
                            RailDelegate, SelectionBar, SHOT_H, SHOT_W, StarRating, StatTile, ZOOM_STEPS, _tile_pixmap, _when)


class GamesMixin:
    """Methods only; the state lives on DistillWindow (initialised by `_gm_init`)."""

    def _gm_init(self) -> None:
        self._gm_store: gm.GameStore | None = None
        self._gm_by_id: dict[str, gm.Game] = {}
        self._gm_art: dict[str, QPixmap] = {}
        self._gm_art_tried: set[str] = set()
        self._gm_art_q = ArtQueue(lambda g, key: gm.fetch_cover(g, key), lambda g, found: self.ui_q.put(("games_art", g.id, found)),
                                  workers=ART_WORKERS, busy=rc.SgdbBusy, on_busy=lambda g: self._gm_art_tried.discard(g.id))
        self._gm_details: dict[str, gm.Details] = {}
        self._gm_refetch: set[str] = set()  # games whose LaunchBox details are to be looked up again (their name or platform was edited)
        self._gm_mode = "all"  # the pill showing: 'all', 'fav', 'recent' (played) or 'new' (added)
        self._gm_platform = ""  # the platform picked in the sidebar ('' = every platform)
        self._gm_collection = 0  # the collection picked in the sidebar (0 = none; a platform and a collection are never both picked)
        self._gm_filters: dict = {}  # the Filters menu: genre, decade, developer, publisher, region, rating, status, played, missing
        self._gm_health: dict[str, gm.PlatformHealth] = {}  # platform -> whether its games would start (worked out when the page is shown; see `_gm_refresh_health`)
        self._gm_notes_gid = ""  # the game the notes box is showing (a save that is still pending goes to this one even after the page has moved on)
        self._gm_mine_dirty = False  # a rating or status was changed on the page: the grid's order and filters may be out of date
        self._gm_group = True  # versions of one game share a poster (Filters menu; remembered between runs)
        self._gm_groups: dict[str, list[gm.Game]] = {}  # shown game id -> all its versions (only where there is more than one)
        self._gm_group_of: dict[str, str] = {}  # any version's id -> the id shown for it
        self._gm_missing: set[str] = set()  # shown games whose file isn't there (checked in the background)
        self._gm_missing_token = 0
        self._gm_selected: set[str] = set()  # ids of the games whose dot is ticked
        self._gm_collection_names: dict[int, str] = {}
        self._gm_smart = 0  # the smart collection picked in the sidebar (0 = none; a platform, a collection and a smart collection are never picked together)
        self._gm_smart_names: dict[int, str] = {}
        self._gm_backdrop_on = False  # the detail page's background art is showing
        self._gm_ra: mm.RaGame | None = None
        self._gm_flight: flight.Flight | None = None  # the cover in the air between the grid and a page
        self._gm_hold_id = ""  # the game whose grid cell is left empty while its cover flies to it
        self._gm_media_key = ""  # the game whose background, screenshots and achievements the detail page is showing
        self._gm_media_busy: set[str] = set()  # games whose pictures are being looked for right now
        self._gm_ra_mode = ""  # what the achievements card shows: '', 'connect', 'loading', 'error', 'game'
        self._gm_zoom = 1.0  # poster size, 1.0 = normal (the slider; remembered between runs)
        self._gm_sort = "name"  # a key of gm.SORTS (the sort box; remembered between runs)
        self._gm_matching = False  # a LaunchBox lookup of the selected games is running
        self._gm_rail_was = False
        self._gm_rail_moved = False  # the current item changed during this press (it was a new choice)
        self._gm_current = ""
        self._gm_scanning = False
        self._gm_stop = threading.Event()
        self._gm_loaded = False
        self._gm_is_empty = False  # the library has no games (the page's scrollbar goes while that is so)
        self._gm_list_scroll = 0
        self._gm_view = "grid"  # 'grid' (posters) or 'list' (a table with sortable columns; Filters menu, remembered between runs)
        self._gm_col: tuple[str, bool] = ("name", False)  # the list's sort: a key of gm.LIST_COLUMNS and whether it runs high to low
        self._gm_random_last = ""  # the game Random game picked last, so it isn't picked twice running
        self._gm_verifying = False  # a DAT check of the selected games is running
        self._gm_installs: dict[str, float] = {}  # games being installed from RomM right now -> how much of the file is in (0..1)
        self._gm_install_stop: dict[str, threading.Event] = {}
        self._gm_romm_busy = False  # the RomM listing is being read

    @property
    def gm_store(self) -> gm.GameStore:
        if self._gm_store is None:
            self._gm_store = gm.GameStore()
        return self._gm_store

    # ---- building -------------------------------------------------------------------------------------------
    def _build_games_page(self) -> QWidget:
        self._gm_init()
        page = QWidget()
        self._gm_page = page
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        self.gm_rail_frame = self._gm_build_rail()
        row.addWidget(self.gm_rail_frame)
        self.gm_views = ModalStack()
        self.gm_views.dismissed.connect(self._gm_back)
        self.gm_views.addWidget(self._gm_build_grid())
        self.gm_views.addWidget(self._gm_build_detail())
        row.addWidget(self.gm_views, 1)
        page.setMinimumSize(1, PAGE_MIN_H)
        return page

    def _gm_build_rail(self) -> QWidget:
        """The platforms in the library, with how many games each has. Choosing one narrows the grid to it; choosing it again lets go."""
        rail = QFrame()
        rail.setObjectName("gmRail")
        rail.setFixedWidth(212)
        T.bind_css(rail, lambda: f"QFrame#gmRail {{ background: {T.card.name()}; border: 1px solid {T.card_border.name()}; border-radius: 14px; }}")
        lay = QVBoxLayout(rail)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(4)
        self.gm_rail = QListWidget()
        self.gm_rail.setFrameShape(QFrame.Shape.NoFrame)
        self.gm_rail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.gm_rail.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.gm_rail.setSpacing(1)
        self.gm_rail.setMouseTracking(True)
        self.gm_rail.setItemDelegate(RailDelegate(self.gm_rail))
        T.bind_css(self.gm_rail, lambda: "QListWidget { background: transparent; border: none; outline: 0; }")
        smooth_scroll(self.gm_rail)
        self.gm_rail.itemPressed.connect(self._gm_rail_pressed)
        self.gm_rail.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.gm_rail.customContextMenuRequested.connect(self._gm_rail_menu)
        self.gm_rail.currentItemChanged.connect(self._gm_rail_changed)
        self.gm_rail.itemClicked.connect(self._gm_rail_clicked)
        lay.addWidget(self.gm_rail, 1)
        return rail

    def _gm_build_grid(self) -> QWidget:
        view = GridPage()
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.gm_tools = QWidget()  # two rows like Recomps: search, sort and size on top, the filter pills and the actions below
        tools = QVBoxLayout(self.gm_tools)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.gm_search = QLineEdit()
        self.gm_search.setPlaceholderText("Search your games, developers and publishers...")
        self.gm_search.setClearButtonEnabled(True)
        self.gm_search.textChanged.connect(self._gm_fill)
        self.gm_sort_box = QComboBox()
        self.gm_sort_box.setToolTip("How the games are ordered. Recently played lists the latest first unless you pick another order.")
        for key, label in gm.SORTS.items():
            self.gm_sort_box.addItem(label, key)
        self.gm_sort_box.activated.connect(self._gm_sort_changed)
        self.gm_filter_btn = QPushButton("Filters")
        self.gm_filter_btn.setToolTip("Narrow the games by genre, decade, developer, publisher, region, your rating or progress, and choose whether versions of a game share a poster.")
        fmenu = QMenu(self.gm_filter_btn)
        fmenu.aboutToShow.connect(lambda: self._gm_fill_filter_menu(fmenu))
        self.gm_filter_btn.setMenu(fmenu)
        self.gm_zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.gm_zoom_slider.setRange(0, len(ZOOM_STEPS) - 1)
        self.gm_zoom_slider.setValue(ZOOM_STEPS.index(100))
        self.gm_zoom_slider.setFixedWidth(110)
        self.gm_zoom_slider.setToolTip("Poster size")
        self.gm_zoom_slider.valueChanged.connect(self._gm_zoom_changed)
        small, big = QLabel(), QLabel()
        for lab, size in ((small, 11), (big, 16)):
            lab.setPixmap(icons.pixmap("image", T.fg3, size))
            lab.setToolTip("Poster size")
        bar.addWidget(self.gm_search, 1)
        bar.addWidget(self.gm_sort_box)
        bar.addWidget(self.gm_filter_btn)
        bar.addSpacing(6)
        bar.addWidget(small)
        bar.addWidget(self.gm_zoom_slider)
        bar.addWidget(big)
        tools.addLayout(bar)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.gm_pills: dict[str, Pill] = {}
        group = QButtonGroup(self.gm_tools)
        group.setExclusive(True)
        for key, text in (("all", "All games"), ("fav", "Favourites"), ("recent", "Recently played"), ("new", "Recently added"), ("installed", "Installed")):
            pill = Pill(text, PILL_COLOURS[key])
            pill.clicked.connect(lambda _c=False, k=key: self._gm_pill(k))
            group.addButton(pill)
            actions.addWidget(pill)
            self.gm_pills[key] = pill
        self.gm_pills["all"].setChecked(True)
        actions.addStretch(1)
        self.gm_scan_btn = QPushButton("Scan library")
        self.gm_scan_btn.setToolTip("Look through your game folders again for new, moved and removed games. Files are only read, never changed.")
        self.gm_scan_btn.clicked.connect(self._gm_scan)
        self.gm_folder_btn = QPushButton("Libraries...")
        self.gm_folder_btn.setToolTip("The folders that hold your games: add more, or take one off the list.")
        self.gm_folder_btn.clicked.connect(self._gm_libraries)
        actions.addWidget(self.gm_folder_btn)
        actions.addWidget(self.gm_scan_btn)
        tools.addLayout(actions)
        lay.addWidget(self.gm_tools)
        self.gm_status = hint("", live=True)
        lay.addWidget(self.gm_status)
        self.gm_empty = EmptyLibrary(self._gm_choose_folder)  # not in the layout: moved onto the visible area by `_gm_on_show`
        view.empty = self.gm_empty
        self.gm_list = GameGrid(self)
        self.gm_list.setViewMode(QListView.ViewMode.IconMode)
        self.gm_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.gm_list.setMovement(QListView.Movement.Static)
        self.gm_list.setUniformItemSizes(True)
        self.gm_list.setSpacing(4)
        self.gm_list.setWrapping(True)  # posters keep their size; the one that no longer fits the row drops to the next line
        self.gm_list.setFlow(QListView.Flow.LeftToRight)
        self.gm_list.setFrameShape(QFrame.Shape.NoFrame)
        self.gm_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.gm_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.gm_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.gm_list.customContextMenuRequested.connect(self._gm_context)
        self.gm_list.setItemDelegate(GameDelegate(self))
        self.gm_list.setMinimumHeight(480)
        self.gm_list.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)  # a narrow window narrows the grid, it never forces the page wider
        smooth_scroll(self.gm_list, bounce=True)
        self.gm_list.itemActivated.connect(self._gm_open_item)
        self.gm_list.itemClicked.connect(self._gm_open_item)
        lay.addWidget(self.gm_list, 1)
        self.gm_table = self._gm_build_table()
        lay.addWidget(self.gm_table, 1)
        self.gm_table.setVisible(False)
        self.gm_bar = SelectionBar(view)  # moved onto the visible area by `_gm_on_show`
        view.bar = self.gm_bar
        self.gm_bar.all_btn.clicked.connect(self._gm_select_all)
        self.gm_bar.fav_btn.clicked.connect(lambda: self._gm_bulk_favorite())
        self.gm_bar.match_btn.clicked.connect(lambda: self._gm_bulk_match())
        self.gm_bar.remove_btn.clicked.connect(lambda: self._gm_bulk_remove())
        self.gm_bar.close_btn.clicked.connect(self._gm_clear_selection)
        menu = QMenu(self.gm_bar)
        menu.aboutToShow.connect(lambda: self._gm_fill_add_menu(menu))
        self.gm_bar.platform_btn.setMenu(menu)
        return view

    def _gm_build_table(self) -> QTableWidget:
        """The list view: one row a game, a column for what is worth sorting by. Clicking a heading sorts by it; clicking it again turns it round."""
        cols = list(gm.LIST_COLUMNS.items())
        table = QTableWidget(0, len(cols))
        table.setHorizontalHeaderLabels([head for _key, (head, _first) in cols])
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(34)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        table.setShowGrid(False)
        table.setFrameShape(QFrame.Shape.NoFrame)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.setMinimumHeight(480)
        table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        head = table.horizontalHeader()
        head.setSectionsClickable(True)
        head.setSortIndicatorShown(True)
        head.setStretchLastSection(False)
        head.setHighlightSections(False)
        head.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)  # the title takes what the other columns leave
        for i, width in enumerate((0, 130, 80, 70, 150, 90, 120, 120, 110, 110)):
            if width:
                table.setColumnWidth(i, width)
        T.bind_css(table, lambda: (
            f"QTableWidget {{ background: transparent; border: none; outline: 0; color: {T.fg.name()}; }}"
            f"QTableWidget::item {{ padding: 0 10px; border-bottom: 1px solid {T.card_border.name()}; }}"
            f"QTableWidget::item:selected {{ background: {T.hover.name()}; color: {T.fg.name()}; }}"
            f"QHeaderView::section {{ background: transparent; color: {T.fg3.name()}; border: none; border-bottom: 1px solid {T.card_border.name()}; "
            f"padding: 8px 10px; font-weight: 600; }}"))
        smooth_scroll(table)
        head.sectionClicked.connect(self._gm_column_clicked)
        table.itemSelectionChanged.connect(self._gm_table_selected)
        table.cellDoubleClicked.connect(lambda row, _col: self._gm_show_detail(self._gm_row_id(row)))
        table.customContextMenuRequested.connect(self._gm_table_context)
        return table

    def _gm_hero_css(self) -> str:
        """The top card: the colour wash the Emulator and Recomp pages use; a little see-through while the game's art shows behind it."""
        card = tint(T.card, 222 if self._gm_backdrop_on else 255)
        return (f"QFrame#card {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {tint(T.grad[1], 34)}, stop:0.6 {card}, stop:1 {card}); "
                f"border: 1px solid {T.card_border.name()}; border-radius: 14px; }}")

    def _gm_build_detail(self) -> QWidget:
        view = BackdropView()
        self.gmd_view = view
        lay = detail_shell(view)  # no back button: click outside the card (or Esc) to close it, like every detail page

        hero = Card()
        hero.lay.setContentsMargins(22, 20, 22, 18)
        hero.lay.setSpacing(16)
        self.gmd_hero = hero
        T.bind_css(hero, self._gm_hero_css)
        top = QHBoxLayout()
        top.setSpacing(22)
        self.gmd_cover = CoverFrame()
        self.gmd_cover.editRequested.connect(lambda: self._gm_edit(self._gm_current))  # the same window as the grid's pencil (cover included)
        self.gmd_cover.edit_btn.setToolTip("Edit this game")
        self.gmd_cover.setFixedSize(DETAIL_W, DETAIL_H)
        self.gmd_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.gmd_cover.setWordWrap(True)
        top.addWidget(self.gmd_cover, 0, Qt.AlignmentFlag.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(6)
        self.gmd_chips = QHBoxLayout()
        self.gmd_chips.setSpacing(6)
        chips = QHBoxLayout()
        chips.addLayout(self.gmd_chips)
        chips.addStretch(1)
        self.gmd_logo = PlatformLogo()  # top right of the card
        self.gmd_logo.setVisible(False)
        chips.addWidget(self.gmd_logo, 0, Qt.AlignmentFlag.AlignTop)
        info.addLayout(chips)
        self.gmd_title = QLabel()
        self.gmd_title.setObjectName("pageTitle")
        self.gmd_title.setStyleSheet("font-size: 20pt;")
        self.gmd_title.setWordWrap(True)
        info.addWidget(self.gmd_title)
        self.gmd_sub = QLabel()
        self.gmd_sub.setObjectName("muted")
        self.gmd_sub.setWordWrap(True)
        info.addWidget(self.gmd_sub)
        self.gmd_version = QComboBox()
        self.gmd_version.setToolTip("This game has more than one version in your library.")
        self.gmd_version.setMaximumWidth(460)
        self.gmd_version.activated.connect(self._gm_pick_version)
        self.gmd_version.setVisible(False)
        info.addWidget(self.gmd_version, 0, Qt.AlignmentFlag.AlignLeft)
        mine = QHBoxLayout()  # what the player thinks of the game: stars and where they are with it
        mine.setSpacing(12)
        self.gmd_stars = StarRating()
        self.gmd_stars.changed.connect(lambda n: self._gm_save_mine(rating=n))
        self.gmd_status = QComboBox()
        self.gmd_status.setToolTip("Where you are with this game.")
        self.gmd_status.addItem("No status", "")
        for key, label in gm.STATUSES.items():
            self.gmd_status.addItem(label, key)
        self.gmd_status.activated.connect(lambda _i: self._gm_save_mine(status=str(self.gmd_status.currentData() or "")))
        mine.addWidget(self.gmd_stars)
        mine.addWidget(self.gmd_status)
        mine.addStretch(1)
        info.addLayout(mine)
        info.addSpacing(4)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.gmd_tiles = {key: StatTile(icon, caption) for key, icon, caption in (
            ("released", "calendar", "Released"), ("playtime", "clock", "Time played"), ("last", "play", "Last played"), ("genre", "gamepad", "Genre"))}
        for tile in self.gmd_tiles.values():
            tiles.addWidget(tile, 1)
        info.addLayout(tiles)
        info.addSpacing(4)
        self.gmd_blurb = QLabel()
        self.gmd_blurb.setWordWrap(True)
        info.addWidget(self.gmd_blurb)
        info.addStretch(1)
        top.addLayout(info, 1)
        hero.add(top)
        rule = QFrame()
        rule.setFixedHeight(1)
        T.bind_css(rule, lambda: f"background: {T.card_border.name()}; border: none;")
        hero.add(rule)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.gmd_play = accent_button("Play")
        self.gmd_play.setMinimumHeight(40)
        self.gmd_play.setMinimumWidth(112)
        self.gmd_play.clicked.connect(self._gm_play)
        self._gm_play_fill = ButtonFill(self.gmd_play)  # the Install button fills as the download comes in
        self._gm_romm_timer = QTimer(view)  # while the page is open, look at RomM now and then (a sync only runs when the last one is old)
        self._gm_romm_timer.setInterval(30 * 60 * 1000)
        self._gm_romm_timer.timeout.connect(lambda: self._gm_romm_sync(False))
        self._gm_romm_timer.start()
        self.gmd_continue = QPushButton("Continue")  # only shown when RetroArch has a save state for the game
        self.gmd_continue.setMinimumHeight(40)
        self.gmd_continue.clicked.connect(self._gm_continue)
        self.gmd_continue.setVisible(False)
        self.gmd_with = QComboBox()
        self.gmd_with.setMinimumHeight(40)
        self.gmd_with.setMinimumWidth(220)
        self.gmd_with.setToolTip("Which emulator starts this game.")
        self.gmd_with.activated.connect(self._gm_with_changed)
        self.gmd_default = QPushButton("Make default")
        self.gmd_default.setMinimumHeight(40)
        self.gmd_default.clicked.connect(self._gm_make_default)
        self.gmd_opts = QPushButton("Options...")
        self.gmd_opts.setMinimumHeight(40)
        self.gmd_opts.setToolTip("The words put after this emulator's program when it starts a game.")
        self.gmd_opts.clicked.connect(self._gm_options)
        self.gmd_opts.setVisible(False)
        self.gmd_fav = QPushButton("Favourite")
        self.gmd_fav.setMinimumHeight(40)
        self.gmd_fav.clicked.connect(self._gm_toggle_favorite)
        self.gmd_edit = QPushButton("Edit...")
        self.gmd_edit.setToolTip("Change this game's name, platform, publisher, developer, year, genre, cover, rating, status and notes.")
        self.gmd_edit.setMinimumHeight(40)
        self.gmd_edit.clicked.connect(lambda: self._gm_edit(self._gm_current))
        self.gmd_folder = QPushButton("Show in folder")
        self.gmd_folder.setMinimumHeight(40)
        self.gmd_folder.clicked.connect(self._gm_show_folder)
        for w in (self.gmd_play, self.gmd_continue, self.gmd_with, self.gmd_default, self.gmd_opts):
            row.addWidget(w)
        row.addStretch(1)
        for w in (self.gmd_fav, self.gmd_edit, self.gmd_folder):
            row.addWidget(w)
        hero.add(row)
        self.gmd_warn = QLabel()
        self.gmd_warn.setWordWrap(True)
        self.gmd_warn.setVisible(False)
        T.bind_css(self.gmd_warn, lambda: f"color: {T.warn.name()}; background: transparent;")
        hero.add(self.gmd_warn)
        self.gmd_msg = hint("", live=True)
        hero.add(self.gmd_msg)
        lay.addWidget(hero)

        main = QWidget()
        left = QVBoxLayout(main)
        left.setContentsMargins(0, 0, 0, 0)
        self.gmd_about_card = Card()
        self.gmd_about_card.lay.setContentsMargins(20, 16, 20, 18)
        self.gmd_about_card.lay.setSpacing(12)
        about_head = section_header("About", "info")
        self.gmd_lb_chip = SourceChip("LaunchBox", "From the LaunchBox Games Database", LAUNCHBOX_COLOUR)
        self.gmd_lb_chip.setVisible(False)
        about_head.addWidget(self.gmd_lb_chip)
        self.gmd_about_card.add(about_head)
        self.gmd_about = QLabel()
        self.gmd_about.setWordWrap(True)
        self.gmd_about.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.gmd_about_card.add(self.gmd_about)
        left.setSpacing(14)
        left.addWidget(self.gmd_about_card)
        self.gmd_notes_card = Card()
        self.gmd_notes_card.lay.setContentsMargins(20, 16, 20, 18)
        self.gmd_notes_card.lay.setSpacing(12)
        self.gmd_notes_card.add(section_header("My notes", "edit"))
        self.gmd_notes = NotesEdit()
        self.gmd_notes.committed.connect(lambda text: self._gm_save_mine(notes=text, gid=self._gm_notes_gid))
        self.gmd_notes_card.add(self.gmd_notes)
        left.addWidget(self.gmd_notes_card)
        # screenshots: a row of picture tiles, click one to see it larger
        self.gmd_shots_card = Card()
        self.gmd_shots_card.lay.setContentsMargins(20, 16, 20, 12)
        self.gmd_shots_card.lay.setSpacing(12)
        shots_head = section_header("Screenshots", "image")
        shots_head.addWidget(SourceChip("LaunchBox", "From the LaunchBox Games Database", LAUNCHBOX_COLOUR))
        self.gmd_shots_card.add(shots_head)
        self.gmd_shots = HStrip(SHOT_H)
        self.gmd_shots_card.add(self.gmd_shots)
        self.gmd_shots_card.setVisible(False)
        left.addWidget(self.gmd_shots_card)
        # achievements from RetroAchievements: a rounded square for each, the ones won in full colour
        self.gmd_ra_card = Card()
        self.gmd_ra_card.lay.setContentsMargins(20, 16, 20, 12)
        self.gmd_ra_card.lay.setSpacing(10)
        ra_head = section_header("Achievements", "star")
        self.gmd_ra_chip = LinkChip("RetroAchievements", "Open RetroAchievements.org", RA_COLOUR, "https://retroachievements.org")
        ra_head.addWidget(self.gmd_ra_chip)
        self.gmd_ra_card.add(ra_head)
        ra_row = QHBoxLayout()
        ra_row.setSpacing(10)
        self.gmd_ra_text = QLabel()
        self.gmd_ra_text.setWordWrap(True)
        T.bind_css(self.gmd_ra_text, lambda: f"color: {T.fg2.name()}; background: transparent;")
        self.gmd_ra_btn = QPushButton()
        self.gmd_ra_btn.clicked.connect(self._gm_ra_button)
        self.gmd_ra_hide = QPushButton("Don't show")
        self.gmd_ra_hide.setToolTip("Hide this card until you connect RetroAchievements.")
        self.gmd_ra_hide.clicked.connect(self._gm_ra_hide)
        ra_row.addWidget(self.gmd_ra_text, 1)
        ra_row.addWidget(self.gmd_ra_hide)
        ra_row.addWidget(self.gmd_ra_btn)
        self.gmd_ra_card.add(ra_row)
        self.gmd_ra_strip = HStrip(BADGE)
        self.gmd_ra_card.add(self.gmd_ra_strip)
        self.gmd_ra_card.setVisible(False)
        left.addWidget(self.gmd_ra_card)
        left.addStretch(1)
        side = Card()
        side.lay.setContentsMargins(18, 16, 18, 16)  # a side card, as on the recomp and emulator pages
        side.lay.setSpacing(12)
        side.add(section_header("Details", "list"))
        self.gmd_facts = FactList()
        side.add(self.gmd_facts)
        lay.addWidget(TwoColumn(main, side, side_width=300, wide_at=760))
        lay.addStretch(1)
        return view

    # ---- showing the page ---------------------------------------------------------------------------------
    def _gm_on_show(self) -> None:
        self.gm_bar.attach(self.settings_scroll.viewport())  # the page scrolls; the bar must not, or it sits below the fold
        self.gm_empty.attach(self.settings_scroll.viewport())  # ...and so does the empty page's block: centred in what is really showing
        if not self._gm_loaded:
            self._gm_loaded = True
            self._gm_load_view_settings()
            self._gm_reload()
            if self.gm_store.setting("scan_on_open") == "1" and self.gm_store.library_roots():
                QTimer.singleShot(400, self._gm_scan)
        self._gm_romm_sync(False)
        self._gm_refresh_health()
        self._gm_refresh_buttons()

    def _gm_refresh_health(self) -> None:
        """Work out which platforms would not start a game (nothing installed for them, a core's BIOS files missing) and mark them in the sidebar."""
        try:
            rows = gm.health_overview(self.gm_store.systems(), self.gm_store.default_for, self._gm_retro(), self._gm_emus)
        except Exception:  # a problem with the Emulators page must not stop the Games page
            return
        self._gm_health = {h.system: h for h in rows}
        for i in range(self.gm_rail.count()):
            item = self.gm_rail.item(i)
            health = self._gm_health.get(str(item.data(ID_ROLE) or ""))
            if health is not None:
                item.setData(HEALTH_ROLE, health.level)
                name = str(item.data(ID_ROLE))
                item.setToolTip(f"{name}\n{health.summary}\nRight-click > Platform setup" if health.level != "ok" else name)
        self.gm_rail.viewport().update()

    def _gm_platform_setup(self, start: str = "") -> None:
        """The Platform setup window: what plays each platform, its launch options and BIOS status."""
        if not self.gm_store.systems():
            return
        dlg = PlatformSetupDialog(self, self.gm_store, self._gm_retro, self._gm_emus, start or self._gm_platform)
        dlg.exec()
        self._gm_refresh_health()
        if dlg.changed and self._gm_current:
            g = self._gm_game()
            if g is not None:
                self._gm_fill_with(g)
                self._gm_refresh_buttons()

    def _gm_rail_heading(self, text: str) -> QListWidgetItem:
        head = QListWidgetItem(text)
        head.setFlags(Qt.ItemFlag.NoItemFlags)
        small = head.font()
        small.setPointSizeF(9.0)
        small.setBold(True)
        head.setFont(small)
        head.setForeground(T.fg3)
        return head

    def _gm_rail_key(self) -> str:
        """The sidebar row that is chosen: a platform's name, '@c:<id>' for a collection, '@s:<id>' for a smart collection, '' for none."""
        if self._gm_smart:
            return f"@s:{self._gm_smart}"
        return f"@c:{self._gm_collection}" if self._gm_collection else self._gm_platform

    def _gm_reload(self) -> None:
        """Re-read the saved library: the pills' counts, the sidebar's platforms and collections, then the grid."""
        total, fav, played = self.gm_store.totals()
        for key, text, n in (("all", "All games", total), ("fav", "Favourites", fav), ("recent", "Recently played", played),
                             ("new", "Recently added", self.gm_store.recent_added()), ("installed", "Installed", self.gm_store.installed_count())):
            self.gm_pills[key].setText(f"{text}  {n}")
        rail = self.gm_rail
        rail.blockSignals(True)
        rail.clear()
        by_key: dict[str, QListWidgetItem] = {}
        for system, count in self.gm_store.systems():
            item = QListWidgetItem(system)
            item.setData(ID_ROLE, system)
            item.setData(COUNT_ROLE, count)
            health = self._gm_health.get(system)
            item.setData(HEALTH_ROLE, health.level if health is not None else "")
            item.setToolTip(f"{system}\n{health.summary}\nRight-click > Platform setup" if health is not None and health.level != "ok" else system)
            rail.addItem(item)
            by_key[system] = item
        collections = self.gm_store.collections()
        self._gm_collection_names = {cid: name for cid, name, _n in collections}
        if collections:
            rail.addItem(self._gm_rail_heading("COLLECTIONS"))
        for cid, name, count in collections:
            item = QListWidgetItem(name)
            item.setData(ID_ROLE, f"@c:{cid}")
            item.setData(COUNT_ROLE, count)
            item.setToolTip(f"{name}: right-click to rename or delete")
            rail.addItem(item)
            by_key[f"@c:{cid}"] = item
        smart = self.gm_store.smart_collections()
        self._gm_smart_names = {sid: name for sid, name, _n in smart}
        if smart:
            rail.addItem(self._gm_rail_heading("SMART COLLECTIONS"))
        for sid, name, count in smart:
            item = QListWidgetItem(name)
            item.setData(ID_ROLE, f"@s:{sid}")
            item.setData(COUNT_ROLE, count)
            item.setToolTip(f"{name}: a saved search that stays up to date. Right-click to rename or delete")
            rail.addItem(item)
            by_key[f"@s:{sid}"] = item
        key = self._gm_rail_key()
        if key in by_key:
            rail.setCurrentItem(by_key[key])
        else:
            self._gm_platform, self._gm_collection, self._gm_smart = "", 0, 0
            rail.setCurrentItem(None)
        rail.blockSignals(False)
        self._gm_fill()

    def _gm_rail_pressed(self, _item: QListWidgetItem) -> None:
        """A press chooses the item before this fires, so a press that changed the choice was a new pick; one that didn't is a click on the
        platform that was already chosen, which lets it go (see `_gm_rail_clicked`)."""
        self._gm_rail_was = not self._gm_rail_moved
        self._gm_rail_moved = False

    def _gm_rail_changed(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is None or item.data(ID_ROLE) is None:
            return
        self._gm_rail_moved = True
        key = str(item.data(ID_ROLE))
        if key.startswith("@c:"):
            self._gm_platform, self._gm_collection, self._gm_smart = "", int(key[3:]), 0
        elif key.startswith("@s:"):
            self._gm_platform, self._gm_collection, self._gm_smart = "", 0, int(key[3:])
        else:
            self._gm_platform, self._gm_collection, self._gm_smart = key, 0, 0
        self._gm_leave_detail()
        self._gm_fill()

    def _gm_rail_clear(self) -> None:
        """Let go of the platform or collection that is chosen in the sidebar."""
        self._gm_platform, self._gm_collection, self._gm_smart = "", 0, 0
        self.gm_rail.blockSignals(True)
        self.gm_rail.setCurrentItem(None)
        self.gm_rail.clearSelection()
        self.gm_rail.blockSignals(False)

    def _gm_rail_clicked(self, item: QListWidgetItem) -> None:
        """Choosing the row that is already chosen lets it go, so every game shows again."""
        if self._gm_rail_was and str(item.data(ID_ROLE) or "") == self._gm_rail_key():
            self._gm_rail_clear()
            self._gm_leave_detail()
            self._gm_fill()
        self._gm_rail_was = False

    def _gm_rail_menu(self, pos) -> None:
        item = self.gm_rail.itemAt(pos)
        key = str(item.data(ID_ROLE) or "") if item is not None else ""
        menu = QMenu(self)
        if key.startswith("@c:"):
            cid = int(key[3:])
            menu.addAction("Rename...", lambda: self._gm_rename_collection(cid))
            menu.addAction("Delete collection", lambda: self._gm_delete_collection(cid))
        elif key.startswith("@s:"):
            sid = int(key[3:])
            menu.addAction("Rename...", lambda: self._gm_rename_smart(sid))
            menu.addAction("Delete smart collection", lambda: self._gm_delete_smart(sid))
        else:  # a platform, or the empty part of the list: the setup window (opened on the platform that was clicked)
            menu.addAction("Platform setup...", lambda: self._gm_platform_setup(key))
        menu.exec(self.gm_rail.viewport().mapToGlobal(pos))

    def _gm_rename_collection(self, cid: int) -> None:
        text, ok = msgbox.get_text(self, "Rename collection", "Name:", self._gm_collection_names.get(cid, ""))
        if ok and text.strip():
            self.gm_store.rename_collection(cid, text)
            self._gm_reload()

    def _gm_delete_collection(self, cid: int) -> None:
        name = self._gm_collection_names.get(cid, "this collection")
        answer = msgbox.question(self, "Delete collection", f"Delete {name}?\n\nThe games in it stay in your library.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.gm_store.delete_collection(cid)
        if self._gm_collection == cid:
            self._gm_collection = 0
        self._gm_reload()

    def _gm_rename_smart(self, sid: int) -> None:
        text, ok = msgbox.get_text(self, "Rename smart collection", "Name:", self._gm_smart_names.get(sid, ""))
        if ok and text.strip():
            self.gm_store.rename_smart_collection(sid, text)
            self._gm_reload()

    def _gm_delete_smart(self, sid: int) -> None:
        name = self._gm_smart_names.get(sid, "this smart collection")
        answer = msgbox.question(self, "Delete smart collection", f"Delete {name}?\n\nOnly the saved search goes; your games are not touched.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.gm_store.delete_smart_collection(sid)
        if self._gm_smart == sid:
            self._gm_smart = 0
        self._gm_reload()

    def _gm_current_rules(self) -> dict:
        """What is showing now, as smart-collection rules: the picked smart collection's own rules, the platform, the search words, the Favourites
        pill and the filters on top."""
        base = self.gm_store.smart_rules(self._gm_smart) if self._gm_smart else {}
        filters = {**base.get("filters", {}), **{k: v for k, v in self._gm_filters.items() if k != "missing"}}
        return gm.clean_rules({"system": self._gm_platform or base.get("system", ""),
                               "query": " ".join(x for x in (base.get("query", ""), self.gm_search.text()) if x),
                               "favorites": self._gm_mode == "fav" or base.get("favorites", False), "filters": filters})

    def _gm_save_smart(self) -> None:
        rules = self._gm_current_rules()
        if not rules:
            return
        text, ok = msgbox.get_text(self, "Smart collection", "Name (for example Unplayed SNES RPGs from the 90s).\nSaving under an existing name replaces its rules:")
        sid = self.gm_store.save_smart_collection(text, rules) if ok else 0
        if sid:
            self._gm_platform, self._gm_collection, self._gm_smart = "", 0, sid
            self._gm_filters.clear()
            self.gm_search.blockSignals(True)
            self.gm_search.clear()
            self.gm_search.blockSignals(False)
            self._gm_mode = "all"
            self._gm_leave_detail()
            self._gm_reload()

    def _gm_pill(self, key: str) -> None:
        """A pill was pressed. All games also lets go of the platform or collection picked in the sidebar."""
        self._gm_mode = key
        if key == "all" and (self._gm_platform or self._gm_collection or self._gm_smart):
            self._gm_rail_clear()
        self._gm_leave_detail()
        self._gm_fill()

    def _gm_sync_scrollbar(self, empty: bool | None = None) -> None:
        """With no games there is nothing to scroll to, so the page's scrollbar goes; it is back with the games and on every other page."""
        if empty is not None:
            self._gm_is_empty = empty
        off = getattr(self, "current_page", "") == "games" and self._gm_is_empty
        self.settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if off else Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def _gm_leave_detail(self) -> None:
        if self.gm_views.currentIndex() == 1:
            self._gm_cancel_flight()
            self._gm_current = ""
            self.gm_views.setCurrentIndex(0)
            self.gmd_notes.flush()  # whatever was typed last is saved before the page moves on
            if self._gm_mine_dirty:  # the order or the filters may depend on what was just changed
                self._gm_mine_dirty = False
                if self._gm_sort in ("rating", "status") or any(k in self._gm_filters for k in ("rating", "status", "installed")) or self._gm_mode == "installed":
                    QTimer.singleShot(0, self._gm_fill)

    def _gm_fill(self, *_args) -> None:
        mode, platform = self._gm_mode, self._gm_platform
        total = self.gm_store.totals()[0]
        empty = total == 0
        self.gm_empty.set_empty(empty)
        self._gm_page.setMinimumHeight(0 if empty else PAGE_MIN_H)
        self._gm_sync_scrollbar(empty)
        self._gm_show_view(empty)
        self.gm_tools.setVisible(not empty)
        self.gm_rail_frame.setVisible(not empty)
        self.gm_scan_btn.setEnabled(not self._gm_scanning)
        wanted = {k: v for k, v in self._gm_filters.items() if k != "missing"}
        if mode == "installed":  # the Installed pill: what can be started from this computer now
            wanted["installed"] = "yes"
        games = self.gm_store.games(system=platform, query=self.gm_search.text(), favorites=mode == "fav", recent=mode == "recent", sort=self._gm_sort,
                                    filters=wanted, collection=self._gm_collection, smart=self._gm_smart, added=mode == "new")
        if self._gm_filters.get("missing"):
            self._gm_missing = gm.missing_ids(games)
            games = [g for g in games if g.id in self._gm_missing]
        self._gm_groups, self._gm_group_of = {}, {}
        shown = games
        if self._gm_group:  # versions of one game (regions, revisions, formats) share one poster
            grouped = gm.group_versions(games)
            shown = [first for first, _versions in grouped]
            for first, versions in grouped:
                for v in versions:
                    self._gm_group_of[v.id] = first.id
                    self._gm_by_id[v.id] = v
                if len(versions) > 1:
                    self._gm_groups[first.id] = versions
        self._gm_by_id.update({g.id: g for g in shown})
        self._gm_selected &= {g.id for g in shown}  # a game that is no longer shown can't stay selected
        if self._gm_view == "list":
            self._gm_fill_table(shown)
            self._gm_selection_changed()
            self._gm_update_filter_button()
            if not self._gm_filters.get("missing"):
                self._gm_check_missing(shown)
            if not self._gm_scanning and not self._gm_matching:
                self.gm_status.setText(self._gm_summary(len(shown)) if not empty else "")
            return
        self.gm_list.setUpdatesEnabled(False)
        self.gm_list.clear()
        for g in shown:
            item = QListWidgetItem()
            item.setData(ID_ROLE, g.id)
            item.setSizeHint(self._gm_cell_size())
            versions = len(self._gm_groups.get(g.id, ()))
            item.setToolTip(f"{g.name}\n{g.system}" + (f", {g.region}" if g.region else "") + (f"\n{versions} versions in your library" if versions > 1 else "")
                            + (f"\n{g.discs} discs" if g.discs > 1 else ""))
            self.gm_list.addItem(item)
        self.gm_list.setUpdatesEnabled(True)
        self._gm_selection_changed()
        self._gm_update_filter_button()
        if not self._gm_filters.get("missing"):
            self._gm_check_missing(shown)
        if not self._gm_scanning and not self._gm_matching:
            self.gm_status.setText(self._gm_summary(len(shown)) if not empty else "")

    def _gm_summary(self, n: int) -> str:
        """The line under the toolbar: what is showing."""
        if self._gm_smart:
            where = f"the smart collection {self._gm_smart_names.get(self._gm_smart, '')}"
        elif self._gm_collection:
            where = f"the collection {self._gm_collection_names.get(self._gm_collection, '')}"
        else:
            where = {"all": "your library", "fav": "your favourites", "recent": "the games you have played",
                                                                                  "new": f"the games added in the last {gm.RECENT_ADD_DAYS} days", "installed": "your installed games"}[self._gm_mode] + (f" on {self._gm_platform}" if self._gm_platform else "")
        shown = [str(v) for k, v in self._gm_filters.items() if k not in ("played", "missing", "rating", "status", "source", "installed")]
        shown += [{"romm": "from RomM", "local": "on this computer"}.get(self._gm_filters.get("source", ""), ""),
                  {"yes": "installed", "no": "not installed"}.get(self._gm_filters.get("installed", ""), "")]
        if self._gm_filters.get("rating"):
            r = self._gm_filters["rating"]
            shown.append("not rated" if r == "unrated" else f"{r} star{'s' if r != '1' else ''}" + ("" if r == "5" else " or more"))
        if self._gm_filters.get("status"):
            shown.append(gm.STATUSES.get(self._gm_filters["status"], "no status").lower())
        shown += [{"played": "played", "unplayed": "unplayed"}.get(self._gm_filters.get("played", ""), "")] + (["missing files"] if self._gm_filters.get("missing") else [])
        shown = [x for x in shown if x]
        if not n:
            return "No game matches that." if self.gm_search.text().strip() or shown else "Nothing here yet."
        return f"{n} game{'s' if n != 1 else ''} in {where}" + (f", filtered to {', '.join(shown)}" if shown else "") + "."

    def _gm_check_missing(self, games: list[gm.Game]) -> None:
        """Look (in the background: a drive can be slow) for shown games whose file isn't there, and dim them."""
        self._gm_missing_token += 1
        token, snapshot = self._gm_missing_token, list(games)

        def work() -> None:
            self.ui_q.put(("games_missing", token, gm.missing_ids(snapshot)))

        threading.Thread(target=work, daemon=True).start()

    # ---- filters -----------------------------------------------------------------------------------------------
    def _gm_update_filter_button(self) -> None:
        n = len(self._gm_filters)
        self.gm_filter_btn.setText(f"Filters  ·  {n}" if n else "Filters")

    def _gm_fill_filter_menu(self, menu: QMenu) -> None:
        menu.clear()
        facets = self.gm_store.facets()
        for key, title in (("genre", "Genre"), ("decade", "Decade"), ("developer", "Developer"), ("publisher", "Publisher"), ("region", "Region")):
            values = facets.get(key, [])
            current = self._gm_filters.get(key, "")
            sub_menu = menu.addMenu(title + (f":  {current}" if current else ""))
            if not values:
                sub_menu.addAction("Nothing to choose yet (try Get info)").setEnabled(False)
                continue
            for value, n in values[:30]:
                act = sub_menu.addAction(f"{value}   ({n})")
                act.setCheckable(True)
                act.setChecked(current == value)
                act.triggered.connect(lambda _c=False, k=key, v=value: self._gm_set_filter(k, v))
            if len(values) > 30:
                sub_menu.addSeparator()
                sub_menu.addAction(f"{len(values) - 30} more: use the search box").setEnabled(False)
        for key, title, options in (("rating", "Rating", (("5", "5 stars"), ("4", "4 stars or more"), ("3", "3 stars or more"), ("unrated", "Not rated"))),
                                    ("status", "Progress", (*gm.STATUSES.items(), ("none", "No status")))):
            current = self._gm_filters.get(key, "")
            sub_menu = menu.addMenu(title + (f":  {dict(options).get(current, current)}" if current else ""))
            for value, label in options:
                act = sub_menu.addAction(label)
                act.setCheckable(True)
                act.setChecked(current == value)
                act.triggered.connect(lambda _c=False, k=key, v=value: self._gm_set_filter(k, v))
        for key, title, options in (("source", "Source", (("romm", "From RomM"), ("local", "On this computer"))),
                                    ("installed", "Installed", (("yes", "Installed"), ("no", "Not installed yet")))):
            current = self._gm_filters.get(key, "")
            sub_menu = menu.addMenu(title + (f":  {dict(options).get(current, current)}" if current else ""))
            for value, label in options:
                act = sub_menu.addAction(label)
                act.setCheckable(True)
                act.setChecked(current == value)
                act.triggered.connect(lambda _c=False, k=key, v=value: self._gm_set_filter(k, v))
        status = menu.addMenu("Status")
        for key, value, label in (("played", "played", "Played"), ("played", "unplayed", "Unplayed"), ("missing", True, "Missing files")):
            act = status.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._gm_filters.get(key) == value)
            act.triggered.connect(lambda _c=False, k=key, v=value: self._gm_set_filter(k, v))
        menu.addSeparator()
        group = menu.addAction("Group versions of a game")
        group.setCheckable(True)
        group.setChecked(self._gm_group)
        group.triggered.connect(lambda _c=False: self._gm_set_group(not self._gm_group))
        as_list = menu.addAction("List view")
        as_list.setCheckable(True)
        as_list.setChecked(self._gm_view == "list")
        as_list.setToolTip("Rows with columns instead of posters. Click a heading to sort by it, again to turn it round.")
        as_list.triggered.connect(lambda _c=False: self._gm_set_view("grid" if self._gm_view == "list" else "list"))
        menu.addAction("Random game", self._gm_random)
        menu.addAction("Playtime by platform...", self._gm_stats)
        save = menu.addAction("Save as smart collection...")
        save.setEnabled(bool(self._gm_current_rules()))
        save.triggered.connect(lambda _c=False: self._gm_save_smart())
        clear = menu.addAction("Clear filters")
        clear.setEnabled(bool(self._gm_filters))
        clear.triggered.connect(lambda _c=False: self._gm_set_filter("", None))

    def _gm_set_filter(self, key: str, value) -> None:
        """Choose a filter value (choosing the one that is chosen lets it go); an empty key clears every filter."""
        if not key:
            self._gm_filters.clear()
        elif self._gm_filters.get(key) == value:
            del self._gm_filters[key]
        else:
            self._gm_filters[key] = value
        self._gm_leave_detail()
        self._gm_fill()

    def _gm_set_group(self, on: bool) -> None:
        self._gm_group = on
        self.gm_store.set_setting("group", "1" if on else "0")
        self._gm_fill()

    def _gm_cell_size(self) -> QSize:
        cw, ch, _pw, _ph = cell_metrics(self._gm_zoom)
        return QSize(cw, ch)

    def _gm_sort_changed(self, _index: int = 0) -> None:
        self._gm_sort = str(self.gm_sort_box.currentData() or "name")
        self.gm_store.set_setting("sort", self._gm_sort)
        self._gm_fill()

    def _gm_zoom_changed(self, step: int) -> None:
        """The poster-size slider: every cell is resized, and the grid lays itself out again."""
        self._gm_zoom = ZOOM_STEPS[max(0, min(step, len(ZOOM_STEPS) - 1))] / 100
        self.gm_store.set_setting("zoom", str(ZOOM_STEPS[max(0, min(step, len(ZOOM_STEPS) - 1))]))
        size = self._gm_cell_size()
        self.gm_list.setUpdatesEnabled(False)
        for i in range(self.gm_list.count()):
            self.gm_list.item(i).setSizeHint(size)
        self.gm_list.setUpdatesEnabled(True)
        self.gm_list.scheduleDelayedItemsLayout()
        self.gm_list.viewport().update()

    def _gm_load_view_settings(self) -> None:
        """Bring back the sort and poster size from the last run (without saving them again)."""
        key = self.gm_store.setting("sort", "name")
        self._gm_group = self.gm_store.setting("group", "1") != "0"
        self._gm_view = "list" if self.gm_store.setting("view") == "list" else "grid"
        col, _sep, way = self.gm_store.setting("list_sort", "name:asc").partition(":")
        self._gm_col = (col if col in gm.LIST_COLUMNS else "name", way == "desc")
        self._gm_sort = key if key in gm.SORTS else "name"
        self.gm_sort_box.blockSignals(True)
        self.gm_sort_box.setCurrentIndex(max(0, self.gm_sort_box.findData(self._gm_sort)))
        self.gm_sort_box.blockSignals(False)
        try:
            pct = int(self.gm_store.setting("zoom", "100"))
        except ValueError:
            pct = 100
        pct = pct if pct in ZOOM_STEPS else 100
        self._gm_zoom = pct / 100
        self.gm_zoom_slider.blockSignals(True)
        self.gm_zoom_slider.setValue(ZOOM_STEPS.index(pct))
        self.gm_zoom_slider.blockSignals(False)

    # ---- RomM ----------------------------------------------------------------------------------------------------
    ROMM_STALE = 6 * 3600  # a listing older than this is read again when the page opens or the timer fires

    def _gm_romm_configured(self) -> bool:
        return bool(romm.normalize_url(apikeys.get("romm_url")) and apikeys.get("romm"))

    def _gm_romm_folder(self) -> Path:
        """Where installed RomM games go: the player's choice (Libraries window), else a folder of Distill's own."""
        return Path(self.gm_store.setting("romm_folder") or str(rc.root() / "RomM"))

    def _gm_romm_sync(self, force: bool) -> None:
        """Read the games on the player's RomM server in the background and merge them into the library. On startup and now and then, only
        when the last read is old; `force` (the Scan button) always reads."""
        if self._gm_romm_busy or not self._gm_romm_configured():
            return
        try:
            last = float(self.gm_store.setting("romm_synced", "0") or 0)
        except ValueError:
            last = 0.0
        if not force and time.time() - last < self.ROMM_STALE:
            return
        self._gm_romm_busy = True
        url, token, folder, q, stop = apikeys.get("romm_url"), apikeys.get("romm"), self._gm_romm_folder(), self.ui_q, self._gm_stop
        store = self.gm_store
        self.gm_status.setText("Reading RomM...")

        def work() -> None:
            try:
                entries = romm.fetch_library(url, token, folder, lambda t: q.put(("games_note", t)), stop)
                if stop.is_set():
                    q.put(("games_romm", 0, 0, "", force))
                    return
                added, removed = store.sync_remote(entries, f"romm:{romm.server_key(url)}")
                store.set_setting("romm_synced", str(time.time()))
                q.put(("games_romm", added, removed, "", force))
            except romm.RommError as exc:
                q.put(("games_romm", 0, 0, str(exc), force))
            except Exception as exc:  # noqa: BLE001 - a server that misbehaves must not take the page down
                q.put(("games_romm", 0, 0, f"RomM sync failed: {exc}", force))

        threading.Thread(target=work, daemon=True).start()

    def _gm_romm_done(self, added: int, removed: int, error: str, manual: bool) -> None:
        self._gm_romm_busy = False
        if error:
            if manual:  # a quiet failure at startup (the server is off, say) isn't worth a message; a pressed Scan is
                self.gm_status.setText(error)
            return
        self._gm_reload()
        if manual or added or removed:
            bits = [f"{added} new" if added else "", f"{removed} gone" if removed else ""]
            self.gm_status.setText("RomM: " + (", ".join(b for b in bits if b) or "nothing changed") + ".")

    def _gm_sync_play_button(self, g: gm.Game) -> None:
        """The detail page's main button: Play, or for a RomM game that is not on this computer yet Install, and Installing (filling) while it comes in."""
        btn = self.gmd_play
        if g.id in self._gm_installs:
            btn.setText("Installing")
            btn.setEnabled(False)
            btn.setToolTip("Downloading from RomM. It starts by itself when it is in.")
            if not self._gm_play_fill.isVisible():
                self._gm_play_fill.begin()
            self._gm_play_fill.progress(self._gm_installs[g.id])
            return
        self._gm_play_fill.finish()
        if g.remote and not g.installed:
            btn.setText("Install")
            btn.setEnabled(True)
            btn.setToolTip(f"Download {fmt_size(g.size)} from RomM, then start it." if g.size else "Download it from RomM, then start it.")
        else:
            btn.setText("Play")
            btn.setToolTip("")

    def _gm_install(self, gid: str) -> None:
        """Download a RomM game on a worker thread (the button fills as it comes in), then start it with the emulator the page is set to."""
        g = self.gm_store.get(gid)
        if g is None or not g.remote or gid in self._gm_installs:
            return
        url, token = apikeys.get("romm_url"), apikeys.get("romm")
        if not (romm.normalize_url(url) and token):
            self._toast("Add your RomM address and token first: Settings > API keys > Games & artwork.", "warn", 6000)
            return
        stop = threading.Event()
        self._gm_installs[gid], self._gm_install_stop[gid] = 0.0, stop
        self._gm_installing_changed(g)
        q, dest = self.ui_q, Path(g.path)

        def work() -> None:
            def progress(done: int, total: int) -> None:
                q.put(("games_install", gid, "progress", done / total if total > 0 else 0.0))

            try:
                path = romm.Client(url, token).download(g.remote_id, g.remote_name, max(1, g.remote_files), dest, progress, stop, g.size)
            except romm.RommError as exc:
                q.put(("games_install", gid, "error", str(exc)))
                return
            except Exception as exc:  # noqa: BLE001
                q.put(("games_install", gid, "error", f"The install failed: {exc}"))
                return
            q.put(("games_install", gid, "done", str(path)))

        threading.Thread(target=work, daemon=True).start()

    def _gm_cancel_install(self, gid: str) -> None:
        if (stop := self._gm_install_stop.get(gid)) is not None:
            stop.set()

    def _gm_installing_changed(self, g: gm.Game | None) -> None:
        self.gm_list.viewport().update()
        if g is not None and self._gm_current == g.id:
            self._gm_sync_play_button(g)

    def _gm_install_event(self, gid: str, kind: str, payload) -> None:
        g = self.gm_store.get(gid)
        if kind == "progress":
            if gid in self._gm_installs:
                self._gm_installs[gid] = max(self._gm_installs[gid], float(payload))
                self.gm_list.viewport().update()
                if self._gm_current == gid and g is not None:
                    self._gm_play_fill.progress(self._gm_installs[gid])
                    self.gmd_msg.setText(f"Installing from RomM: {int(self._gm_installs[gid] * 100)}%")
            return
        self._gm_installs.pop(gid, None)
        self._gm_install_stop.pop(gid, None)
        self._gm_installing_changed(g)
        if g is None:
            return
        if kind == "error":
            msg = "Install cancelled." if payload == "Cancelled." else str(payload)
            (self.gmd_msg if self._gm_current == gid else self.gm_status).setText(msg)
            return
        self._gm_reload()  # the game is installed now: it is no longer greyed as 'not here', and the page can offer Play
        fresh = self.gm_store.get(gid) or g
        if self._gm_current == gid:
            self._gm_fill_with(fresh)
            self._gm_refresh_buttons()
        # the file is on disk: hand it straight to the emulator, as a press of Play would
        try:
            label = self._gm_launch(fresh, (self.gmd_with.currentData() or "") if self._gm_current == gid else fresh.emulator or "")
        except gm.GameError as exc:
            (self.gmd_msg if self._gm_current == gid else self.gm_status).setText(f"Installed {fresh.name}, but it didn't start: {exc}")
            return
        (self.gmd_msg if self._gm_current == gid else self.gm_status).setText(f"Installed {fresh.name} and started it with {label}.")

    # ---- list view -----------------------------------------------------------------------------------------------
    def _gm_show_view(self, empty: bool = False) -> None:
        """Show the posters or the list (whichever is chosen), and turn off what the list has no use for."""
        listing = self._gm_view == "list"
        self.gm_list.setVisible(not empty and not listing)
        self.gm_table.setVisible(not empty and listing)
        self.gm_sort_box.setEnabled(not listing)  # the list is sorted by its headings
        self.gm_zoom_slider.setEnabled(not listing)

    def _gm_set_view(self, view: str) -> None:
        self._gm_view = view
        self.gm_store.set_setting("view", view)
        self._gm_leave_detail()
        self._gm_fill()

    def _gm_row_id(self, row: int) -> str:
        item = self.gm_table.item(row, 0)
        return str(item.data(ID_ROLE)) if item is not None else ""

    def _gm_fill_table(self, shown: list[gm.Game]) -> None:
        col, descending = self._gm_col
        rows = gm.sort_by_column(shown, col, descending)
        keys = list(gm.LIST_COLUMNS)
        now = time.time()
        table = self.gm_table
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        table.setRowCount(len(rows))
        for r, g in enumerate(rows):
            for c, key in enumerate(keys):
                item = QTableWidgetItem(gm.column_text(g, key, now))
                if c == 0:
                    item.setData(ID_ROLE, g.id)
                    versions = len(self._gm_groups.get(g.id, ()))
                    item.setToolTip(f"{g.name}\n{g.system}" + (f", {g.region}" if g.region else "") + (f"\n{versions} versions in your library" if versions > 1 else ""))
                if key in ("year", "rating", "last_played", "play_seconds", "added"):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(r, c, item)
        table.blockSignals(False)
        table.horizontalHeader().setSortIndicator(keys.index(col), Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
        table.setUpdatesEnabled(True)
        self._gm_dim_missing()
        self._gm_sync_table_selection()

    def _gm_dim_missing(self) -> None:
        """Rows whose file isn't there are drawn in the muted colour, as the posters are dimmed."""
        if self._gm_view != "list":
            return
        dim, normal = QColor(T.disabled_fg), QColor(T.fg)
        table = self.gm_table
        for r in range(table.rowCount()):
            colour = dim if self._gm_row_id(r) in self._gm_missing else normal
            for c in range(table.columnCount()):
                if (item := table.item(r, c)) is not None:
                    item.setForeground(colour)

    def _gm_column_clicked(self, index: int) -> None:
        """A heading was clicked: sort by it (the way it starts is the natural one for it); clicking the same one again turns it round."""
        key = list(gm.LIST_COLUMNS)[index]
        col, descending = self._gm_col
        self._gm_col = (key, not descending) if key == col else (key, gm.LIST_COLUMNS[key][1] == "desc")
        self.gm_store.set_setting("list_sort", f"{self._gm_col[0]}:{'desc' if self._gm_col[1] else 'asc'}")
        self._gm_fill()

    def _gm_table_selected(self) -> None:
        """Rows picked in the list are the page's selection, so the bar and its actions work as they do for posters."""
        self._gm_selected = {self._gm_row_id(i.row()) for i in self.gm_table.selectionModel().selectedRows()} - {""}
        self._gm_selection_changed()

    def _gm_sync_table_selection(self) -> None:
        """Make the list's highlighted rows match `_gm_selected` (it was changed from the bar or a menu)."""
        if self._gm_view != "list":
            return
        table = self.gm_table
        table.blockSignals(True)
        table.clearSelection()
        mode = table.selectionMode()
        table.setSelectionMode(QTableWidget.SelectionMode.MultiSelection)
        for r in range(table.rowCount()):
            if self._gm_row_id(r) in self._gm_selected:
                table.selectRow(r)
        table.setSelectionMode(mode)
        table.blockSignals(False)

    def _gm_table_context(self, pos) -> None:
        item = self.gm_table.itemAt(pos)
        if item is None:
            return
        gid = self._gm_row_id(item.row())
        if gid and gid not in self._gm_selected:  # right-clicking a row outside the selection is about that row alone
            self._gm_selected = {gid}
            self._gm_sync_table_selection()
            self._gm_selection_changed()
        if gid:
            self._gm_context_menu(gid, self.gm_table.viewport().mapToGlobal(pos))

    # ---- random game, DAT check, playtime ----------------------------------------------------------------------
    def _gm_random(self) -> None:
        """Open a game picked at random from the ones showing (the platform, collection, pill, search and filters all narrow the choice)."""
        shown = [self._gm_by_id[i] for i in self._gm_shown_ids() if i in self._gm_by_id]
        g = gm.pick_random(shown, self._gm_random_last)
        if g is None:
            self.gm_status.setText("No game to pick from.")
            return
        self._gm_random_last = g.id
        self._gm_show_detail(g.id)

    def _gm_stats(self) -> None:
        """Time played on each platform, over the whole library."""
        StatsDialog(self, gm.playtime_by_platform(self.gm_store.games())).exec()

    def _gm_verify(self, ids: list[str]) -> None:
        """Check the files of these games against a DAT the player picks (No-Intro, Redump, TOSEC) and say what matches."""
        if self._gm_verifying:
            self._toast("A DAT check is already running.", "warn", 4000)
            return
        games = self._gm_tool_games(ids)
        if not games:
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Choose a DAT file", "", "DAT files (*.dat *.xml);;All files (*)")
        if not path:
            return
        from .. import datverify  # noqa: PLC0415
        try:
            dat = datverify.load_dat(Path(path))
        except Exception as exc:  # noqa: BLE001 - a DAT that cannot be read is said in words
            msgbox.warning(self, "DAT check", f"Couldn't read that DAT file.\n\n{exc}")
            return
        self._gm_verifying = True
        self.gm_status.setText(f"Checking {len(games)} game{'s' if len(games) != 1 else ''} against {dat.name or Path(path).name}...")
        q, stop, names = self.ui_q, threading.Event(), {g.id: g.name for g in games}

        def work() -> None:
            try:
                result = gm.verify_games(games, dat, stop, lambda text: q.put(("games_verify_note", text)))
                q.put(("games_verify", result, names, dat.name or Path(path).name))
            except Exception as exc:  # noqa: BLE001
                q.put(("games_verify", exc, names, ""))

        threading.Thread(target=work, daemon=True).start()

    def _gm_verify_done(self, result, names: dict[str, str], dat_name: str) -> None:
        self._gm_verifying = False
        if isinstance(result, Exception):
            self.gm_status.setText("")
            msgbox.warning(self, "DAT check", f"The check stopped: {result}")
            return
        self.gm_status.setText(f"{dat_name}: {result.summary()}")
        lines = []
        for title, ids in (("Differ from the DAT (bad dump, patch or hack)", result.bad), ("Not in the DAT", result.unknown), ("Couldn't be read", result.unreadable)):
            if ids:
                lines.append(f"{title}:\n" + "\n".join(f"  {names.get(i, i)}" for i in ids[:12]) + (f"\n  ...and {len(ids) - 12} more" if len(ids) > 12 else ""))
        if result.skipped:
            lines.append(f"{len(result.skipped)} playlist or cue sheet file(s) were skipped: their tracks are checked by Library Report.")
        msgbox.information(self, "DAT check", f"{dat_name}\n{result.summary()}" + ("\n\n" + "\n\n".join(lines) if lines else ""))

    def _gm_context(self, pos) -> None:
        """Right-click on a poster: what can be done with that game (or with all the selected games, when it is one of them)."""
        item = self.gm_list.itemAt(pos)
        if item is not None:
            self._gm_context_menu(str(item.data(ID_ROLE)), self.gm_list.viewport().mapToGlobal(pos))

    def _gm_context_menu(self, gid: str, where) -> None:
        g = self.gm_store.get(gid)
        if g is None:
            return
        many = gid in self._gm_selected and len(self._gm_selected) > 1
        ids = list(self._gm_selected) if many else [gid]
        games = [x for x in (self.gm_store.get(i) for i in ids) if x is not None]
        menu = QMenu(self)
        if not many:
            if gm.resume_state(g, g.emulator or "", self._gm_retro(), self.gm_store.default_for(g.system), self._gm_emus(g.system)) is not None:
                menu.addAction("Continue", lambda: self._gm_quick_play(gid, resume=True))
            if gid in self._gm_installs:
                menu.addAction("Cancel install", lambda: self._gm_cancel_install(gid))
            elif g.remote and not g.installed:
                menu.addAction("Install", lambda: self._gm_install(gid))
            else:
                menu.addAction("Play", lambda: self._gm_quick_play(gid))
            menu.addAction("Open details", lambda: self._gm_show_detail(gid))
            menu.addSeparator()
        menu.addAction("Deselect" if gid in self._gm_selected else "Select", lambda: self._gm_toggle_select(gid))
        menu.addAction("Remove from favourites" if all(x.favorite for x in games) else "Add to favourites", lambda: self._gm_bulk_favorite(ids))
        self._gm_fill_collection_menu(menu.addMenu("Add to collection"), ids)
        self._gm_fill_platform_menu(menu.addMenu("Add to platform"), ids)
        self._gm_fill_details_menu(menu.addMenu("Set details"), ids)
        menu.addAction("Get info from LaunchBox", lambda: self._gm_bulk_match(ids))
        self._gm_tools_menu(menu.addMenu("Tools"), ids)
        menu.addAction(f"{g.system} setup...", lambda: self._gm_platform_setup(g.system))
        if not many:
            menu.addAction("Edit...", lambda: self._gm_edit(gid))
            menu.addAction("Show in folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(g.path).parent))))
        menu.addSeparator()
        menu.addAction("Remove from library" if not many else f"Remove {len(ids)} games from library", lambda: self._gm_bulk_remove(ids))
        menu.exec(where)

    # ---- Distill's other tools, from the games ------------------------------------------------------------------
    def _gm_tools_menu(self, menu: QMenu, ids: list[str]) -> None:
        """Tools > convert (only when there is a tool for these files), fix names and discs, find the soundtrack."""
        games = [g for g in (self.gm_store.get(i) for i in self._gm_expand(ids)) if g is not None]
        files = gm.tool_files(games)
        try:
            target = route_drop(files) if files else None
        except OSError:  # a file that can't be read just has no tool offered
            target = None
        if target is not None and (target.page in self.tabs or target.page == "switch"):
            menu.addAction(f"Convert with {PAGE_INFO[target.page][0]}...", lambda: self._gm_tool_convert(ids))
        menu.addAction("Fix names and discs...", lambda: self._gm_tool_fix(ids))
        menu.addAction("Find soundtrack...", lambda: self._gm_tool_sound(ids))
        menu.addAction("Verify against a DAT...", lambda: self._gm_verify(ids))

    def _gm_tool_games(self, ids: list[str]) -> list[gm.Game]:
        return [g for g in (self.gm_store.get(i) for i in self._gm_expand(ids)) if g is not None]

    def _gm_tool_busy(self) -> bool:
        if getattr(self, "is_processing", False) or getattr(self, "org_busy", False):
            self._toast("A run is in progress: try again when it has finished.", "warn", 5000)
            return True
        return False

    def _gm_tool_convert(self, ids: list[str]) -> None:
        """Open the conversion page that suits these games with their files already chosen (the same routing as dropping them on the window)."""
        files = gm.tool_files(self._gm_tool_games(ids))
        if files and not self._gm_tool_busy() and not self._route_drop(files):
            self._toast("Distill has no tool for those files, or they are in different folders.", "warn", 6000)

    def _gm_tool_fix(self, ids: list[str]) -> None:
        """Open Fix Names & Discs on the folder these games are in."""
        games = self._gm_tool_games(ids)
        folder = gm.common_folder(games)
        if folder is None:
            self._toast("Those games are on different drives: pick them from one place.", "warn", 6000)
            return
        if self._gm_tool_busy():
            return
        label = games[0].name if len(games) == 1 else f"{len(games)} games"
        self._org_drop(DropTarget("fix", folder, (), label, "from your Games library"))

    def _gm_tool_sound(self, ids: list[str]) -> None:
        """Put the games' names in the Soundtrack Grabber and search."""
        names = gm.soundtrack_names(self._gm_tool_games(ids))
        if not names:
            return
        self.st_search.setText(names[0])  # the grabber searches one name at a time
        self._show_page("sound")
        if len(names) > 1:
            self._toast(f"Searching for {names[0]}. Search the other {len(names) - 1} one at a time.", "info", 6000)
        if self.st_search_btn.isEnabled():
            self._st_search()
        else:
            self._toast("A soundtrack search is already running. Press Search when it has finished.", "warn", 6000)

    def _gm_quick_play(self, gid: str, resume: bool = False) -> None:
        """Start a game straight from the grid with the emulator it is set to (or the platform's default), from its save state with `resume`."""
        g = self.gm_store.get(gid)
        if g is None:
            return
        if g.remote and not g.installed:  # a game on RomM is installed first, and starts when it is in
            self._gm_install(gid)
            return
        try:
            label = self._gm_launch(g, g.emulator or "", resume=resume)
        except gm.GameError as exc:
            self.gm_status.setText(str(exc))
            return
        self.gm_status.setText(f"{'Continuing' if resume else 'Started'} {g.name} with {label}.")

    # ---- selecting games --------------------------------------------------------------------------------------
    def _gm_toggle_select(self, gid: str) -> None:
        if gid in self._gm_selected:
            self._gm_selected.discard(gid)
        elif gid in self._gm_by_id:
            self._gm_selected.add(gid)
        self.gm_list.viewport().update()
        self._gm_sync_table_selection()
        self._gm_selection_changed()

    def _gm_shown_ids(self) -> list[str]:
        """The ids of the games showing, in the order they are shown (posters or rows)."""
        if self._gm_view == "list":
            return [self._gm_row_id(r) for r in range(self.gm_table.rowCount())]
        return [str(self.gm_list.item(i).data(ID_ROLE)) for i in range(self.gm_list.count())]

    def _gm_select_all(self) -> None:
        self._gm_selected = set(self._gm_shown_ids())
        self.gm_list.viewport().update()
        self._gm_sync_table_selection()
        self._gm_selection_changed()

    def _gm_clear_selection(self) -> None:
        self._gm_selected.clear()
        self.gm_list.viewport().update()
        self._gm_sync_table_selection()
        self._gm_selection_changed()

    def _gm_selection_changed(self) -> None:
        """The bar slides up while anything is selected and away when nothing is."""
        n = len(self._gm_selected)
        bar = self.gm_bar
        if n:
            bar.count.setText(f"{n} selected")
            everything = n >= len(self._gm_shown_ids())
            bar.all_btn.setVisible(not everything)
            chosen = [self._gm_by_id[g] for g in self._gm_selected if g in self._gm_by_id]
            bar.fav_btn.setText("★ Unfavourite" if chosen and all(g.favorite for g in chosen) else "☆ Favourite")
        bar.set_open(n > 0)

    def _gm_selected_games(self) -> list[gm.Game]:
        return [g for g in (self.gm_store.get(i) for i in self._gm_selected) if g is not None]

    def _gm_expand(self, ids: list[str] | None) -> list[str]:
        """The ids an action reaches: a poster that stands for several versions of a game stands for all of them."""
        out: list[str] = []
        for i in (list(self._gm_selected) if ids is None else ids):
            out.extend(v.id for v in self._gm_groups[i]) if i in self._gm_groups else out.append(i)
        return list(dict.fromkeys(out))

    def _gm_bulk_favorite(self, ids: list[str] | None = None) -> None:
        chosen = [g for g in (self.gm_store.get(i) for i in self._gm_expand(ids)) if g is not None]
        if chosen:
            self.gm_store.set_favorite_many([g.id for g in chosen], not all(g.favorite for g in chosen))
            self._gm_reload()

    def _gm_fill_platform_menu(self, menu: QMenu, ids: list[str] | None = None) -> None:
        menu.clear()
        for system, _n in self.gm_store.systems():
            if system != gm.UNKNOWN:
                menu.addAction(system, lambda s=system: self._gm_bulk_platform(s, ids))
        menu.addSeparator()
        menu.addAction("Another platform...", lambda: self._gm_bulk_platform_other(ids))
        menu.addAction("Back to the platform the scan found", lambda: self._gm_bulk_platform("", ids))

    def _gm_fill_collection_menu(self, menu: QMenu, ids: list[str] | None = None) -> None:
        menu.clear()
        collections = self.gm_store.collections()
        for cid, name, _n in collections:
            menu.addAction(name, lambda c=cid: self._gm_add_to_collection(c, ids))
        if collections:
            menu.addSeparator()
        menu.addAction("New collection...", lambda: self._gm_new_collection(ids))
        if self._gm_collection:
            menu.addAction(f"Remove from {self._gm_collection_names.get(self._gm_collection, 'this collection')}", lambda: self._gm_remove_from_collection(ids))

    def _gm_fill_add_menu(self, menu: QMenu, ids: list[str] | None = None) -> None:
        """The selection bar's 'Add to' menu: a collection, or a platform."""
        menu.clear()
        self._gm_fill_collection_menu(menu.addMenu("Collection"), ids)
        self._gm_fill_platform_menu(menu.addMenu("Platform"), ids)
        self._gm_fill_details_menu(menu.addMenu("Set details"), ids)

    def _gm_add_to_collection(self, cid: int, ids: list[str] | None = None) -> None:
        ids = self._gm_expand(ids)
        if ids:
            self.gm_store.add_to_collection(cid, ids)
            self._gm_reload()
            self.gm_status.setText(f"Added to {self._gm_collection_names.get(cid, 'the collection')}.")

    def _gm_new_collection(self, ids: list[str] | None = None) -> None:
        text, ok = msgbox.get_text(self, "New collection", "Name (for example Co-op nights, or Finish this year):")
        cid = self.gm_store.create_collection(text) if ok else 0
        if cid:
            self._gm_add_to_collection(cid, ids)

    def _gm_remove_from_collection(self, ids: list[str] | None = None) -> None:
        ids = self._gm_expand(ids)
        if ids and self._gm_collection:
            self.gm_store.remove_from_collection(self._gm_collection, ids)
            self._gm_selected.difference_update(ids)
            self._gm_reload()

    def _gm_fill_details_menu(self, menu: QMenu, ids: list[str] | None = None) -> None:
        """Genre, developer, publisher or year for every game at once (the platform has its own menu beside this one)."""
        menu.clear()
        for key, title in (("genre", "Genre"), ("developer", "Developer"), ("publisher", "Publisher"), ("year", "Year")):
            menu.addAction(f"{title}...", lambda k=key: self._gm_bulk_details(k, ids))

    def _gm_bulk_details(self, key: str, ids: list[str] | None = None) -> None:
        """Ask for one value and give it to every selected game. The box starts with the value they all share, if they do; clearing it and
        pressing OK empties the field after a question."""
        ids = self._gm_expand(ids)
        chosen = [g for g in (self.gm_store.get(i) for i in ids) if g is not None]
        if not chosen:
            return
        title = key.capitalize()
        shared = {getattr(g, key) for g in chosen}
        word = f"{len(chosen)} games" if len(chosen) != 1 else "this game"
        hint = " (four digits, for example 1994)" if key == "year" else (" (separate several with commas)" if key == "genre" else "")
        text, ok = msgbox.get_text(self, f"Set {key}", f"{title} for {word}{hint}:", shared.pop() if len(shared) == 1 else "")
        text = " ".join(text.split())
        if not ok:
            return
        if key == "year" and text and not (len(text) == 4 and text.isdigit()):
            self.gm_status.setText("A year is four digits, for example 1994. Nothing was changed.")
            return
        if not text:
            answer = msgbox.question(self, f"Clear {key}", f"Clear the {key} of {word}?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.gm_store.update_details_many([g.id for g in chosen], **{key: text})
        for gid in {g.id for g in chosen}:  # the overview and pictures are looked up again for the new details
            mm.clear_media(gid)
        self._gm_reload()
        self.gm_status.setText(f"{title} set for {word}." if text else f"{title} cleared for {word}.")

    def _gm_bulk_platform_other(self, ids: list[str] | None = None) -> None:
        ids = self._gm_expand(ids)
        names = sorted({*gm.system_choices(), *(s for s, _n in self.gm_store.systems() if s != gm.UNKNOWN)}, key=str.casefold)
        text, ok = msgbox.get_item(self, "Add to platform", f"Platform for {len(ids)} game{'s' if len(ids) != 1 else ''}:", names, 0, editable=True)
        if ok and text.strip():
            self._gm_bulk_platform(text.strip(), ids)

    def _gm_bulk_platform(self, system: str, ids: list[str] | None = None) -> None:
        ids = self._gm_expand(ids)
        if ids:
            self.gm_store.set_system_many(ids, system)
            self._gm_art_tried.difference_update(ids)  # a cover is looked for again on the new platform
            self._gm_details.clear()
            self._gm_refetch.update(ids)
            self._gm_reload()

    def _gm_bulk_remove(self, ids: list[str] | None = None) -> None:
        ids = self._gm_expand(ids)
        if not ids:
            return
        word = f"{len(ids)} games" if len(ids) != 1 else "this game"
        answer = msgbox.question(self, "Remove from library", f"Remove {word} from your library?\n\nThe files stay where they are; Distill just stops listing them, "
                                 "and scanning again doesn't bring them back.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.gm_store.hide(ids)
        self._gm_selected.difference_update(ids)
        self._gm_reload()

    def _gm_bulk_match(self, ids: list[str] | None = None) -> None:
        """Look games up in the LaunchBox Games Database, one after the other in the background, and fill in what they are missing."""
        ids = self._gm_expand(ids)
        games = [g for g in (self.gm_store.get(i) for i in ids) if g is not None]
        if not games or self._gm_matching or self._gm_scanning:
            return
        self._gm_matching = True
        store = self.gm_store
        self.gm_status.setText(f"Getting info for {len(games)} game{'s' if len(games) != 1 else ''}...")

        def work() -> None:
            done = changed = 0
            error = ""
            for g in games:
                try:
                    m = gm.best_match(g)
                    changed += bool(m and gm.fill_from_match(store, g, m))
                except gm.GameError as exc:
                    error = str(exc)
                    break
                except Exception:  # one game that can't be read must not stop the rest
                    pass
                done += 1
                self.ui_q.put(("games_match", done, len(games), changed, ""))
            self.ui_q.put(("games_match", done, len(games), changed, error or "end"))

        threading.Thread(target=work, daemon=True).start()

    # ---- scanning the library -----------------------------------------------------------------------------
    def _gm_choose_folder(self) -> None:
        """Add a games folder (the empty page's button) and scan."""
        roots = self.gm_store.library_roots()
        folder = QFileDialog.getExistingDirectory(self, "Choose the folder that holds your games", roots[-1] if roots else "")
        if not folder:
            return
        self.gm_store.add_library_root(folder)
        self._gm_scan()

    def _gm_libraries(self) -> None:
        dlg = LibrariesDialog(self, self.gm_store, self._gm_retro)
        dlg.exec()
        if dlg.romm_moved:  # installed games are looked for in the new folder, so the paths are worked out again
            self._gm_romm_sync(True)
        if dlg.added:
            self._gm_scan()
        elif dlg.changed:
            self._gm_selected.clear()
            self._gm_reload()

    def _gm_scan(self) -> None:
        if self._gm_scanning:
            return
        roots = self.gm_store.library_roots()
        self._gm_romm_sync(True)  # Scan looks at every source: the folders, and RomM if it is set up
        if not roots:
            if not self._gm_romm_configured():
                self._gm_choose_folder()
            return
        self._gm_scanning = True
        self.gm_scan_btn.setEnabled(False)
        self._gm_stop.clear()
        store, stop = self.gm_store, self._gm_stop
        self.gm_status.setText(f"Scanning {roots[0]}..." if len(roots) == 1 else f"Scanning {len(roots)} folders...")

        def work() -> None:
            added = removed = 0
            missing: list[str] = []
            try:
                for i, root in enumerate(roots, 1):
                    if stop.is_set():
                        break
                    if not Path(root).is_dir():  # a drive that is unplugged: its games stay listed, nothing is deleted
                        missing.append(root)
                        continue
                    prefix = f"Folder {i} of {len(roots)}: " if len(roots) > 1 else ""
                    findings, _note = find_library(Path(root), True, {"_note": lambda t, p=prefix: self.ui_q.put(("games_note", p + t))}, stop)
                    # a library folder named for a platform ('PS2', 'Game Boy Advance') places the games the scan can't
                    a, r = store.sync([e for f in findings for e in f.data], root, gm.folder_platform(root))
                    added, removed = added + a, removed + r
            except Exception as exc:  # a folder that can't be read must not take the page down
                self.ui_q.put(("games_scanned", added, removed, f"The scan failed: {exc}", missing))
                return
            self.ui_q.put(("games_scanned", added, removed, "", missing))

        threading.Thread(target=work, daemon=True).start()

    def _gm_event(self, ev: tuple) -> None:
        name = ev[0]
        if name == "games_note":
            if self._gm_scanning:
                self.gm_status.setText(ev[1])
        elif name == "games_scanned":
            self._gm_scanning = False
            _n, added, removed, error, missing = ev
            self._gm_reload()
            if error:
                self.gm_status.setText(error)
            else:
                bits = [f"{added} new" if added else "", f"{removed} gone" if removed else ""]
                text = "Scan finished: " + (", ".join(b for b in bits if b) or "nothing changed") + f". {self.gm_store.totals()[0]} games in all."
                if missing:
                    text += f" Couldn't reach {', '.join(missing)}: its games were left as they are."
                self.gm_status.setText(text)
        elif name == "games_romm":
            self._gm_romm_done(*ev[1:])
        elif name == "games_install":
            self._gm_install_event(*ev[1:])
        elif name == "games_media":
            _n, key, kind, payload = ev
            if kind == "done":
                self._gm_media_busy.discard(key)
            elif key == self._gm_media_key and self._gm_current:  # only for the game whose page is open
                g = self._gm_game()
                if g is None:
                    return
                if kind == "backdrop" and payload:
                    self._gm_set_backdrop(QPixmap(str(payload)))
                elif kind == "shots":
                    self._gm_show_shots(g, [Path(x) for x in payload])
                elif kind == "ra" and self._gm_ra_mode == "loading":
                    self._gm_show_ra(*payload)
        elif name == "games_missing":
            _n, token, ids = ev
            if token == self._gm_missing_token and ids != self._gm_missing:
                self._gm_missing = set(ids)
                self.gm_list.viewport().update()
                self._gm_dim_missing()
                if ids and not self._gm_scanning and not self._gm_matching and not self._gm_filters.get("missing"):
                    self.gm_status.setText(self.gm_status.text() + f" {len(ids)} file{'s are' if len(ids) != 1 else ' is'} missing.")
        elif name == "games_verify_note":
            if self._gm_verifying:
                self.gm_status.setText(ev[1])
        elif name == "games_verify":
            self._gm_verify_done(ev[1], ev[2], ev[3])
        elif name == "games_session":
            _n, gid, seconds = ev
            g = self.gm_store.get(gid)
            if g is not None:
                self.gm_status.setText(f"Played {g.name} for {gm.fmt_playtime(seconds)}.")
                if gid == self._gm_current:
                    self._gm_apply_facts(g, self._gm_details.get(gid))
        elif name == "games_match":
            _n, done, total, changed, state = ev
            if state:
                self._gm_matching = False
                self._gm_reload()
                self._gm_details.clear()
                self.gm_status.setText(state if state != "end" else f"Got info for {changed} of {total} game{'s' if total != 1 else ''}"
                                       + ("" if changed == total else ". The rest weren't found or had nothing new."))
                if self._gm_current:
                    self._gm_refetch.add(self._gm_current)
                    self._gm_show_detail(self._gm_current)
            else:
                self.gm_status.setText(f"Getting info... {done} of {total}")
        elif name == "games_art":
            self._gm_set_art(ev[1], ev[2])
        elif name == "games_details":
            self._gm_details[ev[1]] = ev[2]
            if ev[1] == self._gm_current:
                self._gm_apply_details()

    # ---- covers ---------------------------------------------------------------------------------------------
    def _gm_pixmap(self, g: gm.Game) -> QPixmap | None:
        """The cover for the grid. Called while painting, so only what is on screen is ever loaded or fetched."""
        if g.id not in self._gm_art:
            self._gm_ensure_art(g)
        return self._gm_art.get(g.id)

    def _gm_ensure_art(self, g: gm.Game) -> None:
        if g.id not in self._gm_art and (hit := gm.cover_path(g)) is not None:
            self._gm_set_art(g.id, hit, repaint=False)
        key = self.rc_store.sgdb_key()
        if g.id in self._gm_art_tried or rc.cover_settled(gm.shim(g), key):
            return
        self._gm_art_tried.add(g.id)
        self._gm_art_q.add(g, key)

    def _gm_set_art(self, gid: str, path: Path | None, repaint: bool = True) -> None:
        pix = cover_pixmap(path, DETAIL_W, DETAIL_H, self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0)
        if pix is None:
            return
        if gid not in self._gm_art and len(self._gm_art) >= ART_CACHE_MAX:
            self._gm_art.pop(next(iter(self._gm_art)))
        self._gm_art[gid] = pix
        if repaint:
            self.gm_list.viewport().update()
        if gid == self._gm_current and (cur := self._gm_by_id.get(gid)) is not None:
            self._gm_detail_cover(cur)

    def _gm_detail_cover(self, g: gm.Game) -> None:
        pix = self._gm_art.get(g.id)
        if pix is not None:
            self.gmd_cover.setText("")
            self.gmd_cover.setStyleSheet("background: transparent; border: none;")
            dpr = pix.devicePixelRatio() or 1.0
            self.gmd_cover.setFixedSize(math.ceil(pix.width() / dpr), math.ceil(pix.height() / dpr))
            self.gmd_cover.setPixmap(rounded_pixmap(pix, 16))
            return
        self.gmd_cover.setFixedSize(DETAIL_W, DETAIL_H)
        self.gmd_cover.setPixmap(QPixmap())
        self.gmd_cover.setText(g.name)
        T.bind_css(self.gmd_cover, lambda: f"background: {T.input.name()}; border: 1px solid {T.card_border.name()}; border-radius: 16px; "
                                           f"color: {T.fg3.name()}; font-weight: 700; padding: 16px;")

    def _gm_apply_cover(self, g: gm.Game, data: bytes | None) -> None:
        try:
            path = rc.set_cover(g.id, data) if data is not None else rc.clear_cover(g.id)
        except rc.RecompError as exc:
            msgbox.warning(self, "Cover", str(exc))
            return
        self._gm_art.pop(g.id, None)
        self._gm_art_tried.discard(g.id)
        if path is not None:
            self._gm_set_art(g.id, path)
        else:
            self._gm_detail_cover(g)
            self.gm_list.viewport().update()

    # ---- a game's page --------------------------------------------------------------------------------------
    def _gm_open_item(self, item: QListWidgetItem) -> None:
        gid = str(item.data(ID_ROLE))
        if self._gm_selected:  # while games are selected, a click on a poster selects it too rather than opening it
            self._gm_toggle_select(gid)
        else:
            self._gm_show_detail(gid)

    def _gm_back(self) -> None:
        gid = self._gm_current
        self._gm_cancel_flight()
        leaving = None  # the page's cover and where it is, for it to glide back to its place in the grid
        if gid and self._gm_can_fly() and self.gm_views.currentIndex() == 1:
            view = self.settings_scroll.viewport()
            box = flight.box_of(self.gmd_cover, view)
            if view.rect().intersects(box):
                leaving = (flight.picture_of(self.gmd_cover), box)
        self._gm_current = ""
        self.gm_views.setCurrentIndex(0)
        self._gm_reload()  # a favourite, a play or a new title changes the grid and the rail's counts
        self._gm_scroll_list(self._gm_list_scroll)  # back to where the grid was
        if leaving is not None:
            self._gm_fly_to_poster(gid, *leaving)

    # ---- the cover's flight between the grid and a page ---------------------------------------------------------
    def _gm_can_fly(self) -> bool:
        return False  # the detail is a card over the blurred list: no cover flight

    def _gm_scroll_list(self, value: int) -> None:
        """Move the grid's scroll bar, now and once more after it has been laid out (a rebuilt list starts at the top)."""
        bar = (self.gm_table if self._gm_view == "list" else self.gm_list).verticalScrollBar()
        bar.setValue(value)
        QTimer.singleShot(0, lambda: bar.setValue(value))

    def _gm_poster_box(self, gid: str) -> QRect | None:
        """Where this game's cover is in the grid, in the coordinates of the scroll area's viewport (where the flying picture lives); None when
        the game isn't in the grid or its row is scrolled out of sight."""
        if self._gm_view == "list":  # rows have no cover to fly to
            return None
        shown = self._gm_group_of.get(gid, gid)
        for i in range(self.gm_list.count()):
            item = self.gm_list.item(i)
            if item.data(ID_ROLE) == shown:
                break
        else:
            return None
        cell = self.gm_list.visualItemRect(item)
        grid = self.gm_list.viewport()
        if not grid.rect().intersects(cell):
            return None
        poster = poster_rect(cell, self._gm_art.get(shown), self._gm_zoom).toRect()  # the same slot the delegate paints into
        return QRect(grid.mapTo(self.settings_scroll.viewport(), poster.topLeft()), poster.size())

    def _gm_cancel_flight(self) -> None:
        if self._gm_flight is not None:
            self._gm_flight.finish()  # puts back whatever it was holding hidden
            self._gm_flight = None

    def _gm_fly_to_page(self, start: QRect) -> None:
        """The grid's cover glides to its place on the page while the page fades in; the page's own copy stays hidden until it lands."""
        view = self.settings_scroll.viewport()
        cover = self.gmd_cover
        picture = flight.picture_of(cover)
        motion.reveal(self.gm_views.widget(1))
        flight.hold(cover)
        self._gm_flight = flight.Flight(view, picture, start, lambda: flight.box_of(cover, view), done=lambda: flight.release(cover)).go()

    def _gm_fly_to_poster(self, gid: str, picture, start: QRect) -> None:
        """The page's cover glides back down to its cell while the grid fades in. The cell is looked up on every frame (the grid may still be
        settling) and its own cover stays out of sight until the picture lands on it."""
        view = self.settings_scroll.viewport()
        self._gm_hold_id = self._gm_group_of.get(gid, gid)
        self.gm_list.viewport().update()

        def done() -> None:
            self._gm_hold_id = ""
            self.gm_list.viewport().update()

        motion.reveal(self.gm_views.widget(0))
        self._gm_flight = flight.Flight(view, picture, start, lambda: self._gm_poster_box(gid), done=done).go()

    def _gm_game(self) -> gm.Game | None:
        g = self.gm_store.get(self._gm_current) if self._gm_current else None
        if g is not None:
            self._gm_by_id[g.id] = g
        return g

    def _gm_show_detail(self, gid: str) -> None:
        g = self.gm_store.get(gid)
        if g is None:
            return
        self._gm_cancel_flight()
        leaving = None  # where this game's cover is in the grid, for it to glide up from
        if self.gm_views.currentIndex() == 0:
            self._gm_list_scroll = (self.gm_table if self._gm_view == "list" else self.gm_list).verticalScrollBar().value()
            if self._gm_can_fly():
                leaving = self._gm_poster_box(gid)
        self._gm_by_id[gid] = g
        self._gm_current = gid
        self.gm_views.setCurrentIndex(1)
        self._gm_detail_cover(g)
        self._gm_ensure_art(g)
        self._gm_fill_detail(g)
        if gid in self._gm_details:
            self._gm_apply_details()
        else:
            self.gmd_about.setText("Looking for information about this game...")
            force = gid in self._gm_refetch  # its name or platform was edited: the saved lookup is for the old one
            self._gm_refetch.discard(gid)

            def work() -> None:
                try:
                    found = gm.launchbox_details(g, self.rc_store, force=force)
                except Exception:
                    found = gm.Details()
                self.ui_q.put(("games_details", gid, found))

            threading.Thread(target=work, daemon=True).start()
        if leaving is not None:
            self._gm_fly_to_page(leaving)

    def _gm_fill_detail(self, g: gm.Game) -> None:
        clear_layout(self.gmd_chips)
        logo = self.gmd_logo.set_system(g.system)
        self.gmd_logo.setVisible(logo)
        for text, css in ((g.system.upper() if not logo else "", chip_css(T.accent_text)), (gm.SOURCES.get(g.source, "").upper(), chip_css(QColor(ROMM_COLOUR))),
                          (g.region, pill_css(T.fg3)), (g.kind, pill_css(T.fg3)), (f"{g.discs} DISCS" if g.discs > 1 else "", pill_css(T.fg3))):
            if text:
                label = QLabel(text)
                label.setStyleSheet(css)
                self.gmd_chips.addWidget(label)
        self.gmd_title.setText(g.name)
        self.gmd_sub.setText(Path(g.path).name)
        self.gmd_blurb.setText("")
        self.gmd_blurb.setVisible(False)
        self.gmd_msg.setText("")
        self._gm_fill_versions(g)
        self._gm_fill_mine(g)
        self._gm_fill_with(g)
        self._gm_refresh_buttons()
        self._gm_load_media(g)
        self._gm_apply_facts(g, self._gm_details.get(g.id))

    def _gm_fill_mine(self, g: gm.Game) -> None:
        """The player's own rating, status and notes on a game's page."""
        self.gmd_stars.set_value(g.rating)
        self.gmd_status.blockSignals(True)
        self.gmd_status.setCurrentIndex(max(0, self.gmd_status.findData(g.status)))
        self.gmd_status.blockSignals(False)
        self.gmd_notes.flush()  # typing on the game we are leaving is saved to that game first
        self._gm_notes_gid = g.id
        self.gmd_notes.set_text(g.notes)

    def _gm_save_mine(self, gid: str = "", **fields) -> None:
        """Save a rating, status or notes the player changed on the game's page (for every version of the game, as in the Edit window)."""
        gid = gid or self._gm_current
        g = self.gm_store.get(gid) if gid else None
        if g is None:
            return
        for other in self._gm_groups.get(self._gm_group_of.get(gid, gid), ()):
            if other.id != gid:
                self.gm_store.update_details(other.id, **fields)
        self.gm_store.update_details(gid, **fields)
        fresh = self.gm_store.get(gid)
        if fresh is not None:
            self._gm_by_id[gid] = fresh
        if "notes" not in fields:
            self._gm_mine_dirty = True

    def _gm_fill_versions(self, g: gm.Game) -> None:
        """The Version box on a game's page, for a game that has more than one version in the library."""
        versions = self._gm_groups.get(self._gm_group_of.get(g.id, g.id), [])
        box = self.gmd_version
        box.blockSignals(True)
        box.clear()
        for v in versions:
            box.addItem(gm.version_label(v), v.id)
        box.setCurrentIndex(max(0, box.findData(g.id)))
        box.setVisible(len(versions) > 1)
        box.blockSignals(False)

    def _gm_pick_version(self, index: int) -> None:
        vid = self.gmd_version.itemData(index)
        if vid and vid != self._gm_current:
            self._gm_show_detail(str(vid))

    def _gm_update_warnings(self, g: gm.Game) -> None:
        """Say so on the game's page when its file is gone, or when the core that will play it may need BIOS files that aren't in RetroArch's folder."""
        lines = []
        if not Path(g.path).exists():
            lines.append(f"This game's file isn't there: {g.path}. Plug the drive back in or scan your library again.")
        setup = self._gm_retro()
        if setup is not None and self._gm_effective(g) == gm.RETROARCH:
            dem, dchoice = gm.parse_choice(self.gm_store.default_for(g.system))
            _emu, core = gm.parse_choice(self.gmd_with.currentData() or "")
            if not core:
                cores = gm.cores_for(g.system, setup.cores, setup.infos)
                core = dchoice if dem == gm.RETROARCH and dchoice in cores else (cores[0] if cores else "")
            missing = gm.missing_firmware(setup, core) if core else []
            if missing:
                names = ", ".join(dict.fromkeys(f.path for f in missing[:6]))
                lines.append(f"{setup.core_label(core)} may need BIOS files that aren't in {setup.system_dir}: {names}.")
        self.gmd_warn.setText("\n".join(lines))
        self.gmd_warn.setVisible(bool(lines))

    def _gm_retro(self) -> gm.RetroSetup | None:
        """The installed RetroArch (from the Emulators page), with the cores it has; None when it isn't set up."""
        try:
            inst = self.em_store.installed(gm.RETROARCH)
        except Exception:
            inst = None
        return gm.retro_setup(inst.folder, inst.exe, inst.linked) if inst is not None else None

    def _gm_emus(self, system: str) -> dict[str, gm.Standalone]:
        """The emulators of their own (installed on the Emulators page) that play a platform."""
        try:
            catalog = self._em_everything()

            def exe(rid: str):
                inst = self.em_store.installed(rid)
                path = inst.exe_path() if inst is not None else None
                return path if path is not None and path.exists() else None

            return gm.standalone_for(system, catalog, exe, lambda rid: self.gm_store.setting(f"args:{rid}"))
        except Exception:  # the Emulators page not being ready must not stop a game from starting
            return {}

    def _gm_fill_with(self, g: gm.Game) -> None:
        """The 'Play with' list: Automatic, then every installed RetroArch core and every installed emulator of its own that plays this platform."""
        setup = self._gm_retro()
        emus = self._gm_emus(g.system)
        default = self.gm_store.default_for(g.system)
        combo = self.gmd_with
        combo.blockSignals(True)
        combo.clear()
        cores = gm.cores_for(g.system, setup.cores, setup.infos) if setup else []
        playable = bool(cores or emus)
        if not playable:
            combo.addItem("Nothing installed for this platform", "")
            want = ", ".join(gm.preferred_cores(g.system)[:2])
            self.gmd_msg.setText(f"Nothing installed can play {g.system}. Install an emulator for it on the Emulators page"
                                 + (f", or a RetroArch core ({want} is a good choice)." if want else "."))
        else:
            dem, dchoice = gm.parse_choice(default)
            if cores:
                best = dchoice if dem == gm.RETROARCH and dchoice in cores else cores[0]
                auto = setup.core_label(best)
            else:
                auto = next(iter(emus.values())).name
            if dem in emus:
                auto = emus[dem].name
            combo.addItem(f"Automatic  ({auto})", "")
            for core in cores:
                label = setup.core_label(core) + ("  (system default)" if dem == gm.RETROARCH and core == dchoice else "")
                combo.addItem(f"RetroArch · {label}", f"{gm.RETROARCH}:{core}")
            for rid, s in emus.items():
                combo.addItem(s.name + ("  (system default)" if rid == dem else ""), rid)
            wanted = combo.findData(g.emulator) if g.emulator else 0
            combo.setCurrentIndex(max(0, wanted))
        combo.setEnabled(playable)
        combo.blockSignals(False)
        self.gmd_play.setEnabled(playable)
        self._gm_sync_play_button(g)

    def _gm_effective(self, g: gm.Game) -> str:
        """The emulator that will start the game with what the 'Play with' box says now: 'retroarch', a standalone emulator's id, or ''."""
        choice = self.gmd_with.currentData() or self.gm_store.default_for(g.system)
        emu, _core = gm.parse_choice(choice)
        if emu:
            return emu
        setup = self._gm_retro()
        if setup is not None and gm.cores_for(g.system, setup.cores, setup.infos):
            return gm.RETROARCH
        return next(iter(self._gm_emus(g.system)), "")

    def _gm_refresh_buttons(self) -> None:
        g = self._gm_game() if self._gm_current else None
        if g is None:
            return
        self.gmd_fav.setText("★ Favourited" if g.favorite else "☆ Favourite")
        choice = self.gmd_with.currentData() or ""
        self.gmd_default.setVisible(bool(choice) and choice != self.gm_store.default_for(g.system))
        self.gmd_default.setText(f"Make default for {g.system}")
        emu = self._gm_effective(g)
        self.gmd_opts.setVisible(bool(emu) and emu != gm.RETROARCH)  # launch options only exist for an emulator of its own
        state = gm.resume_state(g, choice, self._gm_retro(), self.gm_store.default_for(g.system), self._gm_emus(g.system))
        self.gmd_continue.setVisible(state is not None)
        if state is not None:
            self.gmd_continue.setToolTip(f"Pick up where you left off: save state slot {state.slot}, saved {gm.fmt_ago(state.saved)}.")
        self._gm_update_warnings(g)

    def _gm_with_changed(self, _index: int = 0) -> None:
        g = self._gm_game()
        if g is None:
            return
        self.gm_store.set_emulator(g.id, self.gmd_with.currentData() or "")
        self._gm_refresh_buttons()

    def _gm_make_default(self) -> None:
        g = self._gm_game()
        choice = self.gmd_with.currentData() or ""
        if g is None or not choice:
            return
        self.gm_store.set_default(g.system, choice)
        self.gm_store.set_emulator(g.id, "")  # the game follows the system again
        self._gm_fill_with(g)
        self._gm_refresh_buttons()
        self.gmd_msg.setText(f"{self.gmd_with.currentText().replace('  (system default)', '')} is now the default for {g.system}.")

    def _gm_launch(self, g: gm.Game, choice: str, resume: bool = False) -> str:
        """Start a game (from its newest save state with `resume`) and begin timing the session. Returns what it started with; raises
        gm.GameError with something the player can act on."""
        plan = gm.plan_launch(g, choice, self._gm_retro(), self.gm_store.default_for(g.system), self._gm_emus(g.system), resume=resume)
        proc = gm.spawn(plan)
        self.gm_store.record_play(g.id)
        store, gid, q = self.gm_store, g.id, self.ui_q

        def finished(seconds: float) -> None:
            store.add_playtime(gid, seconds)
            q.put(("games_session", gid, seconds))

        gm.watch_session(proc, finished)
        return plan.label

    def _gm_play(self) -> None:
        g = self._gm_game()
        if g is None:
            return
        if g.remote and not g.installed:
            self._gm_install(g.id)
            return
        try:
            label = self._gm_launch(g, self.gmd_with.currentData() or "")
        except gm.GameError as exc:
            self.gmd_msg.setText(str(exc))
            return
        self.gmd_msg.setText(f"Started with {label}. Distill adds up the time while it runs.")
        if (fresh := self._gm_game()) is not None:
            self._gm_apply_facts(fresh, self._gm_details.get(fresh.id))

    def _gm_continue(self) -> None:
        g = self._gm_game()
        if g is None:
            return
        try:
            label = self._gm_launch(g, self.gmd_with.currentData() or "", resume=True)
        except gm.GameError as exc:
            self.gmd_msg.setText(str(exc))
            return
        self.gmd_msg.setText(f"Continuing with {label}. Distill adds up the time while it runs.")
        if (fresh := self._gm_game()) is not None:
            self._gm_apply_facts(fresh, self._gm_details.get(fresh.id))

    def _gm_options(self) -> None:
        """Change the words put after an emulator's program when it starts a game."""
        g = self._gm_game()
        emu = self._gm_effective(g) if g is not None else ""
        found = self._gm_emus(g.system).get(emu) if g is not None else None
        if found is None:
            return
        default = gm.STANDALONE_ARGS.get(emu, "{rom}")
        text, ok = msgbox.get_text(self, f"{found.name} launch options",
                                   "What goes after the program to start a game. {rom} is the game's file, {dir} its folder, {name} its name without the "
                                   "extension. Leave empty to go back to Distill's own.", found.args or default)
        if not ok:
            return
        text = text.strip()
        self.gm_store.set_setting(f"args:{emu}", "" if text in ("", default) else text)
        self.gmd_msg.setText(f"Launch options for {found.name} " + ("saved." if text not in ("", default) else "are back to Distill's own."))

    def _gm_toggle_favorite(self) -> None:
        g = self._gm_game()
        if g is None:
            return
        self.gm_store.set_favorite(g.id, not g.favorite)
        self._gm_game()
        self._gm_refresh_buttons()

    def _gm_edit(self, gid: str) -> None:
        """The Edit game window (from a poster's pencil or the game's page). Saving keeps the details apart from what the scan found."""
        g = self.gm_store.get(gid) if gid else None
        if g is None or self._gm_scanning:
            return
        names = sorted({*gm.system_choices(), *(s for s, _n in self.gm_store.systems() if s != gm.UNKNOWN)}, key=str.casefold)
        dlg = EditGameDialog(self, g, names, self.rc_store.sgdb_key(), self._gm_art.get(gid))
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.values is None:
            return
        v = dlg.values
        self.gm_store.update_details(gid, title=v["title"], system=v["system"], developer=v["developer"], publisher=v["publisher"], year=v["year"], genre=v["genre"],
                                     rating=v["rating"], status=v["status"], notes=v["notes"])
        for other in self._gm_groups.get(gid, ()):  # the same game in another version: the facts, and what the player thinks of it, are the same
            if other.id != gid:
                self.gm_store.update_details(other.id, developer=v["developer"], publisher=v["publisher"], year=v["year"], genre=v["genre"],
                                             rating=v["rating"], status=v["status"], notes=v["notes"])
        fresh = self.gm_store.get(gid)
        before = (g.name, g.system, g.developer, g.publisher, g.year, g.genre)
        after = (fresh.name, fresh.system, fresh.developer, fresh.publisher, fresh.year, fresh.genre) if fresh is not None else before
        if after != before:  # the name or details changed: the overview, background and screenshots are looked up again for the new ones
            for key in {gid, self._gm_group_of.get(gid, gid), *(o.id for o in self._gm_groups.get(gid, ()))}:
                mm.clear_media(key)
        if dlg.cover_data is not None:
            self._gm_apply_cover(g, dlg.cover_data)
        elif dlg.cover_off:
            self._gm_apply_cover(g, None)
        elif fresh is not None and (fresh.name, fresh.system) != (g.name, g.system):
            gm.forget_cover(g)  # a cover is looked for again under the new name and platform (one the player chose stays)
            self._gm_art.pop(gid, None)
            self._gm_art_tried.discard(gid)
        if fresh is not None:
            self._gm_by_id[gid] = fresh
        self._gm_details.pop(gid, None)
        self._gm_refetch.add(gid)
        self._gm_reload()
        if self._gm_current == gid and fresh is not None:
            self._gm_show_detail(gid)

    def _gm_show_folder(self) -> None:
        g = self._gm_game()
        if g is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(g.path).parent)))

    # ---- what is known about the game -------------------------------------------------------------------------
    # ---- background art, screenshots, achievements ---------------------------------------------------------
    def _gm_set_backdrop(self, pix: QPixmap | None) -> None:
        self.gmd_view.set_art(pix)
        self._gm_backdrop_on = pix is not None and not pix.isNull()
        self.gmd_hero.setStyleSheet(self._gm_hero_css())  # see-through while there is art behind it

    def _gm_blur_of(self, g: gm.Game) -> QPixmap | None:
        """Until real background art is found (or if there is none) the game's own cover, blown up soft, stands in for it."""
        cover = self._gm_art.get(g.id)
        return cover.scaled(32, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation) if cover is not None else None

    def _gm_load_media(self, g: gm.Game) -> None:
        """Fill the game's page with its background, screenshots and achievements: what is saved shows at once, the rest is fetched in the background."""
        key = self._gm_group_of.get(g.id, g.id)  # versions of one game share them
        self._gm_media_key = key
        title = g.name
        platform = gm.LB_PLATFORM_NAMES.get(g.system, "" if g.system == gm.UNKNOWN else g.system)
        saved = mm.cached_backdrop(key)
        self._gm_set_backdrop(QPixmap(str(saved)) if saved else self._gm_blur_of(g))
        shots = mm.cached_screenshots(key)
        self._gm_show_shots(g, shots)
        if (not saved or not shots or any(not p.is_file() for p in shots)) and key not in self._gm_media_busy:
            self._gm_media_busy.add(key)
            sgdb, q = self.rc_store.sgdb_key(), self.ui_q

            def work() -> None:
                try:
                    if not saved:
                        back = mm.fetch_backdrop(key, title, platform, sgdb)
                        q.put(("games_media", key, "backdrop", str(back) if back else ""))
                    if not shots or any(not p.is_file() for p in shots):  # (previews arrive first, then the full-size pictures swap in)
                        def on_shots(paths) -> None:
                            q.put(("games_media", key, "shots", [str(x) for x in paths]))

                        on_shots(mm.fetch_screenshots(key, title, platform, on_update=on_shots))
                except Exception:  # pictures that can't be fetched just aren't shown
                    pass
                finally:
                    q.put(("games_media", key, "done", ""))

            threading.Thread(target=work, daemon=True).start()
        self._gm_load_ra(g, key)

    def _gm_show_shots(self, g: gm.Game, paths: list[Path]) -> None:
        tiles: list[QWidget] = []
        shown = [Path(p) for p in paths if mm.shot_image(Path(p)) is not None]  # (the big picture's path: it may still be downloading)
        for i, full in enumerate(shown):
            pix = _tile_pixmap(mm.shot_image(full), SHOT_W, SHOT_H, 12)
            if pix is None:
                continue
            tile = ClickLabel()
            tile.setFixedSize(SHOT_W, SHOT_H)
            tile.setPixmap(pix)
            tile.setCursor(Qt.CursorShape.PointingHandCursor)
            tile.setToolTip("Click to see it larger")
            tile.clicked.connect(lambda i=i, name=g.name: ImagePreview(self, name, [Path(x) for x in shown], i).exec())
            tiles.append(tile)
        self.gmd_shots.set_tiles(tiles)
        self.gmd_shots_card.setVisible(bool(tiles))

    def _gm_load_ra(self, g: gm.Game, key: str) -> None:
        card = self.gmd_ra_card
        self._gm_ra, self._gm_ra_mode = None, ""
        self.gmd_ra_strip.set_tiles([])
        self.gmd_ra_strip.setVisible(False)
        self.gmd_ra_hide.setVisible(False)
        self.gmd_ra_chip.set_url("https://retroachievements.org", "Open RetroAchievements.org")
        if not mm.ra_console_id(g.system):  # RetroAchievements has no games for this platform
            card.setVisible(False)
            return
        user, secret = apikeys.get("ra_user"), apikeys.get("ra")
        if not (user and secret):
            if self.gm_store.setting("ra_hide") == "1":
                card.setVisible(False)
                return
            self._gm_ra_mode = "connect"
            self.gmd_ra_text.setText("See this game's achievements here, and which ones you have unlocked. It needs your RetroAchievements user name and Web API key.")
            self.gmd_ra_btn.setText("Connect...")
            self.gmd_ra_btn.setVisible(True)
            self.gmd_ra_hide.setVisible(True)
            card.setVisible(True)
            return
        self._gm_ra_mode = "loading"
        self.gmd_ra_text.setText("Looking for this game's achievements...")
        self.gmd_ra_btn.setVisible(False)
        self.gmd_ra_hide.setVisible(False)
        card.setVisible(True)
        q, system, title, path = self.ui_q, g.system, g.name, g.path

        def work() -> None:
            try:
                game = mm.ra_progress(system, title, user, secret, path)
                if game is None:
                    q.put(("games_media", key, "ra", ("none", None, {})))
                    return
                from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
                with ThreadPoolExecutor(max_workers=6) as pool:
                    paths = list(pool.map(lambda a: mm.ra_badge(a.badge, locked=not a.earned), game.achievements[:80]))
                q.put(("games_media", key, "ra", ("game", game, {a.id: str(p) for a, p in zip(game.achievements, paths) if p})))
            except mm.MediaError as exc:
                q.put(("games_media", key, "ra", ("error", str(exc), {})))
            except Exception as exc:  # anything else: say so rather than leave "looking" up for ever
                q.put(("games_media", key, "ra", ("error", f"Couldn't read the achievements: {exc}", {})))

        threading.Thread(target=work, daemon=True).start()

    def _gm_show_ra(self, state: str, data, badges: dict) -> None:
        card = self.gmd_ra_card
        if state == "none":  # nothing on RetroAchievements for this game: no card at all
            self._gm_ra_mode = ""
            card.setVisible(False)
        elif state == "error":
            self._gm_ra_mode = "error"
            self.gmd_ra_text.setText(str(data))
            self.gmd_ra_btn.setText("Change key...")
            self.gmd_ra_btn.setVisible(True)
        else:
            game: mm.RaGame = data
            self._gm_ra, self._gm_ra_mode = game, "game"
            got, total = game.earned, len(game.achievements)
            points, all_points = game.points
            line = f"{got} of {total} unlocked  ·  {points:,} of {all_points:,} points"
            self.gmd_ra_text.setText(line + ("  ·  Complete!" if total and got == total else ""))
            self.gmd_ra_btn.setVisible(False)  # the chip above opens the game's page
            self.gmd_ra_chip.set_url(game.url, "Open this game on RetroAchievements")
            tiles: list[QWidget] = []
            for a in game.achievements:
                tile = ClickLabel()
                tile.setFixedSize(BADGE, BADGE)
                path = badges.get(a.id)
                pix = _tile_pixmap(Path(path), BADGE, BADGE, 14, dim=not a.earned) if path else None
                if pix is not None:
                    tile.setPixmap(pix)
                else:
                    tile.setText("?")
                    tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    T.bind_css(tile, lambda: f"background: {T.input.name()}; border: 1px solid {T.card_border.name()}; border-radius: 14px; color: {T.fg3.name()};")
                when = f"  ·  unlocked {a.date[:10]}" if a.earned and a.date else ""
                tile.setToolTip(f"{a.title}\n{a.description}\n{a.points} point{'s' if a.points != 1 else ''}{when}" + ("  ·  hardcore" if a.hardcore else ""))
                tile.setCursor(Qt.CursorShape.PointingHandCursor)
                tile.clicked.connect(lambda aid=a.id: QDesktopServices.openUrl(QUrl(f"https://retroachievements.org/achievement/{aid}")))
                tiles.append(tile)
            self.gmd_ra_strip.set_tiles(tiles)
            self.gmd_ra_strip.setVisible(bool(tiles))
        card.setVisible(self._gm_ra_mode != "")

    def _gm_ra_hide(self) -> None:
        self.gm_store.set_setting("ra_hide", "1")
        self.gmd_ra_card.setVisible(False)

    def _gm_ra_button(self) -> None:
        if self._gm_ra_mode in ("connect", "error"):
            if RetroAchievementsDialog(self).exec() == QDialog.DialogCode.Accepted:
                g = self._gm_game()
                if g is not None:
                    self._gm_load_ra(g, self._gm_media_key)

    def _gm_apply_details(self) -> None:
        g = self._gm_game()
        if g is None:
            return
        d = self._gm_details.get(g.id) or gm.Details()
        self.gmd_about.setText(d.overview or "No description was found for this game.")
        self.gmd_lb_chip.setVisible(bool(d.overview))
        self._gm_apply_facts(g, d)

    def _gm_apply_facts(self, g: gm.Game, d: gm.Details | None) -> None:
        d = d or gm.Details()
        played = f"{g.play_count} time{'s' if g.play_count != 1 else ''}" if g.play_count else ""
        session = f"Last session {gm.fmt_playtime(g.last_session)}" if g.last_session else ""
        released, genre = g.year or d.released, g.genre or d.genres
        self.gmd_tiles["released"].set_value(released)
        self.gmd_tiles["playtime"].set_value(gm.fmt_playtime(g.play_seconds) if g.play_seconds else "", "\n".join(x for x in (f"Played {played}" if played else "", session) if x))
        self.gmd_tiles["last"].set_value(_when(g.last_played))
        self.gmd_tiles["genre"].set_value(genre)
        self.gmd_facts.set_facts([  # (the release, play time, last play and genre are on the card at the top)
            ("DEVELOPER", g.developer or d.developers, None), ("PUBLISHER", g.publisher or d.publishers, None),
            ("PLATFORM", g.system, None), ("REGION", g.region, None), ("FORMAT", g.kind, None), ("DISCS", str(g.discs) if g.discs > 1 else "", None),
            ("SIZE", fmt_size(g.size) if g.size else "", None), ("PLAYED", played, None), ("LAST SESSION", gm.fmt_playtime(g.last_session) if g.last_session else "", None),
            ("FILE", g.path, None)])
