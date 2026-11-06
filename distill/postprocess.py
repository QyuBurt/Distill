"""What happens to downloads when they finish: unpack archives, optionally deleting them afterwards. No Qt in here.

zip is unpacked with Python (every entry's destination is checked, so a hostile archive can't write outside its folder);
7z and rar go through a 7-Zip on this computer (or py7zr for 7z). An archive with a single top-level item unpacks next
to itself; one with several unpacks into a folder named after it. Multi-part sets are unpacked from their first part
and never deleted (the other parts aren't tracked)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .constants import SUBPROCESS_FLAGS
from .system import _py7zr, find_7zip

ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar")
_PART = re.compile(r"\.part0*(\d+)\.rar$", re.IGNORECASE)
_OLD_PART = re.compile(r"\.(?:r\d{2}|z\d{2})$", re.IGNORECASE)


@dataclass(slots=True)
class Unpacked:
    archive: Path
    ok: bool
    note: str
    into: Path | None = None
    deleted: bool = False


def is_first_archive(path: Path) -> bool:
    name = path.name.lower()
    if _OLD_PART.search(name):
        return False
    if m := _PART.search(name):
        return int(m.group(1)) == 1
    return name.endswith(ARCHIVE_EXTENSIONS) or name.endswith(".7z.001")


def is_multipart(path: Path) -> bool:
    name = path.name.lower()
    return bool(_PART.search(name)) or name.endswith(".7z.001") or path.with_suffix(".r00").exists()


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while (candidate := path.with_name(f"{path.stem} ({n}){path.suffix}")).exists():
        n += 1
    return candidate


def _safe_zip_extract(archive: Path, target: Path) -> None:
    root = target.resolve()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            out = (target / info.filename).resolve()
            if not out.is_relative_to(root):
                raise ValueError(f"unsafe path in the archive: {info.filename}")
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)


def _extract(archive: Path, target: Path) -> None:
    name = archive.name.lower()
    if name.endswith(".zip"):
        _safe_zip_extract(archive, target)
        return
    seven = find_7zip()
    if seven:
        done = subprocess.run([seven, "x", "-y", f"-o{target}", str(archive)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
        if done.returncode not in (0, 1):  # 1 = finished with warnings
            lines = [ln for ln in (done.stdout + done.stderr).splitlines() if ln.strip()]
            raise RuntimeError(lines[-1] if lines else f"7-Zip exited with {done.returncode}")
        return
    if name.endswith(".7z"):
        with _py7zr().SevenZipFile(archive) as z:
            z.extractall(target)
        return
    raise RuntimeError("needs 7-Zip installed to open " + archive.suffix + " files")


def unpack(archive: Path, delete: bool) -> Unpacked:
    staging = archive.with_name(f".{archive.stem}.distill_unpack")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        _extract(archive, staging)
        items = sorted(staging.iterdir())
        if not items:
            raise RuntimeError("the archive is empty")
        if len(items) == 1:
            final = _unique(archive.with_name(items[0].name))
            os.replace(items[0], final)
        else:
            final = _unique(archive.with_name(re.sub(r"(\.part0*1)?$", "", archive.stem, flags=re.I)))
            os.replace(staging, final)
            staging = None  # type: ignore[assignment]
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        return Unpacked(archive, False, str(exc) or type(exc).__name__)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    removed = False
    if delete and not is_multipart(archive):
        try:
            archive.unlink()
            removed = True
        except OSError:
            pass
    return Unpacked(archive, True, f"unpacked to {final.name}", final, removed)


def unpack_all(paths: list[Path], delete: bool, log: Callable[[str], None], stop: threading.Event) -> list[Unpacked]:
    todo = [p for p in paths if p.is_file() and is_first_archive(p)]
    results = []
    for n, path in enumerate(todo, 1):
        if stop.is_set():
            break
        log(f"Unpacking {n} of {len(todo)}: {path.name}")
        res = unpack(path, delete)
        results.append(res)
        log(f"  {'OK' if res.ok else 'FAILED'}: {res.note}" + (" (archive deleted)" if res.deleted else ""))
    return results
