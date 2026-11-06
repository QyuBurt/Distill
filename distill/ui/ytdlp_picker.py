"""The format picker on the Download page: a video or audio site row (yt-dlp) opens to show an MP4 / MP3 choice and a
quality choice below it, as JDownloader's rows do.

A QTableView row can't hold child rows, so an opened row is made taller and its panel is laid over the extra space (a
child of the table's viewport). `FormatRows` keeps the two together: it re-measures after a sort, filter, removal or
scroll. `DlDelegate` draws the row's chevron and its choice, and the panel's half of the highlight."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEasingCurve, QEvent, QModelIndex, QObject, QRect, QTimer, QVariantAnimation, Qt, Signal
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTableView, QWidget,
)

from .. import ytdlp
from . import icons, motion
from .theme import T
from .widgets import RevealDelegate

CHEVRON_W = 24  # px at the left of the name cell that open and close the row
PANEL_H = 46  # px a row grows by while it is open


class FormatPanel(QWidget):
    """'Format [MP4 video | MP3 audio]  Quality [...]' for one row."""

    chosen = Signal(str)  # the stored choice ('mp4:1080', 'mp3:320') after the person changed a drop-down
    applyAll = Signal(str)  # the person asked for this choice on every video in the list

    def __init__(self, spec: str, heights: list[int] | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)  # a click here is not a click on the row behind
        self._heights = heights
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 6)
        lay.setSpacing(8)
        self.kind = QComboBox()
        for key, label in ytdlp.KINDS.items():
            self.kind.addItem(label, key)
        self.quality = QComboBox()
        self.apply_all = QPushButton("Apply to all")
        self.apply_all.setFlat(True)
        self.apply_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_all.setToolTip("Use this format and quality for every video and audio link in the list")
        self.note = QLabel()
        self.note.setObjectName("hint")
        for w in (QLabel("Format"), self.kind, QLabel("Quality"), self.quality, self.apply_all):
            lay.addWidget(w)
        lay.addWidget(self.note, 1)
        self.set_spec(spec)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.quality.currentIndexChanged.connect(lambda *_: self._emit())
        self.apply_all.clicked.connect(lambda: self.applyAll.emit(self.spec()))

    # ---- what is chosen ---------------------------------------------------------------------------------------
    def spec(self) -> str:
        return ytdlp.make_choice(str(self.kind.currentData()), int(self.quality.currentData() or 0))

    def set_spec(self, spec: str) -> None:
        """Show this choice (without announcing it as a change)."""
        kind, quality = ytdlp.parse_choice(spec) or ("mp4", 0)
        self.kind.blockSignals(True)
        self.kind.setCurrentIndex(max(0, self.kind.findData(kind)))
        self.kind.blockSignals(False)
        self._fill(quality)

    def set_heights(self, heights: list[int] | None) -> None:
        """The heights this video comes in (None: unknown, offer the usual ones). Keeps the choice where it can."""
        self._heights = heights
        self._fill(int(self.quality.currentData() or 0))

    def set_busy(self, text: str) -> None:
        self._busy = text
        self._note()

    # ---- internals --------------------------------------------------------------------------------------------
    def _fill(self, wanted: int) -> None:
        mp3 = self.kind.currentData() == "mp3"
        if mp3:
            options = [(f"{r} kbps", r) for r in ytdlp.MP3_RATES]
            pick = min((r for r in ytdlp.MP3_RATES), key=lambda r: abs(r - wanted)) if wanted else ytdlp.MP3_RATES[0]
        else:
            heights = ytdlp.mp4_options(self._heights)
            options = [("Best available", 0)] + [(f"{h}p", h) for h in heights]
            pick = 0 if not wanted else next((h for h in heights if h <= wanted), heights[-1])
        self.quality.blockSignals(True)
        self.quality.clear()
        for label, value in options:
            self.quality.addItem(label, value)
        self.quality.setCurrentIndex(max(0, self.quality.findData(pick)))
        self.quality.blockSignals(False)
        self._note()

    def _note(self) -> None:
        busy = getattr(self, "_busy", "")
        if busy:
            text = busy
        elif not ytdlp.ffmpeg_found():
            text = ("MP3 needs ffmpeg, which wasn't found." if self.kind.currentData() == "mp3"
                    else "Without ffmpeg yt-dlp saves the best single file.")
        else:
            text = ""
        self.note.setText(text)

    def _kind_changed(self) -> None:
        self._fill(0 if self.kind.currentData() == "mp4" else 320)  # a new kind starts at its own sensible default
        self._emit()

    def _emit(self) -> None:
        self.chosen.emit(self.spec())


@dataclass(eq=False, slots=True)
class _Open:
    rf: object
    panel: FormatPanel
    extra: float = 0.0  # px of the row's growth shown right now (animated)
    target: float = 0.0
    anim: QVariantAnimation | None = None


class FormatRows(QObject):
    """Opens and closes the format panels of a download table, and keeps their rows the right height."""

    _probed = Signal(str, object)  # (row url, heights or None) from the thread that asked yt-dlp

    def __init__(self, table: QTableView, proxy, model, locked: Callable[[int], bool], changed: Callable[[], None]):
        super().__init__(table)
        self.table, self.proxy, self.model = table, proxy, model
        self._locked, self._changed = locked, changed
        self._open: dict[int, _Open] = {}  # id(RemoteFile) -> its panel
        self._tall: set[int] = set()  # view rows whose height this class has changed
        self._heights: dict[str, list[int] | None] = {}  # url -> heights found by yt-dlp (None: it could not tell)
        self._pending: set[str] = set()
        self._soon = QTimer(self, singleShot=True, interval=0)
        self._soon.timeout.connect(self.relayout)
        for sig in (proxy.layoutChanged, proxy.rowsInserted, proxy.rowsRemoved, proxy.rowsMoved, proxy.modelReset):
            sig.connect(lambda *_: self._soon.start())
        for sig in (table.verticalScrollBar().valueChanged, table.horizontalScrollBar().valueChanged,
                    table.horizontalHeader().sectionResized):
            sig.connect(lambda *_: self.place())
        model.dataChanged.connect(lambda *_: self.refresh_locks())
        table.viewport().installEventFilter(self)
        self._probed.connect(self._got_heights)

    # ---- what the table and delegate ask --------------------------------------------------------------------
    @staticmethod
    def expandable(rf) -> bool:
        return str(rf.url).startswith(ytdlp.PREFIX)

    def _base(self) -> int:
        return self.table.verticalHeader().defaultSectionSize()

    def info(self, index: QModelIndex) -> tuple[int, str] | None:
        """(px the row has grown by, its choice's label) for a row that can open; None for every other row."""
        src = self.proxy.mapToSource(index).row()
        if not 0 <= src < len(self.model.files):
            return None
        rf = self.model.files[src]
        if not self.expandable(rf):
            return None
        entry = self._open.get(id(rf))
        return (round(entry.extra) if entry else 0), ytdlp.choice_label(rf.fmt)

    def click_part(self, index: QModelIndex) -> str:
        """Where the person's click landed on a row that can open: 'chevron', 'panel' (the room below the row's own
        line) or '' (an ordinary click on it)."""
        if not index.isValid() or self.info(index) is None:
            return ""
        pos = self.table.viewport().mapFromGlobal(QCursor.pos())
        rect = self.table.visualRect(self.proxy.index(index.row(), 1))
        if pos.y() - rect.top() >= self._base():
            return "panel"
        return "chevron" if index.column() == 1 and 0 <= pos.x() - rect.left() < CHEVRON_W else ""

    # ---- opening and closing ---------------------------------------------------------------------------------
    def toggle(self, src_rows: list[int]) -> None:
        """Open these rows (those that can), or close them all when they are all open already."""
        files = [self.model.files[i] for i in src_rows if 0 <= i < len(self.model.files)]
        files = [rf for rf in files if self.expandable(rf)]
        if not files:
            return
        want_open = any(not (e := self._open.get(id(rf))) or e.target == 0 for rf in files)
        for rf in files:
            self._set_open(rf, want_open)

    def _set_open(self, rf, open_: bool) -> None:
        entry = self._open.get(id(rf))
        if entry is None:
            if not open_:
                return
            panel = FormatPanel(rf.fmt or ytdlp.preset_choice(ytdlp.SETTINGS.preset), self._heights.get(rf.url),
                                self.table.viewport())
            panel.chosen.connect(lambda spec, f=rf: self._choose(f, spec))
            panel.applyAll.connect(self._apply_all)
            panel.hide()
            entry = self._open[id(rf)] = _Open(rf, panel)
            self._probe(rf.url, panel)
        entry.target = float(PANEL_H if open_ else 0)
        if not open_:
            entry.panel.hide()  # the panel leaves before the row folds up
        if entry.anim is not None:  # turning round part-way: carry on from where the row has got to
            entry.anim.stop()
            entry.anim.deleteLater()
            entry.anim = None
        if not motion.on() or not self.table.isVisible() or not self.table.window().isVisible():
            entry.extra = entry.target
            self._settle(entry)
            self.relayout()
            return
        anim = entry.anim = QVariantAnimation(self, duration=170, startValue=entry.extra, endValue=entry.target)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda v, e=entry: self._frame(e, float(v)))
        anim.finished.connect(lambda e=entry: self._settle(e))
        anim.start()

    def _frame(self, entry: _Open, value: float) -> None:
        entry.extra = value
        self.relayout()

    def _settle(self, entry: _Open) -> None:
        entry.extra = entry.target
        if entry.target == 0:
            self._open.pop(id(entry.rf), None)
            entry.panel.deleteLater()
        self.relayout()
        self.table.viewport().update()

    # ---- choosing ----------------------------------------------------------------------------------------------
    def _choose(self, rf, spec: str) -> None:
        rf.fmt = spec
        self._touch_names()
        self._changed()

    def _apply_all(self, spec: str) -> None:
        for i, rf in enumerate(self.model.files):
            if self.expandable(rf) and i not in self.model.removed and not self._locked(i):
                rf.fmt = spec
                if (entry := self._open.get(id(rf))) is not None:
                    entry.panel.set_spec(spec)
        self._touch_names()
        self._changed()

    def _touch_names(self) -> None:
        if self.model.files:
            self.model.dataChanged.emit(self.model.index(0, 1), self.model.index(len(self.model.files) - 1, 1))

    # ---- which qualities exist ------------------------------------------------------------------------------
    def _probe(self, url: str, panel: FormatPanel) -> None:
        if url in self._heights or url in self._pending:
            return
        exe = ytdlp.find_exe()
        if exe is None:
            return
        self._pending.add(url)
        panel.set_busy("Checking the qualities this video has...")

        def work() -> None:
            try:
                heights = ytdlp.probe_heights(url, exe) or None
            except Exception:  # noqa: BLE001  (any failure just means the usual list is offered)
                heights = None
            try:
                self._probed.emit(url, heights)
            except RuntimeError:  # the window closed meanwhile
                pass
        threading.Thread(target=work, daemon=True).start()

    def _got_heights(self, url: str, heights) -> None:
        self._pending.discard(url)
        self._heights[url] = heights
        for entry in self._open.values():
            if entry.rf.url == url:
                entry.panel.set_busy("")
                entry.panel.set_heights(heights)

    # ---- laying it out ---------------------------------------------------------------------------------------
    def refresh_locks(self) -> None:
        for entry in self._open.values():
            src = self.model.row_of(entry.rf)
            entry.panel.setEnabled(src is None or not self._locked(src))

    def relayout(self) -> None:
        """Give each open row its height, drop panels whose rows have gone, and put the panels where their rows are."""
        table, proxy, base = self.table, self.proxy, self._base()
        rows = proxy.rowCount()
        for r in self._tall:
            if r < rows:
                table.setRowHeight(r, base)
        self._tall.clear()
        for key, entry in list(self._open.items()):
            src = self.model.row_of(entry.rf)
            if src is None:  # the row was removed from the list
                self._open.pop(key)
                entry.panel.deleteLater()
                continue
            ix = proxy.mapFromSource(self.model.index(src, 0))
            if ix.isValid() and entry.extra > 0:
                table.setRowHeight(ix.row(), base + round(entry.extra))
                self._tall.add(ix.row())
        self.refresh_locks()
        self.place()

    def place(self) -> None:
        """Move each panel under its row (the row may have scrolled, or its columns been resized)."""
        table, proxy, base = self.table, self.proxy, self._base()
        left = table.columnViewportPosition(1) + CHEVRON_W
        width = max(0, table.viewport().width() - left - 8)
        for entry in self._open.values():
            src = self.model.row_of(entry.rf)
            ix = proxy.mapFromSource(self.model.index(src, 0)) if src is not None else QModelIndex()
            ready = ix.isValid() and entry.target > 0 and entry.extra >= PANEL_H - 0.5
            if not ready:
                entry.panel.hide()
                continue
            top = table.rowViewportPosition(ix.row()) + base
            entry.panel.setGeometry(QRect(left, top, width, PANEL_H))
            entry.panel.show()
            entry.panel.raise_()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.table.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.place)
        return False


class DlDelegate(RevealDelegate):
    """The download list's painter: the ripple-in of RevealDelegate, plus the chevron and chosen format of a row that
    can open, and the highlight under an open row's panel."""

    def __init__(self, view: QTableView, info: Callable[[QModelIndex], tuple[int, str] | None]):
        super().__init__(view)
        self._info = info

    def paint(self, painter, option, index) -> None:
        got = self._info(index)
        if got is None:
            super().paint(painter, option, index)
            return
        extra, label = got
        alpha = self._alpha.get(index.row(), 1.0) if self._alpha else 1.0  # the ripple-in of a fresh list
        if alpha <= 0.0:
            return
        painter.save()
        painter.setOpacity(painter.opacity() * alpha)
        if extra > 0:  # the room under the row's own line: just the highlight, clipped to that room
            full = QStyleOptionViewItem(option)
            self.initStyleOption(full, index)
            style = option.widget.style() if option.widget is not None else QApplication.style()
            painter.save()
            painter.setClipRect(QRect(option.rect.left(), option.rect.bottom() + 1 - extra, option.rect.width(), extra),
                                Qt.ClipOperation.IntersectClip)
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, full, painter, option.widget)
            painter.restore()
        line = QStyleOptionViewItem(option)  # the row's own line: the cell as it is without the room under it
        line.rect = QRect(option.rect.left(), option.rect.top(), option.rect.width(), option.rect.height() - extra)
        if index.column() == 1:
            icon = icons.pixmap("chevron-down" if extra > 0 else "chevron-right", T.fg3, 14)
            painter.drawPixmap(line.rect.left() + 6, line.rect.top() + (line.rect.height() - 14) // 2, icon)
            line.rect.adjust(CHEVRON_W, 0, 0, 0)
            if label:
                shown = line.fontMetrics.horizontalAdvance(label) + 14
                painter.setPen(QColor(T.fg3))
                painter.drawText(QRect(line.rect.right() - shown + 1, line.rect.top(), shown - 8, line.rect.height()),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), label)
                line.rect.adjust(0, 0, -shown, 0)
        QStyledItemDelegate.paint(self, painter, line, index)  # (the fade is already applied above)
        painter.restore()
