"""`Distill --launch game:<id>` / `Distill --launch recomp:<id>`: start one game from the command line, with no window, and stay until it ends.

This is what a Sunshine app entry runs (see streaming.py). Sunshine ends the stream when the program it started exits, and ends the game
by ending that program's process tree, so unlike the Play button this does not detach: it starts the emulator (or recomp) as a child, waits,
and then adds the session to the game's play time. The emulator, core, launch options and BIOS rules are the ones the Games page uses; it
reads the same games.db and state files, so a change made in Distill applies to the next stream too.

Nothing here imports Qt, and nothing opens a window: a failure goes to launch.log beside the other Distill data (and to stderr when there is one).
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import emulators as em
from . import games as gm
from . import recomps as rc

FLAG = "--launch"
RESUME_FLAG = "--resume"
KINDS = ("game", "recomp")
_TARGET = re.compile(r"^(game|recomp):([A-Za-z0-9._-]+)$")


class LaunchError(Exception):
    """Something the player can act on, in plain words."""


def target(kind: str, ident: str) -> str:
    """'game:g1a2b3c' / 'recomp:ship-of-harkinian': the one word that names a game on the command line."""
    return f"{kind}:{ident}"


def parse_target(text: str) -> tuple[str, str]:
    m = _TARGET.match((text or "").strip())
    if not m:
        raise LaunchError(f"'{text}' is not something Distill can start. Use game:<id> or recomp:<id>.")
    return m.group(1), m.group(2)


def parse_args(argv: list[str]) -> tuple[str, str, bool]:
    """(kind, id, resume) from the program's arguments, with or without the program name in front. Accepts `--launch x` and `--launch=x`."""
    resume = RESUME_FLAG in argv
    for i, arg in enumerate(argv):
        if arg == FLAG:
            if i + 1 >= len(argv):
                raise LaunchError(f"{FLAG} needs a game, like {FLAG} game:g1a2b3c")
            return (*parse_target(argv[i + 1]), resume)
        if arg.startswith(FLAG + "="):
            return (*parse_target(arg.split("=", 1)[1]), resume)
    raise LaunchError(f"{FLAG} is missing")


# ---- what the Games page uses to find an emulator, without the page ------------------------------------------------
def retro_of(em_store: rc.Store) -> gm.RetroSetup | None:
    """The installed RetroArch (from the Emulators page's records), with the cores it has; None when it isn't set up."""
    try:
        inst = em_store.installed(gm.RETROARCH)
    except Exception:
        inst = None
    return gm.retro_setup(inst.folder, inst.exe, inst.linked) if inst is not None else None


def full_catalog() -> list[em.Emulator]:
    """Every emulator and fork, as the Emulators page lists them."""
    base = em.load_catalog()
    return [*base, *em.load_forks(base)]


def standalones_of(em_store: rc.Store, catalog: list[em.Emulator], store: gm.GameStore, system: str) -> dict[str, gm.Standalone]:
    """The emulators of their own (installed on the Emulators page) that play a platform."""
    try:
        def exe(rid: str):
            inst = em_store.installed(rid)
            path = inst.exe_path() if inst is not None else None
            return path if path is not None and path.exists() else None

        return gm.standalone_for(system, catalog, exe, lambda rid: store.setting(f"args:{rid}"))
    except Exception:  # the Emulators page not being ready must not stop a game from starting
        return {}


# ---- one session ----------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Session:
    proc: object  # the running program (a subprocess.Popen)
    label: str  # what it is: 'RetroArch · mGBA', 'Ship of Harkinian'
    name: str  # the game's name
    finished: Callable[[float], None]  # called with how long it ran
    after: Callable[[], None] | None = None  # called when it has closed, however short (the game's "run after" command)


def prepare_game(ident: str, resume: bool = False, *, store: gm.GameStore | None = None, em_store: rc.Store | None = None,
                 catalog: list[em.Emulator] | None = None, detach: bool = False) -> Session:
    store = store or gm.GameStore()
    game = store.get(ident)
    if game is None:
        raise LaunchError("That game isn't in your Distill library any more. Export your library to Sunshine again.")
    if not game.installed:
        raise LaunchError(f"{game.name} hasn't been downloaded yet.")
    em_store = em_store or em.open_store()
    emus = standalones_of(em_store, catalog if catalog is not None else full_catalog(), store, game.system)
    try:
        plan = gm.plan_launch(game, game.emulator, retro_of(em_store), store.default_for(game.system), emus, resume=resume, profile=store.profile(game.id))
    except gm.GameError as exc:
        raise LaunchError(str(exc)) from exc
    plan.admin = False  # (Sunshine ends the game by ending the process tree: a program started elevated would be out of its reach)
    if not detach and plan.cmd and plan.cmd[0] == "open":
        plan.cmd.insert(1, "-W")  # a Mac's `open` hands over and returns at once; -W makes it wait until the program quits
    try:
        proc = gm.spawn(plan, detach=detach)
    except gm.GameError as exc:
        raise LaunchError(str(exc)) from exc
    store.record_play(game.id)

    def finished(seconds: float) -> None:
        if seconds >= gm.MIN_SESSION:  # shorter is a launcher handing over to another program, not a game being played
            store.add_playtime(game.id, seconds)

    return Session(proc, plan.label, game.name, finished, gm.post_hook(plan))


def prepare_recomp(ident: str, *, rc_store: rc.Store | None = None, detach: bool = False) -> Session:
    rc_store = rc_store or rc.Store()
    inst = rc_store.installed(ident)
    if inst is None:
        raise LaunchError("That project isn't installed any more. Install it on the PC Recomps page, then export your library to Sunshine again.")
    name = next((r.game or r.name for r in rc.load_catalog() if r.id == ident), ident)
    try:
        proc = rc.launch(inst, detach=detach)
    except rc.RecompError as exc:
        raise LaunchError(str(exc)) from exc
    return Session(proc, name, name, lambda _seconds: None)


def prepare(kind: str, ident: str, resume: bool = False, **kw) -> Session:
    if kind == "game":
        return prepare_game(ident, resume, **kw)
    if resume:
        raise LaunchError("Only games played in RetroArch can continue from a save state.")
    return prepare_recomp(ident, **{k: v for k, v in kw.items() if k in ("rc_store", "detach")})


def wait(session: Session) -> int:
    """Block until the program ends, record the session, and return its exit code (0 when it can't be told)."""
    start = time.time()
    try:
        code = session.proc.wait()
    except Exception:  # a process that can't be waited on just isn't timed
        return 0
    if session.after is not None:
        try:
            session.after()
        except Exception:  # (a command after the game that fails is its own business)
            pass
    session.finished(time.time() - start)
    return code if isinstance(code, int) else 0


# ---- the command line -------------------------------------------------------------------------------------------------
def log_path() -> Path:
    return rc.root() / "launch.log"


def _complain(message: str) -> None:
    """Said where somebody can find it: there is no window, and a windowed .exe has no console either."""
    try:
        with log_path().open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except OSError:
        pass
    if sys.stderr is not None:
        try:
            print(f"Distill: {message}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass


def main(argv: list[str]) -> int:
    """Exit code 0 when the program ran (whatever it returned), 2 when it could not be started."""
    try:
        kind, ident, resume = parse_args(argv)
        session = prepare(kind, ident, resume)
    except LaunchError as exc:
        _complain(str(exc))
        return 2
    except Exception as exc:  # no window to show a crash in: keep the details where they can be read
        _complain(f"unexpected error ({type(exc).__name__}): {exc}")
        return 2
    wait(session)
    return 0
