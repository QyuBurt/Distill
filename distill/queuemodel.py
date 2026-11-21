"""The run's files as a queue: what each task line means, and how many are waiting, running, done or in trouble.

The batch engines (batch.py, switchconv.py) already describe every file with two events, ("new", id, text) and
("update", id, state, message), and write the same lines into the log. Nothing in them changes here: this module reads
those lines, so the Queue view and the log can never disagree. Nothing here imports Qt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .models import fmt_size

WAITING, ACTIVE, DONE, SKIPPED, PROBLEM = "waiting", "active", "done", "skipped", "problem"

# every state word the engines write, and which of the five groups it belongs to
PHASE_OF = {
    "WAITING": WAITING, "QUEUED": WAITING,
    "EXTRACTING": ACTIVE, "CONVERTING": ACTIVE, "VERIFYING": ACTIVE, "COPYING": ACTIVE, "DECRYPTING": ACTIVE,
    "RETRYING": ACTIVE, "WORKING": ACTIVE, "COMPRESSING": ACTIVE, "UNPACKING": ACTIVE,
    "VERIFIED": DONE, "CONVERTED": DONE, "UNZIPPED": DONE, "OK": DONE,
    "SKIPPED": SKIPPED,
    "ERROR": PROBLEM, "FAILED": PROBLEM, "FLAGGED": PROBLEM, "STALLED": PROBLEM,
}
# states whose last bracket is a reason ("Game (USA) (Verification failed - exit code 1)"), not part of the name
_NOTE_STATES = frozenset({"ERROR", "FAILED", "SKIPPED", "FLAGGED", "STALLED", "UNZIPPED", "QUEUED", "RETRYING"})

_STATE = re.compile(r"^\[\s*([A-Za-z ]+?)\s*\]\s*(.*)$")
_PCT = re.compile(r"^\(\s*(\d{1,3}(?:\.\d+)?)\s*%\)\s*")
_STEP = re.compile(r"^\(([^()]*)\)\s+(.+)$")
_TAIL = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_EXISTS = re.compile(r"^(.*?)\s*\(([A-Za-z0-9]+ exists)\)\s*$")
_SIZE = r"[\d,]+(?:\.\d+)?\s[KMG]?B"
# what models.size_change() and BatchRunner._finish_one() add to a finished title: "71% smaller (4.20 GB -> 1.22 GB)",
# "same size (...)", or for the pages that unpack instead of compress, "extracted 1.20 GB" / "decrypted 4.00 GB"
_CHANGE = re.compile(rf"^(.*?) - ((?:\d+% (?:smaller|larger)|same size) \(({_SIZE}) -> ({_SIZE})\)|(?:extracted|decrypted) ({_SIZE}))$")
_UNITS = {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30}
CHILD_MARK = "\u2514\u2500"  # "└─": a game found inside an archive


@dataclass(frozen=True, slots=True)
class Task:
    state: str            # "CONVERTING" (upper case, no brackets); "" when the line carried none
    phase: str            # WAITING | ACTIVE | DONE | SKIPPED | PROBLEM
    name: str             # the file or game
    step: str = ""        # what a running row is doing right now: "hashing RVZ", "merging update 1/2"
    note: str = ""        # why a row was skipped, flagged or failed, or what it was waiting for
    pct: float | None = None  # 0 to 100 when the tool reports it, else None (the row shows no bar)
    child: bool = False   # a game inside an archive (its parent row is the archive)
    result: str = ""      # what a finished row changed: "71% smaller", "extracted 1.20 GB"
    before: int = 0       # bytes, read back from the sizes in the line (so only as exact as its two decimals)
    after: int = 0

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after)


def size_bytes(text: str) -> int:
    """'4.93 KB' -> bytes: the way models.fmt_size wrote it, read back."""
    number, _space, unit = text.strip().partition(" ")
    try:
        return round(float(number.replace(",", "")) * _UNITS.get(unit, 1))
    except ValueError:
        return 0


def parse_task(text: str) -> Task:
    """Read one task line, the way the log shows it: "[ CONVERTING ] (12.5%) Game (USA)"."""
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    first, more = (lines[0], lines[1:]) if lines else ("", [])
    m = _STATE.match(first)
    state, rest = (m.group(1).strip().upper(), m.group(2).strip()) if m else ("", first)
    phase = PHASE_OF.get(state, ACTIVE if state else WAITING)
    child = rest.startswith(CHILD_MARK)
    if child:
        rest = rest[len(CHILD_MARK):].strip()
    pct, step, note = None, "", ""
    if (p := _PCT.match(rest)) is not None:
        pct, rest = min(100.0, float(p.group(1))), rest[p.end():]
    elif phase == ACTIVE and (s := _STEP.match(rest)) is not None:
        step, rest = s.group(1).strip(), s.group(2).strip()
    if state in _NOTE_STATES and (t := _TAIL.match(rest)) is not None and t.group(1):
        rest, note = t.group(1), t.group(2).strip()
    elif phase == DONE and (e := _EXISTS.match(rest)) is not None:
        rest, note = e.group(1), e.group(2)
    result, before, after = "", 0, 0
    if phase == DONE and (c := _CHANGE.match(rest)) is not None:
        rest = c.group(1)
        result = c.group(2).split(" (")[0]
        if c.group(3):
            before, after = size_bytes(c.group(3)), size_bytes(c.group(4))
        else:
            after = size_bytes(c.group(5))
    if more:  # the Switch pages put the result on the lines under the name ("      -> Game.nsz")
        tail = " ".join(ln.removeprefix("->").strip() for ln in more)
        note = f"{note}  {tail}".strip() if note else tail
    return Task(state, phase, rest, step, note, pct, child, result, before, after)


@dataclass(frozen=True, slots=True)
class Tally:
    total: int = 0
    waiting: int = 0
    active: int = 0
    done: int = 0
    skipped: int = 0
    problem: int = 0
    saved: int = 0        # bytes freed by the finished titles so far (a title that came out larger frees nothing)

    @property
    def finished(self) -> int:
        return self.done + self.skipped + self.problem

    def headline(self, running: bool) -> str:
        """The one line above the queue: "3 of 12 done · 2 running · 1 failed"."""
        if not self.total:
            return ""
        extras = [f"{n} {word}" for n, word in ((self.active if running else 0, "running"), (self.problem, "failed"),
                                                (self.skipped, "skipped")) if n]
        if self.saved:
            extras.append(f"saved {fmt_size(self.saved)}")
        if not running and self.finished >= self.total and not self.problem and not self.skipped:
            return " \u00b7 ".join([f"All {self.total} done", *extras])
        return " \u00b7 ".join([f"{self.done} of {self.total} done", *extras])


def matches(task: Task, view: str) -> bool:
    """Whether a row belongs under a filter: "all", "active" (running now) or "problems"."""
    return view == "all" or (view == "active" and task.phase == ACTIVE) or (view == "problems" and task.phase == PROBLEM)


class RunQueue:
    """Every task line of the run in the order it appeared. A retry adds to the same queue as a new run: the rows of the
    earlier runs stay (so the old errors stay readable) but are `stale`, and no longer counted, because the engine starts
    its ids over from 1 each run."""

    def __init__(self) -> None:
        self._tasks: list[Task] = []
        self._run_of: list[int] = []
        self._row: dict[tuple[int, int], int] = {}
        self._run = 0

    def __len__(self) -> int:
        return len(self._tasks)

    def clear(self) -> None:
        self._tasks.clear()
        self._run_of.clear()
        self._row.clear()
        self._run = 0

    def begin_run(self, keep: bool = False) -> None:
        """A run is starting. keep=True (a retry) leaves the earlier rows in place as stale ones."""
        if keep and self._tasks:
            self._run += 1
        else:
            self.clear()

    def task_at(self, row: int) -> Task:
        return self._tasks[row]

    def stale(self, row: int) -> bool:
        return self._run_of[row] != self._run

    def row_of(self, tid: int) -> int | None:
        return self._row.get((self._run, tid))

    def new(self, tid: int, text: str) -> int:
        """Add a task; its row. (An id seen twice in one run is an update, not a second row.)"""
        if (row := self.row_of(tid)) is not None:
            self._tasks[row] = parse_task(text)
            return row
        self._tasks.append(parse_task(text))
        self._run_of.append(self._run)
        self._row[(self._run, tid)] = len(self._tasks) - 1
        return len(self._tasks) - 1

    def update(self, tid: int, text: str) -> int | None:
        """Rewrite a task's line; its row, or None for an id never announced (the log ignores those too)."""
        if (row := self.row_of(tid)) is None:
            return None
        old = self._tasks[row]
        new = parse_task(text)
        if old.child and not new.child:  # an update line may drop the └─ mark: the game is still inside its archive
            new = replace(new, child=True)
        self._tasks[row] = new
        return row

    def close_run(self) -> list[int]:
        """The run is over. A row still waiting or running was cut short (Stop, or a crash): say so, instead of leaving it
        looking busy for ever. The rows changed."""
        changed = []
        for i, task in enumerate(self._tasks):
            if self._run_of[i] != self._run or task.phase not in (WAITING, ACTIVE):
                continue
            started = task.phase == ACTIVE
            self._tasks[i] = Task("STOPPED" if started else "NOT RUN", SKIPPED, task.name, note=(
                "stopped before it finished" if started else "not started"), child=task.child)
            changed.append(i)
        return changed

    def tally(self) -> Tally:
        """Counts for the current run. A game found inside an archive counts as itself, and its archive row does not (the
        archive is only the way in), so '4 of 9 done' means games, not games plus the zip they came from."""
        rows = [i for i in range(len(self._tasks)) if self._run_of[i] == self._run]
        parents_with_games: set[int] = set()
        parent = None
        for i in rows:
            if self._tasks[i].child:
                if parent is not None:
                    parents_with_games.add(parent)
            else:
                parent = i
        counts = {WAITING: 0, ACTIVE: 0, DONE: 0, SKIPPED: 0, PROBLEM: 0}
        total = saved = 0
        for i in rows:
            if i in parents_with_games:
                continue
            total += 1
            counts[self._tasks[i].phase] += 1
            saved += self._tasks[i].saved
        return Tally(total, counts[WAITING], counts[ACTIVE], counts[DONE], counts[SKIPPED], counts[PROBLEM], saved)
