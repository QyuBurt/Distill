"""Organisation tools: multi-disc grouping, leftovers, duplicates, playlist repair and name tidying,
with preview, apply and undo."""
from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import shutil
import threading
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .constants import APP_DIR, ILLEGAL_NAME_CHARS
from . import bios as biosmod, cuebin, patcher
from .datverify import load_dat, verify as dat_verify
from .fingerprint import find_same_content
from .frontends import PRESET_NONE, folder_for
from .formats import associated_files
from .library import LibraryEntry
from .models import fmt_size
from .onegr import DEFAULT_PRIORITY, choose as onegr_choose, parse_priority
from .system import file_size
from .systems import (
    detect_system, leading_hint, region_of,
)


ORGANIZE_EXTENSIONS = frozenset({".chd", ".cue", ".gdi", ".iso", ".cso", ".zso", ".rvz", ".gcm", ".wbfs", ".gcz",
                                 ".wia", ".cdi", ".toc", ".nrg", ".pbp"})


SIDECAR_EXTENSIONS = frozenset({".sbi", ".lsd", ".sub", ".ppf"})  # small per-disc files that travel with the disc


# "(Disc 1)", "(Disc 2 of 3)", "[CD2]", "(Disk 1)" - or the same unbracketed ("Game Disc 1").
DISC_RE = re.compile(
    r"[(\[]\s*(?:disc|disk|cd)\s*[-_ ]?(\d{1,2})(?:\s*(?:of|/)\s*(\d{1,2}))?\s*[)\]]"
    r"|(?<![A-Za-z0-9])(?:disc|disk|cd)\s*[-_ ]?(\d{1,2})(?:\s*of\s*(\d{1,2}))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)


# "(Disc 1 - Leon)" -> "(Disc 1) (Leon)": a label inside the marker is treated like one after it.
MARKER_LABEL_RE = re.compile(r"([(\[]\s*(?:disc|disk|cd)\s*[-_ ]?\d{1,2}(?:\s*(?:of|/)\s*\d{1,2})?)\s*[-,:]\s*"
                             r"([^()\[\]]+?)\s*([)\]])", re.IGNORECASE)


PAREN_TAG_RE = re.compile(r"\s*\(([^()]*)\)")


_REGION_WORDS = (r"usa|europe|japan|world|asia|korea|china|taiwan|hong kong|brazil|australia|canada|france|germany|spain|"
                 r"italy|uk|netherlands|sweden|denmark|norway|finland|russia|mexico|argentina|scandinavia|latin america|"
                 r"portugal|poland|greece|new zealand|india|israel|south africa|turkey|ntsc|pal|ntsc-u|ntsc-j|pal-e")


_LANGUAGE_CODES = (r"en|fr|de|es|it|nl|pt|sv|no|da|fi|ja|ko|zh|ru|pl|ca|cs|hu|tr|el|ar|he|en-gb|en-us|pt-br|"
                   r"zh-hans|zh-hant")


STANDARD_TAG_RE = re.compile(rf"^(?:{_REGION_WORDS}|{_LANGUAGE_CODES}|rev\s*[0-9a-z.]+|v\s*\d[\d.]*[a-z]?|beta\s*\d*|"
                             r"proto(?:type)?\s*\d*|demo|sample|unl|alt\s*\d*|pirate|bios|track\s*\d+)$", re.IGNORECASE)


def is_standard_tag(content: str) -> bool:
    """True for the kind of tag that says WHICH release a file is (region, language, revision, beta...): \"USA\",
    \"En,Fr,De\", \"Rev 1\". Anything else in brackets is free text: a disc's label (\"Leon\"), an edition name..."""
    parts = [p for p in re.split(r"\s*[,+/]\s*", content.strip()) if p]
    return bool(parts) and all(STANDARD_TAG_RE.match(p) for p in parts)


def parse_disc(stem: str) -> tuple[str, int, int | None] | None:
    """('Game (USA)', 2, 3) for 'Game (USA) (Disc 2 of 3)'; None when the name has no disc marker.

    Region, revision and language tags are kept, so a USA disc 1 is never paired with a Europe disc 2. A free-text
    label straight after the marker is the disc's own title, not the game's, and is dropped: 'Resident Evil 2 (Disc 1)
    (Leon)' and '... (Disc 2)(Claire)' are both 'Resident Evil 2'.
    """
    stem = MARKER_LABEL_RE.sub(r"\1\3 (\2)", stem)
    matches = list(DISC_RE.finditer(stem))
    if not matches:
        return None
    m = matches[-1]
    number = int(m[1] or m[3])
    total = m[2] or m[4]
    rest = stem[m.end():]
    while (label := PAREN_TAG_RE.match(rest)) and not is_standard_tag(label[1]):
        rest = rest[label.end():]
    base = re.sub(r"\s+", " ", f"{stem[:m.start()]} {rest}").strip(" -_.,")
    return (base, number, int(total) if total else None) if base and number >= 1 else None


ANY_TAG_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]")


ROMAN_RE = re.compile(r"^(?:i{1,3}|iv|vi{0,3}|ix|xi{0,3}|xiv|xv|xvi{0,3}|xix|xx)$")


def title_core(stem: str) -> tuple[str, int | None]:
    """(the game's title boiled down for comparing, its disc number or None): no tags, punctuation, case or leading
    'The', so 'Legend of Zelda, The (USA) [!]' and 'The Legend of Zelda' compare equal."""
    parsed = parse_disc(stem)
    base, number = (parsed[0], parsed[1]) if parsed else (stem, None)
    text = ANY_TAG_RE.sub("", base)
    if m := ARTICLE_RE.match(text):
        text = f"{m[2]} {m[1]}{m[3]}"
    words = re.sub(r"[^\w\s]", " ", text.replace("_", " ").replace("&", " and ").casefold()).split()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words), number


def release_tags(name: str) -> tuple[str, ...]:
    """The region / language / revision tags of a name, normalised: two files only belong together if these agree."""
    return tuple(sorted(t.casefold().strip() for t in re.findall(r"[(\[]([^)\]]*)[)\]]", name) if is_standard_tag(t)))


def similar_titles(a: str, b: str) -> bool:
    """Same game, spelled a little differently? Similarity alone can't tell 'Final Fantasy VII' from 'VIII' (0.97),
    so the numbers in the two titles must match exactly: a sequel is never a spelling variant."""
    if a == b:
        return True
    numerals = lambda t: [w for w in t.split() if w.isdigit() or ROMAN_RE.match(w)]  # noqa: E731
    if numerals(a) != numerals(b):
        return False
    if SequenceMatcher(None, a, b).ratio() >= 0.8:
        return True
    ta, tb = a.split(), b.split()
    shared = 0
    while shared < min(len(ta), len(tb)) and ta[shared] == tb[shared]:
        shared += 1
    if shared == min(len(ta), len(tb)):  # one is the other plus a word or two ("... Dual Shock")
        return shared > 0 and max(len(ta), len(tb)) - shared <= 2
    # The same multi-word title followed by different short labels ("Big Story Hero" / "Big Story Villain").
    return shared >= 2 and shared * 2 >= max(len(ta), len(tb)) and len(ta) - shared <= 2 and len(tb) - shared <= 2


def _common_title(bases: list[str]) -> str:
    """The shared start of several spellings of one title, plus the release tags (region...) they agree on."""
    prefix: list[str] = []
    for words in zip(*(b.split() for b in bases)):
        if len({w.casefold() for w in words}) != 1:
            break
        prefix.append(words[0])
    name = " ".join(prefix).strip(" -_.,")
    name = ANY_TAG_RE.sub("", name).strip(" -_.,") if "(" in name or "[" in name else name
    if len(name) < 3:
        return min(bases, key=len)
    tags = [t for t in re.findall(r"[(\[][^)\]]*[)\]]", bases[0]) if is_standard_tag(t[1:-1])]
    return " ".join([name, *tags])


def folder_name_for(base: str) -> str:
    return ILLEGAL_NAME_CHARS.sub("", base).strip(" .")


@dataclass(eq=False, slots=True)
class DiscSet:
    parent: Path  # folder the discs are in now
    name: str  # folder (and playlist) name
    discs: dict[int, Path]  # disc number -> its main file (.chd / .cue / .gdi / .iso ...)
    target: Path  # folder the set ends up in
    moves: list[tuple[Path, Path]] = field(default_factory=list)  # (from, to) for every file that moves
    playlist: Path | None = None  # None: the playlist already exists
    problem: str = ""  # why this set can't be organized (the row is shown but can't be ticked)
    candidates: dict[int, list[Path]] = field(default_factory=dict)  # every file found for each disc number
    expected: int = 0  # how many discs the game has (highest number seen, or the "of N" in the names)
    extras: dict[int, int] = field(default_factory=dict)  # disc number -> track / sidecar files that travel with it
    loose: bool = False  # paired by similar names rather than identical ones: shown, but not ticked

    @property
    def note(self) -> str:
        if self.problem:
            return self.problem
        text = "Ready" if self.moves else "Already in its folder - playlist only"
        return text + "  |  similar names: check the discs" if self.loose else text


def _disc_files(main: Path) -> list[Path]:
    """The disc's main file plus the track files and sidecars that must stay with it."""
    files = sorted(associated_files(main) - {main})
    files += [p for p in sorted(main.parent.iterdir())
              if p.is_file() and p != main and p.stem == main.stem and p.suffix.lower() in SIDECAR_EXTENSIONS]
    return [main, *dict.fromkeys(files)]


def _is_incomplete(entries: list[tuple[int, int | None, Path, str]]) -> bool:
    numbers = {e[0] for e in entries}
    expected = max(max(numbers), max((e[1] or 0 for e in entries), default=0))
    return expected < 2 or numbers != set(range(1, expected + 1))


def _merge_loose(groups: dict[tuple[Path, str], list]) -> tuple[dict[tuple[Path, str], list], set[tuple[Path, str]]]:
    """Join, within a folder, groups that can't be complete sets on their own but look like parts of one game: no disc
    number in common, the same region / revision tags, and titles that are similar (see similar_titles)."""
    by_folder: dict[Path, list[tuple[Path, str]]] = {}
    for key, entries in groups.items():
        if _is_incomplete(entries):
            by_folder.setdefault(key[0], []).append(key)
    merged, loose = dict(groups), set()
    for folder, keys in by_folder.items():
        parent = {k: k for k in keys}

        def root_of(k):
            while parent[k] != k:
                k = parent[k]
            return k

        info = {k: (title_core(groups[k][0][3])[0], release_tags(groups[k][0][3]), {e[0] for e in groups[k]}) for k in keys}
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                (ta, tags_a, nums_a), (tb, tags_b, nums_b) = info[a], info[b]
                if tags_a == tags_b and not nums_a & nums_b and similar_titles(ta, tb):
                    parent[root_of(b)] = root_of(a)
        clusters: dict[tuple[Path, str], list] = {}
        for k in keys:
            clusters.setdefault(root_of(k), []).append(k)
        for members in clusters.values():
            if len(members) < 2:
                continue
            entries = [e for k in members for e in groups[k]]
            if len({e[0] for e in entries}) != len(entries):
                continue  # two files for one disc number: leave the strict result alone
            name = _common_title([e[3] for e in entries])
            for k in members:
                del merged[k]
            new_key = (folder, name.casefold())
            if new_key in merged:
                continue
            merged[new_key] = [(n, t, p, name) for n, t, p, _b in entries]
            loose.add(new_key)
    return merged, loose


def scan_multidisc(root: Path, recursive: bool, stop: threading.Event, loose: bool = False) -> list[DiscSet]:
    """Find multi-disc games under root. Nothing is moved; the result is the plan. With `loose`, discs whose names are
    only similar are paired too (flagged on the set so the caller can leave them unticked)."""
    groups: dict[tuple[Path, str], list[tuple[int, int | None, Path, str]]] = {}
    for folder, _subdirs, filenames in _walk(root, recursive, stop):
        mains, _owned = _disc_mains(folder, filenames)
        for path in mains:  # track files belonging to a .cue / .gdi are not discs of their own
            if (parsed := parse_disc(path.stem)) is not None:
                base, number, total = parsed
                groups.setdefault((folder, base.casefold()), []).append((number, total, path, base))

    loose_keys: set[tuple[Path, str]] = set()
    if loose:
        groups, loose_keys = _merge_loose(groups)
    sets, claimed = [], set()
    for (folder, _key), entries in sorted(groups.items(), key=lambda kv: (str(kv[0][0]).lower(), kv[0][1])):
        numbers = sorted({e[0] for e in entries})
        declared = max((e[1] or 0 for e in entries), default=0)
        if len(numbers) < 2 and declared < 2:
            continue  # a lone "Disc 1" is just a single-disc game
        name = folder_name_for(entries[0][3])
        if not name:
            continue
        by_number: dict[int, list[Path]] = {}
        for number, _t, path, _b in entries:
            by_number.setdefault(number, []).append(path)
        target = folder if folder.name.casefold() == name.casefold() else folder / name
        expected = max(numbers[-1], declared)
        dset = DiscSet(folder, name, {n: ps[0] for n, ps in by_number.items()}, target,
                       candidates=by_number, expected=expected, loose=(folder, _key) in loose_keys)

        missing = [n for n in range(1, expected + 1) if n not in by_number]
        dupes = [n for n, ps in by_number.items() if len(ps) > 1]
        key = (folder, name.casefold())
        if missing:
            dset.problem = "Missing disc " + ", ".join(map(str, missing)) + f" of {expected} - left alone"
        elif dupes:
            dset.problem = f"Disc {dupes[0]} has more than one file ({', '.join(p.name for p in by_number[dupes[0]])})"
        elif key in claimed:
            dset.problem = "Another set in this folder maps to the same folder name"
        claimed.add(key)

        if not dset.problem:
            if target.exists() and not target.is_dir():
                dset.problem = f"A file named '{name}' is in the way"
            for number in sorted(dset.discs):
                files = _disc_files(dset.discs[number])
                dset.extras[number] = len(files) - 1
                for f in files:
                    dest = target / f.relative_to(folder)
                    if dest == f:
                        continue
                    if dest.exists():
                        dset.problem = dset.problem or f"'{dest.name}' already exists in the target folder"
                    dset.moves.append((f, dest))
            playlist = target / f"{name}.m3u"
            if not playlist.exists():
                dset.playlist = playlist
            elif not dset.moves:
                continue  # already organized: folder, discs and playlist are all there
        sets.append(dset)
    return sets


# ---- Generic findings: what every Organize tool previews and applies ---------------------------------------
HOLDING_DIR = "_Distill_removed"  # removed files wait here (under the scanned folder) instead of being deleted


LEGACY_HOLDING_DIRS = ("_SquashUI_removed", "_qbSquash_removed")  # left by earlier versions: still skipped when scanning


COMPRESSED_EXTENSIONS = frozenset({".chd", ".rvz", ".cso", ".zso", ".gcz", ".wia"})


SOURCE_EXTENSIONS = frozenset({".cue", ".gdi", ".iso", ".nrg", ".cdi", ".toc", ".gcm"})


SINGLE_FILE_EXTENSIONS = frozenset({".chd", ".iso", ".cso", ".zso", ".rvz", ".gcm", ".wbfs", ".gcz", ".wia",
                                    ".cdi", ".nrg", ".pbp"})  # disc images that are one file (safe to rename)


# First bytes of each compressed format: proof a "converted copy" is real before its source is offered for removal.
IMAGE_MAGIC = {".chd": b"MComprHD", ".rvz": b"RVZ\x01", ".cso": b"CISO", ".zso": b"ZISO",
               ".gcz": b"\x01\xc0\x0b\xb1", ".wia": b"WIA\x01"}


TRACK_RE = re.compile(r"\(Track \d+\)", re.IGNORECASE)


ARTICLE_RE = re.compile(r"^(.*?),\s+(The|A|An)(?=\s*[(\[-]|$)(.*)$")


BRACKET_TAG_RE = re.compile(r"\s*\[([^\]]*)\]")


DISC_TAG_RE = re.compile(r"\s*\[\s*(?:disc|disk|cd)\s*[-_ ]?\d", re.IGNORECASE)


DUPE_POLICIES = ("Prefer compressed (CHD / RVZ / CSO), then newest", "Keep the newest", "Keep the largest",
                 "Keep the smallest")


@dataclass(eq=False, slots=True)
class Finding:
    """One row of an Organize preview: what would change, and in ops exactly how."""
    title: str
    count: str = ""
    note: str = "Ready"
    children: list[tuple[str, str, str]] = field(default_factory=list)  # (name, count column, note) per detail row
    ops: list[tuple] = field(default_factory=list)  # ("mkdir"|"move"|"write"|"replace_line"|"rmdir"|"delete", ...)
    problem: str = ""  # why it can't be applied (the row is shown but can't be ticked)
    checked: bool = True  # ticked by default
    size: int = 0  # bytes it frees, where that applies
    data: list = field(default_factory=list)  # rows a report exports (LibraryEntry for the library report)
    subject: object = None  # the thing this row is about, where the page needs it (the ROM for the patch page)

    @property
    def status(self) -> str:
        return self.problem or self.note


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _m3u_key(line: str) -> str:
    return line.strip().replace("\\", "/").removeprefix("./").casefold()


def _read_text(path: Path) -> str:
    # surrogateescape keeps playlists in a legacy code page byte-for-byte intact when they are written back
    return path.read_text(encoding="utf-8-sig", errors="surrogateescape")


def _write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8", errors="surrogateescape", newline="\n")
    os.replace(tmp, path)


def _walk(root: Path, recursive: bool, stop: threading.Event):
    """(folder, subfolder names, file names) for root and, if recursive, everything below it. The holding
    folder of removed files is never entered."""
    for dirpath, dirnames, filenames in os.walk(root):
        if stop.is_set():
            return
        subdirs = list(dirnames)
        if recursive:
            dirnames[:] = [d for d in dirnames if d not in (HOLDING_DIR, *LEGACY_HOLDING_DIRS)]
        else:
            dirnames.clear()
        yield Path(dirpath), subdirs, filenames


def _disc_mains(folder: Path, filenames: list[str]) -> tuple[list[Path], set[Path]]:
    """The disc images in a folder, leaving out track files that belong to a .cue / .gdi (and those track files)."""
    present = [folder / n for n in filenames if Path(n).suffix.lower() in ORGANIZE_EXTENSIONS]
    owned = {t for p in present if p.suffix.lower() in (".cue", ".gdi") for t in associated_files(p) - {p}}
    return [p for p in present if p not in owned], owned


def _removal_ops(files: list[Path], root: Path, permanent: bool) -> list[tuple]:
    """Move files into the holding folder (undoable), or delete them outright."""
    ops: list[tuple] = []
    for f in files:
        if permanent:
            ops.append(("delete", str(f)))
            continue
        dest = root / HOLDING_DIR / _rel(f, root)
        n = 2
        while dest.exists():
            dest = dest.with_name(f"{f.stem} ({n}){f.suffix}")
            n += 1
        ops.append(("move", str(f), str(dest)))
    return ops


def _run_op(op: tuple, done: list[dict]) -> None:
    """Carry out one operation, recording what is needed to reverse it in done."""
    kind = op[0]
    if kind == "mkdir":
        d, chain = Path(op[1]), []
        while not d.exists():
            chain.append(d)
            d = d.parent
        for d in reversed(chain):
            d.mkdir()
            done.append({"op": "mkdir", "path": str(d)})
    elif kind == "move":
        src, dst = Path(op[1]), Path(op[2])
        if dst.exists():
            raise FileExistsError(f"{dst.name} already exists")
        _run_op(("mkdir", str(dst.parent)), done)
        shutil.move(str(src), str(dst))
        done.append({"op": "move", "src": str(src), "dst": str(dst)})
    elif kind == "write":
        path = Path(op[1])
        old = _read_text(path) if path.exists() else None
        _write_text(path, op[2])
        done.append({"op": "write", "path": str(path), "old": old})
    elif kind == "replace_line":  # in a playlist: the line naming op[2] now names op[3]
        path = Path(op[1])
        if not path.exists():
            return
        old = _read_text(path)
        lines = old.split("\n")
        hits = [i for i, line in enumerate(lines) if _m3u_key(line) == _m3u_key(op[2])]
        if hits:
            for i in hits:
                lines[i] = op[3]
            _write_text(path, "\n".join(lines))
            done.append({"op": "write", "path": str(path), "old": old})
    elif kind == "patchchain":  # ("patchchain", rom, out, patch1, patch2, ...)
        patcher.apply_chain(Path(op[1]), [Path(p) for p in op[3:]], Path(op[2]))
        done.append({"op": "made", "paths": [op[2]]})
    elif kind in ("convert", "patch"):  # creates new files from existing ones; undo deletes what it made
        out_dir = Path(op[3]) if kind == "convert" else Path(op[3]).parent
        before = {p.name for p in out_dir.iterdir()} if out_dir.is_dir() else set()
        try:
            if kind == "patch":
                patcher.apply_patch(Path(op[1]), Path(op[2]), Path(op[3]))
            elif op[1] == "merge":
                cuebin.merge_cue(Path(op[2]), out_dir / Path(op[2]).name)
            elif op[1] == "split":
                cuebin.split_cue(Path(op[2]), out_dir)
            else:
                cuebin.ccd_to_cue(Path(op[2]), out_dir)
        except Exception:
            for p in (out_dir.iterdir() if out_dir.is_dir() else ()):
                if p.name not in before:
                    p.unlink(missing_ok=True)
            raise
        made = [str(p) for p in out_dir.iterdir() if p.name not in before]
        done.append({"op": "made", "paths": made})
    elif kind == "rmdir":
        Path(op[1]).rmdir()
        done.append({"op": "rmdir", "path": op[1]})
    elif kind == "delete":
        Path(op[1]).unlink()  # permanent: nothing to record
    else:
        raise ValueError(f"unknown operation {kind!r}")


def _undo_ops(done: list[dict]) -> list[str]:
    """Reverse recorded operations, newest first. Returns what couldn't be put back."""
    problems: list[str] = []
    for e in reversed(done):
        try:
            match e["op"]:
                case "move":
                    src, dst = Path(e["src"]), Path(e["dst"])
                    if not dst.exists():
                        problems.append(f"{dst.name} is no longer there")
                    elif src.exists():
                        problems.append(f"{src.name}: something else is at the original location")
                    else:
                        src.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dst), str(src))
                case "write":
                    path = Path(e["path"])
                    if e["old"] is None:
                        path.unlink(missing_ok=True)
                    else:
                        _write_text(path, e["old"])
                case "made":
                    for made in e["paths"]:
                        Path(made).unlink(missing_ok=True)
                case "mkdir":
                    with contextlib.suppress(OSError):
                        Path(e["path"]).rmdir()  # only succeeds when empty
                case "rmdir":
                    Path(e["path"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"{Path(e.get('path') or e.get('dst') or '?').name}: {exc}")
    return problems


def undo_file(tool: str) -> Path:
    return APP_DIR / f"distill_undo_{tool}.json"


UNDO_RUNS_KEPT = 20  # how many runs of each tool can be undone, newest first


def run_log_file() -> Path:
    return APP_DIR / "distill_organise_log.txt"


def _load_runs(tool: str) -> list[dict]:
    """The tool's recorded runs, oldest first. Reads the single-run file older versions wrote too."""
    try:
        data = json.loads(undo_file(tool).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict) and isinstance(data.get("runs"), list):
        return [r for r in data["runs"] if isinstance(r, dict) and isinstance(r.get("items"), list)]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [{"made": data.get("made", ""), "items": data["items"]}]
    return []


def undo_depth(tool: str) -> int:
    """How many runs of this tool can still be undone."""
    return len(_load_runs(tool))


def _save_undo(tool: str, items: list[dict], made: str) -> None:
    runs = [r for r in _load_runs(tool) if r.get("made") != made]
    runs.append({"made": made, "items": items})
    try:
        undo_file(tool).write_text(json.dumps({"runs": runs[-UNDO_RUNS_KEPT:]}, indent=1), encoding="utf-8")
    except OSError:
        pass  # the undo record is a convenience; never fail the run because of it


def op_description(op: tuple) -> tuple[str, str, str]:
    """(action, from, to) in words for one planned operation, for reports and the run log."""
    kind = op[0]
    if kind == "move":
        same_folder = Path(op[1]).parent == Path(op[2]).parent
        return ("Rename" if same_folder else "Move"), op[1], op[2]
    if kind == "mkdir":
        return "Create folder", "", op[1]
    if kind == "write":
        return "Write playlist", "", op[1]
    if kind == "replace_line":
        return "Update playlist", op[1], f"{op[2]} -> {op[3]}"
    if kind == "convert":
        return {"merge": "Merge tracks into one BIN", "split": "Split into one BIN per track"}.get(
            op[1], "Convert to BIN/CUE"), op[2], op[3]
    if kind == "patch":
        return "Apply patch", op[2], op[3]
    if kind == "patchchain":
        return "Apply patches in order", " + ".join(Path(p).name for p in op[3:]), op[2]
    if kind == "rmdir":
        return "Remove folder", "", op[1]
    if kind == "delete":
        return "Delete permanently", op[1], ""
    return kind, "", ""


def _append_run_log(title: str, made: str, done: list[Finding]) -> None:
    """Keep a readable record of every run next to the app (what moved where), whatever happens to the undo files."""
    if not done:
        return
    lines = [f"=== {made.replace('T', ' ')[:19]}  {title}: {len(done)} item(s) ==="]
    for finding in done:
        lines.append(f"* {finding.title}")
        for op in finding.ops:
            action, src, dst = op_description(op)
            lines.append(f"    {action.upper():<18} {src + '  ->  ' + dst if src and dst else src or dst}")
    try:
        with run_log_file().open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
    except OSError:
        pass


def report_rows(tool_title: str, findings: list[Finding], checked: set[int]) -> list[list[str]]:
    """A preview as rows (what would happen, to which file) for saving before anything is touched."""
    rows = [["Tool", "Item", "Ticked", "Status", "Action", "From", "To"]]
    for i, f in enumerate(findings):
        state = "Blocked" if f.problem else "Yes" if i in checked else "No"
        if not f.ops:
            rows.append([tool_title, f.title, state, f.status, "", "", ""])
        for op in f.ops:
            rows.append([tool_title, f.title, state, f.status, *op_description(op)])
    return rows


def save_report(path: Path, rows: list[list[str]]) -> None:
    """Write report rows as a spreadsheet-friendly .csv, or as plain readable text for any other extension."""
    if path.suffix.lower() == ".csv":
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(rows)
        return
    lines: list[str] = []
    last = None
    for tool, item, state, status, action, src, dst in rows[1:]:
        if item != last:
            lines += ["", f"[{state}] {item}  -  {status}"]
            last = item
        if action:
            lines.append(f"    {action}: {src + '  ->  ' + dst if src and dst else src or dst}")
    path.write_text(f"{rows[1][0] if len(rows) > 1 else ''} preview\n" + "\n".join(lines) + "\n", encoding="utf-8")


def apply_findings(tool: str, findings: list[Finding], log: Callable[[str], None],
                   progress: Callable[[int, int], None], stop: threading.Event,
                   on_result: Callable[[int, str], None] | None = None, title: str = "") -> tuple[int, list[tuple[str, str]]]:
    """Apply each finding's operations. A finding that fails part-way is put back. Every change is recorded in the
    tool's undo history after each finding, so a run can be reversed, and written to the readable run log."""
    items: list[dict] = []
    applied: list[Finding] = []
    failures: list[tuple[str, str]] = []
    made = datetime.now().isoformat(timespec="microseconds")
    ok = 0
    for n, finding in enumerate(findings, 1):
        if stop.is_set():
            break
        done: list[dict] = []
        if on_result:
            on_result(n - 1, "Working...")
        try:
            for op in finding.ops:
                _run_op(op, done)
            ok += 1
            applied.append(finding)
            log(f"Done: {finding.title}")
            if on_result:
                on_result(n - 1, "Done")
        except Exception as exc:
            _undo_ops(done)
            done = []
            failures.append((finding.title, str(exc) or type(exc).__name__))
            log(f"FAILED: {finding.title} - {exc} (put back)")
            if on_result:
                on_result(n - 1, f"Failed: {exc or type(exc).__name__} (put back)")
        if done:
            items.append({"name": finding.title, "ops": done})
            _save_undo(tool, items, made)
        progress(n, len(findings))
    _append_run_log(title or tool, made, applied)
    return ok, failures


def undo_last(tool: str) -> tuple[int, list[str]] | None:
    """(items restored, problems) for the tool's newest run; None when there is nothing to undo. Each call steps one run
    further back."""
    runs = _load_runs(tool)
    if not runs:
        return None
    items = [i for i in runs[-1].get("items", []) if isinstance(i, dict)]
    problems: list[str] = []
    for item in reversed(items):
        problems += _undo_ops(item.get("ops", []))
    if len(runs) > 1:
        with contextlib.suppress(OSError):
            undo_file(tool).write_text(json.dumps({"runs": runs[:-1]}, indent=1), encoding="utf-8")
    else:
        undo_file(tool).unlink(missing_ok=True)
    return len(items), problems


# ---- Tool 1: multi-disc games --------------------------------------------------------------------------------
def discset_finding(d: DiscSet) -> Finding:
    kids: list[tuple[str, str, str]] = []
    for number in range(1, d.expected + 1):
        files = d.candidates.get(number)
        if not files:
            kids.append(("(not found)", f"Disc {number}", "Missing"))
            continue
        for path in files:
            extra = d.extras.get(number, 0)
            kids.append((path.name, f"Disc {number}",
                         "Duplicate" if len(files) > 1 else f"+{extra} track / extra file(s)" if extra else ""))
    ops: list[tuple] = []
    if not d.problem:
        if not d.target.exists():
            ops.append(("mkdir", str(d.target)))
        ops += [("move", str(a), str(b)) for a, b in d.moves]
        if d.playlist is not None:
            ops.append(("write", str(d.playlist), "\n".join(d.discs[n].name for n in sorted(d.discs)) + "\n"))
    where = d.name if d.target != d.parent else f"{d.name}  (in place)"
    return Finding(where, str(len(d.discs)), d.note, kids, ops, d.problem, checked=not d.loose)


def find_multidisc(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    found = [discset_finding(d) for d in scan_multidisc(root, recursive, stop, bool(opts.get("loose")))]
    missing = [f for f in found if f.problem.startswith("Missing disc")]
    if opts.get("only_missing"):
        found = missing
    notes = []
    if any(not f.checked for f in found):
        notes.append("similar-name matches are left unticked")
    if missing:
        notes.append(f"{len(missing)} game(s) are missing discs")
    return found, "; ".join(notes)


# ---- Tool 2: clean up leftovers ------------------------------------------------------------------------------
def _looks_converted(path: Path) -> bool:
    """A compressed image that is plausibly complete: big enough and starting with its format's header."""
    magic = IMAGE_MAGIC.get(path.suffix.lower())
    try:
        if path.stat().st_size < 1024:
            return False
        if magic is None:
            return True
        with path.open("rb") as f:
            return f.read(len(magic)) == magic
    except OSError:
        return False


def find_leftovers(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    permanent = bool(opts.get("permanent"))
    out: list[Finding] = []
    for folder, subdirs, names in _walk(root, recursive, stop):
        files = [folder / n for n in names]
        mains, owned = _disc_mains(folder, names)
        compressed: dict[str, list[Path]] = {}
        for p in files:
            if p.suffix.lower() in COMPRESSED_EXTENSIONS:
                compressed.setdefault(p.stem.casefold(), []).append(p)
        for main in sorted(mains):
            if main.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            twins = [t for t in compressed.get(main.stem.casefold(), []) if _looks_converted(t)]
            if not twins:
                continue
            group = _disc_files(main)
            size = sum(file_size(f) for f in group)
            out.append(Finding(main.name, str(len(group)), f"Converted copy: {twins[0].name}  |  frees {fmt_size(size)}",
                               [(f.name, fmt_size(file_size(f)), "") for f in group],
                               _removal_ops(group, root, permanent), size=size))
        for p in files:
            if p.name.lower().endswith(".part"):
                out.append(Finding(p.name, "1", "Leftover from an interrupted run", [],
                                   _removal_ops([p], root, permanent), size=file_size(p)))
            elif p.suffix.lower() in (".bin", ".img", ".wav") and TRACK_RE.search(p.stem) and p not in owned:
                out.append(Finding(p.name, "1", "Track file that no .cue in this folder uses", [],
                                   _removal_ops([p], root, permanent), checked=False, size=file_size(p)))
        if folder != root and not names and not subdirs:
            out.append(Finding(_rel(folder, root), "", "Empty folder", [], [("rmdir", str(folder))]))
    out.sort(key=lambda f: f.title.lower())
    return out, ""


# ---- Tool 3: duplicate games ---------------------------------------------------------------------------------
MATCH_MODES = ("Same name", "Same title, ignoring region / version tags", "Same contents (size, then checksum)")


# Game files that aren't disc images (cartridge ROMs, PC games, archives). Save files and patches are left out on purpose.
OTHER_GAME_EXTENSIONS = frozenset({
    ".gba", ".gbc", ".gb", ".nes", ".fds", ".sfc", ".smc", ".n64", ".z64", ".v64", ".nds", ".dsi", ".3ds", ".cia",
    ".gen", ".md", ".smd", ".sms", ".gg", ".pce", ".a26", ".a78", ".lnx", ".ws", ".wsc", ".ngp", ".ngc", ".vb",
    ".xci", ".nsp", ".wad", ".dol", ".elf", ".exe", ".zip", ".7z", ".rar"})


def _duplicate_candidates(root: Path, recursive: bool, stop: threading.Event, other: bool) -> list[Path]:
    found: list[Path] = []
    for folder, _subdirs, names in _walk(root, recursive, stop):
        found += _disc_mains(folder, names)[0]
        if other:
            found += [folder / n for n in names if Path(n).suffix.lower() in OTHER_GAME_EXTENSIONS]
    return found


def find_duplicates(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    policy = opts.get("keep") or DUPE_POLICIES[0]
    permanent = bool(opts.get("permanent"))
    mode = opts.get("match") or MATCH_MODES[0]
    other = bool(opts.get("other_files"))
    note = opts.get("_note") or (lambda _text: None)  # the page's live status line, when there is one
    candidates = _duplicate_candidates(root, recursive, stop, other)

    skipped = 0
    groups: dict[str, list[Path]] = {}
    if mode == MATCH_MODES[2]:
        singles = [p for p in candidates if p.suffix.lower() not in (".cue", ".gdi")]  # a sheet doesn't hold the data
        skipped = len(candidates) - len(singles)
        note(f"Looking at the sizes of {len(singles)} file(s)...")
        for n, members in enumerate(find_same_content(singles, stop, note)):
            groups[f"#{n}"] = members
    else:
        for p in candidates:
            key = re.sub(r"\s+", " ", p.stem).strip().casefold()
            if mode == MATCH_MODES[1]:
                core, number = title_core(p.stem)
                key = f"{core} #{number}" if number else core  # Disc 1 and Disc 2 are not copies of each other
            groups.setdefault(key, []).append(p)

    out: list[Finding] = []
    for _key, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        discs_only = all(m.suffix.lower() in ORGANIZE_EXTENSIONS for m in members)
        if mode == MATCH_MODES[0] and discs_only and len({m.parent for m in members}) < 2:
            continue  # copies side by side in one folder are the Clean up tool's job
        files = {m: _disc_files(m) for m in members}
        sizes = {m: sum(file_size(f) for f in files[m]) for m in members}

        def rank(p: Path) -> tuple:
            if policy == "Keep the newest":
                return (_mtime(p),)
            if policy == "Keep the largest":
                return (sizes[p],)
            if policy == "Keep the smallest":
                return (-sizes[p],)
            return (p.suffix.lower() in COMPRESSED_EXTENSIONS, _mtime(p))

        keeper = max(members, key=rank)
        losers = [m for m in members if m is not keeper]
        freed = sum(sizes[m] for m in losers)
        kids = [(_rel(m, root), fmt_size(sizes[m]), "KEEP" if m is keeper else "Remove")
                for m in [keeper, *sorted(losers)]]
        ops = _removal_ops([f for m in losers for f in files[m]], root, permanent)
        if mode == MATCH_MODES[2]:
            why, ticked = "identical contents", True  # proven by checksum, whatever the files are called
        elif mode == MATCH_MODES[1]:
            why, ticked = "similar titles - check these are the same release", False
        elif not discs_only:
            why, ticked = "same name, not necessarily the same file - check", False
        else:
            why, ticked = "", True
        extra = f"  |  {why}" if why else ""
        out.append(Finding(keeper.stem, str(len(members)), f"Keeping {_rel(keeper, root)}  |  frees {fmt_size(freed)}{extra}",
                           kids, ops, checked=ticked, size=freed))
    how = {MATCH_MODES[0]: "matched by name, not by contents",
           MATCH_MODES[1]: "matched by title; these are left unticked for you to check",
           MATCH_MODES[2]: "matched by contents" + (f"; {skipped} .cue / .gdi sheet(s) skipped" if skipped else "")}[mode]
    return out, how + ("; ROMs and other game files included, left unticked" if other and mode != MATCH_MODES[2] else "")


# ---- Tool 3b: one game, one ROM (1G1R) ---------------------------------------------------------------------------
def find_onegr(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    """Group releases of one title (any region or revision) and keep the best by region priority; the rest are moved
    aside. Disc numbers are never mixed up: Disc 1 and Disc 2 are separate titles."""
    priority = parse_priority(str(opts.get("regions") or ", ".join(DEFAULT_PRIORITY)))
    permanent = bool(opts.get("permanent"))
    groups: dict[tuple[str, str, int | None], list[Path]] = {}
    for p in _duplicate_candidates(root, recursive, stop, bool(opts.get("other_files", True))):
        core, number = title_core(p.stem)
        if core:
            groups.setdefault((str(p.parent), core, number), []).append(p)
    out: list[Finding] = []
    for (_folder, core, _n), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        by_stem: dict[str, list[Path]] = {}
        for m in members:
            by_stem.setdefault(m.stem, []).append(m)
        keep_stem = onegr_choose(list(by_stem), priority)
        keeper = max(by_stem[keep_stem], key=lambda p: (p.suffix.lower() in COMPRESSED_EXTENSIONS, _mtime(p)))
        losers = [m for m in members if m is not keeper]
        files = {m: _disc_files(m) if m.suffix.lower() in ORGANIZE_EXTENSIONS else [m] for m in members}
        freed = sum(file_size(f) for m in losers for f in files[m])
        kids = [(_rel(m, root), fmt_size(sum(file_size(f) for f in files[m])), "KEEP" if m is keeper else "Move aside")
                for m in [keeper, *sorted(losers)]]
        ops = _removal_ops([f for m in losers for f in files[m]], root, permanent)
        out.append(Finding(keeper.stem, str(len(members)), f"Keeping {_rel(keeper, root)}  |  frees {fmt_size(freed)}",
                           kids, ops, size=freed))
    return out, f"region priority: {', '.join(priority)}"


# ---- Tool 4: playlist repair ---------------------------------------------------------------------------------
def find_playlist_fixes(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    add_missing = bool(opts.get("add_missing", True))
    by_name: dict[str, list[Path]] = {}
    by_stem: dict[str, list[Path]] = {}
    in_folder: dict[Path, list[Path]] = {}
    lists: list[Path] = []
    for folder, _subdirs, names in _walk(root, recursive, stop):
        for n in names:
            p = folder / n
            if p.suffix.lower() == ".m3u":
                lists.append(p)
            elif p.suffix.lower() in ORGANIZE_EXTENSIONS:
                by_name.setdefault(n.casefold(), []).append(p)
                by_stem.setdefault(p.stem.casefold(), []).append(p)
                in_folder.setdefault(folder, []).append(p)

    def best(cands: list[Path]) -> Path | None:
        if len(cands) == 1:
            return cands[0]
        compressed = [c for c in cands if c.suffix.lower() in COMPRESSED_EXTENSIONS]
        return compressed[0] if len(compressed) == 1 else None

    out: list[Finding] = []
    for m3u in sorted(lists, key=lambda p: str(p).lower()):
        try:
            lines = _read_text(m3u).split("\n")
        except OSError:
            continue
        comments = [ln.strip() for ln in lines if ln.strip().startswith("#")]
        entries = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        if not entries:
            continue
        kids: list[tuple[str, str, str]] = []
        final: list[str] = []
        seen: set[str] = set()
        changed, unresolved = False, 0
        for entry in entries:
            p = Path(entry.replace("\\", "/"))
            path = p if p.is_absolute() else m3u.parent / p
            if path.exists():
                new, note, target = entry, "OK", path
                if p.is_absolute():
                    with contextlib.suppress(ValueError):
                        new = path.relative_to(m3u.parent).as_posix()
                        note = "Made relative"
            else:
                same_folder = [c for c in by_stem.get(path.stem.casefold(), []) if c.parent == path.parent]
                target = (best(same_folder) or best(by_name.get(path.name.casefold(), []))
                          or best(by_stem.get(path.stem.casefold(), [])))
                if target:
                    new = os.path.relpath(target, m3u.parent).replace("\\", "/")
                    note = f"Now {new}"
                else:
                    new, note = entry, "Missing"
                    unresolved += 1
            key = str(target).casefold() if target else _m3u_key(entry)
            if key in seen:
                kids.append((entry, "", "Duplicate - removed"))
                changed = True
                continue
            seen.add(key)
            changed |= new != entry
            final.append(new)
            kids.append((entry, "", note))
        if add_missing:
            parsed = [parse_disc(Path(x).stem) for x in final]
            if parsed and all(parsed) and len({q[0].casefold() for q in parsed}) == 1:
                base, have, added = parsed[0][0].casefold(), {q[1] for q in parsed}, False
                for cand in sorted(in_folder.get(m3u.parent, []),
                                   key=lambda c: (c.suffix.lower() not in COMPRESSED_EXTENSIONS, c.name.lower())):
                    pc = parse_disc(cand.stem)
                    if pc and pc[0].casefold() == base and pc[1] not in have:
                        final.append(cand.name)
                        have.add(pc[1])
                        kids.append((cand.name, "", "Added (was missing from the playlist)"))
                        added = changed = True
                if added:
                    final.sort(key=lambda x: parse_disc(Path(x).stem)[1])
        title = _rel(m3u, root)
        if changed:
            note = "Will be repaired" + (f"  |  {unresolved} still missing" if unresolved else "")
            out.append(Finding(title, str(len(final)), note, kids,
                               [("write", str(m3u), "\n".join([*comments, *final]) + "\n")]))
        elif unresolved:
            out.append(Finding(title, str(len(final)), "", kids, [],
                               problem=f"{unresolved} entr{'y' if unresolved == 1 else 'ies'} point to files that "
                                       "couldn't be found anywhere in this folder"))
    return out, ""


# ---- Tool 5: tidy filenames ----------------------------------------------------------------------------------
TEMPLATE_TOKENS = ("title", "region", "tags", "disc", "name", "system")
_TOKEN_RE = re.compile(r"\{(" + "|".join(TEMPLATE_TOKENS) + r")\}", re.IGNORECASE)
_EMPTY_GROUP_RE = re.compile(r"\s*(?:\(\s*\)|\[\s*\])")


def apply_template(name: str, template: str, system: str = "") -> str:
    """Build a file name from a pattern such as '{title} ({region}) {disc}'. The name is split into its title, region,
    disc marker and remaining tags; a part the name doesn't have comes out empty and takes its empty brackets with it."""
    work = name
    disc = ""
    if matches := list(DISC_RE.finditer(work)):
        m = matches[-1]
        total = m[2] or m[4]
        disc = f"(Disc {int(m[1] or m[3])}" + (f" of {int(total)})" if total else ")")
        work = f"{work[:m.start()]} {work[m.end():]}"
    region = region_of(work)
    tags = [t for t in re.findall(r"[(\[][^)\]]*[)\]]", work) if t[1:-1].strip() != region]
    title = re.sub(r"\s+", " ", ANY_TAG_RE.sub("", work)).strip(" -_.,")
    parts = {"title": title, "region": region, "tags": " ".join(tags), "disc": disc, "name": name, "system": system}
    built = _TOKEN_RE.sub(lambda m: parts[m.group(1).lower()], template)
    built = _EMPTY_GROUP_RE.sub("", built)
    return re.sub(r"\s+", " ", ILLEGAL_NAME_CHARS.sub("", built)).strip(" -_.,")


def tidy_name(stem: str, opts: dict, system: str = "") -> str:
    """The tidied form of a file name (without its extension); stem itself when nothing needs changing."""
    name = stem
    if opts.get("spaces", True):
        name = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
    if opts.get("discs", True) and (matches := list(DISC_RE.finditer(name))):
        m = matches[-1]
        total = m[2] or m[4]
        canon = f"(Disc {int(m[1] or m[3])}" + (f" of {int(total)})" if total else ")")
        if m.group(0) != canon:
            name = f"{name[:m.start()]}{canon}{name[m.end():]}"
    if opts.get("articles", False) and (m := ARTICLE_RE.match(name)):
        name = f"{m[2]} {m[1]}{m[3]}"
    if opts.get("tags", False):
        name = BRACKET_TAG_RE.sub(lambda m: m.group(0) if DISC_TAG_RE.match(m.group(0)) else "", name)
    if template := str(opts.get("template") or "").strip():
        name = apply_template(name, template, system) or name
    if name == stem:
        return stem
    return re.sub(r"\s+", " ", name).strip(" -_.,") or stem


def find_tidy_renames(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    template = str(opts.get("template") or "").strip()
    if template and not _TOKEN_RE.search(template):
        raise ValueError("The rename pattern needs at least one of " + " ".join("{" + t + "}" for t in TEMPLATE_TOKENS))
    wants_system = bool(re.search(r"\{system\}", template, re.IGNORECASE))
    out: list[Finding] = []
    claimed: dict[tuple[Path, str], str] = {}
    skipped = 0
    for folder, _subdirs, names in _walk(root, recursive, stop):
        mains = sorted(_disc_mains(folder, names)[0])
        if not mains:
            continue
        taken = {n.casefold() for n in names}
        listed: dict[Path, set[str]] = {}
        for n in names:
            if n.lower().endswith(".m3u"):
                with contextlib.suppress(OSError):
                    listed[folder / n] = {_m3u_key(ln) for ln in _read_text(folder / n).split("\n")}
        for main in mains:
            new_stem = tidy_name(main.stem, opts, detect_system(main, root) if wants_system else "")
            if new_stem.casefold() == main.stem.casefold():
                continue  # unchanged, or only the letter case differs (not worth the rename)
            if main.suffix.lower() not in SINGLE_FILE_EXTENSIONS:
                skipped += 1
                continue
            files = [main] + [folder / n for n in names if Path(n).stem == main.stem
                              and Path(n).suffix.lower() in SIDECAR_EXTENSIONS]
            ops: list[tuple] = []
            kids: list[tuple[str, str, str]] = []
            problem = ""
            for f in files:
                dest_name = new_stem + f.suffix
                if dest_name.casefold() in taken:
                    problem = problem or f"'{dest_name}' already exists"
                if (other := claimed.get((folder, dest_name.casefold()))) is not None:
                    problem = problem or f"Same new name as {other}"
                claimed[(folder, dest_name.casefold())] = f.name
                ops.append(("move", str(f), str(folder / dest_name)))
                kids.append((f"{f.name}  ->  {dest_name}", "", ""))
            for m3u, keys in listed.items():
                if _m3u_key(main.name) in keys:
                    ops.append(("replace_line", str(m3u), main.name, new_stem + main.suffix))
                    kids.append((f"Playlist {m3u.name} is updated to match", "", ""))
            out.append(Finding(main.name, str(len(files)), f"->  {new_stem + main.suffix}", kids, ops, problem))
    out.sort(key=lambda f: f.title.lower())
    return out, (f"{skipped} .cue / .gdi / .toc set(s) skipped (they name their track files inside)" if skipped else "")


# ---- Tool 6: sort by system ----------------------------------------------------------------------------------
UNKNOWN_CHOICES = ("Leave them where they are", "Move them to an 'Unknown system' folder")
UNKNOWN_FOLDER = "Unknown system"
NOT_SORTED_EXTENSIONS = frozenset({".zip", ".7z", ".rar", ".exe", ".elf", ".dol"})  # can't be placed by system
SORT_EXTENSIONS = (ORGANIZE_EXTENSIONS | OTHER_GAME_EXTENSIONS | {".pkg", ".zar"}) - NOT_SORTED_EXTENSIONS
LIBRARY_EXTENSIONS = ORGANIZE_EXTENSIONS | OTHER_GAME_EXTENSIONS | {".pkg", ".zar"}


def _is_game_file(path: Path, allowed: frozenset[str]) -> bool:
    ext = path.suffix.lower()
    if ext not in allowed:
        return False
    if ext == ".md":  # Mega Drive ROMs are 128 KB and up; a smaller .md is a text file
        return file_size(path) >= 65536
    return True


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def find_by_system(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    """One finding per system: its files, and where each goes ('<destination>/<System>/<same sub-path as now>').
    Disc sets (.cue + tracks) and playlists travel with their discs; sub-folders under a system folder are kept so a
    multi-disc game's folder stays intact. Files already in a folder named for their system stay where they are."""
    dest_text = str(opts.get("dest") or "").strip()
    dest_root = Path(dest_text) if dest_text else root  # no folder given: sort inside the scanned folder
    unknown_moves = opts.get("unknown") == UNKNOWN_CHOICES[1]
    preset = opts.get("preset") or PRESET_NONE
    note = opts.get("_note")
    moves: dict[str, list[tuple[Path, Path]]] = {}
    unknown: list[Path] = []
    taken: dict[str, Path] = {}  # destination (case-folded) -> the file that claimed it
    already = clashes = looked = 0
    skip_dest = dest_root != root and _inside(dest_root, root)

    def place(system: str, files: list[Path], main: Path) -> None:
        nonlocal already, clashes
        if system and dest_root == root and leading_hint(main, root) == system:
            already += 1  # sits under a folder named for its system (PS2/..., Dreamcast/...)
            return
        rel_parent = main.parent.relative_to(root)
        if leading_hint(main, root) or (rel_parent.parts and rel_parent.parts[0].casefold() == UNKNOWN_FOLDER.casefold()):
            rel_parent = Path(*rel_parent.parts[1:])  # the system folder replaces a wrong or leftover one
        base = dest_root / folder_for(preset, system or UNKNOWN_FOLDER) / rel_parent
        for f in files:
            target = base / (f.relative_to(main.parent) if _inside(f, main.parent) else Path(f.name))
            if target == f:
                already += 1
                continue
            key = str(target).casefold()
            if target.exists() or key in taken:
                clashes += 1
                continue
            taken[key] = f
            moves.setdefault(system, []).append((f, target))

    for folder, _subdirs, names in _walk(root, recursive, stop):
        if skip_dest and _inside(folder, dest_root):
            continue
        mains, _owned = _disc_mains(folder, names)
        mains = [m for m in mains if _is_game_file(m, SORT_EXTENSIONS)]
        mains += [folder / n for n in names if Path(n).suffix.lower() not in ORGANIZE_EXTENSIONS
                  and _is_game_file(folder / n, SORT_EXTENSIONS)]
        system_of: dict[str, str] = {}
        for main in mains:
            looked += 1
            if note and looked % 25 == 0:
                note(f"Reading files... {looked} checked")
            system = detect_system(main, root)
            system_of[_m3u_key(main.name)] = system
            if system or unknown_moves:
                place(system, _disc_files(main) if main.suffix.lower() in ORGANIZE_EXTENSIONS else [main], main)
            else:
                unknown.append(main)
        for n in names:  # a playlist goes wherever all its discs go
            if not n.lower().endswith(".m3u"):
                continue
            listed = {system_of[k] for ln in _read_text(folder / n).split("\n") if (k := _m3u_key(ln)) in system_of}
            if len(listed) == 1 and (system := next(iter(listed))):
                place(system, [folder / n], folder / n)
    findings: list[Finding] = []
    for system in sorted(moves, key=lambda x: (not x, x.casefold())):
        kids = [(f"{src.relative_to(root)}  ->  {dst.relative_to(dest_root) if _inside(dst, dest_root) else dst}", "", "")
                for src, dst in moves[system]]
        label = system or UNKNOWN_FOLDER
        findings.append(Finding(label, str(len(moves[system])), f"->  {folder_for(preset, label)}{os.sep}", kids,
                                [("move", str(a), str(b)) for a, b in moves[system]]))
    if unknown:
        kids = [(str(p.relative_to(root)), "", "") for p in unknown[:300]]
        findings.append(Finding(UNKNOWN_FOLDER, str(len(unknown)), "", kids, [],
                                "Couldn't tell the system (CHDs and archives need a folder named for it) - left alone"))
    bits = []
    if already:
        bits.append(f"{already} already in place")
    if clashes:
        bits.append(f"{clashes} skipped (name already taken)")
    return findings, ", ".join(bits)


def _confirm_sort(picks: list[Finding], opts: dict) -> str:
    files = sum(len(f.ops) for f in picks)
    where = str(opts.get("dest") or "").strip() or "the scanned folder"
    return (f"{files} file(s) will be moved into per-system folders in {where}.\n\n"
            "Undo last run puts every one back. Empty folders left behind can be removed with Remove extras.\n\nContinue?")


# ---- Tool 7: library report ----------------------------------------------------------------------------------
def find_library(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    """One finding per system, holding a LibraryEntry per game. Nothing is changed; the page exports what's ticked."""
    note = opts.get("_note")
    entries: list[LibraryEntry] = []
    for folder, _subdirs, names in _walk(root, recursive, stop):
        mains, _owned = _disc_mains(folder, names)
        mains = [m for m in mains if _is_game_file(m, LIBRARY_EXTENSIONS)]
        mains += [folder / n for n in names if Path(n).suffix.lower() not in ORGANIZE_EXTENSIONS
                  and _is_game_file(folder / n, LIBRARY_EXTENSIONS)]
        for main in mains:
            files = _disc_files(main) if main.suffix.lower() in ORGANIZE_EXTENSIONS else [main]
            entries.append(LibraryEntry(
                main.name, detect_system(main, root) or UNKNOWN_FOLDER, main.suffix.lstrip(".").upper(),
                region_of(main.stem), sum(file_size(f) for f in files), max(_mtime(f) for f in files),
                "" if folder == root else str(folder.relative_to(root)), str(main)))
            if note and len(entries) % 25 == 0:
                note(f"Reading files... {len(entries)} found")
    by_system: dict[str, list[LibraryEntry]] = {}
    for e in entries:
        by_system.setdefault(e.system, []).append(e)
    findings = []
    for system in sorted(by_system, key=lambda x: (x == UNKNOWN_FOLDER, x.casefold())):
        group = sorted(by_system[system], key=lambda e: e.name.casefold())
        kids = [(e.name, e.kind, fmt_size(e.size)) for e in group[:500]]
        if len(group) > 500:
            kids.append((f"... and {len(group) - 500} more (all are in the report)", "", ""))
        findings.append(Finding(system, str(len(group)), fmt_size(sum(e.size for e in group)), kids, [], data=group))
    total = sum(e.size for e in entries)
    return findings, f"{len(entries)} file(s), {fmt_size(total)} in all" if entries else ""


# ---- Tool: disc repair (BIN/CUE merge, split, CloneCD) ---------------------------------------------------------
DISC_ACTIONS = ("Merge multi-file BIN/CUE into one BIN", "Split one BIN into a file per track",
                "Convert CloneCD (.ccd / .img) to BIN/CUE")


def find_discfix(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    action = opts.get("action") or DISC_ACTIONS[0]
    replace = bool(opts.get("replace", True))
    kind = {DISC_ACTIONS[0]: "merge", DISC_ACTIONS[1]: "split", DISC_ACTIONS[2]: "ccd"}[action]
    out: list[Finding] = []
    skipped = 0
    for folder, _subdirs, names in _walk(root, recursive, stop):
        for n in sorted(names):
            src, ext = folder / n, Path(n).suffix.lower()
            try:
                if kind == "ccd" and ext == ".ccd":
                    originals = [p for p in (src, src.with_suffix(".img"), src.with_suffix(".sub")) if p.exists()]
                    made = [f"{src.stem}.bin", f"{src.stem}.cue"]
                elif kind != "ccd" and ext == ".cue":
                    tracks = cuebin.parse_cue(src)
                    refs = list(dict.fromkeys(t.file for t in tracks))
                    if (kind == "merge") != (len(refs) > 1):
                        continue
                    originals = [src, *(folder / r for r in refs)]
                    if kind == "merge":
                        made = [src.name, f"{src.stem}.bin"]
                    else:
                        width = len(str(len(tracks)))
                        made = [src.name, *(f"{Path(refs[0]).stem} (Track {t.number:0{width}d}).bin" for t in tracks)]
                else:
                    continue
            except (cuebin.CueError, OSError):
                skipped += 1
                continue
            missing = [p.name for p in originals if not p.exists()]
            final = folder if replace else folder / f"{src.stem} (converted)"
            clash = [m for m in made if (final / m).exists() and not (replace and final / m in originals)]
            problem = (f"{', '.join(missing)} missing" if missing else
                       f"{clash[0]} already exists" if clash else "")
            tmp = folder / f".distill_tmp_{src.stem}"
            ops: list[tuple] = []
            if not problem:
                ops = [("mkdir", str(tmp)), ("convert", kind, str(src), str(tmp))]
                if replace:
                    ops += _removal_ops(originals, root, False)
                ops += [("move", str(tmp / m), str(final / m)) for m in made]
                ops.append(("rmdir", str(tmp)))
            kids = [(p.name, "", "Original" + (" (moved aside)" if replace else "")) for p in originals] + \
                   [(m, "", "New") for m in made]
            freed = 0
            out.append(Finding(src.stem, str(len(originals)), action.split(" ")[0] + ("" if replace else " (originals kept)"),
                               kids, ops, problem, size=freed))
    return out, (f"{skipped} unreadable sheet(s) skipped" if skipped else "")


# ---- Tool: apply patches -----------------------------------------------------------------------------------------
PATCHABLE_EXTENSIONS = (OTHER_GAME_EXTENSIONS - {".zip", ".7z", ".rar", ".exe", ".elf", ".dol"}) | {".iso", ".bin"}
_CRC_CACHE: dict[tuple[str, int, int], str | None] = {}


@dataclass(eq=False, slots=True)
class PatchCandidate:
    """One patch offered for one game."""
    patch: Path
    how: str  # 'checksum' | 'name' | 'only' | 'manual'
    problem: str = ""  # why it can't be applied to this game (wrong ROM, needs xdelta3 ...)
    ticked: bool = False  # ticked by default only when the checksum proves it is the right ROM

    @property
    def verified(self) -> bool:
        return self.how == "checksum" and not self.problem

    @property
    def label(self) -> str:
        return {"checksum": "Checksum matches", "name": "Matched by name (unverified)",
                "only": "Only ROM in its folder (unverified)", "manual": "Chosen by you"}[self.how]


def _file_crc(path: Path, stop: threading.Event) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key in _CRC_CACHE:
        return _CRC_CACHE[key]
    if (result := _crc32_hex(path, stop)) is not None:
        _CRC_CACHE[key] = result
    return result


def _crc32_hex(path: Path, stop: threading.Event, skip: int = 0) -> str | None:
    """CRC-32 of a file from byte `skip` on, read in chunks so a big image is never held in memory and a Stop is
    noticed between them. None if it can't be read or was stopped."""
    crc = 0
    try:
        with path.open("rb") as f:
            f.seek(skip)
            while chunk := f.read(4 << 20):
                if stop.is_set():
                    return None
                crc = zlib.crc32(chunk, crc)
    except OSError:
        return None
    return f"{crc & 0xFFFFFFFF:08x}"


def check_pair(patch: Path, rom: Path, stop: threading.Event, how: str) -> PatchCandidate:
    """Judge a patch against a ROM: UPS / BPS can be proven right or wrong; IPS and xdelta only claim to match."""
    cand = PatchCandidate(patch, how)
    if patcher.detect(patch) == "xdelta" and not patcher.xdelta_tool():
        cand.problem = "Needs the xdelta3 program on PATH"
        return cand
    if info := patcher.source_info(patch):
        size, crc = info
        have = _file_crc(rom, stop) if file_size(rom) == size else None
        if have == crc:
            cand.how = "manual" if how == "manual" else "checksum"
            cand.ticked = True
        elif file_size(rom) == size + 512 and _crc32_hex(rom, stop, skip=512) == crc:
            cand.problem = "ROM has a 512-byte copier header: remove it first"
        else:
            cand.problem = f"Wrong ROM: the patch is for checksum {crc}" + ("" if have is None else f", this one is {have}")
    if how == "manual" and not cand.problem:
        cand.ticked = True  # you chose this pair yourself
    return cand


def _name_related(rom: Path, patch: Path) -> bool:
    a, b = title_core(rom.stem)[0].split(), title_core(patch.stem)[0].split()
    return bool(a) and b[:len(a)] == a


def _refresh_patch_rows(f: Finding) -> None:
    f.count = str(len(f.data))
    f.children = [(c.patch.name, "", c.problem or c.label) for c in f.data]
    f.note = f"{sum(1 for c in f.data if c.ticked and not c.problem)} ticked" if f.data else "No patch found"


def _game_row(rom: Path, root: Path, cands: list[PatchCandidate]) -> Finding:
    f = Finding(rom.name, "", "", [], [], subject=rom, data=cands)
    _refresh_patch_rows(f)
    return f


UNMATCHED_TITLE = "Patches without a game"


def attach_patch(findings: list[Finding], patch: Path, rom: Path, stop: threading.Event) -> None:
    """Pair a patch with a ROM by hand: it leaves any game or the unmatched group it was under, and goes under `rom`."""
    for f in findings:
        before = len(f.data)
        f.data = [c for c in f.data if c.patch != patch]
        if len(f.data) != before:
            _refresh_patch_rows(f)
    cand = check_pair(patch, rom, stop, "manual")
    game = next((f for f in findings if f.subject == rom), None)
    if game is None:
        game = _game_row(rom, rom.parent, [])
        findings.insert(0, game)
    game.data.append(cand)
    _refresh_patch_rows(game)
    findings[:] = [f for f in findings if not (f.title == UNMATCHED_TITLE and not f.data)]


def detach_patch(findings: list[Finding], patch: Path) -> None:
    """Take a patch off its game; it goes to the unmatched group."""
    for f in findings:
        if any(c.patch == patch for c in f.data) and f.title != UNMATCHED_TITLE:
            f.data = [c for c in f.data if c.patch != patch]
            _refresh_patch_rows(f)
    group = next((f for f in findings if f.title == UNMATCHED_TITLE), None)
    if group is None:
        group = Finding(UNMATCHED_TITLE, "", "", [], [], "Right-click a patch to choose its game")
        findings.append(group)
    group.data.append(PatchCandidate(patch, "manual"))
    _refresh_patch_rows(group)
    group.note = ""


def find_patch_games(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    """Games that have a patch available, each listing its patches. UPS / BPS patches are matched by checksum and ticked;
    IPS and xdelta by name (left unticked). Patches nothing matched are grouped at the bottom, to match by hand."""
    note = opts.get("_note") or (lambda _t: None)
    show_all = bool(opts.get("show_all", False))
    roms: list[Path] = []
    patches: list[Path] = []
    archive_rows: list[Finding] = []
    for folder, _subdirs, names in _walk(root, recursive, stop):
        for n in sorted(names):
            ext, path = Path(n).suffix.lower(), folder / n
            if ext in patcher.PATCH_EXTENSIONS:
                patches.append(path)
            elif ext in PATCHABLE_EXTENSIONS and not (ext == ".md" and file_size(path) < 65536):
                roms.append(path)
            elif ext == ".zip":  # a translation is often downloaded as an archive holding the patch
                with contextlib.suppress(Exception), zipfile.ZipFile(path) as z:
                    inside = [i for i in z.namelist() if Path(i).suffix.lower() in patcher.PATCH_EXTENSIONS]
                    if inside:
                        archive_rows.append(Finding(n, "", "", [(i, "", "") for i in inside], [],
                                                    "The patch is inside this archive: extract it first", checked=False))
    by_rom: dict[Path, list[PatchCandidate]] = {}
    loose: list[Path] = []
    manual = {Path(p): r for p, r in opts.get("_manual", [])}  # patch -> the ROM chosen for it ('' = deliberately none)
    for n, patch in enumerate(patches, 1):
        if stop.is_set():
            break
        note(f"Matching patch {n} of {len(patches)}: {patch.name}")
        if patch in manual:
            if manual[patch]:
                by_rom.setdefault(Path(manual[patch]), []).append(check_pair(patch, Path(manual[patch]), stop, "manual"))
            else:
                loose.append(patch)
            continue
        info = patcher.source_info(patch)
        hits = [r for r in roms if info and file_size(r) == info[0] and _file_crc(r, stop) == info[1]]
        how = "checksum"
        if not hits:
            hits = [r for r in roms if _name_related(r, patch)][:5]
            how = "name"
        if not hits:
            same = [r for r in roms if r.parent == patch.parent]
            hits, how = (same, "only") if len(same) == 1 else ([], "")
        if not hits:
            loose.append(patch)
        for r in hits:
            by_rom.setdefault(r, []).append(check_pair(patch, r, stop, how))
    out = [_game_row(r, root, c) for r, c in sorted(by_rom.items(), key=lambda kv: kv[0].name.casefold())]
    if show_all:
        have = set(by_rom)
        out += [_game_row(r, root, []) for r in sorted(roms, key=lambda r: r.name.casefold()) if r not in have]
        out.sort(key=lambda f: f.title.casefold())
    if loose:
        group = Finding(UNMATCHED_TITLE, "", "", [], [], "Right-click a patch to choose its game",
                        data=[PatchCandidate(p, "manual") for p in loose])
        _refresh_patch_rows(group)
        group.note = ""
        out.append(group)
    out += archive_rows
    msg = f"{len(roms)} ROM(s) and {len(patches)} patch file(s) looked at"
    if not patches and not archive_rows:
        msg = (f"No patch files (.ips .ups .bps .xdelta) found; {len(roms)} ROM(s) looked at. "
               "Patches inside .7z / .rar archives aren't detected: extract them first.")
    return out, msg


def name_key(rom: Path, patches: list[Path]) -> tuple[str, ...]:
    """Identifies one output (a ROM and the patches that make it) for a name the user chose."""
    return (str(rom), *map(str, patches))


def clean_output_name(rom: Path, text: str) -> str:
    """A file name from what the user typed: illegal characters dropped, the ROM's extension kept or added."""
    name = ILLEGAL_NAME_CHARS.sub("", text.strip()).strip(" .")
    if not name:
        return ""
    return name if name.lower().endswith(rom.suffix.lower()) else name + rom.suffix


def plan_patches(rom: Path, patches: list[Path], stack: bool, taken: set[str] | None = None,
                 names: dict | None = None) -> list[tuple[Path, list[Path]]]:
    """(output file, patches applied to make it): one output holding them all in order when stacking, else one per patch.
    `names` maps name_key(...) to a name the user chose. Names never collide with files on disk or with each other."""
    taken = taken if taken is not None else set()
    groups = [patches] if stack else [[p] for p in patches]
    plans = []
    for g in groups:
        custom = clean_output_name(rom, (names or {}).get(name_key(rom, g), ""))
        out = rom.with_name(custom) if custom else patcher.output_name(rom, g)
        n = 2
        while str(out).casefold() in taken or (custom and out.exists()):
            stem = Path(custom).stem if custom else f"{rom.stem} ({' + '.join(p.stem for p in g)})"
            out = out.with_name(f"{stem} ({n}){rom.suffix}")
            n += 1
        taken.add(str(out).casefold())
        plans.append((out, g))
    return plans


def patch_findings(findings: list[Finding], ticks: set[tuple[int, int]], stack: bool,
                   names: dict | None = None) -> list[Finding]:
    """What applying the ticked patches does, as ordinary findings (so apply, undo and the run log all work)."""
    out: list[Finding] = []
    taken: set[str] = set()
    for i, f in enumerate(findings):
        if not isinstance(f.subject, Path):
            continue
        picked = [c.patch for j, c in enumerate(f.data) if (i, j) in ticks and not c.problem]
        for dest, ps in plan_patches(f.subject, picked, stack, taken, names) if picked else []:
            ops = [("patch", str(f.subject), str(ps[0]), str(dest))] if len(ps) == 1 else \
                  [("patchchain", str(f.subject), str(dest), *map(str, ps))]
            out.append(Finding(dest.name, str(len(ps)), f"{f.subject.name}  +  " + ", ".join(p.name for p in ps), [], ops))
    return out


# ---- Tool 8: DAT verification ------------------------------------------------------------------------------
def find_dat_verify(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    """Hash files against a No-Intro / Redump / TOSEC DAT. Renames to the canonical names are offered (ticked);
    bad, unknown and missing files are reported (nothing to apply). CHD files can't be hashed without extracting."""
    dat_text = str(opts.get("dat") or "").strip().strip('"')
    if not dat_text:
        return [], ""  # no DAT given: the step has nothing to do and the page moves on to the report
    try:
        dat = load_dat(Path(dat_text))
    except (OSError, ValueError) as e:
        return [Finding("DAT file", "", problem=str(e))], str(e)
    note = opts.get("_note") or (lambda _t: None)
    files: list[Path] = []
    chd = 0
    for folder, _subdirs, names in _walk(root, recursive, stop):
        for n in names:
            ext = Path(n).suffix.lower()
            if ext == ".chd":
                chd += 1
            elif ext in OTHER_GAME_EXTENSIONS or ext in (".bin", ".iso", ".img", ".zip"):
                files.append(folder / n)
    rep = dat_verify(files, dat, stop, note)
    out: list[Finding] = []
    renames, taken = [], set()
    for v in rep.ok:
        if v.renamed:
            dst = v.path.with_name(v.canonical)
            if dst.exists() or str(dst).casefold() in taken:
                continue
            taken.add(str(dst).casefold())
            renames.append((v, dst))
    if renames:
        kids = [(f"{v.path.name}  ->  {dst.name}", "", "") for v, dst in renames]
        out.append(Finding("Rename to canonical names", str(len(renames)), "Matched by hash", kids,
                           [("move", str(v.path), str(dst)) for v, dst in renames]))
    for title, items in (("Bad: named like a game, contents differ", [(p.name, g.name) for p, g in rep.bad]),
                         ("Not in the DAT", [(p.name, "") for p in rep.unknown]),
                         ("Unreadable", [(p.name, "") for p in rep.unreadable])):
        if items:
            out.append(Finding(title, str(len(items)), "", [(a if not b else f"{a}  (expected {b})", "", "") for a, b in items[:500]],
                               [], "Report only", checked=False))
    if rep.incomplete:
        kids = [(f"{g.name}: missing {', '.join(r.name for r in miss)}", "", "") for g, miss in rep.incomplete[:500]]
        out.append(Finding("Incomplete games", str(len(rep.incomplete)), "", kids, [], "Report only", checked=False))
    if rep.missing:
        kids = [(g.name, "", "") for g in rep.missing[:500]]
        out.append(Finding("Missing from your library", str(len(rep.missing)), "", kids, [], "Report only", checked=False))
    summary = f"{len(rep.ok)} of {len(files)} file(s) verified against {dat.name} ({len(dat.games)} games)"
    if chd:
        summary += f"; {chd} CHD skipped (extract to check)"
    return out, summary


# ---- Tool: BIOS check ------------------------------------------------------------------------------------------------
def find_bios(root: Path, recursive: bool, opts: dict, stop: threading.Event) -> tuple[list[Finding], str]:
    """Check an emulator BIOS folder (default: the scanned folder) against a JSON hash list. Report only."""
    db_text = str(opts.get("biosdb") or "").strip().strip('"')
    if not db_text:
        return [], ""  # no list given: the step has nothing to do and the page moves on
    try:
        db = biosmod.load_db(Path(db_text))
    except ValueError as e:
        return [Finding("BIOS list", "", problem=str(e))], str(e)
    folder_text = str(opts.get("biosdir") or "").strip().strip('"')
    results = biosmod.check(Path(folder_text) if folder_text else root, db, stop)
    by_system: dict[str, list] = {}
    for r in results:
        by_system.setdefault(r.system, []).append(r)
    out: list[Finding] = []
    bad_total = 0
    for system, rows in by_system.items():
        ok = sum(r.status == "ok" for r in rows)
        trouble = [r for r in rows if r.status != "ok" and (r.required or r.status == "bad")]
        bad_total += len(trouble)
        kids = [(r.file, "Required" if r.required else "Optional",
                 {"ok": "OK", "missing": "Missing", "bad": "Wrong checksum"}[r.status] + (f"  ({r.note})" if r.note else ""))
                for r in sorted(rows, key=lambda r: (r.status == "ok", r.file))]
        out.append(Finding(system, f"{ok}/{len(rows)}", f"{len(trouble)} problem(s)" if trouble else "All good", kids, [],
                           "Report only" if trouble else "", checked=False))
    return out, f"{bad_total} problem(s) across {len(by_system)} system(s)"
