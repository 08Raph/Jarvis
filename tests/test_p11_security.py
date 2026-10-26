"""Prompt 11: regression tests for defects found by the final audit (docs/PROMPT11_FINAL_AUDIT.md). One class per finding id.
REAL: the real policy/redaction/storage code, real files, a real loopback HTTP server. Nothing here ran on Windows."""
import contextlib
import http.server
import io
import ipaddress
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from p11_common import AppCase, ROOT  # noqa: F401
from jarvis import cli
from jarvis.memory import secrets
from jarvis.net import policy as NP


class F1_SsrfEmbeddedAddresses(unittest.TestCase):
    """F-1: IPv6 forms that embed a private IPv4 address were accepted (64:ff9b::7f00:1 = NAT64 + 127.0.0.1)."""

    def refused(self, ip):
        fam = 10 if ":" in ip else 2
        with mock.patch("socket.getaddrinfo", return_value=[(fam, 1, 6, "", (ip, 443))]):
            with self.assertRaises(NP.NetworkDisabled, msg=ip):
                NP.assert_public_host("example.test")

    def test_embedded_private_addresses_are_refused(self):
        for ip in ("64:ff9b::7f00:1", "64:ff9b::a00:5", "64:ff9b::c0a8:101", "64:ff9b::a9fe:a9fe", "64:ff9b:1::1", "::ffff:127.0.0.1",
                   "2002:7f00:1::", "2002:a00:1::", "::1", "fe80::1", "127.0.0.1", "169.254.169.254", "100.64.0.1"):
            self.refused(ip)

    def test_public_addresses_including_embedded_public_ones_pass(self):
        for ip in ("8.8.8.8", "93.184.216.34", "::ffff:8.8.8.8", "64:ff9b::808:808", "2606:4700:4700::1111"):
            fam = 10 if ":" in ip else 2
            with mock.patch("socket.getaddrinfo", return_value=[(fam, 1, 6, "", (ip, 443))]):
                NP.assert_public_host("example.test")

    def test_one_private_answer_among_public_ones_is_enough_to_refuse(self):
        infos = [(2, 1, 6, "", ("8.8.8.8", 443)), (10, 1, 6, "", ("64:ff9b::7f00:1", 443, 0, 0))]
        with mock.patch("socket.getaddrinfo", return_value=infos):
            with self.assertRaises(NP.NetworkDisabled):
                NP.assert_public_host("example.test")


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"internal secret page")

    def log_message(self, *a):
        pass


class F1b_DnsRebindingPeerCheck(unittest.TestCase):
    """F-1b: the name is checked before connecting, but a hostile DNS server can answer differently at connect time. The response must not be
    handed back when the connection really reached a non-public address. (A real loopback server stands in for the 'internal' service.)"""

    def setUp(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.server_close)
        self.url = f"http://localhost:{self.srv.server_address[1]}/"
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def policy(self):
        pol = NP.NetworkPolicy(allow_insecure_localhost=True)       # test-only switch so a plain-http loopback server can be used
        pol.enable("web_research")
        return pol

    def test_rebinding_is_detected_after_connecting(self):
        # Simulate the rebinding: the pre-connect lookup is made to look public, the real connection goes to 127.0.0.1.
        with mock.patch.object(NP, "assert_public_host", lambda host: None):
            with self.assertRaises(NP.NetworkDisabled) as cm:
                self.policy().open("web_research", self.url, require_public=True)
        self.assertIn("DNS rebinding", str(cm.exception))

    def test_without_require_public_the_check_is_not_applied(self):
        r = self.policy().open("web_research", self.url, require_public=False)
        self.assertEqual(r.read(), b"internal secret page")
        r.close()

    def test_unreadable_peer_fails_closed(self):
        class Fake:
            closed = False
            def close(self): self.closed = True
        with self.assertRaises(NP.NetworkDisabled):
            NP.assert_peer_public(Fake())

    def test_peer_check_accepts_a_public_peer(self):
        class Sock:
            def getpeername(self): return ("93.184.216.34", 443)
        class Raw: _sock = Sock()
        class Fp: raw = Raw()
        class Resp: fp = Fp()
        NP.assert_peer_public(Resp())


class F2_SecretRedaction(unittest.TestCase):
    """F-2: secrets in .env / config style (DB_PASSWORD=..., client_secret: ...), URLs with credentials and Authorization headers were not detected,
    so they could be saved to memory and survive 'redaction' in logs, mission records and diagnostics."""

    MUST_DETECT = ["DB_PASSWORD=hunter2", "MY_API_KEY=abc123", "client_secret=zzzz1111", "SECRET_KEY='django-insecure-abc'", "export STRIPE_SECRET=abcdef",
                   "postgres://admin:s3cretpw@db.internal:5432/app", "Authorization: Bearer abc123def456", "Authorization: Basic dXNlcjpwYXNz",
                   "token=abc123xyz", "GITHUB_TOKEN = 'abcdefgh12345'", "password is 'correct horse battery'", "private_key: abcdefghi"]
    MUST_ALLOW = ["max_new_tokens: 128", "max_tokens = 4096", "my secretary: Jane", "token limit is 4096", "bypass: true", "passenger_count: 3",
                  "git clone https://github.com/user/repo.git", "authorization is required for this action", "basic information about the plan",
                  "Remember my meeting at 3pm: bring slides", "key: value", "tokens: 12", "token_limit: 4096"]

    def test_secret_shapes_are_detected_and_removed(self):
        for s in self.MUST_DETECT:
            self.assertIsNotNone(secrets.find_secret(s), s)
            out = secrets.redact(s)
            for leak in ("hunter2", "abc123", "zzzz1111", "django-insecure", "s3cretpw", "dXNlcjpwYXNz", "correct horse", "abcdefgh12345", "abcdefghi"):
                self.assertNotIn(leak, out, s)

    def test_ordinary_text_is_not_flagged(self):
        for s in self.MUST_ALLOW:
            self.assertIsNone(secrets.find_secret(s), s)

    def test_a_quoted_password_with_spaces_is_removed_whole(self):
        self.assertNotIn("horse", secrets.redact("my password is 'correct horse battery'"))

    def test_memory_refuses_to_store_them(self):
        from jarvis.memory.personal import PersonalMemory, MemoryError_
        mem = PersonalMemory.guest()
        for s in ("DB_PASSWORD=hunter2", "postgres://u:pw1234@h/db"):
            with self.assertRaises((secrets.SecretRejected, MemoryError_)):
                mem.add("explicit_preference", s)
        mem.close()

    def test_mission_records_and_diagnostics_use_the_stronger_redaction(self):
        from jarvis.missions.store import redact_obj
        self.assertNotIn("hunter2", str(redact_obj({"message": "loaded DB_PASSWORD=hunter2 from .env"})))
        from jarvis.app import diagnostics
        self.assertNotIn("hunter2", diagnostics.scrub_paths("log: DB_PASSWORD=hunter2"))


class F3_CorruptMemoryDatabase(AppCase):
    """F-3: a damaged memory.sqlite produced a raw sqlite traceback in the CLI and 'Unexpected problem (DatabaseError)' in the window."""
    JUNK = b"\x00\x01 not a database {{{"

    def test_file_is_preserved_and_message_is_actionable(self):
        from jarvis.memory.store import MemoryDatabaseError, MemoryStore
        db = self.cfg.data_dir / "memory.sqlite"
        db.write_bytes(self.JUNK)
        with self.assertRaises(MemoryDatabaseError) as cm:
            MemoryStore(db)
        self.assertIn("jarvis restore", str(cm.exception))
        self.assertIn("did not change or delete", str(cm.exception))
        self.assertEqual(db.read_bytes(), self.JUNK)
        self.assertEqual(sorted(p.name for p in self.cfg.data_dir.iterdir()), ["memory.sqlite"], "no journal/temp file may be left behind")

    def test_cli_reports_instead_of_a_traceback(self):
        (self.cfg.data_dir / "memory.sqlite").write_bytes(self.JUNK)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "memory", "list"])
        self.assertNotEqual(rc, 0)
        self.assertIn("cannot be read", err.getvalue())
        self.assertEqual((self.cfg.data_dir / "memory.sqlite").read_bytes(), self.JUNK)

    def test_window_controller_gives_a_plain_message(self):
        from jarvis.app.controller import AppController, explain_error
        (self.cfg.data_dir / "memory.sqlite").write_bytes(self.JUNK)
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        with self.assertRaises(Exception) as cm:
            c.memory_items()
        msg = explain_error(cm.exception)
        self.assertIn("cannot be read", msg)
        self.assertNotIn("Unexpected problem", msg)

    def test_other_features_still_work_with_a_damaged_memory_file(self):
        (self.cfg.data_dir / "memory.sqlite").write_bytes(self.JUNK)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "desktop", "status"])
        self.assertEqual(rc, 0)


class F4_ForgetAllCoversStoredCopies(AppCase):
    """F-4: `forget-all` left research_sources/ (full text of documents and web pages), code_snapshots/ and code_runs/ behind, and the docs did not say so."""

    def populate(self):
        pd = self.cfg.profile_dir
        pd.mkdir(parents=True, exist_ok=True)
        for d in ("research_sources", "code_snapshots", "code_runs", "code_backups", "desktop_checkpoints", "screenshots"):
            (pd / d).mkdir()
            (pd / d / "x.txt").write_text("PRIVATE-TEXT", encoding="utf-8")
        (pd / "desktop_audit.jsonl").write_text("{}", encoding="utf-8")

    def run_forget(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "memory", "forget-all", "--yes", *extra])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_default_removes_copies_but_keeps_undo_points_and_says_so(self):
        self.populate()
        msg = self.run_forget()
        pd = self.cfg.profile_dir
        for d in ("research_sources", "code_snapshots", "code_runs", "screenshots"):
            self.assertFalse((pd / d).exists(), d)
        self.assertTrue((pd / "code_backups" / "x.txt").exists())
        self.assertTrue((pd / "desktop_checkpoints" / "x.txt").exists())
        self.assertIn("--include-undo-points", msg)
        self.assertIn("code_backups", msg)

    def test_include_undo_points_removes_them(self):
        self.populate()
        self.run_forget("--include-undo-points")
        for d in ("code_backups", "desktop_checkpoints"):
            self.assertFalse((self.cfg.profile_dir / d).exists(), d)

    def test_window_path_uses_the_same_function(self):
        from jarvis.app.controller import AppController
        from jarvis.memory.personal import PersonalMemory
        self.populate()
        PersonalMemory.open(self.cfg.memory_db).close()
        c = AppController(self.cfg, simulate=True, dispatch=lambda fn: fn(), ask_user=lambda r, res: res(True))
        self.addCleanup(c.shutdown, 1.0)
        self.assertIsInstance(c.memory_forget_all(), str)
        self.assertFalse((self.cfg.profile_dir / "research_sources").exists())

    def test_docs_list_every_stored_copy(self):
        t = (ROOT / "docs" / "PRIVACY_AND_NETWORK.md").read_text(encoding="utf-8")
        for name in ("research_sources", "code_snapshots", "code_runs", "code_backups", "desktop_checkpoints"):
            self.assertIn(name, t)


if __name__ == "__main__":
    unittest.main()
