"""'After downloads': the settings that are chosen once and then left (unpack, open Organise, run a command, sleep or shut down,
resume on startup), folded to a single line that says what they will do. Open it to change them.

The controls inside are the same widgets as ever (the page still owns them and saves them); this only gives them a fold.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import icons, motion
from .theme import T


class AfterFold(QWidget):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.head = QPushButton(title)
        self.head.setCheckable(True)
        self.head.setFlat(True)
        self.head.setCursor(Qt.CursorShape.PointingHandCursor)
        self.head.setToolTip("What happens when every download has finished. Click to change it.")
        T.bind_css(self.head, lambda: (f"QPushButton {{ text-align: left; border: none; padding: 4px 2px; font-weight: 600; "
                                       f"color: {T.title.name()}; }} QPushButton:hover {{ color: {T.accent.name()}; }}"))
        T.bind(self.head, self._icon)
        self.head.toggled.connect(self._toggled)
        self.summary = QLabel("")
        self.summary.setObjectName("hint")
        self.summary.setWordWrap(True)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.head, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.summary, 1, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(row)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        self.body_lay.setSpacing(12)
        self.body.setVisible(False)
        lay.addWidget(self.body)

    def _icon(self) -> None:
        self.head.setIcon(QIcon(icons.pixmap("chevron-down" if self.head.isChecked() else "chevron-right", T.fg2, 14)))

    def _toggled(self, open_: bool) -> None:
        self._icon()
        motion.set_visible(self.body, open_, axis="height")

    def set_summary(self, text: str, warn: bool = False) -> None:
        """What the folded settings will do; warn lights it amber (the computer will sleep or shut down)."""
        self.summary.setText(text)
        T.bind_css(self.summary, lambda: f"color: {(T.warn if warn else T.fg3).name()};")
