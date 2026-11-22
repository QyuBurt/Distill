import unittest

from distill import gamemedia as mm
from distill import mediashape as ms


class SizeTableTests(unittest.TestCase):
    def test_every_disc_and_cart_system_has_a_matching_entry(self):
        for s in mm.DISC_SYSTEMS:
            self.assertIn(ms.known_spec(s).shape, ("disc", "umd"), s)
        for s in mm.CART_SYSTEMS:
            self.assertEqual(ms.known_spec(s).shape, "cart", s)

    def test_disc_sizes_follow_the_real_cases(self):
        sc = lambda s: ms.spec_for(s, "disc").scale  # noqa: E731
        self.assertEqual(sc("PlayStation"), sc("Dreamcast"))  # jewel cases: the disc fills the front
        self.assertLess(sc("PlayStation 2"), sc("PlayStation"))  # same 120 mm disc, taller case
        self.assertLess(sc("GameCube"), sc("PlayStation 2"))  # 80 mm disc in a DVD case
        self.assertGreater(sc("PlayStation 3"), sc("PlayStation 2"))  # a Blu-ray case is shorter than a DVD case
        self.assertLess(ms.spec_for("Switch", "cart").scale, ms.spec_for("NES", "cart").scale)

    def test_psp_is_a_umd_and_unknown_systems_keep_the_old_look(self):
        self.assertEqual(ms.spec_for("PSP", "disc").shape, "umd")
        self.assertEqual(ms.spec_for("Elsewhere", "disc"), ms.MediaSpec("disc", ms.DEFAULT_DISC))
        self.assertEqual(ms.spec_for("Elsewhere", "cart"), ms.MediaSpec("cart", ms.DEFAULT_CART))


class FindDiscTests(unittest.TestCase):
    W = H = 800

    def test_finds_the_disc_in_a_shell_ignoring_printed_logos(self):
        def dark(x, y):
            return ((x - 420) ** 2 + (y - 418) ** 2 <= 355 ** 2) or (50 < x < 170 and 690 < y < 720) or (690 < x < 760 and 690 < y < 740)

        cx, cy, r = ms.find_disc(self.W, self.H, dark)
        self.assertAlmostEqual(cx, 420, delta=6)
        self.assertAlmostEqual(cy, 418, delta=6)
        self.assertAlmostEqual(r, 355, delta=8)

    def test_a_bare_disc_or_nothing_gives_none(self):
        self.assertIsNone(ms.find_disc(self.W, self.H, lambda x, y: (x - 400) ** 2 + (y - 400) ** 2 <= 400 ** 2))
        self.assertIsNone(ms.find_disc(self.W, self.H, lambda x, y: False))
        self.assertIsNone(ms.find_disc(self.W, self.H, lambda x, y: 100 < x < 700 and 300 < y < 400))  # a bar is not round


if __name__ == "__main__":
    unittest.main()
