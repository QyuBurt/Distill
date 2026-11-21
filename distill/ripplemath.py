"""The arithmetic of the ripple on a progress bar's leading edge, kept free of Qt so it can be tested anywhere.

The wave itself is the one on the update screen's filling logo (fillmath.wave_amplitude: it dies away at 0 and at 1, so the bar
starts and finishes flat); here it runs along the edge of a bar that fills left to right, so the edge wobbles sideways.
"""
from __future__ import annotations

import math

PHASE_RATE = 1.1  # radians a second the ripple travels (the update screen's logo uses 3.6, the Emulators water 2.4); a slow drift on a bar
WAVE_LENGTH = 2.3  # a wavelength is this many bar-heights long: wide and gentle rather than a tight wobble
STRIP_WAVE = 2.0  # the bottom strip's wavelength, in strip-heights (it was 0.9: a tight wobble)
FILL_TAU = 0.55  # seconds: how quickly a bar's drawn fill closes on the value it was given (it glides between updates)
FILL_EPS = 0.0005  # the drawn fill counts as arrived when it is this close (a fraction of the bar)


def fraction(value: int, minimum: int, maximum: int) -> float:
    """How full a bar is, 0..1. An empty or backwards range counts as empty."""
    if maximum <= minimum:
        return 0.0
    return min(1.0, max(0.0, (value - minimum) / (maximum - minimum)))


def chase(shown: float, target: float, dt: float, tau: float = FILL_TAU) -> float:
    """The drawn fill `dt` seconds later, closing a fixed share of the gap each moment (an exponential ease-out, sized by the real
    time elapsed, so the pace never depends on the frame rate). It only moves forwards; a target behind it is a reset and jumps."""
    if target <= shown:
        return target
    nxt = shown + (target - shown) * (1.0 - math.exp(-max(0.0, dt) / max(tau, 1e-3)))
    return target if target - nxt < FILL_EPS else nxt


def is_rippling(frac: float) -> bool:
    """Only a bar part-way is animated; an empty or a full one is still, and costs nothing."""
    return 0.0 < frac < 1.0


def next_phase(phase: float, dt: float, rate: float = PHASE_RATE) -> float:
    """The wave's phase `dt` seconds later, kept within one turn."""
    return (phase + rate * max(0.0, dt)) % (2 * math.pi)


def edge_offset(amp: float, phase: float, y: float, wavelength: float) -> float:
    """How far the edge is pushed sideways at height `y`."""
    return amp * math.sin(2 * math.pi * y / max(wavelength, 1.0) + phase)


def edge_points(edge_x: float, top: float, bottom: float, amp: float, phase: float, wavelength: float,
                step: float = 2.0) -> list[tuple[float, float]]:
    """(x, y) points down the leading edge from `top` to `bottom`, always ending exactly on `bottom`."""
    if bottom <= top:
        return [(edge_x + edge_offset(amp, phase, top, wavelength), top)]
    points, y = [], top
    while y < bottom:
        points.append((edge_x + edge_offset(amp, phase, y - top, wavelength), y))
        y += max(step, 0.5)
    points.append((edge_x + edge_offset(amp, phase, bottom - top, wavelength), bottom))
    return points
