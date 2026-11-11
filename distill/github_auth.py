"""The GitHub token, if there is one. Without it GitHub allows 60 requests an hour per IP, which a few launches can use up
(each tool is checked for updates at start-up); with a read-only token it is 5,000.

Looked for in this order: the key saved in Settings > API keys > Updates (apikeys.py), $GITHUB_TOKEN / $GH_TOKEN, then
`GITHUB_TOKEN` in distill/local_keys.py (an optional, git-ignored file that updates never overwrite; see
local_keys.example.py).
"""
from __future__ import annotations

import base64
import hashlib
import os

from . import apikeys

try:
    from .local_keys import GITHUB_TOKEN as _LOCAL_TOKEN
except ImportError:
    _LOCAL_TOKEN = ""


_SALT = b"distill-keys-v1"


def _stream(n: int) -> bytes:
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(_SALT + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:n]


def seal(plain: str) -> str:
    """Obfuscate a key for local_keys.py (`python -m distill.github_auth <key>`). Hides it from a plain-text search of the exe; it is not real secrecy."""
    raw = plain.encode()
    return "enc:" + base64.b64encode(bytes(a ^ b for a, b in zip(raw, _stream(len(raw))))).decode()


def reveal(value: str) -> str:
    """Undo seal(); a value without the enc: prefix is returned as is."""
    value = str(value or "").strip()
    if not value.startswith("enc:"):
        return value
    try:
        raw = base64.b64decode(value[4:])
        return bytes(a ^ b for a, b in zip(raw, _stream(len(raw)))).decode()
    except Exception:
        return ""


def github_token() -> str:
    return (apikeys.get("github") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or reveal(_LOCAL_TOKEN)).strip()


def github_headers(headers: dict) -> dict:
    """`headers` plus the token, for a request to api.github.com (and only that: never send it anywhere else)."""
    if token := github_token():
        return {**headers, "Authorization": f"Bearer {token}"}
    return headers


if __name__ == "__main__":  # python -m distill.github_auth <key>   prints the obfuscated form for local_keys.py
    import sys

    for _arg in sys.argv[1:]:
        print(seal(_arg))
