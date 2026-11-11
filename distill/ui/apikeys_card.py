"""Settings > API keys: every key and login in one place, sorted into folders by what they are for, so someone who only plays
games never has to look at the download sites (and the other way round).

    Games & artwork        SteamGridDB, RetroAchievements, RomM
    Downloads & file hosts the sign-ins from the Files & Torrents page (archive.org, 1fichier, TorBox ..., Prowlarr, any host)
    Updates                GitHub

A folder is a heading with a short status ("2 saved") that opens to its cards; all of them start folded. Each edit saves
itself a moment after the last one, like the Downloads sign-ins; keys are hidden while typed and kept in the one sealed
file (see apikeys.py).
"""
from __future__ import annotations

import webbrowser
from typing import NamedTuple

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QPainter
from PySide6.QtWidgets import QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .. import apikeys
from ..downloads import LOGIN_SITES, LOGINS
from . import icons, motion, msgbox
from .state import LABEL_W
from .theme import T
from .widgets import Card, ExpanderHeader, hint


class Service(NamedTuple):
    id: str
    title: str
    blurb: str
    url: str  # where to get a key
    fields: tuple[tuple[str, str, str, bool], ...]  # (saved name, label, what the box says, hidden while typed)
    need: str  # what to say when Test is pressed with a box empty


GAMES = (
    Service("sgdb", "SteamGridDB",
            "Free, from steamgriddb.com > Preferences > API. Gives PC Recomps posters in one portrait size, Games covers and "
            "backgrounds, and emulators their community icons.",
            "https://www.steamgriddb.com/profile/preferences/api",
            (("sgdb", "API key", "SteamGridDB API key (optional)", True),), "Enter a key first."),
    Service("ra", "RetroAchievements",
            "Shows a game's achievements, and which you have unlocked, on its page in Games. Your user name and the Web API key "
            "from your settings page there.",
            "https://retroachievements.org/controlpanel.php",
            (("ra_user", "User name", "RetroAchievements user name (optional)", False),
             ("ra", "Web API key", "RetroAchievements Web API key (optional)", True)), "Enter both the user name and the key."),
    Service("romm", "RomM",
            "Lists the games on your own RomM server in Games, next to the ones on this computer, and installs one with a click. The server's "
            "address, and a client API token from RomM (your profile > Client API tokens). A user:password pair or a session cookie (name=value) also work.",
            "https://docs.romm.app/",
            (("romm_url", "Server", "RomM address, for example http://romm.local:8080 (optional)", False),
             ("romm", "API token", "RomM API token (optional)", True)), "Enter both the server's address and the token."),
)
UPDATES = (
    Service("github", "GitHub",
            "A read-only token (no permissions ticked) lifts GitHub's limit from 60 to 5,000 requests an hour, which tool, emulator "
            "and recomp update checks can use up.",
            "https://github.com/settings/personal-access-tokens/new",
            (("github", "Token", "GitHub token (optional)", True),), "Enter a key first."),
)


class FolderHeader(ExpanderHeader):
    """An Expander's heading row, with a short status to the left of the chevron."""

    summary = ""
    saved = False

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.summary:
            return
        p = QPainter(self)
        font = QFont(self.font())
        font.setPointSizeF(9.5)
        p.setFont(font)
        p.setPen(T.accent_text if self.saved else T.fg3)
        r = QRectF(self.rect())
        p.drawText(QRectF(r.left(), r.top(), r.width() - 48, r.height()), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   self.summary)
        p.end()


class KeyFolder(QWidget):
    """A group on the page: heading, a hairline, and (when open) a line saying what it is for and the cards under it."""

    def __init__(self, title: str, blurb: str, tip: str = "", expanded: bool = False):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = FolderHeader(title, icons.icon_for_title(title))
        self.header.setChecked(expanded)
        self.header.toggled.connect(self._toggled)
        lay.addWidget(self.header)
        rule = QFrame()
        rule.setFixedHeight(1)
        T.bind_css(rule, lambda: f"background: {T.card_border.name()}; border: none; margin: 0 6px;")
        lay.addWidget(rule)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 12, 0, 8)
        self.body_lay.setSpacing(10)
        self.body_lay.addWidget(hint(blurb, tip=tip))
        lay.addWidget(self.body)
        self._toggled(expanded)

    def add(self, item) -> None:
        if isinstance(item, QWidget):
            self.body_lay.addWidget(item)
        else:
            self.body_lay.addLayout(item)

    def set_summary(self, text: str, saved: bool) -> None:
        self.header.summary, self.header.saved = text, saved
        self.header.update()

    def _toggled(self, open_: bool) -> None:
        self.header.update()
        motion.slide(self.body, open_)  # eases open and shut, as an Expander's body does
        self.updateGeometry()


class ApiKeysPanel(QWidget):
    changed = Signal(str)  # the name of the key that was saved

    def __init__(self, downloads: QWidget | None = None) -> None:
        """`downloads` is the sign-in panel (Settings' Files & Torrents accounts); it goes inside the Downloads folder."""
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._save)
        self.edits: dict[str, QLineEdit] = {}
        if problem := apikeys.problem():
            lay.addWidget(hint(f"Saved keys couldn't be read: {problem}. Enter them again to replace them."))

        self.games = KeyFolder("Games & artwork", "For the Games, PC Recomps and Emulators pages. Skip these and everything still works, "
                                                  "with plainer pictures.")
        for service in GAMES:
            self.games.add(self._card(service))
        lay.addWidget(self.games)

        self.downloads = KeyFolder(
            "Downloads & file hosts", "Sign-ins for the Files & Torrents page: file hosts that need an account, debrid services and "
                                      "torrent search.",
            tip="With a TorBox API key, torrents are looked up in TorBox's cache first: cached files download straight from TorBox "
                "over HTTPS, and only what isn't cached falls back to torrenting.")
        if downloads is not None:
            self.downloads.add(downloads)
            downloads.saved.connect(self.refresh)
        lay.addWidget(self.downloads)

        self.updates = KeyFolder("Updates", "Only needed if update checks for tools, emulators or recomps say GitHub is rate-limiting you.")
        for service in UPDATES:
            self.updates.add(self._card(service))
        lay.addWidget(self.updates)

        note = hint(f"Everything on this page is kept in one file, {apikeys.FILE.name}, next to this app: {apikeys.protection()}.")
        lay.addSpacing(6)
        lay.addWidget(note)
        self.refresh()

    # ---- one card per service ------------------------------------------------------------------------------------
    def _card(self, service: Service) -> Card:
        card = Card(service.title)
        card.add(hint(service.blurb))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setColumnStretch(1, 1)
        for row, (name, label, placeholder, hidden) in enumerate(service.fields):
            name_label = QLabel(label)
            name_label.setFixedWidth(LABEL_W)  # the same label column as the other settings forms
            edit = QLineEdit(apikeys.get(name))
            edit.setEchoMode(QLineEdit.EchoMode.Password if hidden else QLineEdit.EchoMode.Normal)  # a user name isn't a secret
            edit.setPlaceholderText(placeholder)
            edit.setMinimumWidth(180)
            edit.textChanged.connect(lambda *_: self._timer.start())
            self.edits[name] = edit
            grid.addWidget(name_label, row, 0)
            grid.addWidget(edit, row, 1)
        get_key, test = QPushButton("Get key..."), QPushButton("Test")
        get_key.clicked.connect(lambda _c=False, u=service.url: webbrowser.open(u))
        test.clicked.connect(lambda _c=False, s=service: self._test(s))
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(get_key)
        buttons.addWidget(test)
        grid.addLayout(buttons, 0, 2)
        card.add(grid)
        return card

    # ---- saving and the folder headings ----------------------------------------------------------------------------
    def _save(self) -> None:
        self._timer.stop()
        for name, edit in self.edits.items():
            text = edit.text().strip()
            if text != apikeys.get(name):
                apikeys.set(name, text)
                self.changed.emit(name)
        self.refresh()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self._timer.isActive():  # closing Settings right after typing must not lose it
            self._save()

    def refresh(self) -> None:
        """Say on each folder's heading how much is saved in it."""
        def line(count: int) -> tuple[str, bool]:
            return (f"{count} saved", True) if count else ("None saved", False)

        def have(services: tuple[Service, ...]) -> int:
            return sum(all(apikeys.get(f[0]) for f in s.fields) for s in services)

        signed_in = sum(any((LOGINS.get(k) or {}).values()) for k in LOGIN_SITES) + sum(1 for e in LOGINS.get("custom", []) if e.get("secret"))
        self.games.set_summary(*line(have(GAMES)))
        self.downloads.set_summary(*line(signed_in))
        self.updates.set_summary(*line(have(UPDATES)))

    # ---- Test ------------------------------------------------------------------------------------------------------
    def _test(self, service: Service) -> None:
        values = {f[0]: self.edits[f[0]].text().strip() for f in service.fields}
        if not all(values.values()):
            msgbox.information(self, service.title, service.need)
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            from .. import gamemedia, recomps  # noqa: PLC0415
            if service.id == "ra":
                problem = gamemedia.ra_test(values["ra_user"], values["ra"])
            elif service.id == "romm":
                from .. import romm  # noqa: PLC0415
                try:
                    problem = romm.Client(values["romm_url"], values["romm"]).test()
                except romm.RommError as exc:
                    problem = str(exc)
            elif service.id == "sgdb":
                problem = recomps.sgdb_test(values["sgdb"])
            else:
                problem = apikeys.test_github(values["github"])
        finally:
            QApplication.restoreOverrideCursor()
        msgbox.information(self, service.title, problem or "The key works.")
