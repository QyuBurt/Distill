import unittest

from distill.artprogress import ArtProgress, detail_line, fmt_eta, tooltip


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Counting(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.p = ArtProgress(self.clock)

    def test_idle_means_not_running(self):
        self.assertFalse(self.p.running())
        self.assertFalse(self.p.snapshot().running)

    def test_a_game_is_counted_once_however_often_it_is_asked_for(self):
        self.p.add("a", "Sonic")
        self.p.add("a", "Sonic")
        self.assertEqual(self.p.snapshot().total, 1)

    def test_done_and_total_and_what_is_active(self):
        for g in "abc":
            self.p.add(g, g.upper())
        self.p.begin("a", "info")
        self.p.begin("b", "cover")
        snap = self.p.snapshot()
        self.assertEqual((snap.total, snap.done, snap.waiting), (3, 0, 1))
        self.assertEqual(set(snap.active), {("A", "info"), ("B", "cover")})
        self.p.finish("a")
        snap = self.p.snapshot()
        self.assertEqual((snap.done, snap.fraction), (1, 1 / 3))
        self.assertEqual(snap.active, (("B", "cover"),))

    def test_a_new_batch_starts_when_everything_had_settled(self):
        self.p.add("a", "A")
        self.p.finish("a")
        self.assertFalse(self.p.running())
        self.p.add("b", "B")
        snap = self.p.snapshot()
        self.assertEqual((snap.total, snap.done), (1, 0))

    def test_dropped_games_were_never_part_of_the_work(self):
        for g in "abc":
            self.p.add(g, g)
        self.p.drop(["b", "c", "zzz"])
        snap = self.p.snapshot()
        self.assertEqual((snap.total, snap.done), (1, 0))

    def test_finishing_twice_counts_once(self):
        self.p.add("a", "A")
        self.p.add("b", "B")
        self.p.finish("a")
        self.p.finish("a")
        self.assertEqual(self.p.snapshot().done, 1)

    def test_found_counts_only_for_open_games(self):
        self.p.add("a", "A")
        self.p.covers_found("a")
        self.p.matched("a")
        self.p.covers_found("nobody")
        snap = self.p.snapshot()
        self.assertEqual((snap.covers, snap.matched), (1, 1))


class Estimate(unittest.TestCase):
    def test_no_estimate_until_a_few_are_done_and_a_few_seconds_have_passed(self):
        clock = Clock()
        p = ArtProgress(clock)
        for i in range(20):
            p.add(str(i), str(i))
        p.finish("0")
        clock.t += 1
        self.assertIsNone(p.snapshot().eta)
        for i in "12":
            p.finish(i)
        self.assertIsNone(p.snapshot().eta)  # three done, but only a second has passed
        clock.t += 9
        self.assertAlmostEqual(p.snapshot().eta, 10 / 3 * 17, places=3)

    def test_formatting(self):
        self.assertEqual(fmt_eta(3), "~10s")
        self.assertEqual(fmt_eta(45), "~45s")
        self.assertEqual(fmt_eta(180), "~3m")
        self.assertEqual(fmt_eta(3600 + 300), "~1h 5m")
        self.assertEqual(fmt_eta(7200), "~2h")


class Words(unittest.TestCase):
    def snap(self, n=3):
        clock = Clock()
        p = ArtProgress(clock)
        for i in range(10):
            p.add(str(i), f"Game {i}")
        for i in range(n):
            p.begin(str(i), "cover" if i % 2 else "info")
        return p.snapshot()

    def test_the_line_names_what_is_being_grabbed_and_counts_the_rest(self):
        line = detail_line(self.snap(3))
        self.assertIn("0 / 10", line)
        self.assertIn("Game 0 (details), Game 1 (cover) +1", line)

    def test_a_note_replaces_the_estimate(self):
        line = detail_line(self.snap(1), "LaunchBox asked for a pause, resuming in 4s")
        self.assertTrue(line.endswith("resuming in 4s"))

    def test_the_tooltip_lists_every_active_game(self):
        tip = tooltip(self.snap(3))
        for name in ("Game 0", "Game 1", "Game 2"):
            self.assertIn(name, tip)
        self.assertIn("Games page", tip)


if __name__ == "__main__":
    unittest.main()
