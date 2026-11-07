"""Nintendo Switch conversions, with no Qt in sight.

Switch 1 (``nscb_rust``, the Rust port of NSC_BUILDER's core):
    compress   .nsp / .xci  ->  .nsz / .xcz
    decompress .nsz / .xcz  ->  .nsp / .xci
    merge      a game, its updates and its DLC  ->  one .nsp / .xci (optionally compressed straight away)

Switch 2 (``nup``, the NX Package format for *unencrypted* dumps):
    pack       a decrypted dump folder (exefs/ + romfs/, or base/ updates/ dlc/)  ->  one .nup
               (Switch 1 .nsp/.xci/.nsz/.xcz files are decrypted first with hactoolnet and your keys)
    edit       add an icon, publisher or display version to a .nup that already exists

The pure parts (title-id grouping, ticket / NACP / CNMT readers, key-file helpers) are plain functions so they can
be tested without any tool; `SwitchRunner` drives the tools and speaks the same event protocol as `BatchRunner`.
"""
from __future__ import annotations

import itertools
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .constants import ILLEGAL_NAME_CHARS, LINE_SPLIT_RE, PERCENT_RE, SUBPROCESS_FLAGS
from .models import BatchSummary, Failure, fmt_size, path_bytes
from .system import bind_to_app_lifetime, file_size, free_bytes, tree_size

CONTAINERS = frozenset({".nsp", ".xci", ".nsz", ".xcz"})
COMPRESSED = frozenset({".nsz", ".xcz"})
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".dat")  # .dat: the icon exactly as the console distributes it
LEVEL_MIN, LEVEL_MAX, LEVEL_DEFAULT = 1, 22, 3
TITLE_ID_RE = re.compile(r"\[([0-9A-Fa-f]{16})\]")
HEX32_RE = re.compile(r"^\s*([0-9A-Fa-f]{32})\s*[=,|]\s*([0-9A-Fa-f]{32})")
LANGUAGES = (  # the order of the 16 title entries in a NACP
    "AmericanEnglish", "BritishEnglish", "Japanese", "French", "German", "LatinAmericanSpanish", "Spanish", "Italian",
    "Dutch", "CanadianFrench", "Portuguese", "Russian", "Korean", "TraditionalChinese", "SimplifiedChinese",
    "BrazilianPortuguese")
PHASE = "Converting"


# ---- pure helpers --------------------------------------------------------------------------------------------------
def title_id_of(name: str) -> str | None:
    """The 16-digit title id in a file name such as 'Game [0100633007D48000][v0].nsp', upper-cased."""
    m = TITLE_ID_RE.search(name)
    return m[1].upper() if m else None


def title_kind(title_id: str) -> str:
    """'base', 'update' or 'dlc', from the shape of a title id (a game ends 000, its update 800, DLC is anything else)."""
    tail = title_id[-3:]
    return "base" if tail == "000" else "update" if tail == "800" else "dlc"


def group_key(title_id: str) -> int:
    """What a game, its update and its DLC have in common: a DLC's id is the game's id plus 0x1000, with its own index."""
    n = int(title_id, 16) >> 12
    return n - 1 if title_kind(title_id) == "dlc" else n


def plan_groups(files: list[Path], ids: dict[Path, str] | None = None) -> tuple[list[list[Path]], list[str]]:
    """Files that belong together (one game, its updates, its DLC), and notes about the ones that can't be grouped.

    `ids` says what each file is (see `identify_files`); without it the [Title ID] in the name is used. A group needs a
    game (the base title) and at least one more piece to be worth merging; the rest are left out with a note."""
    ids = ids if ids is not None else {p: t for p in files if (t := title_id_of(p.name))}
    by_key: dict[int, list[Path]] = {}
    notes: list[str] = []
    for path in sorted(files):
        tid = ids.get(path)
        if tid is None:
            notes.append(f"{path.name}: couldn't tell which game it belongs to. Use 'Review groups...' to link it by hand.")
            continue
        by_key.setdefault(group_key(tid), []).append(path)
    groups: list[list[Path]] = []
    for key, members in sorted(by_key.items()):
        kinds = {title_kind(ids[p]) for p in members}
        if "base" not in kinds:
            notes.append(f"{members[0].name}: its game isn't here, so there is nothing to merge it into.")
        elif len(members) < 2:
            notes.append(f"{members[0].name}: no updates or DLC found for it.")
        else:
            groups.append(sorted(members, key=lambda p: (title_kind(ids[p]), p.name)))
    return groups, notes


def link_value(base_id: str) -> str:
    """The id a hand-linked file is given: a DLC-shaped id in the game's family, so it is grouped with that game as an extra."""
    return f"{(((int(base_id, 16) >> 12) + 1) << 12) | 1:016X}"


def review_files(files: list[Path], links: dict[str, str] | None = None) -> tuple[dict[Path, str], dict[str, str], list[Path]]:
    """(ids, games, unknown) for the 'Review groups' window: what each file was recognised as, the games found (base id ->
    name) a file can be linked to, and the files nothing could place."""
    ids, _notes = identify_files(files, links)
    games = {ids[p]: plain_name(p) for p in files if p in ids and title_kind(ids[p]) == "base"}
    return ids, games, [p for p in files if p not in ids]


def pfs0_files(path: Path, wanted: str, limit: int = 1 << 16) -> list[bytes]:
    """The contents of the small files ending in `wanted` inside a PFS0 container (.nsp / .nsz), read without unpacking."""
    try:
        with path.open("rb") as f:
            head = f.read(0x10)
            if len(head) < 0x10 or head[:4] != b"PFS0":
                return []
            count, strsize = struct.unpack_from("<II", head, 4)
            if count > 4096 or strsize > 1 << 20:
                return []
            table = f.read(count * 0x18 + strsize)
            data_start = 0x10 + count * 0x18 + strsize
            strings = table[count * 0x18:]
            out = []
            for i in range(count):
                offset, size, name_off = struct.unpack_from("<QQI", table, i * 0x18)
                name = strings[name_off:strings.find(b"\0", name_off)].decode("utf-8", "replace")
                if name.lower().endswith(wanted) and 0 < size <= limit:
                    f.seek(data_start + offset)
                    out.append(f.read(size))
            return out
    except (OSError, struct.error):
        return []


def content_title_id(path: Path) -> str | None:
    """The title id from inside a file, with no keys: the ticket in an .nsp / .nsz starts with it. None for .xci / .xcz and for
    files with no ticket (their ids sit in encrypted headers)."""
    if path.suffix.lower() in (".nsp", ".nsz"):
        for data in pfs0_files(path, ".tik"):
            if got := ticket_title_key(data):
                return got[0][:16]
    return None


def identify_files(files: list[Path], links: dict[str, str] | None = None) -> tuple[dict[Path, str], list[str]]:
    """What each file is: a hand-made link first, then the id inside the file, then the id in its name. Files nothing can
    place are left out of the result. The notes say where the name and the content disagree (the content wins)."""
    links, ids, notes = links or {}, {}, []
    for path in files:
        named, inside = title_id_of(path.name), content_title_id(path)
        if (linked := links.get(str(path))) is not None:
            ids[path] = linked
        elif inside:
            ids[path] = inside
            if named and named != inside:
                notes.append(f"{path.name}: the name says {named} but the file is {inside}; using the file.")
        elif named:
            ids[path] = named
    return ids, notes


def plain_name(path: Path) -> str:
    """'Game Name' from 'Game Name [0100...][v0] (Update).nsp': the title without its tags."""
    base = re.split(r"\s*[\[(]", path.stem, maxsplit=1)[0].strip()
    return clean_name(base or path.stem)


def clean_name(text: str) -> str:
    return ILLEGAL_NAME_CHARS.sub("", text).strip(" .") or "Switch Game"


def ticket_title_key(data: bytes) -> tuple[str, str] | None:
    """(rights id, encrypted title key) as hex, from a .tik file; None when it isn't one this can read."""
    if len(data) < 8:
        return None
    sig = struct.unpack_from("<I", data, 0)[0]
    sizes = {0x10000: 0x200, 0x10001: 0x100, 0x10002: 0x3C, 0x10003: 0x200, 0x10004: 0x100, 0x10005: 0x3C}
    if sig not in sizes:
        return None
    body = (4 + sizes[sig] + 0x3F) & ~0x3F  # the signature is padded to a 0x40 boundary
    if len(data) < body + 0x170:
        return None
    key, rights = data[body + 0x40:body + 0x50], data[body + 0x160:body + 0x170]
    return rights.hex().upper(), key.hex().upper()


def read_title_keys(path: str | Path) -> dict[str, str]:
    """rights id -> key from a title.keys file ('rid,key', 'rid=key' and 'rid|key|name' lines all work)."""
    keys: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if m := HEX32_RE.match(line):
                keys[m[1].upper()] = m[2].upper()
    except OSError:
        pass
    return keys


def parse_nacp(data: bytes) -> dict[str, str]:
    """{'name', 'publisher', 'version'} from control.nacp: the first language that has a title (English first)."""
    out = {"name": "", "publisher": "", "version": ""}
    if len(data) < 0x3070:
        return out
    for i in range(len(LANGUAGES)):
        name = data[i * 0x300:i * 0x300 + 0x200].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
        if name:
            out["name"] = name
            out["publisher"] = data[i * 0x300 + 0x200:i * 0x300 + 0x300].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
            break
    out["version"] = data[0x3060:0x3070].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return out


def parse_cnmt(data: bytes) -> tuple[str, int, int] | None:
    """(title id, version, type byte) from a .cnmt file: 0x80 = game, 0x81 = update, 0x82 = DLC."""
    if len(data) < 0x20:
        return None
    title_id, version = struct.unpack_from("<QI", data, 0)
    return f"{title_id:016X}", version, data[0xC]


def parse_nca_info(text: str) -> tuple[str, str]:
    """(content type, title id) from what hactoolnet prints about an NCA, '' for whatever it didn't say."""
    kind = re.search(r"Content Type:\s*(\w+)", text)
    tid = re.search(r"Title ID:\s*([0-9A-Fa-f]{16})", text)
    return (kind[1] if kind else ""), (tid[1].upper() if tid else "")


def check_prod_keys(path: str) -> str:
    """'' when the file looks like a prod.keys, else a sentence saying what is wrong with it."""
    if not path:
        return "No prod.keys is set (Settings > Tools > Switch keys)."
    p = Path(path)
    if not p.is_file():
        return f"The prod.keys file does not exist: {p}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"The prod.keys file can't be read: {exc}"
    if not re.search(r"^\s*(?:master_key_00|header_key|aes_kek_generation_source)\s*=", text, re.MULTILINE):
        return "That file doesn't look like a prod.keys (no master_key_00 or header_key line)."
    return ""


def default_key_files() -> tuple[str, str]:
    """(prod.keys, title.keys) found in the places Switch tools look: ~/.switch and beside the app. '' when not found."""
    from .constants import APP_DIR
    homes = []
    try:
        homes.append(Path.home() / ".switch")
    except RuntimeError:
        pass
    homes += [APP_DIR, APP_DIR / "keys"]
    found = []
    for name in ("prod.keys", "title.keys"):
        found.append(next((str(h / name) for h in homes if (h / name).is_file()), ""))
    return found[0], found[1]


def is_nx_dump(path: Path) -> bool:
    """A decrypted dump `nup pack` can take: exefs/ or romfs/ in it, or base/, updates/ or dlc/ holding them."""
    def has(folder: Path) -> bool:
        return (folder / "exefs").is_dir() or (folder / "romfs").is_dir()

    if not path.is_dir():
        return False
    return has(path) or has(path / "base") or any(c.is_dir() and has(c) for c in _children(path / "updates")) or (
        (path / "dlc").is_dir() and any(has(i) for t in _children(path / "dlc") for i in _children(t)))


def _children(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError:
        return []


def find_image(names: list[str], *folders: Path) -> Path | None:
    """An image called '<name>.png/.jpg/.jpeg/.dat' in one of the folders, tried name by name."""
    for name in names:
        for folder in folders:
            for ext in IMAGE_EXTENSIONS:
                for candidate in (folder / f"{name}{ext}", folder / f"{name}{ext.upper()}"):
                    if candidate.is_file():
                        return candidate
    return None


def find_python() -> str:
    """The interpreter that runs nup (it is a Python package). The running one, unless this is a packaged build."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python", "python3", "py"):
        if found := shutil.which(name):
            return found
    return ""


def nup_command(path: str) -> list[str]:
    """How to start nup: a launcher .py needs Python, a program (a pip-installed `nup`) runs as it is."""
    if path.lower().endswith(".py"):
        py = find_python()
        return [py, path] if py else []
    return [path]


# ---- a dump's title ID ---------------------------------------------------------------------------------------------
def npdm_program_id(npdm: Path) -> str | None:
    """The program ID in a main.npdm (the ACI0 block), as 16 hex digits; None when the file is missing, isn't an NPDM or says 0.
    The ACI0 block is found through the header's own offset, so the larger signature area of Switch 2 NPDMs doesn't matter."""
    try:
        with npdm.open("rb") as f:
            head = f.read(0x80)
            if len(head) < 0x78 or head[:4] != b"META":
                return None
            aci0 = int.from_bytes(head[0x70:0x74], "little")
            f.seek(aci0)
            block = f.read(0x18)
    except OSError:
        return None
    if len(block) < 0x18 or block[:4] != b"ACI0":
        return None
    value = int.from_bytes(block[0x10:0x18], "little")
    return f"{value:016X}" if value else None


def _update_folders(dump: Path) -> list[Path]:
    """updates/<version>/ folders, newest first (numeric versions by value)."""
    def key(p: Path) -> tuple[int, str]:
        return (int(p.name), p.name) if p.name.isdigit() else (-1, p.name)
    return sorted(_children(dump / "updates"), key=key, reverse=True)


def dump_title_id(dump: Path, fallback: str = "") -> tuple[str, str]:
    """(title ID, where it came from) for a decrypted dump, or ('', '').

    nup reads the ID from the base package's main.npdm and records 00000000 when the base has none (a base that is an empty
    placeholder while an update holds the whole game). The same program ID is in the update's main.npdm: an update changes
    the game's files, not its identity, so the newest update's NPDM is as good a source as the base's, and it is what other
    NUP tools do. Next come an ID the caller already knows (from a Switch 1 file's metadata), then one in the folder's name."""
    base = dump / "base" if (dump / "base").is_dir() else dump
    if tid := npdm_program_id(base / "exefs" / "main.npdm"):
        return tid, "base"
    for folder in _update_folders(dump):
        if tid := npdm_program_id(folder / "exefs" / "main.npdm"):
            return tid, f"update {folder.name}"
    if fallback and (tid := title_id_of(f"[{fallback}]")):
        return tid, "the game's metadata"
    if tid := title_id_of(dump.name):
        return tid, "the folder name"
    return "", ""


# ---- dropping a file, folder or zip onto the window --------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SwitchDrop:
    """Where something dropped on the window belongs: which side and action of the Switch page, the folder to use as the source,
    and (for one or a few files) the files to restrict the run to. `archive` is set for a zip that holds Switch files: it was
    recognised but can't be run from inside, so it has to be extracted first."""
    side: str  # "s1" | "s2"
    action: str  # compress | decompress | pack | edit
    source: Path
    only: tuple[Path, ...] = ()
    label: str = ""
    archive: bool = False


def _drop_kind(names: list[str]) -> tuple[str, str] | None:
    """(side, action) for a list of file names or archive member paths, or None when they are not Switch files."""
    exts = {Path(n).suffix.lower() for n in names}
    parts = {part.lower() for n in names for part in Path(n.replace("\\", "/")).parts}
    if ".nup" in exts:
        return "s2", "edit"
    if {"exefs", "romfs"} & parts:
        return "s2", "pack"
    if exts & {".nsp", ".xci"}:
        return "s1", "compress"
    if exts & COMPRESSED:
        return "s1", "decompress"
    return None


def classify_switch_drop(paths: list[Path]) -> SwitchDrop | None:
    """Work out what a drop is by looking at it (a file's extension, a folder's layout, a zip's contents; nothing is extracted).
    One folder, one zip, or any number of files from one folder. None when it isn't a Switch thing, or when the items disagree."""
    if not paths:
        return None
    if len(paths) == 1 and paths[0].is_dir():
        folder = paths[0]
        if is_nx_dump(folder):
            return SwitchDrop("s2", "pack", folder, (), folder.name)
        names: list[str] = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".distill_nx_")]
            names += [n for n in files if Path(n).suffix.lower() in CONTAINERS | {".nup"}]
            if len(names) >= 200:
                break
        kind = _drop_kind(names)
        return SwitchDrop(*kind, folder, (), folder.name) if kind else None
    if not all(p.is_file() for p in paths):
        return None
    if len(paths) == 1 and paths[0].suffix.lower() == ".zip":
        import zipfile
        try:
            with zipfile.ZipFile(paths[0]) as z:
                kind = _drop_kind(z.namelist())
        except (zipfile.BadZipFile, OSError):
            return None
        return SwitchDrop(*kind, paths[0].parent, (paths[0],), paths[0].name, archive=True) if kind else None
    kinds = {_drop_kind([p.name]) for p in paths}
    if len(kinds) != 1 or None in kinds or len({p.parent for p in paths}) != 1:
        return None
    side, action = next(iter(kinds))
    label = paths[0].name if len(paths) == 1 else f"{len(paths)} files"
    return SwitchDrop(side, action, paths[0].parent, tuple(paths), label)


# ---- per-title metadata (Switch 2 > Manage metadata) -------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TitleEdit:
    """What the person typed for one title; a blank field means 'leave it to the game'."""
    name: str = ""
    publisher: str = ""
    display_version: str = ""
    icon: str = ""  # path of a picture chosen by hand
    filename: str = ""  # the .nup's name, without the extension
    title_id: str = ""  # 16 hex digits, for a dump whose own ID can't be read

    def empty(self) -> bool:
        return not (self.name or self.publisher or self.display_version or self.icon or self.filename or self.title_id)


@dataclass(frozen=True, slots=True)
class TitleEntry:
    """One title the source folder holds, as the metadata list shows it. `key` is what edits are filed under: the dump folder,
    the first file of a game's group, or the .nup itself."""
    key: str
    name: str
    title_id: str
    kind: str  # "Dump" | "NSP" | "XCI" | "NSZ" | "XCZ" | "NUP"
    size: int
    filename: str  # what it is saved as, without the extension
    files: tuple[Path, ...]


def find_nup_titles(source: Path, action: str = "pack", links: dict[str, str] | None = None) -> list[TitleEntry]:
    """The titles a Switch 2 run would act on, in the order the runner makes jobs: decrypted dump folders, then Switch 1 files
    grouped by game (action 'pack'), or the .nup files already there (action 'edit'). Cheap: nothing is decrypted."""
    entries: list[TitleEntry] = []
    dumps: list[Path] = []
    files: list[Path] = []
    nups: list[Path] = []
    for root, dirs, names in os.walk(source):
        dirs[:] = [d for d in dirs if not d.startswith(".distill_nx_")]
        here = Path(root)
        if action == "pack" and is_nx_dump(here):
            dumps.append(here)
            dirs[:] = []
            continue
        for n in names:
            suffix = Path(n).suffix.lower()
            if action == "edit" and suffix == ".nup":
                nups.append(here / n)
            elif action == "pack" and suffix in CONTAINERS:
                files.append(here / n)
    for p in sorted(nups):
        entries.append(TitleEntry(str(p), p.stem, title_id_of(p.name) or "", "NUP", file_size(p), p.stem, (p,)))
    for d in sorted(dumps):
        entries.append(TitleEntry(str(d), d.name, dump_title_id(d)[0], "Dump", tree_size(d), clean_name(d.name), (d,)))
    files = sorted(f for f in files if not any(d in f.parents for d in dumps))
    ids, _notes = identify_files(files, links)
    by_key: dict[object, list[Path]] = {}
    for p in files:
        tid = ids.get(p)
        by_key.setdefault(group_key(tid) if tid else p, []).append(p)
    for members in by_key.values():
        members.sort(key=lambda p: (title_kind(ids.get(p) or "0" * 16), p.name))
        first = members[0]
        tid = next((ids[p] for p in members if p in ids and title_kind(ids[p]) == "base"), ids.get(first, ""))
        entries.append(TitleEntry(str(first), plain_name(first), tid, first.suffix.lstrip(".").upper(), sum(file_size(p) for p in members),
                                  clean_name(plain_name(first)), tuple(members)))
    return entries


# ---- what one run is told ------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SwitchConfig:
    side: str  # "s1" | "s2"
    action: str  # s1: compress | decompress | merge   s2: pack | edit
    source: Path
    dest: Path | None  # None: next to each source
    nscb: str = ""
    hactoolnet: str = ""
    nup: str = ""
    prod_keys: str = ""
    title_keys: str = ""
    level: int = LEVEL_DEFAULT
    delete: bool = False
    verify: bool = False
    temp_dir: str = ""
    merge_type: str = "nsp"  # "nsp" | "xci"
    merge_compress: bool = False
    no_delta: bool = False
    rename_first: bool = False
    art_mode: str = "game"  # "game" | "folder" | "none"
    art_dir: str = ""
    publisher: str = ""
    display_version: str = ""
    links: tuple[tuple[str, str], ...] = ()  # (file, title id) pairs chosen by hand for files that couldn't be told
    title_edits: tuple[tuple[str, TitleEdit], ...] = ()  # (TitleEntry.key, what to change) from Switch 2 > Manage metadata

    @property
    def kind(self) -> str:
        """The format name the end-of-run report uses."""
        if self.side == "s2":
            return "NUP"
        return {"compress": "NSZ/XCZ", "decompress": "NSP/XCI", "merge": "Switch"}.get(self.action, "Switch")

    @property
    def tool_name(self) -> str:
        return "nup" if self.side == "s2" else "nscb_rust"


@dataclass(eq=False, slots=True)
class _Job:
    kind: str  # "file" | "group" | "dump" | "nup"
    label: str
    sources: list[Path]
    size: int = 1
    outputs: list[Path] = field(default_factory=list)
    ok: bool = True


class _Fail(Exception):
    def __init__(self, stage: str, reason: str, retryable: bool = True):
        super().__init__(reason)
        self.stage, self.reason, self.retryable = stage, reason, retryable


class SwitchRunner:
    """Scans the source folder, then runs one tool call (or a few) per file or group, one after another.

    The tools are heavy on the disk and already use what they need, so jobs run in turn. Events go out as the
    BatchRunner's do: ("log", text), ("new", id, text), ("update", id, state, text), ("progress", phase, done, total),
    ("work", bytes done, bytes total) and finally ("done", BatchSummary)."""

    def __init__(self, cfg: SwitchConfig, emit: Callable[[tuple], None], pause_event: threading.Event,
                 only: frozenset[Path] | None = None):
        self.cfg, self._emit, self.pause_event, self._only = cfg, emit, pause_event, only
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._ids_n = itertools.count(1)
        self._ids: dict[Path, str] = {}
        self._edits: dict[str, TitleEdit] = dict(cfg.title_edits)
        self._procs: set[subprocess.Popen] = set()
        self._scratch: set[Path] = set()
        self._failures: list[Failure] = []
        self._done_bytes = self._total_bytes = 0
        self._job: _Job | None = None  # the job running now, and which one it is (for the bar)
        self._job_n = 0
        self._job_total = 0
        self._job_frac = 0.0

    # ---- events -----------------------------------------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        self._emit(("log", msg))

    def _new(self, text: str) -> int:
        tid = next(self._ids_n)
        self._emit(("new", tid, text))
        return tid

    def _set(self, tid: int, state: str, msg: str) -> None:
        self._emit(("update", tid, state, msg))

    def shutdown(self) -> None:
        """Abort: kill the tools, wake a paused run, remove scratch folders."""
        self._stop.set()
        self.pause_event.set()
        with self._lock:
            procs, scratch = list(self._procs), list(self._scratch)
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass
        for proc in procs:
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        for folder in scratch:
            shutil.rmtree(folder, ignore_errors=True)

    # ---- running a tool ---------------------------------------------------------------------------------------------
    def _popen(self, cmd: list[str], env: dict | None = None, cwd: Path | None = None) -> subprocess.Popen:
        if sys.platform != "win32" and (nice := shutil.which("nice")):
            cmd = [nice, "-n", "10", *cmd]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                creationflags=SUBPROCESS_FLAGS, env=env, cwd=cwd)
        bind_to_app_lifetime(proc)
        with self._lock:
            self._procs.add(proc)
        if self._stop.is_set():
            proc.kill()
        return proc

    def _job_pct(self, pct: float) -> None:
        """The tool is pct% through the job it is on: the bar and the ETA move with it, not only when a job ends."""
        job = self._job
        if job is None or self._job_total <= 0:
            return
        self._job_frac = max(self._job_frac, min(0.999, pct / 100))  # a job's later tool calls never move it back
        self._emit(("bar", self._job_n - 1 + self._job_frac, self._job_total))
        self._emit(("work", self._done_bytes + self._job_frac * job.size, self._total_bytes))

    def _exec(self, cmd: list[str], tid: int, label: str, state: str = "[ CONVERTING ]", env: dict | None = None,
              cwd: Path | None = None, watch: Path | None = None) -> tuple[int, str]:
        """Run a command, showing its percentage on the task's one line; returns (exit code, output tail)."""
        try:
            proc = self._popen(cmd, env, cwd)
        except OSError as exc:
            return 127, f"could not start {Path(cmd[0]).name}: {exc}"
        tail: list[str] = []
        row, latest, last = label, "", -1.0
        buf = b""
        quiet, beat = threading.Event(), None
        if watch is not None:  # a tool that prints nothing when piped still shows it is alive, by what it has written
            def pulse() -> None:
                while not quiet.wait(2.0):
                    try:
                        written = path_bytes(watch)
                    except OSError:
                        continue
                    if written and not latest:
                        self._set(tid, state, f"({fmt_size(written)} written) {row}")
            beat = threading.Thread(target=pulse, daemon=True, name="pulse")
            beat.start()
        try:
            while chunk := proc.stdout.read(8192):
                *lines, buf = LINE_SPLIT_RE.split(buf + chunk)
                for raw in lines:
                    text = raw.decode(errors="replace").strip()
                    if not text:
                        continue
                    if m := PERCENT_RE.search(raw):
                        pct = float(m[1])
                        if pct - last >= 1.0 or pct >= 99.9 > last:
                            last, row = pct, f"({pct:04.1f}%) {label}"
                            if state == "[ CONVERTING ]" or state == "[ PACKING    ]":
                                self._job_pct(pct)
                    else:
                        tail.append(text)
                        del tail[:-40]
                    latest = text[:160]
                    self._set(tid, state, row)
            if buf.strip():
                tail.append(buf.decode(errors="replace").strip())
            return proc.wait(), "\n".join(tail)
        finally:
            quiet.set()
            try:
                proc.stdout.close()
            except OSError:
                pass
            with self._lock:
                self._procs.discard(proc)

    def _checkpoint(self) -> bool:
        self.pause_event.wait()
        return not self._stop.is_set()

    def _scratch_dir(self, near: Path) -> Path:
        """A fresh working folder: in the temp folder from Performance if set, else beside the output (same drive)."""
        base = Path(self.cfg.temp_dir) if self.cfg.temp_dir else near
        base.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=".distill_nx_", dir=base))
        with self._lock:
            self._scratch.add(folder)
        return folder

    def _drop(self, folder: Path) -> None:
        shutil.rmtree(folder, ignore_errors=True)
        with self._lock:
            self._scratch.discard(folder)

    # ---- tool command lines -----------------------------------------------------------------------------------------
    def _nscb(self, *args: str) -> list[str]:
        cmd = [self.cfg.nscb, *args]
        if self.cfg.prod_keys:
            cmd += ["--keys", self.cfg.prod_keys]
        return cmd

    def _out_dir(self, src: Path) -> Path:
        return self.cfg.dest or src.parent

    def _tid(self, path: Path) -> str:
        return self._ids.get(path) or "0" * 16

    # ---- scanning ---------------------------------------------------------------------------------------------------
    def _walk(self, wanted: Callable[[Path], bool]) -> list[Path]:
        skip = self.cfg.dest.resolve() if self.cfg.dest and self.cfg.dest.exists() else None
        found: list[Path] = []
        for root, dirs, files in os.walk(self.cfg.source):
            dirs[:] = [d for d in dirs if not d.startswith(".distill_nx_")]
            if skip is not None and Path(root).resolve() == skip and skip != self.cfg.source.resolve():
                dirs[:] = []
                continue
            found += [p for p in (Path(root) / f for f in files) if wanted(p)]
        return sorted(found)

    def _scan(self) -> tuple[list[_Job], list[str]]:
        cfg, notes = self.cfg, []
        jobs: list[_Job] = []
        if cfg.side == "s1":
            if cfg.action == "merge":
                files = self._walk(lambda p: p.suffix.lower() in CONTAINERS)
                self._ids, notes = identify_files(files, dict(cfg.links))
                groups, more = plan_groups(files, self._ids)
                notes += more
                jobs = [_Job("group", f"{plain_name(g[0])} ({len(g)} files)", g, sum(file_size(p) for p in g)) for g in groups]
            else:
                want = {"compress": {".nsp", ".xci"}, "decompress": COMPRESSED}[cfg.action]
                jobs = [_Job("file", p.name, [p], file_size(p)) for p in self._walk(lambda p: p.suffix.lower() in want)]
        elif cfg.action == "edit":
            jobs = [_Job("nup", p.name, [p], file_size(p)) for p in self._walk(lambda p: p.suffix.lower() == ".nup")]
        else:
            jobs, notes = self._scan_nup_sources()
        if self._only is not None:
            jobs = [j for j in jobs if any(s in self._only for s in j.sources)]
        return jobs, notes

    def _scan_nup_sources(self) -> tuple[list[_Job], list[str]]:
        """Decrypted dump folders (one NUP each) and Switch 1 files (grouped by game, one NUP per game)."""
        jobs: list[_Job] = []
        dumps: list[Path] = []
        for root, dirs, _files in os.walk(self.cfg.source):
            dirs[:] = [d for d in dirs if not d.startswith(".distill_nx_")]
            here = Path(root)
            if is_nx_dump(here):
                dumps.append(here)
                dirs[:] = []  # a dump is one title: don't look inside it
        jobs += [_Job("dump", d.name, [d], tree_size(d)) for d in dumps]
        files = [p for p in self._walk(lambda p: p.suffix.lower() in CONTAINERS) if not any(d in p.parents for d in dumps)]
        self._ids, notes = identify_files(files, dict(self.cfg.links))
        by_key: dict[object, list[Path]] = {}
        for p in files:
            tid = self._ids.get(p)
            by_key.setdefault(group_key(tid) if tid else p, []).append(p)  # no id: the file is a game of its own
        for members in by_key.values():
            members.sort(key=lambda p: (title_kind(self._tid(p)), p.name))
            jobs.append(_Job("group", plain_name(members[0]) + (f" ({len(members)} files)" if len(members) > 1 else ""),
                             members, sum(file_size(p) for p in members)))
        return jobs, notes

    # ---- the run ----------------------------------------------------------------------------------------------------
    def run(self) -> None:
        cfg = self.cfg
        try:
            problem = self._preflight()
            if problem:
                self._log(f"ERROR: {problem}\n")
                self._emit(("done", BatchSummary(0, (), cfg.kind)))
                return
            jobs, notes = self._scan()
            for note in notes:
                self._log(f"Note: {note}")
            if not jobs:
                self._log("Nothing to do: no matching files were found in the source folder.\n")
                self._emit(("done", BatchSummary(0, (), cfg.kind)))
                return
            self._total_bytes = sum(j.size for j in jobs)
            self._log(f"Found {len(jobs)} item(s), {fmt_size(self._total_bytes)}. Tool: {cfg.tool_name}.\n")
            if cfg.rename_first and cfg.action == "merge" and cfg.side == "s1":
                self._rename_first()
                jobs, notes = self._scan()
                self._total_bytes = sum(j.size for j in jobs)
            self._emit(("progress", PHASE, 0, len(jobs)))
            self._emit(("work", 0, self._total_bytes))
            for n, job in enumerate(jobs, 1):
                if not self._checkpoint():
                    break
                self._job, self._job_n, self._job_total, self._job_frac = job, n, len(jobs), 0.0
                self._run_job(job, n, len(jobs))
                self._job = None
                self._done_bytes += job.size
                self._emit(("progress", PHASE, n, len(jobs)))
                self._emit(("work", self._done_bytes, self._total_bytes))
            self._finish(jobs)
        except Exception as exc:  # a crash here must still end the run, or the window waits forever
            self._log(f"\nERROR: {type(exc).__name__}: {exc}\n")
            self._emit(("done", BatchSummary(0, tuple(self._failures), cfg.kind)))
        finally:
            with self._lock:
                scratch = list(self._scratch)
            for folder in scratch:
                shutil.rmtree(folder, ignore_errors=True)

    def _preflight(self) -> str:
        cfg = self.cfg
        if not cfg.source.is_dir():
            return f"The source folder does not exist: {cfg.source}"
        if cfg.side == "s1":
            if not cfg.nscb or not shutil.which(cfg.nscb):
                return "nscb_rust was not found (Settings > Tools)."
            if bad := check_prod_keys(cfg.prod_keys):
                return bad
            return ""
        if not cfg.nup or not (Path(cfg.nup).is_file() or shutil.which(cfg.nup)):
            return "nup was not found (Settings > Tools)."
        if not nup_command(cfg.nup):
            return "nup is a Python program and no Python was found on this PC (install Python 3, then restart Distill)."
        return ""

    def _rename_first(self) -> None:
        tid = self._new("Naming files from their contents (nscb_rust --renamef)")
        rc, out = self._exec(self._nscb("--renamef", str(self.cfg.source), "--renmode", "skip_corr_tid"), tid, "Renaming",
                             "[ RENAMING   ]")
        self._set(tid, "[    OK      ]" if rc == 0 else "[  FAILED    ]", "Renamed files from their metadata." if rc == 0
                  else f"nscb_rust exit code {rc}: {out.splitlines()[-1] if out else ''}")

    def _run_job(self, job: _Job, n: int, total: int) -> None:
        tid = self._new(f"[  QUEUED    ] {n}/{total}  {job.label}")
        label = f"{n}/{total}  {job.label}"
        try:
            self._set(tid, "[ WORKING    ]", label)
            s1 = self.cfg.side == "s1"
            handler = (self._do_file if job.kind == "file" else self._do_merge if job.kind == "group" and s1
                       else self._do_titles if job.kind == "group" else self._do_dump if job.kind == "dump" else self._do_edit)
            job.outputs = handler(job, tid, label)
            self._set(tid, "[    OK      ]", f"{label}\n      -> " + ", ".join(p.name for p in job.outputs))
        except _Fail as fail:
            job.ok = False
            self._set(tid, "[  FAILED    ]", f"{label}\n      {fail.reason}")
            self._failures.append(Failure(fail.stage, job.label, fail.reason, job.sources[0], fail.retryable))
        except OSError as exc:
            job.ok = False
            self._set(tid, "[  FAILED    ]", f"{label}\n      {exc}")
            self._failures.append(Failure("convert", job.label, str(exc), job.sources[0]))

    def _finish(self, jobs: list[_Job]) -> None:
        cfg = self.cfg
        converted = [j for j in jobs if j.ok and j.outputs]
        orig = sum(j.size for j in converted)
        out = sum(path_bytes(p) for j in converted for p in j.outputs)
        deleted = False
        if cfg.delete and not self._stop.is_set() and cfg.action != "edit":
            for job in converted:
                if job.kind == "dump":
                    continue  # a folder of decrypted files is left alone: only files are used up
                for src in job.sources:
                    try:
                        src.unlink()
                        deleted = True
                    except OSError as exc:
                        self._log(f"Could not delete {src.name}: {exc}")
        self._emit(("done", BatchSummary(len(jobs), tuple(self._failures), cfg.kind, len(converted), orig, out, deleted)))

    # ---- Switch 1 ---------------------------------------------------------------------------------------------------
    def _finish_output(self, produced: list[Path], target: Path, what: str) -> Path:
        """Move the one file a tool made into place under the name we want; several files keep their own names."""
        if not produced:
            raise _Fail("convert", f"{what} finished but made no file.")
        if len(produced) == 1:
            os.replace(produced[0], target)
            return target
        for p in produced:
            os.replace(p, target.parent / p.name)
        return target.parent / produced[0].name

    def _verify(self, path: Path, tid: int, label: str) -> None:
        rc, out = self._exec(self._nscb("--verify", str(path), "--vertype", "dec"), tid, f"Verifying {label}", "[ VERIFYING  ]")
        if rc != 0 or re.search(r"(?i)\b(corrupt|invalid|mismatch|failed)\b", out):
            raise _Fail("verify", f"nscb_rust --verify did not accept the file: {out.splitlines()[-1] if out else 'exit code ' + str(rc)}")

    def _do_file(self, job: _Job, tid: int, label: str) -> list[Path]:
        cfg, src = self.cfg, job.sources[0]
        compress = cfg.action == "compress"
        ext = {".nsp": ".nsz", ".xci": ".xcz", ".nsz": ".nsp", ".xcz": ".xci"}[src.suffix.lower()]
        out_dir = self._out_dir(src)
        target = out_dir / (src.stem + ext)
        if target.exists():
            self._log(f"Skipped {src.name}: {target.name} already exists.")
            return [target]
        tmp = self._scratch_dir(out_dir)
        try:
            args = ["--compress", str(src), "--level", str(cfg.level)] if compress else ["--decompress", str(src)]
            rc, out = self._exec(self._nscb(*args, "-o", str(tmp), "--temp-dir", str(tmp)), tid, label, watch=tmp)
            if rc != 0:
                raise _Fail("convert", f"nscb_rust exit code {rc}" + (f" - {out.splitlines()[-1]}" if out else ""))
            made = [p for p in tmp.iterdir() if p.is_file() and p.suffix.lower() == ext]
            result = self._finish_output(made, target, "nscb_rust")
        finally:
            self._drop(tmp)
        if cfg.verify:
            self._verify(result, tid, label)
        return [result]

    def _do_merge(self, job: _Job, tid: int, label: str) -> list[Path]:
        cfg = self.cfg
        base = next((p for p in job.sources if title_kind(self._tid(p)) == "base"), job.sources[0])
        out_dir = self._out_dir(base)
        ext = "." + cfg.merge_type
        final_ext = {".nsp": ".nsz", ".xci": ".xcz"}[ext] if cfg.merge_compress else ext
        target = out_dir / f"{plain_name(base)} [{self._tid(base)}] (merged){final_ext}"
        if target.exists():
            self._log(f"Skipped {job.label}: {target.name} already exists.")
            return [target]
        tmp = self._scratch_dir(out_dir)
        try:
            args = ["-d", *map(str, job.sources), "--type", cfg.merge_type, "-o", str(tmp), "--temp-dir", str(tmp)]
            if cfg.no_delta:
                args.append("-n")
            rc, out = self._exec(self._nscb(*args), tid, label, watch=tmp)
            if rc != 0:
                raise _Fail("convert", f"nscb_rust exit code {rc}" + (f" - {out.splitlines()[-1]}" if out else ""))
            merged = [p for p in tmp.iterdir() if p.is_file() and p.suffix.lower() == ext]
            if len(merged) != 1:
                raise _Fail("convert", "the merge did not make exactly one file.")
            if cfg.merge_compress:
                self._set(tid, "[ COMPRESSING]", label)
                squeezed = tmp / "squeezed"
                squeezed.mkdir()
                rc, out = self._exec(self._nscb("--compress", str(merged[0]), "--level", str(cfg.level), "-o", str(squeezed),
                                                "--temp-dir", str(tmp)), tid, label, "[ COMPRESSING]")
                if rc != 0:
                    raise _Fail("convert", f"compressing the merged file failed: nscb_rust exit code {rc}")
                merged = [p for p in squeezed.iterdir() if p.is_file() and p.suffix.lower() == final_ext]
                if len(merged) != 1:
                    raise _Fail("convert", "compressing the merged file did not make exactly one file.")
            os.replace(merged[0], target)
        finally:
            self._drop(tmp)
        if cfg.verify:
            self._verify(target, tid, label)
        return [target]

    # ---- Switch 2: decrypting Switch 1 files into a dump -------------------------------------------------------------
    def _hac(self, extra_keys: Path | None) -> list[str]:
        cmd = [self.cfg.hactoolnet]
        if self.cfg.prod_keys:
            cmd += ["-k", self.cfg.prod_keys]
        if extra_keys is not None:
            cmd += ["--titlekeys", str(extra_keys)]
        return cmd

    def _combined_title_keys(self, folder: Path, ticket_dirs: list[Path]) -> Path:
        """One title.keys for hactoolnet: yours, plus the keys in the tickets that came with the files."""
        pairs = read_title_keys(self.cfg.title_keys) if self.cfg.title_keys else {}
        for d in ticket_dirs:
            for tik in d.glob("*.tik"):
                try:
                    if got := ticket_title_key(tik.read_bytes()):
                        pairs.setdefault(*got)
                except OSError:
                    continue
        path = folder / "title.keys"
        path.write_text("".join(f"{rid},{key}\n" for rid, key in pairs.items()), encoding="ascii")
        return path

    def _do_titles(self, job: _Job, tid: int, label: str) -> list[Path]:
        """Switch 1 files -> decrypted dump -> .nup. Needs hactoolnet and prod.keys (and title.keys for ticketless NSPs)."""
        cfg = self.cfg
        if not cfg.hactoolnet or not shutil.which(cfg.hactoolnet):
            raise _Fail("extract", "hactoolnet was not found (Settings > Tools); it decrypts .nsp / .xci files before packing.", False)
        if bad := check_prod_keys(cfg.prod_keys):
            raise _Fail("extract", bad, False)
        first = job.sources[0]
        out_dir = cfg.dest or first.parent
        stage_root = self._scratch_dir(out_dir)
        try:
            need = int(job.size * 2.2)
            if (free := free_bytes(stage_root)) is not None and free < need:
                self._log(f"WARNING: {stage_root.drive or stage_root} has {fmt_size(free)} free and this title may need about "
                          f"{fmt_size(need)} of working space. Set a Temp folder under Performance if it fills up.")
            dump = stage_root / "dump"
            meta = self._decrypt_into(dump, job, stage_root, tid, label)
            edit = self._edits.get(str(first))
            name = (edit.name if edit else "") or meta.get("name") or plain_name(first)
            stem = clean_name(edit.filename) if edit and edit.filename else (
                f"{clean_name(name)}{(' [' + meta['title_id'] + ']') if meta.get('title_id') else ''}")
            return [self._pack(dump, out_dir / f"{stem}.nup", meta, tid, label, [name, plain_name(first), first.stem], edit=edit)]
        finally:
            self._drop(stage_root)

    def _decrypt_into(self, dump: Path, job: _Job, stage_root: Path, tid: int, label: str) -> dict[str, str]:
        cfg = self.cfg
        dump.mkdir(parents=True)
        meta: dict[str, str] = {}
        base_nca: Path | None = None
        dlc_seen: dict[str, int] = {}
        for n, src in enumerate(job.sources):
            if not self._checkpoint():
                raise _Fail("extract", "stopped.", False)
            work = stage_root / f"in{n}"
            work.mkdir()
            file = src
            if src.suffix.lower() in COMPRESSED:  # nscb_rust turns .nsz / .xcz back into .nsp / .xci first
                self._set(tid, "[ UNPACKING  ]", f"{label} (decompressing {src.name})")
                rc, out = self._exec(self._nscb("--decompress", str(src), "-o", str(work), "--temp-dir", str(work)), tid,
                                     f"{label} (decompressing)", "[ UNPACKING  ]")
                made = [p for p in work.iterdir() if p.suffix.lower() in (".nsp", ".xci")]
                if rc != 0 or not made:
                    raise _Fail("extract", f"could not decompress {src.name}: nscb_rust exit code {rc}")
                file = made[0]
            ncas = work / "ncas"
            ncas.mkdir()
            flag = ["-t", "pfs0", "--outdir", str(ncas)] if file.suffix.lower() == ".nsp" else ["-t", "xci", "--securedir", str(ncas)]
            self._set(tid, "[ EXTRACTING ]", f"{label} (opening {file.name})")
            rc, out = self._exec([*self._hac(None), *flag, str(file)], tid, f"{label} (opening {file.name})", "[ EXTRACTING ]")
            if rc != 0:
                raise _Fail("extract", f"hactoolnet could not open {file.name}: {out.splitlines()[-1] if out else rc}")
            keys = self._combined_title_keys(work, [ncas])
            if file is not src:
                file.unlink(missing_ok=True)
            found: list[tuple[Path, str, str]] = []
            for nca in sorted(ncas.glob("*.nca"), key=lambda p: -file_size(p)):
                _rc, info = self._exec([*self._hac(keys), "-t", "nca", str(nca)], tid, label, "[ READING    ]")
                found.append((nca, *parse_nca_info(info)))
            order = {"Meta": 0, "Control": 1, "Program": 2}  # the version and the name are known before the files that need them
            for nca, kind, nca_tid in sorted(found, key=lambda f: order.get(f[1], 3)):
                if kind == "Meta":
                    self._read_meta(nca, keys, work, meta, tid, label)
                elif kind == "Control":
                    if "name" not in meta:
                        self._read_control(nca, keys, work, meta, tid, label)
                elif kind == "Program":
                    was_base = base_nca is None and title_kind(nca_tid or "0" * 16) != "update"
                    self._extract_program(nca, nca_tid, keys, dump, meta, base_nca, tid, label)
                    if was_base:
                        base_nca = nca
                elif kind in ("Data", "PublicData"):
                    dlc_seen[nca_tid] = dlc_seen.get(nca_tid, -1) + 1
                    self._run_hac_dir(nca, keys, ["--romfsdir", str(dump / "dlc" / nca_tid / str(dlc_seen[nca_tid]) / "romfs")], tid, label)
        if not any(is_nx_dump(dump / p) or (dump / p).is_dir() for p in ("base", "updates", "dlc")):
            raise _Fail("extract", "no game data could be read: check that prod.keys is current and that title.keys has the "
                                   "key for this game (Switch 2 titles can't be decrypted on a PC: use a dump made on the console).", False)
        return meta

    def _run_hac_dir(self, nca: Path, keys: Path, args: list[str], tid: int, label: str, base: Path | None = None) -> None:
        for a, b in zip(args, args[1:]):
            if a.startswith("--") and a.endswith("dir"):
                Path(b).mkdir(parents=True, exist_ok=True)
        cmd = [*self._hac(keys), "-t", "nca", *(["--basenca", str(base)] if base else []), *args, str(nca)]
        rc, out = self._exec(cmd, tid, f"{label} (extracting {nca.name[:12]})", "[ EXTRACTING ]")
        if rc != 0:
            raise _Fail("extract", f"hactoolnet could not extract {nca.name}: {out.splitlines()[-1] if out else rc}")

    def _extract_program(self, nca: Path, nca_tid: str, keys: Path, dump: Path, meta: dict,
                         base_nca: Path | None, tid: int, label: str) -> None:
        if title_kind(nca_tid or "0" * 16) == "update":
            version = meta.pop("pending_version", "0")
            folder = dump / "updates" / version
            if base_nca is None:
                self._log(f"Note: the update {nca_tid} needs its game to be extracted first; it is left out.")
                return
            self._run_hac_dir(nca, keys, ["--exefsdir", str(folder / "exefs"), "--romfsdir", str(folder / "romfs")], tid, label, base_nca)
            return
        if (dump / "base").exists():
            self._log("Note: a second program in the same title was left out (a NUP holds one base program).")
            return
        meta.setdefault("title_id", nca_tid)
        self._run_hac_dir(nca, keys, ["--exefsdir", str(dump / "base" / "exefs"), "--romfsdir", str(dump / "base" / "romfs")], tid, label)

    def _read_meta(self, nca: Path, keys: Path, work: Path, meta: dict, tid: int, label: str) -> None:
        out = work / f"meta_{nca.stem[:8]}"
        out.mkdir(exist_ok=True)
        self._exec([*self._hac(keys), "-t", "nca", "--section0dir", str(out), str(nca)], tid, label, "[ READING    ]")
        for cnmt in out.glob("*.cnmt"):
            if got := parse_cnmt(cnmt.read_bytes()):
                title_id, version, kind = got
                if kind == 0x81:
                    meta["pending_version"] = str(version)
                elif kind == 0x80:
                    meta.setdefault("title_id", title_id)
                    meta.setdefault("version_number", str(version))

    def _read_control(self, nca: Path, keys: Path, work: Path, meta: dict, tid: int, label: str) -> None:
        out = work / "control"
        out.mkdir(exist_ok=True)
        self._exec([*self._hac(keys), "-t", "nca", "--romfsdir", str(out), str(nca)], tid, label, "[ READING    ]")
        nacp = out / "control.nacp"
        if nacp.is_file():
            info = parse_nacp(nacp.read_bytes())
            meta.update({k: v for k, v in {"name": info["name"], "publisher": info["publisher"], "version": info["version"]}.items() if v})
        icons = sorted(out.glob("icon_*.dat"), key=lambda p: (p.name != "icon_AmericanEnglish.dat", p.name))
        if icons:
            keep = work.parent / "icon.dat"
            shutil.copy2(icons[0], keep)
            meta["icon"] = str(keep)

    # ---- Switch 2: dumps -> .nup ------------------------------------------------------------------------------------
    def _do_dump(self, job: _Job, tid: int, label: str) -> list[Path]:
        dump = job.sources[0]
        out_dir = self.cfg.dest or dump.parent
        edit = self._edits.get(str(dump))
        # the saved file is named after what the person typed: a file name if they gave one, else the new title, else the folder
        typed = ((edit.filename or edit.name) if edit else "").strip()
        stem = clean_name(typed or dump.name)
        meta = {"name": edit.name} if edit and edit.name else {}
        return [self._pack(dump, out_dir / f"{stem}.nup", meta, tid, label, [dump.name], beside=dump.parent, edit=edit)]

    def _artwork(self, meta: dict, names: list[str], beside: Path | None) -> Path | None:
        mode = self.cfg.art_mode
        if mode == "none":
            return None
        folders = [Path(self.cfg.art_dir)] if self.cfg.art_dir and mode in ("folder", "game") else []
        if mode == "game":
            if meta.get("icon") and Path(meta["icon"]).is_file():
                return Path(meta["icon"])
            if beside is not None:
                folders.append(beside)
        elif beside is not None and not folders:
            folders.append(beside)
        return find_image(names, *[f for f in folders if f.is_dir()])

    def _pack(self, dump: Path, target: Path, meta: dict, tid: int, label: str, names: list[str],
              beside: Path | None = None, edit: TitleEdit | None = None) -> Path:
        cfg = self.cfg
        if target.exists():
            self._log(f"Skipped {target.stem}: {target.name} already exists.")
            return target
        command = nup_command(cfg.nup)
        part = target.with_name(target.name + ".part")
        part.unlink(missing_ok=True)
        args = ["pack", str(dump), str(part), "--level", str(cfg.level)]
        if meta.get("name"):
            args += ["--name", meta["name"]]
        if given := re.sub(r"[^0-9A-Fa-f]", "", edit.title_id if edit else ""):
            if len(given) == 16:
                args += ["--title-id", given.upper()]
                self._log(f"Title ID for {target.stem}: {given.upper()} (entered by hand).")
        else:
            found, where = dump_title_id(dump, meta.get("title_id", ""))
            if found and where != "base":  # nup reads the base's NPDM itself; only step in when that is empty
                args += ["--title-id", found]
                self._log(f"Title ID for {target.stem}: {found}, taken from {where} (the base package has no main.npdm).")
        publisher = (edit.publisher if edit else "") or cfg.publisher or meta.get("publisher", "")
        version = (edit.display_version if edit else "") or cfg.display_version or meta.get("version", "")
        if publisher:
            args += ["--publisher", publisher]
        if version:
            args += ["--display-version", version]
        chosen = Path(edit.icon) if edit and edit.icon and Path(edit.icon).is_file() else None  # a picture picked by hand wins
        if icon := chosen or self._artwork(meta, names, beside):
            args += ["--icon", str(icon)]
            self._log(f"Artwork for {target.stem}: {icon.name}")
        elif cfg.art_mode != "none":
            self._log(f"No artwork found for {target.stem}.")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            rc, out = self._exec([*command, *args], tid, label, "[ PACKING    ]")
            if rc != 0:
                raise _Fail("convert", f"nup pack exit code {rc}" + (f" - {out.splitlines()[-1]}" if out else ""))
            if cfg.verify:
                rc, out = self._exec([*command, "verify", str(part)], tid, f"Verifying {label}", "[ VERIFYING  ]")
                if rc != 0:
                    raise _Fail("verify", f"nup verify exit code {rc}" + (f" - {out.splitlines()[-1]}" if out else ""))
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)
        return target

    def _do_edit(self, job: _Job, tid: int, label: str) -> list[Path]:
        cfg, nup = self.cfg, job.sources[0]
        args = ["edit", str(nup)]
        edit = self._edits.get(str(nup))
        icon = Path(edit.icon) if edit and edit.icon and Path(edit.icon).is_file() else None
        if icon is None and cfg.art_mode != "none":
            icon = find_image([nup.stem], *([Path(cfg.art_dir)] if cfg.art_dir else []), nup.parent)
        if icon:
            args += ["--icon", str(icon)]
        if publisher := (edit.publisher if edit else "") or cfg.publisher:
            args += ["--publisher", publisher]
        if version := (edit.display_version if edit else "") or cfg.display_version:
            args += ["--display-version", version]
        if len(args) == 2:
            self._log(f"Skipped {nup.name}: no artwork named '{nup.stem}' and no publisher or version to set.")
            return [nup]
        rc, out = self._exec([*nup_command(cfg.nup), *args], tid, label, "[ EDITING    ]")
        if rc != 0:
            raise _Fail("convert", f"nup edit exit code {rc}" + (f" - {out.splitlines()[-1]}" if out else ""))
        return [nup]
