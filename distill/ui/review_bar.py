"""The chips at the top of an Organise page's results: what is selected, what applying it frees, what can't be applied.

They say what the line under the list used to say, where you are deciding (above the rows) rather than under them.
What they say comes from orgreview.py, so it can be tested without a window.
"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ..orgreview import ACCENT, BAD, OK, WARN, Chip
from .emu_cards import pill_css
from .theme import T

MOST_CHIPS = 4


class ReviewBar(QWidget):
    """A small row of rounded tags. It never has more than MOST_CHIPS; the ones not needed are hidden."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._labels = [QLabel() for _ in range(MOST_CHIPS)]
        for label in self._labels:
            label.setVisible(False)
            lay.addWidget(label)

    def set_chips(self, chips: list[Chip]) -> None:
        for i, label in enumerate(self._labels):
            if i < len(chips):
                label.setText(chips[i].text)
                T.bind_css(label, lambda tone=chips[i].tone: pill_css(self._colour(tone)))
            label.setVisible(i < len(chips))

    @staticmethod
    def _colour(tone: str):
        return {ACCENT: T.accent, OK: T.ok, WARN: T.warn, BAD: T.bad}.get(tone, T.fg3)
