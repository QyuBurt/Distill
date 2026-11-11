"""Organize pages: scan a library for fixes, apply the ticked ones, undo the last run."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from ..constants import APP_NAME, APP_VERSION
from ..models import fmt_size
from ..library import write_report
from ..organize import (
    apply_findings, Finding, report_rows, run_log_file, save_report, undo_depth, undo_last,
)
from ..orgpages import newest_undo_step, ORG_PAGES, ORG_TOOLS, OrgPage, OrgTool, scan_page
from .state import LABEL_W, OrgState
from .theme import T
from . import motion
from .treeslide import TreeSlide
from .widgets import (AnimCheckBox, Card, CheckTree, Expander, fit_columns, hint, PathRow, RevealDelegate,
                      StepCard, SwitchToggle)


class OrganizeMixin:
    """Organize pages: scan a library for fixes, apply the ticked ones, undo the last run.

    Methods only; the state lives on DistillWindow, which mixes this in."""

    @staticmethod
    def _sync_group(widgets: list, getter: str, setter: str, signal: str) -> None:
        """Several pages show one shared setting: when one changes, the others follow."""
        def changed(value, source) -> None:
            for w in widgets:
                if w is not source and getattr(w, getter)() != value:
                    getattr(w, setter)(value)
        for w in widgets:
            getattr(w, signal).connect(lambda value, src=w: changed(value, src))

    @staticmethod
    def _put(layout, item) -> None:
        (layout.addWidget if isinstance(item, QWidget) else layout.addLayout)(item)

    def _org_add_option(self, st: OrgState, layout, option: tuple) -> None:
        """One option control, added to `layout`; its current value is read through st.opts[name]."""
        name, kind, label, default, *extra = option
        if kind == "check":
            box = AnimCheckBox(label)
            box.setChecked(bool(default))
            self._put(layout, box)
            st.opts[name] = box.isChecked
        elif kind == "switch":  # (name, "switch", label, default, off text, on text, tooltip): styled like the RomM switch
            off_text, on_text, tip = extra
            row = QHBoxLayout()
            row.setSpacing(8)
            sw = SwitchToggle()
            sw.setToolTip(tip)
            off, on = QLabel(off_text), QLabel(on_text)
            for lab, value in ((off, False), (on, True)):  # a click on a word flips the switch too
                lab.setCursor(Qt.CursorShape.PointingHandCursor)
                lab.setToolTip(tip)
                lab.mousePressEvent = lambda _e, v=value: sw.setChecked(v)

            def look(*_args, sw=sw, off=off, on=on) -> None:
                for lab, active in ((off, not sw.isChecked()), (on, sw.isChecked())):
                    font = lab.font()
                    font.setBold(active)
                    lab.setFont(font)
                self._org_option_changed(st)
            caption = QLabel(label)
            caption.setFixedWidth(LABEL_W)
            for w in (caption, off, sw, on):
                row.addWidget(w)
            row.addStretch(1)
            self._put(layout, row)
            sw.toggled.connect(look)
            sw.setChecked(bool(default))
            look()
            st.opts[name] = sw.isChecked
        elif kind in ("folder", "file"):  # a "file" option: (name, "file", label, default, placeholder, dialog filter)
            row = (PathRow(label, label_width=LABEL_W, placeholder=str(extra[0]) if extra else "") if kind == "folder" else
                   PathRow(label, folder=False, label_width=LABEL_W, placeholder=str(extra[0]) if extra else "",
                           file_filter=str(extra[1]) if len(extra) > 1 else "", file_title="Select File"))
            row.setText(str(default))
            self._put(layout, row)
            st.opts[name] = lambda r=row: self._clean(r.text())
            st.rows[name] = row
        elif kind == "text":
            caption = QLabel(label)
            caption.setWordWrap(True)
            self._put(layout, caption)
            edit = QLineEdit(str(default))
            edit.setPlaceholderText(str(extra[0]) if extra else "{title} ({region}) {disc}")
            self._put(layout, edit)
            st.opts[name] = edit.text
        else:
            h = QHBoxLayout()
            h.addWidget(QLabel(label))
            cb = QComboBox()
            cb.addItems(list(extra[0]))
            cb.setCurrentText(default)
            cb.setMinimumWidth(320)
            h.addWidget(cb)
            h.addStretch(1)
            self._put(layout, h)
            st.opts[name] = cb.currentText

    def _org_option_changed(self, st: OrgState) -> None:
        """An option that only changes what a finished scan would do (the patch page's stacking switch)."""
        if st.page.key == "patch" and st.findings and st.tree is not None and "stack" in st.opts:
            self._patch_refresh(st.page.key)
            self._patch_status(st.page.key)

    def _build_org_page(self, page_def: OrgPage) -> QWidget:
        st = OrgState(page_def)
        tool = st.tool
        self.org[page_def.key] = st
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        folder = Expander("Games Folder", expanded=True)
        source = PathRow("Source folder", label_width=LABEL_W)
        self._org_sources.append(source)
        if len(self._org_sources) > 1:
            source.setText(self._org_sources[0].text())
        folder.add(source)
        bar = QHBoxLayout()
        recursive = AnimCheckBox("Include subfolders")
        recursive.setChecked(True if not self._org_recursive else self._org_recursive[0].isChecked())
        self._org_recursive.append(recursive)
        st.scan_btn = QPushButton("Scan")
        st.scan_btn.clicked.connect(lambda _c=False, k=page_def.key: self._org_scan(k))
        st.undo_btn = QPushButton("Undo last run")
        st.undo_btn.setToolTip("Steps back one run at a time" + (" (whichever step on this page ran last)"
                                                                  if page_def.multi else "")
                               + f". Every run is also written to {run_log_file().name}.")
        st.undo_btn.clicked.connect(lambda _c=False, k=page_def.key: self._org_undo(k))
        st.report_btn = QPushButton("Save preview...")
        st.report_btn.setToolTip("Save everything the scan found, and what applying it would do to each file, as a "
                                 "text or spreadsheet file - before anything is changed.")
        st.report_btn.setEnabled(False)
        st.report_btn.clicked.connect(lambda _c=False, k=page_def.key: self._org_save_preview(k))
        bar.addWidget(recursive)
        bar.addSpacing(12)
        bar.addWidget(st.scan_btn)
        bar.addStretch(1)
        if not tool.export:  # the library report changes nothing: no preview file, nothing to undo
            bar.addWidget(st.report_btn)
            bar.addWidget(st.undo_btn)
        folder.add(bar)
        lay.addWidget(folder)

        names = [o[0] for step in page_def.steps for o in step.options]
        shared = {n for n in names if names.count(n) > 1}  # e.g. 'delete permanently': one box for the whole page
        if page_def.multi:
            for n, step in enumerate(page_def.steps, 1):  # one card per step, in the order they run
                card = StepCard(n, step.title, step.blurb)
                st.step_boxes[step.key] = card.switch
                st.step_cards[step.key] = card
                for option in step.options:
                    if option[0] not in shared:
                        self._org_add_option(st, card.content_lay, option)
                lay.addWidget(card)
            if shared:  # shared options come last, once, whichever steps are ticked
                card = Card("Options")
                done: set[str] = set()
                for step in page_def.steps:
                    for option in step.options:
                        if option[0] in shared and option[0] not in done:
                            done.add(option[0])
                            self._org_add_option(st, card.lay, option)
                lay.addWidget(card)
        elif names:
            card = Expander("Options", expanded=True)
            for option in page_def.steps[0].options:
                self._org_add_option(st, card.body_lay, option)
            lay.addWidget(card)

        results = Card("Scan Results")
        results.add(hint("Tick the patches to apply; right-click a game or a patch to match them by hand."
                         if page_def.key == "patch" else "Click a row to tick or untick it. The arrow shows its details."))
        tools_bar = QHBoxLayout()
        for text, fn in (("Select all", lambda _c=False, k=page_def.key: self._org_select(k, True)),
                         ("Clear all", lambda _c=False, k=page_def.key: self._org_select(k, False)),
                         ("Expand all", lambda _c=False, k=page_def.key: self._org_expand(k, True)),
                         ("Collapse all", lambda _c=False, k=page_def.key: self._org_expand(k, False))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            tools_bar.addWidget(b)
        tools_bar.addStretch(1)
        results.add(tools_bar)
        st.steps_label = hint("")
        st.steps_label.setVisible(False)
        results.add(st.steps_label)
        tree = st.tree = CheckTree()
        st.reveal = RevealDelegate(tree)  # the same cascade as the Soundtrack Grabber's list
        tree.setItemDelegate(st.reveal)
        tree.placeholder = "Nothing scanned yet.\nChoose a folder above and press Scan to see what can be done."
        tree.setColumnCount(3)
        tree.setHeaderLabels([tool.headings[0], tool.headings[1], "Status"])
        tree.setRootIsDecorated(True)
        tree.setExpandsOnDoubleClick(False)
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(False)
        tree.setMinimumHeight(140 if names or page_def.multi else 180)
        tree.setColumnWidth(1, 80)
        tree.setColumnWidth(2, 260)
        fit_columns(tree, flex=0)  # every column draggable; the first soaks up the slack
        st.slide = TreeSlide(tree)  # a finding's details are written in when it is opened, as the tracks are
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(lambda pos, k=page_def.key: self._org_context(k, pos))
        tree.itemClicked.connect(lambda item, _col, k=page_def.key: self._org_toggle_item(k, item))
        tree.spacePressed.connect(lambda item, k=page_def.key: self._org_toggle_item(k, item))
        results.add(tree)
        st.status = hint("Choose your games folder and press Scan.", live=True)
        results.add(st.status)
        lay.addWidget(results)

        if len(self._org_sources) == len(ORG_PAGES):  # every Organize page exists now: link the shared settings
            self._sync_group([s.edit for s in self._org_sources], "text", "setText", "textChanged")
            self._sync_group(self._org_recursive, "isChecked", "setChecked", "toggled")
        return page

    def _org_set_busy(self, busy: bool, label: str = "Working...") -> None:
        self.org_busy = busy
        self._org_label = label
        if busy and label != "Scanning...":
            self._org_page = self.current_page  # the page this job was started from: the bar along the bottom leads back to it
        for st in self.org.values():
            st.scan_btn.setEnabled(not busy)
            st.undo_btn.setEnabled(not busy)
            st.report_btn.setEnabled(not busy and bool(st.findings))
        self._sync_run_controls()  # Stop is live while it runs (an Organize run can be stopped between items; each finished item stays undoable)
        self._runbar_sync()

    def _org_blocked(self) -> bool:
        """Organise moves files about, so it waits for a conversion or a download to finish (and the other way round)."""
        return self.org_busy or self.is_processing or self.dl_runner is not None

    def _org_folder(self) -> str:
        return self._clean(self._org_sources[0].text())

    def _org_scan(self, key: str) -> None:
        if self._org_blocked():
            return
        raw = self._org_folder()
        if not raw or not Path(raw).is_dir():
            self._error("Error", f"Please select a valid games folder.\n\nPath checked: {raw}")
            return
        st = self.org[key]
        st.token += 1
        token, root, recursive, page = st.token, Path(raw), self._org_recursive[0].isChecked(), st.page
        opts = {name: getter() for name, getter in st.opts.items()}
        if key == "patch":
            opts["_manual"] = list(st.manual)  # pairs chosen by hand survive a rescan
        if st.step_boxes:
            opts["_steps"] = {k for k, box in st.step_boxes.items() if box.isChecked()}
        opts["_note"] = lambda text: self.ui_q.put(("org_note", key, token, text))  # progress for slow scans (checksums)
        self._org_set_busy(True, "Scanning...")
        st.status.setText("Scanning...")
        self._org_stop.clear()

        def work() -> None:
            step = ""
            try:
                findings, message, step = scan_page(page, root, recursive, opts, self._org_stop)
                error = ""
            except Exception as exc:
                findings, message, error = [], "", f"{type(exc).__name__}: {exc}"
            self.ui_q.put(("org_scan", key, token, findings, message, error, step))

        threading.Thread(target=work, daemon=True).start()

    def _org_show(self, key: str, findings: list[Finding], message: str, error: str, step: str = "") -> None:
        st = self.org[key]
        self._org_set_busy(False)
        if step in ORG_TOOLS and ORG_TOOLS[step] in st.page.steps:  # a multi-step page moves to the step with work
            st.tool = ORG_TOOLS[step]
            st.tree.setHeaderLabels([st.tool.headings[0], st.tool.headings[1], "Status"])
            if self.current_page == key:
                self._refresh_start_label()
        st.findings, st.message = findings, message
        if st.tool.key == "patch":
            self._patch_show(key, findings, message, error)
            return
        st.checked = {i for i, f in enumerate(findings) if not f.problem and f.checked}
        tree = st.tree
        st.slide.cancel()  # no fold may carry on into rows that are about to go
        tree.clear()
        row_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable  # ticking is done by the click handler
        for i, f in enumerate(findings):
            item = QTreeWidgetItem([f.title, str(f.count), f.status])
            item.setFlags(row_flags)
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            item.setToolTip(0, f.title)
            if f.problem:  # shown, but can't be ticked
                item.setForeground(2, T.bad)
            else:
                item.setCheckState(0, Qt.CheckState.Checked if i in st.checked else Qt.CheckState.Unchecked)
            tree.addTopLevelItem(item)
            for name, count, note in f.children:
                child = QTreeWidgetItem([name, str(count), note])
                child.setFlags(row_flags)
                item.addChild(child)
        st.reveal.stagger(len(findings))  # the results ripple in, row by row
        self._set_badge(key, len(findings))
        st.report_btn.setEnabled(bool(findings))
        if error:
            st.status.setText(f"Scan failed: {error}")
        elif not findings:
            st.status.setText(("All steps are clean." if st.page.multi else st.tool.empty)
                              + (f"  ({message})" if message else ""))
        else:
            self._org_update_status(key)
        if st.page.multi and not error:
            ticked = [t for t in st.page.steps if st.step_boxes[t.key].isChecked()]
            showing = bool(findings) and st.tool in ticked
            if showing:
                st.steps_label.setText(f"Step {ticked.index(st.tool) + 1} of {len(ticked)}: {st.tool.title}. "
                                       "Apply it and the next step is scanned for you.")
            motion.fade(st.steps_label, showing)
            for key, card in st.step_cards.items():  # the card whose results are listed lights up
                card.set_current(showing and key == st.tool.key)

    def _org_update_status(self, key: str) -> None:
        st = self.org[key]
        ticked = [st.findings[i] for i in st.checked]
        text = f"{len(ticked)} of {len(st.findings)} ticked"
        if freed := sum(f.size for f in ticked):
            text += f"  |  frees {fmt_size(freed)}"
        if blocked := sum(1 for f in st.findings if f.problem):
            text += f"  |  {blocked} can't be applied (see Status)"
        if st.message:
            text += f"  |  {st.message}"
        st.status.setText(text + ".")

    def _org_context(self, key: str, pos) -> None:
        if self.org[key].tool.key == "patch":
            self._patch_menu(key, pos)

    def _org_toggle_item(self, key: str, item: QTreeWidgetItem) -> None:
        st = self.org[key]
        if st.tool.key == "patch":
            self._patch_toggle(key, item)
            return
        if self.org_busy or item.parent() is not None:  # detail rows under an item aren't ticked on their own
            return
        i = item.data(0, Qt.ItemDataRole.UserRole)
        if i is None or st.findings[i].problem:
            return
        st.checked ^= {i}
        item.setCheckState(0, Qt.CheckState.Checked if i in st.checked else Qt.CheckState.Unchecked)
        self._org_update_status(key)

    def _org_select(self, key: str, on: bool) -> None:
        st = self.org[key]
        if self.org_busy:
            return
        if st.tool.key == "patch":
            self._patch_select(key, on)
            return
        for i, f in enumerate(st.findings):
            if f.problem:
                continue
            (st.checked.add if on else st.checked.discard)(i)
            if (item := st.tree.topLevelItem(i)) is not None:
                item.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self._org_update_status(key)

    def _org_expand(self, key: str, open_: bool) -> None:
        st = self.org[key]
        st.slide.set_expanded([st.tree.topLevelItem(i) for i in range(st.tree.topLevelItemCount())], open_)

    def _org_apply(self) -> None:
        if self.org_busy:
            return
        if self.is_processing or self.dl_runner is not None:
            self._toast("A run is in progress: apply this when it has finished.", "warn", 5000)
            return
        key = self.current_page
        st = self.org[key]
        tool = st.tool
        if tool.key == "patch":
            picks, pick_rows = self._patch_picks(key)
        else:
            picks = [st.findings[i] for i in sorted(st.checked) if not st.findings[i].problem]
            pick_rows = [i for i in sorted(st.checked) if not st.findings[i].problem]  # tree row of each pick
        if not picks:
            self._toast("Scan a folder and tick at least one item first." if tool.key != "patch"
                        else "Scan a folder and tick at least one patch first.", "warn", 6000)
            return
        if tool.export:
            self._org_export(key, picks)
            return
        if tool.confirm and not self._ask(tool.title, tool.confirm(picks, {n: g() for n, g in st.opts.items()})):
            return
        self._org_run(key, tool, picks, pick_rows)

    def _org_run(self, key: str, tool: OrgTool, picks: list, pick_rows: list[int]) -> None:
        """Apply these findings on a worker thread; pick_rows[n] is the tree row that shows pick n's progress."""
        if self.org_busy:
            return
        st = self.org[key]
        self._org_set_busy(True, tool.busy)
        st.status.setText(f"Working on {len(picks)} item(s)...")
        self._org_stop.clear()
        self._log_clear()
        self.ui_q.put(("log", f"=== {APP_NAME} {APP_VERSION} ===\n--- {tool.title} started: {len(picks)} item(s) ---\n"))
        self._update_ui_state("Organising", 0, len(picks))

        def work() -> None:
            ok, failures = 0, []
            try:
                ok, failures = apply_findings(
                    tool.key, picks, lambda m: self.ui_q.put(("log", m)),
                    lambda cur, total: self.ui_q.put(("progress", "Organising", cur, total)), self._org_stop,
                    lambda n, text: self.ui_q.put(("org_row", key, pick_rows[n], text)), tool.title)
            except Exception as exc:
                failures = [("(run aborted)", f"{type(exc).__name__}: {exc}")]
            self.ui_q.put(("org_done", key, ok, failures))

        threading.Thread(target=work, daemon=True).start()

    def _org_finished(self, key: str, ok: int, failures: list[tuple[str, str]]) -> None:
        self._org_set_busy(False)
        self._title_progress(None)
        if not self._org_stop.is_set():
            self._notify_finished("Distill: " + self.org[key].tool.title, f"Done: {ok} item(s)"
                                  + (f", {len(failures)} failed." if failures else "."))
        stopped = self._org_stop.is_set()
        self.lbl_phase.setText("Phase: Stopped" if stopped else "Phase: Complete")
        self.ui_q.put(("log", f"\n--- {self.org[key].tool.title} {'stopped' if stopped else 'complete'}: "
                              f"{ok} done, {len(failures)} failed ---"))
        text = f"Done: {ok} item(s)." + ("\n\nStopped early: the remaining items were not touched." if stopped else "")
        if failures:
            text += "\n\nCouldn't be applied (put back where they were):\n" + "\n".join(
                f"  \u2022 {name}: {why}" for name, why in failures[:12])
        if ok:
            text += "\n\nYou can reverse this with 'Undo last run'."
            if self.org[key].page.multi:
                text += "\nThe next step with something to do is shown next."
        (self._warn if failures else self._info)(self.org[key].tool.title, text)
        self._org_scan(key)  # refresh the list: finished items drop off

    def _org_undo(self, key: str) -> None:
        if self._org_blocked():
            return
        page = self.org[key].page
        step = newest_undo_step([t.key for t in page.steps])
        if step is None:
            self._toast(f"There is no earlier '{page.title}' run to undo.", "info", 6000)
            return
        tool = ORG_TOOLS[step]
        if not self._ask(f"Undo {tool.title}", "Reverse everything the last run changed (files moved back, "
                                               "playlists restored, folders it made removed)?"):
            return
        result = undo_last(step)
        if result is None:
            self._error("Undo Failed", "The undo record couldn't be read.")
            return
        count, problems = result
        text = f"Restored {count} item(s)."
        if later := undo_depth(step):
            text += f"  {later} earlier run(s) can still be undone."
        if problems:
            text += "\n\nSome things couldn't be restored:\n" + "\n".join(f"  \u2022 {p}" for p in problems[:12])
        (self._warn if problems else self._info)(f"Undo {tool.title}", text)
        if self._org_folder():
            self._org_scan(key)

    def _org_default_report_name(self, tool: OrgTool) -> str:
        folder = Path(self._org_folder()) if self._org_folder() else Path.home()
        return str(folder / ("Library report.html" if tool.export else f"{tool.title} preview.txt"))

    def _org_save_preview(self, key: str) -> None:
        """Save what the scan found and what applying it would do, as text or a spreadsheet, before changing anything."""
        st = self.org[key]
        if not st.findings:
            return
        path, _filter = QFileDialog.getSaveFileName(self, "Save Preview", self._org_default_report_name(st.tool),
                                                    "Text file (*.txt);;Spreadsheet (*.csv)")
        if not path:
            return
        if not Path(path).suffix:
            path += ".csv" if "csv" in _filter.lower() else ".txt"
        try:
            if st.tool.key == "patch":
                picks, _rows = self._patch_picks(key)
                rows = report_rows(st.tool.title, picks, set(range(len(picks))))
            else:
                rows = report_rows(st.tool.title, st.findings, st.checked)
            save_report(Path(path), rows)
        except OSError as exc:
            self._error("Couldn't Save", f"{path}\n\n{exc}")
            return
        self._toast(f"Preview saved to {path}. Nothing has been changed.", "ok")

    def _org_export(self, key: str, picks: list) -> None:
        """The library report: save the ticked systems as a web page or a spreadsheet."""
        st = self.org[key]
        entries = [e for f in picks for e in f.data]
        path, _filter = QFileDialog.getSaveFileName(self, "Save Library Report", self._org_default_report_name(st.tool),
                                                    "Web page (*.html);;Spreadsheet (*.csv)")
        if not path:
            return
        if not Path(path).suffix:  # some dialogs don't add the extension of the chosen filter
            path += ".csv" if "csv" in _filter.lower() else ".html"
        try:
            write_report(Path(path), entries)
        except OSError as exc:
            self._error("Couldn't Save", f"{path}\n\n{exc}")
            return
        self._toast(f"Report saved: {len(entries)} game(s) in {path}.", "ok")
