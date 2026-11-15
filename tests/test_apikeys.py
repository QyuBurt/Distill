"""Saved API keys and logins: one sealed file, read back, forgotten when emptied, folded in from the two older files, and used first by the GitHub token lookup."""
from __future__ import annotations

import json
import importlib.util
import errno
import os
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from distill import apikeys, constants, downloads, github_auth as ga, paths


class ApiKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(prefix="distill_keys_")
        self.addCleanup(td.cleanup)
        self.file = Path(td.name) / "keys.json"
        self.dir = Path(td.name)
        for name, value in (("APP_DIR", self.dir / "app"), ("FILE", self.file), ("LEGACY_KEYS_FILE", self.dir / "old_keys.json"),
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

    def test_emptying_the_last_key_preserves_migration_finalization(self):
        apikeys.set("github", "x")
        self.assertTrue(self.file.exists())
        apikeys.set("github", "")
        self.assertTrue(self.file.exists())
        self.assertIn("finalized", apikeys._open(self.file)[0]["migrated"])
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
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".keys.json.lock", "keys.json"])  # one store and its advisory lock

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
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".keys.json.lock", "keys.json"])  # both old files gone
        apikeys._cache = None
        self.assertEqual((apikeys.get("sgdb"), apikeys.logins()["torbox"]), ("S", {"apikey": "T"}))
        self.assertNotIn("bogus", apikeys.load())

    def test_a_key_in_the_new_file_wins_over_an_older_one(self):
        self.assertTrue(apikeys._write({"keys": {"github": "new"}, "logins": {}}))
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


class CredentialPathTests(unittest.TestCase):
    """Exercise import-time routing and migration with placeholder secrets, never user directories."""

    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="distill_credential_paths_")
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.app = self.root / "app"
        self.app.mkdir()
        env = {"HOME": str(self.root / "home"), "DISTILL_PORTABLE": "0"}
        env.update({f"XDG_{category}_HOME": str(self.root / category.lower())
                    for category in ("CONFIG", "DATA", "STATE", "CACHE")})
        for patch in (mock.patch.dict(os.environ, env), mock.patch.object(paths.sys, "platform", "linux"),
                      mock.patch.object(paths, "APP_DIR", self.app), mock.patch.object(constants, "APP_DIR", self.app),
                      mock.patch.object(apikeys.secretstore, "best_method", return_value="plain")):
            patch.start()
            self.addCleanup(patch.stop)
        self.keys = self.load_module()

    def load_module(self):
        spec = importlib.util.spec_from_file_location("distill._credential_path_test", apikeys.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def legacy_pair(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        keys = directory / "distill_apikeys.json"
        logins = directory / "distill_logins.json"
        keys.write_text(json.dumps({"github": "fixture-old-key", "sgdb": "fixture-art"}))
        logins.write_text(json.dumps(self.keys.secretstore.seal(
            json.dumps({"torbox": {"apikey": "fixture-old-login"}}).encode(), "plain")))
        return keys, logins

    def test_linux_keys_and_download_logins_share_the_xdg_file(self):
        self.assertEqual(self.keys.FILE, self.root / "config/distill/distill_keys.json")
        self.assertFalse(self.keys.FILE.parent.exists())
        self.keys.set("github", "fixture-key")
        with mock.patch.object(downloads, "apikeys", self.keys), \
                mock.patch.dict(downloads.LOGINS, {"torbox": {"apikey": "fixture-login"}, "custom": []}, clear=True):
            downloads.save_logins(True)
            self.keys._cache = None
            downloads.LOGINS.clear()
            self.assertTrue(downloads.load_logins())
            self.assertEqual(downloads.LOGINS["torbox"], {"apikey": "fixture-login"})
            downloads.save_logins(False)
        self.assertEqual(self.keys.get("github"), "fixture-key")
        self.assertEqual(self.keys.logins(), {})
        self.assertEqual(sorted(p.name for p in self.keys.FILE.parent.iterdir()), [".distill_keys.json.lock", "distill_keys.json"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.keys.FILE.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.keys.FILE.parent.stat().st_mode), 0o700)

    def test_xdg_legacy_pair_is_folded_in_then_removed(self):
        legacy = self.legacy_pair(self.keys.FILE.parent)
        self.assertEqual(self.keys.get("github"), "fixture-old-key")
        self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-old-login"})
        self.assertTrue(all(not p.exists() for p in legacy))
        self.keys._cache = None
        self.assertEqual(self.keys.get("sgdb"), "fixture-art")

    def test_existing_unified_entries_win_and_missing_legacy_hosts_are_retained(self):
        self.assertTrue(self.keys._write({"keys": {"github": "fixture-current-key"},
                                         "logins": {"torbox": {"apikey": "fixture-current-login"}}}))
        legacy = self.legacy_pair(self.keys.FILE.parent)
        legacy[1].write_text(json.dumps({"torbox": {"apikey": "fixture-old-login"},
                                       "archive.org": {"user": "fixture-user", "pass": "fixture-pass"}}))
        self.keys._cache = None
        self.assertEqual(self.keys.get("github"), "fixture-current-key")
        self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-current-login"})
        self.assertEqual(self.keys.logins()["archive.org"]["user"], "fixture-user")
        self.assertTrue(all(not p.exists() for p in legacy))

    def test_app_folder_pair_is_imported_separately_and_originals_are_preserved(self):
        legacy = self.legacy_pair(self.app)
        before = [p.read_bytes() for p in legacy]
        self.assertEqual(paths.migrate_legacy(), [])  # general migration never copies credentials
        self.assertFalse(self.keys.FILE.exists())
        self.assertEqual(self.keys.get("github"), "fixture-old-key")
        self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-old-login"})
        self.assertEqual([p.read_bytes() for p in legacy], before)
        self.assertTrue(self.keys.FILE.exists())

    def test_existing_xdg_unified_store_is_not_changed_by_app_folder_credentials(self):
        self.assertTrue(self.keys._write({"keys": {"github": "fixture-current-key"},
                                         "logins": {"torbox": {"apikey": "fixture-current-login"}}}))
        before, problem = self.keys._open(self.keys.FILE)
        self.assertFalse(problem)
        legacy = self.legacy_pair(self.app)
        self.keys._cache = None
        self.assertEqual(self.keys.get("github"), "fixture-current-key")
        self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-current-login"})
        after, problem = self.keys._open(self.keys.FILE)
        self.assertFalse(problem)
        self.assertEqual(after["keys"], {**before["keys"], "sgdb": "fixture-art"})
        self.assertEqual(after["logins"], before["logins"])
        self.assertEqual(after["migrated"], ["app:distill_apikeys.json", "app:distill_logins.json", "finalized"])
        self.assertTrue(all(p.exists() for p in legacy))

    def test_forgetting_imported_credentials_does_not_resurrect_app_folder_originals(self):
        legacy = self.legacy_pair(self.app)
        originals = [p.read_bytes() for p in legacy]
        self.assertEqual(self.keys.get("github"), "fixture-old-key")
        self.keys.save_logins(None)
        self.keys.set("github", "")
        self.keys.set("sgdb", "")
        self.keys._cache = None
        self.assertEqual(self.keys.load(), {})
        self.assertEqual(self.keys.logins(), {})
        self.assertTrue(self.keys.FILE.exists())  # durable migration markers survive forgetting
        self.assertEqual(self.keys._open(self.keys.FILE)[0]["migrated"],
                         ["app:distill_apikeys.json", "app:distill_logins.json", "finalized"])
        self.assertEqual([p.read_bytes() for p in legacy], originals)

    def test_xdg_legacy_entries_win_over_app_folder_unified_and_two_file_stores(self):
        self.legacy_pair(self.keys.FILE.parent)
        legacy = self.legacy_pair(self.app)
        unified = self.app / "distill_keys.json"
        unified.write_text(json.dumps({"keys": {"github": "fixture-app-key", "ra": "fixture-ra"},
                                      "logins": {"torbox": {"apikey": "fixture-app-login"}}}))
        before = [p.read_bytes() for p in (*legacy, unified)]
        self.assertEqual(self.keys.get("github"), "fixture-old-key")
        self.assertEqual(self.keys.get("ra"), "fixture-ra")
        self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-old-login"})
        self.assertEqual([p.read_bytes() for p in (*legacy, unified)], before)

    def test_migration_failures_keep_the_old_store_and_legacy_files(self):
        for stage in ("seal", "parent", "chmod", "fsync", "replace"):
            with self.subTest(stage=stage):
                self.keys = self.load_module()
                self.keys.FILE.unlink(missing_ok=True)
                self.assertTrue(self.keys._write({"keys": {"github": "fixture-current"}, "logins": {}}))
                before = self.keys.FILE.read_bytes()
                legacy = self.legacy_pair(self.keys.FILE.parent)
                originals = [p.read_bytes() for p in legacy]
                owner, name, error = {
                    "seal": (self.keys.secretstore, "seal", self.keys.secretstore.SealError("fixture seal failed")),
                    "parent": (paths, "ensure_parent", OSError("fixture parent failed")),
                    "chmod": (os, "chmod", OSError("fixture chmod failed")),
                    "fsync": (os, "fsync", OSError("fixture fsync failed")),
                    "replace": (os, "replace", OSError("fixture replace failed")),
                }[stage]
                with mock.patch.object(owner, name, side_effect=error):
                    self.assertEqual(self.keys.get("github"), "fixture-current")
                self.assertEqual(self.keys.FILE.read_bytes(), before)
                self.assertEqual([p.read_bytes() for p in legacy], originals)
                self.assertFalse(list(self.keys.FILE.parent.glob("*.tmp")))

    def test_failed_first_migration_keeps_both_app_and_xdg_sources(self):
        legacy = (*self.legacy_pair(self.app), *self.legacy_pair(self.keys.FILE.parent))
        originals = [p.read_bytes() for p in legacy]
        with mock.patch.object(os, "replace", side_effect=OSError("fixture replace failed")):
            self.assertEqual(self.keys.get("github"), "fixture-old-key")
        self.assertFalse(self.keys.FILE.exists())
        self.assertEqual([p.read_bytes() for p in legacy], originals)

    @unittest.skipIf(os.name == "nt", "directory fsync uses POSIX descriptors")
    def test_directory_sync_failure_keeps_legacy_files(self):
        legacy = self.legacy_pair(self.keys.FILE.parent)
        real_fsync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("fixture directory sync failed")
            real_fsync(fd)
        with mock.patch.object(os, "fsync", side_effect=sync):
            self.keys.load()
        self.assertTrue(all(p.exists() for p in legacy))

    def test_failed_readback_keeps_legacy_files(self):
        legacy = self.legacy_pair(self.keys.FILE.parent)
        real_open = self.keys._open
        def read(path):
            return (None, "fixture readback failed") if path.name.endswith(".tmp") else real_open(path)
        with mock.patch.object(self.keys, "_open", side_effect=read):
            self.keys.load()
        self.assertTrue(all(p.exists() for p in legacy))

    def test_failed_final_verification_or_sync_restores_the_unfinalized_store_exactly(self):
        for stage in (("readback",) if os.name == "nt" else ("readback", "sync")):
            with self.subTest(stage=stage):
                self.keys = self.load_module()
                self.assertTrue(self.keys._write({"keys": {"github": "fixture-current"}, "logins": {}}))
                before = self.keys.FILE.read_bytes()
                sources = self.legacy_pair(self.keys.FILE.parent)
                originals = [source.read_bytes() for source in sources]
                read, fsync = self.keys._open, os.fsync
                def fail_read(path, read=read):
                    doc, problem = read(path)
                    if path == self.keys.FILE and doc and "finalized" in doc.get("migrated", []):
                        return None, "fixture final verification failed"
                    return doc, problem
                def fail_sync(fd, fsync=fsync):
                    if stat.S_ISDIR(os.fstat(fd).st_mode):
                        raise OSError("fixture final directory sync failed")
                    return fsync(fd)
                patch = (mock.patch.object(self.keys, "_open", side_effect=fail_read) if stage == "readback"
                         else mock.patch.object(os, "fsync", side_effect=fail_sync))
                with patch:
                    self.assertEqual(self.keys.get("github"), "fixture-current")
                self.assertEqual(self.keys.FILE.read_bytes(), before)
                self.assertEqual([source.read_bytes() for source in sources], originals)
                self.assertFalse(list(self.keys.FILE.parent.glob("*.previous")))
                with mock.patch.object(self.keys, "_write") as write:
                    self.keys.set("ra", "fixture-blocked")
                    write.assert_not_called()

    def test_unreadable_unified_file_is_never_replaced_by_migration(self):
        for text in ('{broken', json.dumps({"v": 2, "method": "unknown", "data": ""})):
            with self.subTest(text=text):
                legacy = self.legacy_pair(self.keys.FILE.parent)
                self.keys.FILE.write_text(text)
                self.keys._cache = None
                self.assertEqual(self.keys.load(), {})
                self.assertEqual(self.keys.FILE.read_text(), text)
                self.assertTrue(all(p.exists() for p in legacy))

    def test_portable_and_windows_keep_app_dir_paths_and_same_directory_fold_in(self):
        for platform, portable in (("linux", "1"), ("win32", "0")):
            app = self.root / f"{platform}-{portable}"
            app.mkdir()
            with self.subTest(platform=platform, portable=portable), \
                    mock.patch.object(paths.sys, "platform", platform), \
                    mock.patch.object(paths, "APP_DIR", app), mock.patch.object(constants, "APP_DIR", app), \
                    mock.patch.dict(os.environ, {"DISTILL_PORTABLE": portable}):
                module = self.load_module()
                self.assertEqual(module.FILE, app / "distill_keys.json")
                self.assertEqual(module.LEGACY_KEYS_FILE, app / "distill_apikeys.json")
                self.assertEqual(module.LEGACY_LOGINS_FILE, app / "distill_logins.json")
                legacy = self.legacy_pair(app)
                module.set("github", "fixture-platform-key")
                module.save_logins({"torbox": {"apikey": "fixture-platform-login"}})
                module._cache = None
                self.assertEqual(module.get("github"), "fixture-platform-key")
                self.assertEqual(module.logins()["torbox"], {"apikey": "fixture-platform-login"})
                self.assertTrue(all(not p.exists() for p in legacy))

    def child_command(self, code):
        root = Path(apikeys.__file__).resolve().parent.parent
        prefix = f"""import os, sys, time, json
from pathlib import Path
sys.path.insert(0, {str(root)!r})
from distill import apikeys as keys, paths
paths.sys.platform = 'linux'
paths.APP_DIR = keys.APP_DIR = Path({str(self.app)!r})
keys.FILE = Path({str(self.keys.FILE)!r})
keys.LEGACY_KEYS_FILE = Path({str(self.keys.LEGACY_KEYS_FILE)!r})
keys.LEGACY_LOGINS_FILE = Path({str(self.keys.LEGACY_LOGINS_FILE)!r})
keys.secretstore.best_method = lambda: 'plain'
"""
        return [sys.executable, "-B", "-c", prefix + code]

    def run_child(self, code, timeout=5):
        env = dict(os.environ, USERPROFILE=str(self.root / "home"))
        result = subprocess.run(self.child_command(code), env=env, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def competing_children(self, bodies):
        children = []
        go = self.root / "go"
        try:
            for index, body in enumerate(bodies):
                ready = self.root / f"ready-{index}"
                barrier = f"""Path({str(ready)!r}).write_text('fixture-ready')
deadline = time.monotonic() + 10
while not Path({str(go)!r}).exists():
    assert time.monotonic() < deadline, 'fixture start barrier timed out'
    time.sleep(0.005)
"""
                child = subprocess.Popen(self.child_command(barrier + body), env=dict(os.environ, USERPROFILE=str(self.root / "home")),
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(child)
            deadline = time.monotonic() + 10
            while not all((self.root / f"ready-{i}").exists() for i in range(len(children))):
                self.assertLess(time.monotonic(), deadline, "child fixture did not reach the start barrier")
                for child in children:
                    if child.poll() is not None:
                        out, err = child.communicate()
                        self.fail(out + err)
                time.sleep(0.005)
            go.write_text("fixture-go")
            for child in children:
                out, err = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, out + err)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_empty_custom_list_imports_every_legacy_host(self):
        self.assertTrue(self.keys._write({"keys": {}, "logins": {"custom": []}}))
        entries = [{"host": "one.invalid", "kind": "Cookie", "secret": "fixture-one"},
                   {"host": "two.invalid", "kind": "Cookie", "secret": "fixture-two"}]
        self.keys.LEGACY_LOGINS_FILE.write_text(json.dumps({"custom": entries}))
        self.keys._cache = None
        self.assertEqual(self.keys.logins()["custom"], entries)
        self.assertFalse(self.keys.LEGACY_LOGINS_FILE.exists())
        self.assertEqual(self.keys._open(self.keys.FILE)[0]["migrated"], ["finalized", "local:distill_logins.json"])

    def test_custom_hosts_merge_without_replacing_existing_entries(self):
        current = {"host": "ONE.invalid", "kind": "Cookie", "secret": "fixture-current"}
        incoming = [{"host": "one.invalid", "kind": "Cookie", "secret": "fixture-old"},
                    {"host": "two.invalid", "kind": "Cookie", "secret": "fixture-two"},
                    {"unrecognized": "fixture-entry"}]
        self.assertTrue(self.keys._write({"keys": {}, "logins": {"custom": [current]}}))
        self.keys.LEGACY_LOGINS_FILE.write_text(json.dumps({"custom": incoming}))
        self.keys._cache = None
        self.assertEqual(self.keys.logins()["custom"], [current, *incoming[1:]])
        self.assertFalse(self.keys.LEGACY_LOGINS_FILE.exists())

    def test_missing_host_in_readback_retries_only_after_restart(self):
        self.keys.FILE.parent.mkdir(parents=True)
        entry = {"host": "one.invalid", "secret": "fixture-cookie", "kind": "Cookie"}
        self.keys.LEGACY_LOGINS_FILE.write_text(json.dumps({"custom": [entry]}))
        read = self.keys._open
        def lose_host(path):
            doc, problem = read(path)
            if path.name.endswith(".tmp") and doc:
                doc["logins"]["custom"] = []
            return doc, problem
        with mock.patch.object(self.keys, "_open", side_effect=lose_host):
            self.keys.load()
        self.assertTrue(self.keys.LEGACY_LOGINS_FILE.exists())
        self.assertFalse(self.keys.FILE.exists())
        with mock.patch.object(self.keys, "_write") as write:
            self.assertEqual(self.keys.logins()["custom"], [entry])
            self.keys.set("github", "fixture-blocked")
            write.assert_not_called()
        self.keys = self.load_module()
        self.assertEqual(self.keys.logins()["custom"], [entry])
        self.assertFalse(self.keys.LEGACY_LOGINS_FILE.exists())

    def test_failed_migration_then_successful_forget_never_resurrects_sources(self):
        for location in ("local", "app"):
            for stage in ("write", "readback", "unlink"):
                with self.subTest(location=location, stage=stage):
                    base = self.root / f"{location}-{stage}"
                    app, config = base / "app", base / "config"
                    app.mkdir(parents=True)
                    config.mkdir()
                    source = (config if location == "local" else app) / "distill_logins.json"
                    source.write_text(json.dumps({"torbox": {"apikey": "fixture-forgotten"}}))
                    original = source.read_bytes()
                    with mock.patch.multiple(self.keys, APP_DIR=app, FILE=config / "distill_keys.json",
                                             LEGACY_KEYS_FILE=config / "distill_apikeys.json",
                                             LEGACY_LOGINS_FILE=config / "distill_logins.json"):
                        self.keys._cache = self.keys._migration = None
                        read, unlink = self.keys._open, Path.unlink
                        def fail_read(path, read=read):
                            return (None, "fixture readback failure") if path == self.keys.FILE and path.exists() else read(path)
                        def fail_unlink(path, *args, source=source, unlink=unlink, **kwargs):
                            if path == source:
                                raise OSError("fixture source deletion failure")
                            return unlink(path, *args, **kwargs)
                        patch = {"write": mock.patch.object(self.keys, "_write", return_value=False),
                                 "readback": mock.patch.object(self.keys, "_open", side_effect=fail_read),
                                 "unlink": mock.patch.object(Path, "unlink", fail_unlink)}[stage]
                        with patch:
                            self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-forgotten"})
                        self.assertEqual(source.read_bytes(), original)
                        if stage != "unlink":
                            published, _ = self.keys._open(self.keys.FILE)
                            self.assertEqual((published or {}).get("migrated", []), [])
                        self.keys._cache = self.keys._migration = None  # restart after failed migration
                        self.keys.save_logins(None)
                        self.keys._cache = None
                        self.assertEqual(self.keys.logins(), {})
                        self.assertEqual(self.keys.load(), {})
                        self.assertEqual(self.keys._open(self.keys.FILE)[0]["migrated"], sorted(["finalized", f"{location}:distill_logins.json"]))
                        if location == "app" or stage == "unlink":
                            self.assertEqual(source.read_bytes(), original)
                            source.write_text(json.dumps({"torbox": {"apikey": "fixture-still-ignored"}}))
                            self.keys._cache = None
                            self.assertEqual(self.keys.logins(), {})

    def test_unreadable_source_is_finalized_untouched_before_any_edit(self):
        old_keys, logins = self.legacy_pair(self.app)
        originals = [old_keys.read_bytes(), logins.read_bytes()]
        logins.chmod(0)
        self.addCleanup(logins.chmod, 0o600)
        real_open = os.open
        def enforce_permissions(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(logins) and not logins.stat().st_mode & stat.S_IRUSR:
                raise PermissionError(errno.EACCES, "fixture source is locked")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=enforce_permissions):
            self.assertEqual(self.keys.get("github"), "fixture-old-key")
            self.assertEqual(self.keys._open(self.keys.FILE)[0]["migrated"],
                             ["app:distill_apikeys.json", "app:distill_logins.json", "finalized"])
            self.assertIn("fixture source is locked", self.keys.problem())
            self.assertIn("will not be imported later", self.keys.problem())
            self.keys.set("ra", "fixture-unrelated")
            self.assertIn("fixture source is locked", self.keys.problem())
        logins.chmod(0o600)
        self.assertEqual([old_keys.read_bytes(), logins.read_bytes()], originals)
        self.assertEqual(self.keys.logins(), {})
        self.keys = self.load_module()
        self.assertEqual(self.keys.logins(), {})
        self.assertEqual(self.keys.get("ra"), "fixture-unrelated")
        self.assertEqual([old_keys.read_bytes(), logins.read_bytes()], originals)

    def test_source_becoming_readable_during_session_is_never_imported_by_key_autosave(self):
        self.assertTrue(self.keys._write({"keys": {"ra": "fixture-existing"}, "logins": {}}))
        source, logins = self.legacy_pair(self.app)
        read = self.keys._open
        with mock.patch.object(self.keys, "_open", side_effect=lambda path:
                               (None, "fixture locked source") if path == source else read(path)):
            self.assertEqual(self.keys.load(), {"ra": "fixture-existing"})
            self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-old-login"})
        self.keys.set("romm", "fixture-unrelated")
        self.assertEqual(self.keys.load(), {"ra": "fixture-existing", "romm": "fixture-unrelated"})
        self.assertEqual(self.keys.get("github"), "")
        self.assertEqual(self.keys.get("sgdb"), "")
        self.assertTrue(source.exists())
        self.assertTrue(logins.exists())
        self.keys = self.load_module()
        self.assertEqual(self.keys.load(), {"ra": "fixture-existing", "romm": "fixture-unrelated"})

    def test_sources_created_after_finalization_are_not_imported_even_after_restart(self):
        self.assertEqual(self.keys.load(), {})
        sources = self.legacy_pair(self.app)
        originals = [source.read_bytes() for source in sources]
        self.keys.set("github", "fixture-new")
        self.keys.set("github", "")
        self.keys = self.load_module()
        self.assertEqual(self.keys.load(), {})
        self.assertEqual(self.keys.logins(), {})
        self.assertEqual([source.read_bytes() for source in sources], originals)

    def test_named_forget_during_initial_migration_preserves_other_names(self):
        self.legacy_pair(self.keys.FILE.parent)
        self.keys.set("github", "")
        self.assertEqual(self.keys.load(), {"sgdb": "fixture-art"})
        self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-old-login"})
        self.keys = self.load_module()
        self.assertEqual(self.keys.load(), {"sgdb": "fixture-art"})

    def test_empty_legacy_documents_can_be_marked_without_blocking_new_keys(self):
        self.keys.FILE.parent.mkdir(parents=True)
        self.keys.LEGACY_LOGINS_FILE.write_text("{}")
        self.assertEqual(self.keys.logins(), {})
        self.assertFalse(self.keys.LEGACY_LOGINS_FILE.exists())
        self.keys.set("github", "fixture-key")
        self.assertEqual(self.keys.get("github"), "fixture-key")
        self.assertEqual(self.keys._open(self.keys.FILE)[0]["migrated"], ["finalized", "local:distill_logins.json"])

    def test_failed_publication_retries_only_on_next_start_before_forget(self):
        source = self.app / "distill_logins.json"
        source.write_text(json.dumps({"torbox": {"apikey": "fixture-remembered"}}))
        original = source.read_bytes()
        with mock.patch.object(self.keys, "_write", return_value=False) as write:
            self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-remembered"})
            self.assertEqual(write.call_count, 1)
        self.assertFalse(self.keys.FILE.exists())
        with mock.patch.object(self.keys, "_write") as write:
            self.keys.save_logins(None)
            self.keys.set("github", "fixture-blocked")
            self.keys.logins()
            write.assert_not_called()
        self.assertEqual(source.read_bytes(), original)
        self.keys = self.load_module()
        self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-remembered"})
        self.keys.save_logins(None)
        self.keys = self.load_module()
        self.assertEqual(self.keys.logins(), {})
        self.assertEqual(self.keys._open(self.keys.FILE)[0]["migrated"], ["app:distill_logins.json", "finalized"])
        self.assertEqual(source.read_bytes(), original)

    def test_competing_key_and_login_writers_do_not_lose_updates(self):
        slow = """write = keys._write
def delayed(doc, **kwargs):
    time.sleep(0.03)
    return write(doc, **kwargs)
keys._write = delayed
"""
        self.competing_children([
            slow + "for i in range(8):\n    keys.set('github', f'fixture-key-{i}')\n",
            slow + "for i in range(8):\n    keys.save_logins({'torbox': {'apikey': f'fixture-login-{i}'}})\n",
        ])
        self.assertEqual(self.keys.get("github"), "fixture-key-7")
        self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-login-7"})

    def test_competing_app_migration_and_new_writer_preserve_new_values_and_other_keys(self):
        (self.app / "distill_apikeys.json").write_text(json.dumps({"github": "fixture-old", "ra": "fixture-ra"}))
        self.competing_children([
            """write = keys._write
def delayed(doc, **kwargs):
    time.sleep(0.04)
    return write(doc, **kwargs)
keys._write = delayed
keys.load()
""",
            "keys.set('github', 'fixture-new')\nkeys.set('sgdb', 'fixture-art')\n",
        ])
        self.assertEqual(self.keys.load(), {"github": "fixture-new", "ra": "fixture-ra", "sgdb": "fixture-art"})
        self.assertTrue((self.app / "distill_apikeys.json").exists())

    def test_lock_timeout_and_uncreatable_lock_fail_without_writing(self):
        self.keys.set("github", "fixture-current")
        before = self.keys.FILE.read_bytes()
        legacy = self.legacy_pair(self.app)
        started = time.monotonic()
        with mock.patch.object(self.keys, "_LOCK_TIMEOUT", 0.03), \
                mock.patch.object(self.keys, "_try_lock", side_effect=BlockingIOError(errno.EAGAIN, "fixture busy")), \
                mock.patch.object(self.keys, "_write") as write:
            self.keys.set("sgdb", "fixture-refused")
            write.assert_not_called()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn("lock", self.keys._problem)
        self.assertEqual(self.keys.FILE.read_bytes(), before)
        with mock.patch.object(paths, "ensure_parent", side_effect=PermissionError("fixture unwritable")), \
                mock.patch.object(self.keys, "_write") as write:
            self.keys.save_logins({"torbox": {"apikey": "fixture-refused"}})
            write.assert_not_called()
        self.assertEqual(self.keys.FILE.read_bytes(), before)
        self.assertTrue(all(path.exists() for path in legacy))

    def _assert_failed_migration_refuses_edits_until_restart(self, keys, legacy):
        original = legacy.read_bytes()
        cache = json.loads(json.dumps(keys._cache))
        files = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in legacy.parent.iterdir()}
        with mock.patch.object(downloads, "apikeys", keys), mock.patch.dict(downloads.LOGINS, {}, clear=True):
            self.assertFalse(downloads.load_logins())
            loaded = json.loads(json.dumps(downloads.LOGINS))
            downloads.LOGINS["archive.org"] = {"access": "fixture-refused"}
            with mock.patch.object(keys, "_write", wraps=keys._write) as write:
                self.assertFalse(downloads.save_logins(True))
                self.assertEqual(downloads.LOGINS, loaded)
                self.assertFalse(keys.set("github", "fixture-refused"))
                self.assertFalse(keys.set("github", ""))
                self.assertFalse(keys.save_logins({"torbox": {"apikey": "fixture-refused"}}))
                self.assertFalse(keys.save_logins(None))
                self.assertFalse(downloads.save_logins(False))
                write.assert_not_called()
            self.assertEqual(keys._cache, cache)
            self.assertEqual(downloads.LOGINS, loaded)
            self.assertIn("read-only this session", keys.problem())
            self.assertIn("restart Distill", keys.problem())
            self.assertIn("Nothing was changed", downloads.LOGIN_PROBLEM)
            self.assertIn(str(keys.FILE.parent), keys.problem())
        self.assertEqual({path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in legacy.parent.iterdir()}, files)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertFalse(keys.FILE.exists())
        restarted = self.load_module()
        self.assertEqual(restarted.logins(), {"torbox": {"apikey": "fixture-legacy-login"}})
        self.assertTrue(restarted.set("github", "fixture-working"))
        self.assertTrue(restarted.save_logins({"torbox": {"apikey": "fixture-updated"}}))
        self.assertTrue(restarted.save_logins(None))
        self.assertTrue(restarted.set("github", ""))
        fresh = self.load_module()
        self.assertEqual(fresh.logins(), {})
        self.assertEqual(fresh.load(), {})
        self.assertFalse(legacy.exists())

    @unittest.skipUnless(os.name == "posix", "requires effective POSIX directory permissions")
    def test_initial_permission_failure_refuses_all_edits_until_restart(self):
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory write permissions")
        config = self.keys.FILE.parent
        config.mkdir(parents=True, mode=0o700)
        legacy = self.keys.LEGACY_LOGINS_FILE
        legacy.write_text(json.dumps({"torbox": {"apikey": "fixture-legacy-login"}}))
        config.chmod(0o500)
        self.addCleanup(config.chmod, 0o700)
        if os.access(config, os.W_OK):
            self.skipTest("chmod does not make the config directory unwritable")
        keys = self.load_module()
        self.assertEqual(keys.logins(), {})
        config.chmod(0o700)
        self._assert_failed_migration_refuses_edits_until_restart(keys, legacy)

    def test_initial_lock_fault_refuses_all_edits_until_restart(self):
        config = self.keys.FILE.parent
        config.mkdir(parents=True)
        legacy = self.keys.LEGACY_LOGINS_FILE
        legacy.write_text(json.dumps({"torbox": {"apikey": "fixture-legacy-login"}}))
        keys = self.load_module()
        with mock.patch.object(keys, "_try_lock", side_effect=OSError(errno.EIO, "fixture lock acquisition failed")):
            self.assertEqual(keys.logins(), {})
        self._assert_failed_migration_refuses_edits_until_restart(keys, legacy)

    def test_legacy_sgdb_state_key_is_retained_and_usable_until_destination_save_succeeds(self):
        from distill import recomps
        state = self.root / "data/distill/recomps/state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"sgdb_key": " fixture-legacy-key ", "find_new": True}))
        original = state.read_bytes()
        with mock.patch.object(self.keys, "_try_lock", side_effect=OSError(errno.EIO, "fixture lock failed")):
            self.assertEqual(self.keys.load(), {})
        store = recomps.Store(state)
        with mock.patch.object(recomps, "apikeys", self.keys), \
                mock.patch.object(recomps, "BUILTIN_SGDB_KEY", "fixture-built-in"), \
                mock.patch.object(store, "save", wraps=store.save) as save:
            for _attempt in range(2):
                self.assertEqual(store.sgdb_key(), "fixture-legacy-key")
                self.assertEqual(store.data["sgdb_key"], " fixture-legacy-key ")
                self.assertEqual(state.read_bytes(), original)
                self.assertEqual(self.keys.get("sgdb"), "")
                self.assertIn("read-only this session", self.keys.problem())
            save.assert_not_called()
        working = self.load_module()  # restart permits the destination write
        with mock.patch.object(recomps, "apikeys", working):
            self.assertEqual(store.sgdb_key(), "fixture-legacy-key")
            self.assertEqual(working.get("sgdb"), "fixture-legacy-key")
            self.assertNotIn("sgdb_key", json.loads(state.read_text()))
            self.assertTrue(json.loads(state.read_text())["find_new"])
            self.assertEqual(recomps.Store(state).sgdb_key(), "fixture-legacy-key")
        # Both keys exist while the write is refused: the readable unified key wins and the legacy entry is left alone.
        self.assertTrue(working.set("sgdb", "fixture-unified-key"))
        state.write_text(json.dumps({"sgdb_key": " fixture-revoked-legacy ", "find_new": True}))
        original = state.read_bytes()
        refusing = self.load_module()
        with mock.patch.object(refusing, "_try_lock", side_effect=OSError(errno.EIO, "fixture lock failed")):
            self.assertEqual(refusing.load(), {"sgdb": "fixture-unified-key"})
        store = recomps.Store(state)
        with mock.patch.object(recomps, "apikeys", refusing), \
                mock.patch.object(recomps, "BUILTIN_SGDB_KEY", "fixture-built-in"), \
                mock.patch.object(store, "save", wraps=store.save) as save:
            for _attempt in range(2):
                self.assertEqual(store.sgdb_key(), "fixture-unified-key")
                self.assertEqual(store.data["sgdb_key"], " fixture-revoked-legacy ")
                self.assertEqual(state.read_bytes(), original)
                self.assertEqual(refusing.get("sgdb"), "fixture-unified-key")
                self.assertFalse(refusing.set("sgdb", "fixture-refused"))
                self.assertIn("read-only this session", refusing.problem())
            save.assert_not_called()
        working = self.load_module()  # restart permits the destination write
        with mock.patch.object(recomps, "apikeys", working):
            self.assertEqual(store.sgdb_key(), "fixture-unified-key")
            self.assertEqual(working.get("sgdb"), "fixture-unified-key")
            self.assertNotIn("sgdb_key", json.loads(state.read_text()))
            self.assertTrue(json.loads(state.read_text())["find_new"])
            self.assertEqual(recomps.Store(state).sgdb_key(), "fixture-unified-key")

    def test_lock_inode_is_persistent_and_owner_only(self):
        self.keys.set("github", "fixture-key")
        lock = self.keys._lock_file()
        inode = lock.stat().st_ino
        self.keys.set("github", "")
        self.assertTrue(self.keys.FILE.exists())  # the finalization marker survives forgetting
        self.assertEqual(lock.stat().st_ino, inode)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_windows_lock_backend_uses_nonblocking_byte_lock_and_unlock(self):
        backend = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0, locking=mock.Mock())
        with mock.patch.dict(sys.modules, {"msvcrt": backend}), mock.patch.object(os, "name", "nt"), \
                mock.patch.object(os, "lseek") as seek:
            self.keys._try_lock(12)
            self.keys._try_lock(12, release=True)
        self.assertEqual(backend.locking.call_args_list, [mock.call(12, 1, 1), mock.call(12, 0, 1)])
        self.assertEqual(seek.call_args_list, [mock.call(12, 0, os.SEEK_SET), mock.call(12, 0, os.SEEK_SET)])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO sources require POSIX")
    def test_fifo_and_symlink_sources_and_locks_never_block_or_get_deleted(self):
        self.run_child("""keys.FILE.parent.mkdir(parents=True)
paths_to_test = [keys.FILE, keys.LEGACY_KEYS_FILE, keys.LEGACY_LOGINS_FILE,
                 keys.APP_DIR / 'distill_keys.json', keys.APP_DIR / 'distill_apikeys.json',
                 keys.APP_DIR / 'distill_logins.json', keys._lock_file()]
fifo = keys.APP_DIR / 'fixture-fifo-target'
os.mkfifo(fifo)
for path in paths_to_test:
    if path == keys._lock_file():
        path.unlink(missing_ok=True)  # remove this single-process fixture's prior ordinary lock
    for symlink in (False, True):
        if symlink:
            path.symlink_to(fifo)
        else:
            os.mkfifo(path)
        keys.FILE.unlink(missing_ok=True) if path != keys.FILE else None
        keys._cache = keys._migration = None
        assert keys.load() == {}
        assert keys.problem(), str(path)
        assert os.path.lexists(path)
        if path in (keys.FILE, keys._lock_file()):
            keys.set('github', 'fixture-refused')
            keys.save_logins({'torbox': {'apikey': 'fixture-refused'}})
            assert os.path.lexists(path)
        path.unlink()
""", timeout=5)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO replacement race requires POSIX")
    def test_regular_file_swapped_for_fifo_during_open_is_rejected_with_timeout(self):
        self.run_child("""from unittest import mock
source = keys.APP_DIR / 'fixture-source.json'
source.write_text('{"github":"fixture-old"}')
real_open = os.open
def swap(path, *args, **kwargs):
    if path == source:
        source.unlink()
        os.mkfifo(source)
    return real_open(path, *args, **kwargs)
with mock.patch.object(os, 'open', side_effect=swap):
    doc, problem = keys._open(source)
assert doc is None and problem
assert source.exists()
""", timeout=5)

    def test_oversized_and_directory_credentials_stay_unreadable(self):
        source = self.app / "fixture-large.json"
        source.write_bytes(b"x" * (self.keys._READ_LIMIT + 1))
        self.assertIsNone(self.keys._open(source)[0])
        self.assertTrue(self.keys._open(source)[1])
        directory = self.app / "fixture-directory"
        directory.mkdir()
        self.assertIsNone(self.keys._open(directory)[0])
        self.assertTrue(self.keys._open(directory)[1])

    def test_two_sequential_stale_login_snapshots_use_the_last_save(self):
        known = {"host": "known.invalid", "kind": "Cookie", "secret": "fixture-known"}
        self.keys.set("github", "fixture-kept-key")
        self.keys.save_logins({"archive.org": {"user": "fixture-user"}, "custom": [known]})
        first = self.keys.logins()
        second = self.keys.logins()  # both callers loaded the same initial snapshot
        first["torbox"] = {"apikey": "fixture-first-writer"}
        first["custom"][0]["secret"] = "fixture-first-secret"
        self.keys.save_logins(first)
        self.assertEqual(self.keys.logins(), first)
        second["archive.org"]["user"] = "fixture-last-writer"
        self.keys.save_logins(second)
        self.keys = self.load_module()
        self.assertEqual(self.keys.logins(), second)
        self.assertNotIn("torbox", self.keys.logins())
        self.assertEqual(self.keys.logins()["custom"][0]["secret"], "fixture-known")
        self.assertEqual(self.keys.get("github"), "fixture-kept-key")
        self.keys.set("github", "fixture-new-key")
        self.keys.set("sgdb", "fixture-unrelated-key")
        self.assertEqual(self.keys.get("github"), "fixture-new-key")
        self.keys.set("github", "")
        self.assertEqual(self.keys.get("github"), "")

    def test_forgotten_keys_and_logins_stay_deleted_in_a_fresh_process_with_retained_originals(self):
        sources = self.legacy_pair(self.app)
        originals = [source.read_bytes() for source in sources]
        self.assertEqual(self.keys.get("github"), "fixture-old-key")
        self.assertEqual(self.keys.logins()["torbox"], {"apikey": "fixture-old-login"})
        self.keys.save_logins(None)
        self.keys.set("github", "")
        self.keys.set("sgdb", "")
        self.run_child("assert keys.load() == {}\nassert keys.logins() == {}\n")
        self.assertEqual([source.read_bytes() for source in sources], originals)

    def test_unreadable_portable_unified_store_blocks_fallback_imports_permanently(self):
        sources = (*self.legacy_pair(self.app), *self.legacy_pair(self.keys.FILE.parent))
        portable = self.app / "distill_keys.json"
        portable.write_text(json.dumps({"keys": {}, "logins": {},
                                        "migrated": ["local:distill_apikeys.json", "local:distill_logins.json"]}))
        originals = [source.read_bytes() for source in (*sources, portable)]
        read = self.keys._open
        with mock.patch.object(self.keys, "_open", side_effect=lambda path:
                               (None, "fixture sealed elsewhere") if path == portable else read(path)):
            self.assertEqual(self.keys.load(), {})
            self.assertEqual(self.keys.logins(), {})
            self.assertIn("left untouched", self.keys.problem())
            self.assertIn("will not be imported later", self.keys.problem())
        self.keys.set("ra", "fixture-unrelated")
        self.keys = self.load_module()
        self.assertEqual(self.keys.load(), {"ra": "fixture-unrelated"})
        self.assertEqual(self.keys.logins(), {})
        self.assertEqual([source.read_bytes() for source in (*sources, portable)], originals)

    def test_portable_migration_markers_translate_to_known_app_sources_in_xdg(self):
        sources = self.legacy_pair(self.app)
        unlink = Path.unlink
        def retain(path, *args, **kwargs):
            if path in sources:
                raise PermissionError("fixture retained portable source")
            return unlink(path, *args, **kwargs)
        with mock.patch.dict(os.environ, {"DISTILL_PORTABLE": "1"}):
            portable = self.load_module()
            with mock.patch.object(Path, "unlink", retain):
                self.assertEqual(portable.get("github"), "fixture-old-key")
            portable.save_logins(None)
            portable.set("github", "")
            portable.set("sgdb", "")
            doc = portable._open(portable.FILE)[0]
            doc["migrated"] += ["app:distill_keys.json", "local:unknown.json", "app:unknown.json",
                                "other:distill_logins.json"]
            self.assertTrue(portable._write(doc))
        all_sources = (*sources, self.app / "distill_keys.json")
        originals = [source.read_bytes() for source in all_sources]
        self.assertEqual(self.keys.load(), {})
        self.assertEqual(self.keys.logins(), {})
        self.assertEqual(self.keys._open(self.keys.FILE)[0]["migrated"],
                         ["app:distill_apikeys.json", "app:distill_keys.json", "app:distill_logins.json", "finalized"])
        self.keys._cache = None
        self.assertEqual(self.keys.load(), {})
        self.assertEqual(self.keys.logins(), {})
        self.assertEqual([source.read_bytes() for source in all_sources], originals)

    def test_explicit_forget_never_restores_finalized_unreadable_sources(self):
        sources = self.legacy_pair(self.keys.FILE.parent)
        originals = [source.read_bytes() for source in sources]
        read = self.keys._open
        with mock.patch.object(downloads, "apikeys", self.keys), \
                mock.patch.dict(downloads.LOGINS, {}, clear=True):
            with mock.patch.object(self.keys, "_open", side_effect=lambda path:
                                   (None, "fixture locked source") if path in sources else read(path)):
                downloads.load_logins()
            downloads.save_logins(False)
            self.keys.set("github", "")
            self.assertEqual(self.keys.get("github"), "")
            self.keys = self.load_module()
            self.assertEqual(self.keys.logins(), {})
            self.assertEqual(self.keys.get("github"), "")
            self.assertEqual([source.read_bytes() for source in sources], originals)

    def test_linux_login_load_save_reload_preserves_the_complete_snapshot_and_keys(self):
        parent = {"host": "example.com", "kind": "Cookie", "user": "", "secret": "fixture-parent"}
        child = {"host": "dl.example.com", "kind": "Cookie", "user": "", "secret": "fixture-child"}
        self.keys.set("github", "fixture-kept-key")
        self.keys.save_logins({"archive.org": {"access": "fixture-access"},
                               "torbox": {"apikey": "fixture-login"}, "custom": [parent, child]})
        with mock.patch.object(downloads, "apikeys", self.keys), \
                mock.patch.dict(downloads.LOGINS, {}, clear=True):
            self.assertTrue(downloads.load_logins())
            snapshot = json.loads(json.dumps(downloads.LOGINS))
            downloads.save_logins(True)
            self.assertEqual(downloads.LOGINS, snapshot)
            self.assertEqual(self.keys.logins(), snapshot)
            with mock.patch.object(downloads, "apikeys", self.load_module()):
                self.assertTrue(downloads.load_logins())
                self.assertEqual(downloads.LOGINS, snapshot)
                self.assertEqual(downloads.apikeys.get("github"), "fixture-kept-key")
            self.assertEqual(self.keys.FILE, self.root / "config/distill/distill_keys.json")

    def test_saved_custom_hosts_are_specific_first_with_stable_ties(self):
        parent = {"host": "example.com", "kind": "Cookie", "user": "", "secret": "fixture-parent"}
        equal_a = {"host": "aaa.example.com", "kind": "Cookie", "user": "", "secret": "fixture-a"}
        equal_b = {"host": "bbb.example.com", "kind": "Cookie", "user": "", "secret": "fixture-b"}
        child = {"host": "dl.example.com", "kind": "Cookie", "user": "", "secret": "fixture-child"}
        with mock.patch.object(downloads, "apikeys", self.keys), \
                mock.patch.dict(downloads.LOGINS, {"custom": [parent, equal_b, equal_a, child]}, clear=True):
            downloads.save_logins(True)
            expected = [equal_b, equal_a, child, parent]
            self.assertEqual(downloads.LOGINS["custom"], expected)
            self.assertEqual(self.keys._open(self.keys.FILE)[0]["logins"]["custom"], expected)
            downloads.LOGINS["custom"][0]["secret"] = "fixture-changed"
            downloads.save_logins(True)
            self.assertEqual([e["host"] for e in downloads.LOGINS["custom"]], [e["host"] for e in expected])
            self.assertEqual(self.keys._open(self.keys.FILE)[0]["logins"]["custom"], downloads.LOGINS["custom"])

    def _qt_application(self):
        if os.environ.get("DISTILL_SKIP_UI_TESTS"):
            self.skipTest("UI tests disabled by DISTILL_SKIP_UI_TESTS")
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("optional Qt is unavailable")
        self._qt_app = QApplication.instance() or QApplication([])
        return self._qt_app

    def _login_dialog(self, saved):
        self._qt_application()
        from distill.ui.dialogs import LoginsDialog
        from PySide6.QtCore import QCoreApplication, QEvent
        for patch in (mock.patch.object(downloads, "apikeys", self.keys),
                      mock.patch.dict(downloads.LOGINS, {}, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.keys.save_logins(saved)
        downloads.load_logins()
        dialog = LoginsDialog(None, [], True, embedded=True)
        def cleanup():
            dialog._timer.stop()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(cleanup)
        dialog.show()
        self._qt_app.processEvents()
        return dialog

    def test_incomplete_custom_row_survives_save_and_autosaves_when_completed(self):
        parent = {"host": "example.com", "kind": "Cookie", "user": "", "secret": "fixture-parent"}
        dialog = self._login_dialog({"custom": [parent]})
        from PySide6.QtTest import QTest
        dialog._add_custom_row({})
        row = dialog.custom[-1]
        host, _kind, _user, secret, _gone = row
        host.setText("https://DL.Example.com/")
        secret.setFocus()
        self._qt_app.processEvents()
        self.assertIs(self._qt_app.focusWidget(), secret)
        dialog._save()
        self.assertIs(dialog.custom[-1], row)
        self.assertEqual(host.text(), "https://DL.Example.com/")
        self.assertEqual(secret.text(), "")
        self.assertIs(self._qt_app.focusWidget(), secret)
        self.assertEqual(self.keys.logins()["custom"], [parent])
        secret.setText("fixture-child")
        self.assertTrue(dialog._timer.isActive())
        QTest.qWait(dialog._timer.interval() + 150)
        self.assertFalse(dialog._timer.isActive())
        self.assertIs(dialog.custom[-1], row)
        self.assertEqual(host.text(), "https://DL.Example.com/")
        self.assertEqual(secret.text(), "fixture-child")
        self.assertIs(self._qt_app.focusWidget(), secret)
        expected = [{"host": "dl.example.com", "kind": "Basic (user + password)",
                     "user": "", "secret": "fixture-child"}, parent]
        self.assertEqual(downloads.LOGINS["custom"], expected)
        self.assertEqual(self.keys._open(self.keys.FILE)[0]["logins"]["custom"], expected)

    def test_embedded_dialog_saves_displayed_fields_without_replacing_editors(self):
        dialog = self._login_dialog({"archive.org": {"access": "fixture-start"}, "custom": []})
        edit = dialog.fields[("archive.org", "access")]
        edit.setFocus()
        self._qt_app.processEvents()
        for value in ("fixture-first", "fixture-second"):
            edit.setText(value)
            dialog._save()
            self.assertIs(dialog.fields[("archive.org", "access")], edit)
            self.assertIs(self._qt_app.focusWidget(), edit)
            self.assertEqual(edit.text(), value)
            self.assertEqual(self.keys.logins()["archive.org"]["access"], value)
            self.assertEqual(downloads.LOGINS["archive.org"]["access"], value)

    def test_credential_editors_show_refused_saves_without_success_signals(self):
        self._qt_application()
        from distill.ui import apikeys_card, games_dialog
        from PySide6.QtCore import QCoreApplication, QEvent
        with mock.patch.object(self.keys, "_try_lock", side_effect=OSError(errno.EIO, "fixture lock failed")):
            self.assertEqual(self.keys.logins(), {})
        dialog = self._login_dialog({})
        saved = mock.Mock()
        dialog.saved.connect(saved)
        dialog.fields[("archive.org", "access")].setText("fixture-refused")
        dialog._save()
        self.assertFalse(dialog.ok)
        saved.assert_not_called()
        self.assertIn("read-only this session", dialog._save_problem.text())
        self.assertFalse(dialog._save_problem.isHidden())
        self.assertEqual(downloads.LOGINS, {"custom": []})
        with mock.patch.object(apikeys_card, "apikeys", self.keys):
            panel = apikeys_card.ApiKeysPanel()
            try:
                changed = mock.Mock()
                panel.changed.connect(changed)
                panel.edits["github"].setText("fixture-refused")
                panel._save()
                changed.assert_not_called()
                self.assertIn("read-only this session", panel._save_problem.text())
                self.assertFalse(panel._save_problem.isHidden())
                self.assertEqual(self.keys.get("github"), "")
            finally:
                panel._timer.stop()
                panel.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        ra = mock.Mock()
        ra.user.text.return_value = "fixture-user"
        ra.key.text.return_value = "fixture-key"
        with mock.patch.object(games_dialog, "apikeys", self.keys):
            games_dialog.RetroAchievementsDialog._save(ra)
        ra.accept.assert_not_called()
        ra._say.assert_called_once_with(self.keys.problem())

if __name__ == "__main__":
    unittest.main()
