"""The test run must never touch the real app folder (see tests/__init__.py)."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

from distill import constants, organize, soundtracks

from tests import SANDBOX, app_modules, is_app_data

PKG = Path(constants.__file__).resolve().parent


class IsolationTests(unittest.TestCase):
    def test_network_guard_blocks_non_loopback_audit_events(self):
        # Emit the real socket audit events without creating a socket or doing
        # DNS; this also works in sandboxes that deny even loopback sockets.
        for event, args in (("socket.connect", (None, ("198.51.100.23", 443))),
                            ("socket.connect", (None, ("2001:db8::23", 443, 0, 0))),
                            ("socket.getaddrinfo", ("guard-test.invalid", 443, 0, 0, 0))):
            with self.subTest(event=event, args=args):
                with self.assertRaisesRegex(AssertionError, "non-loopback"):
                    sys.audit(event, *args)

    def test_network_guard_allows_loopback_and_local_sockets(self):
        for host in ("127.0.0.1", "127.0.0.2", "::1", "localhost", b"127.0.0.1", None):
            with self.subTest(host=host):
                sys.audit("socket.getaddrinfo", host, 80, 0, 0, 0)
        sys.audit("socket.connect", None, ("127.0.0.1", 80))
        sys.audit("socket.connect", None, ("::1", 80, 0, 0))
        sys.audit("socket.connect", None, "/tmp/local-test-socket")

    @unittest.skipIf(sys.platform == "win32", "Windows environment is preserved")
    def test_home_and_xdg_paths_are_inside_the_run_sandbox(self):
        self.assertIn(SANDBOX, Path.home().parents)
        for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            with self.subTest(name=name):
                self.assertIn(SANDBOX, Path(os.environ[name]).parents)

    def test_keyring_uses_only_the_in_memory_test_backend(self):
        try:
            import keyring
        except ImportError:
            self.skipTest("optional keyring is not installed")
        backend = keyring.get_keyring()
        self.assertEqual(type(backend).__module__, "tests")
        self.assertEqual(type(backend).__name__, "MemoryKeyring")
        backend.set_password("isolation-test", "user", "secret")
        self.assertEqual(backend.get_password("isolation-test", "user"), "secret")
        backend.delete_password("isolation-test", "user")
        self.assertIsNone(backend.get_password("isolation-test", "user"))

    def test_no_module_keeps_a_path_in_the_real_app_folder(self):
        escaped = [f"{m.__name__}.{name} = {value}" for m in app_modules() if m is not constants
                   for name, value in vars(m).items() if is_app_data(value, constants.APP_DIR, PKG)]
        self.assertEqual(escaped, [], "these paths still point at the real app folder")

    def test_state_files_land_in_the_sandbox(self):
        for path in (organize.undo_file("tidy"), organize.run_log_file(), soundtracks.CACHE_FILE, soundtracks.SAVES_FILE):
            with self.subTest(path=path.name):
                self.assertIn(SANDBOX, path.parents)

    def test_every_moved_constant_is_inside_the_sandbox(self):
        moved = {"persist": ("SETTINGS_FILE",), "downloads": ("LOGIN_FILE", "SESSION_FILE"),
                 "apikeys": ("FILE",), "netrules": ("RULES_FILE",), "ytdlp": ("SETTINGS_FILE",),
                 "torrents": ("TORRENT_FILE", "TORRENT_DIR"), "torrent_runners": ("TORRENT_DIR",),
                 "soundtracks": ("PREFS_FILE", "SAVES_FILE", "CACHE_FILE"),
                 "crashlog": ("CRASH_FILE",), "motion": ("MOTION_LOG",), "title_dialog": ("ICON_CACHE",),
                 "formats": ("PS3_NAME_CACHE",), "torrentsearch": ("TRACKERS_FILE",), "download_page": ("SESSION_FILE",)}
        for module in app_modules():
            for name in moved.get(module.__name__.rsplit(".", 1)[-1], ()):
                with self.subTest(module=module.__name__, name=name):
                    self.assertIn(SANDBOX, getattr(module, name).parents)

    def test_bundled_assets_are_still_read_from_the_package(self):
        from distill import recomps

        self.assertTrue((PKG / "assets" / "recomps.json").is_file())
        self.assertTrue(recomps.load_catalog())  # would be empty if the guard had moved the assets too

    def test_every_app_folder_constant_is_a_plain_module_level_assignment(self):
        """The guard rebases module attributes, so a path built inside a function from APP_DIR must go through the
        module's own APP_DIR name (which is rebased) and never through `constants.APP_DIR` (which isn't)."""
        offenders = []
        for path in sorted(PKG.rglob("*.py")):
            if path.name == "constants.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr == "APP_DIR" and isinstance(node.value, ast.Name) \
                        and node.value.id == "constants":
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [], "use `from .constants import APP_DIR` so the test guard can redirect it")


if __name__ == "__main__":
    unittest.main()
