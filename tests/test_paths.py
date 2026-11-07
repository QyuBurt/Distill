"""Linux runtime locations, including creation at the actual persistence point."""
from __future__ import annotations

import os
import importlib.util
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from distill import downloads, netrules, soundtracks, tools, torrents, ytdlp

HOST_LINUX = sys.platform.startswith("linux")
HOST_POSIX = os.name == "posix"


class ConsumerPathsTests(unittest.TestCase):
    def test_policy_fixture_selects_linux_on_mocked_legacy_hosts(self):
        for platform in ("win32", "darwin"):
            with self.subTest(mocked_host=platform), mock.patch.object(sys, "platform", platform):
                case = PathsTests("test_defaults_overrides_invalid_and_cwd_independence")
                result = unittest.TestResult()
                case.run(result)
                self.assertTrue(result.wasSuccessful(), str(result.failures + result.errors))

    @unittest.skipUnless(HOST_LINUX, "import-time XDG constants require a native Linux host")
    def test_linux_constants_use_xdg_categories(self):
        from distill import apikeys, crashlog, formats, torrentsearch
        for actual, category in ((downloads.LOGIN_FILE, "CONFIG"), (downloads.SESSION_FILE, "STATE"),
                                 (netrules.RULES_FILE, "CONFIG"), (soundtracks.CACHE_FILE, "CACHE"),
                                 (torrents.TORRENT_DIR, "STATE"), (torrents.TORRENT_FILE, "CONFIG"),
                                 (ytdlp.SETTINGS_FILE, "CONFIG"), (soundtracks.PREFS_FILE, "CONFIG"),
                                 (soundtracks.SAVES_FILE, "STATE"), (apikeys.FILE, "CONFIG"),
                                 (crashlog.CRASH_FILE, "STATE"), (formats.PS3_NAME_CACHE, "CACHE"),
                                 (torrentsearch.TRACKERS_FILE, "CACHE")):
            with self.subTest(path=actual.name):
                self.assertEqual(actual.parent, Path(os.environ[f"XDG_{category}_HOME"]) / "distill")

    def test_persistence_creates_missing_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "fresh" / "config" / "distill_rules.json"
            with mock.patch.object(netrules, "RULES_FILE", target), mock.patch.object(netrules.RULES, "global_kbps", 1):
                netrules.save_rules()
            self.assertTrue(target.is_file(), "save_rules silently lost data when parent was absent")

    def test_tool_resolution_is_pure_and_uses_data(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "missing"
            with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}):
                manager = tools.ToolManager(lambda _: None)
                self.assertEqual(manager.root, data / "distill" / "tools")
                self.assertEqual(ytdlp.tools_folder(), data / "distill" / "tools" / "ytdlp")
            self.assertFalse(data.exists())


class PathsTests(unittest.TestCase):
    def setUp(self):
        if HOST_POSIX:
            previous_umask = os.umask(0o077)
            self.addCleanup(os.umask, previous_umask)
        self.assertIsNotNone(importlib.util.find_spec("distill.paths"), "Qt-free path policy is missing")
        from distill import paths
        self.paths = paths
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        for patch in (mock.patch.object(sys, "platform", "linux"),
                      mock.patch.object(Path, "home", return_value=self.base / "home")):
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.base / "home"), "DISTILL_PORTABLE": "",
                                            **{f"XDG_{k}_HOME": "" for k in ("CONFIG", "DATA", "STATE", "CACHE")}})
        patch.start()
        self.addCleanup(patch.stop)

    def test_defaults_overrides_invalid_and_cwd_independence(self):
        before = list(self.base.iterdir())
        defaults = {"config": ".config", "data": ".local/share", "state": ".local/state", "cache": ".cache"}
        for category, suffix in defaults.items():
            getter = getattr(self.paths, category + "_dir")
            expected = self.base / "home" / suffix / "distill"
            for value in ("", "relative", "~/elsewhere", "bad\0value", "/bad\0value"):
                with self.subTest(category=category, value=value), mock.patch.object(os, "environ", dict(os.environ, **{f"XDG_{category.upper()}_HOME": value})):
                    self.assertEqual(getter(), expected)
            with mock.patch.dict(os.environ, {f"XDG_{category.upper()}_HOME": str(self.base / category)}):
                self.assertEqual(getter(), self.base / category / "distill")
        previous = Path.cwd()
        try:
            os.chdir(self.base)
            self.assertEqual(self.paths.state_dir(), self.base / "home/.local/state/distill")
        finally:
            os.chdir(previous)
        self.assertEqual(list(self.base.iterdir()), before)

    def test_platform_and_exact_portable_selection(self):
        for platform in ("win32", "darwin", "freebsd"):
            with self.subTest(mocked_platform=platform), mock.patch.object(sys, "platform", platform):
                for category in ("config", "data", "state", "cache"):
                    self.assertEqual(getattr(self.paths, category + "_dir")(), self.paths.APP_DIR)
        for value in ("0", "true", ""):
            with mock.patch.dict(os.environ, {"DISTILL_PORTABLE": value}):
                self.assertNotEqual(self.paths.data_dir(), self.paths.APP_DIR)
        with mock.patch.dict(os.environ, {"DISTILL_PORTABLE": "1"}):
            self.assertEqual(self.paths.data_dir(), self.paths.APP_DIR)
            blocked = self.base / "file"
            blocked.write_text("blocked")
            with self.assertRaises(OSError):
                self.paths.ensure_dir(blocked / "child")
            self.assertEqual(self.paths.data_dir(), self.paths.APP_DIR)

    def test_no_home_is_a_clear_error(self):
        with mock.patch.object(Path, "home", side_effect=RuntimeError("Could not determine home directory")):
            with self.assertRaisesRegex(RuntimeError, "home"):
                self.paths.config_dir()

    @unittest.skipUnless(HOST_POSIX, "directory mode preservation requires POSIX permissions")
    def test_owner_only_new_components_existing_modes_preserved(self):
        existing = self.base / "existing"
        previous_umask = os.umask(0o077)
        try:
            existing.mkdir(mode=0o755)
        finally:
            os.umask(previous_umask)
        existing.chmod(0o755)
        self.paths.ensure_parent(existing / "one/two/file")
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o755)
        for folder in (existing / "one", existing / "one/two"):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        with mock.patch("os.chmod", side_effect=PermissionError("no chmod")):
            self.paths.ensure_dir(existing / "three")

    def test_root_and_cache_categories_and_assets(self):
        from distill import constants, emulators, recomps
        with mock.patch.object(recomps, "ROOT", None):
            self.assertEqual(recomps.root(), self.paths.data_dir() / "recomps")
            self.assertEqual(recomps.cache_root(), self.paths.cache_dir() / "recomps")
            self.assertEqual(recomps.Store().path, recomps.root() / "state.json")
            self.assertEqual(emulators.store_path(), recomps.root() / "emulators_state.json")
            for path in (recomps.art_dir(), recomps._source_cache("test"), emulators._relation_dir()):
                self.assertIn(recomps.cache_root(), path.parents)
            self.assertIn(recomps.cache_root(), emulators.logo_file(emulators.load_catalog()[0]).parents)
        self.assertTrue((constants.ASSETS_DIR / "recomps.json").is_file())
        self.assertTrue(recomps.load_catalog())
        self.assertFalse((self.base / "home").exists())

    def test_explicit_and_app_relative_tools_keep_their_meaning(self):
        from distill import system
        folder = self.base / "app/tools/dolphintool"
        folder.mkdir(parents=True)
        exe = folder / "DolphinTool.exe"
        exe.write_bytes(b"synthetic")
        with mock.patch.object(system, "APP_DIR", self.base / "app"):
            self.assertEqual(system.resolve_tool_path(str(exe)), str(exe))
            self.assertEqual(system.resolve_tool_path("./tools/dolphintool/DolphinTool.exe"), str(exe))
            self.assertEqual(system.resolve_dolphintool(str(exe)), str(exe))
            self.assertEqual(system.resolve_dolphintool("./tools/dolphintool"), str(exe))

    @unittest.skipUnless(HOST_POSIX, "symlink policy fixture requires POSIX symlink support")
    def test_xdg_symlink_path_is_not_resolved(self):
        target = self.base / "target"
        target.mkdir()
        link = self.base / "link"
        link.symlink_to(target, target_is_directory=True)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(link)}):
            self.assertEqual(self.paths.config_dir(), link / "distill")


@unittest.skipUnless(HOST_POSIX and hasattr(os, "O_NOFOLLOW"), "migration integration requires POSIX no-follow, FIFO and file permissions")
class MigrationTests(PathsTests):
    # Inherit the same disposable path policy fixture; all input is synthetic.
    def setUp(self):
        super().setUp()
        self.legacy = self.base / "legacy"
        self.legacy.mkdir()
        patch = mock.patch.object(self.paths, "APP_DIR", self.legacy)
        patch.start()
        self.addCleanup(patch.stop)

    def test_allowlist_byte_copy_idempotence_and_deferred_names(self):
        allowed = ["distill_settings.json", "distill_emulators_catalog.json", "distill_recomps_catalog.json",
                   "distill_forks.json", "distill_ytdlp.json", "distill_torrent_settings.json", "distill_soundtracks.json"]
        keys = ("cleanup", "dupes", "onegr", "discfix", "patch", "tidy", "multi", "playlists", "bysystem", "datcheck", "bioscheck", "report")
        state = ["distill_soundtrack_saves.json", *[f"distill_undo_{k}.json" for k in keys]]
        deferred = ["distill_apikeys.json", "distill_logins.json", "distill_rules.json", "distill_download_session.json",
                    "distill_crash.txt", "distill_motion.txt", "distill_organise_log.txt", "distill_torbox_debug.json",
                    "distill_soundtrack_cache.json", "distill_undo_unknown.json"]
        raw = b"arbitrary bytes\0\xff\n"
        for name in allowed + state + deferred:
            (self.legacy / name).write_bytes(raw)
        for name in ("tools", "recomps", "distill_torrents", "icon_cache"):
            (self.legacy / name).mkdir()
        result = self.paths.migrate_legacy()
        self.assertEqual(sum(r["status"] == "copied" for r in result), len(allowed + state))
        for names, dest in ((allowed, self.paths.config_dir()), (state, self.paths.state_dir())):
            for name in names:
                self.assertEqual((dest / name).read_bytes(), raw)
                self.assertEqual(stat.S_IMODE((dest / name).stat().st_mode), 0o600)
                self.assertEqual((self.legacy / name).read_bytes(), raw)
        self.assertFalse(any(r["status"] == "copied" for r in self.paths.migrate_legacy()))
        self.assertEqual({p.name for p in self.paths.config_dir().iterdir()}, set(allowed))
        self.assertEqual({p.name for p in self.paths.state_dir().iterdir()}, set(state))

    def test_existing_file_and_symlink_destinations_win(self):
        self.paths.ensure_dir(self.paths.config_dir())
        for name in ("distill_settings.json", "distill_forks.json"):
            (self.legacy / name).write_bytes(b"old")
        target = self.base / "target"
        target.write_bytes(b"new")
        (self.paths.config_dir() / "distill_settings.json").write_bytes(b"new")
        link = self.paths.config_dir() / "distill_forks.json"
        link.symlink_to(target)
        self.paths.migrate_legacy()
        self.assertEqual((self.paths.config_dir() / "distill_settings.json").read_bytes(), b"new")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"new")

    def test_unsafe_sources_and_empty_startup_create_nothing(self):
        self.assertEqual(self.paths.migrate_legacy(), [])
        self.assertFalse((self.base / "home").exists())
        target = self.base / "target"
        target.write_text("secret")
        (self.legacy / "distill_settings.json").symlink_to(target)
        (self.legacy / "distill_forks.json").mkdir()
        with (self.legacy / "distill_ytdlp.json").open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        result = self.paths.migrate_legacy()
        self.assertEqual(len(result), 3)
        self.assertTrue(all(r["status"] == "skipped" for r in result))
        self.assertFalse((self.base / "home").exists())

    def test_concurrent_destination_and_partial_failure(self):
        (self.legacy / "distill_settings.json").write_bytes(b"source")
        destination = self.paths.config_dir() / "distill_settings.json"
        def raced_link(source, dest):
            Path(dest).write_bytes(b"winner")
            raise FileExistsError("race")
        with mock.patch.object(os, "link", side_effect=raced_link):
            result = self.paths.migrate_legacy()
        self.assertEqual(destination.read_bytes(), b"winner")
        self.assertFalse(any(r["status"] == "copied" for r in result))
        destination.unlink()
        def partial_copy(source, destination):
            destination.write(b"partial")
            raise OSError("partial copy")
        with mock.patch("shutil.copyfileobj", side_effect=partial_copy):
            result = self.paths.migrate_legacy()
        self.assertEqual(result[0]["status"], "failed")
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.iterdir()), [])

    def test_publication_failure_continues_and_destination_directory_wins(self):
        for name in ("distill_settings.json", "distill_forks.json", "distill_ytdlp.json"):
            (self.legacy / name).write_bytes(b"synthetic")
        self.paths.ensure_dir(self.paths.config_dir() / "distill_ytdlp.json")
        original = os.link
        def fail_one(source, destination):
            if Path(destination).name == "distill_settings.json":
                raise OSError("cannot publish")
            return original(source, destination)
        with mock.patch.object(os, "link", side_effect=fail_one):
            result = self.paths.migrate_legacy()
        statuses = {Path(r["source"]).name: r["status"] for r in result}
        self.assertEqual(statuses, {"distill_settings.json": "failed", "distill_forks.json": "copied", "distill_ytdlp.json": "skipped"})
        self.assertFalse((self.paths.config_dir() / "distill_settings.json").exists())
        self.assertFalse(list(self.paths.config_dir().glob(".distill-migrate-*")))

    def test_fifo_is_skipped_and_open_identity_is_rechecked(self):
        fifo = self.legacy / "distill_settings.json"
        os.mkfifo(fifo)
        regular = self.legacy / "distill_forks.json"
        regular.write_bytes(b"synthetic")
        info = regular.stat()
        wrong = mock.Mock(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1)
        with mock.patch.object(os, "fstat", return_value=wrong):
            result = self.paths.migrate_legacy()
        self.assertEqual([r["status"] for r in result], ["skipped", "failed"])
        self.assertFalse((self.base / "home").exists())

    def test_portable_and_mocked_windows_are_noops(self):
        (self.legacy / "distill_settings.json").write_text("old")
        with mock.patch.dict(os.environ, {"DISTILL_PORTABLE": "1"}):
            self.assertEqual(self.paths.migrate_legacy(), [])
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(self.paths.migrate_legacy(), [])
        self.assertFalse((self.base / "home").exists())
