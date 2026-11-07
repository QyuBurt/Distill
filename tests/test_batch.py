"""End-to-end runs of the conversion engine against stand-in command-line tools (no chdman needed)."""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from distill.batch import BatchRunner
from distill.models import Config
from tests.helpers import (FAKE_CHDMAN, FAKE_PKG_EXTRACTOR, POSIX_ONLY, TempDirTest, build_pkg, logs, make_tool,
                           run_batch)


class IsolatedAppDir(TempDirTest):
    """Keep engine logs in the per-test directory in both XDG and legacy layouts."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("distill.batch.APP_DIR", self.tmp / "app")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("distill.batch.paths.state_dir", return_value=self.tmp / "app")
        patcher.start()
        self.addCleanup(patcher.stop)


def config(tmp: Path, **kw) -> Config:
    base = dict(source=tmp / "in", dest=tmp / "out", chdman="", extract=False, verify=True, delete=False,
                thread_budget=4, io_mode="ssd", stall_minutes=0, temp_dir=str(tmp / "work"))
    return Config(**{**base, **kw})


@POSIX_ONLY
class ChdRunTests(IsolatedAppDir):
    def setUp(self):
        super().setUp()
        (self.tmp / "work").mkdir()
        self.chdman = str(make_tool(self.tmp / "fake_chdman", FAKE_CHDMAN))

    def cfg(self, **kw) -> Config:
        return config(self.tmp, chdman=self.chdman, **kw)

    def make_game(self, name: str) -> list[Path]:
        cue = self.write(f"in/{name}.cue", f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE2/2352\n')
        return [cue, self.write(f"in/{name}.bin", b"b" * 5000)]

    def test_converts_verifies_and_keeps_the_originals(self):
        sources = self.make_game("Good Game")
        events, summary = run_batch(self.cfg())
        self.assertEqual((summary.total, summary.failures), (1, ()))
        self.assertEqual(summary.converted, 1)
        out = list((self.tmp / "out").rglob("*.chd"))
        self.assertEqual([p.name for p in out], ["Good Game.chd"])
        self.assertTrue(all(p.exists() for p in sources))
        self.assertFalse(list((self.tmp / "out").rglob("*.part")))
        self.assertIn("Batch Process Complete", logs(events))

    def test_delete_removes_the_cue_and_its_tracks_only_after_success(self):
        good = self.make_game("Good")
        keep = self.write("in/notes.txt", "keep me")
        _events, summary = run_batch(self.cfg(delete=True))
        self.assertEqual(summary.failures, ())
        self.assertTrue(all(not p.exists() for p in good))
        self.assertTrue(keep.exists())

    def strict_chdman(self) -> None:
        """A chdman that, like the real one, fails when a track file named in the cue sheet isn't there."""
        check = ('    for line in src.read_text(errors="ignore").splitlines():\n'
                 '        if line.strip().upper().startswith("FILE"):\n'
                 '            ref = line.split(chr(34))[1]\n'
                 '            if not (src.parent / ref).is_file():\n'
                 '                print("Error opening input file", ref); sys.exit(1)\n')
        body = FAKE_CHDMAN.replace('    out.write_bytes(b"MComprHD"', check + '    out.write_bytes(b"MComprHD"', 1)
        self.assertNotEqual(body, FAKE_CHDMAN)
        self.chdman = str(make_tool(self.tmp / "strict_chdman", body))

    def zip_with_renamed_track(self) -> None:
        import zipfile
        (self.tmp / "in").mkdir(exist_ok=True)
        with zipfile.ZipFile(self.tmp / "in" / "Burn Cycle.zip", "w") as z:
            z.writestr("Burn Cycle.cue", 'FILE "Burn: Cycle.bin" BINARY\n  TRACK 01 MODE2/2352\n')
            z.writestr("Burn_ Cycle.bin", b"b" * 5000)  # how unpacking on Windows names a file called 'Burn: Cycle.bin'

    def test_a_track_file_renamed_by_unpacking_is_found_again_by_its_cue_sheet(self):
        self.strict_chdman()
        self.zip_with_renamed_track()
        events, summary = run_batch(self.cfg(extract=True))
        self.assertEqual((summary.converted, summary.failures), (1, ()), logs(events))
        self.assertIn("is stored as 'Burn_ Cycle.bin'", logs(events))  # said in the log, so it isn't a silent change

    def test_without_the_repair_that_same_zip_would_fail(self):
        self.strict_chdman()
        self.zip_with_renamed_track()
        with mock.patch("distill.batch.repair_sheet_references", return_value=[]):
            _events, summary = run_batch(self.cfg(extract=True))
        self.assertEqual(summary.converted, 0)  # the test above passes because of the repair, not by accident

    def test_each_title_is_one_line_that_only_changes_state_and_ends_at_100_percent(self):
        import zipfile

        chatty = FAKE_CHDMAN.replace('print("Compression complete ... final ratio = 40.0%")',
                                     'print("Compressing, 97.9% complete... (ratio=57.1%)"); print("Compression complete ... final ratio = 57.1%")')
        self.chdman = str(make_tool(self.tmp / "chatty_chdman", chatty))
        self.make_game("Plain Game")
        with zipfile.ZipFile(self.tmp / "in" / "Zipped Game.zip", "w") as z:
            z.writestr("Zipped Game.cue", 'FILE "Zipped Game.bin" BINARY\n  TRACK 01 MODE2/2352\n')
            z.writestr("Zipped Game.bin", b"b" * 5000)
        events, summary = run_batch(self.cfg(extract=True))
        self.assertEqual(summary.failures, (), logs(events))
        rows = [e for e in events if e[0] == "update"]
        self.assertTrue(rows)
        self.assertFalse([e for e in rows if "\n" in e[3] or "      >" in e[3]], "a title's row is one line, never a tool-output second line")
        self.assertNotIn("[extract]", logs(events), "no separate extraction messages under a title still being worked on")
        zipped = [e[2].strip() for e in rows if "Zipped Game" in e[3] and e[2].strip() in ("[ EXTRACTING ]", "[  WAITING   ]", "[ CONVERTING ]")]
        self.assertLess(zipped.index("[ EXTRACTING ]"), zipped.index("[  WAITING   ]"), zipped)  # unpacked = no longer EXTRACTING
        converting = [e for e in rows if e[2] == "[ CONVERTING ]"]
        self.assertTrue(any(e[3].startswith("(97.9%)") for e in converting))
        self.assertTrue(any(e[3].startswith("(100.0%)") for e in converting), "the row reaches 100% when the tool finishes")

    # ---- a chdman that does not exit --------------------------------------------------------------------------------
    DONE = 'print("Compression complete ... final ratio = 40.0%"); sys.exit(0)'

    def hanging(self, after: str) -> None:
        """A chdman that writes its CHD, then runs `after` instead of exiting."""
        body = FAKE_CHDMAN.replace(self.DONE, after)
        self.assertNotEqual(body, FAKE_CHDMAN)
        self.chdman = str(make_tool(self.tmp / "hanging_chdman", body))

    def run_hanging(self):
        self.make_game("Cdi Game")
        with mock.patch("distill.batch.FINISH_GRACE", 1.0), mock.patch("distill.batch.UNCONFIRMED_GRACE", 1.5):
            return run_batch(self.cfg())

    def test_a_chdman_that_says_it_finished_and_then_spins_is_closed_and_its_file_checked(self):
        self.hanging('print("Compression complete ... final ratio = 40.0%", flush=True)\n    while True: pass')
        events, summary = self.run_hanging()  # the watchdog is off here: CPU use would have counted as progress anyway
        self.assertEqual((summary.converted, summary.failures), (1, ()), logs(events))
        self.assertIn("did not exit", logs(events))
        self.assertEqual([p.name for p in (self.tmp / "out").rglob("*.chd")], ["Cdi Game.chd"])
        self.assertFalse(list((self.tmp / "out").rglob("*.part")))

    def test_a_chdman_that_says_it_finished_and_then_goes_silent_is_closed_not_restarted(self):
        self.hanging('print("Compression complete ... final ratio = 40.0%", flush=True)\n    import time; time.sleep(600)')
        events, summary = self.run_hanging()
        self.assertEqual((summary.converted, summary.failures), (1, ()), logs(events))
        self.assertNotIn("STALLED", logs(events))  # the finished file is kept, not thrown away and converted again

    def test_a_chdman_stuck_at_100_percent_without_its_final_line_is_closed_and_its_file_checked(self):
        self.hanging('print("Compressing, 100.0% complete... (ratio=40.0%)", flush=True)\n    while True: pass')
        events, summary = self.run_hanging()
        self.assertEqual((summary.converted, summary.failures), (1, ()), logs(events))
        self.assertIn("reached 100%", logs(events))

    def test_a_finished_chdman_that_cannot_be_closed_is_reported_as_that_and_not_restarted(self):
        self.hanging('print("Compression complete ... final ratio = 40.0%", flush=True)\n    import time; time.sleep(600)')
        with mock.patch.object(BatchRunner, "_await_exit", return_value=False):  # as if Windows were still flushing the file
            events, summary = self.run_hanging()
        text = logs(events)
        self.assertEqual(summary.converted, 0)
        self.assertEqual(len(summary.failures), 1, text)
        self.assertIn("could not be closed", summary.failures[0].reason)
        self.assertIn("reported that it had finished", text)
        self.assertNotIn("made no progress for", text)  # not the old, misleading "3 min" wording
        self.assertNotIn("RETRYING", text)  # the same job would only end the same way

    def test_a_chdman_that_exits_while_something_else_holds_its_output_open_is_not_waited_for(self):
        stray = ("import subprocess\n    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'])  # inherits the output pipe\n"
                 "    print('Compression complete ... final ratio = 40.0%', flush=True); sys.exit(0)")
        self.hanging(stray)
        self.make_game("Cdi Game")
        started = time.monotonic()
        with mock.patch("distill.batch.FINISH_GRACE", 60.0):  # so only the exit check can end the wait in time
            events, summary = run_batch(self.cfg())
        self.assertEqual((summary.converted, summary.failures), (1, ()), logs(events))
        self.assertLess(time.monotonic() - started, 7.0)

    def test_a_hang_that_leaves_no_usable_file_is_a_failure_not_a_success(self):
        body = FAKE_CHDMAN.replace('out.write_bytes(b"MComprHD" + b"\\0" * 4096)', "pass").replace(
            self.DONE, 'print("Compressing, 100.0% complete... (ratio=40.0%)", flush=True)\n    while True: pass')
        self.chdman = str(make_tool(self.tmp / "hanging_chdman", body))
        events, summary = self.run_hanging()
        self.assertEqual(summary.converted, 0)
        self.assertEqual(len(summary.failures), 1, logs(events))
        self.assertFalse(list((self.tmp / "out").rglob("*.chd")))

    def test_a_failing_tool_is_reported_and_the_source_survives(self):
        broken = self.make_game("BROKEN")
        good = self.make_game("Fine")
        _events, summary = run_batch(self.cfg(delete=True))
        self.assertEqual(len(summary.failures), 1)
        failure = summary.failures[0]
        self.assertEqual((failure.stage, failure.retryable), ("convert", True))
        self.assertIn("BROKEN", failure.name)
        self.assertIn("simulated failure", failure.reason)
        self.assertTrue(all(p.exists() for p in broken), "a failed source must never be deleted")
        self.assertTrue(all(not p.exists() for p in good))
        self.assertFalse(list((self.tmp / "out").rglob("BROKEN*.chd*")), "no half-written output left behind")

    def test_retry_runs_only_the_named_sources(self):
        self.make_game("One")
        two = self.make_game("Two")
        _events, summary = run_batch(self.cfg(), only=frozenset({two[0]}))
        self.assertEqual(summary.total, 1)
        self.assertEqual([p.name for p in (self.tmp / "out").rglob("*.chd")], ["Two.chd"])

    def test_an_existing_output_is_checked_not_redone(self):
        self.make_game("Again")
        run_batch(self.cfg())
        out = next((self.tmp / "out").rglob("Again.chd"))
        before = out.stat().st_mtime_ns
        _events, summary = run_batch(self.cfg())
        self.assertEqual(summary.failures, ())
        self.assertEqual(out.stat().st_mtime_ns, before)

    def test_empty_source_folder_ends_cleanly(self):
        (self.tmp / "in").mkdir()
        events, summary = run_batch(self.cfg())
        self.assertIsNone(summary)
        self.assertIn("No valid files", logs(events))
        self.assertEqual(events[-1][0], "done")  # the UI is always told the run ended

    def test_stop_ends_the_run_and_reports_done(self):
        from distill.batch import BatchRunner

        self.make_game("Slow")
        events: list[tuple] = []
        pause = threading.Event()
        pause.set()
        runner = BatchRunner(self.cfg(), events.append, pause)
        runner.shutdown()  # stop before it starts: nothing may be converted, and it must still finish
        runner.run()
        self.assertEqual(events[-1], ("done", None))  # a stopped run never reports a summary
        self.assertNotIn("Batch Process Complete", logs(events))
        self.assertFalse(list((self.tmp / "out").rglob("*.chd")))


@POSIX_ONLY
class PkgRunTests(IsolatedAppDir):
    GAME = "UP0001-CUSA12345_00-GAMEGAMEGAMEGAME"
    UPDATE = "UP0001-CUSA12345_00-PATCHPATCHPATCHPA"
    DLC = "UP0001-CUSA12345_00-ABCDEFGHIJKLMNOP"

    def setUp(self):
        super().setUp()
        (self.tmp / "work").mkdir()
        self.extractor = str(make_tool(self.tmp / "pkg_extractor", FAKE_PKG_EXTRACTOR))
        (self.tmp / "in").mkdir()
        build_pkg(self.tmp / "in" / "game.pkg", self.GAME, "gd")
        build_pkg(self.tmp / "in" / "update.pkg", self.UPDATE, "gp")
        build_pkg(self.tmp / "in" / "dlc.pkg", self.DLC, "ac")

    def cfg(self, **kw) -> Config:
        kw.setdefault("pkgext", self.extractor)
        return config(self.tmp, mode="pkg", pkg_tool="shadplus", **kw)

    def test_shadps4_layout_for_game_update_and_dlc(self):
        events, summary = run_batch(self.cfg(pkg_layout="shadps4"))
        self.assertEqual(summary.failures, (), logs(events))
        out = self.tmp / "out"
        self.assertTrue((out / "CUSA12345" / "eboot.bin").is_file())
        self.assertTrue((out / "CUSA12345-UPDATE" / "data.bin").is_file())
        self.assertTrue((out / "addcont" / "CUSA12345" / "ABCDEFGHIJKLMNOP" / "data.bin").is_file())
        self.assertFalse((out / "CUSA12345-UPDATE" / "eboot.bin").exists(), "an update never carries its own eboot")

    def test_folder_layout_names_folders_after_the_files(self):
        _events, summary = run_batch(self.cfg(pkg_layout="folder"))
        self.assertEqual(summary.failures, ())
        self.assertEqual(sorted(p.name for p in (self.tmp / "out").iterdir()), ["dlc", "game", "update"])

    def test_non_ps4_packages_are_skipped_and_logged(self):
        self.write("in/other.pkg", b"MZ" + b"\0" * 400)
        events, summary = run_batch(self.cfg(pkg_layout="folder"))
        self.assertEqual(summary.total, 3)
        self.assertIn("Skipped 1 .pkg", logs(events))

    def test_an_extractor_that_writes_nothing_is_a_failure(self):
        silent = make_tool(self.tmp / "silent", "sys.exit(0)")
        _events, summary = run_batch(self.cfg(pkg_layout="folder", pkgext=str(silent)))
        self.assertEqual(len(summary.failures), 3)

    def test_a_game_without_an_eboot_is_flagged(self):
        no_eboot = make_tool(self.tmp / "no_eboot", FAKE_PKG_EXTRACTOR.replace('(out / "eboot.bin").write_bytes(b"elf")', "pass"))
        _events, summary = run_batch(self.cfg(pkg_layout="folder", pkgext=str(no_eboot)))
        self.assertEqual(len(summary.failures), 1)  # the update and the DLC have no eboot of their own
        self.assertTrue(summary.failures[0].name.startswith("game"))
        self.assertIn("eboot.bin", summary.failures[0].reason.lower())


if __name__ == "__main__":
    unittest.main()
