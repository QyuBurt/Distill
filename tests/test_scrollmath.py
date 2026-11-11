"""The wheel glide's arithmetic (distill/ui/scrollmath.py) needs no Qt, so these run everywhere."""
from __future__ import annotations

import math
import unittest

from distill.ui import scrollmath as sm


def glide(target: float, hz: float, seconds: float = 1.0) -> list[float]:
    pos = vel = 0.0
    out = []
    for _ in range(int(hz * seconds)):
        pos, vel = sm.damp_step(pos, vel, target, 1.0 / hz)
        out.append(pos)
    return out


class DampStepTests(unittest.TestCase):
    def test_it_starts_gently_and_never_overshoots(self):
        for hz in (60, 144, 240):
            path = glide(350, hz)
            steps = [b - a for a, b in zip([0.0] + path, path)]
            self.assertLess(steps[0], 0.4 * max(steps), "no leap on the first frame: it eases in")
            self.assertLessEqual(max(path), 350.0 + 1e-6)
            peak = steps.index(max(steps))
            self.assertTrue(all(b >= a - 1e-9 for a, b in zip(steps[:peak], steps[1:peak + 1])), "speeds up smoothly")
            self.assertTrue(all(b <= a + 1e-9 for a, b in zip(steps[peak:], steps[peak + 1:])), "then slows smoothly")

    def test_the_old_chase_leapt_on_its_first_frame_and_this_does_not(self):
        old_first = 350 * (1 - math.exp(-(1 / 60) * 1000 / 80))  # the previous exponential chase at 60 Hz
        new_first = glide(350, 60)[0]
        self.assertGreater(old_first, 5 * new_first)

    def test_one_long_frame_lands_where_many_short_ones_do(self):
        pos = vel = 0.0
        for _ in range(100):
            pos, vel = sm.damp_step(pos, vel, 100.0, 0.001)
        one = sm.damp_step(0.0, 0.0, 100.0, 0.1)
        self.assertAlmostEqual(one[0], pos, places=6)
        self.assertAlmostEqual(one[1], vel, places=6)

    def test_a_target_that_moves_mid_glide_keeps_the_speed(self):
        pos = vel = 0.0
        for _ in range(10):
            pos, vel = sm.damp_step(pos, vel, 70.0, 1 / 144)
        _, after = sm.damp_step(pos, vel, 140.0, 1 / 144)
        self.assertGreater(after, 0.8 * vel, "a second notch speeds it up, it does not restart it")

    def test_it_comes_to_rest(self):
        pos = vel = 0.0
        for frame in range(3 * 144):
            pos, vel = sm.damp_step(pos, vel, 70.0, 1 / 144)
            if sm.at_rest(pos, vel, 70.0):
                break
        self.assertLess(frame / 144, 0.8)


class WheelGainTests(unittest.TestCase):
    def test_first_notch_and_slow_turns_are_plain(self):
        self.assertEqual(sm.wheel_gain(1, None), 1.0)
        self.assertEqual(sm.wheel_gain(1, 0.5), 1.0)
        self.assertEqual(sm.wheel_gain(0, 0.01), 1.0)

    def test_a_hard_spin_is_capped(self):
        self.assertEqual(sm.wheel_gain(1, 0.001), sm.ACCEL_MAX)
        self.assertEqual(sm.wheel_gain(-1, 0.005), sm.ACCEL_MAX)

    def test_it_rises_smoothly_with_the_rate(self):
        gains = [sm.wheel_gain(1, g) for g in (0.2, 0.12, 0.08, 0.04, 0.02, 0.01)]
        self.assertEqual(gains, sorted(gains))
        self.assertGreater(gains[-1], gains[0])

    def test_a_hi_res_wheel_is_not_mistaken_for_a_fast_one(self):
        # 15 units (an eighth of a notch) every 16 ms is ~8 notches a second: a gentle turn
        self.assertLess(sm.wheel_gain(15 / 120, 0.016), 1.1)


class SpringEaseTests(unittest.TestCase):
    def test_it_starts_at_zero_and_lands_exactly_on_one(self):
        self.assertEqual(sm.spring_ease(0.0), 0.0)
        self.assertEqual(sm.spring_ease(1.0), 1.0)
        self.assertEqual(sm.spring_ease(2.0), 1.0)

    def test_it_gives_a_little_past_home_once_and_settles(self):
        vals = [sm.spring_ease(i / 100) for i in range(101)]
        peak = max(vals)
        self.assertGreater(peak, 1.03)
        self.assertLess(peak, 1.15, "a small give: a big one shows a gap on the far side")
        crossings = sum(1 for a, b in zip(vals, vals[1:]) if (a - 1.0) * (b - 1.0) < 0)
        self.assertLessEqual(crossings, 3)
        self.assertLess(abs(vals[-2] - 1.0), 0.01)


if __name__ == "__main__":
    unittest.main()
