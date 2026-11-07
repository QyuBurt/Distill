"""The Local wireless card on Ryujinx (Ryubing)'s page: the lobbies on the LDN server, one row each, with the game, its version, how full it is,
the mode and who is in it, so you can see what is joinable (and whether your game version matches) before you launch. All the work is in
`distill.ldn`; this file lays it out and keeps the worker thread's result on the UI thread (event `emu_ldn`, routed by `_em_event`).
It refreshes while the card is on screen, keeping open rows open and the list where it was scrolled."""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem

from .. import ldn
from .. import recomps as rc
from .emu_cards import pill_css, section_header
from .theme import T
from .widgets import Card, fit_columns, hint

LDN_HELP = ("Lobbies on Ryujinx's LDN server right now. Click one to see who is in it. To join, start the same game at the same version with "
            "Multiplayer set to \"Ryujinx Ldn\" in Ryujinx's settings, then use the game's local play menu. Private lobbies show a count only.")
COLUMNS = ("Game", "Version", "Players", "Mode", "Status", "Open for")
RIGHT = (2,)


class LdnMixin:
    """Methods only; the state lives on DistillWindow (initialised by `_ldn_init`, called from `_em_init`)."""

    def _ldn_init(self) -> None:
        self._ldn_snap: ldn.Snapshot | None = None
        self._ldn_error = ""
        self._ldn_loading = False
        self._ldn_shown = False  # whether the card was on screen at the last sync
        self._ldn_open: set[str] = set()  # ids of the lobbies the person has opened
        self._ldn_lobbies: dict[str, ldn.Lobby] = {}

    def _ldn_build_card(self) -> Card:
        card = Card()
        card.lay.setContentsMargins(20, 16, 20, 16)
        card.lay.setSpacing(10)
        head = section_header("Local wireless", "gamepad")
        self.emd_ldn_count = QLabel()
        T.bind_css(self.emd_ldn_count, lambda: pill_css(T.fg3))
        head.addWidget(self.emd_ldn_count)
        card.add(head)
        self.emd_ldn_totals = hint("")
        card.add(self.emd_ldn_totals)

        row = QHBoxLayout()
        self.emd_ldn_search = QLineEdit()
        self.emd_ldn_search.setPlaceholderText("Search games or players")
        self.emd_ldn_search.setClearButtonEnabled(True)
        self.emd_ldn_search.textChanged.connect(lambda _t: self._ldn_fill())
        row.addWidget(self.emd_ldn_search, 1)
        self.emd_ldn_joinable = QCheckBox("Joinable only")
        self.emd_ldn_joinable.toggled.connect(lambda _c: self._ldn_fill())
        row.addWidget(self.emd_ldn_joinable)
        card.add(row)

        self.emd_ldn_tree = QTreeWidget()
        self.emd_ldn_tree.setHeaderLabels(COLUMNS)
        self.emd_ldn_tree.setRootIsDecorated(True)
        self.emd_ldn_tree.setUniformRowHeights(True)
        self.emd_ldn_tree.setAlternatingRowColors(True)
        self.emd_ldn_tree.setMinimumHeight(280)
        self.emd_ldn_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        for col, width in ((1, 80), (2, 70), (3, 150), (4, 80), (5, 80)):
            self.emd_ldn_tree.setColumnWidth(col, width)
        fit_columns(self.emd_ldn_tree, flex=0)
        self.emd_ldn_tree.itemExpanded.connect(self._ldn_toggled)
        self.emd_ldn_tree.itemCollapsed.connect(self._ldn_toggled)
        self.emd_ldn_tree.currentItemChanged.connect(lambda cur, _prev: self._ldn_describe(cur))
        card.add(self.emd_ldn_tree)
        self.emd_ldn_info = hint(LDN_HELP)
        card.add(self.emd_ldn_info)
        self.emd_ldn_msg = hint("", live=True)
        card.add(self.emd_ldn_msg)

        buttons = QHBoxLayout()
        self.emd_ldn_refresh = QPushButton("Refresh")
        self.emd_ldn_refresh.clicked.connect(lambda: self._ldn_load())
        buttons.addWidget(self.emd_ldn_refresh)
        buttons.addStretch(1)
        card.add(buttons)

        self._ldn_timer = QTimer(self)
        self._ldn_timer.setInterval(ldn.REFRESH_SECONDS * 1000)
        self._ldn_timer.timeout.connect(self._ldn_tick)
        card.setVisible(False)
        return card

    # ---- keeping the card in step with the page ---------------------------------------------------------------
    def _ldn_sync(self) -> None:
        """Called whenever the details page refreshes: shows the card for an installed Ryubing and starts or stops the refresh."""
        e = self._em_by_id.get(self._em_current)
        inst = self.em_store.installed(self._em_current) if e else None
        show = bool(e and e.id == ldn.EMULATOR_ID and not e.parent and inst is not None and inst.exe_path().exists())
        self.emd_ldn_card.setVisible(show)
        if not show:
            self._ldn_timer.stop()
            self._ldn_shown = False
            return
        if not self._ldn_shown:
            self._ldn_shown = True
            self._ldn_timer.start()
            self._ldn_fill()
            self._ldn_load()

    def _ldn_tick(self) -> None:
        if self.emd_ldn_card.isVisible():
            self._ldn_load()
        else:
            self._ldn_timer.stop()
            self._ldn_shown = False

    # ---- loading ----------------------------------------------------------------------------------------------
    def _ldn_load(self) -> None:
        if self._ldn_loading:
            return
        self._ldn_loading = True
        self.emd_ldn_refresh.setEnabled(False)
        if self._ldn_snap is None:
            self.emd_ldn_msg.setText("Asking the LDN server...")

        def work() -> None:
            try:
                self.ui_q.put(("emu_ldn", ldn.fetch(), ""))
            except rc.RecompError as exc:
                self.ui_q.put(("emu_ldn", None, str(exc)))
            except Exception as exc:  # noqa: BLE001 - a worker must always answer, or the card is stuck on "Asking"
                self.ui_q.put(("emu_ldn", None, f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _ldn_event(self, ev: tuple) -> None:
        _n, snap, error = ev
        self._ldn_loading = False
        self.emd_ldn_refresh.setEnabled(True)
        if snap is not None:
            self._ldn_snap, self._ldn_error = snap, ""  # on failure the last good list stays, with the reason under it
        else:
            self._ldn_error = error or "Couldn't read the LDN server."
        self._ldn_fill()

    # ---- showing it -------------------------------------------------------------------------------------------
    def _ldn_toggled(self, item: QTreeWidgetItem) -> None:
        lobby_id = item.data(0, Qt.ItemDataRole.UserRole)
        if lobby_id is None:
            return
        (self._ldn_open.add if item.isExpanded() else self._ldn_open.discard)(lobby_id)

    def _ldn_visible(self, lob: ldn.Lobby, text: str) -> bool:
        if self.emd_ldn_joinable.isChecked() and not lob.joinable:
            return False
        return not text or text in f"{lob.name} {lob.title_id} {lob.version} {' '.join(lob.names)}".lower()

    def _ldn_fill(self) -> None:
        snap = self._ldn_snap
        tree = self.emd_ldn_tree
        bar = tree.verticalScrollBar()
        scroll = bar.value()
        tree.blockSignals(True)  # rebuilding must not count as the person opening or closing rows
        tree.clear()
        self._ldn_lobbies = {}
        if snap is None:
            tree.blockSignals(False)
            self.emd_ldn_count.setVisible(False)
            self.emd_ldn_totals.setText("")
            self.emd_ldn_msg.setText(self._ldn_error)
            return
        s = snap.stats
        self.emd_ldn_count.setText(f"{s.total_players} playing")
        self.emd_ldn_count.setVisible(True)
        self.emd_ldn_totals.setText(f"{s.public_players} in {s.public_games} public lobb{'y' if s.public_games == 1 else 'ies'}   \u00b7   "
                                    f"{s.private_players} in {s.private_games} private")
        text = self.emd_ldn_search.text().strip().lower()
        ok, mute = T.ok, T.fg3
        shown = 0
        for lob in snap.lobbies:
            if not self._ldn_visible(lob, text):
                continue
            shown += 1
            self._ldn_lobbies[lob.id] = lob
            full = f"{lob.players}/{lob.max_players}" if lob.max_players else str(lob.players)
            item = QTreeWidgetItem([lob.name + ("" if lob.public else "  (private)"), lob.version or "?", full, lob.mode,
                                    lob.status, ldn.age_text(lob.created)])
            item.setData(0, Qt.ItemDataRole.UserRole, lob.id)
            for col in RIGHT:
                item.setTextAlignment(col, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            item.setForeground(4, QBrush(ok if lob.joinable else mute))
            for who in lob.names:
                child = QTreeWidgetItem([who])
                child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                item.addChild(child)
            if not lob.public:
                note = QTreeWidgetItem(["Private lobby: the server doesn't share who is in it."])
                note.setFlags(note.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                note.setForeground(0, QBrush(mute))
                item.addChild(note)
            tree.addTopLevelItem(item)
            item.setExpanded(lob.id in self._ldn_open)
        tree.blockSignals(False)
        self._ldn_open &= set(self._ldn_lobbies) | {lob.id for lob in snap.lobbies}  # forget lobbies that have closed
        bar.setValue(scroll)
        if self._ldn_error:
            self.emd_ldn_msg.setText(f"{self._ldn_error} Showing the last list.")
        elif not snap.lobbies:
            self.emd_ldn_msg.setText("No lobbies right now.")
        elif not shown:
            self.emd_ldn_msg.setText("No lobby matches that.")
        else:
            self.emd_ldn_msg.setText("")
        self._ldn_describe(tree.currentItem())

    def _ldn_describe(self, item: QTreeWidgetItem | None) -> None:
        lob = self._ldn_lobbies.get(item.data(0, Qt.ItemDataRole.UserRole)) if item is not None else None
        if lob is None:
            self.emd_ldn_info.setText(LDN_HELP)
            return
        bits = [lob.name, f"Title ID {lob.title_id}" if lob.title_id else "", f"Game version {lob.version}" if lob.version else "",
                lob.mode, "Public" if lob.public else "Private"]
        self.emd_ldn_info.setText("   \u00b7   ".join(b for b in bits if b))
