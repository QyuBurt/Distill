"""Saved API keys: kept sealed on disk, read back, forgotten when emptied, and used first by the GitHub token lookup."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from distill import apikeys, github_auth as ga


class ApiKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(prefix="distill_keys_")
        self.addCleanup(td.cleanup)
        self.file = Path(td.name) / "keys.json"
        p = mock.patch.object(apikeys, "FILE", self.file)
        p.start()
        self.addCleanup(p.stop)
        apikeys._cache = None

    def test_a_saved_key_is_read_back_and_trimmed(self):
        apikeys.set("github", "  ghp_abc ")
        apikeys._cache = None  # as if the app had been restarted
        self.assertEqual(apikeys.get("github"), "ghp_abc")
        self.assertEqual(apikeys.get("sgdb"), "")

    def test_the_file_does_not_hold_the_key_as_plain_text_where_the_platform_can_seal_it(self):
        with mock.patch.object(apikeys.secretstore, "best_method", return_value="plain"):
            apikeys.set("sgdb", "secret-value")
        doc = json.loads(self.file.read_text())
        self.assertEqual(doc["v"], 2)  # sealed envelope, not a bare {"sgdb": ...}
        self.assertNotIn("sgdb", doc)

    def test_emptying_the_last_key_removes_the_file(self):
        apikeys.set("github", "x")
        self.assertTrue(self.file.exists())
        apikeys.set("github", "")
        self.assertFalse(self.file.exists())
        self.assertEqual(apikeys.get("github"), "")

    def test_unknown_names_are_refused(self):
        with self.assertRaises(KeyError):
            apikeys.set("nope", "x")

    def test_a_broken_file_reads_as_no_keys(self):
        self.file.write_text("{not json")
        self.assertEqual(apikeys.load(), {})

    def test_the_saved_github_token_beats_the_environment_and_the_local_file(self):
        apikeys.set("github", "saved")
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "env"}, clear=True), mock.patch.object(ga, "_LOCAL_TOKEN", "file"):
            self.assertEqual(ga.github_token(), "saved")
            self.assertEqual(ga.github_headers({})["Authorization"], "Bearer saved")
        apikeys.set("github", "")
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "env"}, clear=True), mock.patch.object(ga, "_LOCAL_TOKEN", "file"):
            self.assertEqual(ga.github_token(), "env")

    def test_test_github_messages(self):
        import urllib.error
        with mock.patch.object(apikeys.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("u", 401, "x", {}, None)):
            self.assertIn("rejected", apikeys.test_github("bad"))
        with mock.patch.object(apikeys.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")):
            self.assertIn("reach", apikeys.test_github("k"))


if __name__ == "__main__":
    unittest.main()
