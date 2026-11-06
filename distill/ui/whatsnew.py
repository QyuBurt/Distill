"""A one-time 'What's new' note the first time a new version starts."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTextBrowser, QVBoxLayout, QWidget

from ..constants import APP_VERSION

# Release notes shown once after an update, as (heading, items). Empty for 1.0: a first install has nothing to compare to.
NOTES: tuple[tuple[str, tuple[str, ...]], ...] = ()


def notes_html() -> str:
    parts = []
    for heading, items in NOTES:
        parts.append(f"<h3>{heading}</h3><ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
    return "".join(parts)


class WhatsNewDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"What's new in {APP_VERSION}")
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>Distill {APP_VERSION}</b>"))
        view = QTextBrowser()
        view.setOpenExternalLinks(False)
        view.setHtml(notes_html())
        lay.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)
