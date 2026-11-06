from __future__ import annotations

import hashlib
import shutil
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import distill.downloads as dl
from distill.downloads import (DownloadRunner, LinkError, RemoteFile, describe_error, list_open_directory,
                                parse_archive_url, site_for)
from tests.helpers import POSIX_ONLY, TempDirTest, web_server

DATA = bytes(range(256)) * 400  # 102,400 bytes


def run_downloads(jobs, dest: Path, **kw):
    events: list[tuple] = []
    pause = threading.Event()
    pause.set()
    runner = DownloadRunner(jobs, dest, kw.pop("threads", 2), kw.pop("verify", True), kw.pop("per_item", False),
                            events.append, pause)
    runner.run()
    return runner, events


class UrlTests(unittest.TestCase):
    def test_archive_item_links(self):
        self.assertEqual(parse_archive_url("https://archive.org/details/some-item"), ("some-item", ""))
        self.assertEqual(parse_archive_url("archive.org/download/item/sub%20dir/file.zip"), ("item", "sub dir/file.zip"))
        self.assertIsNone(parse_archive_url("https://example.com/details/x"))

    def test_an_archive_link_without_an_item_is_explained(self):
        with self.assertRaises(LinkError):
            parse_archive_url("https://archive.org/")

    def test_site_matching_is_by_host_not_by_substring(self):
        with mock.patch.object(dl, "LOGINS", {}):
            self.assertEqual(site_for("https://archive.org/download/x/y"), "archive.org")
            self.assertEqual(site_for("https://ia800.us.archive.org/f"), "archive.org")
            self.assertEqual(site_for("https://1fichier.com/?abc"), "1fichier")
            self.assertEqual(site_for("https://evil-archive.org.example.com/"), "")
            self.assertEqual(site_for("https://notarchive.org/"), "")

    def test_describe_error(self):
        err = urllib.error.HTTPError("http://x/y", 404, "nf", {}, None)
        self.assertEqual(describe_error(err), "HTTP 404 - not found")
        self.assertIn("rate limited", describe_error(urllib.error.HTTPError("http://x/y", 429, "", {}, None)))
        self.assertEqual(describe_error(TimeoutError()), "Timed out")
        self.assertTrue(describe_error(urllib.error.URLError("dns")).startswith("Network error"))


class ListingTests(unittest.TestCase):
    PAGE = ('<html><body><a href="?C=N;O=D">Name</a><a href="../">Parent</a><a href="game%20one.chd">one</a>'
            '<a href="sub/">sub</a><a href="#top">top</a><a href="mailto:a@b.c">m</a><a href="two.cue">two</a></body></html>')

    def test_open_directory_lists_only_files_beside_it(self):
        with web_server(pages={"/roms/": self.PAGE}) as (base, _h):
            files = list_open_directory(base + "/roms/")
        self.assertEqual(sorted(f.name for f in files), ["game one.chd", "two.cue"])
        self.assertTrue(all(f.url.startswith(base + "/roms/") for f in files))

    def test_a_direct_file_link_is_a_single_preselected_file(self):
        (only,) = list_open_directory("http://example.invalid/files/game.chd")
        self.assertEqual((only.name, only.preselect), ("game.chd", True))

    def test_not_a_web_link(self):
        with self.assertRaises(LinkError):
            list_open_directory("ftp://example.com/x/")


class DownloadRunnerTests(TempDirTest):
    def rf(self, base: str, name: str = "file.bin", data: bytes = DATA, **kw) -> RemoteFile:
        return RemoteFile(name, f"{base}/{name}", len(data), md5=kw.pop("md5", hashlib.md5(data).hexdigest()), **kw)

    def test_downloads_and_verifies(self):
        with web_server({"/file.bin": DATA}) as (base, _h):
            runner, events = run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertEqual((runner.done, runner.failures), (1, []))
        self.assertEqual((self.tmp / "dl" / "file.bin").read_bytes(), DATA)
        self.assertFalse(list((self.tmp / "dl").glob("*.part")))
        self.assertEqual(events[-1][0], "dl_done")
        self.assertEqual((events[-1][1].total, events[-1][1].done), (1, 1))

    def test_resumes_a_partial_file_with_a_range_request(self):
        part = self.tmp / "dl" / "file.bin.part"
        part.parent.mkdir()
        part.write_bytes(DATA[:30_000])
        with web_server({"/file.bin": DATA}) as (base, handler):
            runner, _events = run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertEqual(runner.failures, [])
        self.assertEqual((self.tmp / "dl" / "file.bin").read_bytes(), DATA)
        self.assertIn(("/file.bin", "bytes=30000-"), handler.hits)
        self.assertLess(runner.bytes_received, len(DATA))  # only the missing part came over the wire

    def test_an_existing_complete_file_is_skipped(self):
        (self.tmp / "dl").mkdir()
        (self.tmp / "dl" / "file.bin").write_bytes(DATA)
        with web_server({"/file.bin": DATA}) as (base, handler):
            runner, _events = run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertEqual((runner.skipped, runner.done), (1, 0))
        self.assertEqual(handler.hits, [])

    def test_a_wrong_checksum_is_rejected_and_never_left_behind(self):
        with web_server({"/file.bin": DATA}) as (base, _h), mock.patch.object(dl, "DL_ATTEMPTS", 1):
            runner, _events = run_downloads([(0, self.rf(base, md5="0" * 32))], self.tmp / "dl")
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("Checksum", runner.failures[0][2])
        self.assertFalse((self.tmp / "dl" / "file.bin").exists())
        self.assertFalse((self.tmp / "dl" / "file.bin.part").exists())

    def test_a_second_corrupt_copy_stops_the_retries(self):
        """Two copies with the wrong checksum mean the listing is wrong, not the network: give up, don't loop."""
        with web_server({"/file.bin": DATA}) as (base, handler):
            runner, _events = run_downloads([(0, self.rf(base, md5="0" * 32))], self.tmp / "dl")
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("Checksum", runner.failures[0][2])
        self.assertEqual(len(handler.hits), 2)
        self.assertFalse((self.tmp / "dl" / "file.bin").exists())

    def test_404_is_reported_without_retrying_forever(self):
        with web_server({}) as (base, handler):
            runner, _events = run_downloads([(0, self.rf(base, "missing.bin"))], self.tmp / "dl")
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("404", runner.failures[0][2])
        self.assertEqual(len(handler.hits), 1)

    def test_unsafe_names_never_leave_the_destination(self):
        with web_server({"/x": DATA}) as (base, handler):
            jobs = [(i, RemoteFile(name, f"{base}/x", len(DATA))) for i, name in
                    enumerate(["../escape.bin", "a/../../escape.bin", "/abs/../../x.bin"])]
            runner, _events = run_downloads(jobs, self.tmp / "dl", verify=False)
        self.assertEqual(len(runner.failures), 3)
        self.assertEqual(handler.hits, [])
        self.assertFalse((self.tmp / "escape.bin").exists())

    @POSIX_ONLY
    def test_a_symlink_inside_the_destination_cannot_be_used_to_escape(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.tmp / "dl").mkdir()
        (self.tmp / "dl" / "evil").symlink_to(outside, target_is_directory=True)
        with web_server({"/x": DATA}) as (_base, handler):
            jobs = [(0, RemoteFile("evil/pwned.bin", _base + "/x", len(DATA)))]
            runner, _events = run_downloads(jobs, self.tmp / "dl", verify=False)
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("Unsafe", runner.failures[0][2])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(handler.hits, [])

    def test_per_item_folders(self):
        with web_server({"/file.bin": DATA}) as (base, _h):
            runner, _events = run_downloads([(0, self.rf(base, group="Some: Item"))], self.tmp / "dl", per_item=True)
        self.assertEqual(runner.failures, [])
        self.assertTrue((self.tmp / "dl" / "Some_ Item" / "file.bin").is_file())

    def test_several_files_in_parallel(self):
        files = {f"/f{i}.bin": DATA[: 10_000 + i] for i in range(6)}
        with web_server(files) as (base, _h):
            jobs = [(i, self.rf(base, p[1:], d)) for i, (p, d) in enumerate(files.items())]
            runner, _events = run_downloads(jobs, self.tmp / "dl", threads=3)
        self.assertEqual((runner.done, runner.failures), (6, []))

    def test_stop_before_start_downloads_nothing_and_still_signals_done(self):
        with web_server({"/file.bin": DATA}) as (base, handler):
            events: list[tuple] = []
            pause = threading.Event()
            pause.set()
            runner = DownloadRunner([(0, self.rf(base))], self.tmp / "dl", 1, True, False, events.append, pause)
            runner.shutdown()
            runner.run()
        self.assertEqual(handler.hits, [])
        self.assertEqual(events[-1][0], "dl_done")


if __name__ == "__main__":
    unittest.main()


class RulesAndDebridTests(TempDirTest):
    """Per-site limits, the speed limit and debrid unlocking, through the real runner and a local server."""

    rf = DownloadRunnerTests.rf

    def setUp(self):
        super().setUp()
        from distill import netrules
        self.nr = netrules
        saved = (netrules.RULES.global_kbps, netrules.RULES.schedule, netrules.RULES.hosts)
        self.addCleanup(lambda: (setattr(netrules.RULES, "global_kbps", saved[0]), setattr(netrules.RULES, "schedule", saved[1]),
                                 setattr(netrules.RULES, "hosts", saved[2])))

    def test_per_site_cap_holds_while_other_work_goes_on(self):
        self.nr.RULES.hosts = [self.nr.HostRule("127.0.0.1", max_files=1)]
        seen = {"now": 0, "max": 0}
        lock = threading.Lock()
        real = dl.GATE
        orig_acq, orig_rel = real.try_acquire, real.release

        def acquire(host):
            ok = orig_acq(host)
            if ok:
                with lock:
                    seen["now"] += 1
                    seen["max"] = max(seen["max"], seen["now"])
            return ok

        def release(host):
            with lock:
                seen["now"] -= 1
            orig_rel(host)
        real.try_acquire, real.release = acquire, release
        self.addCleanup(lambda: (setattr(real, "try_acquire", orig_acq), setattr(real, "release", orig_rel)))
        files = {f"/f{i}.bin": DATA[: 20_000 + i] for i in range(4)}
        with web_server(files) as (base, _h):
            jobs = [(i, self.rf(base, p[1:], d)) for i, (p, d) in enumerate(files.items())]
            runner, _e = run_downloads(jobs, self.tmp / "dl", threads=4)
        self.assertEqual((runner.done, runner.failures), (4, []))
        self.assertEqual(seen["max"], 1)

    def test_speed_limit_slows_the_download(self):
        import time
        old = dl.DL_CHUNK
        dl.DL_CHUNK = 8192
        self.addCleanup(lambda: setattr(dl, "DL_CHUNK", old))
        self.nr.RULES.global_kbps = 200  # 100 KB at 200 KB/s: about half a second
        with web_server({"/file.bin": DATA}) as (base, _h):
            t0 = time.monotonic()
            runner, _e = run_downloads([(0, self.rf(base))], self.tmp / "dl")
            took = time.monotonic() - t0
        self.assertEqual((runner.done, runner.failures), (1, []))
        self.assertGreater(took, 0.35)

    def test_a_debrid_service_swaps_in_the_direct_link(self):
        from distill import debrid

        class Fake(debrid.Provider):
            key, name, site = "realdebrid", "Fake", "real-debrid.com"
            def _load_hosts(self): return ["127.0.0.1"]
            def unlock(self, url): return debrid.Unlocked(url.replace("/hoster/abc", "/real.bin"))

        saved = dict(dl.LOGINS)
        dl.LOGINS["realdebrid"] = {"apikey": "t"}
        debrid._LIVE[("realdebrid", "t")] = Fake("t")

        def undo():
            dl.LOGINS.clear()
            dl.LOGINS.update(saved)
            debrid._LIVE.clear()
        self.addCleanup(undo)
        with web_server({"/real.bin": DATA}) as (base, _h):
            rf = RemoteFile("real.bin", f"{base}/hoster/abc", len(DATA), md5=hashlib.md5(DATA).hexdigest())
            runner, _e = run_downloads([(0, rf)], self.tmp / "dl")
        self.assertEqual((runner.done, runner.failures), (1, []))
        self.assertEqual((self.tmp / "dl" / "real.bin").read_bytes(), DATA)


class SegmentedTests(TempDirTest):
    BIG = bytes(range(256)) * 1000  # 256,000 bytes

    def setUp(self):
        super().setUp()
        from distill import netrules
        self.nr = netrules
        saved = (netrules.RULES.segments, netrules.RULES.hosts)
        old = (dl.SEG_BLOCK, dl.SEG_MIN)
        dl.SEG_BLOCK, dl.SEG_MIN = 64_000, 100_000  # small blocks so a small file is split
        netrules.RULES.segments = 3
        self.addCleanup(lambda: (setattr(netrules.RULES, "segments", saved[0]), setattr(netrules.RULES, "hosts", saved[1]),
                                 setattr(dl, "SEG_BLOCK", old[0]), setattr(dl, "SEG_MIN", old[1])))

    def rf(self, base, data=None):
        data = data or self.BIG
        return RemoteFile("big.bin", f"{base}/big.bin", len(data), md5=hashlib.md5(data).hexdigest())

    def test_split_download_is_identical_and_uses_ranges(self):
        with web_server({"/big.bin": self.BIG}) as (base, h):
            runner, _e = run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertEqual((runner.done, runner.failures), (1, []))
        self.assertEqual((self.tmp / "dl" / "big.bin").read_bytes(), self.BIG)
        self.assertEqual(sorted(r for _p, r in h.hits),
                         sorted(["bytes=0-63999", "bytes=64000-127999", "bytes=128000-191999", "bytes=192000-255999"]))
        self.assertEqual(list((self.tmp / "dl").glob("*.part*")), [])

    def test_resume_only_fetches_the_missing_blocks(self):
        import json
        dest = self.tmp / "dl"
        dest.mkdir()
        part = dest / "big.bin.part"
        body = bytearray(len(self.BIG))
        body[:128_000] = self.BIG[:128_000]  # blocks 0 and 1 are already here
        part.write_bytes(bytes(body))
        (dest / "big.bin.part.segs").write_text(json.dumps({"size": len(self.BIG), "block": 64_000, "done": [0, 1]}))
        with web_server({"/big.bin": self.BIG}) as (base, h):
            runner, _e = run_downloads([(0, self.rf(base))], dest)
        self.assertEqual((runner.done, runner.failures), (1, []))
        self.assertEqual((dest / "big.bin").read_bytes(), self.BIG)
        self.assertEqual(sorted(r for _p, r in h.hits), ["bytes=128000-191999", "bytes=192000-255999"])

    def test_server_without_ranges_falls_back_to_one_connection(self):
        with web_server({"/big.bin": self.BIG}) as (base, h):
            orig = h.do_GET

            def no_ranges(self_):  # a server that ignores Range and sends everything
                self_.headers.replace_header("Range", "") if self_.headers.get("Range") else None
                orig(self_)
            h.do_GET = no_ranges
            runner, events = run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertEqual((runner.done, runner.failures), (1, []))
        self.assertEqual((self.tmp / "dl" / "big.bin").read_bytes(), self.BIG)
        self.assertIn("one connection", "\n".join(str(e[1]) for e in events if e[0] == "log"))

    def test_off_by_default_and_per_site_override(self):
        self.nr.RULES.segments = 1
        with web_server({"/big.bin": self.BIG}) as (base, h):
            run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertEqual([r for _p, r in h.hits], [""])  # one plain request
        shutil.rmtree(self.tmp / "dl")
        self.nr.RULES.hosts = [self.nr.HostRule("127.0.0.1", segments=2)]
        with web_server({"/big.bin": self.BIG}) as (base, h):
            run_downloads([(0, self.rf(base))], self.tmp / "dl")
        self.assertGreater(len(h.hits), 1)


class PriorityTests(unittest.TestCase):
    def test_prioritize_moves_waiting_files_to_the_front(self):
        runner = dl.DownloadRunner([(i, RemoteFile(f"f{i}", f"http://x/{i}", 1)) for i in range(5)], Path("."), 1, False, False,
                                   lambda _e: None, threading.Event())
        self.assertEqual(runner.prioritize([3, 4, 99]), 2)
        self.assertEqual([j[0] for j in runner._queue], [3, 4, 0, 1, 2])
        self.assertEqual(runner.prioritize([]), 0)


class PostActionTests(TempDirTest):
    def test_archives_are_unpacked_and_deleted_after_the_run(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Game.gba", b"rom-bytes")
        data = buf.getvalue()
        with web_server({"/Game.zip": data}) as (base, _h):
            rf = RemoteFile("Game.zip", f"{base}/Game.zip", len(data))
            events: list = []
            runner = dl.DownloadRunner([(0, rf)], self.tmp / "dl", 1, False, False, events.append, threading.Event())
            runner.pause_event.set()
            runner.post_action = "extract_delete"
            runner.run()
        self.assertEqual(runner.done, 1)
        self.assertEqual((self.tmp / "dl" / "Game.gba").read_bytes(), b"rom-bytes")
        self.assertFalse((self.tmp / "dl" / "Game.zip").exists())
        self.assertTrue(any(e[0] == "log" and "Unpacking 1 of 1" in str(e[1]) for e in events))


class DebridTorrentRunnerTests(TempDirTest):
    def test_cached_files_download_over_https_and_the_rest_go_to_the_swarm(self):
        from distill import debrid
        from distill.torrent_runners import DebridTorrentRunner

        class Fake(debrid.Provider):
            key, name, site = "premiumize", "Fake", "premiumize.me"
            torrent_api = True

            def __init__(self, base):
                super().__init__("t")
                self.base = base

            def torrent_cached(self, hashes): return {h for h in hashes if h.startswith("a")}
            def torrent_open(self, ih, magnet): return debrid.TorrentHandle(ih, [debrid.DebridFile("0", "Movie/a.bin", len(DATA))])
            def torrent_link(self, handle, file_id): return f"{self.base}/a.bin"

        saved = dict(dl.LOGINS)
        dl.LOGINS["premiumize"] = {"apikey": "t"}
        self.addCleanup(lambda: (dl.LOGINS.clear(), dl.LOGINS.update(saved), debrid._LIVE.clear()))
        with web_server({"/a.bin": DATA}) as (base, _h):
            fake = Fake(base)
            debrid._LIVE[("premiumize", "t")] = fake
            ih = "a" * 40
            jobs = [(0, RemoteFile("Movie/a.bin", f"torrent:{ih}:0", len(DATA), "Movie"))]
            events: list = []
            runner = DebridTorrentRunner(jobs, self.tmp / "dl", 2, events.append, threading.Event(), {}, fake)
            runner.pause_event.set()
            runner.run()
        self.assertEqual((runner.done, runner.failures, runner.from_cache), (1, [], 1))
        self.assertEqual((self.tmp / "dl" / "Movie" / "a.bin").read_bytes(), DATA)
        self.assertTrue(any(e[0] == "dl_source" and e[2] == "Fake" for e in events))
