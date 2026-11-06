"""The Distill app icon, from the bundled PNGs (one per size, drawn natively so small sizes stay crisp). The dark look uses
the deep "dark" colourway and the light look the "bright" one."""
from __future__ import annotations

from PySide6.QtGui import QIcon, QPixmap

from .assets import asset
from .theme import T

SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def _variant(dark: bool | None) -> str:
    return "dark" if (T.dark if dark is None else dark) else "bright"


def app_icon_pixmap(size: int = 256, dark: bool | None = None) -> QPixmap:
    """The icon at (about) this size: the nearest bundled size at or above it, so it is only ever scaled down. `dark`
    picks the colourway; left out, it follows the current look."""
    wanted = next((s for s in SIZES if s >= size), SIZES[-1])
    return QPixmap(str(asset("icons", f"distill-{_variant(dark)}-{wanted}.png")))


def make_app_icon(dark: bool | None = None) -> QIcon:
    """Window / taskbar icon with every bundled size, so the shell picks the sharpest one. `dark` as for app_icon_pixmap."""
    icon = QIcon()
    variant = _variant(dark)
    for size in SIZES:
        pix = QPixmap(str(asset("icons", f"distill-{variant}-{size}.png")))
        if not pix.isNull():
            icon.addPixmap(pix)
    return icon
