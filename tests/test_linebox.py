"""The Links box's height follows its content. No Qt."""
from __future__ import annotations

import unittest

from distill.linebox import MAX_ROWS, box_height, rows_for


class Rows(unittest.TestCase):
    def test_an_empty_box_and_a_single_link_are_one_line(self):
        self.assertEqual(rows_for(""), 1)
        self.assertEqual(rows_for("https://archive.org/details/x"), 1)

    def test_one_more_line_for_each_link(self):
        self.assertEqual(rows_for("a\nb"), 2)
        self.assertEqual(rows_for("a\nb\nc"), 3)

    def test_a_trailing_newline_gives_the_cursor_its_line(self):
        self.assertEqual(rows_for("a\n"), 2)

    def test_it_stops_growing_at_the_limit(self):
        self.assertEqual(rows_for("x\n" * 50), MAX_ROWS)
        self.assertEqual(rows_for("x\n" * 50, max_rows=3), 3)

    def test_the_limit_is_never_below_one_row(self):
        self.assertEqual(rows_for("a\nb", max_rows=0), 1)


class Height(unittest.TestCase):
    def test_the_overhead_plus_the_lines(self):
        self.assertEqual(box_height("", 16, 26), 42)
        self.assertEqual(box_height("a\nb\nc", 16, 26), 74)

    def test_a_full_box_has_a_fixed_height_and_scrolls(self):
        self.assertEqual(box_height("x\n" * 100, 16, 26), 26 + 16 * MAX_ROWS)

    def test_a_line_height_of_nothing_still_gives_a_visible_box(self):
        self.assertGreater(box_height("", 0, 26), 26)


if __name__ == "__main__":
    unittest.main()
