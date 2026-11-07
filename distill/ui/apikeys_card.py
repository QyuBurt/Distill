"""Settings > General > API keys: the person's own SteamGridDB and GitHub keys. Each edit saves itself a moment after the last
one, like the Accounts page; the key is hidden while typed and kept sealed on disk (see apikeys.py)."""
from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QGridLayout, QLabel, QLineEdit, QMessageBox, QPushButton

from .. import apikeys
from .state import LABEL_W
from .widgets import Card, hint

# name, label, where to get one, what the box says, what a key does
KEYS = (
    ("sgdb", "SteamGridDB", "https://www.steamgriddb.com/profile/preferences/api", "SteamGridDB API key (optional)"),
    ("github", "GitHub", "https://github.com/settings/personal-access-tokens/new", "GitHub token (optional)"),
)


class ApiKeysCard(Card):
    changed = Signal(str)  # the name of the key that was saved

    def __init__(self) -> None:
        super().__init__("API Keys")
        self.add(hint("Optional, and kept on this computer only. SteamGridDB (free, from steamgriddb.com > Preferences > API) "
                      "gives PC Recomps posters in one portrait size and emulators their community icons. A GitHub token "
                      "(a read-only one, with no permissions ticked, is enough) lifts GitHub's limit from 60 to 5,000 "
                      "requests an hour, which tool, emulator and recomp update checks can use up."))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._save)
        self.edits: dict[str, QLineEdit] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setColumnStretch(1, 1)
        for row, (name, label, url, placeholder) in enumerate(KEYS):
            name_label = QLabel(label)
            name_label.setFixedWidth(LABEL_W)  # the same label column as the other settings forms
            edit = QLineEdit(apikeys.get(name))
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText(placeholder)
            edit.setMinimumWidth(180)
            edit.textChanged.connect(lambda *_: self._timer.start())
            self.edits[name] = edit
            get_key, test = QPushButton("Get key..."), QPushButton("Test")
            get_key.clicked.connect(lambda _c=False, u=url: webbrowser.open(u))
            test.clicked.connect(lambda _c=False, n=name: self._test(n))
            for col, w in enumerate((name_label, edit, get_key, test)):
                grid.addWidget(w, row, col)
        self.add(grid)

    def _save(self) -> None:
        self._timer.stop()
        for name, edit in self.edits.items():
            text = edit.text().strip()
            if text != apikeys.get(name):
                apikeys.set(name, text)
                self.changed.emit(name)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self._timer.isActive():  # closing Settings right after typing must not lose it
            self._save()

    def _test(self, name: str) -> None:
        text = self.edits[name].text().strip()
        title = dict((k[0], k[1]) for k in KEYS)[name]
        if not text:
            QMessageBox.information(self, title, "Enter a key first.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            from .. import recomps  # noqa: PLC0415
            problem = recomps.sgdb_test(text) if name == "sgdb" else apikeys.test_github(text)
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(self, title, problem or "The key works.")
