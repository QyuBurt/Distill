import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from distill import clipwatch, postprocess


class ClipTests(unittest.TestCase):
    def test_only_download_looking_links(self):
        text = ("see https://example.com/page and https://archive.org/details/x, also "
                "https://host.com/files/game.zip). magnet:?xt=urn:btih:abc123 "
                "https://drive.google.com/file/d/abcdefghijk/view https://www.youtube.com/watch?v=a "
                "https://news.site/story.html")
        got = clipwatch.links_in_text(text)
        self.assertEqual(got, ["https://archive.org/details/x", "https://host.com/files/game.zip",
                               "magnet:?xt=urn:btih:abc123", "https://drive.google.com/file/d/abcdefghijk/view",
                               "https://www.youtube.com/watch?v=a"])
        self.assertEqual(clipwatch.links_in_text(""), [])
        self.assertEqual(clipwatch.links_in_text("https://a.com/x.zip https://a.com/x.zip"), ["https://a.com/x.zip"])


class UnpackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def zipped(self, name, files):
        p = self.tmp / name
        with zipfile.ZipFile(p, "w") as z:
            for n, data in files.items():
                z.writestr(n, data)
        return p

    def test_single_item_unpacks_beside_the_archive(self):
        z = self.zipped("Game.zip", {"Game.gba": b"rom"})
        res = postprocess.unpack(z, delete=True)
        self.assertTrue(res.ok and res.deleted)
        self.assertEqual((self.tmp / "Game.gba").read_bytes(), b"rom")
        self.assertFalse(z.exists())
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(".")], [])

    def test_several_items_get_a_folder_and_names_never_clash(self):
        z = self.zipped("Pack.zip", {"a.bin": b"1", "b.cue": b"2"})
        (self.tmp / "Pack").mkdir()
        res = postprocess.unpack(z, delete=False)
        self.assertEqual(res.into.name, "Pack (2)")
        self.assertTrue(z.exists())
        self.assertEqual(sorted(p.name for p in res.into.iterdir()), ["a.bin", "b.cue"])

    def test_hostile_paths_are_refused(self):
        z = self.zipped("Evil.zip", {"../escape.txt": b"x"})
        res = postprocess.unpack(z, delete=True)
        self.assertFalse(res.ok)
        self.assertTrue(z.exists())
        self.assertFalse((self.tmp.parent / "escape.txt").exists())

    def test_bad_archive_is_reported_and_kept(self):
        bad = self.tmp / "Broken.zip"
        bad.write_bytes(b"not a zip")
        res = postprocess.unpack(bad, delete=True)
        self.assertFalse(res.ok)
        self.assertTrue(bad.exists())

    def test_multipart_rules(self):
        self.assertTrue(postprocess.is_first_archive(Path("a.part1.rar")))
        self.assertFalse(postprocess.is_first_archive(Path("a.part2.rar")))
        self.assertFalse(postprocess.is_first_archive(Path("a.r00")))
        self.assertTrue(postprocess.is_multipart(Path("a.part01.rar")))

    def test_unpack_all_skips_non_archives(self):
        z = self.zipped("One.zip", {"x.txt": b"1"})
        (self.tmp / "plain.bin").write_bytes(b"x")
        logs = []
        res = postprocess.unpack_all([z, self.tmp / "plain.bin", self.tmp / "missing.zip"], False, logs.append, threading.Event())
        self.assertEqual(len(res), 1)
        self.assertTrue(any("Unpacking 1 of 1" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
