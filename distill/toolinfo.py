"""What Distill knows about each tool's copy: which version it downloaded, whether the copy is the person's own, and whether an
"Update now" can do anything on this computer. It asks the tool managers (tools.py) what they already know; it downloads and
changes nothing. No Qt.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from . import tools


def managers() -> dict[str, type]:
    """Every tool manager, by the key it reports under ("chdman", "ytdlp", ...)."""
    found: dict[str, type] = {}
    stack = [tools.ToolManager]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            stack.append(sub)
            if sub.KEY:
                found[sub.KEY] = sub
    return found


@dataclass(frozen=True, slots=True)
class ToolInfo:
    version: str      # the release Distill downloaded, as the tool's page words it ("v0.14.0"); "" if none yet
    source: str       # where it comes from ("MAME", "Dolphin", "yt-dlp")
    own_copy: bool    # the person put a copy in the tool folder themselves: Distill leaves it alone
    can_update: bool  # whether "Update now" can do anything here
    reason: str       # when it can't: why, in a sentence


def tool_info(manager) -> ToolInfo:
    """Read one manager's answers. `manager` is a tools.ToolManager (anything with the same few members will do)."""
    try:
        if manager._local_copy():
            return ToolInfo("", manager.SOURCE, True, False, "You placed your own copy in the tools folder, so Distill leaves it alone.")
        cached = manager.cached()
        version = str(manager._label(cached[0])) if cached else ""
    except Exception:  # a folder that vanished, a drive that dropped: no version to show, and no reason to fail the page
        return ToolInfo("", manager.SOURCE, False, False, "Couldn't read this tool's folder.")
    if manager.WINDOWS_ONLY and sys.platform != "win32":
        return ToolInfo(version, manager.SOURCE, False, False,
                        "Distill only downloads this tool on Windows: install it with your package manager.")
    return ToolInfo(version, manager.SOURCE, False, True, "")


def version_text(info: ToolInfo) -> str:
    """The short line beside a tool: what it is running."""
    if info.own_copy:
        return "Your own copy"
    if info.version:
        return f"{info.version}  \u00b7  {info.source}"
    return "Not downloaded yet"


class ToolInfos:
    """The managers, built once and asked as often as needed (building one makes sure its folder exists, so it is not done per ask)."""

    def __init__(self) -> None:
        self._managers: dict[str, object] = {}

    def info(self, key: str) -> ToolInfo | None:
        """None for a key with no manager."""
        if key not in self._managers:
            cls = managers().get(key)
            if cls is None:
                return None
            self._managers[key] = cls(lambda _event: None)  # nobody listens: this only reads
        return tool_info(self._managers[key])
