"""One album in the Soundtrack Grabber, laid out like a music player's album page: the cover, title and buttons on the
left, the track list on the right. A track shows its number until the pointer is over it, then a play button, a save
button and a tick box."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout,
                               QWidget)

from .theme import T
from .trackrow import PlayToggle
from .widgets import AnimCheckBox

COVER = 240  # the cover's side, in pixels
LEFT_W = 264  # the left column
ROW_H = 46  # a track row
FIRST_SHOWN = 60  # tracks drawn before 'Show all'


@dataclass(slots=True)
class TrackInfo:
    number: str  # as shown in the first column
    title: str
    detail: str  # under the title: format and size
    checked: bool = False


class Cover(QWidget):
    """The cover, cropped square with rounded corners. A grey tile with a note stands in until it arrives."""

    def __init__(self, side: int = COVER):
        super().__init__()
        self._pix: QPixmap | None = None
        self.setFixedSize(side, side)

    def set_pixmap(self, pix: QPixmap | None) -> None:
        self._pix = pix
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, 10, 10)
        p.setClipPath(clip)
        if self._pix is None or self._pix.isNull():
            p.fillRect(rect, T.card_border)
            p.setPen(QPen(T.fg3, 2))
            c = rect.center()  # a music note
            p.drawEllipse(QPointF(c.x() - 10, c.y() + 18), 9, 7)
            p.drawLine(QPointF(c.x() - 2, c.y() + 16), QPointF(c.x() - 2, c.y() - 22))
            p.drawLine(QPointF(c.x() - 2, c.y() - 22), QPointF(c.x() + 16, c.y() - 14))
            return
        side = self.width()
        scaled = self._pix.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                  Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap(0, 0, scaled, (scaled.width() - side) // 2, (scaled.height() - side) // 2, side, side)


class RoundPlay(QAbstractButton):
    """The big round play / pause button under the cover."""

    SIZE = 56

    def __init__(self):
        super().__init__()
        self._playing = False
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_playing(self, on: bool) -> None:
        self._playing = on
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        fill = QColor(T.title)
        if self.underMouse():
            fill = fill.darker(112)
        p.setBrush(fill)
        p.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        p.setBrush(T.bg)
        c = self.rect().center()
        if self._playing:
            p.drawRoundedRect(QRectF(c.x() - 9, c.y() - 10, 6, 20), 1.5, 1.5)
            p.drawRoundedRect(QRectF(c.x() + 3, c.y() - 10, 6, 20), 1.5, 1.5)
        else:
            tri = QPainterPath(QPointF(c.x() - 7, c.y() - 11))
            tri.lineTo(QPointF(c.x() + 11, c.y()))
            tri.lineTo(QPointF(c.x() - 7, c.y() + 11))
            tri.closeSubpath()
            p.drawPath(tri)


def _round_button(text: str, tip: str) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setFixedSize(40, 40)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    T.bind_css(b, lambda: f"QToolButton {{ background: {T.btn[0].name()}; border: none; border-radius: 20px; "
                          f"color: {T.fg.name()}; font-size: 15pt; }} "
                          f"QToolButton:hover {{ background: {T.btn_hover[0].name()}; }} "
                          f"QToolButton::menu-indicator {{ image: none; }}")
    return b


class TrackLine(QWidget):
    """[number | play]  title / detail ........ [save] [tick]"""

    play = Signal()
    save = Signal()
    ticked = Signal(bool)
    menu = Signal(object)  # a QPoint, global

    def __init__(self, info: TrackInfo, can_play: bool):
        super().__init__()
        self._current = False
        self._hover = False
        self.setFixedHeight(ROW_H)
        self.setMouseTracking(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 10, 0)
        lay.setSpacing(10)
        self.index = QLabel(info.number)
        self.index.setFixedWidth(32)
        self.index.setAlignment(Qt.AlignmentFlag.AlignCenter)
        T.bind_css(self.index, lambda: f"color: {T.fg3.name()};")
        self.index.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.playbtn = PlayToggle() if can_play else None
        cell = QWidget()
        cell.setFixedSize(32, 24)
        cl = QHBoxLayout(cell)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.index)
        if self.playbtn is not None:
            self.playbtn.setParent(cell)
            self.playbtn.move(5, 1)
            self.playbtn.hide()
            self.playbtn.clicked.connect(self.play)
        lay.addWidget(cell)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel(info.title)
        self.title.setToolTip(info.title)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.sub = QLabel(info.detail)
        self.sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sub.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        T.bind_css(self.sub, lambda: f"color: {T.fg3.name()}; font-size: 9pt;")
        self._style_title()
        text.addStretch(1)
        text.addWidget(self.title)
        text.addWidget(self.sub)
        text.addStretch(1)
        lay.addLayout(text, 1)
        self._title_text, self._sub_text = info.title, info.detail
        self.savebtn = QToolButton()
        self.savebtn.setText("\u2193")
        self.savebtn.setToolTip("Save this track")
        self.savebtn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.savebtn.setFixedSize(30, 30)
        T.bind_css(self.savebtn, lambda: f"QToolButton {{ background: transparent; border: none; border-radius: 15px; "
                                         f"color: {T.fg2.name()}; font-size: 13pt; }} "
                                         f"QToolButton:hover {{ background: {T.hover.name()}; color: {T.title.name()}; }}")
        self.savebtn.clicked.connect(self.save)
        self.savebtn.hide()
        lay.addWidget(self.savebtn)
        self.check = AnimCheckBox("")
        self.check.setToolTip("Tick to save with 'Save Selected Soundtracks'")
        self.check.setChecked(info.checked)
        self.check.toggled.connect(self.ticked)
        self.check.toggled.connect(lambda _on: self._show_hover())
        lay.addWidget(self.check)
        self._show_hover()

    # ---- look -----------------------------------------------------------------------------------
    def _style_title(self) -> None:
        on = self._current
        T.bind_css(self.title, lambda: f"color: {T.accent_text.name() if on else T.title.name()}; "
                                       f"font-weight: {600 if on else 500};")

    def _show_hover(self) -> None:
        show = self._hover or self._current
        if self.playbtn is not None:
            self.playbtn.setVisible(show)
            self.index.setVisible(not show)
        self.savebtn.setVisible(self._hover)
        self.check.setVisible(self._hover or self.check.isChecked())

    def set_current(self, on: bool, playing: bool = False) -> None:
        if on != self._current:
            self._current = on
            self._style_title()
            self._show_hover()
            self.update()
        if self.playbtn is not None:
            self.playbtn.set_playing(on and playing)

    def set_checked(self, on: bool) -> None:
        self.check.blockSignals(True)
        self.check.setChecked(on)
        self.check._t = 1.0 if on else 0.0
        self.check.blockSignals(False)
        self.check.update()
        self._show_hover()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        room = max(self.title.width(), 40)
        self.title.setText(self.title.fontMetrics().elidedText(self._title_text, Qt.TextElideMode.ElideRight, room))
        self.sub.setText(self.sub.fontMetrics().elidedText(self._sub_text, Qt.TextElideMode.ElideRight, room))

    def enterEvent(self, event) -> None:
        self._hover = True
        self._show_hover()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self._show_hover()
        self.update()
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.playbtn is not None:
            self.play.emit()

    def contextMenuEvent(self, event) -> None:
        self.menu.emit(event.globalPos())

    def paintEvent(self, _event) -> None:
        if not (self._hover or self._current):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        if self._current:
            wash = QLinearGradient(0, 0, self.width(), 0)
            start, end = T.wash(60, 18)
            wash.setColorAt(0.0, start)
            wash.setColorAt(1.0, end)
            p.setBrush(wash)
        else:
            p.setBrush(QBrush(T.hover))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0, 1, 0, -1), 8, 8)


class AlbumView(QWidget):
    """The whole block for one album."""

    playAlbum = Signal()
    saveAlbum = Signal()
    playTrack = Signal(int)
    saveTrack = Signal(int)
    trackMenu = Signal(int, object)
    tickChanged = Signal()
    moreMenu = Signal(object)  # the more button, for the owner to pop a menu from

    def __init__(self, can_play: bool):
        super().__init__()
        self._can_play = can_play
        self._lines: list[TrackLine] = []
        self._infos: list[TrackInfo] = []
        self._shown_all = False
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 14, 4, 14)
        root.setSpacing(28)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)
        self.cover = Cover()
        ll.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignHCenter)
        ll.addSpacing(12)
        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        T.bind_css(self.title, lambda: f"color: {T.title.name()}; font-size: 15pt; font-weight: 700;")
        ll.addWidget(self.title)
        self.subtitle = QLabel("")
        self.subtitle.setWordWrap(True)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        T.bind_css(self.subtitle, lambda: f"color: {T.fg2.name()};")
        ll.addWidget(self.subtitle)
        self.meta = QLabel("")
        self.meta.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        T.bind_css(self.meta, lambda: f"color: {T.fg3.name()};")
        ll.addWidget(self.meta)
        ll.addSpacing(10)
        buttons = QHBoxLayout()
        buttons.setSpacing(14)
        buttons.addStretch(1)
        self.save_btn = _round_button("\u2193", "Save this album")
        self.save_btn.clicked.connect(self.saveAlbum)
        self.play_btn = RoundPlay()
        self.play_btn.clicked.connect(self.playAlbum)
        self.more_btn = _round_button("\u22ee", "More")
        self.more_btn.clicked.connect(lambda: self.moreMenu.emit(self.more_btn))
        for b in (self.save_btn, self.play_btn, self.more_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        ll.addLayout(buttons)
        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.status.setWordWrap(True)
        ll.addWidget(self.status)
        ll.addStretch(1)
        root.addWidget(left, 0, Qt.AlignmentFlag.AlignTop)

        right = QWidget()
        self._right = QVBoxLayout(right)
        self._right.setContentsMargins(0, 0, 0, 0)
        self._right.setSpacing(0)
        self.note = QLabel("Loading tracks...")
        T.bind_css(self.note, lambda: f"color: {T.fg3.name()};")
        self._right.addWidget(self.note)
        self._list = QVBoxLayout()
        self._list.setSpacing(0)
        self._right.addLayout(self._list)
        self.more_tracks = QPushButton("")
        self.more_tracks.setVisible(False)
        self.more_tracks.clicked.connect(self._show_all)
        self._right.addWidget(self.more_tracks, 0, Qt.AlignmentFlag.AlignLeft)
        self._right.addStretch(1)
        root.addWidget(right, 1)
        self.set_busy(True)

    # ---- content --------------------------------------------------------------------------------
    def set_info(self, title: str, subtitle: str, meta: str) -> None:
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.subtitle.setVisible(bool(subtitle))
        self.meta.setText(meta)

    def set_cover(self, pix: QPixmap | None) -> None:
        self.cover.set_pixmap(pix)

    def set_busy(self, busy: bool) -> None:
        for b in (self.save_btn, self.play_btn):
            b.setEnabled(not busy)

    def set_note(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def set_status(self, text: str, kind: str = "") -> None:
        colour = {"ok": lambda: T.ok, "warn": lambda: T.warn, "bad": lambda: T.bad}.get(kind, lambda: T.fg3)
        T.bind_css(self.status, lambda: f"color: {colour().name()};")
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def set_tracks(self, infos: list[TrackInfo]) -> None:
        self.clear_tracks()
        self._infos = infos
        self._shown_all = len(infos) <= FIRST_SHOWN
        self.set_note("" if infos else "No tracks found.")
        for i, info in enumerate(infos if self._shown_all else infos[:FIRST_SHOWN]):
            self._add_line(i, info)
        self._update_more()
        self.set_busy(not infos)

    def _add_line(self, i: int, info: TrackInfo) -> None:
        line = TrackLine(info, self._can_play)
        line.play.connect(lambda i=i: self.playTrack.emit(i))
        line.save.connect(lambda i=i: self.saveTrack.emit(i))
        line.menu.connect(lambda pos, i=i: self.trackMenu.emit(i, pos))
        line.ticked.connect(lambda on, i=i: self._ticked(i, on))
        self._lines.append(line)
        self._list.addWidget(line)

    def _ticked(self, i: int, on: bool) -> None:
        self._infos[i].checked = on
        self.tickChanged.emit()

    def _update_more(self) -> None:
        hidden = len(self._infos) - len(self._lines)
        self.more_tracks.setVisible(hidden > 0)
        self.more_tracks.setText(f"Show all {len(self._infos)} tracks")

    def _show_all(self) -> None:
        for i in range(len(self._lines), len(self._infos)):
            self._add_line(i, self._infos[i])
        self._shown_all = True
        self._update_more()

    def clear_tracks(self) -> None:
        for line in self._lines:
            line.setParent(None)
            line.deleteLater()
        self._lines, self._infos = [], []
        self.more_tracks.setVisible(False)

    # ---- state ----------------------------------------------------------------------------------
    def checked_indices(self) -> list[int]:
        return [i for i, info in enumerate(self._infos) if info.checked]

    def set_all_checked(self, on: bool) -> None:
        for info in self._infos:
            info.checked = on
        for line in self._lines:
            line.set_checked(on)
        self.tickChanged.emit()

    def set_playing(self, index: int | None, playing: bool, album_playing: bool) -> None:
        """`index`: the track of this album that is playing or paused (None: none of them)."""
        for i, line in enumerate(self._lines):
            line.set_current(i == index, playing)
        self.play_btn.set_playing(album_playing and playing)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        hint.setHeight(max(hint.height(), COVER + 190))
        return hint
