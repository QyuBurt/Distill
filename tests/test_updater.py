"""The start-up update check: version comparison and release parsing (no network)."""
from __future__ import annotations

import json
import unittest
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


class CheckTests(unittest.TestCase):
    def _release(self, **over):
        data = {"tag_name": "v1.1", "html_url": "https://github.com/QyuBurt/Distill/releases/tag/v1.1", "body": "notes",
                "assets": [{"name": "Distill-portable.zip", "browser_download_url": "https://x/z.zip", "size": 5},
                           {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/sums"}]}
        data.update(over)
        return data

    def _fake_get(self, release):
        def get(url, *a, **k):
            return json.dumps(release).encode() if url == updater.API_URL else b"abc123  Distill-portable.zip\n"
        return get

    def test_newer_release_found(self):
        with mock.patch.object(updater, "_get", self._fake_get(self._release())), \
             mock.patch.object(updater, "install_kind", return_value="portable"):
            up = updater.check("v1.0")
        self.assertEqual((up.tag, up.asset_url, up.sha256), ("v1.1", "https://x/z.zip", "abc123"))

    def test_same_version_draft_prerelease_and_errors_give_none(self):
        for rel in (self._release(tag_name="v1.0"), self._release(draft=True), self._release(prerelease=True)):
            with mock.patch.object(updater, "_get", self._fake_get(rel)):
                self.assertIsNone(updater.check("v1.0"))
        with mock.patch.object(updater, "_get", side_effect=OSError("offline")):
            self.assertIsNone(updater.check("v1.0"))

    def test_source_run_offers_the_page_only(self):
        with mock.patch.object(updater, "_get", self._fake_get(self._release())), \
             mock.patch.object(updater, "install_kind", return_value="source"):
            self.assertEqual(updater.check("v1.0").asset_url, "")


class ScriptTests(unittest.TestCase):
    def test_helper_script_waits_swaps_and_restarts(self):
        from pathlib import Path
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
