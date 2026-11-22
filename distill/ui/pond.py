"""The Distill pond: a small easter egg (Shift + click the logo). Click or drag on the water to make ripples; the little animals answer
with notes. The rules are in pondmath.py and the sounds in pondsound.py (no Qt there, both tested); this is the window and the drawing."""
from __future__ import annotations

import math
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QWidget

from .. import pondmath as pm
from .. import pondsound as ps
from .frameless import FramelessDialog
from .theme import T

try:
    from PySide6.QtMultimedia import QSoundEffect
    AUDIO_OK = True
except ImportError:  # no sound support: the pond is then only to look at
    AUDIO_OK = False

SOUND_DIR = Path(tempfile.gettempdir()) / "distill-pond-v1"
LABELS = {"jelly": "Jelly", "minnow": "Minnows", "tadpole": "Tadpole", "snail": "Snail", "crab": "Crab"}
TIPS = {"jelly": "Rings a bell, with a second note above it.", "minnow": "Dart away from a ripple, one note each.",
        "tadpole": "Plucks a string and ripples back, so it can set others off.", "snail": "A low hum, by where it sits across the pond.",
        "crab": "A drum: kick, snare or hat (each new crab takes the next)."}
DRUM_COLOURS = {"kick": "#e5584f", "snare": "#f08a4b", "hat": "#f2c14e"}


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t), round(a.blue() + (b.blue() - a.blue()) * t))


def _brand(t: float) -> QColor:
    """A point along the brand's blue - violet - pink gradient, 0..1."""
    a, b, c = T.grad
    return _mix(a, b, t * 2) if t < 0.5 else _mix(b, c, t * 2 - 1)


def _alpha(colour: QColor, a: float) -> QColor:
    out = QColor(colour)
    out.setAlpha(max(0, min(255, int(a))))
    return out


class PondAudio(QObject):
    """The pond's sounds: written to a temporary folder the first time (a second or so, off the window's thread), then one QSoundEffect each."""

    ready = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._fx: dict[str, QSoundEffect] = {}
        self.loaded = False
        if AUDIO_OK:
            self.ready.connect(self._load)
            threading.Thread(target=self._make, daemon=True).start()

    def _make(self) -> None:
        try:
            ps.write_all(SOUND_DIR)
        except OSError:
            return
        self.ready.emit()

    def _load(self) -> None:
        for key in ps.all_keys():
            fx = QSoundEffect(self)
            fx.setSource(QUrl.fromLocalFile(str(SOUND_DIR / ps.file_name(key))))
            self._fx[key] = fx
        self.loaded = True

    def play(self, key: str, vel: float) -> None:
        fx = self._fx.get(key)
        if fx is None:
            return
        if fx.isPlaying():
            fx.stop()
        fx.setVolume(max(0.05, min(1.0, vel)))
        fx.play()

    def stop(self) -> None:
        for fx in self._fx.values():
            fx.stop()


class PondView(QWidget):
    FRAME_MS = 16

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.model = pm.Pond()
        self.audio = PondAudio(self)
        self._t0 = time.perf_counter()
        self._populated = False
        self._last_drop: tuple[float, float, float] | None = None  # x, y, time of the last ripple a drag made
        self._touched = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(self.FRAME_MS)

    # ---- the pond ------------------------------------------------------------------------------------------------
    def now(self) -> float:
        return time.perf_counter() - self._t0

    def shutdown(self) -> None:
        self.timer.stop()
        self.audio.stop()

    def resizeEvent(self, event) -> None:
        self.model.resize(self.width(), self.height())
        if not self._populated and self.width() > 100:
            self._populated = True
            for kind in ("jelly", "tadpole", "minnow", "snail", "crab"):
                self.model.spawn(kind)
            self.model.add_ripple(self.width() / 2, self.height() * 0.45, self.now() + 0.6, 0.9)  # a first ripple, to show what this is
        super().resizeEvent(event)

    def add(self, kind: str) -> None:
        self.model.spawn(kind)

    def clear(self) -> None:
        self.model.clear()

    def _tick(self) -> None:
        for key, vel in self.model.update(self.now()):
            self.audio.play(key, vel)
        self.update()

    # ---- the mouse -----------------------------------------------------------------------------------------------
    def _drop(self, x: float, y: float, power: float = 1.0) -> None:
        self._touched = True
        self._last_drop = (x, y, self.now())
        self.model.add_ripple(x, y, self.now(), power)

    def mousePressEvent(self, e) -> None:
        pos = e.position()
        if e.button() == Qt.MouseButton.RightButton:
            self.model.remove_at(pos.x(), pos.y())
        elif e.button() == Qt.MouseButton.LeftButton:
            self._drop(pos.x(), pos.y())

    def mouseMoveEvent(self, e) -> None:
        if not (e.buttons() & Qt.MouseButton.LeftButton) or self._last_drop is None:
            return
        pos, (lx, ly, lt) = e.position(), self._last_drop
        if math.hypot(pos.x() - lx, pos.y() - ly) > 48 and self.now() - lt > 0.07:
            self._drop(pos.x(), pos.y(), 0.8)

    # ---- drawing -------------------------------------------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = self.now()
        w, h = self.width(), self.height()
        sky = QLinearGradient(0, 0, 0, h)
        sky.setColorAt(0.0, QColor("#0d2a44"))
        sky.setColorAt(1.0, QColor("#050f1c"))
        p.fillRect(self.rect(), QBrush(sky))
        for i, hue in enumerate((0.1, 0.5, 0.9)):  # slow patches of light drifting under the surface
            cx = w * (0.5 + 0.38 * math.sin(now * 0.07 + i * 2.1))
            cy = h * (0.5 + 0.3 * math.cos(now * 0.05 + i * 1.7))
            glow = QRadialGradient(cx, cy, max(w, h) * 0.35)
            glow.setColorAt(0.0, _alpha(_brand(hue), 26))
            glow.setColorAt(1.0, _alpha(_brand(hue), 0))
            p.fillRect(self.rect(), QBrush(glow))
        if self.model.tide:
            beat = (now - self.model.clock.origin) / self.model.clock.beat_len % 1.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_alpha(QColor("#ffffff"), 70 + 120 * (1 - beat)))
            p.drawEllipse(QPointF(w / 2, h / 2), 4 + 5 * (1 - beat), 4 + 5 * (1 - beat))
        for r in self.model.ripples:
            self._ripple(p, r, now)
        for c in sorted(self.model.creatures, key=lambda c: c.y):
            self._creature(p, c, now)
        if not self._touched and self.model.creatures:
            p.setPen(_alpha(QColor("#ffffff"), 120))
            p.drawText(QRectF(0, h - 40, w, 24), Qt.AlignmentFlag.AlignCenter, "Click or drag on the water. Right-click an animal to send it away.")
        p.end()

    def _ripple(self, p: QPainter, r: pm.Ripple, now: float) -> None:
        radius, s = r.radius(now), r.strength(now)
        colour = _brand(r.hue)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for k in range(3):  # a ring and two fainter ones trailing it
            rr = radius - k * 13
            if rr <= 1:
                continue
            p.setPen(QPen(_alpha(colour, 190 * s * (1 - k * 0.32)), 2.0 - k * 0.5))
            p.drawEllipse(QPointF(r.x, r.y), rr, rr)

    def _creature(self, p: QPainter, c: pm.Creature, now: float) -> None:
        colour = {"jelly": _brand(0.92), "minnow": _brand(0.05), "tadpole": _brand(0.5), "snail": QColor("#e8b07a"),
                  "crab": QColor(DRUM_COLOURS.get(c.drum, "#e5584f"))}[c.kind]
        e = c.energy
        if e > 0.02:  # the glow while its note sounds
            glow = QRadialGradient(c.x, c.y, 38 * (0.6 + e))
            glow.setColorAt(0.0, _alpha(colour, 130 * e))
            glow.setColorAt(1.0, _alpha(colour, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QPointF(c.x, c.y), 38 * (0.6 + e), 38 * (0.6 + e))
        p.save()
        p.translate(c.x, c.y)
        {"jelly": self._jelly, "minnow": self._minnow, "tadpole": self._tadpole, "snail": self._snail, "crab": self._crab}[c.kind](p, c, colour)
        p.restore()

    def _jelly(self, p: QPainter, c: pm.Creature, colour: QColor) -> None:
        r = 15.0
        pulse = 1 + 0.22 * c.energy + 0.06 * math.sin(c.phase * 2)
        dome = QPainterPath()
        dome.moveTo(-r * pulse, 0)
        dome.arcTo(QRectF(-r * pulse, -r * 0.9, 2 * r * pulse, 1.8 * r * 0.9), 180, -180)  # over the top, left to right
        dome.quadTo(0, r * 0.5 / pulse, -r * pulse, 0)
        p.setPen(QPen(_alpha(colour, 230), 1.2))
        p.setBrush(_alpha(colour, 120 + 90 * c.energy))
        p.drawPath(dome)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_alpha(colour, 170), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for i in range(5):
            x0 = -r * 0.7 + i * r * 0.35
            line = QPainterPath(QPointF(x0, 2))
            for s in range(1, 9):
                line.lineTo(x0 + math.sin(c.phase * 1.6 + i + s * 0.6) * 2.5, 2 + s * 2.6)
            p.drawPath(line)

    def _minnow(self, p: QPainter, c: pm.Creature, colour: QColor) -> None:
        p.rotate(math.degrees(c.heading))
        wag = math.sin(c.phase * 12 + c.index)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_alpha(colour, 235))
        p.drawEllipse(QRectF(-6, -2.6, 12, 5.2))
        tail = QPainterPath(QPointF(-5, 0))
        tail.lineTo(-11, -3.4 * wag - 2)
        tail.lineTo(-11, -3.4 * wag + 2)
        tail.closeSubpath()
        p.drawPath(tail)

    def _tadpole(self, p: QPainter, c: pm.Creature, colour: QColor) -> None:
        p.rotate(math.degrees(c.heading))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_alpha(colour, 210), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        tail = QPainterPath(QPointF(-4, 0))
        for s in range(1, 10):
            tail.lineTo(-4 - s * 2.3, math.sin(c.phase * 9 - s * 0.7) * s * 0.45)
        p.drawPath(tail)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_alpha(colour, 245))
        p.drawEllipse(QPointF(0, 0), 5.2, 4.6)

    def _snail(self, p: QPainter, c: pm.Creature, colour: QColor) -> None:
        if math.cos(c.heading) < 0:
            p.scale(-1, 1)
        draw = 1 - 0.35 * c.squeeze
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_alpha(colour, 200))
        p.drawRoundedRect(QRectF(-13, -2, 26 * (1 - 0.15 * c.squeeze), 6), 3, 3)
        p.setPen(QPen(_alpha(colour, 200), 1.2))
        for sx in (8, 11):  # eye stalks
            p.drawLine(QPointF(sx * draw, -2), QPointF((sx + 1) * draw, -7 + 3 * c.squeeze))
        spiral = QPainterPath()
        for i in range(0, 41):
            a = i * 0.32
            rad = (1.0 + a * 0.62) * draw
            pt = QPointF(-2 + rad * math.cos(a), -8 + rad * math.sin(a))
            spiral.moveTo(pt) if i == 0 else spiral.lineTo(pt)
        p.setBrush(_alpha(colour, 70))
        p.setPen(QPen(_alpha(colour, 235), 1.5))
        p.drawEllipse(QPointF(-2, -8), 9 * draw, 9 * draw)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(spiral)

    def _crab(self, p: QPainter, c: pm.Creature, colour: QColor) -> None:
        snap = c.energy
        p.setPen(QPen(_alpha(colour, 220), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for side in (-1, 1):
            for i in range(3):  # legs
                y = 2 + i * 3
                p.drawLine(QPointF(side * 8, y), QPointF(side * (15 + i), y + 3 + math.sin(c.phase * 8 + i) * 1.2))
            p.drawLine(QPointF(side * 9, -3), QPointF(side * 15, -8 - 5 * snap))  # the arm
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_alpha(colour, 235))
        p.drawEllipse(QRectF(-11, -6, 22, 13))
        for side in (-1, 1):
            p.drawEllipse(QPointF(side * 16, -10 - 5 * snap), 4.2 + 1.5 * snap, 4.2 + 1.5 * snap)  # the claw
        p.setBrush(QColor("#0b1a2a"))
        for side in (-1, 1):
            p.drawEllipse(QPointF(side * 4, -3), 1.5, 1.5)


class PondDialog(FramelessDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, "Distill pond", size=(980, 660), min_size=(720, 480))
        self.view = PondView()
        self.content.addWidget(self.view, 1)
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 10, 16, 12)
        row.setSpacing(8)
        for kind, label in LABELS.items():
            button = QPushButton(f"+ {label}")
            button.setToolTip(TIPS[kind])
            button.clicked.connect(lambda _=False, k=kind: self.view.add(k))
            row.addWidget(button)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.view.clear)
        row.addWidget(clear)
        row.addStretch(1)
        self.tide = QPushButton("Tide")
        self.tide.setCheckable(True)
        self.tide.setToolTip("A ripple from the middle on every beat. Animals far out answer later, which is where the rhythm comes from.")
        self.tide.toggled.connect(self._set_tide)
        row.addWidget(self.tide)
        self.snap = QPushButton("Snap")
        self.snap.setCheckable(True)
        self.snap.setChecked(True)
        self.snap.setToolTip("Notes wait for the beat, so everything stays in time.")
        self.snap.toggled.connect(lambda on: setattr(self.view.model, "snap", on))
        row.addWidget(self.snap)
        self.tempo = QSlider(Qt.Orientation.Horizontal)
        self.tempo.setRange(*pm.BPM_RANGE)
        self.tempo.setValue(int(self.view.model.clock.bpm))
        self.tempo.setFixedWidth(130)
        self.tempo.valueChanged.connect(self._set_tempo)
        row.addWidget(self.tempo)
        self.bpm_label = QLabel(f"{self.tempo.value()} bpm")
        self.bpm_label.setMinimumWidth(52)
        row.addWidget(self.bpm_label)
        self.content.addWidget(bar)

    def _set_tide(self, on: bool) -> None:
        self.view.model.tide = on
        if on:
            self.view.model.clock.origin = self.view.now()  # the first beat of the bar is now

    def _set_tempo(self, bpm: int) -> None:
        self.bpm_label.setText(f"{bpm} bpm")
        self.view.model.set_bpm(bpm, self.view.now())

    def done(self, result: int) -> None:
        self.view.shutdown()
        super().done(result)
