"""The Organise review bar's chips. No Qt."""
from __future__ import annotations

import unittest

from distill.orgreview import ACCENT, BAD, MUTED, OK, Chip, is_destination, note_tone, patch_chips, review_chips
from distill.organize import Finding


class ReviewChips(unittest.TestCase):
    def test_nothing_scanned_says_nothing(self):
        self.assertEqual(review_chips([], set()), [])

    def test_the_selection_and_what_it_frees(self):
        fs = [Finding("a", size=2 * 2**30), Finding("b", size=2**30), Finding("c")]
        self.assertEqual(review_chips(fs, {0, 1}), [Chip("2 of 3 selected", ACCENT), Chip("frees 3.00 GB", OK)])

    def test_only_what_is_ticked_counts_towards_what_is_freed(self):
        fs = [Finding("a", size=2**30), Finding("b", size=2**30)]
        self.assertEqual(review_chips(fs, {1})[1], Chip("frees 1.00 GB", OK))

    def test_nothing_ticked_is_muted_and_frees_nothing(self):
        self.assertEqual(review_chips([Finding("a", size=5)], set()), [Chip("0 of 1 selected", MUTED)])

    def test_rows_that_cannot_be_applied_are_counted_whether_or_not_they_are_ticked(self):
        fs = [Finding("a"), Finding("b", problem="locked"), Finding("c", problem="locked")]
        self.assertEqual(review_chips(fs, {0})[-1], Chip("2 can't be applied", BAD))

    def test_a_stale_index_is_ignored_not_a_crash(self):
        self.assertEqual(review_chips([Finding("a")], {0, 7})[0].text, "1 of 1 selected")


class PatchChips(unittest.TestCase):
    def test_none_ticked(self):
        self.assertEqual(patch_chips(0, 0, 0), [Chip("nothing selected", MUTED)])

    def test_patches_games_and_files(self):
        self.assertEqual(patch_chips(3, 2, 4), [Chip("3 patches on 2 games", ACCENT), Chip("4 patched files will be made", OK)])
        self.assertEqual(patch_chips(1, 1, 1)[0].text, "1 patch on 1 game")


class NoteTone(unittest.TestCase):
    def test_what_stays_is_green_and_what_goes_is_red(self):
        self.assertEqual(note_tone("KEEP"), OK)
        self.assertEqual(note_tone("Remove"), BAD)
        self.assertEqual(note_tone("Remove folder"), BAD)
        self.assertEqual(note_tone("Delete permanently"), BAD)
        self.assertEqual(note_tone("Duplicate - removed"), BAD)

    def test_everything_else_keeps_the_ordinary_colour(self):
        for text in ("Ready", "Duplicate", "", "Keeping Game.chd  |  frees 1.00 GB", "Moved", "+2 track / extra file(s)"):
            with self.subTest(text=text):
                self.assertIsNone(note_tone(text))

    def test_it_ignores_case_and_spaces(self):
        self.assertEqual(note_tone("  keep "), OK)


class Destination(unittest.TestCase):
    def test_the_arrow_forms_the_rename_and_sort_pages_write(self):
        self.assertTrue(is_destination("->  Crash Bandicoot (USA).chd"))
        self.assertTrue(is_destination("->  PlayStation 2/"))
        self.assertTrue(is_destination("  ->  X"))

    def test_other_notes_are_not_destinations(self):
        for text in ("", "Ready", "Matched by hash", "Keeping a.chd  |  frees 1.00 GB", "Leftover from an interrupted run", "a -> b"):
            with self.subTest(text=text):
                self.assertFalse(is_destination(text))


if __name__ == "__main__":
    unittest.main()
