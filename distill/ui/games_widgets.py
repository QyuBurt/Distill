"""The Games page's own widgets: the poster and platform delegates, the grid, a game's backdrop, the stat tiles, the pills, the selection bar and
the empty-library screen. No page logic here: the page itself (`GamesMixin`) is in games_page.py, the engine in distill/games.py."""
from __future__ import annotations

import math
import time
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QStyle,
                               QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget)

from .. import games as gm
from . import fillmath, icons, motion
from .emu_cards import SourceChip, clear_layout
from .recomp_page import CELL_W, ID_ROLE, POSTER_H, POSTER_W, art_rect, rounded_pixmap, star_polygon
from .assets import asset
from .theme import AlignC, T
from .widgets import accent_button, BackdropView  # noqa: F401  (BackdropView lives in widgets.py now; the Recomp page uses it too)

CELL_H = 322  # a poster, then the title and the system under it
DOT = 20  # the select dot in a poster's top-left corner
EDIT_BTN = 30  # the pencil in its bottom-right corner (shown while the pointer is over the poster)
PILL_COLOURS = {"all": QColor("#53b1cf"), "fav": QColor("#ff6b81"), "recent": QColor("#a78bfa"), "new": QColor("#4ade80"), "installed": QColor("#fbbf24")}  # All games, Favourites, Recently played, Recently added, Installed


def _when(epoch: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(epoch)) if epoch > 0 else ""


ZOOM_STEPS = (70, 85, 100, 115, 130)  # the poster sizes the slider offers, in percent


def cell_metrics(zoom: float) -> tuple[int, int, float, float]:
    """(cell width, cell height, poster width, poster height) at a poster size (1.0 = normal): the space round a poster stays the same."""
    pw, ph = POSTER_W * zoom, POSTER_H * zoom
    return round(CELL_W - POSTER_W + pw), round(CELL_H - POSTER_H + ph), pw, ph


def poster_rect(cell: QRect, pix: QPixmap | None, zoom: float = 1.0) -> QRectF:
    """Where a game's cover is inside its grid cell (the slot is centred in the cell, the cover rests on its bottom edge)."""
    _cw, _ch, pw, ph = cell_metrics(zoom)
    slot = QRectF(cell.x() + (cell.width() - pw) / 2, cell.y() + 6, pw, ph)
    return art_rect(slot, pix)


def dot_rect(poster: QRectF) -> QRectF:
    """The select dot, in the poster's top-right corner (the rating is top-left, as on the Recomp posters)."""
    return QRectF(poster.right() - 9 - DOT, poster.y() + 9, DOT, DOT)


RATE_STAR, RATE_GAP, RATE_PAD = 16, 3, 10  # a star in the rating when it opens out to five, the space between them, the room at each end
RATE_FULL = 2 * RATE_PAD + gm.MAX_RATING * RATE_STAR + (gm.MAX_RATING - 1) * RATE_GAP  # the open rating's width
RATE_EASE_MS = 240  # how long the one star takes to become five (and back)


def rating_rect(poster: QRectF, rated: bool, t: float = 0.0) -> QRectF:
    """The rating in the poster's top-left corner: a small star (and the number once rated) at t = 0, five stars at t = 1."""
    closed = 40.0 if rated else 26.0
    return QRectF(poster.x() + 8, poster.y() + 8, closed + (RATE_FULL - closed) * max(0.0, min(1.0, t)), 20)


def rating_star_at(poster: QRectF, x: float) -> int:
    """Which of the five stars (1 to 5) an x position is over in the open rating."""
    return max(1, min(gm.MAX_RATING, int((x - (poster.x() + 8 + RATE_PAD)) // (RATE_STAR + RATE_GAP)) + 1))


SHOT_W, SHOT_H = 236, 133  # a screenshot tile (16:9)
BADGE = 64  # an achievement's rounded square
ROMM_COLOUR = "#8B74E8"  # the RomM chip (a game from the player's RomM server): RomM's own violet-blue, a shade apart from the Recently played pill
RA_COLOUR = "#DDB13B"  # RetroAchievements' chip: a gold that sits well with the accent on both themes
PLAY_BTN = 54  # the play button that appears in the middle of a poster under the pointer


def play_rect(poster: QRectF) -> QRectF:
    return QRectF(poster.center().x() - PLAY_BTN / 2, poster.center().y() - PLAY_BTN / 2, PLAY_BTN, PLAY_BTN)


def edit_rect(poster: QRectF) -> QRectF:
    return QRectF(poster.right() - EDIT_BTN - 9, poster.bottom() - EDIT_BTN - 9, EDIT_BTN, EDIT_BTN)


def _tint(c: QColor, alpha: int) -> str:
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


class GameDelegate(QStyledItemDelegate):
    """One game in the grid: its cover (or a quiet card with its title), the select dot top-left, a star top-right if it is a favourite, the
    pencil bottom-right while the pointer is over it, and the title and the platform under it. Selected games get an accent outline."""

    def __init__(self, window):
        super().__init__(window)
        self.win = window

    def sizeHint(self, option, index) -> QSize:
        cw, ch, _pw, _ph = cell_metrics(self.win._gm_zoom)
        return QSize(cw, ch)

    def paint(self, p: QPainter, option, index) -> None:
        gid = index.data(ID_ROLE)
        g = self.win._gm_by_id.get(gid)
        if g is None:
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(0.0 if gid == self.win._gm_hold_id else 1.0)  # a cover still in the air towards this cell leaves its place empty until it lands
        cell = option.rect
        _cw, _ch, pw, ph = cell_metrics(self.win._gm_zoom)
        slot = QRectF(cell.x() + (cell.width() - pw) / 2, cell.y() + 6, pw, ph)
        pix = self.win._gm_pixmap(g)
        poster = art_rect(slot, pix)
        clip = QPainterPath()
        clip.addRoundedRect(poster, 12, 12)
        selected = gid in self.win._gm_selected
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.save()
        p.setClipPath(clip)
        if pix is not None:
            p.drawPixmap(poster, pix, QRectF(pix.rect()))
        else:
            p.fillRect(poster, T.input)
            p.setPen(T.fg3)
            f = QFont(p.font())
            f.setBold(True)
            f.setPointSizeF((f.pointSizeF() if f.pointSizeF() > 0 else 10.0) + 1)
            p.setFont(f)
            p.drawText(poster.adjusted(12, 12, -12, -12), int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap), g.name)
        if selected:
            wash = QColor(T.accent)
            wash.setAlpha(36)
            p.fillRect(poster, wash)
        missing = gid in self.win._gm_missing
        if missing:
            p.fillRect(poster, QColor(0, 0, 0, 140))
        p.restore()
        p.setPen(QPen(T.accent if hovered or selected else T.card_border, 3 if selected else 2 if hovered else 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(clip)
        # the select dot: a quiet ring, filled with a tick once the game is selected
        dot = dot_rect(poster)
        p.setPen(QPen(QColor(255, 255, 255, 215 if selected or hovered else 150), 1.6))
        p.setBrush(T.accent if selected else QColor(0, 0, 0, 150))
        p.drawEllipse(dot)
        if selected:
            p.drawPixmap(int(dot.center().x() - 6), int(dot.center().y() - 6), icons.pixmap("check", QColor("#ffffff"), 12, stroke=2.6))
        if g.favorite:  # beside the select dot, which is in the top-right corner
            box = QRectF(dot.x() - 6 - 26, poster.y() + 8, 26, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 190))
            p.drawRoundedRect(box, 10, 10)
            p.setBrush(QColor(255, 196, 0))
            p.drawPolygon(star_polygon(box.center().x(), box.center().y() + 0.5, 6.5))
        self._paint_rating(p, option, poster, g, gid)
        versions = len(self.win._gm_groups.get(gid, ()))
        lift = 0
        if g.source:  # a game from the player's RomM server wears its chip bottom-left, and a bar fills along the bottom while it installs
            f = QFont(option.font)
            f.setPointSizeF(8.5)
            f.setBold(True)
            p.setFont(f)
            chip = QRectF(poster.x() + 9, poster.bottom() - 29, p.fontMetrics().horizontalAdvance(gm.SOURCES.get(g.source, g.source).upper()) + 16, 20)
            colour = QColor(ROMM_COLOUR)
            p.setPen(QPen(QColor(colour.red(), colour.green(), colour.blue(), 200), 1))
            p.setBrush(QColor(14, 14, 20, 215))
            p.drawRoundedRect(chip, 10, 10)
            p.setPen(colour.lighter(125))
            p.drawText(chip, AlignC, gm.SOURCES.get(g.source, g.source).upper())
            lift = 26
            frac = self.win._gm_installs.get(gid)
            if frac is not None:
                track = QRectF(poster.x() + 6, poster.bottom() - 7, poster.width() - 12, 4)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 170))
                p.drawRoundedRect(track, 2, 2)
                p.setBrush(colour)
                p.drawRoundedRect(QRectF(track.x(), track.y(), max(4.0, track.width() * frac), 4), 2, 2)
        if versions > 1 or missing or g.discs > 1:
            text = "File missing" if missing else f"{versions} versions" if versions > 1 else f"{g.discs} discs"
            f = QFont(option.font)
            f.setPointSizeF(8.5)
            f.setBold(True)
            p.setFont(f)
            tag = QRectF(poster.x() + 9, poster.bottom() - 29 - lift, p.fontMetrics().horizontalAdvance(text) + 16, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 200))
            p.drawRoundedRect(tag, 10, 10)
            p.setPen(T.warn if missing else QColor("#ffffff"))
            p.drawText(tag, AlignC, text)
        if hovered and not self.win._gm_selected:  # (while games are selected a click selects, so there is nothing to play with it)
            big = play_rect(poster)
            glow = QColor(T.accent)
            glow.setAlpha(235)
            p.setPen(QPen(QColor(255, 255, 255, 90), 1.5))
            p.setBrush(glow)
            p.drawEllipse(big)
            p.drawPixmap(int(big.center().x() - 12 + 1.5), int(big.center().y() - 12), icons.pixmap("play", QColor("#ffffff"), 24))
        if hovered:
            btn = edit_rect(poster)
            p.setPen(QPen(QColor(255, 255, 255, 70), 1))
            p.setBrush(QColor(14, 14, 20, 205))
            p.drawEllipse(btn)
            p.drawPixmap(int(btn.center().x() - 8), int(btn.center().y() - 8), icons.pixmap("edit", QColor("#ffffff"), 16))
        name_font = QFont(option.font)
        name_font.setBold(True)
        p.setFont(name_font)
        p.setPen(T.title)
        name_rect = QRectF(slot.x(), slot.bottom() + 8, pw, 20)
        p.drawText(name_rect, AlignC, p.fontMetrics().elidedText(g.name, Qt.TextElideMode.ElideRight, int(pw)))
        sub = QFont(option.font)
        sub.setPointSizeF(9.0)
        p.setFont(sub)
        p.setPen(T.fg3)
        line = g.system + (f"  ·  {g.region}" if g.region else "")
        p.drawText(QRectF(slot.x(), name_rect.bottom() + 1, pw, 18), AlignC, p.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, int(pw)))
        if g.last_played > 0:  # when it was last started, in words
            ago = QFont(sub)
            ago.setPointSizeF(8.5)
            p.setFont(ago)
            p.setPen(T.fg3)
            p.drawText(QRectF(slot.x(), name_rect.bottom() + 19, pw, 16), AlignC, f"Played {gm.fmt_ago(g.last_played)}")
        p.restore()

    def _paint_rating(self, p: QPainter, option, poster: QRectF, g, gid) -> None:
        """The player's rating in the top-left corner, as on a recomp's poster: an empty star until the game is rated, then a small filled star and
        the number. With the pointer on it the one star eases out into five that light up under the pointer (the grid keeps the animation)."""
        t, hover = self.win.gm_list.rate_state(gid)
        box = rating_rect(poster, bool(g.rating), t)
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 190))
        p.drawRoundedRect(box, 10, 10)
        gold = QColor(255, 196, 0)
        if t < 0.5:  # the closed rating fades out as the box opens...
            p.setOpacity(1.0 - 2.0 * t)
            if g.rating:
                f = QFont(option.font)
                f.setPointSizeF(9.0)
                f.setBold(True)
                p.setFont(f)
                p.setBrush(gold)
                p.drawPolygon(star_polygon(box.x() + 13, box.center().y() + 0.5, 6.5))
                p.setPen(QColor(255, 255, 255))
                p.drawText(QRectF(box.x() + 22, box.y(), 14, box.height()), AlignC, str(g.rating))
            else:
                p.setPen(QPen(QColor(255, 255, 255, 190), 1.3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPolygon(star_polygon(box.center().x(), box.center().y() + 0.5, 5.8))
        else:  # ... and the five stars fade in
            p.setOpacity(2.0 * t - 1.0)
            shown = hover or g.rating
            for i in range(gm.MAX_RATING):
                on = i < shown
                p.setPen(QPen(gold if on else QColor(255, 255, 255, 170), 1.3))
                p.setBrush(gold if on else Qt.BrushStyle.NoBrush)
                p.drawPolygon(star_polygon(box.x() + RATE_PAD + RATE_STAR / 2 + i * (RATE_STAR + RATE_GAP), box.center().y() + 0.5, RATE_STAR / 2 - 2))
        p.restore()


COUNT_ROLE = Qt.ItemDataRole.UserRole + 7  # a rail row's game count
HEALTH_ROLE = Qt.ItemDataRole.UserRole + 8  # a platform row's setup health: 'ok', 'warn' or 'bad' (a dot is drawn for the last two)
LOGO_BOX = QSize(112, 30)  # the room a platform's logo gets in the sidebar
LOGO_NAMES = {"PC Engine": "NEC PC Engine", "Mega Drive": "Sega Mega Drive", "Switch": "Nintendo Switch", "PlayStation 4": "Sony PlayStation 4", "PlayStation 5": "Sony PlayStation 5",
              "PC": "Windows"}  # the logo to use where LaunchBox's name differs
_logos: dict[tuple, QPixmap | None] = {}


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-").replace("--", "-")


def platform_logo(system: str, dark: bool, dpr: float = 2.0, box: QSize = LOGO_BOX) -> QPixmap | None:
    """The bundled logo for a platform, fitted to `box` (LOGO_BOX: the sidebar's): the light-coloured set on the dark theme, the dark-coloured
    set on the light one. None when there is no logo for it (the sidebar then shows the platform's name)."""
    key = (system, dark, dpr, box.width(), box.height())
    if key in _logos:
        return _logos[key]
    pix = None
    folder = "on-dark" if dark else "on-light"
    for name in (LOGO_NAMES.get(system), gm.LB_PLATFORM_NAMES.get(system), system):
        if not name:
            continue
        path = asset("platforms", folder, _slug(name) + ".png")
        if path.exists():
            src = QPixmap(str(path))
            if not src.isNull():
                pix = src.scaled(int(box.width() * dpr), int(box.height() * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
                pix.setDevicePixelRatio(dpr)
                break
    _logos[key] = pix
    return pix


class RailDelegate(QStyledItemDelegate):
    """A platform in the sidebar: its logo on the left and its game count on the right (the name in text where there is no logo)."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), 28 if index.data(COUNT_ROLE) is None else LOGO_BOX.height() + 18)

    def paint(self, p: QPainter, option, index) -> None:
        system, count = index.data(ID_ROLE), index.data(COUNT_ROLE)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(2, 1, -2, -1)
        if count is None:  # the "PLATFORMS" heading
            p.setFont(index.data(Qt.ItemDataRole.FontRole) or option.font)
            p.setPen(T.fg3)
            p.drawText(r.adjusted(10, 0, 0, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), str(index.data(Qt.ItemDataRole.DisplayRole)))
            p.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.select if selected else T.hover)
            p.drawRoundedRect(r, 9, 9)
        num_w = 30
        if index.data(HEALTH_ROLE) in ("warn", "bad"):  # the platform's setup needs attention (right-click > Platform setup)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.bad if index.data(HEALTH_ROLE) == "bad" else T.warn)
            p.drawEllipse(QPointF(r.x() + 15, r.center().y()), 4.5, 4.5)
        mid = QRectF(r.x() + 36, r.y(), r.width() - 72, r.height())  # the same room either side: what is in it is centred in the row
        is_collection = str(system).startswith(("@c:", "@s:"))
        pix = None if is_collection else platform_logo(str(system), T.dark)
        name = str(index.data(Qt.ItemDataRole.DisplayRole))
        bold = QFont(option.font)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(T.title if selected else T.fg2)
        if is_collection:  # a small list icon and the name, together in the middle
            text = p.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, int(mid.width() - 24))
            total = 24 + p.fontMetrics().horizontalAdvance(text)
            x0 = r.center().x() - total / 2
            p.drawPixmap(int(x0), int(r.center().y() - 8), icons.pixmap("list", T.title if selected else T.fg2, 16))
            p.drawText(QRectF(x0 + 24, r.y(), total - 24 + 2, r.height()), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        elif pix is not None:
            w, h = pix.width() / pix.devicePixelRatio(), pix.height() / pix.devicePixelRatio()
            p.drawPixmap(QRectF(r.center().x() - w / 2, r.center().y() - h / 2, w, h), pix, QRectF(pix.rect()))
        else:
            p.drawText(mid, int(Qt.AlignmentFlag.AlignCenter), p.fontMetrics().elidedText(str(system), Qt.TextElideMode.ElideRight, int(mid.width())))
        f = QFont(option.font)
        f.setPointSizeF(9.0)
        p.setFont(f)
        p.setPen(T.fg3)
        p.drawText(QRectF(r.right() - 8 - num_w, r.y(), num_w, r.height()), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), str(count))
        p.restore()


class GameGrid(QListWidget):
    """The posters. Pressing a poster's dot selects it, pressing its pencil edits it (neither opens the game); Esc clears the selection, Delete removes it and
    Ctrl+A selects everything shown; right-click opens a menu for the game."""

    def __init__(self, window):
        super().__init__()
        self.win = window
        self._swallow = False  # a press on the dot or the pencil: its release is not a click on the game
        self._rate_gid, self._rate_t, self._rate_hover, self._rate_open = "", 0.0, 0, False  # the poster whose rating is open (or closing)
        self._rate_pos = QPoint()  # a point inside that poster's cell, to find it again while it animates
        self._rate_anim = QVariantAnimation(self)
        self._rate_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._rate_anim.valueChanged.connect(self._rate_step)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._rate_snap())
        self.setMouseTracking(True)
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)  # selection is the page's own (`_gm_selected`), drawn by the delegate

    # ---- the rating opening out to five stars ------------------------------------------------------------------
    def rate_state(self, gid: str) -> tuple[float, int]:
        """(how far open the rating is, 0 to 1; the star under the pointer, 0 for none) for one poster."""
        return (self._rate_t, self._rate_hover) if gid == self._rate_gid else (0.0, 0)

    def _rate_repaint(self) -> None:
        item = self.itemAt(self._rate_pos)
        self.viewport().update(self.visualItemRect(item).adjusted(-4, -4, 4, 4) if item is not None else self.viewport().rect())

    def _rate_step(self, value) -> None:
        self._rate_t = float(value)
        self._rate_repaint()

    def _rate_to(self, target: float) -> None:
        self._rate_anim.stop()
        if abs(target - self._rate_t) < 0.001:
            self._rate_t = target
            return
        self._rate_anim.setDuration(max(60, int(RATE_EASE_MS * abs(target - self._rate_t))))
        self._rate_anim.setStartValue(self._rate_t)
        self._rate_anim.setEndValue(target)
        self._rate_anim.start()

    def _rate_snap(self) -> None:
        """Close the rating at once (the grid scrolled, or the pointer left it)."""
        self._rate_anim.stop()
        was = self._rate_t > 0 or self._rate_hover
        self._rate_open, self._rate_t, self._rate_hover = False, 0.0, 0
        if was:
            self.viewport().update()

    def _rate_track(self, pos) -> bool:
        """Follow the pointer over the rating corner: open it when the pointer is on it, light the star under the pointer, close it when the
        pointer leaves. True while the pointer is on it."""
        item = self.itemAt(pos)
        g = self.win._gm_by_id.get(str(item.data(ID_ROLE))) if item is not None else None
        inside, gid, poster = False, "", QRectF()
        if g is not None:
            gid = g.id
            poster = poster_rect(self.visualItemRect(item), self.win._gm_art.get(gid), self.win._gm_zoom)
            same = gid == self._rate_gid and self._rate_open
            inside = rating_rect(poster, bool(g.rating), 1.0 if same else 0.0).adjusted(-3, -3, 3, 3).contains(QPointF(pos))
        if inside:
            if gid != self._rate_gid:
                self._rate_anim.stop()
                self._rate_gid, self._rate_t, self._rate_hover = gid, 0.0, 0
            self._rate_pos = QPoint(pos)
            if not self._rate_open:
                self._rate_open = True
                self._rate_to(1.0)
            hover = rating_star_at(poster, pos.x()) if self._rate_t >= 0.5 else 0
            if hover != self._rate_hover:
                self._rate_hover = hover
                self._rate_repaint()
        elif self._rate_open:
            self._rate_open, self._rate_hover = False, 0
            self._rate_to(0.0)
            self._rate_repaint()
        return inside

    def _rate_set(self, gid: str, stars: int) -> None:
        self.win._gm_save_mine(gid, rating=stars)
        self.viewport().update()

    def leaveEvent(self, event) -> None:
        self._rate_snap()
        super().leaveEvent(event)

    def _hit(self, pos) -> tuple[str, str]:
        item = self.itemAt(pos)
        if item is None:
            return "", ""
        gid = str(item.data(ID_ROLE))
        poster = poster_rect(self.visualItemRect(item), self.win._gm_art.get(gid), self.win._gm_zoom)
        at = QPointF(pos)
        g = self.win._gm_by_id.get(gid)
        if g is not None and rating_rect(poster, bool(g.rating), self._rate_t if gid == self._rate_gid else 0.0).adjusted(-3, -3, 3, 3).contains(at):
            return gid, "rate"
        if not self.win._gm_selected and play_rect(poster).contains(at):
            return gid, "play"
        if dot_rect(poster).adjusted(-5, -5, 5, 5).contains(at):
            return gid, "dot"
        if edit_rect(poster).adjusted(-3, -3, 3, 3).contains(at):
            return gid, "edit"
        return gid, ""

    def mousePressEvent(self, event) -> None:
        self._swallow = False
        if event.button() == Qt.MouseButton.LeftButton:
            gid, what = self._hit(event.position().toPoint())
            if what == "rate":
                self._swallow = True
                g = self.win._gm_by_id.get(gid)
                if g is not None and gid == self._rate_gid and self._rate_t >= 0.5:  # (a click while it is still one star opens it, it doesn't rate)
                    star = rating_star_at(poster_rect(self.visualItemRect(self.itemAt(event.position().toPoint())), self.win._gm_art.get(gid), self.win._gm_zoom),
                                          event.position().x())
                    stars = 0 if star == g.rating else star  # the same star again clears the rating, as on the game's page
                    QTimer.singleShot(0, lambda: self._rate_set(gid, stars))
                event.accept()
                return
            if what:
                self._swallow = True
                action = {"dot": self.win._gm_toggle_select, "play": self.win._gm_quick_play}.get(what, self.win._gm_edit)
                QTimer.singleShot(0, lambda: action(gid))  # after the press has finished: the edit window is modal
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._swallow:
            self._swallow = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        on_rating = self._rate_track(pos)
        _gid, what = self._hit(pos)
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if what or on_rating else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self.win._gm_selected:
            self.win._gm_clear_selection()
        elif event.key() == Qt.Key.Key_Delete and self.win._gm_selected:
            self.win._gm_bulk_remove()
        elif event.key() == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.win._gm_select_all()
        else:
            super().keyPressEvent(event)


class HStrip(QScrollArea):
    """A row of tiles that scrolls sideways (the mouse wheel scrolls it too)."""

    def __init__(self, tile_height: int):
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(tile_height + 18)  # room for the scroll bar under the tiles
        self.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.viewport().setAutoFillBackground(False)
        host = QWidget()
        host.setObjectName("stripHost")
        host.setStyleSheet("#stripHost { background: transparent; }")
        self._lay = QHBoxLayout(host)
        self._lay.setContentsMargins(0, 0, 0, 6)
        self._lay.setSpacing(10)
        self.setWidget(host)

    def set_tiles(self, tiles: list[QWidget]) -> None:
        clear_layout(self._lay)
        for t in tiles:
            self._lay.addWidget(t, 0, Qt.AlignmentFlag.AlignTop)
        self._lay.addStretch(1)

    def wheelEvent(self, event) -> None:
        bar = self.horizontalScrollBar()
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar.setValue(bar.value() - delta)
        event.accept()


def _tile_pixmap(path: Path, w: int, h: int, radius: int, dim: bool = False) -> QPixmap | None:
    src = QPixmap(str(path))
    if src.isNull():
        return None
    dpr = 2.0
    pix = src.scaled(int(w * dpr), int(h * dpr), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    pix = pix.copy((pix.width() - int(w * dpr)) // 2, (pix.height() - int(h * dpr)) // 2, int(w * dpr), int(h * dpr))
    pix.setDevicePixelRatio(dpr)
    if dim:
        faded = QPixmap(pix.size())
        faded.setDevicePixelRatio(dpr)
        faded.fill(Qt.GlobalColor.transparent)
        q = QPainter(faded)
        q.setOpacity(0.5)
        q.drawPixmap(0, 0, pix)
        q.end()
        pix = faded
    return rounded_pixmap(pix, radius)


class LinkChip(SourceChip):
    """A source chip that opens the source's page when it is clicked."""

    def __init__(self, text: str, tip: str, colour: str, url: str = ""):
        super().__init__(text, tip, colour)
        self._url = url
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_url(self, url: str, tip: str = "") -> None:
        self._url = url
        if tip:
            self.setToolTip(tip)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._url and self.rect().contains(event.position().toPoint()):
            QDesktopServices.openUrl(QUrl(self._url))
        super().mouseReleaseEvent(event)


class PlatformLogo(QWidget):
    """A platform's logo, right-aligned in its box: at the top right of a game's card. Nothing is drawn for a platform that has no logo
    (`has_logo` says which, so the card can hide it)."""
    BOX = QSize(132, 38)

    def __init__(self):
        super().__init__()
        self.setFixedSize(self.BOX)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._system = ""

    def set_system(self, system: str) -> bool:
        self._system = system
        self.update()
        return self.has_logo()

    def has_logo(self) -> bool:
        return bool(self._system) and platform_logo(self._system, T.dark, box=self.BOX) is not None

    def paintEvent(self, _event) -> None:
        pix = platform_logo(self._system, T.dark, box=self.BOX) if self._system else None
        if pix is None:
            return
        dpr = pix.devicePixelRatio() or 1.0
        w, h = pix.width() / dpr, pix.height() / dpr
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(QRectF(self.width() - w, (self.height() - h) / 2, w, h), pix, QRectF(pix.rect()))


class ElideLabel(QLabel):
    """A one-line label that cuts its text short with '...' instead of making its row wider."""

    def __init__(self):
        super().__init__()
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self._elide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(10, self.width())))


class StatTile(QFrame):
    """One small rounded tile on a game's card: an icon, a caption and a single value ('-' in a quiet colour when there is nothing to show)."""

    def __init__(self, icon: str, caption: str):
        super().__init__()
        self.setObjectName("statTile")
        self._icon, self._empty = icon, True
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 10)
        lay.setSpacing(10)
        self.glyph = QLabel()
        self.glyph.setFixedSize(20, 20)
        lay.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.caption = QLabel(caption)
        self.value = ElideLabel()
        self.value.set_full_text("-")
        col.addWidget(self.caption)
        col.addWidget(self.value)
        lay.addLayout(col, 1)
        T.bind(self, self._restyle)

    def set_value(self, text: str, tip: str = "") -> None:
        self._empty = not text
        self.value.set_full_text(text or "-")
        self.setToolTip(tip or text)
        self._restyle()

    def _restyle(self) -> None:
        self.setStyleSheet(f"QFrame#statTile {{ background: {T.input.name()}; border: 1px solid {T.card_border.name()}; border-radius: 12px; }}"
                           "QLabel { background: transparent; border: none; }")
        self.glyph.setPixmap(icons.pixmap(self._icon, T.accent_text, 18))
        self.caption.setStyleSheet(f"color: {T.fg3.name()}; font-size: 9pt; font-weight: 600;")
        self.value.setStyleSheet(f"color: {(T.fg3 if self._empty else T.title).name()}; font-size: 10.5pt; font-weight: 700;")


class StarRating(QWidget):
    """Five stars the player clicks to rate a game. Clicking the star that is already the rating clears it. `changed` is sent with the new
    number of stars (0 = none) only when the player clicks, never when `set_value` is called."""

    changed = Signal(int)
    STAR = 22  # one star's size, in pixels

    def __init__(self, value: int = 0, editable: bool = True):
        super().__init__()
        self._value, self._hover, self._editable = gm.clamp_rating(value), 0, editable
        self.setFixedSize(self.STAR * gm.MAX_RATING + 4 * 3, self.STAR + 4)
        self.setMouseTracking(True)
        if editable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Your rating. Click a star to rate this game; click the same star again to clear it.")
        T.bind(self, self.update)

    def value(self) -> int:
        return self._value

    def set_value(self, value: int) -> None:
        self._value = gm.clamp_rating(value)
        self.update()

    def _star_at(self, x: float) -> int:
        return max(1, min(gm.MAX_RATING, int(x // (self.STAR + 3) + 1)))

    def mouseMoveEvent(self, e) -> None:
        if self._editable:
            self._hover = self._star_at(e.position().x())
            self.update()

    def leaveEvent(self, _e) -> None:
        self._hover = 0
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self._editable and e.button() == Qt.MouseButton.LeftButton:
            star = self._star_at(e.position().x())
            self._value = 0 if star == self._value else star
            self.update()
            self.changed.emit(self._value)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        shown = self._hover or self._value
        for i in range(gm.MAX_RATING):
            on = i < shown
            p.setPen(QPen(T.warn if on else T.fg3, 1.4))
            p.setBrush(T.warn if on else Qt.BrushStyle.NoBrush)
            p.drawPolygon(star_polygon(2 + self.STAR / 2 + i * (self.STAR + 3), self.height() / 2 + 0.5, self.STAR / 2 - 1))
        p.end()


class NotesEdit(QPlainTextEdit):
    """The notes box on a game's page. `committed` is sent with the text shortly after typing stops, and at once when the box loses focus."""

    committed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setPlaceholderText("Your own notes: where you got to, tips, what to try next")
        self.setMinimumHeight(96)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(700)
        self._timer.timeout.connect(lambda: self.committed.emit(self.toPlainText()))
        self.textChanged.connect(self._timer.start)

    def set_text(self, text: str) -> None:
        """Show another game's notes (nothing is sent; call `flush` first if the last game's are still pending)."""
        self._timer.stop()
        if self.toPlainText() != text:
            self.blockSignals(True)
            self.setPlainText(text)
            self.blockSignals(False)

    def flush(self) -> None:
        """Send what was typed now, if it has not been sent yet."""
        if self._timer.isActive():
            self._timer.stop()
            self.committed.emit(self.toPlainText())

    def focusOutEvent(self, e) -> None:
        self.flush()
        super().focusOutEvent(e)


class Pill(QPushButton):
    """A rounded filter button: grey until it is chosen, then tinted with its own colour."""

    def __init__(self, text: str, colour: QColor):
        super().__init__(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(32)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        T.bind_css(self, lambda: (
            f"QPushButton {{ background: {T.input.name()}; color: {T.fg3.name()}; border: 1px solid {T.card_border.name()}; border-radius: 16px; "
            f"padding: 0 16px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {T.hover.name()}; color: {T.fg2.name()}; }}"
            f"QPushButton:checked {{ background: {_tint(colour, 44)}; color: {colour.name()}; border: 1px solid {_tint(colour, 190)}; }}"))


PAGE_MIN_H = 560  # the Games page is never shorter than this while there are games (the window scrolls instead); with none it fits the window


class LibraryLogo(QWidget):
    """The picture on the empty page: three posters fanned out, the one in front carrying a game pad."""

    def __init__(self, size: int = 120):
        super().__init__()
        self.setFixedSize(size, size)

    def set_side(self, size: int) -> None:
        if size != self.width():
            self.setFixedSize(size, size)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cw, ch = w * 0.46, h * 0.64
        for angle, dx, alpha in ((-13, -w * 0.20, 70), (13, w * 0.20, 70), (0, 0, 255)):
            p.save()
            p.translate(w / 2 + dx, h * 0.54)
            p.rotate(angle)
            card = QRectF(-cw / 2, -ch / 2, cw, ch)
            if alpha == 255:
                grad = QLinearGradient(card.topLeft(), card.bottomRight())
                grad.setColorAt(0.0, QColor("#5b9bd5"))
                grad.setColorAt(1.0, QColor("#9a5bd6"))
                p.setBrush(grad)
                p.setPen(Qt.PenStyle.NoPen)
            else:
                edge = QColor(T.accent)
                edge.setAlpha(120)
                fill = QColor(T.accent)
                fill.setAlpha(alpha // 3)
                p.setBrush(fill)
                p.setPen(QPen(edge, 1.4))
            p.drawRoundedRect(card, 9, 9)
            p.restore()
        glyph = icons.pixmap("gamepad", QColor("#ffffff"), int(w * 0.30))
        p.drawPixmap(int(w / 2 - w * 0.15), int(h * 0.54 - w * 0.15), glyph)


class EmptyLibrary(QWidget):
    """What the Games page shows with no games: no card, just the logo, a line and one button.

    Like the selection bar it floats over the window's visible area (the scroll area's viewport) rather than sitting in the page, so it
    is centred in what the window really shows however tall the page behind it is, and it never needs the page to scroll. When the window
    is short the logo and the line under the title shrink (then go), so the button is always in view."""

    TIERS = ((120, True), (84, True), (56, False), (0, False))  # (logo size, show the line under the title), biggest first

    def __init__(self, on_choose):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 24)  # the margin lifts the block a touch above true centre, which looks centred
        lay.setSpacing(10)
        lay.addStretch(1)
        self._logo = LibraryLogo()
        lay.addWidget(self._logo, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(8)
        title = QLabel("Build your library")
        T.bind_css(title, lambda: f"font-size: 20pt; font-weight: 700; color: {T.title.name()}; background: transparent;")
        lay.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)
        sub = QLabel("Pick the folder that holds your games. Distill finds them, works out each platform and adds covers.\nNothing in the folder is changed.")
        sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        sub.setWordWrap(True)
        T.bind_css(sub, lambda: f"color: {T.fg3.name()}; background: transparent;")
        self._title, self._sub = title, sub
        lay.addWidget(sub, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(10)
        pick = accent_button("Choose library folder...")
        pick.setMinimumHeight(40)
        pick.setMinimumWidth(230)
        pick.clicked.connect(on_choose)
        self.pick = pick
        lay.addWidget(pick, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(1)
        self._empty = False  # the library has no games
        self._page_on = False  # the grid's page is the one showing
        self.hide()

    def attach(self, host: QWidget) -> None:
        """Move onto the window's visible area (the scroll area's viewport), once, and keep covering it."""
        if self.parentWidget() is host:
            return
        self.setParent(host)
        host.installEventFilter(ResizeWatch(self, self.place))
        self._sync()

    def place(self) -> None:
        host = self.parentWidget()
        if host is not None:
            self.setGeometry(0, 0, host.width(), host.height())

    def set_empty(self, empty: bool) -> None:
        self._empty = empty
        self._sync()

    def page_shown(self, shown: bool) -> None:
        self._page_on = shown
        self._sync()

    def _sync(self) -> None:
        show = self._empty and self._page_on and self.parentWidget() is not None
        if show:
            self.place()
            self._fit()
        self.setVisible(show)
        if show:
            self.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        """Use the biggest logo and wording that fit in the height there is."""
        lay = self.layout()
        width = max(160, min(460, self.width() - 48))
        wrap = getattr(Qt.TextFlag.TextWordWrap, "value", Qt.TextFlag.TextWordWrap)
        text_h = self._sub.fontMetrics().boundingRect(QRect(0, 0, width, 10000), int(wrap), self._sub.text()).height()
        self._sub.setFixedSize(width, text_h + 4)  # worked out here: a wrapped label's own height-for-width comes out too short in a box this wide
        for side, with_line in self.TIERS:
            self._logo.setVisible(side > 0)
            if side:
                self._logo.set_side(side)
            self._sub.setVisible(with_line)
            lay.invalidate()
            if lay.totalSizeHint().height() <= self.height():
                break


class SelectionBar(QFrame):
    """The bar that slides up from the bottom of the grid while games are selected: add them to a platform, favourite them, remove them."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("gmBar")
        self.setFixedHeight(56)
        T.bind_css(self, lambda: f"QFrame#gmBar {{ background: {T.card.name()}; border: 1px solid {T.border.name()}; border-radius: 16px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 8, 10, 8)
        lay.setSpacing(8)
        self.count = QLabel()
        T.bind_css(self.count, lambda: f"color: {T.title.name()}; font-weight: 700; background: transparent;")
        self.all_btn = QPushButton("Select all")
        self.platform_btn = QPushButton("Add to")
        self.platform_btn.setToolTip("Add the selected games to a collection or move them to a platform.")
        self.fav_btn = QPushButton("Favourite")
        self.match_btn = QPushButton("Get info")
        self.match_btn.setToolTip("Look the selected games up in the LaunchBox Games Database and fill in their developer, publisher, year and genre where empty.")
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setToolTip("Take the selected games out of the library. Their files are not deleted.")
        self.close_btn = QToolButton()
        self.close_btn.setIcon(QIcon(icons.pixmap("close", T.fg2, 16)))
        self.close_btn.setToolTip("Clear the selection (Esc)")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setAutoRaise(True)
        for b in (self.all_btn, self.platform_btn, self.match_btn, self.fav_btn, self.remove_btn):
            b.setMinimumHeight(36)
        lay.addWidget(self.count)
        lay.addStretch(1)
        for b in (self.all_btn, self.platform_btn, self.match_btn, self.fav_btn, self.remove_btn, self.close_btn):
            lay.addWidget(b)
        self._open = False
        self._page_on = False
        self._follow: Follow | None = None
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._settled)
        self.hide()

    def attach(self, host: QWidget) -> None:
        """Move the bar onto the window's visible area (the scroll area's viewport), once, and keep it at that area's bottom."""
        if self.parentWidget() is host:
            return
        self.setParent(host)
        self._follow = Follow(self)
        host.installEventFilter(self._follow)
        self.place()
        if self._open and self._page_on:  # setParent hid it
            self.show()
            self.raise_()

    def page_shown(self, shown: bool) -> None:
        self._page_on = shown
        if shown and self._open:
            self.place()
            self.show()
            self.raise_()
        elif not shown:
            self._anim.stop()
            self.hide()

    def _spots(self) -> tuple[int, int, int]:
        """(width, x, y when shown); the bar rests just above the grid's bottom edge and hides just below it."""
        host = self.parentWidget()
        width = max(320, min(720, host.width() - 32))
        return width, (host.width() - width) // 2, host.height() - self.height() - 18

    def place(self) -> None:
        """Keep the bar where it belongs after the page has been resized."""
        width, x, shown = self._spots()
        self.setFixedWidth(width)
        if self._open and self._page_on and not self._anim.state() == QPropertyAnimation.State.Running:
            self.move(x, shown)

    def set_open(self, open_: bool) -> None:
        if open_ == self._open:
            self.place()
            return
        self._open = open_
        self._anim.stop()
        width, x, shown = self._spots()
        hidden = self.parentWidget().height() + 6
        self.setFixedWidth(width)
        if open_ and self._page_on:
            self.move(x, hidden if not self.isVisible() else self.y())
            self.show()
            self.raise_()
        end = QPoint(x, shown if open_ else hidden)
        if not motion.on() or not self._page_on:
            self.move(end)
            if not open_:
                self.hide()
            return
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(end)
        self._anim.start()

    def _settled(self) -> None:
        if not self._open:
            self.hide()


class Follow(QObject):
    """Keeps a bar at the bottom of the window's visible area: it listens for the area being resized."""

    def __init__(self, bar: SelectionBar):
        super().__init__(bar)
        self.bar = bar

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self.bar.place()
        return False


class ResizeWatch(QObject):
    """Calls `fn` whenever the widget it is installed on (as an event filter) changes size."""

    def __init__(self, parent: QObject, fn):
        super().__init__(parent)
        self._fn = fn

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self._fn()
        return False


class GridPage(QWidget):
    """The grid's page. The selection bar belongs to the visible area, not to this page (the page can be taller than the window), so it is only
    shown while this page is."""

    def __init__(self):
        super().__init__()
        self.bar: SelectionBar | None = None
        self.empty: EmptyLibrary | None = None  # the 'Build your library' block, which floats over the visible area while this page shows

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.bar is not None:
            self.bar.page_shown(True)
        if self.empty is not None:
            self.empty.page_shown(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self.bar is not None:
            self.bar.page_shown(False)
        if self.empty is not None:
            self.empty.page_shown(False)


class ButtonFill(QWidget):
    """Lies over a button while something installs and fills it from the left as the download comes in, with the same rippling edge as the
    emulator cards' fill. Clicks pass straight through it."""

    def __init__(self, button: QWidget):
        super().__init__(button)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._button = button
        self._frac = 0.0  # what is shown, 0..1
        self._goal = 0.0  # what it is moving to
        self._phase = 0.0
        self._on = False
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._step)
        self._ripple = QTimer(self)
        self._ripple.setInterval(40)
        self._ripple.timeout.connect(self._tick)
        self.hide()

    def begin(self) -> None:
        self._on, self._frac, self._goal = True, 0.0, 0.0
        self.setGeometry(self._button.rect())
        self.raise_()
        self.show()
        if motion.on():
            self._ripple.start()

    def progress(self, frac: float) -> None:
        if not self._on:
            return
        self._goal = max(self._goal, min(1.0, max(0.0, frac)))
        self._anim.stop()
        if not motion.on() or not self.isVisible():
            self._step(self._goal)
            return
        self._anim.setDuration(220)
        self._anim.setStartValue(self._frac)
        self._anim.setEndValue(self._goal)
        self._anim.start()

    def finish(self) -> None:
        self._on = False
        self._ripple.stop()
        self._anim.stop()
        self._frac = 0.0
        self.hide()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.setGeometry(self._button.rect())

    def _step(self, value) -> None:
        self._frac = float(value)
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.35) % (2 * math.pi)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        if not self._on or self._frac <= 0:
            return
        w, h = self.width(), self.height()
        edge = w * self._frac
        amp = fillmath.wave_amplitude(self._frac, min(9.0, w * 0.03)) if motion.on() else 0.0
        wave = lambda y: edge + amp * math.sin(2 * math.pi * y / max(1, h) + self._phase)  # noqa: E731
        water = QPainterPath()  # everything left of the rippling edge: the part that is in
        water.moveTo(0, 0)
        water.lineTo(wave(0), 0)
        for y in range(3, h + 3, 3):
            water.lineTo(wave(min(y, h)), min(y, h))
        water.lineTo(0, h)
        water.closeSubpath()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 8, 8)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setClipPath(clip.intersected(water))
        p.fillRect(self.rect(), QColor(255, 255, 255, 70))
        p.end()
