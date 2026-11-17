"""The Qt-free half of `Distill.exe --self-test`, which CI runs on the freshly built program."""
from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from distill import selftest
from distill.selftest import Report


class ReportTests(unittest.TestCase):
    def test_a_warning_never_fails_the_run_but_a_failure_does(self):
        r = Report()
        r.note(True, "fine")
        r.note(False, "optional thing missing", warn=True)
        self.assertEqual(r.exit_code(), 0)
        self.assertIn("RESULT: passed", r.text())
        r.note(False, "required thing missing")
        self.assertEqual(r.exit_code(), 1)
        self.assertIn("RESULT: FAILED", r.text())

    def test_labels(self):
        r = Report()
        r.note(True, "a")
        r.note(False, "b", warn=True)
        r.note(False, "c")
        self.assertEqual([line.split()[0] for line in r.lines], ["ok", "WARN", "FAIL"])


class AssetTests(unittest.TestCase):
    def test_the_source_tree_passes_and_counts_the_real_entries(self):
        """The report used to print len() of the whole object: 3 for emulators.json and 2 for recomps.json, the keys."""
        import json

        r = Report()
        selftest.check_assets(r)
        self.assertEqual(r.exit_code(), 0, r.text())
        for name, key in selftest.CATALOGS.items():
            entries = json.loads((selftest.constants.ASSETS_DIR / name).read_text(encoding="utf-8"))[key]
            self.assertIn(f"ok    {name}: {len(entries)} entries", r.lines, name)
            self.assertGreater(len(entries), 3, f"{name} holds fewer entries than its own top-level keys")

    def test_an_empty_assets_folder_fails(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(selftest.constants, "ASSETS_DIR", Path(td)):
            r = Report()
            selftest.check_assets(r)
        self.assertEqual(r.exit_code(), 1)

    def _report(self, emulators: str, recomps: str = '{"recomps": [{"id": "a"}, {"id": "b"}]}') -> Report:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "emulators.json").write_text(emulators, encoding="utf-8")
            (root / "recomps.json").write_text(recomps, encoding="utf-8")
            with mock.patch.object(selftest.constants, "ASSETS_DIR", root):
                r = Report()
                selftest.check_assets(r)
        return r

    def test_a_catalog_that_parses_but_holds_nothing_fails(self):
        r = self._report('{"version": "1.2", "last_updated": "2026-11-05", "emulators": []}')
        self.assertEqual(r.exit_code(), 1)
        self.assertIn("FAIL  emulators.json: 0 entries", r.lines)
        self.assertIn("ok    recomps.json: 2 entries", r.lines)  # a good catalog still passes

    def test_a_catalog_without_its_list_fails(self):
        """A shell that is only its metadata used to count as a pass: three keys, nothing in them."""
        r = self._report('{"version": "1.2", "last_updated": "2026-11-05"}')
        self.assertEqual(r.exit_code(), 1)
        self.assertIn("FAIL  emulators.json: no 'emulators' list", r.lines)

    def test_a_catalog_of_the_wrong_shape_fails(self):
        r = self._report('["an", "array", "is", "not", "a", "catalog"]')
        self.assertEqual(r.exit_code(), 1)
        self.assertIn("FAIL  emulators.json: no 'emulators' list", r.lines)


class PackageTests(unittest.TestCase):
    def missing(self, name):
        real = importlib.import_module

        def fake(module, *a, **k):
            if module == name:
                raise ImportError(f"No module named {name}")
            return real(module, *a, **k)

        return mock.patch.object(selftest.importlib, "import_module", fake)

    def test_a_packaged_build_without_a_bundled_package_fails(self):
        with self.missing("libtorrent"):
            r = Report()
            selftest.check_packages(r, frozen=True)
        self.assertEqual(r.exit_code(), 1)
        self.assertTrue(any(line.startswith("FAIL") and "libtorrent" in line for line in r.lines))

    def test_a_source_run_without_it_only_warns(self):
        with self.missing("libtorrent"):
            r = Report()
            selftest.check_packages(r, frozen=False)
        self.assertEqual(r.exit_code(), 0)
        self.assertTrue(any(line.startswith("WARN") and "libtorrent" in line for line in r.lines))

    def test_a_missing_sevenzip_backend_is_informational_even_when_packaged(self):
        with self.missing("pyppmd"), mock.patch.object(selftest, "BUNDLED_PACKAGES", ()):
            r = Report()
            selftest.check_packages(r, frozen=True)
        self.assertEqual(r.exit_code(), 0)
        self.assertTrue(any(line.startswith("WARN") and "pyppmd" in line for line in r.lines))


if __name__ == "__main__":
    unittest.main()
