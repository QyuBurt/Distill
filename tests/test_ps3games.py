from __future__ import annotations

import threading
import unittest

from distill.games import GameStore, clean_title
from distill.organize import find_library
from distill.ps3games import is_ps3_folder, read_ps3_folder, region_of_title_id
from tests.helpers import TempDirTest, build_sfo


class Ps3FolderTests(TempDirTest):
    def disc(self, name: str = "inFAMOUS [BLUS30000]", title: str = "inFAMOUS\u2122", tid: str = "BLUS30000"):
        base = f"Sony PlayStation 3/{name}/PS3_GAME"
        self.write(f"{base}/PARAM.SFO", build_sfo({"TITLE": title, "TITLE_ID": tid}))
        self.write(f"{base}/USRDIR/EBOOT.BIN", b"SCE\0")
        self.write(f"{base}/INSTALL.PKG", b"x" * 64)
        self.write(f"{base}/USRDIR/data/level.pkg", b"x" * 16)
        self.write(f"Sony PlayStation 3/{name}/PS3_DISC.SFB", b".SFB")

    def test_reads_title_region_and_eboot(self):
        self.disc()
        game = read_ps3_folder(self.tmp / "Sony PlayStation 3" / "inFAMOUS [BLUS30000]")
        self.assertEqual((game.title, game.title_id, game.region), ("inFAMOUS", "BLUS30000", "USA"))
        self.assertEqual(game.eboot.name, "EBOOT.BIN")

    def test_title_falls_back_to_the_folder_name(self):
        self.write("Ratchet [BCES00510]/PS3_GAME/USRDIR/EBOOT.BIN")
        self.assertEqual(read_ps3_folder(self.tmp / "Ratchet [BCES00510]").title, "Ratchet")

    def test_no_eboot_means_not_a_game(self):
        self.write("Thing/PS3_GAME/PARAM.SFO", build_sfo({"TITLE": "X"}))
        self.assertIsNone(read_ps3_folder(self.tmp / "Thing"))

    def test_cheap_test_and_regions(self):
        self.assertTrue(is_ps3_folder(self.tmp, ["ps3_game"], []))
        self.assertTrue(is_ps3_folder(self.tmp, ["USRDIR"], ["param.sfo"]))
        self.assertFalse(is_ps3_folder(self.tmp, ["USRDIR"], []))
        self.assertEqual((region_of_title_id("BCES00510"), region_of_title_id("BLJM60001"), region_of_title_id("junk")), ("Europe", "Japan", ""))

    def test_scan_lists_one_game_booting_the_eboot(self):
        self.disc()
        self.write("Sony PlayStation 3/Other Game [BLES00001]/PS3_GAME/PARAM.SFO", build_sfo({"TITLE": "Other Game", "TITLE_ID": "BLES00001"}))
        self.write("Sony PlayStation 3/Other Game [BLES00001]/PS3_GAME/USRDIR/EBOOT.BIN")
        findings, _ = find_library(self.tmp, True, {}, threading.Event())
        entries = [e for f in findings for e in f.data]
        self.assertEqual(sorted(e.name for e in entries), ["Other Game", "inFAMOUS"])  # no INSTALL.PKG, no level.pkg
        first = next(e for e in entries if e.name == "inFAMOUS")
        self.assertEqual((first.system, first.region, first.kind), ("PlayStation 3", "USA", "FOLDER"))
        self.assertTrue(first.path.endswith("EBOOT.BIN"))
        self.assertEqual(first.folder, "Sony PlayStation 3")
        store = GameStore(self.tmp / "games.db")
        store.sync(entries, str(self.tmp))
        self.assertEqual(sorted(g.title for g in store.games()), ["Other Game", "inFAMOUS"])


if __name__ == "__main__":
    unittest.main()
