"""The little player at the bottom of the sidebar: what is playing, previous / play-pause / next, a seek bar, stop."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QSizePolicy, QSlider, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from . import icons, motion
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


class ClickLabel(QLabel):
    """A label that reports left clicks (and keeps them from reaching the player card behind it)."""

    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.pixmap() is not None and not self.pixmap().isNull():
            self.clicked.emit()
            event.accept()
        else:
            super().mousePressEvent(event)


class ArtPreview(QDialog):
    """The cover at a larger size. Click it, or press Esc, to close."""

    def __init__(self, pixmap: QPixmap, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title or "Cover")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
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
        self._art = ClickLabel()  # the cover; hidden when the source has none
        self._full: QPixmap | None = None  # the cover at full size, for the larger preview
        self._art.clicked.connect(self.show_large_art)
        self._art.setObjectName("nowArt")
        self._art.setFixedSize(self.ART, self.ART)
        self._art.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        b.setToolTip(tip)
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

    # ---- volume -------------------------------------------------------------------------------
    def _volume_moved(self, *_args) -> None:
        if self._volume.value() > 0 and self._mute.isChecked() and self.sender() is self._volume:
            self._mute.setChecked(False)  # moving the slider while muted brings the sound back
        self._refresh_mute_icon()
        self.volumeChanged.emit(self._volume.value() / 100, self._mute.isChecked() or self._volume.value() == 0)

    def _refresh_mute_icon(self) -> None:
        silent = self._mute.isChecked() or self._volume.value() == 0
        self._mute.setIcon(self._icon("volume-muted" if silent else "volume"))
        self._mute.setToolTip("Unmute" if silent else "Mute")
        self._volume.setToolTip(f"Volume: {self._volume.value()}%")

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
        """Show the cover (rounded, cropped square); a placeholder while it loads; nothing when there is none."""
        self._full = pixmap if pixmap is not None and not pixmap.isNull() else None
        self._art.setCursor(Qt.CursorShape.PointingHandCursor if self._full else Qt.CursorShape.ArrowCursor)
        self._art.setToolTip("Click for a larger cover" if self._full else "")
        if pixmap is not None and not pixmap.isNull():
            ratio = self.devicePixelRatioF()
            side = int(self.ART * ratio)
            scaled = pixmap.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
            out = QPixmap(side, side)
            out.fill(Qt.GlobalColor.transparent)
            painter = QPainter(out)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(0, 0, side, side, 6 * ratio, 6 * ratio)
            painter.setClipPath(clip)
            painter.drawPixmap((side - scaled.width()) // 2, (side - scaled.height()) // 2, scaled)
            painter.end()
            out.setDevicePixelRatio(ratio)
            self._art.setPixmap(out)
            self._art.setText("")
            self._art.setVisible(True)
        elif pending:
            self._art.setPixmap(QPixmap())
            self._art.setText("\u266a")
            self._art.setVisible(True)
        else:
            self._art.setVisible(False)

    def show_large_art(self) -> None:
        """Open the cover in a bigger window (nothing when there is no cover yet)."""
        if self._full is None:
            return
        ArtPreview(self._full, f"{self._title._full} - {self._game._full}".strip(" -"), self.window()).exec()

    def set_playing(self, playing: bool) -> None:
        self._playing = playing
        self._slider.set_moving(playing)  # the wave ripples while it plays and settles flat when paused
        self._play.setIcon(self._icon("pause" if playing else "play"))
        self._play.setToolTip("Pause" if playing else "Play")

    def set_can_skip(self, back: bool, forward: bool) -> None:
        self._forward.setEnabled(forward)  # 'previous' always works: it restarts the track when there is none before
        self._back.setToolTip("Previous track" if back else "Restart this track")

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
