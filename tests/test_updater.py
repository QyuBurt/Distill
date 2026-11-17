"""The start-up update check: version comparison, release parsing per platform, and the safety of what follows (no network)."""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from distill import updater


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v1.0.2"), (1, 0, 2))
        self.assertEqual(updater.parse_version("1.5"), (1, 5))
        self.assertEqual(updater.parse_version("nightly"), ())

    def test_is_newer(self):
        self.assertTrue(updater.is_newer("v1.0.1", "v1.0"))
        self.assertTrue(updater.is_newer("v1.1", "v1.0"))
        self.assertTrue(updater.is_newer("v2.0", "v1.9"))
        self.assertFalse(updater.is_newer("v1.0", "v1.0"))
        self.assertFalse(updater.is_newer("v1.0.0", "v1.0"))
        self.assertFalse(updater.is_newer("v0.9", "v1.0"))
        self.assertFalse(updater.is_newer("junk", "v1.0"))

    def test_a_two_part_and_a_three_part_tag_are_the_same_release(self):
        """v1.5 and v1.5.0 must compare equal, or the prompt reappears after every launch (the inherited v1.0 bug)."""
        self.assertFalse(updater.is_newer("v1.5.0", "v1.5"))
        self.assertFalse(updater.is_newer("v1.5", "v1.5.0"))
        self.assertTrue(updater.is_newer("v1.5.1", "v1.5.0"))
        self.assertTrue(updater.is_newer("v1.6", "v1.5.0"))


class CheckTests(unittest.TestCase):
    def _release(self, **over):
        data = {"tag_name": "v1.1", "html_url": "https://github.com/QyuBurt/Distill/releases/tag/v1.1", "body": "notes",
                "assets": [{"name": "Distill-portable.zip", "browser_download_url": "https://x/z.zip", "size": 5},
                           {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/sums"}]}
        data.update(over)
        return data

    def _fake_get(self, release, sums=b"abc123  Distill-portable.zip\n"):
        def get(url, *a, **k):
            return json.dumps(release).encode() if url == updater.API_URL else sums
        return get

    def _check(self, release, kind, current="v1.0", sums=b"abc123  Distill-portable.zip\n"):
        with mock.patch.object(updater, "_get", self._fake_get(release, sums)), \
             mock.patch.object(updater, "install_kind", return_value=kind):
            return updater.check(current)

    def test_newer_release_found(self):
        up = self._check(self._release(), "portable")
        self.assertEqual((up.tag, up.asset_url, up.sha256), ("v1.1", "https://x/z.zip", "abc123"))
        self.assertTrue(up.installable)  # the packaged Windows build swaps itself
        self.assertEqual(up.message, "")

    def test_same_version_draft_prerelease_and_errors_give_none(self):
        for rel in (self._release(tag_name="v1.0"), self._release(draft=True), self._release(prerelease=True)):
            self.assertIsNone(self._check(rel, "portable"))
        with mock.patch.object(updater, "_get", side_effect=OSError("offline")):
            self.assertIsNone(updater.check("v1.0"))

    def test_the_installed_version_being_current_means_no_prompt_at_all(self):
        rel = self._release(tag_name="v1.5.0", assets=[])
        for kind in ("portable", "onefile", "appimage", "source"):
            with self.subTest(kind=kind):
                self.assertIsNone(self._check(rel, kind, current="v1.5.0"))
        self.assertIsNone(self._check(self._release(tag_name="v1.5.0"), "appimage", current="v1.5"))  # same release, shorter tag

    def test_source_run_offers_the_page_only(self):
        up = self._check(self._release(), "source")
        self.assertEqual((up.asset_url, up.asset_name, up.installable), ("", "", False))

    def test_linux_release_without_a_linux_build_says_so_and_picks_nothing(self):
        """GitHub's releases are Windows-only so far: the prompt must say so and never offer that exe to a Linux install."""
        up = self._check(self._release(), "appimage")
        self.assertEqual((up.asset_url, up.asset_name, up.sha256), ("", "", None))
        self.assertFalse(up.installable)
        self.assertIn("no Linux build", up.message)
        self.assertEqual(up.page, "https://github.com/QyuBurt/Distill/releases/tag/v1.1")

    def test_linux_release_with_a_linux_build_is_offered_with_its_checksum(self):
        rel = self._release(assets=[
            {"name": "Distill.exe", "browser_download_url": "https://x/Distill.exe", "size": 5},
            {"name": "Distill-linux-x86_64.AppImage", "browser_download_url": "https://x/Distill-linux-x86_64.AppImage", "size": 9},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/sums"}])
        sums = b"abc123  Distill.exe\nfeed99  Distill-linux-x86_64.AppImage\n"
        up = self._check(rel, "appimage", sums=sums)
        self.assertEqual((up.asset_name, up.asset_url, up.sha256),
                         ("Distill-linux-x86_64.AppImage", "https://x/Distill-linux-x86_64.AppImage", "feed99"))
        self.assertEqual((up.asset_size, up.installable), (9, False))  # Linux installs it by hand, never in place
        self.assertIn("Distill-linux-x86_64.AppImage", up.message)

    def test_linux_never_fetches_the_checksums_for_an_asset_it_will_not_use(self):
        release = self._release()  # a Windows zip and the sums file, nothing else

        def get(url, *a, **k):
            self.assertEqual(url, updater.API_URL)  # the checksum file is only read for the asset that was chosen
            return json.dumps(release).encode()

        with mock.patch.object(updater, "_get", get), mock.patch.object(updater, "install_kind", return_value="appimage"):
            self.assertEqual(updater.check("v1.0").asset_url, "")

    def test_windows_builds_still_get_their_own_asset_and_install_it(self):
        rel = self._release(assets=[
            {"name": "Distill.exe", "browser_download_url": "https://x/Distill.exe", "size": 5},
            {"name": "Distill-portable.zip", "browser_download_url": "https://x/Distill-portable.zip", "size": 7},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/sums"}])
        sums = b"exe111  Distill.exe\nzip222  Distill-portable.zip\n"
        one = self._check(rel, "onefile", sums=sums)
        self.assertEqual((one.asset_name, one.sha256, one.installable), ("Distill.exe", "exe111", True))
        portable = self._check(rel, "portable", sums=sums)
        self.assertEqual((portable.asset_name, portable.sha256, portable.installable), ("Distill-portable.zip", "zip222", True))

    def test_the_linux_asset_is_an_appimage_before_a_tarball(self):
        assets = {"SHA256SUMS.txt": {}, "Distill-linux.tar.gz": {"name": "Distill-linux.tar.gz"},
                  "Distill-linux-x86_64.AppImage": {"name": "Distill-linux-x86_64.AppImage"}, "Distill.exe": {"name": "Distill.exe"}}
        self.assertEqual(updater._linux_asset(assets)["name"], "Distill-linux-x86_64.AppImage")
        self.assertIsNone(updater._linux_asset({"Distill.exe": {"name": "Distill.exe"}}))
        self.assertIsNone(updater._linux_asset({}))


class PlatformTests(unittest.TestCase):
    def test_install_kind_names_each_build(self):
        with mock.patch.object(updater.sys, "frozen", True, create=True), tempfile.TemporaryDirectory() as td:
            with mock.patch.object(updater.sys, "platform", "linux"):
                self.assertEqual(updater.install_kind(), "appimage")
            with mock.patch.object(updater, "APP_DIR", Path(td)), mock.patch.object(updater.sys, "platform", "win32"):
                self.assertEqual(updater.install_kind(), "onefile")  # a single .exe
                (Path(td) / "_internal").mkdir()
                self.assertEqual(updater.install_kind(), "portable")  # the folder build
        self.assertEqual(updater.install_kind(), "source")  # this run is a checkout

    def test_apply_refuses_a_platform_it_cannot_install_on(self):
        for kind in ("source", "appimage"):
            with self.subTest(kind=kind), mock.patch.object(updater, "install_kind", return_value=kind), \
                    mock.patch.object(updater.subprocess, "Popen") as popen:
                with self.assertRaises(RuntimeError):
                    updater.apply(Path("/tmp/Distill-linux-x86_64.AppImage"))
                popen.assert_not_called()  # nothing was handed to a helper script


class EnabledTests(unittest.TestCase):
    def test_the_startup_check_can_be_switched_off(self):
        try:
            with mock.patch.dict(os.environ, {"DISTILL_NO_UPDATE_CHECK": "1"}):
                importlib.reload(updater)
                self.assertFalse(updater.CHECK_ENABLED)
            with mock.patch.dict(os.environ) as env:
                env.pop("DISTILL_NO_UPDATE_CHECK", None)
                importlib.reload(updater)
                self.assertTrue(updater.CHECK_ENABLED)
        finally:
            importlib.reload(updater)


class ScriptTests(unittest.TestCase):
    def test_helper_script_waits_swaps_and_restarts(self):
        for kind, marker in (("portable", "tar -xf"), ("onefile", "move /y")):
            text = updater._script(kind, Path("C:/tmp/payload"), "v9.9")
            self.assertIn(marker, text)
            self.assertIn("Please wait", text)
            self.assertNotIn("timeout", text)  # fails without a console
            self.assertIn("ping -n 2", text)
            self.assertIn("distill-update.log", text)
            self.assertLess(text.index(":wait"), text.index(":swap"))
            self.assertLess(text.index(":swap"), text.index(":restart"))
            self.assertIn("start \"\"", text)
            self.assertNotIn("ExecutionPolicy", text)  # no install window unless one was staged

    def test_install_window_runs_first_and_the_plain_swap_is_still_the_fallback(self):
        for kind, marker in (("portable", "tar -xf"), ("onefile", "move /y")):
            text = updater._script(kind, Path("C:/tmp/payload"), "v9.9", Path("C:/tmp/apply_update.json"))
            window = text.index("powershell -NoProfile -ExecutionPolicy Bypass -File")
            self.assertIn("apply_update.ps1", text)
            self.assertIn("-Spec", text)
            self.assertLess(window, text.index(":wait"))  # the window goes first ...
            self.assertLess(text.index("if not errorlevel 1 goto restart"), text.index(":wait"))  # ... and success skips the plain swap
            self.assertLess(text.index(":wait"), text.index(":swap"))  # ... which stays as the fallback
            self.assertIn(marker, text)
            self.assertLess(text.index(":swap"), text.index(":restart"))

    def test_staging_copies_what_the_window_needs_and_writes_its_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "icon.png").write_bytes(b"png")
            (tmp / "payload.zip").write_bytes(b"zip")
            spec = {"logo": str(tmp / "icon.png"), "start": 0.7}
            spec_path, ready = updater._stage_splash(spec, tmp / "payload.zip", "portable")
            data = json.loads(spec_path.read_text(encoding="utf-8"))
            self.assertEqual((data["kind"], data["start"], data["ready"]), ("portable", 0.7, str(ready)))
            self.assertTrue(Path(data["logo"]).is_file())
            self.assertTrue(spec_path.with_suffix(".ps1").is_file())
            self.assertFalse(ready.exists())  # the script creates it once its window is up
            self.assertIsNone(updater._stage_splash({"logo": str(tmp / "nope.png")}, tmp / "payload.zip", "portable"))

    def test_install_window_script_is_ascii_and_falls_back_with_exit_1(self):
        from distill import update_splash
        self.assertTrue(update_splash.SCRIPT.isascii())  # PowerShell 5.1 reads a script without a BOM as ANSI
        self.assertIn("exit 1", update_splash.SCRIPT)
        self.assertIn("exit 0", update_splash.SCRIPT)
        self.assertEqual(update_splash.SCRIPT.count("{"), update_splash.SCRIPT.count("}"))
        self.assertEqual(update_splash.SCRIPT.count("("), update_splash.SCRIPT.count(")"))
        self.assertIn("UpdateLayeredWindow", update_splash.SCRIPT)  # the bare logo is a per-pixel-alpha window, not a box with a colour key
        self.assertNotIn("DrawString", update_splash.SCRIPT)  # and it carries no text


class FillMathTests(unittest.TestCase):
    """The arithmetic behind the update screen's filling logo (ui/fillmath.py has no Qt)."""

    def test_fraction_is_clamped_and_an_unknown_size_is_nothing(self):
        from distill.ui import fillmath as fm

        self.assertEqual(fm.fraction(0, 100), 0.0)
        self.assertEqual(fm.fraction(25, 100), 0.25)
        self.assertEqual(fm.fraction(150, 100), 1.0)
        self.assertEqual(fm.fraction(-5, 100), 0.0)
        self.assertEqual(fm.fraction(50, 0), 0.0)

    def test_percent_only_reads_100_when_it_really_is_done(self):
        from distill.ui import fillmath as fm

        self.assertEqual(fm.percent_text(0.0), "0%")
        self.assertEqual(fm.percent_text(0.424), "42%")
        self.assertEqual(fm.percent_text(0.999), "99%")
        self.assertEqual(fm.percent_text(1.0), "100%")
        self.assertEqual(fm.percent_text(7), "100%")

    def test_size_text(self):
        from distill.ui import fillmath as fm

        self.assertEqual(fm.size_text(13 << 20, 48 << 20), "13.0 of 48.0 MB")
        self.assertEqual(fm.size_text(13 << 20, 0), "13.0 MB")

    def test_colour_rises_from_the_bottom_to_the_top(self):
        from distill.ui import fillmath as fm

        self.assertEqual(fm.edge_y(0, 200, 0.0), 200)  # nothing filled: the edge is at the bottom
        self.assertEqual(fm.edge_y(0, 200, 0.5), 100)
        self.assertEqual(fm.edge_y(0, 200, 1.0), 0)
        self.assertEqual(fm.edge_y(10, 200, 1.0), 10)
        ys = [fm.edge_y(0, 240, i / 10) for i in range(11)]
        self.assertEqual(ys, sorted(ys, reverse=True))

    def test_ripple_dies_away_at_both_ends_and_never_exceeds_its_peak(self):
        from distill.ui import fillmath as fm

        self.assertEqual(fm.wave_amplitude(0.0, 4), 0.0)
        self.assertAlmostEqual(fm.wave_amplitude(1.0, 4), 0.0, places=6)
        self.assertAlmostEqual(fm.wave_amplitude(0.5, 4), 4.0)
        for i in range(101):
            self.assertLessEqual(fm.wave_amplitude(i / 100, 4), 4.0 + 1e-9)


if __name__ == "__main__":
    unittest.main()
