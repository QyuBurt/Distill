"""The arithmetic of the update screen's filling logo, kept free of Qt so it can be tested anywhere."""
from __future__ import annotations

import math


def fraction(done: int, total: int) -> float:
    """How much of the download is in, 0..1. An unknown size (total <= 0) counts as nothing yet."""
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, done / total))


def percent_text(frac: float) -> str:
    """'42%'. Never reads 100% until the fraction really is 1, so a nearly-done download doesn't look finished."""
    frac = min(1.0, max(0.0, frac))
    return f"{min(99, int(frac * 100))}%" if frac < 1.0 else "100%"


def size_text(done: int, total: int) -> str:
    """'12.4 of 48.0 MB', or just '12.4 MB' when the total is not known."""
    mb = lambda n: f"{n / (1 << 20):.1f}"  # noqa: E731
    return f"{mb(done)} of {mb(total)} MB" if total > 0 else f"{mb(done)} MB"


def edge_y(top: float, height: float, frac: float) -> float:
    """The y of the colour's top edge in a picture `height` tall that starts at `top`: the bottom at 0, the top at 1."""
    return top + height * (1.0 - min(1.0, max(0.0, frac)))


def wave_amplitude(frac: float, peak: float) -> float:
    """How tall the ripple on the colour's edge is. It dies away to nothing at 0 and at 1, so the fill starts and ends flat."""
    frac = min(1.0, max(0.0, frac))
    return peak * math.sin(math.pi * frac) ** 0.5
