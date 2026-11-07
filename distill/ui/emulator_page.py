"""Emulators page: a grid of compact tiles (or, switched on, larger cards) grouped by console maker, with what is installed, what has an update and what your library
needs; a page per emulator to install, update, launch, link an existing copy or remove it, with the forks people have added
under it as cards of their own; one Emulators folder to point at. All the work is in `distill.emulators`; this file lays it out."""
from __future__ import annotations

import html
import threading
from pathlib import Path

from PySide6.QtCore import QRect, QSize, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton, QStackedWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)

from .. import emulators as em
from .. import recomps as rc
from . import flight, icons, motion
from .emu_images import normalize_icons
from .emu_cards import (
    caps_label, CardGrid, Callout, chip_css, clear_layout, break_path, EmulatorCard, EmulatorTile, FactList, ForkRow, group_chip, group_colour, LogoTile,
    pill_css, section_header, segment_css, StatTile, tint, TwoColumn,
)
from .fork_dialog import AddForkDialog
from .retroarch_cores import CoresMixin
from .ryujinx_ldn import LdnMixin
from .theme import T
from .widgets import accent_button, Card, hint

FILTERS = ("All emulators", "Installed", "Updates available", "Not installed", "For my library")
TILES = (("installed", "installed", 1), ("updates", "updates available", 2), ("library", "match your library", 4))  # key, caption, filter it applies


class EmulatorMixin(CoresMixin, LdnMixin):
    """Methods only; the state lives on DistillWindow (initialised by `_em_init`)."""

    def _em_init(self) -> None:
        self._em_base: list[em.Emulator] = []  # the manifest as written
        self._em_catalog: list[em.Emulator] = []  # ... plus what emurelation adds (emulators only: forks are kept apart)
        self._em_forks: list[em.Emulator] = []  # the forks the person added, from distill_forks.json
        self._em_by_id: dict[str, em.Emulator] = {}  # emulators and forks
        self._em_store: rc.Store | None = None
        self._em_latest: dict[str, object] = {}  # id -> (Release, asset) | error text
        self._em_busy: dict[str, threading.Event] = {}  # installs in progress (the Event cancels)
        self._em_prog: dict[str, str] = {}  # id -> what its install is doing now
        self._em_counts: dict[str, int] = {}  # library system -> games, from the Library Report
        self._em_current = ""
        self._em_snap = False  # the next detail refresh follows a change of emulator: no animation
        self._em_loaded = False
        self._em_scanning = False
        self.em_cards: dict[str, EmulatorCard | EmulatorTile] = {}  # compact tiles by default, cards when the person picks them
        self._em_shown: list[str] = []  # ids of the cards on the page, in order
        self._em_built_cards: bool | None = None  # whether those were built as cards (False: tiles); None before the first build
        self._em_fork_rows: dict[str, ForkRow] = {}
        self._em_logos_started = False
        self._em_list_scroll = 0  # where the list was scrolled to when an emulator was opened
        self._cores_init()  # RetroArch's Cores card
        self._ldn_init()  # Ryujinx's Local wireless card
        self._em_flight: flight.Flight | None = None  # the icon in the air between the list and a page
        self._em_flight_id = 0  # bumped by every new flight and cancellation, so a stale one knows to stand down

    @property
    def em_store(self) -> rc.Store:
        if self._em_store is None:
            self._em_store = em.open_store()
        return self._em_store

    # ---- building -------------------------------------------------------------------------------------------
    def _build_emulator_page(self) -> QWidget:
        self._em_init()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        self.em_views = QStackedWidget()
        self.em_views.addWidget(self._em_build_list())
        self.em_views.addWidget(self._em_build_detail())
        lay.addWidget(self.em_views, 1)
        page.setMinimumSize(1, 560)  # a width of 1, not 0: Qt reads 0 as "unset" and falls back to the widest child
        return page

    def _em_build_list(self) -> QWidget:
        view = QWidget()
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.em_tiles: dict[str, StatTile] = {}
        for key, caption, _mode in TILES:
            tile = StatTile(key, caption)
            tile.clicked.connect(self._em_tile_clicked)
            tiles.addWidget(tile, 1)
            self.em_tiles[key] = tile
        lay.addLayout(tiles)
        folder_card = Card()
        folder = QHBoxLayout()
        folder.setSpacing(8)
        label = QLabel("Emulators folder")
        label.setObjectName("muted")
        self.em_root_edit = QLineEdit()
        self.em_root_edit.setReadOnly(True)
        self.em_root_edit.setPlaceholderText("Not set. New installs go into Distill's own folder; pick one to keep them all together.")
        self.em_root_edit.setToolTip("Where emulators are installed, and the first place Distill looks for ones you already have. "
                                     "Updates keep your saves and settings: only files the download replaces are touched.")
        self.em_root_btn = QPushButton("Choose...")
        self.em_root_btn.clicked.connect(self._em_choose_root)
        self.em_scan_btn = QPushButton("Find installed")
        self.em_scan_btn.setToolTip("Look in the Emulators folder and the usual places for emulators you already have, and link them. Nothing is changed.")
        self.em_scan_btn.clicked.connect(lambda: self._em_scan(quiet=False))
        folder.addWidget(label)
        folder.addWidget(self.em_root_edit, 1)
        folder.addWidget(self.em_root_btn)
        folder.addWidget(self.em_scan_btn)
        folder_card.add(folder)
        lay.addWidget(folder_card)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.em_search = QLineEdit()
        self.em_search.setPlaceholderText("Search emulators, systems and forks...")
        self.em_search.setClearButtonEnabled(True)
        self.em_search.textChanged.connect(self._em_fill)
        self.em_filter = QComboBox()
        self.em_filter.addItems(FILTERS)
        self.em_filter.currentIndexChanged.connect(self._em_fill)
        self.em_updates_btn = QPushButton("Check for updates")
        self.em_updates_btn.clicked.connect(lambda: self._em_check_updates(quiet=False))
        self.em_all_btn = accent_button("Update all")
        self.em_all_btn.clicked.connect(self._em_update_all)
        self.em_all_btn.setVisible(False)
        top.addWidget(self.em_search, 1)
        top.addWidget(self.em_filter)
        top.addWidget(self._em_build_view_switch())
        top.addWidget(self.em_updates_btn)
        top.addWidget(self.em_all_btn)
        lay.addLayout(top)
        self.em_status = hint("", live=True)
        lay.addWidget(self.em_status)
        self.em_need = hint("")
        lay.addWidget(self.em_need)
        self.em_sections = QWidget()
        self.em_sections_lay = QVBoxLayout(self.em_sections)
        self.em_sections_lay.setContentsMargins(0, 0, 0, 0)
        self.em_sections_lay.setSpacing(16)
        lay.addWidget(self.em_sections)
        lay.addStretch(1)
        return view

    def _em_build_view_switch(self) -> QWidget:
        """Tiles | Cards: two joined buttons, one always pressed. Tiles (the compact look) is the default; the choice is remembered
        through `em_view_cards`, the one the settings file keeps."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.em_view_tiles = QPushButton("Tiles")
        self.em_view_cards = QPushButton("Cards")
        group = QButtonGroup(box)
        group.setExclusive(True)
        for button, first, tip in ((self.em_view_tiles, True, "Compact tiles: more emulators on screen at once."),
                                   (self.em_view_cards, False, "Larger cards that also show each emulator's version, games and forks.")):
            button.setCheckable(True)
            button.setToolTip(tip)
            T.bind_css(button, lambda first=first: segment_css(first))
            group.addButton(button)
            row.addWidget(button)
        self.em_view_tiles.setChecked(True)
        self.em_view_cards.toggled.connect(self._em_view_changed)  # after the default is set: building the page is not a change
        return box

    def _em_view_changed(self, _on: bool) -> None:
        if self._em_loaded:  # until the page is first shown there is nothing on it to redraw, and showing it builds the right look
            self._em_fill()

    def _em_build_detail(self) -> QWidget:
        """One emulator's page. A hero (picture, name, status, the buttons and the progress of an install), then two columns: what it is,
        its release notes and its forks on the left; the facts, the update channel and the links in a side panel. Under about 820 px the
        panel drops below the column, and the whole page stays a readable width however wide the window is."""
        view = QWidget()
        outer = QHBoxLayout(view)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        page = QWidget()
        page.setMaximumWidth(1060)  # long lines are hard to read: on a wide window the page stays centred at this width
        outer.addWidget(page, 1000)
        outer.addStretch(1)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        self.emd_back = QPushButton("← All emulators")
        self.emd_back.setFlat(True)
        self.emd_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.emd_back.clicked.connect(self._em_back)
        lay.addWidget(self.emd_back, 0, Qt.AlignmentFlag.AlignLeft)

        # -- the hero
        hero = self.emd_hero = Card()
        hero.lay.setContentsMargins(22, 20, 22, 18)
        hero.lay.setSpacing(16)
        head = QHBoxLayout()
        head.setSpacing(18)
        self.emd_logo = LogoTile(76, 18)
        head.addWidget(self.emd_logo, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(5)
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.emd_chip = QLabel()
        self.emd_status = QLabel()  # the same pill the list uses: Up to date / Update available / Not installed ...
        chips.addWidget(self.emd_chip)
        chips.addStretch(1)
        chips.addWidget(self.emd_status)
        names.addLayout(chips)
        self.emd_title = QLabel()
        self.emd_title.setObjectName("pageTitle")
        self.emd_title.setStyleSheet("font-size: 20pt;")  # the page's own title is already 22pt above: this one a step down
        self.emd_title.setWordWrap(True)
        names.addWidget(self.emd_title)
        self.emd_sub = QLabel()
        self.emd_sub.setObjectName("muted")
        self.emd_sub.setWordWrap(True)
        names.addWidget(self.emd_sub)
        self.emd_state = QLabel()
        self.emd_state.setWordWrap(True)
        self.emd_state.setTextFormat(Qt.TextFormat.RichText)
        names.addSpacing(2)
        names.addWidget(self.emd_state)
        head.addLayout(names, 1)
        hero.add(head)
        rule = QFrame()
        rule.setFixedHeight(1)
        T.bind_css(rule, lambda: f"background: {T.card_border.name()}; border: none;")
        hero.add(rule)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.emd_main = accent_button("Install")
        self.emd_main.setMinimumHeight(40)
        self.emd_main.setMinimumWidth(112)
        self.emd_main.clicked.connect(self._em_main_action)
        self.emd_launch = QPushButton("Launch")
        self.emd_launch.clicked.connect(self._em_launch)
        self.emd_folder = QPushButton("Open folder")
        self.emd_folder.clicked.connect(self._em_open_folder)
        self.emd_locate = QPushButton("I already have it...")
        self.emd_locate.setToolTip("Point Distill at a copy you already installed, so it can launch and update it.")
        menu = QMenu(self.emd_locate)
        menu.addAction("Choose its folder...", self._em_locate_folder)
        menu.addAction("Choose its program file...", self._em_locate_file)
        self.emd_locate.setMenu(menu)
        self.emd_remove = QPushButton("Uninstall")
        self.emd_remove.clicked.connect(self._em_remove)
        self.emd_unfork = QPushButton("Remove fork")
        self.emd_unfork.setToolTip("Take this fork off the list. An installed copy has to be uninstalled first.")
        self.emd_unfork.clicked.connect(self._em_remove_fork)
        danger = (f"QPushButton:hover {{ background: {T.btn_danger[0].name()}; border-color: {T.btn_danger[1].name()}; color: {T.bad.name()}; }}")
        for w in (self.emd_launch, self.emd_folder, self.emd_locate, self.emd_remove, self.emd_unfork):
            w.setMinimumHeight(40)
        for w in (self.emd_remove, self.emd_unfork):
            w.setStyleSheet(danger)  # only the hover is red: the destructive buttons sit apart and stay quiet until pointed at
        for w in (self.emd_main, self.emd_launch, self.emd_folder, self.emd_locate):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.emd_remove)
        row.addWidget(self.emd_unfork)
        hero.add(row)
        self.emd_bar = QProgressBar()
        self.emd_bar.setTextVisible(False)
        self.emd_bar.setFixedHeight(8)
        T.bind_css(self.emd_bar, lambda: f"QProgressBar {{ border: none; border-radius: 4px; background: {T.input.name()}; }}"
                                   f"QProgressBar::chunk {{ border-radius: 4px; background: {T.gradient_css()}; }}")
        hero.add(self.emd_bar)
        self.emd_msg = hint("", live=True)
        hero.add(self.emd_msg)
        lay.addWidget(hero)

        # -- the left column
        main = QWidget()
        left = QVBoxLayout(main)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(14)
        self.emd_about_card = Card()
        self.emd_about_card.lay.setContentsMargins(20, 16, 20, 18)
        self.emd_about_card.lay.setSpacing(12)
        self.emd_about_card.add(section_header("About", "info"))
        self.emd_about = QLabel()
        self.emd_about.setWordWrap(True)
        self.emd_about.setTextFormat(Qt.TextFormat.RichText)
        self.emd_about_card.add(self.emd_about)
        self.emd_notices = QVBoxLayout()  # callouts: who the downloads are trusted from, what a fork shares, what is unconfirmed
        self.emd_notices.setSpacing(8)
        self.emd_about_card.add(self.emd_notices)
        self.emd_also = hint("")
        self.emd_about_card.add(self.emd_also)
        left.addWidget(self.emd_about_card)
        self.emd_notes_card = Card()
        self.emd_notes_card.lay.setContentsMargins(20, 16, 20, 14)
        notes_head = section_header("Release notes", "clock")
        self.emd_notes_ver = QLabel()
        T.bind_css(self.emd_notes_ver, lambda: pill_css(T.accent_text))
        notes_head.addWidget(self.emd_notes_ver)
        self.emd_notes_card.add(notes_head)
        self.emd_notes_toggle = QPushButton()
        self.emd_notes_toggle.setCheckable(True)
        self.emd_notes_toggle.setFlat(True)
        self.emd_notes_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.emd_notes_toggle.setIconSize(QSize(14, 14))
        self.emd_notes_toggle.setStyleSheet("text-align: left; padding: 2px 0;")
        self.emd_notes_toggle.toggled.connect(self._em_toggle_notes)
        self.emd_notes_card.add(self.emd_notes_toggle)
        self.emd_notes = QTextBrowser()
        self.emd_notes.setOpenExternalLinks(True)
        self.emd_notes.setMinimumHeight(160)
        self.emd_notes.setVisible(False)
        self.emd_notes_card.add(self.emd_notes)
        left.addWidget(self.emd_notes_card)
        self.emd_cores_card = self._cores_build_card()  # shown on RetroArch's page only
        left.addWidget(self.emd_cores_card)
        self.emd_ldn_card = self._ldn_build_card()  # shown on Ryujinx's page only
        left.addWidget(self.emd_ldn_card)
        self.emd_forks_card = Card()
        self.emd_forks_card.lay.setContentsMargins(20, 16, 20, 16)
        self.emd_forks_card.lay.setSpacing(10)
        forks_head = section_header("Forks", "branch")
        self.emd_forks_count = QLabel()
        T.bind_css(self.emd_forks_count, lambda: pill_css(T.fg3))
        forks_head.addWidget(self.emd_forks_count)
        self.emd_forks_card.add(forks_head)
        self.emd_forks_card.add(hint("Community builds of this emulator, often made for particular games. Each installs into a folder of its own, "
                                     "so your normal copy is never touched."))
        self.emd_forks_box = QWidget()
        self.emd_forks_lay = QVBoxLayout(self.emd_forks_box)
        self.emd_forks_lay.setContentsMargins(0, 0, 0, 0)
        self.emd_forks_lay.setSpacing(8)
        self.emd_forks_card.add(self.emd_forks_box)
        self.emd_forks_empty = hint("No forks added yet.")
        self.emd_forks_card.add(self.emd_forks_empty)
        add_row = QHBoxLayout()
        self.emd_add_fork = QPushButton("Add fork...")
        self.emd_add_fork.setToolTip("Add someone's build of this emulator from GitHub.")
        self.emd_add_fork.clicked.connect(self._em_add_fork)
        add_row.addWidget(self.emd_add_fork)
        add_row.addStretch(1)
        self.emd_forks_card.add(add_row)
        left.addWidget(self.emd_forks_card)
        left.addStretch(1)

        # -- the side panel
        side = QWidget()
        right = QVBoxLayout(side)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(14)
        facts_card = Card()
        facts_card.lay.setContentsMargins(18, 16, 18, 16)
        facts_card.lay.setSpacing(12)
        facts_card.add(section_header("Details", "list"))
        self.emd_facts = FactList()
        facts_card.add(self.emd_facts)
        self.emd_channel_box = QWidget()
        channel = QVBoxLayout(self.emd_channel_box)
        channel.setContentsMargins(0, 0, 0, 0)
        channel.setSpacing(5)
        channel.addWidget(caps_label("Update channel"))
        self.emd_channel = QComboBox()
        self.emd_channel.addItems(["Stable", "Nightly"])
        self.emd_channel.setToolTip("Stable: the newest proper release. Nightly: the newest build of any kind.")
        self.emd_channel.currentIndexChanged.connect(self._em_channel_changed)
        channel.addWidget(self.emd_channel)
        facts_card.add(self.emd_channel_box)
        right.addWidget(facts_card)
        self.emd_links_card = Card()
        self.emd_links_card.lay.setContentsMargins(18, 16, 18, 10)
        self.emd_links_card.lay.setSpacing(10)
        self.emd_links_card.add(section_header("Links", "external"))
        self.emd_links = QLabel()
        self.emd_links.setTextFormat(Qt.TextFormat.RichText)
        self.emd_links.setWordWrap(True)
        self.emd_links.setOpenExternalLinks(False)
        self.emd_links.linkActivated.connect(lambda u: QDesktopServices.openUrl(QUrl(u)))
        self.emd_links_card.add(self.emd_links)
        right.addWidget(self.emd_links_card)
        lay.addWidget(TwoColumn(main, side))
        lay.addStretch(1)
        return view

    # ---- showing the page -----------------------------------------------------------------------------------
    def _em_on_show(self) -> None:
        """Called each time the page is opened. The first time: load the manifest, then (off the UI thread) the emurelation
        mapping and a look for emulators already on this PC."""
        self._em_refresh_counts()
        if not self._em_loaded:
            self._em_loaded = True
            root = em.emulators_root(self.em_store)
            self.em_root_edit.setText(str(root) if root else "")
            self._em_apply_catalog(em.load_catalog(), base=True)
            self._em_scan(quiet=True, with_relation=True)
            self._em_fetch_logos()
        self._em_fill()

    def _em_fetch_logos(self) -> None:
        """Once per session, in a worker thread: bring the pictures that ship with the app to one size, then download the picture of
        each emulator that has none yet (the shipped one always comes first). Each arrives as an event."""
        if self._em_logos_started:
            return
        self._em_logos_started = True
        from ..iconfinder import sgdb_key
        key = sgdb_key()  # with a SteamGridDB key its community icons are used; without one the project owner's picture is
        catalog = list(self._em_catalog)

        def work() -> None:
            try:
                normalize_icons()
            except Exception:  # tidying the folder must never stop the pictures arriving
                pass
            for e in catalog:
                if em.logo_settled(e, key):
                    continue
                before = em.logo_file(e).stat().st_mtime if em.logo_file(e).is_file() else None
                path = em.fetch_logo_best(e, key)
                if path is not None and (before is None or path.stat().st_mtime != before):
                    self.ui_q.put(("emu_logo", e.id, str(path)))
        threading.Thread(target=work, daemon=True).start()

    def _em_refresh_counts(self) -> None:
        st = self.org.get("report")
        entries = [e for f in (st.findings if st else []) for e in f.data]
        self._em_counts = em.library_counts(entries)

    def _em_reload_forks(self) -> None:
        self._em_forks = em.load_forks(self._em_catalog)
        self._em_by_id = {e.id: e for e in (*self._em_catalog, *self._em_forks)}

    def _em_apply_catalog(self, catalog: list[em.Emulator], base: bool = False) -> None:
        if base:
            self._em_base = catalog
        self._em_catalog = catalog
        self._em_reload_forks()
        self._em_fill()
        if self._em_current in self._em_by_id and self.em_views.currentIndex() == 1:
            self._em_refresh_detail()

    # ---- the cards --------------------------------------------------------------------------------------------
    def _em_row(self, e: em.Emulator) -> dict:
        inst = self.em_store.installed(e.id)
        latest = self._em_latest.get(e.id)
        code, text = em.status_text(e, inst, latest)
        games = em.games_for(e, self._em_counts)
        return {"inst": inst, "code": code, "text": text, "games": games,
                "version": (inst.version or "unknown") if inst else ""}

    def _em_visible(self) -> list[em.Emulator]:
        words = self.em_search.text().lower().split()
        mode = self.em_filter.currentIndex()
        out = []
        for e in self._em_catalog:
            row = self._em_row(e)
            forks = em.forks_of(self._em_forks, e.id)
            frows = [self._em_row(f) for f in forks]
            installed = row["inst"] is not None or any(r["inst"] is not None for r in frows)
            update = row["code"] == "update" or any(r["code"] == "update" for r in frows)
            if mode == 1 and not installed:
                continue
            if mode == 2 and not update:
                continue
            if mode == 3 and (installed or row["code"] == "unavailable"):
                continue
            if mode == 4 and not row["games"]:
                continue
            blob = f"{e.name} {e.system} {e.group} {' '.join(e.systems)} {' '.join(e.aliases)} " \
                   + " ".join(f"{f.name} {f.description} {f.repo}" for f in forks)
            if all(w in blob.lower() for w in words):
                out.append(e)
        return out

    def _em_fill(self, *_a) -> None:
        """Show the emulators that pass the search and filter. When they are the ones already on the page, the cards are only
        updated in place (an update check finishing must not rebuild everything under the person's mouse)."""
        visible = self._em_visible()
        ids = [e.id for e in visible]
        if ids == self._em_shown and self.em_view_cards.isChecked() == self._em_built_cards and all(i in self.em_cards for i in ids):
            for e in visible:
                self._em_apply_card(e)
        else:
            self._em_rebuild(visible)
        self._em_refresh_status()

    def _em_rebuild(self, visible: list[em.Emulator]) -> None:
        lay = self.em_sections_lay
        while lay.count():
            item = lay.takeAt(0)
            if (w := item.widget()) is not None:
                w.setParent(None)
                w.deleteLater()
        self.em_cards = {}
        self._em_shown = [e.id for e in visible]
        as_cards = self._em_built_cards = self.em_view_cards.isChecked()
        if not visible:
            empty = QLabel("No emulators match.")
            empty.setObjectName("muted")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(120)
            lay.addWidget(empty)
            return
        groups: dict[str, list[em.Emulator]] = {}
        for e in visible:
            groups.setdefault(e.group, []).append(e)
        for group, items in groups.items():
            colour = group_colour(group)
            head = group_chip(group, len(items))
            grid = CardGrid() if as_cards else CardGrid(min_card=250, gap=8, max_cols=5)
            cards = []
            for e in items:
                card = (EmulatorCard if as_cards else EmulatorTile)(e.id, e.name, e.system, colour)
                if (cached := em.icon_path(e)) is not None:
                    card.set_logo(str(cached))
                card.clicked.connect(self._em_show_detail)
                card.action.connect(self._em_card_action)
                self.em_cards[e.id] = card
                self._em_apply_card(e)
                cards.append(card)
            grid.set_cards(cards)
            box = QWidget()
            col = QVBoxLayout(box)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(8)
            col.addWidget(head, 0, Qt.AlignmentFlag.AlignLeft)  # a marker the width of its text, not a bar
            col.addWidget(grid)
            lay.addWidget(box)

    def _em_apply_card(self, e: em.Emulator) -> None:
        card = self.em_cards.get(e.id)
        if card is None:
            return
        row = self._em_row(e)
        forks = em.forks_of(self._em_forks, e.id)
        have = sum(1 for f in forks if self.em_store.installed(f.id))
        busy = e.id in self._em_busy
        facts = [row["version"]] if row["inst"] else []
        if row["games"]:
            facts.append(f"{row['games']:,} games")
        if forks:
            facts.append(f"{len(forks)} fork{'s' if len(forks) != 1 else ''}" + (f", {have} installed" if have else ""))
        code, inst = row["code"], row["inst"]
        card.apply(text=self._em_prog.get(e.id, "Working...") if busy else row["text"], colour=self._em_colour(code),
                   facts="  ·  ".join(f for f in facts if f), can_launch=inst is not None and code != "gone",
                   can_update=code == "update" and em.can_update(inst) and isinstance(self._em_latest.get(e.id), tuple), busy=busy, tip=e.notes)

    def _em_colour(self, code: str):
        return {"update": T.warn, "current": T.ok, "unknown": T.fg3, "gone": T.bad, "unavailable": T.fg3, "missing": T.fg3}.get(code, T.fg3)

    def _em_tile_clicked(self, key: str) -> None:
        mode = next(m for k, _c, m in TILES if k == key)
        self.em_filter.setCurrentIndex(0 if self.em_filter.currentIndex() == mode else mode)

    def _em_card_action(self, rid: str, kind: str) -> None:
        if kind == "launch":
            self._em_launch_id(rid)
        elif (e := self._em_by_id.get(rid)) is not None:
            self._em_begin_install(e)

    def _em_everything(self) -> list[em.Emulator]:
        return [*self._em_catalog, *self._em_forks]

    def _em_updatable(self) -> list[em.Emulator]:
        out = []
        for e in self._em_everything():
            inst = self.em_store.installed(e.id)
            if em.can_update(inst) and em.auto_update_ok(e) and e.id not in self._em_busy and em.update_state(e, inst, self._em_latest.get(e.id)) == "update" \
                    and isinstance(self._em_latest.get(e.id), tuple):
                out.append(e)
        return out

    def _em_refresh_status(self, shown: int | None = None) -> None:
        have = sum(1 for e in self._em_everything() if self.em_store.installed(e.id))
        ups = len(self._em_updatable())
        self.em_tiles["installed"].set_value(have)
        self.em_tiles["updates"].set_value(ups, hot=ups > 0)
        self.em_tiles["library"].set_value(sum(1 for e in self._em_catalog if em.games_for(e, self._em_counts)))
        if self._em_counts:
            need = em.missing_for_library(self._em_catalog, self.em_store, self._em_counts)[:4]
            self.em_need.setText("Your library has games for systems with no emulator yet: " + ", ".join(f"{e.name} ({n:,})" for e, n in need) if need else "")
        else:
            self.em_need.setText("Run the Library Report to see which emulators your games need.")
        self.em_need.setVisible(bool(self.em_need.text()))
        self.em_all_btn.setText(f"Update all ({ups})")  # before it opens, so it opens to its size
        motion.set_visible(self.em_all_btn, ups > 0, axis="width")
        self.em_all_btn.setEnabled(not self._em_busy)

    def _em_back(self) -> None:
        cur = self._em_by_id.get(self._em_current)
        if cur is not None and cur.parent in self._em_by_id:  # a fork goes back to the emulator it belongs to
            self._em_show_detail(cur.parent)
            return
        rid = self._em_current
        self._em_cancel_flight()
        leaving = None  # the page's picture and where it is, for it to glide back to its tile
        if rid and self._em_can_fly() and self.em_views.currentIndex() == 1:
            view = self.settings_scroll.viewport()
            box = flight.box_of(self.emd_logo, view)
            if view.rect().intersects(box):
                leaving = (flight.picture_of(self.emd_logo), box)
        self._em_current = ""
        self.em_views.setCurrentIndex(0)
        self._em_fill()
        self._em_scroll_to(self._em_list_scroll)  # back to where the list was
        if leaving is not None:
            self._em_fly_to_tile(rid, *leaving)

    def _em_refresh_views(self) -> None:
        if self.em_views.currentIndex() == 1:
            self._em_refresh_detail()
        else:
            self._em_fill()

    def _em_scroll_to(self, value: int) -> None:
        """Move the page's scroll bar, now and once more after the new view has been laid out (its height changes first)."""
        bar = self.settings_scroll.verticalScrollBar()
        bar.setValue(value)
        QTimer.singleShot(0, lambda: bar.setValue(value))

    # ---- the icon's flight between the list and a page --------------------------------------------------------
    def _em_can_fly(self) -> bool:
        return motion.on() and self.em_views.isVisible() and self.em_views.window().isVisible()

    def _em_logo_of(self, rid: str):
        """The picture (or monogram) on this emulator's tile or card in the list, when there is one."""
        card = self.em_cards.get(rid)
        logo = getattr(card, "mono", None) or getattr(card, "tile", None)
        return logo if logo is not None and logo.isVisibleTo(self.em_views) else None

    def _em_cancel_flight(self) -> None:
        self._em_flight_id += 1
        if self._em_flight is not None:
            self._em_flight.finish()  # puts back whatever it was holding hidden
            self._em_flight = None

    def _em_fly_to_page(self, start: QRect) -> None:
        """The tile's picture glides to its place at the top of the page while the page fades in; the page's own copy stays hidden until it lands."""
        view = self.settings_scroll.viewport()
        logo = self.emd_logo
        picture = flight.picture_of(logo)
        motion.reveal(self.em_views.widget(1))
        flight.hold(logo)
        self._em_flight = flight.Flight(view, picture, start, lambda: flight.box_of(logo, view), done=lambda: flight.release(logo)).go()

    def _em_fly_to_tile(self, rid: str, picture, start: QRect) -> None:
        """The page's picture glides back down to the emulator's tile while the list fades in. The tile is looked up on every frame (the list may
        be rebuilt or still settling) and is kept hidden until the picture lands on it."""
        view = self.settings_scroll.viewport()
        held: list = []

        def end() -> QRect | None:
            logo = self._em_logo_of(rid)
            if logo is None:
                return None
            if logo not in held:
                flight.hold(logo)
                held.append(logo)
            return flight.box_of(logo, view)

        def done() -> None:
            for logo in held:
                flight.release(logo)

        motion.reveal(self.em_views.widget(0))
        end()  # hide the tile's own picture now, before the first frame shows it next to the one in the air
        self._em_flight = flight.Flight(view, picture, start, end, done=done).go()

    def _em_say(self, text: str) -> None:
        (self.emd_msg if self.em_views.currentIndex() == 1 else self.em_status).setText(text)

    # ---- the page for one emulator ----------------------------------------------------------------------------
    def _em_show_detail(self, rid: str) -> None:
        e = self._em_by_id.get(rid)
        if e is None:
            return
        parent = self._em_by_id.get(e.parent) if e.parent else None
        self._em_cancel_flight()
        leaving = None  # where this emulator's picture is in the list, for it to glide up from
        if self.em_views.currentIndex() == 0:
            self._em_list_scroll = self.settings_scroll.verticalScrollBar().value()
            if self._em_can_fly() and (tile_logo := self._em_logo_of(rid)) is not None:
                box = flight.box_of(tile_logo, self.settings_scroll.viewport())
                if self.settings_scroll.viewport().rect().intersects(box):
                    leaving = box
        self._em_current = rid
        self._em_snap = True  # a different emulator: its buttons swap at once; only a change to the same one animates
        self.em_views.setCurrentIndex(1)
        self._em_scroll_to(0)
        self.emd_back.setText(f"← {parent.name}" if parent else "← All emulators")
        src = parent or e  # a fork wears its emulator's picture and colour
        colour = group_colour(src.group)
        self.emd_chip.setText((f"FORK OF {parent.name}  ·  {e.system}" if parent else f"{e.group}  ·  {e.system}").upper())
        T.bind_css(self.emd_chip, lambda: chip_css(colour))
        T.bind_css(self.emd_hero, lambda: f"QFrame#card {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {tint(colour, 36)}, stop:0.6 {T.card.name()}, "
            f"stop:1 {T.card.name()}); border: 1px solid {T.card_border.name()}; border-radius: 14px; }}")
        self._em_detail_logo(e)
        self.emd_title.setText(e.name)
        facts = []
        if e.repo:
            facts.append(f"by {e.repo.split('/')[0]}")
        facts.append({"github": "releases on GitHub", "gitlab": "releases on GitLab", "forgejo": "releases on Forgejo"}.get(e.source, "downloads from its own site"))
        self.emd_sub.setText("  ·  ".join(facts))
        description = e.description or e.notes or f"{e.name} plays {e.system}."
        self.emd_about.setText(f'<p style="margin: 0; line-height: 155%;">{html.escape(description)}</p>')
        notices: list[tuple[str, str]] = []
        if e.portable:
            what = f"a folder named {e.portable.rstrip('/')}" if e.portable.endswith("/") else f"a file named {e.portable}"
            notices.append(("info", f"Keeps its settings beside the program when {what} sits next to it. "
                                    + ("Distill adds it when it installs this emulator; an update leaves it as it is." if e.portable_auto
                                       else "Its Windows download works that way already.")))
        if parent:
            notices.append(("ok", f"Same author as {parent.name}, which Distill already trusts.") if e.author_known else
                           ("warn", "Distill doesn't know this author: you confirmed it yourself. Downloads are only accepted from "
                                    f"{e.trusted_owner}, and it is never updated unless you press Update."))
            notices.append(("info", "It installs in a folder of its own and may share settings or saves with the original. See its notes before running both."))
        elif e.trusted_owner:
            notices.append(("ok", f"Downloads are only accepted from {e.trusted_owner}: fake copies of this project are circulating with malware in them."))
        if e.verify:
            notices.append(("warn", "This entry's download source hasn't been confirmed yet. If installing fails, use the website link."))
        clear_layout(self.emd_notices)
        for kind, text in sorted(notices, key=lambda n: ("warn", "ok", "info").index(n[0])):  # what needs care comes first
            self.emd_notices.addWidget(Callout(kind, text))
        self.emd_also.setText("Also listed for: " + ", ".join(e.also[:12]) + (" ..." if len(e.also) > 12 else "") + "  (from emurelation)" if e.also else "")
        self.emd_also.setVisible(bool(e.also))
        links = []
        if e.website:
            links.append(("Website", e.website))
        if e.repo and e.source == "github":
            links += [("GitHub", f"https://github.com/{e.repo}"), ("All releases", f"https://github.com/{e.repo}/releases"),
                      ("Report an issue", f"https://github.com/{e.repo}/issues")]
        accent = T.accent_text.name()
        self.emd_links.setText("".join(f'<p style="margin: 0 0 8px 0;"><a href="{html.escape(u, quote=True)}" style="color: {accent}; '
                                       f'text-decoration: none;">{t}&nbsp;&nbsp;↗</a></p>' for t, u in links))
        self.emd_links_card.setVisible(bool(links))
        self.emd_channel.blockSignals(True)
        self.emd_channel_box.setVisible(len(e.channels) > 1)
        self.emd_channel.setCurrentIndex(1 if em.channel_of(self.em_store, e) == "nightly" else 0)
        self.emd_channel.blockSignals(False)
        self.emd_notes.setMarkdown("Checking for the latest release...")
        self.emd_notes_toggle.setChecked(False)
        self.emd_bar.setValue(0)
        self.emd_msg.setText("")
        self.emd_forks_card.setVisible(parent is None)
        self.emd_unfork.setVisible(parent is not None)
        self._em_refresh_detail()
        for x in (e, *em.forks_of(self._em_forks, e.id)):
            if x.id not in self._em_latest:
                self._em_fetch_latest(x)
        if leaving is not None:
            self._em_fly_to_page(leaving)

    def _em_toggle_notes(self, open_: bool) -> None:
        motion.set_visible(self.emd_notes, open_, axis="height", animate=not self._em_snap)
        self._em_notes_title()

    def _em_notes_title(self) -> None:
        latest = self._em_latest.get(self._em_current)
        e = self._em_by_id.get(self._em_current)
        version = em.label_of(e, latest[0], latest[1]) if isinstance(latest, tuple) and e else ""
        self.emd_notes_ver.setText(version)
        self.emd_notes_ver.setVisible(bool(version))
        on = self.emd_notes_toggle.isChecked()
        self.emd_notes_toggle.setText("Hide release notes" if on else "Show release notes")
        self.emd_notes_toggle.setIcon(icons.icon("chevron-down" if on else "chevron-right", T.fg3, 14))

    def _em_detail_logo(self, e: em.Emulator) -> None:
        """The picture on the page: the cached one when there is one (a fork uses its emulator's), else two letters."""
        src = self._em_by_id.get(e.parent, e) if e.parent else e
        colour = group_colour(src.group)
        path = em.icon_path(src)
        if not (path is not None and self.emd_logo.show_logo(str(path), colour)):
            self.emd_logo.show_monogram(e.name, colour)

    # ---- the Emulators folder, scanning ---------------------------------------------------------------------
    def _em_choose_root(self) -> None:
        start = self.em_root_edit.text() or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation)
        path = QFileDialog.getExistingDirectory(self, "Choose your Emulators folder", start)
        if not path:
            return
        em.set_emulators_root(self.em_store, path)
        self.em_root_edit.setText(path)
        self._em_scan(quiet=False)

    def _em_scan(self, quiet: bool = False, with_relation: bool = False) -> None:
        """Look for emulators already on this PC, in a worker thread. With `with_relation` the emurelation mapping is loaded first,
        so its alternate names help the search."""
        if self._em_scanning:
            return
        self._em_scanning = True
        self.em_scan_btn.setEnabled(False)
        if not quiet:
            self.em_status.setText("Looking for emulators you already have...")
        base, store, forks = list(self._em_base), self.em_store, list(self._em_forks)

        def work() -> None:
            relation = None
            if with_relation:
                try:
                    relation = em.load_relation()
                except Exception:  # the mapping only adds to the manifest: never fatal
                    relation = None
            catalog = em.enrich(base, relation)
            try:
                found = em.scan_all(store, [*catalog, *forks])
            except Exception as exc:
                self.ui_q.put(("emu_scan", catalog if relation else None, None, quiet, f"Search failed: {exc}"))
                return
            self.ui_q.put(("emu_scan", catalog if relation else None, found, quiet, ""))
        threading.Thread(target=work, daemon=True).start()

    def _em_on_scan(self, catalog, found, quiet: bool, error: str) -> None:
        self._em_scanning = False
        self.em_scan_btn.setEnabled(True)
        if catalog:
            self._em_apply_catalog(catalog)
        if found is None:
            self.em_status.setText(error)
            return
        new = em.apply_scan(self.em_store, found)
        self._em_fill()
        if new:
            names = ", ".join(self._em_by_id[i].name for i in new if i in self._em_by_id)
            self.em_status.setText(f"Found {len(new)} emulator{'s' if len(new) != 1 else ''} you already have: {names}.")
        elif not quiet:
            self.em_status.setText("No new emulators found. Put them in your Emulators folder, or open one and press \"I already have it\".")
        self._em_check_updates(quiet=True)
        if self._em_current:
            self._em_refresh_detail()

    def _em_refresh_detail(self) -> None:
        e = self._em_by_id.get(self._em_current)
        if e is None:
            return
        inst = self.em_store.installed(e.id)
        latest = self._em_latest.get(e.id)
        busy = e.id in self._em_busy
        ok, warn, bad, mute = (c.name() for c in (T.ok, T.warn, T.bad, T.fg3))
        code, _text = em.status_text(e, inst, latest)
        line = ""
        if inst is None:
            if not em.downloadable(e):
                why = _text if e.notes and not e.description else e.notes or "There is nothing to download for this PC yet."
                line = f'<span style="color:{mute}">{html.escape(why)}</span>'  # (a downloadable one just says "Not installed" in the pill)
        elif code == "gone":
            line = f'<span style="color:{bad}">Its files are missing from {html.escape(inst.folder)}.</span> Reinstall it, or point Distill at the copy you have.'
        else:
            where = {"": "Installed by Distill", "folder": "Your own install, linked", "file": "Your own install, linked (shares a folder)"}[inst.linked]
            colour = warn if code == "update" else ok if code == "current" else mute
            tail = {"update": " An update is available.", "current": " Up to date.", "unknown": " Its version couldn't be compared."}.get(code, "")
            line = f'<span style="color:{colour}">{where}.{tail}</span>'
        self.emd_state.setText(line)
        self.emd_state.setVisible(bool(line))
        self.emd_status.setText("Working..." if busy else _text)
        T.bind_css(self.emd_status, lambda: pill_css(T.accent_text if busy else self._em_colour(code)))
        state = em.update_state(e, inst, latest)
        rows: list[tuple[str, str, object]] = []
        if inst:
            rows.append(("Installed", inst.version or "unknown version", None))
        if isinstance(latest, tuple):
            rel, asset = latest
            rows.append(("Latest release", em.label_of(e, rel, asset), T.warn if state == "update" else None))
            rows.append(("Released", rel.published, None))
            rows.append(("Download", asset["name"] + (f" ({rc.fmt_size(asset['size'])})" if asset.get("size") else ""), None))
            self.emd_notes.setMarkdown(rel.body or "_No release notes._")
        elif isinstance(latest, str):
            rows.append(("Latest release", latest, None))
            self.emd_notes.setMarkdown("_Release notes unavailable._")
        elif em.downloadable(e):
            rows.append(("Latest release", "Checking...", None))
        if inst:
            rows.append(("Location", break_path(inst.folder), None))
        if games := em.games_for(e, self._em_counts):
            rows.append(("In your library", f"{games:,} games", None))
        self.emd_facts.set_facts(rows)
        can_get = isinstance(latest, tuple) and (inst is None or em.can_update(inst))  # nothing installed yet: always installable
        self.emd_main.setText("Cancel" if busy else "Update" if state == "update" else "Install" if inst is None else
                              "Update to latest" if state == "unknown" and can_get else "Reinstall")
        self.emd_main.setEnabled(busy or can_get)
        self.emd_main.setToolTip("Distill won't update an install that shares its folder with other programs. Move it to a folder of its own and link it again."
                                 if inst and inst.linked == "file" else "")
        animate, self._em_snap = not self._em_snap, False  # buttons come and go smoothly when this emulator's state changes

        def show(widget, on: bool, axis: str = "width") -> None:
            motion.set_visible(widget, on, axis=axis, animate=animate)

        self.emd_remove.setText("Unlink" if inst and inst.linked else "Uninstall")  # before it opens, so it opens to its size
        show(self.emd_launch, inst is not None)
        self.emd_launch.setEnabled(inst is not None and not busy and code != "gone")
        show(self.emd_folder, inst is not None)
        show(self.emd_locate, inst is None or bool(inst.linked) or code == "gone")
        self.emd_locate.setEnabled(not busy)
        show(self.emd_remove, inst is not None)
        self.emd_remove.setEnabled(not busy)
        show(self.emd_bar, busy, "height")
        self._em_notes_title()
        self._em_refresh_forks(e)
        self._cores_sync()
        self._ldn_sync()

    # ---- latest release / update check ------------------------------------------------------------------------
    def _em_fetch_latest(self, e: em.Emulator, force: bool = False) -> None:
        store = self.em_store

        def work() -> None:
            self.ui_q.put(("emu_latest", e.id, em.latest_for(e, store, force)))
        threading.Thread(target=work, daemon=True).start()

    def _em_check_updates(self, quiet: bool = False) -> None:
        """Ask each installed emulator's source for its newest release, one after another in one worker thread."""
        todo = [e for e in self._em_everything() if self.em_store.installed(e.id) and em.downloadable(e)]
        cur = self._em_by_id.get(self._em_current)
        if cur is not None and cur not in todo:
            todo.append(cur)
        if not todo:
            if not quiet:
                self.em_status.setText("Nothing installed yet, so nothing to update.")
            return
        if not quiet:
            self.em_status.setText(f"Checking {len(todo)} emulator(s) for updates...")
        store = self.em_store

        def work() -> None:
            for e in todo:
                self.ui_q.put(("emu_latest", e.id, em.latest_for(e, store, force=not quiet)))
            if not quiet:
                self.ui_q.put(("emu_note", "Update check finished."))
        threading.Thread(target=work, daemon=True).start()

    def _em_channel_changed(self, index: int) -> None:
        e = self._em_by_id.get(self._em_current)
        if e is None:
            return
        em.set_channel(self.em_store, e, "nightly" if index == 1 else "stable")
        self._em_latest.pop(e.id, None)
        self.emd_msg.setText("Looking for the newest build on that channel...")
        self._em_refresh_detail()
        self._em_fetch_latest(e)

    # ---- install / update / launch / link ---------------------------------------------------------------------
    def _em_start_install(self, e: em.Emulator, rel: rc.Release, asset: dict, cancel: threading.Event) -> None:
        """Runs in a worker thread: one install or update, reported through the event queue."""
        store = self.em_store

        def progress(text: str, done: int, total: int) -> None:
            self.ui_q.put(("emu_prog", e.id, text, done, total))
        try:
            inst = em.EmulatorInstaller(store, progress, cancel).install(e, rel, asset)
            self.ui_q.put(("emu_done", e.id, inst, ""))
        except rc.Cancelled:
            self.ui_q.put(("emu_done", e.id, None, "Cancelled."))
        except rc.RecompError as exc:
            self.ui_q.put(("emu_done", e.id, None, str(exc)))
        except Exception as exc:  # whatever else goes wrong is shown, not lost in a thread
            self.ui_q.put(("emu_done", e.id, None, f"{type(exc).__name__}: {exc}"))

    def _em_main_action(self) -> None:
        e = self._em_by_id.get(self._em_current)
        if e is not None:
            self._em_begin_install(e)

    def _em_begin_install(self, e: em.Emulator) -> None:
        """Install, update or reinstall `e` (or cancel it when it is already running): from its own page, its card or its row."""
        if e.id in self._em_busy:
            self._em_busy[e.id].set()
            self._em_say("Cancelling...")
            return
        latest = self._em_latest.get(e.id)
        if not isinstance(latest, tuple):
            return
        inst = self.em_store.installed(e.id)
        if why := em.update_blocked(e, inst):  # a protected folder (Program Files): nothing is started, so nothing can hang
            QMessageBox.information(self, "Can't update here", why)
            return
        if inst is None and em.emulators_root(self.em_store) is None:
            answer = QMessageBox.question(
                self, "Where should it go?",
                "You haven't chosen an Emulators folder.\n\nChoose one now to keep all your emulators together (and have Distill find the ones "
                "you already have there)? Otherwise it is installed inside Distill's own folder.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Yes:
                self._em_choose_root()
                if em.emulators_root(self.em_store) is None:
                    return
        if inst is not None and inst.linked and not self._p_data.get("skip_own_install_confirm"):
            box = QMessageBox(QMessageBox.Icon.Question, "Update your own install?",
                              f"{e.name} is your own install in:\n{inst.folder}\n\nThe new files are copied over it. Your saves, settings and "
                              "anything else in that folder are kept.\n\nContinue?",
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            again = QCheckBox("Do not show this again")
            box.setCheckBox(again)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return  # a No is never remembered: only a Yes with the box ticked skips the question next time
            if again.isChecked():
                self._p_data["skip_own_install_confirm"] = True
                self._settings_save()
        rel, asset = latest
        cancel = threading.Event()
        self._em_busy[e.id] = cancel
        self._em_prog[e.id] = "Starting..."
        if e.id == self._em_current:
            self.emd_bar.setRange(0, 0)
            self.emd_msg.setText("Starting...")
        self._em_refresh_views()
        threading.Thread(target=self._em_start_install, args=(e, rel, asset, cancel), daemon=True).start()

    def _em_update_all(self) -> None:
        todo = self._em_updatable()
        if not todo or self._em_busy:
            return
        protected = [e for e in todo if em.update_blocked(e, self.em_store.installed(e.id))]
        todo = [e for e in todo if e not in protected]
        left_out = ""
        if protected:
            left_out = (f"\n\nNot updated, because they are in protected folders: {', '.join(e.name for e in protected)}. Move them to a folder "
                        "you can write to and link them again to update them.")
        if not todo:
            QMessageBox.information(self, "Can't update here", left_out.strip())
            return
        names = ", ".join(e.name for e in todo)
        if QMessageBox.question(self, "Update all", f"Update {len(todo)} emulator{'s' if len(todo) != 1 else ''}?\n\n{names}\n\n"
                                f"Each is updated where it already is. Saves and settings are kept.{left_out}") != QMessageBox.StandardButton.Yes:
            return
        cancel = threading.Event()
        jobs = [(e, *self._em_latest[e.id]) for e in todo]
        for e, _r, _a in jobs:
            self._em_busy[e.id] = cancel
        self._em_refresh_status()
        self.em_status.setText(f"Updating {len(jobs)} emulator(s)...")

        def work() -> None:
            for e, rel, asset in jobs:
                if cancel.is_set():
                    self.ui_q.put(("emu_done", e.id, None, "Cancelled."))
                    continue
                self._em_start_install(e, rel, asset, cancel)
            self.ui_q.put(("emu_note", "Updates finished."))
        threading.Thread(target=work, daemon=True).start()

    def _em_launch(self) -> None:
        self._em_launch_id(self._em_current)

    def _em_launch_id(self, rid: str) -> None:
        inst = self.em_store.installed(rid)
        if inst is None:
            return
        try:
            rc.launch(inst)
            self._em_say("Started.")
        except rc.RecompError as exc:
            self._em_say(str(exc))

    def _em_open_folder(self) -> None:
        if inst := self.em_store.installed(self._em_current):
            QDesktopServices.openUrl(QUrl.fromLocalFile(inst.folder))

    def _em_locate_folder(self) -> None:
        e = self._em_by_id.get(self._em_current)
        if e is None:
            return
        start = self.em_root_edit.text() or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation)
        path = QFileDialog.getExistingDirectory(self, f"Choose the folder {e.name} is in", start)
        if path:
            self._em_link(e, Path(path))

    def _em_locate_file(self) -> None:
        e = self._em_by_id.get(self._em_current)
        if e is None:
            return
        start = self.em_root_edit.text() or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation)
        path, _f = QFileDialog.getOpenFileName(self, f"Choose {e.name}'s program file", start)
        if path:
            self._em_link(e, Path(path))

    def _em_link(self, e: em.Emulator, path: Path) -> None:
        try:
            found = em.link(self.em_store, e, path)
        except rc.RecompError as exc:
            self.emd_msg.setText(str(exc))
            return
        shared = " It shares a folder with other programs, so Distill will launch it but won't update it." if found.linked == "file" else ""
        self.emd_msg.setText(f"Linked {e.name}" + (f" {found.version}" if found.version else "") + f".{shared}{self._em_released_note(found)}")
        self._em_refresh_detail()

    def _em_released_note(self, found: em.Found) -> str:
        """Said after a link that took a program away from another entry (a fork's folder a scan had given to its parent)."""
        names = [self._em_by_id[i].name if i in self._em_by_id else i for i in found.released]
        return f" {' and '.join(names)} was pointing at it and no longer is." if names else ""
        self._em_fetch_latest(e)

    def _em_remove(self) -> None:
        e = self._em_by_id.get(self._em_current)
        inst = self.em_store.installed(self._em_current) if e else None
        if e is None or inst is None or e.id in self._em_busy:
            return
        if inst.linked:
            ask = f"Stop managing {e.name}?\n\nIt is your own install, so nothing is deleted: Distill just forgets it."
        else:
            ask = (f"Remove {e.name} from this PC?\n\nIts folder is deleted, including any saves or settings it keeps there "
                   "(unless you moved them elsewhere). Your own game files are not touched.")
        if QMessageBox.question(self, "Unlink" if inst.linked else "Uninstall", ask) != QMessageBox.StandardButton.Yes:
            return
        self.emd_msg.setText(em.uninstall(self.em_store, e))
        self._em_refresh_detail()

    # ---- forks ------------------------------------------------------------------------------------------------
    def _em_refresh_forks(self, e: em.Emulator) -> None:
        """The fork rows on an emulator's page: rebuilt when the set of forks changes, otherwise only updated in place."""
        if e.parent:
            return
        forks = em.forks_of(self._em_forks, e.id)
        self.emd_forks_count.setText(str(len(forks)))
        self.emd_forks_count.setVisible(bool(forks))
        if [f.id for f in forks] != list(self._em_fork_rows):
            lay = self.emd_forks_lay
            while lay.count():
                item = lay.takeAt(0)
                if (w := item.widget()) is not None:
                    w.setParent(None)
                    w.deleteLater()
            self._em_fork_rows = {}
            for f in forks:
                trust = "same author as the emulator" if f.author_known else "unverified author: you accepted the risk"
                row = ForkRow(f.id, f.name, f.trusted_owner, f.description, trust)
                row.action.connect(self._em_fork_action)
                self._em_fork_rows[f.id] = row
                lay.addWidget(row)
        for f in forks:
            self._em_apply_fork_row(f)
        self.emd_forks_empty.setVisible(not forks)

    def _em_apply_fork_row(self, f: em.Emulator) -> None:
        row = self._em_fork_rows.get(f.id)
        if row is None:
            return
        info = self._em_row(f)
        inst, code, latest = info["inst"], info["code"], self._em_latest.get(f.id)
        text, tip = info["text"], ""
        if f.id in self._em_busy:
            show = {"cancel", "details"}
            text = self._em_prog.get(f.id, "Working...")
            enabled = set(show)
        elif inst is not None and code != "gone":
            show = {"launch", "details"} | ({"update"} if code == "update" and em.can_update(inst) else set())
            enabled = set(show)
        elif em.downloadable(f):
            show = {"install", "details"}
            enabled = {"details"} | ({"install"} if isinstance(latest, tuple) else set())
            if isinstance(latest, str):
                text, tip = "No download", latest
        else:
            show = enabled = {"details"}
            tip = f.notes
        row.apply(text=text, colour=self._em_colour(code), show=show, enabled=enabled, tip=tip)

    def _em_fork_action(self, rid: str, kind: str) -> None:
        f = self._em_by_id.get(rid)
        if f is None:
            return
        if kind == "details":
            self._em_show_detail(rid)
        elif kind == "launch":
            self._em_launch_id(rid)
        elif kind == "cancel":
            if rid in self._em_busy:
                self._em_busy[rid].set()
        else:
            self._em_begin_install(f)

    def _em_add_fork(self) -> None:
        e = self._em_by_id.get(self._em_current)
        if e is None or e.parent:
            return
        dlg = AddForkDialog(self, e, self.em_store, self._em_catalog)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        try:
            fork = em.add_fork(e, v["repo"], v["name"], v["description"], v["accepted"], self._em_catalog)
        except rc.RecompError as exc:
            self.emd_msg.setText(str(exc))
            return
        self._em_reload_forks()
        fork = self._em_by_id.get(fork.id, fork)  # the entry as loaded, so a link below is recorded against it
        note = ""
        if v.get("folder"):
            try:
                found = em.link(self.em_store, fork, Path(v["folder"]))
                note = f" Linked to the copy you already have.{self._em_released_note(found)}"
            except rc.RecompError as exc:
                note = f" Not linked: {exc}"
        self._em_refresh_views()
        self._em_refresh_detail()
        self.emd_msg.setText(f"Added {fork.name}.{note}")
        self._em_fetch_latest(fork)

    def _em_remove_fork(self) -> None:
        f = self._em_by_id.get(self._em_current)
        if f is None or not f.parent:
            return
        if self.em_store.installed(f.id):
            QMessageBox.information(self, "Remove fork", f"{f.name} is installed. Uninstall (or unlink) it first, then remove it from the list.")
            return
        if QMessageBox.question(self, "Remove fork", f"Take {f.name} off the list?") != QMessageBox.StandardButton.Yes:
            return
        parent = f.parent
        em.remove_fork(f)
        self._em_latest.pop(f.id, None)
        self._em_reload_forks()
        self._em_show_detail(parent)
        self.emd_msg.setText(f"Removed {f.name}.")

    # ---- events from the worker threads -----------------------------------------------------------------------
    def _em_on_parent_page(self, rid: str) -> bool:
        """Is this a fork of the emulator whose page is open?"""
        e = self._em_by_id.get(rid)
        return bool(e and e.parent and e.parent == self._em_current and self.em_views.currentIndex() == 1)

    def _em_event(self, ev: tuple) -> None:
        name = ev[0]
        if name == "emu_latest":
            self._em_latest[ev[1]] = ev[2]
            if ev[1] == self._em_current or self._em_on_parent_page(ev[1]):
                self._em_refresh_detail()
            if self.em_views.currentIndex() == 0:
                self._em_fill()
            else:
                self._em_refresh_status()
        elif name == "emu_prog":
            _n, rid, text, done, total = ev
            self._em_prog[rid] = text
            if rid == self._em_current and self.em_views.currentIndex() == 1:
                self.emd_msg.setText(text)
                self.emd_bar.setRange(0, total if total else 0)
                self.emd_bar.setValue(done if total else 0)
            elif self._em_on_parent_page(rid) and rid in self._em_fork_rows:
                self._em_fork_rows[rid].set_busy_text(text)
            elif self.em_views.currentIndex() == 0 and rid in self.em_cards:  # an install running with the cards showing
                self.em_cards[rid].set_busy_text(text)
        elif name == "emu_done":
            _n, rid, inst, error = ev
            self._em_busy.pop(rid, None)
            self._em_prog.pop(rid, None)
            if rid == self._em_current and self.em_views.currentIndex() == 1:
                self.emd_msg.setText(error or f"Installed {inst.version}. Press Launch to start it.")
            elif error and self.em_views.currentIndex() == 1:
                self.emd_msg.setText(f"{self._em_by_id[rid].name}: {error}" if rid in self._em_by_id else error)
            self._em_refresh_views()
            self._em_refresh_status()
        elif name == "emu_logo":
            if ev[1] in self.em_cards:
                self.em_cards[ev[1]].set_logo(ev[2])
            cur = self._em_by_id.get(self._em_current)
            if cur is not None and self.em_views.currentIndex() == 1 and (cur.parent or cur.id) == ev[1]:
                self._em_detail_logo(cur)
        elif name == "emu_scan":
            self._em_on_scan(ev[1], ev[2], ev[3], ev[4])
        elif name == "emu_note":
            if self.em_views.currentIndex() == 0:
                self._em_fill()
            self.em_status.setText(ev[1])
        elif name.startswith("emu_cores_"):
            self._cores_event(ev)
        elif name == "emu_ldn":
            self._ldn_event(ev)
