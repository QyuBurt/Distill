"""The Add fork window: paste a GitHub address, let Distill look at the repository, and add it under an emulator.
Nothing is saved or downloaded until the person presses Add fork; an author Distill doesn't know needs a tick first."""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QWidget

from .. import emulators as em
from .. import recomps as rc
from .frameless import FramelessDialog
from .theme import T
from .widgets import accent_button, AnimCheckBox, hint, PathRow


class AddForkDialog(FramelessDialog):
    _looked = Signal(object)  # an em.ForkReport, sent from the worker thread

    def __init__(self, parent: QWidget, emulator: em.Emulator, store: rc.Store, catalog: list[em.Emulator]):
        super().__init__(parent, f"Add a fork of {emulator.name}", "branch", None, (560, 0))
        self.emulator, self.store, self.catalog = emulator, store, catalog
        self.report: em.ForkReport | None = None
        lay = self.body()
        lay.addWidget(hint("A fork is someone else's build of this emulator, often made for particular games. It installs into a folder of its own, "
                           "so your normal copy is never touched."))
        row = QHBoxLayout()
        self.repo = QLineEdit()
        self.repo.setPlaceholderText("github.com/owner/name")
        self.repo.setClearButtonEnabled(True)
        self.repo.textChanged.connect(self._edited)
        self.repo.returnPressed.connect(self._look)
        self.look_btn = QPushButton("Check")
        self.look_btn.clicked.connect(self._look)
        row.addWidget(self.repo, 1)
        row.addWidget(self.look_btn)
        lay.addLayout(row)
        self.name = QLineEdit()
        self.name.setPlaceholderText(f"Name (optional, shown as {emulator.name} (author))")
        lay.addWidget(self.name)
        self.desc = QPlainTextEdit()
        self.desc.setPlaceholderText("What is it for? One or two lines, e.g. \"Fixes for Astro Bot. Experimental.\"")
        self.desc.setFixedHeight(66)
        lay.addWidget(self.desc)
        self.existing = PathRow("A copy you already have (optional)", folder=True, placeholder="The folder it is in")
        self.existing.setToolTip("Already downloaded this fork? Choose its folder and Distill will use it as it is, instead of downloading another.")
        lay.addWidget(self.existing)
        self.evidence = QLabel()
        self.evidence.setWordWrap(True)
        self.evidence.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.evidence)
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        self.warn.setTextFormat(Qt.TextFormat.RichText)
        self.warn.setVisible(False)
        lay.addWidget(self.warn)
        self.accept_risk = AnimCheckBox("I've checked this author and accept the risk")
        self.accept_risk.setVisible(False)
        self.accept_risk.toggled.connect(self._sync)
        lay.addWidget(self.accept_risk)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.add_btn = accent_button("Add fork")
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.add_btn)
        lay.addLayout(buttons)
        self._looked.connect(self._on_looked)

    # ---- looking at the repository --------------------------------------------------------------------------
    def _edited(self, _text: str = "") -> None:
        self.report = None
        self.evidence.setText("")
        self.warn.setVisible(False)
        self.accept_risk.setVisible(False)
        self.accept_risk.setChecked(False)
        self._sync()

    def _look(self) -> None:
        text = self.repo.text()
        if not text.strip():
            return
        self.look_btn.setEnabled(False)
        self.evidence.setText(f'<span style="color:{T.fg3.name()}">Looking at it...</span>')
        threading.Thread(target=lambda: self._looked.emit(em.inspect_fork(self.emulator, text, self.store, self.catalog)), daemon=True).start()

    def _on_looked(self, rep: em.ForkReport) -> None:
        self.look_btn.setEnabled(True)
        if em.parse_repo_or_empty(self.repo.text()) != rep.repo and not rep.error:
            return  # the text changed while it was being looked at
        self.report = rep
        ok, warn, bad, mute = (c.name() for c in (T.ok, T.warn, T.bad, T.fg3))
        lines = []
        if rep.error:
            lines.append(f'<span style="color:{bad}">{rep.error}</span>')
        else:
            size = f" ({rc.fmt_size(rep.size)})" if rep.size else ""
            lines.append(f'<span style="color:{ok}">Download found:</span> {rep.asset}{size} in {rep.release}' + (f", {rep.released}" if rep.released else ""))
            lines.append(f'<span style="color:{mute}">★ {rep.stars:,}  ·  created {rep.created or "?"}  ·  last push {rep.pushed or "?"}</span>')
            if rep.prerelease_only:
                lines.append(f'<span style="color:{warn}">Only pre-releases are published.</span>')
            if rep.standalone:
                lines.append(f'<span style="color:{mute}">Not marked as a fork on GitHub: a standalone copy.</span>')
            if rep.archived:
                lines.append(f'<span style="color:{bad}">This repository is archived: it no longer receives builds.</span>')
        self.evidence.setText("<br>".join(lines))
        needs_tick = bool(rep.ok and not rep.known)
        self.warn.setVisible(rep.ok)
        if rep.ok:
            owner = rep.repo.split("/")[0]
            if rep.known:
                self.warn.setText(f'<span style="color:{ok}">{owner} also makes an emulator Distill lists.</span> '
                                  "Downloads are accepted only from that account.")
            else:
                self.warn.setText(f'<span style="color:{bad}"><b>Distill doesn\'t know {owner}.</b></span> Fake emulator repositories carry malware, '
                                  "and stars and downloads can be faked. Only add a fork you found through the emulator's own community or its author. "
                                  "Distill will download only from this account, and never update it unless you press Update.")
        self.accept_risk.setVisible(needs_tick)
        self._sync()

    def _sync(self, *_a) -> None:
        rep = self.report
        self.add_btn.setEnabled(bool(rep and rep.ok and (rep.known or self.accept_risk.isChecked())))

    # ---- the answer ---------------------------------------------------------------------------------------------
    def values(self) -> dict:
        return {"repo": self.report.repo if self.report else self.repo.text(), "name": self.name.text().strip(),
                "description": self.desc.toPlainText().strip(), "accepted": self.accept_risk.isChecked(),
                "folder": self.existing.text().strip()}
