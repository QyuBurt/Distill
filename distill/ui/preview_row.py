"""'Estimate savings' on the CHD, RVZ, CSO and ZAR pages: a button under the folders and one line of result.

It samples the files the batch would take (preview.py) on a background thread and reports back through the window's event queue,
as the other background jobs do. Nothing is converted, written or changed; it needs no conversion tool, only the source folder.
"""
from __future__ import annotations

import itertools
import threading
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from ..models import Config
from ..preview import preview
from .widgets import hint


class PreviewMixin:
    def _pv_row(self, mode: str) -> QWidget:
        """The button and its result line, for the Folders card of `mode`'s page."""
        if not hasattr(self, "_pv_rows"):
            self._pv_rows, self._pv_state, self._pv_tokens = {}, {}, itertools.count(1)
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        button = QPushButton("Estimate savings")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip("Sample the files in the source folder and estimate how much space this conversion would free. "
                          "Nothing is converted or changed, and it needs no conversion tool.")
        button.clicked.connect(lambda: self._pv_start(mode))
        label = hint("", live=True)
        lay.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(label, 1)
        self._pv_rows[mode] = SimpleNamespace(button=button, label=label)
        return box

    def _pv_start(self, mode: str) -> None:
        """Start the estimate, or (while one is running) stop it."""
        row = self._pv_rows[mode]
        if (running := self._pv_state.get(mode)) is not None:
            running[1].set()
            return
        tv = self.tabs[mode]
        source = Path(self._clean(tv.source.text()))
        if not source.is_dir():
            self._error("Error", f"Please select a valid source directory.\n\nPath checked: {source}")
            return
        cfg = Config(source=source, dest=None, chdman="", extract=mode != "zar" and tv.extract.isChecked(), verify=False,
                     delete=False, thread_budget=1, mode=mode)
        token, stop = next(self._pv_tokens), threading.Event()
        self._pv_state[mode] = (token, stop)
        row.button.setText("Stop estimating")
        row.label.setText("Looking at the files...")

        def work() -> None:
            try:
                got = preview(cfg, lambda i, n: self.ui_q.put(("preview_progress", mode, token, i, n)), stop)
                self.ui_q.put(("preview_done", mode, token, got, ""))
            except Exception as exc:  # a folder that vanished, a drive that dropped: say so, do not die silently
                self.ui_q.put(("preview_done", mode, token, None, str(exc) or exc.__class__.__name__))

        threading.Thread(target=work, daemon=True, name=f"preview-{mode}").start()

    def _pv_event(self, ev: tuple) -> None:
        kind, mode, token, *rest = ev
        state = self._pv_state.get(mode)
        if state is None or state[0] != token:
            return  # a result from an estimate that was replaced or already finished
        row = self._pv_rows[mode]
        if kind == "preview_progress":
            row.label.setText(f"Looking at file {rest[0] + 1} of {rest[1]}...")
            return
        got, error = rest
        del self._pv_state[mode]
        row.button.setText("Estimate savings")
        row.label.setText(f"Could not estimate: {error}" if error else got.headline() if got else "")
