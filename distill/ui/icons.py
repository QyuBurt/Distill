"""Line icons for card headings, the sidebar search and similar. Drawn from SVG path data (24 x 24 grid, 2 px round
stroke) and tinted at runtime, so they stay sharp at any scale and follow the theme."""
from __future__ import annotations

import re

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .assets import asset

PATHS: dict[str, str] = {
    "search": '<circle cx="11" cy="11" r="7.5"/><path d="M21 21l-4.6-4.6"/>',
    "sidebar": '<rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="M9.5 4.5v15"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "folder-open": '<path d="M3 18V6a2 2 0 0 1 2-2h4l2 2.5h6a2 2 0 0 1 2 2V9"/>'
                   '<path d="M3 18l2.4-7.2A2 2 0 0 1 7.3 9.5H21a1 1 0 0 1 .95 1.3L19.7 17.6A2 2 0 0 1 17.8 19H4a1 1 0 0 1-1-1z"/>',
    "wand": '<path d="M5 19L16 8"/><path d="M14 6l4 4"/><path d="M18 3v3M16.5 4.5h3"/><path d="M6 5v2.5M4.75 6.25h2.5"/>'
            '<path d="M19 15v2.5M17.75 16.25h2.5"/>',
    "sliders": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/>'
               '<circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    "terminal": '<path d="M5 8l4 4-4 4"/><path d="M12 17h7"/><rect x="2.5" y="3.5" width="19" height="17" rx="3"/>',
    "disc": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="M12 3a9 9 0 0 1 6.4 2.6"/>',
    "gamepad": '<path d="M7 9h10a4 4 0 0 1 4 4.2l-.3 3.3a2 2 0 0 1-3.5 1.1L15.8 15.5H8.2l-1.4 2.1a2 2 0 0 1-3.5-1.1'
               'L3 13.2A4 4 0 0 1 7 9z"/><path d="M8 11.5v3M6.5 13h3"/><path d="M15.5 12.2h.01M17.8 13.8h.01"/>',
    "cube": '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
    "archive": '<rect x="3" y="4" width="18" height="4.5" rx="1.2"/><path d="M5 8.5V18a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5"/>'
               '<path d="M10 12.5h4"/>',
    "brush": '<path d="M14.5 4.5l5 5-8 8-5-5z"/><path d="M6.5 12.5C5 14 4.2 15.5 4 18c-.1 1.2-.9 2-2 2 2.4.4 4.6.2 6-1.2"/>'
             '<path d="M13 6l5 5"/>',
    "layout": '<rect x="3.5" y="3.5" width="7.5" height="9" rx="1.5"/><rect x="13" y="3.5" width="7.5" height="5" rx="1.5"/>'
              '<rect x="13" y="11" width="7.5" height="9.5" rx="1.5"/><rect x="3.5" y="15" width="7.5" height="5.5" rx="1.5"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "download": '<path d="M12 3v12M7 10.5l5 5 5-5"/><path d="M4 18v1a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-1"/>',
    "music": '<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>'
            '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "file": '<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M13 2v7h7"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "chevron-up": '<path d="M6 15l6-6 6 6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15.5" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
    # media transport: solid shapes use the placeholder @c, which svg_bytes swaps for the colour asked for
    "play": '<path d=\"M8 5l11 7-11 7z\" fill=\"@c\"/>',
    "pause": '<rect x=\"6.5\" y=\"5\" width=\"3.6\" height=\"14\" rx=\"1\" fill=\"@c\"/>'
             '<rect x=\"13.9\" y=\"5\" width=\"3.6\" height=\"14\" rx=\"1\" fill=\"@c\"/>',
    "star": '<path d="M12 3.2l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 3.1 1-6.1-4.4-4.3 6.1-.9z" fill="@c"/>',
    "stop": '<rect x=\"6\" y=\"6\" width=\"12\" height=\"12\" rx=\"2\" fill=\"@c\"/>',
    "skip-forward": '<path d=\"M5 5l9 7-9 7z\" fill=\"@c\"/><path d=\"M18.5 5v14\"/>',
    "skip-back": '<path d=\"M19 5l-9 7 9 7z\" fill=\"@c\"/><path d=\"M5.5 5v14\"/>',
    "volume": '<path d=\"M4 9.5h3.5L12 5.5v13l-4.5-4H4z\" fill=\"@c\"/><path d=\"M15.5 9a4.5 4.5 0 0 1 0 6M18.2 6.5a8 8 0 0 1 0 11\"/>',
    "volume-muted": '<path d=\"M4 9.5h3.5L12 5.5v13l-4.5-4H4z\" fill=\"@c\"/><path d=\"M16 9.5l5 5M21 9.5l-5 5\"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.8h.01"/>',
    "alert": '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.2h.01"/>',
    "shield-check": '<path d="M12 3l8 3v5.5c0 4.6-3.3 8.1-8 9.5-4.7-1.4-8-4.9-8-9.5V6z"/><path d="M8.8 12l2.3 2.3 4.2-4.6"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
    "branch": '<circle cx="6" cy="5.5" r="2.3"/><circle cx="6" cy="18.5" r="2.3"/><circle cx="18" cy="9" r="2.3"/><path d="M6 8v8M18 11.3c0 3.7-6 2.7-11 5"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="1.8"/><path d="M21 15.5l-5-5L5.5 21"/>',
    "upload": '<path d="M12 15.5V4M7.5 8.5L12 4l4.5 4.5"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4h6v3"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "edit": '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7'
                'M5.5 18.5l1.7-1.7M16.8 7.2l1.7-1.7"/>',
}

# card / heading words -> icon, so a card gets its icon from its title without each page having to name one
BY_TITLE = (
    ("folder", "folder-open"), ("workflow", "wand"), ("option", "wand"), ("compression", "sliders"), ("speed", "sliders"),
    ("level", "sliders"), ("log", "terminal"), ("performance", "sliders"), ("save", "download"), ("download", "download"),
    ("link", "link"), ("file", "file"),
    ("games", "gamepad"), ("soundtrack", "music"), ("music", "music"), ("package", "cube"), ("archive", "archive"),
    ("playstation", "playstation"), ("junk", "brush"), ("platform", "layout"), ("report", "chart"), ("disc", "disc"), ("setting", "settings"),
    ("update", "branch"),
)


def icon_for_title(title: str) -> str | None:
    """The icon for a card title, by whole words ('Folders' and 'Live process log' match; 'Logins' does not)."""
    low = title.lower()
    return next((icon for word, icon in BY_TITLE if re.search(rf"\b{word}s?\b", low)), None)


def svg_bytes(name: str, colour: str, stroke: float = 2.0) -> QByteArray:
    body = PATHS[name].replace("@c", colour)
    return QByteArray((f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{colour}" '
                       f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>').encode())


PICTURES = {"playstation": "playstation.png", "n64": "n64.png", "xbox": "xbox.png", "dreamcast": "dreamcast.png",
            "atari": "atari.png", "gamecube": "gamecube.png", "retroarch": "retroarch.png", "multi": "multi.png", "pc": "pc.png", "mobile": "mobile.png"}  # icons that are bundled pictures rather than line drawings
_cache: dict[tuple, QPixmap] = {}


def pixmap(name: str, colour: QColor | str, size: int = 18, dpr: float = 2.0, stroke: float = 2.0) -> QPixmap:
    """The icon as a crisp pixmap of `size` logical pixels (drawn at `dpr` times that)."""
    col = colour.name() if isinstance(colour, QColor) else colour
    key = (name, col, size, dpr, stroke)
    if key not in _cache:
        px = QPixmap(int(size * dpr), int(size * dpr))
        px.setDevicePixelRatio(dpr)
        px.fill(Qt.GlobalColor.transparent)
        painter = QPainter(px)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if name in PICTURES:  # a bundled picture (white on transparent), tinted to the colour asked for
            src = QPixmap(str(asset("icons", PICTURES[name])))
            src = src.scaled(int(size * dpr), int(size * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            tinted = QPixmap(src.size())
            tinted.fill(QColor(col))
            tp = QPainter(tinted)
            tp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            tp.drawPixmap(0, 0, src)
            tp.end()
            w, h = tinted.width() / dpr, tinted.height() / dpr
            painter.drawPixmap(QRectF((size - w) / 2, (size - h) / 2, w, h), tinted, QRectF(tinted.rect()))
        else:
            QSvgRenderer(svg_bytes(name, col, stroke)).render(painter, QRectF(0, 0, size, size))
        painter.end()
        _cache[key] = px
    return _cache[key]


def icon(name: str, colour: QColor | str, size: int = 18) -> QIcon:
    return QIcon(pixmap(name, colour, size))


def button_icon(name: str, colour: QColor | str, hover: QColor | str | None = None, size: int = 18) -> QIcon:
    """An icon for a flat button: `colour` normally, `hover` (when given) while the pointer is over it."""
    out = QIcon()
    out.addPixmap(pixmap(name, colour, size), QIcon.Mode.Normal)
    out.addPixmap(pixmap(name, hover if hover is not None else colour, size), QIcon.Mode.Active)
    return out
