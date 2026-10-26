import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis.config import load_config
from jarvis.data.provenance import LicenseError, ProvenanceLedger, redact_pii, scan_pii
from jarvis.inference.engine import InferenceEngine, ModelNotAvailable, model_status
from jarvis.memory.store import MemoryStore
from jarvis.tokenizer.bpe import BPETokenizer
from jarvis.tools.registry import build_default_registry, safe_eval

CORPUS = ("the quick brown fox jumps over the lazy dog. " * 40
          + "Jarvis helps with notes, timers and questions. " * 40 + "naïve café 你好 😀\n" * 5)


class TokenizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tok = BPETokenizer.train(CORPUS, 400)

    def test_roundtrip_ascii_and_unicode(self):
        for s in ["the quick brown fox", "naïve café 你好 😀", "", "  spaces   and\ttabs\n", "unseen zzyzx text"]:
            self.assertEqual(self.tok.decode(self.tok.encode(s)), s)

    def test_compresses(self):
        s = "the quick brown fox jumps over the lazy dog."
        self.assertLess(len(self.tok.encode(s)), len(s.encode()))

    def test_special_tokens_and_size(self):
        ids = self.tok.encode("hi", bos=True, eos=True)
        self.assertEqual(ids[0], self.tok.bos_id)
        self.assertEqual(ids[-1], self.tok.eos_id)
        self.assertEqual(self.tok.decode(ids), "hi")
        self.assertLessEqual(self.tok.vocab_size, 400)
        self.assertTrue(all(0 <= i < self.tok.vocab_size for i in ids))

    def test_plain_text_never_yields_special(self):
        ids = self.tok.encode("<bos><eos><pad>")
        self.assertFalse(set(ids) & set(self.tok.special.values()))

    def test_deterministic_and_save_load(self):
        again = BPETokenizer.train(CORPUS, 400)
        self.assertEqual(again.fingerprint, self.tok.fingerprint)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            self.tok.save(p)
            loaded = BPETokenizer.load(p)
        self.assertEqual(loaded.encode("the lazy dog"), self.tok.encode("the lazy dog"))

    def test_vocab_floor(self):
        with self.assertRaises(ValueError):
            BPETokenizer.train("abc", 10)


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = ProvenanceLedger(self.dir / "l.sqlite")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def _file(self, name, text):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_rejects_unlicensed(self):
        p = self._file("a.txt", "hello")
        with self.assertRaises(LicenseError):
            self.ledger.register(p, "a", "me", "All-Rights-Reserved")

    def test_approve_and_dedupe(self):
        p = self._file("a.txt", "hello world")
        i1 = self.ledger.register(p, "a", "me", "CC0-1.0")
        i2 = self.ledger.register(p, "a-again", "me", "CC0-1.0")
        self.assertEqual(i1, i2)
        self.assertEqual(len(self.ledger.approved()), 1)

    def test_pii_quarantine_then_removal(self):
        p = self._file("b.txt", "contact bob@example.com for details")
        sid = self.ledger.register(p, "b", "me", "MIT")
        self.assertEqual(self.ledger.approved(), [])
        self.ledger.approve_quarantined(sid)
        self.assertEqual(len(self.ledger.approved()), 1)
        self.ledger.mark_removed(sid, "takedown request")
        self.assertEqual(self.ledger.approved(), [])
        out = self.dir / "m.json"
        self.ledger.export_manifest(out)
        self.assertEqual(json.loads(out.read_text())[0]["status"], "removed")

    def test_pii_helpers(self):
        self.assertIn("email", scan_pii("a@b.com"))
        self.assertNotIn("a@b.com", redact_pii("mail a@b.com now"))
        self.assertEqual(scan_pii("plain harmless sentence"), {})


class MemoryTests(unittest.TestCase):
    def test_conversation_notes_export_delete(self):
        m = MemoryStore()
        c = m.new_conversation("t")
        m.add_message(c, "user", "hi")
        m.add_message(c, "assistant", "hello")
        self.assertEqual([x["role"] for x in m.messages(c)], ["user", "assistant"])
        nid = m.add_note("buy milk")
        self.assertTrue(m.delete_note(nid))
        self.assertFalse(m.delete_note(nid))
        m.add_note("keep")
        self.assertEqual(len(m.export_all()["notes"]), 1)
        m.delete_conversation(c)
        self.assertEqual(m.messages(c), [])
        m.delete_all()
        self.assertEqual(m.export_all()["notes"], [])

    def test_bad_role_rejected(self):
        m = MemoryStore()
        c = m.new_conversation()
        with self.assertRaises(Exception):
            m.add_message(c, "hacker", "x")

    def test_persists_to_disk(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.sqlite"
            a = MemoryStore(p)
            a.add_note("persist me")
            a.close()
            b = MemoryStore(p)
            self.assertEqual(b.notes()[0]["text"], "persist me")
            b.close()


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryStore()

    def test_calculator(self):
        self.assertEqual(safe_eval("2+3*4"), 14)
        self.assertEqual(safe_eval("-(2**3)"), -8)
        for bad in ["__import__('os')", "2**1000", "abs(3)", "1;2", "'a'*5"]:
            with self.assertRaises(Exception):
                safe_eval(bad)

    def test_run_logs_truthfully(self):
        reg = build_default_registry(self.mem, platform="linux")
        r = reg.run("calculate", {"expression": "6*7"})
        self.assertTrue(r.ok)
        self.assertEqual(r.output, "42")
        bad = reg.run("calculate", {"expression": "import os"})
        self.assertFalse(bad.ok)
        self.assertEqual(bad.status, "error")
        self.assertEqual([a["status"] for a in self.mem.actions()], ["ok", "error"])

    def test_unknown_tool(self):
        reg = build_default_registry(self.mem)
        r = reg.run("format_c_drive")
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "unknown_tool")

    def test_open_app_requires_windows_and_confirmation(self):
        linux = build_default_registry(self.mem, {"notepad": "notepad.exe"}, platform="linux")
        self.assertEqual(linux.run("open_app", {"name": "notepad"}, confirm=lambda t, a: True).status, "unsupported")
        win = build_default_registry(self.mem, {"notepad": "notepad.exe"}, platform="win32")
        self.assertEqual(win.run("open_app", {"name": "notepad"}).status, "declined")
        self.assertEqual(win.run("open_app", {"name": "notepad"}, confirm=lambda t, a: False).status, "declined")

    def test_open_app_rejects_non_allowlisted(self):
        win = build_default_registry(self.mem, {"notepad": "notepad.exe"}, platform="win32")
        r = win.run("open_app", {"name": "powershell"}, confirm=lambda t, a: True)
        self.assertEqual(r.status, "error")

    def test_notes_tools(self):
        reg = build_default_registry(self.mem)
        self.assertEqual(reg.run("list_notes").output, "(no notes)")
        self.assertTrue(reg.run("save_note", {"text": "x"}).ok)
        self.assertFalse(reg.run("save_note", {"text": "  "}).ok)
        self.assertIn("x", reg.run("list_notes").output)


class ConfigAndEngineTests(unittest.TestCase):
    def test_defaults_and_toml(self):
        self.assertEqual(load_config(None).top_k, 40)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text('[paths]\ndata_dir="x"\n[generation]\ntemperature=0.5\n[apps]\nNotepad="notepad.exe"\n')
            c = load_config(p)
        self.assertEqual(c.temperature, 0.5)
        self.assertEqual(c.app_allowlist, {"notepad": "notepad.exe"})
        self.assertEqual(c.model_dir, Path("x/model"))

    def test_invalid_config_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text("[generation]\ntemperature=9\n")
            with self.assertRaises(ValueError):
                load_config(p)

    def test_engine_refuses_without_model(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(model_status(d)["trained_model_present"])
            with self.assertRaises(ModelNotAvailable):
                InferenceEngine(d)


if __name__ == "__main__":
    unittest.main()
