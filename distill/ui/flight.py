"""A picture that flies from where it was to where it is going: the emulator's icon leaving its tile for the top of its page, and back.

The real widgets don't move (they sit in different pages). A copy of the picture is drawn in a floating widget over the page while the
caller holds the real destination hidden, and the copy is removed when it lands. The end is asked for on every frame, because the page
that holds it is still being laid out when the flight begins. Nothing animates when motion is reduced (Settings > Reduce Motion)."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QRect, Qt, QVariantAnimation
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from . import motion
from .flightmath import Box, ease_in_out_cubic, lerp_box


def box_of(widget: QWidget, within: QWidget) -> QRect:
    """Where `widget` is, in the coordinates of `within` (an ancestor of it)."""
    return QRect(widget.mapTo(within, QPoint(0, 0)), widget.size())


def picture_of(widget) -> QPixmap:
    """What a QLabel shows now, as it was drawn: its picture when it has one, else a snapshot of the widget (a monogram tile)."""
    px = widget.pixmap() if hasattr(widget, "pixmap") else None
    return px if px is not None and not px.isNull() else widget.grab()


def hold(widget: QWidget) -> None:
    """Make a widget invisible without letting go of its place in the layout."""
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)


def release(widget: QWidget) -> None:
    try:
        widget.setGraphicsEffect(None)
    except RuntimeError:  # the widget was deleted while the picture was in the air (the list was rebuilt)
        pass


class _Flyer(QWidget):
    def __init__(self, parent: QWidget, pixmap: QPixmap):
        super().__init__(parent)
        self._px = pixmap
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(self.rect(), self._px)


class Flight(QObject):
    """One flight. `end` returns the destination box (or None when it can't be found: the picture stays where it last was); `done` runs
    exactly once, whether it landed or was cancelled, and is where the caller un-hides the real destination."""

    def __init__(self, parent: QWidget, pixmap: QPixmap, start: QRect, end: Callable[[], QRect | None],
                 done: Callable[[], None] = lambda: None, duration: int = 320):
        super().__init__(parent)
        self._start: Box = (start.x(), start.y(), start.width(), start.height())
        self._last: Box = self._start
        self._end, self._done, self._finished = end, done, False
        self._flyer = _Flyer(parent, pixmap)
        self._flyer.setGeometry(start)
        self._flyer.show()
        self._flyer.raise_()
        self._anim = QVariantAnimation(self, duration=duration, startValue=0.0, endValue=1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)  # the easing is ease_in_out_cubic, applied here so the maths is testable
        self._anim.valueChanged.connect(lambda v: self._step(float(v)))
        self._anim.finished.connect(self.finish)

    def go(self) -> "Flight":
        if not motion.on() or not self.parent().window().isVisible():
            self.finish()
        else:
            self._anim.start()
        return self

    def _step(self, t: float) -> None:
        if self._finished:
            return
        target = self._end()
        goal = (target.x(), target.y(), target.width(), target.height()) if target is not None else self._last
        self._last = lerp_box(self._start, goal, ease_in_out_cubic(t))
        self._flyer.setGeometry(*self._last)

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._anim.stop()
        self._flyer.hide()
        self._flyer.deleteLater()
        self._done()
        self.deleteLater()
