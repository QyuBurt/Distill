import unittest

from distill.runview import DL, TOOL, Controls, RunFacts, bar_targets, controls, view_of


class Views(unittest.TestCase):
    def test_only_the_download_pages_drive_the_download_run(self):
        self.assertEqual(view_of("dl"), DL)
        for mode in ("chd", "rvz", "switch", "org", "games", "recomps", "emus"):
            self.assertEqual(view_of(mode), TOOL)


class ControlRules(unittest.TestCase):
    def test_idle_the_start_button_is_live_and_the_others_are_not(self):
        for view in (TOOL, DL):
            self.assertEqual(controls(view, RunFacts()), Controls(True, False, False, "Pause"))

    def test_a_download_does_not_touch_the_conversion_pages_buttons(self):
        f = RunFacts(dl=True, dl_paused=True)
        self.assertEqual(controls(TOOL, f), Controls(True, False, False, "Pause"))  # a conversion can still be started
        self.assertEqual(controls(DL, f), Controls(False, True, True, "Resume"))

    def test_a_conversion_does_not_touch_the_download_pages_buttons(self):
        f = RunFacts(tool=True)
        self.assertEqual(controls(DL, f), Controls(True, False, False, "Pause"))  # a download can still be started
        self.assertEqual(controls(TOOL, f), Controls(False, True, True, "Pause"))

    def test_both_at_once_each_page_shows_its_own(self):
        f = RunFacts(tool=True, dl=True, tool_paused=True)
        self.assertEqual(controls(TOOL, f), Controls(False, True, True, "Resume"))
        self.assertEqual(controls(DL, f), Controls(False, True, True, "Pause"))

    def test_stopping_turns_pause_and_stop_off_until_it_is_over(self):
        self.assertEqual(controls(DL, RunFacts(dl=True, dl_stopping=True)), Controls(False, False, False, "Pause"))
        self.assertEqual(controls(TOOL, RunFacts(tool=True, tool_stopping=True)), Controls(False, False, False, "Pause"))

    def test_organise_blocks_start_on_every_page_and_owns_stop(self):
        for view in (TOOL, DL):
            self.assertEqual(controls(view, RunFacts(org=True)), Controls(False, False, True, "Pause"))
            self.assertEqual(controls(view, RunFacts(org=True, org_stopping=True)), Controls(False, False, False, "Pause"))

    def test_start_waits_for_the_tool_checks_on_both_sides(self):
        for view in (TOOL, DL):
            self.assertFalse(controls(view, RunFacts(pending_tools=True)).start)

    def test_a_paused_flag_means_nothing_when_nothing_is_running(self):
        self.assertEqual(controls(DL, RunFacts(dl_paused=True)).pause_text, "Pause")


class BottomBar(unittest.TestCase):
    def test_nothing_running_nothing_to_show(self):
        self.assertEqual(bar_targets(None, None, "chd"), [])

    def test_a_job_on_the_page_on_screen_is_not_away(self):
        self.assertEqual(bar_targets(("chd", "Converting..."), None, "chd"), [])
        self.assertEqual(bar_targets(None, ("dl", "Downloading..."), "dl"), [])

    def test_the_conversion_comes_first_and_the_download_follows(self):
        got = bar_targets(("chd", "Converting..."), ("dl", "Downloading..."), "games")
        self.assertEqual(got, [(TOOL, "chd", "Converting..."), (DL, "dl", "Downloading...")])

    def test_each_page_only_hides_its_own_job_from_the_bar(self):
        both = (("chd", "Converting..."), ("dl", "Downloading..."))
        self.assertEqual(bar_targets(*both, "chd"), [(DL, "dl", "Downloading...")])
        self.assertEqual(bar_targets(*both, "dl"), [(TOOL, "chd", "Converting...")])


if __name__ == "__main__":
    unittest.main()
