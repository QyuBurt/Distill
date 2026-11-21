"""The savings preview, against the real scan with real (small) files. No Qt."""
from __future__ import annotations

import os
import threading
import unittest
import zipfile
from pathlib import Path

from distill.models import Config
from distill.preview import Preview, preview
from tests.helpers import TempDirTest


class Headline(unittest.TestCase):
    def test_a_normal_preview(self):
        text = Preview("chd", 3, 0, 3 * 2**30, 2**30).headline()
        self.assertEqual(text, "About 2.00 GB saved (67%): 3 titles, 3.00 GB \u2192 about 1.00 GB")

    def test_one_title_is_singular_and_archives_are_mentioned(self):
        text = Preview("rvz", 1, 2, 2**30, 2**29).headline()
        self.assertIn("1 title,", text)
        self.assertTrue(text.endswith("\u00b7 2 archives not counted"))

    def test_nothing_found_and_only_archives(self):
        self.assertEqual(Preview("chd", 0, 0, 0, 0).headline(), "Nothing to convert was found in that folder.")
        self.assertIn("Only archives found (1)", Preview("chd", 0, 1, 0, 0).headline())

    def test_a_stopped_preview_says_so(self):
        self.assertTrue(Preview("chd", 1, 0, 100, 50, stopped=True).headline().endswith("stopped early"))

    def test_the_saving_never_goes_negative(self):
        self.assertEqual(Preview("chd", 1, 0, 100, 120).saved, 0)


class WithRealFiles(TempDirTest):
    def game(self, name: str, data: bytes) -> None:
        self.write(f"in/{name}.cue", f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE2/2352\n')
        self.write(f"in/{name}.bin", data)

    def cfg(self, mode="chd", **kw) -> Config:
        return Config(source=self.tmp / "in", dest=self.tmp / "out", chdman="", verify=False, delete=False, thread_budget=2,
                      mode=mode, extract=kw.pop("extract", False), **kw)

    def test_a_compressible_disc_is_estimated_far_smaller(self):
        self.game("Zeros", b"\0" * (4 << 20))
        got = preview(self.cfg())
        self.assertEqual((got.titles, got.archives), (1, 0))
        self.assertGreater(got.before, 4 << 20)
        self.assertLess(got.after, got.before // 10)

    def test_an_incompressible_disc_barely_shrinks_and_never_grows(self):
        self.game("Noise", os.urandom(4 << 20))
        got = preview(self.cfg())
        self.assertLessEqual(got.after, got.before)
        self.assertGreater(got.after, got.before * 0.7)

    def test_several_titles_add_up(self):
        self.game("A", b"\0" * (2 << 20))
        self.game("B", b"\0" * (2 << 20))
        got = preview(self.cfg())
        self.assertEqual(got.titles, 2)
        self.assertGreater(got.before, 4 << 20)

    def test_an_archive_is_counted_but_not_estimated(self):
        self.game("A", b"\0" * (2 << 20))
        zpath = self.tmp / "in" / "Packed.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("Packed.iso", b"x" * 1000)
        got = preview(self.cfg(extract=True))
        self.assertEqual((got.titles, got.archives), (1, 1))

    def test_a_page_that_does_not_compress_has_no_preview(self):
        self.game("A", b"\0" * 1000)
        self.assertIsNone(preview(self.cfg(mode="pkg")))

    def test_an_empty_folder_finds_nothing(self):
        (self.tmp / "in").mkdir()
        got = preview(self.cfg())
        self.assertEqual((got.titles, got.archives, got.before), (0, 0, 0))

    def test_stop_ends_it_early_and_progress_is_reported(self):
        self.game("A", b"\0" * 1000)
        self.game("B", b"\0" * 1000)
        seen = []
        stop = threading.Event()

        def progress(i, n):
            seen.append((i, n))
            stop.set()  # the person pressed Stop after the first title

        got = preview(self.cfg(), progress, stop)
        self.assertEqual(seen, [(0, 2)])
        self.assertTrue(got.stopped)
        self.assertEqual(got.titles, 1)

    def test_a_game_folder_is_sampled_through_its_largest_files(self):
        folder = self.tmp / "in" / "Game"
        folder.mkdir(parents=True)
        (folder / "eboot.bin").write_bytes(b"\0" * (3 << 20))
        (folder / "tiny.txt").write_bytes(b"hello")
        (self.tmp / "in" / "Game" / "sce_sys").mkdir()
        (self.tmp / "in" / "Game" / "sce_sys" / "param.sfo").write_bytes(b"\0" * 100)
        from distill.preview import _folder_estimate
        before, after = _folder_estimate(folder, "ZAR")
        self.assertEqual(before, (3 << 20) + 5 + 100)
        self.assertLess(after, before // 10)
        self.assertEqual(_folder_estimate(self.tmp / "in" / "Game" / "sce_sys", "ZAR")[0], 100)


if __name__ == "__main__":
    unittest.main()
