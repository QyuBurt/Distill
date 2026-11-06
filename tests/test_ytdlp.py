import os
import stat
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

from distill import downloads as dl, ytdlp
from distill.downloads import RemoteFile


class ParseTests(unittest.TestCase):
    def test_routing(self):
        self.assertTrue(ytdlp.is_video_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(ytdlp.is_video_url("https://m.soundcloud.com/a/b"))
        self.assertTrue(ytdlp.is_video_url("yt:https://example.com/odd"))
        self.assertFalse(ytdlp.is_video_url("https://example.com/video.mp4"))
        self.assertFalse(ytdlp.is_video_url("https://notyoutube.com/x"))
        self.assertEqual(ytdlp.strip_prefix("yt:https://a/b"), "https://a/b")

    def test_single_video_and_playlist(self):
        one = ytdlp.parse_listing({"id": "abc", "title": "Song", "webpage_url": "https://y/watch?v=abc", "filesize_approx": 5000}, "u")
        self.assertEqual((one[0].name, one[0].url, one[0].size, one[0].group), ("Song [abc]", "ytdlp:https://y/watch?v=abc", 5000, ""))
        pl = ytdlp.parse_listing({"_type": "playlist", "title": "Mix", "entries": [
            {"id": "a1", "title": "One", "url": "https://y/watch?v=a1"},
            {"id": "b2", "title": "Two", "ie_key": "Youtube"},
            {"id": "c3", "title": "No link"}]}, "u")
        self.assertEqual([(r.name, r.group) for r in pl], [("One [a1]", "Mix"), ("Two [b2]", "Mix")])
        self.assertTrue(pl[1].url.endswith("watch?v=b2"))
        with self.assertRaises(ytdlp.YtdlpError):
            ytdlp.parse_listing({"entries": []}, "u")

    def test_progress_and_final_path(self):
        self.assertEqual(ytdlp.parse_progress("dlp|1000|4000|NA|250.5\n"), (1000, 4000, 250.5))
        self.assertEqual(ytdlp.parse_progress("dlp|1000|NA|8000|NA"), (1000, 8000, None))
        self.assertIsNone(ytdlp.parse_progress("[download] 50%"))
        self.assertEqual(ytdlp.final_path("dlpath|/x/y.mp4\n"), Path("/x/y.mp4"))

    def test_command_escapes_percent_and_limits_rate(self):
        cmd = ytdlp.command(Path("yt-dlp"), "https://y/v", Path("/d/100% Pure [x]"), "Up to 720p", 500)
        self.assertIn("/d/100%% Pure [x].%(ext)s".replace("/", os.sep), cmd)
        self.assertIn("500K", cmd)
        self.assertEqual(cmd[-2:], ["--", "https://y/v"])
        self.assertIn("bv*[height<=720]+ba/b[height<=720]", cmd)

    def test_choices_read_label_and_become_arguments(self):
        self.assertEqual(ytdlp.parse_choice("mp4:1080"), ("mp4", 1080))
        self.assertEqual(ytdlp.parse_choice("MP3:best"), ("mp3", 0))
        self.assertEqual(ytdlp.parse_choice("mp4"), ("mp4", 0))
        for bad in ("", "avi:720", "mp4:fast", "mp3:-5"):
            self.assertIsNone(ytdlp.parse_choice(bad), bad)
        self.assertEqual(ytdlp.make_choice("mp3", 320), "mp3:320")
        self.assertEqual(ytdlp.make_choice("mp4", 0), "mp4:best")
        self.assertEqual([ytdlp.choice_label(c) for c in ("mp4:1080", "mp4:best", "mp3:128", "")],
                         ["MP4 1080p", "MP4 best", "MP3 128 kbps", ""])
        self.assertEqual(ytdlp.choice_args(""), [])
        mp3 = ytdlp.choice_args("mp3:192")
        self.assertIn("-x", mp3)
        self.assertEqual(mp3[mp3.index("--audio-format") + 1], "mp3")
        self.assertEqual(mp3[mp3.index("--audio-quality") + 1], "192K")
        mp4 = ytdlp.choice_args("mp4:720")
        self.assertEqual(mp4[mp4.index("-f") + 1], "bv*[height<=720]+ba/b[height<=720]")
        self.assertEqual(mp4[mp4.index("--merge-output-format") + 1], "mp4")
        self.assertEqual(ytdlp.choice_args("mp4:best")[1], "bv*+ba/b")  # no height limit

    def test_a_rows_choice_wins_over_the_settings_quality(self):
        plain = ytdlp.command(Path("yt-dlp"), "https://y/v", Path("/d/x"), "Up to 720p")
        self.assertIn("bv*[height<=720]+ba/b[height<=720]", plain)
        own = ytdlp.command(Path("yt-dlp"), "https://y/v", Path("/d/x"), "Up to 720p", choice="mp3:320")
        self.assertIn("--audio-format", own)
        self.assertNotIn("bv*[height<=720]+ba/b[height<=720]", own)
        self.assertEqual(own[-2:], ["--", "https://y/v"])

    def test_picker_starts_from_what_the_settings_quality_means(self):
        self.assertEqual(ytdlp.preset_choice("Best quality"), "mp4:best")
        self.assertEqual(ytdlp.preset_choice("Up to 1080p"), "mp4:1080")
        self.assertEqual(ytdlp.preset_choice("Audio only"), "mp3:320")

    def test_qualities_offered_follow_what_the_video_has(self):
        self.assertEqual(ytdlp.mp4_options([1080, 720, 360]), [1080, 720, 480, 360, 240, 144])
        self.assertEqual(ytdlp.mp4_options([800])[0], 800)  # its own top height, when not a usual step
        self.assertEqual(ytdlp.mp4_options(None), list(ytdlp.MP4_STEPS))
        data = {"formats": [{"height": 720, "vcodec": "avc1"}, {"height": 1080, "vcodec": "vp9"},
                            {"height": None, "vcodec": "none"}, {"height": 2160, "vcodec": "none"}, "junk"]}
        self.assertEqual(ytdlp.parse_heights(data), [1080, 720])  # audio-only streams are not heights
        out = mock.Mock(returncode=0, stdout='{"formats":[{"height":480,"vcodec":"x"}]}', stderr="")
        self.assertEqual(ytdlp.probe_heights("ytdlp:https://y/v", Path("yt-dlp"), run=lambda *a, **k: out), [480])
        bad = mock.Mock(returncode=1, stdout="", stderr="ERROR: x")
        with self.assertRaises(ytdlp.YtdlpError):
            ytdlp.probe_heights("https://y/v", Path("yt-dlp"), run=lambda *a, **k: bad)

    def test_missing_ffmpeg_is_not_retried(self):
        self.assertTrue(ytdlp.is_permanent_failure("ERROR: Postprocessing: ffprobe and ffmpeg not found."))

    def test_permanent_failures(self):
        self.assertTrue(ytdlp.is_permanent_failure("Private video. Sign in if you've been granted access"))
        self.assertFalse(ytdlp.is_permanent_failure("HTTP Error 503"))

    def test_listing_through_a_fake_run(self):
        out = mock.Mock(returncode=0, stdout='{"id":"z","title":"T","webpage_url":"https://y/z"}', stderr="")
        rows = ytdlp.list_entries("https://youtube.com/watch?v=z", Path("yt-dlp"), run=lambda *a, **k: out)
        self.assertEqual(rows[0].name, "T [z]")
        bad = mock.Mock(returncode=1, stdout="", stderr="ERROR: Video unavailable\n")
        with self.assertRaises(ytdlp.YtdlpError) as cm:
            ytdlp.list_entries("https://youtube.com/watch?v=z", Path("yt-dlp"), run=lambda *a, **k: bad)
        self.assertIn("Video unavailable", str(cm.exception))


@unittest.skipIf(sys.platform == "win32", "uses a shell-script stand-in for yt-dlp")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.exe = self.tmp / "fake-yt-dlp"
        self.exe.write_text(textwrap.dedent(f'''\
            #!{sys.executable}
            import sys, os
            args = sys.argv[1:]
            open({str(self.tmp / "args.txt")!r}, "w").write("\\n".join(args))
            out = args[args.index("-o") + 1].replace("%(ext)s", "mp4").replace("%%", "%")
            if "FAIL" in args[-1]:
                print("ERROR: Private video. Sign in", flush=True); sys.exit(1)
            for n in (1000, 2000, 3000):
                print(f"dlp|{{n}}|3000|NA|1000", flush=True)
            open(out, "wb").write(b"x" * 3000)
            print("dlpath|" + out, flush=True)
            '''))
        self.exe.chmod(self.exe.stat().st_mode | stat.S_IEXEC)
        p = mock.patch.object(ytdlp, "find_exe", return_value=self.exe)
        p.start()
        self.addCleanup(p.stop)

    def run_one(self, url, name="Song [abc]"):
        events = []
        rf = RemoteFile(name, "ytdlp:" + url, None, "", None, False, True, "yt-dlp")
        runner = dl.DownloadRunner([(0, rf)], self.tmp / "out", 1, False, False, events.append, threading.Event())
        runner.pause_event.set()
        runner.run()
        return runner, events

    def test_downloads_and_reports_progress(self):
        runner, events = self.run_one("https://y/ok")
        self.assertEqual((runner.done, runner.failures), (1, []))
        self.assertEqual((self.tmp / "out" / "Song [abc].mp4").stat().st_size, 3000)
        rows = [str(e[2]) for e in events if e[0] == "dl_row"]
        self.assertTrue(any("/" in r for r in rows), rows)  # 'got / total' progress text
        self.assertTrue(any(e[0] == "dl_speed" and e[2] for e in events))

    def test_the_rows_choice_reaches_yt_dlp_and_a_different_format_is_not_skipped(self):
        self.run_one("https://y/ok")
        rf = RemoteFile("Song [abc]", "ytdlp:https://y/ok", None, "", None, False, True, "yt-dlp", "mp3:128")
        events = []
        runner = dl.DownloadRunner([(0, rf)], self.tmp / "out", 1, False, False, events.append, threading.Event())
        runner.pause_event.set()
        runner.run()
        self.assertEqual((runner.done, runner.skipped), (1, 0))  # the .mp4 that is there is not the MP3 asked for
        args = (self.tmp / "args.txt").read_text().split("\n")
        self.assertEqual(args[args.index("--audio-quality") + 1], "128K")

    def test_second_run_skips_and_a_private_video_fails_without_retries(self):
        self.run_one("https://y/ok")
        runner, _e = self.run_one("https://y/ok")
        self.assertEqual((runner.done, runner.skipped), (0, 1))
        runner, events = self.run_one("https://y/FAIL", "Other [zzz]")
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("Private video", runner.failures[0][2])
        self.assertFalse(any(e[0] == "dl_row" and "Retrying" in str(e[2]) for e in events))


if __name__ == "__main__":
    unittest.main()
