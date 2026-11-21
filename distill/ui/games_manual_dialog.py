"""The "Find a manual" window: manuals found online for a game (the Internet Archive; see manualsearch.py), to pick one from. Choosing one
downloads it (with a bar and a Stop) to a temporary file and hands that back as `path`; the caller keeps it as the game's manual."""
from __future__ import annotations

import re
import threading
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .. import manualsearch as ms
from . import motion
from .frameless import FramelessDialog, hairline
from .theme import T
from .widgets import accent_button, hint


class ManualPickerDialog(FramelessDialog):
    _found = Signal(object, str)  # ([Manual] or None, what went wrong)
    _progress = Signal(int, int)  # (bytes so far, bytes in all or 0)
    _fetched = Signal(object, str)  # (the downloaded file or None, what went wrong)

    def __init__(self, parent: QWidget, game: str, system: str, platform: str, region: str = ""):
        super().__init__(parent, f"Find a manual: {game}", "search", (700, 580), (560, 460))
        self.path: Path | None = None  # the downloaded PDF, once one was chosen
        self._results: list[ms.Manual] = []
        self._alive, self._busy = True, False
        self._cancel = threading.Event()
        ms.sweep_temp()
        outer = self.content
        self.status = hint(f"Looking on the Internet Archive for the manual of {game}...", live=True)
        self.status.setContentsMargins(22, 14, 22, 0)
        outer.addWidget(self.status)
        self.list = QListWidget()
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setSpacing(2)
        T.bind_css(self.list, lambda: (f"QListWidget {{ background: transparent; border: none; outline: 0; padding: 6px 14px; color: {T.fg.name()}; }}"
                                       f"QListWidget::item {{ padding: 10px 12px; border-radius: 10px; }}"
                                       f"QListWidget::item:hover {{ background: {T.hover.name()}; }}"
                                       f"QListWidget::item:selected {{ background: {T.select.name()}; color: {T.title.name()}; }}"))
        self.list.itemSelectionChanged.connect(self._sync)
        self.list.itemDoubleClicked.connect(lambda _i: self._use())
        outer.addWidget(self.list, 1)
        self.run_box = QWidget()  # shown while a manual is being downloaded
        run = QVBoxLayout(self.run_box)
        run.setContentsMargins(22, 0, 22, 6)
        run.setSpacing(5)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        T.bind_css(self.bar, lambda: (f"QProgressBar {{ background: {T.input.name()}; border: none; border-radius: 3px; }}"
                                       f"QProgressBar::chunk {{ background: {T.accent.name()}; border-radius: 3px; }}"))
        self.bar_text = hint("", live=True)
        run.addWidget(self.bar)
        run.addWidget(self.bar_text)
        self.run_box.setVisible(False)
        outer.addWidget(self.run_box)
        outer.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(22, 12, 22, 14)
        foot.setSpacing(8)
        self.page_btn = QPushButton("View page")
        self.page_btn.setAutoDefault(False)
        self.page_btn.setToolTip("Open the Internet Archive's page for this one, to look at it before you use it")
        self.page_btn.clicked.connect(self._view)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.clicked.connect(self._cancel_clicked)
        self.use_btn = accent_button("Use this manual")
        self.use_btn.setAutoDefault(False)
        self.use_btn.clicked.connect(self._use)
        foot.addWidget(self.page_btn)
        foot.addStretch(1)
        foot.addWidget(self.cancel_btn)
        foot.addWidget(self.use_btn)
        outer.addLayout(foot)
        self._found.connect(self._on_found)
        self._progress.connect(self._on_progress)
        self._fetched.connect(self._on_fetched)
        self._sync()

        def work() -> None:
            try:
                found = ms.search(game, system, platform, region=region)
            except ms.ManualError as exc:
                self._send(self._found, None, str(exc))
            except Exception as exc:  # a reply that can't be read must not take the window down
                self._send(self._found, None, f"The search failed: {exc}")
            else:
                self._send(self._found, found, "")

        threading.Thread(target=work, daemon=True).start()

    # ---- results --------------------------------------------------------------------------------------------------
    def _send(self, signal, *args) -> None:
        if self._alive:
            try:
                signal.emit(*args)
            except RuntimeError:  # the window is gone
                pass

    def _on_found(self, found, error: str) -> None:
        if error:
            self.status.setText(error)
            return
        self._results = list(found or [])
        if not self._results:
            self.status.setText("No manual found for this game. The Internet Archive may not have one, or it may be listed under another name. "
                                "You can still choose a PDF from your computer.")
            return
        self.status.setText(f"{len(self._results)} found, best first. These are scans uploaded by other people, so look at the page if you are unsure.")
        for m in self._results:
            detail = "  -  ".join(p for p in (m.source, m.size_text(), f"{round(m.score * 100)}% match") if p)
            item = QListWidgetItem(f"{m.title}\n{detail}")
            item.setToolTip(m.page)
            self.list.addItem(item)
        self.list.setCurrentRow(0)

    def _current(self) -> ms.Manual | None:
        row = self.list.currentRow()
        return self._results[row] if 0 <= row < len(self._results) else None

    def _sync(self) -> None:
        chosen = self._current() is not None and not self._busy
        self.use_btn.setEnabled(chosen)
        self.page_btn.setEnabled(self._current() is not None)
        self.list.setEnabled(not self._busy)

    def _view(self) -> None:
        m = self._current()
        if m is not None:
            QDesktopServices.openUrl(QUrl(m.page))

    # ---- downloading ----------------------------------------------------------------------------------------------
    def _use(self) -> None:
        m = self._current()
        if m is None or self._busy:
            return
        self._busy = True
        self._cancel.clear()
        safe = re.sub(r"[^\w.-]+", "_", m.identifier)[:60]
        dest = ms.temp_dir() / f"{safe}.pdf"
        self.bar.setValue(0)
        self.bar_text.setText("Starting the download...")
        motion.set_visible(self.run_box, True, axis="height")
        self.cancel_btn.setText("Stop")
        self._sync()

        def work() -> None:
            try:
                path = ms.download(m.url, dest, lambda done, total: self._send(self._progress, done, total), self._cancel.is_set)
            except ms.ManualError as exc:
                self._send(self._fetched, None, str(exc))
            else:
                self._send(self._fetched, path, "")

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, done: int, total: int) -> None:
        mb = done / (1 << 20)
        if total > 0:
            motion.ease_value(self.bar, int(1000 * done / total))
            self.bar_text.setText(f"{mb:.1f} of {total / (1 << 20):.1f} MB")
        else:
            self.bar_text.setText(f"{mb:.1f} MB")

    def _on_fetched(self, path, error: str) -> None:
        self._busy = False
        self.cancel_btn.setText("Cancel")
        if path is not None:
            self.path = Path(path)
            self.accept()
            return
        self.run_box.setVisible(False)
        self.status.setText("Stopped." if error == "Cancelled." else error)
        self._sync()

    def _cancel_clicked(self) -> None:
        if self._busy:
            self._cancel.set()  # stops the download; the window stays so another can be chosen
            self.bar_text.setText("Stopping...")
        else:
            self.reject()

    def done(self, result: int) -> None:
        self._alive = False
        self._cancel.set()
        super().done(result)
