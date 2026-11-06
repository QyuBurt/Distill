"""Disc-image repair: merge a multi-file BIN/CUE into one BIN, split one BIN back into a file per track, and turn
CloneCD (.ccd/.img/.sub) into BIN/CUE. No Qt in here. Nothing is overwritten; callers pick fresh output names."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

FRAMES_PER_SECOND = 75
_SECTOR = {"AUDIO": 2352, "MODE1/2352": 2352, "MODE2/2352": 2352, "MODE1/2048": 2048, "MODE2/2336": 2336,
           "MODE2/2048": 2048, "CDG": 2448, "CDI/2336": 2336, "CDI/2352": 2352}
_FILE_RE = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))\s+(\w+)', re.IGNORECASE)
_TRACK_RE = re.compile(r"^\s*TRACK\s+(\d+)\s+(\S+)", re.IGNORECASE)
_INDEX_RE = re.compile(r"^\s*INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)", re.IGNORECASE)


class CueError(Exception):
    pass


def msf_to_frames(m: int, s: int, f: int) -> int:
    return (m * 60 + s) * FRAMES_PER_SECOND + f


def frames_to_msf(n: int) -> str:
    return f"{n // (60 * FRAMES_PER_SECOND):02d}:{n // FRAMES_PER_SECOND % 60:02d}:{n % FRAMES_PER_SECOND:02d}"


@dataclass(slots=True)
class Track:
    number: int
    mode: str
    file: str = ""  # file holding the track (as written in the cue)
    indexes: dict[int, int] = field(default_factory=dict)  # index number -> frame within its file
    extra: list[str] = field(default_factory=list)  # FLAGS / ISRC / PREGAP ... lines, kept as they are

    @property
    def sector(self) -> int:
        return _SECTOR.get(self.mode.upper(), 2352)


def parse_cue(path: Path) -> list[Track]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        raise CueError(str(e)) from e
    tracks: list[Track] = []
    current_file = ""
    for line in text.splitlines():
        if m := _FILE_RE.match(line):
            current_file = m[1] or m[2]
        elif m := _TRACK_RE.match(line):
            tracks.append(Track(int(m[1]), m[2], current_file))
        elif tracks and (m := _INDEX_RE.match(line)):
            tracks[-1].indexes[int(m[1])] = msf_to_frames(int(m[2]), int(m[3]), int(m[4]))
        elif tracks and line.strip().split(" ")[0].upper() in ("FLAGS", "ISRC", "PREGAP", "POSTGAP", "TITLE", "PERFORMER"):
            tracks[-1].extra.append(line.strip())
    if not tracks:
        raise CueError("No tracks found in the cue sheet")
    return tracks


def _write_cue(path: Path, tracks: list[Track], files_in_order: bool = True) -> None:
    out: list[str] = []
    last = None
    for t in tracks:
        if t.file != last:
            out.append(f'FILE "{t.file}" BINARY')
            last = t.file
        out.append(f"  TRACK {t.number:02d} {t.mode}")
        out += [f"    {e}" for e in t.extra]
        out += [f"    INDEX {i:02d} {frames_to_msf(f)}" for i, f in sorted(t.indexes.items())]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _files(cue: Path, tracks: list[Track]) -> list[Path]:
    seen: list[str] = []
    for t in tracks:
        if t.file not in seen:
            seen.append(t.file)
    paths = [cue.parent / n for n in seen]
    for p in paths:
        if not p.is_file():
            raise CueError(f"{p.name} is missing")
    return paths


def _fresh(path: Path) -> None:
    if path.exists():
        raise CueError(f"{path.name} already exists")


def merge_cue(cue: Path, out_cue: Path) -> Path:
    """One BIN holding every track, with a cue sheet whose times count from the start of that one file."""
    tracks = parse_cue(cue)
    files = _files(cue, tracks)
    if len(files) < 2:
        raise CueError("Already a single file")
    out_bin = out_cue.with_suffix(".bin")
    _fresh(out_cue)
    _fresh(out_bin)
    base: dict[str, int] = {}
    total = 0
    sector_of = {t.file: t.sector for t in tracks}
    with out_bin.open("wb") as dst:
        for p in files:
            base[p.name] = total
            size = p.stat().st_size
            sector = sector_of.get(p.name, 2352)
            if size % sector:
                raise CueError(f"{p.name}: size isn't a whole number of {sector}-byte sectors")
            with p.open("rb") as f:
                shutil.copyfileobj(f, dst, 8 << 20)
            total += size // sector
    # a file's frame offset is only exact when every file shares one sector size, which real discs do
    for t in tracks:
        off = base[t.file]
        t.indexes = {i: f + off for i, f in t.indexes.items()}
        t.file = out_bin.name
    _write_cue(out_cue, tracks)
    return out_cue


def split_cue(cue: Path, out_dir: Path) -> Path:
    """One file per track: '<name> (Track 01).bin' ... in out_dir, plus a cue sheet to match."""
    tracks = parse_cue(cue)
    files = _files(cue, tracks)
    if len(files) != 1:
        raise CueError("Needs a cue sheet that points at one BIN")
    src = files[0]
    sector = tracks[0].sector
    total = src.stat().st_size // sector
    out_dir.mkdir(parents=True, exist_ok=True)
    out_cue = out_dir / cue.name
    _fresh(out_cue)
    width = len(str(len(tracks)))  # Redump style: Track 1 ... Track 9, Track 01 ... Track 12
    starts = [min(t.indexes.values()) if t.indexes else 0 for t in tracks]
    ends = starts[1:] + [total]
    names = [f"{src.stem} (Track {t.number:0{width}d}).bin" for t in tracks]
    for n in names:
        _fresh(out_dir / n)
    new: list[Track] = []
    with src.open("rb") as f:
        for t, a, b, name in zip(tracks, starts, ends, names):
            f.seek(a * sector)
            left = (b - a) * sector
            with (out_dir / name).open("wb") as dst:
                while left:
                    chunk = f.read(min(left, 8 << 20))
                    if not chunk:
                        raise CueError("BIN is shorter than its cue sheet says")
                    dst.write(chunk)
                    left -= len(chunk)
            new.append(Track(t.number, t.mode, name, {i: fr - a for i, fr in t.indexes.items()}, list(t.extra)))
    _write_cue(out_cue, new)
    return out_cue


_CCD_SECTION = re.compile(r"^\[(.+)\]$")


def _read_ccd(path: Path) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    cur: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if m := _CCD_SECTION.match(line):
            cur = sections.setdefault(m[1].upper(), {})
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip().upper()] = v.strip()
    return sections


def ccd_to_cue(ccd: Path, out_dir: Path | None = None, move: bool = False) -> Path:
    """CloneCD -> BIN/CUE: the .img becomes the .bin (copied, or moved) and a cue is written from the .ccd's tracks.
    The .sub subchannel file is left behind; BIN/CUE has no place for it."""
    img = ccd.with_suffix(".img")
    if not img.is_file():
        raise CueError(f"{img.name} is missing")
    out_dir = out_dir or ccd.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_bin, out_cue = out_dir / f"{ccd.stem}.bin", out_dir / f"{ccd.stem}.cue"
    _fresh(out_bin)
    _fresh(out_cue)
    sections = _read_ccd(ccd)
    numbered = sorted(((int(k.split()[1]), v) for k, v in sections.items() if re.fullmatch(r"TRACK \d+", k)),
                      key=lambda x: x[0])
    if not numbered:
        raise CueError("No tracks found in the .ccd file")
    tracks = []
    for number, sec in numbered:
        mode = {"0": "AUDIO", "1": "MODE1/2352", "2": "MODE2/2352"}.get(sec.get("MODE", "0"), "MODE1/2352")
        t = Track(number, mode, out_bin.name)
        if "INDEX 0" in sec:
            t.indexes[0] = int(sec["INDEX 0"])
        t.indexes[1] = int(sec.get("INDEX 1", "0"))
        tracks.append(t)
    (shutil.move if move else shutil.copyfile)(str(img), str(out_bin))
    _write_cue(out_cue, tracks)
    return out_cue
