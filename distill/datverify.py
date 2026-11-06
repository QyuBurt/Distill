"""Checking files against a DAT (No-Intro, Redump, TOSEC): hash them, match by SHA-1 / MD5 / CRC32+size, name the
canonical file names, and say what is missing or bad. Reads Logiqx XML (what all three publish) and the older
clrmamepro text layout. No Qt in here."""
from __future__ import annotations

import hashlib
import re
import threading
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET


@dataclass(slots=True)
class DatRom:
    name: str
    size: int
    crc: str = ""
    md5: str = ""
    sha1: str = ""


@dataclass(slots=True)
class DatGame:
    name: str
    roms: list[DatRom] = field(default_factory=list)


@dataclass(slots=True)
class Dat:
    name: str
    games: list[DatGame]

    def index(self) -> dict[str, tuple[DatGame, DatRom]]:
        idx: dict[str, tuple[DatGame, DatRom]] = {}
        for g in self.games:
            for r in g.roms:
                if r.sha1:
                    idx.setdefault("sha1:" + r.sha1, (g, r))
                if r.md5:
                    idx.setdefault("md5:" + r.md5, (g, r))
                if r.crc:
                    idx.setdefault(f"crc:{r.crc}:{r.size}", (g, r))
        return idx


def _rom(name: str, size: str, crc: str, md5: str, sha1: str) -> DatRom:
    return DatRom(name, int(size) if str(size).isdigit() else 0, crc.lower().zfill(8) if crc else "", md5.lower(), sha1.lower())


_CMP_GAME = re.compile(r"^\s*(?:game|machine)\s*\(\s*$", re.IGNORECASE)
_CMP_NAME = re.compile(r'^\s*name\s+(?:"([^"]*)"|(\S+))\s*$', re.IGNORECASE)
_CMP_ROM = re.compile(r"^\s*rom\s*\((.*)\)\s*$", re.IGNORECASE)
_CMP_FIELD = re.compile(r'(\w+)\s+(?:"([^"]*)"|(\S+))')


def load_dat(path: Path) -> Dat:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    if raw.lstrip().startswith("<"):
        try:
            root = ET.fromstring(raw.encode("utf-8"))
        except ET.ParseError as e:
            raise ValueError(f"Not a valid DAT file: {e}") from e
        header = root.find("header/name")
        games = []
        for g in list(root.iter("game")) + list(root.iter("machine")):
            roms = [_rom(r.get("name", ""), r.get("size", ""), r.get("crc", ""), r.get("md5", ""), r.get("sha1", ""))
                    for r in g.findall("rom")]
            if roms:
                games.append(DatGame(g.get("name", ""), roms))
        return Dat(header.text.strip() if header is not None and header.text else path.stem, games)
    games, name, cur, depth_name = [], path.stem, None, ""
    for line in raw.splitlines():
        if _CMP_GAME.match(line):
            cur = DatGame("")
        elif cur is not None and (m := _CMP_NAME.match(line)):
            cur.name = m[1] if m[1] is not None else m[2]
        elif cur is not None and (m := _CMP_ROM.match(line)):
            f = {k.lower(): (a if a is not None and a != "" else b) for k, a, b in _CMP_FIELD.findall(m[1])}
            cur.roms.append(_rom(f.get("name", ""), f.get("size", ""), f.get("crc", ""), f.get("md5", ""), f.get("sha1", "")))
        elif cur is not None and line.strip() == ")":
            if cur.roms:
                games.append(cur)
            cur = None
        elif cur is None and (m := _CMP_NAME.match(line)) and not depth_name:
            depth_name = name = m[1] if m[1] is not None else m[2]
    if not games:
        raise ValueError("No games found: not a Logiqx XML or clrmamepro DAT")
    return Dat(name, games)


@dataclass(slots=True)
class Hashes:
    size: int
    crc: str
    md5: str
    sha1: str


def hash_stream(f, stop: threading.Event) -> Hashes | None:
    md5, sha1, crc, size = hashlib.md5(), hashlib.sha1(), 0, 0
    while chunk := f.read(4 << 20):
        if stop.is_set():
            return None
        md5.update(chunk)
        sha1.update(chunk)
        crc = zlib.crc32(chunk, crc)
        size += len(chunk)
    return Hashes(size, f"{crc & 0xFFFFFFFF:08x}", md5.hexdigest(), sha1.hexdigest())


def hash_file(path: Path, stop: threading.Event) -> Hashes | None:
    """Hashes of a file; a .zip holding exactly one file is hashed by what's inside (the usual ROM-set layout)."""
    try:
        if path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = [i for i in z.infolist() if not i.is_dir()]
                if len(names) == 1:
                    with z.open(names[0]) as f:
                        return hash_stream(f, stop)
        with path.open("rb") as f:
            return hash_stream(f, stop)
    except (OSError, zipfile.BadZipFile):
        return None


def match(h: Hashes, idx: dict[str, tuple[DatGame, DatRom]]) -> tuple[DatGame, DatRom] | None:
    return idx.get("sha1:" + h.sha1) or idx.get("md5:" + h.md5) or idx.get(f"crc:{h.crc}:{h.size}")


@dataclass(slots=True)
class Verified:
    path: Path
    game: DatGame
    rom: DatRom

    @property
    def canonical(self) -> str:
        """The file's DAT name; for a one-file game inside a .zip the .zip is renamed to the game."""
        if self.path.suffix.lower() == ".zip" and not self.rom.name.lower().endswith(".zip"):
            return self.game.name + ".zip"
        return self.rom.name

    @property
    def renamed(self) -> bool:
        return self.path.name != self.canonical


@dataclass(slots=True)
class Report:
    ok: list[Verified] = field(default_factory=list)  # matched
    unknown: list[Path] = field(default_factory=list)  # in no DAT entry
    bad: list[tuple[Path, DatGame]] = field(default_factory=list)  # named like a game but contents differ
    unreadable: list[Path] = field(default_factory=list)
    missing: list[DatGame] = field(default_factory=list)  # games none of whose files were found
    incomplete: list[tuple[DatGame, list[DatRom]]] = field(default_factory=list)  # some tracks found, others not


def _norm(name: str) -> str:
    return re.sub(r"\.[a-z0-9]{1,5}$", "", name.casefold()).strip()


def verify(files: list[Path], dat: Dat, stop: threading.Event, note: Callable[[str], None] | None = None) -> Report:
    idx = dat.index()
    by_name = {_norm(g.name): g for g in dat.games}
    for g in dat.games:
        for r in g.roms:
            by_name.setdefault(_norm(r.name), g)
    rep = Report()
    for n, p in enumerate(files, 1):
        if stop.is_set():
            break
        if note:
            note(f"Hashing {n} of {len(files)}: {p.name}")
        h = hash_file(p, stop)
        if h is None:
            if not stop.is_set():
                rep.unreadable.append(p)
        elif hit := match(h, idx):
            rep.ok.append(Verified(p, *hit))
        elif g := by_name.get(_norm(p.name)):
            rep.bad.append((p, g))
        else:
            rep.unknown.append(p)
    found: dict[int, set[int]] = {}
    for v in rep.ok:
        found.setdefault(id(v.game), set()).add(id(v.rom))
    for g in dat.games:
        have = found.get(id(g), set())
        if not have:
            if not stop.is_set():
                rep.missing.append(g)
        elif len(have) < len(g.roms):
            rep.incomplete.append((g, [r for r in g.roms if id(r) not in have]))
    return rep
