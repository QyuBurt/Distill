"""The Media and Emulator tabs of the Edit game window.

Media: one row for each kind of picture a game has (cover, background, disc or cartridge, screenshots, manual). Every row has the same
shape: the picture in a box of one size (click it to see it large), then a text box of one length saying what the game has and where it came
from, with Look up (find one online), Add (choose a file of your own), a bin (take it away) and Automatic (go back to finding one). Nothing
is saved before Save is pressed: choices are collected in a `gamemedia.MediaPlan`, which the page carries out. A 'Find missing' button looks for
every picture the game still lacks, with a bar that says which one is being fetched.

Emulator: which emulator starts this game (or Automatic), whether that is to become the default for the platform, and the launch options of an
emulator of its own, with a way into the Platform setup window.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeyEvent, QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import gamemedia as mm
from .. import games as gm
from . import icons
from .frameless import FramelessDialog, hairline
from .recomp_dialog import _caption, _icon_button, _stack
from . import shimmermath
from .shimmer import Shimmer, busy
from .theme import T
from .widgets import AnimCheckBox, accent_button, hint, symbolize

PICTURES = "Pictures (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;All files (*)"
PREVIEW_W, PREVIEW_H = 128, 96  # the box every picture sits in, whatever its shape
SHOT_W, SHOT_H = 72, 48  # the small tiles under the screenshots row
SHOT_SHOWN = 5  # tiles shown in the screenshots row; the rest are counted
BTN_W, BTN_H = 40, 34  # every button on a row is a symbol of this one size, so the rows line up


def tabs_css() -> str:
    """Text tabs on a hairline, as on the Media page."""
    return (f"QTabWidget::pane {{ border: none; border-top: 1px solid {T.card_border.name()}; }} "
            f"QTabWidget::tab-bar {{ left: 14px; }} "
            f"QTabBar {{ background: transparent; }} "
            f"QTabBar::tab {{ background: transparent; color: {T.fg3.name()}; border: none; padding: 9px 18px; margin: 0; font-weight: 600; }} "
            f"QTabBar::tab:hover {{ color: {T.fg.name()}; }} "
            f"QTabBar::tab:selected {{ color: {T.title.name()}; }}")


def _pixmap_of(data: bytes | None = None, path: Path | None = None) -> QPixmap | None:
    pix = QPixmap()
    ok = pix.loadFromData(data) if data is not None else (path is not None and pix.load(str(path)))
    return pix if ok and not pix.isNull() else None


def _fit(pix: QPixmap | None, w: int, h: int) -> QPixmap | None:
    return pix.scaled(w * 2, h * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation) if pix is not None else None


def _frame_css() -> str:
    return f"background: {T.input.name()}; border: 1px solid {T.card_border.name()}; border-radius: 8px; color: {T.fg3.name()};"


class _Clickable(QLabel):
    """A label that says when it is clicked."""
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _set_picture(label: QLabel, pix: QPixmap | None, empty: str = "") -> None:
    """Fit `pix` in `label`, whole and centred (never stretched); with none, the label's own words (or nothing)."""
    if pix is None:
        label.clear()
        label.setText(empty)
        return
    w, h = label.width() - 2, label.height() - 2
    scaled = pix.scaled(w * 2, h * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(2.0)
    label.setPixmap(scaled)


class _PreviewBox(_Clickable):
    """The box a picture sits in: the same size for every row. With a picture in it, a click opens it large."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        T.bind_css(self, _frame_css)

    def show_picture(self, pix: QPixmap | None, empty: str = "No picture") -> None:
        _set_picture(self, pix, empty)
        self.set_zoomable(pix is not None)

    def show_icon(self, pix: QPixmap) -> None:
        """A symbol standing for something that is not a picture (the manual): not something to enlarge."""
        self.setPixmap(pix)
        self.set_zoomable(False)

    def set_zoomable(self, on: bool) -> None:
        self.setCursor(Qt.CursorShape.PointingHandCursor if on else Qt.CursorShape.ArrowCursor)
        self.setToolTip("Click to see it larger" if on else "")


class _Row(QFrame):
    """One kind of picture: its box, its name, a text box saying what the game has, and the buttons. Every row is laid out the same way, so
    the boxes, the text boxes and the buttons line up from one row to the next."""

    def __init__(self, title: str, tip: str = ""):
        super().__init__()
        self.setObjectName("mediaRow")
        T.bind_css(self, lambda: f"#mediaRow {{ background: {T.card.name()}; border: 1px solid {T.card_border.name()}; border-radius: 12px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(14)
        self.preview = _PreviewBox()
        lay.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignTop)
        self.mid = QVBoxLayout()  # the name, then the text box and buttons (the screenshots row adds its small tiles under them)
        self.mid.setSpacing(8)
        self.title = QLabel(title)
        T.bind_css(self.title, lambda: f"color: {T.title.name()}; font-weight: 600; background: transparent;")
        self.mid.addWidget(self.title)
        self.field = QLineEdit()  # says what the game has; it is read, not typed in (the buttons change it)
        self.field.setReadOnly(True)
        self.field.setFixedHeight(BTN_H)
        self.field.setMinimumWidth(120)
        self.find_btn = _icon_button("search", f"Look up a {title.lower()} online", size=(BTN_W, BTN_H))
        self.file_btn = _icon_button("folder-open", "Add one from this computer", size=(BTN_W, BTN_H))
        self.none_btn = _icon_button("trash", f"No {title.lower()} for this game", danger=True, size=(BTN_W, BTN_H))
        self.auto_btn = _icon_button("wand", "Automatic: go back to finding one online by itself", size=(BTN_W, BTN_H))
        for button in (self.find_btn, self.auto_btn):  # (hidden where a row has no use for it, but its room stays so the text boxes stay one length)
            policy = button.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            button.setSizePolicy(policy)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(6)
        self.buttons.addWidget(self.field, 1)
        for button in (self.find_btn, self.file_btn, self.none_btn, self.auto_btn):
            self.buttons.addWidget(button)
        self.mid.addLayout(self.buttons)
        self.mid.addStretch(1)
        lay.addLayout(self.mid, 1)
        if tip:
            self.setToolTip(tip)

    def set_state(self, text: str, tip: str = "") -> None:
        self.field.setText(text)
        self.field.setCursorPosition(0)
        self.field.setToolTip(tip or text)


class PictureZoom(FramelessDialog):
    """Pictures at a readable size. With several (the screenshots) the arrows or the left and right keys move between them, and a click shows
    the next; with one, a click closes it."""

    def __init__(self, parent: QWidget, title: str, pics: list[QPixmap], index: int = 0):
        super().__init__(parent, title, "image", (1000, 660), (520, 380))
        self.pics = [p for p in pics if p is not None and not p.isNull()]
        self.index = max(0, min(index, len(self.pics) - 1))
        self.label = _Clickable()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.label.setMinimumSize(1, 1)
        self.label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.label.clicked.connect(self._clicked)
        self.content.addWidget(self.label, 1)
        self.content.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 10, 20, 12)
        foot.setSpacing(8)
        self.prev_btn, self.next_btn = QPushButton("‹"), QPushButton("›")
        for button, step in ((self.prev_btn, -1), (self.next_btn, 1)):
            button.setAutoDefault(False)
            button.setFixedWidth(44)
            button.setVisible(len(self.pics) > 1)
            button.clicked.connect(lambda _c=False, by=step: self._step(by))
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
        self.counter.setText(f"{self.index + 1} of {len(self.pics)}" if len(self.pics) > 1 else "")
        self._fit()

    def _fit(self) -> None:
        if not self.pics:
            return
        pix = self.pics[self.index]
        room = self.label.size()
        if pix.width() > room.width() or pix.height() > room.height():  # too big for the window: made to fit (a small picture is not blown up)
            pix = pix.scaled(room, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.label.setPixmap(pix)

    def _step(self, by: int) -> None:
        if len(self.pics) > 1:
            self.index = (self.index + by) % len(self.pics)
            self._load()

    def _clicked(self) -> None:
        if len(self.pics) > 1:
            self._step(1)
        else:
            self.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Left:
            self._step(-1)
        elif event.key() == Qt.Key.Key_Right:
            self._step(1)
        else:
            super().keyPressEvent(event)


class MediaTab(QWidget):
    """The pictures of a game. `key` is the name the game's pictures are kept under; `lookup()` gives what to search for as (name, LaunchBox
    platform, platform name) from the Details tab as it is now, so a name or platform changed there is what is searched."""
    _progress = Signal(int, int, str)  # (steps done, steps in all, what is being fetched)
    _finished = Signal(int)  # sent when the fetching is over; the number says which run

    def __init__(self, key: str, lookup: Callable[[], tuple[str, str, str]], sgdb_key: str, cover_actions: dict[str, Callable[[], None]],
                 plan: mm.MediaPlan, parent: QWidget | None = None):
        super().__init__(parent)
        self.key, self.lookup, self.sgdb_key, self.cover_actions, self.plan = key, lookup, sgdb_key, cover_actions, plan
        self._cover: QPixmap | None = None
        self._run = 0
        self._alive = True
        self._new_shots: list[QPixmap | None] = []  # previews of plan.shots_add, in step with it
        self._art_src: dict[str, bytes | Path | None] = {}  # what each picture's box shows now (to be enlarged on a click)
        self._shot_src: list[bytes | Path] = []  # the screenshots shown now, in order
        self._busy: set[str] = set()  # the pictures not yet fetched by Find missing pictures: their previews shimmer
        self._steps: list[str] = []
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        host = QWidget()
        host.setObjectName("mediaBody")
        host.setStyleSheet("#mediaBody { background: transparent; }")
        scroll.setWidget(host)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scroll, 1)
        body = QVBoxLayout(host)
        body.setContentsMargins(20, 16, 20, 16)
        body.setSpacing(10)

        self.cover_row = _Row("Cover", "The poster in your library and on the game's page. It is set on this row or beside the name on the Details tab.")
        self.cover_row.preview.clicked.connect(self.zoom_cover)
        self.cover_row.find_btn.clicked.connect(self.cover_actions["find"])
        self.cover_row.file_btn.clicked.connect(self.cover_actions["file"])
        self.cover_row.none_btn.clicked.connect(self.cover_actions["remove"])
        self.cover_row.auto_btn.setVisible(False)
        body.addWidget(self.cover_row)

        self.rows: dict[str, _Row] = {}
        tips = {"back": "The wide picture behind the top of the game's page.",
                "disc": "The game's own disc, shown behind its cover on the game's page.",
                "cart": "The game's own cartridge, shown behind its cover on the game's page."}
        for stem, title in (("back", "Background"), ("disc", "Disc"), ("cart", "Cartridge")):
            row = _Row(title, tips[stem])
            row.preview.clicked.connect(lambda s=stem: self.zoom_art(s))
            row.find_btn.clicked.connect(lambda _c=False, s=stem: self.find_art(s))
            row.file_btn.clicked.connect(lambda _c=False, s=stem: self.choose_file(s))
            row.none_btn.clicked.connect(lambda _c=False, s=stem: self.stage_art(s, "off"))
            row.auto_btn.clicked.connect(lambda _c=False, s=stem: self.stage_art(s, "reset"))
            self.rows[stem] = row
            body.addWidget(row)

        self.shots_row = self._build_screenshots()
        body.addWidget(self.shots_row)
        self.manual_row = self._build_manual()
        body.addWidget(self.manual_row)
        body.addStretch(1)

        outer.addWidget(hairline())
        foot = QVBoxLayout()
        foot.setContentsMargins(20, 10, 20, 12)
        foot.setSpacing(6)
        line = QHBoxLayout()
        line.setSpacing(10)
        self.fetch_btn = QPushButton("Find missing pictures")
        self.fetch_btn.setAutoDefault(False)
        self.fetch_btn.setToolTip("Look online for the background, disc or cartridge and screenshots this game doesn't have yet.")
        self.fetch_btn.clicked.connect(self.find_missing)
        self.fetch_note = hint("", live=True)
        line.addWidget(self.fetch_btn)
        line.addWidget(self.fetch_note, 1)
        foot.addLayout(line)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setVisible(False)
        T.bind_css(self.bar, lambda: (f"QProgressBar {{ background: {T.input.name()}; border: none; border-radius: 3px; }}"
                                       f"QProgressBar::chunk {{ background: {T.accent.name()}; border-radius: 3px; }}"))
        foot.addWidget(self.bar)
        outer.addLayout(foot)
        self._progress.connect(self._on_progress)
        self._finished.connect(self._on_finished)
        self.refresh()

    def closing(self) -> None:
        self._alive = False

    # ---- the rows that are not one picture ----------------------------------------------------------------------
    def _build_screenshots(self) -> QFrame:
        row = _Row("Screenshots", "Pictures of the game, shown in a strip on its page. Click one to see it larger; right-click one to remove it.")
        row.preview.clicked.connect(lambda: self.zoom_shots(0))
        row.find_btn.setToolTip("Look up screenshots online and pick the ones you want")
        row.file_btn.setToolTip("Add screenshots from this computer")
        row.none_btn.setToolTip("Remove every screenshot")
        row.find_btn.clicked.connect(self.find_shots)
        row.file_btn.clicked.connect(self.add_shot_files)
        row.none_btn.clicked.connect(self.clear_shots)
        row.auto_btn.clicked.connect(self.reset_shots)
        self.strip = QHBoxLayout()
        self.strip.setSpacing(6)
        row.mid.insertLayout(2, self.strip)  # under the text box and its buttons
        return row

    def _build_manual(self) -> QFrame:
        row = _Row("Manual", "A PDF manual, read as a book with turning pages from the game's page.")
        row.find_btn.setVisible(False)
        row.file_btn.setToolTip("Choose a PDF manual from this computer")
        row.none_btn.setToolTip("Remove the manual")
        row.auto_btn.setVisible(False)
        row.file_btn.clicked.connect(self.choose_manual)
        row.none_btn.clicked.connect(self.clear_manual)
        return row

    # ---- showing what there is --------------------------------------------------------------------------------------
    def set_cover(self, pix: QPixmap | None) -> None:
        self._cover = pix
        self.refresh_cover()

    def refresh_cover(self) -> None:
        self.cover_row.preview.show_picture(self._cover)
        self.cover_row.set_state("The poster you see in your library" if self._cover is not None else "No cover yet",
                                 "" if self._cover is not None else "No cover yet. Look one up online or add a file.")
        self.cover_row.none_btn.setEnabled(self._cover is not None)

    def kind(self) -> str:
        return mm.media_kind(self.lookup()[2])

    def refresh(self) -> None:
        self.refresh_cover()
        kind = self.kind()
        for stem, row in self.rows.items():
            shown = stem == "back" or stem == kind  # (a platform with discs shows the disc row, one with cartridges the cartridge row)
            row.setVisible(shown)
            if shown:
                self._refresh_art(stem, row)
        self._refresh_shots()
        self._refresh_manual()

    def _staged(self, stem: str) -> tuple[str, bytes | None] | None:
        return self.plan.art.get(stem)

    def _refresh_art(self, stem: str, row: _Row) -> None:
        staged = self._staged(stem)
        state = ""
        source: bytes | Path | None = None
        if staged is not None:
            op, data = staged
            pix = _pixmap_of(data) if op == "set" else None
            source = data if op == "set" else None
            text, tip = {"set": ("A picture you chose: saved on Save", "A picture you chose. It is saved when you press Save."),
                         "off": ("None: nothing is shown or looked for", "None: nothing will be shown or looked for."),
                         "reset": ("Automatic: looked for again on Save", "Automatic: it is looked for again when you press Save.")}[op]
            auto = True
        else:
            state = mm.art_state(self.key, stem)
            path = mm._dir() / f"{stem}_{self.key}.{mm.ART_EXT[stem]}"
            pix = _pixmap_of(path=path) if state in ("custom", "auto") else None
            source = path if pix is not None else None
            text, tip = {"custom": ("Chosen by you", ""), "auto": ("Found online", ""), "off": ("None: nothing is shown or looked for", ""),
                         "none": ("Not found yet", "Not found yet. Look one up, add a file, or press Find missing pictures.")}[state]
            auto = state in ("custom", "off")
        self._art_src[stem] = source
        row.preview.show_picture(pix, "None" if (staged[0] == "off" if staged is not None else state == "off") else "No picture")
        busy(row.preview, stem in self._busy and pix is None)  # on its way: a light band sweeps the empty preview
        row.set_state(text, tip)
        row.auto_btn.setVisible(auto)
        row.none_btn.setEnabled(not (staged[0] == "off" if staged is not None else state == "off"))

    def _disk_shots(self) -> list[Path]:
        if self.plan.shots_mode == "off":
            return []
        shots = [p for p in mm.cached_screenshots(self.key) if p not in self.plan.shots_remove]
        return [p for p in shots if mm._is_custom_shot(p)] if self.plan.shots_mode == "reset" else shots

    def _refresh_shots(self) -> None:
        while self.strip.count():
            item = self.strip.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        tiles: list[tuple[QPixmap | None, Callable[[], None]]] = []
        self._shot_src = []
        for path in self._disk_shots():
            tiles.append((_pixmap_of(path=mm.shot_image(path)) if mm.shot_image(path) else None, lambda p=path: self._remove_disk_shot(p)))
            self._shot_src.append(path)
        for i, pix in enumerate(self._new_shots):
            tiles.append((pix, lambda i=i: self._remove_new_shot(i)))
            self._shot_src.append(self.plan.shots_add[i])
        for n, (pix, remove) in enumerate(tiles[:SHOT_SHOWN]):
            tile = _Clickable()
            tile.setFixedSize(SHOT_W, SHOT_H)
            tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
            T.bind_css(tile, _frame_css)
            _set_picture(tile, pix)
            tile.setCursor(Qt.CursorShape.PointingHandCursor)
            tile.clicked.connect(lambda n=n: self.zoom_shots(n))
            tile.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            tile.customContextMenuRequested.connect(lambda pos, t=tile, r=remove: self._tile_menu(t, pos, r))
            tile.setToolTip("Click to see it larger. Right-click to remove it.")
            self.strip.addWidget(tile)
        self.shots_row.preview.show_picture(tiles[0][0] if tiles else None, "No pictures")
        busy(self.shots_row.preview, not tiles and "shots" in self._busy)
        if not tiles and "shots" in self._busy:  # on their way
            for i in range(3):
                holder = Shimmer(8, shimmermath.stagger(i))
                holder.setFixedSize(SHOT_W, SHOT_H)
                self.strip.addWidget(holder)
        if len(tiles) > SHOT_SHOWN:
            more = QLabel(f"+{len(tiles) - SHOT_SHOWN} more")
            T.bind_css(more, lambda: f"color: {T.fg3.name()}; background: transparent;")
            self.strip.addWidget(more)
        self.strip.addStretch(1)
        mode = self.plan.shots_mode
        staged = bool(self.plan.shots_add or self.plan.shots_remove or mode)
        if not tiles:
            text = ("Looking for screenshots..." if "shots" in self._busy else
                    "None" if mode == "off" or mm.screenshots_off(self.key) else "Not found yet")
        else:
            text = f"{len(tiles)} screenshot{'s' if len(tiles) != 1 else ''}"
        if staged:
            text += ": changes saved on Save"
        self.shots_row.set_state(text)
        off = mm._art_marker(self.key, "shot", "off").is_file()
        self.shots_row.auto_btn.setVisible(off or bool(mode) or any(mm._is_custom_shot(p) for p in self._disk_shots()) or bool(self.plan.shots_add))
        self.shots_row.none_btn.setEnabled(bool(tiles))

    def _tile_menu(self, tile: QLabel, pos, remove: Callable[[], None]) -> None:
        menu = QMenu(self)
        menu.addAction("Remove this screenshot", remove)
        menu.exec(tile.mapToGlobal(pos))

    def _refresh_manual(self) -> None:
        op = self.plan.manual
        has = (op[0] == "set") if op is not None else mm.cached_manual(self.key) is not None
        if op is not None:
            text = f"{Path(op[1]).name}: saved on Save" if op[0] == "set" else "Removed on Save"
            tip = f"{Path(op[1]).name}: saved when you press Save." if op[0] == "set" else "Removed when you press Save."
        else:
            text = "A PDF is kept for this game" if has else "No manual yet"
            tip = "A PDF is kept for this game. Read it from the book button on the game's page." if has else "No manual yet. Add a PDF."
        self.manual_row.set_state(text, tip)
        self.manual_row.preview.show_icon(icons.pixmap("book", T.fg if has else T.fg3, 36))
        self.manual_row.none_btn.setEnabled(has)

    # ---- seeing a picture large -------------------------------------------------------------------------------------------
    def zoom_cover(self) -> None:
        if self._cover is not None:
            PictureZoom(self, "Cover", [self._cover]).exec()

    def zoom_art(self, stem: str) -> None:
        pix = self._load_source(self._art_src.get(stem))
        if pix is not None:
            PictureZoom(self, self.rows[stem].title.text(), [pix]).exec()

    def zoom_shots(self, index: int = 0) -> None:
        pics = [self._load_source(src) for src in self._shot_src]
        if any(p is not None for p in pics):
            PictureZoom(self, "Screenshots", [p for p in pics if p is not None], index).exec()

    @staticmethod
    def _load_source(source: bytes | Path | None) -> QPixmap | None:
        if source is None:
            return None
        if isinstance(source, Path):  # a screenshot's big picture may still be on its way: its small preview does until then
            return _pixmap_of(path=source if source.is_file() else mm.shot_image(source))
        return _pixmap_of(source)

    # ---- changing one picture ------------------------------------------------------------------------------------------
    def say(self, text: str) -> None:
        self.fetch_note.setText(text)

    def stage_art(self, stem: str, op: str, data: bytes | None = None) -> None:
        if op == "reset" and mm.art_state(self.key, stem) in ("none", "auto"):
            self.plan.art.pop(stem, None)  # nothing to undo on disk: just drop the staged change
        else:
            self.plan.art[stem] = (op, data)
        self._refresh_art(stem, self.rows[stem])

    def choose_file(self, stem: str) -> None:
        data = self._pick_file(f"Choose a {self.rows[stem].title.text().lower()}")
        if data is not None:
            self.stage_art(stem, "set", data)

    def _pick_file(self, title: str, many: bool = False):
        if many:
            paths, _ = QFileDialog.getOpenFileNames(self, title, "", PICTURES)
        else:
            path, _ = QFileDialog.getOpenFileName(self, title, "", PICTURES)
            paths = [path] if path else []
        out: list[bytes] = []
        for path in paths:
            try:
                out.append(mm.read_image(path))
            except mm.MediaError as exc:
                self.say(str(exc))
        return out if many else (out[0] if out else None)

    def find_art(self, stem: str) -> None:
        name, platform, _system = self.lookup()
        dlg = ArtPickerDialog(self, f"Choose a {self.rows[stem].title.text().lower()}", stem, name, platform, self.sgdb_key,
                              (264, 148) if stem == "back" else (160, 160))
        if dlg.exec() == ArtPickerDialog.DialogCode.Accepted and dlg.chosen:
            self.stage_art(stem, "set", dlg.chosen[0])

    # ---- screenshots -----------------------------------------------------------------------------------------------------
    def _remove_disk_shot(self, path: Path) -> None:
        self.plan.shots_remove.append(path)
        self._refresh_shots()

    def _remove_new_shot(self, index: int) -> None:
        if 0 <= index < len(self.plan.shots_add):
            del self.plan.shots_add[index]
            del self._new_shots[index]
        self._refresh_shots()

    def _add_shots(self, pictures: list[bytes]) -> None:
        for data in pictures:
            self.plan.shots_add.append(data)
            self._new_shots.append(_pixmap_of(data))
        self._refresh_shots()

    def add_shot_files(self) -> None:
        pictures = self._pick_file("Add screenshots", many=True)
        if pictures:
            self._add_shots(pictures)

    def find_shots(self) -> None:
        name, platform, _system = self.lookup()
        dlg = ArtPickerDialog(self, "Choose screenshots", "shots", name, platform, self.sgdb_key, (216, 122), multi=True)
        if dlg.exec() == ArtPickerDialog.DialogCode.Accepted and dlg.chosen:
            self._add_shots(dlg.chosen)

    def clear_shots(self) -> None:
        self.plan.shots_mode, self.plan.shots_remove = "off", []
        self.plan.shots_add.clear()
        self._new_shots.clear()
        self._refresh_shots()

    def reset_shots(self) -> None:
        self.plan.shots_mode, self.plan.shots_remove = "reset", []
        self._refresh_shots()

    # ---- the manual -------------------------------------------------------------------------------------------------------
    def choose_manual(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose the manual", "", "PDF files (*.pdf);;All files (*)")
        if not path:
            return
        try:
            with open(path, "rb") as f:
                ok = f.read(5) == b"%PDF-"
        except OSError:
            ok = False
        if not ok:
            self.say("That file couldn't be used as a manual. It needs to be a PDF.")
            return
        self.plan.manual = ("set", path)
        self._refresh_manual()

    def clear_manual(self) -> None:
        self.plan.manual = ("clear", "") if mm.cached_manual(self.key) is not None else None
        self._refresh_manual()

    # ---- finding what is missing ------------------------------------------------------------------------------------------
    def find_missing(self) -> None:
        name, platform, system = self.lookup()
        kind = mm.media_kind(system)
        key, sgdb = self.key, self.sgdb_key
        todo: list[tuple[str, str, Callable[[Callable[[str], None]], object]]] = []
        if self._wanted("back"):
            todo.append(("back", "the background", lambda say: mm.fetch_backdrop(key, name, platform, sgdb, retry=True)))
        if kind and self._wanted(kind):
            todo.append((kind, f"the {'disc' if kind == 'disc' else 'cartridge'} picture", lambda say: mm.fetch_media_art(key, kind, name, platform, retry=True)))
        if self._wanted_shots():
            def shots(say) -> None:
                mm.fetch_screenshots(key, name, platform, retry=True, on_update=lambda paths: say(f"Screenshots: {min(len(paths), 8)} of 8"))
            todo.append(("shots", "the screenshots", shots))
        if not todo:
            self.say("Nothing is missing. (Pictures you turned off are left alone.)")
            return
        self._run += 1
        run, total = self._run, len(todo)
        self.fetch_btn.setEnabled(False)
        self.bar.setRange(0, total * 100)
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.say(f"Looking for {todo[0][1]}... (1 of {total})")
        self._steps = [stem for stem, _what, _fetch in todo]
        self._busy = set(self._steps)
        self.refresh()

        def work() -> None:
            for i, (_stem, what, fetch) in enumerate(todo):
                if not self._alive:
                    return
                self._emit(self._progress, run * 1000 + i, total, f"Looking for {what}... ({i + 1} of {total})")
                try:
                    fetch(lambda text, i=i: self._emit(self._progress, run * 1000 + i, total, f"{text}  ({i + 1} of {total})"))
                except Exception:  # a picture that can't be fetched is just not there
                    pass
            self._emit(self._finished, run)

        threading.Thread(target=work, daemon=True).start()

    def _wanted(self, stem: str) -> bool:
        return stem not in self.plan.art and mm.art_state(self.key, stem) == "none"

    def _wanted_shots(self) -> bool:
        off = mm._art_marker(self.key, "shot", "off").is_file() or self.plan.shots_mode == "off"
        return not off and not [p for p in mm.cached_screenshots(self.key) if not mm._is_custom_shot(p)]

    def _emit(self, signal, *args) -> None:
        if self._alive:
            try:
                signal.emit(*args)
            except RuntimeError:  # the window is gone
                pass

    def _on_progress(self, code: int, total: int, text: str) -> None:
        if code // 1000 != self._run:
            return
        step = code % 1000
        self.bar.setValue(min(total * 100, step * 100 + 50))
        self.say(text)
        self._busy = set(self._steps[step:])  # (the ones before it are in, or were not to be found)
        self.refresh()

    def _on_finished(self, run: int) -> None:
        if run != self._run:
            return
        self._busy.clear()
        self.bar.setValue(self.bar.maximum())
        self.bar.setVisible(False)
        self.fetch_btn.setEnabled(True)
        self.say("Done.")
        self.refresh()


class ArtPickerDialog(FramelessDialog):
    """Pictures found online for a game, to pick from. `chosen` is the list of pictures' bytes picked (one, or several when `multi`)."""
    _found = Signal(int, object, str)  # (search number, [(address, source)] or None, what went wrong)
    _pic = Signal(int, int, object)  # (search number, row, picture bytes or None)

    def __init__(self, parent: QWidget, title: str, kind: str, game: str, platform: str, sgdb_key: str, size: tuple[int, int], multi: bool = False):
        super().__init__(parent, title, "search", (800, 620), (560, 420))
        self.chosen: list[bytes] = []
        self._alive, self._pics = True, {}
        outer = self.content
        self.status = hint("Searching...", live=True)
        self.status.setContentsMargins(20, 14, 20, 0)
        outer.addWidget(self.status)
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setIconSize(QSize(*size))
        self.grid.setGridSize(QSize(size[0] + 24, size[1] + 44))
        self.grid.setFrameShape(QFrame.Shape.NoFrame)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.grid.setSelectionMode(QListWidget.SelectionMode.MultiSelection if multi else QListWidget.SelectionMode.SingleSelection)
        T.bind_css(self.grid, lambda: (f"QListWidget {{ background: transparent; border: none; outline: 0; padding: 6px 14px; color: {T.fg3.name()}; }}"
                                       f"QListWidget::item {{ border-radius: 12px; }}"
                                       f"QListWidget::item:hover {{ background: {T.hover.name()}; }}"
                                       f"QListWidget::item:selected {{ background: {T.select.name()}; color: {T.title.name()}; }}"))
        if not multi:
            self.grid.itemClicked.connect(self._pick_one)
        outer.addWidget(self.grid, 1)
        outer.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        foot.addWidget(hint("Click the picks, then Use selected." if multi else "Click a picture to use it."))
        foot.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        foot.addWidget(cancel)
        if multi:
            use = accent_button("Use selected")
            use.setAutoDefault(False)
            use.clicked.connect(self._pick_many)
            foot.addWidget(use)
        outer.addLayout(foot)
        self._found.connect(self._on_found)
        self._pic.connect(self._on_pic)
        threading.Thread(target=self._work, args=(kind, game, platform, sgdb_key), daemon=True).start()

    def done(self, result: int) -> None:
        self._alive = False
        super().done(result)

    def _emit(self, signal, *args) -> None:
        if self._alive:
            try:
                signal.emit(*args)
            except RuntimeError:  # the window is gone
                pass

    def _work(self, kind: str, game: str, platform: str, sgdb_key: str) -> None:
        try:
            found = mm.art_candidates(kind, game, platform, sgdb_key)
        except Exception as exc:  # a page that can't be read must not take the window down
            self._emit(self._found, 0, None, f"The search failed: {exc}")
            return
        self._emit(self._found, 0, found, "")
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        def one(item: tuple[str, str]) -> bytes | None:
            try:
                return mm.fetch_image(item[0])
            except mm.MediaError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            for row, data in enumerate(pool.map(one, found)):
                if not self._alive:
                    return
                self._emit(self._pic, 0, row, data)

    def _on_found(self, _serial: int, found, error: str) -> None:
        if error:
            self.status.setText(error)
            return
        if not found:
            self.status.setText("Nothing found online for this game. Check its name and platform on the Details tab, or choose a file instead.")
            return
        self.status.setText(f"{len(found)} found.")
        for _url, src in found:
            item = QListWidgetItem(src)
            item.setSizeHint(self.grid.gridSize())
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            self.grid.addItem(item)

    def _on_pic(self, _serial: int, row: int, data) -> None:
        item = self.grid.item(row)
        if item is None:
            return
        pix = _pixmap_of(data) if data else None
        if pix is None:
            item.setText("couldn't load")
            return
        self._pics[row] = data
        item.setIcon(QIcon(_fit(pix, self.grid.iconSize().width(), self.grid.iconSize().height())))

    def _pick_one(self, item: QListWidgetItem) -> None:
        data = self._pics.get(self.grid.row(item))
        if data:
            self.chosen = [data]
            self.accept()

    def _pick_many(self) -> None:
        rows = sorted(self.grid.row(i) for i in self.grid.selectedItems())
        self.chosen = [self._pics[r] for r in rows if r in self._pics]
        if self.chosen:
            self.accept()


class EmulatorTab(QWidget):
    """Which emulator starts this game, and its options. `info_for(system)` says what can play a platform (see games.emulator_options);
    `manage()` opens the Platform setup window, after which the lists are read again."""

    def __init__(self, choice: str, system: Callable[[], str], info_for: Callable[[str], gm.EmulatorOptions], manage: Callable[[], None] | None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.start_choice, self.system, self.info_for, self.manage = choice, system, info_for, manage
        self.info: gm.EmulatorOptions | None = None
        self._offer = False  # 'use it for every game on the platform' is on offer (the tick box can be hidden by being on another tab)
        self._args: dict[str, str] = {}  # what was typed for each emulator of its own (kept while switching between them)
        self._args_seen: str = ""  # the emulator the options box is showing
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        self.play_with = QComboBox()
        self.play_with.setMinimumHeight(38)
        self.play_with.activated.connect(lambda _i: self._changed())
        lay.addLayout(_stack("Play this game with", self.play_with))
        self.note = hint("", live=True)
        lay.addWidget(self.note)
        self.make_default = AnimCheckBox("")
        lay.addWidget(self.make_default)
        self.args_box = QWidget()
        box = QVBoxLayout(self.args_box)
        box.setContentsMargins(0, 6, 0, 0)
        box.setSpacing(6)
        self.args_caption = _caption("Launch options")
        self.args_edit = QLineEdit()
        self.args_edit.setClearButtonEnabled(True)
        self.args_edit.textEdited.connect(self._typed)
        reset = QPushButton("Use Distill's own")
        reset.setAutoDefault(False)
        reset.setToolTip("Go back to the options Distill starts this emulator with")
        symbolize(reset, "undo", "Use Distill's own options")
        reset.clicked.connect(self._reset_args)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.args_edit, 1)
        row.addWidget(reset)
        box.addWidget(self.args_caption)
        box.addLayout(row)
        self.args_hint = hint("What goes after the program to start a game. {rom} is the game's file, {dir} its folder and {name} its name without the "
                              "extension. These options apply to every game this emulator plays.")
        box.addWidget(self.args_hint)
        lay.addWidget(self.args_box)
        lay.addStretch(1)
        foot = QHBoxLayout()
        self.manage_btn = QPushButton("Platform setup...")
        self.manage_btn.setAutoDefault(False)
        self.manage_btn.setToolTip("Choose each platform's default emulator and see which BIOS files are missing. Install emulators on the Emulators page.")
        self.manage_btn.clicked.connect(self._manage)
        symbolize(self.manage_btn, "settings", "Platform setup: choose each platform's default emulator and see which BIOS files are missing. Install emulators on the Emulators page.")
        self.manage_btn.setVisible(manage is not None)
        foot.addWidget(self.manage_btn)
        foot.addStretch(1)
        lay.addLayout(foot)
        self.refresh()

    # ---- what the tab holds ----------------------------------------------------------------------------------------------
    def choice(self) -> str:
        return str(self.play_with.currentData() or "")

    def default_requested(self) -> bool:
        return self._offer and self.make_default.isChecked()

    def launch_args(self) -> dict[str, str]:
        """The launch options that were changed here: emulator id -> its options ('' = Distill's own)."""
        out: dict[str, str] = {}
        if self.info is None:
            return out
        for emu, text in self._args.items():
            if emu not in self.info.args:
                continue
            _name, used, own = self.info.args[emu]
            text = text.strip()
            if text != used and not (text == "" and used == own):
                out[emu] = "" if text in ("", own) else text
        return out

    # ---- keeping the lists right ------------------------------------------------------------------------------------------
    def refresh(self) -> None:
        """Read what can play the platform again (after the platform was changed, or Platform setup was used)."""
        keep = self.choice() if self.info is not None else self.start_choice
        system = self.system()
        self.info = self.info_for(system)
        info = self.info
        combo = self.play_with
        combo.blockSignals(True)
        combo.clear()
        if not info.playable:
            combo.addItem("Nothing installed for this platform", "")
        else:
            combo.addItem(f"Automatic  ({info.auto_label})", "")
            for value, label in info.choices:
                combo.addItem(label + ("  (platform default)" if value == info.default else ""), value)
            combo.setCurrentIndex(max(0, combo.findData(keep)))
        combo.setEnabled(info.playable)
        combo.blockSignals(False)
        self._changed()

    def _changed(self) -> None:
        info = self.info
        if info is None:
            return
        choice = self.choice()
        system = self.system() or "this platform"
        self.note.setText(info.problem if not info.playable else
                          (f"Automatic follows the platform's default, which is {info.label_of(info.default) or info.auto_label}." if not choice else
                           f"Only this game is started with {info.label_of(choice) or choice}; other {system} games follow the platform's default."))
        offer = self._offer = bool(choice) and choice != info.default
        self.make_default.setVisible(offer)
        self.make_default.setText(f"Use {info.label_of(choice)} for every {system} game" if offer else "")
        if not offer:
            self.make_default.setChecked(False)
        emu = info.effective(choice)
        standalone = bool(emu) and emu != gm.RETROARCH and emu in info.args
        self.args_box.setVisible(standalone)
        if standalone and emu != self._args_seen:
            name, used, own = info.args[emu]
            self._args_seen = emu
            self.args_caption.setText(f"Launch options for {name}")
            self.args_edit.setText(self._args.get(emu, used))
            self.args_edit.setPlaceholderText(own)

    def _typed(self, text: str) -> None:
        if self._args_seen:
            self._args[self._args_seen] = text

    def _reset_args(self) -> None:
        if self.info is not None and self._args_seen in self.info.args:
            own = self.info.args[self._args_seen][2]
            self.args_edit.setText(own)
            self._args[self._args_seen] = own

    def _manage(self) -> None:
        if self.manage is not None:
            self.manage()
            self.refresh()
