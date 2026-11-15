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
        with mock.patch.object(tools, "WINDOWS", True):
            got = self.pick(self.assets("pkg_extractor.AppImage", "install_pkg.sh", "install_pkg.bat", "pkg_extractor.exe"))
        self.assertEqual(got["name"], "pkg_extractor.exe")

    def test_windows_zip_is_preferred_to_the_linux_one(self):
        with mock.patch.object(tools, "WINDOWS", True):
            got = self.pick(self.assets("pkg_extractor_linux.zip", "pkg_extractor_windows.zip"))
        self.assertEqual(got["name"], "pkg_extractor_windows.zip")

    def test_linux_the_zip_of_the_linux_package_is_taken(self):
        with mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools.sys, "platform", "linux"):
            got = self.pick(self.assets("ShadPs4Plus-PkgExtractor-1.1-linux.AppImage",
                                        "ShadPs4Plus-PkgExtractor-1.1-linux.zip",
                                        "ShadPs4Plus-PkgExtractor-1.1.zip"))
        self.assertEqual(got["name"], "ShadPs4Plus-PkgExtractor-1.1-linux.zip")

    def test_linux_ignores_the_windows_programs(self):
        with mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools.sys, "platform", "linux"):
            self.assertIsNone(self.pick(self.assets("pkg_extractor.exe", "pkg_extractor_windows.zip")))

    def test_macos_cannot_select_a_linux_or_windows_package(self):
        with mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools.sys, "platform", "darwin"):
            self.assertIsNone(self.pick(self.assets("pkg_extractor_linux.zip", "pkg_extractor_windows.zip",
                                                   "pkg_extractor.exe")))

    def test_nothing_usable(self):
        with mock.patch.object(tools, "WINDOWS", True):
            self.assertIsNone(self.pick(self.assets("install_pkg.bat", "pkg_extractor.AppImage", "source.tar.gz")))
            self.assertIsNone(self.pick([]))


class PkgExtractorManagerTests(TempDirTest):
    def setUp(self):
        super().setUp()
        # These fixtures exercise the preserved Windows app-relative layout.
        patcher = mock.patch.object(tools.sys, "platform", "win32")
        patcher.start()
        self.addCleanup(patcher.stop)
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
        with mock.patch.object(tools, "WINDOWS", True), \
                mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(listing).encode())):
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
        with mock.patch.object(tools, "WINDOWS", True):
            self.mgr._unpack(archive, tmp, dest)
        self.assertEqual((dest / self.mgr.EXE_NAME).read_bytes(), EXE)  # installed under this system's name

    def test_unpacks_a_zip_keeping_companion_files(self):
        archive = self.tmp / "dl.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("bundle/Pkg_Extractor.exe", EXE)
            z.writestr("bundle/helper.dll", b"MZ")
            z.writestr("bundle/install_pkg.bat", "@echo off")
        tmp, dest = self.tmp / "tmp", self.mgr.root / "PKG_EXTRACTOR_1_1"
        tmp.mkdir()
        with mock.patch.object(tools, "WINDOWS", True):
            self.mgr._unpack(archive, tmp, dest)
        self.assertTrue((dest / self.mgr.EXE_NAME).is_file())  # the app always looks for this one name
        self.assertTrue((dest / "helper.dll").is_file())

    def test_the_linux_package_becomes_a_launcher_around_its_appimage(self):
        """Linux gets an AppImage, which needs FUSE to mount itself: it is wrapped in a launcher that never FUSE-mounts."""
        archive = self.tmp / "linux.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("ShadPs4Plus-PkgExtractor-1.1-linux/pkg_extractor.AppImage", b"\x7fELF" + b"\0" * 80_000)
            z.writestr("ShadPs4Plus-PkgExtractor-1.1-linux/install_pkg.sh", "#!/bin/sh\n")
        tmp, dest = self.tmp / "tmp", self.mgr.root / "PKG_EXTRACTOR_1_1"
        tmp.mkdir()
        with mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools.sys, "platform", "linux"):
            self.mgr._unpack(archive, tmp, dest)
        launcher = dest / self.mgr.EXE_NAME
        self.assertTrue(launcher.is_file())
        self.assertTrue(launcher.stat().st_mode & 0o111)  # it has to be runnable
        self.assertTrue((dest / "pkg_extractor.AppImage").is_file())
        self.assertIn("APPIMAGE_EXTRACT_AND_RUN=1", launcher.read_text())
        with mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools.sys, "platform", "linux"):
            self.assertEqual(self.mgr._probe(launcher), "ok")

    def test_macos_cannot_download_or_use_a_linux_appimage_launcher(self):
        launcher = self.write("pkg_extractor", PkgExtractorManager.LINUX_LAUNCHER.encode())
        launcher.chmod(0o755)
        with mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools.sys, "platform", "darwin"), \
                mock.patch.object(tools.urllib.request, "urlopen") as request, \
                mock.patch.object(self.mgr, "_install") as install, \
                mock.patch.object(self.mgr, "_local_copy", return_value=None), \
                mock.patch.object(self.mgr, "_legacy_copy", return_value=None):
            with self.assertRaisesRegex(tools.ReleaseUnavailable, "Windows and Linux only"):
                self.mgr._latest_release()
            self.mgr.run()
            request.assert_not_called()
            install.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "doesn't look like a program"):
                self.mgr._probe(launcher)
        self.assertFalse(any(event[0] == f"{self.mgr.KEY}_path" for event in self.events))

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
        exe = self.write(f"tools/pkg-extractor/PKG_EXTRACTOR_1_1/{self.mgr.EXE_NAME}", EXE)
        self.assertEqual(self.mgr.cached(), ("PKG_EXTRACTOR_1_1", exe))
        (self.mgr.root / "current.txt").write_text("../../evil")
        self.assertIsNone(self.mgr.cached())  # a tag that doesn't match the pattern is never followed


class NativeReleaseTests(TempDirTest):
    def test_tool_tarball_cannot_write_outside_its_destination(self):
        import tarfile
        archive = self.tmp / "bad.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            entry = tarfile.TarInfo("../escaped")
            entry.size = 3
            tar.addfile(entry, io.BytesIO(b"bad"))
        with mock.patch.object(tools.ToolManager, "_pick_root", return_value=self.tmp):
            mgr = tools.ToolManager(lambda _: None)
        with self.assertRaises((tarfile.FilterError, RuntimeError)):
            mgr._extract_tar(archive, self.tmp / "dest")
        self.assertFalse((self.tmp / "escaped").exists())

    def test_linux_downloads_choose_native_assets_and_keep_published_checksums(self):
        cases = (
            (tools.PkgToAnyPs5Manager, "pkg-to-anyps5-linux-x86_64.tar.gz", "pkg-to-anyps5-windows-x86_64.zip"),
            (tools.XdvdfsManager, "xdvdfs-linux-abcdef.zip", "xdvdfs-windows-abcdef.zip"),
            (tools.Ps3QddManager, "ps3dec-x86_64-unknown-linux-gnu.tar.gz", "PS3QDD-keys.zip"),
        )
        for cls, linux, windows in cases:
            with self.subTest(tool=cls.__name__), \
                    mock.patch.object(cls, "_pick_root", return_value=self.tmp), \
                    mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools, "LINUX", True), \
                    mock.patch.object(tools.sys, "platform", "linux"), \
                    mock.patch.object(tools, "ARM", False), \
                    mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(
                        json.dumps(release("v1.2.3", windows, linux)).encode())) as request:
                got = cls(lambda _: None)._latest_release()
                self.assertEqual(got.name, linux)
                self.assertEqual(got.sha256, "ab" * 32)
                self.assertEqual(request.call_count, 1)

    def test_linux_tool_downloads_are_unavailable_on_macos(self):
        cases = (
            (tools.Ps3QddManager, "ps3dec-x86_64-apple-darwin.tar.gz"),
            (tools.PkgToAnyPs5Manager, "pkg-to-anyps5-macos-x86_64.tar.gz"),
            (tools.XdvdfsManager, "xdvdfs-macos.zip"),
            (tools.FfmpegManager, "ffmpeg-master-latest-linux64-gpl.tar.xz"),
        )
        for cls, name in cases:
            self.assertEqual(cls.WINDOWS_ONLY, not tools.LINUX)
            data = release("v1.2.3", name)
            with self.subTest(tool=cls.__name__), \
                    mock.patch.object(cls, "_pick_root", return_value=self.tmp / cls.SUBDIR), \
                    mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools, "LINUX", False), \
                    mock.patch.object(tools.sys, "platform", "darwin"), \
                    mock.patch.object(cls, "_local_copy", return_value=None), \
                    mock.patch.object(cls, "_legacy_copy", return_value=None), \
                    mock.patch.object(cls, "_install") as install, \
                    mock.patch.object(tools.urllib.request, "urlopen", side_effect=lambda *a, **k:
                                      FakeResponse(json.dumps(data).encode())) as request:
                events = []
                mgr = cls(events.append)
                with self.assertRaisesRegex(tools.ReleaseUnavailable, "Windows and Linux only"):
                    mgr._latest_release()
                if cls is tools.Ps3QddManager:
                    request.assert_not_called()
                mgr.run()
                install.assert_not_called()
                self.assertFalse(any(event[0] == f"{mgr.KEY}_path" for event in events))

    def test_linux_relinker_download_keeps_the_matching_libraries(self):
        data = [release("v1.2.3", "relinker-v1.2.3.exe", "prx-windows-v1.2.3.zip",
                        "relinker-v1.2.3", "prx-linux-v1.2.3.tar.gz")]
        with mock.patch.object(tools.AnyPS5Manager, "_pick_root", return_value=self.tmp), \
                mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools.sys, "platform", "linux"), \
                mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(data).encode())):
            mgr = tools.AnyPS5Manager(lambda _: None)
            got = mgr._latest_release()
        self.assertEqual(got.name, "relinker-v1.2.3")
        self.assertEqual(mgr._libs_release.name, "prx-linux-v1.2.3.tar.gz")

    def test_macos_cannot_download_the_linux_relinker_and_libraries(self):
        data = [release("v1.2.3", "relinker-v1.2.3", "prx-linux-v1.2.3.tar.gz")]
        with mock.patch.object(tools.AnyPS5Manager, "_pick_root", return_value=self.tmp), \
                mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools.sys, "platform", "darwin"), \
                mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(data).encode())):
            mgr = tools.AnyPS5Manager(lambda _: None)
            with self.assertRaisesRegex(tools.ReleaseUnavailable, "No AnyPS5 release"):
                mgr._latest_release()
        self.assertIsNone(mgr._libs_release)

    def test_windows_ps3dec_uses_the_original_release_without_a_checksum_request_or_probe(self):
        data = release("v1.2.3", "PS3QDD.zip", "PS3QDD-keys.zip", "SHA256SUMS")
        for asset in data["assets"]:
            asset.pop("digest")
        exe = self.write("PS3Dec.exe", EXE)
        with mock.patch.object(tools.Ps3QddManager, "_pick_root", return_value=self.tmp), \
                mock.patch.object(tools, "WINDOWS", True), \
                mock.patch.object(tools.subprocess, "run") as run, \
                mock.patch.object(tools.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(data).encode())) as request:
            mgr = tools.Ps3QddManager(lambda _: None)
            got = mgr._latest_release()
            mgr._probe(exe)
        self.assertEqual(got.name, "PS3QDD-keys.zip")
        self.assertIsNone(got.sha256)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0].full_url, mgr.API_URL)
        run.assert_not_called()

    def test_older_linux_tool_folders_are_used_in_place_without_migration(self):
        data = self.tmp / "data" / "distill"
        app = self.tmp / "app"
        for base in (app, data.parent / tools.APP_NAME):
            with self.subTest(base=base), \
                    mock.patch.object(tools.sys, "platform", "linux"), \
                    mock.patch.object(tools, "APP_DIR", app), \
                    mock.patch.object(tools.paths, "data_dir", return_value=data), \
                    mock.patch.object(tools.shutil, "which", return_value=None), \
                    mock.patch.dict("os.environ", {"DISTILL_PORTABLE": "0"}):
                mgr = tools.PkgToAnyPs5Manager(lambda _: None)
                legacy = base / "tools" / mgr.SUBDIR / mgr.EXE_NAME
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.write_bytes(EXE)
                with mock.patch.object(mgr, "_latest_release") as fetch:
                    mgr.run()
                fetch.assert_not_called()
                self.assertEqual(mgr.root, data / "tools" / mgr.SUBDIR)
                self.assertFalse(mgr.root.exists())
                self.assertEqual(legacy.read_bytes(), EXE)
                legacy.unlink()


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
        names = mgr.FILES  # ("ffmpeg.exe", "ffprobe.exe") on Windows, ("ffmpeg", "ffprobe") elsewhere
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name in (*[f"pkg/bin/{n}" for n in names], "pkg/bin/ffplay", "pkg/doc/readme.txt"):
                z.writestr(name, b"MZ")
        archive = self.tmp / "a.zip"
        archive.write_bytes(buf.getvalue())
        stage = self.tmp / "stage"
        stage.mkdir()
        mgr._unpack(archive, self.tmp, stage)
        self.assertEqual(sorted(p.name for p in stage.iterdir()), sorted(names))
        if not tools.WINDOWS:
            self.assertTrue(all(p.stat().st_mode & 0o111 for p in stage.iterdir()))  # unpacked programs are runnable

    def test_ffmpeg_takes_only_the_two_programs_from_a_linux_tarball(self):
        import tarfile
        mgr, _ = self.manager(tools.FfmpegManager)
        src = self.tmp / "pkg"
        (src / "bin").mkdir(parents=True)
        for name in ("ffmpeg", "ffprobe", "ffplay"):
            (src / "bin" / name).write_bytes(b"\x7fELF" + b"\0" * 100)
        (src / "bin").joinpath("ffmpeg").chmod(0o644)  # the tarball does not always carry the exec bit
        archive = self.tmp / "a.tar.xz"
        with tarfile.open(archive, "w:xz") as tar:
            tar.add(src, arcname="ffmpeg-master-latest-linux64-gpl")
        stage = self.tmp / "stage"
        stage.mkdir()
        with mock.patch.object(tools, "WINDOWS", False), \
                mock.patch.object(mgr, "FILES", ("ffmpeg", "ffprobe")):
            mgr._unpack(archive, self.tmp, stage)
        self.assertEqual(sorted(p.name for p in stage.iterdir()), ["ffmpeg", "ffprobe"])
        self.assertTrue((stage / "ffmpeg").stat().st_mode & 0o111)  # made runnable by the unpacker

    def test_ffmpeg_version_is_the_build_date(self):
        mgr, _ = self.manager(tools.FfmpegManager)
        data = {"assets": [{"name": "ffmpeg-master-latest-win64-gpl.zip", "browser_download_url": "https://x/y.zip",
                            "size": 5, "updated_at": "2026-10-30T04:05:06Z"}]}
        with mock.patch.object(tools, "WINDOWS", True), \
                mock.patch.object(tools.FlatToolManager, "_github", return_value=data), \
                mock.patch.object(tools.platform, "machine", return_value="AMD64"):
            rel = mgr._latest_release()
        self.assertEqual(rel.tag, "20261030")
        self.assertEqual(mgr._label(rel.tag), "2026-10-30")

    def test_ffmpeg_asks_for_the_linux_tarball_off_windows(self):
        mgr, _ = self.manager(tools.FfmpegManager)
        data = {"assets": [{"name": "ffmpeg-master-latest-linux64-gpl.tar.xz", "browser_download_url": "https://x/y.xz",
                            "size": 5, "updated_at": "2026-10-30T04:05:06Z"}]}
        with mock.patch.object(tools, "WINDOWS", False), mock.patch.object(tools, "LINUX", True), \
                mock.patch.object(tools.FlatToolManager, "_github", return_value=data), \
                mock.patch.object(tools.platform, "machine", return_value="x86_64"):
            rel = mgr._latest_release()
        self.assertEqual(rel.name, "ffmpeg-master-latest-linux64-gpl.tar.xz")

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
        names = mgr.FILES

        def fake_download(_rel, dest):
            with zipfile.ZipFile(dest, "w") as z:
                for name in names:
                    z.writestr(f"p/bin/{name}", b"MZ")

        with mock.patch.object(mgr, "_download", side_effect=fake_download), \
                mock.patch.object(tools.FfmpegManager, "_probe", return_value="ffmpeg version"):
            mgr._install(rel)
        self.assertTrue(all((self.tmp / name).is_file() for name in names))
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
