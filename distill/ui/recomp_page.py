"""PC Recomps page: a poster grid of native PC ports, a page per project with install / update / play, and a toggle that
shows only the projects the player added. All the work is in `distill.recomps`; this file lays it out."""
from __future__ import annotations

import html
import math
import threading
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QProgressBar, QPushButton, QStyledItemDelegate, QStyle, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from ..artqueue import ArtQueue
from .. import gamemedia as mm
from .. import recomps as rc
from ..systems import SYSTEM_NAMES
from . import flight, icons, motion, msgbox
from .emu_cards import LAUNCHBOX_COLOUR, Callout, chip_css, clear_layout, break_path, FactList, pill_css, section_header, SourceChip, tint, TwoColumn
from .motion import smooth_scroll
from .theme import AlignC, AlignL, AlignV, T
from .recomp_dialog import AddRecompDialog, CoverSearchDialog, read_picture
from .widgets import accent_button, AnimRadio, BackdropView, Card, detail_shell, ModalStack, hint

POSTER_W, POSTER_H = 176, 250  # the cover, 7:10
CELL_W, CELL_H = 196, 346
DETAIL_W, DETAIL_H = 220, 314
ID_ROLE, STATE_ROLE = Qt.ItemDataRole.UserRole, Qt.ItemDataRole.UserRole + 1
ART_CACHE_MAX = 400
ART_WORKERS = 6  # posters fetched at once, only for what is on screen; a rate-limit answer from SteamGridDB is waited out, never taken for a miss


def cover_pixmap(path: Path | None, w: int, h: int, dpr: float = 1.0) -> QPixmap | None:
    """The whole picture scaled to fit inside w x h, or None when it can't be read. Box art comes in every shape (N64 boxes are wider
    than tall), so it is never cropped - and never padded either: the picture keeps its own shape and whoever draws it sizes the
    poster to match (`art_rect` in the grid, the cover label on the page), so there are no grey bars beside a narrow cover."""
    if path is None:
        return None
    pix = QPixmap(str(path))
    if pix.isNull():
        return None
    pix = pix.scaled(int(w * dpr), int(h * dpr), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    pix.setDevicePixelRatio(dpr)
    return pix


def art_rect(slot: QRectF, pix: QPixmap | None) -> QRectF:
    """Where a cover goes inside its slot: as large as fits, in its own shape, centred across and resting on the bottom edge so the
    name under it stays close. With no picture (a placeholder card) it is the whole slot."""
    if pix is None:
        return slot
    dpr = pix.devicePixelRatio() or 1.0
    pw, ph = pix.width() / dpr, pix.height() / dpr
    if pw <= 0 or ph <= 0:
        return slot
    k = min(slot.width() / pw, slot.height() / ph)
    w, h = pw * k, ph * k
    return QRectF(slot.x() + (slot.width() - w) / 2, slot.bottom() - h, w, h)


def rounded_pixmap(pix: QPixmap, radius: int = 16) -> QPixmap:
    """The picture with round corners and a hairline edge, the way the grid draws its posters (the poster grid clips as it paints)."""
    dpr = pix.devicePixelRatio() or 1.0
    w, h = pix.width() / dpr, pix.height() / dpr
    out = QPixmap(pix.size())
    out.setDevicePixelRatio(dpr)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    p.setClipPath(clip)
    p.drawPixmap(QRectF(0, 0, w, h), pix, QRectF(pix.rect()))
    p.setClipping(False)
    p.setPen(QPen(T.card_border, 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    edge = QPainterPath()
    edge.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), radius, radius)
    p.drawPath(edge)
    p.end()
    return out


def star_polygon(cx: float, cy: float, outer: float) -> QPolygonF:
    """A five-pointed star centred on (cx, cy), one point straight up."""
    inner = outer * 0.45
    pts = []
    for i in range(10):
        r = outer if i % 2 == 0 else inner
        a = -math.pi / 2 + i * math.pi / 5
        pts.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
    return QPolygonF(pts)


class CoverFrame(QLabel):
    """The poster on a project's page. A small edit button shows in its bottom-right corner only while the pointer is over the poster."""

    editRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.edit_btn = QToolButton(self)
        self.edit_btn.setFixedSize(34, 34)
        self.edit_btn.setIconSize(QSize(18, 18))
        self.edit_btn.setIcon(QIcon(icons.pixmap("edit", QColor("#ffffff"), 18)))
        self.edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_btn.setToolTip("Change the cover")
        self.edit_btn.setStyleSheet("QToolButton { background: rgba(14, 14, 20, 175); border: 1px solid rgba(255, 255, 255, 60); border-radius: 17px; }"
                                    "QToolButton:hover { background: rgba(14, 14, 20, 230); border-color: rgba(255, 255, 255, 140); }")
        self.edit_btn.installEventFilter(self)
        self.edit_btn.clicked.connect(self.editRequested)
        self.edit_btn.hide()
        self._place()

    def _place(self) -> None:
        self.edit_btn.move(self.width() - self.edit_btn.width() - 10, self.height() - self.edit_btn.height() - 10)

    def _pointer_inside(self) -> bool:
        return self.rect().contains(self.mapFromGlobal(QCursor.pos()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place()

    def enterEvent(self, event) -> None:
        self._place()
        self.edit_btn.show()
        self.edit_btn.raise_()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._pointer_inside():  # moving onto the button itself is still being over the poster
            self.edit_btn.hide()
        super().leaveEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit_btn and event.type() == QEvent.Type.Leave and not self._pointer_inside():
            self.edit_btn.hide()
        return super().eventFilter(obj, event)


class _Fade(QWidget):
    """A soft fade into the card's colour along the bottom edge of cut-off text."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event) -> None:  # noqa: N802
        from PySide6.QtGui import QLinearGradient
        clear, solid = QColor(T.card), QColor(T.card)
        clear.setAlpha(0)
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0.0, clear)
        grad.setColorAt(1.0, solid)
        p = QPainter(self)
        p.fillRect(self.rect(), grad)
        p.end()


class ClampedText(QWidget):
    """Wrapped text that shows at most `limit` pixels of itself, fading out at the cut, until it is expanded. `overflows` is True when
    the text is taller than that, which is when the caller shows its Show more button."""

    def __init__(self, limit: int = 230):
        super().__init__()
        self._limit = limit
        self._expanded = False
        self._natural = 0
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._fade = _Fade(self)
        self._fade.hide()
        self._anim = QVariantAnimation(self, duration=240, easingCurve=QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(lambda v: self.setFixedHeight(int(v)))
        self.on_overflow = lambda _o: None
        self.setFixedHeight(0)

    @property
    def overflows(self) -> bool:
        return self._natural > self._limit + 12

    def setText(self, text: str) -> None:
        self.label.setText(text)
        self._expanded = False
        self._relayout(animate=False)

    def set_expanded(self, on: bool) -> None:
        self._expanded = on
        self._relayout(animate=True)

    def _target(self) -> int:
        return self._natural if self._expanded or not self.overflows else self._limit

    def _relayout(self, animate: bool) -> None:
        w = self.width()
        if w < 10:
            return
        self.label.setGeometry(0, 0, w, 10)
        self._natural = max(0, self.label.heightForWidth(w))
        self.label.setGeometry(0, 0, w, self._natural)
        target = self._target()
        self._anim.stop()
        if animate and motion.on() and self.isVisible() and self.height() != target:
            self._anim.setStartValue(self.height())
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self.setFixedHeight(target)
        self._place_fade()
        self.on_overflow(self.overflows)

    def _place_fade(self) -> None:
        show = self.overflows and not self._expanded
        self._fade.setGeometry(0, max(0, self._target() - 34), self.width(), 34)
        self._fade.setVisible(show)
        self._fade.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if event.oldSize().width() != self.width():
            self._relayout(animate=False)
        else:
            self._place_fade()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, lambda: self._relayout(animate=False))


class PosterDelegate(QStyledItemDelegate):
    """One game: its cover (or a coloured placeholder with the name), badges for what you own and have installed, and
    the project's name and game under it."""

    def __init__(self, window):
        super().__init__(window)
        self.win = window

    def sizeHint(self, option, index) -> QSize:
        return QSize(CELL_W, CELL_H)

    def paint(self, p: QPainter, option, index) -> None:
        rid = index.data(ID_ROLE)
        r = self.win._rc_by_id.get(rid)
        if r is None:
            return
        state = index.data(STATE_ROLE) or {}
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(0.0 if r.id == self.win._rc_hold_id else 1.0)  # a cover still in the air towards this cell leaves its place empty until it lands
        cell = option.rect
        slot = QRectF(cell.x() + (cell.width() - POSTER_W) / 2, cell.y() + 6, POSTER_W, POSTER_H)  # the room a poster has; the text lines up to it
        pix = self.win._rc_pixmap(r)
        poster = art_rect(slot, pix)  # the poster itself takes the cover's own shape, so nothing is padded to fill the slot
        clip = QPainterPath()
        clip.addRoundedRect(poster, 12, 12)
        p.save()
        p.setClipPath(clip)
        if pix is not None:
            p.drawPixmap(poster, pix, QRectF(pix.rect()))
        else:
            p.fillRect(poster, T.input)  # no poster found: a quiet card with the game's name, same shade as a fitted poster's border
            p.setPen(T.fg3)
            f = QFont(p.font())
            f.setBold(True)
            f.setPointSizeF((f.pointSizeF() if f.pointSizeF() > 0 else 10.0) + 1)
            p.setFont(f)
            p.drawText(poster.adjusted(12, 12, -12, -12), int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap), r.game)
        p.restore()
        hovered = option.state & QStyle.StateFlag.State_MouseOver
        selected = option.state & QStyle.StateFlag.State_Selected
        p.setPen(QPen(T.accent if hovered or selected else T.card_border, 2 if hovered or selected else 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(clip)
        badges = []
        if state.get("update"):
            badges.append(("Update", T.warn))
        elif state.get("installed"):
            badges.append(("Installed", T.ok))
        if (own := state.get("owned")) == "verified":
            badges.append(("ROM ✓", T.ok))
        elif own == "likely":
            badges.append(("ROM?", T.warn))
        x = poster.right() - 8
        small = QFont(p.font())
        small.setPointSizeF(9.0)  # the badge on a poster: nothing in the app is set smaller than 9pt
        small.setBold(True)
        p.setFont(small)
        fm = p.fontMetrics()
        for text, col in badges:
            w = fm.horizontalAdvance(text) + 14
            box = QRectF(x - w, poster.y() + 8, w, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 190))
            p.drawRoundedRect(box, 10, 10)
            p.setPen(col)
            p.drawText(box, AlignC, text)
            x -= w + 4
        if r.score > 0:  # the score sits top-left, opposite the status badges: a small star and the number
            text = f"{r.score:.1f}"
            w = fm.horizontalAdvance(text) + 34
            box = QRectF(poster.x() + 8, poster.y() + 8, w, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 190))
            p.drawRoundedRect(box, 10, 10)
            p.setBrush(QColor(255, 196, 0))
            p.drawPolygon(star_polygon(box.x() + 13, box.center().y() + 0.5, 6.5))
            p.setPen(QColor(255, 255, 255))
            p.drawText(QRectF(box.x() + 22, box.y(), w - 26, box.height()), AlignL | AlignV, text)
        p.setOpacity(1.0)
        name_font = QFont(option.font)
        name_font.setBold(True)
        p.setFont(name_font)
        p.setPen(T.title)
        text_w = POSTER_W  # the text sits exactly under the cover, centred on it
        name_rect = QRectF(slot.x(), slot.bottom() + 8, text_w, 20)
        p.drawText(name_rect, AlignC, p.fontMetrics().elidedText(r.name, Qt.TextElideMode.ElideRight, int(text_w)))
        sub = QFont(option.font)
        sub.setPointSizeF(9.0)
        p.setFont(sub)
        p.setPen(T.fg3)
        p.drawText(QRectF(slot.x(), name_rect.bottom() + 1, text_w, 18), AlignC,
                   p.fontMetrics().elidedText(r.game, Qt.TextElideMode.ElideRight, int(text_w)))
        p.setPen(T.accent_text)
        p.drawText(QRectF(slot.x(), name_rect.bottom() + 20, text_w, 18), AlignC, r.system)
        p.restore()


class RecompMixin:
    """Methods only; the state lives on DistillWindow (initialised by `_rc_init`)."""

    def _rc_init(self) -> None:
        self._rc_catalog: list[rc.Recomp] = []
        self._rc_by_id: dict[str, rc.Recomp] = {}
        self._rc_weighted: dict[str, float] = {}  # id -> score weighed by stars, what the grid is ordered by
        self._rc_store: rc.Store | None = None
        self._rc_owned: dict[str, rc.RomMatch] = {}
        self._rc_latest: dict[str, object] = {}  # id -> (Release, asset) | error text
        self._rc_art: dict[str, QPixmap] = {}
        self._rc_art_tried: set[str] = set()  # asked for this session (queued, fetched, or settled)
        self._rc_art_q = ArtQueue(lambda r, key, token: rc.fetch_cover(r, token, False, key),
                                  lambda r, found: self.ui_q.put(("recomp_art", r.id, found)),
                                  workers=ART_WORKERS, busy=rc.SgdbBusy, on_busy=lambda r: self._rc_art_tried.discard(r.id))  # rate-limited: ask again next time it is drawn
        self._rc_busy: dict[str, threading.Event] = {}  # installs in progress (the Event cancels)
        self._rc_current = ""
        self._rc_banner_key = ""  # the game whose banner the page is showing or waiting for
        self._rc_banner_real = False  # True once real banner art (not the cover stand-in) is on the page
        self._rc_banner_busy: set[str] = set()  # banners being looked for right now
        self._rc_snap = False  # the next detail refresh follows a change of game: no animation
        self._rc_flight: flight.Flight | None = None  # the cover in the air between the grid and a page
        self._rc_flight_id = 0  # bumped by every new flight and cancellation
        self._rc_hold_id = ""  # the game whose grid cell is left empty while its cover flies to it
        self._rc_open_after = ""  # a project just added or edited: its page opens once the rebuilt catalog arrives
        self._rc_list_scroll = 0  # where the grid was scrolled to when a page was opened
        self._rc_loaded = False
        self._rc_game: dict[str, str] = {}  # id -> the LaunchBox Overview of the game ('' = none found)
        self._rc_about: dict[str, str] = {}  # id -> the one-line description the developers gave the repository
        self._rc_check_stop = threading.Event()

    @property
    def rc_store(self) -> rc.Store:
        if self._rc_store is None:
            self._rc_store = rc.Store()
        return self._rc_store

    # ---- building -------------------------------------------------------------------------------------------
    def _build_recomp_page(self) -> QWidget:
        self._rc_init()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        self.rc_views = ModalStack()
        self.rc_views.dismissed.connect(self._rc_back)
        self.rc_views.addWidget(self._rc_build_grid())
        self.rc_views.addWidget(self._rc_build_detail())
        lay.addWidget(self.rc_views, 1)
        page.setMinimumSize(1, 560)  # a width of 1, not 0: Qt reads 0 as "unset" and falls back to the widest child (the grid's toolbar)
        return page

    def _rc_build_grid(self) -> QWidget:
        view = QWidget()
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        bar = QHBoxLayout()  # two rows: filters on top, actions below, so the page never needs more width than the window has
        bar.setSpacing(8)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.rc_search = QLineEdit()
        self.rc_search.setPlaceholderText("Search games and projects...")
        self.rc_search.setClearButtonEnabled(True)
        self.rc_search.textChanged.connect(self._rc_fill)
        self.rc_system = QComboBox()
        self.rc_system.currentIndexChanged.connect(self._rc_fill)
        self.rc_kind = QComboBox()
        self.rc_kind.setToolTip("What kind of project it is: a static recompilation, a decompilation, a source port or an engine remake.")
        self.rc_kind.currentIndexChanged.connect(self._rc_fill)
        self.rc_mine = AnimRadio("My recomps")
        self.rc_mine.setAutoExclusive(False)  # a radio that can be switched off again: it is a filter, not one of a set
        self.rc_mine.setToolTip("Show only the projects you added yourself with Add recomp.")
        self.rc_mine.toggled.connect(self._rc_fill)
        self.rc_installed = AnimRadio("Installed")
        self.rc_installed.setAutoExclusive(False)  # a filter, like My recomps: it can be switched off again
        self.rc_installed.setToolTip("Show only the projects installed on this computer.")
        self.rc_installed.toggled.connect(self._rc_fill)
        self.rc_check_btn = QPushButton("Check my library")
        self.rc_check_btn.setToolTip("Compare the last Library Report with what each project needs. Files are only read, never changed.")
        self.rc_check_btn.clicked.connect(self._rc_check_library)
        self.rc_updates_btn = QPushButton("Check for updates")
        self.rc_updates_btn.clicked.connect(self._rc_check_updates)
        bar.addWidget(self.rc_search, 1)
        bar.addWidget(self.rc_system)
        bar.addWidget(self.rc_kind)
        actions.addWidget(self.rc_mine)
        actions.addWidget(self.rc_installed)
        actions.addStretch(1)
        actions.addWidget(self.rc_check_btn)
        self.rc_add_btn = QPushButton("Add recomp...")
        self.rc_add_btn.setToolTip("Add a project that isn't listed yet: its GitHub address, the game it plays and what kind of project it is.")
        self.rc_add_btn.clicked.connect(lambda: self._rc_add_dialog())
        actions.addWidget(self.rc_add_btn)
        actions.addWidget(self.rc_updates_btn)
        lay.addLayout(bar)
        lay.addLayout(actions)
        self.rc_status = hint("", live=True)
        lay.addWidget(self.rc_status)
        self.rc_list = QListWidget()
        self.rc_list.setViewMode(QListView.ViewMode.IconMode)
        self.rc_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.rc_list.setMovement(QListView.Movement.Static)
        self.rc_list.setUniformItemSizes(True)
        self.rc_list.setSpacing(4)
        self.rc_list.setMouseTracking(True)
        self.rc_list.setFrameShape(QFrame.Shape.NoFrame)
        self.rc_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rc_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.rc_list.setItemDelegate(PosterDelegate(self))
        self.rc_list.setMinimumHeight(480)
        smooth_scroll(self.rc_list, bounce=True)  # a gentler, gliding wheel (the default flies through a long catalog) that gives a little at either end
        self.rc_list.itemActivated.connect(self._rc_open_item)
        self.rc_list.itemClicked.connect(self._rc_open_item)
        lay.addWidget(self.rc_list, 1)
        return view

    def _rc_hero_css(self) -> str:
        """The top card: the colour wash the Emulator page uses; a little see-through while the game's banner shows behind it."""
        card = tint(T.card, 222 if self._rc_banner_on() else 255)
        return (f"QFrame#card {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {tint(T.grad[1], 34)}, stop:0.6 {card}, stop:1 {card}); "
                f"border: 1px solid {T.card_border.name()}; border-radius: 14px; }}")

    def _rc_banner_on(self) -> bool:
        return getattr(self, "rcd_view", None) is not None and self.rcd_view._src is not None

    def _rc_build_detail(self) -> QWidget:
        """One project's page: a hero (rounded poster, name, what the library has, the buttons), then two columns - what to know before
        playing, what the developers say and the release notes on the left; facts and links in a side panel that drops below when narrow."""
        view = BackdropView()  # paints the game's banner art across the top of the page, fading into the page's colour
        self.rcd_view = view
        lay = detail_shell(view)  # no back button: click outside the card (or Esc) to close it

        # -- the hero
        hero = Card()
        hero.lay.setContentsMargins(22, 20, 22, 18)
        hero.lay.setSpacing(16)
        self.rcd_hero = hero
        T.bind_css(hero, self._rc_hero_css)
        top = QHBoxLayout()
        top.setSpacing(22)
        self.rcd_cover = CoverFrame()
        self.rcd_cover.editRequested.connect(self._rc_cover_menu)
        self.rcd_cover.setFixedSize(DETAIL_W, DETAIL_H)
        self.rcd_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rcd_cover.setWordWrap(True)
        top.addWidget(self.rcd_cover, 0, Qt.AlignmentFlag.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(6)
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.rcd_chips = QHBoxLayout()  # the system, then the tags: refilled for each game
        self.rcd_chips.setSpacing(6)
        chips.addLayout(self.rcd_chips)
        chips.addStretch(1)
        self.rcd_status = QLabel()  # Installed / Update available / Not installed
        chips.addWidget(self.rcd_status)
        info.addLayout(chips)
        self.rcd_title = QLabel()
        self.rcd_title.setObjectName("pageTitle")
        self.rcd_title.setStyleSheet("font-size: 20pt;")  # the page title above is 22pt already: this one a step down
        self.rcd_title.setWordWrap(True)
        info.addWidget(self.rcd_title)
        self.rcd_sub = QLabel()
        self.rcd_sub.setObjectName("muted")
        self.rcd_sub.setWordWrap(True)
        info.addWidget(self.rcd_sub)
        info.addSpacing(6)
        self.rcd_desc = QLabel()
        self.rcd_desc.setWordWrap(True)
        self.rcd_desc.setTextFormat(Qt.TextFormat.RichText)
        info.addWidget(self.rcd_desc)
        info.addSpacing(6)
        self.rcd_rom_box = QVBoxLayout()  # one callout: does the library have the game this needs
        self.rcd_rom_box.setSpacing(8)
        info.addLayout(self.rcd_rom_box)
        info.addStretch(1)
        top.addLayout(info, 1)
        hero.add(top)
        rule = QFrame()
        rule.setFixedHeight(1)
        T.bind_css(rule, lambda: f"background: {T.card_border.name()}; border: none;")
        hero.add(rule)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.rcd_main = accent_button("Install")
        self.rcd_main.setMinimumHeight(40)
        self.rcd_main.setMinimumWidth(112)
        self.rcd_main.clicked.connect(self._rc_main_action)
        self.rcd_play = QPushButton("Play")
        self.rcd_play.clicked.connect(self._rc_play)
        self.rcd_more = QPushButton("Open folder")
        self.rcd_more.clicked.connect(self._rc_open_folder)
        self.rcd_remove = QPushButton("Uninstall")
        self.rcd_remove.clicked.connect(self._rc_uninstall)
        T.bind_css(self.rcd_remove, lambda: f"QPushButton:hover {{ background: {T.btn_danger[0].name()}; border-color: {T.btn_danger[1].name()}; "
                                      f"color: {T.bad.name()}; }}")  # only the hover is red: it sits apart and stays quiet until pointed at
        self.rcd_show_rom = QPushButton("Show my ROM")
        self.rcd_show_rom.clicked.connect(self._rc_show_rom)
        self.rcd_edit = QPushButton("Edit...")  # only for a project from the player's own list
        self.rcd_edit.setToolTip("Change this project's details: its name, game, which download to pick, or its repository.")
        self.rcd_edit.clicked.connect(lambda: self._rc_add_dialog(self._rc_by_id.get(self._rc_current)))
        self.rcd_unlist = QPushButton("Remove from my list")
        self.rcd_unlist.setToolTip("Take this project off your own list. An installed copy has to be uninstalled first.")
        self.rcd_unlist.clicked.connect(self._rc_unlist)
        for b in (self.rcd_play, self.rcd_more, self.rcd_show_rom, self.rcd_remove, self.rcd_edit, self.rcd_unlist):
            b.setMinimumHeight(40)
        for b in (self.rcd_main, self.rcd_play, self.rcd_more, self.rcd_show_rom, self.rcd_edit):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.rcd_unlist)
        row.addWidget(self.rcd_remove)
        hero.add(row)
        self.rcd_bar = QProgressBar()
        self.rcd_bar.setTextVisible(False)
        self.rcd_bar.setFixedHeight(8)
        T.bind_css(self.rcd_bar, lambda: f"QProgressBar {{ border: none; border-radius: 4px; background: {T.input.name()}; }}"
                                   f"QProgressBar::chunk {{ border-radius: 4px; background: {T.gradient_css()}; }}")
        hero.add(self.rcd_bar)
        self.rcd_msg = hint("", live=True)
        hero.add(self.rcd_msg)
        lay.addWidget(hero)

        # -- the left column
        main = QWidget()
        left = QVBoxLayout(main)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(14)
        self.rcd_game_card = Card()  # the game's Overview from the LaunchBox Games Database; only shown when there is one
        self.rcd_game_card.lay.setContentsMargins(20, 16, 20, 18)
        self.rcd_game_card.lay.setSpacing(12)
        game_head = section_header("About the game", "info")
        game_head.addWidget(SourceChip("LaunchBox", "From the LaunchBox Games Database", LAUNCHBOX_COLOUR))
        self.rcd_game_card.add(game_head)
        self.rcd_game = ClampedText()
        self.rcd_game_card.add(self.rcd_game)
        self.rcd_game_toggle = QPushButton()
        self.rcd_game_toggle.setCheckable(True)
        self.rcd_game_toggle.setFlat(True)
        self.rcd_game_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rcd_game_toggle.setIconSize(QSize(14, 14))
        self.rcd_game_toggle.setStyleSheet("text-align: left; padding: 2px 0;")
        self.rcd_game_toggle.toggled.connect(self._rc_toggle_game)
        self.rcd_game_toggle.setVisible(False)
        self.rcd_game.on_overflow = self._rc_game_overflow
        self.rcd_game_card.add(self.rcd_game_toggle)
        self.rcd_game_card.setVisible(False)
        left.addWidget(self.rcd_game_card)
        self.rcd_about_card = Card()
        self.rcd_about_card.lay.setContentsMargins(20, 16, 20, 18)
        self.rcd_about_card.lay.setSpacing(12)
        self.rcd_about_card.add(section_header("From the developers", "branch"))
        self.rcd_about = QLabel()
        self.rcd_about.setWordWrap(True)
        self.rcd_about.setTextFormat(Qt.TextFormat.RichText)
        self.rcd_about_card.add(self.rcd_about)
        left.addWidget(self.rcd_about_card)
        self.rcd_notes_card = Card()
        self.rcd_notes_card.lay.setContentsMargins(20, 16, 20, 14)
        notes_head = section_header("Release notes", "clock")
        self.rcd_notes_ver = QLabel()
        T.bind_css(self.rcd_notes_ver, lambda: pill_css(T.accent_text))
        notes_head.addWidget(self.rcd_notes_ver)
        self.rcd_notes_card.add(notes_head)
        self.rcd_notes_toggle = QPushButton()
        self.rcd_notes_toggle.setCheckable(True)
        self.rcd_notes_toggle.setFlat(True)
        self.rcd_notes_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rcd_notes_toggle.setIconSize(QSize(14, 14))
        self.rcd_notes_toggle.setStyleSheet("text-align: left; padding: 2px 0;")
        self.rcd_notes_toggle.toggled.connect(self._rc_toggle_notes)
        self.rcd_notes_card.add(self.rcd_notes_toggle)
        self.rcd_notes = QTextBrowser()
        self.rcd_notes.setOpenExternalLinks(True)
        self.rcd_notes.setMinimumHeight(160)
        self.rcd_notes.setVisible(False)  # collapsed until asked for
        self.rcd_notes_card.add(self.rcd_notes)
        left.addWidget(self.rcd_notes_card)
        left.addStretch(1)

        # -- the side panel
        side = QWidget()
        right = QVBoxLayout(side)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(14)
        facts_card = Card()
        facts_card.lay.setContentsMargins(18, 16, 18, 16)
        facts_card.lay.setSpacing(12)
        facts_card.add(section_header("Details", "list"))
        self.rcd_facts = FactList()
        facts_card.add(self.rcd_facts)
        right.addWidget(facts_card)
        links_card = Card()
        links_card.lay.setContentsMargins(18, 16, 18, 10)
        links_card.lay.setSpacing(10)
        links_card.add(section_header("Links", "external"))
        self.rcd_links = QLabel()
        self.rcd_links.setTextFormat(Qt.TextFormat.RichText)
        self.rcd_links.setWordWrap(True)  # "Other projects for this game: ..." can be long
        self.rcd_links.setOpenExternalLinks(False)
        self.rcd_links.linkActivated.connect(lambda u: QDesktopServices.openUrl(QUrl(u)))
        links_card.add(self.rcd_links)
        right.addWidget(links_card)
        lay.addWidget(TwoColumn(main, side))
        lay.addStretch(1)
        return view

    # ---- showing the page -----------------------------------------------------------------------------------
    def _rc_on_show(self) -> None:
        """Called each time the page is opened: loads the catalog the first time, and pulls in a fresh Library Report."""
        if not self._rc_loaded:
            self._rc_loaded = True
            self._rc_owned = self.rc_store.owned()
            self._rc_apply_catalog(rc.load_catalog(), first=True)
            self._rc_reload_catalog()  # extra sources and GitHub search, off the UI thread
            self._rc_check_updates(quiet=True)
        self._rc_refresh_status()

    def _rc_apply_catalog(self, catalog: list, first: bool = False) -> None:
        keep = self.rc_system.currentText() if self.rc_system.currentIndex() > 0 else ""
        self._rc_catalog = catalog
        self._rc_by_id = {r.id: r for r in catalog}
        self._rc_weighted = rc.weighted_scores(catalog)
        self.rc_system.blockSignals(True)
        self.rc_system.clear()
        self.rc_system.addItem("All systems")
        for system in sorted({r.system for r in catalog}, key=str.casefold):
            self.rc_system.addItem(system)
        if keep and (i := self.rc_system.findText(keep)) > 0:
            self.rc_system.setCurrentIndex(i)
        self.rc_system.blockSignals(False)
        keep_kind = self.rc_kind.currentText() if self.rc_kind.currentIndex() > 0 else ""
        self.rc_kind.blockSignals(True)
        self.rc_kind.clear()
        self.rc_kind.addItem("All types")
        self.rc_kind.addItems(rc.kinds_in(catalog))
        if keep_kind and (i := self.rc_kind.findText(keep_kind)) > 0:
            self.rc_kind.setCurrentIndex(i)
        self.rc_kind.blockSignals(False)
        self._rc_fill()
        if self._rc_open_after in self._rc_by_id:
            rid, self._rc_open_after = self._rc_open_after, ""
            self._rc_show_detail(rid)

    def _rc_reload_catalog(self, force: bool = False) -> None:
        """Rebuild the catalog from the bundled list plus the sources and GitHub search, in a worker thread."""
        sources, find_new, curation = self.rc_store.sources(), self.rc_store.find_new(), self.rc_store.curation()
        self.rc_status.setText("Loading project lists...")

        def work() -> None:
            try:
                self.ui_q.put(("recomp_catalog", rc.build_catalog(sources, find_new, force, curation), ""))
            except Exception as exc:  # a bad list must not take the page down
                self.ui_q.put(("recomp_catalog", None, f"Couldn't load the project lists: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _rc_add_dialog(self, existing: rc.Recomp | None = None) -> None:
        """Add a project to the player's own list (or, with `existing`, change one that is already there)."""
        systems = sorted({r.system for r in self._rc_catalog} | set(SYSTEM_NAMES), key=str.casefold)
        dlg = AddRecompDialog(self, self.rc_store, systems, existing)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.entry is None:
            return
        try:
            rc.save_user_entry(dlg.entry)
        except rc.RecompError as exc:
            self.rc_status.setText(str(exc))
            return
        if existing and existing.repo != dlg.entry["repo"] and self.rc_store.installed(existing.id):
            self.rc_status.setText(f"Saved. {existing.name} is installed from {existing.repo}; it will update from {dlg.entry['repo']} from now on.")
        else:
            self.rc_status.setText(f"{'Saved' if existing else 'Added'} {dlg.entry['name']}. It is in your own list.")
        message = self.rc_status.text()
        if dlg.cover_changed:  # a cover was chosen or taken off in the window: draw the new state, not the old picture
            self._rc_art.pop(dlg.entry["id"], None)
            self._rc_art_tried.discard(dlg.entry["id"])
            if dlg.cover_path is not None:
                self._rc_set_art(dlg.entry["id"], dlg.cover_path, repaint=False)
        self._rc_open_after = dlg.entry["id"]
        self._rc_reload_catalog()  # (says "Loading project lists..." while it works)
        QTimer.singleShot(0, lambda: self.rc_status.setText(message))

    # ---- changing a project's cover from its page --------------------------------------------------------------
    def _rc_cover_menu(self) -> None:
        r = self._rc_by_id.get(self._rc_current)
        if r is None or r.id in self._rc_busy:
            return
        menu = QMenu(self)
        search = menu.addAction("Search SteamGridDB...")
        upload = menu.addAction("Upload from this computer...")
        menu.addSeparator()
        remove = menu.addAction("Remove cover")
        remove.setEnabled(rc.cached_cover(r) is not None)
        chosen = menu.exec(QCursor.pos())
        if chosen is search:
            key = self.rc_store.sgdb_key()
            if not key:
                msgbox.information(self, "Search SteamGridDB", "Searching SteamGridDB needs a free API key. Add yours under Settings > API keys > Games & artwork, then try again.")
                return
            dlg = CoverSearchDialog(self, key, r.game or r.name)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.chosen:
                self._rc_apply_cover(r, dlg.chosen)
        elif chosen is upload:
            path, _f = QFileDialog.getOpenFileName(self, "Choose a cover", "", "Pictures (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;All files (*)")
            if path:
                try:
                    data = read_picture(path)
                except rc.RecompError as exc:
                    msgbox.warning(self, "Choose a cover", str(exc))
                    return
                self._rc_apply_cover(r, data)
        elif chosen is remove:
            if msgbox.question(self, "Remove cover", f"Remove the cover of {r.name}?\n\nNone will be looked for afterwards. You can choose one again here at any time.") \
                    == QMessageBox.StandardButton.Yes:
                self._rc_apply_cover(r, None)

    def _rc_apply_cover(self, r: rc.Recomp, data: bytes | None) -> None:
        """Keep `data` as the project's cover (or, with None, take its cover away) and redraw the page and the grid."""
        try:
            path = rc.set_cover(r.id, data) if data is not None else rc.clear_cover(r.id)
        except rc.RecompError as exc:
            msgbox.warning(self, "Cover", str(exc))
            return
        self._rc_art.pop(r.id, None)
        self._rc_art_tried.discard(r.id)
        if path is not None:
            self._rc_set_art(r.id, path)
        else:
            self._rc_detail_cover(r)
            self.rc_list.viewport().update()
        self.rc_status.setText(f"Changed the cover of {r.name}." if data is not None else f"Removed the cover of {r.name}.")

    def _rc_unlist(self) -> None:
        r = self._rc_by_id.get(self._rc_current)
        if r is None or not rc.is_custom(r) or r.id in self._rc_busy:
            return
        if self.rc_store.installed(r.id):
            msgbox.information(self, "Remove from my list", f"{r.name} is installed. Uninstall it first, so its files go too, then take it off your list.")
            return
        if msgbox.question(self, "Remove from my list", f"Take {r.name} off your own list?\n\nIt can be added again later.") != QMessageBox.StandardButton.Yes:
            return
        rc.remove_user_entry(r.id)
        self._rc_back()
        self.rc_status.setText(f"Removed {r.name} from your list.")
        self._rc_reload_catalog()

    def _rc_state(self, r: rc.Recomp) -> dict:
        inst = self.rc_store.installed(r.id)
        latest = self._rc_latest.get(r.id)
        update = bool(inst and isinstance(latest, tuple) and rc.is_newer(inst.version, latest[0].tag))
        own = self._rc_owned.get(r.id)
        return {"installed": inst is not None, "update": update, "owned": own.status if own else ""}

    def _rc_fill(self, *_a) -> None:
        text = self.rc_search.text().lower().split()
        system = self.rc_system.currentText() if self.rc_system.currentIndex() > 0 else ""
        kind = self.rc_kind.currentText() if self.rc_kind.currentIndex() > 0 else ""
        mine = self.rc_mine.isChecked()
        installed = self.rc_installed.isChecked()
        self.rc_list.clear()
        shown = 0
        # best weighted score first (a high score backed by few stars is pulled toward the average); ties go to the
        # more-starred project, then by name. Projects with no score sink to the end.
        weighted = self._rc_weighted
        for r in sorted(self._rc_catalog, key=lambda r: (-weighted.get(r.id, 0.0), -r.stars, r.game.casefold(), r.name.casefold())):
            state = self._rc_state(r)
            if system and r.system != system:
                continue
            if kind and kind not in rc.kinds_of(r):
                continue
            if mine and not rc.is_custom(r):
                continue
            if installed and not state.get("installed"):
                continue
            blob = f"{r.name} {r.game} {r.system} {' '.join(r.tags)}".lower()
            if not all(t in blob for t in text):
                continue
            item = QListWidgetItem()
            item.setData(ID_ROLE, r.id)
            item.setData(STATE_ROLE, state)
            item.setSizeHint(QSize(CELL_W, CELL_H))
            item.setToolTip(f"{r.name}: {r.game} ({r.system})")
            self.rc_list.addItem(item)
            shown += 1
        self._rc_refresh_status(shown)

    def _rc_refresh_status(self, shown: int | None = None) -> None:
        total = len(self._rc_catalog)
        shown = self.rc_list.count() if shown is None else shown
        if self.rc_mine.isChecked():
            msg = (f"{shown} of your own project{'s' if shown != 1 else ''}." if shown else
                   "You haven't added any recomps yet. Press Add recomp... to add one from its GitHub address.")
        elif self.rc_installed.isChecked():
            msg = (f"{shown} of {total} projects installed." if shown else "Nothing is installed yet. Open a project and press Install.")
        else:
            msg = f"{shown} of {total} projects."
            if not self._rc_owned:
                msg += " Run the Library Report and press Check my library to see which ones you can play."
        self.rc_status.setText(msg)

    # ---- covers ---------------------------------------------------------------------------------------------
    def _rc_pixmap(self, r: rc.Recomp) -> QPixmap | None:
        """The poster for the grid. Called while painting, so only what is on screen is ever loaded or fetched."""
        if r.id not in self._rc_art:
            self._rc_ensure_art(r)
        return self._rc_art.get(r.id)

    def _rc_ensure_art(self, r: rc.Recomp) -> None:
        """Load the cached poster now, and queue a background fetch for a missing one (or a better one, once a SteamGridDB key is set)."""
        if r.id not in self._rc_art and (hit := rc.cached_cover(r)) is not None:
            self._rc_set_art(r.id, hit, repaint=False)
        key = self.rc_store.sgdb_key()
        if r.id in self._rc_art_tried or rc.cover_settled(r, key):
            return
        self._rc_art_tried.add(r.id)
        self._rc_art_q.add(r, key, self.rc_store.token())

    def _rc_set_art(self, rid: str, path: Path | None, repaint: bool = True) -> None:
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        pix = cover_pixmap(path, DETAIL_W, DETAIL_H, dpr)
        if pix is None:
            return
        if rid not in self._rc_art and len(self._rc_art) >= ART_CACHE_MAX:
            self._rc_art.pop(next(iter(self._rc_art)))
        self._rc_art[rid] = pix
        if repaint:
            self.rc_list.viewport().update()
        if rid == self._rc_current and (cur := self._rc_by_id.get(rid)) is not None:
            self._rc_detail_cover(cur)
            if not self._rc_banner_real:
                self._rc_set_banner(self._rc_banner_stand_in(cur), False)

    # ---- the banner behind a project's page -------------------------------------------------------------------
    def _rc_set_banner(self, pix: QPixmap | None, real: bool) -> None:
        self.rcd_view.set_art(pix)
        self._rc_banner_real = real and pix is not None and not pix.isNull()
        self.rcd_hero.setStyleSheet(self._rc_hero_css())  # see-through while there is art behind it

    def _rc_banner_stand_in(self, r: rc.Recomp) -> QPixmap | None:
        """Until real banner art is found (or if there is none) the poster, blown up soft, stands in for it."""
        cover = self._rc_art.get(r.id)
        return cover.scaled(32, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation) if cover is not None else None

    def _rc_load_banner(self, r: rc.Recomp) -> None:
        """Put the banner of the game this project plays behind its page: a saved one shows at once, otherwise it is looked for in the
        background (SteamGridDB with the person's key, else LaunchBox fan art) and swaps in when it arrives."""
        key = mm.recomp_banner_key(r.game)
        self._rc_banner_key = key
        saved = mm.cached_recomp_banner(r.game)
        if saved is not None:
            self._rc_set_banner(QPixmap(str(saved)), True)
            return
        self._rc_set_banner(self._rc_banner_stand_in(r), False)
        if key in self._rc_banner_busy:
            return
        self._rc_banner_busy.add(key)
        sgdb, q = self.rc_store.sgdb_key(), self.ui_q

        def work() -> None:
            try:
                found = mm.fetch_recomp_banner(r, sgdb)
            except Exception:  # a banner is a nicety: whatever goes wrong, the page just goes without
                found = None
            q.put(("recomp_banner", key, str(found) if found else ""))
        threading.Thread(target=work, daemon=True).start()

    # ---- opening a project ----------------------------------------------------------------------------------
    def _rc_open_item(self, item: QListWidgetItem) -> None:
        self._rc_show_detail(item.data(ID_ROLE))

    def _rc_back(self) -> None:
        rid = self._rc_current
        self._rc_cancel_flight()
        leaving = None  # the page's cover and where it is, for it to glide back to its place in the grid
        if rid and self._rc_can_fly() and self.rc_views.currentIndex() == 1:
            view = self.settings_scroll.viewport()
            box = flight.box_of(self.rcd_cover, view)
            if view.rect().intersects(box):
                leaving = (flight.picture_of(self.rcd_cover), box)
        self._rc_current = ""
        self.rc_views.setCurrentIndex(0)
        self._rc_fill()
        self._rc_scroll_list(self._rc_list_scroll)  # back to where the grid was
        if leaving is not None:
            self._rc_fly_to_poster(rid, *leaving)

    # ---- the cover's flight between the grid and a page -------------------------------------------------------
    def _rc_can_fly(self) -> bool:
        return False  # the detail is a card over the blurred list: no cover flight

    def _rc_scroll_list(self, value: int) -> None:
        """Move the grid's scroll bar, now and once more after it has been laid out (a rebuilt list starts at the top)."""
        bar = self.rc_list.verticalScrollBar()
        bar.setValue(value)
        QTimer.singleShot(0, lambda: bar.setValue(value))

    def _rc_poster_box(self, rid: str) -> QRect | None:
        """Where this game's cover is in the grid, in the coordinates of the scroll area's viewport (where the flying picture lives);
        None when the game isn't in the grid or its row is scrolled out of sight."""
        if rid not in self._rc_by_id:
            return None
        for i in range(self.rc_list.count()):
            item = self.rc_list.item(i)
            if item.data(ID_ROLE) == rid:
                break
        else:
            return None
        cell = self.rc_list.visualItemRect(item)
        grid = self.rc_list.viewport()
        if not grid.rect().intersects(cell):
            return None
        slot = QRectF(cell.x() + (cell.width() - POSTER_W) / 2, cell.y() + 6, POSTER_W, POSTER_H)  # the same slot the delegate paints into
        poster = art_rect(slot, self._rc_art.get(rid)).toRect()
        return QRect(grid.mapTo(self.settings_scroll.viewport(), poster.topLeft()), poster.size())

    def _rc_cancel_flight(self) -> None:
        self._rc_flight_id += 1
        if self._rc_flight is not None:
            self._rc_flight.finish()  # puts back whatever it was holding hidden
            self._rc_flight = None

    def _rc_fly_to_page(self, start: QRect) -> None:
        """The grid's cover glides to its place on the page while the page fades in; the page's own copy stays hidden until it lands."""
        view = self.settings_scroll.viewport()
        cover = self.rcd_cover
        picture = flight.picture_of(cover)
        motion.reveal(self.rc_views.widget(1))
        flight.hold(cover)
        self._rc_flight = flight.Flight(view, picture, start, lambda: flight.box_of(cover, view), done=lambda: flight.release(cover)).go()

    def _rc_fly_to_poster(self, rid: str, picture, start: QRect) -> None:
        """The page's cover glides back down to its cell while the grid fades in. The cell is looked up on every frame (the grid may still be
        settling) and its own cover stays out of sight until the picture lands on it."""
        view = self.settings_scroll.viewport()
        self._rc_hold_id = rid
        self.rc_list.viewport().update()

        def done() -> None:
            self._rc_hold_id = ""
            self.rc_list.viewport().update()

        motion.reveal(self.rc_views.widget(0))
        self._rc_flight = flight.Flight(view, picture, start, lambda: self._rc_poster_box(rid), done=done).go()

    def _rc_show_detail(self, rid: str) -> None:
        r = self._rc_by_id.get(rid)
        if r is None:
            return
        self._rc_cancel_flight()
        leaving = None  # where this game's cover is in the grid, for it to glide up from
        if self.rc_views.currentIndex() == 0:
            self._rc_list_scroll = self.rc_list.verticalScrollBar().value()
            if self._rc_can_fly():
                leaving = self._rc_poster_box(rid)
        self._rc_current = rid
        self._rc_snap = True  # a different game: its buttons swap at once; only a change to the same one animates
        self.rc_views.setCurrentIndex(1)
        self._rc_detail_cover(r)
        self._rc_ensure_art(r)
        self._rc_load_banner(r)
        clear_layout(self.rcd_chips)
        self.rcd_chips.addWidget(self._rc_chip(r.system.upper(), chip_css(T.accent_text)))
        for tag in r.tags[:4]:
            self.rcd_chips.addWidget(self._rc_chip(tag, pill_css(T.warn if tag == "Unverified" else T.fg3)))
        self.rcd_title.setText(r.name)
        self.rcd_sub.setText(f"Plays {r.game}  ·  by {r.repo.split('/')[0]}")
        self.rcd_desc.setText(self._rc_para(r.description) if r.description else "")
        self.rcd_desc.setVisible(bool(r.description))
        links = [("GitHub", f"https://github.com/{r.repo}"), ("All releases", f"https://github.com/{r.repo}/releases"),
                 ("Report an issue", f"https://github.com/{r.repo}/issues")]
        if r.website:
            links.insert(0, ("Website", r.website))
        if r.discord:
            links.append(("Discord", r.discord))
        accent = T.accent_text.name()

        def link(text: str, url: str) -> str:
            return (f'<p style="margin: 0 0 8px 0;"><a href="{html.escape(url, quote=True)}" style="color: {accent}; '
                    f'text-decoration: none;">{html.escape(text)}&nbsp;&nbsp;↗</a></p>')
        body = "".join(link(t, u) for t, u in links)
        if r.others:  # other projects for the same game that the curated list folded into this one
            body += (f'<p style="margin: 10px 0 6px 0; color: {T.fg3.name()}; font-size: 9pt; font-weight: 600;">'
                     "Other projects for this game</p>" + "".join(link(name, f"https://github.com/{repo}") for name, repo in r.others[:6]))
        self.rcd_links.setText(body)
        self.rcd_notes.setMarkdown("Checking GitHub...")
        self._rc_set_about(self._rc_about.get(rid) or r.description or "Loading...")
        self.rcd_notes_toggle.setChecked(False)  # release notes start collapsed
        if rid not in self._rc_about:
            self._rc_fetch_about(r)
        self._rc_set_game(self._rc_game.get(rid, ""))
        if rid not in self._rc_game:
            self._rc_fetch_game(r)
        self.rcd_bar.setValue(0)
        self.rcd_msg.setText("")
        self._rc_refresh_detail()
        if rid not in self._rc_latest:
            self._rc_fetch_latest(r)
        if leaving is not None:
            self._rc_fly_to_page(leaving)

    def _rc_toggle_notes(self, open_: bool) -> None:
        motion.set_visible(self.rcd_notes, open_, axis="height", animate=not self._rc_snap)
        self._rc_notes_title()

    def _rc_notes_title(self) -> None:
        latest = self._rc_latest.get(self._rc_current)
        version = latest[0].tag if isinstance(latest, tuple) else ""
        self.rcd_notes_ver.setText(version)
        self.rcd_notes_ver.setVisible(bool(version))
        on = self.rcd_notes_toggle.isChecked()
        self.rcd_notes_toggle.setText("Hide release notes" if on else "Show release notes")
        self.rcd_notes_toggle.setIcon(icons.icon("chevron-down" if on else "chevron-right", T.fg3, 14))

    @staticmethod
    def _rc_needs_game(r: rc.Recomp) -> bool:
        """An engine remake rebuilds the game from scratch, so it doesn't need the person's own copy."""
        return "Engine remake" not in rc.kinds_of(r)

    @staticmethod
    def _rc_para(text: str) -> str:
        return f'<p style="margin: 0; line-height: 155%;">{html.escape(text)}</p>'

    def _rc_set_about(self, text: str) -> None:
        self.rcd_about.setText(self._rc_para(text))

    @staticmethod
    def _rc_chip(text: str, css: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(css)
        return label

    def _rc_detail_cover(self, r: rc.Recomp) -> None:
        """The poster on the page with round corners, or - when there is none yet - a quiet rounded card with the game's name."""
        pix = self._rc_art.get(r.id)
        if pix is not None:
            self.rcd_cover.setText("")
            self.rcd_cover.setStyleSheet("background: transparent; border: none;")
            dpr = pix.devicePixelRatio() or 1.0
            self.rcd_cover.setFixedSize(math.ceil(pix.width() / dpr), math.ceil(pix.height() / dpr))  # the container is the poster's own size
            self.rcd_cover.setPixmap(rounded_pixmap(pix, 16))
            return
        self.rcd_cover.setFixedSize(DETAIL_W, DETAIL_H)
        self.rcd_cover.setPixmap(QPixmap())
        self.rcd_cover.setText(r.game)
        T.bind_css(self.rcd_cover, lambda: f"background: {T.input.name()}; border: 1px solid {T.card_border.name()}; border-radius: 16px; "
                                     f"color: {T.fg3.name()}; font-weight: 700; padding: 16px;")

    def _rc_refresh_detail(self) -> None:
        r = self._rc_by_id.get(self._rc_current)
        if r is None:
            return
        animate, self._rc_snap = not self._rc_snap, False  # buttons come and go smoothly when this game's state changes
        inst = self.rc_store.installed(r.id)
        latest = self._rc_latest.get(r.id)
        busy = r.id in self._rc_busy
        own = self._rc_owned.get(r.id)
        unverified = "Unverified" in r.tags
        clear_layout(self.rcd_rom_box)
        notes: list[tuple[str, str]] = []
        if own and own.status == "verified":
            notes.append(("ok", f"Your library has the right game: {Path(own.path).name}"))
        elif own and own.status == "likely":
            notes.append(("warn", f"Your library probably has it: {Path(own.path).name}." + (f" {own.note}" if own.note else "")))
        elif own and own.status == "mismatch":
            notes.append(("bad", f"Found {Path(own.path).name}, but it isn't a dump this project accepts."))
        elif self._rc_owned and self._rc_needs_game(r):
            notes.append(("info", f"No matching game found in your library. You need your own copy of {r.game}."))
        setup = r.setup or ("" if unverified or not self._rc_needs_game(r) else "Needs your own copy of the game. Nothing here includes it.")
        if setup:
            notes.append(("info", setup))
        if unverified:
            notes.append(("warn", "Found by a GitHub search, not checked: it may be a tool rather than a game, "
                                  "or have no download for your system."))
        for note in notes:
            self.rcd_rom_box.addWidget(Callout(*note))
        motion.set_visible(self.rcd_show_rom, bool(own and own.path), axis="width", animate=animate)
        update = bool(inst and isinstance(latest, tuple) and rc.is_newer(inst.version, latest[0].tag))
        if busy:
            status, colour = "Working...", T.accent_text
        elif update:
            status, colour = "Update available", T.warn
        elif inst:
            status, colour = "Installed", T.ok
        else:
            status, colour = "Not installed", T.fg3
        self.rcd_status.setText(status)
        T.bind_css(self.rcd_status, lambda: pill_css(colour))
        rows: list[tuple[str, str, object]] = []
        if inst:
            rows.append(("Installed", inst.version, None))
        if isinstance(latest, tuple):
            rel, asset = latest
            rows.append(("Latest release", rel.tag, T.warn if update else None))
            rows.append(("Released", rel.published, None))
            rows.append(("Download", f"{asset['name']} ({rc.fmt_size(asset['size'])})", None))
            self.rcd_notes.setMarkdown(rel.body or "_No release notes._")
        elif isinstance(latest, str):
            rows.append(("Latest release", latest, None))
            self.rcd_notes.setMarkdown("_Release notes unavailable._")
        else:
            rows.append(("Latest release", "Checking...", None))
        if inst:
            rows.append(("Location", break_path(inst.folder), None))
        rows.append(("Developer", r.repo.split("/")[0], None))
        if r.stars:
            rows.append(("GitHub stars", f"★ {r.stars:,}", None))
        if r.updated:
            rows.append(("Last commit", r.updated, None))
        if r.source:
            rows.append(("Listed by", r.source, None))
        self.rcd_facts.set_facts(rows)
        self.rcd_main.setText("Cancel" if busy else "Update" if update else "Install" if not inst else "Reinstall")
        self.rcd_main.setEnabled(busy or isinstance(latest, tuple))
        motion.set_visible(self.rcd_play, inst is not None, axis="width", animate=animate)
        self.rcd_play.setEnabled(inst is not None and not busy)
        motion.set_visible(self.rcd_more, inst is not None, axis="width", animate=animate)
        motion.set_visible(self.rcd_remove, inst is not None, axis="width", animate=animate)
        self.rcd_remove.setEnabled(not busy)
        mine = rc.is_custom(r)
        motion.set_visible(self.rcd_edit, mine, axis="width", animate=animate)
        motion.set_visible(self.rcd_unlist, mine, axis="width", animate=animate)
        self.rcd_unlist.setEnabled(not busy)
        motion.set_visible(self.rcd_bar, busy, axis="height", animate=animate)
        self._rc_notes_title()

    # ---- latest release -------------------------------------------------------------------------------------
    def _rc_fetch_latest(self, r: rc.Recomp, force: bool = False) -> None:
        store = self.rc_store

        def work() -> None:
            try:
                found = rc.choose_release(rc.releases(r.repo, store, force), r)
                out: object = found if found else f"No download for {rc.platform_key()} in the recent releases. Use the GitHub link."
            except rc.RecompError as exc:
                out = str(exc)
            self.ui_q.put(("recomp_latest", r.id, out))
        threading.Thread(target=work, daemon=True).start()

    def _rc_fetch_about(self, r: rc.Recomp, force: bool = False) -> None:
        store = self.rc_store

        def work() -> None:
            self.ui_q.put(("recomp_about", r.id, rc.project_about(r.repo, store, force, r.description)))
        threading.Thread(target=work, daemon=True).start()

    def _rc_fetch_game(self, r: rc.Recomp, force: bool = False) -> None:
        store = self.rc_store

        def work() -> None:
            try:
                text = rc.launchbox_overview(r, store, force)
            except Exception:  # the card is a nicety: whatever goes wrong, it just stays hidden
                text = ""
            self.ui_q.put(("recomp_game", r.id, text))
        threading.Thread(target=work, daemon=True).start()

    def _rc_set_game(self, text: str) -> None:
        paras = [pp.strip() for pp in text.split("\n\n") if pp.strip()]
        self.rcd_game_toggle.blockSignals(True)
        self.rcd_game_toggle.setChecked(False)  # a new text starts collapsed
        self.rcd_game_toggle.blockSignals(False)
        self.rcd_game.setText("".join(f'<p style="margin: 0 0 8px 0; line-height: 155%;">{html.escape(pp)}</p>' for pp in paras))
        self.rcd_game_card.setVisible(bool(paras))
        self._rc_game_overflow(self.rcd_game.overflows)

    def _rc_game_overflow(self, over: bool) -> None:
        self.rcd_game_toggle.setVisible(over)
        on = self.rcd_game_toggle.isChecked()
        self.rcd_game_toggle.setText("Show less" if on else "Show more")
        self.rcd_game_toggle.setIcon(icons.icon("chevron-up" if on else "chevron-down", T.fg3, 14))

    def _rc_toggle_game(self, open_: bool) -> None:
        self.rcd_game.set_expanded(open_)
        self._rc_game_overflow(self.rcd_game.overflows)

    def _rc_check_updates(self, quiet: bool = False) -> None:
        """Ask GitHub about every installed project (and refresh the open one)."""
        todo = [r for r in self._rc_catalog if self.rc_store.installed(r.id)]
        if self._rc_current and self._rc_current in self._rc_by_id and self._rc_by_id[self._rc_current] not in todo:
            todo.append(self._rc_by_id[self._rc_current])
        if not todo:
            if not quiet:
                self.rc_status.setText("Nothing installed yet, so nothing to update.")
            return
        if not quiet:
            self.rc_status.setText(f"Checking {len(todo)} project(s) for updates...")
        for r in todo:
            self._rc_fetch_latest(r, force=not quiet)

    # ---- install / update / play ----------------------------------------------------------------------------
    def _rc_main_action(self) -> None:
        r = self._rc_by_id.get(self._rc_current)
        if r is None:
            return
        if r.id in self._rc_busy:
            self._rc_busy[r.id].set()
            self.rcd_msg.setText("Cancelling...")
            return
        latest = self._rc_latest.get(r.id)
        if not isinstance(latest, tuple):
            return
        rel, asset = latest
        if self._rc_owned and not self._rc_owned.get(r.id) and self._rc_needs_game(r) and msgbox.question(
                self, "Install without the game?",
                f"Your library check hasn't found {r.game}.\n\n{r.name} needs your own copy of the game to run. Install it anyway?"
        ) != QMessageBox.StandardButton.Yes:
            return
        cancel = threading.Event()
        self._rc_busy[r.id] = cancel
        self.rcd_bar.setRange(0, 0)
        self.rcd_msg.setText("Starting...")
        self._rc_refresh_detail()
        store = self.rc_store

        def progress(text: str, done: int, total: int) -> None:
            self.ui_q.put(("recomp_prog", r.id, text, done, total))

        def work() -> None:
            try:
                inst = rc.Installer(store, progress, cancel).install(r, rel, asset)
                self.ui_q.put(("recomp_done", r.id, inst, ""))
            except rc.Cancelled:
                self.ui_q.put(("recomp_done", r.id, None, "Cancelled."))
            except rc.RecompError as exc:
                self.ui_q.put(("recomp_done", r.id, None, str(exc)))
            except Exception as exc:  # whatever else goes wrong is shown, not lost in a thread
                self.ui_q.put(("recomp_done", r.id, None, f"{type(exc).__name__}: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _rc_play(self) -> None:
        inst = self.rc_store.installed(self._rc_current)
        if inst is None:
            return
        try:
            rc.launch(inst)
            self.rcd_msg.setText("Started.")
        except rc.RecompError as exc:
            self.rcd_msg.setText(str(exc))

    def _rc_open_folder(self) -> None:
        if inst := self.rc_store.installed(self._rc_current):
            QDesktopServices.openUrl(QUrl.fromLocalFile(inst.folder))

    def _rc_show_rom(self) -> None:
        if (own := self._rc_owned.get(self._rc_current)) and own.path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(own.path).parent)))

    def _rc_uninstall(self) -> None:
        r = self._rc_by_id.get(self._rc_current)
        if r is None or r.id in self._rc_busy:
            return
        if msgbox.question(self, "Uninstall", f"Remove {r.name} from this PC?\n\nIts folder is deleted, including any saves or settings "
                                                   "the game keeps there. Your own game files are not touched.") != QMessageBox.StandardButton.Yes:
            return
        rc.uninstall(self.rc_store, r)
        self.rcd_msg.setText("Uninstalled.")
        self._rc_refresh_detail()

    # ---- library check --------------------------------------------------------------------------------------
    def _rc_library_entries(self) -> list:
        st = self.org.get("report")
        return [e for f in (st.findings if st else []) for e in f.data]

    def _rc_check_library(self) -> None:
        entries = self._rc_library_entries()
        if not entries:
            self.rc_status.setText("Run the Library Report first (Organise Library > Library Report), then come back.")
            return
        if not any(getattr(e, "path", "") for e in entries):
            self.rc_status.setText("That report predates this feature. Scan again, then press Check my library.")
            return
        self.rc_check_btn.setEnabled(False)
        self._rc_check_stop.clear()
        catalog, stop = list(self._rc_catalog), self._rc_check_stop

        def work() -> None:
            try:
                found = rc.match_library(catalog, entries, stop, lambda t: self.ui_q.put(("recomp_note", t)))
            except Exception as exc:
                self.ui_q.put(("recomp_owned", None, f"Check failed: {exc}"))
                return
            self.ui_q.put(("recomp_owned", found, ""))
        threading.Thread(target=work, daemon=True).start()
        self.rc_status.setText("Checking your library...")

    # ---- events from the worker threads ---------------------------------------------------------------------
    def _rc_event(self, ev: tuple) -> None:
        name = ev[0]
        if name == "recomp_catalog":
            if ev[1] is None:
                self.rc_status.setText(ev[2])
            else:
                self._rc_apply_catalog(ev[1])
            return
        if name == "recomp_about":
            self._rc_about[ev[1]] = ev[2]
            if ev[1] == self._rc_current:
                self._rc_set_about(ev[2] or "The developers haven't written a description.")
            return
        if name == "recomp_game":
            self._rc_game[ev[1]] = ev[2]
            if ev[1] == self._rc_current:
                self._rc_set_game(ev[2])
            return
        if name == "recomp_banner":
            _n, key, path = ev
            self._rc_banner_busy.discard(key)
            if path and key == self._rc_banner_key and self._rc_current:  # only for the game whose page is open
                self._rc_set_banner(QPixmap(path), True)
            return
        if name == "recomp_art":
            self._rc_set_art(ev[1], ev[2])
        elif name == "recomp_latest":
            self._rc_latest[ev[1]] = ev[2]
            if ev[1] == self._rc_current:
                self._rc_refresh_detail()
            self._rc_refresh_item(ev[1])
        elif name == "recomp_prog":
            _n, rid, text, done, total = ev
            if rid == self._rc_current:
                self.rcd_msg.setText(text)
                self.rcd_bar.setRange(0, total if total else 0)
                self.rcd_bar.setValue(done if total else 0)
        elif name == "recomp_done":
            _n, rid, inst, error = ev
            self._rc_busy.pop(rid, None)
            if rid == self._rc_current:
                self.rcd_msg.setText(error or f"Installed {inst.version}. Press Play to start it.")
                self._rc_refresh_detail()
            self._rc_refresh_item(rid)
        elif name == "recomp_note":
            self.rc_status.setText(ev[1])
        elif name == "recomp_owned":
            self.rc_check_btn.setEnabled(True)
            if ev[1] is None:
                self.rc_status.setText(ev[2])
                return
            self._rc_owned = ev[1]
            self.rc_store.set_owned(ev[1])
            self._rc_fill()
            self._rc_refresh_detail()
            if not ev[1]:
                self.rc_status.setText("No game in your library matches a project in the catalog.")

    def _rc_refresh_item(self, rid: str) -> None:
        r = self._rc_by_id.get(rid)
        if r is None:
            return
        for i in range(self.rc_list.count()):
            it = self.rc_list.item(i)
            if it.data(ID_ROLE) == rid:
                it.setData(STATE_ROLE, self._rc_state(r))
        self.rc_list.viewport().update()
