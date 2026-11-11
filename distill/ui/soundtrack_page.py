"""Soundtrack Grabber: a media player. One search bar; the albums found are laid out like a music app's album page
(cover on the left, tracks on the right). Tracks stream from KHInsider (or archive.org); ticked tracks and albums are
saved through the Files & torrents download list, from KHInsider when it has them and archive.org otherwise."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QBrush, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

try:  # previews need Qt's media player; without it the page works the same, just without the play buttons
    from PySide6.QtMultimedia import QAudio, QAudioOutput, QMediaPlayer, QSoundEffect
    AUDIO_OK = True
except ImportError:  # pragma: no cover - depends on the install
    AUDIO_OK = False

from .. import organize as org
from .. import soundtrack_tags as tags
from .. import soundtracks as st
from ..downloads import DownloadRunner, http_open, LinkError, RemoteFile
from ..models import fmt_size
from .albumview import AlbumView, TrackInfo
from .assets import asset
from .emu_cards import hairline
from .state import LABEL_W
from .theme import T
from . import motion, msgbox, scratchmath
from .widgets import accent_button, AnimCheckBox, Card, hint, PathRow, StageSwitch, SwitchToggle

ART_CACHE_MAX = 64  # covers kept in memory
ART_MAX_BYTES = st.COVER_MAX_BYTES
SHOWN_ALBUMS = 10  # albums drawn per search; the rest are one click away

# (word in the status, symbol, colour) - the first that fits wins
STATUS_STYLE = (
    ("failed", "\u2717", "bad"), ("nothing found", "\u2717", "bad"), ("no tracks", "\u2717", "bad"),
    ("skipped", "\u26a0", "warn"), ("already", "\u2713", "ok"), ("done", "\u2713", "ok"), ("saved", "\u2713", "ok"),
    ("queued", "\u2026", "busy"), ("downloaded", "\u2026", "busy"), ("finding", "\u2026", "busy"),
)


class SoundRow:
    """One album on the page: what was found, its tracks, its widget and where it is in saving."""

    def __init__(self, album: st.Album, query: str):
        self.album = album
        self.query = query  # what was searched for: the game's name, used to find its folder in RomM mode
        self.view: AlbumView | None = None
        self.rule: QFrame | None = None
        self.files: list[RemoteFile] = []  # in the order the view lists them
        self.started = False  # its tracks have been handed to the download list
        self.saved: list[RemoteFile] = []  # the files as last handed over (named for where they go)
        self.removed = False
        self.match_folder: Path | None = None  # a game picked by hand (RomM mode)
        self.finalized = True  # nothing waiting on a finished run (tags, cover, summary)


def _track_infos(files: list[RemoteFile]) -> list[TrackInfo]:
    infos = []
    for i, f in enumerate(files, 1):
        rest = st._rest(f.name)
        folder, _, base = rest.rpartition("/")
        stem, dot, ext = base.rpartition(".")
        number, title = st.tidy_title(stem if dot else base)
        bits = [(ext or "").upper(), fmt_size(f.size) if f.size is not None else "", folder.replace("/", " / ")]
        infos.append(TrackInfo(str(number if number is not None else i), title, "  \u00b7  ".join(b for b in bits if b)))
    return infos


class SoundtrackMixin:
    """Soundtracks page. Methods only; the state lives on DistillWindow, which mixes this in."""

    def _build_sound_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self._st_pending: dict = {}
        self._st_resolve_token = 0
        self._st_stream_token = 0
        self._st_owner: SoundRow | None = None  # the album whose tracks are queued
        self._st_extra: list[st.Album] = []
        self._st_query = ""

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.st_search = QLineEdit()
        self.st_search.setPlaceholderText("Search game soundtracks...  e.g. Persona 5, Chrono Trigger")
        self.st_search.setClearButtonEnabled(True)
        self.st_search.setMinimumHeight(40)
        self.st_search.returnPressed.connect(self._st_search)
        self.st_search_btn = accent_button("Search")
        self.st_search_btn.setToolTip("Shift-click to ignore results remembered from the last few days")
        self.st_search_btn.clicked.connect(self._st_search)
        self.st_opts_btn = QPushButton("Options")
        self.st_opts_btn.setCheckable(True)
        self.st_opts_btn.setMinimumHeight(38)
        self.st_opts_btn.setToolTip("Where to save, quality, RomM and tagging")
        bar.addWidget(self.st_search, 1)
        bar.addWidget(self.st_search_btn)
        bar.addWidget(self.st_opts_btn)
        lay.addLayout(bar)

        opts = Card()
        self.st_options = opts
        self.st_dest = PathRow("Save to", label_width=LABEL_W)
        self.st_dest.edit.setAcceptDrops(False)
        self.st_dest.setText(self.dl_dest.text())
        self._sync_group([self.dl_dest.edit, self.st_dest.edit], "text", "setText", "textChanged")  # one setting
        opts.add(self.st_dest)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.st_mode = SwitchToggle()  # off = Standard, on = RomM
        self.st_mode.setToolTip(
            "Off:  <Save to>/Soundtracks/<Album>/01 - Title.flac\n\n"
            "RomM:  <Games directory>/<platform>/<Game>/soundtrack/01 - Title.flac\n"
            "The game is found by the name you searched for (right-click an album > Match to game... to pick it).")
        self.st_mode_off, self.st_mode_on = QLabel("Off"), QLabel("RomM")
        for lab in (self.st_mode_off, self.st_mode_on):
            lab.setCursor(Qt.CursorShape.PointingHandCursor)
        self.st_mode_off.mousePressEvent = lambda _e: self.st_mode.setChecked(False)
        self.st_mode_on.mousePressEvent = lambda _e: self.st_mode.setChecked(True)
        label = QLabel("RomM support")
        label.setFixedWidth(LABEL_W)
        for w in (label, self.st_mode_off, self.st_mode, self.st_mode_on):
            mode_row.addWidget(w)
        self.st_folder_btn = QPushButton("Games directory...")
        self.st_folder_btn.clicked.connect(self._st_pick_folder)
        mode_row.addSpacing(10)
        mode_row.addWidget(self.st_folder_btn)
        mode_row.addStretch(1)
        opts.add(mode_row)
        self.st_mode_hint = hint("")
        opts.add(self.st_mode_hint)
        self.st_quality = StageSwitch()
        self.st_quality.addItem("Best (FLAC first)", "best")
        self.st_quality.addItem("MP3 only", "mp3")
        self.st_quality.setToolTip("Best uses FLAC when the album has it and MP3 otherwise; MP3 only skips FLAC.")
        qrow = QHBoxLayout()
        qrow.addStretch(1)
        qrow.addWidget(self.st_quality)
        qrow.addStretch(1)
        opts.add(qrow)
        self.st_tag = AnimCheckBox("Tag the files and save a cover.jpg beside them")
        self.st_tag.setToolTip("Fills in missing title, album, artist, track and disc numbers and the cover. Existing "
                               "tags are never overwritten.\n"
                               + ("" if tags.available() else "Needs the 'mutagen' package: only cover.jpg is saved without it."))
        opts.add(self.st_tag)
        tools = QHBoxLayout()
        self.st_undo_btn = QPushButton("Undo last save")
        self.st_undo_btn.clicked.connect(lambda _c=False: self._st_undo_save())
        self.st_log_btn = QPushButton("Open move log")
        self.st_log_btn.clicked.connect(lambda _c=False: self._st_open_log())
        self.st_cache_btn = QPushButton("Forget remembered searches")
        self.st_cache_btn.clicked.connect(lambda _c=False: (st.clear_search_cache(),
                                                            self._toast("Remembered searches cleared.", "ok", 4000)))
        for b in (self.st_undo_btn, self.st_log_btn, self.st_cache_btn):
            tools.addWidget(b)
        tools.addStretch(1)
        opts.add(tools)
        opts.setVisible(False)
        self.st_opts_btn.toggled.connect(lambda on: motion.slide(opts, on) if opts.isVisible() != on else None)
        lay.addWidget(opts)

        prefs = st.load_prefs()
        self.st_tag.setChecked(prefs.get("tag", True))
        self.st_tag.toggled.connect(lambda *_: self._st_mode_changed())
        self._st_games_dir = Path(prefs["games_dir"]) if prefs.get("games_dir") else None
        self.st_mode.setChecked(prefs.get("mode") == st.SAVE_ROMM)
        self.st_mode.toggled.connect(lambda *_: self._st_mode_changed())
        if prefs.get("quality") == "mp3":
            self.st_quality.setCurrentIndex(1)
        self.st_quality.currentIndexChanged.connect(lambda *_: self._st_prefs_update({"quality": self.st_quality.currentData()}))

        self.st_scroll = QScrollArea()
        self.st_scroll.setWidgetResizable(True)
        self.st_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.st_scroll.setMinimumHeight(320)
        body = QWidget()
        self.st_results = QVBoxLayout(body)
        self.st_results.setContentsMargins(0, 0, 8, 0)
        self.st_results.setSpacing(0)
        self.st_empty = QLabel("Search for a game to see its soundtrack albums.\nPlay a track to stream it; "
                               "hover a track to save it or tick it.")
        self.st_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        T.bind_css(self.st_empty, lambda: f"color: {T.fg3.name()};")
        self.st_results.addWidget(self.st_empty)
        self.st_more_results = QPushButton("Show more results")
        self.st_more_results.setVisible(False)
        self.st_more_results.clicked.connect(self._st_show_more)
        self.st_results.addWidget(self.st_more_results, 0, Qt.AlignmentFlag.AlignHCenter)
        self.st_results.addStretch(1)
        self.st_scroll.setWidget(body)
        lay.addWidget(self.st_scroll, 1)

        self.st_status = hint("Search for a game above.", live=True)
        lay.addWidget(self.st_status)
        foot = QHBoxLayout()
        self.st_retry_btn = QPushButton("Retry failed")
        self.st_retry_btn.setVisible(False)
        self.st_retry_btn.clicked.connect(self._st_retry_failed)
        self.st_untick_btn = QPushButton("Untick all")
        self.st_untick_btn.clicked.connect(lambda: self._st_select(False))
        self.st_summary = hint("")
        self._st_failed_rows: list[int] = []
        self._st_notes: list[tuple[str, str]] = []
        foot.addWidget(self.st_retry_btn)
        foot.addWidget(self.st_untick_btn)
        foot.addWidget(self.st_summary, 1)
        lay.addLayout(foot)
        self._explain(self.st_undo_btn, "Delete the tracks the last save created and put back any game files it moved "
                                         "into their own folders", "Nothing to undo yet.")
        self._st_cascading = False
        self._st_volume, self._st_muted = float(prefs.get("volume", 0.8)), bool(prefs.get("muted", False))
        self.now_playing.set_volume(self._st_volume, self._st_muted)
        self._st_mode_changed(save=False)
        return page

    # ---- status ---------------------------------------------------------------------------------
    def _st_status(self, r: SoundRow, text: str) -> None:
        """An album's status line (under its buttons), with a coloured symbol in front."""
        if r.view is None:
            return
        low = text.lower()
        for needle, symbol, kind in STATUS_STYLE:
            if text and needle in low:
                r.view.set_status(f"{symbol} {text}", kind if kind in ("ok", "warn", "bad") else "")
                return
        r.view.set_status(text)

    def _st_update_cards(self) -> None:
        """Keep the options hint telling where things go."""
        self._st_mode_changed(save=False)

    def _persist_columns(self) -> dict[str, list[int]]:
        return {}

    def _restore_columns(self, widths: dict[str, list]) -> None:
        return

    # ---- search ---------------------------------------------------------------------------------
    def _st_pick_folder(self) -> None:
        start = str(self._st_games_dir or self._clean(self.st_dest.text()) or "")
        if folder := QFileDialog.getExistingDirectory(self, "Games directory (holds your games or game folders)", start):
            self._st_games_dir = Path(folder)
            self._st_mode_changed()

    def _st_clear_results(self) -> None:
        for r in self._st_rows:
            for w in (r.view, r.rule):
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
        self._st_rows = []
        self._st_extra = []
        self.st_more_results.setVisible(False)

    def _st_search(self) -> None:
        query = st.title_case(st.search_name(self.st_search.text().strip()))
        if not query or not self.st_search.text().strip():
            self.st_status.setText("Type a game name first.")
            return
        self._st_token += 1
        token, prefer = self._st_token, self.st_quality.currentData()
        fresh = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._st_clear_results()
        self._st_query = query
        self.st_empty.setText(f"Searching for {query}...")
        self.st_empty.setVisible(True)
        self.st_search_btn.setEnabled(False)
        self.st_status.setText("Searching...")
        self._st_update_summary()
        threading.Thread(target=self._st_search_worker, args=(token, query, prefer, fresh), daemon=True).start()

    def _st_search_worker(self, token: int, query: str, prefer: str, fresh: bool = False) -> None:  # worker thread
        def found(i: int, res: st.GameResult) -> None:
            self.ui_q.put(("st_found", token, i, res))
            shown = res.albums[:SHOWN_ALBUMS]
            self._st_list_worker(token, 0, shown, query, prefer)
        try:
            st.find_albums([st.GameRequest(query)], on_result=found, should_stop=lambda: token != self._st_token,
                           use_cache=not fresh)
        finally:
            self.ui_q.put(("st_searched", token))

    def _st_list_worker(self, token: int, first: int, albums: list[st.Album], query: str, prefer: str) -> None:  # worker thread
        """List the tracks of these albums one after another (kind to the sites); rows are numbered from `first`."""
        for offset, album in enumerate(albums):
            if token != self._st_token:
                return
            try:
                files, error = st.files_for(query, album, prefer=prefer), ""
            except LinkError as exc:
                files, error = [], str(exc)
            except Exception as exc:  # a surprise must not leave the album stuck on 'Loading tracks...'
                files, error = [], f"{type(exc).__name__}: {exc}"
            self.ui_q.put(("st_tracks", token, first + offset, album, files, error))

    # ---- results --------------------------------------------------------------------------------
    def _st_add_album(self, album: st.Album) -> SoundRow:
        r = SoundRow(album, self._st_query)
        view = AlbumView(AUDIO_OK)
        r.view = view
        view.set_info(album.title, album.detail, album.provider)
        view.playAlbum.connect(lambda rr=r: self._st_play_album(rr))
        view.saveAlbum.connect(lambda rr=r: self._st_save([(rr, list(rr.files))]))
        view.playTrack.connect(lambda i, rr=r: self._st_play_track(rr, i))
        view.saveTrack.connect(lambda i, rr=r: self._st_save([(rr, [rr.files[i]])]) if i < len(rr.files) else None)
        view.trackMenu.connect(lambda i, pos, rr=r: self._st_track_menu(rr, i, pos))
        view.moreMenu.connect(lambda btn, rr=r: self._st_album_menu(rr, btn))
        view.tickChanged.connect(self._st_update_summary)
        r.rule = hairline()
        at = self.st_results.count() - 2  # before the 'more' button and the stretch
        self.st_results.insertWidget(at, view)
        self.st_results.insertWidget(at + 1, r.rule)
        self._st_rows.append(r)
        self._st_want_art(album.art)
        self._st_row_cover(r)
        return r

    def _st_on_found(self, row: int, res: st.GameResult) -> None:
        if not res.albums:
            self.st_empty.setText(f"Search failed: {res.error}" if res.error else f"Nothing found for {self._st_query}.")
            return
        self.st_empty.setVisible(False)
        for album in res.albums[:SHOWN_ALBUMS]:
            self._st_add_album(album)
        self._st_extra = res.albums[SHOWN_ALBUMS:]
        self._st_show_more_button()

    def _st_show_more_button(self) -> None:
        self.st_more_results.setVisible(bool(self._st_extra))
        self.st_more_results.setText(f"Show {min(len(self._st_extra), SHOWN_ALBUMS)} more of {len(self._st_extra)} results")

    def _st_show_more(self) -> None:
        batch, self._st_extra = self._st_extra[:SHOWN_ALBUMS], self._st_extra[SHOWN_ALBUMS:]
        first = len(self._st_rows)
        for album in batch:
            self._st_add_album(album)
        self._st_show_more_button()
        threading.Thread(target=self._st_list_worker, args=(self._st_token, first, batch, self._st_query,
                                                             self.st_quality.currentData()), daemon=True).start()

    def _st_on_tracks(self, row: int, album: st.Album, files: list[RemoteFile], error: str) -> None:
        if row >= len(self._st_rows) or self._st_rows[row].album is not album:
            return
        r = self._st_rows[row]
        r.files = files
        self._st_want_art(album.art)
        self._st_row_cover(r)
        r.view.set_info(album.title, album.detail, album.provider)
        if error or not files:
            r.view.set_tracks([])
            r.view.set_note(error or "No tracks found.")
            return
        known = [f.size for f in files if f.size is not None]
        r.view.set_info(album.title, album.detail,
                        f"{len(files)} songs" + (f"  \u00b7  {fmt_size(sum(known))}" if known else "") + f"  \u00b7  {album.provider}")
        r.view.set_tracks(_track_infos(files))
        if have := self._st_existing(self._st_game(r)):
            self._st_status(r, f"Already have {have} file(s)")
        self._st_sync_icons()
        self._st_update_summary()

    def _st_on_searched(self) -> None:
        self.st_search_btn.setEnabled(True)
        n = len([r for r in self._st_rows if not r.removed])
        if n:
            self.st_status.setText(f"{n} album(s) shown. Play a track to stream it; hover a track to save it.")
        elif not self.st_empty.isVisible():
            self.st_empty.setVisible(True)
        self._st_update_summary()

    def _st_row_cover(self, r: SoundRow) -> None:
        if r.view is not None:
            r.view.set_cover(self._st_art_cache.get(r.album.art))

    def _st_want_art(self, url: str) -> None:
        if url and url not in self._st_art_cache and url not in self._st_art_loading:
            self._st_art_loading.add(url)
            threading.Thread(target=self._st_art_worker, args=(url,), daemon=True).start()

    # ---- ticking and saving ---------------------------------------------------------------------
    def _st_game(self, r: SoundRow) -> str:
        """The name its folder gets: the album's own title in Standard mode, the game searched for in RomM mode."""
        return r.query if self._st_mode() == st.SAVE_ROMM else r.album.title

    def _st_select(self, on: bool) -> None:
        for r in self._st_rows:
            if r.view is not None and r.files and not r.removed:
                r.view.set_all_checked(on)

    def _st_ticked(self) -> list[SoundRow]:
        return [r for r in self._st_rows if r.files and not r.removed and r.view is not None and r.view.checked_indices()]

    def _st_checked_files(self, r: SoundRow) -> list[RemoteFile]:
        return [r.files[i] for i in r.view.checked_indices() if i < len(r.files)] if r.view is not None else []

    def _st_update_summary(self) -> None:
        picks = self._st_ticked()
        files = [f for r in picks for f in self._st_checked_files(r)]
        size = sum(f.size or 0 for f in files)
        self.st_summary.setText(f"{len(files)} track(s) ticked from {len(picks)} album(s)" + (f"  |  {fmt_size(size)}" if size else "")
                                if picks else "")

    def _st_download(self) -> None:
        picks = self._st_ticked()
        if not picks:
            self.st_status.setText("Tick some tracks first (hover a track), or press an album's save button.")
            return
        self._st_save([(r, self._st_checked_files(r)) for r in picks], batch=True)

    def _st_refresh_status(self) -> None:
        """While a download runs, show each started album's progress."""
        for r in self._st_rows:
            if not r.started or not r.saved:
                continue
            rows = [i for f in r.saved if (i := self.dl_model.row_of(f)) is not None]
            if not rows:
                continue
            done = sum(self.dl_model.is_finished(i) for i in rows)
            self._st_status(r, "Done" if done == len(rows) else f"{done} / {len(rows)} downloaded")

    # ---- save mode and places -------------------------------------------------------------------
    def _st_mode(self) -> str:
        return st.SAVE_ROMM if self.st_mode.isChecked() else st.SAVE_STANDARD

    def _st_default_dir(self) -> Path:
        return self._dl_default_dir()

    def _st_mode_changed(self, save: bool = True) -> None:
        romm = self._st_mode() == st.SAVE_ROMM
        for lab, active in ((self.st_mode_off, not romm), (self.st_mode_on, romm)):
            font = lab.font()
            font.setBold(active)
            lab.setFont(font)
        self.st_dest.setEnabled(not romm)
        self.st_folder_btn.setVisible(romm)
        if romm:
            where = str(self._st_games_dir) if self._st_games_dir else "not chosen yet - press Games directory..."
            self.st_mode_hint.setText(f"RomM: tracks go in a 'soundtrack' folder beside each game. Games directory: {where}")
        else:
            self.st_mode_hint.setText("Off: tracks go in <Save to>/Soundtracks/<Album>/. A blank Save to uses your Downloads folder.")
        self._st_update_undo()
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
        if res is None and (moved := org.undo_last("soundtrack")) is not None:
            res = (0, moved[0], moved[1])
        if res is None:
            self.st_status.setText("Nothing to undo.")
        else:
            removed, restored, problems = res
            bits = [f"removed {removed} track file(s)"] + ([f"put back {restored} game(s)"] if restored else [])
            self.st_status.setText("Undid the last save: " + ", ".join(bits)
                                   + (f". {len(problems)} problem(s): {problems[0]}" if problems else "."))
        for r in self._st_rows:
            if r.started:
                r.started, r.saved = False, []
                self._st_status(r, "")
        self._st_update_undo()

    def _st_open_log(self) -> None:
        path = org.run_log_file()
        if not path.exists():
            self.st_status.setText("No game files have been moved yet, so there is no log.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ---- saving (an album, ticked tracks, or one track) -----------------------------------------
    def _st_save(self, entries: list[tuple[SoundRow, list[RemoteFile]]], batch: bool = False,
                 other_folder: bool = False, resolved: bool = False) -> None:
        """Save these (album row, files) pairs. khinsider tracks first have their real file addresses looked up
        (and an album that cannot be had there is taken from archive.org instead). Standard mode files them under
        Soundtracks/, RomM mode beside the game. `other_folder` asks where to save (Standard layout)."""
        entries = [(r, fs) for r, fs in entries if fs]
        if not entries:
            self.st_status.setText("Nothing to save: those albums have no tracks listed yet.")
            return
        if not resolved and any(st.is_khinsider_page(f.url) for _r, fs in entries for f in fs):
            self._st_resolve_token += 1
            token = self._st_resolve_token
            self._st_pending[token] = (entries, batch, other_folder)
            prefer = self.st_quality.currentData()
            jobs = []
            for i, (r, fs) in enumerate(entries):
                self._st_status(r, "Finding files...")
                jobs.append((i, self._st_game(r), r.album, fs, len(fs) == len(r.files)))
            self.st_status.setText("Finding the files...")
            threading.Thread(target=self._st_resolve_worker, args=(token, jobs, prefer), daemon=True).start()
            return
        mode = st.SAVE_STANDARD if other_folder else self._st_mode()
        if mode == st.SAVE_ROMM:  # two albums of one game would share a folder and collide: the first goes, the rest wait
            seen, kept = set(), []
            for r, fs in entries:
                key = self._st_game(r).casefold()
                if key not in seen:
                    seen.add(key)
                    kept.append((r, fs))
            if len(kept) < len(entries):
                self.st_status.setText("RomM mode: one album per game at a time. Saved the first; save the other next.")
            entries = kept
        items = [(self._st_game(r), fs) for r, fs in entries]
        scan = None
        if mode == st.SAVE_ROMM:
            if self._st_games_dir is None or not self._st_games_dir.is_dir():
                self._st_pick_games_dir_only()
            scan = self._st_scan()
            if scan is None:
                self.st_status.setText("RomM mode needs a games directory: open Options and press Games directory...")
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
            dest = Path(raw) if raw else self._st_default_dir()
        picks: dict[str, st.GameEntry] = {}
        for r, _ in entries:
            if mode == st.SAVE_ROMM and r.match_folder and scan is not None:
                if hit := next((e for e in scan if e.folder == r.match_folder), None):
                    picks[self._st_game(r)] = hit
        plan = st.plan_save(items, mode, dest, scan, picks)

        if len(items) == 1 and plan.ambiguous:
            game, hits = next(iter(plan.ambiguous.items()))
            labels = [str(e.folder.relative_to(dest)) for e in hits]
            label, ok = msgbox.get_item(self, "Which Game?", f"'{game}' matches more than one game:", labels, 0, False)
            if not ok:
                return
            plan = st.plan_save(items, mode, dest, scan, {**picks, game: hits[labels.index(label)]})
        if not plan.files and mode == st.SAVE_ROMM and len(items) == 1:
            why = plan.skipped[0][1] if plan.skipped else "no match"
            ask = msgbox.question(self, "Game Not Found", f"'{items[0][0]}': {why}.\n\nSave it to your Downloads folder instead?")
            if ask != QMessageBox.StandardButton.Yes:
                return
            mode, dest = st.SAVE_STANDARD, self._st_default_dir()
            items = [(entries[0][0].album.title, items[0][1])]
            plan = st.plan_save(items, mode, dest)
        if not plan.files:
            self.st_status.setText("Nothing saved: " + "; ".join(f"{g}: {why}" for g, why in plan.skipped[:3]))
            return
        if mode == st.SAVE_ROMM and (batch or plan.moves or plan.skipped):
            if not self._st_confirm(plan, dest):
                return
        runner = self._dl_runner()
        if runner is not None and self._dl_dest_path() != dest:
            self.st_status.setText(f"A download to {self._dl_dest_path()} is still running. Wait for it to finish, then save again.")
            return
        failed = self._st_apply_moves(plan)
        if not plan.files:
            self.st_status.setText("Nothing saved: " + "; ".join(f"{g}: {why}" for g, why in failed[:3]))
            return

        self.dl_dest.setText(str(dest))
        if not other_folder and mode == st.SAVE_STANDARD and not self._clean(self.st_dest.text()):
            self.st_dest.setText(str(dest))
        notes = plan.skipped + failed
        self._st_notes, self._st_failed_rows = notes, []
        motion.fade(self.st_retry_btn, False)
        names = [g for g, _ in items]
        for (r, _), game in zip(entries, names):
            if game in plan.by_game:
                r.saved, r.started, r.finalized = plan.by_game[game], True, False
                self._st_status(r, "Queued")
            elif why := dict(notes).get(game):
                self._st_status(r, f"Skipped: {why}")

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

    def _st_resolve_worker(self, token: int, jobs: list, prefer: str) -> None:  # worker thread
        out = []
        for i, game, album, files, whole in jobs:
            new, problems = st.resolve_files(files, prefer)
            note = ""
            if len(new) < len(files):
                fb = st.archive_fallback(game, album.title, prefer) if whole and album.provider == st.KHInsiderProvider.name else None
                if fb is not None:
                    new, note = fb[1], "Saved from archive.org instead"
                elif problems:
                    note = problems[0]
            out.append((i, new, note))
        self.ui_q.put(("st_resolved", token, out))

    def _st_on_resolved(self, token: int, out: list) -> None:
        pending = self._st_pending.pop(token, None)
        if pending is None:
            return
        entries, batch, other_folder = pending
        ready = []
        for i, files, note in out:
            r = entries[i][0]
            if files:
                ready.append((r, files))
                self._st_status(r, "")
            else:
                self._st_status(r, f"Failed: {note or 'no files found'}")
            if note and files:
                self.st_status.setText(f"{r.album.title}: {note}.")
        if ready:
            self._st_save(ready, batch, other_folder, resolved=True)
        else:
            self.st_status.setText("Couldn't find files for those tracks on khinsider or archive.org.")

    # ---- when the run ends: summary, retry, tags ------------------------------------------------
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
            r.finalized = not bad
            tracks += len(ok)
            self._st_failed_rows += bad
            if bad:
                failed_games += 1
                self._st_status(r, f"{len(bad)} failed, {len(ok)} saved")
            else:
                saved_games += 1
                self._st_status(r, "Done")
            if ok and dest is not None and self.st_tag.isChecked():
                jobs.append((r.album, ok))
        parts = [f"Saved {tracks} track(s) from {saved_games + failed_games} album(s)"]
        if failed_games:
            parts.append(f"{len(self._st_failed_rows)} track(s) failed")
        if self._st_notes:
            parts.append(f"{len(self._st_notes)} skipped")
        self.st_status.setText(", ".join(parts) + ".")
        motion.fade(self.st_retry_btn, bool(self._st_failed_rows))
        if saved_games and not failed_games and dest is not None:
            self._toast(f"Saved {tracks} track(s) from {saved_games} album(s).", "ok", 20000,
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
        """Tag the saved files and save each album's cover beside its tracks."""
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
        choice, _tick = msgbox.ask(self, "question", "Save soundtracks beside games", "\n\n".join(lines),
                                   [("OK", "accept"), ("Cancel", "reject")], default=0, escape=1, details="\n".join(detail))
        return choice == 0

    def _st_apply_moves(self, plan: st.SavePlan) -> list[tuple[str, str]]:
        if not plan.moves:
            return []
        findings = [org.Finding(game, ops=ops) for game, ops in plan.moves.items()]
        _ok, failures = org.apply_findings("soundtrack", findings, lambda _m: None, lambda *_: None, threading.Event())
        failed = [(game, why) for game, why in failures]
        for game, _why in failed:
            plan.drop(game)
        return failed

    # ---- menus ----------------------------------------------------------------------------------
    def _st_album_menu(self, r: SoundRow, button) -> None:
        menu = QMenu(self)
        ready = bool(r.files)
        menu.addAction("Save album").triggered.connect(lambda: self._st_save([(r, list(r.files))]))
        menu.actions()[0].setEnabled(ready)
        other = menu.addAction("Save album to another folder...")
        other.setEnabled(ready)
        other.triggered.connect(lambda: self._st_save([(r, list(r.files))], other_folder=True))
        menu.addAction("Open save folder").triggered.connect(lambda: self._st_open_folder(r))
        menu.addAction("Tick all tracks").triggered.connect(lambda: r.view.set_all_checked(True))
        if self._st_mode() == st.SAVE_ROMM:
            menu.addAction("Match to game...").triggered.connect(lambda: self._st_match_game(r))
            if r.match_folder:
                menu.addAction("Clear the matched game").triggered.connect(lambda: self._st_clear_match(r))
        menu.addSeparator()
        menu.addAction("Remove from the list").triggered.connect(lambda: self._st_remove_games([r]))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        menu.deleteLater()

    def _st_track_menu(self, r: SoundRow, i: int, pos) -> None:
        if i >= len(r.files):
            return
        menu = QMenu(self)
        menu.addAction("Save track").triggered.connect(lambda: self._st_save([(r, [r.files[i]])]))
        menu.addAction("Save track to another folder...").triggered.connect(
            lambda: self._st_save([(r, [r.files[i]])], other_folder=True))
        menu.exec(pos)
        menu.deleteLater()

    def _st_match_game(self, r: SoundRow) -> None:
        entries = self._st_scan()
        if not entries:
            self.st_status.setText("Choose a Games directory first (Options > Games directory...).")
            return
        labels = {}
        for e in sorted(entries, key=lambda e: e.folder.name.casefold()):
            rel = str(e.folder.relative_to(self._st_games_dir)) + ("" if e.is_dir else "  (loose file)")
            labels[rel] = e
        current = next((i for i, e in enumerate(labels.values()) if e.folder == r.match_folder), 0)
        label, ok = msgbox.get_item(self, "Match to Game", f"Which game is '{r.album.title}' the soundtrack of?",
                                    list(labels), current, False)
        if ok and label in labels:
            r.match_folder = labels[label].folder
            self._st_status(r, f"Matched: {labels[label].folder.name}")

    def _st_clear_match(self, r: SoundRow) -> None:
        r.match_folder = None
        self._st_status(r, "")

    def _st_remove_games(self, rows: list[SoundRow]) -> None:
        for r in rows:  # hidden, not deleted: workers report by row number
            r.removed = True
            for w in (r.view, r.rule):
                if w is not None:
                    w.setVisible(False)
        self._st_update_summary()

    def _st_open_folder(self, r: SoundRow) -> None:
        mode = self._st_mode()
        root = self._st_games_dir if mode == st.SAVE_ROMM else Path(self._clean(self.st_dest.text()) or self._st_default_dir())
        folder = st.target_folder(root, self._st_game(r), mode, self._st_scan() if mode == st.SAVE_ROMM else None)
        while folder is not None and not folder.exists() and folder != folder.parent:
            folder = folder.parent
        if folder is None or not folder.exists():
            self.st_status.setText("Nothing has been saved for that album yet.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


    # ---- previews -----------------------------------------------------------------------------
    # The queue holds plain (title, game, url) tuples, not tree rows, so playback carries on when the person changes
    # page or searches again; the sidebar's NowPlaying widget is the control surface.
    def _st_url_playing(self) -> str | None:
        return self._st_queue[self._st_index][2] if 0 <= self._st_index < len(self._st_queue) else None

    def _st_is_playing(self) -> bool:
        return bool(AUDIO_OK and self._st_player is not None
                    and self._st_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)

    def _st_sync_icons(self) -> None:
        """Show which track (and album) is playing: its play button is a square, its row is highlighted."""
        if not AUDIO_OK:
            return
        playing = self._st_is_playing()
        for r in self._st_rows:
            if r.view is None:
                continue
            mine = r is self._st_owner and self._st_index >= 0
            r.view.set_playing(self._st_index if mine else None, playing, mine)

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
            player.playbackStateChanged.connect(lambda state: (self.now_playing.set_playing(
                state == QMediaPlayer.PlaybackState.PlayingState), self._st_sync_icons()))
            player.positionChanged.connect(self.now_playing.set_position)
            player.durationChanged.connect(self.now_playing.set_duration)
            self._st_player, self._st_output = player, output
            self._st_vinyl_get()  # the record sounds load now, so the first hold of the disc has no wait
        return self._st_player

    def _st_play_album(self, r: SoundRow) -> None:
        """The big play button: start the album, or pause / resume it when it is the one queued."""
        if r is self._st_owner and self._st_index >= 0:
            self._st_toggle_pause()
        else:
            self._st_play_track(r, 0)

    def _st_play_track(self, r: SoundRow, i: int) -> None:
        """Play this track (it streams) and queue the rest of its album; pressing the playing one pauses it."""
        if not r.files or not 0 <= i < len(r.files):
            return
        if r is self._st_owner and i == self._st_index:
            self._st_toggle_pause()
            return
        self._st_owner = r
        self._st_queue = [(st.tidy_title(st._rest(f.name).rpartition("/")[2].rpartition(".")[0])[1], r.album.title,
                           f.url, r.album.art or "") for f in r.files]
        self._st_play_index(i)

    def _st_play_index(self, index: int) -> None:
        if not 0 <= index < len(self._st_queue):
            self._st_stop()
            return
        self._st_index = index
        title, game, url, art = self._st_queue[index]
        player = self._st_player_get()
        player.stop()
        self._st_stream_token += 1
        token = self._st_stream_token
        np = self.now_playing
        np.show_track(title, game)
        np.set_can_skip(index > 0, index + 1 < len(self._st_queue))
        np.set_duration(0)
        np.set_position(0)
        np.set_playing(True)
        motion.slide(np, True)  # the player grows up out of the sidebar
        self._st_show_art(art)
        self._st_sync_icons()
        if st.is_khinsider_page(url):  # the file address is on the track's page: look it up, then stream
            prefer = self.st_quality.currentData()
            threading.Thread(target=self._st_stream_worker, args=(token, index, url, prefer), daemon=True).start()
        else:
            self._st_start_stream(url)

    def _st_stream_worker(self, token: int, index: int, url: str, prefer: str) -> None:  # worker thread
        try:
            direct, error = st.resolve_url(url, prefer), ""
        except LinkError as exc:
            direct, error = "", str(exc)
        except Exception as exc:
            direct, error = "", f"{type(exc).__name__}: {exc}"
        self.ui_q.put(("st_stream", token, index, direct, error))

    def _st_on_stream(self, token: int, index: int, url: str, error: str) -> None:
        if token != self._st_stream_token or index != self._st_index:
            return  # another track was chosen meanwhile
        if error or not url:
            self._st_stop()
            self.st_status.setText("Couldn't find that track's file" + (f": {error}" if error else "") + ".")
            return
        self._st_start_stream(url)

    def _st_start_stream(self, url: str) -> None:
        player = self._st_player_get()
        player.setSource(QUrl(url))
        player.play()

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
                    self._st_row_cover(row)
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

    # ---- the disc: hold it to stop the record, turn it to wind the track forward and back (a small easter egg) -------------------
    def _st_vinyl_get(self) -> dict:
        """The record sounds (bundled in assets/sounds), loaded the first time the disc is held."""
        if getattr(self, "_st_vinyl", None) is None:
            self._st_vinyl = {}
            if AUDIO_OK:
                for name in ("vinyl-stop", "vinyl-start", "scratch-fwd", "scratch-back"):
                    effect = QSoundEffect(self)
                    effect.setSource(QUrl.fromLocalFile(str(asset("sounds", name + ".wav"))))
                    self._st_vinyl[name] = effect
        return self._st_vinyl

    VINYL_START_MS = 500  # how long vinyl-start.wav runs: the music waits this long after letting go so the record "spins up" first
    VINYL_SOFTEN = 0.7  # the record sounds sit a little under the music's own volume

    def _st_vinyl_play(self, name: str, gain: float = 1.0) -> None:
        """One of the record sounds, at the player's own volume (silent when it is muted)."""
        level = min(self._st_output_level() * gain * self.VINYL_SOFTEN, 1.0)
        if level <= 0 or (effect := self._st_vinyl_get().get(name)) is None:
            return
        effect.setVolume(level)
        effect.play()

    def _st_scratch_start(self) -> None:
        """The disc was held: pause where it is, with the sound of a record being stopped."""
        player = self._st_player
        if player is None or self._st_index < 0:
            return
        self._st_scratch = {"was_playing": player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
                            "start": player.position(), "target": None, "applied": None, "grain": 0.0}
        if getattr(self, "_st_scratch_timer", None) is None:
            self._st_scratch_timer = QTimer(self)  # seeking a stream on every mouse move would stall it: at most one a tick
            self._st_scratch_timer.setSingleShot(True)
            self._st_scratch_timer.setInterval(50)
            self._st_scratch_timer.timeout.connect(self._st_scratch_apply)
        if getattr(self, "_st_resume_timer", None) is not None:
            self._st_resume_timer.stop()  # grabbed again before the spin-up finished: the music stays held
        player.pause()
        self._st_vinyl_play("vinyl-stop")

    def _st_scratch_apply(self) -> None:
        s = getattr(self, "_st_scratch", None)
        if s is not None and s["target"] is not None and s["target"] != s["applied"] and self._st_player is not None:
            s["applied"] = s["target"]
            self._st_player.setPosition(s["target"])

    def _st_scratch_move(self, turned: float, speed: float) -> None:
        """The disc was turned `turned` degrees from where it was grabbed: seek that far (clockwise is forward) and scratch."""
        s, player = getattr(self, "_st_scratch", None), self._st_player
        if s is None or player is None:
            return
        s["target"] = scratchmath.scrub_ms(s["start"], turned, player.duration())
        if not self._st_scratch_timer.isActive():
            self._st_scratch_apply()
            self._st_scratch_timer.start()
        now, gain = time.monotonic(), scratchmath.grain_gain(speed)
        if gain and now - s["grain"] >= scratchmath.grain_gap_ms(speed) / 1000:
            s["grain"] = now
            self._st_vinyl_play("scratch-fwd" if speed > 0 else "scratch-back", gain)

    def _st_scratch_end(self) -> None:
        """The disc was let go: carry on from where it was turned to, if the music was playing when it was held."""
        s, self._st_scratch = getattr(self, "_st_scratch", None), None
        player = self._st_player
        if getattr(self, "_st_scratch_timer", None) is not None:
            self._st_scratch_timer.stop()
        if s is None or player is None or self._st_index < 0:
            return
        if s["target"] is not None:
            player.setPosition(s["target"])
        if s["was_playing"]:
            self._st_vinyl_play("vinyl-start")  # the music starts only once the spin-up sound has played
            if getattr(self, "_st_resume_timer", None) is None:
                self._st_resume_timer = QTimer(self)
                self._st_resume_timer.setSingleShot(True)
                self._st_resume_timer.timeout.connect(self._st_scratch_resume)
            self._st_resume_timer.start(self.VINYL_START_MS)

    def _st_scratch_resume(self) -> None:
        """The spin-up sound is done: now the music carries on."""
        player = self._st_player
        if player is not None and self._st_index >= 0 and getattr(self, "_st_scratch", None) is None:
            player.play()

    def _st_stop(self) -> None:
        """Stop, forget the queue, hide the sidebar player and put every play icon back."""
        self._st_index, self._st_queue, self._st_owner = -1, [], None
        self._st_stream_token += 1
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
                               + ".")
