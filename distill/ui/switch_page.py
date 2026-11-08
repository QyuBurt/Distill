"""Switch NSZ / NUP page: a Switch 1 | Switch 2 switch (like Tiles | Cards on Emulators) over two sets of options.

Switch 1 compresses, decompresses and merges NSP / XCI files with nscb_rust. Switch 2 packs decrypted dumps (or
Switch 1 files, decrypted on the way with hactoolnet) into .nup with nup, with artwork. All the work is in
`distill.switchconv`; this file lays it out and reads it into a `SwitchConfig`."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..models import fmt_size
from ..switchconv import (
    check_prod_keys, CONTAINERS, default_key_files, find_nup_titles, LEVEL_DEFAULT, link_value, nup_command, review_files, SwitchConfig,
    SwitchDrop, title_kind, TitleEdit, TitleEntry,
)
from ..system import resolve_tool_path
from . import motion
from .emu_cards import segment_css, TwoColumn
from .frameless import FramelessDialog
from .state import LABEL_W
from .theme import T
from .title_dialog import TitleDialog
from .widgets import AnimCheckBox, Expander, hint, PathRow, StageSwitch, SwitchToggle, ToolChips

KEY_FILTER = "Key files (*.keys *.txt);;All files (*)"
# widgets the settings file remembers (see PersistMixin._persist_widgets)
SW_PERSIST = (
    "sw_source", "sw_dest", "sw_side_s2", "sw_action1", "sw_action2", "sw_level", "sw_merge_type",
    "sw_no_delta", "sw_rename", "sw_verify1", "sw_delete1", "sw_art_dir", "sw_manage",
    "sw_verify2", "sw_delete2", "sw_prod_keys", "sw_title_keys",
)
# Compression levels as named stops (nscb_rust / nup take 1-22): number, name, what it means
LEVEL_STOPS = ((1, "Fast", "Quickest, files a little larger."), (LEVEL_DEFAULT, "Default", "A balance of speed and size."),
               (12, "High", "Smaller files, noticeably slower."), (22, "Maximum", "Smallest files: very slow and memory hungry."))


class SwitchMixin:
    """Methods only; the widgets live on DistillWindow (made by `_sw_make_widgets`, before Settings is built)."""

    def _sw_make_widgets(self) -> None:
        self.sw_source = PathRow("Source folder", label_width=LABEL_W)
        self.sw_dest = PathRow("Destination folder", label_width=LABEL_W)
        self.sw_prod_keys = PathRow("prod.keys", folder=False, label_width=LABEL_W, file_filter=KEY_FILTER,
                                    file_title="Select prod.keys", placeholder="Your console's keys (dumped with Lockpick_RCM)")
        self.sw_title_keys = PathRow("title.keys", folder=False, label_width=LABEL_W, file_filter=KEY_FILTER,
                                     file_title="Select title.keys", placeholder="Optional: keys for games whose files carry no ticket")
        prod, title = default_key_files()  # the usual places: ~/.switch, beside the app
        self.sw_prod_keys.setText(prod)
        self.sw_title_keys.setText(title)
        self.sw_keys_status = hint("", live=True)
        self.sw_prod_keys.edit.textChanged.connect(self._sw_refresh_note)

        def stages(items: list[tuple[str, object, str]]) -> StageSwitch:
            # each stop's description sits centred underneath: the longest label no longer pushes it out of view
            switch = StageSwitch(describe="below")
            for text, data, description in items:
                switch.addItem(text, data, description)
            return switch

        self._sw_stages = stages
        self.sw_action1 = stages([
            ("Compress", "compress", "NSP / XCI to the smaller NSZ / XCZ."),
            ("Decompress", "decompress", "NSZ / XCZ back to NSP / XCI."),
            ("Merge", "merge", "A game, its updates and its DLC into one NSP / XCI."),
            ("Merge + Compress", "mergec", "A game, its updates and its DLC into one file, then compressed to NSZ / XCZ."),
        ])
        self.sw_action2 = stages([("Pack", "pack", "Decrypted dumps and Switch 1 files become one .nup each."),
                                  ("Edit", "edit", "Change the icon, publisher or version of .nup files that already exist.")])
        self.sw_level = self._sw_level_switch()
        self.sw_merge_type = stages([("NSP", "nsp", "One .nsp (installs from the eShop-style file)."),
                                     ("XCI", "xci", "One .xci (cartridge image).")])
        self.sw_no_delta = AnimCheckBox("Leave out delta fragments (smaller; they are only used for patching on the console)")
        self.sw_rename = AnimCheckBox("Name files from their contents first (adds [Title ID], renames the source files)")
        self.sw_verify1 = AnimCheckBox("Verify each result with nscb_rust")
        self.sw_delete1 = AnimCheckBox("Delete original files after success")
        self.sw_art_dir = PathRow("Artwork folder", label_width=LABEL_W,
                                  placeholder="Optional: pictures named like each game ('Game.png', '.jpg')")
        # Manage metadata: off, every title takes the game's own name, icon, publisher and version; on, a list of the titles in
        # the source folder opens each one's properties (see `_sw_edit_title`)
        self.sw_manage = SwitchToggle()
        self._sw_edits: dict[str, TitleEdit] = {}  # TitleEntry.key -> what was changed by hand
        self._sw_titles: list[TitleEntry] = []
        self._sw_scan_timer = QTimer(self)
        self._sw_scan_timer.setSingleShot(True)
        self._sw_scan_timer.setInterval(450)
        self._sw_scan_timer.timeout.connect(self._sw_rescan)
        self.sw_verify2 = AnimCheckBox("Verify each .nup after packing (nup verify)")
        self.sw_verify2.setChecked(True)
        self.sw_delete2 = AnimCheckBox("Delete the original .nsp / .xci files after success (dump folders are never deleted)")
        self._sw_only: tuple[Path, ...] = ()  # files a drop picked out: the run is limited to them (while the source stays)
        self._sw_only_source = ""
        self._sw_links: dict[str, str] = {}  # file -> id chosen by hand in 'Review groups...'
        self.sw_review_btn = QPushButton("Review groups...")
        self.sw_review_btn.setToolTip("See how the files were recognised (from the data inside them, then their names) and link any "
                                      "Distill couldn't place to the right game before you start.")
        self.sw_review_btn.clicked.connect(self._sw_review)
        self.sw_side_s1, self.sw_side_s2 = QPushButton("Switch 1"), QPushButton("Switch 2")
        self.sw_action1.currentIndexChanged.connect(lambda *_: self._sw_layout())
        self.sw_action2.currentIndexChanged.connect(lambda *_: self._sw_layout())
        self.sw_manage.toggled.connect(lambda *_: self._sw_layout())
        self.sw_source.edit.textChanged.connect(lambda *_: self._sw_scan_soon())
        self.sw_source.edit.textChanged.connect(lambda *_: self._sw_refresh_single())
        self.sw_art_dir.edit.textChanged.connect(lambda *_: self._sw_scan_soon())

    # ---- building --------------------------------------------------------------------------------------------------
    def _build_switch_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(12)
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        group = QButtonGroup(box)
        group.setExclusive(True)
        for button, first, tip in ((self.sw_side_s1, True, "NSP / XCI / NSZ / XCZ: compress, decompress and merge."),
                                   (self.sw_side_s2, False, "NUP: pack decrypted dumps, with artwork.")):
            button.setCheckable(True)
            button.setToolTip(tip)
            T.bind_css(button, lambda first=first: segment_css(first))
            group.addButton(button)
            row.addWidget(button)
        self.sw_side_s1.setChecked(True)
        self.sw_side_s2.toggled.connect(lambda *_: self._sw_layout())
        top.addWidget(box)
        self.sw_blurb = hint("")
        top.addWidget(self.sw_blurb, 1)
        top.addWidget(self.sw_review_btn)
        lay.addLayout(top)

        dirs = Expander("Folders", expanded=True)
        dirs.add(self.sw_source)
        dirs.add(self.sw_dest)
        dirs.add(hint("Destination blank: each result is saved next to its source file."))
        self.sw_single = QWidget()  # shown after a file is dropped on the window: only that file is run
        sr = QHBoxLayout(self.sw_single)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(10)
        self.sw_single_label = hint("", live=True)
        sr.addWidget(self.sw_single_label, 1)
        clear = QPushButton("Use the whole folder")
        clear.setFlat(True)
        clear.clicked.connect(lambda *_: self._sw_clear_only())
        sr.addWidget(clear)
        dirs.add(self.sw_single)
        self.sw_single.setVisible(False)

        def form(card: Expander, label: str, widget: QWidget) -> QWidget:
            """A label with its stage switch under it, so the switch has the card's whole width: in a half-width column a label
            beside it left the switch too little room and its stops were cut off."""
            holder = QWidget()
            h = QVBoxLayout(holder)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            lbl = QLabel(label)
            lbl.setObjectName("pathLabel")
            h.addWidget(lbl)
            h.addWidget(widget)
            card.add(holder)
            return holder

        self.sw_card1 = c1 = Expander("Switch 1 (NSZ / XCZ)", expanded=True)
        form(c1, "Action", self.sw_action1)
        self.sw_level_row1 = form(c1, "Compression level", self.sw_level)
        self.sw_merge_box = QWidget()
        mb = QVBoxLayout(self.sw_merge_box)
        mb.setContentsMargins(0, 0, 0, 0)
        mb.setSpacing(8)
        lbl = QLabel("Merge into")
        lbl.setObjectName("pathLabel")
        mb.addWidget(lbl)
        mb.addWidget(self.sw_merge_type)
        for w in (self.sw_no_delta, self.sw_rename):
            mb.addWidget(w)
        mb.addWidget(hint("Files are grouped by the [Title ID] in their names: a game, its update (...800) and its DLC.",
                          tip="A group needs the game and at least one update or DLC. The newest update is kept."))
        c1.add(self.sw_merge_box)
        c1.add(self.sw_verify1)
        c1.add(self.sw_delete1)
        c1.add(hint("Uses prod.keys from Settings > Tools > Switch keys. nscb_rust is downloaded and updated automatically."))

        self.sw_card2 = c2 = Expander("Switch 2 (NUP)", expanded=True)
        form(c2, "Action", self.sw_action2)
        self.sw_level_row2 = form(c2, "Compression level", self._sw_level_switch(follow=self.sw_level))
        c2.add(self.sw_art_dir)
        manage = QWidget()
        mh = QHBoxLayout(manage)
        mh.setContentsMargins(0, 0, 0, 0)
        mh.setSpacing(12)
        mlabel = QLabel("Manage metadata")
        mlabel.setFixedSize(LABEL_W, 24)
        mh.addWidget(mlabel, 0, Qt.AlignmentFlag.AlignVCenter)
        mh.addWidget(self.sw_manage, 0, Qt.AlignmentFlag.AlignVCenter)
        mh.addWidget(hint("Off: each title keeps its own name, icon, publisher and version."), 1, Qt.AlignmentFlag.AlignVCenter)
        self.sw_manage_row = manage
        c2.add(manage)
        self.sw_meta_box = QWidget()
        mm = QVBoxLayout(self.sw_meta_box)
        mm.setContentsMargins(0, 0, 0, 0)
        mm.setSpacing(6)
        self.sw_title_table = QTableWidget(0, 5)
        table = self.sw_title_table
        table.setHorizontalHeaderLabels(["Title", "Title ID", "Format", "Size", "Changes"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setShowGrid(False)
        table.setCursor(Qt.CursorShape.PointingHandCursor)
        table.setMinimumHeight(150)
        table.setMaximumHeight(260)
        head = table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        table.cellClicked.connect(lambda row, _col: self._sw_edit_title(row))
        mm.addWidget(table)
        self.sw_title_note = hint("", live=True)
        mm.addWidget(self.sw_title_note)
        c2.add(self.sw_verify2)
        c2.add(self.sw_delete2)
        c2.add(hint("Sources: decrypted dump folders (exefs/ + romfs/, or base/ updates/ dlc/), or Switch 1 .nsp / .xci / .nsz / "
                    ".xcz (decrypted with hactoolnet and your keys). Switch 2 games can't be decrypted on a PC: dump them on the "
                    "console and point at that folder.",
                    tip="A NUP holds unencrypted data, so no keys are needed to pack a dump. Artwork beside a dump folder is picked up "
                        "by name: 'Game Folder.png' next to 'Game Folder/'."))
        main = QWidget()  # the mode's options on the left, the performance budget on the right
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(14)
        main_lay.addWidget(dirs)  # half width like the other cards
        main_lay.addWidget(c1)
        main_lay.addWidget(c2)
        main_lay.addStretch(1)
        side = QWidget()
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 0, 0, 0)
        side_lay.setSpacing(14)
        side_lay.addStretch(1)
        self.perf_slots["switch"] = side_lay
        lay.addWidget(TwoColumn(main, side, wide_at=760, equal=True))
        lay.addWidget(self.sw_meta_box)  # the title list takes the full width
        lay.addWidget(self.sw_note_label())
        lay.addStretch(1)
        page.setProperty("compact", True)
        self._sw_layout(animate=False)
        return page

    def _sw_level_switch(self, follow: StageSwitch | None = None) -> StageSwitch:
        """The compression level as named stops (Fast ... Maximum), like Settings' speed options. The Switch 2 card gets a second
        one that follows the first, so there is one value in two places."""
        switch = self._sw_stages([(name, n, text) for n, name, text in LEVEL_STOPS])
        switch.setValue(LEVEL_DEFAULT)
        if follow is not None:
            switch.setValue(follow.value())
            switch.valueChanged.connect(follow.setValue)
            follow.valueChanged.connect(switch.setValue)
        return switch

    def sw_note_label(self) -> QWidget:
        """What is ready and what is missing: one chip per tool, green or red (filled by `_sw_refresh_note`)."""
        self.sw_note = ToolChips()
        return self.sw_note

    # ---- state -----------------------------------------------------------------------------------------------------
    def _sw_is_s2(self) -> bool:
        return self.sw_side_s2.isChecked()

    def _sw_layout(self, animate: bool = True) -> None:
        if not hasattr(self, "sw_card2"):  # a switch moved while the page is still being built
            return
        s2 = self._sw_is_s2()
        act1, act2 = self.sw_action1.currentData(), self.sw_action2.currentData()
        page = getattr(self, "pages", {}).get("switch")
        # motion.set_visible reads a widget on a page that isn't showing as already hidden and skips it, so a change made
        # while the page is off screen (a file dropped on another page) must set the widgets directly
        offscreen = page is not None and not page.isVisible()
        show = (lambda w, on: w.setVisible(on)) if offscreen else (lambda w, on: motion.set_visible(w, on, animate=animate))  # noqa: E731
        merging = act1 in ("merge", "mergec")
        show(self.sw_review_btn, (not s2 and merging) or (s2 and act2 == "pack"))
        show(self.sw_card1, not s2)
        show(self.sw_card2, s2)
        show(self.sw_level_row1, act1 in ("compress", "mergec"))
        show(self.sw_merge_box, merging)
        show(self.sw_verify1, True)
        show(self.sw_level_row2, act2 == "pack")
        show(self.sw_art_dir, True)
        show(self.sw_manage_row, act2 == "pack")  # Edit always lists its .nup files: nothing to switch on
        show(self.sw_meta_box, self._sw_lists_titles())
        if self._sw_lists_titles():
            self._sw_scan_soon()
        show(self.sw_verify2, act2 == "pack")
        show(self.sw_delete2, act2 == "pack")
        self.sw_blurb.setText("Compress, decompress and merge Switch games with nscb_rust." if not s2
                              else "Pack decrypted dumps into one .nup each, with artwork, using nup.")
        if hasattr(self, "start_btn"):
            self._refresh_start_label()
        self._sw_refresh_note()

    def _sw_busy_label(self) -> str:
        if not self._sw_is_s2():
            return {"compress": "Compressing...", "decompress": "Decompressing...", "merge": "Merging...",
                    "mergec": "Merging..."}.get(self.sw_action1.currentData(), "Working...")
        return "Packing..." if self.sw_action2.currentData() == "pack" else "Applying..."

    def _sw_start_label(self) -> str:
        if not self._sw_is_s2():
            return {"compress": "Start Switch Compression", "decompress": "Start Switch Decompression",
                    "merge": "Start Switch Merge", "mergec": "Start Switch Merge + Compression"}.get(self.sw_action1.currentData(), "Start")
        return "Start NUP Packing" if self.sw_action2.currentData() == "pack" else "Apply Changes to NUPs"

    def _sw_tool(self, key: str) -> str:
        text = self._clean(self._tool_text(key))
        path = resolve_tool_path(text)
        return path if path and (Path(path).is_file() or shutil.which(path)) else ""

    def _sw_refresh_note(self, *_args) -> None:
        """What is ready and what is missing for the side that is showing."""
        if not hasattr(self, "sw_note"):
            return
        s2 = self._sw_is_s2()
        chips = []
        for key, name, needed in (("nscb", "nscb_rust", True), ("hactoolnet", "hactoolnet", s2), ("nup", "nup", s2)):
            if needed:
                ready = bool(self._sw_tool(key))
                chips.append((name, ready, "ready" if ready else "not found: it is downloaded automatically, see Settings > Tools"))
        keys = self.sw_prod_keys.text().strip()
        problem = check_prod_keys(resolve_tool_path(keys)) if keys else ""
        if keys and not problem:
            chips.append(("prod.keys", True, "looks valid"))
        else:
            why = problem or "not set: add it under Settings > Tools > Switch keys"
            chips.append(("prod.keys", False, why + (" (only needed to decrypt Switch 1 files)" if s2 else "")))
        self.sw_note.set_states(chips)
        self.sw_keys_status.setText("prod.keys looks valid." if keys and not problem else problem if keys else "")

    # ---- running ---------------------------------------------------------------------------------------------------
    def _sw_start(self) -> None:
        if cfg := self._sw_build_config():
            only = frozenset(self._sw_only_files()) or None
            if only and not cfg.merge_compress and cfg.action != "merge":  # one file can't be merged with anything
                self._launch(cfg, only)
            else:
                self._launch(cfg)

    def _sw_build_config(self) -> SwitchConfig | None:
        clean = self._clean
        s2 = self._sw_is_s2()
        source = Path(clean(self.sw_source.text()))
        if not clean(self.sw_source.text()) or not source.is_dir():
            self._error("Error", f"Please select a valid source folder.\n\nPath checked: {source}")
            return None
        dest_raw = clean(self.sw_dest.text())
        temp_raw = clean(self.temp_dir.text())
        if temp_raw and not Path(temp_raw).is_dir():
            self._error("Error", f"The temp folder does not exist.\n\nPath checked: {temp_raw}")
            return None
        nscb, hac, nup = self._sw_tool("nscb"), self._sw_tool("hactoolnet"), self._sw_tool("nup")
        prod = resolve_tool_path(clean(self.sw_prod_keys.text()))
        title = resolve_tool_path(clean(self.sw_title_keys.text()))
        action = (self.sw_action2 if s2 else self.sw_action1).currentData()
        merge_compress = action == "mergec"
        if merge_compress:  # "Merge + Compress" is a merge whose result is compressed
            action = "merge"
        if s2:
            if not nup:
                self._tool_missing("Could not locate nup.\n\nIt is downloaded automatically; check Settings > Tools for its status, "
                                   "or select a nup.py launcher yourself.")
                return None
            if not nup_command(nup):
                self._error("Python needed", "nup is a Python program and no Python was found on this PC.\n\nInstall Python 3 "
                                             "(python.org), then restart Distill.")
                return None
        elif not nscb:
            self._tool_missing("Could not locate nscb_rust.\n\nIt is downloaded automatically; check Settings > Tools for its status, "
                               "or select nscb_rust.exe yourself.")
            return None
        if not s2 and (bad := check_prod_keys(prod)):
            self._error("prod.keys", bad + "\n\nSet it under Settings > Tools > Switch keys.")
            self._open_settings(0)
            return None
        if title and not Path(title).is_file():
            self._error("title.keys", f"The title.keys file does not exist.\n\nPath checked: {title}")
            return None
        art_dir = clean(self.sw_art_dir.text())
        if s2 and art_dir and not Path(art_dir).is_dir():
            self._error("Error", f"The artwork folder does not exist.\n\nPath checked: {art_dir}")
            return None
        edits: tuple[tuple[str, TitleEdit], ...] = ()
        if self._sw_lists_titles():  # only edits for titles this source folder still holds
            keys = {t.key for t in find_nup_titles(source, action or "pack", dict(self._sw_links))}
            edits = tuple((k, e) for k, e in self._sw_edits.items() if k in keys and not e.empty())
        return SwitchConfig(
            side="s2" if s2 else "s1", action=action or "compress", source=source, dest=Path(dest_raw) if dest_raw else None,
            nscb=nscb, hactoolnet=hac, nup=nup, prod_keys=prod, title_keys=title, level=self.sw_level.value(),
            delete=(self.sw_delete2 if s2 else self.sw_delete1).isChecked() and action != "edit",
            verify=(self.sw_verify2 if s2 else self.sw_verify1).isChecked() and action != "edit", temp_dir=temp_raw,
            merge_type=self.sw_merge_type.currentData() or "nsp", merge_compress=merge_compress,
            no_delta=self.sw_no_delta.isChecked(), rename_first=self.sw_rename.isChecked(), art_mode="game", art_dir=art_dir,
            links=tuple((f, t) for f, t in self._sw_links.items() if Path(f).is_file()), title_edits=edits)

    # ---- dropping a file, folder or zip ------------------------------------------------------------------------------
    def _sw_only_files(self) -> list[Path]:
        """The files a drop picked out, while the source box still shows the folder they were dropped from."""
        if self._sw_only and self._clean(self.sw_source.text()) == self._sw_only_source:
            return [p for p in self._sw_only if p.is_file()]
        return []

    def _sw_refresh_single(self) -> None:
        files = self._sw_only_files()
        if files:
            self.sw_single_label.setText(f"Only {files[0].name if len(files) == 1 else f'{len(files)} dropped files'} will be run, "
                                         "not the rest of the folder.")
        self.sw_single.setVisible(bool(files))  # plain: the page may be off screen, where animated changes are skipped

    def _sw_clear_only(self) -> None:
        self._sw_only, self._sw_only_source = (), ""
        self._sw_refresh_single()

    def _sw_handle_drop(self, drop: SwitchDrop) -> None:
        """A Switch file, dump folder or zip was dropped on the window: open this page set up for it."""
        if drop.archive:  # recognised, but neither tool can read inside a zip
            side = "Switch 2 (NUP)" if drop.side == "s2" else "Switch 1"
            self._info("Zip file", f"{drop.label} holds {side} files, but they have to be extracted first.\n\n"
                                   "Extract the zip, then drop the extracted file or folder here.")
            return
        self._sw_apply_drop(drop)

    def _sw_apply_drop(self, drop: SwitchDrop) -> None:
        self._sw_only, self._sw_only_source = (), ""
        self.sw_source.setText(str(drop.source))
        (self.sw_side_s2 if drop.side == "s2" else self.sw_side_s1).setChecked(True)
        switch = self.sw_action2 if drop.side == "s2" else self.sw_action1
        if (i := switch.findData(drop.action)) >= 0:
            switch.setCurrentIndex(i)
        self._sw_only, self._sw_only_source = drop.only, self._clean(self.sw_source.text())
        self._sw_layout(animate=False)
        self._sw_refresh_single()
        self._show_page("switch")
        side = "Switch 2" if drop.side == "s2" else "Switch 1"
        self._toast(f"{drop.label}: opened {side}, {switch.currentText()}. Change the action if you want something else.", "info", 7000)

    # ---- managing metadata -------------------------------------------------------------------------------------------
    def _sw_lists_titles(self) -> bool:
        """The title list shows on Switch 2 when Manage metadata is on, and always for the Edit action."""
        return self._sw_is_s2() and (self.sw_action2.currentData() == "edit" or self.sw_manage.isChecked())

    def _sw_scan_soon(self) -> None:
        """Look for the titles again shortly after the source folder, the artwork folder or the action changes."""
        if hasattr(self, "_sw_scan_timer") and self._sw_lists_titles():
            self._sw_scan_timer.start()

    def _sw_rescan(self) -> None:
        source = Path(self._clean(self.sw_source.text()))
        action = self.sw_action2.currentData() or "pack"
        titles: list[TitleEntry] = []
        if source.is_dir():
            try:
                titles = find_nup_titles(source, action, dict(self._sw_links))
            except OSError:
                titles = []
        self._sw_titles = sorted(titles, key=lambda t: t.name.lower())
        table = self.sw_title_table
        table.setRowCount(len(self._sw_titles))
        for row, t in enumerate(self._sw_titles):
            edit = self._sw_edits.get(t.key)
            cells = (t.name, t.title_id or "-", t.kind, fmt_size(t.size), self._sw_changes_text(edit))
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(t.key)
                table.setItem(row, col, item)
        if not source.is_dir():
            self.sw_title_note.setText("Choose a source folder to list its titles.")
        elif not self._sw_titles:
            self.sw_title_note.setText("No titles found: dump folders, .nsp / .xci / .nsz / .xcz files" if action == "pack"
                                       else "No .nup files found in the source folder.")
        else:
            changed = sum(1 for t in self._sw_titles if (e := self._sw_edits.get(t.key)) and not e.empty())
            self.sw_title_note.setText(f"{len(self._sw_titles)} title(s), {changed} changed. Click one to edit its properties.")

    @staticmethod
    def _sw_changes_text(edit: TitleEdit | None) -> str:
        if edit is None or edit.empty():
            return ""
        parts = [label for label, on in (("name", edit.name), ("publisher", edit.publisher), ("version", edit.display_version),
                                         ("icon", edit.icon), ("file name", edit.filename)) if on]
        return ", ".join(parts)

    def _sw_edit_title(self, row: int) -> None:
        """The properties window for one title in the list."""
        if not 0 <= row < len(self._sw_titles):
            return
        entry = self._sw_titles[row]
        dlg = TitleDialog(entry, self._sw_edits.get(entry.key), self._clean(self.sw_art_dir.text()),
                          existing_nup=entry.kind == "NUP", parent=self)
        if dlg.exec():
            edit = dlg.edit()
            if edit.empty():
                self._sw_edits.pop(entry.key, None)
            else:
                self._sw_edits[entry.key] = edit
            self._sw_rescan()

    # ---- reviewing how files group -----------------------------------------------------------------------------------
    def _sw_review(self) -> None:
        """Show what each file was recognised as, and let the person link the ones that couldn't be placed to a game."""
        source = Path(self._clean(self.sw_source.text()))
        if not source.is_dir():
            self._error("Review groups", "Please select a valid source folder first.")
            return
        files: list[Path] = []
        for root, dirs, names in os.walk(source):
            dirs[:] = [d for d in dirs if not d.startswith(".distill_nx_")]
            files += [Path(root) / n for n in names if Path(n).suffix.lower() in CONTAINERS]
        files.sort()
        if not files:
            self._info("Review groups", "No .nsp / .xci / .nsz / .xcz files were found in the source folder.")
            return
        ids, games, unknown = review_files(files, self._sw_links)
        dlg = FramelessDialog(self, "Review groups", "list", (860, 480), (640, 360))
        lay = dlg.body()
        lay.addWidget(hint(f"{len(files)} file(s), {len(unknown)} not recognised. Files are placed by the data inside them, then by the "
                           "[Title ID] in their names. For one that couldn't be placed, choose the game it belongs to."))
        table = QTableWidget(len(files), 3)
        table.setHorizontalHeaderLabels(["File", "Recognised as", "Group with"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        order = sorted(files, key=lambda p: (p not in unknown, p.name.lower()))
        combos: list[tuple[Path, QComboBox]] = []
        for row, path in enumerate(order):
            linked = str(path) in self._sw_links
            tid = ids.get(path)
            what = "Not recognised" if tid is None else f"{title_kind(tid)} {tid}" + (" (linked by hand)" if linked else "")
            for col, text in enumerate((str(path.relative_to(source)), what)):
                table.setItem(row, col, QTableWidgetItem(text))
            combo = QComboBox()
            combo.setObjectName("inlineCombo")  # inside a table row: the compact padding, so its text is not clipped
            combo.addItem("Automatic", None)
            for base, name in sorted(games.items(), key=lambda kv: kv[1].lower()):
                if base != tid:
                    combo.addItem(f"{name} [{base}]", base)
            if linked:
                want = next((b for b in games if link_value(b) == self._sw_links[str(path)]), None)
                if (i := combo.findData(want)) >= 0:
                    combo.setCurrentIndex(i)
            table.setCellWidget(row, 2, combo)
            combos.append((path, combo))
        lay.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec():
            for path, combo in combos:
                if (base := combo.currentData()) is None:
                    self._sw_links.pop(str(path), None)
                else:
                    self._sw_links[str(path)] = link_value(base)
