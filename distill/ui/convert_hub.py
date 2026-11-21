"""All Tools: one card for each converter and library organiser, grouped by what it does, with what goes in and what comes out (or
what it is for) and, for a converter, whether its tool is ready. A card opens that page. The pin at its top right puts the page in the
sidebar or takes it out: it is the same pin as Settings > Sidebar, so the two always agree. The pages are unchanged and keep their keys.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..convertpages import GROUPS, IN_OUT, TOOL_OF, organise_keys, page_keys
from ..orgreview import ACCENT
from ..toolstate import describe, overview
from .emu_cards import CardGrid, Clickable, section_header
from .nav import PAGE_INFO
from .review_bar import ReviewBar
from .settings_page import TOOL_TONES
from .theme import T


class PinButton(QAbstractButton):
    """A small pushpin: filled, in the accent colour and pointing straight down when the page is in the sidebar; an outline, tilted sideways, when it is not. It takes its own clicks,
    so pressing it never opens the card behind it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(26, 26)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.toggled.connect(self._retip)
        self._retip(False)

    def _retip(self, on: bool) -> None:
        self.setToolTip("In the sidebar. Click to take it out." if on else "Not in the sidebar. Click to pin it there.")

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.hover)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        colour = T.accent if self.isChecked() else T.fg3
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(0 if self.isChecked() else 40)  # pinned: needle straight down; not pinned: tilted
        p.setPen(QPen(colour, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(colour if self.isChecked() else Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(-3.2, -8.0, 6.4, 8.0), 1.6, 1.6)  # the head
        p.drawLine(QPointF(-5.0, 0.0), QPointF(5.0, 0.0))  # the collar
        p.drawLine(QPointF(0.0, 0.0), QPointF(0.0, 7.5))  # the needle


class ConverterCard(Clickable):
    """A tool: its name, a tag for its tool's state (converters only), a pin, and a line of what it does."""

    def __init__(self, key: str, title: str, line: str) -> None:
        super().__init__(key, "converterCard")
        self.setMinimumHeight(88)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 12, 12)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = QLabel(title)
        self.title.setWordWrap(True)
        T.bind_css(self.title, lambda: f"color: {T.title.name()}; font-weight: 700; font-size: 11pt; background: transparent;")
        self.tag = QLabel("")
        self.tag.setVisible(False)
        self.pin = PinButton()
        top.addWidget(self.title, 1)
        top.addWidget(self.tag, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.pin, 0, Qt.AlignmentFlag.AlignTop)
        self.line = QLabel(line)
        self.line.setWordWrap(True)
        T.bind_css(self.line, lambda: f"color: {T.fg3.name()}; background: transparent;")
        lay.addLayout(top)
        lay.addWidget(self.line)
        lay.addStretch(1)

    def set_state(self, text: str, colour: Callable[[], QColor]) -> None:
        """Tag the card (Ready, Checking, Updating, Missing, Running). `colour` is asked again when the theme changes."""
        self.tag.setText(text)
        self.tag.setVisible(True)
        T.bind_css(self.tag, lambda: (f"background: rgba({colour().red()}, {colour().green()}, {colour().blue()}, 40); "
                                      f"color: {colour().name()}; border-radius: 9px; padding: 2px 9px; "
                                      "font-size: 9pt; font-weight: 600;"))


class ConvertHubMixin:
    def _build_convert_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        self.cv_review = ReviewBar()  # how many of the converters' tools are ready
        lay.addWidget(self.cv_review)
        self.cv_cards: dict[str, ConverterCard] = {}
        for title, items in GROUPS:
            lay.addLayout(section_header(title))
            cards = []
            for key, _tool in items:
                card = ConverterCard(key, PAGE_INFO[key][0], IN_OUT[key])
                cards.append(card)
                self.cv_cards[key] = card
            lay.addWidget(self._cv_grid(cards))
        lay.addLayout(section_header("Organise your library"))
        cards = []
        for key in organise_keys():
            card = ConverterCard(key, PAGE_INFO[key][0], PAGE_INFO[key][1])
            cards.append(card)
            self.cv_cards[key] = card
        lay.addWidget(self._cv_grid(cards))
        for key, card in self.cv_cards.items():
            card.clicked.connect(self._nav_click)
            card.pin.toggled.connect(lambda on, k=key: self._cv_pin(k, on))
            if (box := self._pin_boxes.get(key)) is not None:  # the pin follows Settings > Sidebar, and the other way round
                box.toggled.connect(lambda *_: self._cv_sync_pins())
        lay.addStretch(1)
        page.setMinimumSize(1, 400)  # a width of 1, not 0: Qt reads 0 as "unset" and falls back to the widest child
        self._cv_sync_pins()
        self._cv_refresh()
        return page

    @staticmethod
    def _cv_grid(cards: list[ConverterCard]) -> CardGrid:
        grid = CardGrid(min_card=290, max_cols=3)
        grid.set_cards(cards)
        return grid

    def _cv_pin(self, key: str, on: bool) -> None:
        """A card's pin was pressed: pin or unpin the page, as Settings > Sidebar would."""
        if (box := self._pin_boxes.get(key)) is not None:
            box.setChecked(on)

    def _cv_sync_pins(self) -> None:
        """Show each card's pin as the sidebar has it (without pressing it)."""
        for key, card in self.cv_cards.items():
            card.pin.blockSignals(True)
            card.pin.setChecked(self._is_pinned(key))
            card.pin.blockSignals(False)
            card.pin._retip(card.pin.isChecked())
            card.pin.update()

    def _cv_refresh(self) -> None:
        """Tag every converter card with its tool's state (the rule the sidebar dots use), or Running if its page has the job going."""
        if not hasattr(self, "cv_cards"):
            return
        running = self._tool_target()
        states = {}
        for key in page_keys():
            states[key] = self._tool_state(TOOL_OF[key])
            text, tone = describe(states[key])
            if running is not None and running[0] == key:
                text, tone = "Running", ACCENT
            self.cv_cards[key].set_state(text, TOOL_TONES[tone])
        self.cv_review.set_chips(overview(states))
