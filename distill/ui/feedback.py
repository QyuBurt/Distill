"""Feedback while the app works: notices, progress in the title bar, a notification when a long run ends, the
log's search and save, the Ctrl+K page jumper and tooltips that say why a control is greyed out."""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QSystemTrayIcon

from ..constants import APP_NAME
from ..models import fmt_duration
from ..runview import DL, view_of
from .nav import NAV_GROUPS, PAGE_INFO
from .palette import GoToDialog

NOTIFY_MIN_SECS = 20  # shorter runs finish before you have looked away


class FeedbackMixin:
    """Methods only; the state lives on DistillWindow, which mixes this in."""

    # ---- notices ---------------------------------------------------------------------------
    def _toast(self, text: str, level: str = "info", ms: int = 9000, action: tuple | None = None) -> None:
        """A message in the strip under the page title: for things that only need a glance, not a click."""
        self.banner.show_message(text, level, ms, action)

    # ---- title bar progress ----------------------------------------------------------------
    def _title_progress(self, percent: int | None, paused: bool | None = None) -> None:
        base = getattr(self, "_base_title", "") or self.windowTitle()
        if percent is None:
            self.setWindowTitle(base)
        else:
            tag = "Paused" if (self.is_paused if paused is None else paused) else f"{percent}%"
            self.setWindowTitle(f"{tag} - {base}")

    # ---- finished-run notification -----------------------------------------------------------
    def _notify_finished(self, title: str, text: str, secs: float | None = None) -> None:
        """Flash the taskbar button and show a toast when a run ends while the window is in the background."""
        check = getattr(self, "notify_check", None)
        if check is None or not check.isChecked() or self.isActiveWindow():
            return
        if secs is not None and secs < NOTIFY_MIN_SECS:
            return
        QApplication.alert(self, 0)  # flashes until the window is brought forward
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = getattr(self, "_tray", None)
        if tray is None:
            tray = self._tray = QSystemTrayIcon(self.windowIcon(), self)
        tray.show()
        tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 8000)
        QTimer.singleShot(12000, tray.hide)  # the tray icon exists only for the toast

    def _notify_run_done(self, secs: float, text: str | None = None) -> None:
        self._notify_finished(f"{APP_NAME}: run finished", f"{text or self.lbl_total_prog.text()}  -  took {fmt_duration(secs)}",
                              secs)

    def _aura_state(self) -> tuple[bool, bool]:
        """(running, paused) for the Start button's glow: the run of the page on screen, or an Organise job."""
        dl = view_of(self._current_mode()) == DL
        own, paused = (self.dl_runner is not None, self.dl_paused) if dl else (self.is_processing, self.is_paused)
        return bool(own or self.org_busy), bool(paused and own)

    # ---- the log ---------------------------------------------------------------------------
    def _log_save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Log", "distill-log.txt", "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.log.toPlainText())
        except OSError as exc:
            self._error("Couldn't Save", f"{path}\n\n{exc}")
            return
        self._toast(f"Log saved to {path}", "ok")

    def _log_find(self, _text: str = "") -> None:
        """Select the next match for the search box (wrapping round at the end)."""
        needle = self.log_find.text()
        if not needle:
            return
        if not self.log.find(needle):
            cursor = self.log.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            self.log.setTextCursor(cursor)
            if not self.log.find(needle):
                self._toast(f"'{needle}' is not in the log.", "warn", 3000)

    # ---- Ctrl+K ----------------------------------------------------------------------------
    def _goto_entries(self) -> list[tuple[str, str]]:
        out = []
        for group, items in NAV_GROUPS:
            for key, _text in items:
                if key in self.pages:
                    out.append((f"{PAGE_INFO[key][0]}    ({group.title()})", key))
        out.append(("Settings", "__settings__"))
        return out

    def _open_goto(self) -> None:
        dlg = GoToDialog(self._goto_entries(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted and (key := dlg.chosen()):
            self._open_settings() if key == "__settings__" else self._nav_click(key)

    # ---- why is this greyed out? -------------------------------------------------------------
    def _aura_setup(self) -> None:
        """Keep the Start button's glow and drift in step with whether a batch is running or paused."""
        self._aura_timer = QTimer(self)
        self._aura_timer.setInterval(150)
        self._aura_timer.timeout.connect(self._aura_sync)
        self._aura_timer.start()

    def _aura_sync(self) -> None:
        self._runbar_sync()  # a safety net: the bar along the bottom always matches whether a job is running and where
        button = getattr(self, "start_btn", None)
        if button is None or not hasattr(button, "set_state"):
            return
        state = self._aura_state()
        if state != getattr(self, "_aura_last", None):
            self._aura_last = state
            button.set_state(*state)

    def _why_setup(self) -> None:
        self._why_items: list[tuple[object, str, str]] = []
        self._why_timer = QTimer(self)
        self._why_timer.setInterval(700)
        self._why_timer.timeout.connect(self._why_refresh)
        self._why_timer.start()

    def _explain(self, widget, enabled_tip: str, disabled_tip: str) -> None:
        """Give a control a tooltip for each state, so a greyed-out one says what it is waiting for."""
        self._why_items.append((widget, enabled_tip, disabled_tip))
        self._why_refresh()

    def _why_refresh(self) -> None:
        for widget, on, off in self._why_items:
            try:
                tip = on if widget.isEnabled() else off
                if widget.toolTip() != tip:
                    widget.setToolTip(tip)
            except RuntimeError:  # the widget is gone
                continue
