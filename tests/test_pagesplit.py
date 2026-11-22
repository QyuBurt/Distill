import unittest

from distill.ui import pagesplit as ps

P, L = (595.0, 842.0), (1190.0, 842.0)  # a portrait page, and two of them side by side


class SplitTests(unittest.TestCase):
    def test_a_normal_manual_is_left_alone(self):
        sizes = [P] * 12
        self.assertFalse(ps.looks_like_spreads(sizes))
        self.assertEqual(ps.page_units(sizes), [(i, "whole") for i in range(12)])

    def test_scanned_spreads_are_split_but_a_lone_cover_is_not(self):
        sizes = [P] + [L] * 8 + [P]  # front cover, spreads, back cover
        units = ps.page_units(sizes)
        self.assertTrue(ps.looks_like_spreads(sizes))
        self.assertEqual(units[0], (0, "whole"))
        self.assertEqual(units[1:3], [(1, "left"), (1, "right")])
        self.assertEqual(units[-1], (9, "whole"))
        self.assertEqual(len(units), 1 + 16 + 1)
        self.assertAlmostEqual(ps.book_aspect(sizes, units), 0.706, places=2)

    def test_all_wide_pages_are_split(self):
        self.assertEqual(len(ps.page_units([L] * 18)), 36)

    def test_a_few_wide_pages_in_a_portrait_manual_stay_whole(self):
        sizes = [P] * 9 + [L]  # a fold-out map
        self.assertEqual(ps.page_units(sizes), [(i, "whole") for i in range(10)])

    def test_a_panorama_is_not_two_pages(self):
        self.assertFalse(ps.looks_like_spreads([(3000.0, 842.0)] * 4))

    def test_the_player_can_force_it_either_way(self):
        sizes = [P] * 4
        self.assertEqual(ps.page_units(sizes, "off"), [(i, "whole") for i in range(4)])
        self.assertEqual(ps.page_units([L] * 3, "off"), [(i, "whole") for i in range(3)])
        self.assertEqual(len(ps.page_units([(800.0, 700.0)] * 3, "on")), 6)  # (nearly square pages split when asked)

    def test_the_page_a_unit_came_from(self):
        units = ps.page_units([P] + [L] * 3)
        self.assertEqual([ps.first_unit_of(units, n) for n in range(4)], [0, 1, 3, 5])


class CoverTests(unittest.TestCase):
    def test_the_marked_cover_comes_first_and_what_was_before_it_goes_to_the_end(self):
        units = ps.page_units([L] * 3)  # (0 left = back cover, 0 right = front cover, then the inside pages)
        out = ps.with_cover(units, (0, "right"))
        self.assertEqual(out[0], (0, "right"))
        self.assertEqual(out[-1], (0, "left"))
        self.assertEqual(sorted(out), sorted(units))  # nothing lost

    def test_no_cover_or_the_first_page_changes_nothing(self):
        units = ps.page_units([L] * 2)
        self.assertEqual(ps.with_cover(units, None), units)
        self.assertEqual(ps.with_cover(units, (0, "left")), units)

    def test_a_cover_from_a_different_cut_falls_back_to_its_pdf_page(self):
        whole = ps.page_units([L] * 3, "off")  # not split now, but the cover was chosen as 1:right when it was
        self.assertEqual(ps.with_cover(whole, (1, "right"))[0], (1, "whole"))

    def test_it_is_saved_as_text(self):
        self.assertEqual(ps.parse_cover(ps.cover_text((4, "right"))), (4, "right"))
        self.assertIsNone(ps.parse_cover(""))
        self.assertIsNone(ps.parse_cover("x:left"))
        self.assertEqual(ps.cover_text(None), "")


if __name__ == "__main__":
    unittest.main()
