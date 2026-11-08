"""The last chance to cancel a sleep, hibernate or shutdown that the Download page has been asked to do when the downloads end."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from .frameless import FramelessDialog


class PowerCountdown(FramelessDialog):
    """Counts down from `seconds`. `go` fires when it reaches zero; `called_off` when the person cancels (Cancel, Esc or the
    close button). Not modal, and kept on top, so it is seen when a screen has been left alone for hours."""

    go = Signal()
    called_off = Signal()

    def __init__(self, parent: QWidget | None, phrase: str, seconds: int = 60):
        super().__init__(parent, "Distill", "clock", None, (400, 0))
        self._phrase = phrase
        self._left = max(1, int(seconds))
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        lay = self.body((20, 18, 20, 16), 14)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setMinimumWidth(320)
        lay.addWidget(self.label)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel = QPushButton("Cancel")
        self.cancel.setDefault(True)
        self.cancel.clicked.connect(self.reject)
        row.addWidget(self.cancel)
        lay.addLayout(row)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._refresh()
        self._timer.start()

    def seconds_left(self) -> int:
        return self._left

    def _refresh(self) -> None:
        self.label.setText(f"The downloads are finished.\n\nThis computer will {self._phrase} in {self._left} "
                           f"second{'s' if self._left != 1 else ''}.")

    def _tick(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self._timer.stop()
            self.accept()
            self.go.emit()
        else:
            self._refresh()

    def reject(self) -> None:
        if self._timer.isActive():  # cancelled while counting; once it has run out there is nothing left to call off
            self._timer.stop()
            self.called_off.emit()
        super().reject()
