"""While a file or folder is dragged over the window: the window softens behind a slowly turning glow round its edge, in the
brand colours, so it is plain that it can be dropped on.

The glow is a long, faint fade with no outline, and drifts round slowly (about fifteen seconds a lap), fading in and out gently. The window is not blurred
live (that would re-render every widget on every frame): a picture of it is taken once when the drag arrives, blurred, and
laid over the window with the glow on top. The overlay lets every mouse and drag event through to the widgets underneath,
so dropping on a box that takes drops still works. With reduced motion it appears and goes at once and does not turn."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QFont, QFontMetricsF, QPainter, QPainterPath, QPixmap,
                           QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget

from . import motion
from .theme import T


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    """a moved t (0..1) of the way towards b."""
    t = max(0.0, min(1.0, t))
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


class DropGlow(QWidget):
    # What is on screen is built from three cached pictures, so a frame costs two cheap blits and nothing is re-drawn at full size:
    #   _back   the window, blurred and calmed, made once when the drag arrives;
    #   _mask   the shape of the glow (a faint white fade inwards from the edge, no outline), made once at 1/SHRINK size;
    #   _frame  the brand colours turned round and cut to the mask, also at 1/SHRINK size: the only thing redrawn as it turns.
    # The colours are laid out round the window's perimeter (not by angle from its centre), so a wide window shows no one big
    # patch of a single colour, and pink is only a faint accent.
    LAYERS, DEPTH, LAYER_ALPHA = 48, 90.0, 3
    CORNER, CORNER_GROWTH, SPREAD = 34.0, 0.9, 2.0  # corner radius of the shallowest band, its growth with depth, edge crowding
    SHRINK = 4  # the glow pictures are 1/4 size: it is a soft fade, so nothing is lost, and each frame is 16x cheaper
    BLUR_DIV = 7  # the window picture is shrunk to a seventh, then stretched back: a soft blur
    LAP = 0.0022  # of a turn per frame at 30 fps: about fifteen seconds a lap
    CYCLES = 2  # the palette repeats this many times round the edge
    PINK = 0.4  # how far the last brand colour is let into the mix (0 = none)
    FADE_IN, FADE_OUT = 420, 360  # ms
    TINT = (90, 70)  # alpha of the calming wash over the window: dark theme, light theme
    LABEL = "Drop files anywhere"  # centred on the window; "" for none
    LABEL_PT, LABEL_SPACING = 11.5, 0.8  # point size, and the extra space between letters in px
    HALO = 0.55  # how much of the page colour softly backs the label, so it reads without a box

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # drags and clicks reach what is underneath
        self._back: QPixmap | None = None
        self._mask: QPixmap | None = None
        self._frame: QPixmap | None = None
        self._fade, self._target, self._phase = 0.0, 0.0, 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim.valueChanged.connect(self._set_fade)
        self._anim.finished.connect(self._settled)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self.hide()

    # ---- show / hide ----------------------------------------------------------------------------
    def reveal(self) -> None:
        if self._target == 1.0:
            return
        self._target = 1.0
        parent = self.parentWidget()
        self.setGeometry(parent.rect())
        if not self.isVisible():  # a picture of the window as it is, taken while this is not part of it
            self._back = self._calmed(parent.grab())
            self._build()
            self.show()
        self.raise_()
        if motion.on() and not self._timer.isActive():
            self._timer.start()
        self._go(1.0, self.FADE_IN)

    def dismiss(self) -> None:
        if self._target == 0.0:
            return
        self._target = 0.0
        self._go(0.0, self.FADE_OUT)

    def resized(self) -> None:
        """The window changed size while the glow is up: make its pictures again at the new size."""
        self.setGeometry(self.parentWidget().rect())
        if self._back is not None:  # stretch the picture already taken (a new one would include this overlay)
            dpr = self._back.devicePixelRatio()
            self._back = self._back.scaled(self.size() * dpr, Qt.AspectRatioMode.IgnoreAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)
            self._back.setDevicePixelRatio(dpr)
            self._build()

    def _go(self, goal: float, ms: int) -> None:
        self._anim.stop()
        if not motion.on():
            self._set_fade(goal)
            self._settled()
            return
        self._anim.setDuration(ms)
        self._anim.setStartValue(self._fade)
        self._anim.setEndValue(goal)
        self._anim.start()

    def _set_fade(self, value) -> None:
        self._fade = float(value)
        self.update()

    def _settled(self) -> None:
        if self._target == 0.0 and self._fade <= 0.001:
            self._timer.stop()
            self.hide()
            self._back = self._mask = self._frame = None  # new pictures are made next time

    def _tick(self) -> None:
        self._phase = (self._phase + self.LAP) % 1.0
        self._turn()
        self.update()

    # ---- the cached pictures --------------------------------------------------------------------
    @classmethod
    def _blurred(cls, shot: QPixmap) -> QPixmap:
        w, h = shot.width(), shot.height()
        if w < 8 or h < 8:
            return shot
        small = shot.scaled(max(1, w // cls.BLUR_DIV), max(1, h // cls.BLUR_DIV),
                            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        out = small.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        out.setDevicePixelRatio(shot.devicePixelRatio())
        return out

    def _calmed(self, shot: QPixmap) -> QPixmap:
        """The blurred window with the calming wash already laid on it, so painting it is a single blit."""
        out = self._blurred(shot)
        p = QPainter(out)
        tint = QColor(T.bg)
        tint.setAlpha(self.TINT[0] if T.dark else self.TINT[1])
        p.fillRect(out.rect(), tint)
        p.end()
        return out

    def _small(self) -> tuple[int, int]:
        return max(1, self.width() // self.SHRINK), max(1, self.height() // self.SHRINK)

    def _build(self) -> None:
        """The glow's shape: LAYERS faint white bands, each filling everything outside a rounded rectangle pulled in by its
        depth, so the light is strongest at the edge and fades smoothly inwards. White at a tiny alpha, because that stays exact
        in 8 bits (a tiny alpha of a dark colour would round its colour away and shift the hue)."""
        w, h = self.width(), self.height()
        sw, sh = self._small()
        mask = QPixmap(sw, sh)
        mask.fill(Qt.GlobalColor.transparent)
        p = QPainter(mask)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(sw / max(1, w), sh / max(1, h))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, self.LAYER_ALPHA))
        rect = QRectF(0, 0, w, h)
        limit = min(w, h) / 2 - 2
        for n in range(self.LAYERS, 0, -1):
            depth = self.DEPTH * (n / self.LAYERS) ** self.SPREAD
            if depth >= limit:
                continue
            radius = self.CORNER + depth * self.CORNER_GROWTH
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.OddEvenFill)
            path.addRect(rect)
            path.addRoundedRect(rect.adjusted(depth, depth, -depth, -depth), radius, radius)
            p.drawPath(path)
        p.end()
        self._mask = mask
        self._turn()

    def _palette(self) -> list[QColor]:
        c0, c1, c2 = (QColor(c) for c in T.grad)
        return [c0, c1, _blend(c1, c2, self.PINK), c1]

    def _turn(self) -> None:
        """The colours, turned to the current phase and cut to the glow's shape."""
        if self._mask is None:
            return
        sw, sh = self._mask.width(), self._mask.height()
        n = 96  # the colours are laid out on a square, then stretched to the window: even along its edge whatever its shape
        grad = QConicalGradient(n / 2, n / 2, -self._phase * 360.0)  # turns clockwise
        pal = self._palette()
        steps = 24 * self.CYCLES
        for i in range(steps + 1):
            t = i / steps
            x = (t * self.CYCLES) % 1.0
            x = 1.0 - abs(2.0 * x - 1.0)  # there and back through the palette, so each lap closes without a seam
            x = x * x * (3.0 - 2.0 * x)  # smoothstep: no kink where the direction turns
            pos = x * (len(pal) - 1)
            k = min(int(pos), len(pal) - 2)
            grad.setColorAt(t, _blend(pal[k], pal[k + 1], pos - k))
        frame = self._frame if self._frame is not None and self._frame.size() == self._mask.size() else QPixmap(sw, sh)
        frame.fill(Qt.GlobalColor.transparent)
        p = QPainter(frame)
        p.scale(sw / n, sh / n)
        p.fillRect(QRectF(0, 0, n, n), QBrush(grad))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        p.resetTransform()
        p.drawPixmap(0, 0, self._mask)
        p.end()
        self._frame = frame

    # ---- painting -------------------------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        if self._fade <= 0.001 or self._back is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setOpacity(self._fade)
        p.drawPixmap(self.rect(), self._back)
        if self._frame is not None:
            p.drawPixmap(self.rect(), self._frame)  # stretched back up: bilinear, so the fade stays smooth
        if self.LABEL:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._paint_label(p, QRectF(self.rect()).center())

    def _paint_label(self, p: QPainter, centre) -> None:
        """The invitation: a quiet label on a soft pool of the page colour, with no box or outline round it."""
        font = QFont(T.text_font)
        font.setPointSizeF(self.LABEL_PT)
        font.setWeight(QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, self.LABEL_SPACING)
        fm = QFontMetricsF(font)
        width = fm.horizontalAdvance(self.LABEL)
        box = QRectF(0, 0, width + 8, fm.height() + 4)
        box.moveCenter(centre)
        radius = width * 0.75 + 30
        halo = QRadialGradient(0, 0, radius)  # fades to nothing by its rim: the label sits in a pool, not a pill
        for stop, alpha in ((0.0, 1.0), (0.35, 0.8), (0.65, 0.32), (1.0, 0.0)):
            col = QColor(T.bg)
            col.setAlpha(round(255 * self.HALO * alpha))
            halo.setColorAt(stop, col)
        p.save()
        p.translate(centre)
        p.scale(1.0, 0.42)  # the pool is a wide oval
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QRectF(-radius, -radius, radius * 2, radius * 2))
        p.restore()
        p.setFont(font)
        text = QColor(T.title)
        text.setAlpha(225)
        p.setPen(text)
        # the last letter is followed by the letter spacing too: shift left by half of it so the text sits truly centred
        p.drawText(box.translated(-self.LABEL_SPACING / 2, 0), int(Qt.AlignmentFlag.AlignCenter), self.LABEL)


class DropGlowFilter(QObject):
    """Watches drags over the window (wherever on it they are) and shows the glow while one that the window would take is in
    progress. Moving from one child widget to the next briefly reads as a leave, so a leave only counts after a short wait."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._win = window
        self._glow = DropGlow(window)
        self._active = False
        self._leave = QTimer(self)
        self._leave.setSingleShot(True)
        self._leave.setInterval(90)
        self._leave.timeout.connect(self._ended)
        QApplication.instance().installEventFilter(self)

    def _wants(self, event) -> bool:
        win = self._win
        try:
            if win._drop_has_files(event):
                return True
            mime = event.mimeData()
            return getattr(win, "current_page", "") == "dl" and (mime.hasUrls() or mime.hasText())  # the Download page takes links
        except Exception:
            return False

    def _ended(self) -> None:
        self._active = False
        self._glow.dismiss()

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Resize and obj is self._win:
            if self._glow.isVisible():
                self._glow.resized()
            return False
        if kind not in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.DragLeave, QEvent.Type.Drop):
            return False
        if not isinstance(obj, QWidget) or obj.window() is not self._win:
            return False
        if kind == QEvent.Type.DragEnter:
            self._active = self._wants(event)
            if self._active:
                self._leave.stop()
                self._glow.reveal()
        elif kind == QEvent.Type.DragMove:
            if self._active:
                self._leave.stop()
                self._glow.reveal()  # no-op while it is already showing
        elif kind == QEvent.Type.DragLeave:
            if self._active:
                self._leave.start()
        else:  # Drop: whatever the drop does, the invitation is over
            self._leave.stop()
            self._ended()
        return False
