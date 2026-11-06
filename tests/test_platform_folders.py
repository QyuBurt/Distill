"""A batch with a blank Destination files each result under a folder named for its platform."""
from __future__ import annotations

from pathlib import Path

from distill.models import Config
from tests.helpers import FAKE_CHDMAN, POSIX_ONLY, make_tool, run_batch
from tests.test_batch import IsolatedAppDir, config

SATURN = b"SEGA SEGASATURN " + b"\0" * 2336 + b"\0" * 4000


@POSIX_ONLY
class PlatformFolderTests(IsolatedAppDir):
    def setUp(self):
        super().setUp()
        (self.tmp / "work").mkdir()
        self.chdman = str(make_tool(self.tmp / "fake_chdman", FAKE_CHDMAN))

    def cfg(self, **kw) -> Config:
        return config(self.tmp, **{"chdman": self.chdman, "dest": None, **kw})

    def game(self, folder: str, name: str, data: bytes) -> None:
        self.write(f"{folder}/{name}.cue", f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE1/2352\n')
        self.write(f"{folder}/{name}.bin", data)

    def converted(self, root: Path) -> list[str]:
        return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.chd"))

    def test_blank_destination_makes_a_folder_for_the_platform(self):
        self.game("in", "Panzer", SATURN)
        self.game("in/some/deep/folder", "Nights", SATURN)
        _events, summary = run_batch(self.cfg())
        self.assertEqual((summary.converted, summary.failures), (2, ()))
        self.assertEqual(self.converted(self.tmp / "in"), ["Sega Saturn/Nights.chd", "Sega Saturn/Panzer.chd"])

    def test_an_unknown_platform_stays_next_to_its_source(self):
        self.game("in/misc", "Mystery", b"\0" * 6000)
        run_batch(self.cfg())
        self.assertEqual(self.converted(self.tmp / "in"), ["misc/Mystery.chd"])

    def test_a_chosen_destination_wins(self):
        self.game("in", "Panzer", SATURN)
        run_batch(self.cfg(dest=self.tmp / "out"))
        self.assertEqual(self.converted(self.tmp / "out"), ["Panzer.chd"])
        self.assertEqual(self.converted(self.tmp / "in"), [])

    def test_the_old_behaviour_is_one_checkbox_away(self):
        self.game("in/misc", "Panzer", SATURN)
        run_batch(self.cfg(platform_folders=False))
        self.assertEqual(self.converted(self.tmp / "in"), ["misc/Panzer.chd"])

    def test_a_source_already_named_for_the_platform_is_not_nested(self):
        self.game("Saturn", "Panzer", SATURN)
        run_batch(self.cfg(source=self.tmp / "Saturn"))
        self.assertEqual(self.converted(self.tmp / "Saturn"), ["Panzer.chd"])

    def test_a_game_under_a_matching_folder_stays_in_it(self):
        self.game("in/Sega Saturn/RPGs", "Panzer", SATURN)
        run_batch(self.cfg())
        self.assertEqual(self.converted(self.tmp / "in"), ["Sega Saturn/RPGs/Panzer.chd"])

    def test_a_second_run_finds_the_earlier_result_and_does_nothing(self):
        self.game("in", "Panzer", SATURN)
        run_batch(self.cfg())
        _events, summary = run_batch(self.cfg())
        self.assertEqual(self.converted(self.tmp / "in"), ["Sega Saturn/Panzer.chd"])
        self.assertEqual(summary.failures, ())
