"""Prompt-6 privacy / memory / integration audit.

WHAT IS REAL AND WHAT IS FAKE (read this before trusting a green run):
  REAL    : SQLite databases and files on disk, the HTTP-free online Service code, the CLI entry point, the memory agent,
            the prompt builder, the secret detector, the launcher crash log, the installer script text (static).
  FAKE    : the language model. `RuleFollowingFakeLlama` below is a deterministic stand-in that answers by simple rules
            from the prompt it is given. It proves that memory REACHES (or is withheld from) the prompt. It says nothing
            about how well the real Qwen3 model uses that prompt. No real model is loaded or downloaded here.
  NOT TESTED HERE: Windows, a microphone, DPAPI, PyInstaller, Inno Setup, real model inference, SSD erase behaviour.
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import cli, launcher
from jarvis.assistant import LocalAssistant
from jarvis.config import Config
from jarvis.firstrun import first_run
from jarvis.inference import gguf_engine as ge
from jarvis.memory import secrets
from jarvis.memory.personal import PersonalMemory
from jarvis.memory.store import MemoryStore
from jarvis.models.manager import ModelStore
from jarvis.online.flows import register_flow
from jarvis.storage import backup_sqlite, restore_sqlite
from jarvis.telemetry.schema import SchemaError, build_event
from jarvis_server.app import Service
from jarvis_server.db import DB
from jarvis_server.learning import export_training_data
from test_local_model import FakeLlama, make_gguf
from test_online import PW, FakeBackend


# ===================================================================== online learning consent (server, real code)
class ServerConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = DB(self.dir / "s.db")
        self.svc = Service(self.db, FakeBackend())

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def call(self, path, body=None, token=None, ip="1.1.1.1"):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        return self.svc.handle("POST", path, h, json.dumps(body or {}).encode(), ip)

    def reg(self, name="alice", ip="1.1.1.1", **kw):
        b = dict(username=name, password=PW, accepted_terms_sha256=self.svc.terms_sha, age_confirmed=True)
        b.update(kw)
        return self.call("/v1/register", b, ip=ip)

    def chat(self, t, text="hello", **kw):
        return self.call("/v1/chat", dict(messages=[{"role": "user", "content": text}], **kw), token=t)

    def opt_in(self, t):
        return self.call("/v1/settings/learning", dict(learning_opt_in=True, learning_consent_sha256=self.svc.learning_notice_sha), token=t)

    def export(self):
        return export_training_data(self.db, self.dir / "t.jsonl")

    def test_default_registration_never_marks_or_exports_anything(self):
        t = self.reg()[1]["token"]
        r = self.chat(t, "private words")[1]
        self.assertFalse(r["learning_applies"])
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM messages WHERE learn_ok=1")["c"], 0)
        self.assertEqual(self.export(), 0)
        self.assertFalse(self.call("/v1/me", token=t)[1]["learning_opt_in"])

    def test_a_wrong_or_missing_consent_fingerprint_is_refused_and_changes_nothing(self):
        t = self.reg()[1]["token"]
        for body in (dict(learning_opt_in=True), dict(learning_opt_in=True, learning_consent_sha256="f" * 64),
                     dict(learning_opt_in=True, learning_consent_sha256=self.svc.terms_sha)):   # the TERMS hash is not the notice hash
            s, r = self.call("/v1/settings/learning", body, token=t)
            self.assertEqual((s, r["error"]), (400, "learning_consent_required"))
        self.assertFalse(self.chat(t)[1]["learning_applies"])
        self.assertEqual(self.export(), 0)

    def test_only_messages_after_consent_are_exportable_and_withdrawal_stops_it(self):
        t = self.reg()[1]["token"]
        self.chat(t, "before consent")
        time.sleep(0.01)
        self.assertEqual(self.opt_in(t)[0], 200)
        self.assertTrue(self.chat(t, "after consent")[1]["learning_applies"])
        out = self.dir / "t.jsonl"
        self.assertEqual(export_training_data(self.db, out, mark_used=False), 2)       # the after-consent pair only
        text = out.read_text()
        self.assertIn("after consent", text)
        self.assertNotIn("before consent", text)
        self.call("/v1/settings/learning", dict(learning_opt_in=False), token=t)
        self.assertEqual(self.export(), 0)

    def test_legacy_default_on_database_is_paused_not_silently_changed(self):
        path = self.dir / "old.db"
        con = sqlite3.connect(path)
        con.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, recovery_hash TEXT NOT NULL,
          created_at REAL NOT NULL, terms_sha TEXT NOT NULL, terms_accepted_at REAL NOT NULL, age_confirmed INTEGER NOT NULL,
          learning_opt_in INTEGER NOT NULL DEFAULT 1, learning_changed_at REAL,
          failed_logins INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
          content TEXT NOT NULL, created_at REAL NOT NULL, learn_ok INTEGER NOT NULL, used_in_export INTEGER NOT NULL DEFAULT 0);
        PRAGMA user_version = 1;""")
        from jarvis_server import security as sec
        con.execute("INSERT INTO users(id,username,pw_hash,recovery_hash,created_at,terms_sha,terms_accepted_at,age_confirmed,learning_opt_in)"
                    " VALUES(1,'old',?,?,?,?,?,1,1)", (sec.hash_password(PW), sec.hash_password("x"), time.time() - 5000, self.svc.terms_sha, time.time() - 5000))
        con.execute("INSERT INTO messages VALUES(1,1,'c','user','legacy message',?,1,0)", (time.time() - 4000,))
        con.commit()
        con.close()
        db = DB(path)
        try:
            self.assertEqual(db.one("SELECT learning_opt_in FROM users")["learning_opt_in"], 1)           # stored value untouched
            self.assertEqual(db.one("SELECT learning_basis FROM users")["learning_basis"], "legacy_default")
            svc = Service(db, FakeBackend())
            tok = svc.handle("POST", "/v1/login", {}, json.dumps(dict(username="old", password=PW)).encode(), "9.9.9.9")[1]["token"]
            h = {"Authorization": f"Bearer {tok}"}
            me = svc.handle("POST", "/v1/me", h, b"{}", "9.9.9.9")[1]
            self.assertFalse(me["learning_opt_in"])                        # reported as not active ...
            self.assertTrue(me["learning_consent_needed"])                 # ... and the user is told why
            chat = svc.handle("POST", "/v1/chat", h, json.dumps(dict(messages=[{"role": "user", "content": "new"}])).encode(), "9.9.9.9")[1]
            self.assertFalse(chat["learning_applies"])
            self.assertEqual(export_training_data(db, self.dir / "o.jsonl"), 0)
            svc.handle("POST", "/v1/settings/learning",  h, json.dumps(dict(learning_opt_in=True, learning_consent_sha256=svc.learning_notice_sha)).encode(), "9.9.9.9")
            self.assertEqual(db.one("SELECT learning_basis FROM users")["learning_basis"], "explicit")
            self.assertEqual(export_training_data(db, self.dir / "o2.jsonl", mark_used=False), 0)         # legacy + pre-consent rows stay out
        finally:
            db.close()

    def test_changed_notice_text_makes_old_consent_stale(self):
        t = self.reg()[1]["token"]
        self.opt_in(t)
        self.assertTrue(self.chat(t)[1]["learning_applies"])
        self.svc.learning_notice_sha = "0" * 64                            # operator edits the notice
        self.assertFalse(self.chat(t)[1]["learning_applies"])
        me = self.call("/v1/me", token=t)[1]
        self.assertTrue(me["learning_consent_needed"])

    def test_delete_chats_removes_history_and_needs_confirmation(self):
        t = self.reg()[1]["token"]
        a = self.chat(t, "one")[1]["conversation_id"]
        self.chat(t, "two")
        self.assertEqual(self.call("/v1/messages/delete", {}, token=t)[0], 400)                 # nothing implied
        self.assertEqual(self.call("/v1/messages/delete", {"conversation_id": a}, token=t)[1]["deleted_messages"], 2)
        self.assertEqual(self.call("/v1/messages/delete", {"confirm": True}, token=t)[1]["deleted_messages"], 2)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM messages")["c"], 0)
        self.assertEqual(self.call("/v1/messages/delete", {"confirm": True})[0], 401)           # needs a session

    def test_deleting_chats_only_touches_the_callers_messages(self):
        a = self.reg("alice")[1]["token"]
        b = self.reg("bob", ip="2.2.2.2")[1]["token"]
        self.chat(a, "a1")
        self.chat(b, "b1")
        self.call("/v1/messages/delete", {"confirm": True}, token=a)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM messages")["c"], 2)                # bob's pair remains

    def test_terms_endpoint_offers_the_notice_and_says_default_off(self):
        s, r = self.svc.handle("GET", "/v1/terms", {}, b"", "1.1.1.1")
        self.assertEqual(r["learning_default"], "off")
        self.assertEqual(r["learning_notice_sha256"], self.svc.learning_notice_sha)
        self.assertIn("OFF unless you confirm", r["learning_notice"])


# ===================================================================== client flow (stub client, real flow code)
class RegisterFlowTests(unittest.TestCase):
    class StubClient:
        def __init__(self): self.sent = None
        def terms(self): return {"text": "terms", "sha256": "a" * 64, "learning_notice": "notice", "learning_notice_sha256": "b" * 64}
        def register(self, *a):
            self.sent = a
            return {"learning_opt_in": bool(a[4]), "recovery_code": "CODE"}

    def run_flow(self, consent_answer):
        c = self.StubClient()
        answers = iter(["I accept", "yes", consent_answer, "dave", PW, PW])
        out = []
        ok = register_flow(c, lambda p: next(answers), lambda p: next(answers), out.append)
        return ok, c.sent, "\n".join(out)

    def test_anything_other_than_the_exact_phrase_keeps_learning_off(self):
        for ans in ("", "no", "off", "yes", "i consent", "I CONSENT", "I consent please"):
            ok, sent, _ = self.run_flow(ans)
            self.assertTrue(ok)
            self.assertFalse(sent[4], ans)                  # learning flag
            self.assertIsNone(sent[5], ans)                 # no consent fingerprint was sent

    def test_exact_phrase_sends_the_fingerprint_of_the_notice_that_was_shown(self):
        ok, sent, text = self.run_flow("I consent")
        self.assertTrue(sent[4])
        self.assertEqual(sent[5], "b" * 64)
        self.assertIn("notice", text)

    def test_server_without_a_notice_can_never_turn_learning_on(self):
        c = self.StubClient()
        c.terms = lambda: {"text": "t", "sha256": "a" * 64}
        answers = iter(["I accept", "yes", "dave", PW, PW])
        self.assertTrue(register_flow(c, lambda p: next(answers), lambda p: next(answers), lambda s: None))
        self.assertFalse(c.sent[4])


# ===================================================================== a deterministic FAKE model
class RuleFollowingFakeLlama(FakeLlama):
    """FAKE. Reads only the system prompt. Rules: answers 'metric' if the prompt lists that preference; answers the cat's
    name only if the prompt contains it; otherwise says it has no record. Proves prompt plumbing, not model quality."""
    def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
        self.calls.append(messages)
        sys_p, user = messages[0]["content"], messages[-1]["content"].lower()
        if "cat" in user and "called" in user:
            text = "Your cat is called Tomato." if "Tomato" in sys_p else "I have no record of that."
        elif "units" in user:
            text = "Metric." if "metric" in sys_p.lower() else "I don't know your preference."
        elif "no record" in sys_p and "NO saved memory matches" in sys_p:
            text = "I have no record of that earlier conversation."
        else:
            text = "OK."
        if not stream:
            return {"choices": [{"message": {"content": text}}]}
        return iter({"choices": [{"delta": {"content": text}}]} for _ in [0])


class PromptIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()
        s = ModelStore(self.cfg.models_dir)
        s.install_gguf(make_gguf(self.d / "m.gguf"), "fake", "Apache-2.0", "t")
        s.set_active("fake")
        FakeLlama.instances.clear()
        RuleFollowingFakeLlama.instances = FakeLlama.instances

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kw):
        return LocalAssistant(self.cfg, engine_factory=lambda p: ge.GGUFEngine(p, llama_factory=RuleFollowingFakeLlama), **kw)

    def last_prompt(self):
        return FakeLlama.instances[-1].calls[-1][0]["content"]

    def test_approved_relevant_memory_reaches_the_prompt_and_irrelevant_memory_does_not(self):
        a = self.make()
        a.memory.add("approved_context", "My cat is called Tomato", source="user_said")
        a.memory.add("approved_context", "The garage code reminder is blue door", source="user_said")
        a.memory.add("project", "Apollo project uses PostgreSQL", source="user_said", project="apollo")
        self.assertIn("Tomato", a.reply("what is my cat called?"))
        p = self.last_prompt()
        self.assertIn("Tomato", p)
        self.assertNotIn("PostgreSQL", p)
        self.assertNotIn("blue door", p)
        a.reply("tell me a fun fact about volcanoes")
        for irrelevant in ("Tomato", "PostgreSQL", "blue door"):
            self.assertNotIn(irrelevant, self.last_prompt())
        a.close()

    def test_without_the_memory_the_fake_model_has_nothing_to_use(self):
        a = self.make()
        self.assertIn("no record", a.reply("what is my cat called?"))
        a.close()

    def test_pending_suggestions_never_reach_the_prompt_until_approved(self):
        a = self.make()
        row = a.memory.add("approved_context", "My cat is called Tomato", source="assistant_inferred")
        self.assertEqual(row["status"], "pending")
        a.reply("what is my cat called?")
        self.assertNotIn("Tomato", self.last_prompt())
        a.memory.approve(row["id"])
        self.assertIn("Tomato", a.reply("what is my cat called?"))
        a.close()

    def test_rejected_deleted_superseded_and_expired_memories_never_reach_the_prompt(self):
        a = self.make()
        gone = a.memory.add("approved_context", "My cat is called Tomato", source="user_said")
        a.memory.delete(gone["id"])
        a.reply("what is my cat called?")
        self.assertNotIn("Tomato", self.last_prompt())
        old = a.memory.add("explicit_preference", "I use imperial units", key="style.units", source="user_said")
        a.memory.add("explicit_preference", "I use metric units", key="style.units", source="user_said")
        a.reply("which units do I like?")
        p = self.last_prompt()
        self.assertIn("metric", p)
        self.assertNotIn("imperial", p)                                    # superseded
        self.assertEqual(a.memory.get(old["id"])["status"], "superseded")
        past = a.memory._now() - __import__("datetime").timedelta(hours=1)
        a.memory.add("temporary_task", "Answer only in French for now", expires_at=past)
        a.reply("hi")
        self.assertNotIn("French", self.last_prompt())
        a.close()

    def test_reference_to_an_earlier_conversation_without_a_record_is_flagged_for_honesty(self):
        a = self.make()
        out = a.reply("As I told you yesterday, my sister is called Ann. What is her name?")
        self.assertIn("no record", out.lower())
        p = self.last_prompt()
        self.assertIn("NO saved memory matches", p)
        a.close()

    def test_reference_to_an_earlier_conversation_with_a_record_offers_only_that_record(self):
        a = self.make()
        a.memory.add("approved_context", "Sister is called Ann", source="user_said")
        a.reply("As I told you last week, my sister is called Ann. What is her name?")
        p = self.last_prompt()
        self.assertNotIn("NO saved memory matches", p)
        self.assertIn("Sister is called Ann", p)
        a.close()

    def test_recall_questions_without_evidence_never_call_the_model(self):
        a = self.make()
        for q in ("what did we discuss about the budget?", "do it like last time", "resume the apollo project"):
            n = len(FakeLlama.instances[0].calls) if FakeLlama.instances else 0
            out = a.reply(q)
            self.assertTrue("don't" in out or "nothing" in out.lower(), q)
            self.assertEqual(len(FakeLlama.instances[0].calls), n, q)
        a.close()

    def test_retraining_counter_stays_zero_after_lots_of_memory_activity(self):
        a = self.make()
        a.reply("remember that I prefer metric units")
        a.reply("from now on keep answers short")
        a.reply("what did we discuss?")
        rep = a.memory.learning_report()
        self.assertEqual(rep["counts"]["model_retraining"], 0)
        self.assertFalse(rep["model_weights_changed_by_conversations"])
        from jarvis.memory.personal import MemoryError_
        with self.assertRaises(MemoryError_):
            a.memory.log_learning("model_retraining", "pretend")
        a.close()

    def test_secret_in_a_message_is_not_saved_and_not_echoed(self):
        a = self.make()
        out = a.reply("remember that my password is hunter2xyzSecret")
        self.assertNotIn("hunter2xyzSecret", out)
        self.assertEqual([r for r in a.memory.list(status=None) if "hunter2" in r["text"]], [])
        self.assertNotIn("hunter2xyzSecret", json.dumps(a.memory.export(), default=str))
        a.close()


# ===================================================================== memory deletion & files on disk
class MemoryDeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.db = self.d / "memory.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def blobs(self):
        return {p.name: p.read_bytes() for p in self.d.iterdir() if p.is_file()}

    def test_deleting_a_memory_removes_its_older_versions_and_its_vector(self):
        m = PersonalMemory.open(self.db, embedder=lambda t: [float(len(t)), 1.0, 0.0])
        m.add("explicit_preference", "Prefers imperial units", key="style.units", source="user_said")
        new = m.add("explicit_preference", "Prefers metric units", key="style.units", source="user_said")
        self.assertEqual(len(m.list(status=None)), 2)
        self.assertTrue(m.delete(new["id"]))
        self.assertEqual(m.list(status=None), [])                                # no hidden "previous preference" left
        self.assertEqual(m.db.execute("SELECT COUNT(*) c FROM memory_vectors").fetchone()["c"], 0)
        self.assertEqual(m.search("units"), [])
        self.assertEqual(m.context_block("units"), "")
        m.close()

    def test_deleting_one_memory_keeps_unrelated_ones(self):
        m = PersonalMemory.open(self.db)
        a = m.add("approved_context", "keep me alpha", source="user_said")
        b = m.add("approved_context", "remove me beta", source="user_said")
        m.delete(b["id"])
        self.assertEqual([r["id"] for r in m.list()], [a["id"]])
        self.assertFalse(m.delete(b["id"]))                                      # already gone
        m.close()

    def test_database_uses_a_rollback_journal_not_wal_and_leaves_no_side_files(self):
        m = PersonalMemory.open(self.db)
        m.add("approved_context", "journal check", source="user_said")
        self.assertEqual(m.db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(m.db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        names = {p.name for p in self.d.iterdir()}
        self.assertEqual(names, {"memory.sqlite"})                               # no -wal, -shm or leftover -journal
        m.close()

    def test_deleted_text_is_gone_from_the_main_file_and_from_every_sibling_file(self):
        """Inspects raw bytes. This is evidence for THIS filesystem only, not a guarantee for SSDs / journaling filesystems."""
        marker = "zxqv-marker-unique-7731"
        m = PersonalMemory.open(self.db)
        row = m.add("approved_context", f"temporary fact {marker}", source="user_said")
        m.log_learning("memory_update", "x", None)
        self.assertTrue(any(marker.encode() in b for b in self.blobs().values()))      # sanity: it WAS on disk
        m.delete(row["id"])
        m.close()
        leaks = [n for n, b in self.blobs().items() if marker.encode() in b]
        self.assertEqual(leaks, [])

    def test_forget_all_clears_the_live_file_but_old_backups_keep_the_data_until_removed(self):
        marker = "qqpz-marker-unique-4410"
        m = PersonalMemory.open(self.db)
        m.add("approved_context", f"secret-ish note {marker}", source="user_said")
        m.close()
        bdir = self.d / "backups"
        bk = backup_sqlite(self.db, bdir)
        m = PersonalMemory.open(self.db)
        m.forget_everything()
        m.close()
        self.assertNotIn(marker.encode(), self.db.read_bytes())
        self.assertIn(marker.encode(), bk.read_bytes())                          # the documented limitation, proven
        ms = MemoryStore(self.db)
        try:
            self.assertEqual(list(ms.export_all().get("messages", [])), [])
        finally:
            ms.close()

    def test_cli_forget_all_include_backups_removes_them(self):
        def run(*a):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["--data-dir", str(self.d / "data"), *a])
            return rc, out.getvalue() + err.getvalue()
        run("memory", "add", "approved_context", "note for backup test")
        run("backup")
        cfg = Config(data_dir=self.d / "data")
        self.assertEqual(len(list(cfg.backups_dir.glob("*.sqlite"))), 1)
        rc, text = run("memory", "forget-all", "--yes")
        self.assertEqual(rc, 0)
        self.assertIn("--include-backups", text)                                  # told that backups remain
        self.assertEqual(len(list(cfg.backups_dir.glob("*.sqlite"))), 1)
        rc, text = run("memory", "forget-all", "--yes", "--include-backups")
        self.assertEqual(len(list(cfg.backups_dir.glob("*.sqlite"))), 0)
        self.assertIn("not guaranteed", text)


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.d / "data"), *args])
        return rc, out.getvalue(), err.getvalue()

    def test_backup_is_per_profile_and_contains_only_the_memory_database(self):
        self.cli("--profile", "alice", "memory", "add", "approved_context", "alice only")
        self.cli("memory", "add", "approved_context", "default only")
        cfg_a = Config(data_dir=self.d / "data", profile="alice")
        (cfg_a.profile_dir / "personality.json").write_text("{}")
        self.assertEqual(self.cli("--profile", "alice", "backup")[0], 0)
        files = list(cfg_a.backups_dir.iterdir())
        self.assertEqual([f.suffix for f in files], [".sqlite"])                  # personality/voice settings are NOT included
        con = sqlite3.connect(files[0])
        texts = [r[0] for r in con.execute("SELECT text FROM memories")]
        con.close()
        self.assertEqual(texts, ["alice only"])
        self.assertFalse((Config(data_dir=self.d / "data").backups_dir).exists() and list(Config(data_dir=self.d / "data").backups_dir.glob("*.sqlite")))

    def test_restore_brings_back_deleted_memory_and_keeps_a_safety_copy(self):
        self.cli("memory", "add", "approved_context", "restore me")
        cfg = Config(data_dir=self.d / "data")
        self.cli("backup")
        bk = next(cfg.backups_dir.glob("*.sqlite"))
        self.cli("memory", "forget-all", "--yes")
        self.assertIn("Nothing saved", self.cli("memory", "list")[1])
        self.assertEqual(self.cli("restore", str(bk))[0], 0)
        self.assertIn("restore me", self.cli("memory", "list")[1])
        self.assertGreaterEqual(len(list(cfg.backups_dir.glob("*.sqlite"))), 2)    # safety copy of the pre-restore file

    def test_restore_never_applies_a_stale_journal_to_the_restored_file(self):
        db = self.d / "m.sqlite"
        MemoryStore(db).close()
        bk = backup_sqlite(db, self.d / "b")
        (self.d / "m.sqlite-journal").write_bytes(b"stale junk")
        (self.d / "m.sqlite-wal").write_bytes(b"stale junk")
        restore_sqlite(bk, db)
        self.assertFalse((self.d / "m.sqlite-journal").exists())
        self.assertFalse((self.d / "m.sqlite-wal").exists())
        MemoryStore(db).close()

    def test_restore_rejects_corrupt_and_newer_schema_backups_and_leaves_the_live_file_alone(self):
        db = self.d / "m.sqlite"
        m = PersonalMemory.open(db)
        m.add("approved_context", "live data", source="user_said")
        m.close()
        before = db.read_bytes()
        bad = self.d / "bad.sqlite"
        bad.write_bytes(b"this is not a database at all" * 20)
        with self.assertRaises(Exception):
            restore_sqlite(bad, db)
        newer = self.d / "newer.sqlite"
        con = sqlite3.connect(newer)
        con.execute("CREATE TABLE t(x)")
        con.execute("PRAGMA user_version=99")
        con.commit()
        con.close()
        with self.assertRaises(RuntimeError):
            restore_sqlite(newer, db)
        self.assertEqual(db.read_bytes(), before)

    def test_guest_cannot_back_up_or_restore_the_owners_memory(self):
        self.cli("memory", "add", "approved_context", "owner data")
        for cmd in (("backup",), ("restore", "whatever.sqlite")):
            rc, _, err = self.cli("--guest", *cmd)
            self.assertEqual(rc, 2, cmd)
            self.assertIn("Guest", err)
        self.assertFalse(list(Config(data_dir=self.d / "data").backups_dir.glob("*.sqlite")))


# ===================================================================== guest, profiles, online, secrets, logs
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.d / "data"), *args])
        return rc, out.getvalue(), err.getvalue()

    def test_guest_mode_blocks_the_online_service_and_creates_no_online_files(self):
        for sub in (["status"], ["connect", "https://example.invalid"], ["register"], ["chat", "hi"], ["learning", "on"]):
            rc, _, err = self.cli("--guest", "online", *sub)
            self.assertEqual(rc, 2, sub)
            self.assertIn("Guest mode", err)
        data = self.d / "data"
        self.assertFalse((data / "online.json").exists())
        self.assertFalse((data / "online.token").exists())

    def test_guest_session_reads_nothing_from_and_writes_nothing_to_the_owners_memory(self):
        self.cli("memory", "add", "approved_context", "owner secret-ish fact zebra")
        cfg = Config(data_dir=self.d / "data")
        before = cfg.memory_db.read_bytes()
        g = PersonalMemory.guest()
        self.assertEqual(g.list(status=None), [])
        self.assertEqual(g.search("zebra"), [])
        g.add("approved_context", "guest-only fact", source="user_said")
        self.assertEqual(cfg.memory_db.read_bytes(), before)
        rc, out, _ = self.cli("--guest", "memory", "list")
        self.assertNotIn("zebra", out)

    def test_invalid_profiles_are_rejected_everywhere_before_any_file_is_made(self):
        for bad in ("../x", "a/b", "con", "profiles", "x" * 40, "a b", ""):
            rc, _, err = self.cli("--profile", bad, "memory", "list")
            self.assertEqual(rc, 2, repr(bad))
        self.assertFalse((self.d / "data" / "profiles").exists())
        os.environ["JARVIS_PROFILE"] = "../evil"
        try:
            self.assertEqual(self.cli("memory", "list")[0], 2)
        finally:
            del os.environ["JARVIS_PROFILE"]
        self.assertFalse((self.d.parent / "evil").exists())

    def test_secret_detector_refuses_common_shapes_without_echoing_and_admits_it_is_best_effort(self):
        samples = ["my password is correcthorse9", "key sk-" + "a1B2" * 8, "token ghp_" + "A1b2C3d4" * 4,
                   "card 4111 1111 1111 1111", "-----BEGIN RSA PRIVATE KEY-----"]
        for s in samples:
            with self.assertRaises(secrets.SecretRejected) as cm:
                secrets.check(s)
            msg = str(cm.exception)
            for frag in ("correcthorse9", "4111", "sk-", "ghp_", "BEGIN"):
                self.assertNotIn(frag, msg)
            self.assertIn("best effort", msg)
        # Known limit, documented: ordinary words that happen to be secret are NOT caught.
        self.assertIsNone(secrets.find_secret("the vault opens with swordfish"))

    def test_redact_removes_cards_and_tokens_for_summaries_and_logs(self):
        out = secrets.redact("pay with 4111 1111 1111 1111 and sk-" + "a1B2" * 8 + " ok")
        self.assertNotIn("4111", out)
        self.assertNotIn("sk-a1B2", out)
        self.assertIn("ok", out)

    def test_telemetry_events_refuse_secret_looking_values_and_unknown_fields(self):
        iid = "a" * 32
        with self.assertRaises(SchemaError):
            build_event("selftest_result", iid, {"model": "sk-" + "a1B2" * 8, "passed": True})
        with self.assertRaises(SchemaError):
            build_event("app_start", iid, {"chat_text": "hello"})
        with self.assertRaises(SchemaError):
            build_event("error", iid, {"component": "chat", "code": "unknown", "message": "my password is x"})

    def test_crash_log_and_console_message_hide_secrets(self):
        secret = "hunter2xyz"
        try:
            raise RuntimeError(f"login failed, password is {secret} and card 4111 1111 1111 1111")
        except RuntimeError as e:
            path = launcher.write_crash_log(e, self.d)
        text = path.read_text()
        self.assertNotIn(secret, text)
        self.assertNotIn("4111", text)
        out = io.StringIO()
        def boom(argv): raise RuntimeError(f"bad token ghp_{'A1b2C3d4' * 4}")
        with contextlib.redirect_stdout(out):
            rc = launcher.run(boom, ["x"], data_dir=self.d, ours=lambda: False)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("ghp_A1b2", out.getvalue())

    def test_first_run_on_an_existing_install_keeps_consent_settings_and_memories(self):
        cfg = Config(data_dir=self.d / "data")
        first_run(cfg)
        from jarvis.net.policy import NetworkPolicy
        pol = NetworkPolicy.load(cfg.settings_path)
        pol.enable("telemetry")
        pol.save(cfg.settings_path)
        self.cli("memory", "add", "approved_context", "survives upgrade")
        (cfg.data_dir / "online.json").write_text('{"url": "https://x.example"}')
        first_run(cfg)                                                            # what a post-upgrade run does
        self.assertTrue(NetworkPolicy.load(cfg.settings_path).is_enabled("telemetry"))
        self.assertIn("survives upgrade", self.cli("memory", "list")[1])
        self.assertTrue((cfg.data_dir / "online.json").exists())

    def test_installer_never_deletes_user_data_silently(self):
        iss = (ROOT / "packaging" / "installer.iss").read_text()
        low = iss.lower()
        self.assertNotIn("[installdelete]", low)
        self.assertNotIn("[uninstalldelete]", low)
        i = iss.index("DelTree(DataDir()")
        guard = iss[max(0, i - 500):i]
        self.assertIn("not UninstallSilent", guard)
        self.assertIn("MB_DEFBUTTON2", guard)                                     # the default button is No
        self.assertIn("'backup'", iss)                                            # upgrade makes a safety backup first

    def test_nothing_in_the_online_client_sends_local_memory(self):
        import re
        for f in (ROOT / "src" / "jarvis" / "online").glob("*.py"):
            self.assertNotRegex(f.read_text(), r"jarvis\.memory|PersonalMemory|memory_db", f.name)


if __name__ == "__main__":
    unittest.main()
