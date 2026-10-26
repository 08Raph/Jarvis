"""Adaptive personal memory: categories, provenance, expiry, secrets, interaction rules, verified procedures, profiles,
guest sessions, export/delete, honesty about retraining. Real SQLite, fake model engine (no real inference)."""
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import cli
from jarvis.assistant import LocalAssistant
from jarvis.config import Config, valid_profile
from jarvis.inference import gguf_engine as ge
from jarvis.memory import retrieval, secrets
from jarvis.memory.interaction import MemoryAgent
from jarvis.memory.personal import CATEGORIES, MemoryError_, PersonalMemory, ProcedureNotVerified
from jarvis.memory.store import SCHEMA_VERSION, MemoryStore
from jarvis.models.manager import ModelStore
from jarvis.personality import PersonalityStore
from test_local_model import FakeLlama, make_gguf


class Clock:
    def __init__(self):
        self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def fresh():
    clk = Clock()
    return PersonalMemory.guest(), clk


class StoreAndRowsTests(unittest.TestCase):
    def test_v1_database_is_upgraded_in_place_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "m.sqlite"
            con = sqlite3.connect(db)
            con.executescript("CREATE TABLE notes(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, created_at TEXT NOT NULL);"
                              "INSERT INTO notes(text,created_at) VALUES('old note','2025-01-01'); PRAGMA user_version=1;")
            con.commit()
            con.close()
            s = MemoryStore(db)
            self.assertEqual(s.notes()[0]["text"], "old note")
            self.assertEqual(s.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            PersonalMemory(s).add("approved_context", "likes tea")
            s.close()

    def test_every_category_works_and_rows_carry_provenance(self):
        m, _ = fresh()
        for c in CATEGORIES:
            r = m.add(c, f"item for {c}", source="user_said", confidence=0.8)
            self.assertEqual((r["category"], r["source"], r["confidence"], r["status"]), (c, "user_said", 0.8, "active"))
            self.assertTrue(r["created_at"] and r["updated_at"])
        with self.assertRaises(MemoryError_):
            m.add("diary", "x")
        with self.assertRaises(MemoryError_):
            m.add("project", "x", source="rumour")
        with self.assertRaises(MemoryError_):
            m.add("project", "x", confidence=1.5)
        with self.assertRaises(MemoryError_):
            m.add("project", "y" * 1001)

    def test_temporary_items_expire_and_are_purged(self):
        clk = Clock()
        m = PersonalMemory(MemoryStore(":memory:"), now=clk)
        r = m.add("temporary_task", "keep it short for now")
        self.assertIsNotNone(r["expires_at"])
        m.add("explicit_preference", "metric units")
        self.assertEqual(len(m.list()), 2)
        clk.t += timedelta(hours=25)
        self.assertEqual([x["category"] for x in m.list()], ["explicit_preference"])      # hidden as soon as it expires
        self.assertEqual(len(m.list(include_expired=True)), 2)
        self.assertEqual(m.purge_expired(), 1)
        self.assertEqual(len(m.list(include_expired=True)), 1)
        self.assertEqual(m.search("short")[0:0], [])

    def test_assistant_inferred_is_always_pending_until_approved(self):
        m, _ = fresh()
        r = m.add("explicit_preference", "likes short answers", source="assistant_inferred", status="active")
        self.assertEqual(r["status"], "pending")
        self.assertEqual(m.list(), [])
        self.assertEqual(m.search("short answers"), [])
        self.assertEqual(m.context_block("short answers"), "")
        a = m.approve(r["id"])
        self.assertEqual((a["status"], a["source"]), ("active", "user_confirmed"))
        self.assertIn("short answers", m.context_block(""))

    def test_same_key_supersedes_and_duplicates_collapse(self):
        m, _ = fresh()
        a = m.add("explicit_preference", "short answers", key="style.length")
        b = m.add("explicit_preference", "detailed answers", key="style.length")
        self.assertEqual([r["id"] for r in m.list()], [b["id"]])
        self.assertEqual(m.get(a["id"])["status"], "superseded")
        c = m.add("approved_context", "likes tea")
        d = m.add("approved_context", "Likes  TEA")
        self.assertEqual(c["id"], d["id"])

    def test_edit_delete_and_secure_delete(self):
        with tempfile.TemporaryDirectory() as d:
            m = PersonalMemory.open(Path(d) / "m.sqlite")
            r = m.add("approved_context", "my cat is called Zorbulon")
            self.assertEqual(m.update(r["id"], "my cat is called Mimi")["source"], "user_edit")
            self.assertTrue(m.delete(r["id"]))
            self.assertFalse(m.delete(r["id"]))
            self.assertEqual(m.db.execute("PRAGMA secure_delete").fetchone()[0], 1)
            m.close()
            self.assertNotIn(b"Zorbulon", (Path(d) / "m.sqlite").read_bytes())


class SecretsTests(unittest.TestCase):
    BAD = ["my password is hunter2", "api key: abcd1234efgh", "sk-abcdefghijklmnopqrstuvwxyz1234", "AKIAABCDEFGHIJKLMNOP",
           "ghp_abcdefghijklmnopqrstuvwxyz0123", "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4fw", "-----BEGIN RSA PRIVATE KEY-----",
           "card 4111 1111 1111 1111", "ssn 123-45-6789", "token = Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A",
           "the pin is 4821", "recovery code: 1234-5678", "x" * 5 + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7"]
    OK = ["my password manager is Bitwarden", "I prefer metric units", "the project uses Python 3.12.1", "call me Sam",
          "I like pin-up posters", "meeting at 10:30 about the quarterly-report-final-version", "version 2026.10.25 released"]

    def test_secrets_refused_and_never_stored(self):
        m, _ = fresh()
        for s in self.BAD:
            with self.assertRaises(secrets.SecretRejected, msg=s):
                m.add("approved_context", s)
        self.assertEqual(m.list(status=None), [])
        try:
            m.add("approved_context", "password is hunter2")
        except secrets.SecretRejected as e:
            self.assertNotIn("hunter2", str(e))
        self.assertEqual(m.db.execute("SELECT COUNT(*) FROM learning_log").fetchone()[0], 0)

    def test_normal_text_is_accepted(self):
        m, _ = fresh()
        for s in self.OK:
            m.add("approved_context", s)

    def test_secrets_blocked_in_every_write_path(self):
        m, _ = fresh()
        r = m.add("approved_context", "fine")
        for fn in (lambda: m.update(r["id"], "password is abc"), lambda: m.add("project", "x", project="sk-abcdefghijklmnopqrstuvwxyz"),
                   lambda: m.record_run("run", "unverified", steps=["login with password: abc"]),
                   lambda: m.save_skill("s", "use token = AKIAABCDEFGHIJKLMNOP")):
            with self.assertRaises(secrets.SecretRejected):
                fn()

    def test_summaries_are_redacted_not_dropped(self):
        m, _ = fresh()
        r = m.add("conversation_summary", "We set it up. The user's password is hunter2 and then we deployed.", source="assistant_summary",
                  redact_secrets=True)
        self.assertNotIn("hunter2", r["text"])
        self.assertIn("deployed", r["text"])


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.mem, _ = fresh()
        self.tmp = tempfile.TemporaryDirectory()
        self.pers = PersonalityStore(Path(self.tmp.name) / "p.json")
        self.a = MemoryAgent(self.mem, self.pers)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remember_saves_with_explanation_and_delete_hint(self):
        o = self.a.process("Remember that I prefer metric units")
        self.assertTrue(o.handled)
        self.assertEqual(self.mem.list("explicit_preference")[0]["text"], "I prefer metric units")
        for phrase in ("Saved to memory #1", "jarvis memory delete 1", "not retrained", "stays on this PC"):
            self.assertIn(phrase, o.reply)
        self.assertIn("Saved to memory number 1", o.spoken)
        self.assertNotIn("jarvis memory", o.spoken)

    def test_guest_notice_says_session_only(self):
        a = MemoryAgent(PersonalMemory.guest(), None, guest=True)
        self.assertIn("this session only", a.process("remember that I like tea").reply)

    def test_remember_a_secret_is_refused(self):
        o = self.a.process("remember my password is hunter2")
        self.assertEqual(o.kind, "secret_refused")
        self.assertEqual(self.mem.list(status=None), [])

    def test_temporary_vs_durable(self):
        t = self.a.process("remember for now that I am testing the printer")
        self.assertEqual(self.mem.list("temporary_task")[0]["id"], t.saved[0])
        self.assertEqual(self.mem.list("approved_context"), [])
        self.assertIn("24 hours", t.reply)
        p = self.a.process("remember that from now on I want replies in French")
        self.assertEqual(self.mem.list("explicit_preference")[0]["id"], p.saved[0])
        self.assertIsNone(self.mem.list("explicit_preference")[0]["expires_at"])

    def test_promote_temporary_with_remember_that(self):
        self.a.process("remember for now that I am testing the printer")
        o = self.a.process("remember that")
        self.assertEqual(o.kind, "promoted")
        r = self.mem.list("approved_context")
        self.assertEqual(r[0]["text"], "I am testing the printer")
        self.assertIsNone(r[0]["expires_at"])
        self.assertEqual(self.mem.list("temporary_task"), [])

    def test_style_feedback_is_temporary_then_proposed_not_applied(self):
        o1 = self.a.process("keep answers short")
        self.assertFalse(o1.handled)
        self.assertIn("for now only", o1.notice)
        self.assertEqual(self.mem.list("explicit_preference"), [])
        o2 = self.a.process("keep answers short")
        self.assertIn("NOT active", o2.notice)
        self.assertEqual(self.mem.list("explicit_preference"), [])                   # only a pending proposal
        self.assertEqual(len(self.mem.list(status="pending")), 1)
        o3 = self.a.process("yes, remember that")
        self.assertEqual(o3.kind, "promoted")
        self.assertEqual(self.mem.list("explicit_preference")[0]["text"], "Prefers short, concise answers.")

    def test_durable_style_saves_and_queues_personality_proposal_without_applying_it(self):
        before = dict(self.pers.profile.traits)
        o = self.a.process("from now on keep answers short")
        self.assertEqual(o.kind, "preference_saved")
        self.assertEqual(len(self.pers.pending), 1)
        self.assertEqual(self.pers.pending[0]["trait"], "verbosity")
        self.assertEqual(self.pers.profile.traits, before)                            # profile unchanged until approval
        self.assertIn("jarvis personality approve", o.reply)
        self.assertEqual(self.mem.learning_report()["counts"]["profile_adjustment"], 1)

    def test_normal_requests_are_not_hijacked(self):
        for t in ("write a short poem about autumn", "give me a short summary of this article", "make a list of formal greetings",
                  "what is the shorter route to Ottawa?", "be careful with the API", "explain how formal logic works",
                  "use Python for this", "no worries", "stop", "note the time is 5", "explain quantum computing in simple terms",
                  "don't forget the milk", "never gonna give you up", "say that again", "resume writing tips", "continue with my homework",
                  "I like pizza", "always use tabs in code", "what did you say"):
            o = self.a.process(t)
            self.assertEqual((o.handled, o.kind), (False, "pass"), t)
        self.assertEqual(self.mem.list(status=None), [])

    def test_dont_do_that_again_asks_what_instead_of_guessing(self):
        o = self.a.process("don't do that again")
        self.assertEqual(o.kind, "ask_avoid")
        self.assertEqual(self.mem.list(status=None), [])
        o2 = self.a.process("don't use bullet lists")
        self.assertEqual(o2.kind, "correction_saved")
        self.assertEqual(self.mem.list("correction")[0]["text"], "Avoid: don't use bullet lists")
        self.a.process("don't do that again")
        self.assertEqual(self.a.process("never mind").kind, "cancelled")

    def test_correction_is_temporary_unless_made_durable(self):
        o = self.a.process("no, my sister's name is Maria not Marie")
        self.assertEqual(o.kind, "correction_temp")
        self.assertEqual(len(self.mem.list("temporary_task")), 1)
        self.a.process("remember that")
        self.assertEqual(self.mem.list("correction")[0]["text"], "User correction: my sister's name is Maria not Marie")

    def test_usual_style_uses_saved_prefs_or_admits_none(self):
        self.assertEqual(self.a.process("write it in my usual style").kind, "no_style")
        self.a.process("remember that I prefer short answers")
        o = self.a.process("write it in my usual style")
        self.assertEqual(o.kind, "usual_style")
        self.assertIn("short answers", o.notice)

    def test_like_last_time_requires_evidence(self):
        o = self.a.process("do it like last time")
        self.assertEqual((o.handled, o.kind), (True, "no_evidence"))
        self.assertIn("don't have a record", o.reply)
        self.mem.add("procedure", "weekly report: collect numbers -> fill template -> email Dana", source="verified_outcome")
        o2 = self.a.process("make the weekly report like last time")
        self.assertEqual(o2.kind, "like_last_time")
        self.assertIn("email Dana", o2.extra_context)
        self.assertFalse(o2.handled)

    def test_recall_and_resume_say_so_when_nothing_is_saved(self):
        self.assertEqual(self.a.process("what did we discuss yesterday?").kind, "no_evidence")
        o = self.a.process("resume my website project")
        self.assertEqual(o.kind, "no_evidence")
        self.assertIn("nothing saved", o.reply)
        self.mem.add("project", "website: use Astro, deploy on Fridays", project="website")
        self.mem.add("conversation_summary", "We chose Astro and listed three pages.", project="website", source="assistant_summary")
        o2 = self.a.process("resume my website project")
        self.assertEqual(o2.kind, "project_recall")
        self.assertIn("Astro", o2.reply)
        self.assertIn("Latest summary", o2.reply)

    def test_forget_deletes_the_single_match_or_asks(self):
        self.mem.add("approved_context", "my cat is called Mimi")
        self.mem.add("approved_context", "my dog is called Rex")
        self.assertEqual(self.a.process("forget about the cat").kind, "forgot")
        self.assertEqual([r["text"] for r in self.mem.list()], ["my dog is called Rex"])
        self.assertEqual(self.a.process("forget my parrot").kind, "forget_none")

    def test_project_note_and_call_me(self):
        o = self.a.process("remember that for the Garden project we use raised beds")
        self.assertEqual(self.mem.list("project")[0]["project"], "Garden")
        self.assertIn("Garden", o.reply)
        self.a.process("call me Sam")
        self.assertEqual(self.mem.list("explicit_preference")[0]["text"], "Address the user as Sam.")

    def test_workflow_saved_only_after_user_confirms_it_worked(self):
        self.a.offer_workflow("fix wifi", ["restart router", "forget network", "reconnect"])
        self.assertEqual(self.mem.list("procedure"), [])
        o = self.a.process("that worked")
        self.assertEqual(o.kind, "procedure_saved")
        p = self.mem.list("procedure")[0]
        self.assertEqual((p["source"], p["meta"]["steps"][0]), ("verified_outcome", "restart router"))
        self.a.offer_workflow("bad idea", ["do thing"])
        self.assertEqual(self.a.process("that didn't work").kind, "run_failed")
        self.assertEqual(len(self.mem.list("procedure")), 1)
        stats = {s["name"]: s for s in self.mem.run_stats()}
        self.assertEqual(stats["bad idea"]["failure"], 1)
        self.assertEqual(stats["fix wifi"]["success"], 1)

    def test_that_worked_without_a_pending_workflow_does_nothing(self):
        o = self.a.process("that worked")
        self.assertEqual(o.kind, "pass")
        self.assertEqual(self.mem.list("procedure"), [])


class ProceduresSkillsLearningTests(unittest.TestCase):
    def setUp(self):
        self.m, _ = fresh()

    def test_unverified_or_mismatched_runs_cannot_become_procedures(self):
        r1 = self.m.record_run("deploy", "success")                     # nobody verified it
        self.assertEqual(self.m.db.execute("SELECT outcome FROM workflow_runs WHERE id=?", (r1,)).fetchone()[0], "unverified")
        with self.assertRaises(ProcedureNotVerified):
            self.m.save_procedure("deploy", ["a"], r1)
        r2 = self.m.record_run("deploy", "failure", "user_confirmed")
        with self.assertRaises(ProcedureNotVerified):
            self.m.save_procedure("deploy", ["a"], r2)
        r3 = self.m.record_run("other", "success", "tool_check")
        with self.assertRaises(ProcedureNotVerified):
            self.m.save_procedure("deploy", ["a"], r3)                  # run belongs to a different workflow
        with self.assertRaises(ProcedureNotVerified):
            self.m.save_procedure("deploy", ["a"], 9999)
        self.assertEqual(self.m.list("procedure"), [])

    def test_procedure_confidence_follows_verified_results(self):
        rid = self.m.record_run("deploy", "success", "tool_check")
        p = self.m.save_procedure("deploy", ["build", "upload"], rid)
        p = self.m.record_procedure_result(p["id"], False, "user_confirmed")
        self.assertEqual((p["meta"]["successes"], p["meta"]["failures"], p["confidence"]), (1, 1, 0.5))
        with self.assertRaises(ProcedureNotVerified):
            self.m.record_procedure_result(p["id"], True, "assistant")

    def test_skill_versions_and_assistant_proposed_improvement_needs_approval(self):
        s1 = self.m.save_skill("Weekly report", "Use headings: Done, Next, Risks.")
        s2 = self.m.improve_skill("Weekly report", "Headings: Done, Next, Risks, Asks.", "user edit")
        self.assertEqual((s1["meta"]["version"], s2["meta"]["version"]), (1, 2))
        self.assertEqual([r["id"] for r in self.m.list("skill")], [s2["id"]])
        s3 = self.m.improve_skill("Weekly report", "Add a TL;DR.", "seen it requested", by_user=False)
        self.assertEqual(s3["status"], "pending")
        self.assertEqual([r["id"] for r in self.m.list("skill")], [s2["id"]])
        self.m.approve(s3["id"])
        self.assertEqual([r["id"] for r in self.m.list("skill")], [s3["id"]])
        with self.assertRaises(MemoryError_):
            self.m.improve_skill("nonexistent", "x", "y")
        self.assertGreaterEqual(self.m.learning_report()["counts"]["skill_update"], 3)

    def test_model_retraining_is_never_claimed(self):
        with self.assertRaises(MemoryError_):
            self.m.log_learning("model_retraining", "fine-tuned on chats")
        self.m.add("approved_context", "x1")
        rep = self.m.learning_report()
        self.assertEqual(rep["counts"]["model_retraining"], 0)
        self.assertFalse(rep["model_weights_changed_by_conversations"])
        self.assertIn("does not retrain", rep["statement"])


class RetrievalTests(unittest.TestCase):
    def test_lexical_ranking_and_relevance_threshold(self):
        m, _ = fresh()
        m.add("project", "Garden: raised beds need cedar boards", project="garden")
        m.add("project", "Taxes: file the return before April", project="taxes")
        hits = m.search("cedar boards for the beds")
        self.assertEqual(hits[0]["project"], "garden")
        self.assertEqual(m.search("quantum chromodynamics"), [])
        self.assertTrue(retrieval.tokens("Running dogs jumped"))

    def test_optional_embedder_adds_semantic_matches_and_failures_are_harmless(self):
        table = {"car": [1, 0, 0], "automobile": [0.95, 0.05, 0], "banana": [0, 1, 0]}

        def emb(text):
            for w, v in table.items():
                if w in text.lower():
                    return v
            return [0, 0, 1]
        m = PersonalMemory(MemoryStore(":memory:"), embedder=emb)
        m.add("approved_context", "I drive an automobile to work")
        m.add("approved_context", "I eat a banana at lunch")
        self.assertEqual(m.search("my car")[0]["text"], "I drive an automobile to work")     # no shared word, found by vector
        bad = PersonalMemory(MemoryStore(":memory:"), embedder=lambda t: 1 / 0)
        bad.add("approved_context", "still saved")
        self.assertEqual(bad.search("saved")[0]["text"], "still saved")

    def test_context_block_is_labelled_and_bounded(self):
        m, _ = fresh()
        for i in range(30):
            m.add("explicit_preference", f"preference number {i} with some words")
        block = m.context_block("anything", max_chars=400)
        self.assertLessEqual(len(block), 400)
        self.assertIn("Never claim to remember anything that is not listed", block)
        self.assertEqual(PersonalMemory.guest().context_block("x"), "")


class ProfilesGuestAndExportTests(unittest.TestCase):
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

    def test_profile_names_are_safe_folder_names(self):
        for bad in ("", "../x", "a/b", "a.b", "con", "COM1", "profiles", "x" * 33, " ", "a b"):
            with self.assertRaises(ValueError, msg=bad):
                valid_profile(bad)
        self.assertEqual(valid_profile("Alice_2"), "alice_2")

    def test_default_layout_unchanged_and_profiles_are_separate(self):
        base = self.d / "data"
        self.assertEqual(Config(data_dir=base).memory_db, base / "memory.sqlite")
        self.assertEqual(Config(data_dir=base, profile="bob").memory_db, base / "profiles" / "bob" / "memory.sqlite")
        self.assertEqual(Config(data_dir=base, profile="bob").backups_dir, base / "profiles" / "bob" / "backups")

    def test_one_profile_never_sees_anothers_memory(self):
        self.assertEqual(self.cli("--profile", "alice", "memory", "add", "approved_context", "alice likes jazz")[0], 0)
        self.assertEqual(self.cli("memory", "add", "approved_context", "default user likes rock")[0], 0)
        _, a, _ = self.cli("--profile", "alice", "memory", "list")
        _, b, _ = self.cli("--profile", "bob", "memory", "list")
        _, dflt, _ = self.cli("memory", "list")
        self.assertIn("jazz", a)
        self.assertNotIn("rock", a)
        self.assertIn("Nothing saved", b)
        self.assertIn("rock", dflt)
        self.assertNotIn("jazz", dflt)
        self.assertEqual(self.cli("--profile", "../evil", "memory", "list")[0], 2)

    def test_where_reports_location_and_honest_encryption_note(self):
        rc, out, _ = self.cli("--profile", "alice", "memory", "where")
        info = json.loads(out)
        self.assertTrue(info["memory_database"].endswith("memory.sqlite"))
        self.assertIn("NOT encrypted", info["note"])
        self.assertIn("alice", info["memory_database"])

    def test_guest_session_reads_and_writes_nothing_on_disk(self):
        self.cli("memory", "add", "approved_context", "owner secret hobby is sailing")
        data = self.d / "data"
        ModelStore(data / "models").install_gguf(make_gguf(self.d / "m.gguf"), "fake", "Apache-2.0", "t")
        ModelStore(data / "models").set_active("fake")
        files_before = sorted(p.relative_to(self.d).as_posix() for p in self.d.rglob("*"))
        sizes_before = {p: p.stat().st_size for p in data.rglob("*") if p.is_file()}
        cfg = Config(data_dir=data)
        a = LocalAssistant(cfg, guest=True, engine_factory=lambda p: ge.GGUFEngine(p, llama_factory=FakeLlama))
        FakeLlama.instances.clear()
        self.assertIn("this session only", a.reply("remember that I like tea"))
        a.reply("what hobby do I have?")
        sent = a.engine.llm.calls[-1][0]["content"]
        self.assertNotIn("sailing", sent)                        # the owner's memory is invisible to a guest
        self.assertIn("like tea", sent)                          # but the guest's own session note is used
        for _ in range(3):
            a.reply("another message")
        self.assertIsNone(a.summarize_session())
        a.close()
        files_after = sorted(p.relative_to(self.d).as_posix() for p in self.d.rglob("*"))
        self.assertEqual(files_before, files_after)
        self.assertEqual(sizes_before, {p: p.stat().st_size for p in data.rglob("*") if p.is_file()})   # nothing was modified
        con = sqlite3.connect(data / "memory.sqlite")
        self.assertNotIn("tea", " ".join(r[0] for r in con.execute("SELECT text FROM memories")))
        con.close()

    def test_export_forget_all_and_backup_restore(self):
        self.cli("memory", "add", "explicit_preference", "short answers")
        out = self.d / "export.json"
        self.assertEqual(self.cli("memory", "export", "--out", str(out))[0], 0)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["format"], "project-jarvis-memory-export")
        self.assertEqual(doc["memories"][0]["text"], "short answers")
        self.assertIn("learning_log", doc)
        rc, bk, _ = self.cli("backup")
        self.assertEqual(rc, 0)
        self.assertEqual(self.cli("memory", "forget-all")[0], 2)                   # needs --yes
        self.assertEqual(self.cli("memory", "forget-all", "--yes")[0], 0)
        self.assertIn("Nothing saved", self.cli("memory", "list")[1])
        self.assertEqual(self.cli("restore", bk.strip())[0], 0)
        self.assertIn("short answers", self.cli("memory", "list")[1])

    def test_cli_pending_approve_reject_edit_delete(self):
        data = self.d / "data"
        data.mkdir()
        m = PersonalMemory.open(data / "memory.sqlite")
        a = m.add("explicit_preference", "likes brevity", source="assistant_inferred")
        b = m.add("explicit_preference", "likes puns", source="assistant_inferred")
        m.close()
        self.assertIn("likes brevity", self.cli("memory", "pending")[1])
        self.assertEqual(self.cli("memory", "approve", str(a["id"]))[0], 0)
        self.assertEqual(self.cli("memory", "reject", str(b["id"]))[0], 0)
        self.assertIn("Nothing is waiting", self.cli("memory", "pending")[1])
        self.assertEqual(self.cli("memory", "edit", str(a["id"]), "likes brief answers")[0], 0)
        self.assertIn("brief answers", self.cli("memory", "list")[1])
        self.assertEqual(self.cli("memory", "add", "approved_context", "my password is hunter2")[0], 2)
        self.assertEqual(self.cli("memory", "delete", str(a["id"]))[0], 0)
        self.assertEqual(self.cli("--guest", "memory", "list")[0], 2)

    def test_cli_procedure_flow_needs_verification(self):
        rc, out, _ = self.cli("memory", "procedure-start", "backup docs", "open folder", "copy files")
        self.assertIn("Registered run #1", out)
        self.assertIn("No verified procedures", self.cli("memory", "procedures")[1])
        self.assertEqual(self.cli("memory", "procedure-verify", "1", "--worked")[0], 0)
        self.assertIn("worked 1x", self.cli("memory", "procedures")[1])
        self.assertEqual(self.cli("memory", "procedure-verify", "1", "--worked")[0], 2)      # already resolved


class AssistantIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()
        s = ModelStore(self.cfg.models_dir)
        s.install_gguf(make_gguf(self.d / "m.gguf"), "fake", "Apache-2.0", "t")
        s.set_active("fake")
        FakeLlama.instances.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kw):
        return LocalAssistant(self.cfg, engine_factory=lambda p: ge.GGUFEngine(p, llama_factory=FakeLlama), **kw)

    def calls(self):
        return FakeLlama.instances[0].calls

    def test_memory_changes_the_prompt_not_the_model(self):
        a = self.make()
        a.reply("remember that I prefer metric units")
        self.assertEqual(self.calls(), [])                              # handled locally: the model was not even called
        a.reply("how far is 10 miles?")
        sys_prompt = self.calls()[-1][0]["content"]
        self.assertIn("I prefer metric units", sys_prompt)
        self.assertIn("never invent past conversations", sys_prompt)
        a.close()
        b = self.make()                                                  # persists across sessions on this profile
        b.reply("hello")
        self.assertIn("I prefer metric units", FakeLlama.instances[-1].calls[-1][0]["content"])
        b.close()

    def test_no_evidence_questions_never_reach_the_model(self):
        a = self.make()
        out = a.reply("do it like last time")
        self.assertIn("don't have a record", out)
        out2 = a.reply("what did we decide about the budget?")
        self.assertIn("don't have anything saved", out2)
        self.assertEqual(self.calls(), [])
        a.close()

    def test_stream_path_handles_memory_commands_too(self):
        a = self.make()
        self.assertIn("Saved to memory #1", "".join(a.reply_stream("remember that I like tea")))
        self.assertIn("Saved to memory #1", a.last_notice or a.last_outcome.reply)
        a.close()

    def test_summaries_are_opt_in_redacted_and_saved_as_visible_memory(self):
        from jarvis.assistant import set_summaries, summaries_enabled
        self.assertFalse(summaries_enabled(self.cfg))
        set_summaries(self.cfg, True)
        self.assertTrue(summaries_enabled(self.cfg))
        a = self.make()
        for t in ("hello", "plan my week", "add gym on Monday"):
            a.reply(t)
        a.engine.chat = lambda msgs, mt, temp: "Planned the week; the user's password is hunter2; gym Monday."
        row = a.summarize_session(project="week")
        self.assertEqual(row["category"], "conversation_summary")
        self.assertNotIn("hunter2", row["text"])
        self.assertEqual(row["source"], "assistant_summary")
        a.close()

    def test_named_profile_gets_its_own_folder_and_memory(self):
        bob = Config(data_dir=self.cfg.data_dir, profile="bob")
        a = LocalAssistant(bob, engine_factory=lambda p: ge.GGUFEngine(p, llama_factory=FakeLlama))
        a.reply("remember that bob likes chess")
        a.close()
        self.assertTrue((self.cfg.data_dir / "profiles" / "bob" / "memory.sqlite").is_file())
        b = self.make()
        self.assertEqual(b.memory.list(), [])
        b.close()

    def test_voice_gets_short_confirmation(self):
        a = self.make()
        rep = a.reply("remember that I like tea")
        self.assertNotIn("jarvis memory", a.spoken_version(rep))
        self.assertIn("Saved to memory number", a.spoken_version(rep))
        a.close()


class PrivacyStaticTests(unittest.TestCase):
    def test_memory_package_has_no_network_code(self):
        for f in (ROOT / "src" / "jarvis" / "memory").glob("*.py"):
            src = f.read_text(encoding="utf-8")
            for bad in ("import socket", "urllib", "import http", "requests", "jarvis.net", "jarvis.telemetry", "jarvis.online"):
                self.assertNotIn(bad, src, f"{f.name}: {bad}")

    def test_telemetry_schema_cannot_carry_memory_content(self):
        from jarvis.telemetry.schema import EVENTS
        fields = {f for _, (_, fs, _) in EVENTS.items() for f in fs}
        self.assertFalse({"text", "memory", "message", "preference", "summary", "content"} & fields)

    def test_online_service_client_does_not_use_personal_memory(self):
        for f in (ROOT / "src" / "jarvis" / "online").glob("*.py"):
            self.assertNotIn("jarvis.memory", f.read_text(encoding="utf-8"), f.name)


if __name__ == "__main__":
    unittest.main()
