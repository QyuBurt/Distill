"""The Queue view of a conversion run: every file as a row with its state, its progress and what it saved.

It is fed by the same two events that write the log ("new" and "update" task lines), through queuemodel.RunQueue, so it
shows exactly what the log says, one row a file instead of one line a change. Nothing the engines do is different.
"""
from __future__ import annotations

from bisect import bisect_left

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QLabel, QListView, QPushButton, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

from ..models import fmt_size
from ..queuemodel import ACTIVE, DONE, PROBLEM, SKIPPED, RunQueue, Task, matches
from .theme import T
from .widgets import paint_placeholder

TASK_ROLE = Qt.ItemDataRole.UserRole + 1
STALE_ROLE = Qt.ItemDataRole.UserRole + 2
ROW_H = 48
CHIP_W = 98
EMPTY = "Nothing is running.\nStart a batch and each of its files shows up here, with its progress and what it saved."
FILTERS = (("all", "All", None), ("active", "Running", "accent"), ("problems", "Problems", "bad"))


def phase_colour(phase: str) -> QColor:
    """The colour a row's chip and bar are drawn in."""
    return {ACTIVE: T.accent, DONE: T.ok, PROBLEM: T.bad, SKIPPED: T.fg2}.get(phase, T.fg3)


def _tint(colour: QColor, alpha: int) -> QColor:
    return QColor(colour.red(), colour.green(), colour.blue(), alpha)


class QueueModel(QAbstractListModel):
    """The queue's rows, all of them or only those under a filter. Reads RunQueue; never changes it."""

    def __init__(self, queue: RunQueue, parent=None) -> None:
        super().__init__(parent)
        self.queue = queue
        self.view = "all"
        self._count = 0  # rows told to the views so far (the queue may already hold one more, for a moment)
        self._rows: list[int] = []  # when filtered: the queue rows that are listed, in order

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return self._count if self.view == "all" else len(self._rows)

    def _qrow(self, row: int) -> int:
        return row if self.view == "all" else self._rows[row]

    def task_at(self, row: int) -> Task:
        return self.queue.task_at(self._qrow(row))

    def stale(self, row: int) -> bool:
        return self.queue.stale(self._qrow(row))

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < self.rowCount():
            return None
        task = self.task_at(index.row())
        if role == TASK_ROLE:
            return task
        if role == STALE_ROLE:
            return self.stale(index.row())
        if role == Qt.ItemDataRole.DisplayRole:
            return task.name
        if role == Qt.ItemDataRole.ToolTipRole:  # the whole name and reason, for a row that had to be cut short
            return "\n".join(part for part in (task.name, task.step, task.note, task.result) if part)
        return None

    # ---- the queue changed: tell the views, touching as little as possible ----------------------------------
    def reset(self) -> None:
        self.beginResetModel()
        self._refilter()
        self.endResetModel()

    def set_view(self, view: str) -> None:
        self.beginResetModel()
        self.view = view
        self._refilter()
        self.endResetModel()

    def _refilter(self) -> None:
        self._count = len(self.queue)
        self._rows = ([] if self.view == "all"
                      else [i for i in range(len(self.queue)) if matches(self.queue.task_at(i), self.view)])

    def appended(self, qrow: int) -> None:
        """A row was added to the end of the queue."""
        if self.view == "all":
            self.beginInsertRows(QModelIndex(), self._count, qrow)
            self._count = qrow + 1
            self.endInsertRows()
        elif matches(self.queue.task_at(qrow), self.view):
            self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
            self._rows.append(qrow)
            self.endInsertRows()

    def changed(self, qrow: int) -> None:
        """A row's task was rewritten. Only a filter can make it come or go; otherwise just its own row repaints."""
        if self.view == "all":
            ix = self.index(qrow, 0)
            self.dataChanged.emit(ix, ix)
            return
        at = bisect_left(self._rows, qrow)
        listed = at < len(self._rows) and self._rows[at] == qrow
        if listed != matches(self.queue.task_at(qrow), self.view):
            self.reset()
        elif listed:
            ix = self.index(at, 0)
            self.dataChanged.emit(ix, ix)


class QueueDelegate(QStyledItemDelegate):
    """One file: a chip with its state, its name over what is happening or what came of it, and a thin bar while it runs."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), ROW_H)

    def paint(self, p: QPainter, option, index) -> None:
        task = index.data(TASK_ROLE)
        if not isinstance(task, Task):
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if index.data(STALE_ROLE):  # a row of an earlier run (kept when failed ones are retried)
            p.setOpacity(0.45)
        r = QRectF(option.rect).adjusted(2, 2, -2, -2)
        if bool(option.state & QStyle.StateFlag.State_MouseOver):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.hover)
            p.drawRoundedRect(r, 9, 9)
        colour = phase_colour(task.phase)
        indent = 22 if task.child else 0

        chip = QRectF(r.x() + 8 + indent, r.center().y() - 11, CHIP_W, 22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_tint(colour, 40))
        p.drawRoundedRect(chip, 11, 11)
        small = QFont(option.font)
        small.setPointSizeF(8.0)
        small.setBold(True)
        p.setFont(small)
        p.setPen(colour)
        p.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), task.state or "WAITING")

        left = chip.right() + 12
        right = r.right() - 10
        percent = f"{task.pct:.0f}%" if task.pct is not None else ""
        if percent:
            p.setPen(T.fg2)
            p.drawText(QRectF(right - 44, r.y() + 4, 44, 20), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), percent)
            right -= 52
        width = max(40.0, right - left)

        bold = QFont(option.font)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(T.title)
        name = p.fontMetrics().elidedText(task.name, Qt.TextElideMode.ElideMiddle, int(width))
        p.drawText(QRectF(left, r.y() + 4, width, 20), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), name)

        info = QFont(option.font)
        info.setPointSizeF(9.0)
        p.setFont(info)
        text, text_colour = self._detail(task)
        p.setPen(text_colour)
        text = p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(width))
        p.drawText(QRectF(left, r.y() + 23, width, 16), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)

        if task.phase == ACTIVE and task.pct is not None:  # the tool's own progress, under the text
            track = QRectF(left, r.bottom() - 4, width, 3)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.border)
            p.drawRoundedRect(track, 1.5, 1.5)
            p.setBrush(colour)
            p.drawRoundedRect(QRectF(track.x(), track.y(), max(3.0, track.width() * task.pct / 100), track.height()), 1.5, 1.5)
        p.restore()

    @staticmethod
    def _detail(task: Task) -> tuple[str, QColor]:
        """The line under the name, and the colour it is written in."""
        if task.phase == DONE and task.result:
            sizes = f"  \u00b7  {fmt_size(task.before)} \u2192 {fmt_size(task.after)}" if task.before and task.after else ""
            return f"{task.result}{sizes}", (T.warn if "larger" in task.result else T.ok)
        if task.phase == DONE:
            return task.note, T.fg3
        if task.phase == ACTIVE:
            return task.step or ("Working" if task.pct is None else ""), T.fg3
        if task.phase == PROBLEM:
            return task.note, T.bad
        return task.note, T.fg3


class _List(QListView):
    """The list, with an empty state, and a signal for the person taking the scroll into their own hands."""

    userScrolled = Signal()
    placeholder = ""
    placeholder_icon = ""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.placeholder and self.model() is not None and self.model().rowCount() == 0:
            paint_placeholder(self, self.placeholder, self.placeholder_icon)

    def wheelEvent(self, event) -> None:
        self.userScrolled.emit()
        super().wheelEvent(event)


class QueueView(QWidget):
    """A headline ("3 of 12 done \u00b7 2 running"), All / Running / Problems filters, and the files."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.queue = RunQueue()
        self.running = False
        self._follow = True  # keep the file being worked on in view, until the person scrolls
        self._refresh_due = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.headline = QLabel("")
        self.headline.setObjectName("muted")
        head.addWidget(self.headline, 1)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.filters: dict[str, QPushButton] = {}
        for key, text, tone in FILTERS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(28)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            T.bind_css(button, lambda tone=tone: self._pill_css(tone))
            button.toggled.connect(lambda on, k=key: self._set_filter(k) if on else None)
            self._group.addButton(button)
            self.filters[key] = button
            head.addWidget(button)
        lay.addLayout(head)

        self.model = QueueModel(self.queue, self)
        self.list = _List()
        self.list.setModel(self.model)
        self.list.setItemDelegate(QueueDelegate(self.list))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setMinimumHeight(120)
        self.list.placeholder = EMPTY
        self.list.placeholder_icon = "list"
        self.list.userScrolled.connect(self._let_go)
        self.filters["all"].setChecked(True)  # after the model and list exist: toggling calls _set_filter
        self.list.verticalScrollBar().sliderPressed.connect(self._let_go)
        T.bind_css(self.list, lambda: (f"QListView {{ background: {T.input.name()}; border: 1px solid {T.border.name()}; "
                                       "border-radius: 10px; padding: 4px; outline: 0; }"))
        lay.addWidget(self.list, 1)

    @staticmethod
    def _pill_css(tone: str | None) -> str:
        colour = getattr(T, tone) if tone else T.fg2
        return (f"QPushButton {{ background: {T.input.name()}; color: {T.fg3.name()}; border: 1px solid {T.card_border.name()}; "
                "border-radius: 14px; padding: 0 14px; font-weight: 600; }"
                f"QPushButton:hover {{ background: {T.hover.name()}; color: {T.fg2.name()}; }}"
                f"QPushButton:checked {{ background: rgba({colour.red()}, {colour.green()}, {colour.blue()}, 44); color: {colour.name()}; "
                f"border: 1px solid rgba({colour.red()}, {colour.green()}, {colour.blue()}, 190); }}")

    # ---- fed by the run's task events ------------------------------------------------------------------------
    def begin_run(self, keep: bool = False) -> None:
        """A run is starting; keep=True is a retry, which leaves the earlier rows in place, dimmed."""
        self.queue.begin_run(keep)
        self._follow = True
        self.model.reset()
        self._refresh()

    def clear(self) -> None:
        self.queue.clear()
        self.model.reset()
        self._refresh()

    def task_new(self, tid: int, text: str) -> None:
        before = len(self.queue)
        row = self.queue.new(tid, text)
        (self.model.appended if len(self.queue) > before else self.model.changed)(row)
        self._refresh_soon()

    def task_update(self, tid: int, text: str) -> None:
        if (row := self.queue.update(tid, text)) is not None:
            self.model.changed(row)
            self._refresh_soon()

    def set_running(self, on: bool) -> None:
        """The run started or ended. Ending settles any row that was cut short."""
        self.running = on
        if not on:
            for row in self.queue.close_run():
                self.model.changed(row)
        self._refresh()

    # ---- the headline, and keeping the work in view ----------------------------------------------------------
    def _refresh_soon(self) -> None:
        if not self._refresh_due:  # a batch of events is one redraw of the headline, not one each
            self._refresh_due = True
            QTimer.singleShot(150, self._refresh)

    def _refresh(self) -> None:
        self._refresh_due = False
        self.headline.setText(self.queue.tally().headline(self.running))
        if self._follow and self.running:
            self._scroll_to_work()

    def _scroll_to_work(self) -> None:
        for row in range(self.model.rowCount()):
            if self.model.task_at(row).phase == ACTIVE and not self.model.stale(row):
                self.list.scrollTo(self.model.index(row, 0), QAbstractItemView.ScrollHint.EnsureVisible)
                return

    def _let_go(self) -> None:
        self._follow = False

    def _set_filter(self, view: str) -> None:
        self._follow = True
        self.model.set_view(view)
        self.list.placeholder = ("Nothing is running right now." if view == "active"
                                 else "No problems.\nEvery file so far went through." if view == "problems" else EMPTY)
