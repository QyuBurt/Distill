"""A frozen, softened copy of the app laid over the app itself while the update screen is up: the window turns grey and a little
out of focus, so the logo filling in the middle is all that has colour. It is a picture taken once when the screen opens (the app
is modal behind it, so nothing in it changes), faded in, and faded out again when the screen closes."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QRect, Qt, QVariantAnimation
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from . import motion

SOFTEN = 3.0  # logical px: the picture is shrunk by this much and stretched back, which is a gentle blur
FADE_IN_MS, FADE_OUT_MS = 320, 180


def softened(pix: QPixmap, soften: float = SOFTEN) -> QPixmap:
    """The picture in grey and a little blurred, at the same size and pixel ratio."""
    dpr = pix.devicePixelRatio()
    img = pix.toImage().convertToFormat(QImage.Format.Format_Grayscale8)
    w, h = img.width(), img.height()
    step = max(1.0, soften * dpr)  # a screen with more pixels per logical pixel blurs over as many, so it looks the same
    small = img.scaled(max(1, round(w / step)), max(1, round(h / step)), Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    soft = small.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    out = QPixmap.fromImage(soft)
    out.setDevicePixelRatio(dpr)
    return out


class Veil(QWidget):
    """Covers the whole of `window` with its grey, softened picture."""

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._pix = softened(window.grab())  # taken before this widget is shown, so it isn't in its own picture
        self._opacity = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._on_anim)
        self.setGeometry(QRect(0, 0, window.width(), window.height()))

    @classmethod
    def over(cls, window: QWidget) -> "Veil":
        veil = cls(window)
        veil.show()
        veil.raise_()
        veil._fade(1.0, FADE_IN_MS)
        return veil

    def release(self) -> None:
        """Fade away and delete itself."""
        if not motion.on() or not self.isVisible():
            self.deleteLater()
            return
        self._fade(0.0, FADE_OUT_MS, then=self.deleteLater)

    def opacity(self) -> float:
        return self._opacity

    def _fade(self, to: float, ms: int, then=None) -> None:
        self._anim.stop()
        if not motion.on() or not self.isVisible():
            self._opacity = to
            self.update()
            if then:
                then()
            return
        try:
            self._anim.finished.disconnect()
        except (RuntimeError, TypeError):
            pass  # nothing was connected
        if then:
            self._anim.finished.connect(then)
        self._anim.setDuration(ms)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(float(self._opacity))
        self._anim.setEndValue(to)
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._opacity = float(value)
        self.update()

    def paintEvent(self, _event) -> None:
        if self._pix.isNull() or self._opacity <= 0.0:
            return
        p = QPainter(self)
        p.setOpacity(self._opacity)
        p.drawPixmap(self.rect(), self._pix)
        p.end()
