"""One switch for every animation (Settings > Run Feedback > Reduce Motion), and the few motions the app shares: a
smooth height fold (cards, sidebar groups, banners, the player), a fade for things that appear or go, an eased
progress bar, and a short fade-in for dialogs. Every one of them is instant when motion is reduced, or while the
window is not on screen."""
from __future__ import annotations

import math
import os
import time

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QRect, Qt, QTimer, QVariantAnimation
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QDialog, QGraphicsOpacityEffect, QHeaderView, QLabel,
                               QPlainTextEdit, QProgressBar, QScrollArea, QWidget)

from ..constants import APP_DIR

FULL = 16777215  # QWIDGETSIZE_MAX: "no height limit"


class Motion:
    reduce = False  # set from Settings; when True nothing animates


def on() -> bool:
    return not Motion.reduce


def _freeze(widget: QWidget, width: int, height: int) -> None:
    """Lay the widget's children out at a full size and stop its layout reacting, so that while the widget itself is
    squeezed smaller (and so clips them) they keep their size instead of being crushed together - which is what made
    text overprint itself mid-fold. Always paired with _thaw."""
    lay = widget.layout()
    if lay is None:
        return
    lay.activate()
    lay.setGeometry(QRect(0, 0, max(width, 1), max(height, 1)))
    lay.setEnabled(False)
    widget._frozen_layout = lay


def _thaw(widget: QWidget) -> None:
    lay = getattr(widget, "_frozen_layout", None)
    if lay is not None:
        widget._frozen_layout = None
        lay.setEnabled(True)
        lay.invalidate()


def _settle_layouts(widget: QWidget) -> None:
    """Bring every layout above this widget up to date now, bottom to top, and refit anything that pins a height to
    its content (a `refit` method, as SizedStack has).

    Left alone, a change of size reaches each ancestor as a posted LayoutRequest, one event-loop turn per widget
    boundary - card, page, the stack that is pinned to the page, the column under it. A frame can be painted part-way
    through that, with the card already grown but the pinned height not yet: the content is then clipped by the
    stack instead of the card, the next card sits flush against it, and on the way back a ghost of the old frame is
    left under the header. Doing it here, on every tick of the animation, means no frame is ever painted with the
    layers disagreeing."""
    parent = widget.parentWidget()
    while parent is not None:
        lay = parent.layout()
        if lay is not None:
            lay.activate()
        refit = getattr(parent, "refit", None)
        if callable(refit):
            refit()
        parent = parent.parentWidget()


def _scroll_area_of(widget: QWidget) -> QScrollArea | None:
    """The QScrollArea this widget is scrolled by, if any (widget -> ... -> viewport -> area)."""
    node = widget.parentWidget()
    while node is not None:
        outer = node.parentWidget()
        if isinstance(outer, QScrollArea) and outer.viewport() is node:
            return outer
        node = outer
    return None


class _ScrollHold:
    """Keeps a scroll area's scrollable height from shrinking while something inside it folds shut.

    Without it, closing a card on a page that is scrolled to its end makes the page shorter, the scroll range shrinks,
    and the scroll area slides *everything* down to fill the gap - the cards above and the very header that was just
    clicked, out from under the cursor, a few pixels each frame. Held, the cards above stay exactly where they are and
    only what is below the card moves up. The space that is left shows as blank under the last card, as a short page
    already does.

    The hold lets go as soon as that can't move anything: when the scroll position is back within the page's real
    range (the user scrolled up, or it never needed holding), when the page changes, or when asked."""

    def __init__(self, area: QScrollArea):
        self.area, self.content, self.bar = area, area.widget(), area.verticalScrollBar()
        self.floor = self.content.minimumHeight()
        self.refresh()
        self.bar.valueChanged.connect(self.maybe_release)

    def refresh(self) -> None:
        self.content.setMinimumHeight(max(self.content.height(), self.floor))

    def _real_max(self) -> int:
        """How far the page would scroll with nothing held."""
        lay = self.content.layout()
        if lay is None:
            return 0
        lay.activate()
        width = max(self.content.width(), 1)
        need = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        return max(max(need, lay.totalMinimumSize().height()) - self.area.viewport().height(), 0)

    def maybe_release(self, *_args) -> None:
        if self.bar.value() <= self._real_max():
            self.release()

    def release(self) -> None:
        if getattr(self.area, "_scroll_hold", None) is not self:
            return
        self.area._scroll_hold = None
        try:
            self.bar.valueChanged.disconnect(self.maybe_release)
        except (RuntimeError, TypeError):
            pass
        self.content.setMinimumHeight(self.floor)


def _hold_scroll(widget: QWidget) -> None:
    """Called as something inside a scroll area starts to fold shut (see _ScrollHold)."""
    area = _scroll_area_of(widget)
    if area is None or area.widget() is None:
        return
    bar = area.verticalScrollBar()
    if bar.maximum() <= 0 or bar.value() <= 0:  # nothing scrolls, or it is at the top: nothing to keep in place
        return
    hold = getattr(area, "_scroll_hold", None)
    if hold is None:
        area._scroll_hold = _ScrollHold(area)
    else:
        hold.refresh()


def _fold_ended(widget: QWidget) -> None:
    """Called once a fold has finished: let the hold go if the page is already within its real range."""
    area = _scroll_area_of(widget)
    if area is not None and (hold := getattr(area, "_scroll_hold", None)) is not None:
        hold.maybe_release()


def release_scroll_hold(widget: QWidget) -> None:
    """Drop any hold on the scroll area around this widget now (for when the content is about to change)."""
    area = _scroll_area_of(widget)
    if area is not None and (hold := getattr(area, "_scroll_hold", None)) is not None:
        hold.release()


def _open_width(widget: QWidget) -> int:
    """The width a widget will have once open: its own if it is on screen, else what its parent has to offer (a
    hidden widget's own width is whatever it last had, or a default, and wrapped text measures wrongly against it)."""
    parent = widget.parentWidget()
    if widget.isVisible() or parent is None:
        return max(widget.width(), 1)
    lay = parent.layout()
    margins = lay.contentsMargins() if lay is not None else None
    room = parent.width() - (margins.left() + margins.right() if margins is not None else 0)
    return max(room, widget.minimumWidth(), 1)


def _natural_height(widget: QWidget, width: int, low: int, high: int) -> int:
    """How tall a widget wants to be at this width, within the limits it was given."""
    lay = widget.layout()
    if lay is not None:
        lay.activate()
        want = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
    else:
        want = widget.sizeHint().height()
    return max(min(max(want, widget.minimumSizeHint().height()), high), low, 1)


def _restore_height(widget: QWidget) -> None:
    limits = getattr(widget, "_slide_limits", None)
    if limits is not None:
        widget._slide_limits = None
        widget.setMinimumHeight(limits[0])
        widget.setMaximumHeight(limits[1])


def slide(widget: QWidget, open_: bool, duration: int = 200) -> None:
    """Show or hide `widget` by growing or shrinking its height (instantly when motion is reduced, or while its
    window is not on screen yet). Calling it again mid-way reverses smoothly from where it is, and calling it with
    True on something that is already open resizes it to its new natural height. Its children are laid out at full
    size and clipped rather than squeezed, and its own size limits (a header fixed at 48 px) are put back afterwards."""
    running = getattr(widget, "_slide_anim", None)
    if running is not None:
        running.stop()
    if getattr(widget, "_slide_limits", None) is None:  # not already mid-way: remember what the widget's limits were
        widget._slide_limits = (widget.minimumHeight(), widget.maximumHeight())
    low, high = widget._slide_limits
    _thaw(widget)
    if not on() or not widget.window().isVisible() or (not open_ and not widget.isVisible()):
        _restore_height(widget)
        widget.setVisible(open_)
        return
    if open_:
        start = widget.height() if widget.isVisible() else 0
        width = _open_width(widget)
        target = _natural_height(widget, width, low, high)
        if target < start:
            _hold_scroll(widget)  # resizing smaller
        widget.setMinimumHeight(0)
        widget.setMaximumHeight(start)
        _freeze(widget, width, target)
        widget.setVisible(True)
    else:
        start, target = widget.height(), 0
        _hold_scroll(widget)
        widget.setMinimumHeight(0)
        _freeze(widget, widget.width(), start)
    anim = QVariantAnimation(widget, duration=duration)
    anim.setStartValue(int(start))
    anim.setEndValue(int(target))
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    probe = _Probe(f"slide {'open' if open_ else 'close'} {type(widget).__name__} {start}->{target}px", widget) if _PROBE else None

    def step(v) -> None:
        widget.setMaximumHeight(max(int(v), 0))
        _settle(widget, probe)

    anim.valueChanged.connect(step)

    def finish() -> None:
        if not open_:
            widget.setVisible(False)
        _restore_height(widget)
        _thaw(widget)
        _settle_layouts(widget)
        _fold_ended(widget)
        if probe is not None:
            probe.done()

    anim.finished.connect(finish)
    widget._slide_anim = anim
    anim.start()


def _fade(widget: QWidget, start: float, end: float, duration: int, done=None) -> None:
    """Animate `widget`'s opacity from `start` to `end`, then drop the effect (an idle effect makes Qt draw the
    widget off-screen first, which is wasted work). A widget that already carries someone else's effect is left alone."""
    running = getattr(widget, "_fade_anim", None)
    if running is not None:
        running.stop()
    effect = widget.graphicsEffect()
    if effect is not None and not isinstance(effect, QGraphicsOpacityEffect):
        if done is not None:
            done()
        return
    if effect is None:
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
    effect.setOpacity(start)
    anim = QVariantAnimation(widget, duration=duration, startValue=float(start), endValue=float(end))
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.valueChanged.connect(lambda v: effect.setOpacity(float(v)))

    def finish() -> None:
        if widget.graphicsEffect() is effect:
            widget.setGraphicsEffect(None)
        if done is not None:
            done()

    anim.finished.connect(finish)
    widget._fade_anim = anim
    widget._fade_goal = end > 0.5
    anim.start()


def _animating(widget: QWidget) -> bool:
    anim = getattr(widget, "_fade_anim", None)
    return anim is not None and anim.state() == QVariantAnimation.State.Running


def fade(widget: QWidget, show: bool, duration: int = 160) -> None:
    """Show or hide `widget` with a fade rather than a cut. Safe to call again and again with the same answer: it only
    acts when something changes. (Use `slide` instead where the space itself should open and close.)"""
    shown = widget.isVisibleTo(widget.window())
    closing = _animating(widget) and not widget._fade_goal
    if not on() or not widget.window().isVisible():
        running = getattr(widget, "_fade_anim", None)
        if running is not None:
            running.stop()
        if isinstance(widget.graphicsEffect(), QGraphicsOpacityEffect):
            widget.setGraphicsEffect(None)
        widget.setVisible(show)
        return
    if show:
        if shown and not closing:
            return
        widget.setVisible(True)
        _fade(widget, 0.0, 1.0, duration)
    else:
        if not shown or closing:
            return
        _fade(widget, 1.0, 0.0, duration, done=lambda: widget.setVisible(False))


def reveal(widget: QWidget, duration: int = 220) -> None:
    """Fade a widget that is already on screen in from nothing, for content that has just been filled."""
    if on() and widget.isVisible() and widget.window().isVisible():
        _fade(widget, 0.0, 1.0, duration)


MOTION_LOG = APP_DIR / "distill_motion.txt"
_PROBE = bool(os.environ.get("DISTILL_MOTION_DEBUG"))


class _Probe:
    """Times one fold, for finding out where a janky one spends its frames. Off unless DISTILL_MOTION_DEBUG is set;
    each finished fold then appends a line to distill_motion.txt: how many frames it got, the longest wait between
    two of them, and how long the layouts took to settle on each."""

    def __init__(self, label: str, widget: QWidget | None = None):
        self.label = label
        self.start = self.last = time.perf_counter()
        self.frames = 0
        self.worst_gap = self.settle_total = self.worst_settle = 0.0
        area = _scroll_area_of(widget) if widget is not None else None
        self.bar = area.verticalScrollBar() if area is not None else None
        self.scrolls: list[int] = [self.bar.value()] if self.bar is not None else []

    def frame(self, settle_seconds: float) -> None:
        if self.bar is not None:
            self.scrolls.append(self.bar.value())
        now = time.perf_counter()
        self.worst_gap = max(self.worst_gap, now - self.last)
        self.last = now
        self.frames += 1
        self.settle_total += settle_seconds
        self.worst_settle = max(self.worst_settle, settle_seconds)

    def done(self) -> None:
        took = (time.perf_counter() - self.start) * 1000
        scroll = f", page scroll {min(self.scrolls)}..{max(self.scrolls)} (moved {self.scrolls[-1] - self.scrolls[0]:+d})" \
            if self.scrolls else ""
        line = (f"{time.strftime('%H:%M:%S')}  {self.label}: {self.frames} frames in {took:.0f} ms, longest gap "
                f"{self.worst_gap * 1000:.0f} ms, layout settle {self.settle_total * 1000:.0f} ms total / "
                f"{self.worst_settle * 1000:.0f} ms worst{scroll}\n")
        try:
            with open(MOTION_LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def _settle(widget: QWidget, probe: _Probe | None) -> None:
    if probe is None:
        _settle_layouts(widget)
        return
    began = time.perf_counter()
    _settle_layouts(widget)
    probe.frame(time.perf_counter() - began)


class _Presence(QObject):
    """Opens or closes one widget by growing or shrinking it along an axis while it fades, driven by a single
    animation so the two can't drift apart. The widget's own size limits are put back exactly afterwards (a bar
    fixed at 12 px stays 12 px), and asking for the opposite mid-way reverses from where it is."""

    def __init__(self, widget: QWidget, axis: str):
        super().__init__(widget)
        self.w, self.axis = widget, axis
        self.goal = widget.isVisibleTo(widget.window())
        self.t = 1.0 if self.goal else 0.0  # how open it is, 0..1
        self.target = 1  # its size along the axis when fully open
        self.saved: tuple[int, int] | None = None  # (minimum, maximum) along the axis while it is being folded
        self.effect: QGraphicsOpacityEffect | None = None
        self.fade = True
        self.anim = QVariantAnimation(self)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.valueChanged.connect(self._step)
        self.anim.finished.connect(self._done)

    # -- what the widget is allowed to measure along the axis ---------------------------------------------------
    def _limits(self) -> tuple[int, int]:
        w = self.w
        return (w.minimumHeight(), w.maximumHeight()) if self.axis == "height" else (w.minimumWidth(), w.maximumWidth())

    def _set_limits(self, low: int, high: int) -> None:
        w = self.w
        if self.axis == "height":
            w.setMinimumHeight(low)
            w.setMaximumHeight(high)
        else:
            w.setMinimumWidth(low)
            w.setMaximumWidth(high)

    def _natural(self, low: int, high: int) -> int:
        """How big it wants to be along the axis when open, within the limits it was given."""
        w, lay = self.w, self.w.layout()
        if self.axis == "height":
            return _natural_height(w, _open_width(w), low, high)
        if lay is not None:
            lay.activate()
        want = lay.totalSizeHint().width() if lay is not None else w.sizeHint().width()
        return max(min(max(want, w.minimumSizeHint().width()), high), low, 1)

    # -- the animation ---------------------------------------------------------------------------------------------
    def set(self, on: bool, animate: bool, duration: int, fade: bool = True) -> None:
        self.fade = fade
        if self.anim.state() == QVariantAnimation.State.Running:
            if animate and on == self.goal:
                return
            self.anim.stop()
        elif on == self.w.isVisibleTo(self.w.window()):
            self.goal = on
            return
        if animate:
            self._begin(on, duration)
        else:
            self._finish(on)

    def _begin(self, on: bool, duration: int) -> None:
        w = self.w
        if self.saved is None:  # from rest: measure it first
            self.saved = self._limits()
            opening = not w.isVisibleTo(w.window())
            extent = w.height() if self.axis == "height" else w.width()
            self.target = self._natural(*self.saved) if opening else max(extent, 1)
            self.t = 0.0 if opening else 1.0
            if not on and not opening and self.axis == "height":
                _hold_scroll(w)
            across = (_open_width(w) if opening else max(w.width(), 1)) if self.axis == "height" else \
                (max(w.sizeHint().height(), 1) if opening else max(w.height(), 1))
            self._own_effect()
            self._apply(self.t)
            _freeze(w, across if self.axis == "height" else self.target, self.target if self.axis == "height" else across)
            if opening:
                w.setVisible(True)
        else:
            self._own_effect()
        self.goal = on
        end = 1.0 if on else 0.0
        self.anim.setStartValue(self.t)
        self.anim.setEndValue(end)
        self.anim.setDuration(max(60, round(duration * abs(end - self.t))))
        self.anim.start()

    def _own_effect(self) -> None:
        """Fade with an opacity effect of our own, unless the widget already carries someone else's."""
        if self.fade and self.effect is None and self.w.graphicsEffect() is None:
            self.effect = QGraphicsOpacityEffect(self.w)
            self.effect.setOpacity(self.t)
            self.w.setGraphicsEffect(self.effect)

    def _apply(self, t: float) -> None:
        size = max(round(self.target * t), 0)
        if self.axis == "height":
            self.w.setMinimumHeight(0)
            self.w.setMaximumHeight(size)
        else:
            self.w.setMinimumWidth(0)
            self.w.setMaximumWidth(size)
        if self.effect is not None:
            try:
                self.effect.setOpacity(t)
            except RuntimeError:  # something else replaced the effect, and Qt deleted ours
                self.effect = None

    def _step(self, value) -> None:
        self.t = float(value)
        self._apply(self.t)
        _settle_layouts(self.w)

    def _done(self) -> None:
        self._finish(self.goal)

    def _finish(self, on: bool) -> None:
        """Land in the final state at once: put every limit back, drop the effect, show or hide."""
        if not on:
            self.w.setVisible(False)
        if self.saved is not None:
            self._set_limits(*self.saved)
            self.saved = None
        _thaw(self.w)
        if self.effect is not None:
            try:
                if self.w.graphicsEffect() is self.effect:
                    self.w.setGraphicsEffect(None)
            except RuntimeError:
                pass
            self.effect = None
        if on:
            self.w.setVisible(True)
        self.goal, self.t = on, 1.0 if on else 0.0
        _settle_layouts(self.w)
        _fold_ended(self.w)


def _on_screen(widget: QWidget) -> bool:
    parent = widget.parentWidget()
    return on() and parent is not None and parent.isVisible() and widget.window().isVisible()


def set_visible(widget: QWidget, shown: bool, *, axis: str = "height", animate: bool = True, duration: int = 200,
                fade: bool = True) -> None:
    """Show or hide `widget` so the space opens or closes and it fades, rather than popping in and out.

    axis is "height" for something in a column and "width" for something in a row. Safe to call again and again with
    the same answer (it only acts when something changes), and mid-way it reverses from where it is. It is instant
    when motion is reduced, when the widget's page is not on screen, or with animate=False - pass that when the
    thing being shown has just been swapped for another, so the change isn't mistaken for an event. fade=False folds
    without the opacity effect (for many widgets at once, where an effect on each would cost more than it gives).
    (`slide` folds without fading and `fade` fades without moving the layout; this does both.)"""
    presence = getattr(widget, "_presence", None)
    if presence is None:
        if widget.isVisibleTo(widget.window()) == shown:
            return
        if not (animate and _on_screen(widget)):
            widget.setVisible(shown)
            return
        presence = widget._presence = _Presence(widget, axis)
    presence.set(shown, animate and _on_screen(widget), duration, fade)


def swap_text(label: QLabel, text: str, duration: int = 170) -> None:
    """Set a label's text so the change is felt: the new words fade up from faint rather than replacing the old in a
    blink. For status lines that change now and then; one that changes faster than every 0.4 s just updates, so a
    ticking message doesn't flicker."""
    if text == label.text():
        return
    QLabel.setText(label, text)  # the base setter: a StatusLabel's own setText is what called us
    now = time.monotonic()
    quick = now - getattr(label, "_swapped_at", 0.0) < 0.4
    label._swapped_at = now
    if not quick and on() and label.isVisible() and label.window().isVisible():
        _fade(label, 0.15, 1.0, duration)


def _pulse(bar: QProgressBar) -> None:
    """A bar that has just filled dips and returns, a quiet 'done'."""
    if on() and bar.isVisible() and bar.maximum() > bar.minimum() and bar.value() >= bar.maximum():
        _fade(bar, 0.45, 1.0, 420)


def ease_value(bar: QProgressBar, target: int, duration: int = 180) -> None:
    """Move a progress bar to `target` smoothly. Forward progress glides; a reset or a finish jumps at once."""
    target = int(target)
    running = getattr(bar, "_ease_anim", None)
    if running is not None:
        running.stop()
    current = bar.value()
    if not on() or not bar.isVisible() or target <= current:
        bar.setValue(target)
        return
    anim = QVariantAnimation(bar, duration=duration, startValue=current, endValue=target)
    anim.setEasingCurve(QEasingCurve.Type.OutQuad)
    anim.valueChanged.connect(lambda v: bar.setValue(int(v)) if int(v) > bar.value() else None)  # never backwards
    if target >= bar.maximum() > bar.minimum():
        anim.finished.connect(lambda: _pulse(bar))
    bar._ease_anim = anim
    anim.start()


BUMP_MAX = 16  # the furthest the content can be pulled past its end, in pixels
BUMP_STEP = 7  # how far one wheel notch pulls it before the resistance
BUMP_IDLE_MS = 110  # the wheel has stopped when this long passes without a notch
BUMP_SETTLE_MS = 460  # how long it takes to spring back


def bump_offset(travel: float) -> int:
    """Pixels to pull for this much wheel travel: easy at first, stiffer as it stretches, never past BUMP_MAX."""
    return round(BUMP_MAX * math.tanh(max(travel, 0.0) / BUMP_MAX))


def bump_travel(offset: float) -> float:
    """The wheel travel that produces this pull: bump_offset() run backwards, for picking a pull up mid spring-back."""
    return BUMP_MAX * math.atanh(min(max(offset, 0.0) / BUMP_MAX, 0.999))


class SmoothWheel(QObject):
    """Mouse-wheel scrolling that glides: each notch moves the view STEP pixels (a gentler step than the default) further along,
    and the view chases that target by closing a fixed share of the remaining distance every frame (an exponential ease-out,
    sized by the real time between frames). Because the chase is continuous, a notch that arrives while it is moving only moves
    the target: the speed never restarts, so a fast spin of the wheel is one steady glide instead of a surge for every notch
    (which is what made text in a long list shimmer). Touchpads, which already report smooth pixel deltas, and a view with
    Reduce Motion on, keep the normal behaviour. At the top or bottom the wheel is passed on, so a page around the view still
    scrolls.

    With bounce=True, a wheel that has nowhere left to go also pulls the content a few pixels past its end, with growing
    resistance, and when the wheel stops it springs back and settles into place."""
    STEP = 70
    TAU = 80.0  # ms: the time constant of the chase - about 95% of the way after three of these; a lone notch lands in ~0.3 s
    TICK_MS = 8  # how often it moves (a precise timer; each move is sized by the time really elapsed, so the pace never depends on it)

    def __init__(self, view: QAbstractScrollArea, bounce: bool = False):
        super().__init__(view)
        self.view = view
        if isinstance(view, QAbstractItemView):
            # the glide moves the bar in pixels; an item view's default is rows, where one notch would be 70 of them
            view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view._smooth_wheel = self
        self.bar = view.verticalScrollBar()
        self.bounce = bounce
        self._travel = 0.0  # wheel travel accumulated against the end, signed: + pulls down (at the top), - up
        self._pulled = 0  # how far the viewport is displaced now
        self._home = None  # where the viewport belongs, noted when a pull starts
        self._idle = QTimer(self, singleShot=True, interval=BUMP_IDLE_MS)
        self._idle.timeout.connect(self._settle)
        self._spring = QVariantAnimation(self, duration=BUMP_SETTLE_MS)
        self._spring.setEasingCurve(QEasingCurve.Type.OutBack)
        self._spring.valueChanged.connect(lambda v: self._place(round(float(v))))
        self._spring.finished.connect(self._landed)
        self.target: float | None = None
        self._pos = 0.0  # where the glide has got to, in fractional pixels (the bar itself only takes whole ones)
        self._last = 0.0
        self._anim = QTimer(self, interval=self.TICK_MS)  # the glide's clock: stop() ends it where it is
        self._anim.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim.timeout.connect(self._step)
        view.viewport().installEventFilter(self)

    def nudge(self, notches: float) -> bool:
        """Move by `notches` wheel notches (positive = up). False when there is nowhere to go in that direction."""
        running = self._anim.isActive() and self.target is not None
        base = self.target if running else float(self.bar.value())
        new = min(max(base - notches * self.STEP, float(self.bar.minimum())), float(self.bar.maximum()))
        if new == base:
            return False
        if not running:  # a glide that is already moving carries on from where it is: only its target changes
            self._pos = float(self.bar.value())
            self._last = time.monotonic()
        self.target = new
        if not self._anim.isActive():
            self._anim.start()
        return True

    def _step(self) -> None:
        now = time.monotonic()
        dt = min(now - self._last, 0.05)  # (a long stall must not become one big jump)
        self._last = now
        if self.target is None:
            self._anim.stop()
            return
        gap = self.target - self._pos
        arrived = abs(gap) < 0.5
        self._pos = self.target if arrived else self._pos + gap * (1.0 - math.exp(-dt * 1000.0 / self.TAU))
        self.bar.setValue(round(self._pos))
        if arrived:
            self.target = None
            self._anim.stop()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Wheel and on():
            dy = event.angleDelta().y()
            if event.pixelDelta().isNull() and dy and self.nudge(dy / 120):
                self._settle()  # it is moving again, so any pull lets go
                event.accept()
                return True
            if self.bounce and dy:
                self._pull(dy)  # nowhere left to go this way; the event is still passed on
        return False

    # -- the bounce ------------------------------------------------------------------------------------------------------------
    def _pull(self, dy: int) -> None:
        up = dy > 0
        if self.bar.maximum() <= self.bar.minimum():  # everything fits: there is no end to push against
            return
        if not (self.bar.value() <= self.bar.minimum() if up else self.bar.value() >= self.bar.maximum()):
            return
        sign = 1 if up else -1
        if self._spring.state() == QVariantAnimation.State.Running:  # caught mid spring-back: carry on from where it is
            self._spring.stop()
            self._travel = math.copysign(bump_travel(abs(self._pulled)), self._pulled) if self._pulled else 0.0
        if self._travel and (self._travel > 0) != (sign > 0):
            self._travel = 0.0  # the other end: start fresh
        self._travel += sign * abs(dy) / 120 * BUMP_STEP
        self._place(sign * bump_offset(abs(self._travel)))
        self._idle.start()

    def _place(self, offset: int) -> None:
        viewport = self.view.viewport()
        if self._home is None:
            self._home = viewport.pos()
        viewport.move(self._home.x(), self._home.y() + offset)
        self._pulled = offset

    def _settle(self) -> None:
        if not self._pulled:
            return
        self._idle.stop()
        self._spring.stop()
        self._spring.setStartValue(float(self._pulled))
        self._spring.setEndValue(0.0)
        self._spring.start()

    def _landed(self) -> None:
        self._place(0)
        self._home, self._pulled, self._travel = None, 0, 0.0


def smooth_scroll(view: QAbstractScrollArea, bounce: bool = False) -> SmoothWheel:
    """Give a list, table or text view glide-style wheel scrolling (see SmoothWheel); bounce=True adds the pull and
    spring-back at either end. A view that already has it is left alone (and gains the bounce if asked)."""
    if (existing := getattr(view, "_smooth_wheel", None)) is not None:
        existing.bounce = existing.bounce or bounce
        return existing
    return SmoothWheel(view, bounce)


def wants_smooth_scroll(view: QAbstractScrollArea) -> bool:
    """Which scroll surfaces get the glide and bounce when they first appear.

    Not a text box that scrolls in lines (the log follows new output on its own, and a glide target would fight it);
    not a header, which isn't what the wheel scrolls; not the list inside a combo box or a completer, a popup
    that is gone in a moment."""
    if isinstance(view, (QPlainTextEdit, QHeaderView)):
        return False
    return view.window().windowType() != Qt.WindowType.Popup


class _ScrollEverywhere(QObject):
    """Gives every scroll surface the glide the first time it is shown, so a page, a dialog or a list added later is
    covered without anyone remembering to ask."""

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(obj, QAbstractScrollArea) \
                and getattr(obj, "_smooth_wheel", None) is None and wants_smooth_scroll(obj):
            smooth_scroll(obj, bounce=True)
        return False


class _DialogFade(QObject):
    """Fades each dialog window in the first time it is shown (140 ms), so none of them blinks into existence."""

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(obj, QDialog) and obj.isWindow() \
                and not event.spontaneous() and on():
            obj.setWindowOpacity(0.0)
            anim = QVariantAnimation(obj, duration=140, startValue=0.0, endValue=1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(lambda v: obj.setWindowOpacity(float(v)))
            anim.finished.connect(lambda: obj.setWindowOpacity(1.0))
            obj._show_fade = anim
            anim.start()
        return False


def install_dialog_fade(app) -> None:
    app._dialog_fade = _DialogFade(app)
    app.installEventFilter(app._dialog_fade)


def install_smooth_scroll(app) -> None:
    app._scroll_everywhere = _ScrollEverywhere(app)
    app.installEventFilter(app._scroll_everywhere)
