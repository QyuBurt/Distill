"""Remembering the window and every page's options between runs (one small JSON file next to the app)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLineEdit, QSpinBox

from ..constants import APP_DIR, APP_VERSION
from ..system import resolve_tool_path
from .state import TOOLS
from .switch_page import SW_PERSIST
from .theme import T
from .whatsnew import NOTES, WhatsNewDialog
from .widgets import Expander, PathRow, StageSwitch

SETTINGS_FILE = APP_DIR / "distill_settings.json"
SAVE_DELAY_MS = 800  # after the last change, so dragging a splitter writes once
SHOW_WHATS_NEW = True  # tests switch this off


def read_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(data: dict) -> None:
    try:
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except OSError:
        pass  # a convenience: never interrupt the app for it


def accessors(widget) -> tuple[Callable[[], object], Callable[[object], None], object] | None:
    """(get, set, change signal) for the kinds of widget that hold an option, or None for anything else."""
    if isinstance(widget, PathRow):
        return widget.text, widget.setText, widget.edit.textChanged
    if isinstance(widget, StageSwitch):  # before QComboBox: it stands in for one but is not one
        def put(value) -> None:
            if (i := widget.findData(value)) >= 0:
                widget.setCurrentIndex(i)
        return widget.currentData, put, widget.currentIndexChanged
    if isinstance(widget, QAbstractButton):
        return widget.isChecked, lambda v: widget.setChecked(bool(v)), widget.toggled
    if isinstance(widget, QComboBox):
        return widget.currentText, widget.setCurrentText, widget.currentTextChanged
    if isinstance(widget, QSpinBox):
        return widget.value, lambda v: widget.setValue(int(v)), widget.valueChanged
    if isinstance(widget, QLineEdit):
        return widget.text, widget.setText, widget.textChanged
    return None


class PersistMixin:
    """Window geometry, splitter, last page, folded cards, and the options of every page. Methods only; the state
    lives on DistillWindow, which mixes this in."""

    def _persist_setup(self) -> None:
        self._p_binds: dict[str, tuple[Callable, Callable]] = {}
        self._p_ready = False
        self._p_data: dict = {}
        self._p_timer = QTimer(self)
        self._p_timer.setSingleShot(True)
        self._p_timer.setInterval(SAVE_DELAY_MS)
        self._p_timer.timeout.connect(self._settings_save)

    def _persist_soon(self, *_args) -> None:
        if getattr(self, "_p_ready", False):
            self._p_timer.start()

    def _persist_bind(self, key: str, widget) -> None:
        found = accessors(widget)
        if found is None:
            return
        get, put, signal = found
        self._p_binds[key] = (get, put)
        signal.connect(self._persist_soon)

    def _persist_widgets(self) -> None:
        """Everything worth remembering, by a stable key."""
        for mode, tv in self.tabs.items():
            for name in ("source", "dest", "extract", "delete"):
                self._persist_bind(f"{mode}.{name}", getattr(tv, name))
            if tv.verify.isEnabled():  # pages that always verify have a fixed box
                self._persist_bind(f"{mode}.verify", tv.verify)
        for i, row in enumerate(self._org_sources):
            self._persist_bind(f"org.source.{i}", row)
        for i, box in enumerate(self._org_recursive):
            self._persist_bind(f"org.recursive.{i}", box)
        for name in ("chd_compression", "cso_speed", "rvz_scrub", "rvz_level", "thread_budget", "io_mode", "temp_dir",
                     "ps3_output", "pkg_layout", "pkg_merge", "pkg_zar", "ps5_kyty", "notify_check", "reduce_motion", "compact_box", "em_view_cards", "dl_watch", "dl_command", *SW_PERSIST):
            if (widget := getattr(self, name, None)) is not None:
                self._persist_bind(name, widget)
        for key, box in getattr(self, "_pin_boxes", {}).items():  # which tools are pinned to the sidebar
            self._persist_bind(f"pin.{key}", box)
        for key in TOOLS:
            if rows := self._tool_rows.get(key):
                self._persist_tool(key, rows[0])
        for key, page in self.pages.items():
            for card in page.findChildren(Expander):
                card.header.toggled.connect(self._persist_soon)

    def _persist_tool(self, key: str, row) -> None:
        """A tool path the person chose (Browse or typed). The path the app found or downloaded is never kept: it names a
        version folder that an update replaces, and the app finds its own copy again at every start."""
        def get() -> str:
            text = row.path.text().strip()
            return "" if text == self._auto.get(key, "") else text

        def put(value) -> None:
            value = str(value or "").strip()
            if value and (Path(resolve_tool_path(value)).exists() or shutil.which(value)):  # a copy that was moved or deleted is let go
                self._set_tool_text(key, value)
        self._p_binds[f"tool.{key}"] = (get, put)
        row.path.edit.textChanged.connect(self._persist_soon)

    @staticmethod
    def _card_id(page_key: str, card: Expander) -> str:
        return f"{page_key}/{card.property('settings_id') or card.header.text()}"

    def _settings_restore(self) -> None:
        self._p_data = data = read_settings()
        self._p_seen = data.get("last_version")  # the version that last ran here (None before this file existed)
        self._persist_widgets()
        for key, value in (data.get("values") or {}).items():
            if key in self._p_binds:
                try:
                    self._p_binds[key][1](value)
                except (TypeError, ValueError):
                    pass  # a stale or hand-edited value: keep the default
        folded = data.get("cards") or {}
        for key, page in self.pages.items():
            for card in page.findChildren(Expander):
                if (state := folded.get(self._card_id(key, card))) is not None:
                    card.header.setChecked(bool(state))
        geometry = data.get("geometry")
        if isinstance(geometry, str):
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        sizes = data.get("splitter")
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(n, int) and n > 0 for n in sizes):
            self._p_splitter = sizes
            if getattr(self, "_panel_mode", "conv") == "conv":
                self.splitter.setSizes(sizes)
        widths = data.get("columns") or {}
        self._p_columns = {k: v for k, v in widths.items() if isinstance(v, list)}
        self._restore_columns(self._p_columns)
        page = data.get("page")
        if isinstance(page, str) and page in self.pages:
            self._show_page(page)
        self.splitter.splitterMoved.connect(self._persist_soon)
        self._p_ready = True

    def _settings_save(self) -> None:
        if not getattr(self, "_p_ready", False):
            return
        data = dict(self._p_data)
        values = {}
        for key, (get, _put) in self._p_binds.items():
            try:
                values[key] = get()
            except (RuntimeError, TypeError):
                continue
        data["values"] = values
        data["cards"] = {self._card_id(key, card): card.header.isChecked()
                         for key, page in self.pages.items() for card in page.findChildren(Expander)}
        data["geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
        if getattr(self, "_panel_mode", "conv") == "conv":
            data["splitter"] = self._p_splitter = list(self.splitter.sizes())
        data["page"] = self.current_page
        cols = dict(getattr(self, "_p_columns", {}))
        cols.update(self._persist_columns())
        data["columns"] = cols
        data["last_version"] = APP_VERSION
        data["theme"] = "dark" if T.dark else "light"
        self._p_data = data
        write_settings(data)

    def _restore_columns(self, widths: dict[str, list]) -> None:
        """Hook: apply remembered column widths (the page mixins override this)."""

    def _maybe_whats_new(self) -> None:
        """Once per new version, for people who already used the app (a fresh install has nothing to compare to)."""
        if not SHOW_WHATS_NEW or not NOTES or self._p_seen == APP_VERSION or getattr(self, "_first_run", False):
            return
        self._p_seen = APP_VERSION
        WhatsNewDialog(self).exec()

    def _persist_columns(self) -> dict[str, list[int]]:
        """Column widths of the lists whose columns the person can drag (the page mixins override this)."""
        return {}

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._persist_soon()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._persist_soon()
