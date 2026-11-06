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
        for kind, marker in (("portable", "Expand-Archive"), ("onefile", "move /y")):
            text = updater._script(kind, Path("C:/tmp/payload"))
            self.assertIn(marker, text)
            self.assertNotIn("timeout", text)  # fails without a console
            self.assertIn("ping -n 2", text)
            self.assertIn("distill-update.log", text)
            self.assertLess(text.index(":wait"), text.index(":swap"))
            self.assertLess(text.index(":swap"), text.index(":restart"))
            self.assertIn("start \"\"", text)


if __name__ == "__main__":
    unittest.main()
