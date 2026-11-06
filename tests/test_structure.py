"""Guards on the shape of the package, so the split can't quietly rot."""
from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401  (puts the project on sys.path)

PKG = Path(__file__).resolve().parent.parent / "distill"
CORE = ["constants", "bootstrap", "system", "formats", "models", "batch", "organize", "downloads", "links",
        "torrents", "torrentsearch", "torbox", "torrent_runners", "tools", "soundtracks", "switchconv"]


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(PKG.parent).with_suffix("").parts)


def internal_imports(path: Path, runtime_only: bool = True) -> set[str]:
    """distill modules this file imports at module level (TYPE_CHECKING and in-function imports excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    here = module_name(path).split(".")[:-1]
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level:
            base = here[: len(here) - (node.level - 1)] if node.level > 1 else here
            target = ".".join([*base, *(node.module.split(".") if node.module else [])])
            found.add(target)
    return found


class StructureTests(unittest.TestCase):
    def test_every_core_module_imports_without_qt(self):
        for name in CORE:
            with self.subTest(module=name):
                importlib.import_module(f"distill.{name}")

    def test_core_never_touches_the_ui_or_qt(self):
        for name in CORE:
            src = (PKG / f"{name}.py").read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [("." * node.level) + (node.module or "")]
                else:
                    continue
                for m in mods:
                    with self.subTest(module=name, imports=m):
                        self.assertFalse(m.startswith("PySide6"), f"{name} imports Qt")
                        self.assertFalse(m.startswith(".ui") or m.startswith("..ui"), f"{name} imports the UI")

    def test_no_import_cycles(self):
        graph = {module_name(p): {m for m in internal_imports(p) if m.startswith("distill")}
                 for p in PKG.rglob("*.py") if p.name != "__init__.py"}
        state: dict[str, int] = {}

        def visit(node: str, trail: list[str]) -> None:
            if state.get(node) == 1:
                self.fail("import cycle: " + " -> ".join([*trail[trail.index(node):], node]))
            if state.get(node) == 2:
                return
            state[node] = 1
            for dep in sorted(graph.get(node, ())):
                if dep in graph:
                    visit(dep, [*trail, node])
            state[node] = 2

        for mod in sorted(graph):
            visit(mod, [])

    def test_a_packaged_build_keeps_its_files_beside_the_exe_and_never_runs_pip(self):
        import sys
        from unittest import mock

        import distill.bootstrap as bootstrap
        import distill.constants as constants

        fake_exe = Path(__file__).resolve().parent / "dist" / "Distill.exe"
        try:
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(fake_exe)):
                importlib.reload(constants)
                self.assertEqual(constants.APP_DIR, fake_exe.parent)
                with mock.patch.object(bootstrap.subprocess, "check_call") as pip:
                    bootstrap.ensure_dependencies()
                    pip.assert_not_called()
        finally:
            importlib.reload(constants)
        self.assertEqual(constants.APP_DIR, PKG.parent)  # a source checkout: the folder holding the package

    def test_version_and_name(self):
        from distill.constants import APP_NAME, APP_VERSION

        self.assertEqual(APP_NAME, "Distill")
        self.assertRegex(APP_VERSION, r"^v\d+\.\d+$")

    def test_pyproject_version_agrees_with_the_app_version(self):
        """One number, two files: the window title / release tags use APP_VERSION, packaging metadata uses pyproject."""
        import re

        from distill.constants import APP_VERSION

        text = (PKG.parent / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(declared, "pyproject.toml has no version")
        self.assertTrue(declared.group(1) == APP_VERSION[1:] or declared.group(1).startswith(APP_VERSION[1:] + "."),
                        f"pyproject says {declared.group(1)}, distill/constants.py says {APP_VERSION}")

    def test_bundled_assets_are_all_there(self):
        """A packaged build ships whatever is in distill/assets; a missing file only shows up as a plain-looking window."""
        import json

        from distill.constants import ASSETS_DIR

        self.assertEqual(ASSETS_DIR, PKG / "assets")  # a source checkout
        for catalog in ("emulators.json", "recomps.json"):
            with self.subTest(catalog=catalog):
                self.assertTrue(json.loads((ASSETS_DIR / catalog).read_text(encoding="utf-8")))
        fonts = {p.stem for p in (ASSETS_DIR / "fonts").glob("*.ttf")}
        for needed in ("Inter_18pt-Regular", "Inter_18pt-Medium", "Inter_18pt-SemiBold", "Inter_18pt-Bold", "Outfit-Bold",
                       "JetBrainsMono-Regular", "JetBrainsMono-Bold"):
            with self.subTest(font=needed):
                self.assertIn(needed, fonts)
        for icon in ("distill-dark-256.png", "distill-bright-256.png"):
            with self.subTest(icon=icon):
                self.assertTrue((ASSETS_DIR / "icons" / icon).is_file())
        icons = list((ASSETS_DIR / "emulator_images" / "icons").glob("*.png"))  # the emulator pictures that ship with the app
        self.assertGreater(len(icons), 10, "emulator icons are missing from distill/assets/emulator_images/icons")
        for licence in ("OFL.txt", "OFL-Outfit.txt", "OFL-JetBrainsMono.txt"):  # the fonts' licence asks for it to travel with them
            with self.subTest(licence=licence):
                self.assertTrue((ASSETS_DIR / "fonts" / licence).is_file())


if __name__ == "__main__":
    unittest.main()
