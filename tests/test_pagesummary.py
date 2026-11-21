"""The one-line summaries shown on a folded card. No Qt."""
from __future__ import annotations

import unittest

from distill.pagesummary import FOLD_TITLES, LAYOUT, SEP, calm_pending, changed_options, options_summary, perf_summary, workflow_summary


class Workflow(unittest.TestCase):
    def test_the_usual_setup(self):
        self.assertEqual(workflow_summary(True, True, False), (f"Unpack archives{SEP}Verify results{SEP}Keeps originals", False))

    def test_deleting_the_originals_is_a_warning(self):
        text, warn = workflow_summary(False, True, True)
        self.assertEqual(text, f"Verify results{SEP}Deletes originals")
        self.assertTrue(warn)

    def test_a_page_with_no_unpack_box(self):
        self.assertEqual(workflow_summary(None, False, False)[0], "Keeps originals")

    def test_a_page_that_always_checks_says_so_whatever_the_box_holds(self):
        self.assertEqual(workflow_summary(True, False, False, always_verified=True)[0],
                         f"Unpack archives{SEP}Checks each result{SEP}Keeps originals")


OPTIONS = [("a", "check", "Delete empty folders", True), ("b", "switch", "Mode", False, "Copy", "Move", ""),
           ("c", "folder", "Output folder", ""), ("d", "file", "Patch list", "x.txt", "", "")]


class Options(unittest.TestCase):
    def values(self, **kw):
        base = {"a": lambda: True, "b": lambda: False, "c": lambda: "", "d": lambda: "x.txt"}
        base.update({k: (lambda v=v: v) for k, v in kw.items()})
        return base

    def test_nothing_changed_reads_defaults(self):
        self.assertEqual(options_summary(OPTIONS, self.values()), ("Defaults", False))

    def test_which_ones_changed(self):
        self.assertEqual(changed_options(OPTIONS, self.values(a=False, c="D:/out")), ["Delete empty folders", "Output folder"])
        self.assertEqual(options_summary(OPTIONS, self.values(a=False, c="D:/out"))[0], "2 changed from the defaults")

    def test_text_is_compared_without_stray_spaces(self):
        self.assertEqual(changed_options(OPTIONS, self.values(c="  ")), [])

    def test_a_switch_is_compared_as_on_or_off(self):
        self.assertEqual(changed_options(OPTIONS, self.values(b=True)), ["Mode"])

    def test_an_option_with_no_control_or_a_dead_one_is_skipped(self):
        def gone():
            raise RuntimeError("Internal C++ object already deleted")
        self.assertEqual(changed_options(OPTIONS, {"a": gone}), [])


class Performance(unittest.TestCase):
    def test_the_usual_setup(self):
        self.assertEqual(perf_summary("High", 12, 16, "Auto"), (f"High{SEP}12 of 16 threads{SEP}Disk: Auto", False))

    def test_an_own_temp_folder_is_mentioned_but_not_its_path(self):
        text, _ = perf_summary("All", 16, 16, "SSD", "D:/ram")
        self.assertTrue(text.endswith(f"Disk: SSD{SEP}Own temp folder"))
        self.assertNotIn("ram", text)

    def test_a_blank_temp_folder_is_not_one(self):
        self.assertNotIn("temp", perf_summary("All", 4, 4, "Auto", "   ")[0])

    def test_it_copes_with_a_control_that_has_no_choice_yet(self):
        self.assertEqual(perf_summary("", None, 8, "")[0], "Default")
        self.assertEqual(perf_summary("Low", None, 8, "HDD")[0], f"Low{SEP}Disk: HDD")


class OneTimeFold(unittest.TestCase):
    def test_a_settings_file_that_has_not_seen_the_layout_gets_it(self):
        self.assertTrue(calm_pending({}))  # a fresh install
        self.assertTrue(calm_pending({"cards": {"chd/Workflow": True}}))  # an existing one, with its cards as it left them

    def test_once_it_is_marked_it_is_left_alone(self):
        self.assertFalse(calm_pending({"layout": LAYOUT}))

    def test_a_marker_from_some_other_layout_counts_as_not_seen(self):
        self.assertTrue(calm_pending({"layout": "something-else"}))

    def test_only_the_two_cards_that_have_summaries_are_folded(self):
        self.assertEqual(set(FOLD_TITLES), {"Workflow", "Options"})


if __name__ == "__main__":
    unittest.main()
