from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from jarvis_server import security as sec
from jarvis_server.backends import NoModelBackend
from jarvis_server.db import DB
from jarvis.telemetry.schema import SchemaError, validate_event

SESSION_DAYS = 30
MAX_SESSION_DAYS = 90            # a session can be renewed, but never lives longer than this from the original sign-in
MAX_BODY = 64 * 1024
MAX_MESSAGES, MAX_MSG_CHARS, MAX_TOTAL_CHARS = 40, 8000, 32000
LOCK_AFTER, LOCK_SECONDS = 5, 15 * 60
_CONV = re.compile(r"^[A-Za-z0-9-]{1,64}$")
DEFAULT_TERMS = Path(__file__).with_name("terms_en.md")
DEFAULT_LEARNING_NOTICE = Path(__file__).with_name("learning_notice_en.md")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class Service:
    def __init__(self, db: DB, backend=None, terms_text: str | None = None, learning_notice: str | None = None, trust_proxy: bool = False,
                 max_concurrent_generations: int = 2, clock=time.time, telemetry_enabled: bool = True,
                 telemetry_retention_days: int = 180):
        self.db, self.backend = db, backend or NoModelBackend()
        self.terms = terms_text if terms_text is not None else DEFAULT_TERMS.read_text(encoding="utf-8")
        self.terms_sha = hashlib.sha256(self.terms.encode("utf-8")).hexdigest()
        self.learning_notice = learning_notice if learning_notice is not None else DEFAULT_LEARNING_NOTICE.read_text(encoding="utf-8")
        self.learning_notice_sha = hashlib.sha256(self.learning_notice.encode("utf-8")).hexdigest()
        self.trust_proxy, self.clock = trust_proxy, clock
        self.limiter = sec.RateLimiter(clock=time.monotonic)
        self.gen_slots = threading.BoundedSemaphore(max_concurrent_generations)
        self.telemetry_enabled, self.telemetry_retention_days = telemetry_enabled, telemetry_retention_days
        self.purge_telemetry()

    def purge_telemetry(self) -> int:
        cutoff = self.clock() - self.telemetry_retention_days * 86400
        return self.db.q("DELETE FROM telemetry WHERE received_at < ?", (cutoff,)).rowcount

    # ---------------------------------------------------------------- entry point
    def handle(self, method: str, path: str, headers: dict, body: bytes, ip: str) -> tuple[int, dict]:
        try:
            if not self.limiter.allow(f"ip:{ip}", 240, 60):
                raise ApiError(429, "rate_limited", "Too many requests. Please slow down.")
            route = (method.upper(), path.split("?")[0])
            fn = self.ROUTES.get(route)
            if fn is None:
                raise ApiError(404, "not_found", "Unknown endpoint.")
            data = self._json(body) if route[0] == "POST" else {}
            return 200, fn(self, data, headers, ip)
        except ApiError as e:
            return e.status, {"error": e.code, "message": e.message}
        except Exception:  # never leak internals to clients
            self.db.event("internal_error")
            return 500, {"error": "internal", "message": "Something went wrong on the server."}

    @staticmethod
    def _json(body: bytes) -> dict:
        if len(body) > MAX_BODY:
            raise ApiError(413, "too_large", "Request too large.")
        try:
            d = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "Request body must be JSON.")
        if not isinstance(d, dict):
            raise ApiError(400, "bad_json", "Request body must be a JSON object.")
        return d

    # ---------------------------------------------------------------- helpers
    def _user(self, headers: dict):
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if not auth.startswith("Bearer ") or len(auth) > 200:
            raise ApiError(401, "auth_required", "Sign in first.")
        row = self.db.one("SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_digest = ?",
                          (sec.token_digest(auth[7:]),))
        if row is None or row["expires_at"] < self.clock():
            raise ApiError(401, "auth_required", "Your session is not valid. Sign in again.")
        if row["status"] != "active":
            raise ApiError(403, "account_unavailable", "This account is not available.")
        return row

    @staticmethod
    def _bearer(headers: dict) -> str:
        return (headers.get("authorization") or headers.get("Authorization") or "")[7:]

    def _session(self, uid: int, origin_at: float | None = None) -> str:
        t = sec.new_token()
        now = self.clock()
        self.db.q("INSERT INTO sessions(token_digest, user_id, created_at, expires_at, origin_at) VALUES(?,?,?,?,?)",
                  (sec.token_digest(t), uid, now, now + SESSION_DAYS * 86400, origin_at if origin_at is not None else now))
        return t

    @staticmethod
    def _str(d: dict, key: str, maxlen: int = 300) -> str:
        v = d.get(key)
        if not isinstance(v, str) or not v or len(v) > maxlen:
            raise ApiError(400, "bad_request", f"'{key}' is required.")
        return v

    # ---------------------------------------------------------------- routes
    def health(self, d, h, ip):
        return {"status": "ok", "model_connected": bool(self.backend.available()), "terms_sha256": self.terms_sha}

    def get_terms(self, d, h, ip):
        return {"sha256": self.terms_sha, "text": self.terms, "learning_default": "off",
                "learning_notice": self.learning_notice, "learning_notice_sha256": self.learning_notice_sha}

    def _learning_active(self, u) -> bool:
        """Training use is allowed only with an explicit, current consent. A stored 1 from the old ON-by-default era
        ('legacy_default') or a consent to an older notice text does NOT count."""
        return bool(u["learning_opt_in"]) and u["learning_basis"] == "explicit" and u["learning_consent_sha"] == self.learning_notice_sha

    def _consent_ok(self, d) -> bool:
        return d.get("learning_consent_sha256") == self.learning_notice_sha

    def register(self, d, h, ip):
        if not self.limiter.allow(f"reg:{ip}", 5, 3600):
            raise ApiError(429, "rate_limited", "Too many sign-ups from this network. Try later.")
        username = sec.normalize_username(self._str(d, "username", 64))
        password = self._str(d, "password", 400)
        if not sec.valid_username(username):
            raise ApiError(400, "bad_username", "Username: 3-32 characters, letters, digits, . _ - only.")
        problem = sec.password_problem(password, username)
        if problem:
            raise ApiError(400, "weak_password", problem)
        if d.get("accepted_terms_sha256") != self.terms_sha:
            raise ApiError(400, "terms_not_accepted", "You must accept the current terms before creating an account.")
        if d.get("age_confirmed") is not True:
            raise ApiError(400, "age_not_confirmed", "You must confirm you are at least 16.")
        learning = d.get("learning_opt_in", False)
        if not isinstance(learning, bool):
            raise ApiError(400, "bad_request", "'learning_opt_in' must be true or false.")
        if learning and not self._consent_ok(d):
            raise ApiError(400, "learning_consent_required",
                           "Learning stays OFF unless you confirm the current learning notice (learning_consent_sha256).")
        display = d.get("display_name", username)
        problem = sec.display_name_problem(display)
        if problem:
            raise ApiError(400, "bad_display_name", problem[0].upper() + problem[1:] + ".")
        display = sec.clean_display_name(display)
        code = sec.new_recovery_code()
        now = self.clock()
        try:
            cur = self.db.q("INSERT INTO users(username, pw_hash, recovery_hash, created_at, terms_sha, terms_accepted_at, "
                            "age_confirmed, learning_opt_in, learning_changed_at, learning_basis, learning_consent_sha, learning_consent_at, display_name, pw_changed_at) "
                            "VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
                            (username, sec.hash_password(password), sec.hash_password(code), now, self.terms_sha, now,
                             int(learning), now, "explicit" if learning else "none",
                             self.learning_notice_sha if learning else None, now if learning else None, display, now))
        except sqlite3.IntegrityError:
            raise ApiError(409, "username_unavailable", "That username is not available.")
        self.db.event("register", cur.lastrowid)
        return {"token": self._session(cur.lastrowid), "recovery_code": code, "learning_opt_in": learning}

    def login(self, d, h, ip):
        username = sec.normalize_username(self._str(d, "username", 64))
        password = self._str(d, "password", 400)
        if not (self.limiter.allow(f"login-ip:{ip}", 20, 600) and self.limiter.allow(f"login-user:{username}", 10, 600)):
            raise ApiError(429, "rate_limited", "Too many sign-in attempts. Try again later.")
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        generic = ApiError(401, "bad_credentials", "Wrong username or password. After several wrong attempts sign-in pauses for a while.")
        if row is None:
            sec.verify_password(password, sec.DUMMY_HASH)  # equalize timing
            raise generic
        if row["locked_until"] > self.clock():
            sec.verify_password(password, sec.DUMMY_HASH)
            raise generic                                   # the same answer as an unknown user or a wrong password: a lock does not reveal that the account exists
        if not sec.verify_password(password, row["pw_hash"]):
            fails = row["failed_logins"] + 1
            lock = self.clock() + LOCK_SECONDS if fails >= LOCK_AFTER else 0
            self.db.q("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?", (0 if lock else fails, lock, row["id"]))
            self.db.event("login_failed", row["id"])
            raise generic
        if row["status"] != "active":                       # only someone who knows the password learns the account is unavailable
            self.db.event("login_refused_status", row["id"])
            raise ApiError(403, "account_unavailable", "This account is not available.")
        self.db.q("UPDATE users SET failed_logins = 0, locked_until = 0 WHERE id = ?", (row["id"],))
        if sec.needs_rehash(row["pw_hash"]):
            self.db.q("UPDATE users SET pw_hash = ? WHERE id = ?", (sec.hash_password(password), row["id"]))
        self.db.purge_expired_sessions()
        return {"token": self._session(row["id"]), "needs_terms_acceptance": row["terms_sha"] != self.terms_sha}

    def logout(self, d, h, ip):
        self._user(h)
        self.db.q("DELETE FROM sessions WHERE token_digest = ?", (sec.token_digest(self._bearer(h)),))
        return {"ok": True}

    def renew(self, d, h, ip):
        """Swap the current session for a new token with a fresh expiry. The old token stops working at once. A session chain ends
        MAX_SESSION_DAYS after the original sign-in; after that the person must sign in again."""
        u = self._user(h)
        dig = sec.token_digest(self._bearer(h))
        s = self.db.one("SELECT origin_at, created_at FROM sessions WHERE token_digest = ?", (dig,))
        origin = (s["origin_at"] if s and s["origin_at"] is not None else (s["created_at"] if s else self.clock()))
        if self.clock() - origin > MAX_SESSION_DAYS * 86400:
            self.db.q("DELETE FROM sessions WHERE token_digest = ?", (dig,))
            raise ApiError(401, "auth_required", "Your session is too old to renew. Sign in again.")
        self.db.q("DELETE FROM sessions WHERE token_digest = ?", (dig,))
        return {"token": self._session(u["id"], origin)}

    def revoke_all(self, d, h, ip):
        """'Sign out everywhere': every session of this account, including the one used for this call, stops working."""
        u = self._user(h)
        n = self.db.q("DELETE FROM sessions WHERE user_id = ?", (u["id"],)).rowcount
        self.db.event("sessions_revoked", u["id"])
        return {"ok": True, "revoked": n}

    def set_display_name(self, d, h, ip):
        u = self._user(h)
        name = d.get("display_name")
        problem = sec.display_name_problem(name)
        if problem:
            raise ApiError(400, "bad_display_name", problem[0].upper() + problem[1:] + ".")
        self.db.q("UPDATE users SET display_name = ? WHERE id = ?", (sec.clean_display_name(name), u["id"]))
        return {"display_name": sec.clean_display_name(name)}

    def change_password(self, d, h, ip):
        """Needs the CURRENT password even though the caller is signed in. Every other session of the account is revoked."""
        u = self._user(h)
        if not self.limiter.allow(f"pw:{u['id']}", 5, 600):
            raise ApiError(429, "rate_limited", "Too many attempts. Try again later.")
        if not sec.verify_password(self._str(d, "current_password", 400), u["pw_hash"]):
            raise ApiError(401, "bad_credentials", "Your current password is wrong.")
        new_pw = self._str(d, "new_password", 400)
        problem = sec.password_problem(new_pw, u["username"])
        if problem:
            raise ApiError(400, "weak_password", problem)
        if sec.verify_password(new_pw, u["pw_hash"]):
            raise ApiError(400, "weak_password", "The new password must be different from the current one.")
        self.db.q("UPDATE users SET pw_hash = ?, pw_changed_at = ?, failed_logins = 0, locked_until = 0 WHERE id = ?",
                  (sec.hash_password(new_pw), self.clock(), u["id"]))
        n = self.db.q("DELETE FROM sessions WHERE user_id = ? AND token_digest != ?", (u["id"], sec.token_digest(self._bearer(h)))).rowcount
        self.db.event("password_changed", u["id"])
        return {"ok": True, "other_sessions_revoked": n}

    def me(self, d, h, ip):
        u = self._user(h)
        return {"username": u["username"], "display_name": u["display_name"] or u["username"], "status": u["status"],
                "created_at": u["created_at"], "session_expires_at": u["expires_at"], "learning_opt_in": self._learning_active(u),
                "learning_stored_value": bool(u["learning_opt_in"]), "learning_basis": u["learning_basis"],
                "learning_consent_needed": bool(u["learning_opt_in"]) and not self._learning_active(u),
                "needs_terms_acceptance": u["terms_sha"] != self.terms_sha, "model_connected": bool(self.backend.available())}

    def accept_terms(self, d, h, ip):
        u = self._user(h)
        if d.get("sha256") != self.terms_sha:
            raise ApiError(400, "terms_not_accepted", "That is not the current terms version.")
        self.db.q("UPDATE users SET terms_sha = ?, terms_accepted_at = ? WHERE id = ?", (self.terms_sha, self.clock(), u["id"]))
        return {"ok": True}

    def set_learning(self, d, h, ip):
        u = self._user(h)
        v = d.get("learning_opt_in")
        if not isinstance(v, bool):
            raise ApiError(400, "bad_request", "'learning_opt_in' must be true or false.")
        withdrawn = 0
        if v and not self._consent_ok(d):
            raise ApiError(400, "learning_consent_required",
                           "Turning learning ON needs your confirmation of the current learning notice (learning_consent_sha256).")
        now = self.clock()
        self.db.q("UPDATE users SET learning_opt_in = ?, learning_changed_at = ?, learning_basis = ?, learning_consent_sha = ?, "
                  "learning_consent_at = ? WHERE id = ?",
                  (int(v), now, "explicit" if v else "none", self.learning_notice_sha if v else None, now if v else None, u["id"]))
        if not v and d.get("withdraw_past", True) is not False:
            cur = self.db.q("UPDATE messages SET learn_ok = 0 WHERE user_id = ? AND used_in_export = 0 AND learn_ok = 1", (u["id"],))
            withdrawn = cur.rowcount
        self.db.event("learning_on" if v else "learning_off", u["id"])
        return {"learning_opt_in": v, "past_messages_withdrawn": withdrawn}

    def chat(self, d, h, ip):
        u = self._user(h)
        if u["terms_sha"] != self.terms_sha:
            raise ApiError(403, "terms_changed", "The terms changed. Accept the new terms to continue.")
        if not self.limiter.allow(f"chat:{u['id']}", 30, 60):
            raise ApiError(429, "rate_limited", "You are sending messages too quickly.")
        if not self.backend.available():
            raise ApiError(503, "no_model", "No AI model is connected to this service yet, so it cannot answer. Nothing was saved.")
        msgs = d.get("messages")
        if not isinstance(msgs, list) or not msgs or len(msgs) > MAX_MESSAGES:
            raise ApiError(400, "bad_request", f"'messages' must be a list of 1-{MAX_MESSAGES} items.")
        total = 0
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str) \
                    or not m["content"].strip() or len(m["content"]) > MAX_MSG_CHARS:
                raise ApiError(400, "bad_request", "Each message needs role user/assistant and non-empty text up to 8000 characters.")
            total += len(m["content"])
        if total > MAX_TOTAL_CHARS or msgs[-1]["role"] != "user":
            raise ApiError(400, "bad_request", "Conversation too long, or the last message is not from the user.")
        cid = d.get("conversation_id") or uuid.uuid4().hex
        if not isinstance(cid, str) or not _CONV.match(cid):
            raise ApiError(400, "bad_request", "Invalid conversation_id.")
        if not self.gen_slots.acquire(blocking=False):
            raise ApiError(503, "busy", "The service is busy. Try again in a moment.")
        try:
            reply = self.backend.generate([{"role": m["role"], "content": m["content"]} for m in msgs])
        except Exception:
            self.db.event("model_error", u["id"])
            raise ApiError(502, "model_error", "The model failed to produce a reply.")
        finally:
            self.gen_slots.release()
        learn = int(self._learning_active(u))
        now = self.clock()
        self.db.q("INSERT INTO messages(user_id, conversation_id, role, content, created_at, learn_ok) VALUES(?,?,?,?,?,?)",
                  (u["id"], cid, "user", msgs[-1]["content"], now, learn))
        self.db.q("INSERT INTO messages(user_id, conversation_id, role, content, created_at, learn_ok) VALUES(?,?,?,?,?,?)",
                  (u["id"], cid, "assistant", reply, now, learn))
        return {"reply": reply, "conversation_id": cid, "learning_applies": bool(learn)}

    def export(self, d, h, ip):
        u = self._user(h)
        rows = self.db.all("SELECT conversation_id, role, content, created_at, learn_ok, used_in_export FROM messages "
                           "WHERE user_id = ? ORDER BY id", (u["id"],))
        return {"username": u["username"], "created_at": u["created_at"], "learning_opt_in": self._learning_active(u),
                "learning_basis": u["learning_basis"], "terms_sha256": u["terms_sha"], "messages": [dict(r) for r in rows]}

    def delete_messages(self, d, h, ip):
        """Delete the caller's stored chat history (one conversation, or everything with confirm=true)."""
        u = self._user(h)
        cid = d.get("conversation_id")
        if cid is not None:
            if not isinstance(cid, str) or not _CONV.match(cid):
                raise ApiError(400, "bad_request", "Invalid conversation_id.")
            n = self.db.q("DELETE FROM messages WHERE user_id = ? AND conversation_id = ?", (u["id"], cid)).rowcount
        elif d.get("confirm") is True:
            n = self.db.q("DELETE FROM messages WHERE user_id = ?", (u["id"],)).rowcount
        else:
            raise ApiError(400, "bad_request", "Pass conversation_id, or confirm=true to delete all of your messages.")
        self.db.event("messages_deleted", u["id"])
        return {"deleted_messages": n}

    def delete_account(self, d, h, ip):
        u = self._user(h)
        if not self.limiter.allow(f"del:{u['id']}", 5, 600):
            raise ApiError(429, "rate_limited", "Too many attempts.")
        if not sec.verify_password(self._str(d, "password", 400), u["pw_hash"]):
            raise ApiError(401, "bad_credentials", "Wrong password.")
        self.db.q("DELETE FROM users WHERE id = ?", (u["id"],))  # sessions and messages cascade
        self.db.event("account_deleted", None)
        return {"ok": True, "deleted": True}

    def recover(self, d, h, ip):
        username = sec.normalize_username(self._str(d, "username", 64))
        code = self._str(d, "recovery_code", 64)
        new_pw = self._str(d, "new_password", 400)
        if not (self.limiter.allow(f"rec-ip:{ip}", 10, 3600) and self.limiter.allow(f"rec-user:{username}", 5, 3600)):
            raise ApiError(429, "rate_limited", "Too many attempts. Try again later.")
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        ok = sec.verify_password(code.strip().upper(), row["recovery_hash"] if row else sec.DUMMY_HASH)
        if row is None or not ok:
            raise ApiError(401, "bad_credentials", "Username or recovery code is wrong.")
        problem = sec.password_problem(new_pw, username)
        if problem:
            raise ApiError(400, "weak_password", problem)
        new_code = sec.new_recovery_code()
        self.db.q("UPDATE users SET pw_hash = ?, recovery_hash = ?, failed_logins = 0, locked_until = 0 WHERE id = ?",
                  (sec.hash_password(new_pw), sec.hash_password(new_code), row["id"]))
        self.db.q("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        self.db.event("password_recovered", row["id"])
        return {"recovery_code": new_code}

    # ---- anonymous app telemetry (no sign-in, no IP stored, allowlisted schema shared with the client) ----
    def telemetry(self, d, h, ip):
        if not self.telemetry_enabled:
            raise ApiError(404, "not_found", "Unknown endpoint.")
        if not self.limiter.allow(f"tel-ip:{ip}", 60, 3600):
            raise ApiError(429, "rate_limited", "Too many reports from this network. Try later.")
        evs = d.get("events")
        if not isinstance(evs, list) or not evs or len(evs) > 100:
            raise ApiError(400, "bad_request", "'events' must be a list of 1-100 events.")
        accepted = rejected = 0
        now = self.clock()
        for raw in evs:
            try:
                e = validate_event(raw)
            except (SchemaError, TypeError):
                rejected += 1
                continue
            if not self.limiter.allow(f"tel-id:{e['install_id']}", 600, 3600):
                rejected += 1
                continue
            self.db.q("INSERT INTO telemetry(received_at, install_id, event, event_hour, app_version, data) VALUES(?,?,?,?,?,?)",
                      (now, e["install_id"], e["event"], e["t"], e["data"].get("app_version"), json.dumps(e["data"], sort_keys=True)))
            accepted += 1
        return {"accepted": accepted, "rejected": rejected}

    def telemetry_delete(self, d, h, ip):
        if not self.telemetry_enabled:
            raise ApiError(404, "not_found", "Unknown endpoint.")
        if not self.limiter.allow(f"tel-del:{ip}", 20, 3600):
            raise ApiError(429, "rate_limited", "Too many requests.")
        iid = d.get("install_id")
        if not isinstance(iid, str) or len(iid) != 32 or any(c not in "0123456789abcdef" for c in iid):
            raise ApiError(400, "bad_request", "Invalid install_id.")
        n = self.db.q("DELETE FROM telemetry WHERE install_id = ?", (iid,)).rowcount
        self.db.event("telemetry_deleted")
        return {"deleted_events": n}

    ROUTES = {
        ("POST", "/v1/telemetry"): telemetry, ("POST", "/v1/telemetry/delete"): telemetry_delete,
        ("GET", "/v1/health"): health, ("GET", "/v1/terms"): get_terms,
        ("POST", "/v1/register"): register, ("POST", "/v1/login"): login, ("POST", "/v1/logout"): logout,
        ("POST", "/v1/me"): me, ("POST", "/v1/accept-terms"): accept_terms, ("POST", "/v1/settings/learning"): set_learning,
        ("POST", "/v1/chat"): chat, ("POST", "/v1/messages/delete"): delete_messages, ("POST", "/v1/account/export"): export,
        ("POST", "/v1/account/delete"): delete_account, ("POST", "/v1/recover"): recover,
        ("POST", "/v1/session/renew"): renew, ("POST", "/v1/sessions/revoke-all"): revoke_all,
        ("POST", "/v1/account/display-name"): set_display_name, ("POST", "/v1/account/password"): change_password,
    }
