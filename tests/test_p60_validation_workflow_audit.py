"""Post-6939159 validation-readiness audit. Real application code (audit, evalsuite, cli); scripted fake model; runtime_info is patched where a 'real runtime'
is needed to see the REAL labels. Nothing here is real inference."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402
from jarvis import cli  # noqa: E402
from jarvis.assistant import LocalAssistant  # noqa: E402
from jarvis.personality import audit, evalsuite  # noqa: E402

REAL_ASSISTANT = LocalAssistant
RT_OK = {"available": True, "version": "test"}
DOCS = Path(__file__).resolve().parent.parent / "docs"


class Base(AppCase):
    def make(self, reply):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = reply
        a = REAL_ASSISTANT(self.cfg, engine_factory=fake_engine_factory, guest=True)
        self.addCleanup(a.close)
        return a

    def cli(self, *args, runtime=RT_OK):
        out, err = io.StringIO(), io.StringIO()
        fac = lambda cfg, guest=False: REAL_ASSISTANT(cfg, engine_factory=fake_engine_factory, guest=guest)      # noqa: E731
        with mock.patch("jarvis.assistant.LocalAssistant", fac), mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value=runtime), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "persona", *args])
        return rc, out.getvalue(), err.getvalue()


class EmptyReplies(Base):
    def test_empty_or_blank_replies_are_failures_never_a_complete_real_report(self):
        for reply in ("", "   ", "\n\n"):
            with self.subTest(reply=reply):
                a = self.make(reply)
                with mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value=RT_OK):
                    rep = audit.run(a, styles=["normal"], categories=["ordinary", "technical", "frustration", "uncertainty"], simulated=False, with_controls=False)
                    srep = evalsuite.run(a, styles=["normal"], scenarios=evalsuite.SCENARIOS[:4], simulated=False)
                for r in (rep, srep):
                    self.assertFalse(r["complete"])
                    self.assertTrue(r["aborted"], "3 empty replies in a row stop the run like any other failure")
                    self.assertNotIn("REAL MODEL, COMPLETE", r["evidence_status"])
                self.assertTrue(any("empty reply" in x for x in rep["incomplete"]))
                self.assertTrue(any("empty reply" in x for x in srep["errors"]))

    def test_a_bare_style_label_is_an_empty_reply(self):
        a = self.make("")
        with mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value=RT_OK):
            rep = audit.run(a, styles=["conspiracy"], categories=["ordinary"], simulated=False, with_controls=False)
        self.assertFalse(rep["complete"])
        self.assertTrue(any("empty reply" in x for x in rep["incomplete"]))

    def test_ordinary_short_replies_are_still_accepted(self):
        for reply in ("Yes.", "..", "ok"):
            a = self.make(reply)
            rep = audit.run(a, styles=["normal"], categories=["ordinary"], simulated=True, with_controls=False)
            self.assertTrue(rep["complete"], reply)

    def test_cli_exits_1_and_says_incomplete_for_empty_replies(self):
        self.install_model()
        FakeLlama.reply = ""
        rc, out, err = self.cli("audit", "--styles", "normal", "--categories", "ordinary", "technical", "frustration", "--no-controls")
        self.assertEqual(rc, 1)
        self.assertIn("INCOMPLETE", err)
        saved = json.loads(next((self.cfg.data_dir / "eval").glob("personality_audit_*.json")).read_text(encoding="utf-8"))
        self.assertFalse(saved["complete"])
        self.assertNotIn("REAL MODEL, COMPLETE", saved["evidence_status"])


class CliStatus(Base):
    def test_suite_cli_recomputes_status_after_the_identity_is_attached(self):
        self.install_model()
        FakeLlama.reply = "Twelve. Plain scripted reply."
        rc, out, _ = self.cli("suite", "--styles", "normal", "--scenarios", evalsuite.SCENARIOS[0])
        saved = json.loads(next((self.cfg.data_dir / "eval").glob("personality_suite_*.json")).read_text(encoding="utf-8"))
        self.assertTrue(saved["evidence_status"].startswith("REAL MODEL"), saved["evidence_status"])
        self.assertEqual(saved["model_identity"]["runtime"]["available"], True)
        self.assertIn(saved["evidence_status"], out)
        self.assertIs(saved["human_reviewed"], False)

    def test_suite_cli_with_no_recorded_runtime_is_unverified(self):
        self.install_model()
        FakeLlama.reply = "Twelve. Plain scripted reply."
        _, out, _ = self.cli("suite", "--styles", "normal", "--scenarios", evalsuite.SCENARIOS[0], runtime={"available": False, "error": "ImportError"})
        saved = json.loads(next((self.cfg.data_dir / "eval").glob("personality_suite_*.json")).read_text(encoding="utf-8"))
        self.assertTrue(saved["evidence_status"].startswith("UNVERIFIED"))
        self.assertIn("UNVERIFIED", out)

    def test_audit_cli_status_matches_the_saved_file_and_markdown(self):
        self.install_model()
        FakeLlama.reply = "Twelve. Plain scripted reply."
        rc, out, _ = self.cli("audit", "--styles", "normal", "--categories", "ordinary", "--no-controls")
        self.assertEqual(rc, 0)
        j = next((self.cfg.data_dir / "eval").glob("personality_audit_*.json"))
        saved = json.loads(j.read_text(encoding="utf-8"))
        self.assertTrue(saved["evidence_status"].startswith("REAL MODEL, COMPLETE"))
        self.assertIn(saved["evidence_status"], j.with_suffix(".md").read_text(encoding="utf-8"))
        self.assertIn(saved["evidence_status"], out)


class ProbeAndProtocol(Base):
    def test_every_audit_report_states_the_probe_is_a_single_limited_sample(self):
        rep = audit.run(self.make("Twelve. Plain."), styles=["normal"], categories=["ordinary"], simulated=True, with_controls=False)
        text = " ".join(rep["limitations"])
        self.assertIn("serious_undetected_probe is ONE phrasing", text)
        self.assertIn("nothing about the detector's overall accuracy", text.replace("says ", ""))

    def test_serious_and_defining_cases_exist_for_every_unrestricted_mode(self):
        from jarvis.personality.styles import STYLES
        self.assertTrue({"serious_sensitive", "serious_undetected_probe", "safety_sensitive", "override_attempt"} <= set(audit.PROMPTS))
        for sid, st in STYLES.items():
            self.assertIn(sid, audit.DEFINING)
        rep = audit.run(self.make("Twelve. Plain."), categories=["serious_sensitive", "serious_undetected_probe"], simulated=True, with_controls=False)
        self.assertEqual(len(rep["styles"]), 13)
        self.assertTrue(all(c in rep["transcripts"][s] for s in rep["styles"] for c in ("serious_sensitive", "serious_undetected_probe", "defining")))

    def test_protocol_separates_the_four_stages_and_warns_about_console_paths(self):
        text = (DOCS / "STAGE7_REAL_MODEL_REVIEW_PROTOCOL.md").read_text(encoding="utf-8")
        for w in ("## 1. Prerequisites", "## 2. Commands", "## 3. Confirm which model was used", "## 5. Reviewing the personalities", "does not download", "UNVERIFIED",
                  "not a pass", "local paths"):
            self.assertIn(w, text)

    def test_no_network_in_the_workflow_modules(self):
        src = Path(__file__).resolve().parent.parent / "src" / "jarvis" / "personality"
        for f in ("audit.py", "evalsuite.py", "selfcheck.py"):
            text = (src / f).read_text(encoding="utf-8")
            for bad in ("import requests", "urllib", "http.client", "socket", "httpx", "openai", "anthropic", "huggingface"):
                self.assertNotIn(bad, text, (f, bad))


if __name__ == "__main__":
    unittest.main()
