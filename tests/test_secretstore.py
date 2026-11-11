import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from distill import apikeys, downloads as dl, secretstore


class SecretStoreTests(unittest.TestCase):
    def test_plain_round_trip(self):
        doc = secretstore.seal(b"hello", "plain")
        self.assertEqual(secretstore.unseal(doc), b"hello")

    def test_a_backend_round_trip_and_failure_falls_back(self):
        fake = (lambda b: b[::-1], lambda b: b[::-1])
        with mock.patch.dict(secretstore.METHODS, {"fake": fake}):
            doc = secretstore.seal(b"abc", "fake")
            self.assertEqual((doc["method"], secretstore.unseal(doc)), ("fake", b"abc"))
            self.assertNotIn("abc", json.dumps(doc))
        boom = (lambda b: (_ for _ in ()).throw(OSError("no keyring")), lambda b: b)
        with mock.patch.dict(secretstore.METHODS, {"boom": boom}):
            self.assertEqual(secretstore.seal(b"abc", "boom")["method"], "plain")  # never lose the logins

    def test_unknown_and_unreadable(self):
        with self.assertRaises(secretstore.SealError):
            secretstore.unseal({"v": 2, "method": "nope", "data": ""})
        bad = (lambda b: b, lambda b: (_ for _ in ()).throw(ValueError("wrong key")))
        with mock.patch.dict(secretstore.METHODS, {"bad": bad}), self.assertRaises(secretstore.SealError):
            secretstore.unseal({"v": 2, "method": "bad", "data": ""})


class LoginFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, file in (("FILE", "keys.json"), ("LEGACY_KEYS_FILE", "old_keys.json"), ("LEGACY_LOGINS_FILE", "logins.json")):
            patch = mock.patch.object(apikeys, name, self.tmp / file)
            patch.start()
            self.addCleanup(patch.stop)
        apikeys._cache = None
        self.addCleanup(setattr, apikeys, "_cache", None)
        saved = dict(dl.LOGINS)
        self.addCleanup(lambda: (dl.LOGINS.clear(), dl.LOGINS.update(saved)))

    def test_old_plain_file_still_loads_and_is_then_sealed(self):
        (self.tmp / "logins.json").write_text(json.dumps({"1fichier": {"apikey": "K"}, "custom": [
            {"host": "a.com", "kind": "Cookie", "user": "", "secret": "s=1"}]}))
        self.assertTrue(dl.load_logins())
        self.assertEqual(dl.LOGINS["1fichier"]["apikey"], "K")
        with mock.patch.object(secretstore, "best_method", return_value="plain"):
            dl.save_logins(True)
        on_disk = json.loads((self.tmp / "keys.json").read_text())
        self.assertEqual(on_disk["v"], 2)
        self.assertTrue(dl.load_logins())
        self.assertEqual(dl.LOGINS["custom"][0]["host"], "a.com")

    def test_secrets_are_not_readable_in_a_protected_file(self):
        dl.LOGINS["1fichier"] = {"apikey": "SECRETKEY"}
        fake = (lambda b: bytes(x ^ 0x5A for x in b), lambda b: bytes(x ^ 0x5A for x in b))
        with mock.patch.dict(secretstore.METHODS, {"fake": fake}), mock.patch.object(secretstore, "best_method", return_value="fake"):
            dl.save_logins(True)
            self.assertNotIn("SECRETKEY", (self.tmp / "keys.json").read_text())
            self.assertTrue(dl.load_logins())
        self.assertEqual(dl.LOGINS["1fichier"]["apikey"], "SECRETKEY")

    def test_unlockable_file_is_reported_not_crashed(self):
        (self.tmp / "logins.json").write_text(json.dumps({"v": 2, "method": "nope", "data": ""}))
        self.assertFalse(dl.load_logins())
        self.assertIn("method", dl.LOGIN_PROBLEM)


if __name__ == "__main__":
    unittest.main()
