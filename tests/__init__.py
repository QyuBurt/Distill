"""Keeps the whole test run out of the real app folder.

Distill keeps its state (settings, undo history, run logs, caches ...) in files beside the program. A test that
forgets to redirect one of them would write into - or read from - the developer's own copy, so instead of relying on
every test to remember, importing this package (unittest and pytest both do, before any test module) points every
path the app keeps under its folder at a throw-away folder for the run.

tests/test_isolation.py fails when a path escapes, so a new `SOME_FILE = APP_DIR / ...` can't slip through.
"""
from __future__ import annotations

import atexit
import importlib
import pkgutil
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def app_modules():
    """Every importable distill module. The ones that need Qt are skipped when Qt isn't installed."""
    import distill
    import distill.ui

    for package in (distill, distill.ui):
        for info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
            if not info.name.rsplit(".", 1)[-1].isidentifier():  # local_keys.example.py is a template, not a module
                continue
            try:
                yield importlib.import_module(info.name)
            except ModuleNotFoundError as exc:
                if (exc.name or "").split(".")[0] != "PySide6":
                    raise


def is_app_data(value: object, app_dir: Path, package_dir: Path) -> bool:
    """A path in the app folder that the program writes to (not its own source or bundled assets)."""
    if not isinstance(value, Path):
        return False
    if package_dir == value or package_dir in value.parents:
        return False
    return value == app_dir or app_dir in value.parents


def isolate_app_folder() -> Path:
    from distill import constants

    app_dir, package_dir = constants.APP_DIR, Path(constants.__file__).resolve().parent
    sandbox = Path(tempfile.mkdtemp(prefix="distill_tests_app_"))
    atexit.register(shutil.rmtree, sandbox, True)
    for module in app_modules():
        if module is constants:  # test_structure checks what APP_DIR is for a source checkout and a packaged build
            continue
        for name, value in list(vars(module).items()):
            if is_app_data(value, app_dir, package_dir):
                mock.patch.object(module, name, sandbox / value.relative_to(app_dir)).start()
    return sandbox


SANDBOX = isolate_app_folder()
