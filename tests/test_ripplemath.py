"""The ripple on a progress bar's edge. No Qt."""
from __future__ import annotations

import math
import unittest

from distill import ripplemath as rm
from distill.ui import fillmath


class Fraction(unittest.TestCase):
    def test_the_usual_range(self):
        self.assertEqual(rm.fraction(0, 0, 1000), 0.0)
        self.assertEqual(rm.fraction(250, 0, 1000), 0.25)
        self.assertEqual(rm.fraction(1000, 0, 1000), 1.0)

    def test_out_of_range_is_held_inside_it(self):
        self.assertEqual(rm.fraction(-5, 0, 100), 0.0)
        self.assertEqual(rm.fraction(500, 0, 100), 1.0)

    def test_an_empty_or_backwards_range_is_empty(self):
        self.assertEqual(rm.fraction(5, 0, 0), 0.0)
        self.assertEqual(rm.fraction(5, 10, 0), 0.0)

    def test_a_range_that_does_not_start_at_zero(self):
        self.assertEqual(rm.fraction(15, 10, 20), 0.5)

    def test_only_a_bar_part_way_ripples(self):
        self.assertFalse(rm.is_rippling(0.0))
        self.assertTrue(rm.is_rippling(0.01))
        self.assertTrue(rm.is_rippling(0.99))
        self.assertFalse(rm.is_rippling(1.0))


class Phase(unittest.TestCase):
    def test_it_advances_by_rate_times_time(self):
        self.assertAlmostEqual(rm.next_phase(0.0, 0.5, rate=2.0), 1.0)

    def test_it_wraps_within_one_turn(self):
        got = rm.next_phase(2 * math.pi - 0.1, 1.0, rate=1.0)
        self.assertTrue(0.0 <= got < 2 * math.pi)
        self.assertAlmostEqual(got, 0.9)

    def test_a_clock_that_runs_backwards_does_not_rewind_it(self):
        self.assertEqual(rm.next_phase(1.0, -3.0), 1.0)


class Edge(unittest.TestCase):
    def test_no_amplitude_is_a_straight_edge(self):
        pts = rm.edge_points(100.0, 0.0, 14.0, 0.0, 1.3, 20.0)
        self.assertTrue(all(x == 100.0 for x, _ in pts))

    def test_the_edge_never_strays_further_than_the_amplitude(self):
        pts = rm.edge_points(100.0, 0.0, 14.0, 3.0, 0.7, 17.0)
        self.assertTrue(all(abs(x - 100.0) <= 3.0 + 1e-9 for x, _ in pts))
        self.assertGreater(max(x for x, _ in pts) - min(x for x, _ in pts), 1.0)  # and it does move

    def test_it_runs_from_top_to_bottom_and_ends_exactly_on_the_bottom(self):
        pts = rm.edge_points(50.0, 2.0, 15.0, 2.0, 0.0, 10.0, step=3.0)
        self.assertEqual(pts[0][1], 2.0)
        self.assertEqual(pts[-1][1], 15.0)
        ys = [y for _, y in pts]
        self.assertEqual(ys, sorted(ys))

    def test_a_flat_or_upside_down_bar_does_not_loop_for_ever(self):
        self.assertEqual(len(rm.edge_points(10.0, 5.0, 5.0, 1.0, 0.0, 9.0)), 1)
        self.assertEqual(len(rm.edge_points(10.0, 8.0, 5.0, 1.0, 0.0, 9.0)), 1)

    def test_a_tiny_step_is_not_allowed_to_make_a_huge_list(self):
        self.assertLess(len(rm.edge_points(0.0, 0.0, 14.0, 1.0, 0.0, 9.0, step=0.0)), 60)

    def test_the_ripple_is_the_brands_own_wave_which_dies_at_both_ends(self):
        self.assertEqual(fillmath.wave_amplitude(0.0, 3.0), 0.0)
        self.assertAlmostEqual(fillmath.wave_amplitude(1.0, 3.0), 0.0, places=6)
        self.assertGreater(fillmath.wave_amplitude(0.5, 3.0), 2.9)

    def test_the_fill_glides_forwards_and_never_overshoots(self):
        shown = 0.0
        for _ in range(600):
            nxt = rm.chase(shown, 0.4, 1 / 60)
            self.assertGreaterEqual(nxt, shown)
            self.assertLessEqual(nxt, 0.4)
            shown = nxt
        self.assertEqual(shown, 0.4)  # and it arrives exactly, so the timer can stop

    def test_the_glide_covers_the_same_ground_whatever_the_frame_rate(self):
        slow, fast = 0.0, 0.0
        for _ in range(30):
            slow = rm.chase(slow, 1.0, 1 / 30)
        for _ in range(120):
            fast = rm.chase(fast, 1.0, 1 / 120)
        self.assertAlmostEqual(slow, fast, places=6)

    def test_a_reset_jumps_back_at_once(self):
        self.assertEqual(rm.chase(0.8, 0.0, 1 / 60), 0.0)
        self.assertEqual(rm.chase(0.8, 0.5, 1 / 60), 0.5)

    def test_the_ripple_drifts_slowly_with_a_wide_wavelength(self):
        self.assertLess(rm.PHASE_RATE, 2.4)
        self.assertGreater(rm.WAVE_LENGTH, 2.0)


if __name__ == "__main__":
    unittest.main()
