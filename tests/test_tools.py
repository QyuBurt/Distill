from __future__ import annotations

import io
import json
import re
import unittest
import zipfile
from unittest import mock

import distill.tools as tools
from distill.tools import PkgExtractorManager, mame_version
from tests.helpers import TempDirTest

EXE = b"MZ" + b"\0" * 70_000


def release(tag: str, *assets: str, prerelease: bool = False) -> dict:
    return {"tag_name": tag, "prerelease": prerelease, "draft": False,
            "assets": [{"name": n, "browser_download_url": f"https://example.invalid/{tag}/{n}", "size": 123,
                        "digest": "sha256:" + "ab" * 32} for n in assets]}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AssetChoiceTests(unittest.TestCase):
    pick = staticmethod(PkgExtractorManager._windows_asset)

    @staticmethod
    def assets(*names):
        return [{"name": n} for n in names]

    def test_bare_exe_beats_everything_else(self):
        got = self.pick(self.assets("pkg_extractor.AppImage", "install_pkg.sh", "install_pkg.bat", "pkg_extractor.exe"))
        self.assertEqual(got["name"], "pkg_extractor.exe")

    def test_windows_zip_is_preferred_to_the_linux_one(self):
        got = self.pick(self.assets("pkg_extractor_linux.zip", "pkg_extractor_windows.zip"))
        self.assertEqual(got["name"], "pkg_extractor_windows.zip")

    def test_nothing_usable(self):
        self.assertIsNone(self.pick(self.assets("install_pkg.bat", "pkg_extractor.AppImage", "source.tar.gz")))
        self.assertIsNone(self.pick([]))


class PkgExtractorManagerTests(TempDirTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(tools, "APP_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict("os.environ", {"LOCALAPPDATA": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events: list[tuple] = []
        self.mgr = PkgExtractorManager(self.events.append)

    def test_lives_under_the_app_folder(self):
        self.assertEqual(self.mgr.root, self.tmp / "tools" / "pkg-extractor")
        self.assertTrue(self.mgr.root.is_dir())

    def test_labels_and_tags(self):
        self.assertEqual(self.mgr._label("PKG_EXTRACTOR_1_1"), "v1.1")
        tag = re.compile(self.mgr.TAG_RE)
        self.assertTrue(tag.fullmatch("PKG_EXTRACTOR_1_0") and tag.fullmatch("PKG_EXTRACTOR_2_10_3"))
        self.assertFalse(tag.fullmatch("ShadPS4Plus_0_12_0"))

    def test_picks_the_newest_extractor_release_not_an_emulator_build(self):
        listing = [release("SHADPS4PLUS_0_13", "shadps4plus-win.zip"),
                   release("PKG_EXTRACTOR_1_2", "pkg_extractor.exe", prerelease=True),
                   release("PKG_EXTRACTOR_1_1", "pkg_extractor.AppImage", "pkg_extractor.exe", "install_pkg.bat"),
                   release("PKG_EXTRACTOR_1_0", "pkg_extractor.exe")]
        with mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(listing).encode())):
            rel = self.mgr._latest_release()
        self.assertEqual((rel.tag, rel.name), ("PKG_EXTRACTOR_1_1", "pkg_extractor.exe"))
        self.assertEqual(rel.sha256, "ab" * 32)
        self.assertTrue(rel.url.endswith("PKG_EXTRACTOR_1_1/pkg_extractor.exe"))

    def test_no_windows_package_is_a_clear_error(self):
        listing = [release("PKG_EXTRACTOR_1_1", "pkg_extractor.AppImage", "install_pkg.sh")]
        with mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(listing).encode())):
            with self.assertRaises(RuntimeError):
                self.mgr._latest_release()

    def test_unpacks_a_bare_exe(self):
        archive = self.write("dl/pkg_extractor.exe", EXE)
        tmp, dest = self.tmp / "tmp", self.mgr.root / "PKG_EXTRACTOR_1_1"
        tmp.mkdir()
        self.mgr._unpack(archive, tmp, dest)
        self.assertEqual((dest / "pkg_extractor.exe").read_bytes(), EXE)

    def test_unpacks_a_zip_keeping_companion_files(self):
        archive = self.tmp / "dl.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("bundle/Pkg_Extractor.exe", EXE)
            z.writestr("bundle/helper.dll", b"MZ")
            z.writestr("bundle/install_pkg.bat", "@echo off")
        tmp, dest = self.tmp / "tmp", self.mgr.root / "PKG_EXTRACTOR_1_1"
        tmp.mkdir()
        self.mgr._unpack(archive, tmp, dest)
        self.assertTrue((dest / "pkg_extractor.exe").is_file())  # the app always looks for this one name
        self.assertTrue((dest / "helper.dll").is_file())

    def test_a_zip_without_the_program_is_an_error(self):
        archive = self.tmp / "bad.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("readme.txt", "nothing here")
        (self.tmp / "tmp").mkdir()
        with self.assertRaises(RuntimeError):
            self.mgr._unpack(archive, self.tmp / "tmp", self.mgr.root / "x")

    def test_probe_rejects_things_that_are_not_programs(self):
        good = self.write("good.exe", EXE)
        text = self.write("bad.exe", b"hello" * 20_000)
        tiny = self.write("tiny.exe", b"MZ" + b"\0" * 10)
        self.assertEqual(self.mgr._probe(good), "ok")
        for bad in (text, tiny):
            with self.assertRaises(RuntimeError):
                self.mgr._probe(bad)

    def test_a_hand_placed_copy_is_found(self):
        self.assertIsNone(self.mgr._local_copy())
        exe = self.write("tools/pkg-extractor/pkg_extractor.exe", EXE)
        self.assertEqual(self.mgr._local_copy(), exe)

    def test_cached_copy_needs_a_matching_tag_and_a_file(self):
        self.assertIsNone(self.mgr.cached())
        (self.mgr.root / "current.txt").write_text("PKG_EXTRACTOR_1_1")
        self.assertIsNone(self.mgr.cached())  # named, but not on disk
        exe = self.write("tools/pkg-extractor/PKG_EXTRACTOR_1_1/pkg_extractor.exe", EXE)
        self.assertEqual(self.mgr.cached(), ("PKG_EXTRACTOR_1_1", exe))
        (self.mgr.root / "current.txt").write_text("../../evil")
        self.assertIsNone(self.mgr.cached())  # a tag that doesn't match the pattern is never followed


class MameTests(unittest.TestCase):
    def test_version_label(self):
        self.assertEqual(mame_version("mame0278"), "0.278")


if __name__ == "__main__":
    unittest.main()


class DownloadToolTests(TempDirTest):
    """yt-dlp and ffmpeg: one folder (./tools/ytdlp), loose files, remembered version, busy events."""

    def manager(self, cls):
        events = []
        with mock.patch.object(cls, "_pick_root", return_value=self.tmp):
            return cls(events.append), events

    def test_ffmpeg_takes_only_the_two_programs_from_the_package(self):
        mgr, _ = self.manager(tools.FfmpegManager)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name in ("pkg/bin/ffmpeg.exe", "pkg/bin/ffprobe.exe", "pkg/bin/ffplay.exe", "pkg/doc/readme.txt"):
                z.writestr(name, b"MZ")
        archive = self.tmp / "a.zip"
        archive.write_bytes(buf.getvalue())
        stage = self.tmp / "stage"
        stage.mkdir()
        mgr._unpack(archive, self.tmp, stage)
        self.assertEqual(sorted(p.name for p in stage.iterdir()), ["ffmpeg.exe", "ffprobe.exe"])

    def test_ffmpeg_version_is_the_build_date(self):
        mgr, _ = self.manager(tools.FfmpegManager)
        data = {"assets": [{"name": "ffmpeg-master-latest-win64-gpl.zip", "browser_download_url": "https://x/y.zip",
                            "size": 5, "updated_at": "2026-10-30T04:05:06Z"}]}
        with mock.patch.object(tools.FlatToolManager, "_github", return_value=data), \
                mock.patch.object(tools.platform, "machine", return_value="AMD64"):
            rel = mgr._latest_release()
        self.assertEqual(rel.tag, "20261030")
        self.assertEqual(mgr._label(rel.tag), "2026-10-30")

    def test_ytdlp_picks_this_systems_build(self):
        mgr, _ = self.manager(tools.YtdlpManager)
        data = {"tag_name": "2026.10.01", "assets": [
            {"name": n, "browser_download_url": "https://x/" + n, "size": 1}
            for n in ("yt-dlp", "yt-dlp.exe", "yt-dlp_macos", "yt-dlp_linux")]}
        with mock.patch.object(tools.FlatToolManager, "_github", return_value=data):
            self.assertEqual(mgr._latest_release().name, tools.YtdlpManager.asset_name())

    def test_install_places_files_loose_and_remembers_the_version(self):
        mgr, events = self.manager(tools.FfmpegManager)
        rel = tools.Release("20261030", "p.zip", "https://x/p.zip", 0, None)

        def fake_download(_rel, dest):
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr("p/bin/ffmpeg.exe", b"MZ")
                z.writestr("p/bin/ffprobe.exe", b"MZ")

        with mock.patch.object(mgr, "_download", side_effect=fake_download), \
                mock.patch.object(tools.FfmpegManager, "_probe", return_value="ffmpeg version"):
            mgr._install(rel)
        self.assertTrue((self.tmp / "ffmpeg.exe").is_file() and (self.tmp / "ffprobe.exe").is_file())
        self.assertEqual(mgr.cached()[0], "20261030")
        self.assertIn(("ffmpeg_busy", True), events)
        self.assertEqual(events[-1], ("ffmpeg_busy", False))
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith("dl_")], [])

    def test_both_share_one_folder(self):
        self.assertEqual(tools.YtdlpManager.SUBDIR, tools.FfmpegManager.SUBDIR)


class HactoolnetTests(TempDirTest):
    """LibHac's v0.19.0 release holds hactoolnet-0.19.0-linux.zip, -netcore.zip and -win.zip (nothing for macOS)."""
    NAMES = ("hactoolnet-0.19.0-linux.zip", "hactoolnet-0.19.0-netcore.zip", "hactoolnet-0.19.0-win.zip", "LibHac.0.19.0.nupkg")

    def manager(self):
        with mock.patch.object(tools.ToolManager, "_pick_root", classmethod(lambda cls: self.tmp / "hac")):
            (self.tmp / "hac").mkdir()
            events: list[tuple] = []
            return tools.HactoolnetManager(events.append), events

    def latest(self, mgr, platform_name):
        listing = release("v0.19.0", *self.NAMES)
        with mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(listing).encode())), \
                mock.patch.object(tools.sys, "platform", platform_name), mock.patch.object(tools.platform, "machine", return_value="AMD64"):
            return mgr._latest_release()

    def test_each_system_gets_its_own_build_and_macos_the_portable_one(self):
        mgr, _ = self.manager()
        self.assertEqual(self.latest(mgr, "win32").name, "hactoolnet-0.19.0-win.zip")
        self.assertEqual(self.latest(mgr, "linux").name, "hactoolnet-0.19.0-linux.zip")
        self.assertEqual(self.latest(mgr, "darwin").name, "hactoolnet-0.19.0-netcore.zip")  # no macOS build: runs with dotnet

    def test_a_missing_dotnet_runtime_is_named_not_reported_as_a_broken_download(self):
        out = mock.Mock(stdout=b"", stderr=b"You must install .NET to run this application.\nApp: C:\\x\\hactoolnet.exe")
        with mock.patch.object(tools.subprocess, "run", return_value=out):
            with self.assertRaisesRegex(RuntimeError, r"needs the \.NET runtime"):
                tools.HactoolnetManager._probe(self.tmp / "hactoolnet.exe")

    def test_a_rate_limited_check_says_so_and_names_where_the_key_goes(self):
        mgr, events = self.manager()
        err = tools.urllib.error.HTTPError("u", 403, "rate limit", {}, None)
        with mock.patch.object(mgr, "_latest_release", side_effect=err), mock.patch.object(mgr, "_local_copy", return_value=None):
            mgr._run()
        text = [e[1] for e in events if e[0] == "hactoolnet_status"][-1]
        self.assertIn("rate-limiting", text)
        self.assertIn("API keys", text)
