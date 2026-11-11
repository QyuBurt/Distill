"""Saved API keys and logins: one sealed file, read back, forgotten when emptied, folded in from the two older files, and used first by the GitHub token lookup."""
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
        self.dir = Path(td.name)
        for name, value in (("FILE", self.file), ("LEGACY_KEYS_FILE", self.dir / "old_keys.json"),
                            ("LEGACY_LOGINS_FILE", self.dir / "old_logins.json")):
            p = mock.patch.object(apikeys, name, value)
            p.start()
            self.addCleanup(p.stop)
        apikeys._cache = None
        self.addCleanup(setattr, apikeys, "_cache", None)

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

    def test_keys_and_logins_share_one_file_and_neither_loses_the_other(self):
        apikeys.set("sgdb", "S")
        apikeys.save_logins({"1fichier": {"apikey": "F"}, "custom": []})
        apikeys.set("github", "G")  # saving a key must not drop the logins
        apikeys._cache = None
        self.assertEqual((apikeys.get("sgdb"), apikeys.get("github")), ("S", "G"))
        self.assertEqual(apikeys.logins()["1fichier"], {"apikey": "F"})
        apikeys.save_logins(None)  # 'Remember' unticked: the logins go, the keys stay
        apikeys._cache = None
        self.assertEqual(apikeys.logins(), {})
        self.assertEqual(apikeys.get("sgdb"), "S")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["keys.json"])  # one file, no leftovers

    def test_nothing_in_the_file_reads_as_plain_text(self):
        apikeys.set("sgdb", "SECRET-ONE")
        apikeys.save_logins({"1fichier": {"apikey": "SECRET-TWO"}})
        fake = (lambda b: bytes(x ^ 0x5A for x in b), lambda b: bytes(x ^ 0x5A for x in b))
        with mock.patch.dict(apikeys.secretstore.METHODS, {"fake": fake}), mock.patch.object(apikeys.secretstore, "best_method", return_value="fake"):
            apikeys.set("github", "SECRET-THREE")
            text = self.file.read_text()
            apikeys._cache = None
            self.assertEqual((apikeys.get("github"), apikeys.logins()["1fichier"]), ("SECRET-THREE", {"apikey": "SECRET-TWO"}))
        for secret in ("SECRET-ONE", "SECRET-TWO", "SECRET-THREE"):
            self.assertNotIn(secret, text)

    def test_the_two_older_files_are_folded_in_and_removed(self):
        (self.dir / "old_keys.json").write_text(json.dumps({"sgdb": " S ", "bogus": "x"}))  # the oldest keys file was plain
        sealed = apikeys.secretstore.seal(json.dumps({"torbox": {"apikey": "T"}, "custom": [
            {"host": "a.com", "kind": "Cookie", "user": "", "secret": "s=1"}]}).encode(), "plain")
        (self.dir / "old_logins.json").write_text(json.dumps(sealed))
        self.assertEqual(apikeys.get("sgdb"), "S")
        self.assertEqual(apikeys.logins()["torbox"], {"apikey": "T"})
        self.assertEqual(apikeys.logins()["custom"][0]["host"], "a.com")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["keys.json"])  # both old files gone, one new one
        apikeys._cache = None
        self.assertEqual((apikeys.get("sgdb"), apikeys.logins()["torbox"]), ("S", {"apikey": "T"}))
        self.assertNotIn("bogus", apikeys.load())

    def test_a_key_in_the_new_file_wins_over_an_older_one(self):
        apikeys.set("github", "new")
        apikeys._cache = None
        (self.dir / "old_keys.json").write_text(json.dumps({"github": "old", "sgdb": "S"}))
        self.assertEqual((apikeys.get("github"), apikeys.get("sgdb")), ("new", "S"))
        self.assertFalse((self.dir / "old_keys.json").exists())

    def test_an_older_file_that_cannot_be_unlocked_is_kept_and_reported(self):
        (self.dir / "old_logins.json").write_text(json.dumps({"v": 2, "method": "nope", "data": ""}))
        self.assertEqual(apikeys.logins(), {})
        self.assertIn("method", apikeys.problem())
        self.assertTrue((self.dir / "old_logins.json").exists())  # not deleted: the person may move it back to the right account

    def test_test_github_messages(self):
        import urllib.error
        with mock.patch.object(apikeys.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("u", 401, "x", {}, None)):
            self.assertIn("rejected", apikeys.test_github("bad"))
        with mock.patch.object(apikeys.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")):
            self.assertIn("reach", apikeys.test_github("k"))


if __name__ == "__main__":
    unittest.main()
