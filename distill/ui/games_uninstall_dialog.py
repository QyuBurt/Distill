"""Installed RomM games: how much room they take on this computer and a way to take them off again. A bar shows the drive (each platform's
share of it, anything else on it, and what is free); below it every installed game is listed, biggest first, to uninstall one at a time
or several together. Uninstalling deletes the game's files only: it stays in the library, listed as not installed, ready to install again."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from .. import games as gm
from .. import romm
from ..models import fmt_size
from . import icons, msgbox
from .frameless import FramelessDialog, hairline
from .theme import T
from .widgets import AnimCheckBox, accent_button, hint

PALETTE = ("#4c8dff", "#4caf7a", "#e8b84a", "#e0607e", "#a678e8", "#3fc1c9", "#f08a4b", "#8fa3b8")  # one per platform, in turn
SORTS = (("Size on disk", "size"), ("Name", "name"), ("Platform", "platform"))


def platform_colours(systems: list[str]) -> dict[str, QColor]:
    return {s: QColor(PALETTE[i % len(PALETTE)]) for i, s in enumerate(systems)}


def short(n: int) -> str:
    return fmt_size(n).replace(".00 ", " ")


class SpaceBar(QWidget):
    """The drive as one bar: a segment for each platform's games, then what else is used, then what is free."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(10)
        self._segments: list[tuple[int, QColor]] = []
        self._total = 1

    def set_segments(self, segments: list[tuple[int, QColor]], total: int) -> None:
        self._segments, self._total = segments, max(1, total)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.input)
        p.drawRoundedRect(r, 5, 5)
        p.setClipRect(r)  # (the segments are square: the rounded bar clips them)
        from PySide6.QtGui import QPainterPath  # noqa: PLC0415
        clip = QPainterPath()
        clip.addRoundedRect(r, 5, 5)
        p.setClipPath(clip)
        x = 0.0
        for size, colour in self._segments:
            w = r.width() * size / self._total
            if w <= 0:
                continue
            p.setBrush(colour)
            p.drawRect(QRectF(x, 0, max(w, 2.0), r.height()))
            x += w


class _Row(QFrame):
    def __init__(self, game: gm.Game, size: int, pixmap: QPixmap | None, on_toggle: Callable[[], None], on_uninstall: Callable[["_Row"], None]) -> None:
        super().__init__()
        self.game, self.size = game, size
        self.setObjectName("unRow")
        self.setStyleSheet("#unRow { background: transparent; border-radius: 10px; } #unRow:hover { background: rgba(255,255,255,0.04); }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 10, 6)
        lay.setSpacing(12)
        self.check = AnimCheckBox("")
        self.check.toggled.connect(lambda _on: on_toggle())
        lay.addWidget(self.check)
        thumb = QLabel()
        thumb.setFixedSize(60, 44)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if pixmap is not None and not pixmap.isNull():
            thumb.setPixmap(pixmap.scaled(thumb.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(thumb)
        text = QVBoxLayout()
        text.setSpacing(1)
        name = QLabel(game.name)
        name.setStyleSheet(f"color: {T.fg.name()}; font-size: 11pt;")
        sub = QLabel(game.system.upper())
        sub.setStyleSheet(f"color: {T.fg3.name()}; font-size: 9pt;")
        text.addWidget(name)
        text.addWidget(sub)
        lay.addLayout(text, 1)
        amount = QLabel(short(size))
        amount.setStyleSheet(f"color: {T.fg.name()}; font-size: 10pt;")
        lay.addWidget(amount)
        btn = QPushButton()
        btn.setToolTip("Uninstall this game")
        btn.setFixedSize(34, 30)
        btn.setIcon(icons.icon("trash", T.fg2, 16))
        btn.setIconSize(QSize(16, 16))
        btn.setAutoDefault(False)
        btn.clicked.connect(lambda: on_uninstall(self))
        lay.addWidget(btn)


class InstalledRommDialog(FramelessDialog):
    """`games` are the installed RomM games; `root` is the folder RomM games are installed into; `pixmap_of` gives a game's cover (or None);
    `on_removed` is told the ids of the games that were uninstalled, so the library can show them as not installed."""

    def __init__(self, parent: QWidget, games: list[gm.Game], root: Path, pixmap_of: Callable[[gm.Game], QPixmap | None],
                 on_removed: Callable[[list[str]], None]) -> None:
        super().__init__(parent, "Installed RomM games", "download", (760, 640), (560, 460))
        self.root, self._pixmap_of, self._on_removed = root, pixmap_of, on_removed
        self.items: list[tuple[gm.Game, int]] = []
        for g in games:
            fp = romm.footprint(g.path, max(1, g.remote_files), root)
            if fp is not None:
                self.items.append((g, fp.size))
        self.rows: list[_Row] = []
        outer = self.content
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        panel = QFrame()
        panel.setObjectName("unPanel")
        panel.setStyleSheet(f"#unPanel {{ background: {T.card.name()}; border: 1px solid {T.card_border.name()}; border-radius: 12px; }}")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(18, 14, 18, 14)
        pl.setSpacing(8)
        self.path_label = QLabel(str(root).upper())
        self.path_label.setStyleSheet(f"color: {T.fg.name()}; font-size: 9pt;")
        pl.addWidget(self.path_label)
        self.bar = SpaceBar()
        pl.addWidget(self.bar)
        self.legend = QHBoxLayout()
        self.legend.setSpacing(16)
        pl.addLayout(self.legend)
        wrap = QVBoxLayout()
        wrap.setContentsMargins(20, 16, 20, 0)
        wrap.addWidget(panel)
        outer.addLayout(wrap)

        bar = QHBoxLayout()
        bar.setContentsMargins(24, 16, 24, 6)
        self.count = QLabel()
        self.count.setStyleSheet(f"color: {T.fg.name()}; font-size: 13pt;")
        bar.addWidget(self.count)
        bar.addStretch(1)
        bar.addWidget(QLabel("Sort by"))
        self.sort = QComboBox()
        for label, key in SORTS:
            self.sort.addItem(label, key)
        self.sort.activated.connect(lambda _i: self._rebuild())
        bar.addWidget(self.sort)
        outer.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll.viewport().setAutoFillBackground(False)
        self.host = QWidget()
        self.host.setObjectName("unHost")
        self.host.setStyleSheet("#unHost { background: transparent; }")
        self.list = QVBoxLayout(self.host)
        self.list.setContentsMargins(16, 4, 16, 8)
        self.list.setSpacing(2)
        self.scroll.setWidget(self.host)
        outer.addWidget(self.scroll, 1)

        outer.addWidget(hairline())
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        self.all_box = AnimCheckBox("Select all")
        self.all_box.toggled.connect(self._select_all)
        foot.addWidget(self.all_box)
        self.note = hint("")
        foot.addWidget(self.note, 1)
        self.go = accent_button("Uninstall")
        self.go.setAutoDefault(False)
        self.go.clicked.connect(lambda: self._uninstall([r for r in self.rows if r.check.isChecked()]))
        foot.addWidget(self.go)
        done = QPushButton("Done")
        done.setAutoDefault(False)
        done.clicked.connect(self.accept)
        foot.addWidget(done)
        outer.addLayout(foot)
        self._rebuild()

    # -- what is shown --------------------------------------------------------------------------------------------
    def _sorted(self) -> list[tuple[gm.Game, int]]:
        key = self.sort.currentData()
        if key == "name":
            return sorted(self.items, key=lambda t: t[0].name.casefold())
        if key == "platform":
            return sorted(self.items, key=lambda t: (t[0].system.casefold(), -t[1]))
        return sorted(self.items, key=lambda t: -t[1])

    def _disk(self) -> tuple[int, int]:
        """(total, free) bytes of the drive the games are on; (0, 0) when it can't be read."""
        folder = self.root
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        try:
            usage = shutil.disk_usage(folder)
        except OSError:
            return 0, 0
        return usage.total, usage.free

    def _rebuild(self, keep: set[str] | None = None) -> None:
        keep = keep if keep is not None else {r.game.id for r in self.rows if r.check.isChecked()}
        while self.list.count():
            item = self.list.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.rows = []
        for g, size in self._sorted():
            row = _Row(g, size, self._pixmap_of(g), self._checked, self._uninstall_one)
            row.check.blockSignals(True)
            row.check.setChecked(g.id in keep)
            row.check.blockSignals(False)
            self.rows.append(row)
            self.list.addWidget(row)
        if not self.rows:
            empty = hint("No RomM games are installed on this computer.")
            empty.setContentsMargins(8, 14, 8, 0)
            self.list.addWidget(empty)
        self.list.addStretch(1)
        self.count.setText(f"Items  {len(self.items)}")
        self._draw_bar()
        self._checked()

    def _draw_bar(self) -> None:
        by_system: dict[str, int] = {}
        for g, size in self.items:
            by_system[g.system] = by_system.get(g.system, 0) + size
        order = sorted(by_system, key=lambda s: -by_system[s])
        colours = platform_colours(order)
        ours = sum(by_system.values())
        total, free = self._disk()
        other = max(0, total - free - ours)
        segments = [(by_system[s], colours[s]) for s in order]
        if other:
            segments.append((other, QColor(T.fg3).darker(160)))
        self.bar.set_segments(segments, total or max(1, ours))
        while self.legend.count():
            item = self.legend.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        entries = [(s, by_system[s], colours[s]) for s in order]
        if other:
            entries.append(("OTHER", other, QColor(T.fg3).darker(160)))
        if total:
            entries.append(("FREE", free, QColor(T.input)))
        for label, size, colour in entries:
            dot = QLabel(f'<span style="color:{colour.name()};">●</span>&nbsp;<b>{label.upper()}</b>&nbsp;{short(size)}')
            dot.setStyleSheet(f"color: {T.fg2.name()}; font-size: 9pt;")
            self.legend.addWidget(dot)
        self.legend.addStretch(1)

    def _checked(self) -> None:
        chosen = [r for r in self.rows if r.check.isChecked()]
        self.go.setEnabled(bool(chosen))
        self.go.setText(f"Uninstall {len(chosen)}" if chosen else "Uninstall")
        if chosen:
            self.note.setText(f"{len(chosen)} selected  ·  {short(sum(r.size for r in chosen))} to free")
        elif self.rows:
            self.note.setText("")
        self.all_box.blockSignals(True)
        self.all_box.setChecked(bool(self.rows) and len(chosen) == len(self.rows))
        self.all_box.blockSignals(False)
        self.all_box.setEnabled(bool(self.rows))

    def _select_all(self, on: bool) -> None:
        for r in self.rows:
            r.check.blockSignals(True)
            r.check.setChecked(on)
            r.check.blockSignals(False)
        self._checked()

    def preselect(self, ids: set[str]) -> None:
        """Tick these games (the ones a right-click was on)."""
        for r in self.rows:
            r.check.blockSignals(True)
            r.check.setChecked(r.game.id in ids)
            r.check.blockSignals(False)
        self._checked()

    # -- uninstalling ---------------------------------------------------------------------------------------------
    def _uninstall_one(self, row: _Row) -> None:
        self._uninstall([row])

    def _uninstall(self, rows: list[_Row]) -> None:
        if not rows:
            return
        total = sum(r.size for r in rows)
        what = f"“{rows[0].game.name}”" if len(rows) == 1 else f"{len(rows)} games"
        answer = msgbox.question(self, "Uninstall", f"Uninstall {what} from this computer?\n\nThis deletes the game's files and frees {short(total)}. "
                                 "The game stays in your library and can be installed from RomM again. Save files are not touched.")
        if answer != msgbox.SB.Yes:
            return
        done: list[str] = []
        failed: list[str] = []
        freed = 0
        for r in rows:
            try:
                freed += romm.uninstall(r.game.path, max(1, r.game.remote_files), self.root)
                done.append(r.game.id)
            except romm.RommError as exc:
                failed.append(f"{r.game.name}: {exc}")
        self.items = [(g, s) for g, s in self.items if g.id not in done]
        if done:
            self._on_removed(done)
        self._rebuild(keep=set())
        self.note.setText(f"Uninstalled {len(done)} game{'s' if len(done) != 1 else ''}  ·  freed {short(freed)}" if done else "")
        if failed:
            msgbox.warning(self, "Couldn't uninstall everything", "\n".join(failed[:8]))
