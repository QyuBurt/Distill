"""The pond's sounds, made from maths (like the record sounds in make_vinyl_sounds.py): nothing recorded, nothing to license.

Four tuned voices, each in ten notes of the pond's scale (bell, blip, pluck, bass) and three drums (kick, snare, hat). `render`
gives one sound as 16-bit mono PCM; `write_all` puts them all in a folder as small .wav files the window loads. The same every
time (fixed random seed per sound). No Qt in here.
"""
from __future__ import annotations

import math
import random
import wave
from array import array
from pathlib import Path

from .pondmath import DEGREES, DRUMS, TIMBRES, frequency, midi_of

RATE = 22050
TAU = 2 * math.pi


def all_keys() -> list[str]:
    return [f"{t}:{d}" for t in TIMBRES for d in range(DEGREES)] + list(DRUMS)


def file_name(key: str) -> str:
    return key.replace(":", "-") + ".wav"


def _attack(t: float, secs: float = 0.004) -> float:
    return min(1.0, t / secs)


def _bell(f: float) -> list[float]:
    parts = ((1.0, 1.0, 1.3), (2.76, 0.45, 0.7), (5.4, 0.2, 0.35), (8.93, 0.08, 0.2))  # (frequency ratio, loudness, decay seconds)
    n = int(RATE * 1.7)
    return [sum(a * math.exp(-(i / RATE) / tau) * math.sin(TAU * f * ratio * i / RATE) for ratio, a, tau in parts) * _attack(i / RATE)
            for i in range(n)]


def _blip(f: float) -> list[float]:
    out, phase = [], 0.0
    for i in range(int(RATE * 0.34)):
        t = i / RATE
        phase += TAU * f * (1 + 0.04 * math.exp(-t / 0.03)) / RATE
        out.append((math.sin(phase) + 0.33 * math.sin(3 * phase) + 0.2 * math.sin(5 * phase)) * math.exp(-t / 0.07) * _attack(t, 0.002))
    return out


def _pluck(f: float, rng: random.Random) -> list[float]:
    length = max(2, int(RATE / f))
    ring = [rng.uniform(-1, 1) for _ in range(length)]
    out = []
    for i in range(int(RATE * 1.1)):
        j = i % length
        ring[j] = 0.5 * (ring[j] + ring[(i + 1) % length]) * 0.996
        out.append(ring[j])
    return out


def _bass(f: float) -> list[float]:
    out, phase = [], 0.0
    for i in range(int(RATE * 0.95)):
        t = i / RATE
        phase += TAU * f / RATE
        out.append((math.sin(phase) + 0.35 * math.sin(2 * phase) + 0.1 * math.sin(3 * phase)) * math.exp(-t / 0.35) * _attack(t, 0.008))
    return out


def _kick() -> list[float]:
    out, phase = [], 0.0
    for i in range(int(RATE * 0.4)):
        t = i / RATE
        phase += TAU * (45 + 90 * math.exp(-t / 0.035)) / RATE
        out.append(math.sin(phase) * math.exp(-t / 0.14) + (0.3 if i < 8 else 0.0))
    return out


def _snare(rng: random.Random) -> list[float]:
    return [(rng.uniform(-1, 1) * math.exp(-(i / RATE) / 0.06) * 0.8 + math.sin(TAU * 190 * i / RATE) * math.exp(-(i / RATE) / 0.04) * 0.6)
            for i in range(int(RATE * 0.3))]


def _hat(rng: random.Random) -> list[float]:
    out, prev = [], 0.0
    for i in range(int(RATE * 0.12)):
        n = rng.uniform(-1, 1)
        out.append((n - prev) * math.exp(-(i / RATE) / 0.018))  # the difference of noise: its highs only
        prev = n
    return out


def render(key: str) -> bytes:
    """One sound as 16-bit mono PCM at RATE."""
    rng = random.Random(key)
    name, _, degree = key.partition(":")
    if name in TIMBRES:
        f = frequency(midi_of(name, int(degree)))
        samples = {"bell": lambda: _bell(f), "blip": lambda: _blip(f), "pluck": lambda: _pluck(f, rng), "bass": lambda: _bass(f)}[name]()
    elif name == "kick":
        samples = _kick()
    elif name == "snare":
        samples = _snare(rng)
    elif name == "hat":
        samples = _hat(rng)
    else:
        raise ValueError(f"no such pond sound: {key}")
    peak = max(max((abs(s) for s in samples), default=0.0), 1e-9)
    gain = (0.85 if name in DRUMS else 0.6) / peak
    fade = int(RATE * 0.005)  # the last few milliseconds fade out, so nothing ends on a click
    for i in range(1, min(fade, len(samples)) + 1):
        samples[-i] *= i / fade
    return array("h", (int(max(-1.0, min(1.0, s * gain)) * 32767) for s in samples)).tobytes()


def write_all(folder: Path) -> list[Path]:
    """Make every sound that is not in `folder` yet; returns all the files."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for key in all_keys():
        path = folder / file_name(key)
        if not path.is_file() or path.stat().st_size < 100:
            part = path.with_suffix(".part")
            with wave.open(str(part), "wb") as f:
                f.setnchannels(1)
                f.setsampwidth(2)
                f.setframerate(RATE)
                f.writeframes(render(key))
            part.replace(path)
        paths.append(path)
    return paths
