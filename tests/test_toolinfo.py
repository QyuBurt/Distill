"""The Tools page's version line and whether Update now can do anything. No Qt, no network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from distill import toolinfo, tools
from distill.toolinfo import ToolInfo, ToolInfos, managers, tool_info, version_text
from tests.helpers import TempDirTest


def fake(**kw):
    """A manager-shaped stand-in (a real subclass would be listed by managers() for every other test)."""
    base = dict(SOURCE="MAME", WINDOWS_ONLY=False, _local_copy=lambda: None, cached=lambda: ("v1.2", Path("x")),
                _label=lambda tag: tag)
    base.update(kw)
    return SimpleNamespace(**base)


class Reading(unittest.TestCase):
    def test_a_downloaded_copy_shows_its_version_and_can_be_updated(self):
        got = tool_info(fake())
        self.assertEqual(got, ToolInfo("v1.2", "MAME", False, True, ""))
        self.assertEqual(version_text(got), "v1.2  \u00b7  MAME")

    def test_the_tools_own_wording_of_its_version_is_used(self):
        self.assertEqual(tool_info(fake(_label=lambda tag: f"build {tag}")).version, "build v1.2")

    def test_nothing_downloaded_yet(self):
        got = tool_info(fake(cached=lambda: None))
        self.assertEqual((got.version, got.can_update), ("", True))  # an update is exactly what would fetch it
        self.assertEqual(version_text(got), "Not downloaded yet")

    def test_a_copy_the_person_placed_is_left_alone(self):
        got = tool_info(fake(_local_copy=lambda: Path("C:/tools/chdman.exe")))
        self.assertEqual((got.own_copy, got.can_update), (True, False))
        self.assertIn("leaves it alone", got.reason)
        self.assertEqual(version_text(got), "Your own copy")

    def test_a_windows_only_tool_cannot_be_updated_elsewhere_but_a_cross_platform_one_can(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(tool_info(fake(WINDOWS_ONLY=True)).can_update)
            self.assertIn("package manager", tool_info(fake(WINDOWS_ONLY=True)).reason)
            self.assertTrue(tool_info(fake(WINDOWS_ONLY=False)).can_update)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(tool_info(fake(WINDOWS_ONLY=True)).can_update)

    def test_a_folder_that_cannot_be_read_does_not_break_the_page(self):
        def boom():
            raise OSError("drive gone")
        got = tool_info(fake(cached=boom))
        self.assertEqual((got.version, got.can_update), ("", False))
        self.assertTrue(got.reason)


class TheManagers(unittest.TestCase):
    # the 14 the window starts at launch (ui/main_window.py), by the key each reports under
    LAUNCHED = {"chdman", "dolphin", "maxcso", "shadplus", "anyps5", "zarchive", "ps3qdd", "xdvdfs", "relinker", "nscb",
                "hactoolnet", "nup", "ytdlp", "ffmpeg"}

    def test_every_manager_the_window_starts_is_found(self):
        self.assertEqual(set(managers()), self.LAUNCHED)

    def test_each_is_a_tool_manager(self):
        for key, cls in managers().items():
            with self.subTest(key=key):
                self.assertTrue(issubclass(cls, tools.ToolManager))
                self.assertEqual(cls.KEY, key)


class WithRealManagers(TempDirTest):
    def test_a_real_manager_reads_its_downloaded_version_from_disk(self):
        cls = managers()["chdman"]
        with mock.patch.object(cls, "_pick_root", classmethod(lambda c: self.tmp)):
            (self.tmp / "current.txt").write_text("mame0270")
            (self.tmp / "mame0270").mkdir()
            (self.tmp / "mame0270" / cls.EXE_NAME).write_bytes(b"x")
            with mock.patch.object(sys, "platform", "win32"):
                got = ToolInfos().info("chdman")
        self.assertTrue(got.version, "the version that was downloaded should be shown")
        self.assertTrue(got.can_update)

    def test_a_key_with_no_manager_has_no_info(self):
        self.assertIsNone(ToolInfos().info("nonsense"))


if __name__ == "__main__":
    unittest.main()
