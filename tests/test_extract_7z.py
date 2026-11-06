"""Unpacking a .7z for an emulator or recomp install: a real 7-Zip when there is one (RetroArch's archives use BCJ2, which py7zr can't read), else py7zr."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from distill import recomps as rc
from distill import system


class Unsupported(Exception):
    pass


Unsupported.__name__ = "UnsupportedCompressionMethodError"


class Native(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "RetroArch.7z"
        self.archive.write_bytes(b"7z")
        self.dest = self.root / "stage"

    def fake_run(self, code=0, stderr="", effect=None):
        def run(cmd, **kw):
            self.cmd = cmd
            if effect:
                effect()
            return subprocess.CompletedProcess(cmd, code, "", stderr)
        return run

    def test_a_native_7zip_is_used_when_there_is_one(self):
        def effect():
            (self.dest / "retroarch.exe").write_bytes(b"x")
        with mock.patch.object(system, "find_7zip", return_value="/opt/7z"), mock.patch.object(rc.subprocess, "run", self.fake_run(effect=effect)):
            rc.extract(self.archive, self.dest)
        self.assertEqual(self.cmd[:2], ["/opt/7z", "x"])
        self.assertIn(f"-o{self.dest}", self.cmd)
        self.assertEqual(self.cmd[-1], str(self.archive))
        self.assertTrue((self.dest / "retroarch.exe").exists())

    def test_a_warning_exit_code_is_not_a_failure(self):
        with mock.patch.object(system, "find_7zip", return_value="/opt/7z"), mock.patch.object(rc.subprocess, "run", self.fake_run(code=1)):
            rc.extract(self.archive, self.dest)

    def test_a_failed_run_says_what_7zip_said(self):
        with mock.patch.object(system, "find_7zip", return_value="/opt/7z"), \
                mock.patch.object(rc.subprocess, "run", self.fake_run(code=2, stderr="ERROR: Data Error\nERROR: bad archive")):
            with self.assertRaises(rc.RecompError) as ctx:
                rc.extract(self.archive, self.dest)
        self.assertIn("exit code 2", str(ctx.exception))
        self.assertIn("bad archive", str(ctx.exception))

    @unittest.skipIf(sys.platform == "win32", "symbolic links need privileges on Windows")
    def test_a_link_pointing_outside_is_refused(self):
        def effect():
            os.symlink("/etc", self.dest / "out")
        with mock.patch.object(system, "find_7zip", return_value="/opt/7z"), mock.patch.object(rc.subprocess, "run", self.fake_run(effect=effect)):
            with self.assertRaises(rc.RecompError):
                rc.extract(self.archive, self.dest)

    @unittest.skipIf(sys.platform == "win32", "symbolic links need privileges on Windows")
    def test_a_link_inside_is_fine(self):
        def effect():
            (self.dest / "lib").mkdir(parents=True, exist_ok=True)
            (self.dest / "lib" / "real.so").write_bytes(b"x")
            os.symlink("lib/real.so", self.dest / "link.so")
        with mock.patch.object(system, "find_7zip", return_value="/opt/7z"), mock.patch.object(rc.subprocess, "run", self.fake_run(effect=effect)):
            rc.extract(self.archive, self.dest)


class Fallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "RetroArch.7z"
        self.archive.write_bytes(b"7z")

    def py7zr_that_raises(self, exc: Exception):
        class Module:
            class SevenZipFile:
                def __init__(self, *a, **k):
                    raise exc
        return Module

    def test_bcj2_without_a_native_7zip_says_what_to_install(self):
        err = Unsupported(b"\x03\x03\x01\x1b", "BCJ2 filter is not supported by py7zr.")
        with mock.patch.object(system, "find_7zip", return_value=None), mock.patch.object(system, "_py7zr", return_value=self.py7zr_that_raises(err)):
            with self.assertRaises(rc.RecompError) as ctx:
                rc.extract(self.archive, self.root / "stage")
        self.assertIn("Install 7-Zip", str(ctx.exception))
        self.assertIn("RetroArch.7z", str(ctx.exception))
        self.assertNotIn("py7zr", str(ctx.exception).lower().replace("built-in unpacker", ""))  # no library jargon in front of the person

    def test_no_native_7zip_and_no_py7zr(self):
        with mock.patch.object(system, "find_7zip", return_value=None), mock.patch.object(system, "_py7zr", side_effect=RuntimeError("gone")):
            with self.assertRaises(rc.RecompError) as ctx:
                rc.extract(self.archive, self.root / "stage")
        self.assertIn("Install 7-Zip", str(ctx.exception))

    def test_any_other_failure_names_the_archive(self):
        with mock.patch.object(system, "find_7zip", return_value=None), \
                mock.patch.object(system, "_py7zr", return_value=self.py7zr_that_raises(OSError("disk full"))):
            with self.assertRaises(rc.RecompError) as ctx:
                rc.extract(self.archive, self.root / "stage")
        self.assertIn("disk full", str(ctx.exception))
        self.assertNotIn("Install 7-Zip", str(ctx.exception))

    def test_py7zr_still_unpacks_an_ordinary_archive(self):
        try:
            import py7zr
        except ImportError:
            self.skipTest("py7zr is not installed")
        src = self.root / "src"
        src.mkdir()
        (src / "hello.txt").write_text("hi")
        real = self.root / "real.7z"
        with py7zr.SevenZipFile(real, "w") as z:
            z.writeall(src, "app")
        with mock.patch.object(system, "find_7zip", return_value=None):
            rc.extract(real, self.root / "stage")
        self.assertEqual((self.root / "stage" / "app" / "hello.txt").read_text(), "hi")

    def test_find_7zip_notices_an_install_made_while_running(self):
        with mock.patch.object(system, "_FOUND_7ZIP", ""), mock.patch.object(system.shutil, "which", return_value=None), \
                mock.patch.object(system.sys, "platform", "linux"):
            self.assertIsNone(system.find_7zip())
            with tempfile.NamedTemporaryFile() as exe, mock.patch.object(system.shutil, "which", side_effect=lambda n: exe.name if n == "7zz" else None):
                self.assertEqual(system.find_7zip(), exe.name)  # not stuck on the earlier "none"


if __name__ == "__main__":
    unittest.main()
