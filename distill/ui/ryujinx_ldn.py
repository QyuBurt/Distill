"""The Local wireless card on Ryujinx (Ryubing)'s page: the public lobbies on the LDN server and how many people are playing, so you can
see what is joinable before you launch. All the work is in `distill.ldn`; this file lays it out and keeps the worker thread's result on the
UI thread (event `emu_ldn`, routed by `_em_event`). It reads the server's public numbers only and refreshes while the card is on screen."""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem

from .. import ldn
from .. import recomps as rc
from .emu_cards import pill_css, section_header
from .theme import T
from .widgets import Card, fit_columns, hint

LDN_HELP = "Public lobbies on Ryujinx's LDN server right now. Set Multiplayer to \"Ryujinx Ldn\" in Ryujinx's settings to join one. Private lobbies are only counted."


class LdnMixin:
    """Methods only; the state lives on DistillWindow (initialised by `_ldn_init`, called from `_em_init`)."""

    def _ldn_init(self) -> None:
        self._ldn_snap: ldn.Snapshot | None = None
        self._ldn_error = ""
        self._ldn_loading = False
        self._ldn_shown = False  # whether the card was on screen at the last sync

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

        self.emd_ldn_search = QLineEdit()
        self.emd_ldn_search.setPlaceholderText("Search games")
        self.emd_ldn_search.setClearButtonEnabled(True)
        self.emd_ldn_search.textChanged.connect(lambda _t: self._ldn_fill())
        card.add(self.emd_ldn_search)

        self.emd_ldn_tree = QTreeWidget()
        self.emd_ldn_tree.setHeaderLabels(("Game", "Lobbies", "Players"))
        self.emd_ldn_tree.setRootIsDecorated(False)
        self.emd_ldn_tree.setUniformRowHeights(True)
        self.emd_ldn_tree.setAlternatingRowColors(True)
        self.emd_ldn_tree.setMinimumHeight(220)
        self.emd_ldn_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.emd_ldn_tree.setColumnWidth(1, 80)
        self.emd_ldn_tree.setColumnWidth(2, 80)
        fit_columns(self.emd_ldn_tree, flex=0)
        card.add(self.emd_ldn_tree)
        self.emd_ldn_msg = hint(LDN_HELP, live=True)
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
    def _ldn_fill(self) -> None:
        snap = self._ldn_snap
        tree = self.emd_ldn_tree
        tree.clear()
        if snap is None:
            self.emd_ldn_count.setVisible(False)
            self.emd_ldn_totals.setText("")
            self.emd_ldn_msg.setText(self._ldn_error or LDN_HELP)
            return
        s = snap.stats
        self.emd_ldn_count.setText(f"{s.total_players} playing")
        self.emd_ldn_count.setVisible(True)
        self.emd_ldn_totals.setText(f"{s.public_players} in {s.public_games} public lobb{'y' if s.public_games == 1 else 'ies'}   ·   "
                                    f"{s.private_players} in {s.private_games} private")
        text = self.emd_ldn_search.text().strip().lower()
        rows = [r for r in snap.by_game() if text in r[0].lower()]
        busy = T.ok
        for name, lobbies, players in rows:
            item = QTreeWidgetItem([name, str(lobbies), str(players)])
            for col in (1, 2):
                item.setTextAlignment(col, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            if players >= 2:
                item.setForeground(2, QBrush(busy))
            tree.addTopLevelItem(item)
        if self._ldn_error:
            self.emd_ldn_msg.setText(f"{self._ldn_error} Showing the last list.")
        elif not snap.lobbies:
            self.emd_ldn_msg.setText("No public lobbies right now.")
        elif not rows:
            self.emd_ldn_msg.setText("No game matches that search.")
        else:
            self.emd_ldn_msg.setText(LDN_HELP)
