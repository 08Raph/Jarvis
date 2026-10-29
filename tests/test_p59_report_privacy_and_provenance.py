"""Final Stage 7 readiness review: demonstrable defects in report privacy (path scrubbing) and provenance (a 'real' label without a recorded runtime).
Real application code; scripted fake model; no real inference."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402
from jarvis.assistant import LocalAssistant  # noqa: E402
from jarvis.personality import audit, evalsuite  # noqa: E402


class Scrub(unittest.TestCase):
    CASES = [("C:/Users/alice/m.gguf", "alice"), ("C:\\Users\\Al Ice\\a b.dll", "Ice"), ("error in 'C:\\Users\\Al Ice\\a b.dll'", "Al"), ("\\\\?\\C:\\Users\\alice\\x", "alice"),
             ("/srv/alice/x", "alice"), ("/media/alice/usb/m.gguf", "alice"), ("/data/alice/models/m.gguf", "alice"), ("~/alice-models/m.gguf", "alice"),
             ("%USERPROFILE%\\alice\\m", "alice"), ("$HOME/alice/m", "alice"), ("/run/user/1000/alice/x", "alice"), ("\\\\fileserver\\alice\\m", "alice"),
             ("/Users/alice/Library/x", "alice"), ("/home/alice/x", "alice"), ("D:\\Games\\alice\\m.gguf", "alice"), ("file:///C:/Users/alice/x", "alice")]

    def test_no_user_name_fragment_survives(self):
        for text, name in self.CASES:
            with self.subTest(text=text):
                out = evalsuite.scrub(text)
                self.assertNotIn(name, out)
                self.assertIn("<path>", out)
                self.assertNotIn(name, evalsuite.safe_error(RuntimeError(text)))

    def test_ordinary_text_and_urls_are_left_alone(self):
        for t in ("see https://huggingface.co/a/b", "and/or n/a 3/4", "time 12:30/ratio 3:4", "word:/x", "Loaded the model in 3 s"):
            self.assertEqual(evalsuite.scrub(t), t)

    def test_nested_structures_are_scrubbed(self):
        out = evalsuite.scrub({"a": ["/home/alice/x", {"b": "C:/Users/alice/y"}], "n": 3, "t": ("~/alice/z",)})
        self.assertNotIn("alice", json.dumps(out))
        self.assertEqual(out["n"], 3)


class Provenance(AppCase):
    def make(self):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = "Twelve. Plain scripted reply."
        a = LocalAssistant(self.cfg, engine_factory=fake_engine_factory, guest=True)
        self.addCleanup(a.close)
        return a

    def test_flagged_real_without_a_recorded_runtime_is_unverified_never_real(self):
        a = self.make()
        # A CI build machine can have the real llama.cpp installed, so the missing runtime is simulated instead of assumed from the sandbox.
        with mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value={"available": False, "error": "not installed (test)"}):
            rep = audit.run(a, styles=["normal"], categories=["ordinary"], simulated=False, with_controls=False)
        self.assertNotEqual(rep["model"]["runtime"].get("available"), True)          # the fake looks like a missing runtime
        self.assertTrue(rep["evidence_status"].startswith("UNVERIFIED"))
        self.assertNotIn("REAL MODEL", rep["evidence_status"])
        self.assertIn("NOT human-reviewed", rep["evidence_status"])
        self.assertIn(rep["evidence_status"], audit.to_markdown(rep))

    def test_suite_without_identity_is_unverified_and_with_it_follows_the_runtime(self):
        a = self.make()
        rep = evalsuite.run(a, styles=["normal"], scenarios=[evalsuite.SCENARIOS[0]], simulated=False)
        self.assertTrue(rep["evidence_status"].startswith("UNVERIFIED"))
        rep["model_identity"] = {"runtime": {"available": True}}
        self.assertTrue(evalsuite.evidence_status(rep).startswith("REAL MODEL, COMPLETE"))
        rep["complete"] = False
        self.assertIn("INCOMPLETE", evalsuite.evidence_status(rep))

    def test_incomplete_unverified_run_says_both(self):
        s = evalsuite.evidence_status({"simulated_model": False, "complete": False, "model": {"runtime": {"available": False}}})
        self.assertIn("UNVERIFIED", s)
        self.assertIn("INCOMPLETE", s)

    def test_simulated_flag_always_wins_and_is_never_upgraded(self):
        s = evalsuite.evidence_status({"simulated_model": True, "complete": True, "model": {"runtime": {"available": True}}})
        self.assertIn("SIMULATED", s)
        self.assertNotIn("REAL MODEL", s)

    def test_whole_report_and_transcript_contain_no_user_name_or_path(self):
        a = self.make()
        bad = r"ImportError: DLL load failed C:\Users\Al Ice\AppData\Local\x\llama.dll and /home/alice/.cache/m.gguf"
        with mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value={"available": False, "error": bad}), \
                mock.patch.object(a, "reply", side_effect=RuntimeError("cannot open C:/Users/Al Ice/models/m.gguf")), \
                mock.patch.object(a, "reply_stream", side_effect=RuntimeError("cannot open C:/Users/Al Ice/models/m.gguf"), create=True):
            rep = audit.run(a, styles=["normal"], categories=["ordinary"], simulated=False, with_controls=False)
        blob = json.dumps(rep, default=str) + audit.to_markdown(rep)
        for frag in ("Al Ice", "Ice", "alice", "AppData", "C:\\\\Users", "C:/Users", str(self.d)):
            self.assertNotIn(frag, blob, frag)
        j, m = audit.save(rep, self.cfg.data_dir)
        for f in (j, m):
            self.assertNotIn("alice", f.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
