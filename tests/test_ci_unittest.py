"""Adversarial completion checks: an exit code alone is not a test result."""
import unittest
import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest import mock

from tests.ci_unittest import run, successful


def summary(count=5, result="OK"):
    return f"test_example ... ok\n\n{'-' * 70}\nRan {count} tests in 0.001s\n\n{result}\n"


class CompletionTests(unittest.TestCase):
    def test_crlf_completion_and_spoof(self):
        output = summary().replace("\n", "\r\n")
        self.assertTrue(successful(0, output))
        self.assertFalse(successful(0, output + "test_still_running ...\r\n"))

    def test_child_bytes_are_echoed_verbatim(self):
        checker = Path(__file__).with_name("ci_unittest.py")
        output = summary().replace("\n", "\r\n").encode()
        result = subprocess.run([sys.executable, str(checker), "--timeout", "2", "--", sys.executable,
                                 "-c", f"import sys; sys.stdout.buffer.write({output!r})"],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, output)

    def test_echo_bypasses_text_mode_newline_translation(self):
        output = summary().replace("\n", "\r\n").encode()
        buffer = io.BytesIO()
        # Exercise Windows-style text translation even on a POSIX test host.
        stdout = io.TextIOWrapper(buffer, encoding="utf-8", newline="\r\n")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            result = run([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({output!r})"], 2)
            stdout.flush()
            self.assertEqual(buffer.getvalue(), output)
            self.assertEqual(result, 0)

    def test_accepts_completed_runs(self):
        for result in ("OK", "OK (skipped=2)", "OK (skipped=2, expected failures=1)"):
            with self.subTest(result=result):
                self.assertTrue(successful(0, summary(result=result)))

    def test_rejects_incomplete_or_unsuccessful_runs(self):
        for code, output in ((0, "test_example ... ERROR\n"),
                             (0, summary(result="FAILED (failures=1)")),
                             (1, summary()), (0, summary(0)),
                             (0, summary(result="OK (skipped=5)")),
                             (0, summary() + "test_still_running ... "),
                             (0, "Ran 5 tests in 0.001s\nOK\nmore output\n"),
                             (0, "Ran 5 tests in 0.001s\nOK\n")):
            with self.subTest(code=code, output=output):
                self.assertFalse(successful(code, output))

    def test_real_children(self):
        checker = Path(__file__).with_name("ci_unittest.py")
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "child.py"
            for code, output, expected in ((0, summary(), 0), (0, "no summary", 1),
                                           (3, summary(), 1), (0, summary(0), 1),
                                           (0, summary(result="OK (skipped=5)"), 1),
                                           (0, summary() + "still running", 1),
                                           (0, summary(result="FAILED (errors=1)"), 1)):
                with self.subTest(code=code, output=output):
                    script.write_text(f"print({output!r}); raise SystemExit({code})", encoding="utf-8")
                    result = subprocess.run([sys.executable, str(checker), "--timeout", "2", "--",
                                             sys.executable, str(script)], capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, expected, result.stderr.decode())
                    self.assertIn(output.encode(), result.stdout.replace(b"\r\n", b"\n"))

    def assert_pid_gone(self, pid):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        self.fail(f"descendant {pid} still exists after checker termination")

    @staticmethod
    def kill_survivor(pidfile):
        if pidfile.exists():
            pid = int(pidfile.read_text())
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @unittest.skipIf(os.name == "nt", "tree termination relies on POSIX process groups/sessions; checker is wired only into Linux CI")
    def test_timeout_kills_child_tree(self):
        checker = Path(__file__).with_name("ci_unittest.py")
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "grandchild.pid"
            source = ("import subprocess, sys, time\nfrom pathlib import Path\n"
                      "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'])\n"
                      f"Path({str(pidfile)!r}).write_text(str(child.pid))\ntime.sleep(8)\n")
            try:
                # Without tree termination the inherited pipe remains open for
                # eight seconds, longer than this five-second outer deadline.
                result = subprocess.run([sys.executable, str(checker), "--timeout", "0.5", "--",
                                         sys.executable, "-c", source], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertIn(b"killed child tree", result.stderr)
                self.assert_pid_gone(int(pidfile.read_text()))
            finally:
                self.kill_survivor(pidfile)

    @unittest.skipIf(os.name == "nt", "orphan-pipe semantics rely on POSIX process groups/sessions; checker is wired only into Linux CI")
    def test_post_kill_output_drain_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "escaped.pid"
            source = ("import subprocess, sys\nfrom pathlib import Path\n"
                      f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'], start_new_session={os.name != 'nt'})\n"
                      f"Path({str(pidfile)!r}).write_text(str(child.pid))\nprint('pipe held', flush=True)\n")
            output = io.BytesIO()
            errors = io.StringIO()
            started = time.monotonic()
            try:
                with mock.patch("tests.ci_unittest.DRAIN_TIMEOUT", 0.25), \
                        mock.patch.object(sys, "stdout") as stdout, contextlib.redirect_stderr(errors):
                    stdout.buffer = output
                    self.assertEqual(run([sys.executable, "-c", source], 0.25), 1)
                self.assertLess(time.monotonic() - started, 3)
                self.assertIn("output drain timed out", errors.getvalue())
                self.assertIn(b"pipe held", output.getvalue())
            finally:
                self.kill_survivor(pidfile)

    def test_real_unittest_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "child.py"
            script.write_text("import unittest\nclass T(unittest.TestCase):\n"
                              " def test_one(self): self.assertTrue(True)\nunittest.main()\n", encoding="utf-8")
            with mock.patch.object(sys, "stdout") as stdout, contextlib.redirect_stderr(io.StringIO()):
                stdout.buffer = io.BytesIO()
                self.assertEqual(run([sys.executable, str(script)], 2), 0)
