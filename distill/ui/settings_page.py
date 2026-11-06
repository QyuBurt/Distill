"""Settings window and tool paths."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .. import magnet, ytdlp
from ..constants import (
    PKG_LAYOUTS, PKG_MISSING_HINT,
    PKG_TOOLS, PS3_OUTPUTS, RVZ_DEFAULT_LEVEL,
    RVZ_MAX_LEVEL, TOOL_MISSING_TEXT,
)
from ..downloads import DL_MAX_PARALLEL
from ..system import portable_path, resolve_dolphintool, resolve_tool_path
from . import motion
from .state import LABEL_W, TOOL_GROUPS, TOOLS
from .dialogs import LoginsDialog, TorrentDialog
from .motion import Motion
from .rules_panel import RulesPanel
from .ytdlp_card import YtdlpCard
from .widgets import (AnimCheckBox, AnimRadio, Card, Expander, hint, PathRow, StageSwitch, StepSwitch, ToolRow)


class SettingsMixin:
    """Settings window and tool paths: where each external tool lives and whether it can be run.

    Methods only; the state lives on DistillWindow, which mixes this in."""

    def _make_settings_widgets(self) -> None:
        def spin(lo: int, hi: int, value: int, suffix: str = "") -> QSpinBox:
            box = QSpinBox()
            box.setRange(lo, hi)
            box.setValue(value)
            box.setSuffix(suffix)
            box.setMinimumWidth(90)
            box.setKeyboardTracking(False)
            return box

        def combo(items, current: str | None = None) -> QComboBox:
            box = QComboBox()
            box.addItems(list(items))
            if current is not None:
                box.setCurrentText(current)
            box.setMinimumWidth(180)
            return box

        def stages(items: list[tuple[str, object, str]]) -> StageSwitch:
            switch = StageSwitch(describe="below")  # the description sits under the switch: the half-width columns have no room beside it
            for text, data, description in items:
                switch.addItem(text, data, description)
            return switch

        # choose among a few named levels: a sliding switch, with what each level means shown underneath
        self.chd_compression = stages([
            ("Default", "default", "Smallest files, slowest."),
            ("Fast", "fast", "Quicker, a little larger."),
            ("Fastest", "fastest", "Quickest, little saving."),
            ("None", "none", "No compression: the same size as the source."),
        ])
        self.cso_speed = stages([
            ("Default", "default", "Smallest files, slowest."),
            ("Fast", "fast", "Much quicker, a little larger."),
        ])
        self.rvz_scrub = AnimCheckBox("Scrub junk data (smaller files, but not bit-for-bit identical)")
        # zstd levels as one pill with a chevron at each end (like Settings > Downloads > Overall limit); what the level means sits underneath
        self.rvz_level = StepSwitch()
        for n in range(1, RVZ_MAX_LEVEL + 1):
            how = ("Fastest, largest files." if n <= 3 else "Balanced speed and size." if n <= 9
                   else "Smaller files, noticeably slower." if n <= 17 else "Smallest files, very slow and memory hungry.")
            self.rvz_level.addItem(str(n), n, f"Level {n} of {RVZ_MAX_LEVEL}: {how}" + (" (default)" if n == RVZ_DEFAULT_LEVEL else ""))
        self.rvz_level.setValue(RVZ_DEFAULT_LEVEL)
        # how much of the machine to use: four steps, each showing the thread count it means
        steps: dict[int, str] = {}
        for share, name in ((0.25, "Low"), (0.5, "Medium"), (0.75, "High"), (1.0, "All")):
            steps[max(1, round(self.sys_threads * share))] = name  # a later name wins when counts coincide
        self.thread_budget = StageSwitch(describe="below")
        for count, name in sorted(steps.items()):
            self.thread_budget.addItem(name, count, f"{count} of {self.sys_threads} system threads - dynamically "
                                                    "split across tasks")
        self.thread_budget.setValue(self.sys_threads)  # low priority makes "all cores" safe
        self.io_mode = stages([("Auto", "auto", "Auto-detect: only spinning disks are serialized."),
                               ("HDD", "hdd", "Mechanical HDD: always one disk-heavy task at a time."),
                               ("SSD", "ssd", "SSD / NVMe: no limits.")])
        self.temp_dir = PathRow("Temp folder", label_width=LABEL_W)
        self.ps3_keys = PathRow("Keys folder", label_width=LABEL_W)
        self.ps3_output = combo(PS3_OUTPUTS)
        self.pkg_layout = combo(PKG_LAYOUTS)
        self.pkg_merge = AnimCheckBox("Merge each game's updates into it")  # PS4 page: one patched folder per game
        self.pkg_zar = AnimCheckBox("Convert to .zar after extraction")  # PS4 page: pack each extraction into a .zar
        self.ps5_anyps5 = AnimRadio("AnyPS5  -  source/ + app0/, relinked into app.exe with libs/")
        self.ps5_kyty = AnimRadio("KytyPS5  -  the game files with eboot.bin at the root, packed into a .zar")
        self.ps5_anyps5.setChecked(True)

    def _build_perf_card(self) -> Expander:
        card = Expander("Performance & CPU Budget")
        lay = card.body_lay

        def row(label: str, widget: QWidget, note: str = "") -> None:
            v = QVBoxLayout()  # label over the control: the card is half a page wide, and a label beside it left no room
            v.setSpacing(6)
            lbl = QLabel(label)
            lbl.setObjectName("pathLabel")
            v.addWidget(lbl)
            v.addWidget(widget)
            if note:
                v.addWidget(hint(note))
            lay.addLayout(v)

        lay.setSpacing(14)
        row("Total thread budget", self.thread_budget)
        row("Disk I/O mode", self.io_mode)
        lay.addWidget(self.temp_dir)
        lay.addWidget(hint("Optional. Extract archives to a RAM disk or SSD instead of the system temp folder. "
                           "It needs room for about two unpacked archives."))
        return card

    def _tool_text(self, key: str) -> str:
        rows = self._tool_rows[key]
        return rows[0].path.text().strip() if rows else ""

    def _open_settings(self, page: int = 0) -> None:
        self.settings_list.setCurrentRow(page)
        self.settings_dlg.show()
        self.settings_dlg.raise_()
        self.settings_dlg.activateWindow()

    def _logins_saved(self) -> None:
        self.dl_remember = self.logins_panel.remember_value

    def _set_reduce_motion(self, on: bool) -> None:
        Motion.reduce = on
        self._sync_dot_blink()
        self.start_btn.set_state(bool(self.is_processing or self.org_busy), bool(self.is_paused and self.is_processing))
        self.update()

    def _settings_filter(self, text: str) -> None:
        """Show only the settings whose words contain what was typed; jump to a page that has any."""
        terms = text.lower().split()

        def words(widget) -> str:
            parts = [widget.text() if isinstance(widget, (QLabel, QAbstractButton)) else ""]
            parts += [w.text() for w in widget.findChildren(QLabel)] + [w.text() for w in widget.findChildren(QAbstractButton)]
            return " ".join(parts).lower()

        def match(blob: str) -> bool:
            return all(t in blob for t in terms)

        first_with_hits = -1
        for index in range(self.settings_stack.count()):
            page = self.settings_stack.widget(index).widget()
            hits = 0
            for card in page.findChildren(Card):
                title = (t.text() if (t := card.findChild(QLabel, "cardTitle")) else "").lower()
                title_hit = bool(terms) and match(title)
                items = [card.lay.itemAt(i) for i in range(card.lay.count())]
                rows = [w for item in items if (w := item.widget()) is not None and w.objectName() != "cardTitle"]
                visible = 0
                shows = []
                for row in rows:
                    show = not terms or title_hit or match(words(row))
                    row.setVisible(show)  # at once: the card around it is what animates, and it clips them
                    shows.append(show)
                    visible += show
                if any(item.layout() is not None for item in items):  # rows held in a layout (not a widget): a switch row, say
                    # their words are the whole card's, read only when the title didn't already decide it
                    visible += bool(not terms or title_hit or match(" ".join(w.text() for w in card.findChildren(QLabel)).lower()))
                want = not terms or visible > 0 or title_hit
                state = (want, tuple(shows))
                if state != getattr(card, "_filter_state", (True, (True,) * len(shows))):  # only a real change moves
                    if page.isVisible():
                        motion.slide(card, want)  # opens, closes or resizes to fit the rows that are left
                    else:
                        card.setVisible(want)
                card._filter_state = state
                hits += want
            if hits and first_with_hits < 0:
                first_with_hits = index
        if terms and first_with_hits >= 0 and self.settings_stack.currentIndex() != first_with_hits:
            current = self.settings_stack.currentWidget().widget()
            if not any(getattr(c, "_filter_state", (True,))[0] for c in current.findChildren(Card)):
                self.settings_list.setCurrentRow(first_with_hits)

    def _tool_missing(self, text: str) -> None:
        """A needed tool isn't set up: say so, then open Settings on the Tools page."""
        self._error("Executable Not Found", text + "\n\nTool paths are in Settings (the gear at the bottom left).")
        self._open_settings(0)

    def _build_settings_dialog(self) -> None:
        dlg = self.settings_dlg = QDialog(self)  # created once and kept, so its fields hold their values
        dlg.setWindowTitle("Settings")
        dlg.resize(940, 680)
        dlg.setMinimumSize(760, 480)
        root = QHBoxLayout(dlg)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        nav = self.settings_list = QListWidget()
        nav.setFrameShape(QFrame.Shape.NoFrame)
        nav.setSpacing(2)
        nav.addItems(["Tools", "Downloads", "Accounts", "General"])
        stack = self.settings_stack = QStackedWidget()
        nav.currentRowChanged.connect(stack.setCurrentIndex)
        left = QWidget()
        left.setFixedWidth(190)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(8, 10, 8, 0)
        ll.setSpacing(6)
        self.settings_search = QLineEdit()
        self.settings_search.setPlaceholderText("Search settings")
        self.settings_search.setClearButtonEnabled(True)
        self.settings_search.textChanged.connect(self._settings_filter)
        ll.addWidget(self.settings_search)
        ll.addWidget(nav, 1)
        root.addWidget(left)
        root.addWidget(stack, 1)

        def page(title: str) -> tuple[QVBoxLayout, QWidget]:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # nothing runs off to the right
            inner = QWidget()
            # Ignored: the page is always exactly as wide as the window shows, never wider because of its widest
            # row, so every settings page has the same width and nothing runs off to the right.
            inner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lay = QVBoxLayout(inner)
            lay.setContentsMargins(24, 20, 24, 20)
            lay.setSpacing(10)
            head = QLabel(title)
            head.setObjectName("dialogHead")
            lay.addWidget(head)
            scroll.setWidget(inner)
            stack.addWidget(scroll)
            return lay, inner

        # -- Tools: every conversion tool once; the app finds and updates its own copies in ./tools
        lay, _ = page("Tools")
        lay.addWidget(hint("Tools are found and updated automatically next to this app. Browse only to use a different copy.",
                           tip="Each tool is found and kept up to date automatically in the tools folder next to this app. "
                               "Browse only if you want to use a different copy. The dots in the side panel show which "
                               "conversion tools are ready; the dots at the bottom right of Files & Torrents show yt-dlp, "
                               "ffmpeg and your default downloader."))
        for group, tools in TOOL_GROUPS.items():
            card = Card(group)
            for key, label in tools.items():
                row = ToolRow(label, LABEL_W)
                self._register_tool_row(key, row)
                card.add(row)
            lay.addWidget(card)
        keys = Card("Switch keys")
        keys.add(hint("Needed to open Switch 1 files (nscb_rust) and to decrypt them for NUP (hactoolnet). Dump them from your own "
                      "console with Lockpick_RCM. Distill only remembers where the files are; it never copies or uploads them.",
                      tip="prod.keys: required for NSZ / XCZ / merge and for decrypting NSP / XCI. title.keys: optional, for games "
                          "whose files carry no ticket (the tickets inside a file are used automatically). Found automatically "
                          "in ~/.switch or beside the app. Packing an already-decrypted dump into NUP needs no keys."))
        keys.add(self.sw_prod_keys)
        keys.add(self.sw_title_keys)
        keys.add(self.sw_keys_status)
        lay.addWidget(keys)
        lay.addStretch(1)

        # -- Downloads
        lay, _ = page("Downloads")
        lay.addWidget(hint("How files, torrents and video links are downloaded: speed, schedule, quality and magnet links."))
        card = Card("Downloading")
        row = QHBoxLayout()
        row.setSpacing(12)
        dl_label = QLabel("Parallel downloads")
        dl_label.setFixedSize(LABEL_W, 22)  # as tall as the switch's track, so the text lines up with it
        row.addWidget(dl_label, 0, Qt.AlignmentFlag.AlignTop)
        # files at once: 1, 2, 4, 8 ... up to the limit. Five fixed-width stops plus the label leave no room beside
        # the switch, so its description sits underneath, centred on the switch
        self.dl_threads = StageSwitch(describe="below")
        notes = {1: "One file at a time: gentlest on the server.", 2: "Two files at once.",
                 4: "Four files at once: a good balance.", 8: "Eight files at once: fast on a good connection."}
        for n in sorted({n for n in (1, 2, 4, 8, DL_MAX_PARALLEL) if n <= DL_MAX_PARALLEL}):
            self.dl_threads.addItem(str(n), n, notes.get(n, f"{n} files at once: the most allowed; some sites limit this."))
        self.dl_threads.setValue(4)
        self.dl_threads.valueChanged.connect(self._dl_threads_changed)
        row.addWidget(self.dl_threads, 1, Qt.AlignmentFlag.AlignTop)
        card.add(row)
        self.dl_verify = AnimCheckBox("Verify checksums")
        self.dl_verify.setChecked(True)
        self.dl_folders = AnimCheckBox("Folder per item")
        self.dl_clip = AnimCheckBox("Watch clipboard for links")
        self.dl_clip.setToolTip("Copy a magnet, an archive.org / Mega / Drive / MediaFire link, a video link (YouTube, "
                                "Vimeo ...) or a link to a file and it lands in the link box on the Download page. "
                                "Ordinary web pages are ignored.")
        self._dl_clip_last = ""
        QApplication.clipboard().dataChanged.connect(self._dl_clipboard_changed)
        for box in (self.dl_verify, self.dl_folders, self.dl_clip):
            box.toggled.connect(lambda *_: self._dl_save_soon())
            card.add(box)
        lay.addWidget(card)
        self.rules_panel = RulesPanel()
        lay.addWidget(self.rules_panel)
        lay.addWidget(YtdlpCard())

        if sys.platform == "win32":
            card = Card("Magnet Links")
            card.add(hint("Let magnet links from your browser and other programs open in Distill's Files & Torrents page. "
                          "This is per-user and needs no administrator rights."))
            self.magnet_box = AnimCheckBox("Open magnet links with Distill")
            self.magnet_box.setChecked(magnet.is_registered())
            self.magnet_box.toggled.connect(self._magnet_toggled)
            card.add(self.magnet_box)
            self.magnet_status = hint("", live=True)
            card.add(self.magnet_status)
            b = QPushButton("Open Windows default apps...")
            b.clicked.connect(lambda _c=False: magnet.open_default_apps())
            h = QHBoxLayout()
            h.addWidget(b)
            h.addStretch(1)
            card.add(h)
            lay.addWidget(card)
            self._magnet_refresh()

        card = Card("Torrent Privacy")  # was a popup: ties torrent traffic to your VPN adapter
        self.torrent_panel = TorrentDialog(self, embedded=True)
        card.add(self.torrent_panel)
        lay.addWidget(card)
        lay.addStretch(1)

        # -- Accounts: the sign-in details that used to be a popup
        lay, _ = page("Accounts")
        lay.addWidget(hint("Sign-in details for archive.org, 1fichier, TorBox and any other file host.",
                           tip="With a TorBox API key, torrents are looked up in TorBox's cache first: cached files "
                               "download straight from TorBox over HTTPS, and only what isn't cached falls back to "
                               "torrenting."))
        self.logins_panel = LoginsDialog(self, [], self.dl_remember, embedded=True)  # one card per site, drawn by the panel
        self.logins_panel.saved.connect(self._logins_saved)
        lay.addWidget(self.logins_panel)
        lay.addStretch(1)

        # -- General: app-wide behaviour
        lay, _ = page("General")
        lay.addWidget(hint("App-wide behaviour: animations and notifications."))
        card = Card("Run Feedback")
        self.reduce_motion = AnimCheckBox("Reduce motion (turn off all animations)")
        self.reduce_motion.setToolTip("Stops the Start button's drifting gradient, folding and sliding, checkbox bounces, "
                                      "blinking status dots and the seek bar's wave. The Start button still glows while "
                                      "a batch runs.")
        self.reduce_motion.toggled.connect(self._set_reduce_motion)
        card.add(self.reduce_motion)
        self.notify_check = AnimCheckBox("Notify me when a long run ends while this window is in the background")
        self.notify_check.setChecked(True)
        self.notify_check.setToolTip("Flashes the taskbar button and shows a Windows notification. Runs under "
                                     "20 seconds never notify.")
        card.add(self.notify_check)
        lay.addWidget(card)

        lay.addStretch(1)
        nav.setCurrentRow(0)

    def _magnet_refresh(self) -> None:
        if not magnet.is_registered():
            self.magnet_status.setText("")
        elif magnet.is_default():
            self.magnet_status.setText("Magnet links open in Distill.")
        else:
            self.magnet_status.setText("Registered, but Windows still uses another app for magnet links. Open Default "
                                       "apps below and choose Distill for MAGNET.")

    def _magnet_toggled(self, on: bool) -> None:
        try:
            (magnet.register if on else magnet.unregister)()
        except OSError as exc:
            self.magnet_box.blockSignals(True)
            self.magnet_box.setChecked(not on)
            self.magnet_box.blockSignals(False)
            self._error("Magnet Links", f"Couldn't {'register' if on else 'remove'} the magnet handler: {exc}")
        self._magnet_refresh()

    def _register_tool_row(self, key: str, row: ToolRow) -> None:
        row.path.setText(self._auto[key])
        row.status.setText(f"Checking for {TOOLS[key][0]}...")
        if existing := self._tool_rows[key]:  # a second page showing the same tool starts in step with the first
            row.path.setText(existing[0].path.text())
            row.status.setText(existing[0].status.text())
        self._tool_rows[key].append(row)
        row.path.edit.textEdited.connect(lambda text, k=key: self._set_tool_text(k, text, user=True))
        row.path.edit.textChanged.connect(lambda *_: self._refresh_nav_dots())
        if key == "ytdlp":
            row.path.edit.textChanged.connect(self._ytdlp_path_changed)

    def _ytdlp_path_changed(self, text: str) -> None:
        """A yt-dlp the person picked (or typed) is the one downloads use; the app's own copy needs no setting."""
        text = text.strip()
        picked = "" if not text or text == self._auto["ytdlp"] else resolve_tool_path(text)
        if picked != ytdlp.SETTINGS.exe and (not picked or Path(picked).is_file()):
            ytdlp.SETTINGS.exe = picked
            ytdlp.save_settings()

    def _set_tool_text(self, key: str, text: str, user: bool = False) -> None:
        for row in self._tool_rows[key]:
            if row.path.edit.text() != text:
                row.path.setText(text)

    def _set_tool_status(self, key: str, text: str) -> None:
        for row in self._tool_rows[key]:
            row.status.setText(text)

    def _tool_path_found(self, key: str, path: str) -> None:
        if self._tool_text(key) in ("", self._auto[key]):  # never overwrite a manual choice
            self._set_tool_text(key, path)
        self._auto[key] = path
        if key == "ps3qdd":
            keys = Path(resolve_tool_path(path)).parent / "Keys"
            if keys.is_dir() and self.ps3_keys.text().strip() in ("", self._auto_ps3_keys):
                self._auto_ps3_keys = portable_path(keys)
                self.ps3_keys.setText(self._auto_ps3_keys)

    def _tool_ready(self, key: str) -> None:
        self._pending_tools.discard(key)
        self._pending_dl.discard(key)
        self._refresh_nav_dots()
        self._refresh_markers()
        if not self._pending_tools and not self.is_processing and not self.org_busy:
            self.start_btn.setEnabled(True)

    def _missing_tool(self, path: str, text: str) -> bool:
        """True (after telling the person) when `path` isn't a runnable tool."""
        if path and shutil.which(path):
            return False
        self._tool_missing(text)
        return True

    def _resolved_tools(self) -> dict[str, str]:
        """Every tool path as the engine should run it (./tools/... is anchored to the app folder)."""
        tools = {key: resolve_tool_path(self._clean(self._tool_text(key))) for key in TOOLS}
        tools["dolphin"] = resolve_dolphintool(self._clean(self._tool_text("dolphin")))  # also takes Dolphin.exe / its folder
        return tools

    def _tools_ok(self, mode: str, engine_mode: str, tools: dict[str, str], *, pkg_tool: str, relink: bool,
                  packs_zar: bool, ps3_keys: str) -> bool:
        """Check that the tools this page needs exist; on a miss, say so and open Settings."""
        if mode == "rvz":
            if not tools["dolphin"]:
                self._tool_missing(TOOL_MISSING_TEXT["rvz"])
                return False
        elif mode in ("chd", "cso", "xbox", "ps3"):
            key = {"chd": "chdman", "cso": "maxcso", "xbox": "xdvdfs", "ps3": "ps3qdd"}[mode]
            if self._missing_tool(tools[key], TOOL_MISSING_TEXT[mode]):
                return False
        if mode == "ps3" and ps3_keys and not Path(ps3_keys).is_dir():
            self._error("Error", f"The PS3 keys folder does not exist.\n\nPath checked: {ps3_keys}")
            return False
        if engine_mode == "pkg" and self._missing_tool(
                tools[pkg_tool], f"Could not locate the '{PKG_TOOLS[pkg_tool]}' executable.\n\n{PKG_MISSING_HINT[pkg_tool]}"):
            return False
        if relink and self._missing_tool(tools["relinker"], TOOL_MISSING_TEXT["relinker"]):
            return False
        return not (packs_zar and self._missing_tool(tools["zarchive"], TOOL_MISSING_TEXT["zar"]))
