import tempfile
import unittest
from pathlib import Path

from distill import romm


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "RomM"
        self.addCleanup(self.tmp.cleanup)

    def _file(self, *parts, data=b"x" * 10) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_a_single_file_game_is_just_its_file(self):
        game = self._file("dc", "Chicken Run.chd", data=b"x" * 100)
        other = self._file("dc", "Other.chd")
        fp = romm.footprint(game, 1, self.root)
        self.assertEqual((fp.size, fp.is_folder), (100, False))
        self.assertEqual(romm.uninstall(game, 1, self.root), 100)
        self.assertFalse(game.exists())
        self.assertTrue(other.exists())  # a neighbour, and the platform folder, stay

    def test_a_multi_file_game_goes_with_its_folder_and_only_that(self):
        entry = self._file("psx", "Game", "Game.cue", data=b"a" * 5)
        self._file("psx", "Game", "Game.bin", data=b"b" * 50)
        keep = self._file("psx", "Keep", "Keep.cue")
        self.assertEqual(romm.footprint(entry, 2, self.root).size, 55)
        self.assertEqual(romm.uninstall(entry, 2, self.root), 55)
        self.assertFalse(entry.parent.exists())
        self.assertTrue(keep.exists())

    def test_a_multi_file_game_sitting_in_the_platform_folder_is_never_wiped(self):
        entry = self._file("psx", "Game.cue")
        neighbour = self._file("psx", "Another.cue")
        romm.uninstall(entry, 2, self.root)  # the folder is the platform's: only the file goes
        self.assertFalse(entry.exists())
        self.assertTrue(neighbour.exists())

    def test_nothing_outside_the_install_folder_is_touched(self):
        outside = Path(self.tmp.name) / "elsewhere.chd"
        outside.write_bytes(b"x")
        self.assertIsNone(romm.footprint(outside, 1, self.root))
        with self.assertRaises(romm.RommError):
            romm.uninstall(outside, 1, self.root)
        self.assertTrue(outside.exists())

    def test_a_game_that_is_not_installed_has_no_footprint(self):
        self.assertIsNone(romm.footprint(self.root / "dc" / "Nope.chd", 1, self.root))


if __name__ == "__main__":
    unittest.main()
