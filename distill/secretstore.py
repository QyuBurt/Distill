"""Protecting saved logins at rest. No Qt in here.

Windows: the logins file is encrypted with DPAPI, so only your Windows account on this computer can read it (no
password to remember, nothing extra to install). Elsewhere: AES-GCM with a random key kept in the system keyring, when
the `keyring` and `pycryptodomex` packages are present. With neither, the file is plain text (owner-only where the OS
allows), and the Logins window says so. Moving the file to another computer or account makes it unreadable on purpose."""
from __future__ import annotations

import base64
import ctypes
import os
import sys
from typing import Callable

_ENTROPY = b"Distill logins v2"
KEYRING_SERVICE, KEYRING_USER = "Distill", "logins-key"


class SealError(Exception):
    pass


# ---- Windows DPAPI ------------------------------------------------------------------------------------------------
def _dpapi(data: bytes, protect: bool) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> tuple[Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    src, _keep1 = blob(data)
    ent, _keep2 = blob(_ENTROPY)
    out = Blob()
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    # both calls take: data in, description, entropy, reserved, prompt, flags (1 = never show UI), data out
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):
        raise SealError("Windows could not " + ("protect" if protect else "unlock") + " the logins (different account or computer?)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel.LocalFree(out.pbData)


def _dpapi_seal(data: bytes) -> bytes:
    return _dpapi(data, True)


def _dpapi_open(data: bytes) -> bytes:
    return _dpapi(data, False)


# ---- keyring + AES-GCM ----------------------------------------------------------------------------------------------
def _keyring_key(create: bool) -> bytes | None:
    import keyring  # noqa: PLC0415  (optional)
    got = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    if got:
        return base64.b64decode(got)
    if not create:
        return None
    key = os.urandom(32)
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, base64.b64encode(key).decode("ascii"))
    return key


def _aes():
    try:
        from Cryptodome.Cipher import AES  # noqa: PLC0415
    except ImportError:
        from Crypto.Cipher import AES  # type: ignore  # noqa: PLC0415
    return AES


def _keyring_seal(data: bytes) -> bytes:
    cipher = _aes().new(_keyring_key(True), _aes().MODE_GCM)
    body, tag = cipher.encrypt_and_digest(data)
    return cipher.nonce + tag + body


def _keyring_open(data: bytes) -> bytes:
    key = _keyring_key(False)
    if not key:
        raise SealError("The key for the saved logins is no longer in the system keyring")
    try:
        cipher = _aes().new(key, _aes().MODE_GCM, nonce=data[:16])
        return cipher.decrypt_and_verify(data[32:], data[16:32])
    except ValueError:
        raise SealError("The saved logins can't be unlocked with this keyring") from None


def _keyring_ready() -> bool:
    try:
        import keyring  # noqa: F401, PLC0415
        _aes()
        return True
    except Exception:
        return False


# ---- choosing and using a method --------------------------------------------------------------------------------
METHODS: dict[str, tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]] = {
    "dpapi": (_dpapi_seal, _dpapi_open),
    "keyring": (_keyring_seal, _keyring_open),
    "plain": (lambda b: b, lambda b: b),
}


def best_method() -> str:
    if sys.platform == "win32":
        return "dpapi"
    return "keyring" if _keyring_ready() else "plain"


def describe(method: str | None = None) -> str:
    return {"dpapi": "encrypted so only your Windows account can read it",
            "keyring": "encrypted, with the key kept in your system keyring",
            "plain": "stored as plain text next to this app"}[method or best_method()]


def seal(data: bytes, method: str | None = None) -> dict:
    method = method or best_method()
    try:
        blob = METHODS[method][0](data)
    except Exception as exc:  # a broken keyring or API must not lose the logins: fall back to plain
        if method == "plain":
            raise SealError(str(exc)) from None
        method, blob = "plain", data
    return {"v": 2, "method": method, "data": base64.b64encode(blob).decode("ascii")}


def unseal(doc: dict) -> bytes:
    method = doc.get("method")
    if method not in METHODS:
        raise SealError("The saved logins use a method this version doesn't know")
    try:
        return METHODS[method][1](base64.b64decode(doc.get("data", "")))
    except SealError:
        raise
    except Exception as exc:
        raise SealError(f"The saved logins can't be unlocked: {exc}") from None
