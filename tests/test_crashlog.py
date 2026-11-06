from __future__ import annotations

import sys
import threading
import unittest
from unittest import mock

from distill import crashlog

from tests.helpers import TempDirTest


def boom(msg="kaput"):
    try:
        raise ValueError(msg)
    except ValueError:
        return sys.exc_info()


class CrashLogTests(TempDirTest):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(crashlog, "CRASH_FILE", self.tmp / "crash.txt"),
                        mock.patch.object(sys, "excepthook", lambda *args: None),
                        mock.patch.object(threading, "excepthook", threading.excepthook)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_record_writes_version_and_traceback(self):
        path = crashlog.record(*boom("first"))
        text = path.read_text(encoding="utf-8")
        self.assertIn("ValueError: first", text)
        self.assertIn("Traceback", text)
        self.assertIn(crashlog.APP_VERSION, text)

    def test_entries_accumulate(self):
        crashlog.record(*boom("one"))
        crashlog.record(*boom("two"), where="thread X")
        text = crashlog.CRASH_FILE.read_text(encoding="utf-8")
        self.assertIn("one", text)
        self.assertIn("two", text)
        self.assertIn("thread X", text)

    def test_file_is_trimmed_to_whole_entries(self):
        with mock.patch.object(crashlog, "MAX_BYTES", 4000):
            for i in range(30):
                crashlog.record(*boom(f"error number {i}"))
        text = crashlog.CRASH_FILE.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.encode()), 4000)
        self.assertTrue(text.startswith("=== "))
        self.assertIn("error number 29", text)
        self.assertNotIn("error number 0\n", text)

    def test_unwritable_folder_returns_none_instead_of_raising(self):
        with mock.patch.object(crashlog, "CRASH_FILE", self.tmp / "no" / "such" / "dir" / "c.txt"):
            self.assertIsNone(crashlog.record(*boom()))

    def test_install_records_calls_back_and_survives_a_failing_callback(self):
        seen = []
        crashlog.install(lambda saved, err: seen.append((saved, err)), faults=False)
        with mock.patch.object(crashlog, "_fault_stream", None):
            sys.excepthook(*boom("main"))
        self.assertEqual(len(seen), 1)
        self.assertIn("main", crashlog.CRASH_FILE.read_text(encoding="utf-8"))

        def bad(saved, err):
            raise RuntimeError("dialog failed")
        crashlog.install(bad, faults=False)
        sys.excepthook(*boom("again"))  # must not raise

    def test_ctrl_c_is_not_recorded(self):
        crashlog.install(faults=False)
        with mock.patch.object(sys, "__excepthook__"):
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt:
                sys.excepthook(*sys.exc_info())
        self.assertFalse(crashlog.CRASH_FILE.exists())

    def test_worker_thread_errors_are_recorded_with_the_thread_name(self):
        crashlog.install(faults=False)
        t = threading.Thread(target=lambda: 1 / 0, name="converter-7")
        t.start()
        t.join()
        text = crashlog.CRASH_FILE.read_text(encoding="utf-8")
        self.assertIn("ZeroDivisionError", text)
        self.assertIn("thread converter-7", text)


if __name__ == "__main__":
    unittest.main()
