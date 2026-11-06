"""Ctrl+K: type a few letters of a page's name and press Enter to go there."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

KEY_ROLE = Qt.ItemDataRole.UserRole


class GoToDialog(QDialog):
    """A filter box over a list of (label, key). `chosen()` is the key of the picked entry, or None."""

    def __init__(self, entries: list[tuple[str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Go To")
        self.setMinimumWidth(380)
        self._entries = entries
        lay = QVBoxLayout(self)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Go to a page...")
        self.edit.setClearButtonEnabled(True)
        self.list = QListWidget()
        self.list.setMinimumHeight(260)
        lay.addWidget(self.edit)
        lay.addWidget(self.list)
        self.edit.textChanged.connect(self._filter)
        self.edit.returnPressed.connect(self._accept_current)
        self.list.itemActivated.connect(lambda _i: self._accept_current())
        self.edit.installEventFilter(self)
        self._filter("")

    def _filter(self, text: str) -> None:
        terms = text.lower().split()
        self.list.clear()
        for label, key in self._entries:
            if all(t in label.lower() for t in terms):
                item = QListWidgetItem(label)
                item.setData(KEY_ROLE, key)
                self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def eventFilter(self, obj, event) -> bool:  # Up / Down move the selection while the cursor stays in the box
        if obj is self.edit and event.type() == QEvent.Type.KeyPress:
            step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1}.get(event.key())
            if step is not None and self.list.count():
                self.list.setCurrentRow(min(max(self.list.currentRow() + step, 0), self.list.count() - 1))
                return True
        return super().eventFilter(obj, event)

    def _accept_current(self) -> None:
        if self.list.currentItem() is not None:
            self.accept()

    def chosen(self) -> str | None:
        item = self.list.currentItem()
        return item.data(KEY_ROLE) if item is not None and self.result() == QDialog.DialogCode.Accepted else None
