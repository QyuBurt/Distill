"""The Patch ROMs page: games as rows, the patches that fit each game under it, ticks per patch, a right-click menu to
match by hand, and a live preview of the files that would be made. Methods only; mixed into DistillWindow."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMenu, QTreeWidgetItem

from .. import organize as org
from .theme import T

PATCH_FILTER = "Patches (*.ips *.ups *.bps *.xdelta *.xdelta3 *.vcdiff);;All files (*)"
ROM_FILTER = "All files (*)"
_FLAGS = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


class PatchViewMixin:
    # ---- reading the page --------------------------------------------------------------------------------------
    def _patch_stack(self, key: str) -> bool:
        return bool(self.org[key].opts["stack"]())

    def _patch_is_game(self, f: org.Finding) -> bool:
        return isinstance(f.subject, Path)

    def _patch_plan(self, key: str, i: int) -> list[tuple[Path, list[Path]]]:
        """What game row i would produce with its current ticks."""
        st = self.org[key]
        f = st.findings[i]
        picked = [c.patch for j, c in enumerate(f.data) if (i, j) in st.patch_ticks and not c.problem]
        return org.plan_patches(f.subject, picked, self._patch_stack(key), None, st.patch_names) if picked and self._patch_is_game(f) else []

    # ---- showing it --------------------------------------------------------------------------------------------
    def _patch_show(self, key: str, findings: list, message: str, error: str) -> None:
        st = self.org[key]
        st.patch_ticks = {(i, j) for i, f in enumerate(findings) if self._patch_is_game(f)
                          for j, c in enumerate(f.data) if c.ticked and not c.problem}
        self._patch_fill(key, cascade=True)  # a fresh scan ripples in; a redraw after a hand-made match does not
        self._set_badge(key, sum(1 for f in findings if self._patch_is_game(f) and f.data))
        st.report_btn.setEnabled(bool(findings))
        if error:
            st.status.setText(f"Scan failed: {error}")
        elif not findings:
            st.status.setText(st.tool.empty + (f"  ({message})" if message else ""))
        else:
            self._patch_status(key)

    def _patch_fill(self, key: str, cascade: bool = False) -> None:
        st = self.org[key]
        tree = st.tree
        st.slide.cancel()  # no fold may carry on into rows that are about to go
        tree.clear()
        for i, f in enumerate(st.findings):
            item = QTreeWidgetItem([f.title, f.count, ""])
            item.setFlags(_FLAGS)
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            item.setToolTip(0, str(f.subject) if f.subject else f.title)
            tree.addTopLevelItem(item)
            for j, c in enumerate(f.data):
                child = QTreeWidgetItem([c.patch.name, "", ""])
                child.setFlags(_FLAGS)
                child.setData(0, Qt.ItemDataRole.UserRole, j)
                child.setToolTip(0, str(c.patch))
                item.addChild(child)
            if not f.data and f.children:  # an archive row: its listed contents
                for name, _c, _n in f.children:
                    kid = QTreeWidgetItem([name, "", ""])
                    kid.setFlags(_FLAGS)
                    item.addChild(kid)
        self._patch_refresh(key)
        # every game starts open; the rows are not folded open one by one, the games ripple in instead
        st.slide.set_expanded([tree.topLevelItem(i) for i in range(tree.topLevelItemCount())], True, animate=False)
        if cascade:
            st.reveal.stagger(len(st.findings))

    def _patch_refresh(self, key: str) -> None:
        """Bring every row's tick box and 'would make' text up to date without rebuilding the tree."""
        st = self.org[key]
        tree, stack = st.tree, self._patch_stack(key)
        for i in range(tree.topLevelItemCount()):
            item, f = tree.topLevelItem(i), st.findings[i]
            if not self._patch_is_game(f):
                item.setText(2, f.problem or f.note)
                item.setForeground(2, T.bad if f.problem else T.fg)
                for j in range(item.childCount()):
                    kid = item.child(j)
                    if f.data:
                        kid.setText(2, f.data[j].problem or "Right-click to choose its game")
                        kid.setForeground(2, T.bad if f.data[j].problem else T.fg)
                continue
            plan = self._patch_plan(key, i)
            where = {p: out for out, ps in plan for p in ps}
            ticked = [j for j, c in enumerate(f.data) if (i, j) in st.patch_ticks]
            pickable = [j for j, c in enumerate(f.data) if not c.problem]
            state = (Qt.CheckState.Unchecked if not ticked else
                     Qt.CheckState.Checked if len(ticked) == len(pickable) else Qt.CheckState.PartiallyChecked)
            item.setCheckState(0, state) if pickable else item.setData(0, Qt.ItemDataRole.CheckStateRole, None)
            item.setText(2, ("No patch found" if not f.data else
                             f"->  {plan[0][0].name}" if len(plan) == 1 and stack else
                             f"{len(plan)} copies" if plan else ""))
            for j, c in enumerate(f.data):
                kid = item.child(j)
                if c.problem:
                    kid.setData(0, Qt.ItemDataRole.CheckStateRole, None)
                    kid.setText(2, c.problem)
                    kid.setForeground(2, T.bad)
                    continue
                on = (i, j) in st.patch_ticks
                kid.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                kid.setForeground(2, T.fg)
                if on and c.patch in where:
                    step = f"step {[p for _o, ps in plan for p in ps].index(c.patch) + 1}  " if stack else ""
                    kid.setText(2, f"{step}->  {where[c.patch].name}")
                else:
                    kid.setText(2, c.label)

    def _patch_status(self, key: str) -> None:
        st = self.org[key]
        games = {i for i, _j in st.patch_ticks}
        made = sum(len(self._patch_plan(key, i)) for i in games)
        text = f"{len(st.patch_ticks)} patch(es) ticked on {len(games)} game(s)  |  {made} patched file(s) will be made"
        if st.message:
            text += f"  |  {st.message}"
        st.status.setText(text + ".")
        st.checked = games  # the apply button and the page's counters read this

    # ---- ticking -----------------------------------------------------------------------------------------------
    def _patch_toggle(self, key: str, item: QTreeWidgetItem) -> None:
        st = self.org[key]
        if self.org_busy:
            return
        if item.parent() is None:
            i = item.data(0, Qt.ItemDataRole.UserRole)
            f = st.findings[i]
            if not self._patch_is_game(f):
                return
            ok = {(i, j) for j, c in enumerate(f.data) if not c.problem}
            if not ok:
                return
            st.patch_ticks = st.patch_ticks - ok if ok <= st.patch_ticks else st.patch_ticks | ok
        else:
            i, j = item.parent().data(0, Qt.ItemDataRole.UserRole), item.data(0, Qt.ItemDataRole.UserRole)
            f = st.findings[i]
            if not self._patch_is_game(f) or f.data[j].problem:
                return
            st.patch_ticks ^= {(i, j)}
        self._patch_refresh(key)
        self._patch_status(key)

    def _patch_select(self, key: str, on: bool) -> None:
        st = self.org[key]
        if self.org_busy:
            return
        st.patch_ticks = ({(i, j) for i, f in enumerate(st.findings) if self._patch_is_game(f)
                           for j, c in enumerate(f.data) if not c.problem} if on else set())
        self._patch_refresh(key)
        self._patch_status(key)

    # ---- applying ----------------------------------------------------------------------------------------------
    def _patch_picks(self, key: str, only: set[tuple[int, int]] | None = None) -> tuple[list, list[int]]:
        """(findings to apply, the game row each belongs to) for the ticked patches, or for `only` when given."""
        st = self.org[key]
        ticks = st.patch_ticks if only is None else only
        picks, rows = [], []
        for i in sorted({i for i, _j in ticks}):
            made = org.patch_findings(st.findings, {t for t in ticks if t[0] == i}, self._patch_stack(key),
                                      st.patch_names)
            picks += made
            rows += [i] * len(made)
        return picks, rows

    # ---- right-click menu --------------------------------------------------------------------------------------
    def _patch_menu(self, key: str, pos) -> None:
        st = self.org[key]
        item = st.tree.itemAt(pos)
        if item is None or self.org_busy:
            return
        menu = QMenu(self)
        top = item if item.parent() is None else item.parent()
        i = top.data(0, Qt.ItemDataRole.UserRole)
        f = st.findings[i]
        if item.parent() is None:
            if self._patch_is_game(f):
                if self._patch_stack(key) and (plan := self._patch_plan(key, i)):
                    self._patch_rename_actions(key, menu, f.subject, *plan[0])
                menu.addAction("Add patch...", lambda: self._patch_add_to_game(key, i))
                if any(not c.problem for c in f.data):
                    menu.addAction("Tick all patches", lambda: self._patch_tick_game(key, i, True))
                    menu.addAction("Untick all patches", lambda: self._patch_tick_game(key, i, False))
                    menu.addSeparator()
                    menu.addAction("Apply this game's ticked patches now",
                                   lambda: self._patch_apply_now(key, {t for t in st.patch_ticks if t[0] == i}))
        elif f.data:
            j = item.data(0, Qt.ItemDataRole.UserRole)
            c = f.data[j]
            matched = self._patch_is_game(f)
            if matched and not c.problem:
                menu.addAction("Apply this patch now", lambda: self._patch_apply_now(key, {(i, j)}))
                if (i, j) in st.patch_ticks:
                    group = next(((out, ps) for out, ps in self._patch_plan(key, i) if c.patch in ps), None)
                    if group:
                        self._patch_rename_actions(key, menu, f.subject, *group)
                menu.addSeparator()
            sub = menu.addMenu("Match to game" if not matched else "Match to a different game")
            games = [(n, g) for n, g in enumerate(st.findings) if self._patch_is_game(g) and g is not f][:40]
            for _n, g in games:
                sub.addAction(g.title, lambda g=g: self._patch_match(key, c.patch, g.subject))
            if games:
                sub.addSeparator()
            sub.addAction("Choose a ROM file...", lambda: self._patch_match_browse(key, c.patch))
            if matched:
                if len(f.data) > 1:
                    up = menu.addAction("Move up (applied earlier when stacking)", lambda: self._patch_move(key, i, j, -1))
                    up.setEnabled(j > 0)
                    down = menu.addAction("Move down", lambda: self._patch_move(key, i, j, 1))
                    down.setEnabled(j < len(f.data) - 1)
                menu.addAction("Not for this game", lambda: self._patch_unmatch(key, c.patch))
        if not menu.isEmpty():
            menu.exec(st.tree.viewport().mapToGlobal(pos))

    def _patch_rename_actions(self, key: str, menu: QMenu, rom: Path, out: Path, patches: list[Path]) -> None:
        """'Name the patched ROM...' for the output that `patches` make from `rom` (and 'Use the automatic name')."""
        st = self.org[key]
        k = org.name_key(rom, patches)
        menu.addAction(f"Name the patched ROM...   ({out.name})", lambda: self._patch_rename(key, k, rom, out))
        if k in st.patch_names:
            menu.addAction("Use the automatic name", lambda: self._patch_set_name(key, k, ""))
        menu.addSeparator()

    def _patch_rename(self, key: str, k: tuple[str, ...], rom: Path, out: Path) -> None:
        text, ok = QInputDialog.getText(self, "Name the Patched ROM", f"File name for the patched copy of {rom.name}:",
                                        text=out.name)
        if not ok:
            return
        if text.strip() and not org.clean_output_name(rom, text):
            self._toast("That name has nothing usable in it.", "warn", 5000)
            return
        self._patch_set_name(key, k, text)

    def _patch_set_name(self, key: str, k: tuple[str, ...], text: str) -> None:
        st = self.org[key]
        if text.strip():
            st.patch_names[k] = text
        else:
            st.patch_names.pop(k, None)
        self._patch_refresh(key)
        self._patch_status(key)

    def _patch_tick_game(self, key: str, i: int, on: bool) -> None:
        st = self.org[key]
        ok = {(i, j) for j, c in enumerate(st.findings[i].data) if not c.problem}
        st.patch_ticks = st.patch_ticks | ok if on else st.patch_ticks - ok
        self._patch_refresh(key)
        self._patch_status(key)

    def _patch_add_to_game(self, key: str, i: int) -> None:
        st = self.org[key]
        rom = st.findings[i].subject
        files, _f = QFileDialog.getOpenFileNames(self, "Choose Patch File(s)", str(rom.parent), PATCH_FILTER)
        if files:
            snap = self._patch_snapshot(key)
            self._patch_redraw(key, snap, {self._patch_pair(key, Path(p), rom) for p in files})

    def _patch_match_browse(self, key: str, patch: Path) -> None:
        rom, _f = QFileDialog.getOpenFileName(self, "Choose the ROM This Patch Is For", str(patch.parent), ROM_FILTER)
        if rom:
            self._patch_match(key, patch, Path(rom))

    def _patch_match(self, key: str, patch: Path, rom: Path) -> None:
        snap = self._patch_snapshot(key)
        self._patch_redraw(key, snap, {self._patch_pair(key, patch, rom)})

    def _patch_pair(self, key: str, patch: Path, rom: Path) -> tuple[Path, Path]:
        """Pair them (the patch leaves wherever it was) and remember the choice for the next scan."""
        st = self.org[key]
        org.attach_patch(st.findings, patch, rom, self._org_stop)
        st.manual = [m for m in st.manual if Path(m[0]) != patch] + [(str(patch), str(rom))]
        return rom, patch

    def _patch_unmatch(self, key: str, patch: Path) -> None:
        st = self.org[key]
        snap = self._patch_snapshot(key)
        org.detach_patch(st.findings, patch)
        st.manual = [m for m in st.manual if Path(m[0]) != patch] + [(str(patch), "")]
        self._patch_redraw(key, snap, set())

    def _patch_move(self, key: str, i: int, j: int, step: int) -> None:
        snap = self._patch_snapshot(key)
        data = self.org[key].findings[i].data
        data[j], data[j + step] = data[j + step], data[j]
        self._patch_redraw(key, snap, set())

    @staticmethod
    def _patch_snapshot_of(st) -> set[tuple[Path, Path]]:
        return {(st.findings[i].subject, st.findings[i].data[j].patch) for i, j in st.patch_ticks
                if i < len(st.findings) and j < len(st.findings[i].data)}

    def _patch_snapshot(self, key: str) -> set[tuple[Path, Path]]:
        """Which (ROM, patch) pairs are ticked, so they stay ticked when rows move around after a hand-made change."""
        return self._patch_snapshot_of(self.org[key])

    def _patch_redraw(self, key: str, snap: set, new: set) -> None:
        st = self.org[key]
        keep = snap | new
        st.patch_ticks = {(i, j) for i, f in enumerate(st.findings) if self._patch_is_game(f)
                          for j, c in enumerate(f.data) if not c.problem and (f.subject, c.patch) in keep}
        self._patch_fill(key)
        self._set_badge(key, sum(1 for f in st.findings if self._patch_is_game(f) and f.data))
        self._patch_status(key)

    def _patch_apply_now(self, key: str, ticks: set[tuple[int, int]]) -> None:
        picks, rows = self._patch_picks(key, ticks)
        if picks:
            self._org_run(key, self.org[key].tool, picks, rows)
