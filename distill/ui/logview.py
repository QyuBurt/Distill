"""The run log: colours the lines that matter (errors, warnings) so a long log can be skimmed."""
from __future__ import annotations

import re

from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat, QTextDocument

from .theme import T

BAD_RE = re.compile(r"\b(error|errors|failed|failure|exception|traceback|corrupt\w*)\b", re.IGNORECASE)
WARN_RE = re.compile(r"\b(warning|warn|paused|retry\w*|skipp\w+|stopped|aborted)\b", re.IGNORECASE)
GOOD_RE = re.compile(r"\b(verified|complete[d]?|finished successfully|all \d+ item\(s\) finished)\b", re.IGNORECASE)


def _fmt(colour: QColor, bold: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(colour)
    if bold:
        f.setFontWeight(600)
    return f


class LogHighlighter(QSyntaxHighlighter):
    """Red for failures, amber for warnings and stops, green for verified / complete lines."""

    def __init__(self, document: QTextDocument):
        super().__init__(document)
        self.refresh_colours(rehighlight=False)

    def refresh_colours(self, rehighlight: bool = True) -> None:
        self._bad, self._warn, self._good = _fmt(T.bad, True), _fmt(T.warn), _fmt(T.ok)
        if rehighlight:
            self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        if not text:
            return
        if BAD_RE.search(text):
            self.setFormat(0, len(text), self._bad)
        elif WARN_RE.search(text):
            self.setFormat(0, len(text), self._warn)
        elif GOOD_RE.search(text):
            self.setFormat(0, len(text), self._good)
