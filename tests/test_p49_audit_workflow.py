"""Audit workflow plumbing. FAKE MODEL: replies are scripted, so NOTHING here says anything about personality quality or real generation."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402

from jarvis.assistant import LocalAssistant  # noqa: E402
from jarvis.personality import audit  # noqa: E402
from jarvis.personality.styles import STYLES  # noqa: E402


class Workflow(AppCase):
    def make(self):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = "Twelve. Plain scripted reply number one."
        a = LocalAssistant(self.cfg, engine_factory=fake_engine_factory, guest=True)
        self.addCleanup(a.close)
        return a

    def test_every_personality_gets_identical_prompts_and_a_defining_prompt(self):
        self.assertEqual(set(audit.DEFINING), set(STYLES))
        rep = audit.run(self.make(), simulated=True, with_controls=False)
        self.assertTrue(rep["simulated_model"])
        self.assertIn("SIMULATED", " ".join(rep["limitations"]))
        for cat in audit.PROMPTS:
            users = {tuple(r["user"] for r in rep["transcripts"][s][cat]) for s in STYLES}
            self.assertEqual(len(users), 1, cat)
        self.assertEqual(len(rep["transcripts"]), 17)
        for s in STYLES:
            self.assertEqual(rep["transcripts"][s]["defining"][0]["user"], audit.DEFINING[s])
        self.assertTrue(all(rep["transcripts"][s]["serious_sensitive"][0]["overridden"] for s in STYLES))

    def test_controls_sweep_records_effective_levels_and_blank_human_scores(self):
        rep = audit.run(self.make(), styles=["concise", "normal"], categories=["ordinary"], simulated=True)
        c = rep["controls"]["concise"]["verbosity"]
        self.assertEqual(c["effective_levels"], {"min": 1, "default": 1, "max": 2})
        self.assertEqual(set(c["replies"]), {"min", "default", "max"})
        self.assertTrue(all(v is None for s in rep["human_scores"].values() for v in s.values()), "no score may be invented")

    def test_artifact_contains_no_personal_memory_and_is_saved_as_json_and_markdown(self):
        a = self.make()
        a.memory.add("explicit_preference", "my favourite colour is teal and my secret code word is pineapple")
        rep = audit.run(a, styles=["normal", "savage"], categories=["ordinary"], simulated=True, with_controls=False)
        j, m = audit.save(rep, Path(self.tmp.name))
        text = j.read_text() + m.read_text()
        self.assertNotIn("pineapple", text)
        self.assertEqual(json.loads(j.read_text())["audit_version"] if "audit_version" in json.loads(j.read_text()) else 1, 1)
        self.assertIn("Human rubric", m.read_text())

    def test_analysis_flags_identical_replies_and_repeated_openers(self):
        rep = audit.run(self.make(), styles=["normal", "calm", "technical"], categories=["ordinary", "technical", "frustration"], simulated=True, with_controls=False)
        an = rep["automated_analysis"]
        self.assertTrue(an["pairs_with_overlap_0.7_or_more"], "identical scripted replies must be flagged as too similar")
        self.assertTrue(an["repeated_openers_3plus_times"])
        self.assertEqual(audit.jaccard("a b c", "a b c"), 1.0)
        self.assertEqual(audit.jaccard("a b", "c d"), 0.0)

    def test_compare_ranks_nothing(self):
        rep = audit.run(self.make(), styles=["normal"], categories=["ordinary"], simulated=True, with_controls=False)
        out = audit.compare([rep, rep])
        self.assertEqual(len(out["rows"]), 2)
        self.assertIn("not a ranking", out["note"])

    def test_cli_audit_reports_cleanly_without_a_model(self):
        import io
        from unittest import mock
        from jarvis import cli
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "persona", "audit"])
        self.assertEqual(rc, 2)
        self.assertIn("Cannot run the audit", err.getvalue())


if __name__ == "__main__":
    unittest.main()
