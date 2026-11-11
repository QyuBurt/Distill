"""Reusable widgets: cards, path rows, navigation, tables."""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (QColor, QFont, QGradient, QImage, QKeyEvent, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen, QPolygonF,
                           QFontMetrics, QPixmap, QWheelEvent)
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QCheckBox, QFileDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QRadioButton, QSizePolicy,
    QScrollArea, QStackedWidget, QStyledItemDelegate, QTableView, QToolButton, QTreeWidget,
    QVBoxLayout, QWidget,
)

from ..constants import APP_DIR
from ..system import resolve_tool_path
from . import icons, motion
from .icon import app_icon_pixmap
from .theme import AlignL, AlignR, AlignV, T


# --- Small building blocks ------------------------------------------------------------------------
class StatusLabel(QLabel):
    """A label for messages that change now and then: each new message fades up instead of blinking in. One that is
    rewritten faster than every 0.4 s (a ticking counter) just updates, so it doesn't flicker (see motion.swap_text)."""

    def setText(self, text: str) -> None:  # noqa: N802 - Qt's name
        motion.swap_text(self, text)


DETAIL_WIDTH = 1060  # a detail page (a game, a recomp, an emulator) is never wider than this: long lines are hard to read


def detail_shell(view: QWidget) -> QVBoxLayout:
    """The frame every detail page is built in, so they all look and behave the same: the page's column, centred at DETAIL_WIDTH at
    most however wide the window is. There is no back button: a detail page floats on the blurred list as a card (see ModalStack),
    and clicking outside the card, or Esc, closes it. Returns the column's layout, to fill."""
    shell = QVBoxLayout(view)
    shell.setContentsMargins(0, 0, 0, 0)
    shell.setSpacing(12)
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch(1)
    page = QWidget()
    page.setMaximumWidth(DETAIL_WIDTH)
    row.addWidget(page, 1000)
    row.addStretch(1)
    shell.addLayout(row, 1)
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(14)
    return lay


def hint(text: str, tip: str = "", live: bool = False) -> QLabel:
    """A small, dimmed note. Keep `text` to a sentence; the full detail can go in `tip` (shown on hover).
    live=True is for a status line whose message changes as things happen (see StatusLabel)."""
    label = StatusLabel(text) if live else QLabel(text)
    label.setObjectName("hint")
    if tip and tip != text:
        label.setToolTip(tip)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return label


def fit_columns(tree: QTreeWidget, flex: int, flex_min: int = 160) -> None:
    """Make every column of a tree draggable. Qt cannot drag the edge of a column that stretches, so no column
    stretches: `flex` instead takes up the slack (and follows the window), never narrower than `flex_min`, until
    the person drags a column edge themselves, after which the widths stay as they set them. The last column
    fills what is left so there is never an empty gap on the right."""
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication, QHeaderView

    hdr = tree.header()
    hdr.setStretchLastSection(True)
    for col in range(tree.columnCount()):
        hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
    hdr.setMinimumSectionSize(40)
    state = {"auto": True, "busy": False}
    tree._fit_state = state

    def refit() -> None:
        if not state["auto"] or state["busy"]:
            return
        state["busy"] = True
        used = sum(tree.columnWidth(c) for c in range(tree.columnCount()) if c != flex)
        tree.setColumnWidth(flex, max(flex_min, tree.viewport().width() - used))
        state["busy"] = False

    def resized(*_args) -> None:
        # Only a drag with the mouse button down counts. Qt also resizes sections by itself (the last one stretching, rows being
        # added), and treating that as a drag switched the fitting off before the table was ever shown, leaving Core squeezed.
        if not state["busy"] and QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            state["auto"] = False  # the person dragged a column: keep their widths from now on

    class _Watch(QObject):
        def eventFilter(self, obj, event) -> bool:
            if event.type() == QEvent.Type.Resize:
                refit()
            return False

    hdr.sectionResized.connect(resized)
    watcher = _Watch(tree)
    tree.viewport().installEventFilter(watcher)
    tree._fit_watch = watcher  # keep it alive with the tree
    QTimer.singleShot(0, refit)


def paint_placeholder(view: QWidget, text: str, icon_name: str = "") -> None:
    """The empty state of a list: an optional line icon, the first line of `text` as a headline and the rest as a dimmed
    description, all centred. Call from a view's paintEvent, after the normal painting."""
    painter = QPainter(view.viewport())
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    area = view.viewport().rect().adjusted(28, 0, -28, 0)
    head, _sep, body = text.partition("\n")
    if not body:  # a single line is a description, not a headline
        head, body = "", head
    body = " ".join(body.split())
    wrap = int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap)
    head_font = QFont(view.font())
    head_font.setWeight(QFont.Weight.DemiBold)
    head_font.setPointSizeF(max(10.5, head_font.pointSizeF()))
    body_font = QFont(view.font())
    head_h = QFontMetrics(head_font).boundingRect(area, wrap, head).height() if head else 0
    body_h = QFontMetrics(body_font).boundingRect(area, wrap, body).height() if body else 0
    glyph = icons.pixmap(icon_name, T.ctl_edge, 40) if icon_name in icons.PATHS else None
    gap, icon_h = 10, 40
    total = (icon_h + gap if glyph else 0) + (head_h + 4 if head else 0) + body_h
    y = area.top() + max(0, (area.height() - total) // 2)
    if glyph is not None:
        painter.drawPixmap(area.center().x() - icon_h // 2, y, glyph)
        y += icon_h + gap
    if head:
        painter.setFont(head_font)
        painter.setPen(T.fg)
        painter.drawText(QRect(area.left(), y, area.width(), head_h), wrap, head)
        y += head_h + 4
    if body:
        painter.setFont(body_font)
        painter.setPen(T.fg3)
        painter.drawText(QRect(area.left(), y, area.width(), body_h), wrap, body)
    painter.end()


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = min(max(t, 0.0), 1.0)
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


class _KeyFocus:
    """For a custom-painted control: remembers whether focus arrived from the keyboard (Tab, Shift+Tab, a shortcut), so
    the focus ring shows for keyboard users and does not linger after a mouse click."""

    _kb_focus = False

    def focusInEvent(self, event) -> None:
        self._kb_focus = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                            Qt.FocusReason.ShortcutFocusReason)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        self._kb_focus = False
        super().focusOutEvent(event)
        self.update()

    def _draw_focus(self, p: QPainter, rect: QRectF, radius: float, gap: float = 2.5) -> None:
        """A thin accent ring `gap` pixels outside `rect`, only while the keyboard has focus here."""
        if self._kb_focus and self.hasFocus():
            p.save()
            p.setPen(QPen(T.accent, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(-gap, -gap, gap, gap), radius + gap, radius + gap)
            p.restore()


class _TogglePaint(_KeyFocus):
    """What AnimCheckBox and AnimRadio share: an eased 0..1 'checked' value, and a little scale pulse. Checking
    bounces up past full size and settles; unchecking shrinks and springs back. Both are skipped (the change is
    instant) when motion is reduced or the control is not on screen."""

    DURATION = 260
    PAD = 2  # room around the box: its bounce overshoots by ~1.4px and the focus ring sits just outside it

    def _anim_setup(self) -> None:
        self._t = 1.0 if self.isChecked() else 0.0  # how checked it looks, 0..1
        self._u = 1.0  # progress of the current bounce, 0..1 (1 = at rest)
        self._kind = 0  # +1 checking, -1 unchecking
        self._from = self._to = self._t
        self._anim = QVariantAnimation(self, duration=self.DURATION)
        self._anim.valueChanged.connect(self._frame)
        self.toggled.connect(self._changed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _changed(self, on: bool) -> None:
        self._anim.stop()
        self._from, self._to, self._kind = self._t, (1.0 if on else 0.0), (1 if on else -1)
        if not motion.on() or not self.isVisible():
            self._t, self._u = self._to, 1.0
            self.update()
            return
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def _frame(self, value) -> None:
        u = float(value)
        eased = 1 - (1 - u) ** 3
        self._t = self._from + (self._to - self._from) * eased
        self._u = u
        self.update()

    def _scale(self) -> float:
        if self._u >= 1.0 or not self._kind:
            return 1.0
        wave = math.sin(math.pi * self._u)
        return 1.0 + 0.20 * wave * (1 - 0.4 * self._u) if self._kind > 0 else 1.0 - 0.16 * wave

    def hitButton(self, pos) -> bool:  # the whole row is the target, label included
        return self.rect().contains(pos)

    def _draw_text(self, p: QPainter, x: float) -> None:
        text = self.text()
        if not text:
            return
        p.setFont(self.font())
        p.setPen(T.fg if self.isEnabled() else T.disabled_fg)
        avail = max(self.width() - x - 2, 10)
        p.drawText(QRectF(x, 0, avail, self.height()), AlignL | AlignV,
                   p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(avail)))

    def _text_x(self) -> float:
        return self.PAD + self.BOX + 9

    def _size(self, minimum: bool) -> QSize:
        fm = self.fontMetrics()
        height = max(self.BOX + 7, fm.height() + 6)
        if not self.text():
            return QSize(self.BOX + 2 * self.PAD, height)
        text_w = 36 if minimum else fm.horizontalAdvance(self.text()) + 2
        return QSize(int(self._text_x()) + text_w, height)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)


class AnimCheckBox(_TogglePaint, QCheckBox):
    """A checkbox that bounces when ticked and shrinks and springs back when cleared."""

    BOX = 17

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._anim_setup()

    def sizeHint(self) -> QSize:
        return self._size(False)

    def minimumSizeHint(self) -> QSize:
        return self._size(True)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled, t = self.isEnabled(), self._t
        box = QRectF(float(self.PAD), (self.height() - self.BOX) / 2, self.BOX, self.BOX)
        p.save()
        p.translate(box.center())
        s = self._scale()
        p.scale(s, s)
        p.translate(-box.center())
        on_fill = T.ctl_on if enabled else T.ctl_on_off
        off_edge = T.accent if (self.underMouse() and enabled) else (T.ctl_edge if enabled else T.ctl_edge_off)
        p.setPen(QPen(_mix(off_edge, on_fill, t), 1))
        p.setBrush(_mix(T.input if enabled else T.ctl_off_fill, on_fill, t))
        p.drawRoundedRect(box, 5, 5)
        if t > 0.02:  # the tick draws itself in as it fills
            pts = [QPointF(box.left() + box.width() * x, box.top() + box.height() * y)
                   for x, y in ((0.24, 0.52), (0.44, 0.72), (0.78, 0.31))]
            seg = [math.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in zip(pts, pts[1:])]
            reach = min(t * 1.15, 1.0) * sum(seg)
            path = QPainterPath(pts[0])
            for a, b, length in zip(pts, pts[1:], seg):
                if reach <= 0:
                    break
                f = min(reach / length, 1.0)
                path.lineTo(QPointF(a.x() + (b.x() - a.x()) * f, a.y() + (b.y() - a.y()) * f))
                reach -= length
            p.setPen(QPen(T.accent_fg, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        p.restore()
        self._draw_focus(p, box, 5, gap=1.5)  # snug: the widget only has 2px to spare
        self._draw_text(p, self._text_x())


class AnimRadio(_TogglePaint, QRadioButton):
    """A radio button whose dot pops in with a bounce and shrinks away when another is chosen."""

    BOX = 17

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._anim_setup()

    def sizeHint(self) -> QSize:
        return self._size(False)

    def minimumSizeHint(self) -> QSize:
        return self._size(True)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled, t = self.isEnabled(), self._t
        ring = QRectF(float(self.PAD), (self.height() - self.BOX) / 2, self.BOX, self.BOX)
        edge_off = T.accent if (self.underMouse() and enabled) else (T.ctl_edge if enabled else T.ctl_edge_off)
        p.setPen(QPen(_mix(edge_off, T.accent if enabled else T.ctl_on_off, t), 1))
        p.setBrush(T.input if enabled else T.ctl_off_fill)
        p.drawEllipse(ring)
        radius = self.BOX * 0.25 * t * self._scale()  # the dot grows with the check and bounces with the pulse
        if radius > 0.3:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.accent if enabled else T.ctl_on_off)
            p.drawEllipse(ring.center(), radius, radius)
        self._draw_focus(p, ring, self.BOX / 2, gap=1.5)
        self._draw_text(p, self._text_x())


class Banner(QFrame):
    """A slim message strip under the page header. It stands in for pop-ups that only need acknowledging: it fades
    on its own, can carry one action button, and never takes the keyboard."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 8, 8)
        lay.setSpacing(10)
        self._icon = QLabel()
        self._text = QLabel()
        self._text.setWordWrap(True)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._action = QPushButton()
        self._action.setFlat(True)
        self._action.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action.clicked.connect(self._run_action)
        self._close = QToolButton()
        self._close.setText("\u2715")
        self._close.setAutoRaise(True)
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.setToolTip("Dismiss")
        self._close.clicked.connect(self._dismiss)
        lay.addWidget(self._icon)
        lay.addWidget(self._text, 1)
        lay.addWidget(self._action)
        lay.addWidget(self._close)
        self._callback = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._dismiss)
        self.hide()

    def _dismiss(self) -> None:
        motion.slide(self, False)

    def show_message(self, text: str, level: str = "info", ms: int = 9000, action: tuple | None = None) -> None:
        """level: "info", "ok", "warn" or "bad". `ms`: how long it stays (0 = until dismissed). `action`: (label,
        callable) for one button."""
        def colour_now():  # looked up again when the theme changes
            return {"ok": T.ok, "warn": T.warn, "bad": T.bad}.get(level, T.accent)

        self._icon.setText({"ok": "\u2713", "warn": "\u26a0", "bad": "\u2717"}.get(level, "\u2139"))
        T.bind_css(self._icon, lambda: f"color: {colour_now().name()}; font-weight: 700;")
        T.bind_css(self, lambda: f"QFrame#banner {{ background: {T.select.name()}; border: 1px solid {T.border.name()}; "
                           f"border-left: 4px solid {colour_now().name()}; border-radius: 6px; }}")
        self._text.setText(text)
        self._callback = action[1] if action else None
        self._action.setText(action[0] if action else "")
        self._action.setVisible(bool(action))
        if not self.isVisible() and (holder := self.parentWidget()) is not None and holder.layout() is not None:
            # a hidden strip has no real width yet and its text wraps: measure it at the width it is about to get
            self.resize(max(holder.layout().contentsRect().width(), 1), self.height())
        motion.slide(self, True)  # opens downwards; a message that replaces another just resizes
        self._timer.stop()
        if ms > 0:
            self._timer.start(ms)

    def _run_action(self) -> None:
        callback, self._callback = self._callback, None
        self._dismiss()
        if callback is not None:
            callback()


class PressButton(_KeyFocus, QPushButton):
    """A secondary button: grey on hover, a little smaller while pressed. `danger` buttons (Stop) turn red on hover."""

    def __init__(self, text: str, danger: bool = False, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.danger = danger
        self._scale = 1.0
        self._anim = QVariantAnimation(self, duration=90)
        self._anim.valueChanged.connect(self._set_scale)
        self.pressed.connect(lambda: self._to(0.95))
        self.released.connect(lambda: self._to(1.0))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(40)

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 44, 40)

    def _to(self, scale: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._scale)
        self._anim.setEndValue(scale)
        self._anim.start()

    def _set_scale(self, value) -> None:
        self._scale = float(value)
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
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.translate(r.center())
        p.scale(self._scale, self._scale)
        p.translate(-r.center())
        hover = self.underMouse() and self.isEnabled()
        if not self.isEnabled():
            (fill, edge), text = T.btn_off, T.disabled_fg
        elif hover and self.danger:
            (fill, edge), text = T.btn_danger, T.bad
        elif self.isDown():
            (fill, edge), text = T.btn_down, T.fg
        elif hover:
            (fill, edge), text = T.btn_hover, T.title
        else:
            (fill, edge), text = T.btn, T.fg
        if self._kb_focus and self.hasFocus() and self.isEnabled():
            edge = T.accent  # keyboard focus: the outline lights up, like a focused input
        p.setPen(QPen(edge, 1))
        p.setBrush(fill)
        p.drawRoundedRect(r, 10, 10)
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(text)
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), self.text())


class AuraButton(_KeyFocus, QPushButton):
    """The Start button. Its gradient slowly drifts and a soft glow surrounds it while a batch runs; both hold still
    while paused and the glow fades when idle. Pressing shrinks it a little, like the other buttons."""

    GLOW = 10  # room kept around the button for the glow, in pixels
    PAUSED_SAT = 0.25  # how much of its colour the button keeps while paused (1 = all of it)

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self._phase = 0.0
        self._running = False
        self._paused = False
        self._glow = 0.30  # how strong the glow is now (0..1), eased towards `_glow_goal`
        self._glow_goal = 0.30
        self._sat = 1.0  # colour saturation now, eased towards 1 (running) or PAUSED_SAT (paused)
        self._sat_anim = QVariantAnimation(self, duration=220)
        self._sat_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._sat_anim.valueChanged.connect(self._set_sat)
        self._scale = 1.0
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(33)
        self._tick_timer.timeout.connect(self._tick)
        self._press = QVariantAnimation(self, duration=90)
        self._press.valueChanged.connect(self._set_scale)
        self.pressed.connect(lambda: self._to(0.96))
        self.released.connect(lambda: self._to(1.0))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(40 + 2 * self.GLOW)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 60 + 2 * self.GLOW, 40 + 2 * self.GLOW)

    # ---- state -------------------------------------------------------------------------------
    def set_state(self, running: bool, paused: bool = False) -> None:
        """Tell the button whether a batch is running, and whether it is paused."""
        self._running, self._paused = running, paused
        self._fade_sat(self.PAUSED_SAT if (running and paused) else 1.0)
        self._glow_goal = (0.45 if paused else 1.0) if running else (0.30 if self.isEnabled() else 0.0)
        moving = running and not paused and motion.on() and self.isVisible()
        if moving and not self._tick_timer.isActive():
            self._tick_timer.start()
        elif not moving:
            self._tick_timer.stop()
        self.update()

    def _fade_sat(self, goal: float) -> None:
        """Ease the colour towards `goal`; it jumps when motion is reduced or the button is not on screen."""
        self._sat_anim.stop()
        if abs(self._sat - goal) < 0.001:
            return
        if not motion.on() or not self.isVisible():
            self._set_sat(goal)
            return
        self._sat_anim.setStartValue(self._sat)
        self._sat_anim.setEndValue(goal)
        self._sat_anim.start()

    def _set_sat(self, value) -> None:
        self._sat = float(value)
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.0035) % 1.0  # one full drift in about nine seconds
        self._glow += (self._glow_goal - self._glow) * 0.12
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.set_state(self._running, self._paused)

    def hideEvent(self, event) -> None:
        self._tick_timer.stop()  # nothing to draw while the page is not on screen
        super().hideEvent(event)

    def _to(self, scale: float) -> None:
        self._press.stop()
        self._press.setStartValue(self._scale)
        self._press.setEndValue(scale)
        self._press.start()

    def _set_scale(self, value) -> None:
        self._scale = float(value)
        self.update()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange and not self._running:
            self._glow_goal = 0.30 if self.isEnabled() else 0.0
            self._glow = self._glow_goal
            self.update()

    # ---- painting ------------------------------------------------------------------------------
    def _gradient(self, body: QRectF, alpha: int = 255, lighten: float = 0.0) -> QLinearGradient:
        length = body.width() * 1.25
        start = body.left() - (self._phase * 2 * length if (self._running and motion.on()) else 0.0)
        grad = QLinearGradient(start, 0, start + length, 0)
        grad.setSpread(QGradient.Spread.ReflectSpread)  # runs back and forth, so the drift never jumps
        for stop, col in zip((0.0, 0.5, 1.0), T.grad):
            c = QColor(col)
            if lighten:
                c = QColor(min(255, c.red() + int((255 - c.red()) * lighten)), min(255, c.green() + int((255 - c.green()) * lighten)),
                           min(255, c.blue() + int((255 - c.blue()) * lighten)))
            if self._sat < 1.0:  # paused: wash the colour out (the glow is built from this gradient too)
                hue, sat, val, _a = c.getHsvF()
                c = QColor.fromHsvF(max(hue, 0.0), sat * self._sat, val)
            c.setAlpha(alpha)
            grad.setColorAt(stop, c)
        return grad

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(self.GLOW, self.GLOW, -self.GLOW, -self.GLOW)
        p.translate(body.center())
        p.scale(self._scale, self._scale)
        p.translate(-body.center())
        live = self._running or self.isEnabled()
        p.setPen(Qt.PenStyle.NoPen)
        strength = self._glow * (1.25 if self.underMouse() and self.isEnabled() else 1.0)
        if strength > 0.01 and live:  # the glow: wider and fainter rings of the same gradient
            for i in range(self.GLOW, 0, -1):
                p.setBrush(self._gradient(body, int(34 * strength * (1 - i / (self.GLOW + 1)) ** 1.6 + 2 * strength)))
                p.drawRoundedRect(body.adjusted(-i, -i + 2, i, i + 2), 10 + i, 10 + i)
        if live:
            hover = self.underMouse() and self.isEnabled() and not self._running
            p.setBrush(self._gradient(body, 255, 0.12 if hover else 0.0))
        else:
            p.setBrush(T.grad_off)
        p.drawRoundedRect(body, 10, 10)
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor("#ffffff") if live else T.disabled_fg)
        p.drawText(body, int(Qt.AlignmentFlag.AlignCenter), self.text())
        self._draw_focus(p, body, 10)


def accent_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("accent", True)
    button.setMinimumHeight(38)
    return button


class Card(QFrame):
    """A Fluent card: a rounded, outlined surface with an optional semibold title."""

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(18, 14, 18, 16)  # the same inset as an Expander's body
        self.lay.setSpacing(10)
        if title:
            row = QHBoxLayout()
            row.setSpacing(10)
            if name := icons.icon_for_title(title):
                glyph = QLabel()
                T.bind(glyph, lambda glyph=glyph, name=name: glyph.setPixmap(icons.pixmap(name, T.accent_text, 18)))
                glyph.setFixedSize(18, 18)
                row.addWidget(glyph)
            label = QLabel(title)
            label.setObjectName("cardTitle")  # sentence case, semibold: its size and weight come from the style sheet
            row.addWidget(label)
            row.addStretch(1)
            self.lay.addLayout(row)

    def add(self, item) -> None:
        if isinstance(item, QWidget):
            self.lay.addWidget(item)
        else:
            self.lay.addLayout(item)


class StepCard(Card):
    """One step of a multi-step Organize page as a card: a numbered badge, the step's name, a switch that takes it in or
    out of the scan, a one-line description and the step's own options. The step whose results are showing gets an
    accent edge, a filled badge and a 'Results below' chip. `switch` is used like a checkable button."""

    def __init__(self, number: int, title: str, blurb: str):
        super().__init__("")
        self._current = False
        self.badge = QLabel(str(number))
        self.badge.setFixedSize(24, 24)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        T.bind_css(self.badge, self._badge_css)
        self.chip = QLabel("Results below")
        self.chip.setObjectName("chip")
        self.chip.setVisible(False)
        self.switch = SwitchToggle()
        self.switch.setChecked(True)
        self.switch.setToolTip("Turn off to leave this step out of the scan")
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self.badge)
        name = QLabel(title)
        name.setObjectName("cardTitle")
        head.addWidget(name)
        head.addWidget(self.chip)
        head.addStretch(1)
        head.addWidget(self.switch)
        self.lay.addLayout(head)
        self.content = QWidget()  # everything under the heading: it greys out when the step is switched off
        self.content_lay = QVBoxLayout(self.content)
        self.content_lay.setContentsMargins(34, 0, 0, 0)  # lines up under the title, not under the badge
        self.content_lay.setSpacing(8)
        self.content_lay.addWidget(hint(blurb))
        self.lay.addWidget(self.content)
        self.switch.toggled.connect(self.content.setEnabled)
        T.bind_css(self, self._card_css)

    def add(self, item) -> None:
        """Into the step's content (so a StepCard takes options the way a Card does)."""
        if isinstance(item, QWidget):
            self.content_lay.addWidget(item)
        else:
            self.content_lay.addLayout(item)

    def set_current(self, on: bool) -> None:
        if on != self._current:
            self._current = on
            self.chip.setVisible(on)
            self.badge.setStyleSheet(self._badge_css())
            self.setStyleSheet(self._card_css())

    def _badge_css(self) -> str:
        if self._current:
            return (f"background: {T.accent.name()}; color: {T.bg.name()}; border: 1px solid {T.accent.name()}; "
                    "border-radius: 12px; font-size: 9pt; font-weight: 700;")
        return (f"background: transparent; color: {T.accent_text.name()}; border: 1px solid {T.border.name()}; "
                "border-radius: 12px; font-size: 9pt; font-weight: 700;")

    def _card_css(self) -> str:
        return f"QFrame#card {{ border: 1px solid {T.accent.name() if self._current else T.card_border.name()}; }}"


class ExpanderHeader(QAbstractButton):
    """The heading row of an Expander: icon, spaced capitals, and a chevron that shows whether it is open."""

    def __init__(self, text: str, icon_name: str | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.icon_name = icon_name
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(220, 48)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        if self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 10) if T.dark else QColor(0, 0, 0, 12))
            p.drawRoundedRect(r.adjusted(4, 4, -4, -4), 10, 10)
        x = 18.0
        if self.icon_name:
            p.drawPixmap(int(x), int(r.center().y() - 9), icons.pixmap(self.icon_name, T.accent_text, 18))
            x += 28
        font = QFont(self.font())  # the same look as a card title: sentence case, semibold, a touch larger than the body
        font.setWeight(QFont.Weight.DemiBold)
        font.setPointSizeF(10.5)
        p.setFont(font)
        p.setPen(T.title)
        p.drawText(QRectF(x, r.top(), r.width() - x - 44, r.height()), AlignL | AlignV, self.text())
        chevron = icons.pixmap("chevron-up" if self.isChecked() else "chevron-down", T.fg3, 18)
        p.drawPixmap(int(r.right() - 34), int(r.center().y() - 9), chevron)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)


class Expander(Card):
    """A card whose body folds away behind a header (icon, capitals, chevron)."""

    def __init__(self, title: str, expanded: bool = False):
        super().__init__("")
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.header = ExpanderHeader(title, icons.icon_for_title(title))
        self.header.setChecked(expanded)
        self.header.toggled.connect(self._toggled)
        self.lay.addWidget(self.header)
        self.divider = QFrame()  # a hairline between the heading and the body, as in the design
        self.divider.setFixedHeight(1)
        T.bind_css(self.divider, lambda: f"background: {T.card_border.name()}; border: none; margin: 0 18px;")
        self.lay.addWidget(self.divider)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(18, 12, 18, 16)
        self.body_lay.setSpacing(10)
        self.lay.addWidget(self.body)
        self._toggled(expanded)

    def add(self, item) -> None:
        """Same as Card.add, but into the foldable body (so Expander and Card are interchangeable)."""
        if isinstance(item, QWidget):
            self.body_lay.addWidget(item)
        else:
            self.body_lay.addLayout(item)

    def _toggled(self, open_: bool) -> None:
        self.header.update()
        self.divider.setVisible(open_)
        motion.slide(self.body, open_)  # eases open and shut; the page around it follows frame by frame
        self.updateGeometry()
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, SizedStack):
            parent = parent.parentWidget()
        if parent is not None:  # whatever sits under the page (the Performance card) follows
            QTimer.singleShot(0, parent.refit)


def link_expanders(*cards: Expander) -> None:
    """Make cards that sit side by side fold and unfold as one. Equal-height columns keep a collapsed card as tall as its open
    neighbour, so a card folding alone leaves an empty frame; folded together the whole row closes down to its headings."""
    def follow(source: Expander, open_: bool) -> None:
        for other in cards:
            if other is not source and other.header.isChecked() != open_:  # setChecked emits only on a change, so this can't loop
                other.header.setChecked(open_)

    for card in cards:
        card.header.toggled.connect(lambda open_, c=card: follow(c, open_))


class PathRow(QWidget):
    """A small label above a text box with a Browse... button, for a folder or a program. (`label_width` is accepted
    for older callers; the label now sits above the box.)"""

    def __init__(self, label: str, folder: bool = True, label_width: int = 112, placeholder: str = "",
                 file_filter: str = "", file_title: str = "Select Executable"):
        super().__init__()
        self.folder = folder
        self.file_filter, self.file_title = file_filter, file_title  # for a file row that isn't a program (a .dat, a list)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self.label = QLabel(label)
        self.label.setObjectName("pathLabel")
        self.edit = QLineEdit()
        self.edit.setClearButtonEnabled(True)
        self.edit.setPlaceholderText(placeholder)
        self.button = QPushButton("Browse...")
        self.button.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.edit, 1)
        row.addWidget(self.button)
        outer.addWidget(self.label)
        outer.addLayout(row)
        self.setAcceptDrops(True)  # drop a folder (or, for a program row, a file) straight onto the row
        self.edit.setAcceptDrops(False)

    def _dropped(self, event) -> Path | None:
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if len(urls) != 1:
            return None
        path = Path(urls[0].toLocalFile())
        if self.folder:
            return path if path.is_dir() else None
        return path if path.is_file() and path.suffix.lower() != ".torrent" else None  # torrents belong to the Download page

    def dragEnterEvent(self, event) -> None:
        if self._dropped(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()  # not ours: the window may want it (a .torrent, a magnet link)

    def dropEvent(self, event) -> None:
        if (path := self._dropped(event)) is not None:
            self.edit.setText(str(path))
            event.acceptProposedAction()
        else:
            event.ignore()

    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:
        self.edit.setText(text)

    def _start_dir(self) -> str:
        current = resolve_tool_path(self.edit.text().strip(" \"'"))
        if current:
            path = Path(current)
            folder = path if path.is_dir() else path.parent
            if folder.is_dir():
                return str(folder)
        return str(APP_DIR)

    def _browse(self) -> None:
        if self.folder:
            picked = QFileDialog.getExistingDirectory(self.window(), "Select Directory", self._start_dir())
        else:
            flt = self.file_filter or ("Programs (*.exe);;All files (*)" if sys.platform == "win32" else "All files (*)")
            picked, _ = QFileDialog.getOpenFileName(self.window(), self.file_title, self._start_dir(), flt)
        if picked:
            self.edit.setText(picked)


class ToolRow(QWidget):
    """A PathRow for a conversion tool, with its live status line underneath."""

    def __init__(self, label: str, label_width: int):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.path = PathRow(label, folder=False, label_width=label_width)
        self.status = hint("", live=True)
        lay.addWidget(self.path)
        lay.addWidget(self.status)


class ToolMarkers(QWidget):
    """Small status dots with a name, bottom right: green = installed / ready, blinking amber = being checked or
    downloaded, red = missing. Shares the sidebar dots' blink phase (NavButton.blink_on). A marker named in
    `set_clickable` is drawn as a chip (a pill that lights up under the pointer) and emits `clicked(name)`."""

    clicked = Signal(str)
    GAP, PAD = 12, 8  # between markers; inside a chip, either side of its dot and name

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[tuple[str, str, str]] = []  # (name, "ok" | "warn" | "bad", tooltip line)
        self._clickable: frozenset[str] = frozenset()
        self._hover = -1  # index of the chip under the pointer
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(20)
        self.setMouseTracking(True)

    def set_states(self, items: list[tuple[str, str, str]]) -> None:
        if items == self._items:
            return
        wider = [i[0] for i in items] != [i[0] for i in self._items]
        self._items = list(items)
        self.setToolTip("\n".join(f"{name}: {note}" for name, _s, note in items))
        if wider:
            self.updateGeometry()
        self.update()

    def set_clickable(self, names) -> None:
        """The markers (by name) that act as buttons."""
        names = frozenset(names)
        if names != self._clickable:
            self._clickable = names
            self._hover = -1
            self.unsetCursor()
            self.updateGeometry()
            self.update()

    def blinking(self) -> bool:
        return any(state == "warn" for _n, state, _t in self._items)

    def _font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(11)
        return font

    def _slots(self) -> list[tuple[str, str, float, float, bool]]:
        """(name, state, x of its dot, width of dot and name, is a chip) for each marker, left to right."""
        fm = QFontMetrics(self._font())
        out, x = [], 0.0
        for name, state, _tip in self._items:
            chip = name in self._clickable
            x += self.PAD if chip else 0
            width = 13 + fm.horizontalAdvance(name)
            out.append((name, state, x, width, chip))
            x += width + (self.PAD if chip else 0) + self.GAP
        return out

    def sizeHint(self) -> QSize:
        slots = self._slots()
        if not slots:
            return QSize(0, 20)
        _n, _s, x, width, chip = slots[-1]
        return QSize(int(x + width + (self.PAD if chip else 0)) + 1, 20)

    def _chip_rect(self, x: float, width: float) -> QRectF:
        return QRectF(x - self.PAD + 0.5, 1.5, width + 2 * self.PAD - 1, self.height() - 3)  # centred on height / 2

    def _chip_at(self, pos) -> int:
        for i, (_n, _s, x, width, chip) in enumerate(self._slots()):
            if chip and self._chip_rect(x, width).contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:
        hover = self._chip_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:
        if self._hover != -1:
            self._hover = -1
            self.unsetCursor()
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and (i := self._chip_at(event.position())) >= 0:
            self.clicked.emit(self._items[i][0])
        else:
            super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(self._font())
        fm = p.fontMetrics()
        for i, (name, state, x, width, chip) in enumerate(self._slots()):
            if chip:  # a pill behind the dot and name; the accent edge shows it is under the pointer
                hot = i == self._hover
                rect = self._chip_rect(x, width)
                p.setPen(QPen(T.accent_text if hot else T.chip_edge, 1))
                p.setBrush(T.chip_bg)
                p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            if state == "ok":
                dot = QColor(T.ok)
            elif state == "warn":
                dot = QColor(T.warn)
                dot.setAlphaF(1.0 if NavButton.blink_on or not motion.on() else 0.25)
            else:
                dot = QColor(T.bad)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot)
            mid = self.height() / 2  # the dot, the lettering and the chip all centre on this line
            p.drawEllipse(QRectF(x, mid - 4, 8, 8))
            p.setPen(T.fg2 if chip else T.fg3)
            p.drawText(QPointF(x + 13, round(mid + fm.capHeight() / 2)), name)  # baseline puts the capitals' middle on `mid`


class ToolChips(QWidget):
    """A row of small chips, one per tool or file a page needs: a green dot when it is ready, a red one when it is missing.
    The chip look of the Files & Torrents footer (dot and name), boxed so they read as a set. Hover a chip for its note."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(8)
        self._shown: list[tuple[str, bool, str]] = []
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_states(self, items: list[tuple[str, bool, str]]) -> None:
        """items: (name, ready, tooltip). Rebuilds only when something changed."""
        if items == self._shown:
            return
        self._shown = list(items)
        while self._lay.count():
            if (w := self._lay.takeAt(0).widget()) is not None:
                w.deleteLater()
        self._lay.addStretch(1)  # first, so the chips sit against the right edge
        for name, ready, note in items:
            chip = QFrame()
            chip.setObjectName("toolChip")
            chip.setToolTip(f"{name}: {note}" if note else name)
            T.bind_css(chip, lambda: f"QFrame#toolChip {{ background: {T.chip_bg.name()}; border: 1px solid {T.chip_edge.name()}; border-radius: 10px; }}")
            row = QHBoxLayout(chip)
            row.setContentsMargins(9, 3, 11, 3)
            row.setSpacing(6)
            dot = QLabel()
            dot.setFixedSize(8, 8)
            T.bind_css(dot, lambda ready=ready: f"background: {(T.ok if ready else T.bad).name()}; border: none; border-radius: 4px; padding: 0; margin: 0;")
            text = QLabel(name)
            T.bind_css(text, lambda: f"background: transparent; border: none; padding: 0; color: {T.fg2.name()}; font-size: 9pt;")
            row.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(text, 0, Qt.AlignmentFlag.AlignVCenter)
            self._lay.addWidget(chip)


class NavButton(QAbstractButton):
    """A navigation-pane item: rounded hover/selected fill, accent indicator pill, status dot and badge."""

    blink_on = True  # flipped by a timer in the window; amber ("being downloaded") dots follow it
    _selected = False
    _sel = 0.0  # how selected it looks, eased 0..1

    def __init__(self, key: str, text: str, role: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._sel_anim = QVariantAnimation(self, duration=200)
        self._sel_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._sel_anim.valueChanged.connect(self._sel_step)
        # role: the Theme colour of the dot (its group's colour); state: "ok" / "warn" / "bad" overrides it for
        # conversion tools, whose dot shows whether the tool is ready
        self.key, self.label, self.role, self.state, self.badge, self.selected = key, text, role, None, "", False
        self.setFixedHeight(36)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(200, 36)

    @property
    def selected(self) -> bool:
        return self._selected

    @selected.setter
    def selected(self, on: bool) -> None:
        on = bool(on)
        if on == self._selected:
            return
        self._selected = on
        goal = 1.0 if on else 0.0
        self._sel_anim.stop()
        if not motion.on() or not self.isVisible():
            self._sel = goal
            self.update()
            return
        self._sel_anim.setStartValue(self._sel)
        self._sel_anim.setEndValue(goal)
        self._sel_anim.start()

    def _sel_step(self, value) -> None:
        self._sel = float(value)
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
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        hovered = self.underMouse() and self.isEnabled()
        sel = self._sel
        p.setPen(Qt.PenStyle.NoPen)
        if hovered and sel < 1.0:  # the hover fill hands over to the selected wash as `sel` rises
            hover = QColor(T.hover)
            hover.setAlpha(round(255 * (1.0 - sel)))
            p.setBrush(hover)
            p.drawRoundedRect(r, 9, 9)
        if sel > 0.01:  # a soft teal wash that fades to the right, with the accent bar growing on the left edge
            start, end = T.wash(round(46 * sel), round(18 * sel))
            fill = QLinearGradient(r.left(), 0, r.right(), 0)
            fill.setColorAt(0.0, start)
            fill.setColorAt(1.0, end)
            p.setBrush(fill)
            p.drawRoundedRect(r, 9, 9)
            bar_h = 18 * sel  # a slim pill inset from the edge, so it stays inside the highlight's rounded corner
            p.setBrush(T.accent)
            p.drawRoundedRect(QRectF(r.left() + 4, r.center().y() - bar_h / 2, 3, bar_h), 1.5, 1.5)
        if self.hasFocus():
            p.setPen(QPen(T.fg3, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 8, 8)
        # the dot is neutral (cyan when selected); only a tool that is missing or still being checked colours it
        if self.state == "ok":
            dot = T.ok  # ready
        elif self.state == "warn":  # its tool is being downloaded or checked: amber, blinking
            dot = QColor(T.warn)
            dot.setAlphaF(1.0 if NavButton.blink_on or not motion.on() else 0.25)
        elif self.state == "bad":
            dot = T.bad  # missing
        else:
            dot = _mix(T.dot_idle, T.accent_text, sel)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot)
        p.drawEllipse(QRectF(r.left() + 16, r.center().y() - 4, 8, 8))
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold if sel > 0.5 else QFont.Weight.Medium)
        p.setFont(font)
        fm = p.fontMetrics()
        badge_w = fm.horizontalAdvance(self.badge) + 12 if self.badge else 0
        text_w = max(20, r.width() - 40 - 10 - badge_w)
        p.setPen(_mix(T.fg, T.accent_text, sel))
        p.drawText(QRectF(r.left() + 38, r.top(), text_w, r.height()), AlignL | AlignV,
                   fm.elidedText(self.label, Qt.TextElideMode.ElideRight, int(text_w)))
        if self.badge:
            p.setPen(T.fg3)
            p.drawText(QRectF(r.right() - badge_w - 6, r.top(), badge_w, r.height()), AlignR | AlignV, self.badge)


class NavHeader(QAbstractButton):
    """A collapsible group heading in the navigation pane."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.label, self.open = text, True
        self.setFixedHeight(32)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 0, -2, 0)
        if self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.hover)
            p.drawRoundedRect(r.adjusted(0, 3, 0, -3), 9, 9)
        font = QFont(self.font())  # a group heading in the sidebar: sentence case and semibold, set apart by weight and colour
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(T.fg3)
        p.drawText(QRectF(r.left() + 16, r.top(), r.width() - 44, r.height()), AlignL | AlignV, self.label)
        chevron = icons.pixmap("chevron-down" if self.open else "chevron-up", T.fg3, 16)
        p.drawPixmap(int(r.right() - 28), int(r.center().y() - 8), chevron)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)


class GearButton(QAbstractButton):
    """The Settings entry at the bottom of the navigation pane: a drawn gear and a label."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.label = text
        self.setFixedHeight(40)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if text:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:  # icon only
            self.setFixedWidth(44)
        self.setToolTip("Settings: tool paths, downloads, logins and torrent privacy")

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self.underMouse() or self.isDown():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.select if self.isDown() else T.hover)
            p.drawRoundedRect(r, 5, 5)
        if self.hasFocus():
            p.setPen(QPen(T.fg, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 4, 4)
        # the gear: eight teeth around a ring
        cx, cy = (r.center().x() if not self.label else r.left() + 22), r.center().y()
        p.save()
        p.translate(cx, cy)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(T.fg, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        for _ in range(8):
            p.drawLine(QPointF(0, -6.2), QPointF(0, -9.2))
            p.rotate(45)
        p.setPen(QPen(T.fg, 1.8))
        p.drawEllipse(QPointF(0, 0), 5.6, 5.6)
        p.drawEllipse(QPointF(0, 0), 2.1, 2.1)
        p.restore()
        if not self.label:
            return
        p.setFont(self.font())
        p.setPen(T.fg)
        p.drawText(QRectF(r.left() + 42, r.top(), r.width() - 46, r.height()), AlignL | AlignV, self.label)


class Logo(QWidget):
    """The app icon at sidebar size. Clicking it switches between the dark and the light look; under the pointer it takes a
    soft glow of the brand gradient, like the Start button while a batch runs."""

    clicked = Signal()
    ICON, GLOW = 36, 9  # the icon's size, and the room kept around it for the glow

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(self.ICON + 2 * self.GLOW, self.ICON + 2 * self.GLOW)
        self._pix = app_icon_pixmap(72)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._glow = 0.0  # 0..1, eased towards the pointer being over it
        self._anim = QVariantAnimation(self, duration=220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_glow)

    def refresh_theme(self) -> None:
        """Swap to the icon colourway that belongs to the current look (the bright one in the light look)."""
        self._pix = app_icon_pixmap(72)
        self.update()

    def _set_glow(self, value) -> None:
        self._glow = float(value)
        self.update()

    def _glow_to(self, goal: float) -> None:
        self._anim.stop()
        if not motion.on():
            self._set_glow(goal)
            return
        self._anim.setStartValue(self._glow)
        self._anim.setEndValue(goal)
        self._anim.start()

    def enterEvent(self, _e) -> None:
        self._glow_to(1.0)

    def leaveEvent(self, _e) -> None:
        self._glow_to(0.0)

    def focusInEvent(self, e) -> None:
        super().focusInEvent(e)
        self._glow_to(1.0)

    def focusOutEvent(self, e) -> None:
        super().focusOutEvent(e)
        if not self.underMouse():
            self._glow_to(0.0)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(self.GLOW, self.GLOW, -self.GLOW, -self.GLOW)
        if self._glow > 0.01:  # wider and fainter rings of the brand colours, as on the Start button
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(self.GLOW, 0, -1):
                fade = (1 - i / (self.GLOW + 1)) ** 1.6
                inner = QColor(T.grad[1])
                inner.setAlpha(int((70 * fade + 3) * self._glow))
                p.setBrush(inner)
                p.drawRoundedRect(body.adjusted(-i, -i, i, i), 10 + i, 10 + i)
        p.drawPixmap(body, self._pix, QRectF(self._pix.rect()))


class ThemeWipe(QWidget):
    """The change between the dark and light looks: a picture of the window as it was is laid over the new one, then
    swept away along a diagonal from the top left to the bottom right, with a thin gradient edge on the sweep."""

    DURATION = 620

    def __init__(self, parent: QWidget, shot: QPixmap, done) -> None:
        super().__init__(parent)
        self._shot, self._done = shot, done
        self._s = 0.0  # how far the edge has travelled along the diagonal, in pixels of x + y
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(parent.rect())
        self._total = float(self.width() + self.height()) + self.EDGE
        self._anim = QVariantAnimation(self, duration=self.DURATION, startValue=0.0, endValue=self._total)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(self._moved)
        self._anim.finished.connect(self._finish)

    EDGE = 90  # the width of the soft gradient edge, measured along x + y

    def start(self) -> None:
        self.show()
        self.raise_()
        self._anim.start()

    def _moved(self, value) -> None:
        self._s = float(value)
        self.update()

    def _finish(self) -> None:
        self.hide()
        self.deleteLater()
        self._done()

    @staticmethod
    def _cut(poly: list[QPointF], k: float, keep_past: bool) -> list[QPointF]:
        """The polygon cut by the line x + y = k, keeping the side past it (x + y >= k) or before it (x + y <= k)."""
        def inside(pt: QPointF) -> bool:
            return pt.x() + pt.y() >= k if keep_past else pt.x() + pt.y() <= k

        out: list[QPointF] = []
        for i, cur in enumerate(poly):
            prev = poly[i - 1]
            if inside(cur) != inside(prev):
                d0, d1 = prev.x() + prev.y() - k, cur.x() + cur.y() - k
                t = d0 / (d0 - d1)
                out.append(QPointF(prev.x() + (cur.x() - prev.x()) * t, prev.y() + (cur.y() - prev.y()) * t))
            if inside(cur):
                out.append(cur)
        return out

    def paintEvent(self, _event) -> None:
        w, h, s = float(self.width()), float(self.height()), self._s
        rect = [QPointF(0, 0), QPointF(w, 0), QPointF(w, h), QPointF(0, h)]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # what is still the old window: everything past the line x + y = s (which starts in the top left corner)
        if (old := self._cut(rect, s, True)):
            path = QPainterPath()
            path.addPolygon(QPolygonF(old))
            path.closeSubpath()
            p.setClipPath(path)
            p.drawPixmap(0, 0, self._shot)
            p.setClipping(False)
        # the bright edge: a band trailing behind the line
        a, b = s - float(self.EDGE), s
        if (band := self._cut(self._cut(rect, a, True), b, False)):
            path = QPainterPath()
            path.addPolygon(QPolygonF(band))
            path.closeSubpath()
            g = QLinearGradient(QPointF(a / 2, a / 2), QPointF(b / 2, b / 2))  # across the band, along the sweep
            c0, c1, c2 = (QColor(x) for x in T.grad)
            c0.setAlpha(0)
            c1.setAlpha(60)
            c2.setAlpha(150)
            g.setColorAt(0.0, c0)
            g.setColorAt(0.7, c1)
            g.setColorAt(1.0, c2)
            p.setClipPath(path)
            p.fillRect(self.rect(), g)


class IconButton(QAbstractButton):
    """A small flat button that shows one line icon (the sidebar's hide / keep-open switch)."""

    def __init__(self, name: str, tip: str = "", size: int = 32, parent: QWidget | None = None):
        super().__init__(parent)
        self.name = name
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(tip)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.underMouse() or self.isDown():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.select if self.isDown() else T.hover)
            p.drawRoundedRect(r, 8, 8)
        if self.hasFocus():
            p.setPen(QPen(T.fg3, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 7, 7)
        pix = icons.pixmap(self.name, T.fg if self.underMouse() else T.fg3, 18)
        p.drawPixmap(int(self.width() / 2 - 9), int(self.height() / 2 - 9), pix)


class SideHost(QWidget):
    """The area the sidebar floats over: tells the window when it changes size, so the sidebar can follow."""

    resized = Signal()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit()


class EdgeHotZone(QWidget):
    """An invisible strip down the left edge of the window: the mouse touching it asks the compact sidebar to come out."""

    entered = Signal()
    left = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)

    def enterEvent(self, event) -> None:
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.left.emit()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # draws nothing, whatever the style sheet says
        pass


class SideShadow(QWidget):
    """A soft shadow on the right of the sidebar while it floats over the page (the page is not pushed aside then)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        peak = 95 if T.dark else 45
        grad = QLinearGradient(0, 0, self.width(), 0)
        grad.setColorAt(0.0, QColor(0, 0, 0, peak))
        grad.setColorAt(0.35, QColor(0, 0, 0, round(peak * 0.35)))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), grad)


class RunBar(QAbstractButton):
    """The strip along the bottom of the window while a job runs on a page you are not looking at: what is running,
    how far it has got, and a click takes you back to its page. The whole strip is the progress bar."""

    HEIGHT = 38

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.title, self.detail, self.value = "", "", 0  # value is 0..1000, like the run panel's bar
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_info(self, title: str, detail: str) -> None:
        if (title, detail) == (self.title, self.detail):
            return
        self.title, self.detail = title, detail
        self.setToolTip(f"Back to {title}")
        self.setAccessibleName(f"{title}: {detail}. Click to go back to it.")
        self.update()

    def set_value(self, value: int) -> None:
        value = max(0, min(1000, int(value)))
        if value != self.value:
            self.value = value
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
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), T.side)
        p.setPen(QPen(T.card_border, 1))
        p.drawLine(0, 0, w, 0)
        fill = w * self.value / 1000
        if fill > 0:
            start, end = T.wash(70, 26)
            wash = QLinearGradient(0, 0, max(w, 1), 0)
            wash.setColorAt(0.0, start)
            wash.setColorAt(1.0, end)
            p.fillRect(QRectF(0, 1, fill, h - 1), wash)
            line = QLinearGradient(0, 0, max(w, 1), 0)
            for stop, col in zip((0.0, 0.5, 1.0), T.grad):
                line.setColorAt(stop, col)
            p.fillRect(QRectF(0, 0, fill, 3), line)
        if self.underMouse():
            hover = QColor(T.hover)
            hover.setAlpha(110)
            p.fillRect(QRectF(0, 1, w, h - 1), hover)
        if self.hasFocus():
            p.setPen(QPen(T.fg3, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(1.5, 2.5, w - 3, h - 4))
        pct = f"{self.value // 10}%"
        bold = QFont(self.font())
        bold.setWeight(QFont.Weight.DemiBold)
        p.setFont(bold)
        pct_w = p.fontMetrics().horizontalAdvance("100%") + 6
        chevron = icons.pixmap("chevron-right", T.fg3, 16)
        p.drawPixmap(int(w - 18 - 16), int(h / 2 - 8), chevron)
        p.setPen(T.accent_text)
        p.drawText(QRectF(w - 18 - 16 - 6 - pct_w, 0, pct_w, h), AlignR | AlignV, pct)
        room = int(w - 18 - 16 - 6 - pct_w - 14 - 18)
        fm = p.fontMetrics()
        title = fm.elidedText(self.title, Qt.TextElideMode.ElideRight, max(room, 40))
        p.setPen(T.fg)
        p.drawText(QRectF(18, 0, max(room, 40), h), AlignL | AlignV, title)
        left = 18 + fm.horizontalAdvance(title) + 12
        if self.detail and left < 18 + room:
            plain = QFont(self.font())
            p.setFont(plain)
            p.setPen(T.fg3)
            p.drawText(QRectF(left, 0, 18 + room - left, h), AlignL | AlignV,
                       p.fontMetrics().elidedText(self.detail, Qt.TextElideMode.ElideRight, int(18 + room - left)))


class SwitchToggle(_KeyFocus, QAbstractButton):
    """A sliding on/off switch: the knob glides across, the track takes the accent colour when on. Space and Enter
    toggle it too. Use it like a checkable button (isChecked / setChecked / toggled)."""

    W, H = 44, 24

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._pos = 0.0  # 0 = knob left (off), 1 = knob right (on)
        self._anim = QVariantAnimation(self, duration=140, startValue=0.0, endValue=1.0)
        self._anim.valueChanged.connect(self._moved)
        self.toggled.connect(self._glide)

    def sizeHint(self) -> QSize:
        return QSize(self.W, self.H)

    def setChecked(self, on: bool) -> None:  # a change made in code lands at once; a click glides
        was = self.isChecked()
        super().setChecked(on)
        if on != was:
            self._anim.stop()
            self._pos = 1.0 if on else 0.0
            self.update()

    def _glide(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _moved(self, value) -> None:
        self._pos = float(value)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(0.5, 0.5, self.W - 1, self.H - 1)
        enabled = self.isEnabled()
        grad = QLinearGradient(track.left(), 0, track.right(), 0)
        for stop, col in zip((0.0, 0.5, 1.0), T.grad):
            grad.setColorAt(stop, col if enabled else T.select)
        if self._kb_focus and self.hasFocus() and enabled:  # the track fills the widget, so the ring sits on its edge
            p.setPen(QPen(QColor(255, 255, 255, 200) if self.isChecked() else T.accent_text, 1.5))
        else:
            p.setPen(QPen(Qt.PenStyle.NoPen) if self.isChecked() else QPen(T.border, 1))
        p.setBrush(grad if self._pos > 0.5 else T.select)
        p.drawRoundedRect(track, self.H / 2, self.H / 2)
        d = self.H - 8
        x = 4 + self._pos * (self.W - 8 - d)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff") if self._pos > 0.5 and enabled else T.fg2)
        p.drawEllipse(QRectF(x, 4, d, d))


class _StageTrack(QWidget):
    """The painted part of a StageSwitch: a track with a stop per choice, a knob that glides, a label under each stop."""

    PAD, TRACK_H, KNOB = 14, 22, 16

    def __init__(self, owner: "StageSwitch"):
        super().__init__(owner)
        self.owner = owner
        self._pos = 0.0  # in stops: 0 = first, 1 = second ...
        self._anim = QVariantAnimation(self, duration=140)
        self._anim.valueChanged.connect(self._moved)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # as wide as its labels like, but it gives way when the column is narrow: the steps squeeze and long labels are elided
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def natural_step(self) -> int:
        fm = self.fontMetrics()
        widest = max((fm.horizontalAdvance(t) for t in self.owner.texts()), default=40)
        if self.owner.compact:  # many stops (1..16, a speed ladder): short labels, narrow steps
            return max(28, widest + 12)
        return max(64, widest + 26)

    def min_step(self) -> int:
        return 24 if self.owner.compact else 44

    def step(self) -> int:
        n = max(1, self.owner.count())
        return max(self.min_step(), min(self.natural_step(), self.width() // n)) if self.width() > 0 else self.natural_step()

    def _height_hint(self) -> int:
        return self.TRACK_H + 6 + self.fontMetrics().height() + 2

    def sizeHint(self) -> QSize:
        return QSize(self.natural_step() * max(1, self.owner.count()), self._height_hint())

    def minimumSizeHint(self) -> QSize:
        return QSize(self.min_step() * max(1, self.owner.count()), self._height_hint())

    def glide_to(self, index: int, animate: bool = True) -> None:
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(float(index))
            self._anim.start()
        else:
            self._pos = float(index)
            self.update()

    def _moved(self, value) -> None:
        self._pos = float(value)
        self.update()

    def _index_at(self, x: float) -> int:
        return min(max(int(x // self.step()), 0), max(self.owner.count() - 1, 0))

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.owner.isEnabled():
            self.owner.setCurrentIndex(self._index_at(e.position().x()))
            self.owner.setFocus(Qt.FocusReason.MouseFocusReason)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # dragging the knob picks stops as it passes them
        if e.buttons() & Qt.MouseButton.LeftButton and self.owner.isEnabled():
            self.owner.setCurrentIndex(self._index_at(e.position().x()))

    def paintEvent(self, _event) -> None:
        n, step = self.owner.count(), self.step()
        if n == 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.owner.isEnabled()
        first, last = step / 2, step * (n - 0.5)
        track = QRectF(first - self.PAD, 0.5, (last - first) + 2 * self.PAD, self.TRACK_H - 1)
        p.setPen(QPen(T.border, 1))
        p.setBrush(T.select)
        p.drawRoundedRect(track, self.TRACK_H / 2, self.TRACK_H / 2)
        knob_x = first + self._pos * step
        if self._pos > 0:  # the part of the track up to the knob is filled with the brand gradient
            fill = QRectF(track.left(), track.top(), knob_x - track.left() + self.KNOB / 2 + 2, track.height())
            grad = QLinearGradient(track.left(), 0, track.right(), 0)
            for stop, col in zip((0.0, 0.5, 1.0), T.grad):
                grad.setColorAt(stop, col if enabled else T.select)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(fill, self.TRACK_H / 2, self.TRACK_H / 2)
        cy = self.TRACK_H / 2
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(n):  # a dot at every stop the knob is not on or past
            if i > self._pos + 0.01:
                p.setBrush(T.fg3)
                p.drawEllipse(QPointF(first + i * step, cy), 2.0, 2.0)
        p.setBrush(QColor("#ffffff") if enabled else T.fg3)
        p.drawEllipse(QPointF(knob_x, cy), self.KNOB / 2, self.KNOB / 2)
        font = QFont(self.font())
        for i, text in enumerate(self.owner.texts()):
            active = i == self.owner.currentIndex()
            font.setBold(active)
            p.setFont(font)
            p.setPen(T.fg if active and enabled else T.fg3)
            rect = QRectF(i * step, self.TRACK_H + 6, step, self.fontMetrics().height() + 2)
            shown = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, max(8, int(step) - 4))  # a squeezed step
            p.drawText(rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), shown)


class StageSwitch(QWidget):
    """A sliding switch with two to four stops ("Default / Fast / Fastest / None"): the multi-choice sibling of
    SwitchToggle, and a drop-in for a QComboBox (addItem, currentIndex, currentText, currentData, setCurrentText,
    findData, currentIndexChanged ...). Each stop can carry a description, shown under the switch for the stop
    that is selected."""

    currentIndexChanged = Signal(int)
    currentTextChanged = Signal(str)
    valueChanged = Signal(int)  # when the stops carry numbers: lets it stand in for a QSpinBox

    def __init__(self, parent: QWidget | None = None, describe: str = "right", compact: bool = False):
        """`describe`: where the selected stop's description goes, "right" (the switch stays centred beside it) or
        "below". `compact`: narrow steps, for a switch with a dozen or more short stops."""
        super().__init__(parent)
        self.compact = compact
        self._items: list[tuple[str, object, str]] = []  # (text, data, description)
        self._index = -1
        self._side = describe == "right"
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # "below" fills the width it is given, so the switch and its description share one centre line
        self.setSizePolicy(QSizePolicy.Policy.Maximum if self._side else QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Maximum)
        lay = QHBoxLayout(self) if self._side else QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16 if self._side else 6)
        self._track = self._make_track()
        self._desc = hint("")
        self._desc.setVisible(False)
        if self._side:
            lay.addWidget(self._track, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addWidget(self._desc, 1, Qt.AlignmentFlag.AlignVCenter)
        else:
            self._desc.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            lay.addWidget(self._track, 0, Qt.AlignmentFlag.AlignHCenter)
            lay.addWidget(self._desc)

    def _make_track(self) -> QWidget:
        return _StageTrack(self)

    # ---- the QComboBox-like surface -----------------------------------------------------------
    def addItem(self, text: str, data=None, description: str = "") -> None:
        self._items.append((text, text if data is None else data, description))
        if description and self._side:  # room for the description beside the switch
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        if self._index < 0:
            self._index = 0
        self._track.updateGeometry()
        self._refresh(animate=False)

    def addItems(self, texts) -> None:
        for t in texts:
            self.addItem(t)

    def texts(self) -> list[str]:
        return [t for t, _d, _x in self._items]

    def count(self) -> int:
        return len(self._items)

    def currentIndex(self) -> int:
        return self._index

    def currentText(self) -> str:
        return self._items[self._index][0] if self._index >= 0 else ""

    def currentData(self):
        return self._items[self._index][1] if self._index >= 0 else None

    def findData(self, data) -> int:
        return next((i for i, (_t, d, _x) in enumerate(self._items) if d == data), -1)

    def findText(self, text: str) -> int:
        return next((i for i, (t, _d, _x) in enumerate(self._items) if t == text), -1)

    def setCurrentText(self, text: str) -> None:
        if (i := self.findText(text)) >= 0:
            self.setCurrentIndex(i)

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < len(self._items) or index == self._index:
            return
        self._index = index
        self._refresh()
        self.currentIndexChanged.emit(index)
        self.currentTextChanged.emit(self.currentText())
        if isinstance(self.currentData(), int):
            self.valueChanged.emit(self.currentData())

    def value(self) -> int:
        """The number the selected stop stands for (stops built with numbers as their data)."""
        data = self.currentData()
        return data if isinstance(data, int) else 0

    def setValue(self, n: int) -> None:
        """Select the stop nearest to this number (the lower one on a tie)."""
        nums = [(abs(d - n), d, i) for i, (_t, d, _x) in enumerate(self._items) if isinstance(d, int)]
        if nums:
            self.setCurrentIndex(min(nums)[2])

    def description(self) -> str:
        return self._items[self._index][2] if self._index >= 0 else ""

    # ---- inside ---------------------------------------------------------------------------------
    def _refresh(self, animate: bool = True) -> None:
        self._track.glide_to(max(self._index, 0), animate)
        text = self.description()
        self._desc.setText(text)
        self._desc.setVisible(bool(text))
        self._track.update()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        step = {Qt.Key.Key_Left: -1, Qt.Key.Key_Up: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Down: 1}.get(e.key())
        if step is None:
            super().keyPressEvent(e)
        else:
            self.setCurrentIndex(min(max(self._index + step, 0), self.count() - 1))

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if e.type() == QEvent.Type.EnabledChange:
            self._track.update()


class _StepTrack(QWidget):
    """The painted part of a StepSwitch: a pill with the current value in the middle and a chevron at each end."""

    H, ARROW_W = 30, 32

    def __init__(self, owner: "StageSwitch"):
        super().__init__(owner)
        self.owner = owner
        self._hover = 0  # -1 over the left chevron, +1 over the right one
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self._value_font())
        widest = max((fm.horizontalAdvance(t) for t in self.owner.texts()), default=40)
        return QSize(max(150, widest + 2 * self.ARROW_W + 16), self.H)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _value_font(self) -> QFont:
        font = QFont(self.font())
        font.setBold(True)
        return font

    def glide_to(self, _index: int, _animate: bool = True) -> None:  # a step is instant: just repaint
        self.update()

    def _zone(self, x: float) -> int:
        return -1 if x < self.ARROW_W else 1 if x > self.width() - self.ARROW_W else 0

    def _can(self, step: int) -> bool:
        return self.owner.isEnabled() and 0 <= self.owner.currentIndex() + step < self.owner.count()

    def _step(self, step: int) -> None:
        if self._can(step):
            self.owner.setCurrentIndex(self.owner.currentIndex() + step)

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.owner.isEnabled():
            self.owner.setFocus(Qt.FocusReason.MouseFocusReason)
            if zone := self._zone(e.position().x()):
                self._step(zone)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        zone = self._zone(e.position().x())
        if zone != self._hover:
            self._hover = zone
            self.update()

    def leaveEvent(self, _e) -> None:
        self._hover = 0
        self.update()

    def wheelEvent(self, e: QWheelEvent) -> None:
        if delta := e.angleDelta().y():
            self._step(1 if delta > 0 else -1)
            e.accept()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.owner.isEnabled()
        pill = QRectF(0.5, 0.5, self.width() - 1, self.H - 1)
        focused = self.owner.hasFocus() and enabled
        p.setPen(QPen(T.accent_text if focused else T.border, 1.5 if focused else 1))
        p.setBrush(T.select)
        p.drawRoundedRect(pill, self.H / 2, self.H / 2)
        for side in (-1, 1):  # a soft disc behind the chevron under the pointer
            if self._hover == side and self._can(side):
                x = 4 if side < 0 else self.width() - self.ARROW_W + 4
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(T.border)
                p.drawEllipse(QRectF(x, 4, self.ARROW_W - 8, self.H - 8))
        cy = self.H / 2
        for side in (-1, 1):
            cx = self.ARROW_W / 2 if side < 0 else self.width() - self.ARROW_W / 2
            usable = self._can(side)
            colour = (T.fg if self._hover == side else T.fg2) if usable else T.fg3
            p.setPen(QPen(colour, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            tip, tail = cx + 3 * side, cx - 3 * side  # the point of the chevron faces its own end
            path = QPainterPath(QPointF(tail, cy - 5))
            path.lineTo(tip, cy)
            path.lineTo(tail, cy + 5)
            p.drawPath(path)
        p.setFont(self._value_font())
        p.setPen(T.fg if enabled else T.fg3)
        text_rect = QRectF(self.ARROW_W, 0, self.width() - 2 * self.ARROW_W, self.H)
        p.drawText(text_rect, int(Qt.AlignmentFlag.AlignCenter), self.owner.currentText())


class StepSwitch(StageSwitch):
    """A StageSwitch for a long ladder of values (a dozen speeds, sixteen connection counts): instead of a track with a
    stop for each, one pill shows the value with a chevron at each end to step down or up. The same API and signals
    as StageSwitch, so it drops in wherever one is used (and is saved the same way). The selected value's
    description sits centred underneath."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, describe="below")

    def _make_track(self) -> QWidget:
        return _StepTrack(self)


class RevealDelegate(QStyledItemDelegate):
    """Lets a list's top-level rows fade in one after another (`stagger`), so a freshly filled list ripples into place
    rather than appearing whole. It works on a tree and on a table. It only changes how rows are painted: no item
    data is touched."""

    SOFT = 48  # px: the soft edge of a left-to-right wipe

    def __init__(self, view: QAbstractItemView):
        super().__init__(view)
        self._view = view
        self._alpha: dict[int, float] = {}  # top-level row -> how visible it is now (absent = fully)
        self._rows: dict[tuple[int, int], tuple[float | None, float]] = {}  # (game, track) -> (wipe front, opacity)
        self._anims: list[QVariantAnimation] = []
        self._timers: list[QTimer] = []  # one per row, owned here: they die with the view and never fire on a dead row
        self._flash: dict[int, tuple[QColor, float]] = {}  # top-level row -> (tint, how strong it still is)
        self._flash_anims: dict[int, QVariantAnimation] = {}
        self._leaving: tuple[QVariantAnimation, list[int], Callable[[], None]] | None = None

    def set_row(self, game: int, row: int, front: float | None, alpha: float) -> None:
        """Paint one track row at `alpha`, and only as far right as `front` (viewport x, with a soft edge) if given."""
        self._rows[(game, row)] = (front, alpha)

    def clear_rows(self, game: int | None = None) -> None:
        """Paint a game's track rows normally again (every game's, when none is given)."""
        for key in [k for k in self._rows if game is None or k[0] == game]:
            del self._rows[key]
        self._view.viewport().update()

    def paint(self, painter, option, index) -> None:
        top, track = index, None
        while top.parent().isValid():  # a child row follows the top-level row it belongs to
            track = (top.parent().row(), top.row())
            top = top.parent()
        alpha = self._alpha.get(top.row(), 1.0) if self._alpha else 1.0
        state = self._rows.get(track) if track is not None and self._rows else None
        if state is not None:
            front, own = state
            if front is not None:  # each cell appears as the sweep reaches its left edge
                own *= max(0.0, min(1.0, (front - option.rect.left()) / self.SOFT))
            alpha *= own
        if alpha <= 0.0:
            return
        if track is None and self._flash and (flash := self._flash.get(top.row())) is not None:
            tint = QColor(flash[0])
            tint.setAlphaF(0.30 * flash[1] * min(alpha, 1.0))
            painter.fillRect(option.rect, tint)  # behind the row, so its text stays crisp
        if alpha >= 1.0:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setOpacity(painter.opacity() * alpha)
        super().paint(painter, option, index)
        painter.restore()

    def stagger(self, rows: int, step: int = 28, cap: int = 12, duration: int = 220, first: int = 0) -> None:
        """Fade in `cap` top-level rows from row `first` (of `rows` in all), each `step` ms after the one before."""
        self.ripple(list(range(first, min(rows, first + cap))), step, duration)

    def ripple(self, rows: list[int], step: int = 28, duration: int = 220, cap: int = 12) -> None:
        """Fade in these top-level rows (at most `cap` of them), each `step` ms after the one before."""
        for anim in self._anims:
            anim.stop()
        for timer in self._timers:
            timer.stop()
            timer.deleteLater()
        self._anims.clear()
        self._timers.clear()
        self._alpha.clear()
        if not motion.on() or not self._view.isVisible() or not self._view.window().isVisible():
            return
        for n, row in enumerate(rows[:cap]):
            self._alpha[row] = 0.0
            anim = QVariantAnimation(self, duration=duration, startValue=0.0, endValue=1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(lambda v, r=row: self._set(r, float(v)))
            anim.finished.connect(lambda r=row: self._set(r, 1.0))
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(anim.start)
            timer.start(n * step)
            self._anims.append(anim)
            self._timers.append(timer)
        self._view.viewport().update()

    def flash(self, row: int, color: QColor, duration: int = 1000) -> None:
        """Wash a top-level row in `color` and let it fade, so a change in one row catches the eye. A row that is
        already flashing starts again."""
        if not motion.on() or not self._view.isVisible() or not self._view.window().isVisible():
            return
        if (old := self._flash_anims.pop(row, None)) is not None:
            old.stop()
        anim = QVariantAnimation(self, duration=duration, startValue=1.0, endValue=0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def step(v) -> None:
            self._flash[row] = (color, float(v))
            self._view.viewport().update()

        def done() -> None:
            self._flash.pop(row, None)
            self._flash_anims.pop(row, None)
            self._view.viewport().update()

        anim.valueChanged.connect(step)
        anim.finished.connect(done)
        self._flash_anims[row] = anim
        step(1.0)
        anim.start()

    def fade_out(self, rows: list[int], then: Callable[[], None], duration: int = 200) -> None:
        """Fade these top-level rows away, then call `then` (which really removes them) - so nothing disappears in a
        blink. Without motion, or with nothing to see, `then` runs at once. A removal still fading is finished first."""
        if self._leaving is not None:
            anim, _rows, pending = self._leaving
            self._leaving = None
            anim.stop()
            pending()
        if not rows or not motion.on() or not self._view.isVisible() or not self._view.window().isVisible():
            then()
            return
        anim = QVariantAnimation(self, duration=duration, startValue=1.0, endValue=0.0)
        anim.setEasingCurve(QEasingCurve.Type.InQuad)

        def step(v) -> None:
            for row in rows:
                self._alpha[row] = float(v)
            self._view.viewport().update()

        def done() -> None:
            self._leaving = None
            then()
            for row in rows:  # the rows are gone (or hidden) now; whatever takes their numbers starts fully visible
                self._alpha.pop(row, None)
            self._view.viewport().update()

        anim.valueChanged.connect(step)
        anim.finished.connect(done)
        self._leaving = (anim, rows, done)
        step(1.0)
        anim.start()

    def _set(self, row: int, alpha: float) -> None:
        if alpha >= 1.0:
            self._alpha.pop(row, None)
        else:
            self._alpha[row] = alpha
        self._view.viewport().update()


class ToggleTable(QTableView):
    """A table where Space asks for every selected row to be ticked / unticked, and Delete asks to remove them."""

    rowsToggled = Signal(list)  # view rows
    deletePressed = Signal()
    placeholder = ""  # shown, dimmed, while the table has no rows
    placeholder_icon = ""  # a line icon (see icons.PATHS) drawn above it

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.placeholder and (model := self.model()) is not None and model.rowCount() == 0:
            paint_placeholder(self, self.placeholder, self.placeholder_icon)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if rows := sorted({ix.row() for ix in self.selectionModel().selectedRows()}):
                self.rowsToggled.emit(rows)
            return
        if event.key() == Qt.Key.Key_Delete and not event.isAutoRepeat():
            self.deletePressed.emit()
            return
        super().keyPressEvent(event)


class PlaceholderTree(QTreeWidget):
    """A tree that shows a dimmed message while it has no rows."""

    placeholder = ""
    placeholder_icon = ""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.placeholder and self.topLevelItemCount() == 0:
            paint_placeholder(self, self.placeholder, self.placeholder_icon)


class CheckTree(QTreeWidget):
    """A tree where Space asks for the current row to be ticked / unticked."""

    spacePressed = Signal(object)  # the QTreeWidgetItem
    placeholder = ""  # shown, dimmed, while the tree has no rows
    placeholder_icon = ""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.placeholder and self.topLevelItemCount() == 0:
            paint_placeholder(self, self.placeholder, self.placeholder_icon)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if (item := self.currentItem()) is not None:
                self.spacePressed.emit(item)
            return
        super().keyPressEvent(event)

class _CornerCap(QWidget):
    """Laid over a modal card: paints the dimmed photo of the app back over the four corners outside the card's 16px curve, with an
    anti-aliased edge (a widget mask is jagged), plus the card's outline. The square content under it shows through everywhere else."""

    def __init__(self, overlay: QWidget):
        super().__init__(overlay)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._img: QImage | None = None
        self._key: tuple | None = None

    def paintEvent(self, _event) -> None:
        ov = self.parentWidget()
        shot = getattr(ov, "_shot", None)
        w, h = self.width(), self.height()
        if shot is None or w < 40 or h < 40:
            return
        dpr = self.devicePixelRatioF()
        key = (w, h, self.x(), self.y(), ov.width(), ov.height(), shot.cacheKey(), dpr, T.card_border.rgba())
        if key != self._key:
            img = QImage(round(w * dpr), round(h * dpr), QImage.Format.Format_ARGB32_Premultiplied)
            img.setDevicePixelRatio(dpr)
            img.fill(0)
            q = QPainter(img)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            q.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            q.translate(-self.x(), -self.y())
            q.drawPixmap(ov.rect(), shot)  # what the overlay paints behind the card
            q.fillRect(ov.rect(), QColor(0, 0, 0, 150))
            q.resetTransform()
            shape = QPainterPath()
            shape.addRoundedRect(QRectF(0, 0, w, h), 16, 16)
            q.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            q.fillPath(shape, QColor(0, 0, 0))  # clear the card's own area, edge anti-aliased
            q.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            edge = QPainterPath()
            edge.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 15.5, 15.5)
            q.setPen(QPen(T.card_border, 1))
            q.setBrush(Qt.BrushStyle.NoBrush)
            q.drawPath(edge)
            q.end()
            self._img, self._key = img, key
        QPainter(self).drawImage(0, 0, self._img)


class _ModalOverlay(QWidget):
    """The blurred, dimmed photo of the app with a detail card floating on it. A click on the dim area (or Esc) asks to close."""

    closeRequested = Signal()

    def __init__(self, parent: QWidget, detail: QWidget):
        super().__init__(parent)
        self._shot: QPixmap | None = None
        self._k = 1.0
        self.card = QFrame(self)
        self.card.setObjectName("modalCard")
        T.bind_css(self.card, lambda: f"QFrame#modalCard {{ background: {T.bg.name()}; border: 1px solid {T.card_border.name()}; border-radius: 16px; }}")
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(1, 1, 1, 1)  # just the card's border: the page's art runs right to the edge
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        detail.setParent(None)
        self.scroll.setWidget(detail)
        detail.setAutoFillBackground(False)
        lay.addWidget(self.scroll)
        self._cap = _CornerCap(self)  # smooths the card's rounded corners over the square content
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()

    def open(self, shot: QPixmap, animate: bool = True) -> None:
        w, h = shot.width(), shot.height()
        small = shot.scaled(max(1, w // 14), max(1, h // 14), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._shot = small.scaled(small.width() * 2, small.height() * 2, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._place()
        self.show()
        self.raise_()
        self.setFocus()
        self.scroll.verticalScrollBar().setValue(0)
        if animate and motion.on():
            motion._fade(self, 0.0, 1.0, 180)

    def _place(self) -> None:
        par = self.parentWidget()
        if par is None:
            return
        self.setGeometry(par.rect())
        w = min(DETAIL_WIDTH + 40, max(320, self.width() - 56))
        h = max(240, self.height() - 56)
        self.card.setGeometry((self.width() - w) // 2, (self.height() - h) // 2, w, h)
        self._cap.setGeometry(self.card.geometry())
        self._cap.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        if self._shot is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(self.rect(), self._shot)
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))

    def mousePressEvent(self, event) -> None:
        if not self.card.geometry().contains(event.position().toPoint()):
            self.closeRequested.emit()
        event.accept()

    def wheelEvent(self, event) -> None:
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.closeRequested.emit()
            return
        super().keyPressEvent(event)


class BackdropView(QWidget):
    """The root of a game's or a recomp's page. It paints the game's background art across the top, a little desaturated, and fades it
    into the page's colour as it comes down behind the first card (like the colour wash on the Emulator page, but with the game's own
    picture)."""
    HEIGHT = 400

    def __init__(self):
        super().__init__()
        self._src: QPixmap | None = None
        self._cache: tuple[tuple, QPixmap] | None = None

    def set_art(self, pix: QPixmap | None) -> None:
        self._src = pix if pix is not None and not pix.isNull() else None
        self._cache = None
        self.update()

    def _prepared(self) -> QPixmap | None:
        if self._src is None or self.width() < 20:
            return None
        w, h = self.width(), self.HEIGHT
        key = (w, self._src.cacheKey(), T.bg.rgb())
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        scaled = self._src.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        crop = scaled.copy((scaled.width() - w) // 2, int((scaled.height() - h) * 0.3), w, h)
        colour = crop.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        grey = colour.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32)
        out = QPixmap(w, h)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.drawImage(0, 0, colour)
        p.setOpacity(0.45)  # a little desaturated: the grey copy laid over the colour one
        p.drawImage(0, 0, grey)
        p.setOpacity(1.0)
        page = QColor(T.bg)
        fade = QLinearGradient(0, 0, 0, h)
        for at, alpha in ((0.0, 95), (0.45, 150), (0.8, 235), (1.0, 255)):
            page.setAlpha(alpha)
            fade.setColorAt(at, QColor(page))
        p.fillRect(0, 0, w, h, fade)
        p.end()
        self._cache = (key, out)
        return out

    def paintEvent(self, _event) -> None:
        pix = self._prepared()
        if pix is not None:
            QPainter(self).drawPixmap(0, 0, pix)


class ModalStack(QStackedWidget):
    """A list page (0) and a detail page (1). Showing the detail does not replace the list: the app is blurred and dimmed and the
    detail floats on it as a card; clicking outside the card (or Esc) emits `dismissed`. Everything else behaves like a stack, so
    currentIndex() is 1 while the card is up."""

    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._detail: QWidget | None = None
        self._overlay: _ModalOverlay | None = None
        self._open = False

    def addWidget(self, widget: QWidget) -> int:
        if self.count() == 1 and self._detail is None:
            self._detail = widget
            return super().addWidget(QWidget())  # a stand-in: the real page lives in the card
        return super().addWidget(widget)

    def widget(self, index: int) -> QWidget:
        if index == 1 and self._detail is not None:
            return self._detail
        return super().widget(index)

    def currentIndex(self) -> int:
        return 1 if self._open else super().currentIndex()

    def setCurrentIndex(self, index: int) -> None:
        if index == 1 and self._detail is not None:
            if not self._open:
                self._open = True
                self._show_overlay(animate=True)
            return
        if self._open:
            self._open = False
            if self._overlay is not None:
                self._overlay.hide()
        super().setCurrentIndex(index)

    def _show_overlay(self, animate: bool) -> None:
        host = self.window().centralWidget() if hasattr(self.window(), "centralWidget") else self.window()
        if host is None or self._detail is None:
            return
        if self._overlay is None or self._overlay.parentWidget() is not host:
            self._overlay = _ModalOverlay(host, self._detail)
            self._overlay.closeRequested.connect(self.dismissed)
        shot = host.grab()
        self._overlay.open(shot, animate)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self._open and self._overlay is not None:
            self._overlay.hide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._open:
            QTimer.singleShot(0, lambda: self._open and self.isVisible() and self._show_overlay(animate=False))



class SizedStack(QStackedWidget):
    """A stack that is only as tall as the page showing (a plain one is as tall as its tallest page).

    A page marked compact (property "compact") also gets its height pinned to its content, so whatever sits below
    the stack follows the cards up and down as they fold. Pages that hold a list meant to fill the space
    (Download, Organize) are left to grow."""

    _MAX = 16777215  # QWIDGETSIZE_MAX

    VEIL_MS, VEIL_LIFT = 200, 10  # how long the old page takes to dissolve, and how far it drifts meanwhile

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _i: self._fit())
        self._veil: QLabel | None = None
        self._veil_dir = -1  # -1 drifts up, +1 down

    def setCurrentWidget(self, widget: QWidget, direction: int = -1) -> None:
        """Switch pages. The page being left is photographed first and laid over the new one, then fades and drifts
        away, so the change is a dissolve instead of a cut. `direction` is which way it drifts: up (-1) when the new
        page is further down the sidebar, down (+1) when it is further up, so the motion says where you went.
        (The photo is not interactive: the new page works at once.)"""
        self._veil_dir = -1 if direction <= 0 else 1
        shot = None
        if (widget is not self.currentWidget() and self.currentWidget() is not None and motion.on()
                and self.isVisible() and self.window().isVisible() and not self.window().isMinimized()):
            self._end_veil()
            shot = self.grab()
        super().setCurrentWidget(widget)
        self._fit()
        if shot is not None and not shot.isNull():
            self._start_veil(shot)

    def _start_veil(self, shot: QPixmap) -> None:
        if self._veil is None:
            veil = self._veil = QLabel(self)
            veil.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            veil.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self._veil_fx = QGraphicsOpacityEffect(veil)
            veil.setGraphicsEffect(self._veil_fx)
            self._veil_anim = QVariantAnimation(self, duration=self.VEIL_MS, startValue=0.0, endValue=1.0)
            self._veil_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._veil_anim.valueChanged.connect(self._veil_step)
            self._veil_anim.finished.connect(self._end_veil)
        dpr = shot.devicePixelRatio() or 1.0
        self._veil.setPixmap(shot)
        self._veil.setGeometry(0, 0, round(shot.width() / dpr), round(shot.height() / dpr))
        self._veil_fx.setOpacity(1.0)
        self._veil.raise_()
        self._veil.show()
        self._veil_anim.start()

    def _veil_step(self, value) -> None:
        t = float(value)
        self._veil_fx.setOpacity(1.0 - t)
        self._veil.move(0, round(self.VEIL_LIFT * t) * self._veil_dir)

    def _end_veil(self) -> None:
        if self._veil is not None:
            self._veil_anim.stop()
            self._veil.hide()
            self._veil.move(0, 0)
            self._veil.setPixmap(QPixmap())

    def _fit(self) -> None:
        motion.release_scroll_hold(self)  # a new page has new content: the old page's held scroll range doesn't apply
        current = self.currentWidget()
        for i in range(self.count()):
            page = self.widget(i)
            page.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Preferred if page is current else QSizePolicy.Policy.Ignored)
        self.refit()

    def refit(self) -> None:
        """Re-measure the visible page and size the stack to it."""
        page = self.currentWidget()
        lay = page.layout() if page is not None else None
        if lay is None or not page.property("compact"):
            self.setMinimumHeight(0)
            self.setMaximumHeight(self._MAX)
            self.updateGeometry()
            return
        lay.activate()
        width = max(self.width(), 1)
        need = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        need = max(need, lay.totalMinimumSize().height())
        if self.minimumHeight() != need or self.maximumHeight() != need:
            self.setFixedHeight(need)
        self.updateGeometry()

    def event(self, e) -> bool:
        done = super().event(e)
        if e.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Resize):
            self.refit()
        return done

    def sizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()
