"""The arithmetic of the wheel glide and its bounce, kept free of Qt so it can be tested anywhere."""
from __future__ import annotations

import math

# How fast the glide closes on its target (1/s). A critically damped follower reaches ~95% of the way in about 4.7/OMEGA.
OMEGA = 17.0
# Wheel acceleration, from how fast the wheel is turning in notches per second (a notch is 120 units of wheel delta, so a free-spinning
# hi-res wheel that sends many small deltas counts the same as a clicky one that sends whole notches): at SLOW_RATE or less a notch
# counts for exactly one, so a slow turn stays precise; at FAST_RATE or more for ACCEL_MAX, so a hard spin covers ground.
SLOW_RATE = 6.0
FAST_RATE = 55.0
ACCEL_MAX = 3.0
MIN_GAP = 0.004  # events closer than this are one burst (the system coalesces them), not an infinite speed
# A glide is over when it is this close to its target (pixels) and moving this slowly (pixels per second).
REST_DIST = 0.5
REST_SPEED = 12.0


def damp_step(pos: float, vel: float, target: float, dt: float, omega: float = OMEGA) -> tuple[float, float]:
    """Move `pos` toward `target` for `dt` seconds as a critically damped spring would, carrying its speed `vel` (pixels/s).

    Unlike chasing a fixed share of the gap each frame (which leaps at full speed on the very first frame), this starts from
    the speed it already has, so a glide eases in as well as out, and a notch that arrives mid-glide bends the path without a
    surge. It is the exact solution for any dt, so a late frame lands where an on-time one would have."""
    if dt <= 0.0:
        return pos, vel
    d = pos - target
    t = vel + omega * d
    decay = math.exp(-omega * dt)
    return target + (d + t * dt) * decay, (vel - omega * t * dt) * decay


def at_rest(pos: float, vel: float, target: float) -> bool:
    return abs(target - pos) < REST_DIST and abs(vel) < REST_SPEED


def wheel_gain(notches: float, gap: float | None) -> float:
    """How many times one wheel event counts, from how much it turned (`notches`, any sign) and the time since the last one
    (None: the first of a spin)."""
    if gap is None or notches == 0:
        return 1.0
    rate = abs(notches) / max(gap, MIN_GAP)
    if rate <= SLOW_RATE:
        return 1.0
    if rate >= FAST_RATE:
        return ACCEL_MAX
    x = (rate - SLOW_RATE) / (FAST_RATE - SLOW_RATE)
    return 1.0 + (ACCEL_MAX - 1.0) * x * x * (3.0 - 2.0 * x)  # smoothstep: no kink where it starts or ends


def spring_ease(t: float, zeta: float = 0.6, omega: float = 13.0) -> float:
    """Progress of an under-damped spring let go at 0 and settling on 1: it crosses 1 once, a little past (about 9% at the
    defaults), and comes back. Forced to exactly 0 at t=0 and exactly 1 at t>=1, so the animation lands on its mark.
    (`omega` is per unit of t, not per second.)"""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    wd = omega * math.sqrt(1.0 - zeta * zeta)
    e = math.exp(-zeta * omega * t)
    raw = 1.0 - e * (math.cos(wd * t) + zeta / math.sqrt(1.0 - zeta * zeta) * math.sin(wd * t))
    end = 1.0 - math.exp(-zeta * omega) * (math.cos(wd) + zeta / math.sqrt(1.0 - zeta * zeta) * math.sin(wd))
    return raw / end if end else raw  # stretched a hair so t=1 is exactly 1 (the residue at the end is ~1%)
