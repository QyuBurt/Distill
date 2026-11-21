"""How tall the Links box on the Files & Torrents page should be for what is in it: one line for an empty box or a single link, one
more line for each extra link, up to a limit (after which it scrolls). No Qt.
"""
from __future__ import annotations

MAX_ROWS = 6


def rows_for(text: str, max_rows: int = MAX_ROWS) -> int:
    """How many lines to show: one per line of text (the box does not wrap), at least one, at most `max_rows`."""
    return max(1, min(max_rows, text.count("\n") + 1))


def box_height(text: str, line_height: int, overhead: int, max_rows: int = MAX_ROWS) -> int:
    """Pixels tall for `text`. `overhead` is what the box adds around its lines (border, padding and the document's own margin)."""
    return overhead + rows_for(text, max_rows) * max(1, line_height)
