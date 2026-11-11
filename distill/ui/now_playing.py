"""The little player at the bottom of the sidebar: what is playing, previous / play-pause / next, a seek bar, stop."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QConicalGradient, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QSizePolicy, QSlider, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from . import icons, motion, scratchmath
from .frameless import FramelessDialog
from .theme import T

ART_PREVIEW_MAX = 640  # the larger cover's longest side, in pixels


def clock(ms: int) -> str:
    seconds = max(int(ms), 0) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


class ElidedLabel(QLabel):
    """A label that shortens its text with '...' instead of widening the sidebar."""

    def __init__(self, object_name: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        if object_name:
            self.setObjectName(object_name)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(self.width() - 2, 10)))


class WavySlider(QSlider):
    """The seek bar, in the style of Android's media controls: the part already played is a gentle wave that ripples
    along while music plays and eases flat when it is paused, a pill-shaped handle marks the position, and the rest
    is a plain line. Clicking or dragging anywhere on it seeks."""

    PAD = 9  # room at both ends for the line caps and the handle
    WAVELENGTH = 24.0
    AMPLITUDE = 3.0

    def __init__(self, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._phase = 0.0
        self._amp = 0.0  # the wave's height now, eased between 0 (flat) and 1
        self._moving = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_moving(self, on: bool) -> None:
        """True while music plays: the wave ripples. False: it settles to a straight line."""
        self._moving = on
        if not motion.on():  # reduced motion: a still, gently waved line while playing, flat when paused
            self._timer.stop()
            self._amp = 0.6 if on else 0.0
            self.update()
            return
        if self.isVisible() and (on or self._amp > 0.02) and not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        goal = 1.0 if self._moving else 0.0
        self._amp += (goal - self._amp) * 0.14
        if self._moving:
            self._phase = (self._phase + 0.13) % (2 * math.pi)
        elif self._amp < 0.02:
            self._amp = 0.0
            self._timer.stop()
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.set_moving(self._moving)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def _fraction(self) -> float:
        span = self.maximum() - self.minimum()
        return 0.0 if span <= 0 else min(max((self.value() - self.minimum()) / span, 0.0), 1.0)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x0, x1, cy = float(self.PAD), float(self.width() - self.PAD), self.height() / 2
        hx = x0 + self._fraction() * (x1 - x0)
        down = self.isSliderDown()
        gap = 5.0 if down else 4.0  # clear space either side of the handle
        # the rest of the track: a flat, faint line
        if hx + gap < x1:
            p.setPen(QPen(T.seek_rest, 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(hx + gap, cy), QPointF(x1, cy))
        # the played part: the wave, in the brand gradient
        end = hx - gap
        if end > x0:
            grad = QLinearGradient(x0, 0, x1, 0)
            for stop, col in zip((0.0, 0.5, 1.0), T.grad):
                grad.setColorAt(stop, col)
            path = QPainterPath()
            amp = self.AMPLITUDE * self._amp
            x = x0
            while x <= end:
                # the wave grows from nothing over the first few pixels, so its start sits on the line
                ramp = min(1.0, (x - x0) / 10.0)
                y = cy + amp * ramp * math.sin((x - x0) / self.WAVELENGTH * 2 * math.pi - self._phase)
                path.moveTo(x, y) if x == x0 else path.lineTo(x, y)
                x += 1.5
            p.setPen(QPen(grad, 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
        # the handle
        h = 20.0 if down else 18.0
        w = 5.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.seek_handle)
        p.drawRoundedRect(QRectF(hx - w / 2, cy - h / 2, w, h), w / 2, w / 2)

    # ---- mouse: click or drag to seek ------------------------------------------------------------
    def _value_at(self, x: float) -> int:
        span = max(self.width() - 2 * self.PAD, 1)
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(round(x - self.PAD)), span)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            self.setSliderDown(True)
            self.setSliderPosition(self._value_at(event.position().x()))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.isSliderDown():
            self.setSliderPosition(self._value_at(event.position().x()))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.isSliderDown() and event.button() == Qt.MouseButton.LeftButton:
            self.setSliderPosition(self._value_at(event.position().x()))
            self.setSliderDown(False)  # announces the release: the player seeks to the final position
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class CdArt(QWidget):
    """The cover, shown as a compact disc: the artwork is the printed face, with the hole and the frosted hub ring in the middle,
    and a fixed glint of light across it (it stays still while the disc turns under it, which is what makes the spin read, even
    on a cover that looks the same from every angle). The disc eases up to speed when `set_spinning(True)` is called and coasts
    to a stop after `set_spinning(False)`. With reduced motion it never turns.

    It can also be held, like a record under a hand: press and hold (or press and drag) and it stops at once and reports
    `scratchStarted`; turning it round reports `scratched` with how far it has been turned and how fast; letting go reports
    `scratchEnded` and it eases back up to speed. A quick press and release, without moving, is still just a click."""

    clicked = Signal()
    scratchStarted = Signal()  # held long enough, or dragged: the disc is in a hand now
    scratched = Signal(float, float)  # degrees turned since it was grabbed (clockwise is +), and the turning speed now (degrees per second)
    scratchEnded = Signal()  # let go

    SPIN = 160.0  # degrees per second at full speed: one turn in about 2.3 s
    HOLE = 0.13  # the centre hole's radius, as a share of the disc's
    HUB = 0.30  # the frosted ring around it
    UP = (0.25, 0.35)  # time constants (s) of the two easing stages when speeding up: cascaded, so it starts gently...
    DOWN = (0.20, 0.35)  # ...and, slowing down, it glides to rest (in about two seconds) instead of braking
    HOLD_MS = 220  # held this long without moving counts as grabbing it
    MOVE_PX = 4  # moved this far with the button down counts as grabbing it

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._face: QPixmap | None = None  # the cover, cropped square, at the disc's size (and the screen's pixel ratio)
        self._pending = False  # no cover yet: a blank disc
        self._spinning = False
        self._drive = 0.0  # 0..1, first easing stage
        self._omega = 0.0  # degrees per second now
        self._angle = 0.0
        self._last = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._held = False  # in a hand: it stays put until the hand lets go (and turns only as far as the hand does)
        self._press: QPointF | None = None  # where the button went down on it (None: not down)
        self._pointer = 0.0  # the pointer's angle round the disc at the last move
        self._turned = 0.0  # degrees turned since it was grabbed
        self._speed = 0.0  # degrees per second, smoothed
        self._moved_at = 0.0
        self._cursor = self.cursor()
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(self.HOLD_MS)
        self._hold.timeout.connect(self._grab)

    # ---- what the player tells it ------------------------------------------------------------
    def set_cover(self, pixmap: QPixmap | None, pending: bool = False, ratio: float = 1.0) -> None:
        if pixmap is not None and not pixmap.isNull():
            side = max(int(self._diameter() * ratio), 2)
            scaled = pixmap.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            face = QPixmap(side, side)
            face.fill(Qt.GlobalColor.transparent)
            painter = QPainter(face)
            painter.drawPixmap((side - scaled.width()) // 2, (side - scaled.height()) // 2, scaled)
            painter.end()
            face.setDevicePixelRatio(ratio)
            self._face, self._pending = face, False
        else:
            self._face, self._pending = None, pending
        self.update()

    def pixmap(self) -> QPixmap:
        """The cover as drawn on the disc (a null pixmap while there is none)."""
        return self._face if self._face is not None else QPixmap()

    def set_spinning(self, on: bool) -> None:
        self._spinning = on
        if not motion.on():  # reduced motion: the disc stays where it is
            self._timer.stop()
            self._drive = self._omega = 0.0
            self.update()
            return
        self._run_timer()

    # ---- motion ------------------------------------------------------------------------------
    def _run_timer(self) -> None:
        if self.isVisible() and motion.on() and not self._held and (self._spinning or self._omega > 0.5) and not self._timer.isActive():
            self._last = time.monotonic()
            self._timer.start()

    def _tick(self) -> None:
        now = time.monotonic()
        dt, self._last = min(now - self._last, 0.1), now  # a stall (a drag of the window, say) must not make it lurch
        self.advance(dt)

    def advance(self, dt: float) -> None:
        """Move the disc on by `dt` seconds."""
        t1, t2 = self.UP if self._spinning else self.DOWN
        self._drive += ((1.0 if self._spinning else 0.0) - self._drive) * (1 - math.exp(-dt / t1))
        self._omega += (self._drive * self.SPIN - self._omega) * (1 - math.exp(-dt / t2))
        if not self._spinning and self._omega < 1.0:  # close enough to a stop: rest
            self._drive = self._omega = 0.0
            self._timer.stop()
        self._angle = (self._angle + self._omega * dt) % 360.0
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._run_timer()

    def hideEvent(self, event) -> None:
        self._timer.stop()  # nothing to draw while the player is not on screen
        self._hold.stop()
        self._press = None
        if self._held:  # the player closed with the disc in a hand
            self._let_go()
        super().hideEvent(event)

    # ---- drawing -----------------------------------------------------------------------------
    def _diameter(self) -> int:
        return max(min(self.width(), self.height()), NowPlaying.ART) - 2  # a pixel of room for the rim

    # ---- holding and turning it ----------------------------------------------------------------
    def _polar(self, pos: QPointF) -> tuple[float, float, float]:
        """The pointer's angle round the disc's centre, its distance from it, and the disc's radius."""
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        c = rect.center()
        dx, dy = pos.x() - c.x(), pos.y() - c.y()
        return scratchmath.pointer_angle(dx, dy), math.hypot(dx, dy), min(rect.width(), rect.height()) / 2

    def _grab(self) -> None:
        if self._press is None or self._held:
            return
        self._held = True
        self._hold.stop()
        self._cursor = self.cursor()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self._drive = self._omega = 0.0  # a hand on the record stops it at once
        self._timer.stop()
        self._moved_at = time.monotonic()
        self.update()
        self.scratchStarted.emit()

    def _let_go(self) -> None:
        self._held = False
        self.setCursor(self._cursor)
        self._run_timer()
        self.scratchEnded.emit()  # (the player starts again from here, which is what sets the disc turning)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._face is not None:
            self._press = event.position()
            self._pointer = self._polar(self._press)[0]
            self._turned = self._speed = 0.0
            self._hold.start()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is None:
            super().mouseMoveEvent(event)
            return
        event.accept()
        pos = event.position()
        if not self._held:
            if (pos - self._press).manhattanLength() <= self.MOVE_PX:
                return
            self._grab()
        angle, dist, radius = self._polar(pos)
        if dist < radius * self.HOLE * 1.5:  # right over the hole the angle swings wildly: wait until the pointer is out of it
            return
        step = scratchmath.turn(self._pointer, angle)
        self._pointer = angle
        now = time.monotonic()
        dt, self._moved_at = max(now - self._moved_at, 0.004), now
        self._speed += (step / dt - self._speed) * 0.5
        self._turned += step
        if motion.on():  # (with reduced motion the disc stays put, but the track still follows the hand)
            self._angle = (self._angle + step) % 360.0
            self.update()
        self.scratched.emit(self._turned, self._speed)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press is not None:
            self._hold.stop()
            self._press = None
            if self._held:
                self._let_go()
            else:
                self.clicked.emit()  # pressed and let go without moving: a click
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        c, r = rect.center(), min(rect.width(), rect.height()) / 2
        disc, hole, hub = QPainterPath(), QPainterPath(), QPainterPath()
        disc.addEllipse(c, r, r)
        hole.addEllipse(c, r * self.HOLE, r * self.HOLE)
        hub.addEllipse(c, r * self.HUB, r * self.HUB)
        ring = disc.subtracted(hole)  # the hole is open: the card shows through it
        p.setClipPath(ring)
        p.setPen(Qt.PenStyle.NoPen)
        p.save()
        p.translate(c)
        p.rotate(self._angle)
        if self._face is not None:
            p.drawPixmap(QPointF(-r, -r), self._face)
        else:  # a blank disc: silvery wedges that turn with it
            base = QColor(T.select)
            face = QConicalGradient(QPointF(0, 0), 0)
            for stop, col in ((0.0, base.lighter(130)), (0.25, base.darker(112)), (0.5, base.lighter(130)),
                              (0.75, base.darker(112)), (1.0, base.lighter(130))):
                face.setColorAt(stop, col)
            p.setBrush(face)
            p.drawEllipse(QPointF(0, 0), r, r)
        p.restore()
        glint = QConicalGradient(c, 35)  # fixed in space: the disc turns beneath it
        for stop, alpha in ((0.0, 0), (0.07, 70), (0.14, 0), (0.5, 0), (0.57, 50), (0.64, 0), (1.0, 0)):
            glint.setColorAt(stop, QColor(255, 255, 255, alpha))
        p.setBrush(glint)
        p.drawPath(ring)
        p.setBrush(QColor(238, 241, 249, 120))  # the frosted hub
        p.drawPath(hub.subtracted(hole))
        p.setClipping(False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 50) if T.dark else QColor(0, 0, 0, 60), 1.0))
        for radius in (r - 0.5, r * self.HUB, r * self.HOLE):  # the rim, the hub's edge, the hole's edge
            p.drawEllipse(c, radius, radius)


class ArtPreview(FramelessDialog):
    """The cover at a larger size. Click it, or press Esc, to close."""

    def __init__(self, pixmap: QPixmap, title: str, parent: QWidget | None = None):
        super().__init__(parent, title or "Cover", "image", None, (0, 0))
        lay = self.body((8, 8, 8, 8), 0)
        screen = QApplication.primaryScreen()
        room = int(min(screen.availableGeometry().width(), screen.availableGeometry().height()) * 0.8) if screen else 600
        side = max(120, min(ART_PREVIEW_MAX, room))
        longest = max(pixmap.width(), pixmap.height(), 1)
        target = min(side, longest * 2)  # a small cover is enlarged at most twice, so it stays reasonably sharp
        shown = pixmap.scaled(target, target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        label = QLabel()
        label.setPixmap(shown)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.setToolTip("Click to close")
        lay.addWidget(label)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.accept()


class NowPlaying(QWidget):
    """Shows the track being previewed. It only reports clicks (signals); the Soundtracks page owns the player."""

    ART = 52  # the cover's side, in pixels

    skipBack = Signal()
    togglePlay = Signal()
    skipForward = Signal()
    stopped = Signal()
    seekTo = Signal(int)  # milliseconds
    opened = Signal()  # the title was clicked: take me to where this came from
    volumeChanged = Signal(float, bool)  # 0..1 on the slider, and whether it is muted
    scratchStarted = Signal()  # the disc was held (or dragged): stop the music
    scratched = Signal(float, float)  # degrees it has been turned since, and how fast it is turning now
    scratchEnded = Signal()  # the disc was let go: carry on from where it was turned to

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("nowPlaying")
        host = QVBoxLayout(self)
        host.setContentsMargins(10, 8, 6, 8)
        card = QFrame()
        card.setObjectName("nowPlayingCard")
        host.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(10)
        self._art = CdArt()  # the cover, as a disc that turns while music plays; hidden when the source has none
        self._full: QPixmap | None = None  # the cover at full size (flat, not the disc), for the larger preview
        self._art.clicked.connect(self.show_large_art)
        self._art.scratchStarted.connect(self.scratchStarted)
        self._art.scratched.connect(self.scratched)
        self._art.scratchEnded.connect(self.scratchEnded)
        self._art.setFixedSize(self.ART, self.ART)
        self._art.setVisible(False)
        top.addWidget(self._art)
        words = QVBoxLayout()
        words.setSpacing(1)
        self._title = ElidedLabel("nowTitle")
        self._game = ElidedLabel("hint")
        self._title.setCursor(Qt.CursorShape.PointingHandCursor)
        words.addStretch(1)
        words.addWidget(self._title)
        words.addWidget(self._game)
        words.addStretch(1)
        top.addLayout(words, 1)
        lay.addLayout(top)

        self._slider = WavySlider()
        self._slider.setRange(0, 0)
        self._slider.setFixedHeight(24)
        self._slider.sliderMoved.connect(self.seekTo.emit)
        self._slider.sliderReleased.connect(lambda: self.seekTo.emit(self._slider.value()))
        lay.addWidget(self._slider)

        row = QHBoxLayout()
        row.setSpacing(2)
        self._back = self._button("skip-back", "Previous track (restarts this one first)",
                                  self.skipBack)
        self._play = self._button("pause", "Pause", self.togglePlay)
        self._forward = self._button("skip-forward", "Next track", self.skipForward)
        self._stop = self._button("stop", "Stop and close the player", self.stopped)
        self._time = QLabel("0:00 / 0:00")
        self._time.setObjectName("hint")
        for b in (self._back, self._play, self._forward):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self._time)
        row.addWidget(self._stop)
        lay.addLayout(row)

        vol = QHBoxLayout()  # volume: a mute button and a slider
        vol.setSpacing(2)
        self._mute = QToolButton()
        self._mute.setObjectName("npButton")
        self._mute.setAutoRaise(True)
        self._mute.setFixedSize(28, 26)
        self._mute.setCheckable(True)
        self._mute.toggled.connect(self._volume_moved)
        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._volume.setObjectName("npSlider")
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.setFixedHeight(16)
        self._volume.valueChanged.connect(self._volume_moved)
        vol.addWidget(self._mute)
        vol.addWidget(self._volume, 1)
        lay.addLayout(vol)
        self._refresh_mute_icon()
        self._duration = 0
        self._playing = True

    def _button(self, icon: str, tip: str, signal) -> QToolButton:
        b = QToolButton()
        b.setObjectName("npButton")
        b.setIcon(self._icon(icon))
        b.setAccessibleName(tip)  # no hover tooltip; screen readers still get the name
        b.setAutoRaise(True)
        b.setFixedSize(28, 26)
        b.clicked.connect(lambda _c=False: signal.emit())
        return b

    def _icon(self, name: str):
        """A transport icon in the theme's colours: soft text colour, white under the pointer; play / pause in the
        accent colour so the main control stands out. (The style's stock icons are dark grey: lost on this theme.)"""
        main = name in ("play", "pause")
        return icons.button_icon(name, T.accent_text if main else T.fg, T.title, 18)

    def refresh_theme(self) -> None:
        """Redraw every icon in the current colours."""
        for button, name in ((self._back, "skip-back"), (self._forward, "skip-forward"), (self._stop, "stop")):
            button.setIcon(self._icon(name))
        self._play.setIcon(self._icon("pause" if self._playing else "play"))
        self._refresh_mute_icon()
        self._art.update()  # the blank disc and the rim take the theme's colours

    # ---- volume -------------------------------------------------------------------------------
    def _volume_moved(self, *_args) -> None:
        if self._volume.value() > 0 and self._mute.isChecked() and self.sender() is self._volume:
            self._mute.setChecked(False)  # moving the slider while muted brings the sound back
        self._refresh_mute_icon()
        self.volumeChanged.emit(self._volume.value() / 100, self._mute.isChecked() or self._volume.value() == 0)

    def _refresh_mute_icon(self) -> None:
        silent = self._mute.isChecked() or self._volume.value() == 0
        self._mute.setIcon(self._icon("volume-muted" if silent else "volume"))
        self._mute.setAccessibleName("Unmute" if silent else "Mute")
        self._volume.setAccessibleName("Volume")

    def set_volume(self, level: float, muted: bool = False) -> None:
        """Show a remembered volume without announcing it as a change."""
        for w in (self._volume, self._mute):
            w.blockSignals(True)
        self._volume.setValue(round(min(max(level, 0.0), 1.0) * 100))
        self._mute.setChecked(muted)
        for w in (self._volume, self._mute):
            w.blockSignals(False)
        self._refresh_mute_icon()

    # ---- what the page tells it ---------------------------------------------------------------
    def show_track(self, title: str, game: str) -> None:
        self._title.set_full_text(title)
        self._game.set_full_text(game)

    def set_art(self, pixmap: QPixmap | None, pending: bool = False) -> None:
        """Show the cover on a disc (cropped square, then round); a blank disc while it loads; nothing when there is none."""
        self._full = pixmap if pixmap is not None and not pixmap.isNull() else None
        self._art.setCursor(Qt.CursorShape.PointingHandCursor if self._full else Qt.CursorShape.ArrowCursor)
        self._art.set_cover(self._full, pending, self.devicePixelRatioF())
        self._art.setVisible(self._full is not None or pending)

    def show_large_art(self) -> None:
        """Open the cover in a bigger window (nothing when there is no cover yet)."""
        if self._full is None:
            return
        ArtPreview(self._full, f"{self._title._full} - {self._game._full}".strip(" -"), self.window()).exec()

    def set_playing(self, playing: bool) -> None:
        self._playing = playing
        self._slider.set_moving(playing)  # the wave ripples while it plays and settles flat when paused
        self._art.set_spinning(playing)  # the disc eases up to speed, and coasts to a stop
        self._play.setIcon(self._icon("pause" if playing else "play"))
        self._play.setAccessibleName("Pause" if playing else "Play")

    def set_can_skip(self, back: bool, forward: bool) -> None:
        self._forward.setEnabled(forward)  # 'previous' always works: it restarts the track when there is none before
        self._back.setAccessibleName("Previous track" if back else "Restart this track")

    def set_duration(self, ms: int) -> None:
        self._duration = max(int(ms), 0)
        self._slider.setRange(0, self._duration)
        self._show_time(self._slider.value())

    def set_position(self, ms: int) -> None:
        if not self._slider.isSliderDown():  # the person is dragging it: don't fight them
            self._slider.setValue(min(max(int(ms), 0), self._slider.maximum()))
        self._show_time(int(ms))

    def _show_time(self, ms: int) -> None:
        self._time.setText(f"{clock(ms)} / {clock(self._duration)}")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < self._slider.geometry().top():
            self.opened.emit()
        super().mousePressEvent(event)
