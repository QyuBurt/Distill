"""The update screen: just the Distill logo, floating with no window around it, grey at first and filling bottom to top with
its own colours as the new version comes in. The arithmetic lives in fillmath.py (no Qt, so it is tested everywhere)."""
from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPointF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QDialog, QMenu, QWidget

from . import fillmath, motion
from .assets import asset
from .icon import app_icon_pixmap
from .theme import T

LOGO = 240  # the picture's side, in logical pixels
SCALE = 2.0  # it is kept at twice that and drawn down, so it stays sharp on a high-DPI screen
RIPPLE_PEAK = 4.0  # px: how tall the ripple on the colour's edge gets at most
DOWNLOAD_SHARE = 0.7  # the download fills the logo this far; the install (a separate window, once Distill has quit) does the rest


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
            mask = QPixmap(self._colour.size())  # a full-size mask: DestinationIn only touches what the source covers, so a bare
            mask.setDevicePixelRatio(SCALE)  # path would leave everything outside it fully coloured
            mask.fill(Qt.GlobalColor.transparent)
            mp = QPainter(mask)
            mp.setRenderHint(QPainter.RenderHint.Antialiasing)
            mp.fillPath(self._edge(frac), QColor(0, 0, 0))
            mp.end()
            lp = QPainter(layer)
            lp.drawPixmap(0, 0, self._colour)
            lp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            lp.drawPixmap(0, 0, mask)
            lp.end()
            p.drawPixmap(0, 0, layer)
        p.end()


class UpdateScreen(QDialog):
    """Shown while a new version downloads: nothing but the logo, floating. There is no frame, no title bar, no text and no
    button; the window is transparent, so the logo's own rounded shape is all that shows. That keeps it identical to the
    install window (update_splash.py), which carries on after Distill has quit and opens in exactly the same place.

    The detail is still there for anyone who wants it: hovering the logo shows the percentage and the line of detail, and
    Esc or a right-click (Cancel update) cancels. `cancelled` fires when the person cancels."""

    cancelled = Signal()

    def __init__(self, parent: QWidget | None, name: str, share: float = DOWNLOAD_SHARE):
        super().__init__(parent)
        self._name = name
        self._share = share
        self._overall = 0.0  # how full the logo is meant to be (download share + install share)
        self._finishing = False  # downloaded: nothing left to cancel
        self._dismissing = False
        self.percent = "0%"  # not drawn: shown in the hover tip (and read by the tests)
        self.status = f"Downloading {name}"
        self.setWindowTitle("Updating")  # the taskbar and Alt+Tab still need a name for it
        self.setObjectName("updateScreen")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("#updateScreen { background: transparent; border: none; }")  # the app style must not paint a box behind it
        self.setModal(True)
        self.setFixedSize(LOGO, LOGO)  # the window is the logo and nothing else
        self.logo = FillLogo(LOGO, self)
        self.logo.move(0, 0)
        self._refresh_tip()

    def _refresh_tip(self) -> None:
        tip = f"{self.percent}  \u00b7  {self.status}"
        self.logo.setToolTip(tip if self._finishing else f"{tip}\nEsc, or right-click, to cancel")

    def _show_overall(self, frac: float) -> None:
        self._overall = min(1.0, max(0.0, frac))
        self.logo.set_fraction(self._overall)
        self.percent = fillmath.percent_text(self._overall)

    def set_progress(self, done: int, total: int) -> None:
        if self._finishing:
            return
        self._show_overall(self._share * fillmath.fraction(done, total))
        self.status = f"Downloading {self._name}  \u00b7  {fillmath.size_text(done, total)}"
        self._refresh_tip()

    def set_installing(self) -> None:
        """The file is in: nothing left to cancel. The logo stays where the download left it; the install window carries it on."""
        self._finishing = True
        self._show_overall(self._share)
        self.status = f"Installing {self._name}. Distill restarts by itself."
        self._refresh_tip()

    def set_install_progress(self, frac: float) -> None:
        """How far the install is, 0..1: it fills the rest of the logo (used when no install window takes over)."""
        self._show_overall(self._share + (1.0 - self._share) * frac)
        self._refresh_tip()

    def can_cancel(self) -> bool:
        return not self._finishing

    def contextMenuEvent(self, event) -> None:
        if not self.can_cancel():
            return
        menu = QMenu(self)
        menu.addAction("Cancel update", self.reject)
        menu.exec(event.globalPos())

    def handoff_spec(self) -> dict:
        """Where and how this screen is drawn, in screen pixels, for the install window to continue from it seamlessly. The
        window is the logo, so a position, a size, how full it is and which picture to use is all there is to say."""
        dpr = self.devicePixelRatioF()
        g = self.geometry()
        return {
            "x": round(g.x() * dpr), "y": round(g.y() * dpr), "w": round(g.width() * dpr), "h": round(g.height() * dpr),
            "start": self._overall,
            "logo": str(asset("icons", f"distill-{'dark' if T.dark else 'bright'}-512.png")),
        }

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
