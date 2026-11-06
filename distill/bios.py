"""BIOS checker: hash-verify the files emulators need against a database you supply. No Qt in here.

The database is JSON: {"PlayStation": [{"file": "scph5501.bin", "md5": "...", "required": true, "note": "USA"}]}.
`file` may hold a sub-folder ('dc/dc_boot.bin'); `md5` may be a list when several dumps are accepted. Distill ships no
hashes of its own: export them from your front-end's list (Batocera es_bios, libretro system info) or write your own."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class BiosResult:
    system: str
    file: str
    status: str  # 'ok' | 'missing' | 'bad'
    required: bool
    note: str = ""


def load_db(path: Path) -> dict[str, list[dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ValueError(f"Can't read BIOS list: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("BIOS list must be an object of system -> files")
    return data


def _md5(path: Path, stop: threading.Event) -> str | None:
    h = hashlib.md5()
    try:
        with path.open("rb") as f:
            while chunk := f.read(4 << 20):
                if stop.is_set():
                    return None
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def check(bios_dir: Path, db: dict[str, list[dict]], stop: threading.Event) -> list[BiosResult]:
    out: list[BiosResult] = []
    for system, entries in db.items():
        for e in entries:
            name = str(e.get("file", ""))
            want = e.get("md5", [])
            want = {w.lower() for w in ([want] if isinstance(want, str) else want)}
            req, note = bool(e.get("required", True)), str(e.get("note", ""))
            p = bios_dir / name
            if not p.is_file():
                p = next((c for c in bios_dir.rglob(Path(name).name) if c.is_file()), p)  # misplaced into a sub-folder
            if not p.is_file():
                out.append(BiosResult(system, name, "missing", req, note))
                continue
            got = _md5(p, stop)
            if got is None:
                break
            out.append(BiosResult(system, name, "ok" if not want or got in want else "bad", req, note))
    return out
