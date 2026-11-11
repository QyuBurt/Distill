"""Dialogs: logins, torrent privacy, end-of-run summary."""
from __future__ import annotations

import queue
import threading
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
    QTreeWidgetItem, QWidget,
)

from .. import cookies as cookiemod, debrid
from .. import downloads as _dl
from ..downloads import CUSTOM_KINDS, LinkError, login_for, LOGIN_SITES, LOGINS, save_logins, SKIPPED_LOGINS
from ..models import BatchSummary, fmt_size, STAGE_TITLES
from ..torbox import account_remote_files, AccountFile, AccountItem, torbox_account
from ..torrents import list_adapters, load_torrent_settings, save_torrent_settings
from . import msgbox
from .frameless import FramelessDialog
from .state import LABEL_W
from .theme import T
from .widgets import (accent_button, AnimCheckBox, Card, fit_columns, hint, PlaceholderTree)


# --- Dialogs ------------------------------------------------------------------------------------
def _button_row(*buttons: QPushButton, left: bool = False) -> QHBoxLayout:
    """Buttons along the bottom: to the right in a pop-up, to the left inside Settings (like its other buttons)."""
    row = QHBoxLayout()
    if not left:
        row.addStretch(1)
    for b in buttons:
        row.addWidget(b)
    if left:
        row.addStretch(1)
    return row


def _embed(panel: QDialog) -> None:
    """A panel that sits inside the Settings window: no window of its own, and no page-coloured backdrop (the
    theme gives every QDialog one, which showed as a darker block inside the card)."""
    panel.setWindowFlags(Qt.WindowType.Widget)
    panel.setObjectName("embeddedPanel")


class LoginsDialog(FramelessDialog):
    """Sign-in details for archive.org, 1fichier and any other host. `needed` = sites in this job that have no
    login yet: it then also offers 'Continue without logging in'. `ok` is False only when the person cancels."""

    saved = Signal()  # embedded in Settings: the details were saved (nothing closes)

    def __init__(self, parent: QWidget, needed: list[str] | None, remember: bool, embedded: bool = False):
        super().__init__(parent, "Logins", "link", None, (460, 0), chrome=not embedded)
        self._embedded = embedded  # True when it sits inside the Settings window as the Accounts tab
        if embedded:
            _embed(self)
        else:
            self.setModal(True)
        self.needed = needed or []
        self.ok = False
        self.remember_value = remember
        self._timer = QTimer(self)  # inside Settings every edit saves itself a moment after the last one
        self._timer.setSingleShot(True)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._save)
        lay = self.body((0, 0, 0, 0) if embedded else (20, 18, 20, 16))
        lay.setSpacing(10)
        if needed:
            names = " and ".join(LOGIN_SITES[k][0] for k in needed)
            intro = QLabel(f"This job includes {names} files. Sign in below if they need an account, "
                           "or continue without logging in.")
            intro.setWordWrap(True)
            lay.addWidget(intro)
        if _dl.LOGIN_PROBLEM and not embedded:  # inside Settings the API keys page says it once, at the top
            warn = hint(f"Saved logins couldn't be read: {_dl.LOGIN_PROBLEM}. Enter them again to replace them.")
            lay.addWidget(warn)
        self.fields: dict[tuple[str, str], QLineEdit] = {}

        def field_rows(grid: QGridLayout, key: str, first_row: int, label_text: str | None = None) -> None:
            _name, _hosts, fields, where, _req = LOGIN_SITES[key]
            for offset, (fld, label, hidden) in enumerate(fields):
                edit = QLineEdit((LOGINS.get(key) or {}).get(fld, ""))
                if hidden:
                    edit.setEchoMode(QLineEdit.EchoMode.Password)
                edit.setMinimumWidth(180 if embedded else 300)  # Settings is narrower than a pop-up
                if embedded:
                    edit.textChanged.connect(lambda *_: self._timer.start())
                self.fields[(key, fld)] = edit
                name_label = QLabel(label_text if label_text and offset == 0 else label)
                name_label.setFixedWidth(LABEL_W)  # the same label column as the other settings forms
                grid.addWidget(name_label, first_row + offset, 0)
                grid.addWidget(edit, first_row + offset, 1)
            get_key = QPushButton("Get key...")
            get_key.clicked.connect(lambda _checked=False, u=where: webbrowser.open(u))
            grid.addWidget(get_key, first_row, 2)

        def new_grid() -> QGridLayout:
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setColumnStretch(1, 1)
            return grid

        # TorBox and the other debrid / premium-link services share one card (and one explanation)
        debrid_keys = [k for k in LOGIN_SITES if k == "torbox" or (k in debrid.PROVIDERS and debrid.PROVIDERS[k].links)]
        for key, (name, _hosts, _fields, _where, required) in LOGIN_SITES.items():
            if key in debrid_keys:
                continue
            box = Card(name + (" (needed for these links)" if required else " (optional)"))
            if key == "prowlarr":
                box.add(hint("Adds many more torrent sites to Search torrents (Downloads page), which already works "
                             "without it. Address example: http://localhost:9696 - the key is under Prowlarr's "
                             "Settings > General."))
            grid = new_grid()
            field_rows(grid, key, 0)
            box.add(grid)
            lay.addWidget(box)
        if debrid_keys:
            box = Card("Debrid & Premium Link Services (optional)")
            box.add(hint("Add a key for any of these. Links from sites a service supports are unlocked into fast direct "
                         "links; other links download normally and are never sent to it. TorBox also serves cached "
                         "torrents (anything not cached uses the swarm)."))
            grid = new_grid()
            for row, key in enumerate(debrid_keys):
                field_rows(grid, key, row, LOGIN_SITES[key][0])
            box.add(grid)
            lay.addWidget(box)

        other = Card("Other Sites (Any File Host)")
        other.add(hint("As many as you need. A login for example.com also covers its sub-domains; the most specific "
                       "one is used. Cookie logins can be imported from your browser."))
        self._custom_grid = QGridLayout()
        self._custom_grid.setHorizontalSpacing(8)
        for col, head in enumerate(("Host", "Type", "User", "Password / token / cookie")):
            column_head = QLabel(head)
            column_head.setObjectName("pathLabel")  # the small muted label the folder rows use
            self._custom_grid.addWidget(column_head, 0, col)
        self._custom_grid.setColumnStretch(0, 3)
        self._custom_grid.setColumnStretch(3, 3)
        other.add(self._custom_grid)
        self.custom: list[tuple[QLineEdit, QComboBox, QLineEdit, QLineEdit, QPushButton]] = []
        self._set_custom_rows(LOGINS.get("custom", []))
        add_row = QPushButton("Add another")
        add_row.clicked.connect(lambda _c=False: self._add_custom_row({}))
        import_btn = QPushButton("Import cookies...")
        menu = QMenu(import_btn)
        menu.addAction("From a cookies.txt file...", self._import_cookie_file)
        menu.addAction("From Firefox...", self._import_firefox)
        import_btn.setMenu(menu)
        import_btn.setToolTip("Sign in with your browser, then bring the session over so protected downloads work. "
                              "Chrome, Edge and Brave lock their cookies: export a cookies.txt with a browser extension "
                              "instead.")
        form_btn = QPushButton("Sign in with a web form...")
        form_btn.clicked.connect(self._web_login)
        tools = QHBoxLayout()
        for b_ in (add_row, import_btn, form_btn):
            tools.addWidget(b_)
        tools.addStretch(1)
        other.add(tools)
        lay.addWidget(other)

        from ..secretstore import describe  # noqa: PLC0415
        self.remember = AnimCheckBox(f"Remember on this computer ({describe()})")
        self.remember.setChecked(remember)
        lay.addWidget(self.remember)

        if embedded:  # nothing to press: edits are saved as they are made
            self.remember.toggled.connect(lambda *_: self._timer.start(0))
            return
        save = accent_button("Save and continue" if needed else "Save")
        save.setMinimumHeight(32)
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons = [save]
        if needed:
            skip = QPushButton("Continue without logging in")
            skip.clicked.connect(self._skip)
            buttons.append(skip)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.append(cancel)
        lay.addLayout(_button_row(*buttons))

    def _changed(self) -> None:
        if self._embedded:
            self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self._embedded and self._timer.isActive():  # closing Settings right after typing must not lose it
            self._timer.stop()
            self._save()

    # ---- the open-ended list of other-site logins ----------------------------------------------------
    def _set_custom_rows(self, entries: list[dict]) -> None:
        for widgets in self.custom:
            for w in widgets:
                self._custom_grid.removeWidget(w)
                w.deleteLater()
        self.custom = []
        for entry in entries:
            self._add_custom_row(entry)
        if not entries:
            self._add_custom_row({})

    def _add_custom_row(self, cur: dict) -> None:
        row = len(self.custom) + 1
        host, user, secret = QLineEdit(cur.get("host", "")), QLineEdit(cur.get("user", "")), QLineEdit(cur.get("secret", ""))
        secret.setEchoMode(QLineEdit.EchoMode.Password)
        kind = QComboBox()
        kind.addItems(CUSTOM_KINDS)
        if cur.get("kind") in CUSTOM_KINDS:
            kind.setCurrentText(cur["kind"])
        gone = QPushButton("Remove")
        widgets = (host, kind, user, secret, gone)
        if self._embedded:
            for sig in (host.textChanged, user.textChanged, secret.textChanged, kind.currentIndexChanged):
                sig.connect(lambda *_: self._timer.start())
        gone.clicked.connect(lambda _c=False, w=widgets: self._remove_custom_row(w))
        for col, w in enumerate(widgets):
            self._custom_grid.addWidget(w, row, col)
        self.custom.append(widgets)

    def _remove_custom_row(self, widgets: tuple) -> None:
        entries = [e for e in self._collect_custom(keep_blank=True) if e is not None]
        index = self.custom.index(widgets)
        del entries[index]
        self._set_custom_rows([e for e in entries if e["host"] or e["secret"]])
        self._changed()

    def _collect_custom(self, keep_blank: bool = False) -> list[dict]:
        rows = []
        for host_e, kind_e, user_e, secret_e, _gone in self.custom:
            host = host_e.text().strip().lower()
            host = (urllib.parse.urlsplit(host).hostname or host) if "://" in host else host.strip("/")
            entry = {"host": host, "kind": kind_e.currentText(), "user": user_e.text().strip(),
                     "secret": secret_e.text().strip()}
            if keep_blank or (host and entry["secret"]):
                rows.append(entry)
        return rows

    def _merge_custom(self, new: list[dict]) -> None:
        self._set_custom_rows(cookiemod.merge_logins(self._collect_custom(), new))
        self._changed()

    # ---- cookies and web-form sign-in ----------------------------------------------------------------------
    def _import_cookie_file(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, "Choose a cookies.txt File", "", "Cookie files (*.txt);;All files (*)")
        if path:
            try:
                found = cookiemod.parse_netscape(Path(path).read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                msgbox.warning(self, "Import cookies", f"Can't read that file: {exc}")
                return
            self._pick_cookies(found, "That file has no usable cookies (is it a Netscape-format cookies.txt?).")

    def _import_firefox(self) -> None:
        profiles = cookiemod.firefox_profiles()
        if not profiles:
            msgbox.information(self, "Import cookies", "No Firefox profile was found on this computer.")
            return
        try:
            found = cookiemod.read_firefox(profiles[0])
        except (OSError, ValueError) as exc:
            msgbox.warning(self, "Import cookies", str(exc))
            return
        self._pick_cookies(found, "Firefox has no cookies stored.")

    def _pick_cookies(self, found: list, empty_message: str) -> None:
        groups = cookiemod.group_by_domain(found)
        if not groups:
            msgbox.information(self, "Import cookies", empty_message)
            return
        dlg = CookiePickDialog(self, groups)
        if dlg.exec() and dlg.chosen:
            self._merge_custom(cookiemod.to_logins(groups, dlg.chosen))

    def _web_login(self) -> None:
        dlg = WebLoginDialog(self)
        if dlg.exec() and dlg.result_entry:
            self._merge_custom([dlg.result_entry])

    def _save(self) -> None:
        for key, (_n, _h, fields, _w, _r) in LOGIN_SITES.items():
            vals = {f: self.fields[(key, f)].text().strip() for f, _l, _hid in fields}
            if any(vals.values()):
                LOGINS[key] = vals
            else:
                LOGINS.pop(key, None)
        rows = self._collect_custom()
        LOGINS["custom"] = sorted(rows, key=lambda e: -len(e["host"]))  # most specific site is looked up first
        self.remember_value = self.remember.isChecked()
        save_logins(self.remember_value)
        SKIPPED_LOGINS.update(k for k in self.needed if login_for(f"https://{LOGIN_SITES[k][1][0]}/") is None)
        self.ok = True
        if self._embedded:
            self.saved.emit()
        else:
            self.accept()

    def _skip(self) -> None:
        SKIPPED_LOGINS.update(self.needed)
        self.ok = True
        self.accept()


class CookiePickDialog(FramelessDialog):
    """Choose which sites' cookies to bring in: a browser holds hundreds of tracking domains you don't want."""

    def __init__(self, parent: QWidget, groups: dict):
        super().__init__(parent, "Import Cookies", "file", (460, 480), (380, 360))
        self.setModal(True)
        self.chosen: set[str] = set()
        lay = self.body()
        lay.addWidget(hint("Tick the sites you want to stay signed in to. Only those are kept."))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter sites")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)
        self.list = QListWidget()
        for domain, cookies in groups.items():
            item = QListWidgetItem(f"{domain}   ({len(cookies)} cookie{'s' if len(cookies) != 1 else ''})")
            item.setData(Qt.ItemDataRole.UserRole, domain)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        lay.addWidget(self.list, 1)
        shown = QPushButton("Tick shown")
        shown.clicked.connect(lambda _c=False: self._tick_shown(True))
        none = QPushButton("Untick all")
        none.clicked.connect(lambda _c=False: self._tick_all(False))
        ok = accent_button("Import")
        ok.clicked.connect(self._accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        lay.addLayout(_button_row(shown, none, ok, cancel))

    def _filter(self, text: str) -> None:
        text = text.strip().lower()
        for i in range(self.list.count()):
            self.list.item(i).setHidden(bool(text) and text not in self.list.item(i).data(Qt.ItemDataRole.UserRole))

    def _tick_shown(self, on: bool) -> None:
        for i in range(self.list.count()):
            if not self.list.item(i).isHidden():
                self.list.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _tick_all(self, on: bool) -> None:
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _accept(self) -> None:
        self.chosen = {self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                       if self.list.item(i).checkState() == Qt.CheckState.Checked}
        self.accept()


class WebLoginDialog(FramelessDialog):
    """Sign in through a site's plain login form and keep the session cookies as a login for that site."""

    done = Signal(object, str)  # (host, cookie header) on success, '' + message on failure

    def __init__(self, parent: QWidget):
        super().__init__(parent, "Sign In With a Web Form", "link", None, (520, 0))
        self.setModal(True)
        self.result_entry: dict | None = None
        lay = self.body()
        lay.addWidget(hint("Works for sites with an ordinary username-and-password form. Sites with captchas, "
                           "one-time codes or JavaScript logins won't: sign in with your browser and import its "
                           "cookies instead."))
        grid = QGridLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.com/login")
        self.user_field, self.pass_field = QLineEdit("username"), QLineEdit("password")
        self.user, self.password = QLineEdit(), QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.extra = QPlainTextEdit()
        self.extra.setPlaceholderText("Other form fields, one per line:  name=value")
        self.extra.setFixedHeight(60)
        for row, (label, widget) in enumerate((("Login address", self.url), ("Username field", self.user_field),
                                               ("Password field", self.pass_field), ("Username", self.user),
                                               ("Password", self.password), ("Extra fields", self.extra))):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self.status = hint("", live=True)
        lay.addWidget(self.status)
        self.go = accent_button("Sign in")
        self.go.clicked.connect(self._go)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        lay.addLayout(_button_row(self.go, cancel))
        self.done.connect(self._finished)

    def _go(self) -> None:
        fields = {self.user_field.text().strip() or "username": self.user.text(),
                  self.pass_field.text().strip() or "password": self.password.text()}
        for line in self.extra.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k.strip()] = v.strip()
        url = self.url.text().strip()
        self.go.setEnabled(False)
        self.status.setText("Signing in...")

        def work() -> None:
            try:
                host, header = cookiemod.form_login(url, fields)
                self.done.emit(host, header)
            except ValueError as exc:
                self.done.emit(None, str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _finished(self, host, text: str) -> None:
        self.go.setEnabled(True)
        if host is None:
            self.status.setText(f"Couldn't sign in: {text}")
            return
        self.result_entry = {"host": host, "kind": "Cookie", "user": "", "secret": text}
        self.accept()


class TorrentDialog(FramelessDialog):
    """Pick the VPN adapter torrent traffic is bound to."""

    saved = Signal()

    def __init__(self, parent: QWidget, embedded: bool = False):
        super().__init__(parent, "Torrent Privacy", "shield-check", None, (520, 0), chrome=not embedded)
        self._embedded = embedded  # True when it sits inside the Settings window (Downloads tab)
        self._first_show = True
        if embedded:
            _embed(self)
        else:
            self.setModal(True)
        self.ok = False
        self.cfg = load_torrent_settings()
        self.adapters: list[dict] = []
        self._scanning = False
        self._box: queue.Queue = queue.Queue(maxsize=1)
        lay = self.body((0, 0, 0, 0) if embedded else (20, 18, 20, 16))
        lay.setSpacing(10)
        intro_text = ("Everyone in a torrent swarm can see the IP address you connect from. To keep yours private, "
                      "torrent traffic is tied to your VPN's network adapter: if the VPN drops, torrenting stops "
                      "instead of falling back to your normal connection.")
        if embedded:
            lay.addWidget(hint(intro_text))  # the muted note style the other settings cards use
        else:
            intro = QLabel(intro_text)
            intro.setWordWrap(True)
            intro.setMinimumWidth(520)
            lay.addWidget(intro)
        row = QHBoxLayout()
        row.setSpacing(12)
        adapter_label = QLabel("VPN adapter" if embedded else "VPN adapter:")
        if embedded:
            adapter_label.setFixedWidth(LABEL_W)  # lines up with the labels of the other settings
        row.addWidget(adapter_label)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(200 if embedded else 380)
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.combo, 1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self.refresh_btn)
        lay.addLayout(row)
        lay.addWidget(hint("Connect your VPN first, then press Refresh and pick its adapter (usually marked [VPN])."))
        self.require = AnimCheckBox("Only allow torrents through this adapter (strongly recommended)")
        self.require.setChecked(bool(self.cfg["require_vpn"]))
        self.dht = AnimCheckBox("Use DHT to find peers (needed for magnet links without trackers)")
        self.dht.setChecked(bool(self.cfg["dht"]))
        lay.addWidget(self.require)
        lay.addWidget(self.dht)
        lay.addWidget(hint('Always on: anonymous mode, no UPnP or local discovery, IPv4 on the VPN only, and no seeding.', tip='Always on: anonymous mode (no client name, no address sent to trackers), UPnP / NAT-PMP / local-network discovery off, IPv4 on the VPN only, and no seeding - each torrent is left as soon as your files are done.'))
        self._note_label = QLabel("")  # embedded: what the dialogs below say in the pop-up, shown in place
        self._note_label.setObjectName("notice")
        self._note_label.setWordWrap(True)
        self._note_label.setVisible(False)
        lay.addWidget(self._note_label)
        if embedded:  # nothing to press: every change is saved as it is made
            self.combo.currentIndexChanged.connect(lambda *_: self._autosave())
            self.require.toggled.connect(lambda *_: self._autosave())
            self.dht.toggled.connect(lambda *_: self._autosave())
        else:
            save = accent_button("Save")
            save.setMinimumHeight(32)
            save.setDefault(True)
            save.clicked.connect(self._save)
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            lay.addLayout(_button_row(save, cancel))
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll)
        if not embedded:
            QTimer.singleShot(0, self._refresh)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._embedded and self._first_show:  # scanning for adapters runs PowerShell: only when first looked at
            self._first_show = False
            QTimer.singleShot(0, self._refresh)

    def _refresh(self) -> None:
        """Listing adapters starts PowerShell (a second or two), so it runs off the UI thread."""
        if self._scanning:
            return
        self._scanning = True
        self.refresh_btn.setEnabled(False)
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("Looking for network adapters...")
        self.combo.blockSignals(False)
        threading.Thread(target=lambda: self._box.put(list_adapters()), daemon=True).start()
        self._poll_timer.start()

    def _poll(self) -> None:
        try:
            found = self._box.get_nowait()
        except queue.Empty:
            return
        self._poll_timer.stop()
        self._scanning = False
        self.refresh_btn.setEnabled(True)
        self.adapters = sorted(found, key=lambda a: (not a["vpn"], a["name"].lower()))
        self.combo.blockSignals(True)  # filling the list is not the person choosing an adapter
        self.combo.clear()
        for a in self.adapters:
            self.combo.addItem(f'{"[VPN]  " if a["vpn"] else ""}{a["name"]}  -  {a["ip"]}')
        idx = next((i for i, a in enumerate(self.adapters) if a["name"] == self.cfg["adapter"]),
                   0 if self.adapters and self.adapters[0]["vpn"] else -1)
        self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(False)

    def _autosave(self) -> None:
        """Settings page: save without asking. What the pop-up asks in message boxes is shown in place instead."""
        if self._scanning:
            return
        idx = self.combo.currentIndex()
        chosen = self.adapters[idx] if 0 <= idx < len(self.adapters) else None
        if self.require.isChecked():
            if not chosen:
                self._note("Connect your VPN, press Refresh and pick its adapter. Nothing is saved until you do.")
                return
            note = "" if chosen["vpn"] else (f"'{chosen['name']}' doesn't look like a VPN adapter. If it's your normal "
                                             "connection, peers will see your real IP address.")
        else:
            note = "Without the VPN requirement, peers in the swarm can see your real IP address."
        save_torrent_settings({"adapter": chosen["name"] if chosen else "", "require_vpn": self.require.isChecked(),
                               "dht": self.dht.isChecked()})
        self._note(note)
        self.saved.emit()

    def _note(self, text: str) -> None:
        self._note_label.setText(text)
        self._note_label.setVisible(bool(text))

    def _save(self) -> None:
        idx = self.combo.currentIndex() if not self._scanning else -1
        chosen = self.adapters[idx] if 0 <= idx < len(self.adapters) else None
        yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        if self.require.isChecked():
            if not chosen:
                msgbox.critical(self, "Choose an Adapter", "Connect your VPN, press Refresh and pick its adapter.")
                return
            if not chosen["vpn"] and msgbox.warning(
                    self, "Not a VPN?", f"'{chosen['name']}' doesn't look like a VPN adapter. If it's your normal "
                    "connection, peers will see your real IP address.\n\nUse it anyway?", yes | no, no) != yes:
                return
        elif msgbox.warning(
                self, "No VPN Protection", "Without the VPN requirement, peers in the swarm can see your real IP "
                "address.\n\nContinue without it?", yes | no, no) != yes:
            return
        save_torrent_settings({"adapter": chosen["name"] if chosen else "", "require_vpn": self.require.isChecked(),
                               "dht": self.dht.isChecked()})
        self.ok = True
        if self._embedded:
            self.saved.emit()
        else:
            self.accept()


class SummaryDialog(FramelessDialog):
    """End-of-batch report when something failed: what went wrong, per stage, with a one-click retry."""

    def __init__(self, parent: QWidget, summary: BatchSummary, report: str, on_retry: Callable[[frozenset], None]):
        super().__init__(parent, "Batch Complete - Some Items Failed", "alert", (700, 540), (520, 400))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        failures = summary.failures
        failed_sources = {f.source for f in failures}
        retry_paths = frozenset(f.source for f in failures if f.retryable)
        lay = self.body((20, 18, 20, 16))
        head = QLabel(f"Batch complete: {summary.total - len(failed_sources)} of {summary.total} item(s) succeeded, "
                      f"{len(failed_sources)} failed.")
        head.setObjectName("phase")
        head.setWordWrap(True)
        lay.addWidget(head)
        note = QLabel("Your original files were NOT deleted for any item that failed - everything that errored is "
                      "still exactly where it was.")
        note.setWordWrap(True)
        lay.addWidget(note)
        if report:
            rep = QLabel(report)
            rep.setObjectName("mono")
            rep.setWordWrap(True)
            rep.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(rep)
        card = Card("What Went Wrong")
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setObjectName("log")
        card.add(details)
        lay.addWidget(card, 1)
        head_fmt, why_fmt = QTextCharFormat(), QTextCharFormat()
        head_fmt.setFontWeight(700)
        head_fmt.setForeground(T.accent)
        why_fmt.setForeground(T.fg3)
        plain = QTextCharFormat()
        cur = details.textCursor()
        for stage, title in STAGE_TITLES.items():
            items = [f for f in failures if f.stage == stage]
            if not items:
                continue
            cur.insertText(f"{title} ({len(items)})\n", head_fmt)
            for f in items:
                cur.insertText(f"  \u2022 {f.name}\n", plain)
                extra = "" if f.retryable else "  [retrying won't help - change a setting first]"
                cur.insertText(f"      {f.reason}{extra}\n", why_fmt)
        details.moveCursor(QTextCursor.MoveOperation.Start)
        retry = accent_button(f"Retry Failed ({len(retry_paths)})")
        retry.setMinimumHeight(32)
        retry.setEnabled(bool(retry_paths))
        retry.clicked.connect(lambda: (self.close(), on_retry(retry_paths)))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        lay.addLayout(_button_row(retry, close))


class TorBoxDownloadsDialog(FramelessDialog):
    """What is on the person's TorBox account right now: torrents, usenet and web downloads, each opening to its files.
    Tick what you want and add it to the Files & Torrents list; it then downloads from TorBox over HTTPS. Read-only
    on the account itself: nothing is changed or deleted there."""

    loaded = Signal(int, object, object, str)  # (token, items, notes, error): sent from the worker thread
    addFiles = Signal(object)  # list[RemoteFile] to put in the file list
    DASHBOARD = "https://torbox.app/dashboard"
    MAX_FILES = 300  # files listed under one download; a season pack can hold more than anyone wants to scroll
    _DATA = Qt.ItemDataRole.UserRole

    def __init__(self, parent: QWidget):
        super().__init__(parent, "TorBox Downloads", "download", (820, 580), (600, 420))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._items: list[AccountItem] = []
        self._notes: list[str] = []
        self._ticked: dict[tuple[str, int], set[int]] = {}  # (kind, id) -> ids of its ticked files; survives the filter
        self._token = 0
        self._busy = False  # set while ticks are being copied between a row and its files
        lay = self.body((20, 18, 20, 16))
        self.head = QLabel("TorBox downloads")
        self.head.setObjectName("phase")
        lay.addWidget(self.head)
        bar = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setClearButtonEnabled(True)
        self.filter.setPlaceholderText("Filter by name")
        self.filter.textChanged.connect(self._fill)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        site = QPushButton("Open TorBox")
        site.clicked.connect(lambda: webbrowser.open(self.DASHBOARD))
        bar.addWidget(self.filter, 1)
        bar.addWidget(self.refresh_btn)
        bar.addWidget(site)
        lay.addLayout(bar)
        card = Card()
        self.tree = PlaceholderTree()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Name", "Type", "Size", "Status", "Added"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 100)
        self.tree.setColumnWidth(3, 150)
        self.tree.setColumnWidth(4, 100)
        fit_columns(self.tree, flex=0)
        self.tree.itemChanged.connect(self._item_changed)
        card.add(self.tree)
        lay.addWidget(card, 1)
        self.status = hint("", live=True)
        lay.addWidget(self.status)
        self.select_all = QPushButton("Select all ready")
        self.select_all.clicked.connect(lambda: self._tick_all(True))
        self.clear_all = QPushButton("Clear")
        self.clear_all.clicked.connect(lambda: self._tick_all(False))
        self.add_btn = accent_button("Add to file list")
        self.add_btn.setMinimumHeight(32)
        self.add_btn.clicked.connect(self._add)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addWidget(self.select_all)
        row.addWidget(self.clear_all)
        row.addStretch(1)
        row.addWidget(self.add_btn)
        row.addWidget(close)
        lay.addLayout(row)
        self.loaded.connect(self._loaded)
        self._update_buttons()
        self.refresh()

    # ---- reading the account -------------------------------------------------------------------------------
    def refresh(self) -> None:
        self._token += 1
        token = self._token
        self.refresh_btn.setEnabled(False)
        self.tree.clear()
        self.tree.placeholder = "Asking TorBox..."
        self.status.setText("Fetching your TorBox downloads...")

        def work() -> None:
            try:
                items, notes = torbox_account()
                error = ""
            except LinkError as exc:
                items, notes, error = [], [], str(exc)
            except Exception as exc:  # never leave the dialog stuck on 'Asking TorBox'
                items, notes, error = [], [], f"{type(exc).__name__}: {exc}"
            try:
                self.loaded.emit(token, items, notes, error)
            except RuntimeError:  # the dialog was closed while TorBox was answering
                pass
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, token: int, items, notes, error: str) -> None:
        if token != self._token:  # an older request answering late
            return
        self.refresh_btn.setEnabled(True)
        self._items, self._notes = list(items), list(notes)
        still = {(i.kind, i.id): {f.id for f in i.files} for i in self._items}  # keep ticks for what is still there
        self._ticked = {k: ids & still[k] for k, ids in self._ticked.items() if k in still and ids & still[k]}
        if error:
            self.tree.clear()
            self.tree.placeholder = "Couldn't read your account."
            self.status.setText(error)
            self._update_buttons()
            return
        self.tree.placeholder = "Nothing on your TorBox account."
        self._fill()

    # ---- the list and its ticks ----------------------------------------------------------------------------
    @staticmethod
    def _key(item: AccountItem) -> tuple[str, int]:
        return item.kind, item.id

    def _fill(self, *_args) -> None:
        self._busy = True
        self.tree.clear()
        needle = self.filter.text().strip().lower()
        shown = [i for i in self._items if needle in i.name.lower()]
        box = Qt.ItemFlag.ItemIsUserCheckable
        for item in shown:
            row = QTreeWidgetItem([item.name, item.kind, fmt_size(item.size) if item.size else "", item.status, item.added])
            row.setToolTip(0, item.name)
            row.setData(0, self._DATA, self._key(item))
            have = self._ticked.get(self._key(item), set())
            listed = item.files[:self.MAX_FILES]
            if item.ready and item.files:  # only what TorBox has finished can be downloaded from it
                row.setFlags(row.flags() | box)
            else:
                row.setToolTip(3, "Not finished on TorBox yet: nothing to download.")
            for f in listed:
                child = QTreeWidgetItem([f.name, "", fmt_size(f.size) if f.size else "", "", ""])
                child.setToolTip(0, f.path)
                child.setData(0, self._DATA, f.id)
                if item.ready:
                    child.setFlags(child.flags() | box)
                    child.setCheckState(0, Qt.CheckState.Checked if f.id in have else Qt.CheckState.Unchecked)
                row.addChild(child)
            if len(item.files) > self.MAX_FILES:
                row.addChild(QTreeWidgetItem([f"... and {len(item.files) - self.MAX_FILES} more files", "", "", "", ""]))
            if item.ready and item.files:
                row.setCheckState(0, self._row_state(item, have))
            self.tree.addTopLevelItem(row)
        self._busy = False
        ready = sum(1 for i in self._items if i.ready)
        total = sum(i.size for i in self._items)
        text = f"{len(self._items)} download(s), {ready} ready" + (f"  |  {fmt_size(total)}" if total else "")
        if needle:
            text += f"  |  {len(shown)} match the filter"
        if self._notes:
            text += "  |  " + "  |  ".join(self._notes)
        self.status.setText(text + ".")
        self._update_buttons()

    @staticmethod
    def _row_state(item: AccountItem, have: set[int]) -> Qt.CheckState:
        if not have:
            return Qt.CheckState.Unchecked
        every = len(have) >= len(item.files) and {f.id for f in item.files} <= have
        return Qt.CheckState.Checked if every else Qt.CheckState.PartiallyChecked

    def _item_changed(self, row: QTreeWidgetItem, column: int) -> None:
        """A tick was clicked: a download ticks or clears all its files; a file updates its download's tick."""
        if self._busy or column != 0 or not (row.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        self._busy = True
        try:
            parent = row.parent()
            if parent is None:  # a whole download
                key = row.data(0, self._DATA)
                item = next((i for i in self._items if self._key(i) == key), None)
                if item is None:
                    return
                on = row.checkState(0) != Qt.CheckState.Unchecked  # a click on a partial tick makes it whole
                row.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                for n in range(row.childCount()):
                    child = row.child(n)
                    if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                        child.setCheckState(0, row.checkState(0))
                if on:
                    self._ticked[key] = {f.id for f in item.files}  # all of them, even past the ones listed
                else:
                    self._ticked.pop(key, None)
            else:  # one file
                key = parent.data(0, self._DATA)
                item = next((i for i in self._items if self._key(i) == key), None)
                if item is None:
                    return
                have = self._ticked.setdefault(key, set())
                (have.add if row.checkState(0) == Qt.CheckState.Checked else have.discard)(row.data(0, self._DATA))
                listed = [parent.child(n) for n in range(parent.childCount())
                          if parent.child(n).flags() & Qt.ItemFlag.ItemIsUserCheckable]
                states = {c.checkState(0) for c in listed}
                if len(item.files) > len(listed):  # files past the listing count as ticked only if the download was
                    states.add(Qt.CheckState.Checked if len(have) >= len(item.files) else Qt.CheckState.Unchecked)
                parent.setCheckState(0, Qt.CheckState.Checked if states == {Qt.CheckState.Checked}
                                     else Qt.CheckState.Unchecked if states == {Qt.CheckState.Unchecked}
                                     else Qt.CheckState.PartiallyChecked)
                if not have:
                    self._ticked.pop(key, None)
        finally:
            self._busy = False
        self._update_buttons()

    def _tick_all(self, on: bool) -> None:
        """Select every finished download (the ones the filter shows), or clear every tick."""
        if on:
            needle = self.filter.text().strip().lower()
            for item in self._items:
                if item.ready and item.files and needle in item.name.lower():
                    self._ticked[self._key(item)] = {f.id for f in item.files}
        else:
            self._ticked.clear()
        self._fill()

    def _picks(self) -> list[tuple[AccountItem, AccountFile]]:
        out = []
        for item in self._items:
            if have := self._ticked.get(self._key(item)):
                out.extend((item, f) for f in item.files if f.id in have)
        return out

    def _update_buttons(self) -> None:
        picks = self._picks()
        self.add_btn.setEnabled(bool(picks))
        self.add_btn.setText(f"Add to file list ({len(picks)})" if picks else "Add to file list")
        self.clear_all.setEnabled(bool(picks))
        self.select_all.setEnabled(any(i.ready and i.files for i in self._items))

    def _add(self) -> None:
        picks = self._picks()
        if not picks:
            return
        files = account_remote_files(picks)
        self.addFiles.emit(files)
        self._ticked.clear()
        self._fill()
        self.status.setText(f"Added {len(files)} file(s) to Files & Torrents. They download from TorBox over HTTPS.")
