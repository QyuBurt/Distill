"""A record of unexpected errors.

Distill runs as a windowed .pyw: there is no console, so an exception in a Qt slot or a worker thread would otherwise
vanish without a trace and a bug report would have nothing to go on. Every unhandled error - main thread, worker
thread, or a hard crash in native code - is appended to distill_crash.txt beside the program.
"""
from __future__ import annotations

import contextlib
import faulthandler
import platform
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from .constants import APP_DIR, APP_NAME, APP_VERSION

CRASH_FILE = APP_DIR / "distill_crash.txt"
MAX_BYTES = 256 * 1024  # older entries are dropped past this, so the file can't grow without limit

_fault_stream: TextIO | None = None  # faulthandler needs the file to stay open for the life of the process


def record(exc_type: type[BaseException], exc: BaseException, tb, where: str = "") -> Path | None:
    """Append one error to the crash file. Returns the file, or None if it can't be written (a read-only folder
    must never turn one error into two)."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"=== {stamp} | {APP_NAME} {APP_VERSION} | Python {platform.python_version()} | {platform.platform()}"
    entry = header + (f" | {where}" if where else "") + "\n" + "".join(traceback.format_exception(exc_type, exc, tb)) + "\n"
    try:
        old = CRASH_FILE.read_text(encoding="utf-8", errors="replace") if CRASH_FILE.exists() else ""
        text = old + entry
        if len(text.encode("utf-8")) > MAX_BYTES:
            text = text[-MAX_BYTES // 2:]
            text = text[text.find("\n=== ") + 1:] if "\n=== " in text else entry  # start on a whole entry
        CRASH_FILE.write_text(text, encoding="utf-8")
        return CRASH_FILE
    except OSError:
        return None


def install(on_crash: Callable[[Path | None, BaseException], None] | None = None, *, faults: bool = True) -> None:
    """Route unhandled errors to the crash file. `on_crash(file, error)` is called for main-thread errors only
    (it may show a dialog), and a failure inside it is swallowed."""
    previous = sys.excepthook

    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        saved = record(exc_type, exc, tb)
        with contextlib.suppress(Exception):
            previous(exc_type, exc, tb)  # still printed when there is a console
        if on_crash:
            with contextlib.suppress(Exception):
                on_crash(saved, exc)

    def thread_hook(args) -> None:
        if args.exc_type is not SystemExit:
            record(args.exc_type, args.exc_value, args.exc_traceback, f"thread {getattr(args.thread, 'name', '?')}")

    sys.excepthook = hook
    threading.excepthook = thread_hook
    if faults:
        global _fault_stream
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            _fault_stream = open(CRASH_FILE, "a", encoding="utf-8")  # noqa: SIM115 - kept open on purpose
            faulthandler.enable(_fault_stream)
