"""The Add recomp window: put a project that isn't on any list yet on the PC Recomps page. It is laid out like an edit window for a
game in a library manager: an empty poster at the top left (the shape of SteamGridDB's standard 600 x 900 poster) that can be searched
for on SteamGridDB, uploaded from the person's own computer or removed, and beside it the project's GitHub address with Check, which
fills in the name, game, platform, type and About below it for the person to correct. Everything else (which systems it runs on,
which download to pick, which game dump it needs, links, a note for first launch) is folded away under Additional details. Nothing is
downloaded here. The same window edits an entry from the player's own list."""
from __future__ import annotations

import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QBuffer, QEasingCurve, QIODevice, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QBrush, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QTextOption
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget)

from .. import recomps as rc
from . import icons, motion
from .frameless import FramelessDialog, hairline
from .theme import T
from .widgets import Expander, accent_button, hint

OTHER = "Other"
POSTER_W, POSTER_H = 200, 300  # 2:3, the shape of SteamGridDB's 600 x 900 poster
THUMB_W, THUMB_H = 120, 180  # a poster in the SteamGridDB search window
MAX_UPLOAD_HEIGHT = 1800  # a picture from the person's own computer that is taller than this is scaled down before it is kept
EMPTY_NOTE = "Leave it empty and Distill looks for one by itself."
REMOVED_NOTE = "The cover is removed. None will be looked for."


# ---- small pieces ---------------------------------------------------------------------------------------------
def _caption(text: str) -> QLabel:
    """The small label above a field."""
    label = QLabel(text)
    T.bind_css(label, lambda: f"color: {T.fg3.name()}; font-size: 9pt; font-weight: 600; background: transparent;")
    return label


def _stack(caption: str, widget: QWidget) -> QVBoxLayout:
    """A field with its caption above it."""
    box = QVBoxLayout()
    box.setSpacing(6)
    box.setContentsMargins(0, 0, 0, 0)
    box.addWidget(_caption(caption))
    box.addWidget(widget)
    return box


def _group_title(text: str, note: str = "") -> QWidget:
    """A heading inside Additional details, with an optional line of explanation."""
    holder = QWidget()
    lay = QVBoxLayout(holder)
    lay.setContentsMargins(0, 6, 0, 0)
    lay.setSpacing(2)
    title = QLabel(text)
    T.bind_css(title, lambda: f"color: {T.title.name()}; font-size: 10pt; font-weight: 600; background: transparent;")
    lay.addWidget(title)
    if note:
        lay.addWidget(hint(note))
    return holder


def _icon_button(name: str, tip: str, danger: bool = False, size: tuple[int, int] = (40, 36)) -> QToolButton:
    """A square button with a line icon, in the app's button colours (red edge for the one that deletes)."""
    button = QToolButton()
    button.setFixedSize(*size)
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setIconSize(QSize(18, 18))
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def style() -> None:
        fill, edge = T.btn_danger if danger else T.btn
        hover_fill, hover_edge = (T.btn_danger[0], T.bad) if danger else T.btn_hover
        colour = T.bad if danger else T.fg
        glyph = QIcon()
        glyph.addPixmap(icons.pixmap(name, colour, 18), QIcon.Mode.Normal)
        glyph.addPixmap(icons.pixmap(name, T.disabled_fg, 18), QIcon.Mode.Disabled)
        button.setIcon(glyph)
        button.setStyleSheet(
            f"QToolButton {{ background: {fill.name()}; border: 1px solid {edge.name()}; border-radius: 10px; }}"
            f"QToolButton:hover {{ background: {hover_fill.name()}; border-color: {hover_edge.name()}; }}"
            f"QToolButton:disabled {{ background: {T.btn_off[0].name()}; border-color: {T.btn_off[1].name()}; }}")

    T.bind(button, style)
    return button


def _sgdb_problem(exc: Exception) -> str:
    """What went wrong talking to SteamGridDB, in plain words."""
    if isinstance(exc, rc.SgdbBusy):
        return "SteamGridDB is busy right now. Try again in a minute."
    if isinstance(exc, urllib.error.HTTPError):
        return "SteamGridDB rejected the API key." if exc.code in (401, 403) else f"SteamGridDB answered {exc.code}."
    return "Couldn't reach SteamGridDB."


def read_picture(path: str) -> bytes:
    """A picture from the person's own computer, as bytes the posters can use. A PNG, JPEG, WebP or GIF of a sensible size is kept
    exactly as it is; anything else Qt can open (a BMP, a huge photo) is turned into a PNG first."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise rc.RecompError(f"Couldn't read that file: {exc.strerror or exc}.") from exc
    try:
        return rc.check_cover_bytes(raw)
    except rc.RecompError:
        pass
    image = QImage.fromData(raw)
    if image.isNull():
        raise rc.RecompError("That isn't a picture Distill can use. Pick a PNG, JPEG, WebP or GIF.")
    if image.height() > MAX_UPLOAD_HEIGHT:
        image = image.scaledToHeight(MAX_UPLOAD_HEIGHT, Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return rc.check_cover_bytes(bytes(buffer.data()))


def pick_picture(parent, say) -> bytes | None:
    """Ask for a picture file and read it. `say` is shown the reason when it can't be used; None when cancelled or unusable."""
    path, _ = QFileDialog.getOpenFileName(parent, "Choose a cover", "", "Pictures (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;All files (*)")
    if not path:
        return None
    try:
        return read_picture(path)
    except rc.RecompError as exc:
        say(str(exc))
        return None


class PosterBox(QWidget):
    """The cover: an empty rounded poster (2:3, like SteamGridDB's 600 x 900) that shows the picture in its own shape when there is
    one. The picture is never cropped, so what is seen here is what the page will draw."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(POSTER_W, POSTER_H)
        self._pix: QPixmap | None = None
        self._note = EMPTY_NOTE

    def set_pixmap(self, pix: QPixmap | None) -> None:
        self._pix = pix if pix is not None and not pix.isNull() else None
        self.update()

    def set_note(self, text: str) -> None:
        """The line shown on the empty poster."""
        self._note = text
        self.update()

    def has_picture(self) -> bool:
        return self._pix is not None

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        shape = QPainterPath()
        shape.addRoundedRect(box, 14, 14)
        p.fillPath(shape, QBrush(T.input))
        if self._pix is not None:
            dpr = self._pix.devicePixelRatio() or 1.0
            w, h = self._pix.width() / dpr, self._pix.height() / dpr
            scale = min(box.width() / w, box.height() / h)
            target = QRectF(0, 0, w * scale, h * scale)
            target.moveCenter(box.center())
            p.save()
            p.setClipPath(shape)
            p.drawPixmap(target, self._pix, QRectF(self._pix.rect()))
            p.restore()
            edge = QPen(T.card_border, 1)
        else:
            p.drawPixmap(int(box.center().x() - 20), int(box.center().y() - 50), icons.pixmap("image", T.disabled_fg, 40))
            p.setPen(T.fg2)
            p.drawText(QRectF(box.left(), box.center().y() + 4, box.width(), 20), "No cover", QTextOption(Qt.AlignmentFlag.AlignCenter))
            p.setPen(T.fg3)
            small = p.font()
            small.setPointSizeF(max(7.5, small.pointSizeF() - 1.5))
            p.setFont(small)
            p.drawText(QRectF(box.left() + 18, box.center().y() + 28, box.width() - 36, 60), self._note,
                       QTextOption(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop))
            edge = QPen(T.border, 1.4, Qt.PenStyle.DashLine)
        p.setPen(edge)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(shape)


# ---- the SteamGridDB search window ---------------------------------------------------------------------------
class CoverSearchDialog(FramelessDialog):
    """Search SteamGridDB for a game and pick one of its posters. `chosen` holds the picture's bytes once one is picked."""
    _games_in = Signal(int, object)  # (search number, list of games or an error sentence)
    _grids_in = Signal(int, object)  # (search number, list of posters or an error sentence)
    _thumb_in = Signal(int, int, object)  # (search number, row, picture bytes)
    _full_in = Signal(object)  # the picked poster's bytes, or None

    def __init__(self, parent: QWidget, key: str, term: str = ""):
        super().__init__(parent, "Search SteamGridDB", "search", (720, 640), (560, 480))
        self.key = key
        self.chosen: bytes | None = None
        self._serial = 0  # a newer search makes the answers to an older one stale
        self._alive = True
        lay = self.content
        body = QVBoxLayout()
        body.setContentsMargins(20, 16, 20, 12)
        body.setSpacing(10)
        row = QHBoxLayout()
        self.term = QLineEdit(term)
        self.term.setPlaceholderText("A game's name, e.g. The Legend of Zelda: Ocarina of Time")
        self.term.setClearButtonEnabled(True)
        self.term.returnPressed.connect(self._search)
        self.go = accent_button("Search")
        self.go.setAutoDefault(False)
        self.go.clicked.connect(self._search)
        row.addWidget(self.term, 1)
        row.addWidget(self.go)
        body.addLayout(row)
        self.game = QComboBox()
        self.game.currentIndexChanged.connect(self._game_picked)
        self.game_row = QWidget()
        pick = QHBoxLayout(self.game_row)
        pick.setContentsMargins(0, 0, 0, 0)
        pick.addWidget(_caption("Game"))
        pick.addWidget(self.game, 1)
        self.game_row.setVisible(False)
        body.addWidget(self.game_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        body.addWidget(self.status)
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setWrapping(True)
        self.grid.setUniformItemSizes(True)
        self.grid.setIconSize(QSize(THUMB_W, THUMB_H))
        self.grid.setGridSize(QSize(THUMB_W + 22, THUMB_H + 22))
        self.grid.setSpacing(2)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.itemSelectionChanged.connect(self._selection_changed)
        self.grid.itemDoubleClicked.connect(lambda _item: self._use())
        T.bind_css(self.grid, lambda: (
            f"QListWidget {{ background: {T.input.name()}; border: 1px solid {T.border.name()}; border-radius: 12px; padding: 8px; outline: 0; }}"
            f"QListWidget::item {{ border: 1px solid transparent; border-radius: 10px; padding: 4px; }}"
            f"QListWidget::item:hover {{ background: {T.hover.name()}; }}"
            f"QListWidget::item:selected {{ background: {T.select.name()}; border: 1px solid {T.accent.name()}; }}"))
        body.addWidget(self.grid, 1)
        body.addWidget(hint("Posters come from steamgriddb.com and belong to the people who made them. Pick one, or double-click it."))
        lay.addLayout(body, 1)
        lay.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        foot.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        self.use_btn = accent_button("Use this cover")
        self.use_btn.setAutoDefault(False)
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use)
        foot.addWidget(cancel)
        foot.addWidget(self.use_btn)
        lay.addLayout(foot)
        self._games_in.connect(self._on_games)
        self._grids_in.connect(self._on_grids)
        self._thumb_in.connect(self._on_thumb)
        self._full_in.connect(self._on_full)
        if term.strip():
            self._search()

    def done(self, result: int) -> None:
        self._alive = False
        super().done(result)

    def _emit(self, signal, *args) -> None:
        """Send from a worker thread, unless the window has gone."""
        if self._alive:
            try:
                signal.emit(*args)
            except RuntimeError:  # the Qt object is already gone
                pass

    def _say(self, text: str, kind: str = "mute") -> None:
        colour = {"mute": T.fg3, "bad": T.bad, "ok": T.ok}[kind].name()
        self.status.setText(f'<span style="color:{colour}">{text}</span>' if text else "")

    # ---- searching ---------------------------------------------------------------------------------------
    def _search(self) -> None:
        term = self.term.text().strip()
        if not term:
            return
        self._serial += 1
        serial = self._serial
        self.grid.clear()
        self.game_row.setVisible(False)
        self.use_btn.setEnabled(False)
        self.go.setEnabled(False)
        self._say("Searching SteamGridDB...")

        def work() -> None:
            try:
                found = rc.sgdb_games(term, self.key)
            except Exception as exc:  # whatever went wrong comes back as a sentence
                found = _sgdb_problem(exc)
            self._emit(self._games_in, serial, found)

        threading.Thread(target=work, daemon=True).start()

    def _on_games(self, serial: int, found) -> None:
        if serial != self._serial:
            return
        self.go.setEnabled(True)
        if isinstance(found, str):
            self._say(found, "bad")
            return
        if not found:
            self._say("SteamGridDB has no game with that name. Try fewer words.")
            return
        self.game.blockSignals(True)
        self.game.clear()
        for g in found:
            self.game.addItem(g["name"] + ("  ✓" if g["verified"] else ""), g["id"])
        self.game.blockSignals(False)
        self.game_row.setVisible(len(found) > 1)
        self._game_picked(0)

    def _game_picked(self, _index: int = 0) -> None:
        game_id = self.game.currentData()
        if game_id is None:
            return
        self._serial += 1
        serial = self._serial
        self.grid.clear()
        self.use_btn.setEnabled(False)
        self._say("Loading posters...")

        def work() -> None:
            try:
                grids = rc.sgdb_grids(int(game_id), self.key)
            except Exception as exc:
                grids = _sgdb_problem(exc)
            self._emit(self._grids_in, serial, grids)

        threading.Thread(target=work, daemon=True).start()

    def _on_grids(self, serial: int, grids) -> None:
        if serial != self._serial:
            return
        if isinstance(grids, str):
            self._say(grids, "bad")
            return
        if not grids:
            self._say("This game has no posters on SteamGridDB yet." + (" Try another game in the list." if self.game.count() > 1 else ""))
            return
        grids = grids[:30]
        self._say(f"{len(grids)} poster{'s' if len(grids) != 1 else ''}. Pick one.")
        blank = QPixmap(THUMB_W, THUMB_H)
        blank.fill(T.card)
        for g in grids:
            item = QListWidgetItem(QIcon(blank), "")
            item.setData(Qt.ItemDataRole.UserRole, g["url"])
            item.setToolTip(f"by {g['author']}" if g["author"] else "")
            item.setSizeHint(QSize(THUMB_W + 20, THUMB_H + 20))
            self.grid.addItem(item)
        urls = [g["thumb"] for g in grids]

        def work() -> None:
            def one(row: int) -> None:
                if serial != self._serial or not self._alive:
                    return
                data = rc.fetch_image(urls[row])
                if data:
                    self._emit(self._thumb_in, serial, row, data)

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(one, range(len(urls))))

        threading.Thread(target=work, daemon=True).start()

    def _on_thumb(self, serial: int, row: int, data) -> None:
        item = self.grid.item(row) if serial == self._serial else None
        if item is None:
            return
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        pix = pix.scaled(THUMB_W, THUMB_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        pix = pix.copy((pix.width() - THUMB_W) // 2, (pix.height() - THUMB_H) // 2, THUMB_W, THUMB_H)
        item.setIcon(QIcon(pix))

    # ---- choosing ----------------------------------------------------------------------------------------
    def _selection_changed(self) -> None:
        self.use_btn.setEnabled(bool(self.grid.selectedItems()))

    def _use(self) -> None:
        items = self.grid.selectedItems()
        if not items or not self.use_btn.isEnabled():
            return
        url = str(items[0].data(Qt.ItemDataRole.UserRole))
        self.use_btn.setEnabled(False)
        self.go.setEnabled(False)
        self._say("Downloading the poster...")

        def work() -> None:
            self._emit(self._full_in, rc.fetch_image(url))

        threading.Thread(target=work, daemon=True).start()

    def _on_full(self, data) -> None:
        if not data:
            self.go.setEnabled(True)
            self.use_btn.setEnabled(bool(self.grid.selectedItems()))
            self._say("Couldn't download that poster. Try another one.", "bad")
            return
        self.chosen = data
        self.accept()


# ---- the Add / Edit window -------------------------------------------------------------------------------------
class AddRecompDialog(FramelessDialog):
    _looked = Signal(object)  # an rc.RepoReport, sent from the worker thread

    def __init__(self, parent: QWidget, store: rc.Store, systems: list[str], existing: rc.Recomp | None = None):
        super().__init__(parent, "Edit recomp" if existing else "Add recomp", "edit" if existing else "gamepad", (820, 720), (700, 520))
        self.store, self.existing = store, existing
        self.entry: dict | None = None  # the checked entry, once the person presses Add / Save
        self.cover_changed = False  # a cover was chosen or taken off (set once Add / Save went through)
        self.cover_path: Path | None = None  # where a newly chosen cover was kept
        self._rep: rc.RepoReport | None = None
        self._alive = True
        self._cover_data: bytes | None = None  # a cover chosen in this window, kept when Add / Save is pressed
        self._cover_off = False  # the cover that is kept on disk is to be taken off
        self._cover_on_disk = False  # there is a cover for this project on disk
        self._touched: set[str] = set(["name", "game", "system", "description", "website"] if existing else [])  # fields the person has typed in
        self.setWindowTitle(f"Edit {existing.name}" if existing else "Add a recomp")
        outer = self.content

        # the body scrolls, so opening Additional details never makes the window taller than the screen
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("recompBody")
        content.setStyleSheet("#recompBody { background: transparent; }")
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self._scroll = scroll
        self._to_top: QVariantAnimation | None = None
        body = QVBoxLayout(content)
        body.setContentsMargins(20, 18, 20, 18)
        body.setSpacing(18)

        top = QHBoxLayout()
        top.setSpacing(24)
        body.addLayout(top)

        # left: the poster and what can be done with it
        left = QVBoxLayout()
        left.setSpacing(10)
        self.poster = PosterBox()
        left.addWidget(self.poster, 0, Qt.AlignmentFlag.AlignHCenter)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        tools.addStretch(1)
        self.search_btn = _icon_button("search", "Search SteamGridDB for a cover")
        self.upload_btn = _icon_button("upload", "Upload a cover from this computer")
        self.delete_btn = _icon_button("trash", "Remove the cover", danger=True)
        self.search_btn.clicked.connect(self._search_cover)
        self.upload_btn.clicked.connect(self._upload_cover)
        self.delete_btn.clicked.connect(self._remove_cover)
        for b in (self.search_btn, self.upload_btn, self.delete_btn):
            tools.addWidget(b)
        tools.addStretch(1)
        left.addLayout(tools)
        left.addStretch(1)
        top.addLayout(left)

        # right: the repository, then what Check fills in
        right = QVBoxLayout()
        right.setSpacing(12)
        top.addLayout(right, 1)
        self.repo = QLineEdit()
        self.repo.setPlaceholderText("github.com/owner/name")
        self.repo.setClearButtonEnabled(True)
        self.repo.textChanged.connect(self._repo_edited)
        self.repo.returnPressed.connect(self._look)
        self.look_btn = accent_button("Check")
        self.look_btn.setAutoDefault(False)
        self.look_btn.setToolTip("Look at the repository: it fills in the name, game, platform and type where it can, and shows which downloads it has.")
        self.look_btn.clicked.connect(self._look)
        repo_row = QHBoxLayout()
        repo_row.setSpacing(8)
        repo_row.setContentsMargins(0, 0, 0, 0)
        repo_row.addWidget(self.repo, 1)
        repo_row.addWidget(self.look_btn)
        repo_holder = QWidget()
        repo_holder.setLayout(repo_row)
        right.addLayout(_stack("GitHub repository", repo_holder))
        right.addWidget(hint("For a project that isn't listed yet. Distill downloads it from the repository's releases, checks it against the published "
                             "checksum when there is one, and tells you when there's an update, like any other project. Only add projects you trust."))
        self.evidence = QLabel()
        self.evidence.setWordWrap(True)
        self.evidence.setTextFormat(Qt.TextFormat.RichText)
        right.addWidget(self.evidence)

        self.name = self._field("Name", "The project's name, e.g. Ship of Harkinian", "name", right)
        self.game = self._field("Game name", "The game it plays, e.g. The Legend of Zelda: Ocarina of Time", "game", right)
        self.system = QComboBox()
        self.system.setEditable(True)
        self.system.addItems(systems)
        self.system.setCurrentText("")
        self.system.lineEdit().setPlaceholderText("e.g. Nintendo 64")
        self.system.lineEdit().textEdited.connect(lambda _t: self._touched.add("system"))
        self.kind = QComboBox()
        self.kind.addItems([*rc.CUSTOM_KINDS, OTHER])
        self.kind.setToolTip("Static recompilation turns the game's own code into a PC program; a decompilation rebuilds its source; a source port or "
                             "engine remake re-implements it.")
        pair = QHBoxLayout()
        pair.setSpacing(12)
        pair.addLayout(_stack("Platform", self.system), 1)
        pair.addLayout(_stack("Recomp type", self.kind), 1)
        right.addLayout(pair)
        self.desc = QPlainTextEdit()
        self.desc.setPlaceholderText("What is it? One or two lines (optional).")
        self.desc.setFixedHeight(92)
        self.desc.textChanged.connect(lambda: self._touched.add("description"))
        right.addLayout(_stack("About", self.desc))
        right.addStretch(1)

        # the rest, folded away
        self.extra = Expander("Additional details", expanded=False)
        self.extra.header.toggled.connect(self._extra_toggled)
        self.extra.header.setToolTip("Which systems it runs on, which download to pick, which game dump it needs, links, and a note for first launch.")
        self._build_extra()
        body.addWidget(self.extra)
        body.addStretch(1)

        # the answer
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
        self.add_btn = accent_button("Save" if existing else "Add recomp")
        self.add_btn.setAutoDefault(False)
        self.add_btn.setMinimumWidth(110)
        self.add_btn.clicked.connect(self._accept)
        foot.addWidget(cancel)
        foot.addWidget(self.add_btn)
        outer.addLayout(foot)
        self._looked.connect(self._on_looked)
        if existing:
            self._fill(existing)
        self._cover_buttons()

    # ---- building the form ----------------------------------------------------------------------------------
    def _field(self, caption: str, placeholder: str, key: str, into: QVBoxLayout) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.textEdited.connect(lambda _t, k=key: self._touched.add(k))
        into.addLayout(_stack(caption, edit))
        return edit

    def _plain(self, caption: str, placeholder: str, into=None) -> QLineEdit:
        """A field for Additional details: into a row of fields, or (with none given) on a row of its own."""
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        (into.addLayout if into is not None else self.extra.add)(_stack(caption, edit))
        return edit

    def _build_extra(self) -> None:
        """What was 'More options', sorted into groups under Additional details."""
        add = self.extra.add
        add(_group_title("Runs on"))
        plats = QHBoxLayout()
        plats.setSpacing(18)
        self.platforms: dict[str, QCheckBox] = {}
        for key, label in rc.PLATFORM_NAMES.items():
            box = QCheckBox(label)
            box.setChecked(True)
            self.platforms[key] = box
            plats.addWidget(box)
        plats.addStretch(1)
        add(plats)
        add(_group_title("Which download", "Distill picks a file by its name. Only if the wrong one is picked, give a pattern for the right file's name."))
        files = QHBoxLayout()
        files.setSpacing(12)
        self.patterns: dict[str, QLineEdit] = {}
        hints = {"windows": r"e.g. .*win.*\.zip", "linux": r"e.g. .*linux.*\.tar\.gz", "macos": r"e.g. .*mac.*\.zip"}
        for key, label in rc.PLATFORM_NAMES.items():
            self.patterns[key] = self._plain(f"{label} download", hints[key], files)
        add(files)
        self.exe = self._plain("Program", "Only if the wrong program is started: a pattern for its file name")
        add(_group_title("Game dump", "The copy of the game the project needs. Distill uses it to tell you which of your games it can play."))
        self.rom_names = self._plain("Name contains", "Words its file name has, e.g. ocarina time; ocarina of time")
        dump = QHBoxLayout()
        dump.setSpacing(12)
        self.rom_exts = self._plain("File types", "e.g. .z64 .n64", dump)
        self.rom_sha1 = self._plain("SHA-1", "Checksums of dumps it accepts, to verify a match", dump)
        add(dump)
        add(_group_title("First launch and links"))
        self.setup = self._plain("First launch note", "A line of advice shown on its page, e.g. It asks for your ROM the first time")
        links = QHBoxLayout()
        links.setSpacing(12)
        self.website = self._plain("Website", "Optional: https://...", links)
        self.website.textEdited.connect(lambda _t: self._touched.add("website"))
        self.discord = self._plain("Discord", "Optional invite address", links)
        add(links)
        self.cover = self._plain("Cover image address", "Optional: a direct address of a picture (https://...), used when no cover is chosen above")

    def _fill(self, r: rc.Recomp) -> None:
        """The form for an entry that is being edited."""
        self.repo.setText(r.repo)
        self.name.setText(r.name)
        self.game.setText(r.game)
        self.system.setCurrentText(r.system)
        kind = rc.kinds_of(r)[0]
        self.kind.setCurrentIndex(max(0, self.kind.findText(kind if kind in rc.CUSTOM_KINDS else OTHER)))
        self.website.setText(r.website)
        self.desc.setPlainText(r.description)
        for key, box in self.platforms.items():
            box.setChecked(key in r.platforms)
        for key, edit in self.patterns.items():
            edit.setText(r.asset.get(key, ""))
        self.exe.setText(r.exe)
        self.rom_names.setText("; ".join(" ".join(n) for n in r.rom.names))
        self.rom_exts.setText(" ".join(r.rom.exts))
        self.rom_sha1.setText(" ".join(r.rom.sha1))
        self.setup.setText(r.setup)
        self.discord.setText(r.discord)
        self.cover.setText(r.cover)
        self._touched = {"name", "game", "system", "description", "website"}
        if any(self.patterns[k].text() for k in self.patterns) or r.exe or r.rom.names or r.rom.exts or r.rom.sha1 or r.setup or r.discord or r.cover \
                or r.website or set(r.platforms) != set(rc.PLATFORM_NAMES):
            self.extra.header.setChecked(True)  # something is set in there: show it
        if (path := rc.cached_cover(r)) is not None:
            pix = QPixmap(str(path))
            if not pix.isNull():
                self.poster.set_pixmap(pix)
                self._cover_on_disk = True

    def _extra_toggled(self, open_: bool) -> None:
        """Closing Additional details glides the page back up to the top, instead of leaving it scrolled to blank space."""
        if self._to_top is not None:
            self._to_top.stop()
            self._to_top = None
        bar = self._scroll.verticalScrollBar()
        if open_ or bar.value() <= 0:
            return
        if not motion.on() or not self.isVisible():
            bar.setValue(0)
            return
        anim = QVariantAnimation(self, duration=320)
        anim.setStartValue(bar.value())
        anim.setEndValue(0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda v: bar.setValue(int(v)))
        anim.finished.connect(lambda: bar.setValue(0))
        self._to_top = anim
        anim.start()

    # ---- the cover --------------------------------------------------------------------------------------------
    def _cover_buttons(self) -> None:
        """There is something to remove only while a cover shows."""
        self.delete_btn.setEnabled(self.poster.has_picture())

    def _show_cover(self, data: bytes) -> None:
        pix = QPixmap()
        if not pix.loadFromData(data):
            self._say("Distill couldn't show that picture.")
            return
        self.poster.set_pixmap(pix)
        self._cover_data, self._cover_off = data, False
        self.problem.setVisible(False)
        self._cover_buttons()

    def _search_cover(self) -> None:
        key = self.store.sgdb_key()
        if not key:
            self._say("Searching SteamGridDB needs a free API key. Add yours under Settings > API keys > Games & artwork, then try again.", "warn")
            return
        dlg = CoverSearchDialog(self, key, self.game.text().strip() or self.name.text().strip())
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
            self._cover_off = True  # the one that is kept goes when Save is pressed, and none is looked for afterwards
        self._cover_data = None
        self.poster.set_pixmap(None)
        self.poster.set_note(REMOVED_NOTE if self._cover_off else EMPTY_NOTE)
        self.problem.setVisible(False)
        self._cover_buttons()

    # ---- looking at the repository --------------------------------------------------------------------------
    def _repo_edited(self, _text: str = "") -> None:
        self._rep = None
        self.evidence.setText("")
        self.problem.setVisible(False)

    def _look(self) -> None:
        text = self.repo.text()
        if not text.strip():
            return
        self.look_btn.setEnabled(False)
        self.look_btn.setText("Checking...")
        self.evidence.setText(f'<span style="color:{T.fg3.name()}">Looking at it...</span>')

        def work() -> None:
            rep = rc.inspect_repo(text, self.store)
            if self._alive:
                try:
                    self._looked.emit(rep)
                except RuntimeError:  # the window is gone
                    pass

        threading.Thread(target=work, daemon=True).start()

    def done(self, result: int) -> None:
        self._alive = False
        super().done(result)

    def _on_looked(self, rep: rc.RepoReport) -> None:
        self.look_btn.setEnabled(True)
        self.look_btn.setText("Check")
        self._rep = rep
        ok, warn, bad, mute = (c.name() for c in (T.ok, T.warn, T.bad, T.fg3))
        if rep.error:
            self.evidence.setText(f'<span style="color:{bad}">{rep.error}</span>')
            return
        lines = []
        if rep.downloads:
            found = [f"{rc.PLATFORM_NAMES[p]}: {rep.downloads[p][0]}" for p in rc.PLATFORM_NAMES if p in rep.downloads]
            missing = [rc.PLATFORM_NAMES[p] for p in rc.PLATFORM_NAMES if p not in rep.downloads]
            lines.append(f'<span style="color:{ok}">Download found</span> in {rep.release}' + (f", {rep.released}" if rep.released else "") + "<br>" + "<br>".join(found))
            if missing:
                lines.append(f'<span style="color:{mute}">Nothing found for {", ".join(missing)}. Distill picks files by name; if one is missed, '
                             "set the pattern under Additional details.</span>")
        else:
            lines.append(f'<span style="color:{warn}">No download found yet.</span> You can still add it: Distill will pick one up as soon as a release '
                         "has a file for this PC.")
        lines.append(f'<span style="color:{mute}">★ {rep.stars:,}  ·  last push {rep.pushed or "?"}</span>')
        if rep.prerelease_only:
            lines.append(f'<span style="color:{warn}">Only pre-releases are published.</span>')
        if rep.archived:
            lines.append(f'<span style="color:{bad}">This repository is archived: it no longer receives builds.</span>')
        self.evidence.setText("<br>".join(lines))
        # fill in only what the person hasn't typed, so Check never overwrites their words
        if "name" not in self._touched and rep.name:
            self.name.setText(rep.name)
        if "game" not in self._touched and rep.game:
            self.game.setText(rep.game)
        if "system" not in self._touched and rep.system:
            self.system.setCurrentText(rep.system)
        if rep.kind and not self.existing:
            self.kind.setCurrentIndex(max(0, self.kind.findText(rep.kind)))
        if "description" not in self._touched and rep.description:
            self.desc.blockSignals(True)
            self.desc.setPlainText(rep.description)
            self.desc.blockSignals(False)
        if "website" not in self._touched and rep.website:
            self.website.setText(rep.website)

    # ---- the answer -----------------------------------------------------------------------------------------
    def _say(self, text: str, kind: str = "bad") -> None:
        colour = {"bad": T.bad, "warn": T.warn, "ok": T.ok}[kind].name()
        self.problem.setText(f'<span style="color:{colour}">{text}</span>')
        self.problem.setVisible(True)

    def _accept(self) -> None:
        try:
            self.entry = rc.custom_entry(
                self.repo.text(), self.name.text(), self.game.text(), self.system.currentText(), kind=self.kind.currentText(),
                description=self.desc.toPlainText(), website=self.website.text(), discord=self.discord.text(),
                platforms=[k for k, b in self.platforms.items() if b.isChecked()], setup=self.setup.text(), cover=self.cover.text(),
                asset={k: e.text() for k, e in self.patterns.items()}, exe=self.exe.text(), rom_names=self.rom_names.text(),
                rom_exts=self.rom_exts.text(), rom_sha1=self.rom_sha1.text(), rid=self.existing.id if self.existing else "")
            if self._cover_data is not None:  # a cover chosen here is kept under this project's id; nothing replaces it later
                self.cover_path = rc.set_cover(self.entry["id"], self._cover_data)
                self.cover_changed = True
            elif self._cover_off:
                rc.clear_cover(self.entry["id"])
                self.cover_changed = True
        except rc.RecompError as exc:
            self.entry = None
            self._say(str(exc))
            return
        self.accept()
