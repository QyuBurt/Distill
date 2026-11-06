"""Application start-up."""
from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from .constants import APP_NAME, APP_VERSION
from . import crashlog, magnet, selftest
from .selftest import Report
from .ui import motion
from .ui.assets import DISPLAY_FONT, MONO_FONT, TEXT_FONT, load_fonts
from .ui.popup import install_soft_popups
from .ui.main_window import DistillWindow
from .ui.theme import T

SELF_TEST_FLAG = "--self-test"


def _tell_about_crash(saved, error: BaseException) -> None:
    """Said once per run: a bug that fires in a loop must not bury the window in dialogs."""
    if getattr(_tell_about_crash, "shown", False):
        return
    _tell_about_crash.shown = True
    where = f"Details were saved to:\n{saved}" if saved else "The details could not be saved."
    QMessageBox.warning(None, APP_NAME, f"Something unexpected went wrong ({type(error).__name__}: {error}).\n\n"
                        f"Distill is still running, but if things look off, restart it.\n\n{where}")


def prepare(app: QApplication) -> tuple[str, str, str]:
    """Everything between creating the QApplication and building the window: style, theme, fonts, palette and the
    app-wide behaviours. Returns the (text, display, mono) font families that were registered."""
    app.setStyle("Fusion")  # one predictable base for the custom look (the native Windows style ignores parts of it)
    from .ui.persist import read_settings
    T.dark = read_settings().get("theme", "dark") != "light"  # the look chosen last time (the Distill logo switches it)
    T.text_font, T.display_font, T.mono_font = load_fonts()  # Inter / Outfit / JetBrains Mono, or system fallbacks if missing
    font = QFont(T.text_font)
    font.setPointSizeF(9.5)
    app.setFont(font)
    app.setPalette(T.palette())
    install_soft_popups(app)  # every drop-down list is rounded all the way out, with no square frame behind it
    motion.install_dialog_fade(app)  # dialogs fade in instead of blinking into view
    motion.install_smooth_scroll(app)  # every list, table and page glides under the wheel and gives a little at its ends
    T.refresh()
    return T.text_font, T.display_font, T.mono_font


# ---- `Distill.exe --self-test report.txt` --------------------------------------------------------------------------
# CI runs this on the freshly built program (see selftest.py for why). The checks that need Qt are here.
def self_test(report_path: Path | None) -> int:
    """Start up as far as the real thing does (without going online), write what was found, and return 0 or 1."""
    frozen = bool(getattr(sys, "frozen", False))
    report = Report()
    report.note(True, selftest.describe_runtime())
    try:
        selftest.check_assets(report)
        selftest.check_packages(report, frozen)
        app = QApplication.instance() or QApplication([APP_NAME])
        families = prepare(app)  # the same call main() makes
        report.note(families == (TEXT_FONT, DISPLAY_FONT, MONO_FONT), f"fonts registered: {' / '.join(families)}")
        real_start = threading.Thread.start
        threading.Thread.start = lambda self: None  # the window starts tool-update threads that would go online
        try:
            window = DistillWindow()
        finally:
            threading.Thread.start = real_start
        report.note(window.stack.count() > 5, f"window built with {window.stack.count()} pages")
    except BaseException:  # a crash is a failed test; keep the details
        report.note(False, "unexpected error:\n" + traceback.format_exc())
    if report_path is not None:
        with contextlib.suppress(OSError):
            report_path.write_text(report.text(), encoding="utf-8")
    elif sys.stdout is not None:  # a windowed .exe has none
        print(report.text())
    return report.exit_code()


def main() -> int:
    if SELF_TEST_FLAG in sys.argv[1:]:  # before the crash handler below: its dialog would hang a headless run
        after = sys.argv[sys.argv.index(SELF_TEST_FLAG) + 1:]
        code = self_test(Path(after[0]) if after else None)
        if getattr(sys, "frozen", False):
            os._exit(code)  # no event loop to unwind; don't let Qt's teardown turn a pass into a crash
        return code
    crashlog.install(_tell_about_crash)
    if sys.platform == "win32":
        with contextlib.suppress(Exception):  # a taskbar identity of its own, not "python"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.{APP_VERSION}")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    links = magnet.links_from_args(sys.argv[1:])  # a magnet: link opened from a browser or another program
    if links and magnet.send_to_running(links):
        return 0  # an open Distill took it
    prepare(app)
    window = DistillWindow()
    window.show()
    magnet.serve(window)
    if links:
        QTimer.singleShot(0, lambda: window.open_magnets(links))
    return app.exec()
