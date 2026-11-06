"""The test run must never touch the real app folder (see tests/__init__.py)."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from distill import constants, organize, soundtracks

from tests import SANDBOX, app_modules, is_app_data

PKG = Path(constants.__file__).resolve().parent


class IsolationTests(unittest.TestCase):
    def test_no_module_keeps_a_path_in_the_real_app_folder(self):
        escaped = [f"{m.__name__}.{name} = {value}" for m in app_modules() if m is not constants
                   for name, value in vars(m).items() if is_app_data(value, constants.APP_DIR, PKG)]
        self.assertEqual(escaped, [], "these paths still point at the real app folder")

    def test_state_files_land_in_the_sandbox(self):
        for path in (organize.undo_file("tidy"), organize.run_log_file(), soundtracks.CACHE_FILE, soundtracks.SAVES_FILE):
            with self.subTest(path=path.name):
                self.assertIn(SANDBOX, path.parents)

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
