"""Every key and login the person has given Distill, kept in ONE encrypted file: distill_keys.json. No Qt in here.

    keys    SteamGridDB, GitHub, RetroAchievements and RomM (Settings > API keys > Games and Updates)
    logins  archive.org, 1fichier, TorBox and other debrid services, Prowlarr and any other file host
            (Settings > API keys > Downloads). Only written while "Remember" is ticked there.

The file is sealed the way remembered logins always were (see secretstore.py): encrypted for this Windows account, or with
a key in the system keyring, or plain text in the runtime config directory where neither exists. Older versions kept these in two files
(distill_apikeys.json and distill_logins.json); they are folded into the new one the first time it is read, and removed once
the new file has been written. A key saved here is used before one from the environment or from the optional
distill/local_keys.py, so a build someone else made can still be given a key afterwards.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import secretstore
from . import paths
from .constants import APP_DIR, APP_NAME

FILE = paths.config_dir() / "distill_keys.json"
LEGACY_KEYS_FILE = paths.config_dir() / "distill_apikeys.json"
LEGACY_LOGINS_FILE = paths.config_dir() / "distill_logins.json"
NAMES = ("sgdb", "github", "ra_user", "ra", "romm_url", "romm")  # what can be saved as a key: SteamGridDB (posters, icons), GitHub (rate limit for updates and downloads), RetroAchievements (user name, Web API key), RomM (server address, API token)

_lock = threading.RLock()
_cache: dict | None = None  # {"keys": {name: value}, "logins": {...}} as last read or written
_stamp: tuple[int, int, int] | None = None
_problem = ""  # set when a keys file exists but could not be unlocked (moved to another account or computer?)
_FINALIZED = "finalized"
# One attempt per process/store. Failed publication can only be retried by a fresh start.
_migration: tuple[object, dict, str, bool] | None = None
_READ_LIMIT = 1024 * 1024
_LOCK_TIMEOUT = 2.0


# ---- reading and writing the file ---------------------------------------------------------------------------------
def _read_only(problem: str) -> str:
    return (f"{problem}; Saved logins and keys are read-only this session because the first-run migration could not run "
            f"(fix the permissions on {FILE.parent} and restart Distill). Nothing was changed.")


def _read_bytes(path) -> bytes:
    """Read only a bounded, non-symlink regular file, including rollback copies."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > _READ_LIMIT:
        raise OSError("not a regular file or exceeds the credential size limit")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("credential source changed while opening")
        content = stream.read(_READ_LIMIT + 1)
    if len(content) > _READ_LIMIT:
        raise OSError("exceeds the credential size limit")
    return content


def _open(path) -> tuple[dict | None, str]:
    """Missing files have no warning; unreadable stores never supply fallback data."""
    try:
        content = _read_bytes(path)
        raw = json.loads(content.decode("utf-8"))
        if isinstance(raw, dict) and raw.get("v") == 2:  # sealed: see secretstore.py; the oldest files are plain and still load
            raw = json.loads(secretstore.unseal(raw).decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not a credential object")
    except FileNotFoundError:
        return None, ""
    except secretstore.SealError as exc:
        return None, f"{path.name}: {exc}"
    except (OSError, ValueError) as exc:
        return None, f"{path.name}: {exc}"
    return raw, ""


def _tidy(keys: object, logins: object, migrated: object = None) -> dict:
    doc = {"keys": {k: str(v).strip() for k, v in keys.items() if k in NAMES and str(v).strip()} if isinstance(keys, dict) else {},
            "logins": json.loads(json.dumps(logins)) if isinstance(logins, dict) else {},
            "migrated": sorted({name for name in migrated if isinstance(name, str)}) if isinstance(migrated, list) else []}
    custom = doc["logins"].get("custom")
    if isinstance(custom, list):
        custom.sort(key=lambda entry: -len(_host(entry)))  # stable for equally specific hosts
    return doc


def _document(raw: dict | None) -> dict:
    raw = raw or {}
    return _tidy(raw.get("keys"), raw.get("logins"), raw.get("migrated"))


def _lock_file():
    return FILE.with_name(f".{FILE.name}.lock")


def _try_lock(fd: int, release: bool = False) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextlib.contextmanager
def _process_lock():
    """A persistent sibling inode serializes every read/merge/write/cleanup transaction."""
    fd, acquired, problem = None, False, ""
    try:
        paths.ensure_parent(FILE)
        target = _lock_file()
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise OSError("credential lock is not a private regular file")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags, 0o600)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (info is not None and
                (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
            raise OSError("credential lock changed while opening")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:
            os.chmod(target, 0o600)
        if os.name == "nt" and opened.st_size == 0:
            os.write(fd, b"\0")  # msvcrt locks a single byte at offset zero
            os.fsync(fd)
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                _try_lock(fd)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    except (OSError, ImportError) as exc:
        problem = f"Could not acquire the credential lock: {exc}"
    try:
        yield problem
    finally:
        if fd is not None:
            if acquired:
                with contextlib.suppress(OSError):
                    _try_lock(fd, release=True)
            os.close(fd)  # never unlink a lock inode that another process may be waiting on


def _write(doc: dict, *, publish: bool = False) -> bool:
    """Publish atomically and durably; marker-only documents must survive forgetting."""
    tmp = backup = None
    replaced = False
    try:
        if os.path.lexists(FILE) and not stat.S_ISREG(FILE.lstat().st_mode):
            return False
        if doc["keys"] or doc["logins"] or doc.get("migrated") or publish:
            sealed = json.dumps(secretstore.seal(json.dumps(doc).encode("utf-8")))
            if len(sealed.encode("utf-8")) > _READ_LIMIT:
                return False
            paths.ensure_parent(FILE)
            fd, name = tempfile.mkstemp(prefix=FILE.name + ".", suffix=".tmp", dir=FILE.parent)
            tmp = FILE.parent / name
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.chmod(tmp, 0o600)
                stream.write(sealed)
                stream.flush()
                os.fsync(stream.fileno())
            if publish:
                verified, _ = _open(tmp)
                if verified is None or _document(verified) != doc:
                    return False
                if FILE.exists():
                    backup = tmp.with_name(tmp.name + ".previous")
                    original = _read_bytes(FILE)
                    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(original)
                        stream.flush()
                        os.fsync(stream.fileno())
            os.replace(tmp, FILE)
            replaced = True
            if publish:
                verified, _ = _open(FILE)
                if verified is None or _document(verified) != doc:
                    raise OSError("credential publication could not be verified")
        else:
            FILE.unlink(missing_ok=True)
        if os.name != "nt" and FILE.parent.is_dir():
            fd = os.open(FILE.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return True
    except (OSError, secretstore.SealError):
        if publish and replaced:
            try:
                if backup is not None:
                    os.replace(backup, FILE)
                else:
                    FILE.unlink(missing_ok=True)
                if os.name != "nt":
                    fd = os.open(FILE.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            except OSError:
                backup = None  # retain a recovery copy if the filesystem also refuses rollback
        return False
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        if backup is not None:
            with contextlib.suppress(OSError):
                backup.unlink(missing_ok=True)


def _file_stamp() -> tuple[int, int, int] | None:
    try:
        st = FILE.lstat()
        return st.st_mtime_ns, st.st_size, st.st_ino
    except OSError:
        return None


def _host(entry) -> str:
    return str(entry.get("host", "")).strip().casefold() if isinstance(entry, dict) else ""


def _merge_logins(current: dict, incoming: dict) -> None:
    for name, value in incoming.items():
        if name != "custom":
            current.setdefault(name, value)
            continue
        saved = current.setdefault("custom", [])
        if not isinstance(saved, list) or not isinstance(value, list):
            raise ValueError("custom logins are not a list")
        hosts = {_host(entry) for entry in saved if _host(entry)}
        for entry in value:
            host = _host(entry)
            if host and host not in hosts:
                saved.append(entry)
                hosts.add(host)
            elif not host and entry not in saved:
                saved.append(entry)  # retain unrecognized entries too; migration must not discard them


def _contains(doc: dict, incoming: dict) -> bool:
    """Every imported name/host must be present; existing values win conflicts."""
    if not incoming["keys"].keys() <= doc["keys"].keys():
        return False
    for name, value in incoming["logins"].items():
        if name != "custom":
            if name not in doc["logins"]:
                return False
        else:
            saved = doc["logins"].get("custom", [])
            if not isinstance(saved, list) or not isinstance(value, list):
                return False
            hosts = {_host(entry) for entry in saved if _host(entry)}
            if any((_host(entry) not in hosts) if _host(entry) else (entry not in saved) for entry in value):
                return False
    return True


def _read_locked() -> tuple[dict, str, bool]:
    """Finalize migration once, under the process lock; return whether edits are safe."""
    global _migration
    prior = _migration if _migration is not None and _migration[0] == FILE else None
    if prior is None:
        _migration = (FILE, _document(None), _read_only("Credential migration did not finish"), False)
    found, problem = _open(FILE)
    doc = _document(found)
    if prior is not None:
        if not prior[3]:
            return _document(prior[1]), prior[2], False
        return doc, problem or prior[2], not problem
    if _FINALIZED in doc["migrated"]:
        _migration = (FILE, doc, problem, True)
        return doc, problem, True
    if found is None and (problem or os.path.lexists(FILE)):
        _migration = (FILE, doc, problem, False)
        return doc, problem, False  # never replace an unreadable unified store
    sources = [(LEGACY_KEYS_FILE, "keys", True), (LEGACY_LOGINS_FILE, "logins", True)]
    if paths.layout() == "xdg" and FILE.parent != APP_DIR:
        sources += [(APP_DIR / "distill_keys.json", "unified", False),
                    (APP_DIR / "distill_apikeys.json", "keys", False),
                    (APP_DIR / "distill_logins.json", "logins", False)]
    imported, problems, existing = [], [], {*doc["migrated"]}
    # Read the portable marker store before considering any fallback credentials.
    # If it cannot be unlocked, its forget decisions cannot be established.
    app_store = APP_DIR / "distill_keys.json"
    app_document, app_problem = _open(app_store) if any(kind == "unified" for _, kind, _ in sources) else (None, "")
    blocked = bool(app_problem)
    if blocked:
        problems.append(app_problem)
    for path, kind, remove in sources:
        source = f"{'local' if remove else 'app'}:{path.name}"
        if os.path.lexists(path):
            existing.add(source)
        if source in doc["migrated"] or path == FILE:
            continue
        if blocked:
            continue
        old, old_problem = (app_document, app_problem) if kind == "unified" else _open(path)
        if old_problem:
            problems.append(old_problem)
        if old is None:
            continue
        incoming = _tidy(old.get("keys"), old.get("logins")) if kind == "unified" else _tidy(
            old if kind == "keys" else None, old if kind == "logins" else None)
        merged = _document(doc)
        try:
            _merge_logins(merged["logins"], incoming["logins"])
        except ValueError as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        for name, value in incoming["keys"].items():
            merged["keys"].setdefault(name, value)
        if kind == "unified":
            # A portable store's local sources are app-folder sources from XDG.
            names = {"distill_keys.json", "distill_apikeys.json", "distill_logins.json"}
            translated = {f"app:{name}" for marker in _document(old)["migrated"]
                          for scope, separator, name in [marker.partition(":")]
                          if separator and scope in ("local", "app") and name in names}
            merged["migrated"] = sorted({*merged["migrated"], *translated})
        doc = merged
        imported.append((path, source, remove, old, incoming))
    doc = _document(doc)
    doc["migrated"] = sorted({*doc["migrated"], *existing, _FINALIZED})
    saved = all(_contains(doc, item[4]) for item in imported) and _write(doc, publish=True)
    if not saved:
        problems.append("Could not durably publish and verify the credential migration; originals were retained.")
    else:
        for path, _source, remove, old, _incoming in imported:
            if remove and _open(path) == (old, ""):
                with contextlib.suppress(OSError):
                    path.unlink()
        if problems:
            problems.append("Unavailable legacy sources were left untouched and will not be imported later.")
    problem = "; ".join(problems)
    if not saved:
        problem = _read_only(problem)
    _migration = (FILE, doc, problem, saved)
    return doc, problem, saved


def _read() -> tuple[dict, str]:
    global _migration
    with _lock:
        if _migration is not None and _migration[0] == FILE and not _migration[3]:
            return _document(_migration[1]), _migration[2]
        return _read_with_lock()


def _read_with_lock() -> tuple[dict, str]:
    global _migration
    with _process_lock() as lock_problem:
        if lock_problem:
            if _migration is None or _migration[0] != FILE:
                _migration = (FILE, _document(None), _read_only(lock_problem), False)
            found, problem = _open(FILE)
            doc = _document(found)
            if not _migration[3]:
                _migration = (FILE, doc, _migration[2], False)
                return doc, _migration[2]
            return doc, problem or lock_problem
        doc, problem, _writable = _read_locked()
        return doc, problem


def _state() -> dict:
    global _cache, _stamp, _problem
    with _lock:
        if _cache is None or _stamp != _file_stamp():
            _cache, _problem = _read()
            _stamp = _file_stamp()
        return _cache


def _store(doc: dict) -> bool:
    global _cache, _stamp, _problem
    saved = _write(doc)
    if not saved:
        _problem = "Could not save logins and keys. Check the config directory permissions and try again."
    # DEFERRED: upstream updates the cache even when persistence fails.
    _cache, _stamp = doc, _file_stamp()
    return saved


def _change(update) -> bool:
    global _problem
    with _lock:
        if _migration is not None and _migration[0] == FILE and not _migration[3]:
            _problem = _migration[2]
            return False
        return _change_locked(update)


def _change_locked(update) -> bool:
    global _cache, _stamp, _problem, _migration
    with _process_lock() as lock_problem:
        if lock_problem:
            if _migration is None or _migration[0] != FILE:
                _migration = (FILE, _document(_cache), _read_only(lock_problem), False)
            _problem = _migration[2] if not _migration[3] else lock_problem
            return False
        # Another process may have saved since our cached read. Always read anew
        # under this same lock, and finish migration before applying an edit/forget.
        current, _problem, writable = _read_locked()
        if not writable:
            return False
        _cache, _stamp = current, _file_stamp()
        doc = _document(current)
        update(doc)
        doc = _document(doc)  # normalize custom-host specificity after every update/merge
        return _store(doc)


# ---- keys ---------------------------------------------------------------------------------------------------------
def load() -> dict[str, str]:
    """Every saved key, by name. Read again whenever the file changed, so a key saved from Settings is used at once."""
    with _lock:
        return dict(_state()["keys"])


def get(name: str) -> str:
    return load().get(name, "")


def set(name: str, value: str) -> bool:  # noqa: A001
    """Save a key ('' forgets it), retaining durable per-source migration state."""
    if name not in NAMES:
        raise KeyError(name)
    value = (value or "").strip()
    def update(doc):
        if value:
            doc["keys"][name] = value
        else:
            doc["keys"].pop(name, None)
    return _change(update)


# ---- logins (downloads.py holds the live copy; this is only where it is kept) -----------------------------------------
def logins() -> dict:
    """The remembered logins as saved: {"1fichier": {...}, "custom": [...]}. Empty when none were remembered."""
    with _lock:
        return json.loads(json.dumps(_state()["logins"]))


def save_logins(data: dict | None) -> bool:
    """Replace the remembered logins with this snapshot; None forgets them all, keeping keys."""
    saved = json.loads(json.dumps(data)) if data else {}
    return _change(lambda doc: doc.update(logins=saved))


def problem() -> str:
    """'' normally; else why the saved file couldn't be read (a different Windows account or computer, say)."""
    with _lock:
        _state()
        return _problem


def protection() -> str:
    """How the file is protected on this computer, as a phrase ('encrypted so only your Windows account can read it')."""
    return secretstore.describe()


# ---- checking a key -----------------------------------------------------------------------------------------------
def test_github(token: str) -> str:
    """'' when GitHub accepts the token, else a sentence saying what is wrong."""
    req = urllib.request.Request("https://api.github.com/rate_limit", headers={
        "User-Agent": f"{APP_NAME}-keys", "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.load(resp)
        return ""
    except urllib.error.HTTPError as exc:
        return "GitHub rejected the token." if exc.code in (401, 403) else f"GitHub answered {exc.code}."
    except (urllib.error.URLError, OSError, ValueError):
        return "Couldn't reach GitHub."
