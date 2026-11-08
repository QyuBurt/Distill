"""A pop-up window without the operating system's frame: a rounded panel in the app's colours with a title row (icon, name, and a
close button in the top right that turns red under the pointer). The title row drags the window. Build the window's own content
in `self.content`, a vertical layout that fills the panel under the title row. Esc still closes it, like any dialog."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from . import icons
from .theme import T

CLOSE_RED, CLOSE_RED_DOWN = "#e5484d", "#c93b40"


def hairline() -> QFrame:
    """A one-pixel rule in the card edge colour."""
    line = QFrame()
    line.setFixedHeight(1)
    T.bind_css(line, lambda: f"background: {T.card_border.name()}; border: none;")
    return line


class CloseButton(QToolButton):
    """The X in the top right of a frameless window: quiet until the pointer is over it, then red with a white cross."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(42, 30)
        self.setToolTip("Close")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(f"QToolButton {{ background: transparent; border: none; border-radius: 8px; }}"
                           f"QToolButton:hover {{ background: {CLOSE_RED}; }} QToolButton:pressed {{ background: {CLOSE_RED_DOWN}; }}")
        T.bind(self, self._paint_icon)

    def _paint_icon(self) -> None:
        self.setIcon(QIcon(icons.pixmap("close", "#ffffff" if self.underMouse() else T.fg3, 16)))

    def enterEvent(self, event) -> None:
        self._paint_icon()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._paint_icon()
        super().leaveEvent(event)


class _TitleBar(QWidget):
    """The title row. Pressing on it (not on the close button) lets the system move the window."""

    def mousePressEvent(self, event) -> None:
        handle = self.window().windowHandle()
        if event.button() == Qt.MouseButton.LeftButton and handle is not None:
            handle.startSystemMove()
            event.accept()
            return
        super().mousePressEvent(event)


class FramelessDialog(QDialog):
    """`size` is the opening size (None: whatever the content asks for, at least `min_size`). `chrome=False` makes it a plain panel with
    no frame, title row or window flags, for a dialog that is sometimes built into another window (Settings) instead of shown alone."""

    def __init__(self, parent: QWidget | None, title: str, icon: str = "", size: tuple[int, int] | None = (760, 600),
                 min_size: tuple[int, int] = (520, 400), chrome: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)  # the taskbar and Alt+Tab still need a name for it
        self.title_label: QLabel | None = None
        self.close_btn: CloseButton | None = None
        if not chrome:
            self.content = QVBoxLayout(self)
            self.content.setContentsMargins(0, 0, 0, 0)
            self.content.setSpacing(0)
            return
        self.setObjectName("framelessDialog")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("#framelessDialog { background: transparent; border: none; }")  # the app style must not paint a square behind the panel
        screen = (parent.screen() if parent is not None else None) or QGuiApplication.primaryScreen()
        room = screen.availableGeometry() if screen is not None else None
        if size is not None:
            w, h = (min(size[0], room.width() - 40), min(size[1], room.height() - 80)) if room is not None else size
            self.resize(w, h)
            self.setMinimumSize(min(min_size[0], w), min(min_size[1], h))
        else:
            self.setMinimumSize(*min_size)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self.panel = QFrame()
        self.panel.setObjectName("framelessPanel")
        T.bind_css(self.panel, lambda: f"QFrame#framelessPanel {{ background: {T.bg.name()}; border: 1px solid {T.border.name()}; border-radius: 14px; }}")
        shell.addWidget(self.panel)
        column = QVBoxLayout(self.panel)
        column.setContentsMargins(1, 1, 1, 1)  # inside the panel's own edge
        column.setSpacing(0)

        bar = _TitleBar()
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 9, 10, 9)
        row.setSpacing(8)
        if icon:
            glyph = QLabel()
            glyph.setFixedSize(20, 20)
            T.bind(glyph, lambda: glyph.setPixmap(icons.pixmap(icon, T.accent_text, 20)))
            row.addWidget(glyph)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("cardTitle")
        row.addWidget(self.title_label)
        row.addStretch(1)
        self.close_btn = CloseButton()
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.close_btn)
        column.addWidget(bar)
        column.addWidget(hairline())
        self.content = QVBoxLayout()
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(0)
        column.addLayout(self.content, 1)

    def body(self, margins: tuple[int, int, int, int] = (20, 16, 20, 16), spacing: int = 10) -> QVBoxLayout:
        """A padded vertical layout filling the window under the title row: where a dialog puts what it used to put straight into itself."""
        holder = QWidget()
        holder.setObjectName("framelessBody")
        holder.setStyleSheet("#framelessBody { background: transparent; }")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(*margins)
        lay.setSpacing(spacing)
        self.content.addWidget(holder, 1)
        return lay

    def set_title(self, text: str) -> None:
        self.setWindowTitle(text)
        if self.title_label is not None:
            self.title_label.setText(text)
