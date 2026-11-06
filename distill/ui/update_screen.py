"""The update screen: a big greyscale Distill logo that fills, bottom to top, with the logo's own colours as the new
version comes in. The arithmetic lives in fillmath.py (no Qt, so it is tested everywhere)."""
from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPointF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from . import fillmath, motion
from .icon import app_icon_pixmap
from .theme import T

LOGO = 240  # the picture's side, in logical pixels
SCALE = 2.0  # it is kept at twice that and drawn down, so it stays sharp on a high-DPI screen
RIPPLE_PEAK = 4.0  # px: how tall the ripple on the colour's edge gets at most


def greyscale(pix: QPixmap) -> QPixmap:
    """The same picture in grey, with its transparent corners kept."""
    img = pix.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    grey = img.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32)
    p = QPainter(grey)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)  # keep grey's colour, take the original's shape
    p.drawImage(0, 0, img)
    p.end()
    out = QPixmap.fromImage(grey)
    out.setDevicePixelRatio(pix.devicePixelRatio())
    return out


class FillLogo(QWidget):
    """The Distill icon in grey, with the same icon in colour showing from the bottom up to `fraction`."""

    def __init__(self, side: int = LOGO, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(side, side)
        self._shown = 0.0  # what is drawn right now (eased towards the target)
        self._phase = 0.0
        self._colour = self._grey = QPixmap()
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self._ripple_timer = QTimer(self)
        self._ripple_timer.setInterval(33)
        self._ripple_timer.timeout.connect(self._ripple)
        T.bind(self, self._load)  # the dark and light looks have their own colourway

    def _load(self) -> None:
        px = round(self.width() * SCALE)
        src = app_icon_pixmap(512)
        if src.isNull():
            self._colour = self._grey = QPixmap()
        else:
            colour = src.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            colour.setDevicePixelRatio(SCALE)
            self._colour, self._grey = colour, greyscale(colour)
        self.update()

    def fraction(self) -> float:
        return self._shown

    def set_fraction(self, frac: float) -> None:
        frac = min(1.0, max(0.0, frac))
        self._anim.stop()
        if motion.on() and self.isVisible():
            self._anim.setStartValue(float(self._shown))
            self._anim.setEndValue(frac)
            self._anim.start()
        else:
            self._shown = frac
            self.update()

    def _on_anim(self, value) -> None:
        self._shown = float(value)
        self.update()

    def _ripple(self) -> None:
        if motion.on() and 0.0 < self._shown < 1.0:
            self._phase = (self._phase + 0.12) % (2 * math.pi)
            self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._ripple_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._ripple_timer.stop()

    def _edge(self, frac: float) -> QPainterPath:
        """Everything from the colour's top edge (a gentle ripple, flat when motion is reduced) down to the bottom."""
        w, h = self.width(), self.height()
        y = fillmath.edge_y(0, h, frac)
        amp = fillmath.wave_amplitude(frac, RIPPLE_PEAK) if motion.on() else 0.0
        wave = lambda x: y + amp * math.sin(2 * math.pi * x / (w * 0.6) + self._phase)  # noqa: E731
        path = QPainterPath(QPointF(0, h))
        path.lineTo(0, wave(0))
        for x in range(4, w + 4, 4):
            path.lineTo(min(x, w), wave(min(x, w)))
        path.lineTo(w, h)
        path.closeSubpath()
        return path

    def paintEvent(self, _event) -> None:
        if self._grey.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(0, 0, self._grey)
        frac = self._shown
        if frac >= 1.0:
            p.drawPixmap(0, 0, self._colour)
        elif frac > 0.0:
            layer = QPixmap(self._colour.size())  # the colour, cut to the filled part with an anti-aliased edge
            layer.setDevicePixelRatio(SCALE)
            layer.fill(Qt.GlobalColor.transparent)
            lp = QPainter(layer)
            lp.setRenderHint(QPainter.RenderHint.Antialiasing)
            lp.drawPixmap(0, 0, self._colour)
            lp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            lp.fillPath(self._edge(frac), QColor(0, 0, 0))
            lp.end()
            p.drawPixmap(0, 0, layer)
        p.end()


class UpdateScreen(QDialog):
    """Shown while a new version downloads: the logo, the percentage, one line of detail and a Cancel button.
    `cancelled` fires when the person cancels (the button, Esc or the window's close button)."""

    cancelled = Signal()

    def __init__(self, parent: QWidget | None, name: str):
        super().__init__(parent)
        self._name = name
        self._finishing = False  # downloaded: nothing left to cancel
        self._dismissing = False
        self.setWindowTitle("Updating")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(400)
        self.logo = FillLogo(LOGO, self)
        self.percent = QLabel("0%")
        self.status = QLabel(f"Downloading {name}")
        self.cancel = QPushButton("Cancel")
        self.cancel.setAutoDefault(False)  # Enter must not cancel a download
        self.cancel.clicked.connect(self.reject)
        for label in (self.percent, self.status):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        T.bind_css(self.percent, lambda: f"background: transparent; color: {T.title.name()}; "
                                         f"font-family: \"{T.display_font}\"; font-size: 30pt; font-weight: 700;")
        T.bind_css(self.status, lambda: f"background: transparent; color: {T.fg3.name()}; font-size: 10pt;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(48, 40, 48, 28)
        lay.setSpacing(0)
        lay.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(18)
        lay.addWidget(self.percent)
        lay.addSpacing(2)
        lay.addWidget(self.status)
        lay.addSpacing(24)
        lay.addWidget(self.cancel, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_progress(self, done: int, total: int) -> None:
        if self._finishing:
            return
        frac = fillmath.fraction(done, total)
        self.logo.set_fraction(frac)
        self.percent.setText(fillmath.percent_text(frac))
        self.status.setText(f"Downloading {self._name}  ·  {fillmath.size_text(done, total)}")

    def set_installing(self) -> None:
        """The file is in: the logo is full colour and there is nothing left to cancel."""
        self._finishing = True
        self.cancel.setEnabled(False)
        self.logo.set_fraction(1.0)
        self.percent.setText("100%")
        self.status.setText(f"Installing {self._name}. Distill restarts by itself.")

    def reject(self) -> None:
        if self._finishing and not self._dismissing:
            return  # too late to cancel
        if not self._dismissing:
            self.cancelled.emit()
        super().reject()

    def dismiss(self) -> None:
        """Close without counting it as a cancel (a failure, or a cancel already handled)."""
        self._dismissing = True
        self.reject()
