"""The Qt-free account layer (src/jarvis/online/account.py) against the REAL Jarvis Online service code, over REAL HTTP on 127.0.0.1 (plain HTTP to this computer is the
development-only mode the policy allows). Labelled fakes: (1) scrypt cost is lowered for speed; (2) the 'encrypting' token store in RememberMe uses a reversible stand-in
for DPAPI, because DPAPI exists only on Windows (it is exercised for real in test_p77_dpapi_windows, which runs on the Windows CI job). No test here touches a GUI."""
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(HERE))

from jarvis.online import account as A  # noqa: E402
from jarvis.online import rules  # noqa: E402
from jarvis.online.tokenstore import TokenStore  # noqa: E402
from jarvis_server import security as sec  # noqa: E402
from jarvis_server.__main__ import make_handler  # noqa: E402
from jarvis_server.app import Service  # noqa: E402
from jarvis_server.db import DB  # noqa: E402

PW = "correct horse battery"
PW2 = "another long passphrase 42"
REAL = (sec.SCRYPT_N, sec.SCRYPT_R, sec.SCRYPT_P)


class Live(unittest.TestCase):
    """One real server per test, one account controller (one 'PC') per helper call."""

    def setUp(self):
        sec.SCRYPT_N, sec.SCRYPT_R, sec.SCRYPT_P = 2 ** 10, 8, 1             # FAKE: cheaper hashing, for test speed only
        self.addCleanup(lambda: setattr(sec, "SCRYPT_N", REAL[0]) or setattr(sec, "SCRYPT_R", REAL[1]) or setattr(sec, "SCRYPT_P", REAL[2]))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "server.db")
        self.addCleanup(self.db.close)
        self.svc = Service(self.db, terms_text="Test terms for the account tests.")
        self.svc.limiter.allow = lambda *a, **k: True                          # FAKE: no abuse limits in the plumbing tests (the limits have their own tests in test_p76)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.svc, False))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.pcs = 0

    def pc(self, connect=True, url=None, tokens=None, timeout=20.0) -> A.AccountController:
        self.pcs += 1
        d = Path(self.tmp.name) / f"pc{self.pcs}"
        d.mkdir()
        ctl = A.AccountController(d, d / "settings.json", tokens=tokens or A._Tokens(d / "online.token", TokenStore(d / "online.token", windows=False)), timeout=timeout)
        if connect:
            self.assertTrue(ctl.connect(url or self.url, confirmed=True, local_http=True).ok)
        return ctl

    def signup(self, ctl, name="alice", pw=PW, **kw):
        a = dict(display_name="", pw2=pw, accepted_terms=True, age_ok=True)
        a.update(kw)
        return ctl.sign_up(name, a["display_name"], pw, a["pw2"], a["accepted_terms"], a["age_ok"], a.get("remember", False))


class Connection(Live):
    def test_nothing_is_connected_until_confirmed(self):
        ctl = self.pc(connect=False)
        self.assertFalse(ctl.connect(self.url, confirmed=False, local_http=True).ok)
        st = ctl.state()
        self.assertEqual((st["connected"], st["signed_in"]), (False, False))
        self.assertEqual(ctl.sign_in("alice", PW).code, "not_connected")

    def test_only_https_or_this_computer_over_http_is_accepted(self):
        ctl = self.pc(connect=False)
        for url in ("http://example.com", "ftp://x", "example.com", "http://192.168.1.5", "https://", ""):
            self.assertEqual(ctl.connect(url, confirmed=True, local_http=True).code, "bad_url", url)
        self.assertEqual(ctl.connect("http://127.0.0.1:9", confirmed=True, local_http=False).code, "bad_url")   # needs the development box
        self.assertTrue(ctl.connect("https://jarvis.example.org/", confirmed=True).ok)
        self.assertEqual(ctl.state()["url"], "https://jarvis.example.org")

    def test_state_reads_files_only_and_makes_no_network_call(self):
        ctl = self.pc(url="http://127.0.0.1:9")                                # a closed port; state() must not try it
        from unittest import mock
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("state() made a network request")):
            for _ in range(5):
                st = ctl.state()
        self.assertTrue(st["connected"])

    def test_disconnect_removes_the_saved_sign_in_and_the_address(self):
        ctl = self.pc()
        self.assertTrue(self.signup(ctl).ok)
        self.assertTrue(ctl.disconnect().ok)
        self.assertEqual((ctl.state()["connected"], ctl.state()["signed_in"]), (False, False))
        self.assertFalse((ctl.dir / "online.json").exists())


class SignUp(Live):
    def test_inline_validation_happens_before_any_request(self):
        ctl = self.pc(url="http://127.0.0.1:9")                                # unreachable: a request would give code 'unreachable', not 'invalid'
        r = self.signup(ctl, name="a b", pw="short", pw2="different", accepted_terms=False, age_ok=False)
        self.assertEqual(r.code, "invalid")
        self.assertEqual(set(r.fields), {"username", "password", "password2", "terms", "age"})
        self.assertEqual(self.signup(ctl, pw=PW, pw2=PW + "x").fields.keys(), {"password2"})
        self.assertIn("password", self.signup(ctl, pw="password1").fields)
        self.assertIn("display_name", self.signup(ctl, display_name="x" * 41).fields)

    def test_successful_registration_signs_in_and_returns_the_recovery_code_once(self):
        ctl = self.pc()
        r = self.signup(ctl, name="Alice", display_name="Ally")
        self.assertTrue(r.ok, r)
        self.assertRegex(r.data["recovery_code"], r"^[0-9A-F]{5}(-[0-9A-F]{5}){3}$")
        st = ctl.state()
        self.assertTrue(st["signed_in"])
        self.assertEqual(st["profile"], {"username": "alice", "display_name": "Ally"})
        self.assertTrue(ctl.refresh().ok)
        self.assertNotIn(PW, json.dumps(ctl.state()))

    def test_existing_account_is_reported_on_the_username_field(self):
        self.assertTrue(self.signup(self.pc()).ok)
        r = self.signup(self.pc(), pw=PW2)
        self.assertEqual((r.ok, r.code), (False, "username_unavailable"))
        self.assertIn("username", r.fields)

    def test_the_server_still_refuses_what_a_modified_client_would_send(self):
        ctl = self.pc()
        ctl.validate_signup = lambda *a, **k: {}                                # a client that skips its own checks
        r = ctl.sign_up("alice", "", "short", "short", True, True)
        self.assertEqual((r.ok, r.code), (False, "weak_password"))
        self.assertIn("password", r.fields)

    def test_the_client_and_server_rules_agree(self):
        for u in ["alice", "Al", "a b", "ab", "a" * 33, "a.b-c_d", "-abc", "ÄÖÜ", "bob'--", ""]:
            self.assertEqual(rules.username_problem(u) is None, sec.valid_username(sec.normalize_username(u)), u)
        for pw, user in [("short", "x"), ("password1", "x"), ("a" * 10, "x"), ("abcdefghij", "abcdefghij"), (PW, "x"), ("p" * 201, "x"), ("ab" * 6, "x")]:
            self.assertEqual(rules.password_problem(pw, user) is None, sec.password_problem(pw, user) is None, pw)
        for n in ["Ally", "", "   ", "x" * 41, "x" * 40, "bad‮name", "tab\there", "ok name"]:
            self.assertEqual(rules.display_name_problem(n) is None, sec.display_name_problem(n) is None, repr(n))


class SignIn(Live):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.signup(self.pc(), name="alice").ok)

    def test_sign_in_success(self):
        ctl = self.pc()
        r = ctl.sign_in("ALICE", PW)
        self.assertTrue(r.ok, r)
        self.assertEqual(ctl.state()["profile"]["username"], "alice")

    def test_wrong_password_and_unknown_user_give_the_same_words(self):
        ctl = self.pc()
        a, b = ctl.sign_in("alice", "wrong-password-1"), ctl.sign_in("nobody", "wrong-password-1")
        self.assertEqual((a.ok, a.code, a.message), (b.ok, b.code, b.message))
        self.assertEqual(a.code, "bad_credentials")
        self.assertFalse(ctl.state()["signed_in"])

    def test_empty_fields_are_flagged_without_a_request(self):
        r = self.pc(url="http://127.0.0.1:9").sign_in("", "")
        self.assertEqual((r.code, set(r.fields)), ("invalid", {"username", "password"}))

    def test_server_unavailable_is_a_message_not_a_crash(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()          # a port nothing listens on
        ctl = self.pc(url=f"http://127.0.0.1:{port}")
        r = ctl.sign_in("alice", PW)
        self.assertEqual((r.ok, r.code), (False, "unreachable"))
        self.assertIn("local Jarvis still works", r.message)
        self.assertNotIn(PW, r.message)

    def test_a_server_that_never_answers_times_out_in_bounded_time(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(5)                                  # accepts connections, never replies
        self.addCleanup(s.close)
        ctl = self.pc(url=f"http://127.0.0.1:{s.getsockname()[1]}", timeout=0.6)
        t = time.time()
        r = ctl.sign_in("alice", PW)
        self.assertEqual((r.ok, r.code), (False, "timeout"))
        self.assertLess(time.time() - t, 5)

    def test_a_server_that_sends_junk_is_handled(self):
        class Junk(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200); self.send_header("Content-Length", "9"); self.end_headers(); self.wfile.write(b"not json!")
            def log_message(self, *a): pass
        h = ThreadingHTTPServer(("127.0.0.1", 0), Junk)
        threading.Thread(target=h.serve_forever, daemon=True).start()
        self.addCleanup(h.server_close); self.addCleanup(h.shutdown)
        r = self.pc(url=f"http://127.0.0.1:{h.server_address[1]}").sign_in("alice", PW)
        self.assertEqual((r.ok, r.code), (False, "bad_response"))

    def test_session_expiry_is_detected_and_clears_the_token(self):
        ctl = self.pc()
        self.assertTrue(ctl.sign_in("alice", PW).ok)
        self.db.q("DELETE FROM sessions")                                                      # the service ended the session (expiry, revocation elsewhere)
        r = ctl.refresh()
        self.assertEqual((r.ok, r.code), (False, "session_expired"))
        st = ctl.state()
        self.assertEqual((st["signed_in"], st["expired"]), (False, True))
        self.assertIsNone(ctl.tokens.load())
        self.assertTrue(ctl.sign_in("alice", PW).ok)                                           # signing in again clears the expired state
        self.assertFalse(ctl.state()["expired"])

    def test_renew_keeps_the_person_signed_in_with_a_new_token(self):
        ctl = self.pc()
        ctl.sign_in("alice", PW)
        before = ctl.tokens.load()
        self.assertTrue(ctl.renew().ok)
        self.assertNotEqual(before, ctl.tokens.load())
        self.assertTrue(ctl.refresh().ok)


class SignOutAndAccount(Live):
    def setUp(self):
        super().setUp()
        self.ctl = self.pc()
        self.assertTrue(self.signup(self.ctl).ok)

    def test_sign_out_ends_the_session_on_the_service_and_on_this_pc(self):
        tok = self.ctl.tokens.load()
        r = self.ctl.sign_out()
        self.assertEqual((r.ok, r.message), (True, "Signed out."))
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM sessions")[0], 0)
        self.assertEqual((self.ctl.state()["signed_in"], self.ctl.profile), (False, {}))
        s, _ = self.svc.handle("POST", "/v1/me", {"Authorization": "Bearer " + tok}, b"{}", "1.1.1.1")
        self.assertEqual(s, 401)

    def test_sign_out_still_works_locally_when_the_service_is_gone_and_says_so(self):
        self.httpd.shutdown(); self.httpd.server_close()
        r = self.ctl.sign_out()
        self.assertEqual((r.ok, r.code), (True, "not_told"))
        self.assertIn("not told", r.message)
        self.assertIsNone(self.ctl.tokens.load())

    def test_sign_out_everywhere_ends_every_session(self):
        other = self.pc()
        other.sign_in("alice", PW)
        self.assertTrue(self.ctl.sign_out(everywhere=True).ok)
        self.assertEqual(other.refresh().code, "session_expired")

    def test_account_switching_never_shows_the_previous_person(self):
        self.signup(self.pc(), name="bobby", display_name="Bob")
        self.ctl.sign_out()
        self.assertTrue(self.ctl.sign_in("bobby", PW).ok)
        self.assertEqual(self.ctl.state()["profile"]["username"], "bobby")
        self.ctl.sign_out()
        self.assertEqual(self.ctl.profile, {})
        self.assertEqual(self.ctl.set_display_name("Hacker").code, "signed_out")               # no leftover session

    def test_display_name_change(self):
        self.assertEqual(self.ctl.set_display_name("").code, "invalid")
        self.assertTrue(self.ctl.set_display_name("  New Name ").ok)
        self.assertEqual(self.ctl.state()["profile"]["display_name"], "New Name")
        self.assertEqual(self.ctl.refresh().data["display_name"], "New Name")

    def test_password_change_checks_everything_and_works(self):
        self.assertEqual(self.ctl.change_password("", PW2, PW2).code, "invalid")
        self.assertEqual(self.ctl.change_password(PW, "short", "short").code, "invalid")
        self.assertEqual(self.ctl.change_password(PW, PW2, PW2 + "x").fields.keys(), {"password2"})
        r = self.ctl.change_password("not-the-password", PW2, PW2)
        self.assertEqual((r.ok, r.code), (False, "bad_credentials"))
        self.assertTrue(self.ctl.change_password(PW, PW2, PW2).ok)
        self.assertEqual(self.pc().sign_in("alice", PW).code, "bad_credentials")
        self.assertTrue(self.pc().sign_in("alice", PW2).ok)
        self.assertTrue(self.ctl.refresh().ok)                                                  # this session survives the change

    def test_delete_account_needs_the_password_and_leaves_local_data_alone(self):
        local = self.ctl.dir / "memory.sqlite"
        local.write_text("local memory stand-in")
        self.assertEqual(self.ctl.delete_account("").code, "invalid")
        self.assertEqual(self.ctl.delete_account("wrong-password-1").code, "bad_credentials")
        self.assertTrue(self.ctl.state()["signed_in"])
        self.assertTrue(self.ctl.delete_account(PW).ok)
        self.assertFalse(self.ctl.state()["signed_in"])
        self.assertEqual(self.pc().sign_in("alice", PW).code, "bad_credentials")
        self.assertEqual(local.read_text(), "local memory stand-in")

    def test_forgot_password_uses_the_recovery_code_and_nothing_pretends_to_send_email(self):
        ctl = self.pc()
        code = self.signup(ctl, name="carol").data["recovery_code"]
        r = ctl.recover_password("carol", "WRONG-CODE-0000-0000", PW2, PW2)
        self.assertEqual((r.ok, r.code), (False, "bad_credentials"))
        r = ctl.recover_password("carol", code, PW2, PW2)
        self.assertTrue(r.ok, r)
        self.assertNotEqual(r.data["recovery_code"], code)
        self.assertTrue(self.pc().sign_in("carol", PW2).ok)
        import inspect
        self.assertNotRegex(inspect.getsource(A).lower(), r"smtp|sendmail|email was sent|we.ve sent")


class RememberMe(Live):
    class Reversible(TokenStore):
        """FAKE for DPAPI: pretends to encrypt (reverses the text) so the logic can be tested off Windows."""
        def __init__(self, path):
            super().__init__(path, protect=lambda b: b[::-1], unprotect=lambda b: b[::-1], windows=True)

    def test_remember_is_unavailable_where_the_token_cannot_be_encrypted_and_nothing_is_written(self):
        ctl = self.pc()
        self.assertFalse(ctl.state()["remember_available"])
        self.signup(ctl, remember=True)
        self.assertFalse((ctl.dir / "online.token").exists())
        self.assertTrue(ctl.state()["signed_in"])

    def test_without_remember_nothing_is_written_even_when_encryption_exists(self):
        d = Path(self.tmp.name) / "pcR"; d.mkdir()
        ctl = A.AccountController(d, d / "settings.json", tokens=A._Tokens(d / "online.token", self.Reversible(d / "online.token")))
        ctl.connect(self.url, confirmed=True, local_http=True)
        self.signup(ctl, remember=False)
        self.assertFalse((d / "online.token").exists())

    def test_remember_writes_an_encrypted_token_a_new_session_can_use_and_sign_out_deletes_it(self):
        d = Path(self.tmp.name) / "pcR"; d.mkdir()
        mk = lambda: A.AccountController(d, d / "settings.json", tokens=A._Tokens(d / "online.token", self.Reversible(d / "online.token")))
        ctl = mk()
        ctl.connect(self.url, confirmed=True, local_http=True)
        self.signup(ctl, remember=True)
        raw = (d / "online.token").read_text()
        self.assertTrue(raw.startswith("dpapi:"))
        self.assertNotIn(ctl.tokens.load(), raw)
        again = mk()                                                                            # "restart the app"
        self.assertTrue(again.state()["signed_in"])
        self.assertTrue(again.refresh().ok)
        again.sign_out()
        self.assertFalse((d / "online.token").exists())

    def test_sign_out_deletes_a_remembered_token_even_when_online_features_were_switched_off_meanwhile(self):
        d = Path(self.tmp.name) / "pcR"; d.mkdir()
        ctl = A.AccountController(d, d / "settings.json", tokens=A._Tokens(d / "online.token", self.Reversible(d / "online.token")))
        ctl.connect(self.url, confirmed=True, local_http=True)
        self.signup(ctl, remember=True)
        self.assertTrue((d / "online.token").exists())
        pol = ctl._policy(); pol.disable("online_assistant"); pol.save(ctl.settings_path)      # no client can be built now
        self.assertTrue(ctl.sign_out().ok)
        self.assertFalse((d / "online.token").exists())


class LocalAiIndependence(unittest.TestCase):
    def test_the_account_modules_do_not_import_the_assistant_the_engine_or_memory(self):
        for f in ("account.py", "client.py", "flows.py", "rules.py", "tokenstore.py"):
            src = (ROOT / "src" / "jarvis" / "online" / f).read_text(encoding="utf-8")
            for bad in ("jarvis.assistant", "jarvis.inference", "jarvis.models", "jarvis.memory", "jarvis.model ", "llama_cpp", "jarvis.brains"):
                self.assertNotIn("import " + bad.strip(), src, f)
                self.assertNotIn("from " + bad.strip(), src, f)

    def test_the_local_ai_never_imports_the_account_layer(self):
        for sub in ("inference", "models", "memory", "brains", "tokenizer", "model"):
            for py in (ROOT / "src" / "jarvis" / sub).rglob("*.py"):
                s = py.read_text(encoding="utf-8")
                self.assertNotIn("jarvis.online", s, py.name)
        self.assertNotIn("jarvis.online", (ROOT / "src" / "jarvis" / "assistant.py").read_text(encoding="utf-8"))


class LocalChatWithTheServiceGone(Live):
    """REAL controller, REAL files and SQLite, FAKE scripted model (FakeLlama). Shows the local reply path does not touch the account service."""

    def test_a_local_reply_still_works_while_a_dead_service_is_configured_and_signed_in(self):
        from p10_common import FakeLlama, fake_engine_factory, AppCase
        from jarvis.app.controller import AppController

        class Case(AppCase):
            def runTest(self): pass
        case = Case(); case.setUp(); self.addCleanup(case.tearDown)
        case.install_model()
        acct = A.AccountController(case.cfg.data_dir, case.cfg.settings_path)
        acct.connect(self.url, confirmed=True, local_http=True)
        self.assertTrue(self.signup(acct).ok)
        self.httpd.shutdown(); self.httpd.server_close()                                       # the service dies while the person is "signed in"
        c = AppController(case.cfg, simulate=True, engine_factory=fake_engine_factory)
        self.addCleanup(c.shutdown, 1.0)
        self.assertTrue(c.send("hi"))
        end = time.time() + 10
        while time.time() < end and c.busy:
            c.tasks.pump(); time.sleep(0.01)
        a = [e for e in c.transcript.entries if e.kind == "assistant"]
        self.assertEqual([e.text for e in a], [FakeLlama.reply])
        self.assertEqual(acct.sign_in("alice", PW).code, "unreachable")                        # and the account side reports the problem without breaking anything
        self.assertEqual(len([e for e in c.transcript.entries if e.kind == "error"]), 0)


if __name__ == "__main__":
    unittest.main()
