"""Open and close animation for the rows under a top-level item of a QTreeWidget (the tracks under a game in the
Soundtrack Grabber, the details under a finding on the Organize pages).

Opening: the tracks are written in from the top. Each row opens a moment after the one above it, and its text is
swept in from left to right, a little behind the height. Closing: the rows fold up together and fade as they get
thin, so nothing disappears before the space does.

Qt collapses an item at once and offers no hook before it does, so a closing item is put straight back (nothing
is painted in between) and held open while its rows shrink; only then is it really collapsed. Every way of
opening or closing an item (the arrow, double-click, the keyboard, Expand all / Collapse all) goes through the
same two signals, so they all animate. Nothing animates while motion is reduced or the list is not on screen.

Two things make the rows really change height: each cell's size hint is set, and the row's widget reports the
same height through `set_reveal` (a tree sizes a row as the taller of the two). The fade is painted by the list's
`RevealDelegate`, so a selected row's highlight fades with its text.

A long list (an album of 255 tracks) only animates the rows that can be seen: the ones below the bottom of the
list are far off screen, so they are simply left out (hidden) while the fold runs and put back when it ends. Every
frame therefore costs the same however long the list is.

Any QTreeWidget can use it: plain-text rows need no widget (`row_height=None` measures the natural row height), and
a tree that was built with uniform row heights is switched to varying heights only while a fold runs.
"""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from . import motion

_OUT = QEasingCurve(QEasingCurve.Type.OutCubic)
_IN_OUT = QEasingCurve(QEasingCurve.Type.InOutCubic)


def _clamp(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


class _Fold:
    """One running animation. `starts` is how open each row was when it began (0 = shut, 1 = open), `rows` and
    `written` how open and how far written in each row is now. The animation's value is the elapsed time in ms."""

    __slots__ = ("anim", "expand", "starts", "rows", "written", "lo", "hi", "touched", "hidden")

    def __init__(self, anim: QVariantAnimation, expand: bool, starts: list[float], lo: int, hi: int):
        self.anim, self.expand, self.starts = anim, expand, starts
        self.rows = list(starts)
        self.written = [-1.0] * len(starts)
        self.lo, self.hi = lo, hi  # the rows [lo, hi) are animated; every other row is hidden while the fold runs
        self.touched: set[int] = set()  # rows whose size or widget this fold has changed
        self.hidden: set[int] = set()  # rows this fold has hidden (the ones outside the animated range)


class TreeSlide:
    STEP = 22  # ms between one row starting to open and the next
    ROW_MS = 260  # ms one row takes to open
    CAP = 14  # rows past this open with the last one that is staggered: they are far below what is on screen
    COLLAPSE_MS = 220  # the whole fold-up
    WINDOW_MIN = 30  # a list this short animates every row; a longer one only the rows that can be seen
    MARGIN = 3  # rows past the bottom edge of the list that still animate, so nothing pops in at the edge

    def __init__(self, tree: QTreeWidget, row_height: int | None = None, text_columns: tuple[int, ...] = (2,)):
        """`row_height` is the full height of a child row, or None to use the height the rows have naturally (for
        a tree of plain text rows); `text_columns` are the columns whose plain text the write-in sweeps across to
        (the item widget in column 0 is swept too, via its `set_reveal`)."""
        self.tree = tree
        self.fixed = row_height is not None  # True: the rows carry their own size hint of exactly this height
        self.row_height = row_height or 0
        self.text_columns = text_columns
        self._folds: dict[QTreeWidgetItem, _Fold] = {}
        self._busy = False  # set while this class itself opens or closes an item, so its own signals are ignored
        self._only: set[QTreeWidgetItem] | None = None  # while many items are folded at once: the ones that animate
        self._layout_queued = False
        self._uniform = tree.uniformRowHeights()  # a tree built with equal rows must vary them while a fold runs
        self._varying = False
        tree.itemExpanded.connect(self._expanded)
        tree.itemCollapsed.connect(self._collapsed)

    # ---- the two signals ---------------------------------------------------------------------------
    def _can_animate(self, item: QTreeWidgetItem) -> bool:
        try:
            if self._only is not None and item not in self._only:
                return False
            return (motion.on() and item.parent() is None and item.childCount() > 0 and not item.isHidden()
                    and self.tree.isVisible())
        except RuntimeError:  # the row was deleted
            return False

    def _expanded(self, item: QTreeWidgetItem) -> None:
        if self._busy:
            return
        if not self._can_animate(item) or not self._run(item, expand=True):
            self._stop(item)

    def _collapsed(self, item: QTreeWidgetItem) -> None:
        if self._busy:
            return
        if not self._can_animate(item):
            self._stop(item)
            return
        self._set_expanded(item, True)  # keep the rows on screen while they fold away
        if not self._run(item, expand=False):
            self._stop(item)
            self._set_expanded(item, False)  # not animated after all: leave it shut, as Qt did

    # ---- many items at once ------------------------------------------------------------------------------
    def set_expanded(self, items, open_: bool, animate: bool = True) -> None:
        """Open or close many top-level items (Expand all / Collapse all). Only the ones that are in view animate:
        an animation for every item of a long list would only pile up below what can be seen. `animate=False`
        changes them all at once (a list that has just been filled)."""
        items = [it for it in items if it is not None]
        previous = self._only
        self._only = self._in_view(items) if animate else set()
        try:
            for it in items:
                try:
                    if it.isExpanded() != open_:
                        it.setExpanded(open_)
                except RuntimeError:
                    pass
        finally:
            self._only = previous

    def _in_view(self, items) -> set[QTreeWidgetItem]:
        shown: set[QTreeWidgetItem] = set()
        try:
            height = self.tree.viewport().height()
            for it in items:
                rect = self.tree.visualItemRect(it)
                if rect.height() > 0 and rect.bottom() >= 0 and rect.top() < height:
                    shown.add(it)
        except RuntimeError:
            pass
        return shown

    # ---- what is on screen ---------------------------------------------------------------------------------
    def _measure(self) -> None:
        """Learn the natural height of a row (a tree of plain text rows has no height of its own). Done once, the first
        time something folds: at that moment no row has been changed yet."""
        if self.fixed or self.row_height > 0:
            return
        try:
            for i in range(self.tree.topLevelItemCount()):
                top = self.tree.topLevelItem(i)
                if top.isExpanded() and top.childCount() and not top.isHidden():
                    height = self.tree.visualItemRect(top.child(0)).height()
                    if height > 0:
                        self.row_height = height
                        return
        except RuntimeError:
            pass
        if self.row_height <= 0:
            self.row_height = 26

    def visible_rows(self, item: QTreeWidgetItem) -> tuple[int, int]:
        """The child rows [lo, hi) of this top-level item that are in view, give or take a few rows. A short list is
        all of it. (Used here, and by a page that makes its rows' widgets only for what can be seen.)"""
        n = item.childCount()
        if n <= self.WINDOW_MIN:
            return 0, n
        try:
            rect = self.tree.visualItemRect(item)
            height = self.tree.viewport().height()
        except RuntimeError:
            return 0, min(n, self.WINDOW_MIN)
        if rect.height() <= 0:  # not laid out (the item is hidden): treat it as starting at the top
            return 0, min(n, self.WINDOW_MIN)
        first = rect.bottom() + 1  # where its first child row starts, in viewport pixels
        step = max(self.row_height, 1)
        lo = max(0, (-first) // step - self.MARGIN)
        hi = -(-(height - first) // step) + self.MARGIN
        lo = min(lo, n)
        return lo, min(max(hi, lo), n)

    # ---- running a fold ---------------------------------------------------------------------------------
    def _total_ms(self, expand: bool, rows: int) -> int:
        if not expand:
            return self.COLLAPSE_MS
        return (min(rows, self.CAP) - 1) * self.STEP + self.ROW_MS

    def _run(self, item: QTreeWidgetItem, expand: bool) -> bool:
        """Start folding this item. False when it should not animate (nothing is changed then)."""
        rows = item.childCount()
        self._measure()
        lo, hi = self.visible_rows(item)
        if lo > 0:  # the person has scrolled into the middle of a long list: do as Qt does, at once
            return False
        old = self._folds.get(item)
        if old is not None:  # turning round part-way: every row carries on from where it had got to
            hi = max(hi, min(old.hi, rows))
            starts = list(old.rows[:rows]) + [1.0 if not expand else 0.0] * max(0, rows - len(old.rows))
            touched, hidden = set(old.touched), set(old.hidden)
            del self._folds[item]
            old.anim.blockSignals(True)
            old.anim.stop()
            old.anim.deleteLater()
        else:
            starts = [1.0] * rows
            touched, hidden = set(), set()
            if expand:
                for i in range(lo, hi):  # the rows in view are written in from shut
                    starts[i] = 0.0
        total = self._total_ms(expand, hi - lo)
        anim = QVariantAnimation(self.tree)
        anim.setStartValue(0.0)
        anim.setEndValue(float(total))
        anim.setDuration(total)
        anim.setEasingCurve(QEasingCurve.Type.Linear)  # time itself: each row shapes its own curve
        fold = self._folds[item] = _Fold(anim, expand, starts, lo, hi)
        fold.touched, fold.hidden = touched, hidden
        self._begin_varying()
        self._park_rows(item, fold)
        anim.valueChanged.connect(lambda value, it=item: self._step(it, float(value)))
        anim.finished.connect(lambda it=item, f=fold: self._finished(it, f))
        if not self._apply(item, fold, 0.0):  # the very first frame, before anything is painted
            self._stop(item)
            return True  # already dealt with
        self._relayout_soon()
        anim.start()
        return True

    def _park_rows(self, item: QTreeWidgetItem, fold: _Fold) -> None:
        """The rows outside the animated range are hidden while the fold runs (they are far off screen, and a list
        that lays out only what is shown stays quick whatever its length); the rows inside it are shown."""
        try:
            for i in range(min(len(fold.rows), item.childCount())):
                inside = fold.lo <= i < fold.hi
                if inside and i in fold.hidden:
                    item.child(i).setHidden(False)
                    fold.hidden.discard(i)
                elif not inside and i not in fold.hidden:
                    item.child(i).setHidden(True)
                    fold.hidden.add(i)
                    fold.rows[i] = 0.0
        except RuntimeError:
            pass

    def _step(self, item: QTreeWidgetItem, elapsed: float) -> None:
        fold = self._folds.get(item)
        if fold is None:
            return
        if not self._apply(item, fold, elapsed):
            self._stop(item)
            return
        self._relayout_soon(now=True)

    def _finished(self, item: QTreeWidgetItem, fold: _Fold) -> None:
        if self._folds.get(item) is fold:
            self._stop(item)

    def _stop(self, item: QTreeWidgetItem) -> None:
        """End whatever is running for this item at once: rows back to normal, and the item left open or shut as
        the animation was heading."""
        fold = self._folds.pop(item, None)
        if fold is not None:
            fold.anim.blockSignals(True)
            fold.anim.stop()
            fold.anim.deleteLater()
            if not fold.expand:  # shut first: the rows are then put back out of the layout, which is quicker
                self._set_expanded(item, False)
            self._restore(item, fold)
        self._end_varying()
        self._relayout_soon()

    def cancel(self) -> None:
        """Drop every running fold without touching the rows (the list is about to be cleared)."""
        for fold in self._folds.values():
            fold.anim.blockSignals(True)
            fold.anim.stop()
            fold.anim.deleteLater()
        self._folds.clear()
        clear = getattr(self.tree.itemDelegate(), "clear_rows", None)
        if clear is not None:  # the rows the delegate was painting part-way belong to rows that are going
            clear()
        self._end_varying()

    def _begin_varying(self) -> None:
        if self._uniform and not self._varying:
            self._varying = True
            self.tree.setUniformRowHeights(False)

    def _end_varying(self) -> None:
        if self._varying and not self._folds:
            self._varying = False
            try:
                self.tree.setUniformRowHeights(self._uniform)
            except RuntimeError:
                pass

    # ---- the rows ----------------------------------------------------------------------------------------
    def _shape(self, fold: _Fold, i: int, elapsed: float) -> tuple[float, float, bool]:
        """(how open, how written in, still sweeping) for row `i` at this moment."""
        if fold.expand:
            q = _clamp((elapsed - min(i, self.CAP - 1) * self.STEP) / self.ROW_MS)
            start = fold.starts[i]
            opened = start + (1.0 - start) * _OUT.valueForProgress(q)
            x = _clamp((q - 0.1) / 0.9)  # the text trails the height a little
            written = x * x * (3.0 - 2.0 * x)
            return opened, written, written < 1.0
        s = _clamp(elapsed / self.COLLAPSE_MS)
        opened = fold.starts[i] * (1.0 - _IN_OUT.valueForProgress(s))
        return opened, _clamp(opened / 0.65), False  # fades as the row gets thin, never ahead of the space

    def _apply(self, item: QTreeWidgetItem, fold: _Fold, elapsed: float) -> bool:
        """Show the rows as they should be `elapsed` ms in. False when the item no longer belongs to the list."""
        tree = self.tree
        try:
            if item.treeWidget() is not tree:
                return False
            game = tree.indexOfTopLevelItem(item)
            delegate = tree.itemDelegate()
            paint_row = getattr(delegate, "set_row", None)
            soft = getattr(delegate, "SOFT", 48)
            reach = max((tree.columnViewportPosition(c) + tree.columnWidth(c) for c in self.text_columns), default=0)
            columns = tree.columnCount()
            tree.blockSignals(True)  # setSizeHint would fire itemChanged for every cell
            try:
                for i in range(fold.lo, min(fold.hi, item.childCount(), len(fold.rows))):
                    opened, written, sweeping = self._shape(fold, i, elapsed)
                    if opened == fold.rows[i] and written == fold.written[i]:
                        continue
                    fold.rows[i], fold.written[i] = opened, written
                    fold.touched.add(i)
                    child = item.child(i)
                    height = max(0, round(self.row_height * opened))
                    size = QSize(0, height)
                    for col in range(columns):  # a row is as tall as its tallest cell, so every cell is set
                        child.setSizeHint(col, size)
                    front = written * (reach + soft) if sweeping else None  # viewport x the sweep has reached
                    alpha = 1.0 if sweeping else written
                    widget = tree.itemWidget(child, 0)
                    if widget is not None and hasattr(widget, "set_reveal"):
                        widget.set_reveal(alpha, height, None if front is None else front - widget.x())
                    if paint_row is not None:
                        paint_row(game, i, front, alpha)
            finally:
                tree.blockSignals(False)
            tree.viewport().update()
            return True
        except RuntimeError:
            return False

    def _restore(self, item: QTreeWidgetItem, fold: _Fold) -> None:
        """Rows back to how the list builds them: shown, full height, and the widget and text fully shown. Only the
        rows the fold changed are visited."""
        try:
            tree = self.tree
            columns = tree.columnCount()
            count = item.childCount()
            tree.blockSignals(True)
            try:
                for i in fold.hidden:
                    if i < count:
                        item.child(i).setHidden(False)
                for i in fold.touched:
                    if i >= count:
                        continue
                    child = item.child(i)
                    if self.fixed:
                        child.setSizeHint(0, QSize(0, self.row_height))
                        for col in range(1, columns):
                            child.setData(col, Qt.ItemDataRole.SizeHintRole, None)
                    else:  # plain rows: back to the height the style gives them
                        for col in range(columns):
                            child.setData(col, Qt.ItemDataRole.SizeHintRole, None)
                    widget = tree.itemWidget(child, 0)
                    if widget is not None and hasattr(widget, "set_reveal"):
                        widget.set_reveal(None)
            finally:
                tree.blockSignals(False)
            clear = getattr(tree.itemDelegate(), "clear_rows", None)
            if clear is not None and (game := tree.indexOfTopLevelItem(item)) >= 0:
                clear(game)
        except RuntimeError:
            pass

    def _set_expanded(self, item: QTreeWidgetItem, open_: bool) -> None:
        self._busy = True
        try:
            item.setExpanded(open_)
        except RuntimeError:
            pass
        finally:
            self._busy = False

    # ---- layout --------------------------------------------------------------------------------------------
    def _relayout_soon(self, now: bool = False) -> None:
        """Row heights changed: lay the list out again (this also moves the rows' widgets). While one item folds,
        each animation frame does it on the spot, so no frame is painted with the old row positions; otherwise (the
        first frame and the end, which run inside Qt's own expand / collapse signals, or several items folding at
        once, as with Expand all) it is done once per event-loop pass instead."""
        if now and len(self._folds) <= 1:
            self._relayout()
            return
        if self._layout_queued:
            return
        self._layout_queued = True
        QTimer.singleShot(0, self._relayout)

    def _relayout(self) -> None:
        self._layout_queued = False
        try:
            self.tree.doItemsLayout()
        except RuntimeError:
            pass
