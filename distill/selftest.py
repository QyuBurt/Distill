"""The parts of `Distill.exe --self-test` that need no Qt: bundled data files, bundled packages and the report itself.

CI launches the freshly built program with --self-test, because a packaged build can compile fine and still be missing a
data file or a package that only a launch reveals. A windowed .exe has no console, so the verdict is written to a file.
The window checks live in app.py (they need Qt)."""
from __future__ import annotations

import importlib
import json
import sys

from . import constants

BUNDLED_PACKAGES = ("py7zr", "libtorrent", "mutagen")  # PyInstaller can't see all these need: a packaged build without one is broken
SEVENZIP_BACKENDS = ("bcj", "pyppmd", "pyzstd", "inflate64", "brotli", "Cryptodome", "multivolumefile", "texttable", "psutil")
CATALOGS = ("emulators.json", "recomps.json")


class Report:
    """Lines of `ok` / `WARN` / `FAIL`. A warning never fails the run; any FAIL does."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failed = False

    def note(self, ok: bool, text: str, *, warn: bool = False) -> None:
        """`ok` says whether the check passed; `warn` downgrades a failed check from FAIL to WARN."""
        label = "ok" if ok else "WARN" if warn else "FAIL"
        self.failed = self.failed or label == "FAIL"
        self.lines.append(f"{label:<6}{text}")

    def text(self) -> str:
        return "\n".join([*self.lines, "RESULT: " + ("FAILED" if self.failed else "passed")]) + "\n"

    def exit_code(self) -> int:
        return 1 if self.failed else 0


def check_assets(report: Report) -> None:
    assets = constants.ASSETS_DIR
    report.note(assets.is_dir(), f"assets folder {assets}")
    for name in CATALOGS:
        try:
            entries = json.loads((assets / name).read_text(encoding="utf-8"))
            report.note(bool(entries), f"{name}: {len(entries)} entries")
        except (OSError, ValueError) as exc:
            report.note(False, f"{name}: {exc}")


def check_packages(report: Report, frozen: bool) -> None:
    """A source run may lack a bundled package (the app degrades without it); a packaged build may not."""
    for name in BUNDLED_PACKAGES:
        try:
            module = importlib.import_module(name)
            report.note(True, f"import {name} {getattr(module, '__version__', '')}".rstrip())
        except Exception as exc:  # a broken native library raises more than ImportError
            report.note(False, f"import {name}: {exc}", warn=not frozen)
    for name in SEVENZIP_BACKENDS:  # which of py7zr's optional compressors came along: informational
        try:
            importlib.import_module(name)
        except Exception:
            report.note(False, f"7z back-end '{name}' is missing (some .7z archives won't unpack)", warn=True)


def describe_runtime() -> str:
    from .constants import APP_NAME, APP_VERSION

    kind = "packaged build" if getattr(sys, "frozen", False) else "source run"
    return f"{APP_NAME} {APP_VERSION}: {kind}, Python {sys.version.split()[0]}"
