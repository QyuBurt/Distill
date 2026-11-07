"""The person's own API keys (Settings > General > API keys): SteamGridDB and GitHub. No Qt in here.

Saved the way remembered logins are (see secretstore.py): encrypted for this Windows account, or with a key in the system
keyring, or plain text in the runtime config directory where neither exists. A key saved here is used before one from the environment or
from the optional distill/local_keys.py, so a build someone else made can still be given a key afterwards.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import urllib.error
import urllib.request

from . import secretstore
from . import paths
from .constants import APP_NAME

FILE = paths.config_dir() / "distill_apikeys.json"
NAMES = ("sgdb", "github")  # what can be saved: SteamGridDB (posters, icons), GitHub (rate limit for updates and downloads)

_lock = threading.RLock()
_cache: dict[str, str] | None = None
_stamp: tuple[float, int] | None = None


def _read() -> dict[str, str]:
    try:
        doc = json.loads(FILE.read_text(encoding="utf-8"))
        data = json.loads(secretstore.unseal(doc).decode("utf-8")) if isinstance(doc, dict) and doc.get("v") == 2 else doc
    except (OSError, ValueError, secretstore.SealError):
        return {}
    return {k: str(v).strip() for k, v in data.items() if k in NAMES and str(v).strip()} if isinstance(data, dict) else {}


def _file_stamp() -> tuple[float, int] | None:
    try:
        st = FILE.stat()
        return st.st_mtime, st.st_size
    except OSError:
        return None


def load() -> dict[str, str]:
    """Every saved key, by name. Read again whenever the file changed, so a key saved from Settings is used at once."""
    global _cache, _stamp
    with _lock:
        if _cache is None or _stamp != _file_stamp():
            _cache, _stamp = _read(), _file_stamp()
        return dict(_cache)


def get(name: str) -> str:
    return load().get(name, "")


def set(name: str, value: str) -> None:  # noqa: A001
    """Save a key ('' forgets it). The file goes when no key is left."""
    global _cache, _stamp
    if name not in NAMES:
        raise KeyError(name)
    with _lock:
        keys = load()
        value = (value or "").strip()
        if value:
            keys[name] = value
        else:
            keys.pop(name, None)
        try:
            if keys:
                paths.ensure_parent(FILE)
                FILE.write_text(json.dumps(secretstore.seal(json.dumps(keys).encode("utf-8"))), encoding="utf-8")
                with contextlib.suppress(OSError):
                    os.chmod(FILE, 0o600)  # owner-only where the OS supports it
            else:
                FILE.unlink(missing_ok=True)
        except OSError:
            pass
        _cache, _stamp = keys, _file_stamp()


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
