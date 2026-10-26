"""Stores the sign-in token on this computer. On Windows it is encrypted with DPAPI (tied to the Windows user account).
Elsewhere it is a user-only file. If Windows encryption fails the token is NOT written (you will sign in each time) rather
than stored in plain text. DPAPI code is UNTESTED (written without Windows access); the file logic is tested."""
from __future__ import annotations

import os
from pathlib import Path


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inb = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    outb = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData  # type: ignore[attr-defined]
    if not fn(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb)):
        raise OSError("Windows could not encrypt/decrypt the stored sign-in")
    try:
        return ctypes.string_at(outb.pbData, outb.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outb.pbData)  # type: ignore[attr-defined]


class TokenStore:
    def __init__(self, path: str | Path, protect=None, unprotect=None, windows: bool | None = None):
        self.path = Path(path)
        self.windows = (os.name == "nt") if windows is None else windows
        self._protect = protect or (lambda b: _dpapi(b, True))
        self._unprotect = unprotect or (lambda b: _dpapi(b, False))

    def save(self, token: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.windows:
            try:
                blob = self._protect(token.encode("utf-8")).hex()
            except Exception:
                self.clear()
                return False
            self.path.write_text("dpapi:" + blob, encoding="ascii")
            return True
        self.path.write_text("plain:" + token, encoding="ascii")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return True

    def load(self) -> str | None:
        try:
            raw = self.path.read_text(encoding="ascii").strip()
            if raw.startswith("dpapi:"):
                return self._unprotect(bytes.fromhex(raw[6:])).decode("utf-8") if self.windows else None
            if raw.startswith("plain:") and not self.windows:
                return raw[6:] or None
        except (OSError, ValueError):
            pass
        return None

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass
