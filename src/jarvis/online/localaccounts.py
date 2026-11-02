"""Accounts that live on THIS PC: sign up, log in, log out, change password, recover with a code, delete. No server, no address, no network.

WHAT IT IS: a small, local sign-in for the people who use this copy of Jarvis. Names and passwords never leave the computer. Passwords are stored only as scrypt hashes with a
unique salt (never the password itself); the one-time recovery code is hashed the same way; "stay signed in" keeps a random token whose SHA-256 digest is stored.
WHAT IT IS NOT: it does not encrypt your chats, memory or files, and anyone who can read your Windows user folder can still read them: it separates who is using Jarvis, it is
not a lock on the data. Jarvis keeps working without an account. The online service account (jarvis.online.account) is a separate, optional thing and is not used here.
Sign-up rules are the same as the online ones (jarvis.online.rules), so a person is never told two different things."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from jarvis.online import rules

SCRYPT = dict(n=2 ** 15, r=8, p=1, maxmem=96 * 1024 * 1024, dklen=32)
MAX_ACCOUNTS = 20
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
FILE = "local_accounts.json"
SESSION_FILE = "local_session.json"
WELCOME_FILE = "welcome.json"
GENERIC_BAD_LOGIN = "That username and password do not match."


@dataclass
class Result:
    ok: bool
    code: str
    message: str
    data: object = None


def _hash(secret: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(secret.encode("utf-8"), salt=salt, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def _verify(secret: str, stored: str) -> bool:
    try:
        kind, salt_b, h_b = stored.split("$")
        if kind != "scrypt":
            return False
        salt, want = base64.b64decode(salt_b), base64.b64decode(h_b)
        got = hashlib.scrypt(secret.encode("utf-8"), salt=salt, **SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def _new_recovery_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(10)).decode().rstrip("=")
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


class LocalAccounts:
    def __init__(self, profile_dir: Path, guest: bool = False):
        self.dir = Path(profile_dir)
        self.guest = guest
        self._mem_session: dict | None = None           # a guest or "do not stay signed in" session lives in memory only

    # ------------------------------------------------------------ storage
    @property
    def file(self) -> Path:
        return self.dir / FILE

    @property
    def session_file(self) -> Path:
        return self.dir / SESSION_FILE

    def _load(self) -> dict:
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("users"), dict):
                return d
        except (OSError, ValueError):
            pass
        return {"version": 1, "users": {}}

    def _save(self, d: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.file)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def ask_at_start(self) -> bool:
        """Whether Jarvis opens the Log in / Sign up window at startup when nobody is signed in. On by default; "Don't ask me again" turns it off (the Account page turns it back on)."""
        if self.guest:
            return False
        try:
            raw = json.loads((self.dir / WELCOME_FILE).read_text(encoding="utf-8"))
            return not (isinstance(raw, dict) and raw.get("ask") is False)
        except (OSError, ValueError):
            return True

    def set_ask_at_start(self, on: bool) -> None:
        if self.guest:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / WELCOME_FILE).write_text(json.dumps({"ask": bool(on)}), encoding="utf-8")

    def should_welcome(self) -> bool:
        """True when the startup window should be shown: not a guest, nobody signed in (a kept sign-in is honoured), and the person has not turned it off."""
        return (not self.guest) and self.ask_at_start() and self.current() is None

    def has_accounts(self) -> bool:
        return bool(self._load()["users"])

    # ------------------------------------------------------------ checks (the window shows these before sending anything)
    def validate_signup(self, username: str, display_name: str, pw: str, pw2: str, accepted: bool) -> dict:
        """field -> message for everything wrong; an empty dict means it can go ahead."""
        out: dict = {}
        u = rules.normalize_username(username)
        p = rules.username_problem(u)
        if p:
            out["username"] = p
        elif u in self._load()["users"]:
            out["username"] = "That username is already taken on this PC."
        if display_name.strip():
            p = rules.display_name_problem(display_name)
            if p:
                out["display_name"] = p
        p = rules.password_problem(pw, u)
        if p:
            out["password"] = p
        elif pw != pw2:
            out["password2"] = "The two passwords are not the same."
        if not accepted:
            out["accepted"] = "Please tick the box to confirm you understand how accounts work here."
        return out

    # ------------------------------------------------------------ actions
    def sign_up(self, username: str, display_name: str, pw: str, pw2: str, accepted: bool, stay: bool = False) -> Result:
        if self.guest:
            return Result(False, "guest", "Accounts are not available in a guest session.")
        problems = self.validate_signup(username, display_name, pw, pw2, accepted)
        if problems:
            return Result(False, "invalid", " ".join(problems.values()), problems)
        d = self._load()
        if len(d["users"]) >= MAX_ACCOUNTS:
            return Result(False, "full", f"This PC already has {MAX_ACCOUNTS} accounts.")
        u = rules.normalize_username(username)
        code = _new_recovery_code()
        d["users"][u] = {"display": display_name.strip() or u, "pw": _hash(pw), "recovery": _hash(code.replace("-", "")), "created": time.time(), "failures": 0, "locked_until": 0,
                         "tokens": []}
        self._save(d)
        res = self._start_session(u, stay)
        return Result(True, "created", f"Welcome, {d['users'][u]['display']}. Your account was created and you are signed in.", {"recovery_code": code, **res})

    def sign_in(self, username: str, pw: str, stay: bool = False) -> Result:
        if self.guest:
            return Result(False, "guest", "Accounts are not available in a guest session.")
        u = rules.normalize_username(username)
        d = self._load()
        rec = d["users"].get(u)
        now = time.time()
        if rec is None:
            _verify(pw or "", _hash("x"))                       # equal work for unknown names, so timing does not show which names exist
            return Result(False, "bad_login", GENERIC_BAD_LOGIN)
        if rec.get("locked_until", 0) > now:
            _verify(pw or "", rec["pw"])
            return Result(False, "bad_login", GENERIC_BAD_LOGIN + " (Too many wrong tries: wait a few minutes, or use the recovery code.)")
        if not _verify(pw or "", rec["pw"]):
            rec["failures"] = rec.get("failures", 0) + 1
            if rec["failures"] >= MAX_FAILURES:
                rec["locked_until"], rec["failures"] = now + LOCK_SECONDS, 0
            self._save(d)
            return Result(False, "bad_login", GENERIC_BAD_LOGIN)
        rec["failures"], rec["locked_until"] = 0, 0
        self._save(d)
        res = self._start_session(u, stay)
        return Result(True, "signed_in", f"Welcome back, {rec['display']}.", res)

    def _start_session(self, user: str, stay: bool) -> dict:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self._mem_session = {"user": user, "token": token}
        if stay and not self.guest:
            d = self._load()
            rec = d["users"].get(user)
            if rec is not None:
                rec["tokens"] = (rec.get("tokens", []) + [{"d": digest, "t": time.time()}])[-5:]
                self._save(d)
                self.dir.mkdir(parents=True, exist_ok=True)
                self.session_file.write_text(json.dumps({"user": user, "token": token}), encoding="utf-8")
        else:
            self._clear_session_file()
        return {"user": user}

    def _clear_session_file(self) -> None:
        try:
            self.session_file.unlink()
        except OSError:
            pass

    def current(self) -> dict | None:
        """The signed-in person ({'user','display'}) or None. Reads local files only."""
        s = self._mem_session
        if s is None and not self.guest:
            try:
                raw = json.loads(self.session_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("user"), str) and isinstance(raw.get("token"), str):
                    digest = hashlib.sha256(raw["token"].encode()).hexdigest()
                    rec = self._load()["users"].get(raw["user"])
                    if rec and any(hmac.compare_digest(t["d"], digest) and time.time() - t["t"] < 90 * 86400 for t in rec.get("tokens", [])):
                        s = self._mem_session = {"user": raw["user"], "token": raw["token"]}
            except (OSError, ValueError, KeyError, TypeError):
                s = None
        if s is None:
            return None
        rec = self._load()["users"].get(s["user"])
        if rec is None:
            self._mem_session = None
            return None
        return {"user": s["user"], "display": rec["display"]}

    def sign_out(self) -> Result:
        s = self._mem_session
        if s is not None and not self.guest:
            d = self._load()
            rec = d["users"].get(s["user"])
            if rec is not None:
                digest = hashlib.sha256(s["token"].encode()).hexdigest()
                rec["tokens"] = [t for t in rec.get("tokens", []) if t["d"] != digest]
                self._save(d)
        self._mem_session = None
        self._clear_session_file()
        return Result(True, "signed_out", "You are signed out.")

    def set_display_name(self, name: str) -> Result:
        cur = self.current()
        if cur is None:
            return Result(False, "not_signed_in", "Sign in first.")
        p = rules.display_name_problem(name)
        if p:
            return Result(False, "invalid", p)
        d = self._load()
        d["users"][cur["user"]]["display"] = name.strip()
        self._save(d)
        return Result(True, "ok", "Your display name was changed.")

    def change_password(self, current_pw: str, new: str, new2: str) -> Result:
        cur = self.current()
        if cur is None:
            return Result(False, "not_signed_in", "Sign in first.")
        d = self._load()
        rec = d["users"][cur["user"]]
        if not _verify(current_pw or "", rec["pw"]):
            return Result(False, "bad_login", "The current password is not right.")
        p = rules.password_problem(new, cur["user"])
        if p:
            return Result(False, "invalid", p)
        if new != new2:
            return Result(False, "invalid", "The two new passwords are not the same.")
        rec["pw"] = _hash(new)
        keep = hashlib.sha256((self._mem_session or {}).get("token", "").encode()).hexdigest()
        rec["tokens"] = [t for t in rec.get("tokens", []) if t["d"] == keep]          # every other "stay signed in" is ended
        self._save(d)
        return Result(True, "ok", "Your password was changed.")

    def recover(self, username: str, code: str, new: str, new2: str) -> Result:
        u = rules.normalize_username(username)
        d = self._load()
        rec = d["users"].get(u)
        bad = Result(False, "bad_recovery", "That username and recovery code do not match.")
        if rec is None:
            _verify("x", _hash("x"))
            return bad
        now = time.time()
        if rec.get("locked_until", 0) > now:
            return bad
        if not _verify((code or "").replace("-", "").replace(" ", "").upper(), rec["recovery"]):
            rec["failures"] = rec.get("failures", 0) + 1
            if rec["failures"] >= MAX_FAILURES:
                rec["locked_until"], rec["failures"] = now + LOCK_SECONDS, 0
            self._save(d)
            return bad
        p = rules.password_problem(new, u)
        if p:
            return Result(False, "invalid", p)
        if new != new2:
            return Result(False, "invalid", "The two new passwords are not the same.")
        newcode = _new_recovery_code()
        rec.update(pw=_hash(new), recovery=_hash(newcode.replace("-", "")), failures=0, locked_until=0, tokens=[])
        self._save(d)
        return Result(True, "ok", "Your password was reset. Here is your NEW recovery code: write it down.", {"recovery_code": newcode})

    def delete_account(self, pw: str) -> Result:
        cur = self.current()
        if cur is None:
            return Result(False, "not_signed_in", "Sign in first.")
        d = self._load()
        if not _verify(pw or "", d["users"][cur["user"]]["pw"]):
            return Result(False, "bad_login", "The password is not right, so nothing was deleted.")
        del d["users"][cur["user"]]
        self._save(d)
        self._mem_session = None
        self._clear_session_file()
        return Result(True, "deleted", "Your account was deleted from this PC. Your chats and memory were not touched.")
