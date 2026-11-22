"""The arithmetic of the Distill pond (a small easter egg): ripples spreading over water, little animals that answer them with a
note, and a beat clock that lines the notes up. No Qt in here, so all of it is tested anywhere.

A ripple is a ring growing at a fixed speed whose strength fades with distance. An animal is "hit" at the moment the ring
crosses it. What it does then depends on its kind: the jelly rings a bell, the minnows dart off in an arpeggio, the tadpole plucks
a string and leaves a small ripple of its own (so animals can set each other off, a few rounds at most), the snail hums a low
note, the crab plays a drum. Pitch comes from where the animal is (higher on the water = higher note), always in one
minor pentatonic scale, so anything played together sounds fine. With "snap" on, every note waits for the next beat-grid step.
"""
from __future__ import annotations

import heapq
import itertools
import math
import random
from dataclasses import dataclass, field

PENT = (0, 3, 5, 7, 10)  # minor pentatonic, in semitones above the root
DEGREES = 10  # two octaves of it
TIMBRES = {"bell": 69, "blip": 57, "pluck": 45, "bass": 33}  # the root (A) each sound starts from, as a MIDI note
DRUMS = ("kick", "snare", "hat")
KINDS = ("jelly", "minnow", "tadpole", "snail", "crab")
SCHOOL = 4  # minnows come in a school

SPEED = 250.0  # px a second a ripple spreads
REACH = 330.0  # px over which a ripple's strength falls to 1/e
RIPPLE_LIFE = 3.4  # seconds
HIT_MIN = 0.14  # a ripple weaker than this passes an animal unnoticed
MAX_GEN = 3  # an animal's own ripple may set off another animal, whose ripple another, but no further
MARGIN = 24.0
MAX_PENDING = 64  # notes waiting for their beat; more than this are dropped
BPM_RANGE = (60, 160)
STEPS_PER_BEAT = 2  # the snap grid: eighth notes


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def midi_of(timbre: str, degree: int) -> int:
    degree = int(clamp(degree, 0, DEGREES - 1))
    return TIMBRES[timbre] + 12 * (degree // len(PENT)) + PENT[degree % len(PENT)]


def frequency(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def degree_at(frac_up: float) -> int:
    """The scale step for a position, 0 (bottom of the pond or its left edge) to 1 (top / right edge)."""
    return min(DEGREES - 1, int(clamp(frac_up, 0.0, 1.0) * DEGREES))


def height_degree(y: float, h: float) -> int:
    return degree_at(1.0 - y / max(h, 1.0))


# ---- ripples ------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Ripple:
    x: float
    y: float
    born: float
    power: float = 1.0
    reach: float = REACH
    gen: int = 0  # 0 for one the player made; an animal's answer is one more than the ripple that set it off
    source: int = 0  # the animal that made it (it does not hear its own ripple)
    hue: float = 0.0  # 0..1 along the brand gradient, for drawing

    def radius(self, t: float) -> float:
        return max(0.0, (t - self.born) * SPEED)

    def strength(self, t: float) -> float:
        return self.power * math.exp(-self.radius(t) / max(self.reach, 1.0))

    def alive(self, t: float) -> bool:
        return t - self.born < RIPPLE_LIFE and self.strength(t) > 0.04

    def passes(self, px: float, py: float, t0: float, t1: float) -> float:
        """The strength with which the ring crossed the point between t0 and t1, or 0 when it did not (or was too weak to notice)."""
        d = math.hypot(px - self.x, py - self.y)
        if self.radius(t0) < d <= self.radius(t1):
            s = self.strength(t1)
            return s if s >= HIT_MIN else 0.0
        return 0.0


# ---- the beat -------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Clock:
    bpm: float = 92.0
    origin: float = 0.0  # the time a bar began

    @property
    def beat_len(self) -> float:
        return 60.0 / self.bpm

    @property
    def step_len(self) -> float:
        return self.beat_len / STEPS_PER_BEAT

    def set_bpm(self, bpm: float, now: float) -> None:
        """A new tempo from this moment on: the bar restarts here, so the beat never jumps."""
        self.bpm = clamp(bpm, *BPM_RANGE)
        self.origin = now

    def quantise(self, t: float) -> float:
        """The first grid step at or after `t`."""
        k = math.ceil((t - self.origin) / self.step_len - 1e-6)
        return self.origin + k * self.step_len

    def beats(self, t0: float, t1: float) -> list[tuple[float, bool]]:
        """The beats that fell in (t0, t1], each with whether it is the first of its bar."""
        out = []
        n = math.floor((t0 - self.origin) / self.beat_len) + 1
        while (when := self.origin + n * self.beat_len) <= t1:
            out.append((when, n % 4 == 0))
            n += 1
        return out


class Score:
    """Notes waiting for their time."""

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, str, float, int]] = []
        self._seen: set[tuple[int, str]] = set()
        self._count = itertools.count()

    def __len__(self) -> int:
        return len(self._heap)

    def add(self, when: float, key: str, vel: float, who: int = 0) -> bool:
        slot = (round(when * 1000), key)
        if slot in self._seen or len(self._heap) >= MAX_PENDING:  # the same note twice at once is one note, not a louder one
            return False
        self._seen.add(slot)
        heapq.heappush(self._heap, (when, next(self._count), key, vel, who))
        return True

    def due(self, now: float) -> list[tuple[str, float, int]]:
        out = []
        while self._heap and self._heap[0][0] <= now:
            when, _n, key, vel, who = heapq.heappop(self._heap)
            self._seen.discard((round(when * 1000), key))
            out.append((key, vel, who))
        return out

    def clear(self) -> None:
        self._heap.clear()
        self._seen.clear()


# ---- the animals ----------------------------------------------------------------------------------------------------
PHASE_RATE = {"jelly": 2.2, "minnow": 1.0, "tadpole": 1.0, "snail": 1.2, "crab": 1.0}
COOLDOWN = {"jelly": 0.5, "minnow": 0.3, "tadpole": 0.45, "snail": 0.7, "crab": 0.22}


@dataclass(slots=True)
class Reaction:
    hits: list[tuple[float, str, float]] = field(default_factory=list)  # (seconds from now, sound key, loudness 0..1)
    echo: tuple[float, float] | None = None  # (power, reach) of a ripple the animal makes in answer


@dataclass(eq=False, slots=True)
class Creature:
    kind: str
    x: float
    y: float
    cid: int
    heading: float = 0.0
    speed: float = 30.0
    burst: float = 0.0  # extra speed from a fright, dying away
    phase: float = 0.0
    energy: float = 0.0  # 1 at the moment its note sounds, then fading: drawn as a glow
    squeeze: float = 0.0  # a pulse / retreat, fading
    cool: float = 0.0  # it does not hear another ripple before this time
    drum: str = ""
    index: int = 0  # which of a school it is
    dir: float = 1.0

    def step(self, dt: float, w: float, h: float, rng: random.Random) -> None:
        k, m = self.kind, MARGIN
        self.phase += dt * PHASE_RATE[k]
        self.energy = max(0.0, self.energy - dt * 2.4)
        self.squeeze = max(0.0, self.squeeze - dt * 1.3)
        self.burst *= math.exp(-3.0 * dt)
        if k == "crab":
            if rng.random() < dt * 0.35:
                self.dir = -self.dir
            self.x += self.dir * (16 + self.burst) * dt
            self.y = h - 36
        elif k == "jelly":
            self.x += math.sin(self.phase * 0.17) * 9 * dt
            self.y += math.cos(self.phase * 0.23) * 11 * dt - self.burst * 0.1 * dt
        elif k == "snail":
            self.heading += rng.gauss(0, 0.7) * dt
            self.x += math.cos(self.heading) * 7 * dt
            self.y += math.sin(self.heading) * 4 * dt
        else:  # minnow, tadpole
            self.heading += rng.gauss(0, 1.1 if k == "minnow" else 0.8) * dt
            v = self.speed + self.burst
            self.x += math.cos(self.heading) * v * dt
            self.y += math.sin(self.heading) * v * dt
        top = m + 20 if k == "jelly" else m
        bottom = {"jelly": h * 0.78, "snail": h - 30, "crab": h - 30}.get(k, h - m)
        low = h * 0.55 if k == "snail" else top
        if self.x < m:
            self.x, self.heading, self.dir = m, math.pi - self.heading, 1.0
        elif self.x > w - m:
            self.x, self.heading, self.dir = w - m, math.pi - self.heading, -1.0
        if self.y < low:
            self.y, self.heading = low, -self.heading
        elif self.y > bottom:
            self.y, self.heading = bottom, -self.heading

    def react(self, strength: float, ox: float, oy: float, now: float, w: float, h: float) -> Reaction | None:
        """What this animal does as a ripple of `strength` from (ox, oy) crosses it; None while it is still busy with the last one."""
        if now < self.cool:
            return None
        self.cool = now + COOLDOWN[self.kind]
        vel = clamp(0.35 + 0.8 * strength, 0.0, 1.0)
        away = math.atan2(self.y - oy, self.x - ox)
        k, d = self.kind, height_degree(self.y, h)
        if k == "jelly":
            self.burst = 60.0
            return Reaction([(0.0, f"bell:{d}", vel), (0.0, f"bell:{min(DEGREES - 1, d + 2)}", vel * 0.5)])
        if k == "minnow":
            self.heading, self.burst = away, 230.0
            return Reaction([(self.index * 0.04, f"blip:{min(DEGREES - 1, d + self.index % 3)}", vel * 0.8)])
        if k == "tadpole":
            self.heading, self.burst = away, 90.0
            return Reaction([(0.0, f"pluck:{d}", vel)], echo=(strength * 0.6, 110.0))
        if k == "snail":
            self.squeeze = 1.0
            return Reaction([(0.0, f"bass:{degree_at(self.x / max(w, 1.0))}", min(1.0, vel + 0.1))])
        self.burst = 70.0  # crab
        return Reaction([(0.0, self.drum or "kick", vel)])


def make_creatures(kind: str, x: float, y: float, ids, rng: random.Random, drum: str = "") -> list[Creature]:
    """A new animal at (x, y); minnows come as a school scattered around the point."""
    if kind == "minnow":
        heading = rng.uniform(0, 2 * math.pi)
        return [Creature("minnow", x + rng.uniform(-22, 22), y + rng.uniform(-22, 22), next(ids), heading + rng.uniform(-0.4, 0.4),
                         speed=rng.uniform(34, 48), phase=rng.uniform(0, 6), index=i) for i in range(SCHOOL)]
    if kind == "crab":
        return [Creature("crab", x, y, next(ids), drum=drum or rng.choice(DRUMS), dir=rng.choice((-1.0, 1.0)), phase=rng.uniform(0, 6))]
    return [Creature(kind, x, y, next(ids), rng.uniform(0, 2 * math.pi), speed=rng.uniform(34, 50), phase=rng.uniform(0, 6))]


# ---- the pond -------------------------------------------------------------------------------------------------------
class Pond:
    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.w, self.h = 900.0, 600.0
        self.creatures: list[Creature] = []
        self.ripples: list[Ripple] = []
        self.clock = Clock()
        self.score = Score()
        self.tide = False  # a ripple from the middle on every beat (a big one on the first of the bar)
        self.snap = True  # notes wait for the beat grid
        self.last = 0.0
        self._ids = itertools.count(1)
        self._drums = itertools.cycle(DRUMS)

    def resize(self, w: float, h: float) -> None:
        self.w, self.h = float(w), float(h)

    def spawn(self, kind: str, x: float | None = None, y: float | None = None) -> list[Creature]:
        m = MARGIN + 30
        x = self.rng.uniform(m, max(m + 1, self.w - m)) if x is None else x
        if y is None:
            y = {"crab": self.h - 36, "snail": self.h * 0.8, "jelly": self.h * 0.35}.get(kind, self.rng.uniform(self.h * 0.2, self.h * 0.7))
        made = make_creatures(kind, x, y, self._ids, self.rng, next(self._drums) if kind == "crab" else "")
        self.creatures += made
        return made

    def remove_at(self, x: float, y: float, radius: float = 28.0) -> bool:
        """Take away the nearest animal within `radius` (a whole school goes together)."""
        near = [c for c in self.creatures if math.hypot(c.x - x, c.y - y) <= radius]
        if not near:
            return False
        gone = min(near, key=lambda c: math.hypot(c.x - x, c.y - y))
        group = [c for c in self.creatures if gone.kind == "minnow" and c.kind == "minnow" and math.hypot(c.x - gone.x, c.y - gone.y) < 90] or [gone]
        self.creatures = [c for c in self.creatures if c not in group]
        return True

    def clear(self) -> None:
        self.creatures.clear()
        self.ripples.clear()
        self.score.clear()

    def add_ripple(self, x: float, y: float, now: float, power: float = 1.0, reach: float = REACH, gen: int = 0, source: int = 0) -> Ripple:
        r = Ripple(x, y, now, power, reach, gen, source, hue=self.rng.random())
        self.ripples.append(r)
        return r

    def set_bpm(self, bpm: float, now: float) -> None:
        self.clock.set_bpm(bpm, now)

    def _schedule(self, when: float, key: str, vel: float, who: int) -> None:
        self.score.add(self.clock.quantise(when) if self.snap else when, key, vel, who)

    def update(self, now: float) -> list[tuple[str, float]]:
        """Move everything to `now`. Returns the notes to sound right now: (sound key, loudness)."""
        t0, self.last = self.last, now
        dt = clamp(now - t0, 0.0, 0.1)
        if self.tide:
            for when, bar in self.clock.beats(t0, now):
                self.add_ripple(self.w / 2, self.h / 2, when, 1.0 if bar else 0.45, 420.0 if bar else 190.0, gen=1)
        for c in self.creatures:
            c.step(dt, self.w, self.h, self.rng)
        for r in list(self.ripples):
            for c in list(self.creatures):
                if r.source == c.cid:
                    continue
                s = r.passes(c.x, c.y, t0, now)
                if not s:
                    continue
                reaction = c.react(s, r.x, r.y, now, self.w, self.h)
                if reaction is None:
                    continue
                for delay, key, vel in reaction.hits:
                    self._schedule(now + delay, key, vel, c.cid)
                if reaction.echo and r.gen < MAX_GEN:
                    self.add_ripple(c.x, c.y, now, reaction.echo[0], reaction.echo[1], r.gen + 1, c.cid)
        self.ripples = [r for r in self.ripples if r.alive(now)]
        by_id = {c.cid: c for c in self.creatures}
        out = []
        for key, vel, who in self.score.due(now):
            out.append((key, vel))
            if (c := by_id.get(who)) is not None:
                c.energy, c.squeeze = 1.0, max(c.squeeze, 0.6)
        return out
