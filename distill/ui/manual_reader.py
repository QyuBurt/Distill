"""The manual reader: a game's PDF manual shown as a book on a desk. Pages are laid out as spreads, the cover alone on the right, and each
page can be picked up by its outer edge or corner and turned. Letting go past a third of the way (or flicking) finishes the turn; letting go
sooner lays the page back. A click on the outer part of a page, the arrow keys or the wheel turn it by themselves, and the corners lift a
little under the pointer to show they can be picked up.

Pages are drawn by Qt's own PDF module (QtPdf) at the size they are shown, kept in a small cache, and the next and previous spreads are
drawn ahead while you read. The geometry of the book and of the curl is in curlmath.py (no Qt, tested on its own); this file only
paints it.
"""
from __future__ import annotations

import math

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QElapsedTimer, QPointF, QRectF, QSize, Qt, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIntValidator, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from .. import games as gm
from . import curlmath as cm
from . import pagesplit as ps
from . import motion, scrollmath
from .albumview import SCRIM, SCRIM_ALPHA, SCRIM_ALPHA_HOVER, IconButton
from .frameless import FramelessDialog, hairline
from .theme import T
from .widgets import hint

PAPER = QColor("#f3ecdc")  # the page's own colour: a PDF's white is multiplied onto it, so print stays dark and the white warms
MARGIN = 30
GRAB = 0.5  # the outer half of a page can be picked up
CORNER = 0.22  # within this share of a page's width and height of a corner, the corner lifts under the pointer
SETTLE_OMEGA = 12.0  # how briskly a let-go page finishes its turn or lays back (1/s)
HINT_OMEGA = 20.0
CACHE_MAX = 14
CLICK_SLOP = 6


class _Turn:
    """A leaf in motion. Positions are in the frame of the page being turned (see curlmath)."""

    def __init__(self, side: int, origin: tuple[float, float], start: int, target: int):
        self.side, self.origin, self.start, self.target = side, origin, start, target
        self.pull = origin
        self.vel = (0.0, 0.0)
        self.goal = origin
        self.mode = "drag"  # 'drag' (the pointer holds it), 'settle' (let go, finishing), 'hint' (a corner lifting under the pointer)
        self.done = False  # for 'settle': does it end turned (True) or laid back (False)


class BookView(QWidget):
    spreadChanged = Signal(int)
    coverChosen = Signal(int, str)  # the player clicked the page that is the front cover: (PDF page, part)
    pagesChanged = Signal()  # the pages were re-cut (two-page scans split or joined): the page count is different
    fullScreenRequested = Signal()  # F or F11

    def __init__(self, path: str | Path, parent: QWidget | None = None, split: str = "auto", cover: ps.Unit | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(420, 320)
        self.doc = QPdfDocument(self)
        self.doc.load(str(path))
        self.pdf_pages = self.doc.pageCount() if self.doc.status() == QPdfDocument.Status.Ready else 0
        self._sizes = [(self.doc.pagePointSize(i).width(), self.doc.pagePointSize(i).height()) for i in range(self.pdf_pages)]
        self.split_mode = split  # 'auto', 'on' or 'off': scans of open books have two pages on each PDF page, shown here as two pages
        self.cover = cover  # the page the player marked as the front cover (None: the first one)
        self.picking = False  # waiting for a click on the page that is the cover
        self._units: list[ps.Unit] = []
        self.pages, self.aspect = 0, 0.72
        self._cut()
        self.spread = 0
        self.rtl = False
        self._turn: _Turn | None = None
        self._cache: OrderedDict[tuple[int, int, int], QPixmap] = OrderedDict()
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self._wheel = 0
        self._slide, self._slide_v = 0.0, 0.0  # the book gliding to the middle after a cover opens or shuts (pixels left to go)
        self._press: tuple[QPointF, int, float] | None = None  # (where, side, when) of a press, to tell a click from a drag
        self._last_move: tuple[float, float, float] | None = None  # (x, y, when) of the pointer, for its speed
        self._prefetch_todo: list[int] = []

    # ---- the book ---------------------------------------------------------------------------------------------
    def _cut(self) -> None:
        """Work out the pages to show from the PDF's pages (a wide scan of two pages becomes a left and a right page)."""
        self._units = ps.with_cover(ps.page_units(self._sizes, self.split_mode), self.cover)
        self.pages = len(self._units)
        self.aspect = ps.book_aspect(self._sizes, self._units)

    def set_cover(self, cover: ps.Unit | None) -> None:
        """Make a page the front cover (None: back to the first page). The book opens at it."""
        self._stop_motion()
        self.cover = cover
        self._cut()
        self._cache.clear()
        self.spread = 0
        self.pagesChanged.emit()
        self._changed()

    def pick_cover(self, on: bool = True) -> None:
        """Wait for the player to click the page that is the front cover (Esc gives up)."""
        self.picking = on and self.ok
        self.setCursor(Qt.CursorShape.PointingHandCursor if self.picking else Qt.CursorShape.ArrowCursor)
        self.update()

    def _pick_at(self, pos: QPointF) -> None:
        spine, top, w, h = self._layout()
        side = self._side_at(pos)
        page = self.pages_shown()[0 if side < 0 else 1]
        inside = (spine <= pos.x() <= spine + w) if side > 0 else (spine - w <= pos.x() <= spine)
        if page is None or not inside or not top <= pos.y() <= top + h:
            return
        pdf_page, part = self._units[page]
        self.pick_cover(False)
        self.coverChosen.emit(pdf_page, part)

    def is_split(self) -> bool:
        return any(part != "whole" for _i, part in self._units)

    def set_split(self, mode: str) -> None:
        """Cut the pages again ('auto', 'on' or 'off'), staying on the same PDF page."""
        if mode == self.split_mode or not self.pdf_pages:
            return
        left, right = self.pages_shown()
        page = right if right is not None else left
        here = self._units[page][0] if page is not None and page < len(self._units) else 0
        self._stop_motion()
        self.split_mode = mode
        self._cut()
        self._cache.clear()
        self.spread = cm.spread_of(ps.first_unit_of(self._units, here), self.pages)
        self.pagesChanged.emit()
        self._changed()

    @property
    def ok(self) -> bool:
        return self.pages > 0

    def spread_total(self) -> int:
        return cm.spread_count(self.pages)

    def _spread_pages(self, spread: int) -> tuple[int | None, int | None]:
        """(left, right) pages of a spread. A right-to-left book is the mirror image: its cover is on the left and its pages run the other way."""
        left, right = cm.spread_pages(spread, self.pages)
        return (right, left) if self.rtl else (left, right)

    def _shift(self, spread: int) -> float:
        return -cm.book_shift(spread, self.pages) if self.rtl else cm.book_shift(spread, self.pages)

    def pages_shown(self) -> tuple[int | None, int | None]:
        return self._spread_pages(self.spread)

    def label(self) -> str:
        left, right = self.pages_shown()
        shown = [p + 1 for p in (left, right) if p is not None]
        what = " - ".join(str(p) for p in shown) if len(shown) == 2 else str(shown[0]) if shown else "-"
        return f"Page {what} of {self.pages}" if len(shown) < 2 else f"Pages {what} of {self.pages}"

    def set_rtl(self, on: bool) -> None:
        self.rtl = on
        self.update()

    def go_to_page(self, page: int) -> None:
        """Jump straight to the spread with a page (0-based), without turning."""
        self._stop_motion()
        self.spread = cm.spread_of(page, self.pages)
        self._changed()

    def go_to_spread(self, spread: int) -> None:
        self._stop_motion()
        self.spread = max(0, min(spread, self.spread_total() - 1))
        self._changed()

    def _stop_motion(self) -> None:
        self._turn = None
        self._slide = self._slide_v = 0.0
        self._timer.stop()

    def _changed(self) -> None:
        self.spreadChanged.emit(self.spread)
        self.update()
        self._queue_prefetch()

    def flip(self, direction: int) -> None:
        """Turn one leaf by itself, forwards (+1) or backwards (-1)."""
        if self._turn is not None or not self.ok:
            return
        target = self.spread + direction
        if not 0 <= target < self.spread_total():
            return
        side = cm.reading_order(direction, self.rtl)  # forwards lifts the right-hand page (the left-hand one in a right-to-left book)
        w, h = self._page_size()
        turn = _Turn(side, (w, h), self.spread, target)
        turn.pull = cm.hint_pull(turn.origin, w, h, 0.6)
        turn.mode, turn.done = "settle", True
        turn.goal = cm.turn_target(turn.origin, w, True)
        self._turn = turn
        self._start_clock()

    # ---- geometry ---------------------------------------------------------------------------------------------
    def _page_size(self) -> tuple[float, float]:
        avail_w, avail_h = max(80.0, self.width() - 2 * MARGIN), max(80.0, self.height() - 2 * MARGIN)
        w = min(avail_w / 2, avail_h * self.aspect)
        return w, w / self.aspect

    def _layout(self) -> tuple[float, float, float, float]:
        """(spine x, top y, page width, page height). While a leaf is held the spine stays where it was, so the page follows the pointer
        exactly; when a cover opens or shuts the book then glides to the middle."""
        w, h = self._page_size()
        shift = self._shift(self._turn.start if self._turn is not None else self.spread)
        return self.width() / 2 - shift * w + self._slide, (self.height() - h) / 2, w, h

    def _turn_progress(self, w: float) -> float:
        """0..1 through the turn, from where the grabbed point is."""
        t = self._turn
        if t is None:
            return 0.0
        x = cm.clamp_pull(t.origin, t.pull, w, self._page_size()[1])[0]
        return max(0.0, min(1.0, (w - x) / (2 * w)))

    def _to_frame(self, pos: QPointF, side: int) -> tuple[float, float]:
        spine, top, _w, _h = self._layout()
        return (pos.x() - spine) * side, pos.y() - top

    def _side_has_page(self, side: int) -> bool:
        left, right = self.pages_shown()
        return (right if side > 0 else left) is not None

    def _turnable(self, side: int) -> int | None:
        """The spread a turn from this side's page would go to, or None when that page can't be turned."""
        if not self._side_has_page(side):
            return None
        target = self.spread + cm.reading_order(side, self.rtl)
        return target if 0 <= target < self.spread_total() else None

    def _side_at(self, pos: QPointF) -> int:
        spine = self._layout()[0]
        return 1 if pos.x() >= spine else -1

    # ---- pictures of the pages ----------------------------------------------------------------------------------
    def _pixmap(self, page: int, w: float, h: float) -> QPixmap:
        dpr = self.devicePixelRatioF()
        pw, ph = max(2, round(w * dpr)), max(2, round(h * dpr))
        key = (page, pw, ph)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        out = QPixmap(pw, ph)
        out.setDevicePixelRatio(1.0)
        out.fill(PAPER)
        pdf_page, part = self._units[page]
        size = self.doc.pagePointSize(pdf_page)
        if size.width() > 0 and size.height() > 0:
            halves = 2 if part != "whole" else 1  # a half of a wide scan is drawn from the whole page, cut in two
            k = min(pw / (size.width() / halves), ph / size.height())
            rw, rh = max(1, round(size.width() / halves * k)), max(1, round(size.height() * k))
            image = self.doc.render(pdf_page, QSize(rw * halves, rh))
            if not image.isNull():
                if halves == 2:
                    image = image.copy(0 if part == "left" else image.width() // 2, 0, image.width() // 2, image.height())
                p = QPainter(out)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
                p.drawImage((pw - rw) // 2, (ph - rh) // 2, image)
                p.end()
        out.setDevicePixelRatio(dpr)
        self._cache[key] = out
        while len(self._cache) > CACHE_MAX:
            self._cache.popitem(last=False)
        return out

    def _queue_prefetch(self) -> None:
        wanted: list[int] = []
        for s in (self.spread + 1, self.spread - 1):
            if 0 <= s < self.spread_total():
                wanted += [p for p in cm.spread_pages(s, self.pages) if p is not None]
        self._prefetch_todo = wanted
        QTimer.singleShot(40, self._prefetch_one)

    def _prefetch_one(self) -> None:
        if not self._prefetch_todo or self._turn is not None and self._turn.mode == "drag":
            return
        w, h = self._page_size()
        self._pixmap(self._prefetch_todo.pop(0), w, h)
        if self._prefetch_todo:
            QTimer.singleShot(20, self._prefetch_one)

    # ---- painting -----------------------------------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        self._paint_book(event)
        if self.picking:  # a note over the top of the book
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            text = "Click the page that is the front cover  (Esc to cancel)"
            box = QRectF(0, 0, self.fontMetrics().horizontalAdvance(text) + 36, 34)
            box.moveCenter(QPointF(self.width() / 2, 28))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 190))
            p.drawRoundedRect(box, 17, 17)
            p.setPen(QColor("#ffffff"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_book(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), T.bg.darker(112))
        if not self.ok:
            return
        spine, top, w, h = self._layout()
        t = self._turn
        left, right = self.pages_shown()
        if t is None:
            self._draw_stacks(p, spine, top, w, h, left, right)
            self._draw_page(p, left, -1, spine, top, w, h)
            self._draw_page(p, right, 1, spine, top, w, h)
            return
        target_left, target_right = self._spread_pages(t.target)
        shown_l = left if t.side > 0 else target_left  # the side that isn't turning keeps its page; the turning side shows what is under the leaf
        shown_r = target_right if t.side > 0 else right
        self._draw_stacks(p, spine, top, w, h, shown_l, shown_r)
        self._draw_page(p, shown_l, -1, spine, top, w, h)
        self._draw_page(p, shown_r, 1, spine, top, w, h)
        front = left if t.side < 0 else right  # the leaf's front is the current page on its side; its back is the target spread's page on the other
        back = target_right if t.side < 0 else target_left
        self._draw_leaf(p, front, back, t, spine, top, w, h)

    def _draw_stacks(self, p: QPainter, spine: float, top: float, w: float, h: float, left: int | None, right: int | None) -> None:
        """The thickness of the pages on each side of the book: more on the side with more pages still to go."""
        total = max(1, self.pages)
        for side, page in ((-1, left), (1, right)):
            if page is None:
                continue
            behind = (page / total) if side < 0 else (1 - page / total)
            depth = 1.5 + 8.0 * behind
            edge = spine + side * w
            shade = QColor(PAPER).darker(125)
            p.fillRect(QRectF(min(edge, edge + side * depth), top + depth * 0.5, depth, h), shade)
            for i in range(1, int(depth)):
                line = QColor(PAPER).darker(112 + 6 * (i % 2))
                x = edge + side * i
                p.fillRect(QRectF(x, top + depth * 0.5, 1, h), line)
        if left is not None or right is not None:
            soft = QLinearGradient(0, top + h, 0, top + h + 14)
            soft.setColorAt(0, QColor(0, 0, 0, 70))
            soft.setColorAt(1, QColor(0, 0, 0, 0))
            x0 = spine - (w if left is not None else 0)
            x1 = spine + (w if right is not None else 0)
            p.fillRect(QRectF(x0 + 6, top + h, x1 - x0 - 6, 14), soft)

    def _draw_page(self, p: QPainter, page: int | None, side: int, spine: float, top: float, w: float, h: float) -> None:
        if page is None:
            return
        rect = QRectF(spine if side > 0 else spine - w, top, w, h)
        pm = self._pixmap(page, w, h)
        p.drawPixmap(rect, pm, QRectF(pm.rect()))
        gutter = QLinearGradient(spine, 0, spine + side * w * 0.12, 0)  # the page curves down into the spine
        gutter.setColorAt(0, QColor(0, 0, 0, 55))
        gutter.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(rect, gutter)

    def _draw_leaf(self, p: QPainter, front: int | None, back: int | None, t: _Turn, spine: float, top: float, w: float, h: float) -> None:
        c = cm.curl(t.origin, t.pull, w, h)
        to_widget = QTransform(t.side, 0, 0, 1, spine, top)  # the frame the maths is in, laid onto the screen (mirrored for a left-hand page)
        mirror = QTransform(-1, 0, 0, 1, w, 0)
        front_to_frame = QTransform() if t.side > 0 else mirror
        back_to_frame = mirror if t.side > 0 else QTransform()
        page_rect = QRectF(0, 0, w, h)

        def poly(points) -> QPolygonF:
            return QPolygonF([QPointF(x, y) for x, y in points])

        # what is still flat
        if c.flat:
            p.save()
            p.setTransform(to_widget)
            path = QPainterPath()
            path.addPolygon(poly(c.flat))
            path.closeSubpath()
            p.setClipPath(path)
            p.setTransform(front_to_frame * to_widget)
            if front is not None:
                pm = self._pixmap(front, w, h)
                p.drawPixmap(page_rect, pm, QRectF(pm.rect()))
                p.setTransform(to_widget)
                gutter = QLinearGradient(0, 0, w * 0.12, 0)  # (in the frame, where the spine is always at x = 0)
                gutter.setColorAt(0, QColor(0, 0, 0, 55))
                gutter.setColorAt(1, QColor(0, 0, 0, 0))
                p.fillRect(page_rect, gutter)
            p.restore()
        if c.fold is None:
            return
        d = c.normal
        mid = ((c.fold[0][0] + c.fold[1][0]) / 2, (c.fold[0][1] + c.fold[1][1]) / 2)
        # How much shading the leaf carries: none while it lies flat (just grabbed, or just landed), most halfway over. It eases in
        # and out with the fold's travel across the page, so nothing pops on when the turn starts or off when the page settles.
        travel = max(0.0, min(1.0, (w - (t.origin[0] + t.pull[0]) / 2) / w))  # 0 at the outer edge, 1 at the spine
        k = math.sin(math.pi * travel) ** 0.5
        # the shadow the fold throws on the page and on what lies beneath it
        reach = max(18.0, 0.16 * w)
        p.save()
        p.setTransform(to_widget)
        box = QPainterPath()
        box.addRect(page_rect)
        p.setClipPath(box)
        shade = QLinearGradient(mid[0] - d[0] * reach, mid[1] - d[1] * reach, mid[0] + d[0] * reach, mid[1] + d[1] * reach)
        shade.setColorAt(0.0, QColor(0, 0, 0, 0))
        shade.setColorAt(0.5, QColor(0, 0, 0, round(95 * k)))
        shade.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(page_rect, shade)
        p.restore()
        # the lifted part, seen from behind where it has landed
        if c.landed:
            p.save()
            p.setTransform(to_widget)
            path = QPainterPath()
            path.addPolygon(poly(c.landed))
            path.closeSubpath()
            p.setClipPath(path)
            m11, m12, m21, m22, dx, dy = c.reflect
            swing = QTransform(m11, m12, m21, m22, dx, dy)
            p.setTransform(back_to_frame * swing * to_widget)
            if back is not None:
                pm = self._pixmap(back, w, h)
                p.drawPixmap(page_rect, pm, QRectF(pm.rect()))
                # the same curve into the spine a resting page has (its spine edge is on the right for a left-hand page), so the page
                # does not gain it only when the turn ends
                near, far_x = (w, w * 0.88) if t.side > 0 else (0.0, w * 0.12)
                gutter = QLinearGradient(near, 0, far_x, 0)
                gutter.setColorAt(0, QColor(0, 0, 0, 55))
                gutter.setColorAt(1, QColor(0, 0, 0, 0))
                p.fillRect(page_rect, gutter)
            else:
                p.fillRect(page_rect, PAPER.darker(108))
            p.setTransform(to_widget)
            far = max(18.0, max(abs((x - mid[0]) * d[0] + (y - mid[1]) * d[1]) for x, y in c.landed))
            curve = QLinearGradient(mid[0], mid[1], mid[0] + d[0] * far, mid[1] + d[1] * far)
            curve.setColorAt(0.0, QColor(0, 0, 0, round(80 * k)))
            curve.setColorAt(0.08, QColor(255, 255, 255, round(55 * k)))  # a slight sheen along the bend, fading with the shading
            curve.setColorAt(0.5, QColor(255, 255, 255, 0))
            curve.setColorAt(1.0, QColor(0, 0, 0, round(45 * k)))
            p.fillRect(QRectF(-4 * w, -4 * h, 9 * w, 9 * h), curve)
            p.restore()

    # ---- the pointer --------------------------------------------------------------------------------------------
    def _grab_origin(self, fx: float, fy: float, w: float, h: float) -> tuple[float, float]:
        """Where a page is held: on its outer edge at the pointer's height, or at the nearest corner when that is close."""
        snap = 0.16 * h
        y = 0.0 if fy < snap else h if fy > h - snap else max(0.0, min(h, fy))
        return w, y

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self.ok:
            return
        if self.picking:
            self._pick_at(event.position())
            return
        self.setFocus()
        pos = event.position()
        side = self._side_at(pos)
        w, h = self._page_size()
        fx, fy = self._to_frame(pos, side)
        target = self._turnable(side)
        if target is None or not (GRAB * w <= fx <= w + 12) or not (-12 <= fy <= h + 12):
            return
        if self._turn is not None and self._turn.mode == "settle":
            return
        turn = _Turn(side, self._grab_origin(fx, fy, w, h), self.spread, target)
        turn.mode = "drag"
        turn.pull = (min(fx, w), fy)  # (so a page that was lifting under the pointer carries on from there)
        self._turn = turn
        self._press = (pos, side, event.timestamp() / 1000.0)
        self._last_move = (fx, fy, event.timestamp() / 1000.0)
        turn.vel = (0.0, 0.0)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        t = self._turn
        if t is not None and t.mode == "drag":
            fx, fy = self._to_frame(pos, t.side)
            now = event.timestamp() / 1000.0
            if self._last_move is not None and now > self._last_move[2]:
                dt = now - self._last_move[2]
                vx, vy = (fx - self._last_move[0]) / dt, (fy - self._last_move[1]) / dt
                t.vel = (0.6 * t.vel[0] + 0.4 * vx, 0.6 * t.vel[1] + 0.4 * vy)  # smoothed, so one jumpy event doesn't decide a flick
            self._last_move = (fx, fy, now)
            t.pull = (fx, fy)
            self.update()
            return
        self._hover(pos)

    def _hover(self, pos: QPointF) -> None:
        """Without a button down: the hand cursor over what can be grabbed, and a corner lifting when the pointer is near one."""
        side = self._side_at(pos)
        w, h = self._page_size()
        fx, fy = self._to_frame(pos, side)
        target = self._turnable(side)
        over = target is not None and GRAB * w <= fx <= w and 0 <= fy <= h
        self.setCursor(Qt.CursorShape.OpenHandCursor if over else Qt.CursorShape.ArrowCursor)
        near = over and fx >= w * (1 - CORNER) and (fy <= h * CORNER or fy >= h * (1 - CORNER))
        t = self._turn
        if near and target is not None:
            corner = (w, 0.0 if fy < h / 2 else h)
            if t is None:
                t = _Turn(side, corner, self.spread, target)
                t.mode = "hint"
                self._turn = t
            if t.mode == "hint" and t.origin == corner and t.side == side:
                t.goal = cm.hint_pull(corner, w, h)
                self._start_clock()
                return
        if t is not None and t.mode == "hint":
            t.goal = t.origin  # lays back down; the timer drops it when it is flat
            self._start_clock()

    def leaveEvent(self, event) -> None:
        t = self._turn
        if t is not None and t.mode == "hint":
            t.goal = t.origin
            self._start_clock()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        t = self._turn
        if event.button() != Qt.MouseButton.LeftButton or t is None or t.mode != "drag":
            return
        w, h = self._page_size()
        moved = 0.0
        if self._press is not None:
            d = event.position() - self._press[0]
            moved = (d.x() ** 2 + d.y() ** 2) ** 0.5
        if moved < CLICK_SLOP:  # a click: lift the corner and let the page go over by itself
            t.pull = cm.hint_pull(t.origin, w, h, 0.6)
            t.vel = (0.0, 0.0)
            t.done = True
        else:
            progress = self._turn_progress(w)
            t.done = cm.completes(progress, t.vel[0], w)
        t.mode = "settle"
        t.goal = cm.turn_target(t.origin, w, t.done)
        self._press = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._start_clock()
        self.update()

    def wheelEvent(self, event) -> None:
        self._wheel += event.angleDelta().y()
        if abs(self._wheel) >= 120:
            self.flip(-1 if self._wheel > 0 else 1)
            self._wheel = 0
        event.accept()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if self.picking and key == Qt.Key.Key_Escape:
            self.pick_cover(False)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_Space):
            self.flip(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp):
            self.flip(-1)
        elif key == Qt.Key.Key_Home:
            self.go_to_spread(0)
        elif key == Qt.Key.Key_End:
            self.go_to_spread(self.spread_total() - 1)
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.fullScreenRequested.emit()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._queue_prefetch()

    # ---- motion -------------------------------------------------------------------------------------------------
    def _start_clock(self) -> None:
        if not self._timer.isActive():
            self._clock.start()
            self._timer.start()

    def _tick(self) -> None:
        dt = min(0.05, self._clock.restart() / 1000.0)
        t = self._turn
        if t is not None and t.mode == "drag":
            self._timer.stop()  # the pointer is doing the moving
            return
        if t is None:
            self._slide, self._slide_v = scrollmath.damp_step(self._slide, self._slide_v, 0.0, dt, SETTLE_OMEGA)
            if abs(self._slide) < 0.5 and abs(self._slide_v) < 12:
                self._slide = self._slide_v = 0.0
                self._timer.stop()
            self.update()
            return
        omega = HINT_OMEGA if t.mode == "hint" else SETTLE_OMEGA
        x, vx = scrollmath.damp_step(t.pull[0], t.vel[0], t.goal[0], dt, omega)
        y, vy = scrollmath.damp_step(t.pull[1], t.vel[1], t.goal[1], dt, omega)
        t.pull, t.vel = (x, y), (vx, vy)
        if abs(x - t.goal[0]) < 0.5 and abs(y - t.goal[1]) < 0.5 and abs(vx) < 12 and abs(vy) < 12:
            if t.mode == "settle" and t.done:  # the leaf has landed: the book now shows the next spread, gliding to the middle if a cover moved
                w = self._page_size()[0]
                self._slide, self._slide_v = (self._shift(t.target) - self._shift(t.start)) * w, 0.0
                self.spread, self._turn = t.target, None
                self._changed()
                self._clock.start()
                return  # (the timer carries on while the book glides)
            if t.mode == "settle" or t.goal == t.origin:  # laid back, or a lifted corner back down
                self._turn = None
            self._timer.stop()
        self.update()


class PageBox(QLineEdit):
    """The page number, on its own: no arrows, no words. Type a number and press Enter, or scroll the wheel over it (up for later pages, down
    for earlier ones). (A plain line edit rather than a spin box: a spin box keeps room for its arrows, which left no room for the digits.)"""

    committed = Signal(int)  # a number was typed and accepted
    stepped = Signal(int)  # +1 / -1: a wheel notch or an arrow key; the reader moves a spread and then sets the number itself

    def __init__(self):
        super().__init__()
        self._value, self._min, self._max = 1, 1, 1
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(88, 36)
        self.setStyleSheet("QLineEdit { padding: 0 26px 0 8px; }")  # (room on the right for the scroll hint; the app's own padding is for wide fields)
        self.setMouseTracking(True)
        self.setValidator(QIntValidator(1, 999999, self))
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.editingFinished.connect(self._accept)

    def setRange(self, low: int, high: int) -> None:
        self._min, self._max = low, max(low, high)
        self.setValue(self._value)

    def setValue(self, value: int) -> None:
        self._value = max(self._min, min(self._max, int(value)))
        self.setText(str(self._value))

    def value(self) -> int:
        return self._value

    def _accept(self) -> None:
        try:
            typed = int(self.text())
        except ValueError:
            typed = self._value  # nothing usable: back to the page that is showing
        self.setValue(typed)
        self.committed.emit(self._value)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        """The field, then a small hint on its right that it can be scrolled: a mouse with its wheel, between an up and a down chevron."""
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(T.fg2 if self.underMouse() else T.fg3)
        p.setPen(QPen(colour, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = self.width() - 16.0, self.height() / 2
        for sign in (-1, 1):  # chevrons above and below
            y = cy + sign * 11.5
            p.drawPolyline(QPolygonF([QPointF(cx - 3.2, y + sign * -1.6), QPointF(cx, y + sign * 1.6), QPointF(cx + 3.2, y + sign * -1.6)]))
        p.drawRoundedRect(QRectF(cx - 5, cy - 7, 10, 14), 5, 5)  # the mouse
        p.drawLine(QPointF(cx, cy - 4), QPointF(cx, cy - 1.5))  # and its wheel

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.stepped.emit(1 if event.key() == Qt.Key.Key_Up else -1)
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:  # (a line edit has no wheel of its own)
        up = event.angleDelta().y()
        if up:
            self.stepped.emit(1 if up > 0 else -1)  # a spread shows two pages, so a step is a whole spread
        event.accept()


OVERLAY_BTN = 32  # the small round buttons in the book's top left corner


def _overlay_painter(btn: QPushButton) -> tuple[QPainter, bool, QColor]:
    """A painter for one of the three small buttons over the book: a soft dark round scrim, with the glyph (drawn on a 46 x 40 sheet by the
    button) scaled down to sit in it. White, lighter when the pointer is over it; the accent colour when it is on. Returns (painter, on, colour)."""
    p = QPainter(btn)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    on, over = btn.isChecked(), btn.underMouse()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(SCRIM[0], SCRIM[1], SCRIM[2], SCRIM_ALPHA_HOVER if over else SCRIM_ALPHA))
    p.drawEllipse(QRectF(btn.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
    p.translate(btn.width() / 2, btn.height() / 2)
    p.scale(0.72, 0.72)
    p.translate(-23.0, -20.0)
    colour = QColor(T.accent_text) if on else QColor(255, 255, 255, 255 if over else 205)
    return p, on, colour


class SplitPagesButton(QPushButton):
    """A wide page with a line down the middle, for 'each PDF page is two pages side by side'. On: the wide pages are cut in two and shown
    as separate pages. It starts on or off by itself (see pagesplit.looks_like_spreads); clicking it is the player's say."""

    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(OVERLAY_BTN, OVERLAY_BTN)
        self.setStyleSheet("border: none; background: transparent;")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Split two-page scans")
        self.setToolTip("Some scanned manuals have two pages side by side on every PDF page. When this is on, those are shown as two separate "
                        "pages. It is switched on by itself when the manual looks like that; click to change it.")

    def paintEvent(self, event) -> None:
        p, on, colour = _overlay_painter(self)
        cx = 23.0  # (drawn on a 46 x 40 sheet, scaled down by _overlay_painter)
        sheet = QRectF(cx - 12, 9, 24, 17)
        p.setPen(QPen(colour, 2.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(sheet, 2.5, 2.5)
        if on:  # cut in two, the halves drawn apart
            p.setBrush(QColor(colour.red(), colour.green(), colour.blue(), 50))
            p.drawRoundedRect(QRectF(sheet.left(), sheet.top(), 10, sheet.height()), 2.5, 2.5)
            p.drawRoundedRect(QRectF(sheet.right() - 10, sheet.top(), 10, sheet.height()), 2.5, 2.5)
        else:
            p.drawLine(QPointF(cx, sheet.top() + 1), QPointF(cx, sheet.bottom() - 1))
        y = 32.0  # the arrows under it point outwards when split
        d = 1 if on else -1
        p.drawLine(QPointF(cx - 6 * d, y), QPointF(cx - 10 * d, y))
        p.drawLine(QPointF(cx + 6 * d, y), QPointF(cx + 10 * d, y))
        if on:
            for s in (-1, 1):
                p.drawLine(QPointF(cx + 10 * s, y), QPointF(cx + 7 * s, y - 2.5))
                p.drawLine(QPointF(cx + 10 * s, y), QPointF(cx + 7 * s, y + 2.5))


class CoverButton(QPushButton):
    """A star: 'this is the front cover'. Click it, then click the page that is the cover. Lit when a cover has been marked; click it then
    to go back to the first page being the cover."""

    def __init__(self):
        super().__init__()
        self.setCheckable(True)  # checked: a cover is marked
        self.setFixedSize(OVERLAY_BTN, OVERLAY_BTN)
        self.setStyleSheet("border: none; background: transparent;")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Front cover")
        self.refresh()

    def refresh(self) -> None:
        self.setToolTip("You marked a different page as the front cover. Click to put it back to the first page." if self.isChecked() else
                        "Mark the front cover: click here, then click the page that is the front cover. For scans that start with the back "
                        "cover or the inside pages.")
        self.update()

    def paintEvent(self, event) -> None:
        p, on, colour = _overlay_painter(self)
        p.setPen(QPen(colour, 2.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(colour if on else Qt.BrushStyle.NoBrush)
        cx, cy, r = 23.0, 21.0, 11.0
        pts = [QPointF(cx + (r if k % 2 == 0 else r * 0.45) * math.sin(k * math.pi / 5), cy - (r if k % 2 == 0 else r * 0.45) * math.cos(k * math.pi / 5))
               for k in range(10)]
        p.drawPolygon(QPolygonF(pts))


class ReadingOrderButton(QPushButton):
    """A small book with 本 on its cover and an arrow under it, to say which way the book is read. Off: the arrow points right (read left to
    right, the spine on the left). On: it points left and the spine moves to the right, as in a book read from the back."""

    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(OVERLAY_BTN, OVERLAY_BTN)
        self.setStyleSheet("border: none; background: transparent;")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Right to left")
        self.setToolTip("Read from right to left, for books read from the back (many Japanese manuals): the cover is on the left and "
                        "pages turn the other way.")

    def paintEvent(self, event) -> None:
        p, on, colour = _overlay_painter(self)
        cx = 23.0  # (drawn on a 46 x 40 sheet, scaled down by _overlay_painter)
        book = QRectF(cx - 8, 5, 16, 19)
        p.setPen(QPen(colour, 2.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(book, 2.5, 2.5)
        spine = book.right() - 3.2 if on else book.left() + 3.2
        p.drawLine(QPointF(spine, book.top() + 1), QPointF(spine, book.bottom() - 1))
        face = QRectF(book.left() + (1 if on else 4), book.top(), book.width() - 5, book.height())
        font = QFont(self.font())
        font.setPixelSize(10)
        font.setBold(True)
        p.setFont(font)
        p.drawText(face, Qt.AlignmentFlag.AlignCenter, "本")
        y, d = 32.0, (-1 if on else 1)  # the arrow under the book: left when it reads right to left
        p.drawLine(QPointF(cx - 7 * d, y), QPointF(cx + 7 * d, y))
        p.drawLine(QPointF(cx + 7 * d, y), QPointF(cx + 3.5 * d, y - 3))
        p.drawLine(QPointF(cx + 7 * d, y), QPointF(cx + 3.5 * d, y + 3))


class ManualDialog(FramelessDialog):
    """The reader in a window of its own, with the page number, a jump box and a few options under the book. Where the player had got to
    (and whether the book reads right to left) is kept for each game."""

    def __init__(self, parent: QWidget, title: str, pdf: Path, store: gm.GameStore | None = None, key: str = ""):
        super().__init__(parent, f"Manual: {title}", "file", (1180, 800), (760, 560))
        self.store, self.key, self.pdf = store, key, pdf
        saved = store.setting(f"manual_split:{key}", "") if store is not None and key else ""
        cover = ps.parse_cover(store.setting(f"manual_cover:{key}", "")) if store is not None and key else None
        self.book = BookView(pdf, split={"1": "on", "0": "off"}.get(saved, "auto"), cover=cover)  # (nothing saved: it decides by itself)
        outer = self.content
        if not self.book.ok:
            note = hint("This PDF couldn't be opened. It may be damaged or protected with a password.", live=True)
            note.setContentsMargins(24, 24, 24, 12)
            outer.addWidget(note)
            outer.addStretch(1)
            self._external_button(outer)
            return
        outer.addWidget(self.book, 1)
        outer.addWidget(hairline())
        bar = QHBoxLayout()
        bar.setContentsMargins(16, 10, 16, 12)
        bar.setSpacing(8)
        prev = IconButton("chevron-left", "Previous page", 36, 20, filled=True)
        nxt = IconButton("chevron-right", "Next page", 36, 20, filled=True)
        for b in (prev, nxt):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        prev.clicked.connect(lambda: self.book.flip(-1))
        nxt.clicked.connect(lambda: self.book.flip(1))
        self.where = QLabel()
        self.where.setMinimumWidth(130)
        self.where.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.jump = PageBox()
        self.jump.setRange(1, self.book.pages)
        self.jump.setToolTip(f"Page number (of {self.book.pages}): scroll the wheel over it to move through the book, or type a number")
        self.jump.committed.connect(lambda v: self.book.go_to_page(v - 1))
        self.jump.stepped.connect(lambda d: self.book.go_to_spread(self.book.spread + d))
        self.rtl = ReadingOrderButton()
        self.rtl.toggled.connect(self._rtl_changed)
        self.split = SplitPagesButton()
        self.split.setChecked(self.book.is_split())
        self.split.toggled.connect(self._split_changed)
        self.book.pagesChanged.connect(self._pages_changed)
        self.star = CoverButton()
        self.star.setChecked(cover is not None)
        self.star.refresh()
        self.star.clicked.connect(self._star_clicked)
        self.book.coverChosen.connect(self._cover_chosen)
        bar.addWidget(prev)
        bar.addWidget(self.where)
        bar.addWidget(nxt)
        bar.addSpacing(12)
        bar.addWidget(self.jump)
        bar.addStretch(1)
        # In the top right of the book itself: open the PDF elsewhere, and fill the screen with it (the full-screen square is the corner).
        self.open_btn = IconButton("external", "Open in your PDF viewer", 34, 18, filled=True, scrim=True)
        self.open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.pdf))))
        self.full_btn = IconButton("expand", "Full screen (F)", 34, 18, filled=True, scrim=True)
        self.full_btn.clicked.connect(self.toggle_full_screen)
        for b in (self.open_btn, self.full_btn, self.star, self.split, self.rtl):  # (the three small ones sit in the top left)
            b.setParent(self.book)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.book.installEventFilter(self)
        self.book.fullScreenRequested.connect(self.toggle_full_screen)
        outer.addLayout(bar)
        self.book.spreadChanged.connect(self._on_spread)
        if store is not None and key:
            self.rtl.setChecked(store.setting(f"manual_rtl:{key}", "") == "1")
            try:
                self.book.go_to_spread(int(store.setting(f"manual_spread:{key}", "0")))
            except ValueError:
                pass
        self._on_spread(self.book.spread)
        self.book.setFocus()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.book and event.type() == QEvent.Type.Resize and hasattr(self, "full_btn"):
            self._place_overlay()
        return super().eventFilter(obj, event)

    def _place_overlay(self) -> None:
        margin, gap = 12, 8
        x = self.book.width() - margin - self.full_btn.width()
        self.full_btn.move(x, margin)
        self.open_btn.move(x - gap - self.open_btn.width(), margin)
        left = margin
        for b in (self.star, self.split, self.rtl):  # top left, small and quiet
            b.move(left, margin)
            left += b.width() + 6
            b.raise_()
        self.full_btn.raise_()
        self.open_btn.raise_()

    # ---- full screen --------------------------------------------------------------------------------------------
    def toggle_full_screen(self) -> None:
        """The book across the whole screen (no title row, square corners, the controls kept along the bottom). It fades in; Esc, F or the
        button brings the window back the way it was."""
        full = not self.isFullScreen()
        old = self.windowOpacity()
        self._fade_window(0.0, 90 if self.isVisible() else 0, lambda: self._set_full(full, old))

    def _set_full(self, full: bool, _old: float) -> None:
        column = self.panel.layout()
        for i in (0, 1):  # the title row and the rule under it
            item = column.itemAt(i)
            if item is not None and item.widget() is not None:
                item.widget().setVisible(not full)
        radius, edge = (0, "none") if full else (14, f"1px solid {T.border.name()}")
        self.panel.setStyleSheet(f"QFrame#framelessPanel {{ background: {T.bg.name()}; border: {edge}; border-radius: {radius}px; }}")
        self.full_btn.set_icon("collapse" if full else "expand")
        self.full_btn.setToolTip("Leave full screen (Esc)" if full else "Full screen (F)")
        if full:
            self.showFullScreen()
        else:
            self.showNormal()
        self.book.setFocus()
        self._fade_window(1.0, 240)

    def _fade_window(self, to: float, ms: int, then=None) -> None:
        """Move the whole window's opacity to `to` over `ms`, then call `then`. Straight there when motion is reduced."""
        anim = getattr(self, "_fade", None)
        if anim is not None:
            anim.stop()
        if ms <= 0 or not motion.on():
            self.setWindowOpacity(to)
            if then is not None:
                then()
            return
        anim = self._fade = QVariantAnimation(self, duration=ms, startValue=self.windowOpacity(), endValue=to)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda v: self.setWindowOpacity(float(v)))
        if then is not None:
            anim.finished.connect(then)
        anim.start()

    def reject(self) -> None:
        if self.isFullScreen():  # Esc leaves full screen first; a second Esc closes the window
            self.toggle_full_screen()
            return
        super().reject()

    def _external_button(self, layout) -> None:
        open_btn = QPushButton("Open in PDF viewer")
        open_btn.setAutoDefault(False)
        open_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.pdf))))
        layout.addWidget(open_btn)

    def _rtl_changed(self, on: bool) -> None:
        self.book.set_rtl(on)
        if self.store is not None and self.key:
            self.store.set_setting(f"manual_rtl:{self.key}", "1" if on else "0")

    def _star_clicked(self) -> None:
        """Marked already: clear it. Not yet: wait for a click on the page that is the cover."""
        if self.book.cover is not None:
            self._set_cover(None)
        else:
            self.star.setChecked(False)
            self.book.pick_cover(True)
            self.book.setFocus()

    def _cover_chosen(self, pdf_page: int, part: str) -> None:
        self._set_cover((pdf_page, part))

    def _set_cover(self, cover) -> None:
        self.book.set_cover(cover)
        self.star.setChecked(cover is not None)
        self.star.refresh()
        if self.store is not None and self.key:
            self.store.set_setting(f"manual_cover:{self.key}", ps.cover_text(cover))

    def _split_changed(self, on: bool) -> None:
        self.book.set_split("on" if on else "off")
        if self.store is not None and self.key:
            self.store.set_setting(f"manual_split:{self.key}", "1" if on else "0")

    def _pages_changed(self) -> None:
        self.jump.setRange(1, self.book.pages)
        self.jump.setToolTip(f"Page number (of {self.book.pages}): scroll the wheel over it to move through the book, or type a number")

    def _on_spread(self, spread: int) -> None:
        self.where.setText(self.book.label())
        left, right = self.book.pages_shown()
        page = right if right is not None else left
        if page is not None:
            self.jump.blockSignals(True)
            self.jump.setValue(page + 1)
            self.jump.blockSignals(False)
        if self.store is not None and self.key:
            self.store.set_setting(f"manual_spread:{self.key}", str(spread))
