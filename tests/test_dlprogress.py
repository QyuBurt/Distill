"""The thin progress line under a Files & Torrents row. No Qt."""
from __future__ import annotations

import unittest

from distill.dlprogress import row_look


class RowLook(unittest.TestCase):
    def test_a_download_with_a_known_size_gives_its_percentage(self):
        self.assertEqual(row_look("45%  12.00 MB / 26.00 MB"), ("active", 0.45))
        self.assertEqual(row_look("0%  0 B / 1.00 GB"), ("active", 0.0))

    def test_over_a_hundred_is_held_at_full(self):
        self.assertEqual(row_look("104%  x"), ("active", 1.0))

    def test_an_unknown_size_has_no_line(self):
        self.assertEqual(row_look("12.00 MB"), ("none", None))
        self.assertEqual(row_look("12.00 MB / 40.00 MB"), ("none", None))  # the yt-dlp form: no percentage to read

    def test_finished_and_failed_rows_get_a_whole_line_in_their_own_colour(self):
        self.assertEqual(row_look("Done"), ("done", 1.0))
        self.assertEqual(row_look("Already downloaded"), ("done", 1.0))
        self.assertEqual(row_look("Failed: HTTP 404"), ("failed", 1.0))

    def test_waiting_paused_stopped_and_blank_have_none(self):
        for text in ("", "Looking for peers...", "Waiting for the swarm...", "Checking TorBox cache...", "Paused", "Stopped",
                     "Stopping...", "Retrying (1/3): timeout", "Paused - VPN dropped"):
            with self.subTest(text=text):
                self.assertEqual(row_look(text), ("none", None))

    def test_a_percent_sign_in_the_middle_of_a_message_is_not_progress(self):
        self.assertEqual(row_look("Checking existing files... 40%")[0], "none")


if __name__ == "__main__":
    unittest.main()
