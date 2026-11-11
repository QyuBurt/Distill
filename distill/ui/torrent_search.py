"""The 'Search torrents' pop-up on the Download page: type a query, see names / sizes / seeders / peers, pick rows and
add them to the download list as magnet links. The searching itself lives in torrentsearch.py (no Qt there)."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
    QWidget,
)

from .. import torrentsearch as ts
from ..models import fmt_size
from ..torbox import torbox_cached, torbox_key
from .frameless import FramelessDialog
from .theme import T
from .widgets import accent_button, AnimCheckBox, fit_columns, hint

COLS = ("Name", "Size", "Seeders", "Peers", "Age", "Source", "TorBox")
NAME, SIZE, SEEDS, PEERS, AGE, SOURCE, CACHED = range(len(COLS))
SORT_ROLE = Qt.ItemDataRole.UserRole + 1  # what a column sorts by (the text is for people)


def fmt_age(added: int, now: float | None = None) -> str:
    if added <= 0:
        return ""
    days = int(((now if now is not None else time.time()) - added) // 86400)
    if days < 1:
        return "today"
    if days < 31:
        return f"{days} d"
    if days < 365:
        return f"{days // 30} mo"
    return f"{days // 365} y"


class ResultItem(QTreeWidgetItem):
    """One result row. Numbers sort as numbers ('900' before '1,200' would be wrong as text)."""

    def __init__(self, result: ts.SearchResult):
        super().__init__()
        self.result = result
        texts = (result.name, fmt_size(result.size) if result.size else "", str(result.seeders), str(result.leechers),
                 fmt_age(result.added), ", ".join(result.sources), "")
        keys = (result.name.lower(), result.size, result.seeders, result.leechers, result.added, texts[SOURCE], 0)
        for col, (text, key) in enumerate(zip(texts, keys)):
            self.setText(col, text)
            self.setData(col, SORT_ROLE, key)
            if col in (SIZE, SEEDS, PEERS, AGE):
                self.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setToolTip(NAME, result.name)
        self.mark_cached(False)

    def mark_cached(self, cached: bool) -> None:
        self.setText(CACHED, "Cached" if cached else "")
        self.setData(CACHED, SORT_ROLE, 1 if cached else 0)
        self.setForeground(CACHED, QBrush(T.ok))

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn() if self.treeWidget() else NAME
        a, b = self.data(col, SORT_ROLE), other.data(col, SORT_ROLE)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class TorrentSearchDialog(FramelessDialog):
    """Modeless, so the download list stays visible behind it while rows are added."""

    addLinks = Signal(list)  # magnet links for the download list
    _found = Signal(int, object, object)  # token, results, notes   (worker thread -> here)
    _cached = Signal(int, object)  # token, {info-hash} TorBox has
    _resolved = Signal(object, object)  # magnets, error notes

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, "Search torrents", "search", (920, 560), (700, 420))
        self.setModal(False)
        self._token = 0
        self._busy_adding = False
        self._found.connect(self._show_results)
        self._cached.connect(self._show_cached)
        self._resolved.connect(self._added)
        lay = self.body((20, 18, 20, 16))

        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("What do you want to find?")
        self.query.setClearButtonEnabled(True)
        self.query.returnPressed.connect(self.run_search)
        self.search_btn = accent_button("Search")
        self.search_btn.clicked.connect(self.run_search)
        row.addWidget(self.query, 1)
        row.addWidget(self.search_btn)
        lay.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(SEEDS, Qt.SortOrder.DescendingOrder)
        for col, width in ((SIZE, 90), (SEEDS, 76), (PEERS, 64), (AGE, 60), (SOURCE, 130), (CACHED, 74)):
            self.tree.setColumnWidth(col, width)
        fit_columns(self.tree, NAME)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.itemDoubleClicked.connect(lambda *_: self.add_selected())
        lay.addWidget(self.tree, 1)

        self.status = hint("", live=True)
        lay.addWidget(self.status)

        bottom = QHBoxLayout()
        self.hide_dead = AnimCheckBox("Hide torrents with no seeders")
        self.hide_dead.setChecked(True)
        self.hide_dead.toggled.connect(self._apply_filter)
        self.add_btn = accent_button("Add to downloads")
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self.add_selected)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bottom.addWidget(self.hide_dead)
        bottom.addStretch(1)
        bottom.addWidget(close)
        bottom.addWidget(self.add_btn)
        lay.addLayout(bottom)

        self.status.setText("Searches Knaben and Torrents-CSV with no setup" +
                            (" and your Prowlarr indexers." if ts.prowlarr_config() else
                             ". Add Prowlarr under Settings > API keys > Downloads for many more sites."))
        self.query.setFocus()

    # ---- searching --------------------------------------------------------------------------------------------
    def run_search(self) -> None:
        query = " ".join(self.query.text().split())
        if not query:
            return
        self._token += 1
        self.tree.clear()
        self.search_btn.setEnabled(False)
        self.status.setText("Searching...")
        threading.Thread(target=self._work, args=(self._token, query), daemon=True).start()

    def _work(self, token: int, query: str) -> None:  # worker thread: only signals leave it
        try:
            results, notes = ts.search(query)
        except Exception as exc:
            results, notes = [], [str(exc)]
        self._found.emit(token, results, notes)
        if results and torbox_key():  # which of them TorBox already has: those download over HTTPS, no swarm
            hashes = list(dict.fromkeys(r.infohash for r in results if r.infohash))[:300]
            try:
                self._cached.emit(token, set(torbox_cached(hashes)))
            except Exception:
                pass  # a badge is a nicety; the list is already on screen

    def _show_results(self, token: int, results: list, notes: list) -> None:
        if token != self._token:
            return  # an older search finishing after a newer one started
        self.search_btn.setEnabled(True)
        self.tree.setSortingEnabled(False)
        self.tree.addTopLevelItems([ResultItem(r) for r in results])
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(SEEDS, Qt.SortOrder.DescendingOrder)
        self._apply_filter()
        shown = sum(1 for i in range(self.tree.topLevelItemCount()) if not self.tree.topLevelItem(i).isHidden())
        text = (f"{shown} result{'s' if shown != 1 else ''}" if results else "No results") + \
               (f" ({len(results) - shown} with no seeders hidden)" if results and shown != len(results) else "")
        if notes:
            text += ". " + "; ".join(notes)
        self.status.setText(text)

    def _show_cached(self, token: int, have: set) -> None:
        if token != self._token:
            return
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if isinstance(item, ResultItem) and item.result.infohash in have:
                item.mark_cached(True)

    def _apply_filter(self) -> None:
        hide = self.hide_dead.isChecked()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if isinstance(item, ResultItem):
                item.setHidden(hide and item.result.seeders == 0)

    def _selection_changed(self) -> None:
        self.add_btn.setEnabled(bool(self.tree.selectedItems()) and not self._busy_adding)

    # ---- adding ---------------------------------------------------------------------------------------------
    def add_selected(self) -> None:
        picked = [i.result for i in self.tree.selectedItems() if isinstance(i, ResultItem)]
        if not picked or self._busy_adding:
            return
        self._busy_adding = True
        self.add_btn.setEnabled(False)
        self.status.setText("Adding...")
        threading.Thread(target=self._resolve, args=(picked,), daemon=True).start()

    def _resolve(self, picked: list) -> None:  # worker thread: a result with only a .torrent link needs a download
        magnets, notes = [], []
        for result in picked:
            try:
                magnets.append(ts.to_magnet(result))
            except ts.SearchError as exc:
                notes.append(f"{result.name}: {exc}")
            except Exception as exc:
                notes.append(f"{result.name}: {exc}")
        self._resolved.emit(magnets, notes)

    def _added(self, magnets: list, notes: list) -> None:
        self._busy_adding = False
        self._selection_changed()
        if magnets:
            self.addLinks.emit(magnets)
        text = f"Added {len(magnets)} to the download list" if magnets else "Nothing was added"
        self.status.setText(text + (". " + "; ".join(notes) if notes else "."))

    # ---- for tests --------------------------------------------------------------------------------------------
    def show_results(self, results: list, notes: list | None = None) -> None:
        """Put results on screen as if a search had just returned them."""
        self._found.emit(self._token, results, notes or [])
