"""Start-up update check: looks at GitHub in the background and, if a newer release exists, asks whether to install it."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from .. import updater
from ..constants import APP_NAME, APP_VERSION


class _Bridge(QObject):
    found = Signal(object)  # an updater.Update
    progress = Signal(int, int)
    downloaded = Signal(object)  # a Path, or an Exception


class UpdatePromptMixin:
    """Mixed into DistillWindow. Call _update_check_start() once the window is up."""

    def _update_check_start(self) -> None:
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
        dlg = QProgressDialog(f"Downloading {APP_NAME} {update.tag}...", "Cancel", 0, 100, self)
        dlg.setWindowTitle("Updating")
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        cancelled = threading.Event()
        dlg.canceled.connect(cancelled.set)

        def on_progress(done: int, total: int) -> None:
            if cancelled.is_set():
                raise RuntimeError("cancelled")
            self._upd.progress.emit(done, total)

        def work() -> None:
            try:
                self._upd.downloaded.emit(updater.download(update, on_progress))
            except Exception as exc:
                self._upd.downloaded.emit(exc)

        def on_prog(done: int, total: int) -> None:
            dlg.setMaximum(total if total > 0 else 0)
            dlg.setValue(done)

        def on_done(result) -> None:
            dlg.close()
            self._upd.progress.disconnect(on_prog)
            self._upd.downloaded.disconnect(on_done)
            if isinstance(result, Exception):
                if not cancelled.is_set():
                    QMessageBox.warning(self, "Update failed", f"{result}\n\nYou can download it by hand:\n{update.page}")
                return
            try:
                updater.apply(result)
            except Exception as exc:
                QMessageBox.warning(self, "Update failed", f"{exc}\n\nYou can download it by hand:\n{update.page}")
                return
            self._settings_save()
            from ..app import _hard_exit  # imported here: app.py imports this window
            QApplication.quit()
            QTimer.singleShot(1500, lambda: _hard_exit(0))  # a normal exit can hang in libtorrent's shutdown, and the swap waits for it

        self._upd.progress.connect(on_prog)
        self._upd.downloaded.connect(on_done)
        threading.Thread(target=work, daemon=True).start()
        dlg.show()
