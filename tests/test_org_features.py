"""Sort by system, library report, run history / preview reports, naming patterns and the missing-disc filter."""
import csv
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from distill import organize, orgpages
from distill.library import LibraryEntry, summary, write_csv, write_html
from distill.organize import (
    UNKNOWN_CHOICES, apply_findings, apply_template, find_by_system, find_library, find_multidisc, find_tidy_renames,
    report_rows, save_report, tidy_name, undo_depth, undo_last,
)
from tests.helpers import build_iso

NEVER = threading.Event()
PS2 = {"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_000.00;1\n"}
PS1 = {"SYSTEM.CNF": b"BOOT = cdrom:\\SLUS_000.00;1\n"}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="distill_feat_")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.object(organize.paths, "state_dir", return_value=self.root / "_app")
        (self.root / "_app").mkdir()
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, rel: str, data: bytes = b"x" * 3000) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def apply(self, tool: str, findings):
        title = orgpages.ORG_TOOLS[tool].title  # what the Organize page passes along for the run log
        return apply_findings(tool, findings, lambda _m: None, lambda _a, _b: None, NEVER, title=title)


class SortBySystemTests(Base):
    def library(self):
        build_iso(self.root / "ps2game.iso", PS2)
        build_iso(self.root / "ps1game.iso", PS1)
        self.make("Pokemon.gba")
        self.make("mystery.chd", b"MComprHD")
        return self.root

    def test_one_finding_per_system_and_unknowns_are_left_alone(self):
        findings, _ = find_by_system(self.library(), True, {}, NEVER)
        self.assertEqual({f.title: f.problem == "" for f in findings},
                         {"Game Boy Advance": True, "PlayStation": True, "PlayStation 2": True, "Unknown system": False})

    def test_default_destination_is_per_system_folders_inside_the_scanned_folder(self):
        findings, _ = find_by_system(self.library(), True, {}, NEVER)
        ok, failures = self.apply("bysystem", [f for f in findings if not f.problem])
        self.assertEqual((ok, failures), (3, []))
        self.assertTrue((self.root / "PlayStation 2" / "ps2game.iso").is_file())
        self.assertTrue((self.root / "Game Boy Advance" / "Pokemon.gba").is_file())
        self.assertTrue((self.root / "mystery.chd").is_file())
        self.assertEqual(find_by_system(self.root, True, {}, NEVER)[0][0].title, "Unknown system")  # nothing left to sort

    def test_a_chosen_destination_is_used_instead(self):
        dest = self.root / "Sorted"
        self.make("Pokemon.gba")
        (f,), _ = find_by_system(self.root, True, {"dest": str(dest)}, NEVER)
        self.apply("bysystem", [f])
        self.assertTrue((dest / "Game Boy Advance" / "Pokemon.gba").is_file())

    def test_unknown_files_can_go_to_their_own_folder(self):
        self.make("mystery.chd", b"MComprHD")
        (f,), _ = find_by_system(self.root, True, {"unknown": UNKNOWN_CHOICES[1]}, NEVER)
        self.assertEqual(f.problem, "")
        self.apply("bysystem", [f])
        self.assertTrue((self.root / "Unknown system" / "mystery.chd").is_file())

    def test_cue_tracks_and_playlists_travel_together_and_folders_are_kept(self):
        game = self.root / "Final Fantasy VII"
        sector = b"SEGA SEGASATURN " + b"\0" * 2336
        (game).mkdir()
        (game / "FF7 (Disc 1).bin").write_bytes(sector * 4)
        (game / "FF7 (Disc 1).cue").write_text('FILE "FF7 (Disc 1).bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
        (game / "FF7.m3u").write_text("FF7 (Disc 1).cue\n")
        findings, _ = find_by_system(self.root, True, {}, NEVER)
        self.assertEqual([f.title for f in findings], ["Sega Saturn"])
        self.assertEqual(len(findings[0].ops), 3)  # cue, bin and playlist
        self.apply("bysystem", findings)
        self.assertTrue((self.root / "Sega Saturn" / "Final Fantasy VII" / "FF7.m3u").is_file())
        self.assertTrue((self.root / "Sega Saturn" / "Final Fantasy VII" / "FF7 (Disc 1).bin").is_file())

    def test_a_folder_named_for_the_system_counts_as_sorted(self):
        build_iso(self.root / "PS2" / "game.iso" if (self.root / "PS2").mkdir() is None else None, PS2)
        findings, message = find_by_system(self.root, True, {}, NEVER)
        self.assertEqual(findings, [])
        self.assertIn("already in place", message)

    def test_a_disc_filed_under_the_wrong_system_folder_is_moved(self):
        (self.root / "PS2").mkdir()
        build_iso(self.root / "PS2" / "oops.iso", PS1)
        (f,), _ = find_by_system(self.root, True, {}, NEVER)
        self.apply("bysystem", [f])
        self.assertTrue((self.root / "PlayStation" / "oops.iso").is_file())

    def test_name_clashes_are_skipped_not_overwritten(self):
        self.make("Pokemon.gba")
        self.make("Game Boy Advance/Pokemon.gba", b"other")
        findings, message = find_by_system(self.root, True, {}, NEVER)
        self.assertEqual(findings, [])  # the copy in the system folder is already in place; the loose one is skipped
        self.assertIn("skipped", message)


class LibraryReportTests(Base):
    def test_findings_hold_entries_and_export_to_csv_and_html(self):
        build_iso(self.root / "Game (USA).iso", PS2)
        self.make("Pokemon (Europe).gba")
        findings, message = find_library(self.root, True, {}, NEVER)
        self.assertEqual(sorted(f.title for f in findings), ["Game Boy Advance", "PlayStation 2"])
        self.assertIn("2 file(s)", message)
        entries = [e for f in findings for e in f.data]
        self.assertEqual({e.region for e in entries}, {"USA", "Europe"})
        csv_path, html_path = self.root / "r.csv", self.root / "r.html"
        write_csv(csv_path, entries)
        write_html(html_path, entries)
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][:3], ["System", "Name", "Format"])
        self.assertEqual(len(rows), 3)
        page = html_path.read_text(encoding="utf-8")
        self.assertIn("Pokemon (Europe).gba", page)
        self.assertNotIn("__ROWS__", page)

    def test_html_escapes_names_and_summary_counts(self):
        entries = [LibraryEntry("<b>x</b>.gba", "Game Boy Advance", "GBA", "", 10, 0.0, ""),
                   LibraryEntry("y.gba", "Game Boy Advance", "GBA", "", 5, 0.0, "")]
        page = self.root / "p.html"
        write_html(page, entries)
        self.assertNotIn("<b>x</b>", page.read_text(encoding="utf-8"))
        self.assertEqual(summary(entries), [("Game Boy Advance", 2, 15)])

    def test_cue_sets_count_their_track_files(self):
        self.make("Game.bin", b"\0" * 5000)
        (self.root / "Game.cue").write_text('FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
        findings, _ = find_library(self.root, True, {}, NEVER)
        (entry,) = findings[0].data
        self.assertGreaterEqual(entry.size, 5000)


class HistoryAndReportTests(Base):
    def rename(self, old: str, new: str):
        src = self.make(old)
        return organize.Finding(old, "1", "Ready", [], [("move", str(src), str(self.root / new))])

    def test_runs_stack_and_undo_steps_back_one_at_a_time(self):
        self.apply("tidy", [self.rename("a.iso", "b.iso")])
        self.apply("tidy", [self.rename("c.iso", "d.iso")])
        self.assertEqual(undo_depth("tidy"), 2)
        self.assertEqual(undo_last("tidy"), (1, []))
        self.assertTrue((self.root / "c.iso").is_file())
        self.assertTrue((self.root / "b.iso").is_file())  # the older run is still done
        self.assertEqual(undo_depth("tidy"), 1)
        self.assertEqual(undo_last("tidy"), (1, []))
        self.assertTrue((self.root / "a.iso").is_file())
        self.assertIsNone(undo_last("tidy"))
        self.assertFalse(organize.undo_file("tidy").exists())

    def test_two_runs_in_the_same_clock_tick_are_both_kept(self):
        """Windows' clock can return the same time twice; a repeated run id used to overwrite the earlier run."""
        from datetime import datetime
        frozen = datetime(2026, 1, 1, 12, 0, 0, 123456)
        with mock.patch.object(organize, "datetime") as fake:
            fake.now.return_value = frozen
            stamps = [organize._new_stamp() for _ in range(3)]
        self.assertEqual(len(set(stamps)), 3)
        self.assertEqual(stamps, sorted(stamps))

    def test_the_old_single_run_undo_file_still_works(self):
        src = self.make("new.iso")
        organize.undo_file("tidy").write_text(
            '{"made": "x", "items": [{"name": "n", "ops": [{"op": "move", "src": "%s", "dst": "%s"}]}]}'
            % (str(self.root / "old.iso").replace("\\", "\\\\"), str(src).replace("\\", "\\\\")), encoding="utf-8")
        self.assertEqual(undo_last("tidy"), (1, []))
        self.assertTrue((self.root / "old.iso").is_file())

    def test_every_run_is_written_to_the_readable_log(self):
        self.apply("tidy", [self.rename("a.iso", "b.iso")])
        text = organize.run_log_file().read_text(encoding="utf-8")
        self.assertIn("Tidy filenames", text)
        self.assertIn("RENAME", text)
        self.assertIn("a.iso", text)

    def test_run_log_falls_back_to_the_tool_key_without_a_title(self):
        apply_findings("tidy", [self.rename("c.iso", "d.iso")], lambda _m: None, lambda _a, _b: None, NEVER)
        self.assertIn("  tidy: 1 item(s)", organize.run_log_file().read_text(encoding="utf-8"))

    def test_preview_report_lists_each_operation_and_saves_both_formats(self):
        f = self.rename("a.iso", "b.iso")
        blocked = organize.Finding("x", "1", "", [], [], problem="'x' already exists")
        rows = report_rows("Tidy filenames", [f, blocked], {0})
        self.assertEqual(rows[1][2:5], ["Yes", "Ready", "Rename"])
        self.assertEqual(rows[2][2], "Blocked")
        save_report(self.root / "p.csv", rows)
        save_report(self.root / "p.txt", rows)
        self.assertIn("a.iso", (self.root / "p.csv").read_text(encoding="utf-8-sig"))
        self.assertIn("Rename", (self.root / "p.txt").read_text(encoding="utf-8"))


class NamingPatternTests(Base):
    def test_pattern_parts(self):
        self.assertEqual(apply_template("Game (USA) (Rev 1) (Disc 2)", "{title} ({region}) {disc}"),
                         "Game (USA) (Disc 2)")
        self.assertEqual(apply_template("Game (Disc 1 of 2)", "{title} ({region}) {disc}"), "Game (Disc 1 of 2)")
        self.assertEqual(apply_template("Game (Europe) (Rev 1)", "{title} {tags}"), "Game (Rev 1)")
        self.assertEqual(apply_template("Game (USA)", "[{system}] {title}", "PlayStation 2"), "[PlayStation 2] Game")

    def test_a_pattern_renames_through_the_tidy_tool(self):
        self.make("Some Game [!] (USA) (Rev 1).chd")
        findings, _ = find_tidy_renames(self.root, True, {"template": "{title} ({region})"}, NEVER)
        self.assertEqual(findings[0].note, "->  Some Game (USA).chd")

    def test_system_token_reads_the_file(self):
        build_iso(self.root / "game.iso", PS2)
        findings, _ = find_tidy_renames(self.root, True, {"template": "{title} [{system}]"}, NEVER)
        self.assertEqual(findings[0].note, "->  game [PlayStation 2].iso")

    def test_a_pattern_without_parts_is_refused(self):
        with self.assertRaises(ValueError):
            find_tidy_renames(self.root, True, {"template": "just text"}, NEVER)

    def test_no_pattern_leaves_tidy_name_as_before(self):
        self.assertEqual(tidy_name("Game_(Disk 1)", {"spaces": True, "discs": True}), "Game (Disc 1)")


class MissingDiscTests(Base):
    def test_only_missing_filters_to_games_with_a_gap(self):
        for n in (1, 3):
            self.make(f"Big Game (Disc {n}).chd")
        for n in (1, 2):
            self.make(f"Complete (Disc {n}).chd")
        everything, _ = find_multidisc(self.root, True, {}, NEVER)
        only, message = find_multidisc(self.root, True, {"only_missing": True}, NEVER)
        self.assertEqual(len(everything), 2)
        self.assertEqual([f.title for f in only], ["Big Game"])
        self.assertIn("1 game(s) are missing discs", message)
        self.assertEqual(only[0].children[1], ("(not found)", "Disc 2", "Missing"))


if __name__ == "__main__":
    unittest.main()


class OrgPageTests(unittest.TestCase):
    """The sidebar pages group the tools; a multi-step page shows the first step that has something to do."""

    @staticmethod
    def fake_tool(key, findings, message=""):
        calls = []

        def scan(root, recursive, opts, stop):
            calls.append(key)
            return list(findings), message
        tool = orgpages.OrgTool(key, key, key.title(), "", ("A", "B"), "Go", "Nothing.", scan)
        return tool, calls

    def test_every_tool_lives_on_exactly_one_page(self):
        placed = [t.key for p in orgpages.ORG_PAGES for t in p.steps]
        self.assertEqual(sorted(placed), sorted(orgpages.ORG_TOOLS))
        self.assertEqual(len(placed), len(set(placed)))

    def test_every_tool_has_its_own_busy_wording(self):
        for key, tool in orgpages.ORG_TOOLS.items():
            self.assertTrue(tool.busy.endswith("..."), key)
            self.assertNotEqual(tool.busy, "Working...", key)

    def test_pages_are_in_workflow_order(self):
        self.assertEqual([p.key for p in orgpages.ORG_PAGES], ["fix", "extras", "patch", "bysystem", "report"])
        self.assertEqual([t.key for t in orgpages.PAGES_BY_KEY["fix"].steps], ["tidy", "multi", "playlists", "discfix"])

    def test_first_step_with_findings_wins_and_later_steps_are_not_scanned(self):
        (a, ca), (b, cb), (c, cc) = (self.fake_tool("a", []), self.fake_tool("b", [organize.Finding("x")]),
                                     self.fake_tool("c", [organize.Finding("y")]))
        page = orgpages.OrgPage("p", "P", "P", "", (a, b, c))
        findings, message, step = orgpages.scan_page(page, Path("."), True, {}, threading.Event())
        self.assertEqual((step, [f.title for f in findings]), ("b", ["x"]))
        self.assertEqual((ca, cb, cc), (["a"], ["b"], []))
        self.assertIn("Nothing to do: a", message)

    def test_unticked_steps_are_skipped(self):
        (a, ca), (b, _) = self.fake_tool("a", [organize.Finding("x")]), self.fake_tool("b", [organize.Finding("y")])
        page = orgpages.OrgPage("p", "P", "P", "", (a, b))
        _f, _m, step = orgpages.scan_page(page, Path("."), True, {"_steps": {"b"}}, threading.Event())
        self.assertEqual((step, ca), ("b", []))
        findings, message, _s = orgpages.scan_page(page, Path("."), True, {"_steps": set()}, threading.Event())
        self.assertEqual((findings, message), ([], "Tick at least one step to scan."))

    def test_nothing_to_do_anywhere_names_what_was_checked(self):
        (a, _), (b, _) = self.fake_tool("a", []), self.fake_tool("b", [])
        page = orgpages.OrgPage("p", "P", "P", "", (a, b))
        findings, message, step = orgpages.scan_page(page, Path("."), True, {}, threading.Event())
        self.assertEqual((findings, step, message), ([], "b", "Checked: a, b"))

    def test_single_step_page_behaves_like_the_tool(self):
        t, _ = self.fake_tool("solo", [], "note")
        page = orgpages.OrgPage("p", "P", "P", "", (t,))
        self.assertEqual(orgpages.scan_page(page, Path("."), True, {}, threading.Event()), ([], "note", "solo"))

    def test_one_undo_covers_the_whole_page(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(organize.paths, "state_dir", return_value=Path(td)):
            self.assertIsNone(orgpages.newest_undo_step(["tidy", "multi", "playlists"]))
            organize._save_undo("tidy", [{"name": "n", "ops": []}], "2026-01-01T10:00:00.000000")
            organize._save_undo("playlists", [{"name": "n", "ops": []}], "2026-01-02T10:00:00.000000")
            organize._save_undo("multi", [{"name": "n", "ops": []}], "2026-01-01T12:00:00.000000")
            self.assertEqual(orgpages.newest_undo_step(["tidy", "multi", "playlists"]), "playlists")
            organize.undo_last("playlists")
            self.assertEqual(orgpages.newest_undo_step(["tidy", "multi", "playlists"]), "multi")
