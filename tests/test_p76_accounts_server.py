"""Server-side account security (Jarvis Online service). REAL service code and REAL SQLite; no network. Where scrypt cost would only slow the tests, the parameters are
lowered through the module constants and the test says so; the strong defaults are asserted separately (class HashingDefaults)."""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "server"))

from jarvis_server import security as sec  # noqa: E402
from jarvis_server.app import MAX_SESSION_DAYS, SESSION_DAYS, Service  # noqa: E402
from jarvis_server.db import DB, SCHEMA_VERSION  # noqa: E402

PW = "correct horse battery"
PW2 = "another long passphrase 42"
REAL = (sec.SCRYPT_N, sec.SCRYPT_R, sec.SCRYPT_P)


def cheap():
    sec.SCRYPT_N, sec.SCRYPT_R, sec.SCRYPT_P = 2 ** 10, 8, 1


def restore():
    sec.SCRYPT_N, sec.SCRYPT_R, sec.SCRYPT_P = REAL


class HashingDefaults(unittest.TestCase):
    def test_default_parameters_meet_the_owasp_scrypt_minimum(self):
        self.assertGreaterEqual(REAL[0], 2 ** 16)
        self.assertGreaterEqual(REAL[1], 8)
        self.assertGreaterEqual(REAL[2], 2)

    def test_real_parameters_hash_verify_and_salt_uniquely(self):
        a, b = sec.hash_password(PW), sec.hash_password(PW)
        self.assertNotEqual(a, b)                                  # unique salt each time
        self.assertTrue(sec.verify_password(PW, a))
        self.assertFalse(sec.verify_password(PW + "x", a))
        self.assertNotIn(PW, a)
        self.assertFalse(sec.needs_rehash(a))

    def test_an_old_weak_hash_still_verifies_and_is_flagged_for_upgrade(self):
        import hashlib, os
        salt = os.urandom(16)
        dk = hashlib.scrypt(PW.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
        old = f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"
        self.assertTrue(sec.verify_password(PW, old))
        self.assertTrue(sec.needs_rehash(old))

    def test_a_stored_hash_with_absurd_parameters_is_refused_not_computed(self):
        t = time.time()
        self.assertFalse(sec.verify_password(PW, f"scrypt${2 ** 30}$8$1$00$00"))
        self.assertFalse(sec.verify_password(PW, "scrypt$1000$8$1$00$00"))     # not a power of two
        self.assertFalse(sec.verify_password(PW, "garbage"))
        self.assertLess(time.time() - t, 2)


class Base(unittest.TestCase):
    def setUp(self):
        cheap()
        self.addCleanup(restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.db"
        self.now = [time.time()]
        self.db = DB(self.path)
        self.addCleanup(self.db.close)
        self.svc = Service(self.db, terms_text="Test terms.", clock=lambda: self.now[0])
        self.n = 0

    def call(self, path, body=None, token=None, method="POST", ip="1.1.1.1"):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        return self.svc.handle(method, path, h, json.dumps(body or {}).encode(), ip)

    def reg(self, name="alice", pw=PW, **kw):
        self.n += 1
        body = dict(username=name, password=pw, accepted_terms_sha256=self.svc.terms_sha, age_confirmed=True)
        body.update(kw)
        return self.call("/v1/register", body, ip=f"10.0.0.{self.n}")      # a different address each time: the per-IP sign-up limit is not what is under test

    def token(self, name="alice", **kw):
        s, r = self.reg(name, **kw)
        self.assertEqual(s, 200, r)
        return r["token"]


class Registration(Base):
    def test_register_returns_a_session_a_recovery_code_and_never_a_hash(self):
        s, r = self.reg(display_name="  Alice A.  ")
        self.assertEqual(s, 200)
        self.assertEqual(set(r) & {"pw_hash", "recovery_hash", "password"}, set())
        s, me = self.call("/v1/me", token=r["token"])
        self.assertEqual((me["username"], me["display_name"], me["status"]), ("alice", "Alice A.", "active"))
        self.assertEqual(set(me) & {"pw_hash", "recovery_hash", "token_digest"}, set())

    def test_the_display_name_defaults_to_the_username(self):
        self.assertEqual(self.call("/v1/me", token=self.token())[1]["display_name"], "alice")

    def test_duplicate_registration_is_refused_without_changing_the_first_account(self):
        t = self.token()
        s, r = self.reg("ALICE", pw=PW2)                                      # same name, different case and password
        self.assertEqual((s, r["error"]), (409, "username_unavailable"))
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW))[0], 200)
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW2))[0], 401)
        self.assertEqual(self.call("/v1/me", token=t)[0], 200)

    def test_invalid_input_is_rejected_by_the_server_itself(self):
        for body, code in [(dict(username="a b", password=PW), "bad_username"), (dict(username="al", password=PW), "bad_username"),
                           (dict(username="bob", password="short"), "weak_password"), (dict(username="bob", password="password1"), "weak_password"),
                           (dict(username="bob", password=PW, display_name="x" * 41), "bad_display_name"),
                           (dict(username="bob", password=PW, display_name="bad‮name"), "bad_display_name"),
                           (dict(username="bob", password=PW, display_name="   "), "bad_display_name"),
                           (dict(username="bob", password=PW, accepted_terms_sha256="nope"), "terms_not_accepted"),
                           (dict(username="bob", password=PW, age_confirmed=False), "age_not_confirmed")]:
            s, r = self.reg(**body)
            self.assertEqual((s, r["error"]), (400, code), body)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM users")[0], 0)

    def test_non_string_and_oversized_fields_do_not_crash_the_server(self):
        for body in [dict(username=123, password=PW), dict(username="bob", password=["x"]), dict(username="bob", password="p" * 5000)]:
            s, _ = self.call("/v1/register", dict(accepted_terms_sha256=self.svc.terms_sha, age_confirmed=True, **body))
            self.assertEqual(s, 400)

    def test_sql_injection_text_is_just_text(self):
        self.token()
        for evil in ["alice' OR '1'='1", "alice'; DROP TABLE users;--", "\" OR 1=1 --"]:
            self.assertEqual(self.call("/v1/login", dict(username=evil, password=PW))[0], 401)
            self.assertEqual(self.reg(evil)[0], 400)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM users")[0], 1)
        s, me = self.call("/v1/me", token=self.call("/v1/login", dict(username="alice", password=PW))[1]["token"])
        self.assertEqual(me["username"], "alice")

    def test_only_digests_are_stored(self):
        s, r = self.reg()
        dump = json.dumps([dict(x) for x in self.db.all("SELECT * FROM users")] + [dict(x) for x in self.db.all("SELECT * FROM sessions")])
        for secret in (PW, r["token"], r["recovery_code"]):
            self.assertNotIn(secret, dump)


class SignIn(Base):
    def test_login_success_and_wrong_password_and_unknown_user_look_alike(self):
        self.token()
        ok = self.call("/v1/login", dict(username="Alice", password=PW))
        self.assertEqual(ok[0], 200)
        a = self.call("/v1/login", dict(username="alice", password="wrong-password-1"))
        b = self.call("/v1/login", dict(username="nobody", password="wrong-password-1"))
        self.assertEqual(a, b)

    def test_five_failures_lock_the_account_and_the_lock_is_not_distinguishable(self):
        self.token()
        for i in range(5):
            self.call("/v1/login", dict(username="alice", password="wrong-password-1"), ip=f"9.9.9.{i}")
        locked = self.call("/v1/login", dict(username="alice", password=PW), ip="8.8.8.8")
        self.assertEqual(locked, self.call("/v1/login", dict(username="ghost", password=PW), ip="8.8.8.8"))
        self.now[0] += 16 * 60
        self.svc.limiter = sec.RateLimiter()
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW))[0], 200)

    def test_a_weak_stored_hash_is_upgraded_after_a_correct_sign_in(self):
        import hashlib
        self.token()
        salt = bytes(16)
        dk = hashlib.scrypt(PW.encode(), salt=salt, n=2 ** 8, r=8, p=1, dklen=32)
        self.db.q("UPDATE users SET pw_hash = ?", (f"scrypt$256$8$1${salt.hex()}${dk.hex()}",))
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW))[0], 200)
        new = self.db.one("SELECT pw_hash FROM users")[0]
        self.assertFalse(sec.needs_rehash(new))
        self.assertTrue(sec.verify_password(PW, new))

    def test_a_suspended_account_cannot_use_a_session_or_sign_in(self):
        t = self.token()
        self.db.q("UPDATE users SET status = 'suspended'")                 # how a future administrator action would look; no admin feature exists today
        self.assertEqual(self.call("/v1/me", token=t)[1]["error"], "account_unavailable")
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW))[1]["error"], "account_unavailable")
        self.assertEqual(self.call("/v1/login", dict(username="alice", password="wrong-password-1"))[1]["error"], "bad_credentials")   # a wrong password still learns nothing


class Sessions(Base):
    def test_session_expires(self):
        t = self.token()
        self.assertEqual(self.call("/v1/me", token=t)[0], 200)
        self.now[0] += SESSION_DAYS * 86400 + 1
        self.assertEqual(self.call("/v1/me", token=t)[0], 401)

    def test_logout_revokes_only_that_session(self):
        t1 = self.token()
        t2 = self.call("/v1/login", dict(username="alice", password=PW))[1]["token"]
        self.assertEqual(self.call("/v1/logout", token=t1)[0], 200)
        self.assertEqual(self.call("/v1/me", token=t1)[0], 401)
        self.assertEqual(self.call("/v1/me", token=t2)[0], 200)

    def test_revoke_all_signs_out_every_device(self):
        t1 = self.token()
        t2 = self.call("/v1/login", dict(username="alice", password=PW))[1]["token"]
        s, r = self.call("/v1/sessions/revoke-all", token=t1)
        self.assertEqual((s, r["revoked"]), (200, 2))
        self.assertEqual([self.call("/v1/me", token=t)[0] for t in (t1, t2)], [401, 401])

    def test_renew_swaps_the_token_and_the_old_one_dies(self):
        t = self.token()
        self.now[0] += 10 * 86400
        s, r = self.call("/v1/session/renew", token=t)
        self.assertEqual(s, 200)
        self.assertEqual(self.call("/v1/me", token=t)[0], 401)
        s, me = self.call("/v1/me", token=r["token"])
        self.assertEqual(s, 200)
        self.assertGreater(me["session_expires_at"], self.now[0] + (SESSION_DAYS - 1) * 86400)

    def test_renewal_cannot_extend_a_session_forever(self):
        t = self.token()
        for _ in range(MAX_SESSION_DAYS // 20 + 2):
            self.now[0] += 20 * 86400
            s, r = self.call("/v1/session/renew", token=t)
            if s != 200:
                break
            t = r["token"]
        self.assertEqual((s, r["error"]), (401, "auth_required"))

    def test_tokens_are_long_and_random(self):
        a, b = self.token("alice"), self.token("bobby")
        self.assertGreaterEqual(len(a), 40)
        self.assertNotEqual(a, b)

    def test_every_authenticated_route_refuses_anonymous_and_garbage_tokens(self):
        routes = ["/v1/logout", "/v1/me", "/v1/accept-terms", "/v1/settings/learning", "/v1/chat", "/v1/messages/delete", "/v1/account/export",
                  "/v1/account/delete", "/v1/session/renew", "/v1/sessions/revoke-all", "/v1/account/display-name", "/v1/account/password"]
        for path in routes:
            for tok in (None, "garbage", "A" * 43):
                s, r = self.call(path, {}, token=tok)
                self.assertEqual((path, s, r["error"]), (path, 401, "auth_required"))


class AccountChanges(Base):
    def test_display_name_can_be_changed_and_is_validated(self):
        t = self.token()
        self.assertEqual(self.call("/v1/account/display-name", dict(display_name="Ally"), token=t)[0], 200)
        self.assertEqual(self.call("/v1/me", token=t)[1]["display_name"], "Ally")
        self.assertEqual(self.call("/v1/account/display-name", dict(display_name=""), token=t)[1]["error"], "bad_display_name")
        self.assertEqual(self.call("/v1/account/display-name", dict(display_name=5), token=t)[1]["error"], "bad_display_name")

    def test_change_password_needs_the_current_one_and_a_good_new_one(self):
        t = self.token()
        self.assertEqual(self.call("/v1/account/password", dict(current_password="nope-nope-nope", new_password=PW2), token=t)[1]["error"], "bad_credentials")
        self.assertEqual(self.call("/v1/account/password", dict(current_password=PW, new_password="short"), token=t)[1]["error"], "weak_password")
        self.assertEqual(self.call("/v1/account/password", dict(current_password=PW, new_password=PW), token=t)[1]["error"], "weak_password")
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW))[0], 200)      # nothing changed yet

    def test_change_password_works_and_signs_out_the_other_sessions_only(self):
        t1 = self.token()
        t2 = self.call("/v1/login", dict(username="alice", password=PW))[1]["token"]
        s, r = self.call("/v1/account/password", dict(current_password=PW, new_password=PW2), token=t1)
        self.assertEqual((s, r["other_sessions_revoked"]), (200, 1))
        self.assertEqual(self.call("/v1/me", token=t1)[0], 200)
        self.assertEqual(self.call("/v1/me", token=t2)[0], 401)
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW))[0], 401)
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW2))[0], 200)

    def test_change_password_is_rate_limited(self):
        t = self.token()
        codes = [self.call("/v1/account/password", dict(current_password="nope-nope-nope", new_password=PW2), token=t)[0] for _ in range(7)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5:], [429, 429])

    def test_account_deletion_needs_the_password_and_removes_everything(self):
        t = self.token()
        self.assertEqual(self.call("/v1/account/delete", dict(password="nope-nope-nope"), token=t)[0], 401)
        self.assertEqual(self.call("/v1/me", token=t)[0], 200)
        self.assertEqual(self.call("/v1/account/delete", dict(password=PW), token=t)[0], 200)
        self.assertEqual(self.call("/v1/me", token=t)[0], 401)
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW))[0], 401)
        self.assertEqual([self.db.one(f"SELECT COUNT(*) FROM {x}")[0] for x in ("users", "sessions", "messages")], [0, 0, 0])
        self.assertEqual(self.reg("alice")[0], 200)                         # the name is free again

    def test_recovery_code_resets_the_password_and_signs_everyone_out(self):
        s, r = self.reg()
        s, rec = self.call("/v1/recover", dict(username="alice", recovery_code=r["recovery_code"], new_password=PW2))
        self.assertEqual(s, 200)
        self.assertNotEqual(rec["recovery_code"], r["recovery_code"])
        self.assertEqual(self.call("/v1/me", token=r["token"])[0], 401)
        self.assertEqual(self.call("/v1/login", dict(username="alice", password=PW2))[0], 200)
        self.assertEqual(self.call("/v1/recover", dict(username="alice", recovery_code=r["recovery_code"], new_password=PW))[0], 401)   # a used code is dead


class Isolation(Base):
    class Echo:
        name = "test-only"
        def available(self): return True
        def generate(self, messages): return "echo:" + messages[-1]["content"]

    def setUp(self):
        super().setUp()
        self.svc.backend = self.Echo()
        self.a, self.b = self.token("alice"), self.token("bobby")
        self.call("/v1/chat", dict(messages=[dict(role="user", content="alice secret plan")], conversation_id="conv-1"), token=self.a)
        self.call("/v1/chat", dict(messages=[dict(role="user", content="bobby private note")], conversation_id="conv-1"), token=self.b)   # the SAME conversation id

    def test_export_contains_only_the_callers_messages(self):
        a = json.dumps(self.call("/v1/account/export", token=self.a)[1])
        b = json.dumps(self.call("/v1/account/export", token=self.b)[1])
        self.assertIn("alice secret plan", a); self.assertNotIn("bobby", a)
        self.assertIn("bobby private note", b); self.assertNotIn("alice secret plan", b)
        self.assertEqual(len(self.call("/v1/account/export", token=self.a)[1]["messages"]), 2)

    def test_deleting_a_conversation_id_deletes_only_your_own(self):
        self.assertEqual(self.call("/v1/messages/delete", dict(conversation_id="conv-1"), token=self.a)[1]["deleted_messages"], 2)
        self.assertEqual(len(self.call("/v1/account/export", token=self.b)[1]["messages"]), 2)

    def test_one_account_deleted_leaves_the_other_intact(self):
        self.call("/v1/account/delete", dict(password=PW), token=self.a)
        self.assertEqual(len(self.call("/v1/account/export", token=self.b)[1]["messages"]), 2)

    def test_a_token_from_one_account_never_acts_as_the_other(self):
        self.assertEqual(self.call("/v1/me", token=self.a)[1]["username"], "alice")
        self.assertEqual(self.call("/v1/me", token=self.b)[1]["username"], "bobby")
        self.call("/v1/account/display-name", dict(display_name="Changed"), token=self.a)
        self.assertEqual(self.call("/v1/me", token=self.b)[1]["display_name"], "bobby")

    def test_logout_then_switching_accounts_keeps_sessions_separate(self):
        self.call("/v1/logout", token=self.a)
        a2 = self.call("/v1/login", dict(username="alice", password=PW))[1]["token"]
        self.assertEqual(self.call("/v1/me", token=a2)[1]["username"], "alice")
        self.assertEqual(self.call("/v1/me", token=self.a)[0], 401)
        self.assertEqual(self.call("/v1/me", token=self.b)[1]["username"], "bobby")


V1_SCHEMA = """
CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, recovery_hash TEXT NOT NULL,
  created_at REAL NOT NULL, terms_sha TEXT NOT NULL, terms_accepted_at REAL NOT NULL, age_confirmed INTEGER NOT NULL,
  learning_opt_in INTEGER NOT NULL DEFAULT 0, learning_changed_at REAL,
  learning_basis TEXT NOT NULL DEFAULT 'none', learning_consent_sha TEXT, learning_consent_at REAL,
  failed_logins INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0);
CREATE TABLE sessions(token_digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE messages(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, created_at REAL NOT NULL, learn_ok INTEGER NOT NULL, used_in_export INTEGER NOT NULL DEFAULT 0);
CREATE TABLE telemetry(id INTEGER PRIMARY KEY, received_at REAL NOT NULL, install_id TEXT NOT NULL, event TEXT NOT NULL, event_hour TEXT NOT NULL, app_version TEXT, data TEXT NOT NULL);
CREATE TABLE events(id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, user_id INTEGER);
PRAGMA user_version = 1;
"""


class Migration(unittest.TestCase):
    """HAND-BUILT v1 database: the baseline schema (commit 03ad89a) written out by hand, filled with one account, one session and one message."""

    def make_v1(self, d):
        p = Path(d) / "old.db"
        c = sqlite3.connect(p)
        c.executescript(V1_SCHEMA)
        h = sec.hash_password(PW)
        c.execute("INSERT INTO users(id,username,pw_hash,recovery_hash,created_at,terms_sha,terms_accepted_at,age_confirmed) VALUES(1,'carol',?,?,1,'t',1,1)", (h, h))
        c.execute("INSERT INTO sessions VALUES('dig',1,1,9999999999)")
        c.execute("INSERT INTO messages(user_id,conversation_id,role,content,created_at,learn_ok) VALUES(1,'c','user','kept',1,0)")
        c.commit(); c.close()
        return p

    def test_a_v1_database_is_backed_up_then_upgraded_without_losing_anything(self):
        cheap(); self.addCleanup(restore)
        with tempfile.TemporaryDirectory() as d:
            p = self.make_v1(d)
            db = DB(p)
            self.assertEqual(db.one("PRAGMA user_version")[0], SCHEMA_VERSION)
            u = db.one("SELECT * FROM users")
            self.assertEqual((u["username"], u["display_name"], u["status"]), ("carol", "carol", "active"))
            self.assertEqual(db.one("SELECT origin_at, created_at FROM sessions")[0], 1)
            self.assertEqual(db.one("SELECT content FROM messages")[0], "kept")
            bak = Path(str(p) + ".v1.bak")
            self.assertTrue(bak.exists())
            b = sqlite3.connect(bak)
            self.assertEqual(b.execute("PRAGMA user_version").fetchone()[0], 1)           # the copy is the untouched v1 database
            self.assertNotIn("display_name", {r[1] for r in b.execute("PRAGMA table_info(users)")})
            self.assertEqual(b.execute("SELECT content FROM messages").fetchone()[0], "kept")
            b.close(); db.close()
            db = DB(p)                                                                   # opening again changes nothing and does not overwrite the backup
            self.assertEqual(db.one("SELECT COUNT(*) FROM users")[0], 1)
            db.close()

    def test_the_upgraded_database_signs_the_old_user_in_and_upgrades_the_weak_hash(self):
        cheap(); self.addCleanup(restore)
        with tempfile.TemporaryDirectory() as d:
            db = DB(self.make_v1(d))
            svc = Service(db, terms_text="x")
            h = {"Authorization": "Bearer x"}
            s, r = svc.handle("POST", "/v1/login", {}, json.dumps(dict(username="carol", password=PW)).encode(), "1.1.1.1")
            self.assertEqual(s, 200)
            s, me = svc.handle("POST", "/v1/me", {"Authorization": "Bearer " + r["token"]}, b"{}", "1.1.1.1")
            self.assertEqual((s, me["display_name"]), (200, "carol"))
            db.close()

    def test_a_database_from_a_newer_server_is_refused_and_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "new.db"
            c = sqlite3.connect(p); c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}"); c.commit(); c.close()
            with self.assertRaises(RuntimeError):
                DB(p)
            self.assertFalse(Path(str(p) + f".v{SCHEMA_VERSION + 1}.bak").exists())

    def test_a_fresh_database_gets_the_current_version_and_no_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "fresh.db"
            db = DB(p)
            self.assertEqual(db.one("PRAGMA user_version")[0], SCHEMA_VERSION)
            db.close()
            self.assertEqual(list(Path(d).glob("*.bak")), [])


if __name__ == "__main__":
    unittest.main()
