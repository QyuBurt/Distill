"""What happens when a download run is over: run a command of the person's own, then sleep, hibernate or shut down.
No Qt in here (the Download page starts these and shows the countdown).

The command is a plain command line, not a shell script: it is split at spaces (double quotes keep a path with spaces
together) and started directly, so nothing in a file name can add to it. Four words are filled in:

    {folder}  the download folder        {count}   how many files finished
    {list}    a text file listing them   {failed}  how many failed
    (one path per line, UTF-8)

The command runs in the download folder, and the power step waits for it and is skipped if it fails."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .constants import SUBPROCESS_FLAGS

POWER = ("", "sleep", "hibernate", "shutdown")
POWER_LABELS = ("Nothing", "Sleep", "Hibernate", "Shut down")
POWER_PHRASES = {"sleep": "go to sleep", "hibernate": "hibernate", "shutdown": "shut down"}
COMMAND_TIMEOUT = 3600  # seconds a command may run before it is given up on


def tokenize(text: str) -> list[str]:
    """Split a command line at spaces; "double quotes" keep spaces together. Raises ValueError for an open quote."""
    tokens: list[str] = []
    cur: list[str] = []
    quoted = started = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            started = True
        elif ch.isspace() and not quoted:
            if started:
                tokens.append("".join(cur))
                cur, started = [], False
        else:
            cur.append(ch)
            started = True
    if quoted:
        raise ValueError("a quote was opened and never closed")
    if started:
        tokens.append("".join(cur))
    return tokens


def expand(tokens: list[str], values: dict[str, str]) -> list[str]:
    """Fill {folder}, {list}, {count} and {failed} into each word (only those four; anything else is left as written)."""
    out = []
    for token in tokens:
        for key, value in values.items():
            token = token.replace("{" + key + "}", value)
        out.append(token)
    return out


def write_list(files: list[Path]) -> Path:
    fd, name = tempfile.mkstemp(prefix="distill-finished-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"{p}\n" for p in files))
    return Path(name)


def start_command(template: str, folder: str | Path, files: list[Path], failed: int):
    """Start the person's command. Returns (the running process, '') or (None, a plain reason it could not start)."""
    try:
        words = tokenize(template)
    except ValueError as exc:
        return None, str(exc)
    if not words:
        return None, ""
    listing = write_list(files)
    args = expand(words, {"folder": str(folder), "list": str(listing), "count": str(len(files)), "failed": str(failed)})
    if not (Path(args[0]).is_file() or shutil.which(args[0])):
        return None, f"couldn't find {args[0]}"
    try:
        proc = subprocess.Popen(args, cwd=str(folder) if Path(folder).is_dir() else None, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
    except (OSError, ValueError) as exc:
        return None, str(exc) or type(exc).__name__
    return proc, ""


def _windows_suspend() -> bool:
    import ctypes  # noqa: PLC0415  (only exists to be called on Windows)
    # SetSuspendState(hibernate=False, force=False, disableWakeEvent=False): sleep, even where hibernation is switched on
    return bool(ctypes.windll.powrprof.SetSuspendState(False, False, False))  # type: ignore[attr-defined]


def power_argv(action: str, system: str) -> list[str] | None:
    """The command that does this on this kind of computer, or None where it is done another way (Windows sleep)."""
    if system == "Windows":
        return {"hibernate": ["shutdown", "/h"], "shutdown": ["shutdown", "/s", "/t", "0"]}.get(action)
    if system == "Darwin":
        if action == "shutdown":
            return ["osascript", "-e", 'tell application "System Events" to shut down']
        return ["pmset", "sleepnow"]  # macOS has no separate hibernate to ask for
    return {"sleep": ["systemctl", "suspend"], "hibernate": ["systemctl", "hibernate"], "shutdown": ["systemctl", "poweroff"]}.get(action)


def perform_power(action: str, system: str | None = None, run: Callable = subprocess.run,
                  suspend: Callable[[], bool] = _windows_suspend) -> str:
    """Do it. Returns '' or a plain reason it did not work. (`system`, `run` and `suspend` are there for the tests.)"""
    system = system or platform.system()
    if action not in POWER[1:]:
        return f"unknown action {action!r}"
    try:
        if system == "Windows" and action == "sleep":
            return "" if suspend() else "Windows refused to go to sleep"
        argv = power_argv(action, system)
        if argv is None:
            return f"{action} isn't supported on this computer"
        done = run(argv, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS) if system == "Windows" \
            else run(argv, capture_output=True, text=True)
        if done.returncode != 0:
            return (done.stderr or done.stdout or f"exit code {done.returncode}").strip().splitlines()[0][:160]
        return ""
    except (OSError, ValueError) as exc:
        return str(exc) or type(exc).__name__
