"""Writes the little record sounds the Soundtracks player's disc makes when you hold it and spin it (distill/assets/sounds/).

They are made from maths, not recorded, so there is nothing to license and each file is a few KB. Run `python make_vinyl_sounds.py`
to rebuild them; the output is the same every time (fixed random seed).

    vinyl-stop.wav    the record being stopped by a hand: a low "whoomp" falling away over some surface hiss
    vinyl-start.wav   the same sound backwards and shorter: the record winding back up to speed
    scratch-fwd.wav   a short grain of scratch, sharp at the start (the disc being pushed forward)
    scratch-back.wav  the same grain swelling to a sharp cut (the disc being pulled back)
"""
from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

RATE = 22050
OUT = Path(__file__).resolve().parent / "distill" / "assets" / "sounds"


def _normalise(samples: list[float], peak: float) -> list[float]:
    top = max(max(abs(s) for s in samples), 1e-9)
    return [s * peak / top for s in samples]


def _write(name: str, samples: list[float]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT / name), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples))


def _stop(seconds: float, rng: random.Random) -> list[float]:
    out, phase, hiss = [], 0.0, 0.0
    n = int(RATE * seconds)
    for i in range(n):
        t, u = i / RATE, i / n
        phase += 2 * math.pi * (10 + 110 * (1 - u) ** 2.2) / RATE  # the pitch falls away, lower and softer than before
        tone = math.sin(phase) * 0.7 + math.sin(2 * phase) * 0.08
        hiss += (rng.uniform(-1, 1) - hiss) * 0.1  # a faint, soft surface noise (no crackle)
        env = (1 - u) ** 1.8 * min(1.0, t / 0.03)  # a gentle attack: no click at the start
        out.append((tone * 0.9 + hiss * 0.15 * (1 - u)) * env)
    return _normalise(out, 0.45)


def _grain(forward: bool, rng: random.Random) -> list[float]:
    n, out, slow, fast, phase = int(RATE * 0.09), [], 0.0, 0.0, 0.0
    for i in range(n):
        u = i / n
        t = (u if forward else 1 - u) * 0.09  # the backward grain is the forward one's envelope, reversed
        env = min(1.0, t / 0.012) * math.exp(-t / 0.028) * min(1.0, (1 - u) / 0.15)  # soft in, soft out
        x = rng.uniform(-1, 1)
        slow += (x - slow) * 0.12
        fast += (x - fast) * 0.3  # a narrower band than before: a soft "shff", not a harsh rasp
        phase += 2 * math.pi * ((140 + 120 * u) if forward else (260 - 120 * u)) / RATE
        out.append(((fast - slow) * 0.8 + math.sin(phase) * 0.25) * env)
    out[-1] = 0.0
    return _normalise(out, 0.4)


def main() -> None:
    rng = random.Random(1234)
    _write("vinyl-stop.wav", _stop(0.85, rng))
    start = _stop(0.5, rng)[::-1]
    for i in range(int(RATE * 0.04)):  # the reversed tail ends on a hard edge: ease it out
        start[-1 - i] *= i / (RATE * 0.04)
    _write("vinyl-start.wav", start)
    _write("scratch-fwd.wav", _grain(True, rng))
    _write("scratch-back.wav", _grain(False, rng))


if __name__ == "__main__":
    main()
