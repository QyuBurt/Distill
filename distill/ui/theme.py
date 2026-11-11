"""Fluent colours derived from the live Qt palette."""
from __future__ import annotations

import weakref
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette


AlignL, AlignR, AlignC, AlignV = (Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight,
                                  Qt.AlignmentFlag.AlignCenter, Qt.AlignmentFlag.AlignVCenter)


# --- Theme: the fixed Distill dark palette -------------------------------------------------------
def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """a moved t of the way towards b."""
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


class Theme:
    """Surface, text and accent colours of the Distill look. The values were read from the design mockups; `set_dark`
    switches between the dark palette and the light one."""

    def __init__(self) -> None:
        self.text_font, self.display_font, self.mono_font = "Segoe UI", "Segoe UI", "Cascadia Mono"  # replaced by the bundled fonts at start-up
        self.dark = True
        self._bound: dict[int, tuple[weakref.ref, Callable[[], None]]] = {}  # widgets that restyle themselves on a theme change
        self.refresh()

    # ---- widgets that hold colours of their own ---------------------------------------------------------
    def bind(self, widget, apply: Callable[[], None]) -> None:
        """Run `apply` now and again every time the theme changes. For a widget that was given colours when it was built
        (a style sheet, a tinted pixmap) and would otherwise keep the old theme's. Binding the same widget again replaces
        the earlier callback, so a widget restyled as its state changes doesn't pile them up."""
        key = id(widget)
        self._bound[key] = (weakref.ref(widget, lambda _r, k=key: self._bound.pop(k, None)), apply)
        apply()

    def bind_css(self, widget, css: Callable[[], str]) -> None:
        """setStyleSheet(css()) now, and again whenever the theme changes."""
        self.bind(widget, lambda: widget.setStyleSheet(css()))

    def restyle(self) -> None:
        for ref, apply in list(self._bound.values()):
            if ref() is not None:
                try:
                    apply()
                except RuntimeError:  # the Qt object is already gone
                    pass

    def set_dark(self, dark: bool) -> None:
        self.dark = dark
        self.refresh()

    # text colours that get stored on list/tree items (setForeground) and so must follow a theme change
    _ITEM_COLOURS = ("title", "fg", "fg2", "fg3", "accent", "accent_text", "ok", "warn", "bad", "disabled_fg",
                     "group_org", "group_dl")

    def remap(self, col: QColor) -> QColor | None:
        """The current theme's colour that stands in for `col`, if `col` was one of the previous theme's text colours.
        Item colours are baked into the item when it is made; this lets them be moved over when the look changes."""
        name = getattr(self, "_remap", {}).get(col.rgb())
        return QColor(getattr(self, name)) if name else None

    def refresh(self) -> None:
        old = {n: getattr(self, n).rgb() for n in self._ITEM_COLOURS} if hasattr(self, "bg") else None
        self._refresh_colours()
        if old is not None:
            new = {n: getattr(self, n).rgb() for n in self._ITEM_COLOURS}
            if new != old:  # only a real change of look replaces the map (a repeat refresh keeps it)
                self._remap = {rgb: n for n, rgb in old.items() if rgb != new[n]}

    def _refresh_colours(self) -> None:
        c = QColor
        if self.dark:
            self.bg = c("#131317")            # the page
            self.side = c("#1b1c24")          # sidebar and command bar
            self.card = c("#1c1e2a")
            self.card_border = c("#262938")
            self.input = c("#17181f")
            self.border = c("#2e3245")        # outlines of inputs and buttons
            self.hover = c("#262935")
            self.select = c("#1e2431")        # the selected sidebar row
            self.title = c("#ffffff")
            self.fg = c("#cbd3de")            # primary text
            self.fg2 = c("#aab3c2")
            self.fg3 = c("#95a1b4")           # muted text: labels, blurbs, hints
            self.accent = c("#53b1cf")
            self.accent_text = c("#64cee9")
            self.accent_fg = c("#0b0d12")     # text on an accent fill
            self.chip_bg = c("#172329")
            self.chip_edge = c("#214047")
            self.disabled_fg = c("#5d6577")
            self.placeholder = c("#6b7487")
            self.ctl_edge, self.ctl_edge_off = c("#3b4058"), c("#272a3a")   # outline of an empty box / radio: normal, disabled
            self.ctl_on, self.ctl_on_off = c("#cbd3de"), c("#3a3f50")       # a ticked box: normal, disabled
            self.ctl_off_fill = c("#14151b")                                # a disabled input or box
            self.check_icon = "check-dark.png"                              # the tick drawn on a ticked box
            self.btn = (c("#1e212d"), c("#272a3a"))                         # secondary button: fill, edge
            self.btn_hover = (c("#2a2d3b"), c("#353949"))
            self.btn_down = (c("#202330"), c("#2c3042"))
            self.btn_off = (c("#181a22"), c("#20222e"))
            self.btn_danger = (c("#2f1d25"), c("#6b2f3a"))
            self.grad_off = c("#232632")      # a disabled Start button
            self.dot_idle = c("#4b5063")      # a sidebar dot that has nothing to report
            self.scroll, self.scroll_hover = c("#2e3245"), c("#40465f")
            self.log_fg = c("#6fb8c6")
            self.ok, self.warn, self.bad = c("#5fd4a0"), c("#f2c14e"), c("#ff7a8a")
            self.group_org, self.group_dl = c("#b585f4"), c("#e278b1")
            self.cpu, self.gpu, self.ram = c("#64cee9"), c("#b585f4"), c("#e278b1")
            self.frontend = c("#f2f4f8")      # the neutral colour of the Frontend group
            self.seek_handle = c("#ffffff")   # the seek bar's pill and the volume knob
            self.seek_rest = QColor(255, 255, 255, 60)  # the part of the seek bar not yet played
        else:
            self.bg = c("#eceef4")
            self.side = c("#f8f9fc")
            self.card = c("#ffffff")
            self.card_border = c("#dde1ea")
            self.input = c("#f4f5f9")
            self.border = c("#cbd1de")
            self.hover = c("#e6e9f1")
            self.select = c("#e1e9f6")
            self.title = c("#0e1119")
            self.fg = c("#2a3040")
            self.fg2 = c("#4a526a")
            self.fg3 = c("#5d657c")
            self.accent = c("#2a9bbd")
            self.accent_text = c("#0c7897")
            self.accent_fg = c("#ffffff")
            self.chip_bg = c("#e1f2f7")
            self.chip_edge = c("#b4dbe7")
            self.disabled_fg = c("#8a92a6")
            self.placeholder = c("#7a839a")
            self.ctl_edge, self.ctl_edge_off = c("#b3bacb"), c("#d8dce7")
            self.ctl_on, self.ctl_on_off = c("#2a3040"), c("#c5cad9")
            self.ctl_off_fill = c("#eceef4")
            self.check_icon = "check.png"
            self.btn = (c("#ffffff"), c("#cbd1de"))
            self.btn_hover = (c("#f0f2f8"), c("#b6bfd2"))
            self.btn_down = (c("#e4e8f1"), c("#a9b3c9"))
            self.btn_off = (c("#f1f2f6"), c("#dfe2ea"))
            self.btn_danger = (c("#fde8eb"), c("#e5a0ab"))
            self.grad_off = c("#e2e5ee")
            self.dot_idle = c("#b6bccb")
            self.scroll, self.scroll_hover = c("#c3c9d8"), c("#a7aec2")
            self.log_fg = c("#0e6a80")
            self.ok, self.warn, self.bad = c("#0e7a4e"), c("#8f5a05"), c("#bf2f48")
            self.group_org, self.group_dl = c("#8444d6"), c("#bf3b88")
            self.cpu, self.gpu, self.ram = c("#0c7897"), c("#8444d6"), c("#bf3b88")
            self.frontend = c("#5a6479")
            self.seek_handle = c("#2a3040")   # dark, so the pill shows on the light card
            self.seek_rest = c("#a3abc0")
        self.accent_hover = _mix(self.accent, c("#ffffff"), 0.12)
        self.accent_press = _mix(self.accent, c("#ffffff"), 0.25)
        self.grad = (c("#509dd5"), c("#7c63eb"), c("#c556a7"))  # the Start button / progress fill: the brand's, in both themes

    def palette(self) -> QPalette:
        """The Qt palette that native pieces (message boxes, file dialogs, menus) are drawn with."""
        p = QPalette()
        role = QPalette.ColorRole
        for r, col in ((role.Window, self.bg), (role.WindowText, self.fg), (role.Base, self.input),
                       (role.AlternateBase, self.card), (role.Text, self.fg), (role.Button, self.card),
                       (role.ButtonText, self.fg), (role.ToolTipBase, self.card), (role.ToolTipText, self.fg),
                       (role.Highlight, self.accent), (role.HighlightedText, self.accent_fg),
                       (role.PlaceholderText, self.placeholder), (role.Link, self.accent_text),
                       (role.BrightText, self.title), (role.Light, self.hover), (role.Mid, self.border),
                       (role.Dark, self.bg), (role.Shadow, QColor("#000000"))):
            p.setColor(r, col)
        for r in (role.Text, role.WindowText, role.ButtonText):
            p.setColor(QPalette.ColorGroup.Disabled, r, self.disabled_fg)
        return p

    def wash(self, start: int, end: int) -> tuple[QColor, QColor]:
        """The soft accent-to-violet wash behind a selected row, as two colours with the given alphas (0..255)."""
        a, b = QColor(self.accent), QColor(self.grad[1])
        a.setAlpha(start)
        b.setAlpha(end)
        return a, b

    def gradient_css(self, lighten: float = 0.0) -> str:
        stops = [_mix(g, QColor("#ffffff"), lighten) if lighten else g for g in self.grad]
        return (f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {stops[0].name()}, stop:0.5 {stops[1].name()}, "
                f"stop:1 {stops[2].name()})")

    def qss(self) -> str:
        from .assets import asset_url

        c = lambda col: col.name()  # noqa: E731
        check = asset_url("icons", self.check_icon)
        down, up, right = (asset_url("icons", f"chevron-{d}.png") for d in ("down", "up", "right"))
        mono = f'"{self.mono_font}", "Cascadia Mono", Consolas, monospace'
        off = c(self.disabled_fg)
        btn, btn_h, btn_d, btn_x = (tuple(c(x) for x in pair) for pair in (self.btn, self.btn_hover, self.btn_down, self.btn_off))
        return f"""
QMainWindow, QDialog {{ background: {c(self.bg)}; }}
QDialog#embeddedPanel {{ background: transparent; }}
QToolTip {{ background: {c(self.card)}; color: {c(self.fg)}; border: 1px solid {c(self.border)}; padding: 6px 8px; }}
QFrame#card {{ background: {c(self.card)}; border: 1px solid {c(self.card_border)}; border-radius: 14px; }}
QLabel#cardTitle {{ font-size: 10.5pt; font-weight: 600; color: {c(self.title)}; }}
QLabel#hint {{ color: {c(self.fg3)}; }}
QLabel#notice {{ color: {c(self.warn)}; font-weight: 600; }}
QLabel#muted {{ color: {c(self.fg3)}; }}
QLabel#pageTitle {{ font-family: "{self.display_font}"; font-size: 22pt; font-weight: 700; color: {c(self.title)}; }}
QLabel#phase {{ font-size: 10pt; font-weight: 600; color: {c(self.accent_text)}; }}
QLabel#brandName {{ font-family: "{self.display_font}"; font-size: 13pt; font-weight: 700; color: {c(self.title)}; }}
QLabel#dialogHead {{ font-family: "{self.display_font}"; font-size: 16pt; font-weight: 700; color: {c(self.title)}; }}
QLabel#chip {{ background: {c(self.chip_bg)}; color: {c(self.accent_text)}; border: 1px solid {c(self.chip_edge)}; border-radius: 6px;
    padding: 3px 10px; font-size: 9pt; font-weight: 700; }}
QLabel#pathLabel {{ color: {c(self.fg2)}; font-size: 9pt; font-weight: 500; }}
QLabel#mono {{ font-family: {mono}; }}
QWidget#sidebar {{ background: {c(self.side)}; border: 1px solid {c(self.card_border)}; border-radius: 16px; }}
QWidget#sideFooter {{ border-top: 1px solid {c(self.card_border)}; }}
QWidget#navWell {{ background: {c(self.bg)}; border: 1px solid {c(self.card_border)}; border-radius: 12px; }}
QWidget#navWell QScrollBar:vertical {{ margin: 8px 2px 8px 0; }}
QWidget#commandBar {{ background: {c(self.side)}; border: 1px solid {c(self.card_border)}; border-radius: 16px; }}
QWidget#nowPlaying {{ background: transparent; }}
QFrame#nowPlayingCard {{ background: {c(self.card)}; border: 1px solid {c(self.card_border)}; border-radius: 12px; }}
QLabel#nowTitle {{ font-weight: 600; color: {c(self.title)}; }}
QToolButton#npButton {{ border: none; border-radius: 8px; background: transparent; }}
QToolButton#npButton:hover {{ background: {c(self.hover)}; }}
QToolButton#npButton:disabled {{ background: transparent; }}
QSlider#npSlider::groove:horizontal {{ height: 4px; background: {c(self.border) if self.dark else c(self.seek_rest)}; border-radius: 2px; }}
QSlider#npSlider::sub-page:horizontal {{ background: {self.gradient_css()}; border-radius: 2px; }}
QSlider#npSlider::handle:horizontal {{ width: 10px; margin: -4px 0; border-radius: 5px; background: {c(self.seek_handle)}; border: 1px solid {c(self.seek_handle)}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QTextBrowser, QAbstractSpinBox, QComboBox {{ background: {c(self.input)};
    border: 1px solid {c(self.border)}; border-radius: 10px; padding: 8px 12px; color: {c(self.fg)};
    selection-background-color: {c(self.accent)}; selection-color: {c(self.accent_fg)}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QAbstractSpinBox:focus, QComboBox:focus,
QComboBox:on {{ border-color: {c(self.accent)}; }}
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {{ color: {off}; background: {c(self.ctl_off_fill)}; }}
QPlainTextEdit#log {{ padding: 8px 10px; font-family: {mono}; font-size: 9pt; color: {c(self.log_fg)}; border-radius: 10px; }}
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{ image: url({down}); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{ background: {c(self.card)}; color: {c(self.fg)}; border: 1px solid {c(self.border)}; border-radius: 8px;
    padding: 4px; outline: 0; selection-background-color: {c(self.select)}; selection-color: {c(self.accent_text)}; }}
QAbstractSpinBox {{ padding-right: 24px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 20px; border: none; background: transparent; }}
QAbstractSpinBox::up-arrow {{ image: url({up}); width: 12px; height: 12px; }}
QAbstractSpinBox::down-arrow {{ image: url({down}); width: 12px; height: 12px; }}

QCheckBox, QRadioButton {{ spacing: 10px; color: {c(self.fg)}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {off}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 17px; height: 17px; border: 1px solid {c(self.ctl_edge)};
    background: {c(self.input)}; }}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c(self.accent)}; }}
QCheckBox::indicator:checked {{ background: {c(self.ctl_on)}; border-color: {c(self.ctl_on)}; image: url({check}); }}
QCheckBox::indicator:disabled {{ background: {c(self.ctl_off_fill)}; border-color: {c(self.ctl_edge_off)}; }}
QCheckBox::indicator:checked:disabled {{ background: {c(self.ctl_on_off)}; border-color: {c(self.ctl_on_off)}; }}
QRadioButton::indicator:checked {{ border-color: {c(self.accent)};
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 {c(self.accent)}, stop:0.42 {c(self.accent)},
    stop:0.5 {c(self.input)}, stop:1 {c(self.input)}); }}

QPushButton {{ background: {btn[0]}; border: 1px solid {btn[1]}; border-radius: 10px; padding: 8px 18px;
    color: {c(self.fg)}; font-weight: 500; }}
QPushButton:hover {{ background: {btn_h[0]}; border-color: {btn_h[1]}; color: {c(self.title)}; }}
QPushButton:pressed {{ background: {btn_d[0]}; border-color: {btn_d[1]}; color: {c(self.fg)}; }}
QPushButton:disabled {{ color: {off}; background: {btn_x[0]}; border-color: {btn_x[1]}; }}
QPushButton:flat {{ background: transparent; border: none; color: {c(self.fg3)}; padding: 4px 10px; }}
QPushButton:flat:hover {{ background: {c(self.hover)}; color: {c(self.fg)}; }}
QPushButton:flat:disabled {{ background: transparent; color: {off}; }}
QPushButton[accent="true"] {{ background: {self.gradient_css()}; color: #ffffff; border: none; border-radius: 10px;
    padding: 8px 20px; font-weight: 600; }}
QPushButton[accent="true"]:hover {{ background: {self.gradient_css(0.14)}; }}
QPushButton[accent="true"]:pressed {{ background: {self.gradient_css(0.0)}; padding-top: 9px; }}
QPushButton[accent="true"]:disabled {{ background: {c(self.grad_off)}; color: {off}; }}
QToolButton {{ border: none; border-radius: 8px; background: transparent; color: {c(self.fg)}; }}
QToolButton:hover {{ background: {c(self.hover)}; }}
QToolButton:disabled {{ background: transparent; color: {off}; }}
QToolButton#expanderHeader {{ padding: 12px 16px; text-align: left; font-weight: 700; color: {c(self.fg)}; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c(self.scroll)}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:horizontal {{ background: {c(self.scroll)}; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{ background: {c(self.scroll_hover)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QProgressBar {{ background: {c(self.input)}; border: 1px solid {c(self.card_border)}; border-radius: 6px; height: 12px;
    text-align: center; color: {c(self.fg)}; }}
QProgressBar::chunk {{ border-radius: 5px; background: {self.gradient_css()}; }}
QSlider::groove:horizontal {{ height: 5px; background: {c(self.border)}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {self.gradient_css()}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; margin: -5px 0; border-radius: 7px; background: #ffffff; border: 1px solid {c(self.ctl_edge)}; }}
QSlider::handle:horizontal:hover {{ background: {c(self.accent_text)}; }}

QTreeView, QTableView, QListWidget, QListView {{ background: {c(self.input)}; border: 1px solid {c(self.border)};
    border-radius: 10px; alternate-background-color: {c(self.input)}; gridline-color: {c(self.card_border)}; outline: 0; }}
QTreeView::item, QListWidget::item {{ padding: 5px 6px; }}
QTreeView::item:hover, QTableView::item:hover, QListWidget::item:hover {{ background: {c(self.hover)}; }}
QTreeView::item:selected, QTableView::item:selected, QListWidget::item:selected {{ background: {c(self.select)};
    color: {c(self.accent_text)}; }}
QTreeView::indicator, QTableView::indicator, QListView::indicator {{ width: 16px; height: 16px; border: 1px solid {c(self.ctl_edge)};
    border-radius: 4px; background: {c(self.input)}; }}
QTreeView::indicator:checked, QTableView::indicator:checked, QListView::indicator:checked {{ background: {c(self.ctl_on)};
    border-color: {c(self.ctl_on)}; image: url({check}); }}
QTreeView::indicator:indeterminate, QTableView::indicator:indeterminate {{ background: {c(self.ctl_on_off)}; border-color: {c(self.ctl_on_off)}; }}
QTreeView {{ selection-background-color: {c(self.select)}; selection-color: {c(self.accent_text)}; }}
QTreeView::branch {{ background: transparent; }}
QTreeView::branch:selected {{ background: {c(self.select)}; }}
QTreeView::branch:hover {{ background: {c(self.hover)}; }}
QTreeView::branch:has-children:closed {{ image: url({right}); }}
QTreeView::branch:has-children:open {{ image: url({down}); }}
QHeaderView::section {{ background: {c(self.card)}; color: {c(self.fg3)}; border: none;
    border-bottom: 1px solid {c(self.card_border)}; padding: 9px 12px; font-weight: 600; }}
QTableCornerButton::section {{ background: {c(self.card)}; border: none; }}

QMenu {{ background: {c(self.card)}; border: 1px solid {c(self.border)}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 14px; border-radius: 6px; color: {c(self.fg)}; }}
QMenu::item:selected {{ background: {c(self.select)}; color: {c(self.accent_text)}; }}
QMenu::item:disabled {{ color: {off}; }}
QMenu::separator {{ height: 1px; background: {c(self.card_border)}; margin: 5px 8px; }}
QGroupBox {{ background: transparent; border: 1px solid {c(self.card_border)}; border-radius: 12px; margin-top: 14px;
    padding: 16px 12px 12px 12px; font-weight: 600; color: {c(self.fg)}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {c(self.accent_text)}; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:vertical:hover {{ background: {c(self.border)}; margin: 4px 0; border-radius: 1px; }}
QStatusBar, QDialogButtonBox {{ background: transparent; }}
"""


T = Theme()
