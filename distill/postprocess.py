"""What happens to downloads when they finish: unpack archives, optionally deleting them afterwards. No Qt in here.

zip is unpacked with Python (every entry's destination is checked, so a hostile archive can't write outside its folder);
7z and rar go through a 7-Zip on this computer (or py7zr for 7z). An archive with a single top-level item unpacks next
to itself; one with several unpacks into a folder named after it. Multi-part sets are unpacked from their first part,
and with "delete" every part of the set goes once it has unpacked (a set with a part missing fails first, so nothing is
lost). A password-protected archive is tried with each saved password (one per line in distill_archive_passwords.txt),
then with ones worked out from the site it was downloaded from (www.site.com, site.com, site) and from its own name, which
is what most releases use."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .constants import APP_DIR, SUBPROCESS_FLAGS
from .system import _py7zr, find_7zip

ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar")
_PART = re.compile(r"\.part0*(\d+)\.rar$", re.IGNORECASE)
_OLD_PART = re.compile(r"\.(?:r\d{2}|z\d{2})$", re.IGNORECASE)
PASSWORD_FILE = APP_DIR / "distill_archive_passwords.txt"
MAX_PASSWORDS = 50  # tried per archive: each try can mean reading a big file again
_WRONG_PASSWORD = ("wrong password", "enter password", "encrypted archive", "data error in encrypted", "password is incorrect",
                   "cannot open encrypted")


class NeedPassword(RuntimeError):
    """The archive is encrypted and the password tried (if any) was not the one."""


@dataclass(slots=True)
class Unpacked:
    archive: Path
    ok: bool
    note: str
    into: Path | None = None
    deleted: bool = False
    parts_deleted: int = 0  # the other parts of a multi-part set that went with the first
    used_password: bool = False


def is_first_archive(path: Path) -> bool:
    name = path.name.lower()
    if _OLD_PART.search(name):
        return False
    if m := _PART.search(name):
        return int(m.group(1)) == 1
    return name.endswith(ARCHIVE_EXTENSIONS) or name.endswith(".7z.001")


def is_multipart(path: Path) -> bool:
    name = path.name.lower()
    return (bool(_PART.search(name)) or name.endswith(".7z.001") or path.with_suffix(".r00").exists()
            or path.with_suffix(".z01").exists())


def parts_of(first: Path) -> list[Path]:
    """The files of the multi-part set that starts at `first` (name.part1.rar, name.7z.001, name.rar + name.r00,
    name.zip + name.z01), `first` itself among them and in order. A single archive gives just itself."""
    name = first.name
    folder = first.parent
    patterns: list[str] = []
    if m := re.match(r"^(.*)\.part0*1\.rar$", name, re.IGNORECASE):
        patterns.append(rf"^{re.escape(m.group(1))}\.part0*\d+\.rar$")
    elif m := re.match(r"^(.*)\.7z\.001$", name, re.IGNORECASE):
        patterns.append(rf"^{re.escape(m.group(1))}\.7z\.\d{{3}}$")
    elif m := re.match(r"^(.*)\.rar$", name, re.IGNORECASE):
        patterns.append(rf"^{re.escape(m.group(1))}\.[rs]\d{{2}}$")  # name.r00, name.r01 ... name.s00
    elif m := re.match(r"^(.*)\.zip$", name, re.IGNORECASE):
        patterns.append(rf"^{re.escape(m.group(1))}\.z\d{{2}}$")
    found = [first]
    try:
        siblings = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return found
    for sib in siblings:
        if sib != first and sib.is_file() and any(re.match(p, sib.name, re.IGNORECASE) for p in patterns):
            found.append(sib)
    return found


def load_passwords(path: Path = PASSWORD_FILE) -> list[str]:
    """The saved archive passwords: one per line; blank lines and lines starting with # are ignored."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in lines:
        pw = line.strip("\r\n")
        if pw.strip() and not pw.lstrip().startswith("#") and pw not in out:
            out.append(pw)
    return out


_SECOND_LEVEL = ("co", "com", "org", "net", "gov", "edu", "ac")  # example.co.uk: the name is one label further in
_SHARED_HOSTS = ("archive.org", "1fichier.com", "mega.nz", "mega.io", "mega.co.nz", "github.com", "githubusercontent.com",
                 "sourceforge.net", "torbox.app")  # file hosts: the site that posted the release is somewhere else


def password_hints(url: str) -> list[str]:
    """Passwords a release site is likely to have used, worked out from the address a file came from: for
    https://dl.some-site.com/x.rar that is www.some-site.com, some-site.com, dl.some-site.com, some-site and somesite.
    Nothing for torrents or for hosts that only store other people's files (Drive, MediaFire, archive.org ...)."""
    if not url.lower().startswith(("http://", "https://")):
        return []
    host = (urllib.parse.urlsplit(url).hostname or "").lower().strip(".")
    labels = host.split(".")
    if len(labels) < 2 or host.replace(".", "").isdigit():
        return []
    from . import hosters  # noqa: PLC0415  (hosters imports downloads, which imports this module when it needs it)
    if hosters.hoster_for(url) or any(host == h or host.endswith("." + h) for h in _SHARED_HOSTS):
        return []
    take = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    registrable = ".".join(labels[-take:])
    name = labels[-take] if len(labels) >= take else labels[0]
    out: list[str] = []
    for pw in (f"www.{registrable}", registrable, host, name, name.replace("-", "")):
        if pw and pw not in out:
            out.append(pw)
    return out


def _candidates(archive: Path, passwords, hints=()) -> list[str]:
    """No password first (most archives have none), then the person's saved ones, then ones worked out from the
    site it came from, then the archive's own name."""
    base = re.sub(r"(\.part0*1)?(\.7z)?$", "", archive.stem, flags=re.IGNORECASE)
    out = [""]
    for pw in [*passwords, *hints, base, archive.stem]:
        if pw and pw not in out:
            out.append(pw)
    return out[:MAX_PASSWORDS + 1]


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while (candidate := path.with_name(f"{path.stem} ({n}){path.suffix}")).exists():
        n += 1
    return candidate


def _safe_zip_extract(archive: Path, target: Path, password: str = "") -> None:
    root = target.resolve()
    pwd = password.encode("utf-8") if password else None
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            out = (target / info.filename).resolve()
            if not out.is_relative_to(root):
                raise ValueError(f"unsafe path in the archive: {info.filename}")
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                with z.open(info, pwd=pwd) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
            except RuntimeError as exc:  # zipfile's way of saying "encrypted" or "bad password"
                if "password" in str(exc).lower():
                    raise NeedPassword(str(exc)) from None
                raise
            except zipfile.BadZipFile as exc:
                if info.flag_bits & 0x1:  # a wrong ZipCrypto password shows up as a failed check on the data
                    raise NeedPassword(str(exc)) from None
                raise


def _refuse_unsafe_zip_paths(archive: Path, target: Path) -> None:
    """7-Zip quietly tidies a '../' name into the target folder; Distill's rule is to refuse such an archive, so look before extracting."""
    root = target.resolve()
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if not (target / info.filename).resolve().is_relative_to(root):
                    raise ValueError(f"unsafe path in the archive: {info.filename}")
    except zipfile.BadZipFile:
        pass  # not readable here: 7-Zip or the Python reader will say so


def _seven_extract(seven: str, archive: Path, target: Path, password: str) -> None:
    cmd = [seven, "x", "-y", f"-o{target}"]
    if password:
        cmd.append(f"-p{password}")
    cmd.append(str(archive))
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
    if done.returncode not in (0, 1):  # 1 = finished with warnings
        text = done.stdout + done.stderr
        if any(marker in text.lower() for marker in _WRONG_PASSWORD):
            raise NeedPassword("password protected")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        raise RuntimeError(lines[-1] if lines else f"7-Zip exited with {done.returncode}")


def _extract(archive: Path, target: Path, password: str = "") -> None:
    name = archive.name.lower()
    seven = find_7zip()
    if name.endswith(".zip") and not is_multipart(archive):
        _refuse_unsafe_zip_paths(archive, target)
        if seven:  # 7-Zip first: faster, and it copes with names Python's zipfile mangles on Windows; Python is the fallback
            try:
                _seven_extract(seven, archive, target, password)
                return
            except NeedPassword:
                raise
            except (RuntimeError, OSError):
                shutil.rmtree(target, ignore_errors=True)
                target.mkdir()
        try:
            _safe_zip_extract(archive, target, password)
            return
        except NotImplementedError:  # AES-encrypted or an unusual method: only 7-Zip can open it
            raise RuntimeError("this zip needs 7-Zip installed to open it") from None
    if seven:
        _seven_extract(seven, archive, target, password)
        return
    if name.endswith(".7z"):
        try:
            with _py7zr().SevenZipFile(archive, password=password or None) as z:
                z.extractall(target)
        except Exception as exc:
            if "password" in type(exc).__name__.lower() or "password" in str(exc).lower():
                raise NeedPassword(str(exc)) from None
            raise
        return
    raise RuntimeError("needs 7-Zip installed to open " + archive.suffix + " files")


def unpack(archive: Path, delete: bool, passwords=(), hints=()) -> Unpacked:
    staging = archive.with_name(f".{archive.stem}.distill_unpack")
    used = False
    try:
        for pw in _candidates(archive, passwords, hints):
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir()  # every try starts empty, so a wrong password's leftovers never mix in
            try:
                _extract(archive, staging, pw)
            except NeedPassword:
                continue
            used = bool(pw)
            break
        else:
            n = len(_candidates(archive, passwords, hints)) - 1
            raise RuntimeError(f"password protected: none of the {n} passwords tried opened it. "
                               "Save its password in distill_archive_passwords.txt")
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
    removed, parts = False, 0
    if delete:
        for part in parts_of(archive):  # the whole set, now that all of it was read
            try:
                part.unlink()
            except OSError:
                continue
            if part == archive:
                removed = True
            else:
                parts += 1
    return Unpacked(archive, True, f"unpacked to {final.name}", final, removed, parts, used)


def unpack_all(paths: list[Path], delete: bool, log: Callable[[str], None], stop: threading.Event,
               passwords=(), hints: dict[Path, list[str]] | None = None) -> list[Unpacked]:
    todo = [p for p in paths if p.is_file() and is_first_archive(p)]
    results = []
    for n, path in enumerate(todo, 1):
        if stop.is_set():
            break
        log(f"Unpacking {n} of {len(todo)}: {path.name}")
        res = unpack(path, delete, passwords, (hints or {}).get(path, ()))
        results.append(res)
        extra = (f" (archive deleted{f', with {res.parts_deleted} more part(s)' if res.parts_deleted else ''})"
                 if res.deleted else "")
        log(f"  {'OK' if res.ok else 'FAILED'}: {res.note}" + (" (with a password it worked out or had saved)" if res.used_password else "") + extra)
    return results
