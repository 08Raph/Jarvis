from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
import unicodedata
from collections import defaultdict, deque

# OWASP password-storage guidance for scrypt: N=2^16, r=8, p=2 (about 64 MiB of memory per hash). Older hashes (N=2^14, p=1) still verify and are upgraded at the next sign-in.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 16, 8, 2
SCRYPT_MAXMEM = 256 * 1024 * 1024
_MAX_N, _MAX_R, _MAX_P = 2 ** 20, 16, 16
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
_COMMON = {"password", "password1", "1234567890", "qwertyuiop", "iloveyou12", "letmein123", "administrator", "welcome123"}


def normalize_username(u: str) -> str:
    return (u or "").strip().lower()


def valid_username(u: str) -> bool:
    return bool(_USERNAME.match(u))


def password_problem(pw: str, username: str) -> str | None:
    if not isinstance(pw, str) or len(pw) < 10:
        return "password must be at least 10 characters"
    if len(pw) > 200:
        return "password is too long (max 200)"
    if pw.lower() in _COMMON or pw.lower() == username:
        return "password is too easy to guess"
    if len(set(pw)) < 5:
        return "password needs more variety"
    return None


_DISPLAY_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def display_name_problem(name) -> str | None:
    """A display name is shown to the user only. 1-40 characters, no control or direction-override characters, not blank."""
    if not isinstance(name, str):
        return "display name must be text"
    n = unicodedata.normalize("NFC", name).strip()
    if not 1 <= len(n) <= 40:
        return "display name must be 1-40 characters"
    if _DISPLAY_BAD.search(n):
        return "display name contains characters that are not allowed"
    return None


def clean_display_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).strip()


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        if not (2 <= n <= _MAX_N and n & (n - 1) == 0 and 1 <= r <= _MAX_R and 1 <= p <= _MAX_P):
            return False                       # a stored value outside sane bounds is never used to size a computation
        calc = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=n, r=r, p=p, dklen=32, maxmem=SCRYPT_MAXMEM)
        return hmac.compare_digest(calc, bytes.fromhex(dk))
    except (ValueError, TypeError, MemoryError):
        return False


def needs_rehash(stored: str) -> bool:
    """True when a stored hash was made with weaker parameters than the current ones (it is upgraded after a correct sign-in)."""
    try:
        _, n, r, p, _s, _d = stored.split("$")
        return (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except ValueError:
        return True


DUMMY_HASH = hash_password("dummy-password-for-timing")  # verified against when a username does not exist


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()  # only the digest is stored


def new_recovery_code() -> str:
    raw = secrets.token_hex(10).upper()
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


class RateLimiter:
    """Sliding-window limiter, in memory. These are ABUSE limits (login guessing, flooding), not usage quotas."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = self.clock()
        q = self._hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self._hits) > 20000:  # bound memory
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > window][:10000]:
                self._hits.pop(k, None)
        return True
