"""The download progress bar: the same track, border and brand gradient as every other bar in Distill, with the brand's ripple on
the leading edge of the fill (the wave from the update screen's filling logo, here running down the edge of a bar that fills left
to right). It starts and finishes flat, holds still under Settings > Reduce motion, and only runs a timer while it is part-way.

It is a QProgressBar underneath, so everything that reads or drives the old bar (value, range, valueChanged, ease_value, the
Resonance dock following the download run's bar) works unchanged. It is the download bar, not a second one beside it.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QProgressBar, QWidget

from .. import ripplemath
from . import fillmath, motion
from .theme import T

HEIGHT = 14  # a little over the 12 px of the plain bar, so the ripple has room to show
RADIUS = 6.0
PEAK = 3.0  # px: how far the edge is pushed sideways at most


class RippleBar(QProgressBar):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(HEIGHT)
        self._phase = 0.0
        self._shown = 0.0  # the fill as drawn, 0..1: it glides towards the value instead of stepping to it
        self._clock = motion.FrameClock()
        self._timer = motion.frame_timer(self)
        self._timer.timeout.connect(self._tick)
        self.valueChanged.connect(self._sync)

    def _frac(self) -> float:
        return ripplemath.fraction(self.value(), self.minimum(), self.maximum())

    def _moving(self) -> bool:
        return self._shown < self._frac() - ripplemath.FILL_EPS

    def _sync(self, *_args) -> None:
        """Run the timer only while there is something to animate: visible, motion not reduced, and either part-way (the ripple)
        or still filling towards its value. A bar that cannot animate just shows its value."""
        animate = motion.on() and self.isVisible()
        if not animate or self._frac() < self._shown:  # nothing to glide, or a reset: show the value at once
            self._shown = self._frac()
        live = animate and (ripplemath.is_rippling(self._shown) or ripplemath.is_rippling(self._frac()) or self._moving())
        if live and not self._timer.isActive():
            self._clock.reset()
            self._timer.start(motion.frame_ms(self))
        elif not live and self._timer.isActive():
            self._timer.stop()
            self.update()  # one last frame, with the edge flat

    def _tick(self) -> None:
        dt = self._clock.dt()
        self._phase = ripplemath.next_phase(self._phase, dt)
        self._shown = ripplemath.chase(self._shown, self._frac(), dt)
        self.update()
        if not self._timer.isActive() or not (ripplemath.is_rippling(self._shown) or self._moving()):
            self._sync()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._sync()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(T.card_border, 1))
        p.setBrush(T.input)
        p.drawRoundedRect(track, RADIUS, RADIUS)
        frac = self._shown if self._timer.isActive() or self._moving() else self._frac()
        inner = track.adjusted(0.5, 0.5, -0.5, -0.5)  # inside the border
        if frac > 0.0:
            clip = QPainterPath()
            clip.addRoundedRect(inner, RADIUS - 1, RADIUS - 1)
            p.setClipPath(clip)
            edge = inner.left() + inner.width() * frac
            amp = fillmath.wave_amplitude(frac, PEAK) if motion.on() else 0.0
            path = QPainterPath(QPointF(inner.left(), inner.top()))
            for x, y in ripplemath.edge_points(edge, inner.top(), inner.bottom(), amp, self._phase, inner.height() * ripplemath.WAVE_LENGTH):
                path.lineTo(x, y)
            path.lineTo(inner.left(), inner.bottom())
            path.closeSubpath()
            fill = QLinearGradient(inner.left(), 0, max(edge, inner.left() + 1), 0)  # the three brand colours across the filled part
            for at, colour in zip((0.0, 0.5, 1.0), T.grad):
                fill.setColorAt(at, colour)
            p.fillPath(path, QBrush(fill))
            p.setClipping(False)
        if self.isTextVisible():
            p.setPen(T.fg)
            p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())
