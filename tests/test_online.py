"""Tests for the Jarvis Online service and client. The FakeBackend exists ONLY in this test file; the shipped server can
only use NoModelBackend or a real LocalEngineBackend, so no production path returns made-up answers."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "server"))

from jarvis.net.policy import NetworkDisabled, NetworkPolicy
from jarvis.online.client import OnlineClient, ServiceError
from jarvis.online.flows import login_flow, register_flow
from jarvis.online.tokenstore import TokenStore
from jarvis_server import security as sec
from jarvis_server.app import Service
from jarvis_server.db import DB
from jarvis_server.learning import export_training_data, scrub
from jarvis_server.__main__ import make_handler, main as server_main

PW = "correct horse battery"


class FakeBackend:
    name = "test-only"
    def available(self): return True
    def generate(self, messages): return "test reply to: " + messages[-1]["content"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "s.db")
        self.svc = Service(self.db, FakeBackend(), terms_text="Test terms. Learning is ON by default; turn it off any time.")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, token=None, ip="1.1.1.1"):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        return self.svc.handle(method, path, h, json.dumps(body or {}).encode(), ip)

    def reg(self, name="alice", ip="1.1.1.1", **kw):
        body = dict(username=name, password=PW, accepted_terms_sha256=self.svc.terms_sha, age_confirmed=True)
        body.update(kw)
        return self.call("POST", "/v1/register", body, ip=ip)


    def opt_in(self, token, **kw):
        return self.call("POST", "/v1/settings/learning",
                         dict(learning_opt_in=True, learning_consent_sha256=self.svc.learning_notice_sha, **kw), token=token)


class AccountTests(Base):
    def test_register_requires_terms_age_and_strong_password(self):
        self.assertEqual(self.reg(accepted_terms_sha256="wrong")[1]["error"], "terms_not_accepted")
        self.assertEqual(self.reg(age_confirmed=False)[1]["error"], "age_not_confirmed")
        self.assertEqual(self.reg(password="short")[1]["error"], "weak_password")
        self.assertEqual(self.reg(name="x")[1]["error"], "bad_username")
        s, r = self.reg(ip="5.5.5.5")
        self.assertEqual(s, 200)
        self.assertEqual(self.reg(ip="5.5.5.6")[0], 409)
        self.assertEqual(self.reg(name="ALICE", ip="2.2.2.2")[0], 409)       # case-insensitive usernames

    def test_signup_flood_from_one_address_is_limited(self):
        codes = [self.reg(name=f"user{i}", ip="6.6.6.6")[0] for i in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5:], [429, 429])

    def test_learning_is_off_by_default_and_needs_the_notice_fingerprint(self):
        s, r = self.reg()
        self.assertFalse(r["learning_opt_in"])
        self.assertFalse(self.call("POST", "/v1/me", token=r["token"])[1]["learning_opt_in"])
        s, bad = self.reg("bob", ip="3.3.3.3", learning_opt_in=True)             # asked for ON but gave no consent
        self.assertEqual((s, bad["error"]), (400, "learning_consent_required"))
        s, bad = self.reg("bob", ip="3.3.3.3", learning_opt_in=True, learning_consent_sha256="0" * 64)
        self.assertEqual(s, 400)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM users WHERE username='bob'")["c"], 0)
        s, ok = self.reg("bob", ip="3.3.3.3", learning_opt_in=True, learning_consent_sha256=self.svc.learning_notice_sha)
        self.assertEqual((s, ok["learning_opt_in"]), (200, True))

    def test_secrets_are_not_stored_in_plain_text(self):
        s, r = self.reg()
        rows = json.dumps([dict(x) for x in self.db.all("SELECT * FROM users")]) + json.dumps([dict(x) for x in self.db.all("SELECT * FROM sessions")])
        self.assertNotIn(PW, rows)
        self.assertNotIn(r["token"], rows)
        self.assertNotIn(r["recovery_code"], rows)
        self.assertIn("scrypt$", rows)

    def test_login_errors_are_generic_and_lockout_works(self):
        self.reg()
        a = self.call("POST", "/v1/login", dict(username="alice", password="wrong-password-1"))
        b = self.call("POST", "/v1/login", dict(username="nobody", password="wrong-password-1"))
        self.assertEqual(a, b)                                               # no username enumeration via login
        for _ in range(5):
            self.call("POST", "/v1/login", dict(username="alice", password="wrong-password-1"), ip="9.9.9.9")
        s, r = self.call("POST", "/v1/login", dict(username="alice", password=PW), ip="8.8.8.8")
        self.assertEqual(s, 401)                                             # locked even with the right password ...
        self.assertEqual((s, r), self.call("POST", "/v1/login", dict(username="nobody", password=PW), ip="8.8.8.8"))   # ... and a lock looks exactly like an unknown user
        self.svc.clock = lambda: time.time() + 3600
        self.svc.limiter = sec.RateLimiter()
        self.assertEqual(self.call("POST", "/v1/login", dict(username="alice", password=PW))[0], 200)

    def test_rate_limit_on_ip(self):
        for i in range(240):
            self.call("GET", "/v1/health", ip="7.7.7.7")
        self.assertEqual(self.call("GET", "/v1/health", ip="7.7.7.7")[0], 429)

    def test_recovery_code_resets_password_and_rotates(self):
        s, r = self.reg()
        s, rr = self.call("POST", "/v1/recover", dict(username="alice", recovery_code=r["recovery_code"], new_password="another long passphrase"))
        self.assertEqual(s, 200)
        self.assertNotEqual(rr["recovery_code"], r["recovery_code"])
        self.assertEqual(self.call("POST", "/v1/me", token=r["token"])[0], 401)             # old sessions killed
        self.assertEqual(self.call("POST", "/v1/recover", dict(username="alice", recovery_code=r["recovery_code"], new_password="third long passphrase"))[0], 401)
        self.assertEqual(self.call("POST", "/v1/login", dict(username="alice", password="another long passphrase"))[0], 200)

    def test_logout_invalidates_token(self):
        t = self.reg()[1]["token"]
        self.assertEqual(self.call("POST", "/v1/logout", token=t)[0], 200)
        self.assertEqual(self.call("POST", "/v1/me", token=t)[0], 401)

    def test_bad_input_never_crashes_or_leaks(self):
        self.assertEqual(self.svc.handle("POST", "/v1/login", {}, b"{not json", "1.1.1.1")[0], 400)
        self.assertEqual(self.svc.handle("POST", "/v1/login", {}, b"[1,2]", "1.1.1.1")[0], 400)
        self.assertEqual(self.svc.handle("POST", "/v1/login", {}, b"x" * 70000, "1.1.1.1")[0], 413)
        self.assertEqual(self.svc.handle("GET", "/nope", {}, b"", "1.1.1.1")[0], 404)
        self.assertEqual(self.call("POST", "/v1/login", dict(username=5, password=None))[0], 400)
        self.svc.backend = type("B", (), {"available": lambda s: True, "generate": lambda s, m: 1 / 0})()
        t = self.reg()[1]["token"]
        s, r = self.call("POST", "/v1/chat", dict(messages=[{"role": "user", "content": "hi"}]), token=t)
        self.assertEqual(s, 502)
        self.assertNotIn("division", json.dumps(r))


class ChatAndLearningTests(Base):
    def chat(self, t, text="hello", **kw):
        return self.call("POST", "/v1/chat", dict(messages=[{"role": "user", "content": text}], **kw), token=t)

    def test_must_sign_in_before_chatting(self):
        self.assertEqual(self.chat(None)[0], 401)
        self.assertEqual(self.chat("forged-token")[0], 401)

    def test_no_model_is_honest_and_saves_nothing(self):
        from jarvis_server.backends import NoModelBackend
        self.svc.backend = NoModelBackend()
        t = self.reg()[1]["token"]
        s, r = self.chat(t, "my secret")
        self.assertEqual(s, 503)
        self.assertEqual(r["error"], "no_model")
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM messages")["c"], 0)
        self.assertFalse(self.call("GET", "/v1/health")[1]["model_connected"])

    def test_chat_stores_with_learning_flag_and_validates(self):
        t = self.reg()[1]["token"]
        s, r = self.chat(t, "hello there")
        self.assertEqual((s, r["reply"], r["learning_applies"]), (200, "test reply to: hello there", False))
        self.opt_in(t)
        self.assertTrue(self.chat(t, "after consent")[1]["learning_applies"])
        self.assertEqual(self.chat(t, "")[0], 400)
        self.assertEqual(self.chat(t, "x" * 9000)[0], 400)
        self.assertEqual(self.chat(t, conversation_id="../../etc")[0], 400)
        self.assertEqual(self.call("POST", "/v1/chat", dict(messages=[{"role": "assistant", "content": "hi"}]), token=t)[0], 400)

    def test_turning_learning_off_stops_and_withdraws(self):
        t = self.reg()[1]["token"]
        self.opt_in(t)
        self.chat(t, "first message")
        s, r = self.call("POST", "/v1/settings/learning", dict(learning_opt_in=False), token=t)
        self.assertEqual((r["learning_opt_in"], r["past_messages_withdrawn"]), (False, 2))
        self.assertFalse(self.chat(t, "second message")[1]["learning_applies"])
        out = Path(self.tmp.name) / "train.jsonl"
        self.assertEqual(export_training_data(self.db, out), 0)               # nothing from this user is exported

    def test_keep_past_option_and_turning_back_on(self):
        t = self.reg()[1]["token"]
        self.opt_in(t)
        self.chat(t, "kept")
        r = self.call("POST", "/v1/settings/learning", dict(learning_opt_in=False, withdraw_past=False), token=t)[1]
        self.assertEqual(r["past_messages_withdrawn"], 0)
        self.assertEqual(self.call("POST", "/v1/settings/learning", dict(learning_opt_in=True), token=t)[1]["error"],
                         "learning_consent_required")                          # turning ON needs the fingerprint again
        self.opt_in(t)
        self.assertTrue(self.chat(t, "again")[1]["learning_applies"])

    def test_training_export_scrubs_pii_excludes_users_and_marks_used(self):
        a = self.reg("alice")[1]["token"]
        self.opt_in(a)
        b = self.reg("bob", ip="4.4.4.4", learning_opt_in=False)[1]["token"]
        self.chat(a, "mail me at jane.doe@example.com or call +1 (555) 123-4567 see https://x.example/p?k=1")
        self.chat(b, "bob's private words")
        out = Path(self.tmp.name) / "train.jsonl"
        n = export_training_data(self.db, out)
        text = out.read_text()
        self.assertEqual(n, 2)                                                # only alice's two messages
        for leaked in ("jane.doe", "555", "x.example", "bob", "alice"):
            self.assertNotIn(leaked, text)
        self.assertIn("[email]", text)
        self.assertEqual(export_training_data(self.db, Path(self.tmp.name) / "again.jsonl"), 0)  # not exported twice

    def test_scrub_function(self):
        self.assertEqual(scrub("a@b.co"), "[email]")
        self.assertIn("[secret]", scrub("key " + "a1" * 20))

    def test_terms_change_blocks_chat_until_reaccepted(self):
        t = self.reg()[1]["token"]
        self.svc.terms = "New terms v2"
        import hashlib
        self.svc.terms_sha = hashlib.sha256(self.svc.terms.encode()).hexdigest()
        self.assertEqual(self.chat(t)[1]["error"], "terms_changed")
        self.assertEqual(self.call("POST", "/v1/accept-terms", dict(sha256="bad"), token=t)[0], 400)
        self.call("POST", "/v1/accept-terms", dict(sha256=self.svc.terms_sha), token=t)
        self.assertEqual(self.chat(t)[0], 200)

    def test_export_and_delete_account(self):
        t = self.reg()[1]["token"]
        self.chat(t, "remember this")
        exp = self.call("POST", "/v1/account/export", token=t)[1]
        self.assertEqual(len(exp["messages"]), 2)
        self.assertNotIn("pw_hash", json.dumps(exp))
        self.assertEqual(self.call("POST", "/v1/account/delete", dict(password="wrong-password-1"), token=t)[0], 401)
        self.assertEqual(self.call("POST", "/v1/account/delete", dict(password=PW), token=t)[0], 200)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM messages")["c"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM users")["c"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM sessions")["c"], 0)
        self.assertEqual(self.call("POST", "/v1/me", token=t)[0], 401)

    def test_audit_events_never_contain_content(self):
        t = self.reg()[1]["token"]
        self.chat(t, "top secret sentence")
        self.assertNotIn("top secret", json.dumps([dict(r) for r in self.db.all("SELECT * FROM events")]))

    def test_terms_text_says_learning_is_off_unless_you_opt_in(self):
        text = (ROOT / "server" / "jarvis_server" / "terms_en.md").read_text().lower().replace("**", "")
        for needle in ("learning is off by default", "draft", "[operator:", "at least 16", "cannot be removed from a model"):
            self.assertIn(needle, text, needle)
        self.assertNotIn("on by default", text)
        notice = (ROOT / "server" / "jarvis_server" / "learning_notice_en.md").read_text().lower()
        for needle in ("off unless you confirm", "after you turn it on", "never sent anywhere", "delete-chats", "best effort"):
            self.assertIn(needle, notice, needle)


class LiveHttpAndClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "s.db")
        self.svc = Service(self.db, None, terms_text="Terms. Learning is ON by default and can be turned off.")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.svc, False))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.policy = NetworkPolicy(allow_insecure_localhost=True)
        self.policy.enable("online_assistant")
        self.tokens = TokenStore(Path(self.tmp.name) / "tok")
        self.client = OnlineClient(self.policy, self.url, self.tokens, timeout=10)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.db.close()
        self.tmp.cleanup()

    def scripted(self, answers):
        it = iter(answers)
        out = []
        return (lambda p: next(it)), (lambda p: next(it)), out.append, out

    def test_full_journey_with_no_model_connected(self):
        ask, secret, say, out = self.scripted(["I accept", "yes", "I consent", "carol", PW, PW])
        # ask and secret share one iterator so the order matches the prompts
        self.assertTrue(register_flow(self.client, ask, secret, say))
        text = "\n".join(out)
        self.assertIn("OFF by default", text)
        self.assertIn("RECOVERY CODE", text)
        self.assertTrue(self.client.me()["learning_opt_in"])
        with self.assertRaises(ServiceError) as cm:
            self.client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.code, "no_model")                       # honest: no model, no fake answer
        self.assertIn("cannot answer", cm.exception.message)
        self.assertFalse(self.client.health()["model_connected"])
        self.assertFalse(self.client.set_learning(False)["learning_opt_in"])
        self.client.logout()
        with self.assertRaises(ServiceError) as cm:
            self.client.me()
        self.assertEqual(cm.exception.code, "auth_required")                  # token removed: must sign in again
        ask, secret, say, out = self.scripted(["carol", PW])
        self.assertTrue(login_flow(self.client, ask, secret, say))

    def test_declined_terms_or_age_or_mismatched_password_create_no_account(self):
        for answers in (["no"], ["I accept", "no"], ["I accept", "yes", "", "dave", PW, "different pass 1"]):
            ask, secret, say, out = self.scripted(answers)
            self.assertFalse(register_flow(self.client, ask, secret, say))
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM users")["c"], 0)

    def test_chat_works_once_a_backend_is_connected(self):
        self.svc.backend = FakeBackend()
        ask, secret, say, out = self.scripted(["I accept", "yes", "off", "erin", PW, PW])
        self.assertTrue(register_flow(self.client, ask, secret, say))
        self.assertIn("OFF", "\n".join(out))
        r = self.client.chat([{"role": "user", "content": "hello"}])
        self.assertFalse(r["learning_applies"])
        self.assertEqual(self.client.delete_account(PW)["deleted"], True)

    def test_client_cannot_reach_server_when_feature_disabled_or_http_nonlocal(self):
        off = OnlineClient(NetworkPolicy(), self.url, self.tokens)
        with self.assertRaises(NetworkDisabled):
            off.health()
        strict = NetworkPolicy()
        strict.enable("online_assistant")
        with self.assertRaises(NetworkDisabled):
            OnlineClient(strict, self.url, self.tokens).health()              # plain http refused without the dev flag
        with self.assertRaises(NetworkDisabled):
            OnlineClient(self.policy, "http://example.com", self.tokens).health()

    def test_unreachable_server_is_a_clean_error(self):
        c = OnlineClient(self.policy, "http://127.0.0.1:9", self.tokens, timeout=2)
        with self.assertRaises(ServiceError) as cm:
            c.health()
        self.assertEqual(cm.exception.code, "unreachable")

    def test_audit_log_records_host_not_content(self):
        self.client.health()
        self.assertEqual(self.policy.audit[-1]["feature"], "online_assistant")
        self.assertNotIn("password", json.dumps(self.policy.audit))


class TokenStoreTests(unittest.TestCase):
    def test_plain_file_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            ts = TokenStore(Path(d) / "t", windows=False)
            self.assertTrue(ts.save("abc"))
            self.assertEqual(ts.load(), "abc")
            if os.name != "nt":
                self.assertEqual(oct(os.stat(Path(d) / "t").st_mode & 0o777), "0o600")
            ts.clear()
            self.assertIsNone(ts.load())

    def test_windows_mode_never_falls_back_to_plain_text(self):
        with tempfile.TemporaryDirectory() as d:
            def boom(b): raise OSError("dpapi failed")
            ts = TokenStore(Path(d) / "t", protect=boom, unprotect=boom, windows=True)
            self.assertFalse(ts.save("secret-token"))
            self.assertFalse((Path(d) / "t").exists())
            ts2 = TokenStore(Path(d) / "t2", protect=lambda b: b[::-1], unprotect=lambda b: b[::-1], windows=True)
            self.assertTrue(ts2.save("secret-token"))
            self.assertNotIn("secret-token", (Path(d) / "t2").read_text())
            self.assertEqual(ts2.load(), "secret-token")


class ServerStartupGuards(unittest.TestCase):
    def test_refuses_public_bind_without_tls(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(server_main(["--db", f"{d}/s.db", "--host", "0.0.0.0"]), 2)

    def test_refuses_draft_terms_when_public(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(server_main(["--db", f"{d}/s.db", "--host", "0.0.0.0", "--behind-tls-proxy"]), 2)

    def test_missing_model_dir_fails_loudly_not_with_a_fake(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(server_main(["--db", f"{d}/s.db", "--model-dir", f"{d}/nope"]), 2)


class PackagingBoundaryTests(unittest.TestCase):
    def test_server_is_not_in_client_source_or_exe(self):
        self.assertFalse((ROOT / "src" / "jarvis_server").exists())
        spec = (ROOT / "packaging" / "jarvis.spec").read_text()
        self.assertNotIn("jarvis_server", spec)

    def test_client_cli_requires_connection_and_blocks_http(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            def cli(*a, inp=""):
                p = subprocess.run([sys.executable, "-W", "ignore", "-m", "jarvis", "--data-dir", d, *a], input=inp,
                                   capture_output=True, text=True, env=env, timeout=60)
                return p.returncode, p.stdout, p.stderr
            rc, _, err = cli("online", "chat", "hi")
            self.assertEqual(rc, 2)
            self.assertIn("Not connected", err)
            self.assertEqual(cli("online", "connect", "http://example.com")[0], 2)
            self.assertEqual(cli("online", "connect", "https://svc.example", inp="no\n")[0], 1)   # needs explicit yes
            self.assertEqual(json.loads(cli("privacy")[1])["online_features"]["online_assistant"], False)
            self.assertEqual(cli("online", "connect", "https://svc.example", inp="yes\n")[0], 0)
            self.assertEqual(json.loads(cli("privacy")[1])["online_features"]["online_assistant"], True)
            self.assertEqual(cli("online", "disconnect")[0], 0)


if __name__ == "__main__":
    unittest.main()
