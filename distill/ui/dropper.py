"""Drop a file, folder or zip on the window and it opens the page that handles it.

`droproute.route_drop` decides which page (no Qt there); this applies the answer: it fills in the page's source folder,
limits the run to the dropped files when only some of a folder were dropped, switches 'Extract archives first' on for a zip,
and opens the page. The Switch page has its own setup (`_sw_handle_drop`); the Organize pages fill their option boxes."""
from __future__ import annotations

from pathlib import Path

from ..droproute import DropTarget, route_drop
from .nav import PAGE_INFO


class DropMixin:
    """Methods only; the window supplies the pages."""

    @staticmethod
    def _drop_paths(event) -> list[Path]:
        return [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]

    def _drop_has_files(self, event) -> bool:
        return bool(self._drop_paths(event))

    def _route_drop(self, paths: list[Path]) -> bool:
        """Open the page these paths belong to. False when no page wants them."""
        target = route_drop(paths)
        if target is None:
            return False
        if getattr(self, "is_processing", False) or getattr(self, "org_busy", False):
            self._toast("A run is in progress: drop it again when it has finished.", "warn", 5000)
            return True  # handled: nothing else should react either
        if target.page == "switch":
            self._sw_handle_drop(target.switch)
        elif target.page in self.tabs:
            self._tab_drop(target)
        elif target.page in self.org:
            self._org_drop(target)
        else:
            return False
        return True

    # ---- the conversion pages (CHD, RVZ, CSO, PS3, PKG, Xbox, ZAR) ----------------------------------------------------
    def _drop_limits(self) -> dict[str, tuple[str, frozenset[Path]]]:
        return self.__dict__.setdefault("_drop_limit_map", {})

    def _drop_only(self, mode: str) -> frozenset[Path] | None:
        """The files a drop picked out for this page, while its source box still shows the folder they were dropped from."""
        entry = self._drop_limits().get(mode)
        if entry and self._clean(self.tabs[mode].source.text()) == entry[0]:
            files = frozenset(p for p in entry[1] if p.exists())
            return files or None
        return None

    def _drop_note_row(self, mode: str):
        """The line under a page's folders that says only the dropped file will run, with a way back to the whole folder."""
        from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget
        from .widgets import hint
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        label = hint("", live=True)
        row.addWidget(label, 1)
        clear = QPushButton("Use the whole folder")
        clear.setFlat(True)
        clear.clicked.connect(lambda *_: self._drop_clear(mode))
        row.addWidget(clear)
        holder.setVisible(False)
        tv = self.tabs[mode]
        tv.single, tv.single_label = holder, label
        tv.source.edit.textChanged.connect(lambda *_: self._drop_note(mode))
        return holder

    def _drop_note(self, mode: str) -> None:
        tv = self.tabs[mode]
        only = sorted(self._drop_only(mode) or (), key=lambda p: p.name)
        if only:
            what = only[0].name if len(only) == 1 else f"{len(only)} dropped files"
            tv.single_label.setText(f"Only {what} will be run, not the rest of the folder.")
        tv.single.setVisible(bool(only))  # plain setVisible: the page may be off screen, where animated changes are skipped

    def _drop_clear(self, mode: str) -> None:
        self._drop_limits().pop(mode, None)
        self._drop_note(mode)

    def _tab_drop(self, t: DropTarget) -> None:
        tv = self.tabs[t.page]
        self._drop_limits().pop(t.page, None)
        tv.source.setText(str(t.source))
        if t.archive:
            tv.extract.setChecked(True)  # the archive is unpacked first; the page can't read inside it
        if t.only:
            self._drop_limits()[t.page] = (self._clean(tv.source.text()), frozenset(t.only))
        self._drop_note(t.page)
        self._show_page(t.page)
        self._drop_toast(t, PAGE_INFO[t.page][0])

    # ---- the Organize pages (Fix Names & Discs, Patch ROMs, Library Report) ------------------------------------------------
    def _org_drop(self, t: DropTarget) -> None:
        st = self.org[t.page]
        if t.source is not None:
            self._org_sources[0].setText(str(t.source))
        if t.option is not None and (row := st.rows.get(t.option[0])) is not None:
            row.setText(str(t.option[1]))
        if t.step and st.step_boxes:
            for key, box in st.step_boxes.items():
                if key == t.step:
                    box.setChecked(True)
                elif t.step_exclusive:
                    box.setChecked(False)
        self._show_page(t.page)
        self._drop_toast(t, PAGE_INFO[t.page][0], scan=True)

    def _drop_toast(self, t: DropTarget, title: str, scan: bool = False) -> None:
        text = f"{t.label} ({t.reason}). Opened {title}."
        if t.guess:
            text += " Couldn't tell what kind it is, so check this is the right page."
        elif t.archive:
            text += " It is unpacked first."
        if scan:
            text += " Choose your games folder if needed, then press Scan." if t.option else " Press Scan."
        self._toast(text, "info", 8000)
