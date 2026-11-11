"""Holding and turning the player's disc: the arithmetic (distill/ui/scratchmath.py) and the bundled record sounds. No Qt needed."""
from __future__ import annotations

import re
import tempfile
import unittest
import wave
from pathlib import Path

import make_vinyl_sounds
from distill.ui import scratchmath as sm

ROOT = Path(__file__).resolve().parent.parent
SOUNDS = ROOT / "distill" / "assets" / "sounds"
NAMES = ("vinyl-stop", "vinyl-start", "scratch-fwd", "scratch-back")


class AngleTests(unittest.TestCase):
    def test_pointer_angle_grows_clockwise_on_screen(self):
        self.assertAlmostEqual(sm.pointer_angle(1, 0), 0)
        self.assertAlmostEqual(sm.pointer_angle(0, 1), 90)  # y points down: below the centre is a quarter turn clockwise
        self.assertAlmostEqual(sm.pointer_angle(-1, 0), 180)
        self.assertAlmostEqual(sm.pointer_angle(0, -1), -90)

    def test_a_turn_takes_the_short_way_round_the_seam(self):
        self.assertAlmostEqual(sm.turn(0, 90), 90)
        self.assertAlmostEqual(sm.turn(90, 0), -90)
        self.assertAlmostEqual(sm.turn(170, -170), 20)  # across 180 / -180 is a small step, not a jump of 340
        self.assertAlmostEqual(sm.turn(-170, 170), -20)
        self.assertAlmostEqual(sm.turn(10, 10), 0)

    def test_going_round_once_adds_up_to_a_full_turn(self):
        angle, total = 0.0, 0.0
        for _ in range(36):
            new = sm.pointer_angle(*_at(angle + 10))
            total += sm.turn(angle, new)
            angle = new
        self.assertAlmostEqual(total, 360, places=6)


def _at(deg: float) -> tuple[float, float]:
    import math
    return math.cos(math.radians(deg)), math.sin(math.radians(deg))


class ScrubTests(unittest.TestCase):
    def test_turning_at_the_playing_speed_keeps_pace_with_the_track(self):
        self.assertEqual(sm.scrub_ms(10_000, sm.SPIN, 60_000), 11_000)  # one second's turn is one second of music
        self.assertEqual(sm.scrub_ms(10_000, -sm.SPIN * 2, 60_000), 8_000)
        self.assertEqual(sm.scrub_ms(10_000, 0, 60_000), 10_000)

    def test_it_stays_inside_the_track_and_never_runs_into_the_end(self):
        self.assertEqual(sm.scrub_ms(1_000, -10_000, 60_000), 0)
        self.assertEqual(sm.scrub_ms(50_000, 10_000, 60_000), 60_000 - sm.END_GUARD_MS)  # the end would skip to the next track
        self.assertEqual(sm.scrub_ms(500, 99, 0), 500)  # length not known yet: stay put
        self.assertEqual(sm.scrub_ms(-5, 0, 0), 0)

    def test_the_speed_matches_the_disc(self):
        text = (ROOT / "distill" / "ui" / "now_playing.py").read_text(encoding="utf-8")
        self.assertEqual(float(re.search(r"SPIN = ([\d.]+)", text).group(1)), sm.SPIN)  # CdArt.SPIN: what the disc turns at while playing


class GrainTests(unittest.TestCase):
    def test_a_resting_hand_makes_no_sound_and_a_flick_is_loudest(self):
        self.assertEqual(sm.grain_gain(0), 0.0)
        self.assertEqual(sm.grain_gain(sm.SLOW - 1), 0.0)
        self.assertGreater(sm.grain_gain(sm.SLOW + 1), 0.0)
        self.assertAlmostEqual(sm.grain_gain(sm.FAST), 1.0)
        self.assertAlmostEqual(sm.grain_gain(sm.FAST * 10), 1.0)  # never louder than the volume
        self.assertEqual(sm.grain_gain(-300), sm.grain_gain(300))  # either way round
        speeds = range(0, 700, 20)
        gains = [sm.grain_gain(s) for s in speeds]
        self.assertEqual(gains, sorted(gains))

    def test_grains_come_quicker_the_faster_it_turns(self):
        self.assertEqual(sm.grain_gap_ms(0), 110)
        self.assertEqual(sm.grain_gap_ms(sm.FAST), 50)
        self.assertEqual(sm.grain_gap_ms(-sm.FAST * 5), 50)
        gaps = [sm.grain_gap_ms(s) for s in range(0, 700, 20)]
        self.assertEqual(gaps, sorted(gaps, reverse=True))


class SoundFileTests(unittest.TestCase):
    def test_the_record_sounds_are_there_small_and_not_clipped(self):
        total = 0
        for name in NAMES:
            path = SOUNDS / f"{name}.wav"
            self.assertTrue(path.exists(), name)
            total += path.stat().st_size
            with wave.open(str(path)) as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth()), (1, 2), name)
                seconds = w.getnframes() / w.getframerate()
                samples = memoryview(w.readframes(w.getnframes())).cast("h")
            self.assertTrue(0.05 < seconds < 1.0, f"{name} is {seconds:.2f}s")
            self.assertLess(max(abs(s) for s in samples), 32767, f"{name} clips")
            self.assertGreater(max(abs(s) for s in samples), 8000, f"{name} is too quiet to hear")
            if name.startswith("scratch"):
                self.assertLess(abs(samples[-1]), 300, f"{name} ends on a click")
        self.assertLess(total, 100_000)  # bundled with every build: keep them tiny

    def test_the_bundled_sounds_can_be_rebuilt_exactly(self):
        with tempfile.TemporaryDirectory() as td:
            old = make_vinyl_sounds.OUT
            make_vinyl_sounds.OUT = Path(td)
            try:
                make_vinyl_sounds.main()
            finally:
                make_vinyl_sounds.OUT = old
            for name in NAMES:
                self.assertEqual((Path(td) / f"{name}.wav").read_bytes(), (SOUNDS / f"{name}.wav").read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
