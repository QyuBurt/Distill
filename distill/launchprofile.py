"""A game's own launch settings: what to do differently for this one game when it is started.

  args   options for the game: they replace an emulator of its own's launch options for this game, and are added to RetroArch's command
  cwd    the folder the emulator starts in (instead of its own folder)
  env    environment variables, one KEY=VALUE per line
  pre    a command to run (and wait for) before the emulator starts: mount a drive, switch a display mode, start a helper
  post   a command to run after the emulator closes
  admin  start it as administrator (Windows only; Distill can't time a session it didn't start itself)

In `pre`, `post` and `args`, {rom} is the game's file, {dir} its folder and {name} its file name without the extension. The profile is kept
as JSON in the game store's settings, one per game. No Qt here, so it is tested on its own.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

PRE_TIMEOUT = 60.0  # seconds a "run before" command may take before the game is not started


class HookError(Exception):
    """A command before or after a game failed, in words the player can act on."""


@dataclass(slots=True)
class Profile:
    args: str = ""
    cwd: str = ""
    env: str = ""
    pre: str = ""
    post: str = ""
    admin: bool = False

    def empty(self) -> bool:
        return not (self.args.strip() or self.cwd.strip() or self.env.strip() or self.pre.strip() or self.post.strip() or self.admin)

    def to_json(self) -> str:
        """The profile as it is stored ('' for one with nothing in it, so nothing is kept)."""
        return "" if self.empty() else json.dumps({k: v for k, v in asdict(self).items() if v not in ("", False)}, sort_keys=True)

    @staticmethod
    def from_json(text: str) -> Profile:
        """Read a stored profile; anything unreadable is an empty one (a hand-edited settings file must not stop a game starting)."""
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            return Profile()
        if not isinstance(data, dict):
            return Profile()
        known = {f.name: f.type for f in fields(Profile)}
        out = {}
        for key, value in data.items():
            if key == "admin":
                out[key] = value is True
            elif key in known and isinstance(value, str):
                out[key] = value
        return Profile(**out)


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines to a dict: blank lines, #comments and lines that aren't KEY=VALUE are ignored; the value is taken as written."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _eq, value = line.partition("=")
        key = key.strip()
        if key and not any(c.isspace() for c in key):
            out[key] = value.strip()
    return out


def shell_quote(text: str) -> str:
    return subprocess.list2cmdline([text]) if sys.platform == "win32" else shlex.quote(text)


def expand(command: str, game_path: str) -> str:
    """A command with {rom}, {dir} and {name} filled in (quoted, so a path with spaces stays one word)."""
    path = Path(game_path)
    out = command
    for key, value in (("{rom}", str(path)), ("{dir}", str(path.parent)), ("{name}", path.stem)):
        out = out.replace(key, shell_quote(value))
    return out


def run_hook(command: str, game_path: str, cwd: str = "", wait: bool = True, timeout: float = PRE_TIMEOUT, what: str = "The command") -> int | None:
    """Run a profile's command through the shell. Waits (up to `timeout`) when `wait`, and returns its exit code; otherwise starts it and
    returns None. A command that can't run, fails, or takes too long raises HookError."""
    command = command.strip()
    if not command:
        return 0 if wait else None
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if not wait:
            subprocess.Popen(expand(command, game_path), shell=True, cwd=cwd or None, **quiet)
            return None
        result = subprocess.run(expand(command, game_path), shell=True, cwd=cwd or None, timeout=timeout, **quiet)
    except subprocess.TimeoutExpired as exc:
        raise HookError(f"{what} took longer than {int(timeout)} seconds, so the game was not started.") from exc
    except OSError as exc:
        raise HookError(f"{what} couldn't be run: {exc.strerror or exc}") from exc
    if result.returncode != 0:
        raise HookError(f"{what} failed (it ended with code {result.returncode}), so the game was not started.")
    return result.returncode
