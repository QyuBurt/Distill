"""Frozen Linux children must use host libraries without changing the app's environment."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

HAVE_QT = not os.environ.get("DISTILL_SKIP_UI_TESTS")
if HAVE_QT:
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
    except ImportError:
        HAVE_QT = False

from distill import hostenv
from tests import SANDBOX


class FrozenLinuxTestCase(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "platform", "linux"),
                      mock.patch.object(sys, "_MEIPASS", "/tmp/distill-fake-bundle", create=True)):
            patch.start()
            self.addCleanup(patch.stop)


class HostEnvTests(FrozenLinuxTestCase):
    def test_restores_original_and_preserves_unrelated_variables(self):
        for original in ("/host/lib:/other/lib", ""):
            with self.subTest(original=original):
                env = {"LD_LIBRARY_PATH": sys._MEIPASS, "LD_LIBRARY_PATH_ORIG": original, "KEEP": "yes"}
                before = env.copy()
                self.assertEqual(hostenv.host_env(env), {"LD_LIBRARY_PATH": original, "KEEP": "yes"})
                self.assertEqual(env, before)

    def test_removes_bundle_path_without_original(self):
        self.assertEqual(hostenv.host_env({"LD_LIBRARY_PATH": sys._MEIPASS, "KEEP": "yes"}), {"KEEP": "yes"})

    def test_strips_only_the_first_path_entry(self):
        env = {"LD_LIBRARY_PATH": sys._MEIPASS + ":/host/lib:/other/lib"}
        for pathsep in (":", ";"):
            with self.subTest(host_pathsep=pathsep), mock.patch.object(os, "pathsep", pathsep):
                self.assertEqual(hostenv.host_env(env), {"LD_LIBRARY_PATH": "/host/lib:/other/lib"})

    def test_cleaned_environment_is_unchanged_on_repeated_calls(self):
        for env in ({"LD_LIBRARY_PATH": sys._MEIPASS},
                    {"LD_LIBRARY_PATH": sys._MEIPASS + ":/host/lib:/other/lib"},
                    {"LD_LIBRARY_PATH": sys._MEIPASS, "LD_LIBRARY_PATH_ORIG": "/host/lib"},
                    {"LD_LIBRARY_PATH": sys._MEIPASS, "LD_LIBRARY_PATH_ORIG": ""}):
            with self.subTest(env=env):
                cleaned = hostenv.host_env(env)
                self.assertEqual(hostenv.host_env(cleaned), cleaned)

    def test_leaves_unrelated_library_paths_unchanged(self):
        for path in ("/host/lib", sys._MEIPASS + "-other", "/host/lib:" + sys._MEIPASS):
            with self.subTest(path=path):
                env = {"LD_LIBRARY_PATH": path}
                self.assertEqual(hostenv.host_env(env), env)
        self.assertEqual(hostenv.host_env({}), {})

    def test_defaults_to_a_copy_of_the_process_environment(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": sys._MEIPASS, "KEEP": "yes"}, clear=True):
            self.assertEqual(hostenv.host_env(), {"KEEP": "yes"})
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], sys._MEIPASS)

    def test_qt_paths_remove_only_bundle_entries_anywhere_in_the_list(self):
        for name in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
            for value, expected in (
                    (sys._MEIPASS + "/PySide6/Qt/plugins", {}),
                    (sys._MEIPASS, {}),
                    ("/host/qt:" + sys._MEIPASS + "/PySide6/Qt:" + sys._MEIPASS + ":/other/qt",
                     {name: "/host/qt:/other/qt"}),
                    (sys._MEIPASS + "-other:" + sys._MEIPASS + "/../host/qt",
                     {name: sys._MEIPASS + "-other:" + sys._MEIPASS + "/../host/qt"}),
                    (":" + sys._MEIPASS + "/PySide6/Qt:", {name: ":"}),
                    ("", {name: ""})):
                with self.subTest(name=name, value=value):
                    env = {name: value, "KEEP": "yes"}
                    self.assertEqual(hostenv.host_env(env), {**expected, "KEEP": "yes"})
                    self.assertEqual(env, {name: value, "KEEP": "yes"})
                    self.assertEqual(hostenv.host_env(hostenv.host_env(env)), hostenv.host_env(env))

    def test_qt_original_paths_are_restored_only_for_bundle_owned_values(self):
        for name in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
            for original in ("/host/qt:/other/qt", ""):
                with self.subTest(name=name, original=original):
                    env = {name: sys._MEIPASS + "/PySide6/Qt", name + "_ORIG": original}
                    self.assertEqual(hostenv.host_env(env), {name: original})
                    env[name] = "/host/qt"
                    self.assertEqual(hostenv.host_env(env), env)

    def test_noop_outside_frozen_linux(self):
        env = {"LD_LIBRARY_PATH": sys._MEIPASS, "LD_LIBRARY_PATH_ORIG": "/host/lib",
               "QT_PLUGIN_PATH": sys._MEIPASS + "/plugins", "QT_PLUGIN_PATH_ORIG": "/host/plugins",
               "QML2_IMPORT_PATH": sys._MEIPASS + "/qml", "QML2_IMPORT_PATH_ORIG": "/host/qml"}
        for frozen, platform in ((False, "linux"), (True, "win32"), (True, "darwin")):
            with self.subTest(frozen=frozen, platform=platform), \
                    mock.patch.object(sys, "frozen", frozen), mock.patch.object(sys, "platform", platform):
                result = hostenv.host_env(env)
                self.assertEqual(result, env)
                self.assertIsNot(result, env)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class InstallTests(FrozenLinuxTestCase):
    def setUp(self):
        super().setUp()
        # Restore both process-wide hooks and the installation flag after every test.
        for patch in (mock.patch.object(subprocess.Popen, "__init__", subprocess.Popen.__init__),
                      mock.patch.object(QDesktopServices, "openUrl", QDesktopServices.openUrl),
                      mock.patch.object(hostenv, "_installed", False),
                      mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": sys._MEIPASS})):
            patch.start()
            self.addCleanup(patch.stop)
        # Preserve host requirements such as Windows' SystemRoot for real Python children.
        os.environ.pop("LD_LIBRARY_PATH_ORIG", None)

    def child_library_path(self, **kwargs):
        result = subprocess.run([sys.executable, "-I", "-c", 'import os;print(os.environ.get("LD_LIBRARY_PATH"))'],
                                capture_output=True, text=True, check=True, timeout=10, **kwargs)
        return result.stdout.strip()

    def test_inherited_environment_is_clean_and_parent_is_unchanged(self):
        hostenv.install()
        self.assertEqual(self.child_library_path(), "None")
        self.assertEqual(os.environ["LD_LIBRARY_PATH"], sys._MEIPASS)

    def test_explicit_environment_restores_original_without_mutating_input(self):
        hostenv.install()
        env = dict(os.environ, LD_LIBRARY_PATH=sys._MEIPASS, LD_LIBRARY_PATH_ORIG="/host/lib")
        before = env.copy()
        self.assertEqual(self.child_library_path(env=env), "/host/lib")
        self.assertEqual(env, before)
        self.assertEqual(self.child_library_path(env=dict(os.environ, LD_LIBRARY_PATH="/host/lib")), "/host/lib")

    def test_positional_environment_is_cleaned(self):
        hostenv.install()
        command = [sys.executable, "-I", "-c", 'import os;print(os.environ.get("LD_LIBRARY_PATH"))']
        env = dict(os.environ, LD_LIBRARY_PATH=sys._MEIPASS + ":/host/lib")
        with subprocess.Popen(command, -1, None, None, subprocess.PIPE, None, None, True, False, None, env,
                              text=True) as process:
            stdout, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stdout.strip(), "/host/lib")

    def test_child_qt_paths_are_clean_without_changing_the_parent(self):
        env = dict(os.environ, QT_PLUGIN_PATH="/host/plugins:" + sys._MEIPASS + "/PySide6/Qt/plugins",
                   QML2_IMPORT_PATH=sys._MEIPASS + "/PySide6/Qt/qml", QML2_IMPORT_PATH_ORIG="/host/qml")
        with mock.patch.dict(os.environ, env, clear=True):
            original = dict(os.environ)
            hostenv.install()
            result = subprocess.run([sys.executable, "-I", "-c",
                                     'import json,os;print(json.dumps({k:v for k,v in os.environ.items() '
                                     'if k.startswith(("QT_PLUGIN_PATH", "QML2_IMPORT_PATH"))}))'],
                                    capture_output=True, text=True, check=True, timeout=10)
            self.assertEqual(json.loads(result.stdout), {"QT_PLUGIN_PATH": "/host/plugins", "QML2_IMPORT_PATH": "/host/qml"})
            self.assertEqual(dict(os.environ), original)

    def test_install_is_idempotent(self):
        hostenv.install()
        popen, open_url = subprocess.Popen.__init__, QDesktopServices.openUrl
        hostenv.install()
        self.assertIs(subprocess.Popen.__init__, popen)
        self.assertIs(QDesktopServices.openUrl, open_url)

    def test_install_is_noop_outside_frozen_linux(self):
        popen, open_url = subprocess.Popen.__init__, QDesktopServices.openUrl
        for frozen, platform in ((False, "linux"), (True, "win32"), (True, "darwin")):
            with self.subTest(frozen=frozen, platform=platform), \
                    mock.patch.object(sys, "frozen", frozen), mock.patch.object(sys, "platform", platform):
                hostenv.install()
                self.assertIs(subprocess.Popen.__init__, popen)
                self.assertIs(QDesktopServices.openUrl, open_url)
                self.assertFalse(hostenv._installed)

    def test_qt_url_launch_restores_environment_on_success_and_error(self):
        unrelated = {"PATH": os.environ.get("PATH", ""), "HOME": str(SANDBOX / "home"),
                     **{f"XDG_{kind}_HOME": str(SANDBOX / kind.lower())
                        for kind in ("CONFIG", "DATA", "STATE", "CACHE")}}
        cases = ({"LD_LIBRARY_PATH": sys._MEIPASS, "LD_LIBRARY_PATH_ORIG": "/host/lib"},
                 {"LD_LIBRARY_PATH": sys._MEIPASS, "LD_LIBRARY_PATH_ORIG": ""},
                 {"LD_LIBRARY_PATH": sys._MEIPASS},
                 {"LD_LIBRARY_PATH": sys._MEIPASS + ":/host/lib:/other/lib"},
                 {"LD_LIBRARY_PATH_ORIG": "/host/lib"},
                 {"QT_PLUGIN_PATH": sys._MEIPASS + "/PySide6/Qt/plugins",
                  "QML2_IMPORT_PATH": sys._MEIPASS + "/PySide6/Qt/qml"},
                 {"QT_PLUGIN_PATH": "/host/plugins:" + sys._MEIPASS + "/plugins:/other/plugins",
                  "QML2_IMPORT_PATH": sys._MEIPASS + "/qml:/host/qml"},
                 {"QT_PLUGIN_PATH": sys._MEIPASS + "/plugins", "QT_PLUGIN_PATH_ORIG": "/host/plugins",
                  "QML2_IMPORT_PATH": sys._MEIPASS + "/qml", "QML2_IMPORT_PATH_ORIG": ""},
                 {"QT_PLUGIN_PATH": "/host/plugins", "QT_PLUGIN_PATH_ORIG": "/other/plugins",
                  "QML2_IMPORT_PATH_ORIG": "/host/qml"}, {})
        with mock.patch.object(QDesktopServices, "openUrl") as underlying:
            hostenv.install()
            for library_vars in cases:
                for fails in (False, True):
                    with self.subTest(library_vars=library_vars, fails=fails), \
                            mock.patch.dict(os.environ, unrelated):
                        for name in ("LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG", "QT_PLUGIN_PATH", "QT_PLUGIN_PATH_ORIG",
                                     "QML2_IMPORT_PATH", "QML2_IMPORT_PATH_ORIG"):
                            os.environ.pop(name, None)
                        os.environ.update(library_vars)
                        original = dict(os.environ)
                        expected = hostenv.host_env(original)

                        def open_url(url, fails=fails, expected=expected):
                            self.assertEqual(url, QUrl("https://example.invalid"))
                            self.assertEqual(dict(os.environ), expected)
                            for name, value in unrelated.items():
                                self.assertEqual(os.environ[name], value)
                            # Model a worker copying the environment during this call.
                            self.assertEqual(hostenv.host_env(), expected)
                            if fails:
                                raise RuntimeError("fake launch error")
                            return True

                        underlying.reset_mock()
                        underlying.side_effect = open_url
                        with mock.patch.object(os.environ, "clear", wraps=os.environ.clear) as clear, \
                                mock.patch.object(os.environ, "update", wraps=os.environ.update) as update:
                            if fails:
                                with self.assertRaisesRegex(RuntimeError, "fake launch error"):
                                    QDesktopServices.openUrl(QUrl("https://example.invalid"))
                            else:
                                self.assertTrue(QDesktopServices.openUrl(QUrl("https://example.invalid")))
                            clear.assert_not_called()
                            update.assert_not_called()
                        underlying.assert_called_once()
                        self.assertEqual(dict(os.environ), original)


if __name__ == "__main__":
    unittest.main()
