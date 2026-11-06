"""Gives every tooltip in the app a sensible width."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractItemView, QToolTip, QWidget

from ..tooltips import looks_like_html, tooltip_html


class TooltipWrapper(QObject):
    """An application-wide event filter: a hover tooltip longer than a line is shown wrapped. Item views (the lists)
    are handled too, using the tooltip of the item under the pointer."""

    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.ToolTip or not isinstance(obj, QWidget):
            return False
        view = obj.parentWidget()
        if isinstance(view, QAbstractItemView) and obj is view.viewport():
            index = view.indexAt(event.pos())
            text = index.data(Qt.ItemDataRole.ToolTipRole) if index.isValid() else ""
        else:
            text = obj.toolTip()
        if not text or not isinstance(text, str) or looks_like_html(text):
            return False  # nothing to show, or rich text Qt already wraps
        QToolTip.showText(event.globalPos(), tooltip_html(text), obj)
        return True
