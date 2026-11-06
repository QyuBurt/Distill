"""Wrapping long tooltips (no Qt here, so it is easy to test): Qt shows a plain tooltip on one line, however long."""
from __future__ import annotations

import html
import textwrap

TOOLTIP_WIDTH = 56  # characters per line: about 380 px, a comfortable column


def wrap_tooltip(text: str, width: int = TOOLTIP_WIDTH) -> str:
    """Break long lines at word boundaries. Lines that are short, or start with a space (hand-aligned examples), and
    existing line breaks are left exactly as written."""
    lines: list[str] = []
    for line in text.split("\n"):
        if len(line) <= width or line[:1].isspace():
            lines.append(line)
        else:
            lines.extend(textwrap.wrap(line, width=width, break_long_words=False, break_on_hyphens=False) or [""])
    return "\n".join(lines)


def looks_like_html(text: str) -> bool:
    """A tooltip that is deliberately rich text (Qt wraps those itself)."""
    return text.lstrip().lower().startswith(("<html", "<p>", "<p ", "<b>", "<i>", "<br", "<div", "<table", "<qt"))


def tooltip_html(text: str, width: int = TOOLTIP_WIDTH) -> str:
    """The wrapped text as rich text that keeps its line breaks and spacing, and shows '<Game>' as typed."""
    return "<div style='white-space:pre'>" + html.escape(wrap_tooltip(text, width)) + "</div>"
