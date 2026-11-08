"""The Cores card on RetroArch's page on the Emulators page: every core on the libretro buildbot, ticked when it is in RetroArch's cores folder.
Tick or untick, then Apply; "Update all" brings every core Distill installed up to the latest build. All the work is in `distill.libretro_cores`;
this file lays it out and keeps the worker thread's results on the UI thread (events named `emu_cores_*`, routed by `_em_event`)."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QBrush, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem,
)

from .. import libretro_cores as lc
from .. import recomps as rc
from . import icons, msgbox
from .emu_cards import break_path, pill_css, section_header
from .theme import T
from .widgets import accent_button, Card, fit_columns, hint

CORE_FILTERS = ("All cores", "Installed", "Updates available", "Not installed", "For my library", "Recommended")
STATUS_TEXT = {"missing": "", "current": "Installed", "update": "Update available", "unknown": "Installed", "local": "Installed (not on server)"}
RETROARCH = "retroarch"
CORES_HELP = "Tick the cores you want installed, untick the ones you don't, then press Apply.  ★ marks our pick for a system: a judgment call, not a benchmark."


class CoresMixin:
    """Methods only; the state lives on DistillWindow (initialised by `_cores_init`, called from `_em_init`)."""

    def _cores_init(self) -> None:
        self._cores_cat: lc.Catalog | None = None
        self._cores_all: list[lc.Core] = []
        self._cores_loc: lc.Location | None = None
        self._cores_installed: dict[str, Path] = {}
        self._cores_record: dict[str, dict] = {}
        self._cores_want: dict[str, bool] = {}  # name -> ticked, only where the person changed it from what is installed
        self._cores_items: dict[str, QTreeWidgetItem] = {}
        self._cores_key: tuple = ()  # (folder, exe) the shown list belongs to
        self._cores_loading = False
        self._cores_error = ""
        self._cores_cancel: threading.Event | None = None
        self._cores_filling = False
        self._cores_recs: dict[str, list[tuple[str, str]]] = {}  # core -> the systems it is our pick for

    # ---- the card ---------------------------------------------------------------------------------------------
    def _cores_build_card(self) -> Card:
        card = Card()
        card.lay.setContentsMargins(20, 16, 20, 16)
        card.lay.setSpacing(10)
        head = section_header("Cores", "cube")
        self.emd_cores_count = QLabel()
        T.bind_css(self.emd_cores_count, lambda: pill_css(T.fg3))
        head.addWidget(self.emd_cores_count)
        card.add(head)
        self.emd_cores_where = hint("")
        card.add(self.emd_cores_where)

        row = QHBoxLayout()
        self.emd_cores_search = QLineEdit()
        self.emd_cores_search.setPlaceholderText("Search cores or systems")
        self.emd_cores_search.setClearButtonEnabled(True)
        self.emd_cores_search.textChanged.connect(lambda _t: self._cores_fill())
        row.addWidget(self.emd_cores_search, 1)
        self.emd_cores_filter = QComboBox()
        self.emd_cores_filter.addItems(CORE_FILTERS)
        self.emd_cores_filter.currentIndexChanged.connect(lambda _i: self._cores_fill())
        row.addWidget(self.emd_cores_filter)
        card.add(row)
        self.emd_cores_all_kinds = QCheckBox("Include game engines, media players and tools")
        self.emd_cores_all_kinds.toggled.connect(lambda _c: self._cores_fill())
        card.add(self.emd_cores_all_kinds)

        self.emd_cores_tree = QTreeWidget()
        self.emd_cores_tree.setHeaderLabels(("Core", "Systems", "Status", "BIOS"))
        self.emd_cores_tree.setRootIsDecorated(False)
        self.emd_cores_tree.setUniformRowHeights(True)
        self.emd_cores_tree.setAlternatingRowColors(True)
        self.emd_cores_tree.setMinimumHeight(340)
        self.emd_cores_tree.setSortingEnabled(True)
        self.emd_cores_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.emd_cores_tree.setColumnWidth(1, 170)
        self.emd_cores_tree.setColumnWidth(2, 130)
        self.emd_cores_tree.setColumnWidth(3, 64)
        fit_columns(self.emd_cores_tree, flex=0)  # every column drags (Qt can't drag a stretching one); Core takes the slack until you do
        self.emd_cores_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.emd_cores_tree.itemChanged.connect(self._cores_item_changed)
        self.emd_cores_tree.currentItemChanged.connect(lambda cur, _prev: self._cores_describe(cur))
        card.add(self.emd_cores_tree)
        self.emd_cores_info = hint(CORES_HELP)
        card.add(self.emd_cores_info)

        self.emd_cores_bar = QProgressBar()
        self.emd_cores_bar.setTextVisible(False)
        self.emd_cores_bar.setFixedHeight(8)
        T.bind_css(self.emd_cores_bar, lambda: f"QProgressBar {{ border: none; border-radius: 4px; background: {T.input.name()}; }}"
                                              f"QProgressBar::chunk {{ border-radius: 4px; background: {T.gradient_css()}; }}")
        self.emd_cores_bar.setVisible(False)
        card.add(self.emd_cores_bar)
        self.emd_cores_msg = hint("", live=True)
        card.add(self.emd_cores_msg)

        buttons = QHBoxLayout()
        self.emd_cores_apply = accent_button("Apply changes")
        self.emd_cores_apply.clicked.connect(self._cores_apply)
        buttons.addWidget(self.emd_cores_apply)
        self.emd_cores_update = QPushButton("Update all")
        self.emd_cores_update.setToolTip("Reinstall every core Distill put there whose build on the server is newer.")
        self.emd_cores_update.clicked.connect(self._cores_update_all)
        buttons.addWidget(self.emd_cores_update)
        self.emd_cores_cancel = QPushButton("Cancel")
        self.emd_cores_cancel.clicked.connect(self._cores_cancel_run)
        self.emd_cores_cancel.setVisible(False)
        buttons.addWidget(self.emd_cores_cancel)
        buttons.addStretch(1)
        self.emd_cores_refresh = QPushButton("Refresh list")
        self.emd_cores_refresh.clicked.connect(lambda: self._cores_load(force=True))
        buttons.addWidget(self.emd_cores_refresh)
        self.emd_cores_open = QPushButton("Open cores folder")
        self.emd_cores_open.clicked.connect(self._cores_open_folder)
        buttons.addWidget(self.emd_cores_open)
        card.add(buttons)
        card.setVisible(False)
        return card

    # ---- keeping the card in step with the page ---------------------------------------------------------------
    def _cores_sync(self) -> None:
        """Called whenever the details page refreshes: shows the card for an installed RetroArch, and (re)loads the list when that changed."""
        e = self._em_by_id.get(self._em_current)
        inst = self.em_store.installed(self._em_current) if e else None
        show = bool(e and e.adapter == "libretro_buildbot" and not e.parent and inst is not None and inst.exe_path().exists())
        self.emd_cores_card.setVisible(show)
        if not show or inst is None:
            self._cores_key = ()
            return
        key = (inst.folder, inst.exe)
        busy = e.id in self._em_busy or self._cores_cancel is not None
        if key != self._cores_key:
            self._cores_key = key
            self._cores_cat, self._cores_all, self._cores_want, self._cores_error = None, [], {}, ""
            self._cores_loc = lc.locate(inst.folder, inst.exe, inst.linked)
            self._cores_rescan()
            self._cores_fill()
            if self._cores_loc.ok:
                self._cores_load()
        for w in (self.emd_cores_search, self.emd_cores_filter, self.emd_cores_all_kinds):
            w.setEnabled(not busy)
        self._cores_buttons()

    def _cores_rescan(self) -> None:
        loc = self._cores_loc
        suffix = self._cores_cat.suffix if self._cores_cat else self._cores_suffix()
        self._cores_installed = lc.scan_installed(loc.cores if loc else None, suffix)
        self._cores_record = lc.load_record(loc.cores if loc else None)

    def _cores_suffix(self) -> str:
        try:
            return lc.target()[1]
        except rc.RecompError:
            return ""

    # ---- loading the list -------------------------------------------------------------------------------------
    def _cores_load(self, force: bool = False) -> None:
        inst = self.em_store.installed(self._em_current)
        if inst is None or self._cores_loc is None or not self._cores_loc.ok:
            return
        key, store = self._cores_key, self.em_store
        self._cores_loading = True
        self._cores_error = ""
        self._cores_buttons()
        self.emd_cores_msg.setText("Loading the list of cores...")

        def work() -> None:
            try:
                self.ui_q.put(("emu_cores_catalog", key, lc.fetch_catalog(store, force=force), ""))
            except rc.RecompError as exc:
                self.ui_q.put(("emu_cores_catalog", key, None, str(exc)))
            except Exception as exc:  # noqa: BLE001 - a worker must always answer, or the card says "Loading" forever
                self.ui_q.put(("emu_cores_catalog", key, None, f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _cores_event(self, ev: tuple) -> None:
        name = ev[0]
        if name == "emu_cores_catalog":
            _n, key, cat, error = ev
            if key != self._cores_key:
                return  # for a RetroArch the page has since left
            self._cores_loading = False
            if cat is None:
                self._cores_error = error or "Couldn't read the list of cores."
            else:
                self._cores_cat = cat
                self._cores_rescan()
                self._cores_all = lc.with_local_only(cat, self._cores_installed)
                self._cores_recs = lc.recommendations(cat.cores)
            self._cores_fill()
            self.emd_cores_msg.setText(self._cores_error)
            self._cores_buttons()
        elif name == "emu_cores_prog":
            _n, key, text, done, total = ev
            if key == self._cores_key:
                self.emd_cores_msg.setText(text)
                self.emd_cores_bar.setRange(0, total if total else 0)
                self.emd_cores_bar.setValue(done if total else 0)
        elif name == "emu_cores_done":
            _n, key, result = ev
            self._cores_cancel = None
            self.emd_cores_bar.setVisible(False)
            if key != self._cores_key:
                return
            self._cores_want = {}
            self._cores_rescan()
            if self._cores_cat is not None:
                self._cores_all = lc.with_local_only(self._cores_cat, self._cores_installed)
            self._cores_fill()
            self.emd_cores_msg.setText(result.summary())
            self._cores_sync()

    # ---- showing it -------------------------------------------------------------------------------------------
    def _cores_status(self, core: lc.Core) -> str:
        return lc.status_of(core, core.name in self._cores_installed, self._cores_record.get(core.name))

    def _cores_wanted(self, core: lc.Core) -> bool:
        return self._cores_want.get(core.name, core.name in self._cores_installed)

    def _cores_visible(self, core: lc.Core, text: str, mode: int, library: list[str]) -> bool:
        have = core.name in self._cores_installed
        if not self.emd_cores_all_kinds.isChecked() and not core.is_emulator and not have:
            return False
        if text and text not in f"{core.display} {core.name} {core.systems} {' '.join(core.extensions)}".lower():
            return False
        status = self._cores_status(core)
        if mode == 1:
            return have
        if mode == 2:
            return status == "update"
        if mode == 3:
            return not have
        if mode == 4:
            return any(lc.system_matches(core.systems, s) for s in library)
        if mode == 5:
            return core.name in self._cores_recs
        return True

    def _cores_fill(self) -> None:
        loc = self._cores_loc
        tree = self.emd_cores_tree
        if loc is not None and not loc.ok:
            self.emd_cores_where.setText(loc.reason)
        elif loc is not None and loc.cores is not None:
            self.emd_cores_where.setText(f"Cores folder: {break_path(str(loc.cores))}   ·   latest builds from buildbot.libretro.com")
        self._cores_filling = True
        tree.setSortingEnabled(False)
        tree.clear()
        self._cores_items = {}
        text = self.emd_cores_search.text().strip().lower()
        mode = self.emd_cores_filter.currentIndex()
        library = [s for s, n in self._em_counts.items() if n]
        ok, warn, mute = T.ok, T.warn, T.fg3
        star = icons.icon("star", warn, 14)
        blank_px = QPixmap(28, 28)  # an empty icon the size of the star, so names line up whether or not they have one
        blank_px.setDevicePixelRatio(2.0)
        blank_px.fill(Qt.GlobalColor.transparent)
        blank = QIcon(blank_px)
        for core in self._cores_all:
            if not self._cores_visible(core, text, mode, library):
                continue
            status = self._cores_status(core)
            item = QTreeWidgetItem([core.display, core.systems, STATUS_TEXT[status], ("Required" if core.needs_firmware else "Optional") if core.firmware else ""])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if self._cores_wanted(core) else Qt.CheckState.Unchecked)
            item.setData(0, Qt.ItemDataRole.UserRole, core.name)
            if picks := self._cores_recs.get(core.name):
                item.setIcon(0, star)
                item.setToolTip(0, "\n".join(f"★ Our pick for {system}: {why}" for system, why in picks))
            else:
                item.setIcon(0, blank)
            if status == "update":
                item.setForeground(2, QBrush(warn))
            elif status in ("current", "unknown"):
                item.setForeground(2, QBrush(ok))
            if core.firmware:
                item.setForeground(3, QBrush(warn if core.needs_firmware else mute))
            tree.addTopLevelItem(item)
            self._cores_items[core.name] = item
        tree.setSortingEnabled(True)
        self._cores_filling = False
        n = len(self._cores_installed)
        self.emd_cores_count.setText(f"{n} installed")
        self.emd_cores_count.setVisible(self._cores_loc is not None and self._cores_loc.ok)
        if not self._cores_all and self._cores_loc is not None and self._cores_loc.ok and not self._cores_loading:
            self.emd_cores_info.setText("" if self._cores_error else "No cores to show yet.")  # the error itself is the status line below
            self.emd_cores_info.setVisible(not self._cores_error)
        else:
            self.emd_cores_info.setVisible(True)
            self._cores_describe(tree.currentItem())
        self._cores_buttons()

    def _cores_describe(self, item: QTreeWidgetItem | None) -> None:
        core = next((c for c in self._cores_all if item is not None and c.name == item.data(0, Qt.ItemDataRole.UserRole)), None)
        if core is None:
            self.emd_cores_info.setText(CORES_HELP)
            return
        bits = [f"★ Our pick for {system}: {why}" for system, why in self._cores_recs.get(core.name, [])]
        if core.description.strip():
            bits.append(core.description.strip())
        if core.extensions:
            bits.append("Plays: " + ", ".join("." + x for x in core.extensions[:24]) + (" ..." if len(core.extensions) > 24 else ""))
        for desc, path, optional in core.firmware[:6]:
            bits.append(f"BIOS {'(optional)' if optional else '(required)'}: {path}" + (f" - {desc}" if desc and desc != path else ""))
        if core.license:
            bits.append(f"License: {core.license}")
        if not core.remote:
            bits.append("Not on the server: Distill can't update it, only remove it.")
        self.emd_cores_info.setText("\n".join(bits) or core.display)

    def _cores_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._cores_filling or column != 0:
            return
        name = item.data(0, Qt.ItemDataRole.UserRole)
        want = item.checkState(0) == Qt.CheckState.Checked
        if want == (name in self._cores_installed):
            self._cores_want.pop(name, None)
        else:
            self._cores_want[name] = want
        self._cores_buttons()

    def _cores_plan(self) -> tuple[list[lc.Core], list[lc.Core]]:
        by = {c.name: c for c in self._cores_all}
        add = [by[n] for n, w in self._cores_want.items() if w and n in by and n not in self._cores_installed and by[n].remote]
        drop = [by[n] for n, w in self._cores_want.items() if not w and n in by and n in self._cores_installed]
        return add, drop

    def _cores_updates(self) -> list[lc.Core]:
        return [c for c in self._cores_all if self._cores_status(c) == "update"]

    def _cores_buttons(self) -> None:
        loc = self._cores_loc
        e_busy = self._em_current in self._em_busy
        running = self._cores_cancel is not None
        ready = bool(loc and loc.ok and self._cores_cat is not None and not running and not e_busy)
        add, drop = self._cores_plan()
        parts = ([f"+{len(add)}"] if add else []) + ([f"-{len(drop)}"] if drop else [])
        self.emd_cores_apply.setText(f"Apply changes ({' '.join(parts)})" if parts else "Apply changes")
        self.emd_cores_apply.setEnabled(ready and bool(parts))
        updates = self._cores_updates()
        self.emd_cores_update.setText(f"Update all ({len(updates)})" if updates else "Update all")
        self.emd_cores_update.setEnabled(ready and bool(updates))
        self.emd_cores_refresh.setEnabled(bool(loc and loc.ok) and not running and not self._cores_loading)
        self.emd_cores_open.setEnabled(bool(loc and loc.cores is not None))
        self.emd_cores_cancel.setVisible(running)
        self.emd_cores_tree.setEnabled(bool(loc and loc.ok) and not running)

    # ---- doing it ---------------------------------------------------------------------------------------------
    def _cores_apply(self) -> None:
        add, drop = self._cores_plan()
        if not (add or drop):
            return
        if drop:
            names = ", ".join(c.display for c in drop[:4]) + (f" and {len(drop) - 4} more" if len(drop) > 4 else "")
            ask = f"Delete {len(drop)} core{'s' if len(drop) != 1 else ''} from RetroArch's cores folder?\n\n{names}"
            if msgbox.question(self, "Remove cores", ask) != QMessageBox.StandardButton.Yes:
                return
        self._cores_run(add, drop)

    def _cores_update_all(self) -> None:
        if updates := self._cores_updates():
            self._cores_run(updates, [])

    def _cores_run(self, add: list[lc.Core], drop: list[lc.Core]) -> None:
        loc, cat = self._cores_loc, self._cores_cat
        if loc is None or cat is None or self._cores_cancel is not None:
            return
        cancel = self._cores_cancel = threading.Event()
        key, store = self._cores_key, self.em_store
        self.emd_cores_bar.setRange(0, 0)
        self.emd_cores_bar.setVisible(True)
        self.emd_cores_msg.setText("Starting...")
        self._cores_sync()

        def work() -> None:
            try:
                result = lc.apply_changes(store, loc, cat, add, drop, cancel=cancel,
                                          progress=lambda text, done, total: self.ui_q.put(("emu_cores_prog", key, text, done, total)))
            except Exception as exc:  # noqa: BLE001 - the card must always come back from "working"
                result = lc.BatchResult([], [], {"cores": f"{type(exc).__name__}: {exc}"})
            self.ui_q.put(("emu_cores_done", key, result))

        threading.Thread(target=work, daemon=True).start()

    def _cores_cancel_run(self) -> None:
        if self._cores_cancel is not None:
            self._cores_cancel.set()
            self.emd_cores_msg.setText("Cancelling...")

    def _cores_open_folder(self) -> None:
        loc = self._cores_loc
        if loc is not None and loc.cores is not None:
            loc.cores.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(loc.cores)))
