"""Drop a folder of box art on the window and every game of that platform with a matching picture gets it. No Qt in here.

`plan` reads what was dropped: a folder of pictures (the platform comes from the folder's name: 'PS2', 'Game Boy Advance'), or a
folder of such folders, one per platform. `match` pairs the pictures with a platform's games by title (region and version tags,
punctuation, accents, 'The' and roman numerals are ignored; a picture also matches a game's file name). `apply` saves them as the
games' own covers, which are final: nothing fetched later replaces them."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import games as gm
from . import recomps as rc
from .fuzzy import strip_accents
from .lbmatch import norm_title

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_SUFFIX = re.compile(r"\s*[-_]\s*(?:\d{1,2}|front|box|boxart|cover|2d|3d)\s*$", re.IGNORECASE)  # LaunchBox 'Game-01.png', 'Game - Front.png'
_PREFERRED = ("front", "box", "cover")


@dataclass(frozen=True, slots=True)
class ArtSet:
    folder: Path
    platform: str  # Distill's name for it, or '' when the folder's name doesn't say
    files: tuple[Path, ...]


@dataclass(slots=True)
class Matched:
    pairs: list[tuple[gm.Game, Path]] = field(default_factory=list)  # every game that gets a picture, with it
    unmatched: list[Path] = field(default_factory=list)  # pictures no game on the platform goes with


def _pictures(folder: Path) -> tuple[Path, ...]:
    try:
        return tuple(sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")),
                            key=lambda p: p.name.casefold()))
    except OSError:
        return ()


def platform_of(folder: Path, systems: Iterable[str] = ()) -> str:
    """The platform a folder's name says, in the library's own spelling when the library has it; '' when it says nothing."""
    name = folder.name
    found = gm.folder_platform(str(folder)) or gm.canonical_system(name)
    have = {s.casefold(): s for s in systems}
    if found.casefold() in have:
        return have[found.casefold()]
    return found if found and (found in gm.sysmod.SYSTEM_NAMES or found in gm.LB_PLATFORM_NAMES) else ""


def plan(paths: list[Path], systems: Iterable[str] = ()) -> list[ArtSet]:
    """What the drop holds: a set of pictures for each folder dropped that has some, else for each of its subfolders that has some.
    Loose picture files dropped together are one set (named for their folder). Empty when nothing dropped is a picture or a folder of them."""
    systems = list(systems)
    sets: list[ArtSet] = []
    loose = [p for p in paths if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if loose and len({p.parent for p in loose}) == 1:
        sets.append(ArtSet(loose[0].parent, platform_of(loose[0].parent, systems), tuple(sorted(loose, key=lambda p: p.name.casefold()))))
    for folder in (p for p in paths if p.is_dir()):
        if pics := _pictures(folder):
            sets.append(ArtSet(folder, platform_of(folder, systems), pics))
            continue
        try:
            subs = sorted((d for d in folder.iterdir() if d.is_dir() and not d.name.startswith(".")), key=lambda d: d.name.casefold())
        except OSError:
            subs = []
        for sub in subs:
            if pics := _pictures(sub):
                sets.append(ArtSet(sub, platform_of(sub, systems), pics))
    return sets


def art_key(text: str) -> str:
    """A picture's file name or a game's title as it is compared."""
    text = strip_accents(text)
    return norm_title(_SUFFIX.sub("", text) if _SUFFIX.search(text) else text)


def _rank(path: Path) -> tuple[int, str]:
    stem = path.stem.casefold()
    return (0 if not _SUFFIX.search(stem) else 1 if any(w in stem for w in _PREFERRED) or stem.endswith("01") else 2, stem)


def match(files: Iterable[Path], games: Iterable[gm.Game]) -> Matched:
    """Pair each game with the best picture of its title. Games stored twice (regions, formats) each get it; two pictures for one game: the
    one named plainly or 'front' wins."""
    by_key: dict[str, list[Path]] = {}
    for f in files:
        if key := art_key(f.stem):
            by_key.setdefault(key, []).append(f)
    out, used = Matched(), set()
    for g in games:
        keys = [art_key(g.name)]
        if g.path:
            keys.append(art_key(Path(g.path).stem))
        hit = next((by_key[k] for k in keys if k and k in by_key), None)
        if hit:
            best = min(hit, key=_rank)
            out.pairs.append((g, best))
            used.add(best)
    out.unmatched = [f for fs in by_key.values() if not any(f in used for f in fs) for f in fs]  # (a spare picture of a game that got one isn't unmatched)
    return out


def apply(pairs: list[tuple[gm.Game, Path]], on_progress: Callable[[int, int], None] | None = None, stop=None) -> tuple[list[str], list[str]]:
    """Save each picture as its game's cover. Returns (ids of the games that got one, a line for each that did not and why)."""
    done: list[str] = []
    failed: list[str] = []
    cache: dict[Path, bytes] = {}
    for i, (g, path) in enumerate(pairs, 1):
        if stop is not None and stop.is_set():
            break
        try:
            if path not in cache:
                if path.stat().st_size > rc.MAX_ART_BYTES:
                    raise rc.RecompError(f"too big ({path.stat().st_size / (1 << 20):.1f} MB)")
                cache.clear()  # (the same picture often serves a few versions of one game, one at a time)
                cache[path] = path.read_bytes()
            rc.set_cover(g.id, cache[path])
            done.append(g.id)
        except (OSError, rc.RecompError) as exc:
            failed.append(f"{path.name}: {exc}")
        if on_progress is not None:
            on_progress(i, len(pairs))
    return done, failed
