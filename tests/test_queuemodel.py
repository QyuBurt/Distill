"""The Queue view's data: task lines read the way the log writes them, and the counts above the list. No Qt."""
from __future__ import annotations

import unittest
from unittest import mock

from distill.models import Config
from distill.queuemodel import (ACTIVE, DONE, PHASE_OF, PROBLEM, SKIPPED, WAITING, RunQueue, Tally, matches,
                                parse_task)
from tests.helpers import FAKE_CHDMAN, POSIX_ONLY, TempDirTest, make_tool, run_batch


class ParsingTests(unittest.TestCase):
    def test_a_running_line_gives_its_state_percentage_and_name(self):
        t = parse_task("[ CONVERTING ] (12.5%) Crash Bandicoot (USA)")
        self.assertEqual((t.state, t.phase, t.pct, t.name), ("CONVERTING", ACTIVE, 12.5, "Crash Bandicoot (USA)"))

    def test_a_running_line_without_a_percentage_has_none(self):
        t = parse_task("[ CONVERTING ] Mario Kart Wii")
        self.assertEqual((t.phase, t.pct, t.name), (ACTIVE, None, "Mario Kart Wii"))

    def test_a_step_in_brackets_is_what_the_row_is_doing_not_part_of_the_name(self):
        t = parse_task("[ VERIFYING  ] (hashing RVZ) Mario Kart Wii")
        self.assertEqual((t.step, t.name, t.pct), ("hashing RVZ", "Mario Kart Wii", None))
        t = parse_task("[ EXTRACTING ] (merging update 1/2) Bloodborne")
        self.assertEqual((t.step, t.name), ("merging update 1/2", "Bloodborne"))

    def test_a_percentage_over_a_hundred_is_held_at_a_hundred(self):
        self.assertEqual(parse_task("[ CONVERTING ] (104.0%) X").pct, 100.0)

    def test_a_failure_splits_its_reason_off_the_name_even_when_the_name_has_brackets(self):
        t = parse_task("[   ERROR    ] Game (USA) (Verification failed - exit code 1)")
        self.assertEqual((t.phase, t.name, t.note), (PROBLEM, "Game (USA)", "Verification failed - exit code 1"))

    def test_skipped_flagged_and_stalled_keep_their_reasons(self):
        self.assertEqual(parse_task("[  SKIPPED   ] Game (CHD exists)").note, "CHD exists")
        self.assertEqual(parse_task("[  FLAGGED   ] App (not an unencrypted PS5 debug package - skipped)").phase, PROBLEM)
        t = parse_task("[  STALLED   ] Big Game (converting made no progress - skipped)")
        self.assertEqual((t.phase, t.name), (PROBLEM, "Big Game"))

    def test_a_finished_game_keeps_its_brackets_as_part_of_the_name(self):
        t = parse_task("[  VERIFIED  ] Sonic (USA) (En,Fr)")
        self.assertEqual((t.phase, t.name, t.note), (DONE, "Sonic (USA) (En,Fr)", ""))

    def test_a_finished_game_carries_what_it_saved(self):
        t = parse_task("[  VERIFIED  ] Good - 71% smaller (4.20 GB -> 1.22 GB)")
        self.assertEqual((t.name, t.result, t.phase), ("Good", "71% smaller", DONE))
        self.assertEqual(t.before, round(4.20 * 2**30))
        self.assertEqual(t.saved, round(4.20 * 2**30) - round(1.22 * 2**30))

    def test_a_game_whose_own_name_has_a_dash_is_not_cut_short(self):
        t = parse_task("[ CONVERTED  ] Game Name - NPUB30001 - 12% smaller (1,024.00 MB -> 900.00 MB)")
        self.assertEqual((t.name, t.result), ("Game Name - NPUB30001", "12% smaller"))
        self.assertEqual(t.before, 2**30)

    def test_a_game_that_came_out_larger_saved_nothing(self):
        t = parse_task("[  VERIFIED  ] Tiny - 5% larger (100.00 KB -> 105.00 KB)")
        self.assertEqual((t.result, t.saved), ("5% larger", 0))

    def test_the_pages_that_unpack_report_what_they_made_not_a_saving(self):
        t = parse_task("[  VERIFIED  ] Bloodborne - extracted 24.50 GB")
        self.assertEqual((t.name, t.result, t.saved), ("Bloodborne", "extracted 24.50 GB", 0))
        self.assertEqual(t.after, round(24.5 * 2**30))

    def test_a_game_that_already_had_an_output_says_so(self):
        t = parse_task("[  VERIFIED  ] Sonic (USA) (CHD exists)")
        self.assertEqual((t.phase, t.name, t.note), (DONE, "Sonic (USA)", "CHD exists"))

    def test_waiting_for_disk_is_a_waiting_row_with_the_reason(self):
        t = parse_task("[  QUEUED    ] Game (waiting for disk)")
        self.assertEqual((t.phase, t.name, t.note), (WAITING, "Game", "waiting for disk"))

    def test_a_game_inside_an_archive_is_a_child(self):
        t = parse_task("[  WAITING   ] \u2514\u2500 Disc 1")
        self.assertEqual((t.child, t.name, t.phase), (True, "Disc 1", WAITING))

    def test_the_switch_pages_put_the_result_on_the_lines_under_the_name(self):
        t = parse_task("[    OK      ] Game\n      -> Game.nsz, Game [update].nsz")
        self.assertEqual((t.phase, t.name, t.note), (DONE, "Game", "Game.nsz, Game [update].nsz"))
        t = parse_task("[  FAILED    ] Game\n      no keys found")
        self.assertEqual((t.phase, t.note), (PROBLEM, "no keys found"))

    def test_a_line_with_no_state_is_waiting_and_an_unknown_state_is_running(self):
        self.assertEqual(parse_task("Just a name").phase, WAITING)
        self.assertEqual(parse_task("[ BAKING     ] Cake").phase, ACTIVE)

    def test_nothing_at_all_does_not_crash(self):
        self.assertEqual(parse_task("").name, "")


class QueueTests(unittest.TestCase):
    def test_rows_come_in_the_order_they_were_announced(self):
        q = RunQueue()
        self.assertEqual([q.new(1, "[  WAITING   ] B"), q.new(2, "[  WAITING   ] A")], [0, 1])
        self.assertEqual(q.task_at(0).name, "B")

    def test_an_update_rewrites_its_row_and_an_unknown_id_is_ignored(self):
        q = RunQueue()
        q.new(7, "[  WAITING   ] Game")
        self.assertEqual(q.update(7, "[ CONVERTING ] (50.0%) Game"), 0)
        self.assertEqual((q.task_at(0).phase, q.task_at(0).pct), (ACTIVE, 50.0))
        self.assertIsNone(q.update(99, "[ CONVERTING ] nope"))
        self.assertEqual(len(q), 1)

    def test_an_id_announced_twice_in_one_run_is_still_one_row(self):
        q = RunQueue()
        q.new(1, "[  WAITING   ] A")
        q.new(1, "[ CONVERTING ] A")
        self.assertEqual(len(q), 1)

    def test_an_update_that_drops_the_archive_mark_keeps_the_game_a_child(self):
        q = RunQueue()
        q.new(1, "[  WAITING   ] Pack")
        q.new(2, "[  WAITING   ] \u2514\u2500 Disc")
        q.update(2, "[ CONVERTING ] (10.0%) Disc")
        self.assertTrue(q.task_at(1).child)

    def test_the_tally_counts_games_not_the_archives_they_came_from(self):
        q = RunQueue()
        q.new(1, "[  UNZIPPED  ] Pack (2 games)")
        q.new(2, "[  VERIFIED  ] \u2514\u2500 Disc 1")
        q.new(3, "[ CONVERTING ] \u2514\u2500 Disc 2")
        q.new(4, "[  VERIFIED  ] Loose")
        self.assertEqual(q.tally(), Tally(total=3, waiting=0, active=1, done=2, skipped=0, problem=0))

    def test_an_archive_with_no_games_inside_counts_as_itself(self):
        q = RunQueue()
        q.new(1, "[  SKIPPED   ] Empty.zip (No valid disc images)")
        self.assertEqual(q.tally(), Tally(total=1, skipped=1))

    def test_a_retry_keeps_the_old_rows_but_counts_only_the_new_run(self):
        q = RunQueue()
        q.new(1, "[  VERIFIED  ] Good")
        q.new(2, "[   ERROR    ] Bad (Conversion failed - exit code 1)")
        q.begin_run(keep=True)
        q.new(1, "[ CONVERTING ] Bad")  # the engine's ids start again from 1
        self.assertEqual(len(q), 3)
        self.assertEqual([q.stale(i) for i in range(3)], [True, True, False])
        self.assertEqual(q.tally(), Tally(total=1, active=1))
        q.update(1, "[  VERIFIED  ] Bad")
        self.assertEqual(q.task_at(2).phase, DONE)
        self.assertEqual(q.task_at(0).name, "Good")  # not overwritten by the new id 1

    def test_the_tally_adds_up_what_the_finished_games_saved(self):
        q = RunQueue()
        q.new(1, "[  VERIFIED  ] A - 50% smaller (2.00 GB -> 1.00 GB)")
        q.new(2, "[  VERIFIED  ] B - 50% smaller (1.00 GB -> 512.00 MB)")
        q.new(3, "[ CONVERTING ] C")
        self.assertEqual(q.tally().saved, 2**30 + 2**29)

    def test_closing_a_run_settles_whatever_was_cut_short(self):
        q = RunQueue()
        q.new(1, "[  VERIFIED  ] Done - 10% smaller (1.00 GB -> 900.00 MB)")
        q.new(2, "[ CONVERTING ] (40.0%) Halfway")
        q.new(3, "[  WAITING   ] Never started")
        self.assertEqual(q.close_run(), [1, 2])
        self.assertEqual([q.task_at(i).phase for i in range(3)], [DONE, SKIPPED, SKIPPED])
        self.assertEqual(q.task_at(1).note, "stopped before it finished")
        self.assertEqual(q.task_at(2).note, "not started")
        self.assertEqual(q.tally(), Tally(total=3, done=1, skipped=2, saved=q.tally().saved))
        self.assertEqual(q.close_run(), [], "closing twice changes nothing")

    def test_a_fresh_run_starts_empty(self):
        q = RunQueue()
        q.new(1, "[  VERIFIED  ] A")
        q.begin_run(keep=False)
        self.assertEqual((len(q), q.tally()), (0, Tally()))

    def test_clear_empties_everything(self):
        q = RunQueue()
        q.new(1, "[  VERIFIED  ] A")
        q.clear()
        self.assertEqual(len(q), 0)
        self.assertIsNone(q.row_of(1))


class HeadlineTests(unittest.TestCase):
    def test_a_run_in_progress(self):
        self.assertEqual(Tally(total=12, active=2, done=3, problem=1).headline(True), "3 of 12 done \u00b7 2 running \u00b7 1 failed")

    def test_a_finished_clean_run(self):
        self.assertEqual(Tally(total=5, done=5).headline(False), "All 5 done")

    def test_the_saving_is_mentioned_once_there_is_one(self):
        self.assertEqual(Tally(total=5, done=5, saved=3 * 2**30).headline(False), "All 5 done \u00b7 saved 3.00 GB")
        self.assertEqual(Tally(total=5, done=2, active=1, saved=2**20).headline(True),
                         "2 of 5 done \u00b7 1 running \u00b7 saved 1.00 MB")

    def test_a_finished_run_with_trouble_says_so(self):
        self.assertEqual(Tally(total=5, done=3, problem=1, skipped=1).headline(False), "3 of 5 done \u00b7 1 failed \u00b7 1 skipped")

    def test_nothing_queued_says_nothing(self):
        self.assertEqual(Tally().headline(True), "")

    def test_running_is_not_mentioned_once_the_run_is_over(self):
        self.assertNotIn("running", Tally(total=2, active=1, done=1).headline(False))


class FilterTests(unittest.TestCase):
    def test_each_filter_shows_what_it_says(self):
        run, bad, ok = (parse_task(t) for t in ("[ CONVERTING ] A", "[   ERROR    ] B (x)", "[  VERIFIED  ] C"))
        self.assertEqual([matches(t, "all") for t in (run, bad, ok)], [True, True, True])
        self.assertEqual([matches(t, "active") for t in (run, bad, ok)], [True, False, False])
        self.assertEqual([matches(t, "problems") for t in (run, bad, ok)], [False, True, False])


@POSIX_ONLY
class WithTheRealEngine(TempDirTest):
    """Whatever state words the engine really writes must be ones the queue knows."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("distill.batch.APP_DIR", self.tmp / "app")
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.tmp / "work").mkdir()

    def game(self, name: str) -> None:
        self.write(f"in/{name}.cue", f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE2/2352\n')
        self.write(f"in/{name}.bin", b"b" * 5000)

    def test_a_real_run_ends_with_one_done_row_and_one_problem_row(self):
        self.game("Good")
        self.game("BROKEN")
        cfg = Config(source=self.tmp / "in", dest=self.tmp / "out", chdman=str(make_tool(self.tmp / "chdman", FAKE_CHDMAN)),
                     extract=False, verify=True, delete=False, thread_budget=2, io_mode="ssd", stall_minutes=0,
                     temp_dir=str(self.tmp / "work"))
        events, _summary = run_batch(cfg)
        q = RunQueue()
        states = set()
        for ev in events:
            if ev and ev[0] == "new":
                q.new(ev[1], ev[2])
            elif ev and ev[0] == "update":
                q.update(ev[1], f"{ev[2]} {ev[3]}")
                states.add(ev[2].strip("[] ").upper())
        self.assertLessEqual(states, set(PHASE_OF), "the engine wrote a state the queue does not know")
        phases = {q.task_at(i).name: q.task_at(i).phase for i in range(len(q))}
        self.assertEqual(phases.get("Good"), DONE)
        self.assertEqual(phases.get("BROKEN"), PROBLEM)
        tally = q.tally()
        self.assertEqual((tally.total, tally.done, tally.problem), (2, 1, 1))
        self.assertIn("1 failed", tally.headline(False))
        self.assertGreater(tally.saved, 0, "the engine's own size change should reach the tally")


if __name__ == "__main__":
    unittest.main()
