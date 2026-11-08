"""Switch 2 > Manage metadata: the window that opens when a title is clicked in the list.

Laid out like an emulator's Properties window: the icon with Detect / Upload buttons above the fields. Name, publisher,
display version, icon and file name can be changed; the title id, format and size are shown for reference. A field left
blank means 'use the game's own', so only what is typed here overrides anything."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QRegularExpression, Qt, QTimer
from PySide6.QtGui import QPixmap, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ..constants import APP_DIR
from ..iconfinder import find_icon, IconHit, IconLookupError, sgdb_key
from ..models import fmt_size
from ..switchconv import clean_name, find_image, TitleEdit, TitleEntry
from . import msgbox
from .frameless import FramelessDialog
from .theme import T

IMAGE_FILTER = "Pictures (*.png *.jpg *.jpeg *.dat);;All files (*)"
ICON_SIDE = 240
ICON_CACHE = APP_DIR / "icon_cache"  # pictures found online are kept here, so a run can use them later


class TitleDialog(FramelessDialog):
    def __init__(self, entry: TitleEntry, edit: TitleEdit | None, art_dir: str = "", existing_nup: bool = False,
                 parent: QWidget | None = None):
        """`existing_nup`: the title is a .nup that is being edited, where only the icon, publisher and version can change."""
        super().__init__(parent, "Properties", "settings", None, (440, 0))
        self.entry, self._art_dir, self._icon = entry, art_dir, (edit.icon if edit else "")
        self._search: dict | None = None  # the online lookup while it runs: {"cancel": Event, "result": ...}
        self._poll = QTimer(self)
        self._poll.setInterval(150)
        self._poll.timeout.connect(self._poll_search)
        outer = self.body()

        box = QGroupBox("Info")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(14, 22, 14, 14)
        lay.setSpacing(10)

        self.preview = QLabel()
        self.preview.setFixedSize(ICON_SIDE, ICON_SIDE)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setWordWrap(True)
        T.bind_css(self.preview, lambda: f"background: {T.select.name()}; border: 1px solid {T.chip_edge.name()}; border-radius: 8px; "
                                   f"color: {T.fg3.name()}; padding: 12px;")
        lay.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignHCenter)

        buttons = QHBoxLayout()
        self.detect_btn = QPushButton("Detect Icon")
        self.detect_btn.setToolTip("Look for a picture named like this title (in the artwork folder, then beside the source).")
        self.detect_btn.clicked.connect(self._detect)
        self.upload_btn = QPushButton("Upload Icon...")
        self.upload_btn.clicked.connect(self._upload)
        buttons.addWidget(self.detect_btn)
        buttons.addWidget(self.upload_btn)
        lay.addLayout(buttons)
        self.online_btn = QPushButton("Find Icon Online")
        self.online_btn.setToolTip("Looks for a square icon for the name typed here (else the game's own name): switchiconshowdown.com first, "
                                   "then SteamGridDB. Gives up after 10 seconds.")
        self.online_btn.clicked.connect(self._find_online)
        lay.addWidget(self.online_btn)
        self.icon_note = QLabel("")
        self.icon_note.setObjectName("hint")
        self.icon_note.setWordWrap(True)
        self.icon_note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.icon_note)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        own = "Blank: the game's own"
        self.name = QLineEdit(edit.name if edit else "")
        self.name.setPlaceholderText(entry.name if not existing_nup else own)
        self.publisher = QLineEdit(edit.publisher if edit else "")
        self.publisher.setPlaceholderText(own)
        self.version = QLineEdit(edit.display_version if edit else "")
        self.version.setPlaceholderText(own)
        self.filename = QLineEdit(edit.filename if edit else "")
        self.filename.setPlaceholderText(entry.filename)
        if existing_nup:  # nup edit can set an icon, a publisher and a version, and nothing else
            for field in (self.name, self.filename):
                field.setEnabled(False)
                field.setToolTip("An existing .nup keeps its name and file name; only the icon, publisher and version change.")
        for label, widget in (("Name", self.name), ("Publisher", self.publisher), ("Version", self.version)):
            form.addRow(label, widget)
        self.title_id = QLineEdit(edit.title_id if edit else "")
        self.title_id.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{0,16}")))
        self.title_id.setPlaceholderText(entry.title_id or "Not found: enter it if you know it")
        if entry.kind == "NUP":
            self.title_id.setEnabled(False)
            self.title_id.setToolTip("A packed .nup can't change its title ID.")
        else:
            self.title_id.setToolTip("Normally read from the game's main.npdm (the base's, else the newest update's). "
                                     "Fill this in only to override it.")
        form.addRow("Title ID", self.title_id)
        for label, text in (("Format", entry.kind), ("Size", fmt_size(entry.size))):
            value = QLineEdit(text)
            value.setReadOnly(True)
            form.addRow(label, value)
        form.addRow("Filename", self.filename)
        self.name.textChanged.connect(lambda *_: self.filename.setPlaceholderText(self._default_filename()))
        self.filename.setPlaceholderText(self._default_filename())
        lay.addLayout(form)
        outer.addWidget(box)

        foot = QHBoxLayout()
        note = QLabel("Blank fields keep what the game already says.")
        note.setObjectName("hint")
        foot.addWidget(note, 1)
        bar = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                               | QDialogButtonBox.StandardButton.Reset)
        bar.accepted.connect(self.accept)
        bar.rejected.connect(self.reject)
        bar.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(self._reset)
        foot.addWidget(bar)
        outer.addLayout(foot)

        if self._icon:
            self._show_icon(self._icon)
        else:
            self._detect(quiet=True)

    # ---- the icon ----------------------------------------------------------------------------------------------------
    def _show_icon(self, path: str) -> None:
        pix = QPixmap(path) if path else QPixmap()
        if pix.isNull():
            self.preview.setPixmap(QPixmap())
            self.preview.setText(Path(path).name if path else "The game's own icon is used when it has one.")
            return
        self.preview.setText("")
        self.preview.setPixmap(pix.scaled(ICON_SIDE - 26, ICON_SIDE - 26, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))

    def _detect(self, quiet: bool = False) -> None:
        first = self.entry.files[0]
        beside = first.parent  # a dump folder's picture sits beside it, like a file's
        names = [self.name.text().strip(), self.filename.text().strip(), self.entry.name, self.entry.filename, first.stem]
        folders = [Path(self._art_dir)] if self._art_dir else []
        folders.append(beside)
        found = find_image([n for n in names if n], *[f for f in folders if f.is_dir()])
        if found:
            self._icon = str(found)
            self._show_icon(self._icon)
        elif not quiet:
            self._icon = ""
            self._show_icon("")
            self.preview.setText("No picture named like this title was found.")
        else:
            self._icon = ""
            self._show_icon("")

    def _upload(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, "Choose an Icon", self._icon or str(self.entry.files[0].parent), IMAGE_FILTER)
        if path:
            self._icon = path
            self._show_icon(path)

    def _default_filename(self) -> str:
        """The name the file will be saved under when the Filename box is blank: the new title if one was typed."""
        typed = self.name.text().strip() if self.name.isEnabled() else ""
        if not typed:
            return self.entry.filename
        return clean_name(typed) + (f" [{self.entry.title_id}]" if self.entry.kind not in ("Dump", "NUP") and self.entry.title_id else "")

    # ---- the online lookup (a thread does the waiting; a timer picks up its answer, so closing the window never blocks) ----
    def _find_online(self) -> None:
        if self._search is not None:  # the button doubles as Cancel while a search runs
            self._search["cancel"].set()
            self._finish_search("Search cancelled.")
            return
        names = [self.name.text().strip(), self.entry.name, self.entry.filename]
        state: dict = {"cancel": threading.Event(), "result": None}

        def work() -> None:
            try:
                state["result"] = ("ok", find_icon(names, state["cancel"]))
            except IconLookupError as e:
                state["result"] = ("error", str(e))
            except Exception as e:  # never leave the dialog waiting
                state["result"] = ("error", f"The search failed: {e}")

        self._search = state
        self.online_btn.setText("Searching... (click to cancel)")
        self.icon_note.setText("")
        threading.Thread(target=work, daemon=True).start()
        self._poll.start()

    def _poll_search(self) -> None:
        state = self._search
        if state is None or state["result"] is None:
            return
        kind, value = state["result"]
        if kind == "error":
            self._finish_search(value)
        elif value is None:
            self._finish_search("Nothing like this title was found on Switch Icon Showdown" + (" or SteamGridDB." if sgdb_key() else "."))
        else:
            data, hit = value
            self._take_online_icon(data, hit)

    def _take_online_icon(self, data: bytes, hit: IconHit) -> None:
        try:
            ICON_CACHE.mkdir(parents=True, exist_ok=True)
            ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
            path = ICON_CACHE / f"{hit.file_stem}{ext}"
            path.write_bytes(data)
        except OSError as e:
            self._finish_search(f"Found an icon but could not save it: {e}")
            return
        self._icon = str(path)
        self._show_icon(self._icon)
        self._finish_search(f"Found “{hit.title}” on {hit.source}.")

    def _finish_search(self, note: str) -> None:
        self._poll.stop()
        self._search = None
        self.online_btn.setText("Find Icon Online")
        self.icon_note.setText(note)

    def accept(self) -> None:
        text = self.title_id.text().strip()
        if text and len(text) != 16:
            msgbox.warning(self, "Title ID", "A title ID is 16 hex digits (for example 0100633007D48000). "
                                                  "Complete it, or clear the box to use the game's own.")
            return
        super().accept()

    def done(self, result: int) -> None:
        if self._search is not None:
            self._search["cancel"].set()  # the thread ends by itself; nobody waits for it
        self._poll.stop()
        super().done(result)

    def _reset(self) -> None:
        for field in (self.name, self.publisher, self.version, self.filename, self.title_id):
            field.clear()
        self._icon = ""
        self._detect(quiet=True)

    # ---- the answer --------------------------------------------------------------------------------------------------
    def edit(self) -> TitleEdit:
        """What the person set. An icon that was only auto-detected is not an edit (the run finds the same one itself)."""
        return TitleEdit(name=self.name.text().strip(), publisher=self.publisher.text().strip(),
                         display_version=self.version.text().strip(), icon=self._manual_icon(), filename=self.filename.text().strip(),
                         title_id=self.title_id.text().strip().upper())

    def _manual_icon(self) -> str:
        return self._icon if self._icon and self._icon != self._auto_icon else ""

    @property
    def _auto_icon(self) -> str:
        first = self.entry.files[0]
        folders = [Path(self._art_dir)] if self._art_dir else []
        folders.append(first.parent)
        found = find_image([n for n in (self.entry.name, self.entry.filename, first.stem) if n], *[f for f in folders if f.is_dir()])
        return str(found) if found else ""
