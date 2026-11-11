"""Every key and login the person has given Distill, kept in ONE encrypted file: distill_keys.json. No Qt in here.

    keys    SteamGridDB, GitHub, RetroAchievements and RomM (Settings > API keys > Games and Updates)
    logins  archive.org, 1fichier, TorBox and other debrid services, Prowlarr and any other file host
            (Settings > API keys > Downloads). Only written while "Remember" is ticked there.

The file is sealed the way remembered logins always were (see secretstore.py): encrypted for this Windows account, or with
a key in the system keyring, or plain text next to the app where neither exists. Older versions kept these in two files
(distill_apikeys.json and distill_logins.json); they are folded into the new one the first time it is read, and removed once
the new file has been written. A key saved here is used before one from the environment or from the optional
distill/local_keys.py, so a build someone else made can still be given a key afterwards.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import urllib.error
import urllib.request

from . import secretstore
from .constants import APP_DIR, APP_NAME

FILE = APP_DIR / "distill_keys.json"
LEGACY_KEYS_FILE = APP_DIR / "distill_apikeys.json"  # before the one file: the four keys below
LEGACY_LOGINS_FILE = APP_DIR / "distill_logins.json"  # before the one file: the Accounts page's sign-ins
NAMES = ("sgdb", "github", "ra_user", "ra", "romm_url", "romm")  # what can be saved as a key: SteamGridDB (posters, icons), GitHub (rate limit for updates and downloads), RetroAchievements (user name, Web API key), RomM (server address, API token)

_lock = threading.RLock()
_cache: dict | None = None  # {"keys": {name: value}, "logins": {...}} as last read or written
_stamp: tuple[float, int] | None = None
_problem = ""  # set when a keys file exists but could not be unlocked (moved to another account or computer?)


# ---- reading and writing the file ---------------------------------------------------------------------------------
def _open(path) -> tuple[dict | None, str]:
    """What one file holds, and a sentence when it exists but can't be unlocked. (None, '') = no file or nothing usable in it."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("v") == 2:  # sealed: see secretstore.py; the oldest files are plain and still load
            raw = json.loads(secretstore.unseal(raw).decode("utf-8"))
    except secretstore.SealError as exc:
        return None, str(exc)
    except (OSError, ValueError):
        return None, ""
    return (raw if isinstance(raw, dict) else None), ""


def _tidy(keys: object, logins: object) -> dict:
    return {"keys": {k: str(v).strip() for k, v in keys.items() if k in NAMES and str(v).strip()} if isinstance(keys, dict) else {},
            "logins": dict(logins) if isinstance(logins, dict) else {}}


def _write(doc: dict) -> bool:
    """Seal and save, or remove the file when nothing is left. The new copy is written beside the old one and swapped in, so a
    failure part-way never loses what was there."""
    try:
        if doc["keys"] or doc["logins"]:
            tmp = FILE.with_name(FILE.name + ".tmp")
            tmp.write_text(json.dumps(secretstore.seal(json.dumps(doc).encode("utf-8"))), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)  # owner-only where the OS supports it
            os.replace(tmp, FILE)
        else:
            FILE.unlink(missing_ok=True)
        return True
    except (OSError, secretstore.SealError):
        return False


def _file_stamp() -> tuple[float, int] | None:
    try:
        st = FILE.stat()
        return st.st_mtime, st.st_size
    except OSError:
        return None


def _read() -> tuple[dict, str]:
    """The current file, with anything left in the two older files folded in (and those removed once it is safely saved)."""
    found, problem = _open(FILE)
    doc = _tidy((found or {}).get("keys"), (found or {}).get("logins"))
    moved = []
    old_keys, old_problem = _open(LEGACY_KEYS_FILE)
    if old_keys is not None:
        for name, value in _tidy(old_keys, None)["keys"].items():
            doc["keys"].setdefault(name, value)
        moved.append(LEGACY_KEYS_FILE)
    old_logins, login_problem = _open(LEGACY_LOGINS_FILE)
    if old_logins is not None:
        if not doc["logins"]:
            doc["logins"] = dict(old_logins)
        moved.append(LEGACY_LOGINS_FILE)
    if moved and _write(doc):
        reread, _p = _open(FILE)
        if reread is not None and _tidy(reread.get("keys"), reread.get("logins")) == doc:
            for path in moved:
                with contextlib.suppress(OSError):
                    path.unlink()
    return doc, problem or old_problem or login_problem  # a file that couldn't be opened is left where it is


def _state() -> dict:
    global _cache, _stamp, _problem
    with _lock:
        if _cache is None or _stamp != _file_stamp():
            _cache, _problem = _read()
            _stamp = _file_stamp()
        return _cache


def _store(doc: dict) -> None:
    global _cache, _stamp, _problem
    if _write(doc):
        _problem = ""  # what was unreadable has been replaced
    _cache, _stamp = doc, _file_stamp()


# ---- keys ---------------------------------------------------------------------------------------------------------
def load() -> dict[str, str]:
    """Every saved key, by name. Read again whenever the file changed, so a key saved from Settings is used at once."""
    with _lock:
        return dict(_state()["keys"])


def get(name: str) -> str:
    return load().get(name, "")


def set(name: str, value: str) -> None:  # noqa: A001
    """Save a key ('' forgets it). The file goes when no key or login is left."""
    if name not in NAMES:
        raise KeyError(name)
    with _lock:
        doc = {"keys": dict(_state()["keys"]), "logins": _state()["logins"]}
        value = (value or "").strip()
        if value:
            doc["keys"][name] = value
        else:
            doc["keys"].pop(name, None)
        _store(doc)


# ---- logins (downloads.py holds the live copy; this is only where it is kept) -----------------------------------------
def logins() -> dict:
    """The remembered logins as saved: {"1fichier": {...}, "custom": [...]}. Empty when none were remembered."""
    with _lock:
        return json.loads(json.dumps(_state()["logins"]))


def save_logins(data: dict | None) -> None:
    """Remember these logins, or forget them all (None / empty) while keeping the keys."""
    with _lock:
        doc = {"keys": dict(_state()["keys"]), "logins": json.loads(json.dumps(data)) if data else {}}
        _store(doc)


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
