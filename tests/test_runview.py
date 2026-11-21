import unittest

from distill.runview import ART, DL, TOOL, Controls, RunFacts, bar_jobs, bar_targets, controls, eta_bits, view_of


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


class TheOneBar(unittest.TestCase):
    def test_nothing_running_nothing_to_show(self):
        self.assertEqual(bar_jobs(None, None), [])

    def test_a_job_is_shown_whichever_page_is_on_screen(self):
        self.assertEqual(bar_jobs(("chd", "Converting..."), None), [(TOOL, "chd", "Converting...")])
        self.assertEqual(bar_jobs(None, ("dl", "Downloading...")), [(DL, "dl", "Downloading...")])

    def test_the_conversion_comes_first_and_the_download_follows(self):
        got = bar_jobs(("chd", "Converting..."), ("sound", "Downloading..."))
        self.assertEqual(got, [(TOOL, "chd", "Converting..."), (DL, "sound", "Downloading...")])


class ShortTime(unittest.TestCase):
    def test_a_download_line_gives_its_speed_and_time_left(self):
        text = "Elapsed: 4m  |  1.20 GB of 9.80 GB  |  3.20 MB/s  |  Est. remaining: ~12m"
        self.assertEqual(eta_bits(text), "3.20 MB/s  |  ~12m left")

    def test_a_conversion_line_gives_its_time_left(self):
        text = "Elapsed: 4m   |   Est. remaining: ~1h 05m   |   Done around Tue 3:40 PM"
        self.assertEqual(eta_bits(text), "~1h 05m left")

    def test_nothing_to_say_while_it_is_still_estimating_or_paused(self):
        self.assertEqual(eta_bits("Elapsed: 1m   |   Estimating time remaining (needs the first finished title)..."), "")
        self.assertEqual(eta_bits("Elapsed: 1m  |  1 GB of 2 GB  |  Paused"), "")
        self.assertEqual(eta_bits(""), "")

    def test_a_speed_alone_when_the_total_is_not_known(self):
        self.assertEqual(eta_bits("Elapsed: 1m  |  500.00 MB of 500.00 MB  |  8.00 MB/s"), "8.00 MB/s")

    def test_less_than_a_minute_left(self):
        self.assertEqual(eta_bits("Est. remaining: ~<1m"), "~<1m left")


class ArtJob(unittest.TestCase):
    def test_the_games_pages_cover_fetch_is_a_job_the_bar_stands_for(self):
        self.assertEqual(bar_jobs(None, None, ("games", "Getting covers and details")), [(ART, "games", "Getting covers and details")])

    def test_a_conversion_and_a_download_come_before_it(self):
        jobs = bar_jobs(("chd", "Converting"), ("dl", "Downloading"), ("games", "Getting covers"))
        self.assertEqual([j[0] for j in jobs], [TOOL, DL, ART])

    def test_without_it_nothing_changed(self):
        self.assertEqual(bar_jobs(None, None), [])
        self.assertEqual(bar_jobs(("chd", "x"), None), [(TOOL, "chd", "x")])
