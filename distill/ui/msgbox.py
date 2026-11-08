"""Message boxes and one-line prompts in the app's frameless look, for the places that used QMessageBox and QInputDialog.

`information`, `warning`, `critical` and `question` take what QMessageBox's static methods took and return the same
`QMessageBox.StandardButton`, so a call site only changes the name in front of the call. `ask` is for a box with buttons of its own
(plus an optional extra line, a 'Show details' text that can be Markdown, and a tick box). `get_text` and `get_item` stand in for QInputDialog."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

from . import icons
from .frameless import FramelessDialog
from .theme import T
from .widgets import accent_button, AnimCheckBox

SB = QMessageBox.StandardButton
# (flag, label, role) in the order they sit along the bottom, left to right. 'accept' ones are filled in the accent colour.
_STANDARD = ((SB.Ok, "OK", "accept"), (SB.Yes, "Yes", "accept"), (SB.Retry, "Retry", "accept"), (SB.Save, "Save", "accept"),
             (SB.No, "No", "normal"), (SB.Discard, "Discard", "normal"), (SB.Ignore, "Ignore", "normal"), (SB.Abort, "Abort", "normal"),
             (SB.Cancel, "Cancel", "reject"), (SB.Close, "Close", "reject"))
_KINDS = {"info": ("info", lambda: T.accent_text), "question": ("info", lambda: T.accent_text),
          "warning": ("alert", lambda: T.warn), "critical": ("alert", lambda: T.bad)}


class MessageDialog(FramelessDialog):
    """`buttons` are (label, role) pairs, role being 'accept', 'normal', 'destructive' or 'reject'. After exec(), `chosen` is the index of the
    button pressed, or `escape` when the window was closed (Esc or the X); `checked` is the tick box."""

    def __init__(self, parent: QWidget | None, kind: str, title: str, text: str, buttons: list[tuple[str, str]], default: int = 0,
                 escape: int = -1, informative: str = "", details: str = "", check: str = "", markdown: bool = False,
                 details_open: bool = False, details_label: str = "details"):
        """`details` is a longer text under a Show / Hide button (`details_label` names it); `markdown` draws it as Markdown (headings,
        lists, bold, links) instead of plain text, and `details_open` shows it from the start."""
        glyph, colour = _KINDS[kind]
        super().__init__(parent, title, glyph, None, (560 if markdown and details else 460, 0))
        self._label = details_label
        self.chosen = -1
        self.escape = escape
        lay = self.body((24, 20, 24, 16), 12)
        row = QHBoxLayout()
        row.setSpacing(16)
        badge = QLabel()
        badge.setFixedSize(32, 32)
        badge.setAlignment(Qt.AlignmentFlag.AlignTop)
        T.bind(badge, lambda: badge.setPixmap(icons.pixmap(glyph, colour(), 32)))
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(8)
        main = QLabel(text)
        main.setWordWrap(True)
        main.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        main.setMinimumWidth(340)
        T.bind_css(main, lambda: f"color: {T.title.name()}; font-size: 10.5pt; background: transparent;")
        text_col.addWidget(main)
        if informative:
            note = QLabel(informative)
            note.setWordWrap(True)
            note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            T.bind_css(note, lambda: f"color: {T.fg2.name()}; background: transparent;")
            text_col.addWidget(note)
        self.details: QPlainTextEdit | QTextBrowser | None = None
        if details:
            self.details_btn = QPushButton()
            self.details_btn.setFlat(True)
            self.details_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.details_btn.setAutoDefault(False)
            if markdown:
                self.details = QTextBrowser()
                self.details.setOpenExternalLinks(True)
                self.details.setFrameShape(QFrame.Shape.NoFrame)
                self.details.setMaximumHeight(300)
                T.bind(self.details, lambda: self._draw_markdown(details))
            else:
                self.details = QPlainTextEdit(details)
                self.details.setReadOnly(True)
            self.details.setMinimumHeight(140)
            self.details.setVisible(details_open)
            self.details_btn.setText(f"Hide {self._label}" if details_open else f"Show {self._label}")
            self.details_btn.clicked.connect(self._toggle_details)
            text_col.addWidget(self.details_btn, 0, Qt.AlignmentFlag.AlignLeft)
            text_col.addWidget(self.details)
        self.tick: QCheckBox | None = None
        if check:
            self.tick = AnimCheckBox(check)
            text_col.addWidget(self.tick)
        row.addLayout(text_col, 1)
        lay.addLayout(row)
        lay.addSpacing(6)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        self.buttons: list[QPushButton] = []
        for i, (label, role) in enumerate(buttons):
            btn = accent_button(label) if role == "accept" else QPushButton(label)
            if role == "destructive":
                T.bind_css(btn, lambda: f"QPushButton {{ color: {T.bad.name()}; }}")
            btn.setAutoDefault(False)
            btn.clicked.connect(lambda _c=False, i=i: self._pick(i))
            foot.addWidget(btn)
            self.buttons.append(btn)
        lay.addLayout(foot)
        if 0 <= default < len(self.buttons):
            self.buttons[default].setDefault(True)
            self.buttons[default].setFocus()

    @property
    def checked(self) -> bool:
        return bool(self.tick and self.tick.isChecked())

    def _draw_markdown(self, text: str) -> None:
        """The notes as Markdown, in the app's colours (set again when the theme changes)."""
        view = self.details
        view.setStyleSheet(f"QTextBrowser {{ background: {T.input.name()}; color: {T.fg.name()}; border: 1px solid {T.border.name()}; "
                           f"border-radius: 10px; padding: 8px 12px; }}")
        view.document().setDefaultStyleSheet(
            f"h1, h2 {{ color: {T.title.name()}; font-size: 13pt; margin-top: 14px; margin-bottom: 4px; }} "
            f"h3, h4 {{ color: {T.title.name()}; font-size: 11pt; margin-top: 12px; margin-bottom: 2px; }} "
            f"a {{ color: {T.accent_text.name()}; }} p, li {{ margin-top: 2px; margin-bottom: 2px; }}")
        view.setMarkdown(text)

    def _toggle_details(self) -> None:
        show = not self.details.isVisible()
        self.details.setVisible(show)
        self.details_btn.setText(f"Hide {self._label}" if show else f"Show {self._label}")
        self.adjustSize()

    def _pick(self, index: int) -> None:
        self.chosen = index
        self.accept()

    def reject(self) -> None:
        self.chosen = self.escape
        super().reject()


def ask(parent: QWidget | None, kind: str, title: str, text: str, buttons: list[tuple[str, str]], *, default: int = 0, escape: int = -1,
        informative: str = "", details: str = "", check: str = "", markdown: bool = False, details_open: bool = False,
        details_label: str = "details") -> tuple[int, bool]:
    """A box with buttons of its own. Returns (index of the button pressed or `escape` if it was closed, the tick box)."""
    dlg = MessageDialog(parent, kind, title, text, buttons, default, escape, informative, details, check, markdown, details_open, details_label)
    dlg.exec()
    return dlg.chosen, dlg.checked


def _standard(parent, kind: str, title: str, text: str, buttons, default) -> QMessageBox.StandardButton:
    picked = [(flag, label, role) for flag, label, role in _STANDARD if buttons & flag] or [(SB.Ok, "OK", "accept")]
    default_at = next((i for i, (flag, _l, _r) in enumerate(picked) if flag == default), -1)
    if default_at < 0:  # as QMessageBox does when none is named: the first button that accepts
        default_at = next((i for i, (_f, _l, role) in enumerate(picked) if role == "accept"), 0)
    escape = next((i for i, (flag, _l, _r) in enumerate(picked) if flag in (SB.Cancel, SB.Close)), -1)
    if escape < 0:  # no Cancel: closing the window answers No (or OK, for a plain notice)
        escape = next((i for i, (flag, _l, _r) in enumerate(picked) if flag in (SB.No, SB.Ok)), -1)
    index, _tick = ask(parent, kind, title, text, [(label, role) for _f, label, role in picked], default=default_at, escape=escape)
    return picked[index][0] if 0 <= index < len(picked) else SB.NoButton


def information(parent, title: str, text: str, buttons=SB.Ok, default=SB.NoButton) -> QMessageBox.StandardButton:
    return _standard(parent, "info", title, text, buttons, default)


def question(parent, title: str, text: str, buttons=SB.Yes | SB.No, default=SB.NoButton) -> QMessageBox.StandardButton:
    return _standard(parent, "question", title, text, buttons, default)


def warning(parent, title: str, text: str, buttons=SB.Ok, default=SB.NoButton) -> QMessageBox.StandardButton:
    return _standard(parent, "warning", title, text, buttons, default)


def critical(parent, title: str, text: str, buttons=SB.Ok, default=SB.NoButton) -> QMessageBox.StandardButton:
    return _standard(parent, "critical", title, text, buttons, default)


# ---- one-line prompts ---------------------------------------------------------------------------------------------
class _PromptDialog(FramelessDialog):
    def __init__(self, parent: QWidget | None, title: str, label: str, field: QWidget):
        super().__init__(parent, title, "edit", None, (460, 0))
        self.field = field
        lay = self.body((24, 18, 24, 16), 10)
        question_label = QLabel(label)
        question_label.setWordWrap(True)
        lay.addWidget(question_label)
        lay.addWidget(field)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        ok = accent_button("OK")
        ok.setAutoDefault(False)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        foot.addWidget(cancel)
        foot.addWidget(ok)
        lay.addSpacing(4)
        lay.addLayout(foot)
        if isinstance(field, QLineEdit):
            field.returnPressed.connect(self.accept)
            field.selectAll()
        field.setFocus()


def get_text(parent: QWidget | None, title: str, label: str, text: str = "") -> tuple[str, bool]:
    """Like QInputDialog.getText: (what was typed, whether OK was pressed)."""
    edit = QLineEdit(text)
    dlg = _PromptDialog(parent, title, label, edit)
    ok = dlg.exec() == _PromptDialog.DialogCode.Accepted
    return edit.text(), ok


def get_item(parent: QWidget | None, title: str, label: str, items: list[str], current: int = 0, editable: bool = False) -> tuple[str, bool]:
    """Like QInputDialog.getItem: (the chosen item, whether OK was pressed)."""
    combo = QComboBox()
    combo.addItems(items)
    combo.setEditable(editable)
    combo.setCurrentIndex(max(0, min(current, len(items) - 1)))
    dlg = _PromptDialog(parent, title, label, combo)
    ok = dlg.exec() == _PromptDialog.DialogCode.Accepted
    return combo.currentText(), ok
