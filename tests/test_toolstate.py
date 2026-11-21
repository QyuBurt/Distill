"""The Tools page's states and overview. No Qt."""
from __future__ import annotations

import unittest

from distill.orgreview import ACCENT, BAD, OK, WARN, Chip
from distill.toolstate import CHECKING, MISSING, READY, UPDATING, describe, overview, tool_state


class States(unittest.TestCase):
    def test_the_same_rule_as_the_sidebar_dots_and_the_footer(self):
        self.assertEqual(tool_state(ready=True, pending=False, installing=False), READY)
        self.assertEqual(tool_state(ready=True, pending=True, installing=False), READY)   # a copy is there while it checks for a newer one
        self.assertEqual(tool_state(ready=False, pending=True, installing=False), CHECKING)
        self.assertEqual(tool_state(ready=False, pending=False, installing=False), MISSING)

    def test_a_download_in_progress_wins(self):
        self.assertEqual(tool_state(ready=True, pending=False, installing=True), UPDATING)
        self.assertEqual(tool_state(ready=False, pending=True, installing=True), UPDATING)

    def test_each_state_has_a_word_and_a_colour(self):
        self.assertEqual(describe(READY), ("Ready", OK))
        self.assertEqual(describe(UPDATING), ("Updating", WARN))
        self.assertEqual(describe(MISSING), ("Missing", BAD))
        self.assertEqual(describe("nonsense"), ("Missing", BAD))


class Overview(unittest.TestCase):
    def test_nothing_to_say_with_no_tools(self):
        self.assertEqual(overview({}), [])

    def test_all_ready_is_one_green_tag(self):
        self.assertEqual(overview({"a": READY, "b": READY}), [Chip("2 of 2 ready", OK)])

    def test_busy_and_missing_are_counted_separately(self):
        got = overview({"a": READY, "b": CHECKING, "c": UPDATING, "d": MISSING, "e": MISSING})
        self.assertEqual(got, [Chip("1 of 5 ready", ACCENT), Chip("2 checking or updating", WARN), Chip("2 missing", BAD)])


if __name__ == "__main__":
    unittest.main()
