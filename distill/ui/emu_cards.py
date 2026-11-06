"""The building blocks of the Emulators page: a grid of cards that reflows with the window, one card per emulator, one row per
fork, and the small count tiles at the top. Pure layout and painting: what to show, and what a click does, is decided by
`emulator_page`."""
from __future__ import annotations

import html
import re

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import icons
from .icons import pixmap
from .theme import T


GROUP_COLOURS = {"Sony": "#4d9bff", "Nintendo": "#8a7cf5", "Microsoft": "#5fd4a0", "Sega": "#3f74e8", "Multi": "#b585f4", "Frontend": "#f2f4f8", "Atari": "#f5a742",
                 "PC": "#22c1dc", "Mobile": "#ee6fb5"}


GROUP_LOGOS = {"Sony": "playstation", "Nintendo": "gamecube", "Microsoft": "xbox", "Sega": "dreamcast", "Atari": "atari",
               "Multi": "multi", "PC": "pc", "Mobile": "mobile", "Frontend": "retroarch"}  # group -> bundled mark


def group_colour(group: str) -> QColor:
    if group == "Frontend":  # near-white in the dark theme: a slate grey in the light one
        return QColor(T.frontend)
    return QColor(GROUP_COLOURS.get(group, T.accent_text.name()))


def chip_css(colour: QColor) -> str:
    """The marker style used at the top of each page (small bold capitals in a faint box), in the given colour."""
    r, g, b = colour.red(), colour.green(), colour.blue()
    return (f"background: rgba({r}, {g}, {b}, 30); color: {colour.name()}; border: 1px solid rgba({r}, {g}, {b}, 90); border-radius: 6px; "
            "padding: 2px 9px; font-size: 9pt; font-weight: 700;")


def group_chip(group: str, count: int) -> QFrame:
    """The marker above each group of cards ('SONY  ·  8'): the chip look of `chip_css`, with the maker's mark in front of the
    text in the chip's own colour. A group with no mark of its own is the text alone."""
    colour = group_colour(group)
    r, g, b = colour.red(), colour.green(), colour.blue()
    chip = QFrame()
    chip.setObjectName("groupChip")
    chip.setStyleSheet(f"QFrame#groupChip {{ background: rgba({r}, {g}, {b}, 30); border: 1px solid rgba({r}, {g}, {b}, 90); border-radius: 6px; }}")
    lay = QHBoxLayout(chip)
    lay.setContentsMargins(9, 2, 9, 2)
    lay.setSpacing(6)
    lay.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)  # the chip is exactly as big as its contents
    if (name := GROUP_LOGOS.get(group)) is not None:
        logo = QLabel()
        logo.setFixedSize(12, 12)
        logo.setPixmap(pixmap(name, colour, 12))
        logo.setStyleSheet("background: transparent; border: none; border-radius: 0; padding: 0; margin: 0;")
        lay.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)
    text = QLabel(f"{group.upper()}  \u00b7  {count}")
    T.bind_css(text, lambda: f"background: transparent; border: none; border-radius: 0; padding: 0; color: {colour.name()}; font-size: 9pt; font-weight: 700;")
    text.ensurePolished()  # apply the font before the chip is measured
    lay.addWidget(text, 0, Qt.AlignmentFlag.AlignVCenter)
    return chip


def _device_ratio() -> float:
    """The sharpest screen's pixel ratio (at least 1): a picture drawn at exactly the pixels it will occupy stays crisp, where one drawn
    at a fixed 2x and then shrunk by the screen goes soft."""
    if QGuiApplication.instance() is None:
        return 2.0
    return max([1.0, *(screen.devicePixelRatio() for screen in QGuiApplication.screens())])


def _full_bleed(image: QImage) -> bool:
    """Does the picture have its own background that runs to the edges (a red square, a purple tile)? Checked along all four edges, a
    few pixels in from them, so a picture with rounded corners still counts. Such a picture is let fill the whole tile."""
    w, h = image.width(), image.height()
    if w < 8 or h < 8:
        return False
    inset = max(1, min(w, h) // 64)
    points = [(x, inset) for x in (w // 4, w // 2, 3 * w // 4)] + [(x, h - 1 - inset) for x in (w // 4, w // 2, 3 * w // 4)] \
        + [(inset, y) for y in (h // 4, h // 2, 3 * h // 4)] + [(w - 1 - inset, y) for y in (h // 4, h // 2, 3 * h // 4)]
    return all(image.pixelColor(x, y).alpha() >= 250 for x, y in points)


def logo_pixmap(path: str, colour: QColor, size: int = 40, dpr: float | None = None, radius: int = 10) -> QPixmap | None:
    """The picture in a rounded tile, or None when the file isn't a readable image. A picture with its own background fills the whole
    tile; any other (a logo on transparency) is drawn as it is, large with a thin margin, on nothing: no tint, no outline.
    `colour` (the group's) is no longer used here and is kept so callers need not change."""
    src = QImage(path)
    if src.isNull():
        return None
    dpr = dpr or _device_ratio()
    full = max(1, round(size * dpr))  # the tile in device pixels
    px = QPixmap(full, full)
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)
    side = full / dpr  # the tile in logical pixels, as the device pixels make it
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, side, side), radius, radius)
    p.setClipPath(clip)
    if _full_bleed(src):
        inner = full
    else:
        inner = full - 2 * max(round(3 * dpr), round(full * 0.09))
    scaled = src.scaled(inner, inner, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    off_x, off_y = (full - scaled.width()) // 2, (full - scaled.height()) // 2  # whole device pixels: no resampling on the way out
    p.drawImage(QRectF(off_x / dpr, off_y / dpr, scaled.width() / dpr, scaled.height() / dpr), scaled)
    p.end()
    return px


def pill_css(colour: QColor) -> str:
    """A small rounded tag: the colour as text on a faint wash of itself."""
    return (f"background: rgba({colour.red()}, {colour.green()}, {colour.blue()}, 40); color: {colour.name()}; border-radius: 9px; "
            "padding: 2px 9px; font-size: 9pt; font-weight: 600;")


def segment_css(first: bool) -> str:
    """One half of a two-way switch (Tiles | Cards): outlined like the other buttons, the pressed half lit in the accent. The
    halves share their middle edge, so only the outer corners are round."""
    left, right = ("10px", "0px") if first else ("0px", "10px")
    return (f"QPushButton {{ background: {T.input.name()}; border: 1px solid {T.border.name()}; color: {T.fg3.name()}; "
            f"padding: 8px 14px; font-weight: 600; border-top-left-radius: {left}; border-bottom-left-radius: {left}; "
            f"border-top-right-radius: {right}; border-bottom-right-radius: {right}; {'' if first else 'border-left-width: 0px; '}}}"
            f"QPushButton:hover {{ color: {T.title.name()}; background: {T.hover.name()}; }}"
            f"QPushButton:checked {{ background: {T.select.name()}; color: {T.accent_text.name()}; border-color: {T.accent.name()}; }}")


def _label(text: str = "", css: str = "", wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet(css)
    lab.setWordWrap(wrap)
    return lab


def _shrinkable(label: QLabel) -> QLabel:
    """A label that is clipped rather than pushing its column wider."""
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


class Clickable(QFrame):
    """A rounded surface that lifts its outline on hover and emits `clicked(key)` when pressed (mouse, Enter or Space)."""

    clicked = Signal(str)

    def __init__(self, key: str, name: str = "emuCard", radius: int = 14):
        super().__init__()
        self.key = key
        self.setObjectName(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        T.bind_css(self, lambda: f"QFrame#{name} {{ background: {T.card.name()}; border: 1px solid {T.card_border.name()}; border-radius: {radius}px; }}"
            f"QFrame#{name}:hover, QFrame#{name}:focus {{ border-color: {T.accent.name()}; background: {T.hover.name()}; }}")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.key)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.key)
        else:
            super().keyPressEvent(event)


class CardGrid(QWidget):
    """Cards in as many equal columns as fit. The columns are chosen from the width, so a wider window shows more per row
    and a narrow one a single column."""

    def __init__(self, min_card: int = 300, gap: int = 12, max_cols: int = 4):
        super().__init__()
        self._min, self._gap, self._max = min_card, gap, max_cols
        self._cards: list[QWidget] = []
        self._cols = 0
        self._lay = QGridLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(gap)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(1, super().minimumSizeHint().height())  # the width follows the window; the columns follow the width

    def columns_for(self, width: int) -> int:
        return max(1, min(self._max, (width + self._gap) // (self._min + self._gap)))

    def set_cards(self, cards: list[QWidget]) -> None:
        for card in self._cards:
            self._lay.removeWidget(card)
        self._cards = list(cards)
        self._place(self.columns_for(self.width()))

    def _place(self, cols: int) -> None:
        self._cols = cols
        for card in self._cards:
            self._lay.removeWidget(card)
        for i, card in enumerate(self._cards):
            self._lay.addWidget(card, i // cols, i % cols)
        for c in range(self._max):
            self._lay.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if (cols := self.columns_for(self.width())) != self._cols:
            self._place(cols)


# ---- the page for one emulator: hero, callouts, facts and a body that turns into one column when narrow -------------------------
def tint(colour: QColor, alpha: int) -> str:
    """The colour as a CSS rgba() at the given alpha (0..255)."""
    return f"rgba({colour.red()}, {colour.green()}, {colour.blue()}, {alpha})"


def clear_layout(lay: QLayout) -> None:
    """Remove and delete everything in a layout (widgets and nested layouts)."""
    while lay.count():
        item = lay.takeAt(0)
        if (w := item.widget()) is not None:
            w.setParent(None)
            w.deleteLater()
        elif (inner := item.layout()) is not None:
            clear_layout(inner)


def break_path(text: str) -> str:
    """A path or address with a zero-width space after each separator, so a label can wrap it instead of running off the edge."""
    return re.sub(r"([\\/_.-])", "\\1\u200b", text)


def caps_label(text: str) -> QLabel:
    """A small caption, for the name of a fact or a field: sentence case, medium weight (capitals are hard to read small)."""
    return _label(text, f"color: {T.fg3.name()}; font-size: 9pt; font-weight: 500;")


def section_header(title: str, icon_name: str = "", colour: QColor | None = None) -> QHBoxLayout:
    """The heading row of a card on this page: a small line icon, the title, then room for whatever the caller adds."""
    row = QHBoxLayout()
    row.setSpacing(9)
    if icon_name:
        glyph = QLabel()
        glyph.setPixmap(pixmap(icon_name, colour or T.accent_text, 18))
        glyph.setFixedSize(18, 18)
        row.addWidget(glyph)
    label = QLabel(title)
    label.setObjectName("cardTitle")
    row.addWidget(label)
    row.addStretch(1)
    return row


def labelled(label: str, widget: QWidget, note: str = "") -> QWidget:
    """A field for a half-width card: its name over the control (which gets the card's whole width), a note under it."""
    holder = QWidget()
    box = QVBoxLayout(holder)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(6)
    name = QLabel(label)
    name.setObjectName("pathLabel")
    box.addWidget(name)
    box.addWidget(widget)
    if note:
        hint_label = QLabel(note)
        hint_label.setObjectName("hint")
        hint_label.setWordWrap(True)
        box.addWidget(hint_label)
    return holder


def hairline() -> QFrame:
    """A one-pixel rule between the groups of a card, inset like the rule under an Expander's heading."""
    rule = QFrame()
    rule.setFixedHeight(1)
    T.bind_css(rule, lambda: f"background: {T.card_border.name()}; border: none;")
    return rule


class LogoTile(QLabel):
    """The emulator's picture on its page, or two letters in a tinted tile until the picture is there (or when it never will be)."""

    def __init__(self, size: int = 72, radius: int = 18):
        super().__init__()
        self._size, self._radius = size, radius
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def show_monogram(self, name: str, colour: QColor) -> None:
        self.setPixmap(QPixmap())
        self.setText(re.sub(r"[^A-Za-z0-9]", "", name)[:2].upper() or "?")
        T.bind_css(self, lambda: f"background: {tint(colour, 30)}; color: {colour.name()}; border: 1px solid {tint(colour, 90)}; "
                           f"border-radius: {self._radius}px; font-weight: 700; font-size: {max(10, self._size // 4)}pt;")

    def show_logo(self, path: str, colour: QColor) -> bool:
        """Show the picture; False (and nothing changed) when the file can't be read."""
        if (px := logo_pixmap(path, colour, self._size, radius=self._radius)) is None:
            return False
        self.setText("")
        T.bind_css(self, lambda: "background: transparent; border: none;")  # replaces the monogram's theme-bound style, which would otherwise return on a restyle
        self.setPixmap(px)
        return True


class Callout(QFrame):
    """A note that should not be skimmed past: an icon and a short paragraph on a faint wash of the note's colour.
    kind: "ok" (reassurance), "warn" (be careful), "bad" (something is wrong) or "info" (good to know)."""

    KINDS = {"ok": ("shield-check", "ok"), "warn": ("alert", "warn"), "bad": ("alert", "bad"), "info": ("info", "accent_text")}

    def __init__(self, kind: str, text: str):
        super().__init__()
        icon_name, attr = self.KINDS.get(kind, self.KINDS["info"])
        colour = getattr(T, attr)
        self.setObjectName("callout")
        T.bind_css(self, lambda: f"QFrame#callout {{ background: {tint(colour, 22)}; border: 1px solid {tint(colour, 70)}; border-radius: 10px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 14, 10)
        lay.setSpacing(10)
        glyph = QLabel()
        glyph.setPixmap(pixmap(icon_name, colour, 18))
        glyph.setFixedSize(18, 18)
        glyph.setStyleSheet("background: transparent; border: none;")
        lay.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        body = _label(text, f"background: transparent; border: none; color: {T.fg.name()};", wrap=True)
        lay.addWidget(body, 1)


class FactList(QWidget):
    """Small capitals over a value, one pair under another: the side panel of facts about an emulator. A fact with no value is left out."""

    def __init__(self):
        super().__init__()
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(11)

    def set_facts(self, facts: list[tuple[str, str, QColor | None]]) -> None:
        clear_layout(self._lay)
        for label, value, colour in facts:
            if not value:
                continue
            box = QVBoxLayout()
            box.setSpacing(2)
            box.addWidget(caps_label(label))
            text = _label(value, f"color: {(colour or T.fg).name()}; font-size: 9.5pt;" + (" font-weight: 600;" if colour else ""), wrap=True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.addWidget(text)
            self._lay.addLayout(box)


class TwoColumn(QWidget):
    """A wide column and a narrow side panel next to each other; when the width runs out the panel drops under the column.
    Switching has a little slack (wide from `wide_at`, narrow again below `wide_at - 40`) so a scroll bar appearing can't make it flicker."""

    def __init__(self, main: QWidget, side: QWidget, side_width: int = 300, wide_at: int = 820, gap: int = 14, equal: bool = False,
                 fill: bool = False):
        super().__init__()
        self._main, self._side, self._side_w, self._wide_at = main, side, side_width, wide_at
        self._equal = equal  # two halves instead of a column and a fixed-width panel
        self._fill = fill  # both columns as tall as the taller one (else each keeps its own height, at the top)
        self._wide: bool | None = None
        self._lay = QGridLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(gap)
        self._place(True)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(1, super().minimumSizeHint().height())

    def _place(self, wide: bool) -> None:
        self._wide = wide
        self._lay.removeWidget(self._main)
        self._lay.removeWidget(self._side)
        top = Qt.AlignmentFlag.AlignTop
        if wide:
            if self._equal:
                self._side.setMinimumWidth(0)
                self._side.setMaximumWidth(16777215)
            else:
                self._side.setFixedWidth(self._side_w)
            if self._fill:
                self._lay.addWidget(self._main, 0, 0)
                self._lay.addWidget(self._side, 0, 1)
            else:
                self._lay.addWidget(self._main, 0, 0, top)
                self._lay.addWidget(self._side, 0, 1, top)
            self._lay.setColumnStretch(0, 1)
            self._lay.setColumnStretch(1, 1 if self._equal else 0)
            self._lay.setColumnMinimumWidth(0, 0)
        else:
            self._side.setMinimumWidth(0)
            self._side.setMaximumWidth(16777215)
            self._lay.addWidget(self._main, 0, 0)
            self._lay.addWidget(self._side, 1, 0)
            self._lay.setColumnStretch(0, 1)
            self._lay.setColumnStretch(1, 0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        w = self.width()
        want = w >= self._wide_at if not self._wide else w >= self._wide_at - 40
        if want != self._wide:
            self._place(want)


class EmulatorCard(Clickable):
    """One emulator: a monogram, its name and system, a status tag, a line of facts, and Launch / Update when they apply."""

    action = Signal(str, str)  # emulator id, "launch" | "update"

    def __init__(self, rid: str, name: str, system: str, colour: QColor | None = None):
        super().__init__(rid)
        self.colour = colour or QColor(T.accent_text)
        self.setFixedHeight(126)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(12)
        tile = self.tile = QLabel(re.sub(r"[^A-Za-z0-9]", "", name)[:2].upper() or "?")
        tile.setFixedSize(40, 40)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        c = self.colour
        T.bind_css(tile, lambda: f"background: rgba({c.red()}, {c.green()}, {c.blue()}, 30); color: {c.name()}; "
                           f"border: 1px solid rgba({c.red()}, {c.green()}, {c.blue()}, 90); border-radius: 10px; font-weight: 700; font-size: 10pt;")
        names = QVBoxLayout()
        names.setSpacing(1)
        self.title = _shrinkable(_label(name, f"color: {T.title.name()}; font-weight: 600; font-size: 10.5pt;"))
        self.sub = _shrinkable(_label(system, f"color: {T.fg3.name()}; font-size: 9pt;"))
        names.addWidget(self.title)
        names.addWidget(self.sub)
        self.pill = QLabel()
        top.addWidget(tile)
        top.addLayout(names, 1)
        top.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.info = _shrinkable(_label("", f"color: {T.fg3.name()}; font-size: 9pt;"))
        self.launch = QPushButton("Launch")
        self.update_btn = QPushButton("Update")
        self.update_btn.setProperty("accent", True)
        for b, kind in ((self.launch, "launch"), (self.update_btn, "update")):
            b.setFixedHeight(30)
            b.clicked.connect(lambda _c=False, k=kind: self.action.emit(self.key, k))
        bottom.addWidget(self.info, 1)
        bottom.addWidget(self.launch)
        bottom.addWidget(self.update_btn)
        lay.addLayout(bottom)

    def apply(self, *, text: str, colour: QColor, facts: str, can_launch: bool, can_update: bool, busy: bool, tip: str = "") -> None:
        self.pill.setText(text)
        T.bind_css(self.pill, lambda: pill_css(colour))
        self.info.setText(facts)
        self.launch.setVisible(can_launch)
        self.update_btn.setVisible(can_update)
        self.launch.setEnabled(not busy)
        self.update_btn.setEnabled(not busy)
        self.setToolTip(tip)

    def set_busy_text(self, text: str) -> None:
        self.pill.setText(text[:28])

    def set_logo(self, path: str) -> None:
        """Swap the monogram for the project's picture (left as it is when the file can't be read)."""
        if (px := logo_pixmap(path, self.colour)) is not None:
            self.tile.setText("")
            T.bind_css(self.tile, lambda: "background: transparent; border: none;")  # replaces the monogram's theme-bound style (see LogoTile.show_logo)
            self.tile.setPixmap(px)


class EmulatorTile(Clickable):
    """The compact form of EmulatorCard, for the same job in under half the height: a monogram, the name, a second line with the
    status (in its colour) and the system, and Launch / Update as small icon buttons. It answers the same `apply` and
    `set_busy_text` calls, so the page fills either one the same way. What does not fit (version, games, forks) is in its tooltip."""

    action = Signal(str, str)  # emulator id, "launch" | "update"

    def __init__(self, rid: str, name: str, system: str, colour: QColor | None = None):
        super().__init__(rid, "emuTile", radius=11)
        self.colour = colour or QColor(T.accent_text)
        self.setFixedHeight(58)
        self._name, self._system = name, system
        self._status, self._colour = "", T.fg3
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 9, 8)
        lay.setSpacing(10)
        names = QVBoxLayout()
        names.setSpacing(1)
        self.title = _shrinkable(_label(name, f"color: {T.title.name()}; font-weight: 600; font-size: 10pt;"))
        self.sub = _shrinkable(_label("", f"color: {T.fg3.name()}; font-size: 9pt;"))
        self.sub.setTextFormat(Qt.TextFormat.RichText)
        names.addStretch(1)
        names.addWidget(self.title)
        names.addWidget(self.sub)
        names.addStretch(1)
        self.launch = self._tool_button("play", "Launch", "launch", accent=False)
        self.update_btn = self._tool_button("download", "Update", "update", accent=True)
        c = self.colour
        mono = self.mono = QLabel(re.sub(r"[^A-Za-z0-9]", "", name)[:2].upper() or "?")
        mono.setFixedSize(34, 34)
        mono.setAlignment(Qt.AlignmentFlag.AlignCenter)
        T.bind_css(mono, lambda: f"background: rgba({c.red()}, {c.green()}, {c.blue()}, 30); color: {c.name()}; "
                           f"border: 1px solid rgba({c.red()}, {c.green()}, {c.blue()}, 90); border-radius: 9px; font-weight: 700; font-size: 9pt;")
        lay.addWidget(mono)
        lay.addLayout(names, 1)
        lay.addWidget(self.launch, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.update_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._render()

    def _tool_button(self, glyph: str, verb: str, kind: str, accent: bool) -> QPushButton:
        b = QPushButton()
        b.setIcon(icons.icon(glyph, "#ffffff" if accent else T.fg, 14))
        b.setIconSize(QSize(14, 14))
        b.setFixedSize(30, 28)
        b.setStyleSheet("padding: 0;")  # the global button padding would crowd the icon out
        b.setToolTip(verb)
        b.setAccessibleName(f"{verb} {self._name}")
        if accent:
            b.setProperty("accent", True)
        b.clicked.connect(lambda _c=False, k=kind: self.action.emit(self.key, k))
        return b

    def _render(self) -> None:
        line = f'<span style="color: {self._colour.name()}; font-weight: 600;">{html.escape(self._status)}</span>'
        if self._system:
            line += f"&nbsp;&nbsp;·&nbsp;&nbsp;{html.escape(self._system)}"  # last, so it is the part that gets clipped
        self.sub.setText(line)

    def apply(self, *, text: str, colour: QColor, facts: str, can_launch: bool, can_update: bool, busy: bool, tip: str = "") -> None:
        self._status, self._colour = text, colour
        self._render()
        self.launch.setVisible(can_launch)
        self.update_btn.setVisible(can_update)
        self.launch.setEnabled(not busy)
        self.update_btn.setEnabled(not busy)
        head = "  ·  ".join(part for part in (self._system, facts) if part)
        self.setToolTip("\n\n".join(part for part in (head, tip) if part))

    def set_busy_text(self, text: str) -> None:
        self._status = text[:28]
        self._render()

    def set_logo(self, path: str) -> None:
        """Swap the monogram for the project's picture (left as it is when the file can't be read)."""
        if (px := logo_pixmap(path, self.colour, 34)) is not None:
            self.mono.setText("")
            T.bind_css(self.mono, lambda: "background: transparent; border: none;")  # replaces the monogram's theme-bound style (see LogoTile.show_logo)
            self.mono.setPixmap(px)


class ForkRow(QFrame):
    """One fork inside its emulator's page: name, what it is for, who made it, its status, and the buttons that apply."""

    action = Signal(str, str)  # fork id, "install" | "update" | "launch" | "cancel" | "details"

    def __init__(self, rid: str, name: str, owner: str, description: str, trust: str):
        super().__init__()
        self.key = rid
        self.setObjectName("forkRow")
        T.bind_css(self, lambda: f"QFrame#forkRow {{ background: {T.input.name()}; border: 1px solid {T.card_border.name()}; border-radius: 11px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 11, 12, 11)
        lay.setSpacing(12)
        col = QVBoxLayout()
        col.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(_label(name, f"color: {T.title.name()}; font-weight: 600;"))
        self.pill = QLabel()
        head.addWidget(self.pill)
        head.addStretch(1)
        col.addLayout(head)
        if description:
            col.addWidget(_label(description, f"color: {T.fg.name()};", wrap=True))
        col.addWidget(_label(f"by {owner}" + (f"  ·  {trust}" if trust else ""), f"color: {T.fg3.name()}; font-size: 9pt;", wrap=True))
        lay.addLayout(col, 1)
        self.buttons: dict[str, QPushButton] = {}
        for kind, text in (("launch", "Launch"), ("install", "Install"), ("update", "Update"), ("cancel", "Cancel"), ("details", "Details")):
            b = QPushButton(text)
            b.setFixedHeight(30)
            if kind in ("install", "update"):
                b.setProperty("accent", True)
            b.clicked.connect(lambda _c=False, k=kind: self.action.emit(self.key, k))
            self.buttons[kind] = b
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)

    def apply(self, *, text: str, colour: QColor, show: set[str], enabled: set[str], tip: str = "") -> None:
        self.pill.setText(text)
        T.bind_css(self.pill, lambda: pill_css(colour))
        for kind, b in self.buttons.items():
            b.setVisible(kind in show)
            b.setEnabled(kind in enabled)
        self.setToolTip(tip)

    def set_busy_text(self, text: str) -> None:
        self.pill.setText(text[:28])


class StatTile(Clickable):
    """A count with a caption ('3 installed'). Pressing it applies the matching filter."""

    def __init__(self, key: str, caption: str):
        super().__init__(key, "statTile")
        self.setFixedHeight(66)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(12)
        self.number = _label("0", f"color: {T.title.name()}; font-family: \"{T.display_font}\"; font-size: 20pt; font-weight: 700;")
        self.caption = _shrinkable(_label(caption, f"color: {T.fg3.name()};"))
        lay.addWidget(self.number)
        lay.addWidget(self.caption, 1)

    def set_value(self, n: int, hot: bool = False) -> None:
        T.bind_css(self.number, lambda: f"color: {(T.warn if hot else T.title).name()}; font-family: \"{T.display_font}\"; font-size: 20pt; font-weight: 700;")
        self.number.setText(f"{n:,}")
