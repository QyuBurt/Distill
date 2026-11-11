"""The arithmetic of turning the player's disc by hand: where the pointer is round the disc, how far that is in track time, and how
loud a scratch sounds. Kept free of Qt so it can be tested anywhere."""
from __future__ import annotations

import math

SPIN = 160.0  # degrees per second the disc turns while playing (CdArt.SPIN says the same)
MS_PER_DEGREE = 1000.0 / SPIN  # turn it by hand at the playing speed and the track keeps pace with it
END_GUARD_MS = 300  # never scrub right to the end: that would end the track and move on to the next one
SLOW = 25.0  # degrees per second: slower than this is a hand resting on the disc, not scratching
FAST = 600.0  # ... and this is a hard flick: as loud and as quick as the sound gets


def pointer_angle(dx: float, dy: float) -> float:
    """Degrees of the pointer round the disc's centre, growing clockwise on screen (y points down), as the disc itself turns."""
    return math.degrees(math.atan2(dy, dx))


def turn(prev: float, new: float) -> float:
    """The shortest signed turn from one angle to another, in degrees (-180 to 180), so crossing the 180 / -180 seam is no jump."""
    return (new - prev + 180.0) % 360.0 - 180.0


def scrub_ms(start_ms: int, turned: float, duration_ms: int) -> int:
    """The track time after turning the disc `turned` degrees from where it was grabbed (clockwise = forward). Stays inside the track."""
    if duration_ms <= 0:
        return max(int(start_ms), 0)
    return int(min(max(round(start_ms + turned * MS_PER_DEGREE), 0), max(duration_ms - END_GUARD_MS, 0)))


def grain_gain(speed: float) -> float:
    """How loud one scratch grain is for a disc turning `speed` degrees per second (either way): 0 when it is hardly moving."""
    s = abs(speed)
    if s < SLOW:
        return 0.0
    return 0.2 + 0.8 * min((s - SLOW) / (FAST - SLOW), 1.0)


def grain_gap_ms(speed: float) -> int:
    """How long to wait before the next grain: they come quicker the faster the disc is turned (110 ms down to 50 ms)."""
    return int(110 - 60 * min(max((abs(speed) - SLOW) / (FAST - SLOW), 0.0), 1.0))
