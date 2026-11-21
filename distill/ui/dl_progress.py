"""The Files & Torrents list's painter, with a thin progress line under each row's Status cell: the accent while a file downloads
(as far as its percentage says), a faint green once it is done, a faint red if it failed.

It is the old painter (DlDelegate: the ripple-in of a fresh list, the chevron and format of a video row) with the line added after
it; nothing about how a row is laid out or painted changes.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter

from ..dlprogress import row_look
from .theme import T
from .ytdlp_picker import DlDelegate

STATUS_COLUMN = 4  # ui.models.DlModel.HEAD: "", File, Size, Source, Status, Speed
LINE = 2.5  # px tall


def _tint(colour: QColor, alpha: int) -> QColor:
    return QColor(colour.red(), colour.green(), colour.blue(), alpha)


class ProgressDelegate(DlDelegate):
    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        if index.column() != STATUS_COLUMN:
            return
        kind, frac = row_look(str(index.data() or ""))
        if frac is None:
            return
        alpha = self._alpha.get(index.row(), 1.0) if self._alpha else 1.0  # a fresh list ripples in; the line comes in with its row
        if alpha <= 0.0:
            return
        got = self._info(index)
        room = got[0] if got else 0  # the room under a video row's own line, when its format panel is open
        r = option.rect
        line = QRectF(r.left() + 6, r.bottom() - room - LINE - 2, max(1, r.width() - 12), LINE)
        colour = {"active": T.accent, "done": T.ok, "failed": T.bad}[kind]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(painter.opacity() * alpha)
        painter.setPen(QColor(0, 0, 0, 0))
        painter.setBrush(_tint(colour, 40))
        painter.drawRoundedRect(line, LINE / 2, LINE / 2)
        painter.setBrush(colour if kind == "active" else _tint(colour, 120))
        painter.drawRoundedRect(QRectF(line.x(), line.y(), max(LINE, line.width() * frac), LINE), LINE / 2, LINE / 2)
        painter.restore()
