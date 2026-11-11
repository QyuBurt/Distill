"""The Edit game window: opened from the pencil on a game's poster. It is laid out like the Add recomp window (the poster with its search / upload /
remove buttons on the left, the details on the right). Typing a game name and leaving the box (or pressing Look up) searches the LaunchBox Games
Database; the best entry fills in the platform, developer, publisher, year and genre where they are still empty, and every entry found is listed so
the right one (a game often exists on several platforms) can be picked, which fills in all of them. Nothing is saved until Save is pressed."""
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import apikeys
from .. import gameexport, gameimport
from .. import games as gm
from .. import gamemedia
from .. import recomps as rc
from . import msgbox
from .emu_cards import clear_layout
from .frameless import FramelessDialog, hairline
from .games_widgets import StarRating
from .recomp_dialog import EMPTY_NOTE, REMOVED_NOTE, CoverSearchDialog, PosterBox, _caption, _icon_button, _stack, pick_picture
from .theme import T
from .widgets import AnimCheckBox, accent_button, hint


class EditGameDialog(FramelessDialog):
    _found = Signal(int, object, str)  # (lookup number, [gm.Match] or None, what went wrong), sent from the worker thread

    def __init__(self, parent: QWidget, game: gm.Game, systems: list[str], sgdb_key: str = "", cover: QPixmap | None = None):
        super().__init__(parent, "Edit game", "edit", (820, 760), (700, 560))
        self.game, self.sgdb_key = game, sgdb_key
        self.values: dict[str, str | int | None] | None = None  # what Save was pressed with (see gm.GameStore.update_details)
        self.cover_data: bytes | None = None  # a cover chosen here, to be kept when Save is pressed
        self.cover_off = False  # the cover that is kept on disk is to be taken off
        self._cover_on_disk = gm.cover_path(game) is not None
        self._alive = True
        self._serial = 0  # the lookup whose answer is wanted; older ones are ignored
        self._looked_for = game.name  # the name last searched (or opened with): leaving the box unchanged doesn't search again
        self._matches: list[gm.Match] = []
        self._touched: set[str] = set()  # fields the person typed into: a lookup never overwrites them
        self.setWindowTitle(f"Edit {game.name}")
        outer = self.content

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        body_host = QWidget()
        body_host.setObjectName("gameBody")
        body_host.setStyleSheet("#gameBody { background: transparent; }")
        scroll.setWidget(body_host)
        outer.addWidget(scroll, 1)
        top = QHBoxLayout(body_host)
        top.setContentsMargins(20, 18, 20, 18)
        top.setSpacing(24)

        left = QVBoxLayout()
        left.setSpacing(10)
        self.poster = PosterBox()
        self.poster.set_pixmap(cover)
        left.addWidget(self.poster, 0, Qt.AlignmentFlag.AlignHCenter)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        tools.addStretch(1)
        self.search_btn = _icon_button("search", "Find a cover: the LaunchBox Games Database or SteamGridDB")
        self.upload_btn = _icon_button("upload", "Upload a cover from this computer")
        self.delete_btn = _icon_button("trash", "Remove the cover", danger=True)
        self.search_btn.clicked.connect(self._cover_menu)
        self.upload_btn.clicked.connect(self._upload_cover)
        self.delete_btn.clicked.connect(self._remove_cover)
        for b in (self.search_btn, self.upload_btn, self.delete_btn):
            tools.addWidget(b)
        tools.addStretch(1)
        left.addLayout(tools)
        left.addStretch(1)
        top.addLayout(left)

        right = QVBoxLayout()
        right.setSpacing(12)
        top.addLayout(right, 1)
        self.name = QLineEdit(game.name)
        self.name.setPlaceholderText("The game's name, e.g. Super Metroid")
        self.name.setClearButtonEnabled(True)
        self.name.editingFinished.connect(self._name_done)
        self.look_btn = accent_button("Look up")
        self.look_btn.setAutoDefault(False)
        self.look_btn.setToolTip("Search the LaunchBox Games Database for this name. It fills in the platform, developer, publisher, year and genre.")
        self.look_btn.clicked.connect(lambda: self._look(force=True))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.name, 1)
        row.addWidget(self.look_btn)
        holder = QWidget()
        holder.setLayout(row)
        right.addLayout(_stack("Game name", holder))
        self.status = hint("", live=True)
        right.addWidget(self.status)
        self.results = QListWidget()
        self.results.setFixedHeight(122)
        self.results.setVisible(False)
        self.results.setFrameShape(QFrame.Shape.NoFrame)
        T.bind_css(self.results, lambda: (
            f"QListWidget {{ background: {T.input.name()}; border: 1px solid {T.card_border.name()}; border-radius: 10px; outline: 0; padding: 4px; }}"
            f"QListWidget::item {{ padding: 5px 8px; border-radius: 6px; color: {T.fg2.name()}; }}"
            f"QListWidget::item:hover {{ background: {T.hover.name()}; }}"
            f"QListWidget::item:selected {{ background: {T.select.name()}; color: {T.title.name()}; }}"))
        self.results.itemClicked.connect(self._pick)
        right.addWidget(self.results)

        self.system = QComboBox()
        self.system.setEditable(True)
        self.system.addItems(systems)
        self.system.setCurrentText("" if game.system == gm.UNKNOWN else game.system)
        self.system.lineEdit().setPlaceholderText("e.g. Super Nintendo")
        self.system.lineEdit().textEdited.connect(lambda _t: self._touched.add("system"))
        self.system.activated.connect(lambda _i: self._touched.add("system"))
        right.addLayout(_stack("Platform", self.system))
        self.developer = self._field("developer", game.developer, "e.g. Nintendo R&D1")
        self.publisher = self._field("publisher", game.publisher, "e.g. Nintendo")
        self.year = self._field("year", game.year, "e.g. 1994")
        self.genre = self._field("genre", game.genre, "e.g. Action, Platform")
        pair = QHBoxLayout()
        pair.setSpacing(12)
        pair.addLayout(_stack("Developer", self.developer), 1)
        pair.addLayout(_stack("Publisher", self.publisher), 1)
        right.addLayout(pair)
        pair2 = QHBoxLayout()
        pair2.setSpacing(12)
        pair2.addLayout(_stack("Year released", self.year), 1)
        pair2.addLayout(_stack("Genre", self.genre), 2)
        right.addLayout(pair2)
        self.stars = StarRating(game.rating)
        self.status_box = QComboBox()
        self.status_box.addItem("No status", "")
        for key, label in gm.STATUSES.items():
            self.status_box.addItem(label, key)
        self.status_box.setCurrentIndex(max(0, self.status_box.findData(game.status)))
        mine = QHBoxLayout()
        mine.setSpacing(12)
        mine.addLayout(_stack("Your rating", self.stars), 1)
        mine.addLayout(_stack("Status", self.status_box), 1)
        right.addLayout(mine)
        self.notes = QPlainTextEdit(game.notes)
        self.notes.setPlaceholderText("Your own notes: where you got to, tips, what to try next")
        self.notes.setFixedHeight(78)
        right.addLayout(_stack("Notes", self.notes))
        right.addWidget(hint(f"File: {game.path}"))
        right.addStretch(1)

        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setTextFormat(Qt.TextFormat.RichText)
        self.problem.setContentsMargins(20, 8, 20, 0)
        self.problem.setVisible(False)
        outer.addWidget(self.problem)
        outer.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        foot.setSpacing(8)
        foot.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        self.save_btn = accent_button("Save")
        self.save_btn.setAutoDefault(False)
        self.save_btn.setMinimumWidth(110)
        self.save_btn.clicked.connect(self._accept)
        foot.addWidget(cancel)
        foot.addWidget(self.save_btn)
        outer.addLayout(foot)
        self._found.connect(self._on_found)
        self._cover_buttons()

    def _field(self, key: str, text: str, placeholder: str) -> QLineEdit:
        edit = QLineEdit(text)
        edit.setPlaceholderText(placeholder)
        edit.textEdited.connect(lambda _t, k=key: self._touched.add(k))
        return edit

    def done(self, result: int) -> None:
        self._alive = False
        super().done(result)

    def _say(self, text: str, kind: str = "bad") -> None:
        colour = {"bad": T.bad, "warn": T.warn, "ok": T.ok}[kind].name()
        self.problem.setText(f'<span style="color:{colour}">{text}</span>')
        self.problem.setVisible(True)

    # ---- the cover ------------------------------------------------------------------------------------------
    def _cover_buttons(self) -> None:
        self.delete_btn.setEnabled(self.poster.has_picture())

    def _show_cover(self, data: bytes) -> None:
        pix = QPixmap()
        if not pix.loadFromData(data):
            self._say("Distill couldn't show that picture.")
            return
        self.poster.set_pixmap(pix)
        self.cover_data, self.cover_off = data, False
        self.problem.setVisible(False)
        self._cover_buttons()

    def _cover_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("LaunchBox Games Database...", self._search_launchbox)
        menu.addAction("SteamGridDB...", self._search_sgdb)
        menu.exec(self.search_btn.mapToGlobal(self.search_btn.rect().bottomLeft()))

    def _search_launchbox(self) -> None:
        system = self.system.currentText().strip()
        dlg = LaunchBoxCoverDialog(self, self.name.text().strip() or self.game.title, system)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.chosen:
            try:
                rc.check_cover_bytes(dlg.chosen)
            except rc.RecompError as exc:
                self._say(str(exc))
                return
            self._show_cover(dlg.chosen)

    def _search_sgdb(self) -> None:
        if not self.sgdb_key:
            self._say("Searching SteamGridDB needs a free API key. Add yours under Settings > API keys > Games & artwork, then try again.", "warn")
            return
        dlg = CoverSearchDialog(self, self.sgdb_key, self.name.text().strip() or self.game.title)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.chosen:
            try:
                rc.check_cover_bytes(dlg.chosen)
            except rc.RecompError as exc:
                self._say(str(exc))
                return
            self._show_cover(dlg.chosen)

    def _upload_cover(self) -> None:
        if (data := pick_picture(self, self._say)) is not None:
            self._show_cover(data)

    def _remove_cover(self) -> None:
        if not self.poster.has_picture():
            return
        if self._cover_on_disk:
            self.cover_off = True  # the kept cover goes when Save is pressed, and none is looked for afterwards
        self.cover_data = None
        self.poster.set_pixmap(None)
        self.poster.set_note(REMOVED_NOTE if self.cover_off else EMPTY_NOTE)
        self.problem.setVisible(False)
        self._cover_buttons()

    # ---- looking the name up ----------------------------------------------------------------------------------
    def _name_done(self) -> None:
        """Leaving the name box (or Enter in it) looks the name up, but only when it was changed."""
        if self.name.text().strip() and self.name.text().strip() != self._looked_for:
            self._look(force=False)

    def _look(self, force: bool) -> None:
        title = self.name.text().strip()
        if not title:
            self.status.setText("Type the game's name first.")
            return
        self._looked_for = title
        self._serial += 1
        serial, system = self._serial, self.system.currentText().strip()
        self.look_btn.setEnabled(False)
        self.look_btn.setText("Looking...")
        self.status.setText("Searching the LaunchBox Games Database...")

        def work() -> None:
            matches, error = None, ""
            try:
                matches = gm.launchbox_search(title, system)
            except gm.GameError as exc:
                error = str(exc)
            except Exception as exc:  # a page that can't be read must not take the window down
                error = f"The lookup failed: {exc}"
            if self._alive:
                try:
                    self._found.emit(serial, matches, error)
                except RuntimeError:  # the window is gone
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _on_found(self, serial: int, matches, error: str) -> None:
        if serial != self._serial:
            return
        self.look_btn.setEnabled(True)
        self.look_btn.setText("Look up")
        self.results.clear()
        self._matches = list(matches or [])
        self.results.setVisible(bool(self._matches))
        if error:
            self.status.setText(error)
            return
        if not self._matches:
            self.status.setText("Nothing found for that name. Check the spelling, or fill the details in yourself.")
            return
        for m in self._matches:
            item = QListWidgetItem(m.label)
            item.setSizeHint(QSize(0, 30))
            self.results.addItem(item)
        current = self.system.currentText().strip().casefold()
        best = next((i for i, m in enumerate(self._matches) if m.system.casefold() == current), 0)
        on_platform = bool(current) and self._matches[best].system.casefold() == current
        self.results.setCurrentRow(best)
        self._apply(self._matches[best], overwrite=False)
        self.status.setText(f"Found {len(self._matches)} match{'es' if len(self._matches) != 1 else ''}. "
                            + ("Pick the right one below if it isn't the first." if on_platform or not current else
                               f"None is on {self.system.currentText().strip()}: pick one to move the game to its platform."))

    def _pick(self, item: QListWidgetItem) -> None:
        row = self.results.row(item)
        if 0 <= row < len(self._matches):
            self._apply(self._matches[row], overwrite=True)

    def _apply(self, m: gm.Match, overwrite: bool) -> None:
        """Fill the form from a match. A pick (overwrite) replaces everything; the automatic fill after a search only fills what is empty and
        what the person hasn't typed, and sets the platform only while it is unset."""
        def put(key: str, edit: QLineEdit, value: str) -> None:
            if value and (overwrite or (key not in self._touched and not edit.text().strip())):
                edit.setText(value)

        put("developer", self.developer, m.developer)
        put("publisher", self.publisher, m.publisher)
        put("year", self.year, m.year)
        put("genre", self.genre, m.genre)
        if m.system and (overwrite or (not self.system.currentText().strip() and "system" not in self._touched)):
            self.system.setCurrentText(m.system)
        if overwrite and m.title:
            self.name.setText(m.title)
            self._looked_for = m.title

    # ---- the answer ------------------------------------------------------------------------------------------
    def _accept(self) -> None:
        title = self.name.text().strip()
        system = self.system.currentText().strip()
        if not system and self.game.system == gm.UNKNOWN:
            system = gm.UNKNOWN  # still unplaced: the other details can be saved without one
        elif not system:
            self._say("Choose the platform this game is for.")
            return
        year = self.year.text().strip()
        if year and not (year.isdigit() and len(year) == 4):
            self._say("The year should be four digits, like 1994 (or empty).")
            return
        self.values = {
            "title": "" if title in ("", self.game.title) else title,
            "system": None if system == self.game.system else system,  # not pinned unless the person changed it
            "developer": self.developer.text(), "publisher": self.publisher.text(), "year": year, "genre": self.genre.text(),
            "rating": self.stars.value(), "status": str(self.status_box.currentData() or ""), "notes": self.notes.toPlainText(),
        }
        self.accept()


def _health_dot(colour: QColor, size: int = 12) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pix)


class PlatformSetupDialog(FramelessDialog):
    """One place for each platform: what plays it by default, the launch options of that emulator, and the BIOS files it asks for. The list on the
    left shows which platforms are ready, so a missing BIOS or a removed emulator shows up here before a game is started."""

    def __init__(self, parent: QWidget, store: gm.GameStore, retro: Callable[[], gm.RetroSetup | None],
                 emus_for: Callable[[str], dict[str, gm.Standalone]], start: str = ""):
        super().__init__(parent, "Platform setup", "gamepad", (900, 580), (740, 460))
        self.store, self._retro, self._emus_for = store, retro, emus_for
        self.changed = False  # a default or launch option was changed
        self._setup = retro()
        self._rows: dict[str, gm.PlatformHealth] = {}
        self._system = ""
        self._loading = False
        outer = self.content
        body = QHBoxLayout()
        body.setContentsMargins(20, 14, 20, 8)
        body.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(8)
        self.overview = QLabel()
        self.overview.setWordWrap(True)
        self.platforms = QListWidget()
        self.platforms.setFixedWidth(250)
        self.platforms.setFrameShape(QFrame.Shape.NoFrame)
        self.platforms.setIconSize(QSize(12, 12))
        T.bind_css(self.platforms, lambda: (
            f"QListWidget {{ background: {T.input.name()}; border: 1px solid {T.card_border.name()}; border-radius: 10px; outline: 0; padding: 4px; }}"
            f"QListWidget::item {{ padding: 7px 8px; border-radius: 6px; color: {T.fg2.name()}; }}"
            f"QListWidget::item:hover {{ background: {T.hover.name()}; }}"
            f"QListWidget::item:selected {{ background: {T.select.name()}; color: {T.title.name()}; }}"))
        self.platforms.currentItemChanged.connect(lambda item, _p=None: self._show(str(item.data(Qt.ItemDataRole.UserRole)) if item else ""))
        left.addWidget(self.overview)
        left.addWidget(self.platforms, 1)
        body.addLayout(left)

        right = QVBoxLayout()
        right.setSpacing(12)
        body.addLayout(right, 1)
        self.title = QLabel()
        self.title.setObjectName("pageTitle")
        self.title.setStyleSheet("font-size: 16pt;")
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.state.setTextFormat(Qt.TextFormat.RichText)
        right.addWidget(self.title)
        right.addWidget(self.state)
        self.with_box = QComboBox()
        self.with_box.setToolTip("What starts this platform's games, unless a game has its own choice.")
        self.with_box.activated.connect(self._save_default)
        right.addLayout(_stack("Play with", self.with_box))
        self.opts = QLineEdit()
        self.opts.editingFinished.connect(self._save_options)
        self.opts_box = QWidget()
        opts_lay = _stack("Launch options", self.opts)
        self.opts_note = hint("")
        opts_lay.addWidget(self.opts_note)
        opts_lay.setContentsMargins(0, 0, 0, 0)
        self.opts_box.setLayout(opts_lay)
        right.addWidget(self.opts_box)
        self.bios_box = QWidget()
        bios = QVBoxLayout(self.bios_box)
        bios.setContentsMargins(0, 0, 0, 0)
        bios.setSpacing(6)
        bios.addWidget(_caption("BIOS files"))
        self.bios_text = QLabel()
        self.bios_text.setWordWrap(True)
        self.bios_text.setTextFormat(Qt.TextFormat.RichText)
        self.bios_open = QPushButton("Open the BIOS folder")
        self.bios_open.setAutoDefault(False)
        self.bios_open.clicked.connect(self._open_bios)
        bios.addWidget(self.bios_text)
        bios.addWidget(self.bios_open, 0, Qt.AlignmentFlag.AlignLeft)
        right.addWidget(self.bios_box)
        right.addStretch(1)
        outer.addLayout(body, 1)
        outer.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        foot.setSpacing(10)
        again = QPushButton("Check again")
        again.setAutoDefault(False)
        again.setToolTip("Look at the emulators and BIOS files again, after you have installed or copied something.")
        again.clicked.connect(self.refresh)
        done = accent_button("Done")
        done.setAutoDefault(False)
        done.clicked.connect(self.accept)
        foot.addWidget(again)
        foot.addStretch(1)
        foot.addWidget(done)
        outer.addLayout(foot)
        self.refresh(start)

    # ---- the list -----------------------------------------------------------------------------------------
    def _colour(self, level: str) -> QColor:
        return {"ok": T.ok, "warn": T.warn, "bad": T.bad}[level]

    def refresh(self, keep: str = "") -> None:
        """Read the emulators and BIOS files again and list every platform, the ones that need attention first."""
        keep = keep if isinstance(keep, str) and keep else self._system
        self._setup = self._retro()
        rows = gm.health_overview(self.store.systems(), self.store.default_for, self._setup, self._emus_for)
        self._rows = {h.system: h for h in rows}
        self.platforms.blockSignals(True)
        self.platforms.clear()
        for h in rows:
            item = QListWidgetItem(_health_dot(self._colour(h.level)), f"{h.system}   ({h.games})")
            item.setData(Qt.ItemDataRole.UserRole, h.system)
            item.setToolTip(h.summary)
            self.platforms.addItem(item)
        self.platforms.blockSignals(False)
        self._say_overview()
        row = next((i for i, h in enumerate(rows) if h.system == keep), 0)
        if rows:
            self.platforms.setCurrentRow(row)
            self._show(rows[row].system)
        else:
            self._show("")

    def _say_overview(self) -> None:
        needs = sum(1 for r in self._rows.values() if r.level != "ok")
        if not self._rows:
            text = "No games yet."
        elif needs:
            text = f"{needs} platform{'s' if needs != 1 else ''} need{'' if needs != 1 else 's'} attention."
        else:
            text = "Every platform is ready."
        self.overview.setText(text)
        T.bind_css(self.overview, lambda: f"color: {(T.warn if needs else T.fg3).name()}; background: transparent; font-weight: 600;")

    # ---- one platform -------------------------------------------------------------------------------------
    def _show(self, system: str) -> None:
        self._system = system
        h = self._rows.get(system)
        for w in (self.with_box, self.opts_box, self.bios_box):
            w.setVisible(h is not None)
        if h is None:
            self.title.setText("")
            self.state.setText("")
            return
        self._loading = True
        emus = self._emus_for(system)
        self.title.setText(system)
        lines = [f'<span style="color:{self._colour("ok").name()}">Ready: {h.label}.</span>'] if h.level == "ok" else []
        lines += [f'<span style="color:{self._colour("bad" if not h.emulator else "warn").name()}">{long}</span>' for _short, long in h.problems]
        if h.level == "warn" and h.label:
            lines.insert(0, f"Starts with {h.label}.")
        self.state.setText("<br>".join(lines))
        choices = gm.platform_choices(system, self._setup, emus)
        self.with_box.clear()
        self.with_box.addItem(f"Automatic  ({h.label})" if h.label else "Automatic", "")
        for value, label in choices:
            self.with_box.addItem(label, value)
        self.with_box.setCurrentIndex(max(0, self.with_box.findData(h.default)))
        self.with_box.setEnabled(bool(choices))
        standalone = h.emulator not in ("", gm.RETROARCH) and h.emulator in emus
        self.opts_box.setVisible(standalone)
        if standalone:
            text, default = gm.launch_options(h.emulator, emus[h.emulator].args)
            self.opts.setText(emus[h.emulator].args)
            self.opts.setPlaceholderText(default)
            self.opts_note.setText(f"What {emus[h.emulator].name} is started with, for every platform it plays. {{rom}} is the game's file, {{dir}} its folder, "
                                   "{name} its name without the extension. Empty is Distill's own.")
        retro = h.emulator == gm.RETROARCH and self._setup is not None
        self.bios_box.setVisible(retro)
        if retro:
            info = self._setup.infos.get(h.core, {})
            needed = [f for f in gm.core_firmware(info) if not f.optional]
            if h.firmware:
                names = "<br>".join(f"&nbsp;&nbsp;{f.path}  <span style='color:{T.fg3.name()}'>{f.desc}</span>" for f in h.firmware[:8])
                self.bios_text.setText(f"<span style='color:{T.warn.name()}'>Missing from {self._setup.system_dir}:</span><br>{names}")
            else:
                self.bios_text.setText("All the BIOS files this core asks for are there." if needed else "This core doesn't ask for any BIOS files.")
            self.bios_open.setVisible(self._setup.system_dir is not None)
        self._loading = False

    def _update_row(self) -> None:
        """Work out the platform that was changed again, and show it in the list."""
        system = self._system
        old = self._rows.get(system)
        if old is None:
            return
        h = gm.platform_health(system, old.games, self.store.default_for(system), self._setup, self._emus_for(system))
        self._rows[system] = h
        for i in range(self.platforms.count()):
            item = self.platforms.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == system:
                item.setIcon(_health_dot(self._colour(h.level)))
                item.setToolTip(h.summary)
        self._say_overview()
        self._show(system)

    def _save_default(self, _index: int = 0) -> None:
        if self._loading or not self._system:
            return
        self.store.set_default(self._system, str(self.with_box.currentData() or ""))
        self.changed = True
        self._update_row()

    def _save_options(self) -> None:
        h = self._rows.get(self._system)
        if self._loading or h is None or h.emulator in ("", gm.RETROARCH):
            return
        text = self.opts.text().strip()
        default = gm.launch_options(h.emulator, "")[1]
        self.store.set_setting(f"args:{h.emulator}", "" if text in ("", default) else text)
        self.changed = True

    def _open_bios(self) -> None:
        folder = self._setup.system_dir if self._setup is not None else None
        if folder is not None:
            folder.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class LibrariesDialog(FramelessDialog):
    """The folders that hold the player's games: add one, take one off, and whether to scan them when the Games page is opened."""

    def __init__(self, parent: QWidget, store: gm.GameStore, retro: Callable[[], gm.RetroSetup | None] | None = None):
        super().__init__(parent, "Game libraries", "folder", (640, 470), (520, 360))
        self.store = store
        self._retro = retro or (lambda: None)  # the installed RetroArch, for exporting its playlists
        self.changed = False  # a folder was added or taken off
        self.added = False  # a folder was added, so a scan is due
        self.romm_moved = False  # the folder RomM games are installed into was changed
        outer = self.content
        intro = hint("Distill looks through these folders for games, and works out each game's platform. Files are only read, never changed.")
        intro.setContentsMargins(20, 14, 20, 0)
        outer.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        host = QWidget()
        host.setObjectName("libHost")
        host.setStyleSheet("#libHost { background: transparent; }")
        self.rows = QVBoxLayout(host)
        self.rows.setContentsMargins(20, 12, 20, 12)
        self.rows.setSpacing(8)
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)
        outer.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        foot.setSpacing(10)
        self.scan_open = AnimCheckBox("Scan when I open Games")
        self.scan_open.setToolTip("Look through these folders for new and removed games each time you open the Games page.")
        self.scan_open.setChecked(store.setting("scan_on_open") == "1")
        self.scan_open.toggled.connect(lambda on: store.set_setting("scan_on_open", "1" if on else "0"))
        add = accent_button("Add folder...")
        add.setAutoDefault(False)
        add.clicked.connect(self._add)
        bring = QPushButton("Import / export")  # history from another front end, and the library out to one
        bring.setAutoDefault(False)
        bring.setToolTip("Bring your favourites, play time, stars and collections across from LaunchBox or Playnite (only games already in your library are updated), "
                         "or write your library out as RetroArch playlists.")
        menu = QMenu(bring)
        menu.addAction("Import history from LaunchBox...", self._import_launchbox)
        menu.addAction("Import history from Playnite...", self._import_playnite)
        menu.addSeparator()
        menu.addAction("Export RetroArch playlists...", self._export_retroarch)
        bring.setMenu(menu)
        done = QPushButton("Done")
        done.setAutoDefault(False)
        done.clicked.connect(self.accept)
        foot.addWidget(self.scan_open)
        foot.addStretch(1)
        foot.addWidget(bring)
        foot.addWidget(add)
        foot.addWidget(done)
        outer.addLayout(foot)
        self._fill()

    def _fill(self) -> None:
        clear_layout(self.rows)
        roots = self.store.library_roots()
        counts = self.store.root_counts()
        if not roots:
            self.rows.addWidget(hint("No folders yet. Add the folder that holds your games."))
        for root in roots:
            row = QFrame()
            row.setObjectName("libRow")
            T.bind_css(row, lambda: f"QFrame#libRow {{ background: {T.card.name()}; border: 1px solid {T.card_border.name()}; border-radius: 12px; }}")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(14, 10, 10, 10)
            lay.setSpacing(10)
            text = QVBoxLayout()
            text.setSpacing(2)
            path = QLabel(root)
            path.setWordWrap(True)
            T.bind_css(path, lambda: f"color: {T.title.name()}; font-weight: 600; background: transparent;")
            n = counts.get(root, 0)
            bits = [f"{n} game{'s' if n != 1 else ''}"]
            if not Path(root).is_dir():
                bits.append("folder not found: its games stay listed until it is back")
            elif plat := gm.folder_platform(root):
                bits.append(f"games it can't place go on {plat}")
            sub = QLabel("  ·  ".join(bits))
            T.bind_css(sub, lambda: f"color: {T.fg3.name()}; background: transparent;")
            text.addWidget(path)
            text.addWidget(sub)
            lay.addLayout(text, 1)
            drop = _icon_button("trash", "Take this folder off the list", danger=True)
            drop.clicked.connect(lambda _c=False, r=root: self._remove(r))
            lay.addWidget(drop, 0, Qt.AlignmentFlag.AlignVCenter)
            self.rows.addWidget(row)
        if apikeys.get("romm_url") and apikeys.get("romm"):  # RomM is set up: say where its games are installed, and let that be changed
            self.rows.addWidget(self._romm_row())
        self.rows.addStretch(1)

    def _romm_row(self) -> QFrame:
        row = QFrame()
        row.setObjectName("rommRow")
        T.bind_css(row, lambda: f"QFrame#rommRow {{ background: {T.card.name()}; border: 1px dashed {T.card_border.name()}; border-radius: 12px; }}")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel("RomM downloads go to")
        T.bind_css(title, lambda: f"color: {T.title.name()}; font-weight: 600; background: transparent;")
        folder = self.store.setting("romm_folder") or str(rc.root() / "RomM")
        where = QLabel(folder)
        where.setWordWrap(True)
        T.bind_css(where, lambda: f"color: {T.fg3.name()}; background: transparent;")
        text.addWidget(title)
        text.addWidget(where)
        lay.addLayout(text, 1)
        change = QPushButton("Change...")
        change.setAutoDefault(False)
        change.clicked.connect(self._change_romm_folder)
        lay.addWidget(change, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _change_romm_folder(self) -> None:
        current = self.store.setting("romm_folder") or str(rc.root() / "RomM")
        folder = QFileDialog.getExistingDirectory(self, "Choose where games from RomM are installed", current)
        if folder and folder != current:
            self.store.set_setting("romm_folder", folder)
            self.romm_moved = True
            self._fill()

    def _add(self) -> None:
        roots = self.store.library_roots()
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder that holds your games", roots[-1] if roots else "")
        if not folder:
            return
        if self.store.add_library_root(folder):
            self.changed = self.added = True
            self._fill()

    # ---- importing another front end's history ---------------------------------------------------------------
    def _import_launchbox(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose the folder that holds LaunchBox (the one with LaunchBox.exe)")
        if folder:
            self._run_import("LaunchBox", lambda: gameimport.read_launchbox(folder))

    def _import_playnite(self) -> None:
        pick, _tick = msgbox.ask(self, "info", "Import from Playnite",
                                 "Playnite keeps its library in a database Distill can't open, so Distill reads a JSON file exported from it.",
                                 [("Choose the file...", "accept"), ("Copy the export command", "action"), ("Cancel", "reject")], default=0, escape=2,
                                 informative="Make the file with the export command (it goes in Playnite's SDK console), or choose a JSON export you already have.")
        if pick == 1:
            QApplication.clipboard().setText(gameimport.PLAYNITE_COMMAND)
            msgbox.information(self, "Import from Playnite", "The export command is on your clipboard. Paste it into Playnite's SDK console and run it; it writes "
                               "playnite-games.json to your desktop. Then come back here and choose that file.")
        elif pick == 0:
            path, _f = QFileDialog.getOpenFileName(self, "Choose the Playnite export", "", "JSON files (*.json);;All files (*)")
            if path:
                self._run_import("Playnite", lambda: gameimport.read_playnite(path))

    def _export_retroarch(self) -> None:
        setup = self._retro()
        target = setup.playlist_dir if setup is not None else None
        text = ("Distill writes one playlist per platform, plus Favourites and one for each collection. A playlist RetroArch already has keeps everything "
                "in it; only games it doesn't have yet are added. Nothing is removed.")
        buttons = ([(f"Write to RetroArch ({target.name})", "accept")] if target is not None else []) + [("Choose a folder...", "action"), ("Cancel", "reject")]
        pick, _tick = msgbox.ask(self, "info", "Export RetroArch playlists", text, buttons, default=0, escape=len(buttons) - 1,
                                 informative=f"RetroArch's playlist folder: {target}" if target is not None else "RetroArch isn't set up in Distill yet, so each game is left "
                                 "for RetroArch to pick its core (DETECT).")
        if pick == len(buttons) - 1 or pick < 0:
            return
        folder = str(target) if (target is not None and pick == 0) else QFileDialog.getExistingDirectory(self, "Choose where to write the playlists", str(target or ""))
        if not folder:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = gameexport.export_retroarch(self.store, folder, setup)
        except OSError as exc:
            QApplication.restoreOverrideCursor()
            msgbox.warning(self, "Export RetroArch playlists", f"Couldn't write the playlists to {folder}: {exc}")
            return
        QApplication.restoreOverrideCursor()
        msgbox.information(self, "Export RetroArch playlists", report.summary())

    def _run_import(self, source: str, read: Callable[[], list]) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = gameimport.apply(self.store, read(), source)
        except gameimport.ImportProblem as exc:
            QApplication.restoreOverrideCursor()
            msgbox.warning(self, f"Import from {source}", str(exc))
            return
        except Exception as exc:  # a file in an odd shape must end in a message, not a crash
            QApplication.restoreOverrideCursor()
            msgbox.warning(self, f"Import from {source}", f"The import stopped: {exc}")
            return
        QApplication.restoreOverrideCursor()
        self.changed = True  # the page reloads: new favourites, stars and collections
        msgbox.information(self, f"Import from {source}", report.summary())

    def _remove(self, root: str) -> None:
        n = self.store.root_counts().get(root, 0)
        answer = msgbox.question(self, "Take folder off the list", f"Take {root} off the list?\n\n{n} game{'s' if n != 1 else ''} found in it will leave your "
                                 "library. The files stay where they are, and adding the folder again brings the games back.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.remove_library_root(root)
        self.changed = True
        self._fill()


class RetroAchievementsDialog(FramelessDialog):
    """Asks for the RetroAchievements user name and Web API key (the same two boxes as Settings > API keys > Games & artwork), tests them and saves them."""

    def __init__(self, parent: QWidget):
        super().__init__(parent, "RetroAchievements", "star", (520, 330), (460, 300))
        outer = self.content
        intro = hint("Shows a game's achievements, and the ones you have unlocked, on its page. Use your RetroAchievements user name and the Web API key "
                     "from your settings page on retroachievements.org. They stay on this computer.")
        intro.setContentsMargins(20, 14, 20, 0)
        outer.addWidget(intro)
        form = QVBoxLayout()
        form.setContentsMargins(20, 12, 20, 6)
        form.setSpacing(12)
        self.user = QLineEdit(apikeys.get("ra_user"))
        self.user.setPlaceholderText("Your user name")
        self.key = QLineEdit(apikeys.get("ra"))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Web API key")
        form.addLayout(_stack("User name", self.user))
        form.addLayout(_stack("Web API key", self.key))
        outer.addLayout(form)
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setContentsMargins(20, 4, 20, 0)
        self.problem.setVisible(False)
        outer.addWidget(self.problem)
        outer.addStretch(1)
        outer.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        foot.setSpacing(8)
        get = QPushButton("Get key...")
        get.setAutoDefault(False)
        get.clicked.connect(lambda: webbrowser.open("https://retroachievements.org/controlpanel.php"))
        test = QPushButton("Test")
        test.setAutoDefault(False)
        test.clicked.connect(self._test)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        save = accent_button("Save")
        save.setAutoDefault(False)
        save.setMinimumWidth(100)
        save.clicked.connect(self._save)
        foot.addWidget(get)
        foot.addWidget(test)
        foot.addStretch(1)
        foot.addWidget(cancel)
        foot.addWidget(save)
        outer.addLayout(foot)

    def _say(self, text: str, good: bool = False) -> None:
        colour = (T.ok if good else T.bad).name()
        self.problem.setText(f'<span style="color:{colour}">{text}</span>')
        self.problem.setVisible(True)

    def _test(self) -> None:
        user, key = self.user.text().strip(), self.key.text().strip()
        if not user or not key:
            self._say("Enter both the user name and the key.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            problem = gamemedia.ra_test(user, key)
        finally:
            QApplication.restoreOverrideCursor()
        self._say(problem or "That works.", good=not problem)

    def _save(self) -> None:
        apikeys.set("ra_user", self.user.text())
        apikeys.set("ra", self.key.text())
        self.accept()


class ClickLabel(QLabel):
    """A label that can be clicked (a picture tile)."""
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ImagePreview(FramelessDialog):
    """A screenshot at a readable size. Click it, or use the arrows or the keyboard's left and right keys, to move through the game's screenshots."""

    def __init__(self, parent: QWidget, title: str, paths: list[Path], index: int = 0):
        super().__init__(parent, title, "image", (1000, 640), (520, 360))
        self.paths, self.index = paths, max(0, min(index, len(paths) - 1))
        self._pix = QPixmap()
        self._wait = QTimer(self)  # while only the small preview is here, look now and then for the big picture and swap it in
        self._wait.setInterval(400)
        self._wait.timeout.connect(self._swap_in)
        self.label = ClickLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.label.setMinimumSize(1, 1)
        self.label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.label.clicked.connect(lambda: self._step(1))
        self.content.addWidget(self.label, 1)
        self.content.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 10, 20, 12)
        foot.setSpacing(8)
        self.prev_btn, self.next_btn = QPushButton("‹"), QPushButton("›")
        for b, step in ((self.prev_btn, -1), (self.next_btn, 1)):
            b.setAutoDefault(False)
            b.setFixedWidth(44)
            b.clicked.connect(lambda _c=False, s=step: self._step(s))
        self.counter = QLabel()
        T.bind_css(self.counter, lambda: f"color: {T.fg3.name()}; background: transparent;")
        close = QPushButton("Close")
        close.setAutoDefault(False)
        close.clicked.connect(self.accept)
        foot.addWidget(self.prev_btn)
        foot.addWidget(self.next_btn)
        foot.addWidget(self.counter)
        foot.addStretch(1)
        foot.addWidget(close)
        self.content.addLayout(foot)
        self._load()

    def _load(self) -> None:
        self._wait.stop()
        self._pix = QPixmap()
        if self.paths:
            full = self.paths[self.index]
            shown = gamemedia.shot_image(full)  # the small preview straight away when the big picture is still coming
            self._pix = QPixmap(str(shown)) if shown else QPixmap()
            if shown != full:
                self._wait.start()
        self.counter.setText(f"{self.index + 1} of {len(self.paths)}")
        self.prev_btn.setEnabled(self.index > 0)
        self.next_btn.setEnabled(self.index < len(self.paths) - 1)
        self._fit()

    def _swap_in(self) -> None:
        if self.paths and self.paths[self.index].is_file():
            self._wait.stop()
            self._pix = QPixmap(str(self.paths[self.index]))
            self._fit()

    def _fit(self) -> None:
        if not self._pix.isNull():
            self.label.setPixmap(self._pix.scaled(self.label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _step(self, by: int) -> None:
        self.index = (self.index + by) % len(self.paths) if self.paths else 0
        self._load()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Left:
            self._step(-1)
        elif event.key() == Qt.Key.Key_Right:
            self._step(1)
        else:
            super().keyPressEvent(event)


class LaunchBoxCoverDialog(FramelessDialog):
    """Pick a cover from the LaunchBox Games Database: the front covers of every entry the name could mean, which is a wider choice than a
    single poster. `chosen` holds the picture's bytes once one is picked."""
    _urls_in = Signal(int, object, str)  # (search number, [addresses] or None, what went wrong)
    _pic_in = Signal(int, int, object)  # (search number, row, picture bytes)

    def __init__(self, parent: QWidget, term: str, system: str = ""):
        super().__init__(parent, "Box art from LaunchBox", "search", (780, 660), (560, 480))
        self.chosen: bytes | None = None
        self.system = system
        self._serial = 0
        self._alive = True
        self._pics: dict[int, bytes] = {}
        outer = self.content
        row = QHBoxLayout()
        row.setContentsMargins(20, 14, 20, 0)
        row.setSpacing(8)
        self.term = QLineEdit(term)
        self.term.setClearButtonEnabled(True)
        self.term.setPlaceholderText("The game's name")
        self.term.returnPressed.connect(self._search)
        go = accent_button("Search")
        go.setAutoDefault(False)
        go.clicked.connect(self._search)
        row.addWidget(self.term, 1)
        row.addWidget(go)
        outer.addLayout(row)
        self.status = hint("", live=True)
        self.status.setContentsMargins(20, 8, 20, 0)
        outer.addWidget(self.status)
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setIconSize(QSize(132, 176))
        self.grid.setGridSize(QSize(152, 196))
        self.grid.setFrameShape(QFrame.Shape.NoFrame)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        T.bind_css(self.grid, lambda: (f"QListWidget {{ background: transparent; border: none; outline: 0; padding: 6px 14px; }}"
                                       f"QListWidget::item {{ border-radius: 12px; }}"
                                       f"QListWidget::item:hover {{ background: {T.hover.name()}; }}"
                                       f"QListWidget::item:selected {{ background: {T.select.name()}; }}"))
        self.grid.itemClicked.connect(self._pick)
        outer.addWidget(self.grid, 1)
        outer.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        foot.addWidget(hint("Click a cover to use it."))
        foot.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        foot.addWidget(cancel)
        outer.addLayout(foot)
        self._urls_in.connect(self._on_urls)
        self._pic_in.connect(self._on_pic)
        self._search()

    def done(self, result: int) -> None:
        self._alive = False
        super().done(result)

    def _search(self) -> None:
        term = self.term.text().strip()
        if not term:
            return
        self._serial += 1
        serial, system = self._serial, self.system
        self.grid.clear()
        self._pics.clear()
        self.status.setText("Searching the LaunchBox Games Database...")

        def work() -> None:
            try:
                urls = gm.launchbox_cover_urls(term, system)
            except gm.GameError as exc:
                self._emit(self._urls_in, serial, None, str(exc))
                return
            except Exception as exc:  # a page that can't be read must not take the window down
                self._emit(self._urls_in, serial, None, f"The search failed: {exc}")
                return
            self._emit(self._urls_in, serial, urls, "")
            from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
            with ThreadPoolExecutor(max_workers=4) as pool:
                for row, data in enumerate(pool.map(rc._image, urls)):
                    if not self._alive or serial != self._serial:
                        return
                    self._emit(self._pic_in, serial, row, data)

        threading.Thread(target=work, daemon=True).start()

    def _emit(self, signal, *args) -> None:
        if self._alive:
            try:
                signal.emit(*args)
            except RuntimeError:  # the window is gone
                pass

    def _on_urls(self, serial: int, urls, error: str) -> None:
        if serial != self._serial:
            return
        if error:
            self.status.setText(error)
            return
        if not urls:
            self.status.setText("No box art found for that name. Try a shorter or different spelling.")
            return
        self.status.setText(f"{len(urls)} cover{'s' if len(urls) != 1 else ''} found.")
        for _ in urls:
            item = QListWidgetItem("...")
            item.setSizeHint(QSize(152, 196))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.grid.addItem(item)

    def _on_pic(self, serial: int, row: int, data) -> None:
        item = self.grid.item(row) if serial == self._serial else None
        if item is None:
            return
        pix = QPixmap()
        if data and pix.loadFromData(data):
            self._pics[row] = data
            item.setText("")
            item.setIcon(QIcon(pix.scaled(264, 352, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
        else:
            item.setText("?")  # a picture that can't be read: it can't be picked

    def _pick(self, item: QListWidgetItem) -> None:
        data = self._pics.get(self.grid.row(item))
        if data:
            self.chosen = data
            self.accept()


class _Bar(QWidget):
    """One platform's share of the time played: a rounded bar, as long as its share of the platform that has the most."""

    def __init__(self, share: float):
        super().__init__()
        self.share = max(0.0, min(1.0, share))
        self.setFixedHeight(8)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = self.rect()
        p.setBrush(T.input)
        p.drawRoundedRect(r, 4, 4)
        if self.share > 0:
            r.setWidth(max(8, int(r.width() * self.share)))
            p.setBrush(T.accent)
            p.drawRoundedRect(r, 4, 4)


class StatsDialog(FramelessDialog):
    """Playtime by platform: how long the player has spent on each one, most first, with the whole as a total."""

    def __init__(self, parent: QWidget, rows: list[gm.PlatformTime]):
        super().__init__(parent, "Playtime by platform", "clock", (560, 520), (440, 360))
        whole = sum(r.seconds for r in rows)
        outer = self.content
        if not rows:
            note = hint("Nothing played yet. Time is added up from the games you start from Distill, once they have run for a little while.")
            note.setContentsMargins(20, 16, 20, 0)
            outer.addWidget(note)
            outer.addStretch(1)
        else:
            top = hint(f"{gm.fmt_playtime(whole)} in all, across {sum(r.games for r in rows)} game{'s' if sum(r.games for r in rows) != 1 else ''} on {len(rows)} platform{'s' if len(rows) != 1 else ''}.")
            top.setContentsMargins(20, 14, 20, 0)
            outer.addWidget(top)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
            scroll.viewport().setAutoFillBackground(False)
            host = QWidget()
            host.setObjectName("statHost")
            host.setStyleSheet("#statHost { background: transparent; }")
            col = QVBoxLayout(host)
            col.setContentsMargins(20, 12, 20, 12)
            col.setSpacing(12)
            biggest = rows[0].seconds
            for r in rows:
                line = QHBoxLayout()
                name = QLabel(r.system)
                name.setStyleSheet(f"color: {T.fg.name()}; font-weight: 600;")
                amount = QLabel(f"{gm.fmt_playtime(r.seconds)}  ·  {round(100 * r.share(whole))}%")
                amount.setStyleSheet(f"color: {T.fg3.name()};")
                line.addWidget(name, 1)
                line.addWidget(amount)
                box = QVBoxLayout()
                box.setSpacing(5)
                box.addLayout(line)
                box.addWidget(_Bar(r.seconds / biggest))
                more = hint(f"{r.games} of {r.total} game{'s' if r.total != 1 else ''} played")
                box.addWidget(more)
                col.addLayout(box)
            col.addStretch(1)
            scroll.setWidget(host)
            outer.addWidget(scroll, 1)
        outer.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        foot.addStretch(1)
        done = QPushButton("Done")
        done.setAutoDefault(False)
        done.clicked.connect(self.accept)
        foot.addWidget(done)
        outer.addLayout(foot)
