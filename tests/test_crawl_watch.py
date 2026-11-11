import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from distill import crawl, folderwatch, hosters, links, postprocess
from distill.downloads import LinkError, RemoteFile

PAGE = """<html><head><title> Game  Dumps </title><base href="https://files.example.com/roms/"></head><body>
<a href="mario.zip">Mario</a> <a href="/other/zelda.7z#top">Zelda</a> <a href="about.html">About</a>
<a href="mario.zip">again</a> <a href="https://www.mediafire.com/file/abc/game/file">MF</a>
<a href="https://www.mediafire.com/">front page</a> <a href="https://archive.org/about">nav</a>
<a href="https://archive.org/details/some-item">item</a> <a href="magnet:?xt=urn:btih:abc123&dn=x">m</a>
<a href="https://mega.nz/file/AbC#Key123">mega</a> <a href="pic.jpg">pic</a> <a href="dl.php?id=4">php</a>
<a href="https://www.youtube.com/watch?v=a">video</a>
<img src="cover.png"><video src="clip.mp4"></video>
<p>mirror: https://pixeldrain.com/u/AbCdEf12 and https://files.example.com/roms/sonic.rar.</p>
<script>var u = "https://files.example.com/roms/kirby.7z";</script></body></html>"""


class CrawlTests(unittest.TestCase):
    def test_extension_lists_are_normalised(self):
        self.assertEqual(crawl.normalize_exts("zip, .7Z;rar"), (".zip", ".7z", ".rar"))
        self.assertEqual(crawl.normalize_exts(["ISO", ".iso"]), (".iso",))
        self.assertIsNone(crawl.normalize_exts(""))
        self.assertIsNone(crawl.normalize_exts(None))
        self.assertEqual(crawl.normalize_exts("all"), ("*",))

    def test_scan_tokens(self):
        r = crawl.parse_scan_token("scan:https://a.com/x")
        self.assertEqual((r.url, r.exts), ("https://a.com/x", None))
        r = crawl.parse_scan_token("scan[zip,7z]:a.com/x")
        self.assertEqual((r.url, r.exts), ("https://a.com/x", (".zip", ".7z")))
        self.assertIsNone(crawl.parse_scan_token("https://a.com/x"))
        self.assertIsNone(crawl.parse_scan_token("scanner:https://a.com"))

    def test_finds_downloads_and_ignores_the_rest(self):
        found, title = crawl.find_links(PAGE, "https://elsewhere.example/page")
        self.assertEqual(title, "Game Dumps")
        self.assertEqual(found[:2], ["https://files.example.com/roms/mario.zip", "https://files.example.com/other/zelda.7z"])
        for want in ("https://www.mediafire.com/file/abc/game/file", "https://archive.org/details/some-item",
                     "magnet:?xt=urn:btih:abc123&dn=x", "https://mega.nz/file/AbC#Key123",
                     "https://pixeldrain.com/u/AbCdEf12", "https://files.example.com/roms/sonic.rar",
                     "https://files.example.com/roms/kirby.7z"):
            self.assertIn(want, found)
        for junk in ("https://files.example.com/roms/about.html", "https://www.mediafire.com/", "https://archive.org/about",
                     "https://files.example.com/roms/pic.jpg", "https://www.youtube.com/watch?v=a"):
            self.assertNotIn(junk, found)
        self.assertEqual(len(found), len(set(found)))  # mario.zip was linked twice

    def test_extension_filter_and_any(self):
        only, _ = crawl.find_links(PAGE, "https://x.example/", (".jpg", ".png"))
        self.assertIn("https://files.example.com/roms/pic.jpg", only)
        self.assertIn("https://files.example.com/roms/cover.png", only)
        self.assertNotIn("https://files.example.com/roms/mario.zip", only)
        everything, _ = crawl.find_links(PAGE, "https://x.example/", (crawl.ANY,))
        self.assertIn("https://files.example.com/roms/clip.mp4", everything)
        self.assertNotIn("https://files.example.com/roms/about.html", everything)  # pages are not files
        self.assertNotIn("https://files.example.com/roms/dl.php?id=4", everything)
        videos, _ = crawl.find_links(PAGE, "https://x.example/", None, videos=True)
        self.assertIn("https://www.youtube.com/watch?v=a", videos)

    def test_scan_page_without_a_network(self):
        got, title = crawl.scan_page("https://a.example/p", fetch=lambda url: (PAGE, url))
        self.assertTrue(got and title == "Game Dumps")
        with self.assertRaises(LinkError) as ctx:
            crawl.scan_page("https://a.example/p", (".iso",), fetch=lambda url: ("<p>nothing</p>", url))
        self.assertIn("no iso links", str(ctx.exception))
        with self.assertRaises(LinkError):
            crawl.scan_page("ftp://a.example/p")

        def boom(url):
            raise OSError("down")
        with self.assertRaises(LinkError):
            crawl.scan_page("https://a.example/p", fetch=boom)

    def test_names_never_clash(self):
        urls = ["https://a/x/disc.zip", "https://a/y/disc.zip", "https://a/z.zip", "https://b/disc.zip"]
        names = crawl.unique_names(urls)
        self.assertEqual(names[0], "disc.zip")
        self.assertEqual(names[1], "y/disc.zip")
        self.assertEqual(len({n.lower() for n in names}), 4)


class ResolveScanTests(unittest.TestCase):
    def test_scan_token_lists_the_page_and_sums_up_dead_links(self):
        urls = ["https://a/x/one.zip", "https://a/y/one.zip", "magnet:?xt=urn:btih:abc", "https://www.mediafire.com/file/1/a/file"] \
            + [f"https://drive.google.com/drive/folders/{i}abcdefghij" for i in range(5)]

        def fake_resolve(token):
            if token.startswith("magnet:"):
                return [RemoteFile("m.bin", "torrent:abc:0", 5, "m")]
            if "mediafire" in token:
                return [RemoteFile("file.zip", token, 9, "MediaFire")]
            raise LinkError("Google Drive folder: not here")

        with mock.patch.object(crawl, "scan_page", return_value=(urls, "Page")), \
                mock.patch.object(links, "_resolve_token", side_effect=fake_resolve):
            files, errors = links.resolve_links("scan[zip]:https://p.example/list")
        self.assertEqual([f.name for f in files], ["one.zip", "y/one.zip", "m.bin", "file.zip"])
        self.assertEqual(files[0].group, "Page")
        self.assertEqual(len(errors), 4)  # three shown, then one line for the rest
        self.assertIn("2 more links", errors[-1])

    def test_a_plain_link_still_goes_the_old_way(self):
        with mock.patch.object(links, "_resolve_token", return_value=[RemoteFile("a.zip", "https://h/a.zip", 1)]) as one, \
                mock.patch.object(crawl, "scan_page") as scan:
            files, errors = links.resolve_links("https://h/a.zip")
        self.assertEqual((len(files), errors), (1, []))
        one.assert_called_once()
        scan.assert_not_called()


class PixeldrainTests(unittest.TestCase):
    def test_addresses_and_routing(self):
        self.assertEqual(hosters.hoster_for("https://pixeldrain.com/u/AbCdEf12").name, "Pixeldrain")
        self.assertEqual(hosters.hoster_for("https://pixeldra.in/u/AbCdEf12").name, "Pixeldrain")
        want = "https://pixeldrain.com/api/file/AbCdEf12?download"
        self.assertEqual(hosters.pixeldrain_direct("https://pixeldrain.com/u/AbCdEf12"), want)
        self.assertEqual(hosters.pixeldrain_direct("https://pixeldrain.com/api/file/AbCdEf12?download"), want)
        with self.assertRaises(LinkError):
            hosters.pixeldrain_direct("https://pixeldrain.com/l/ListId01")
        with self.assertRaises(LinkError):
            hosters.pixeldrain_direct("https://pixeldrain.com/")

    def test_lists_are_expanded_and_single_files_are_not(self):
        data = {"success": True, "title": "My roms", "files": [{"id": "aaaa1111", "name": "a.zip", "size": 10},
                                                              {"id": "bbbb2222", "name": "b.7z", "size": "20"}, {"name": "no id"}]}
        got = hosters.pixeldrain_list("https://pixeldrain.com/l/ListId01#item=1", lambda url: data)
        self.assertEqual([(f.name, f.url, f.size, f.group) for f in got],
                         [("a.zip", "https://pixeldrain.com/u/aaaa1111", 10, "My roms"),
                          ("b.7z", "https://pixeldrain.com/u/bbbb2222", 20, "My roms")])
        self.assertIsNone(hosters.pixeldrain_list("https://pixeldrain.com/u/AbCdEf12", lambda url: data))
        with self.assertRaises(LinkError):
            hosters.pixeldrain_list("https://pixeldrain.com/l/ListId01", lambda url: {"success": False, "message": "gone"})
        with self.assertRaises(LinkError):
            hosters.pixeldrain_list("https://pixeldrain.com/l/ListId01", lambda url: {"files": []})


class MultipartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def touch(self, *names):
        for n in names:
            (self.tmp / n).write_bytes(b"x")
        return [self.tmp / n for n in names]

    def fake_extract(self, archive, target, password=""):
        (target / "game.bin").write_bytes(b"data")

    def test_sets_are_found_whatever_their_style(self):
        a = self.touch("g.part1.rar", "g.part2.rar", "g.part10.rar", "other.part1.rar", "g.txt")
        self.assertEqual([p.name for p in postprocess.parts_of(a[0])], ["g.part1.rar", "g.part10.rar", "g.part2.rar"])
        b = self.touch("h.7z.001", "h.7z.002", "h.7z.003")
        self.assertEqual(len(postprocess.parts_of(b[0])), 3)
        c = self.touch("i.rar", "i.r00", "i.r01", "i.s00", "j.r00")
        self.assertEqual({p.name for p in postprocess.parts_of(c[0])}, {"i.rar", "i.r00", "i.r01", "i.s00"})
        d = self.touch("k.zip", "k.z01", "k.z02")
        self.assertEqual({p.name for p in postprocess.parts_of(d[0])}, {"k.zip", "k.z01", "k.z02"})
        (e,) = self.touch("single.zip")
        self.assertEqual(postprocess.parts_of(e), [e])
        self.assertTrue(postprocess.is_multipart(d[0]))

    def test_unpacking_a_set_with_delete_removes_every_part(self):
        parts = self.touch("g.part1.rar", "g.part2.rar", "g.part3.rar", "keep.part1.rar")
        with mock.patch.object(postprocess, "_extract", self.fake_extract):
            res = postprocess.unpack(parts[0], delete=True)
        self.assertTrue(res.ok and res.deleted)
        self.assertEqual(res.parts_deleted, 2)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["game.bin", "keep.part1.rar"])

    def test_a_failed_unpack_keeps_every_part(self):
        parts = self.touch("g.part1.rar", "g.part2.rar")

        def boom(archive, target, password=""):
            raise RuntimeError("Unexpected end of archive")
        with mock.patch.object(postprocess, "_extract", boom):
            res = postprocess.unpack(parts[0], delete=True)
        self.assertFalse(res.ok)
        self.assertTrue(all(p.exists() for p in parts))

    def test_without_delete_nothing_goes(self):
        parts = self.touch("g.part1.rar", "g.part2.rar")
        with mock.patch.object(postprocess, "_extract", self.fake_extract):
            res = postprocess.unpack(parts[0], delete=False)
        self.assertTrue(res.ok and not res.deleted)
        self.assertTrue(all(p.exists() for p in parts))


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.archive = self.tmp / "Release.7z"
        self.archive.write_bytes(b"x")
        self.tried = []

    def opener(self, secret):
        def fake(archive, target, password=""):
            self.tried.append(password)
            (target / "junk.tmp").write_bytes(b"left behind by a wrong try")
            if password != secret:
                raise postprocess.NeedPassword("wrong")
            (target / "game.bin").unlink(missing_ok=True)
            for p in target.iterdir():
                p.unlink()
            (target / "game.bin").write_bytes(b"data")
        return fake

    def test_saved_passwords_are_tried_in_turn(self):
        with mock.patch.object(postprocess, "_extract", self.opener("hunter2")):
            res = postprocess.unpack(self.archive, True, ["nope", "hunter2", "later"])
        self.assertTrue(res.ok and res.used_password and res.deleted)
        self.assertEqual(self.tried, ["", "nope", "hunter2"])  # none first, and it stops at the one that works
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["game.bin"])

    def test_the_archives_own_name_is_tried_too(self):
        with mock.patch.object(postprocess, "_extract", self.opener("Release")):
            res = postprocess.unpack(self.archive, False, [])
        self.assertTrue(res.ok)

    def test_no_password_found_says_so_and_keeps_the_archive(self):
        with mock.patch.object(postprocess, "_extract", self.opener("unknowable")):
            res = postprocess.unpack(self.archive, True, ["a", "b"])
        self.assertFalse(res.ok)
        self.assertIn("none of the", res.note)
        self.assertTrue(self.archive.exists())
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["Release.7z"])  # no staging folder left
        with mock.patch.object(postprocess, "_extract", self.opener("unknowable")):
            res = postprocess.unpack(self.archive, True, [])
        self.assertIn("distill_archive_passwords.txt", res.note)

    def test_an_ordinary_error_stops_at_once(self):
        calls = []

        def boom(archive, target, password=""):
            calls.append(password)
            raise RuntimeError("disk full")
        with mock.patch.object(postprocess, "_extract", boom):
            res = postprocess.unpack(self.archive, False, ["a", "b"])
        self.assertEqual((res.ok, calls), (False, [""]))

    def test_hints_come_from_the_site_a_file_was_downloaded_from(self):
        self.assertEqual(postprocess.password_hints("https://dl.some-site.com/x/Game.rar"),
                         ["www.some-site.com", "some-site.com", "dl.some-site.com", "some-site", "somesite"])
        self.assertEqual(postprocess.password_hints("http://roms.example.co.uk/a.7z")[:2], ["www.example.co.uk", "example.co.uk"])
        self.assertIn("example", postprocess.password_hints("http://roms.example.co.uk/a.7z"))
        for url in ("https://www.mediafire.com/file/a/b/file", "https://archive.org/download/x/y.zip", "https://drive.google.com/uc?id=1",
                    "https://1fichier.com/?abc", "magnet:?xt=urn:btih:abc", "file:///C:/x.torrent", "http://192.168.1.5/a.rar", "torrent:abc:0"):
            self.assertEqual(postprocess.password_hints(url), [], url)

    def test_hints_are_tried_after_saved_passwords_and_before_the_name(self):
        order = postprocess._candidates(self.archive, ["mine"], ["www.site.com", "site.com"])
        self.assertEqual(order, ["", "mine", "www.site.com", "site.com", "Release"])
        with mock.patch.object(postprocess, "_extract", self.opener("site.com")):
            res = postprocess.unpack_all([self.archive], False, lambda m: None, threading.Event(), [],
                                         {self.archive: ["www.site.com", "site.com"]})
        self.assertTrue(res[0].ok and res[0].used_password)
        self.assertEqual(self.tried, ["", "www.site.com", "site.com"])

    def test_password_file(self):
        f = self.tmp / "pw.txt"
        f.write_text("# comment\nalpha\n\n  \nbeta gamma\nalpha\n", encoding="utf-8")
        self.assertEqual(postprocess.load_passwords(f), ["alpha", "beta gamma"])
        self.assertEqual(postprocess.load_passwords(self.tmp / "missing.txt"), [])

    def test_unpack_all_passes_passwords_and_reports_parts(self):
        logs = []
        with mock.patch.object(postprocess, "_extract", self.opener("pw")):
            res = postprocess.unpack_all([self.archive], True, logs.append, threading.Event(), ["pw"])
        self.assertTrue(res[0].ok)
        self.assertTrue(any("with a password" in m for m in logs))


class FolderWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.watch = folderwatch.FolderWatcher(self.tmp)
        self.later = time.time() + 60  # "now", a minute on: every file is old enough to be settled

    def test_a_text_file_is_queued_once_and_moved_aside(self):
        (self.tmp / "links.txt").write_text("hello https://a.com/x.zip, and\nscan[iso]:https://p.example/list\nmagnet:?xt=urn:btih:abc\n")
        (self.tmp / "notes.docx").write_bytes(b"x")
        drops = self.watch.poll(self.later)
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0].links, ("https://a.com/x.zip", "scan[iso]:https://p.example/list", "magnet:?xt=urn:btih:abc"))
        self.assertTrue((self.tmp / "queued" / "links.txt").is_file())
        self.assertFalse((self.tmp / "links.txt").exists())
        self.assertEqual(self.watch.poll(self.later), [])
        (self.tmp / "links.txt").write_text("https://b.com/y.zip")  # a new file with the same name
        again = self.watch.poll(self.later)
        self.assertEqual(again[0].links, ("https://b.com/y.zip",))
        self.assertTrue((self.tmp / "queued" / "links (2).txt").is_file())

    def test_a_torrent_points_at_where_it_now_is(self):
        (self.tmp / "My Game.torrent").write_bytes(b"d4:infod...e")
        (drop,) = self.watch.poll(self.later)
        self.assertEqual(len(drop.links), 1)
        self.assertTrue(drop.links[0].startswith("file:") and drop.links[0].endswith(".torrent"))
        self.assertNotIn(" ", drop.links[0])  # a pasted list is split on spaces
        self.assertIn("queued", drop.links[0])
        self.assertTrue((self.tmp / "queued" / "My Game.torrent").is_file())

    def test_unsettled_and_empty_files_are_left_alone(self):
        (self.tmp / "new.txt").write_text("https://a.com/x.zip")
        self.assertEqual(self.watch.poll(time.time()), [])  # just written: maybe still copying
        self.assertTrue((self.tmp / "new.txt").exists())
        self.assertEqual(len(self.watch.poll(self.later)), 1)
        (self.tmp / "empty.txt").write_text("no links here")
        self.assertEqual(self.watch.poll(self.later), [])
        self.assertTrue((self.tmp / "empty.txt").exists())  # left where it is, and not looked at again
        self.assertEqual(self.watch.poll(self.later), [])

    def test_a_missing_folder_is_not_an_error(self):
        self.assertEqual(folderwatch.FolderWatcher(self.tmp / "nope").poll(self.later), [])


if __name__ == "__main__":
    unittest.main()
