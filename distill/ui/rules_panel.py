"""Settings > Downloads: speed limit, weekly schedule and per-site limits. Edits distill.netrules.RULES; running
downloads pick the new rules up straight away."""
from __future__ import annotations

from PySide6.QtCore import QTime, QTimer, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
                               QTimeEdit, QVBoxLayout, QWidget)

from ..netrules import DAYS, MAX_SEGMENTS, PAUSED, normalize_proxy, RULES, HostRule, Rules, ScheduleRule, describe_schedule, save_rules, set_rules
from .state import LABEL_W
from .widgets import Card, hint, StepSwitch

_DAY_CHOICES = (("Every day", (0, 1, 2, 3, 4, 5, 6)), ("Weekdays", (0, 1, 2, 3, 4)), ("Weekends", (5, 6)),
                *((d, (i,)) for i, d in enumerate(DAYS)))
_MODES = ("Full speed", "Limit to", "Don't download")


# The overall limit is a ladder of stops from slow to unlimited (KB/s; 0 = unlimited, always the last stop).
_LADDER = (64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)


def _long(kbps: int) -> str:
    return f"{kbps // 1024} MB/s" if kbps >= 1024 and kbps % 1024 == 0 else f"{kbps} KB/s"


def _speed_spin(special: str, maximum: int = 10_000_000) -> QSpinBox:
    box = QSpinBox()
    box.setRange(0, maximum)
    box.setSingleStep(128)
    box.setSuffix(" KB/s")
    box.setSpecialValueText(special)
    box.setMinimumWidth(130)
    box.setKeyboardTracking(False)
    return box


class _ScheduleRow(QWidget):
    removed = Signal(object)
    changed = Signal()

    def __init__(self, rule: ScheduleRule):
        super().__init__()
        grid = QGridLayout(self)  # two short lines instead of one long one, so the row never runs off the page
        grid.setContentsMargins(0, 0, 0, 6)
        grid.setHorizontalSpacing(8)
        self.days = QComboBox()
        self.days.addItems([name for name, _d in _DAY_CHOICES])
        self.days.setCurrentIndex(next((i for i, (_n, d) in enumerate(_DAY_CHOICES) if d == rule.days), 0))
        self.start, self.end = QTimeEdit(QTime(rule.start // 60, rule.start % 60)), QTimeEdit(QTime(rule.end // 60 % 24, rule.end % 60))
        for t in (self.start, self.end):
            t.setDisplayFormat("HH:mm")
        self.mode = QComboBox()
        self.mode.addItems(_MODES)
        self.limit = _speed_spin("", 10_000_000)
        self.limit.setValue(max(rule.kbps, 0) or 1024)
        self.mode.setCurrentIndex(0 if rule.kbps == 0 else 2 if rule.kbps == PAUSED else 1)
        self.mode.currentIndexChanged.connect(self._sync)
        gone = QPushButton("Remove")
        gone.clicked.connect(lambda _c=False: self.removed.emit(self))
        for col, w in enumerate((self.days, QLabel("from"), self.start, QLabel("to"), self.end)):
            grid.addWidget(w, 0, col)
        grid.addWidget(self.mode, 1, 0)
        grid.addWidget(self.limit, 1, 1, 1, 2)
        grid.addWidget(gone, 1, 4)
        grid.setColumnStretch(5, 1)
        self._sync()
        for sig in (self.days.currentIndexChanged, self.start.timeChanged, self.end.timeChanged,
                    self.mode.currentIndexChanged, self.limit.valueChanged):
            sig.connect(lambda *_: self.changed.emit())

    def _sync(self) -> None:
        self.limit.setVisible(self.mode.currentIndex() == 1)

    def rule(self) -> ScheduleRule:
        start = self.start.time().hour() * 60 + self.start.time().minute()
        end = self.end.time().hour() * 60 + self.end.time().minute()
        mode = self.mode.currentIndex()
        return ScheduleRule(_DAY_CHOICES[self.days.currentIndex()][1], start, end,
                            0 if mode == 0 else PAUSED if mode == 2 else max(1, self.limit.value()))


class _HostRow(QWidget):
    removed = Signal(object)
    changed = Signal()

    def __init__(self, rule: HostRule):
        super().__init__()
        grid = QGridLayout(self)  # site and proxy on the first line, the limits on the second
        grid.setContentsMargins(0, 0, 0, 6)
        grid.setHorizontalSpacing(8)
        self.host = QLineEdit(rule.host)
        self.host.setPlaceholderText("example.com")
        self.host.setMinimumWidth(150)
        self.files = QSpinBox()
        self.files.setRange(0, 64)
        self.files.setSpecialValueText("No limit")
        self.files.setSuffix(" at once")
        self.files.setValue(rule.max_files)
        self.speed = _speed_spin("No limit")
        self.speed.setValue(rule.kbps)
        self.tries = QSpinBox()
        self.tries.setRange(0, 20)
        self.tries.setSpecialValueText("Default")
        self.tries.setSuffix(" tries")
        self.tries.setValue(rule.attempts)
        self.conns = QSpinBox()
        self.conns.setRange(0, MAX_SEGMENTS)
        self.conns.setSpecialValueText("Overall")
        self.conns.setSuffix(" conn.")
        self.conns.setValue(rule.segments)
        self.conns.setToolTip("Connections per file for this site. 'Overall' uses the setting above.")
        self.proxy = QLineEdit(rule.proxy)
        self.proxy.setPlaceholderText("proxy: overall")
        self.proxy.setToolTip("Blank = the overall proxy. 'direct' = no proxy for this site. Or http://host:port.")
        self.proxy.setMinimumWidth(130)
        gone = QPushButton("Remove")
        gone.clicked.connect(lambda _c=False: self.removed.emit(self))
        grid.addWidget(self.host, 0, 0, 1, 2)
        grid.addWidget(self.proxy, 0, 2, 1, 2)
        grid.addWidget(gone, 0, 4)
        for col, w in enumerate((self.files, self.speed, self.tries, self.conns)):
            grid.addWidget(w, 1, col)
        grid.setColumnStretch(5, 1)
        for sig in (self.host.textChanged, self.proxy.textChanged, self.files.valueChanged, self.speed.valueChanged,
                    self.tries.valueChanged, self.conns.valueChanged):
            sig.connect(lambda *_: self.changed.emit())

    def rule(self) -> HostRule | None:
        host = self.host.text().strip().lower()
        if "://" in host:
            from urllib.parse import urlsplit
            host = urlsplit(host).hostname or ""
        host = host.strip("/ ")
        return HostRule(host, self.files.value(), self.speed.value(), self.tries.value(), self.conns.value(),
                        normalize_proxy(self.proxy.text(), True)[0]) if host else None


class RulesPanel(QWidget):
    saved = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._timer = QTimer(self)  # every change saves itself a moment after the last one
        self._timer.setSingleShot(True)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self._save)

        card = Card("Speed Limit")
        row = QHBoxLayout()
        row.setSpacing(12)
        label = QLabel("Overall limit")
        label.setFixedSize(LABEL_W, 30)  # as tall as the stepper, so the text lines up with it
        self.overall = StepSwitch()
        stops = sorted({*_LADDER, *([RULES.global_kbps] if RULES.global_kbps > 0 else [])})  # a saved odd value keeps its own stop
        for kbps in (*stops, 0):
            self.overall.addItem("Unlimited" if kbps == 0 else _long(kbps), kbps,
                                 "Unlimited: downloads run as fast as your connection allows." if kbps == 0
                                 else f"Limited to {_long(kbps)}, shared by every download.")
        self.overall.setCurrentIndex(max(self.overall.findData(RULES.global_kbps), 0))
        row.addWidget(label, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.overall, 1, Qt.AlignmentFlag.AlignTop)
        card.add(row)
        card.add(hint("Step from slow (left) to unlimited (right). Applies whenever no schedule window below matches."))
        row = QHBoxLayout()
        row.setSpacing(12)
        label = QLabel("Connections per file")
        label.setFixedSize(LABEL_W, 30)
        self.segments = StepSwitch()
        for n in range(1, MAX_SEGMENTS + 1):
            self.segments.addItem("1 (off)" if n == 1 else str(n), n,
                                  "One connection per file (off): the safest." if n == 1 else
                                  f"{n} connections per file: big files (32 MB and up) are fetched in {n} parts at once.")
        self.segments.setValue(RULES.segments)
        self.segments.setToolTip("Splits big files (32 MB and up) into parts fetched at the same time, which helps on "
                                 "servers that limit each connection. Some sites ban for it: set per-site values below.")
        row.addWidget(label, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.segments, 1, Qt.AlignmentFlag.AlignTop)
        card.add(row)
        card.add(hint("This multiplies with Parallel downloads: 4 files at once with 4 connections each is up to 16 "
                      "connections. Fewer is gentler on servers."))
        row = QHBoxLayout()
        label = QLabel("Proxy")
        label.setFixedWidth(LABEL_W)
        self.proxy = QLineEdit(RULES.proxy)
        self.proxy.setPlaceholderText("none (host:port or http://host:port)")
        self.proxy.setToolTip("An http(s) proxy for all downloads. SOCKS isn't supported. A password in the address is "
                              "saved in plain text in distill_rules.json: prefer a proxy that trusts your IP.")
        row.addWidget(label)
        row.addWidget(self.proxy, 1)
        card.add(row)
        card.add(hint("More than one connection can speed up big files on slow-per-connection servers, but some sites "
                      "refuse or ban it. Per-site rules below override this."))
        self.problem = QLabel("")  # a proxy address that can't be saved yet
        self.problem.setObjectName("notice")
        self.problem.setWordWrap(True)
        self.problem.setVisible(False)
        card.add(self.problem)
        self.now_label = hint("")
        card.add(self.now_label)
        lay.addWidget(card)

        card = Card("Schedule")
        card.add(hint("Full speed overnight, slow by day, or nothing on weekends. The first matching window wins; "
                      "outside every window the overall limit applies. A window that ends before it starts runs "
                      "past midnight."))
        self._sched_box = QVBoxLayout()
        card.add(self._sched_box)
        self._sched: list[_ScheduleRow] = []
        for r in RULES.schedule:
            self._add_schedule(r)
        add = QPushButton("Add a window")
        add.clicked.connect(lambda _c=False: (self._add_schedule(ScheduleRule((0, 1, 2, 3, 4, 5, 6), 2 * 60, 8 * 60, 0)), self._timer.start()))
        h = QHBoxLayout()
        h.addWidget(add)
        h.addStretch(1)
        card.add(h)
        lay.addWidget(card)

        card = Card("Per-Site Limits")
        card.add(hint("Some sites ban or throttle too many connections. A rule for example.com also covers "
                      "cdn.example.com; the most specific rule wins."))
        self._host_box = QVBoxLayout()
        card.add(self._host_box)
        self._hosts: list[_HostRow] = []
        for r in RULES.hosts:
            self._add_host(r)
        add = QPushButton("Add a site")
        add.clicked.connect(lambda _c=False: self._add_host(HostRule("")))
        h = QHBoxLayout()
        h.addWidget(add)
        h.addStretch(1)
        card.add(h)
        lay.addWidget(card)

        for sig in (self.overall.valueChanged, self.segments.valueChanged, self.proxy.textChanged):
            sig.connect(lambda *_: self._timer.start())
        self._refresh_now()

    def _add_schedule(self, rule: ScheduleRule) -> None:
        row = _ScheduleRow(rule)
        row.removed.connect(lambda w: self._drop(w, self._sched, self._sched_box))
        row.changed.connect(self._timer.start)
        self._sched.append(row)
        self._sched_box.addWidget(row)

    def _add_host(self, rule: HostRule) -> None:
        row = _HostRow(rule)
        row.removed.connect(lambda w: self._drop(w, self._hosts, self._host_box))
        row.changed.connect(self._timer.start)
        self._hosts.append(row)
        self._host_box.addWidget(row)

    def _drop(self, widget: QWidget, rows: list, box: QVBoxLayout) -> None:
        rows.remove(widget)
        box.removeWidget(widget)
        widget.deleteLater()
        self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self._timer.isActive():  # closing Settings right after an edit must not lose it
            self._timer.stop()
            self._save()

    def _refresh_now(self) -> None:
        kbps = RULES.active_kbps()
        text = "Paused by the schedule" if kbps == PAUSED else "Unlimited" if kbps == 0 else f"{kbps} KB/s"
        self.now_label.setText(f"Right now: {text}.")

    def _save(self) -> None:
        for text, allow, where in [(self.proxy.text(), False, "the overall proxy")] + \
                [(r.proxy.text(), True, f"the proxy for {r.host.text().strip() or 'a site'}") for r in self._hosts]:
            if (problem := normalize_proxy(text, allow)[1]):
                self.problem.setText(f"{where.capitalize()}: {problem}. Nothing is saved until this is fixed.")
                self.problem.setVisible(True)
                return
        self.problem.setVisible(False)
        new = Rules(self.overall.value(), [r.rule() for r in self._sched],
                    [h for h in (r.rule() for r in self._hosts) if h], self.segments.value(),
                    normalize_proxy(self.proxy.text())[0])
        set_rules(new)
        save_rules()
        self._refresh_now()
        self.saved.emit()

    def summary(self) -> list[str]:
        return [describe_schedule(r) for r in RULES.schedule]
