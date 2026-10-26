from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
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


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, bytes.fromhex(dk))
    except (ValueError, TypeError):
        return False


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
