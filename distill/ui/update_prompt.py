"""Start-up update check: looks at GitHub in the background and, if a newer release exists, asks whether to install it."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import updater
from ..constants import APP_NAME, APP_VERSION
from .update_screen import UpdateScreen


class _Bridge(QObject):
    found = Signal(object)  # an updater.Update
    progress = Signal(int, int)
    downloaded = Signal(object)  # a Path, or an Exception


class UpdatePromptMixin:
    """Mixed into DistillWindow. Call _update_check_start() once the window is up."""

    def _update_check_start(self) -> None:
        if updater.PREVIEW:  # DISTILL_UPDATE_PREVIEW=1: see the update screen without a release to download
            QTimer.singleShot(1200, self._update_preview)
            return
        if not updater.CHECK_ENABLED:
            return
        self._upd = _Bridge(self)
        self._upd.found.connect(self._update_offer)
        threading.Thread(target=lambda: self._upd.found.emit(updater.check()), daemon=True).start()

    def _update_offer(self, update) -> None:
        if update is None or self._p_data.get("skip_update") == update.tag or getattr(self, "is_processing", False):
            return
        can_install = bool(update.asset_url)
        box = QMessageBox(self)
        box.setWindowTitle("Update available")
        box.setText(f"<b>{APP_NAME} {update.tag}</b> is available (you have {APP_VERSION}).")
        box.setInformativeText("Install it now? Distill restarts when it is done; your settings and tools are kept."
                               if can_install else "Open the download page?")
        if update.notes.strip():
            box.setDetailedText(update.notes.strip())
        yes = box.addButton("Update now" if can_install else "Open page", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        skip = box.addButton("Skip this version", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(yes)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip:
            self._p_data["skip_update"] = update.tag
            self._settings_save()
        elif clicked is yes:
            self._update_install(update) if can_install else QDesktopServices.openUrl(QUrl(update.page))

    def _update_install(self, update) -> None:
        if getattr(self, "is_processing", False):
            return
        screen = UpdateScreen(self, f"{APP_NAME} {update.tag}")
        cancelled = threading.Event()
        screen.cancelled.connect(cancelled.set)

        def on_progress(done: int, total: int) -> None:
            if cancelled.is_set():
                raise RuntimeError("cancelled")
            self._upd.progress.emit(done, total)

        def work() -> None:
            try:
                self._upd.downloaded.emit(updater.download(update, on_progress))
            except Exception as exc:
                self._upd.downloaded.emit(exc)

        def fail(message: str) -> None:
            screen.dismiss()
            QMessageBox.warning(self, "Update failed", f"{message}\n\nYou can download it by hand:\n{update.page}")

        def on_done(result) -> None:
            self._upd.progress.disconnect(screen.set_progress)
            self._upd.downloaded.disconnect(on_done)
            if isinstance(result, Exception):
                if cancelled.is_set():
                    screen.dismiss()
                else:
                    fail(str(result))
                return
            screen.set_installing()  # the logo stays where the download left it; the install window carries on from there
            try:
                ready = updater.apply(result, update.tag, screen.handoff_spec())
            except Exception as exc:
                fail(str(exc))
                return
            self._settings_save()
            from ..app import _hard_exit  # imported here: app.py imports this window

            def leave() -> None:
                QApplication.quit()
                QTimer.singleShot(1500, lambda: _hard_exit(0))  # a normal exit can hang in libtorrent's shutdown, and the swap waits for it

            def wait_for_window(waited: int = 0) -> None:
                # Quit once the install window is on screen (so the two overlap and there is no gap), but never wait long for it.
                if ready is None:
                    if waited == 0:
                        screen.set_install_progress(1.0)  # no install window: fill the rest here, then go
                    done = waited >= 900
                else:
                    done = (ready.exists() and waited >= 300) or waited >= 6000
                if done:
                    leave()
                else:
                    QTimer.singleShot(100, lambda: wait_for_window(waited + 100))

            wait_for_window()

        self._upd.progress.connect(screen.set_progress)
        self._upd.downloaded.connect(on_done)
        threading.Thread(target=work, daemon=True).start()
        screen.show()

    def _update_preview(self) -> None:
        """The update screen with a made-up download and install, for looking at it (and trying Cancel) without a release."""
        screen = UpdateScreen(self, f"{APP_NAME} v9.9")
        total, steps = 48 << 20, 150
        state = {"n": 0}
        ticker = QTimer(screen)

        def tick() -> None:
            state["n"] += 1
            if state["n"] <= steps:
                screen.set_progress(total * state["n"] // steps, total)
                if state["n"] == steps:
                    screen.set_installing()
            else:  # the install, as the second part of the fill (the real one is a separate window; see update_splash.py)
                screen.set_install_progress((state["n"] - steps) / 40)
                if state["n"] >= steps + 40:
                    ticker.stop()
                    QTimer.singleShot(1200, screen.dismiss)

        screen.cancelled.connect(ticker.stop)
        ticker.timeout.connect(tick)
        ticker.start(40)
        screen.show()
