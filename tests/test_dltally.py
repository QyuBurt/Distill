"""The Files & Torrents chips and the 'After downloads' line. No Qt."""
from __future__ import annotations

import unittest

from distill import afteractions
from distill.dltally import ACTIVE, DONE, FAILED, IDLE, STOPPED, after_summary, classify, list_chips
from distill.orgreview import ACCENT, BAD, MUTED, OK, Chip


class Classify(unittest.TestCase):
    def test_the_statuses_the_engines_really_write(self):
        for text, group in (("", IDLE), ("Done", DONE), ("Already downloaded", DONE), ("Failed: HTTP 404", FAILED),
                            ("Stopped", STOPPED), ("Stopping...", STOPPED), ("45.2%", ACTIVE),
                            ("Looking for peers...", ACTIVE), ("Waiting for the swarm...", ACTIVE),
                            ("Checking TorBox cache...", ACTIVE), ("Paused - VPN dropped", ACTIVE)):
            with self.subTest(text=text):
                self.assertEqual(classify(text), group)


class ListChips(unittest.TestCase):
    def test_an_empty_list_says_nothing(self):
        self.assertEqual(list_chips([], set(), set(), []), [])
        self.assertEqual(list_chips([5], set(), {0}, [""]), [])  # everything removed

    def test_the_selection_with_its_size(self):
        got = list_chips([2**30, 2**30, 10], {0, 1}, set(), ["", "", ""])
        self.assertEqual(got, [Chip("2 of 3 selected \u00b7 2.00 GB", ACCENT)])

    def test_a_size_not_yet_known_is_marked_open_ended(self):
        self.assertEqual(list_chips([2**20, None], {0, 1}, set(), ["", ""])[0].text, "2 of 2 selected \u00b7 1.00 MB+")

    def test_nothing_ticked_is_muted_and_has_no_size(self):
        self.assertEqual(list_chips([5], set(), set(), [""]), [Chip("0 of 1 selected", MUTED)])

    def test_what_is_going_done_and_failed(self):
        got = list_chips([1, 1, 1, 1, 1], {0}, set(), ["12.0%", "Done", "Already downloaded", "Failed: x", ""])
        self.assertEqual([c.text for c in got[1:]], ["1 downloading", "2 done", "1 failed"])
        self.assertEqual([c.tone for c in got[1:]], [ACCENT, OK, BAD])

    def test_removed_rows_are_not_counted_anywhere(self):
        got = list_chips([1, 1], {0, 1}, {1}, ["Done", "Failed: x"])
        self.assertEqual([c.text for c in got], ["1 of 1 selected \u00b7 1 B", "1 done"])

    def test_a_stale_tick_is_ignored(self):
        self.assertEqual(list_chips([1], {0, 9}, set(), [""])[0].text, "1 of 1 selected \u00b7 1 B")


class AfterLine(unittest.TestCase):
    def test_by_default_nothing_happens(self):
        self.assertEqual(after_summary("Nothing more", False, "", "Nothing", False), ("Nothing happens when they finish", False))

    def test_every_option_is_named(self):
        text, armed = after_summary("Unpack archives, then delete them", True, "sync.bat", "Shut down", True)
        self.assertEqual(text, "Unpack archives, then delete them, open Sort by System, run a command, then shut down the computer, resume on startup")
        self.assertTrue(armed)

    def test_only_a_sleep_or_shutdown_is_a_warning(self):
        self.assertFalse(after_summary("Unpack archives", False, "  ", "Nothing", False)[1])
        self.assertTrue(after_summary("", False, "", "Sleep", False)[1])

    def test_it_reads_the_boxes_own_words(self):
        for label in afteractions.POWER_LABELS:
            after_summary("Nothing more", False, "", label, False)  # none of the labels breaks it
        self.assertFalse(after_summary("", False, "", afteractions.POWER_LABELS[0], False)[1])


if __name__ == "__main__":
    unittest.main()
