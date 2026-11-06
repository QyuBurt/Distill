"""The arithmetic of a shared-element flight, kept free of Qt so it can be tested anywhere."""
from __future__ import annotations

Box = tuple[int, int, int, int]  # x, y, width, height


def ease_in_out_cubic(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def lerp_box(a: Box, b: Box, t: float) -> Box:
    """The box `t` of the way (0..1) from `a` to `b`, in whole pixels; exactly `a` at 0 and exactly `b` at 1."""
    if t <= 0:
        return a
    if t >= 1:
        return b
    return tuple(round(p + (q - p) * t) for p, q in zip(a, b))  # type: ignore[return-value]
