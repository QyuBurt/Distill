"""One track in the Soundtrack Grabber list: a tick (save this track?), a play button that morphs between a triangle
and a square, and the title. The track that is playing is highlighted."""
from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QPointF, QRectF, QSize, Qt, Signal, QVariantAnimation
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout, QSizePolicy, QWidget

from . import motion
from .theme import T
from .widgets import AnimCheckBox

# four corner points each, so one shape can slide into the other: a triangle (one corner doubled) and a square
PLAY_PTS = ((8.5, 6.0), (17.0, 11.0), (17.0, 11.0), (8.5, 16.0))
STOP_PTS = ((7.0, 7.0), (15.0, 7.0), (15.0, 15.0), (7.0, 15.0))


class PlayToggle(QAbstractButton):
    """A triangle that turns into a square while its track plays, and back."""

    SIZE = 22

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._p = 0.0  # 0 = play triangle, 1 = stop square
        self._anim = QVariantAnimation(self, duration=170)
        self._anim.valueChanged.connect(self._set)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Play a preview (it streams from the source). Click again to stop.")

    @property
    def playing(self) -> bool:
        return self._p > 0.5

    def set_playing(self, on: bool) -> None:
        goal = 1.0 if on else 0.0
        if goal == self._p and self._anim.state() == QAbstractAnimation.State.Stopped:
            return
        self._anim.stop()
        if not motion.on() or not self.isVisible():
            self._p = goal
            self.update()
            return
        self._anim.setStartValue(self._p)
        self._anim.setEndValue(goal)
        self._anim.start()

    def _set(self, value) -> None:
        self._p = float(value)
        self.update()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hover = self.underMouse()
        if hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.hover)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        pts = [QPointF(a[0] + (b[0] - a[0]) * self._p, a[1] + (b[1] - a[1]) * self._p)
               for a, b in zip(PLAY_PTS, STOP_PTS)]
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        path.closeSubpath()
        colour = T.title if hover else T.accent_text
        p.setPen(QPen(colour, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))  # soft corners
        p.setBrush(colour)
        p.drawPath(path)


class TrackRow(QWidget):
    """[tick] [play] title. `toggled(bool)` when the tick changes, `playClicked()` when the play button is pressed."""

    toggled = Signal(bool)
    playClicked = Signal()
    ROW_MIN = 28  # the row's own minimum height; its list may squeeze it lower while it folds open or shut
    SOFT = 48  # px: the soft edge of the left-to-right wipe while the row is written in
    _fold_h: int | None = None  # while its list folds it open or shut: the height it is allowed right now

    def __init__(self, title: str, checked: bool, can_play: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self._title = title
        self._current = False
        lay = QHBoxLayout(self)
        lay.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)  # the layout must not hold the row open
        lay.setContentsMargins(1, 0, 8, 0)  # + the tick's own 2px of room: the box sits where it always did
        lay.setSpacing(3)
        self.check = AnimCheckBox("")
        self.check.setChecked(checked)
        self.check.setToolTip("Tick the tracks to save")
        self.check.toggled.connect(self.toggled)
        lay.addWidget(self.check)
        self.play = PlayToggle() if can_play else None
        if self.play is not None:
            self.play.clicked.connect(self.playClicked)
            lay.addWidget(self.play)
        self.label = QLabel(title)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # clicks reach the list (select, menu)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.label.setToolTip(title)
        lay.addWidget(self.label, 1)
        self.setMinimumHeight(self.ROW_MIN)

    def set_checked(self, on: bool) -> None:
        """Show a tick state set by the list (the parent game's box), without announcing it."""
        self.check.blockSignals(True)
        self.check.setChecked(on)
        self.check._t = 1.0 if on else 0.0
        self.check.blockSignals(False)
        self.check.update()

    def sizeHint(self) -> QSize:
        # A tree sizes a row as the taller of its own hint and its widget's hint. While the list folds this row
        # open or shut the widget has to follow, or the row would stay full height and only fade.
        hint = super().sizeHint()
        if self._fold_h is not None:
            hint.setHeight(self._fold_h)
        return hint

    def set_reveal(self, opacity: float | None = None, height: int | None = None, front: float | None = None) -> None:
        """Show the row part-way while its list folds it open or shut: `opacity` (0..1), the `height` it is allowed,
        and `front`, how far right (in this row's own x) it has been written so far, with a soft edge. Nothing given
        puts the row back to normal."""
        plain = (opacity is None or opacity >= 1.0) and front is None  # nothing to fade or sweep
        if plain:
            if self.graphicsEffect() is not None:  # an idle effect makes Qt draw the row off-screen first
                self.setGraphicsEffect(None)
            if height is None:
                self._fold_h = None
                self.setMinimumHeight(self.ROW_MIN)
                self.updateGeometry()
                return
        else:
            effect = self.graphicsEffect()
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = QGraphicsOpacityEffect(self)
                self.setGraphicsEffect(effect)
            effect.setOpacity(1.0 if opacity is None else max(0.0, min(1.0, opacity)))
            if front is None:
                effect.setOpacityMask(QBrush())
            else:
                sweep = QLinearGradient(front - self.SOFT, 0.0, front, 0.0)  # solid up to the front, clear beyond it
                sweep.setColorAt(0.0, QColor(0, 0, 0, 255))
                sweep.setColorAt(1.0, QColor(0, 0, 0, 0))
                effect.setOpacityMask(QBrush(sweep))
        self._fold_h = height
        self.setMinimumHeight(0)
        self.updateGeometry()

    def set_current(self, on: bool) -> None:
        """Highlight this row while its track is the one playing (or paused)."""
        if on == self._current:
            return
        self._current = on
        T.bind_css(self.label, lambda: f"color: {T.accent_text.name()}; font-weight: 600;" if on else "")
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.label.setText(self.label.fontMetrics().elidedText(self._title, Qt.TextElideMode.ElideRight,
                                                               max(self.label.width(), 40)))

    def paintEvent(self, _event) -> None:
        if not self._current:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        wash = QLinearGradient(0, 0, self.width(), 0)
        start, end = T.wash(70, 24)
        wash.setColorAt(0.0, start)
        wash.setColorAt(1.0, end)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(wash)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0, 1, -2, -1), 7, 7)
        p.setBrush(T.accent)
        p.drawRoundedRect(QRectF(0, 5, 3, self.height() - 10), 1.5, 1.5)
