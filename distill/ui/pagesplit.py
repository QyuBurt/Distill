"""Spotting PDF manuals whose pages are really two-page spreads (common for scans on archive.org: each PDF page is a wide picture of
the left and right page of an open book), so the reader can show the halves as separate pages. No Qt, tested on its own.

A manual is read as a list of *units*: (PDF page number, part) with part 'whole', 'left' or 'right'. A normal PDF is all 'whole'.
"""
from __future__ import annotations

from statistics import median

WIDE = 1.15  # width / height from which a page may be two pages side by side (a portrait manual page is about 0.65 to 0.8)
MAX_SPREAD = 2.5  # wider than this, each half would itself be landscape: it is a panorama or a poster, not two pages
SHARE = 0.5  # at least this share of the pages must be wide before the document counts as scanned spreads

Unit = tuple[int, str]


def _ratio(size: tuple[float, float]) -> float:
    w, h = size
    return w / h if w > 0 and h > 0 else 0.0


def _splittable(size: tuple[float, float], floor: float = WIDE) -> bool:
    return floor <= _ratio(size) <= MAX_SPREAD


def looks_like_spreads(sizes: list[tuple[float, float]]) -> bool:
    """True when most pages are wide enough to be two portrait pages side by side. A few wide pages in a portrait manual (a fold-out map)
    are not enough."""
    if not sizes:
        return False
    wide = sum(1 for s in sizes if _splittable(s))
    return wide >= max(1, SHARE * len(sizes))


def page_units(sizes: list[tuple[float, float]], mode: str = "auto") -> list[Unit]:
    """The pages to show. mode 'auto' splits wide pages when the document looks like scanned spreads; 'on' splits every wide page
    (the player says so); 'off' splits none. Pages that are not wide (a lone cover) stay whole."""
    split = looks_like_spreads(sizes) if mode == "auto" else mode == "on"
    out: list[Unit] = []
    for i, s in enumerate(sizes):
        if split and _splittable(s, 1.0 if mode == "on" else WIDE):
            out += [(i, "left"), (i, "right")]
        else:
            out.append((i, "whole"))
    return out


def unit_aspect(size: tuple[float, float], part: str) -> float:
    r = _ratio(size)
    return r / 2 if part != "whole" else r


def book_aspect(sizes: list[tuple[float, float]], units: list[Unit], default: float = 0.72) -> float:
    """One page's width / height for the book: the middle one over all pages (a lone landscape fold-out must not change the book's shape)."""
    vals = [unit_aspect(sizes[i], part) for i, part in units if _ratio(sizes[i]) > 0]
    return float(median(vals)) if vals else default


def first_unit_of(units: list[Unit], pdf_page: int) -> int:
    """The index of the first unit that comes from a PDF page (0 if none does). The units need not be in PDF order (see with_cover)."""
    for n, (i, _part) in enumerate(units):
        if i == pdf_page:
            return n
    return 0


def with_cover(units: list[Unit], cover: Unit | None) -> list[Unit]:
    """The units with the player's front cover first: the pages before it (a back cover scanned beside the front one, say) go to the end,
    where a back cover belongs. `cover` that is no longer there (the pages were cut differently) falls back to its PDF page's first unit."""
    if not cover or not units:
        return units
    n = units.index(cover) if cover in units else first_unit_of(units, cover[0])
    return units[n:] + units[:n]


def cover_text(cover: Unit | None) -> str:
    return f"{cover[0]}:{cover[1]}" if cover else ""


def parse_cover(text: str) -> Unit | None:
    """Back from cover_text; None for nothing or anything unreadable."""
    page, _, part = text.partition(":")
    return (int(page), part) if page.isdigit() and part in ("whole", "left", "right") else None
