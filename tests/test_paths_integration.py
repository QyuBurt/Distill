"""Persistence and fresh-process evidence for the Linux path policy. No network or real secrets."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from distill import apikeys, crashlog, downloads, formats, netrules, organize, soundtracks, torrents, torrentsearch, ytdlp

ROOT = Path(__file__).resolve().parent.parent
HOST_LINUX = sys.platform.startswith("linux")


def child_env(base):
    env = dict(os.environ, HOME=str(base / "home"), USERPROFILE=str(base / "home"),
               PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen")
    env.pop("DISTILL_PORTABLE", None)
    for category in ("CONFIG", "DATA", "STATE", "CACHE"):
        env[f"XDG_{category}_HOME"] = str(base / category.lower())
    return env


class InstallerCleanupTests(unittest.TestCase):
    def test_creation_failures_balance_busy_events_and_leave_no_scratch(self):
        from distill import tools
        with tempfile.TemporaryDirectory() as temp:
            for cls in (tools.ToolManager, tools.FlatToolManager, tools.YtdlpManager):
                for stage in ("directory", "scratch"):
                    with self.subTest(manager=cls.__name__, stage=stage):
                        target = Path(temp) / cls.__name__ / stage
                        events = []
                        with mock.patch.object(cls, "_pick_root", return_value=target):
                            manager = cls(events.append)
                        manager.KEY = "fixture"
                        release = tools.Release("v1", "fixture", "https://invalid.test/fixture", 1, None)
                        owner, name = (tools.paths, "ensure_dir") if stage == "directory" else (tools.tempfile, "mkdtemp")
                        with mock.patch.object(owner, name, side_effect=OSError("injected creation failure")):
                            with self.assertRaisesRegex(OSError, "injected creation failure"):
                                manager._install(release)
                        self.assertEqual(events, [("fixture_busy", True), ("fixture_busy", False)])
                        self.assertEqual(list(target.iterdir()) if target.exists() else [], [])


@unittest.skipUnless(HOST_LINUX, "persistence integration requires native Linux XDG routing and POSIX modes")
class PersistenceTests(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)

    @unittest.skipIf(os.name == "nt", "package fixture uses POSIX executable stand-ins")
    def test_batch_fixture_keeps_package_logs_in_its_own_directory(self):
        from tests.test_batch import PkgRunTests
        case = PkgRunTests("test_folder_layout_names_folders_after_the_files")
        try:
            case.setUp()
            case.test_folder_layout_names_folders_after_the_files()
            self.assertTrue((case.tmp / "app" / "pkg_logs").is_dir(),
                            "package fixture left its logs in the shared sandbox state directory")
        finally:
            case.doCleanups()

    def test_emulator_and_recomp_cache_writers_create_missing_parents(self):
        from distill import emulators, recomps
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            emulator = emulators.Emulator(id="fixture", name="Fixture", system="Fixture", repo="fixture/fixture")
            png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
            for kind in ("relation", "logo", "logo-source", "sgdb", "source", "poster", "miss"):
                with self.subTest(writer=kind), mock.patch.object(recomps, "cache_root", return_value=base / kind / "cache"):
                    if kind == "relation":
                        self.assertIsNotNone(emulators.load_relation(fetch=lambda _: b"{}"))
                        target = emulators._relation_dir() / emulators.RELATION_FILES[0].replace("/", "_")
                    elif kind == "logo":
                        target = emulators.fetch_logo(emulator, fetch=lambda _: png)
                    elif kind == "logo-source":
                        emulators._set_logo_source(emulator, "synthetic")
                        target = emulators.logo_source_file(emulator)
                    elif kind == "sgdb":
                        with mock.patch.object(emulators, "bundled_image", return_value=None):
                            target = emulators.fetch_logo_best(emulator, "synthetic", finder=lambda *a, **k: (png, None))
                    elif kind == "source":
                        with mock.patch.object(recomps, "_get", return_value=b'{"fixture": true}'):
                            self.assertEqual(recomps._read_source("https://invalid.test/source", False), {"fixture": True})
                        target = recomps._source_cache("https://invalid.test/source")
                    else:
                        recomp = recomps.Recomp(id="fixture", name="Fixture", game="Fixture", system="Fixture", repo="fixture/fixture")
                        urls = ["https://invalid.test/poster"] if kind == "poster" else []
                        with mock.patch.object(recomps, "launchbox_covers", return_value=urls), \
                                mock.patch.object(recomps, "_image", return_value=png), \
                                mock.patch.object(recomps, "is_portrait", return_value=True):
                            recomps.fetch_cover(recomp)
                        target = recomps.art_dir() / ("fixture.png" if kind == "poster" else "fixture.none")
                    self.assertIsNotNone(target)
                    self.assertTrue(target.is_file())
                    self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_recomp_store_and_discovery_create_missing_parents(self):
        from distill import recomps
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            store = recomps.Store(base / "new/data/state.json")
            store.data["fixture"] = True
            store.save()
            self.assertEqual(json.loads(store.path.read_text()), {"fixture": True})
            cache = base / "new/cache/recomps"
            with mock.patch.object(recomps, "cache_root", return_value=cache), \
                    mock.patch.object(recomps, "_get", return_value=b'{"items": []}'):
                recomps.discover(force=True)
            self.assertTrue((cache / "discover.json").is_file())
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o700)

    def test_fork_writer_creates_missing_parent(self):
        from distill import emulators
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "new/config/forks.json"
            emulators._write_fork_entries(target, [{"fixture": True}])
            self.assertEqual(json.loads(target.read_text()), {"forks": [{"fixture": True}]})
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_undo_rewrite_creates_missing_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "new/state/undo.json"
            runs = [{"items": [], "made": "one"}, {"items": [], "made": "two"}]
            with mock.patch.object(organize, "undo_file", return_value=target), \
                    mock.patch.object(organize, "_load_runs", return_value=runs):
                self.assertEqual(organize.undo_last("tidy"), (0, []))
            self.assertEqual(json.loads(target.read_text()), {"runs": runs[:1]})

    def test_torrent_directory_is_created_owner_only(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "fresh/state/torrents"
            lt = mock.Mock()
            info = lt.torrent_info.return_value
            info.num_files.return_value = 1
            info.name.return_value = "synthetic"
            info.files.return_value.file_path.return_value = "synthetic.rom"
            info.files.return_value.file_size.return_value = 1
            with mock.patch.object(torrents, "TORRENT_DIR", target), \
                    mock.patch.object(torrents, "torrent_hash", return_value="a" * 40), \
                    mock.patch.object(torrents.ENGINE, "note"):
                self.assertEqual(len(torrents.torrent_files_from_bytes(lt, b"synthetic")), 1)
            self.assertEqual((target / ("a" * 40 + ".torrent")).read_bytes(), b"synthetic")
            for folder in (target, target.parent, target.parent.parent):
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

    def test_crash_handler_install_creates_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "fresh/state/crash.txt"
            with mock.patch.object(crashlog, "CRASH_FILE", target), mock.patch.object(crashlog, "_fault_stream", None), \
                    mock.patch.object(sys, "excepthook"), mock.patch.object(threading, "excepthook"), \
                    mock.patch.object(crashlog.faulthandler, "enable") as enable:
                try:
                    crashlog.install()
                    self.assertTrue(target.is_file())
                    enable.assert_called_once_with(crashlog._fault_stream)
                    self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
                finally:
                    if crashlog._fault_stream is not None:
                        crashlog._fault_stream.close()

    def test_torbox_debug_creates_state_parent(self):
        from distill import torbox
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            ih = "a" * 40
            info = {"name": "synthetic", "files": [{"name": "fixture.rom", "size": 1}]}
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(base / "new/state")}), \
                    mock.patch.object(torbox, "torbox_key", return_value="synthetic"), \
                    mock.patch.object(torbox, "torbox_cached", return_value={ih: info}), \
                    mock.patch.object(torbox.ENGINE, "note"):
                self.assertTrue(torbox.torbox_list_magnet(f"magnet:?xt=urn:btih:{ih}"))
            target = base / "new/state/distill/distill_torbox_debug.json"
            self.assertEqual(json.loads(target.read_text()), info)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_pkg_logs_create_state_directory(self):
        from distill import batch
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            runner = mock.MagicMock()
            for name in ("is_ps5_pkg", "packs_zar", "ps5_kyty", "ps5_relink"):
                setattr(runner.cfg, name, False)
            runner._ctx.src.attached = ()
            runner._run_gated.return_value = (1, "synthetic failure")
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(base / "new/state")}), \
                    mock.patch.object(batch, "read_pkg_info", return_value=None):
                batch.BatchRunner._run_pkg(runner, base / "fixture.pkg", base / "part", 1, "fixture", 1)
            target = base / "new/state/distill/pkg_logs"
            self.assertTrue(target.is_dir())
            self.assertEqual(runner._run_gated.call_args.kwargs["cwd"], target)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_pkg_logs_failure_does_not_launch_in_another_directory(self):
        from distill import batch
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            blocker = base / "blocker"
            blocker.write_text("not a directory")
            for portable in ("", "1"):
                with self.subTest(portable=portable):
                    runner = mock.MagicMock()
                    runner.cfg.is_ps5_pkg = False
                    runner._ctx.src.attached = ()
                    runner._run_gated.return_value = (1, "synthetic")
                    with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(blocker), "DISTILL_PORTABLE": portable}), \
                            mock.patch.object(batch, "APP_DIR", blocker), \
                            mock.patch.object(batch, "read_pkg_info", return_value=None):
                        result = batch.BatchRunner._run_pkg(runner, base / "fixture.pkg", base / "part", 1, "fixture", 1)
                    self.assertEqual(result.rc, 1)
                    runner._run_gated.assert_not_called()
                    self.assertIn(str(blocker), result.output)

    def test_both_tool_installers_and_ytdlp_create_root_at_write(self):
        from distill import tools
        with tempfile.TemporaryDirectory() as temp:
            for cls in (tools.ToolManager, tools.FlatToolManager, tools.YtdlpManager):
                with self.subTest(manager=cls.__name__):
                    target = Path(temp) / cls.__name__ / "new/tools"
                    with mock.patch.object(cls, "_pick_root", return_value=target):
                        manager = cls(lambda _: None)
                    self.assertFalse(target.exists())
                    manager.EXE_NAME = "fixture"
                    manager.TAG_RE = r"v\d+"
                    release = tools.Release("v1", "fixture.bin", "https://invalid.test/fixture", 1, None)
                    def unpack(archive, scratch, destination, manager=manager):
                        destination.mkdir(exist_ok=True)
                        (destination / manager.EXE_NAME).write_bytes(b"fixture")
                    def download(release, destination, target=target):
                        self.assertTrue(target.is_dir(), "root must exist before downloading")
                        destination.write_bytes(b"fixture")
                    with mock.patch.object(manager, "_download", side_effect=download, create=True), \
                            mock.patch.object(manager, "_unpack", side_effect=unpack, create=True), \
                            mock.patch.object(manager, "_probe", return_value="fixture", create=True):
                        manager._install(release)
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
                    exe = target / "v1/fixture" if cls is tools.ToolManager else target / "fixture"
                    self.assertEqual(exe.read_bytes(), b"fixture")

    def test_each_non_ui_writer_creates_a_missing_parent(self):
        cases = [(netrules, "RULES_FILE", netrules.save_rules),
                 (torrents, "TORRENT_FILE", lambda: torrents.save_torrent_settings({})),
                 (soundtracks, "PREFS_FILE", lambda: soundtracks.save_prefs({"test": True})),
                 (soundtracks, "SAVES_FILE", lambda: soundtracks._write_saves([{"test": True}])),
                 (soundtracks, "CACHE_FILE", lambda: soundtracks._save_cache({"test": {"t": 1}})),
                 (ytdlp, "SETTINGS_FILE", ytdlp.save_settings),
                 (crashlog, "CRASH_FILE", lambda: crashlog.record(ValueError, ValueError("synthetic"), None)),
                 (apikeys, "FILE", lambda: apikeys.set("github", "synthetic")),
                 (downloads, "LOGIN_FILE", lambda: downloads.save_logins(True))]
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(netrules.RULES, "global_kbps", 1), \
                mock.patch.dict(downloads.LOGINS, {"custom": [{"host": "invalid.test", "secret": "synthetic"}]}):
            for index, (module, name, writer) in enumerate(cases):
                with self.subTest(writer=module.__name__ + "." + name):
                    target = Path(temp) / str(index) / "new" / "file.json"
                    with mock.patch.object(module, name, target):
                        writer()
                    self.assertTrue(target.is_file(), "writer silently lost data")
    def test_ps3_cache_creates_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            iso = Path(temp) / "game.iso"
            iso.write_bytes(b"test")
            target = Path(temp) / "ps3/new/cache.json"
            with mock.patch.object(formats, "PS3_NAME_CACHE", target):
                formats.ps3_remember_name(iso, "Test")
                self.assertEqual(formats.ps3_cached_name(iso), "Test")
            self.assertTrue(target.is_file())
    def test_trackers_create_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "trackers/new/list.txt"
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b"udp://one/announce\nudp://two/announce\nudp://three/announce\n"
            with mock.patch.object(torrentsearch, "TRACKERS_FILE", target), mock.patch.object(torrentsearch, "http_open", return_value=response):
                torrentsearch.refresh_trackers()
            self.assertTrue(target.is_file())

    def test_undo_and_run_log_create_parents(self):
        with tempfile.TemporaryDirectory() as temp:
            undo, log = Path(temp) / "undo/new/history.json", Path(temp) / "log/new/history.txt"
            with mock.patch.object(organize, "undo_file", return_value=undo):
                organize._save_undo("tidy", [], "test")
            self.assertTrue(undo.is_file())
            finding = mock.Mock(title="synthetic", ops=[])
            with mock.patch.object(organize, "run_log_file", return_value=log):
                organize._append_run_log("test", "test", [finding])
            self.assertTrue(log.is_file())

    def test_ui_settings_motion_icon_and_session_parents(self):
        try:
            from distill.ui import persist
        except ImportError:
            self.skipTest("PySide6 unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "settings/new/file.json"
            with mock.patch.object(persist, "SETTINGS_FILE", target):
                persist.write_settings({"test": True})
                self.assertEqual(persist.read_settings(), {"test": True})
            self.assertTrue(target.is_file())
    def test_motion_parent(self):
        try:
            from distill.ui import motion
        except ImportError:
            self.skipTest("PySide6 unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "motion/new/log.txt"
            with mock.patch.object(motion, "MOTION_LOG", target):
                motion._Probe("synthetic").done()
            self.assertTrue(target.is_file())
    def test_icon_parent(self):
        try:
            from distill.ui import title_dialog
        except ImportError:
            self.skipTest("PySide6 unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            folder = base / "icon/new/cache"
            fake = mock.Mock()
            hit = mock.Mock(file_stem="synthetic", title="test", source="fixture")
            with mock.patch.object(title_dialog, "ICON_CACHE", folder):
                title_dialog.TitleDialog._take_online_icon(fake, b"\x89PNGsynthetic", hit)
            self.assertTrue((folder / "synthetic.png").is_file())
    def test_session_parent(self):
        try:
            from distill.ui import download_page
        except ImportError:
            self.skipTest("PySide6 unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "session/new/session.json"
            fake = mock.MagicMock()
            fake.dl_files = [downloads.RemoteFile("a", "https://invalid.test/a", 1)]
            fake.dl_model.removed = set()
            fake.dl_checked = set()
            fake._dl_prio.return_value = set()
            fake.DL_COLS = {}
            fake.dl_threads.value.return_value = 1
            fake.dl_post.currentIndex.return_value = 0
            for field in ("dl_verify", "dl_folders", "dl_autoresume", "dl_organize", "dl_clip"):
                getattr(fake, field).isChecked.return_value = False
            for field in ("dl_url_text", "dl_dest", "dl_filter"):
                getattr(fake, field).text.return_value = ""
                getattr(fake, field).toPlainText.return_value = ""
            with mock.patch.object(download_page, "SESSION_FILE", target):
                download_page.DownloadMixin._dl_save_session(fake)
            self.assertTrue(target.is_file())

    def test_unwritable_destination_never_relocates(self):
        with tempfile.TemporaryDirectory() as temp:
            blocker = Path(temp) / "blocker"
            blocker.write_text("blocked")
            with mock.patch.object(netrules, "RULES_FILE", blocker / "file.json"):
                netrules.save_rules()  # existing policy swallows OSError
            self.assertEqual(list(Path(temp).iterdir()), [blocker])
            self.assertEqual(blocker.read_text(), "blocked")


class ProcessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.copy = Path(temp.name) / "source"
        shutil.copytree(ROOT, self.copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv"))

    def run_child(self, code, env, cwd, root=ROOT, timeout=90):
        if root == ROOT:
            root = self.copy
        result = subprocess.run([sys.executable, "-B", "-c", "import sys; sys.path.insert(0, " + repr(str(root)) + ");\n" + code],
                                env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "FIFO race requires POSIX FIFOs and alarms")
    def test_fifo_swap_during_migration_is_bounded_and_later_files_continue(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            code = """import os, signal, stat
from pathlib import Path
from unittest import mock
from distill import paths
base = Path(os.environ['HOME']).parent
legacy = base / 'legacy'
legacy.mkdir()
first = legacy / 'distill_settings.json'
backup = legacy / 'original-settings'
first.write_bytes(b'original source')
later = legacy / 'distill_forks.json'
later.write_bytes(b'later source')
original_open = os.open
def swap(path, flags, *args, **kwargs):
    if Path(path) == first and not backup.exists():
        first.rename(backup)
        os.mkfifo(first)
    return original_open(path, flags, *args, **kwargs)
def deadline(*_):
    raise AssertionError('migration blocked opening a swapped FIFO')
signal.signal(signal.SIGALRM, deadline)
signal.alarm(2)
try:
    with mock.patch.object(paths.sys, 'platform', 'linux'), mock.patch.object(paths, 'APP_DIR', legacy), mock.patch.object(os, 'open', side_effect=swap):
        result = paths.migrate_legacy()
finally:
    signal.alarm(0)
assert {Path(r['source']).name: r['status'] for r in result} == {'distill_settings.json': 'failed', 'distill_forks.json': 'copied'}
assert stat.S_ISFIFO(first.lstat().st_mode)
assert backup.read_bytes() == b'original source'
assert later.read_bytes() == b'later source'
destination = Path(os.environ['XDG_CONFIG_HOME']) / 'distill'
assert (destination / later.name).read_bytes() == b'later source'
assert not (destination / first.name).exists()
"""
            self.run_child(code, child_env(base), base, timeout=8)

    @unittest.skipUnless(HOST_LINUX, "optional Qt writer checks exercise Linux persistence tests")
    def test_optional_qt_writer_tests_skip_when_pyside_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            code = """import sys, unittest
class NoQt:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PySide6' or fullname.startswith('PySide6.'):
            raise ModuleNotFoundError('PySide6 deliberately blocked', name=fullname)
sys.meta_path.insert(0, NoQt())
names = ['tests.test_paths_integration.PersistenceTests.' + name for name in (
    'test_ui_settings_motion_icon_and_session_parents', 'test_motion_parent', 'test_icon_parent', 'test_session_parent')]
result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
assert result.wasSuccessful(), 'optional Qt tests failed instead of skipping'
assert len(result.skipped) == 4, result.skipped
"""
            self.run_child(code, child_env(base), base)

    @unittest.skipUnless(HOST_LINUX, "source purity is the default Linux XDG launch contract")
    def test_purity_oracle_rejects_source_directory_creation(self):
        target = self.copy / "distill/tools.py"
        target.write_text(target.read_text() + '\n(APP_DIR / "purity_probe").mkdir(exist_ok=True)\n')
        with self.assertRaisesRegex(AssertionError, "source"):
            self.test_import_resolution_and_manager_construction_create_nothing()

    @unittest.skipUnless(HOST_LINUX, "legacy ToolManager construction intentionally probes writable directories")
    def test_import_resolution_and_manager_construction_create_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            code = """from pathlib import Path
import os, stat, hashlib
def snapshot(root):
    result = {}
    for path in (root, *root.rglob('*')):
        mode = path.lstat().st_mode
        content = os.readlink(path) if stat.S_ISLNK(mode) else hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(mode) else None
        result[str(path.relative_to(root))] = (mode, content)
    return result
base = Path(os.environ['HOME']).parent
source = Path(sys.path[0])
before_source, before_runtime = snapshot(source), snapshot(base)
import distill.paths as paths
from distill import tools, recomps, emulators
import pkgutil, importlib, distill, distill.ui
for package in (distill, distill.ui):
    for info in pkgutil.iter_modules(package.__path__, package.__name__ + '.'):
        if info.name.rsplit('.', 1)[-1].isidentifier():
            try: importlib.import_module(info.name)
            except ModuleNotFoundError as error:
                if (error.name or '').split('.')[0] != 'PySide6': raise
tools.ToolManager(lambda _: None)
recomps.root(); recomps.cache_root(); recomps.art_dir(); emulators._relation_dir()
paths.config_dir(); paths.data_dir(); paths.state_dir(); paths.cache_dir()
assert before_source == snapshot(source), 'source copy changed during import/resolution/construction'
assert before_runtime == snapshot(base), 'runtime directories changed during import/resolution/construction'
"""
            # Pre-policy RED must be behavioral, not an import error.
            code = code.replace("import distill.paths as paths", "import importlib.util\nassert importlib.util.find_spec('distill.paths'), 'path policy missing'\nimport distill.paths as paths")
            self.run_child(code, child_env(base), base)

    @unittest.skipUnless(HOST_LINUX, "XDG environment separation is Linux behavior")
    def test_fresh_process_round_trip_and_separate_env(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            env = child_env(base)
            self.run_child("from distill import soundtracks; soundtracks.save_prefs({'fixture': 42})", env, base)
            code = "from distill import soundtracks; import json; print(json.dumps(soundtracks.load_prefs()))"
            self.assertEqual(json.loads(self.run_child(code, env, base)), {"fixture": 42})
            self.assertEqual(json.loads(self.run_child(code, child_env(base / "other"), base)), {})

    def test_frozen_and_fresh_process_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            for platform, portable, frozen in (("linux", "", True), ("linux", "1", True), ("win32", "", False), ("darwin", "", False)):
                with self.subTest(mocked_platform=platform, portable=portable, frozen=frozen):
                    env = child_env(base)
                    env["DISTILL_PORTABLE"] = portable
                    code = f"""import sys, subprocess, importlib.util, shutil, tempfile
subprocess.CREATE_NO_WINDOW = 0
subprocess.BELOW_NORMAL_PRIORITY_CLASS = 0
sys.platform = {platform!r}
sys.frozen = {frozen!r}
sys.executable = {str(base / 'binary/Distill')!r}
sys._MEIPASS = {str(base / 'bundle')!r}
assert importlib.util.find_spec('distill.paths'), 'path policy missing'
from distill import paths, constants
from pathlib import Path
expected = Path({str(base / 'binary')!r}) if sys.frozen else constants.APP_DIR
if sys.platform == 'linux' and {portable!r} != '1':
    expected = Path({env['XDG_DATA_HOME']!r}) / 'distill'
assert paths.data_dir() == expected, (paths.data_dir(), expected)
"""
                    self.run_child(code, env, base)

    def test_legacy_fallbacks_with_windows_userprofile_resolution(self):
        run_child = self.run_child
        def simulate_windows_home(code, env, cwd, **kwargs):
            env = dict(env, USERPROFILE=env["HOME"])
            prefix = "from pathlib import Path; import os\nPath.home = classmethod(lambda cls: cls(os.environ['USERPROFILE']))\n"
            return run_child(prefix + code, env, cwd, **kwargs)
        with mock.patch.object(self, "run_child", side_effect=simulate_windows_home):
            self.test_fresh_process_mocked_legacy_fallbacks()

    def test_fresh_process_mocked_legacy_fallbacks(self):
        for platform in ("win32", "darwin"):
            with self.subTest(mocked_platform=platform), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                code = f"""import os, sys, tempfile
from pathlib import Path
from unittest import mock
from distill import tools, recomps, ytdlp, paths
sys.platform = {platform!r}
base = Path({str(base)!r})
app = base / 'blocked-app'
app.write_text('not a directory')
local = base / 'local'
os.environ['LOCALAPPDATA'] = str(local)
for module in (tools, recomps, ytdlp, paths):
    module.APP_DIR = app
recomps.ROOT = None
manager = tools.ToolManager(lambda _: None)
assert manager.root == local / 'Distill/tools'
assert manager.root.is_dir()
assert ytdlp.tools_folder() == local / 'Distill/tools/ytdlp'
assert recomps.root() == local / 'Distill/recomps'
assert recomps.cache_root() == recomps.root()
blocker = base / 'blocked-local'
blocker.write_text('not a directory')
os.environ['LOCALAPPDATA'] = str(blocker)
recomps.ROOT = None
expected = Path(os.environ['HOME']) / '.local/share/Distill/recomps'
assert recomps.root() == expected
assert expected.is_dir()
home = base / 'blocked-home'
home.mkdir()
(home / '.local').write_text('not a directory')
for name in ('HOME', 'USERPROFILE'):
    os.environ[name] = str(home)
for name in ('TEMP', 'TMP', 'TMPDIR'):
    os.environ[name] = str(base / 'temporary')
recomps.ROOT = None
with mock.patch.object(tempfile, 'gettempdir', return_value=str(base / 'temporary')):
    assert tools.ToolManager(lambda _: None).root == base / 'temporary/distill_'
    assert recomps.root() == base / 'temporary/distill_recomps'
    assert recomps.root().is_dir()
    assert ytdlp.tools_folder() == base / 'temporary/distill_ytdlp'
"""
                self.run_child(code, child_env(base), base)

    @unittest.skipUnless(HOST_LINUX, "strict XDG and portable root selection is Linux behavior")
    def test_unwritable_xdg_and_portable_never_fall_back(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            code = f"""import os
from pathlib import Path
from distill import tools, paths, recomps, ytdlp
base = Path({str(base)!r})
blocker = base / 'blocker'
blocker.write_text('blocked')
for name in ('XDG_DATA_HOME', 'LOCALAPPDATA', 'TEMP', 'TMP', 'TMPDIR'):
    os.environ[name] = str(blocker)
for portable in ('', '1'):
    os.environ['DISTILL_PORTABLE'] = portable
    paths.APP_DIR = blocker
    recomps.ROOT = None
    expected = blocker if portable else blocker / 'distill'
    manager = tools.ToolManager(lambda _: None)
    assert manager.root == expected / 'tools'
    assert recomps.root() == expected / 'recomps'
    assert ytdlp.tools_folder() == expected / 'tools/ytdlp'
    for folder in (manager.root, recomps.root(), ytdlp.tools_folder()):
        try: paths.ensure_dir(folder)
        except OSError: pass
        else: raise AssertionError('unwritable root was accepted')
assert list(base.iterdir()) == [blocker]
"""
            self.run_child(code, child_env(base), base)

    @unittest.skipUnless(HOST_LINUX, "read-only source XDG launch requires Linux routing and POSIX chmod")
    def test_real_read_only_copy_persistence_and_self_test(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            copy = base / "source"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv"))
            def digest():
                return {str(p.relative_to(copy)): hashlib.sha256(p.read_bytes()).hexdigest() for p in copy.rglob("*") if p.is_file()}
            before = digest()
            try:
                for p in copy.rglob("*"):
                    p.chmod(0o555 if p.is_dir() else 0o444)
                copy.chmod(0o555)
                if os.access(copy, os.W_OK):
                    self.skipTest("process can write read-only directories (e.g. root)")
                env = child_env(base / "runtime")
                self.run_child("from distill import soundtracks, netrules, torrents, recomps; soundtracks.save_prefs({'fixture': 1}); netrules.save_rules(); torrents.save_torrent_settings({}); recomps.Store().save()", env, base, root=copy)
                self.assertTrue((Path(env["XDG_CONFIG_HOME"]) / "distill/distill_soundtracks.json").is_file())
                self.assertTrue((Path(env["XDG_DATA_HOME"]) / "distill/recomps/state.json").is_file())
                try:
                    import PySide6  # noqa: F401
                except ImportError:
                    self.skipTest("persistence passed; PySide6 unavailable for self-test")
                result = subprocess.run([sys.executable, "-B", str(copy / "Distill.pyw"), "--self-test", str(base / "report.txt")],
                                        env=env, cwd=base, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(digest(), before)
                self.assertFalse((copy / "recomps").exists())
                self.assertEqual({p.name for p in base.iterdir()}, {"source", "runtime", "report.txt"})
            finally:
                copy.chmod(0o755)
                for p in copy.rglob("*"):
                    if p.is_dir():
                        p.chmod(0o755)
