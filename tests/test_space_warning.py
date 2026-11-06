from __future__ import annotations

import unittest

from distill.models import space_warning

GB = 1 << 30


class SpaceWarningTests(unittest.TestCase):
    def test_roomy_drive_is_quiet(self):
        self.assertEqual(space_warning([10 * GB] * 4, 2, 500 * GB, keeps_size=False, delete=False), "")

    def test_unknown_free_space_or_no_sources_is_quiet(self):
        self.assertEqual(space_warning([GB], 1, None, keeps_size=True, delete=False), "")
        self.assertEqual(space_warning([], 1, GB, keeps_size=True, delete=False), "")

    def test_what_runs_at_once_must_fit(self):
        text = space_warning([30 * GB, 20 * GB, 5 * GB], 2, 30 * GB, keeps_size=True, delete=True)
        self.assertTrue(text.startswith("WARNING"))
        self.assertIn("50.00 GB", text)  # the two largest, as big as their sources

    def test_compressing_assumes_less_than_the_source(self):
        # 2 x 10 GB sources -> 16 GB at 80%: fits in 17 GB, not in 15 GB
        self.assertEqual(space_warning([10 * GB] * 2, 2, 17 * GB, keeps_size=False, delete=False), "")
        self.assertIn("WARNING", space_warning([10 * GB] * 2, 2, 15 * GB, keeps_size=False, delete=False))

    def test_whole_batch_note_only_for_unpacking_that_keeps_originals(self):
        sizes = [10 * GB] * 5
        self.assertTrue(space_warning(sizes, 1, 30 * GB, keeps_size=True, delete=False).startswith("Note"))
        self.assertEqual(space_warning(sizes, 1, 30 * GB, keeps_size=True, delete=True), "")
        self.assertEqual(space_warning(sizes, 1, 30 * GB, keeps_size=False, delete=False), "")

    def test_zero_workers_is_treated_as_one(self):
        self.assertIn("WARNING", space_warning([10 * GB], 0, GB, keeps_size=True, delete=False))


if __name__ == "__main__":
    unittest.main()
