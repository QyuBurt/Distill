"""A combo box's drop-down list sits in a window of its own (a square frame). The theme rounds the list itself, so the
frame's square corners, its 1px square outline and its square shadow show behind it. `soften_popup` makes that window
see-through, frameless and borderless, leaving only the rounded list. (The outline is drawn by the widget style, not
by the window background, so transparency alone doesn't remove it: the window needs a style rule of its own.)"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QComboBox


def soften_popup(combo: QComboBox) -> None:
    """Call once, after the combo is built (its list window exists from then on). Safe to call again."""
    window = combo.view().window()  # the frame around the list
    if window.objectName() == "comboPopup":
        return  # already done
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)  # the native shadow is square too
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    window.setObjectName("comboPopup")
    window.setStyleSheet("#comboPopup { background: transparent; border: none; }")  # by id: nothing inside it is affected


class _SoftPopups(QObject):
    """Application-wide: every combo box gets `soften_popup` the moment Qt first prepares it for showing."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Polish and isinstance(obj, QComboBox):
            soften_popup(obj)
        return False  # never swallows anything


def install_soft_popups(app: QApplication) -> None:
    """Round every drop-down in the app (settings, recomps, emulators, dialogs...). Call once at start-up; repeats are ignored."""
    if getattr(app, "_soft_popups", None) is None:
        app._soft_popups = _SoftPopups(app)  # kept on the app so it lives as long as it does
        app.installEventFilter(app._soft_popups)
