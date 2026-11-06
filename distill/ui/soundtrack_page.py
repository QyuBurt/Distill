"""Soundtracks page: game names in, the albums found (with their tracks) laid out in a list, ticked games downloaded
straight into `<Game>/Soundtrack/` through the Files & torrents download list."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QSize, QStandardPaths, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QDesktopServices, QIcon, QKeySequence, QPainter, QPainterPath, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSizePolicy, QToolButton, QTreeWidgetItem, QVBoxLayout, QWidget,
)

try:  # previews need Qt's media player; without it the page works the same, just without the play icons
    from PySide6.QtMultimedia import QAudio, QAudioOutput, QMediaPlayer
    AUDIO_OK = True
except ImportError:  # pragma: no cover - depends on the install
    AUDIO_OK = False

from .. import organize as org
from .. import soundtrack_tags as tags
from .. import soundtracks as st
from ..downloads import DownloadRunner, http_open, LinkError, RemoteFile
from ..models import fmt_size
from .emu_cards import hairline, section_header, TwoColumn
from .popup import soften_popup
from .state import LABEL_W
from .theme import T
from .trackrow import TrackRow
from .treeslide import TreeSlide
from . import motion
from .widgets import (accent_button, AnimCheckBox, Card, Expander, fit_columns, hint, PathRow, PlaceholderTree,
                      RevealDelegate,
                      StageSwitch, SwitchToggle)

URL_ROLE = Qt.ItemDataRole.UserRole  # a track row keeps the address it streams from here
ART_ROLE = Qt.ItemDataRole.UserRole + 1  # ... and its album's cover address
FILE_ROLE = Qt.ItemDataRole.UserRole + 2  # ... and the RemoteFile it stands for
CHECKED_ROLE = Qt.ItemDataRole.UserRole + 3  # a track row: is it ticked for saving?
TITLE_ROLE = Qt.ItemDataRole.UserRole + 4  # a track row: its title (the row's own widget draws it)
STATE_ROLE = Qt.ItemDataRole.UserRole + 5  # a game row: the tick state last applied to its tracks
HEADER_ROLE = Qt.ItemDataRole.UserRole + 6  # a disc header between a game's tracks ("Disc 2"): not a track
ART_CACHE_MAX = 64  # covers kept in memory (also the thumbnails in the list)
THUMB = 36  # the cover thumbnail beside each game, in pixels
TRACK_ROW_H = 30  # height of a track row in the list
GAME_ROW_H = 56  # height of a game row: the cover and the album picker in it need ~40px (their text clips in a shorter cell)
ATTACH_CHUNK = 24  # track widgets made per pass for the part of a long album that is not in view
ART_MAX_BYTES = st.COVER_MAX_BYTES


SKIP = "(none - leave this game out)"


def _cover_icon(pix: QPixmap) -> QIcon:
    """The cover as a rounded square cropped from its middle, drawn at twice the size so it stays sharp."""
    side = THUMB * 2
    square = pix.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    square = square.copy((square.width() - side) // 2, (square.height() - side) // 2, side, side)
    out = QPixmap(side, side)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(0, 0, side, side, 12, 12)
    painter.setClipPath(clip)
    painter.drawPixmap(0, 0, square)
    painter.end()
    out.setDevicePixelRatio(2.0)
    return QIcon(out)


class AlbumCombo(QComboBox):
    """The album chooser: a short drop-down (about eight rows, then it scrolls) that is never wider than a sensible
    limit, with long titles cut short rather than stretching the list across the screen."""

    MAX_POPUP_WIDTH = 560

    def __init__(self) -> None:
        super().__init__()
        self.setMaxVisibleItems(8)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(18)
        self.view().setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        soften_popup(self)  # the list is rounded: its window must not show square corners behind it

    def showPopup(self) -> None:
        limit = max(self.width(), min(self.MAX_POPUP_WIDTH, self.window().width() - 80))
        self.view().setMinimumWidth(self.width())
        self.view().setMaximumWidth(limit)
        self.view().window().setMaximumWidth(limit + 8)  # the pop-up frame around the list
        super().showPopup()


class NamesBox(QPlainTextEdit):
    """The games box. A text file of names, or a folder of games, can be dropped on it."""

    dropped = Signal(object)  # a Path

    @staticmethod
    def _wanted(event) -> list[Path]:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        return [p for p in paths if p.is_dir() or p.suffix.lower() in (".txt", ".lst", ".csv")]

    def dragEnterEvent(self, event) -> None:
        event.acceptProposedAction() if self._wanted(event) else super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        event.acceptProposedAction() if self._wanted(event) else super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if paths := self._wanted(event):
            self.dropped.emit(paths[0])
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


# (word in the status, symbol, colour) - the first that fits wins; keeps the Status column readable at a glance
STATUS_STYLE = (
    ("failed", "\u2717", "bad"), ("nothing found", "\u2717", "bad"), ("no tracks", "\u2717", "bad"),
    ("skipped", "\u26a0", "warn"), ("check the match", "\u26a0", "warn"), ("already have", "\u2713", "ok"),
    ("done", "\u2713", "ok"), ("ready", "\u2713", "ok"), ("matched", "\u2713", "ok"),
    ("searching", "\u2026", "busy"), ("listing", "\u2026", "busy"), ("queued", "\u2026", "busy"),
    ("downloaded", "\u2026", "busy"),
)
MATCH_COL = 4


class SoundRow:
    """One game on the page: what was found, the album chosen, and that album's tracks."""

    def __init__(self, result: st.GameResult, item: QTreeWidgetItem):
        self.result = result
        self.item = item
        self.combo: QComboBox | None = None
        self.album: st.Album | None = None
        self.files: list[RemoteFile] = []
        self.started = False  # its tracks have been handed to the download list
        self.saved: list[RemoteFile] = []  # the files as last handed over (named for where they go)
        self.removed = False  # taken off the page by the person (kept in the list so worker row numbers stay valid)
        self.match_folder: Path | None = None  # a game picked by hand (RomM mode) when the name did not match
        self.finalized = True  # nothing waiting on a finished run (tags, cover, summary)


class SoundtrackMixin:
    """Soundtracks page. Methods only; the state lives on DistillWindow, which mixes this in."""

    def _build_sound_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        games = Expander("Games", expanded=True)
        games.add(hint("One game per line. 'Name = other name' searches under another name; 'Name = link' uses a link you have.", tip="One game per line, named like its folder (that name is used for the folder). Add '= other name' to search under a different name, and/or '= link' to use an archive.org item or file listing you already have."))
        self.st_names = NamesBox()
        self.st_names.dropped.connect(self._st_names_dropped)
        self.st_names.setFixedHeight(104)
        self.st_names.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)  # else its card hogs spare height
        self.st_names.setPlaceholderText("Persona 5\nSuper Mario 64 = Mario 64 OST\nChrono Trigger = https://archive.org/details/...")
        games.add(self.st_names)
        self.st_folder_btn = QPushButton("Games directory...")
        self.st_folder_btn.setToolTip("Pick the folder that holds your games (game folders or single game files, "
                                      "e.g. your RomM library) and fill the list with their names")
        self.st_folder_btn.clicked.connect(self._st_pick_folder)
        self.st_quality = StageSwitch()
        self.st_quality.addItem("Best (FLAC first)", "best")
        self.st_quality.addItem("MP3 only", "mp3")
        self.st_search_btn = accent_button("Search")
        self.st_search_btn.setToolTip("Shift-click to ignore results remembered from the last few days")
        self.st_search_btn.clicked.connect(self._st_search)
        self.st_quality.setToolTip("Best looks for FLAC first and falls back to MP3; MP3 only skips FLAC.")
        quality = QHBoxLayout()  # centred, and untitled: what a Best / MP3 switch is for is plain from the stops
        quality.addStretch(1)
        quality.addWidget(self.st_quality)
        quality.addStretch(1)
        games.add(quality)
        row = QHBoxLayout()  # the helper on the left, the one thing to press on the right
        row.setSpacing(8)
        row.addWidget(self.st_folder_btn)
        row.addStretch(1)
        row.addWidget(self.st_search_btn)
        games.add(row)
        games.setProperty("settings_id", "Games")
        self.st_games_card = games

        dest = Expander("Save Location", expanded=True)
        dest.setProperty("settings_id", "Save Location")
        self.st_dest_card = dest
        self.st_dest = PathRow("Save to", label_width=LABEL_W)
        self.st_dest.edit.setAcceptDrops(False)
        self.st_dest.setText(self.dl_dest.text())
        self._sync_group([self.dl_dest.edit, self.st_dest.edit], "text", "setText", "textChanged")  # one setting
        dest.add(self.st_dest)
        dest.add(hairline())
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.st_mode = SwitchToggle()  # off = Standard, on = RomM
        self.st_mode.setToolTip(
            "Off:  <Save to>/Soundtracks/<Game>/01 - Title.flac\n\n"
            "RomM:  <Games directory>/<platform>/<Game>/<game file>\n"
            "                                                      /soundtrack/01 - Title.flac\n"
            "A game that is a loose file is first moved into a folder of its own (you confirm it, and can undo it).")
        self.st_mode_off, self.st_mode_on = QLabel("Off"), QLabel("RomM")
        for lab in (self.st_mode_off, self.st_mode_on):  # a click on a word flips the switch too
            lab.setCursor(Qt.CursorShape.PointingHandCursor)
        self.st_mode_off.mousePressEvent = lambda _e: self.st_mode.setChecked(False)
        self.st_mode_on.mousePressEvent = lambda _e: self.st_mode.setChecked(True)
        self.st_mode_hint = hint("")
        label = QLabel("RomM support")
        label.setFixedWidth(LABEL_W)
        mode_row.addWidget(label)
        mode_row.addWidget(self.st_mode_off)
        mode_row.addWidget(self.st_mode)
        mode_row.addWidget(self.st_mode_on)
        mode_row.addStretch(1)
        dest.add(mode_row)
        dest.add(self.st_mode_hint)
        self.st_tag = AnimCheckBox("Tag the files and save a cover.jpg beside them")
        self.st_tag.setToolTip("Fills in missing title, album, artist, track and disc numbers and the cover, so players "
                               "show them properly. Existing tags are never overwritten.\n"
                               + ("" if tags.available() else "Needs the 'mutagen' package: only cover.jpg is saved without it."))
        dest.add(hairline())
        dest.add(self.st_tag)
        prefs = st.load_prefs()
        self.st_tag.setChecked(prefs.get("tag", True))
        self.st_tag.toggled.connect(lambda *_: self._st_mode_changed())
        self._st_games_dir = Path(prefs["games_dir"]) if prefs.get("games_dir") else None
        self.st_mode.setChecked(prefs.get("mode") == st.SAVE_ROMM)
        self.st_mode.toggled.connect(lambda *_: self._st_mode_changed())

        main = QWidget()  # what to look for on the left, where it goes on the right
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(14)
        games.body_lay.addStretch(1)  # a taller card keeps its content at the top
        main_lay.addWidget(games)
        side = QWidget()
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 0, 0, 0)
        side_lay.setSpacing(14)
        dest.body_lay.addStretch(1)
        side_lay.addWidget(dest)
        lay.addWidget(TwoColumn(main, side, wide_at=760, equal=True, fill=True))  # the two cards are one height

        found = Card()
        head = section_header("Soundtracks Found", "music")
        tip = QLabel("Tick the games to download. Open one to see its tracks.")
        tip.setObjectName("hint")
        head.addWidget(tip)
        found.add(head)
        bar = QHBoxLayout()
        for text, fn in (("Select all", lambda: self._st_select(True)), ("Select none", lambda: self._st_select(False)),
                         ("Expand all", lambda: self._st_expand(True)), ("Collapse all", lambda: self._st_expand(False))):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, f=fn: f())
            bar.addWidget(b)
        bar.addStretch(1)
        found.add(bar)
        tree = self.st_tree = PlaceholderTree()
        self._st_reveal = RevealDelegate(tree)  # new results ripple in, row by row
        tree.setItemDelegate(self._st_reveal)
        tree.placeholder = "No soundtracks yet.\nType game names above, or drop a text file or a folder of games on the box, and press Search."
        tree.placeholder_icon = "music"
        tree.setIconSize(QSize(THUMB, THUMB))
        tree.setColumnCount(5)
        tree.setHeaderLabels(["Game", "Album", "Tracks", "Status", "Match"])
        tree.setRootIsDecorated(True)
        tree.setUniformRowHeights(False)
        tree.setMinimumHeight(280)  # the results are the point of the page: the setup above is short enough to leave them room
        hdr = tree.header()
        for col, width in ((0, 220), (2, 130), (3, 190), (MATCH_COL, 70)):
            tree.setColumnWidth(col, width)
        fit_columns(tree, flex=1)  # every column draggable; the Album column soaks up the slack
        hdr.sectionResized.connect(lambda *_: self._persist_soon())
        for key, handler in (("Delete", self._st_key_remove), ("Return", self._st_key_save),
                             ("Enter", self._st_key_save)):
            shortcut = QShortcut(QKeySequence(key), tree)  # Delete removes the selected rows, Enter saves them
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._st_context_menu)
        tree.itemChanged.connect(self._st_item_changed)
        tree.itemExpanded.connect(self._st_attach_tracks)  # the tick / play widgets are made when a game is opened
        self._st_slide = TreeSlide(tree, TRACK_ROW_H)  # connected after the line above: the rows' widgets exist before they fade in
        tree.itemDoubleClicked.connect(lambda item, _c: self._st_play_child(item) if item.parent() else None)
        found.add(tree)
        self.st_status = hint("Type some game names above and press Search.", live=True)
        found.add(self.st_status)
        foot = QHBoxLayout()
        self.st_retry_btn = QPushButton("Retry failed")
        self.st_retry_btn.setToolTip("Try the tracks that failed in the last save again (partial files resume)")
        self.st_retry_btn.setVisible(False)
        self.st_retry_btn.clicked.connect(self._st_retry_failed)
        self.st_more_btn = QToolButton()
        self.st_more_btn.setText("More")
        self.st_more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.st_more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        more = QMenu(self.st_more_btn)
        self.st_undo_btn = more.addAction("Undo last save")
        self.st_undo_btn.setToolTip("Delete the tracks the last save created and put back any game files it moved "
                                    "into their own folders")
        self.st_undo_btn.triggered.connect(lambda _c=False: self._st_undo_save())
        self.st_log_btn = more.addAction("Open move log")
        self.st_log_btn.triggered.connect(lambda _c=False: self._st_open_log())
        more.addSeparator()
        self.st_cache_btn = more.addAction("Forget remembered searches")
        self.st_cache_btn.triggered.connect(lambda _c=False: (st.clear_search_cache(),
                                                              self._toast("Remembered searches cleared.", "ok", 4000)))
        self.st_more_btn.setMenu(more)
        self.st_summary = hint("")
        self._st_failed_rows: list[int] = []
        self._st_notes: list[tuple[str, str]] = []  # skipped / failed games of the last save
        foot.addWidget(self.st_retry_btn)
        foot.addWidget(self.st_more_btn)
        foot.addWidget(self.st_summary, 1)
        found.add(foot)
        lay.addWidget(found, 1)  # spare height goes to the results
        self._explain(self.st_undo_btn, "Delete the tracks the last save created and put back any game files it moved "
                                         "into their own folders", "Nothing to undo yet.")
        self._explain(self.st_dest, "Where soundtracks are saved. Blank uses your Downloads folder.",
                      "RomM mode saves beside your games: set the Games directory instead.")
        self._st_cascading = False
        self._st_volume, self._st_muted = float(prefs.get("volume", 0.8)), bool(prefs.get("muted", False))
        self.now_playing.set_volume(self._st_volume, self._st_muted)
        self._st_mode_changed(save=False)
        return page

    # ---- the list's look ----------------------------------------------------------------------
    def _st_status(self, item: QTreeWidgetItem, text: str) -> None:
        """Set a row's Status with a coloured symbol in front, so the column reads at a glance."""
        low = text.lower()
        for needle, symbol, kind in STATUS_STYLE:
            if text and needle in low:
                colour = {"ok": T.ok, "warn": T.warn, "bad": T.bad}.get(kind)
                item.setText(3, f"{symbol} {text}")
                item.setForeground(3, QBrush(colour) if colour else QBrush(T.fg3))
                return
        item.setText(3, text)
        item.setData(3, Qt.ItemDataRole.ForegroundRole, None)

    def _st_style_match(self, r: SoundRow) -> None:
        """The Match column and the dimmed look of a game that still needs a decision."""
        if r.album is not None:
            good = r.album.score >= st.MATCH_MIN
            r.item.setText(MATCH_COL, f"{r.album.score:.0%}")
            r.item.setForeground(MATCH_COL, QBrush(T.ok if good else T.warn))
        else:
            r.item.setText(MATCH_COL, "")
        dim = r.album is None
        r.item.setForeground(0, QBrush(T.fg3) if dim else QBrush(T.fg))

    def _st_row_icon(self, r: SoundRow) -> None:
        if r.album is not None and (pix := self._st_art_cache.get(r.album.art)) is not None:
            r.item.setIcon(0, _cover_icon(pix))
        else:
            r.item.setIcon(0, QIcon())

    def _st_want_art(self, url: str) -> None:
        if url and url not in self._st_art_cache and url not in self._st_art_loading:
            self._st_art_loading.add(url)
            threading.Thread(target=self._st_art_worker, args=(url,), daemon=True).start()

    def _st_update_cards(self) -> None:
        """Keep the two setup cards' titles telling the story once they are folded away."""
        shown = [r for r in self._st_rows if not r.removed]
        self.st_games_card.header.setText(f"Games  \u00b7  {len(shown)} listed" if shown else "Games")
        def short(text: str, room: int = 24) -> str:  # the card is half the page wide: a long folder name would be cut off mid-letter
            return text if len(text) <= room else text[:room - 1] + "\u2026"

        if self._st_mode() == st.SAVE_ROMM:
            where = short(self._st_games_dir.name) if self._st_games_dir else "no games directory yet"
            self.st_dest_card.header.setText(f"Save location  \u00b7  RomM: {where}")
        else:
            raw = self._clean(self.st_dest.text())
            self.st_dest_card.header.setText(f"Save location  \u00b7  {short(Path(raw).name or raw) if raw else 'Downloads folder'}")

    def _st_names_dropped(self, path: Path) -> None:
        if path.is_dir():
            self._st_games_dir = path
            self._st_mode_changed()
            names = st.game_names_from_folder(path)
            if names:
                self.st_names.setPlainText("\n".join(names))
            else:
                self.st_status.setText("No games found in that folder.")
            return
        try:
            self.st_names.setPlainText(path.read_text(encoding="utf-8", errors="replace")[:200_000])
        except OSError as exc:
            self._toast(f"Couldn't read {path.name}: {exc}", "bad")

    def _persist_columns(self) -> dict[str, list[int]]:
        tree = getattr(self, "st_tree", None)
        return {"st_tree": [tree.columnWidth(i) for i in range(tree.columnCount())]} if tree is not None else {}

    def _restore_columns(self, widths: dict[str, list]) -> None:
        saved = widths.get("st_tree")
        tree = getattr(self, "st_tree", None)
        if tree is not None and isinstance(saved, list) and len(saved) == tree.columnCount():
            for col, w in enumerate(saved):
                if isinstance(w, int) and w > 20:
                    tree.setColumnWidth(col, w)
            if hasattr(tree, "_fit_state"):
                tree._fit_state["auto"] = False  # the person's own widths from last time stay

    # ---- search -------------------------------------------------------------------------------
    def _st_pick_folder(self) -> None:
        start = str(self._st_games_dir or self._clean(self.st_dest.text()) or "")
        if folder := QFileDialog.getExistingDirectory(self, "Games directory (holds your games or game folders)", start):
            self._st_games_dir = Path(folder)
            self._st_mode_changed()
            if names := st.game_names_from_folder(self._st_games_dir):
                self.st_names.setPlainText("\n".join(names))
            else:
                self.st_status.setText("No games found in there.")

    def _st_search(self) -> None:
        typed = self.st_names.toPlainText()
        requests = st.parse_game_lines(typed)
        if tidy := st.title_case_lines(typed):  # the box shows the names as they will be searched and listed
            if tidy != typed:
                self.st_names.setPlainText(tidy)
        if not requests:
            self.st_status.setText("Type at least one game name first.")
            return
        self._st_token += 1
        token, prefer = self._st_token, self.st_quality.currentData()
        fresh = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._st_slide.cancel()  # no fold may carry on into rows that are about to go
        self.st_tree.clear()
        self._st_rows = []
        for req in requests:
            item = QTreeWidgetItem([req.name, "", "", "\u2026 Searching..."])
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setSizeHint(0, QSize(0, GAME_ROW_H))
            self.st_tree.addTopLevelItem(item)
            self._st_rows.append(SoundRow(st.GameResult(req), item))
        self._st_reveal.stagger(len(requests))
        self.st_search_btn.setEnabled(False)
        self.st_status.setText("Searching...")
        # The setup has done its job the moment a search begins: fold it away so the results area is what you see
        # filling in, which is what tells you something is happening. (Already folded is a no-op.)
        self.st_games_card.header.setChecked(False)
        self.st_dest_card.header.setChecked(False)
        threading.Thread(target=self._st_search_worker, args=(token, requests, prefer, fresh), daemon=True).start()

    def _st_search_worker(self, token: int, requests: list[st.GameRequest], prefer: str,
                          fresh: bool = False) -> None:  # worker thread
        def found(i: int, res: st.GameResult) -> None:
            self.ui_q.put(("st_found", token, i, res))
            if res.best is not None:  # list its tracks now, so the person sees what they'd get
                self._st_list_worker(token, i, requests[i].name, res.best, prefer)
        try:
            st.find_albums(requests, on_result=found, should_stop=lambda: token != self._st_token, use_cache=not fresh)
        finally:
            self.ui_q.put(("st_searched", token))

    def _st_list_worker(self, token: int, row: int, game: str, album: st.Album, prefer: str) -> None:  # worker thread
        try:
            files, error = st.files_for(game, album, prefer=prefer), ""
        except LinkError as exc:
            files, error = [], str(exc)
        except Exception as exc:  # a surprise must not leave the row stuck on 'Listing tracks...'
            files, error = [], f"{type(exc).__name__}: {exc}"
        self.ui_q.put(("st_tracks", token, row, album, files, error))

    # ---- results ------------------------------------------------------------------------------
    def _st_on_found(self, row: int, res: st.GameResult) -> None:
        r = self._st_rows[row]
        r.result = res
        combo = AlbumCombo()
        if res.best is None and res.albums:
            combo.addItem(SKIP, None)
        for album in res.albums:
            note = ("" if album.playable is None else
                    f"  [{album.playable} tracks]" if album.playable >= 2 else "  [one file: no track preview]")
            label = album.title + (f" - {album.detail}" if album.detail else "") + f"  ({album.score:.0%})" + note
            combo.addItem(label, album)
        r.combo = combo
        self.st_tree.setItemWidget(r.item, 1, combo)
        if res.best is not None:
            r.album = res.best
            self._st_status(r.item, "Listing tracks...")
        elif res.albums:
            self._st_status(r.item, "Check the match")
        else:
            self._st_status(r.item, "Search failed" if res.error else "Nothing found")
            r.item.setToolTip(3, res.error)
            combo.setEnabled(False)
        self._st_style_match(r)
        self._st_want_art(r.album.art if r.album else "")
        self._st_row_icon(r)
        combo.currentIndexChanged.connect(lambda _i, rr=r: self._st_album_changed(rr))  # after it is filled in

    def _st_album_changed(self, r: SoundRow) -> None:
        album = r.combo.currentData() if r.combo else None
        r.album, r.files = (album if isinstance(album, st.Album) else None), []
        r.item.takeChildren()
        r.item.setText(2, "")
        r.item.setCheckState(0, Qt.CheckState.Unchecked)
        self._st_style_match(r)
        self._st_want_art(r.album.art if r.album else "")
        self._st_row_icon(r)
        if r.album is None:
            self._st_status(r.item, "Check the match")
            return
        self._st_status(r.item, "Listing tracks...")
        threading.Thread(target=self._st_list_worker, args=(self._st_token, self._st_rows.index(r), r.item.text(0),
                                                            r.album, self.st_quality.currentData()),
                         daemon=True).start()

    def _st_on_tracks(self, row: int, album: st.Album, files: list[RemoteFile], error: str) -> None:
        r = self._st_rows[row]
        if album is not r.album:  # the person picked another album while this one was being listed
            return
        r.files = files
        r.item.takeChildren()
        self._st_want_art(album.art)
        self._st_row_icon(r)
        if error or not files:
            r.item.setText(2, "")
            self._st_status(r.item, error or "No tracks found")
            r.item.setToolTip(3, error)
            return
        r.item.setToolTip(3, "")
        r.item.setData(0, STATE_ROLE, None)  # the new tracks take their tick from the game's box below
        known = [f.size for f in files if f.size is not None]
        r.item.setText(2, f"{len(files)} tracks" + (f", {fmt_size(sum(known))}" if known else ""))
        rests = [st._rest(f.name) for f in files]
        folders = [rest.rpartition("/")[0] for rest in rests]
        headed = len(set(folders)) > 1  # several discs / folders: a small header opens each, instead of a prefix on every track
        current = None
        for f, rest, folder in zip(files, rests, folders):
            if headed and folder != current:
                current = folder
                size = sum(x.size or 0 for x, fo in zip(files, folders) if fo == folder)
                count = folders.count(folder)
                r.item.addChild(self._st_header_item(folder, count, size))
            shown = rest.rpartition("/")[2] if headed else rest
            child = QTreeWidgetItem([shown, "", fmt_size(f.size) if f.size is not None else "", ""])
            child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            child.setSizeHint(0, QSize(0, TRACK_ROW_H))
            child.setData(0, FILE_ROLE, f)
            child.setData(0, TITLE_ROLE, shown)
            child.setData(0, CHECKED_ROLE, True)
            if AUDIO_OK:
                child.setData(0, URL_ROLE, f.url)
                child.setData(0, ART_ROLE, album.art)
            r.item.addChild(child)
        if r.item.isExpanded():
            self._st_attach_tracks(r.item)
        self._st_sync_icons()  # a rebuilt list must still show which track is playing
        if have := self._st_existing(r.item.text(0)):
            self._st_status(r.item, f"Already have {have} file(s)")
            r.item.setCheckState(0, Qt.CheckState.Unchecked)
        else:
            self._st_status(r.item, "Ready")
            r.item.setCheckState(0, Qt.CheckState.Checked)

    def _st_header_item(self, folder: str, count: int, size: int) -> QTreeWidgetItem:
        """A divider row between a game's discs: 'Disc 1', with how many tracks follow it."""
        known = f", {fmt_size(size)}" if size else ""
        header = QTreeWidgetItem([folder.replace("/", "  /  ") or "Tracks", "", f"{count} track{'s' if count != 1 else ''}{known}", ""])
        header.setFlags(Qt.ItemFlag.ItemIsEnabled)  # not selectable, tickable or playable
        header.setSizeHint(0, QSize(0, TRACK_ROW_H))
        header.setData(0, HEADER_ROLE, True)
        font = header.font(0)
        font.setBold(True)
        header.setFont(0, font)
        header.setForeground(0, QBrush(T.fg2))
        header.setForeground(2, QBrush(T.fg3))
        return header

    @staticmethod
    def _st_is_header(item: QTreeWidgetItem) -> bool:
        return bool(item.data(0, HEADER_ROLE))

    def _st_prune_headers(self, game: QTreeWidgetItem) -> None:
        """Drop a disc header whose tracks have all been removed (and the last header, when one disc is left)."""
        kids = [game.child(i) for i in range(game.childCount())]
        groups: list[list[QTreeWidgetItem]] = []
        for kid in kids:
            if self._st_is_header(kid):
                groups.append([kid])
            elif groups:
                groups[-1].append(kid)
        for group in groups:
            if len(group) == 1:  # a header with nothing under it
                game.removeChild(group[0])
        left = [game.child(i) for i in range(game.childCount()) if self._st_is_header(game.child(i))]
        if len(left) == 1:  # one disc is not worth a divider
            game.removeChild(left[0])

    def _st_on_searched(self) -> None:
        self.st_search_btn.setEnabled(True)
        shown = [r for r in self._st_rows if not r.removed]
        good = sum(r.result.best is not None for r in shown)
        self.st_status.setText(f"{good} of {len(shown)} game(s) matched well. "
                               "Check the others, then press Save Selected Soundtracks.")
        self._st_update_cards()
        self._st_update_summary()

    # ---- ticking and downloading --------------------------------------------------------------
    def _st_select(self, on: bool) -> None:
        for r in self._st_rows:
            if r.files and not r.removed:
                r.item.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _st_expand(self, open_: bool) -> None:
        self._st_slide.set_expanded([r.item for r in self._st_rows], open_)  # the games in view animate

    def _st_ticked(self) -> list[SoundRow]:
        """Games with at least one track ticked (a game's own box is ticked, or partly ticked when only some are)."""
        return [r for r in self._st_rows
                if r.files and not r.removed and r.item.checkState(0) != Qt.CheckState.Unchecked]

    def _st_checked_files(self, r: SoundRow) -> list[RemoteFile]:
        """The tracks of this game that are ticked for saving."""
        if not r.item.childCount():
            return list(r.files)
        return [f for i in range(r.item.childCount())
                if (c := r.item.child(i)).data(0, CHECKED_ROLE) and (f := c.data(0, FILE_ROLE)) is not None]

    def _st_update_summary(self) -> None:
        picks = self._st_ticked()
        files = [f for r in picks for f in self._st_checked_files(r)]
        size = sum(f.size or 0 for f in files)
        self.st_summary.setText(f"{len(picks)} game(s) ticked  |  {len(files)} track(s)" + (f"  |  {fmt_size(size)}" if size else "")
                                if picks else "")

    # ---- track rows: tick, play, highlight --------------------------------------------------------
    def _st_attach_tracks(self, item: QTreeWidgetItem) -> None:
        """Give a game's tracks their tick and play widgets (done when the game is opened). The tracks in view get
        theirs at once; the rest of a long album follow in small batches, so opening 255 tracks does not stall."""
        if item.parent() is not None:
            return
        lo, hi = self._st_slide.visible_rows(item)
        for i in range(lo, hi):
            self._st_attach_one(item.child(i))
        rest = [item.child(i) for i in range(item.childCount()) if not lo <= i < hi]
        self._st_sync_icons()
        if rest:
            QTimer.singleShot(0, lambda: self._st_attach_batch(item, rest))

    def _st_attach_batch(self, item: QTreeWidgetItem, pending: list[QTreeWidgetItem]) -> None:
        """One pass of the widgets for the tracks that were out of view when their game was opened."""
        try:
            if item.treeWidget() is not self.st_tree:  # the list was searched again meanwhile
                return
            for child in pending[:ATTACH_CHUNK]:
                if child.parent() is item:  # still there (tracks can be removed while this waits)
                    self._st_attach_one(child)
        except RuntimeError:  # a row was deleted
            return
        if len(pending) > ATTACH_CHUNK:
            rest = pending[ATTACH_CHUNK:]
            QTimer.singleShot(0, lambda: self._st_attach_batch(item, rest))
        else:
            self._st_sync_icons()

    def _st_attach_one(self, child: QTreeWidgetItem) -> None:
        if self._st_is_header(child) or self.st_tree.itemWidget(child, 0) is not None:
            return
        row = TrackRow(child.data(0, TITLE_ROLE) or child.text(0), bool(child.data(0, CHECKED_ROLE)),
                       can_play=bool(AUDIO_OK and child.data(0, URL_ROLE)))
        row.toggled.connect(lambda on, c=child: self._st_track_ticked(c, on))
        row.playClicked.connect(lambda c=child: self._st_play_child(c))
        child.setText(0, "")  # the row's widget draws the title
        self.st_tree.setItemWidget(child, 0, row)

    def _st_track_ticked(self, child: QTreeWidgetItem, on: bool) -> None:
        child.setData(0, CHECKED_ROLE, on)
        if (game := child.parent()) is not None:
            self._st_refresh_game(game)

    def _st_refresh_game(self, item: QTreeWidgetItem) -> None:
        """The game's own box follows its tracks: ticked, unticked, or partly ticked."""
        kids = [item.child(i) for i in range(item.childCount()) if not self._st_is_header(item.child(i))]
        total = len(kids)
        ticked = sum(bool(kid.data(0, CHECKED_ROLE)) for kid in kids)
        state = (Qt.CheckState.Checked if ticked == total else Qt.CheckState.Unchecked if ticked == 0
                 else Qt.CheckState.PartiallyChecked)
        self._st_cascading = True
        item.setData(0, STATE_ROLE, state.value)
        item.setCheckState(0, state)
        self._st_cascading = False
        self._st_update_summary()

    def _st_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Ticking or unticking a game's box does the same to all of its tracks."""
        if column == 0 and item.parent() is None and not getattr(self, "_st_cascading", False):
            state = item.checkState(0)
            if item.data(0, STATE_ROLE) != state.value:  # only a real change of the tick, not a colour or text change
                self._st_cascading = True
                item.setData(0, STATE_ROLE, state.value)
                self._st_cascading = False
                if state != Qt.CheckState.PartiallyChecked:
                    on = state == Qt.CheckState.Checked
                    for i in range(item.childCount()):
                        child = item.child(i)
                        child.setData(0, CHECKED_ROLE, on)
                        if (row := self.st_tree.itemWidget(child, 0)) is not None:
                            row.set_checked(on)
        self._st_update_summary()

    def _st_download(self) -> None:
        picks = self._st_ticked()
        if not picks:
            self.st_status.setText("Tick at least one game that has its tracks listed.")
            return
        self._st_save([(r, self._st_checked_files(r)) for r in picks], batch=True)

    def _st_refresh_status(self) -> None:
        """While a download runs, show each started game's progress on its row."""
        for r in self._st_rows:
            if not r.started or not r.saved:
                continue
            rows = [i for f in r.saved if (i := self.dl_model.row_of(f)) is not None]
            if not rows:
                continue
            done = sum(self.dl_model.is_finished(i) for i in rows)
            self._st_status(r.item, "Done" if done == len(rows) else f"{done} / {len(rows)} downloaded")

    # ---- save mode and places -----------------------------------------------------------------
    def _st_mode(self) -> str:
        return st.SAVE_ROMM if self.st_mode.isChecked() else st.SAVE_STANDARD

    def _st_default_dir(self) -> Path:
        loc = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        return Path(loc) if loc else Path.home() / "Downloads"

    def _st_mode_changed(self, save: bool = True) -> None:
        romm = self._st_mode() == st.SAVE_ROMM
        for lab, active in ((self.st_mode_off, not romm), (self.st_mode_on, romm)):
            font = lab.font()
            font.setBold(active)
            lab.setFont(font)
        self.st_dest.setEnabled(not romm)
        if romm:
            where = str(self._st_games_dir) if self._st_games_dir else "not chosen yet - press Games directory..."
            self.st_mode_hint.setText(f"RomM: tracks go in a 'soundtrack' folder beside each game. Games directory: {where}")
        else:
            self.st_mode_hint.setText("Off: tracks go in <Save to>/Soundtracks/<Game>/. A blank Save to uses your Downloads folder.")
        self._st_update_undo()
        if hasattr(self, "st_games_card"):
            self._st_update_cards()
        if save:
            self._st_prefs_update({"mode": self._st_mode(), "games_dir": str(self._st_games_dir or ""),
                                   "tag": self.st_tag.isChecked()})

    def _st_scan(self) -> list[st.GameEntry] | None:
        return st.scan_games(self._st_games_dir) if self._st_games_dir and self._st_games_dir.is_dir() else None

    def _st_existing(self, game: str) -> int:
        if self._st_mode() == st.SAVE_ROMM:
            entries = self._st_scan()
            return st.already_there(self._st_games_dir, game, st.SAVE_ROMM, entries) if entries is not None else 0
        raw = self._clean(self.st_dest.text())
        return st.already_there(Path(raw) if raw else self._st_default_dir(), game)

    def _st_update_undo(self) -> None:
        self.st_undo_btn.setEnabled(st.saves_depth() > 0 or org.undo_depth("soundtrack") > 0)

    def _st_undo_save(self) -> None:
        if self._dl_runner() is not None:
            self.st_status.setText("Wait for the running download to finish (or stop it), then undo.")
            return
        res = st.undo_last_save()
        if res is None and (moved := org.undo_last("soundtrack")) is not None:  # moves from before saves were recorded
            res = (0, moved[0], moved[1])
        if res is None:
            self.st_status.setText("Nothing to undo.")
        else:
            removed, restored, problems = res
            bits = [f"removed {removed} track file(s)"] + ([f"put back {restored} game(s)"] if restored else [])
            self.st_status.setText("Undid the last save: " + ", ".join(bits)
                                   + (f". {len(problems)} problem(s): {problems[0]}" if problems else "."))
        for r in self._st_rows:  # their downloads are gone: allow saving them again
            if r.started:
                r.started, r.saved = False, []
                self._st_status(r.item, "")
        self._st_update_undo()

    def _st_open_log(self) -> None:
        path = org.run_log_file()
        if not path.exists():
            self.st_status.setText("No game files have been moved yet, so there is no log.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ---- saving (batch, one game, or single tracks) -------------------------------------------
    def _st_save(self, entries: list[tuple[SoundRow, list[RemoteFile]]], batch: bool = False,
                 other_folder: bool = False) -> None:
        """Save these (game row, files) pairs. Standard mode files them under Soundtracks/, RomM mode beside the
        game (moving a loose game file into a folder first, after asking). `other_folder` asks where to save and
        always uses the Standard layout there."""
        entries = [(r, fs) for r, fs in entries if fs]
        if not entries:
            self.st_status.setText("Nothing to save: those games have no tracks listed yet.")
            return
        mode = st.SAVE_STANDARD if other_folder else self._st_mode()
        items = [(r.item.text(0), fs) for r, fs in entries]
        scan = None
        if mode == st.SAVE_ROMM:
            if self._st_games_dir is None or not self._st_games_dir.is_dir():
                self._st_pick_games_dir_only()
            scan = self._st_scan()
            if scan is None:
                self.st_status.setText("RomM mode needs a games directory: press Games directory... first.")
                return
            dest = self._st_games_dir
        elif other_folder:
            start = self._clean(self.st_dest.text()) or str(self._st_default_dir())
            folder = QFileDialog.getExistingDirectory(self, "Save Soundtracks to", start)
            if not folder:
                return
            dest = Path(folder)
        else:
            raw = self._clean(self.st_dest.text())
            dest = Path(raw) if raw else self._st_default_dir()  # nowhere chosen: the Downloads folder
        picks: dict[str, st.GameEntry] = {}  # games the person matched by hand
        for r, _ in entries:
            if mode == st.SAVE_ROMM and r.match_folder and scan is not None:
                if hit := next((e for e in scan if e.folder == r.match_folder), None):
                    picks[r.item.text(0)] = hit
        plan = st.plan_save(items, mode, dest, scan, picks)

        if len(items) == 1 and plan.ambiguous:  # one game, several matches: ask which
            game, hits = next(iter(plan.ambiguous.items()))
            labels = [str(e.folder.relative_to(dest)) for e in hits]
            label, ok = QInputDialog.getItem(self, "Which Game?", f"'{game}' matches more than one game:", labels, 0, False)
            if not ok:
                return
            plan = st.plan_save(items, mode, dest, scan, {**picks, game: hits[labels.index(label)]})
        if not plan.files and mode == st.SAVE_ROMM and len(items) == 1:  # nothing to put it beside
            why = plan.skipped[0][1] if plan.skipped else "no match"
            ask = QMessageBox.question(self, "Game Not Found",
                                       f"'{items[0][0]}': {why}.\n\nSave it to your Downloads folder instead?")
            if ask != QMessageBox.StandardButton.Yes:
                return
            mode, dest = st.SAVE_STANDARD, self._st_default_dir()
            plan = st.plan_save(items, mode, dest)
        if not plan.files:
            self.st_status.setText("Nothing saved: " + "; ".join(f"{g}: {why}" for g, why in plan.skipped[:3]))
            return

        if mode == st.SAVE_ROMM and (batch or plan.moves or plan.skipped):  # preview, then confirm
            if not self._st_confirm(plan, dest):
                return
        runner = self._dl_runner()
        if runner is not None and self._dl_dest_path() != dest:
            self.st_status.setText(f"A download to {self._dl_dest_path()} is still running. Wait for it to finish, "
                                   "then save again.")
            return
        failed = self._st_apply_moves(plan)
        if not plan.files:
            self.st_status.setText("Nothing saved: " + "; ".join(f"{g}: {why}" for g, why in failed[:3]))
            return

        self.dl_dest.setText(str(dest))  # the Download page saves everything relative to this
        if not other_folder and mode == st.SAVE_STANDARD and not self._clean(self.st_dest.text()):
            self.st_dest.setText(str(dest))
        notes = plan.skipped + failed
        self._st_notes, self._st_failed_rows = notes, []
        motion.fade(self.st_retry_btn, False)
        for r, _ in entries:
            game = r.item.text(0)
            if game in plan.by_game:
                r.saved, r.started, r.finalized = plan.by_game[game], True, False
                self._st_status(r.item, "Queued")
            elif why := dict(notes).get(game):
                self._st_status(r.item, f"Skipped: {why}")
                r.item.setToolTip(3, "Right-click the game and choose 'Match to game...' to pick it by hand.")

        # what this save creates (for undo): tracks not already on disk. The same track saved again in another layout
        # is a new file; a file that went missing is restarted rather than skipped as 'already in the list'.
        index = {(rf.url, rf.name): i for i, rf in enumerate(self.dl_files)}
        new_files, redo = [], []
        for rf in plan.files:
            try:
                path = DownloadRunner.locate(rf, dest, False)
            except ValueError:
                continue
            if not path.exists() and not path.with_name(path.name + ".part").exists():
                new_files.append(path)
            if (rf.url, rf.name) in index and not path.exists():
                redo.append(index[(rf.url, rf.name)])
        rows = self._dl_append(plan.files, [], by_name=True) + [i for i in redo if i not in self.dl_model.removed]
        self._st_mode_changed(save=False)
        skipped = (f" Skipped {len(notes)}: " + "; ".join(f"{g} ({why})" for g, why in notes[:3])) if notes else ""
        if not rows:
            self.st_status.setText("Those tracks are already saved there (see Files & Torrents)." + skipped)
            for r, _ in entries:
                r.finalized = True
            return
        st.record_save(new_files, dest, moved=bool(plan.moves))
        self._st_update_undo()
        self.st_status.setText(f"Saving {len(rows)} track(s) to {dest}. Progress is in the bar below." + skipped)
        self._dl_start_rows(rows)

    # ---- when the run ends: summary, retry, tags ----------------------------------------------
    def _st_on_run_done(self, sm) -> None:
        pending = [r for r in self._st_rows if r.started and not r.finalized and r.saved]
        if not pending:
            return
        saved_games = failed_games = tracks = 0
        self._st_failed_rows, jobs = [], []
        dest = self._dl_dest_path()
        for r in pending:
            rows = [(f, self.dl_model.row_of(f)) for f in r.saved]
            ok = [f for f, i in rows if i is not None and self.dl_model.is_finished(i)]
            bad = [i for _f, i in rows if i is not None and self.dl_model.status[i].startswith("Failed")]
            r.finalized = not bad  # a failed game is finalized when its retry finishes
            tracks += len(ok)
            self._st_failed_rows += bad
            if bad:
                failed_games += 1
                self._st_status(r.item, f"{len(bad)} failed, {len(ok)} saved")
            else:
                saved_games += 1
                self._st_status(r.item, "Done")
            if ok and dest is not None and self.st_tag.isChecked() and r.album is not None:
                jobs.append((r.album, ok))
        parts = [f"Saved {tracks} track(s) from {saved_games + failed_games} game(s)"]
        if failed_games:
            parts.append(f"{len(self._st_failed_rows)} track(s) failed")
        if self._st_notes:
            parts.append(f"{len(self._st_notes)} game(s) skipped")
        self.st_status.setText(", ".join(parts) + ".")
        motion.fade(self.st_retry_btn, bool(self._st_failed_rows))
        if saved_games and not failed_games and dest is not None:
            self._toast(f"Saved {tracks} track(s) from {saved_games} game(s).", "ok", 20000,
                        ("Open folder", lambda d=dest: self._st_open_saved(d)))
        if jobs and dest is not None:
            threading.Thread(target=self._st_finish_worker, args=(jobs, dest), daemon=True).start()

    def _st_open_saved(self, dest: Path) -> None:
        target = dest / st.STANDARD_ROOT if self._st_mode() == st.SAVE_STANDARD and (dest / st.STANDARD_ROOT).is_dir() else dest
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _st_retry_failed(self) -> None:
        rows, self._st_failed_rows = self._st_failed_rows, []
        motion.fade(self.st_retry_btn, False)
        if rows:
            self.st_status.setText(f"Retrying {len(rows)} track(s)...")
            self._dl_start_rows(rows)

    def _st_finish_worker(self, jobs: list[tuple[st.Album, list[RemoteFile]]], dest: Path) -> None:  # worker thread
        """Tag the saved files and save each soundtrack's cover beside its tracks."""
        made: list[str] = []
        done = skipped = 0
        for album, files in jobs:
            cover, mime = b"", "image/jpeg"
            if album.art:
                try:
                    with http_open(album.art, timeout=20) as resp:
                        cover = resp.read(ART_MAX_BYTES + 1)
                        mime = resp.headers.get_content_type() or mime
                    if len(cover) > ART_MAX_BYTES:
                        cover = b""
                except Exception:
                    cover = b""
            info = tags.TagInfo(album.title, album.detail, cover or None, mime, len(files))
            folders = set()
            for rf in files:
                try:
                    path = DownloadRunner.locate(rf, dest, False)
                except ValueError:
                    continue
                folders.add(path.parent.parent if tags.DISC_RE.search(path.parent.name) else path.parent)
                outcome = tags.tag_file(path, info)
                done += outcome == "tagged"
                skipped += outcome != "tagged"
            if cover:
                for folder in folders:
                    if saved := tags.save_cover_file(folder, cover, mime):
                        made.append(str(saved))
        note = f"Tagged {done} file(s)." if done else ""
        if skipped and not tags.available():
            note = "Install the 'mutagen' package to tag files; covers were still saved."
        self.ui_q.put(("st_finished", note, made))

    def _st_on_finished(self, text: str, made: list[str]) -> None:
        if made:
            st.add_to_last_save([Path(m) for m in made])
        if text:
            self.st_status.setText(self.st_status.text() + " " + text)

    def _st_pick_games_dir_only(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Games directory (holds your games or game folders)",
                                                  str(self._st_games_dir or ""))
        if folder:
            self._st_games_dir = Path(folder)
            self._st_mode_changed()

    def _st_confirm(self, plan: st.SavePlan, dest: Path) -> bool:
        moved = [g for g in plan.by_game if g in plan.moves]
        lines = [f"{len(plan.by_game)} soundtrack(s) will be added in 'soundtrack' folders under {dest}."]
        if moved:
            lines.append(f"{len(moved)} game file(s) will first be moved into a folder of their own (can be undone).")
        if plan.skipped:
            lines.append(f"{len(plan.skipped)} game(s) will be skipped.")
        detail = []
        if moved:
            detail += ["Games moved into their own folder:"] + [f"  {g}" for g in moved] + [""]
        if plan.skipped:
            detail += ["Skipped:"] + [f"  {g}: {why}" for g, why in plan.skipped]
        box = QMessageBox(QMessageBox.Icon.Question, "Save soundtracks beside games", "\n\n".join(lines),
                          QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, self)
        if detail:
            box.setDetailedText("\n".join(detail))
        return box.exec() == QMessageBox.StandardButton.Ok

    def _st_apply_moves(self, plan: st.SavePlan) -> list[tuple[str, str]]:
        """Move loose games into their folders. A game whose move fails is dropped from the plan. Returns those."""
        if not plan.moves:
            return []
        import threading as _t
        findings = [org.Finding(game, ops=ops) for game, ops in plan.moves.items()]
        _ok, failures = org.apply_findings("soundtrack", findings, lambda _m: None, lambda *_: None, _t.Event())
        failed = [(game, why) for game, why in failures]
        for game, _why in failed:
            plan.drop(game)
        return failed

    # ---- right-click menu ---------------------------------------------------------------------
    def _st_row_of_item(self, item: QTreeWidgetItem) -> SoundRow | None:
        top = item.parent() or item
        return next((r for r in self._st_rows if r.item is top), None)

    def _st_selected(self) -> tuple[dict[int, SoundRow], dict[int, tuple[SoundRow, list[QTreeWidgetItem]]]]:
        """What is selected in the list: whole games, and (per game) single tracks."""
        games: dict[int, SoundRow] = {}
        tracks: dict[int, tuple[SoundRow, list[QTreeWidgetItem]]] = {}
        for it in self.st_tree.selectedItems():
            r = self._st_row_of_item(it)
            if r is None or r.removed:
                continue
            if it.parent() is None:
                games[id(r)] = r
            else:
                tracks.setdefault(id(r), (r, []))[1].append(it)
        return games, tracks

    def _st_key_remove(self) -> None:
        games, tracks = self._st_selected()
        if games:
            self._st_remove_games(list(games.values()))
        elif tracks:
            self._st_remove_tracks(list(tracks.values()))

    def _st_key_save(self) -> None:
        games, tracks = self._st_selected()
        entries = [(r, list(r.files)) for r in games.values() if r.files]
        entries += [(r, [f for it in its if (f := it.data(0, FILE_ROLE)) is not None])
                    for rid, (r, its) in tracks.items() if rid not in games]
        if entries:
            self._st_save(entries)

    def _st_context_menu(self, pos) -> None:
        tree = self.st_tree
        item = tree.itemAt(pos)
        if item is not None and not item.isSelected():
            tree.clearSelection()
            item.setSelected(True)
        games, tracks = self._st_selected()
        menu = QMenu(self)
        if games:
            n = len(games)
            what = "soundtrack" if n == 1 else f"{n} soundtracks"
            ready = [(r, list(r.files)) for r in games.values() if r.files]
            save = menu.addAction(f"Save {what}")
            save.setEnabled(bool(ready))
            save.triggered.connect(lambda: self._st_save(ready))
            other = menu.addAction(f"Save {what} to another folder...")
            other.setEnabled(bool(ready))
            other.triggered.connect(lambda: self._st_save(ready, other_folder=True))
            opn = menu.addAction("Open save folder")
            opn.setEnabled(n == 1)
            opn.triggered.connect(lambda: self._st_open_folder(next(iter(games.values()))))
            if self._st_mode() == st.SAVE_ROMM and n == 1:
                row = next(iter(games.values()))
                match = menu.addAction("Match to game...")
                match.triggered.connect(lambda: self._st_match_game(row))
                if row.match_folder:
                    clear = menu.addAction("Clear the matched game")
                    clear.triggered.connect(lambda: self._st_clear_match(row))
            menu.addSeparator()
            rem = menu.addAction(f"Remove {what} from the list")
            rem.triggered.connect(lambda: self._st_remove_games(list(games.values())))
        if tracks:
            if games:
                menu.addSeparator()
            files = [(r, [f for it in its if (f := it.data(0, FILE_ROLE)) is not None]) for r, its in tracks.values()]
            count = sum(len(fs) for _, fs in files)
            what = "track" if count == 1 else f"{count} tracks"
            save = menu.addAction(f"Save {what}")
            save.triggered.connect(lambda: self._st_save(files))
            other = menu.addAction(f"Save {what} to another folder...")
            other.triggered.connect(lambda: self._st_save(files, other_folder=True))
            menu.addSeparator()
            rem = menu.addAction(f"Remove {what} from the list")
            rem.triggered.connect(lambda: self._st_remove_tracks([(r, its) for r, its in tracks.values()]))
        if menu.isEmpty():
            menu.deleteLater()
            return
        menu.exec(tree.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _st_match_game(self, r: SoundRow) -> None:
        """Pick, by hand, which game in the games directory this soundtrack belongs beside."""
        entries = self._st_scan()
        if not entries:
            self.st_status.setText("Choose a Games directory first (Games directory... button).")
            return
        labels = {}
        for e in sorted(entries, key=lambda e: e.folder.name.casefold()):
            rel = str(e.folder.relative_to(self._st_games_dir)) + ("" if e.is_dir else "  (loose file)")
            labels[rel] = e
        current = next((i for i, e in enumerate(labels.values()) if e.folder == r.match_folder), 0)
        label, ok = QInputDialog.getItem(self, "Match to Game", f"Which game is '{r.item.text(0)}' the soundtrack of?",
                                         list(labels), current, False)
        if ok and label in labels:
            r.match_folder = labels[label].folder
            self._st_status(r.item, f"Matched: {labels[label].folder.name}")

    def _st_clear_match(self, r: SoundRow) -> None:
        r.match_folder = None
        self._st_status(r.item, "")

    def _st_remove_games(self, rows: list[SoundRow]) -> None:
        for r in rows:  # hidden, not deleted: search workers report by row number
            r.removed = True
            r.item.setCheckState(0, Qt.CheckState.Unchecked)
            r.item.setSelected(False)
            r.item.setHidden(True)
        self._st_update_summary()

    def _st_remove_tracks(self, groups: list[tuple[SoundRow, list[QTreeWidgetItem]]]) -> None:
        for r, its in groups:
            gone = {id(f) for it in its if (f := it.data(0, FILE_ROLE)) is not None}
            for it in its:
                r.item.removeChild(it)
            r.files = [f for f in r.files if id(f) not in gone]
            self._st_prune_headers(r.item)
            known = [f.size for f in r.files if f.size is not None]
            r.item.setText(2, f"{len(r.files)} tracks" + (f", {fmt_size(sum(known))}" if known else "") if r.files else "")
            if not r.files:
                r.item.setCheckState(0, Qt.CheckState.Unchecked)
                self._st_status(r.item, "No tracks left")
            elif r.item.childCount():
                self._st_refresh_game(r.item)  # the game's box follows the tracks that are left
        self._st_update_summary()

    def _st_open_folder(self, r: SoundRow) -> None:
        mode = self._st_mode()
        root = self._st_games_dir if mode == st.SAVE_ROMM else (Path(self._clean(self.st_dest.text()) or self._st_default_dir()))
        folder = st.target_folder(root, r.item.text(0), mode, self._st_scan() if mode == st.SAVE_ROMM else None)
        while folder is not None and not folder.exists() and folder != folder.parent:
            folder = folder.parent
        if folder is None or not folder.exists():
            self.st_status.setText("Nothing has been saved for that game yet.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---- previews -----------------------------------------------------------------------------
    # The queue holds plain (title, game, url) tuples, not tree rows, so playback carries on when the person changes
    # page or searches again; the sidebar's NowPlaying widget is the control surface.
    def _st_url_playing(self) -> str | None:
        return self._st_queue[self._st_index][2] if 0 <= self._st_index < len(self._st_queue) else None

    def _st_sync_icons(self) -> None:
        """The play button of the playing track is a square (the rest are triangles), and its row is highlighted."""
        if not AUDIO_OK:
            return
        current = self._st_url_playing()
        for r in self._st_rows:
            for i in range(r.item.childCount()):
                child = r.item.child(i)
                row = self.st_tree.itemWidget(child, 0)
                if row is not None:
                    is_current = bool(current) and child.data(0, URL_ROLE) == current
                    row.set_current(is_current)
                    if row.play is not None:
                        row.play.set_playing(is_current)

    def _st_prefs_update(self, changes: dict) -> None:
        prefs = st.load_prefs()
        prefs.update(changes)
        st.save_prefs(prefs)

    def _st_output_level(self) -> float:
        """The linear level for the audio output: a slider is perceptual, so it is converted (and 0 when muted)."""
        level, muted = getattr(self, "_st_volume", 0.8), getattr(self, "_st_muted", False)
        if muted or level <= 0:
            return 0.0
        try:
            return float(QAudio.convertVolume(level, QAudio.VolumeScale.LogarithmicVolumeScale,
                                              QAudio.VolumeScale.LinearVolumeScale))
        except Exception:  # an older Qt without the converter: the slider is used as it is
            return level

    def _st_set_volume(self, level: float, muted: bool) -> None:
        self._st_volume, self._st_muted = level, muted
        if self._st_output is not None:
            self._st_output.setVolume(self._st_output_level())
        self._st_prefs_update({"volume": level, "muted": muted})

    def _st_player_get(self):
        """The media player, made the first time a preview is asked for."""
        if self._st_player is None:
            player = QMediaPlayer(self)
            output = QAudioOutput(self)
            output.setVolume(self._st_output_level())
            player.setAudioOutput(output)
            player.mediaStatusChanged.connect(self._st_media_status)
            player.errorOccurred.connect(self._st_player_error)
            player.playbackStateChanged.connect(lambda state: self.now_playing.set_playing(
                state == QMediaPlayer.PlaybackState.PlayingState))
            player.positionChanged.connect(self.now_playing.set_position)
            player.durationChanged.connect(self.now_playing.set_duration)
            self._st_player, self._st_output = player, output
        return self._st_player

    def _st_play_child(self, item: QTreeWidgetItem) -> None:
        """Play this track (it streams); pressing the track that is playing stops it."""
        parent = item.parent()
        url = item.data(0, URL_ROLE) if parent is not None else None
        if not url:
            return
        if url == self._st_url_playing():
            self._st_stop()
            return
        queue, start = [], 0
        for i in range(parent.childCount()):  # the game's tracks, so next / previous walk through the album
            child = parent.child(i)
            if child_url := child.data(0, URL_ROLE):
                if child is item:
                    start = len(queue)
                queue.append((child.data(0, TITLE_ROLE) or child.text(0), parent.text(0), child_url,
                              child.data(0, ART_ROLE) or ""))
        self._st_queue = queue
        self._st_play_index(start)

    def _st_play_index(self, index: int) -> None:
        if not 0 <= index < len(self._st_queue):
            self._st_stop()
            return
        self._st_index = index
        title, game, url, art = self._st_queue[index]
        player = self._st_player_get()
        player.setSource(QUrl(url))
        player.play()
        np = self.now_playing
        np.show_track(title, game)
        np.set_can_skip(index > 0, index + 1 < len(self._st_queue))
        np.set_duration(0)
        np.set_position(0)
        np.set_playing(True)
        motion.slide(np, True)  # the player grows up out of the sidebar
        self._st_show_art(art)
        self._st_sync_icons()

    # ---- cover art ----------------------------------------------------------------------------
    def _st_art_url_playing(self) -> str:
        return self._st_queue[self._st_index][3] if 0 <= self._st_index < len(self._st_queue) else ""

    def _st_show_art(self, art: str) -> None:
        """Put the cover on the sidebar card: straight away if cached, otherwise fetch it in the background."""
        np = self.now_playing
        if not art:
            np.set_art(None)
        elif (pix := self._st_art_cache.get(art)) is not None:
            np.set_art(pix)
        else:
            np.set_art(None, pending=True)
            if art not in self._st_art_loading:
                self._st_art_loading.add(art)
                threading.Thread(target=self._st_art_worker, args=(art,), daemon=True).start()

    def _st_art_worker(self, url: str) -> None:  # worker thread
        try:
            with http_open(url, timeout=15) as resp:
                data = resp.read(ART_MAX_BYTES)
        except Exception:  # no cover art is fine, it just isn't shown
            data = b""
        self.ui_q.put(("st_art", url, data))

    def _st_on_art(self, url: str, data: bytes) -> None:
        self._st_art_loading.discard(url)
        pix = QPixmap()
        if data and pix.loadFromData(data):
            if len(self._st_art_cache) >= ART_CACHE_MAX:
                self._st_art_cache.pop(next(iter(self._st_art_cache)))
            self._st_art_cache[url] = pix
            for row in self._st_rows:
                if row.album is not None and row.album.art == url:
                    self._st_row_icon(row)
        elif url == self._st_art_url_playing():
            self.now_playing.set_art(None)  # nothing usable came back: drop the placeholder
            return
        if url == self._st_art_url_playing() and url in self._st_art_cache:
            self.now_playing.set_art(self._st_art_cache[url])

    def _st_toggle_pause(self) -> None:
        player = self._st_player
        if player is None or self._st_index < 0:
            return
        if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            player.pause()
        else:
            player.play()

    def _st_step(self, delta: int) -> None:
        """Next (+1) or previous (-1). Previous restarts a track that is well under way, as players usually do."""
        player = self._st_player
        if player is None or self._st_index < 0:
            return
        if delta < 0 and player.position() > 3000:
            player.setPosition(0)
            return
        target = self._st_index + delta
        if 0 <= target < len(self._st_queue):
            self._st_play_index(target)
        elif delta > 0:  # ran off the end of the album
            self._st_stop()
        else:
            player.setPosition(0)

    def _st_seek(self, ms: int) -> None:
        if self._st_player is not None and self._st_index >= 0:
            self._st_player.setPosition(ms)

    def _st_stop(self) -> None:
        """Stop, forget the queue, hide the sidebar player and put every play icon back."""
        self._st_index, self._st_queue = -1, []
        if self._st_player is not None:
            self._st_player.stop()
        if np := getattr(self, "now_playing", None):
            motion.slide(np, False)
        self._st_sync_icons()

    def _st_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._st_step(1)  # on to the next track, or stop after the last

    def _st_player_error(self, _error, message: str) -> None:
        self._st_stop()
        self.st_status.setText("Couldn't play that track" + (f": {message}" if message else "")
                               + ". Previews can't use your saved logins, so private items won't stream.")
