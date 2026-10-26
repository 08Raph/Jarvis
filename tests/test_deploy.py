import http.server
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import jarvis
from jarvis.config import Config
from jarvis.firstrun import first_run
from jarvis.inference.context import ContextBuilder, bm25_rank
from jarvis.memory.store import SCHEMA_VERSION, MemoryStore
from jarvis.models.package import pack_model
from jarvis.net.policy import NetworkDisabled, NetworkPolicy
from jarvis.security import signing
from jarvis.storage import backup_sqlite, restore_sqlite
from jarvis.tokenizer.bpe import BPETokenizer
from jarvis.updater.check import UpdateError, check_for_update, parse_version

TOK = BPETokenizer.train(("the quick brown fox jumps over the lazy dog. " * 30), 300)


class ContextTests(unittest.TestCase):
    def test_bm25_prefers_relevant_note(self):
        notes = ["buy milk and eggs", "dentist appointment on friday", "wifi password hint is on the fridge"]
        self.assertEqual(bm25_rank("when is my dentist appointment", notes, k=1)[0][0], 1)
        self.assertEqual(bm25_rank("zzzz", notes), [])
        self.assertEqual(bm25_rank("anything", []), [])

    def test_long_session_fits_and_drops_oldest_first(self):
        cb = ContextBuilder(TOK, block_size=256, reserve_new_tokens=64)
        hist = []
        for i in range(500):
            hist.append({"role": "user", "content": f"question number {i} about foxes"})
            hist.append({"role": "assistant", "content": f"answer number {i} about dogs"})
        r = cb.build("what is next?", hist, ["fox note one", "unrelated"])
        self.assertLessEqual(r.n_tokens, 256 - 64)
        self.assertGreater(r.turns_dropped, 900)
        self.assertIn("answer number 499", r.prompt)
        self.assertNotIn("question number 0 ", r.prompt)
        self.assertTrue(r.prompt.endswith("User: what is next?\nJarvis:"))

    def test_oversized_user_message_keeps_its_end(self):
        cb = ContextBuilder(TOK, block_size=128, reserve_new_tokens=32)
        r = cb.build("start " + "fox " * 2000 + "THE END", [])
        self.assertTrue(r.user_text_truncated)
        self.assertLessEqual(r.n_tokens, 96)
        self.assertIn("THE END", r.prompt)
        with self.assertRaises(ValueError):
            ContextBuilder(TOK, block_size=64, reserve_new_tokens=64)

    def test_relevant_note_is_injected(self):
        cb = ContextBuilder(TOK, block_size=256, reserve_new_tokens=64)
        r = cb.build("dentist appointment?", [], ["dentist appointment on friday", "buy milk"])
        self.assertEqual(r.notes_used, ["dentist appointment on friday"])


class NoQuotaTests(unittest.TestCase):
    def test_thousands_of_messages_are_stored_without_any_cap(self):
        m = MemoryStore()
        c = m.new_conversation()
        for i in range(3000):
            m.add_message(c, "user" if i % 2 == 0 else "assistant", f"message {i}")
        self.assertEqual(len(m.messages(c)), 3000)


class StorageTests(unittest.TestCase):
    def test_schema_guard_backup_restore(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            db = d / "m.sqlite"
            m = MemoryStore(db)
            m.add_note("keep me")
            m.close()
            con0 = sqlite3.connect(db)
            self.assertEqual(con0.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            con0.close()
            b = backup_sqlite(db, d / "bk")
            m = MemoryStore(db)
            m.delete_note(1)
            m.close()
            restore_sqlite(b, db)
            m = MemoryStore(db)
            self.assertEqual(m.notes()[0]["text"], "keep me")
            m.close()
            con = sqlite3.connect(db)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            con.commit()
            con.close()
            with self.assertRaises(RuntimeError):
                MemoryStore(db)

    def test_backup_pruning_and_corrupt_restore(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            MemoryStore(d / "m.sqlite").close()
            for _ in range(4):
                backup_sqlite(d / "m.sqlite", d / "bk", keep=2)
            self.assertEqual(len(list((d / "bk").glob("*.sqlite"))), 2)
            (d / "bad.sqlite").write_bytes(b"not a database at all" * 50)
            with self.assertRaises(Exception):
                restore_sqlite(d / "bad.sqlite", d / "m.sqlite")


class UpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.www = Path(cls.tmp.name)
        cls.sk, cls.pk = signing.generate_keypair()

        class H(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(H, directory=str(cls.www)))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/m.json"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def publish(self, payload, key=None, tamper=False):
        sig = signing.sign(payload, key or self.sk)
        if tamper:
            payload = dict(payload, latest="9.9.9")
        (self.www / "m.json").write_text(json.dumps({"payload": payload, "signature": sig}))

    def pol(self, enable=True):
        p = NetworkPolicy(allow_insecure_localhost=True)
        if enable:
            p.enable("update_check")
        return p

    GOOD = {"latest": "1.0.0", "installer_url": "https://example.com/s.exe", "installer_sha256": "a" * 64,
            "notes": "n"}

    def test_newer_same_and_older(self):
        self.publish(self.GOOD)
        info = check_for_update(self.pol(), "0.2.0", self.url, self.pk)
        self.assertEqual(info.version, "1.0.0")
        self.assertIsNone(check_for_update(self.pol(), "1.0.0", self.url, self.pk))
        self.assertIsNone(check_for_update(self.pol(), "2.0", self.url, self.pk))

    def test_untrusted_manifests_rejected(self):
        self.publish(self.GOOD, tamper=True)
        with self.assertRaises(UpdateError):
            check_for_update(self.pol(), "0.2.0", self.url, self.pk)
        _, other = signing.generate_keypair()
        self.publish(self.GOOD)
        with self.assertRaises(UpdateError):
            check_for_update(self.pol(), "0.2.0", self.url, other)
        self.publish(dict(self.GOOD, installer_url="http://evil.example/s.exe"))
        with self.assertRaises(UpdateError):
            check_for_update(self.pol(), "0.2.0", self.url, self.pk)
        (self.www / "m.json").write_text("garbage")
        with self.assertRaises(UpdateError):
            check_for_update(self.pol(), "0.2.0", self.url, self.pk)

    def test_disabled_by_default_no_request(self):
        self.publish(self.GOOD)
        p = self.pol(enable=False)
        with self.assertRaises(NetworkDisabled):
            check_for_update(p, "0.2.0", self.url, self.pk)
        self.assertEqual(p.audit, [])

    def test_version_parsing(self):
        self.assertLess(parse_version("0.9.0"), parse_version("0.10.0"))
        with self.assertRaises(ValueError):
            parse_version("1.0-beta")


class FirstRunTests(unittest.TestCase):
    def test_first_run_is_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(data_dir=Path(d) / "pj")
            r1 = first_run(cfg)
            self.assertFalse(any(r1["network"].values()))
            self.assertEqual(r1["installed_models"], [])
            self.assertTrue(r1["next_steps"])
            for sub in ("models", "backups", "logs"):
                self.assertTrue((cfg.data_dir / sub).is_dir())
            r2 = first_run(cfg)
            self.assertEqual(r1["created_at"], r2["created_at"])


class VersionConsistency(unittest.TestCase):
    def test_pyproject_matches_package(self):
        proj = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(proj["project"]["version"], jarvis.__version__)
        self.assertIn("cryptography", " ".join(proj["project"]["dependencies"]))


class CliTests(unittest.TestCase):
    def run_cli(self, data_dir, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        p = subprocess.run([sys.executable, "-W", "ignore", "-m", "jarvis", "--data-dir", str(data_dir), *args],
                           capture_output=True, text=True, env=env, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            data = d / "data"
            self.assertEqual(self.run_cli(data, "version")[1].strip(), jarvis.__version__)
            rc, out, _ = self.run_cli(data, "first-run")
            self.assertEqual(rc, 0)
            self.assertFalse(any(json.loads(out)["network"].values()))
            rc, out, _ = self.run_cli(data, "hardware")
            self.assertEqual(rc, 0)
            self.assertIn("assessments", json.loads(out))
            # privacy
            rc, out, err = self.run_cli(data, "privacy", "enable", "web_search")
            self.assertEqual(rc, 2)
            self.assertIn("no implementation", err)
            self.assertEqual(self.run_cli(data, "privacy", "enable", "cloud_ai")[0], 2)
            self.assertEqual(self.run_cli(data, "privacy", "enable", "update_check")[0], 0)
            self.assertTrue(json.loads(self.run_cli(data, "privacy")[1])["online_features"]["update_check"])
            self.assertFalse(json.loads(self.run_cli(data, "privacy", "disable", "update_check")[1])
                             ["online_features"]["update_check"])
            # update-check refuses while disabled
            rc, _, err = self.run_cli(data, "update-check", "--url", "https://example.invalid/m.json",
                                      "--public-key-hex", "00" * 32)
            self.assertEqual(rc, 2)
            self.assertIn("disabled", err)
            # chat with no model
            rc, _, err = self.run_cli(data, "chat", "hello")
            self.assertEqual(rc, 2)
            self.assertIn("No model is installed", err)
            # model lifecycle
            src = d / "src"
            src.mkdir()
            (src / "model.pt").write_bytes(b"w")
            (src / "tokenizer.json").write_text("{}")
            (src / "LICENSE.txt").write_text("l")
            pkg = d / "t.jarvismodel"
            sha = pack_model(src, pkg, "MIT")
            self.assertEqual(self.run_cli(data, "models", "install", str(pkg), "--id", "tiny", "--sha256", "0" * 64)[0], 3)
            self.assertEqual(self.run_cli(data, "models", "install", str(pkg), "--id", "tiny", "--sha256", sha)[0], 0)
            self.assertEqual(self.run_cli(data, "models", "select", "tiny")[0], 0)
            lst = json.loads(self.run_cli(data, "models", "list")[1])
            self.assertEqual((lst["active"], len(lst["installed"])), ("tiny", 1))
            self.assertEqual(self.run_cli(data, "models", "verify", "tiny")[0], 0)
            (data / "models" / "tiny" / "model.pt").write_bytes(b"tampered")
            self.assertEqual(self.run_cli(data, "models", "verify", "tiny")[0], 3)
            rc, _, err = self.run_cli(data, "chat", "hello")
            self.assertEqual(rc, 3)
            self.assertIn("integrity", err)
            self.assertIn("removed", self.run_cli(data, "models", "remove", "tiny")[1])
            self.assertEqual(json.loads(self.run_cli(data, "models", "list")[1])["installed"], [])
            rc, out, _ = self.run_cli(data, "backup")
            self.assertEqual(rc, 0)
            self.assertTrue(Path(out.strip()).exists())


if __name__ == "__main__":
    unittest.main()
