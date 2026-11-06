"""Bundled files (fonts, icons): found next to the code when run from source, and inside the bundle when frozen."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFontDatabase

from ..constants import ASSETS_DIR as ASSETS

TEXT_FONT = "Inter 18pt"  # Inter's text cut (optical size 18): the UI font, every control and paragraph
DISPLAY_FONT = "Outfit"  # a geometric sans (it echoes the round logo and cards): page titles, dialog heads, big numbers
MONO_FONT = "JetBrains Mono"  # the log, link boxes and paths: the same on every machine, with clear 0/O and 1/l
FONT_FILES = ("Inter_18pt-Regular", "Inter_18pt-Medium", "Inter_18pt-SemiBold", "Inter_18pt-Bold",
              "Outfit-Bold",  # one weight on purpose: it is only ever used big and bold
              "JetBrainsMono-Regular", "JetBrainsMono-Bold")


def asset(*parts: str) -> Path:
    return ASSETS.joinpath(*parts)


def asset_url(*parts: str) -> str:
    """A path for use inside a Qt style sheet (forward slashes, even on Windows)."""
    return asset(*parts).as_posix()


def load_fonts() -> tuple[str, str, str]:
    """Register the bundled font files. Returns (text, display, mono) families; a family whose files are missing falls back
    to Segoe UI (text), the text family (display) or Cascadia Mono (mono)."""
    loaded: set[str] = set()
    for name in FONT_FILES:
        font_id = QFontDatabase.addApplicationFont(str(asset("fonts", name + ".ttf")))
        if font_id >= 0:
            loaded.update(QFontDatabase.applicationFontFamilies(font_id))
    text = TEXT_FONT if TEXT_FONT in loaded else "Segoe UI"
    display = DISPLAY_FONT if DISPLAY_FONT in loaded else text
    mono = MONO_FONT if MONO_FONT in loaded else "Cascadia Mono"
    return text, display, mono
