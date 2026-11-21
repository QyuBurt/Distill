import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from distill import recomps as rc

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 2000


class CoverWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        for patch in (mock.patch.object(rc, "art_dir", lambda: self.dir), mock.patch.object(rc, "launchbox_covers", lambda r: ["http://x/a.png"]),
                      mock.patch.object(rc, "_image", lambda url: PNG)):
            patch.start()
            self.addCleanup(patch.stop)
        self.game = types.SimpleNamespace(id="g1", game="Sonic", system="Mega Drive", cover="", cover_path="", repo="")

    def test_the_picture_is_swapped_in_whole_and_no_part_file_is_left(self):
        dest = rc.fetch_cover(self.game)
        self.assertEqual(dest, self.dir / "g1.png")
        self.assertEqual(dest.read_bytes(), PNG)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["g1.png", "g1.src"])

    def test_the_window_never_sees_a_half_written_cover(self):
        seen = []
        real_replace = Path.replace

        def spying_replace(self_, target):
            seen.append(rc.cached_cover(self.game))  # (before the swap: nothing that looks like a cover is there yet)
            return real_replace(self_, target)
        with mock.patch.object(Path, "replace", spying_replace):
            rc.fetch_cover(self.game)
        self.assertEqual(seen, [None])

    def test_an_older_picture_in_another_format_goes_after_the_new_one_is_in(self):
        (self.dir / "g1.bmp").write_bytes(b"old")
        (self.dir / "g1.none").write_text("Sonic")
        rc.fetch_cover(self.game, retry=True)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["g1.png", "g1.src"])

    def test_an_old_picture_that_cannot_be_removed_does_not_lose_the_new_one(self):
        (self.dir / "g1.bmp").write_bytes(b"old")
        real_unlink = Path.unlink

        def locked(self_, missing_ok=False):
            if self_.name == "g1.bmp":
                raise PermissionError
            return real_unlink(self_, missing_ok=missing_ok)
        with mock.patch.object(Path, "unlink", locked):
            dest = rc.fetch_cover(self.game, retry=True)
        self.assertEqual(dest.read_bytes(), PNG)


if __name__ == "__main__":
    unittest.main()
