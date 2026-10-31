"""Post-fix audit gaps: Persona.preview() for restricted styles, and report honesty (evidence status, path scrubbing). Real application code; scripted fake model."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from restriction_on import setUpModule, tearDownModule  # noqa: E402,F401  (these tests describe the restricted state)
from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.assistant import LocalAssistant  # noqa: E402
from jarvis.personality import audit, evalsuite, styles  # noqa: E402
from jarvis.personality.persona import Persona  # noqa: E402
from jarvis.personality.styles import RESTRICTION_REASON, STYLES, style_guidance  # noqa: E402

ADULT = ["savage", "flirty", "sexy", "romantic"]


class Preview(AppCase):
    def test_restricted_styles_are_described_but_their_prompt_is_not_published(self):
        self.assertTrue(styles.ADULT_MODES_RESTRICTED)
        p = Persona(None, persist=False)
        p.set_adult_optin(True)
        for sid in ADULT:
            pv = p.preview(sid)
            self.assertTrue(pv["restricted"])
            self.assertFalse(pv["available"])
            self.assertEqual(pv["lock_reason"], RESTRICTION_REASON)
            self.assertEqual(pv["model_instructions"], "")
            self.assertEqual(pv["effective_controls"], {})
            self.assertEqual(p.settings.style, "normal")
        guidance = style_guidance("savage", {k: 1 for k in styles.SLIDERS}, {})
        for sid in ADULT:
            self.assertNotIn(guidance[:40], json.dumps(p.preview(sid)))

    def test_other_styles_still_preview_with_real_instructions(self):
        p = Persona(None, persist=False)
        for sid in STYLES:
            if sid in ADULT:
                continue
            pv = p.preview(sid)
            self.assertFalse(pv.get("restricted"))
            self.assertIn("SAFETY", pv["model_instructions"].upper())

    def test_controller_and_cli_give_the_same_non_disclosing_preview(self):
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        self.assertEqual(c.persona_preview("sexy")["model_instructions"], "")
        from jarvis import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "persona", "preview", "romantic"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["model_instructions"], "")

    def test_window_shows_the_reason_for_a_restricted_preview(self):
        src = (Path(__file__).resolve().parent.parent / "src" / "jarvis" / "ui" / "window.py").read_text(encoding="utf-8")
        self.assertIn('pv.get("restricted")', src)
        self.assertIn("Temporarily unavailable", src)

    def test_lifting_the_constant_restores_the_full_preview_only_for_that_case(self):
        with mock.patch.object(styles, "ADULT_MODES_RESTRICTED", False):
            pv = Persona(None, persist=False).preview("savage")
        self.assertTrue(pv["model_instructions"])


class ReportHonesty(AppCase):
    def make(self, reply="Twelve. Plain scripted reply."):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = reply
        a = LocalAssistant(self.cfg, engine_factory=fake_engine_factory, guest=True)
        self.addCleanup(a.close)
        return a

    def test_simulated_report_says_it_is_not_evidence(self):
        rep = audit.run(self.make(), styles=["normal"], categories=["ordinary"], simulated=True, with_controls=False)
        self.assertIn("SIMULATED", rep["evidence_status"])
        self.assertIn("NOT evidence", rep["evidence_status"])
        self.assertFalse(rep["human_reviewed"])
        self.assertIn(rep["evidence_status"], audit.to_markdown(rep))
        srep = evalsuite.run(self.make(), styles=["normal"], scenarios=[evalsuite.SCENARIOS[0]], simulated=True)
        self.assertIn("SIMULATED", srep["evidence_status"])
        self.assertFalse(srep["human_reviewed"])

    def test_a_report_flagged_real_never_says_complete_when_a_case_failed(self):
        a = self.make()
        with mock.patch.object(a, "reply", side_effect=RuntimeError("model died")), mock.patch.object(a, "reply_stream", side_effect=RuntimeError("x"), create=True):
            rep = audit.run(a, styles=["normal"], categories=["ordinary"], simulated=False, with_controls=False)
        self.assertFalse(rep["complete"])
        self.assertIn("INCOMPLETE", rep["evidence_status"])
        self.assertNotIn("REAL MODEL, COMPLETE", rep["evidence_status"])
        self.assertIn("not a pass", rep["evidence_status"])
        with mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value={"available": True}):
            with mock.patch.object(a, "reply", side_effect=RuntimeError("model died")), mock.patch.object(a, "reply_stream", side_effect=RuntimeError("x"), create=True):
                rep2 = audit.run(a, styles=["normal"], categories=["ordinary"], simulated=False, with_controls=False)
        self.assertTrue(rep2["evidence_status"].startswith("REAL MODEL, INCOMPLETE"))

    def test_a_complete_real_flagged_report_still_says_automated_and_not_human_reviewed(self):
        with mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value={"available": True, "version": "test"}):       # stands in for a real runtime
            rep = audit.run(self.make(), styles=["normal"], categories=["ordinary"], simulated=False, with_controls=False)
        self.assertTrue(rep["complete"])
        self.assertIn("REAL MODEL, COMPLETE", rep["evidence_status"])
        self.assertIn("NOT human-reviewed", rep["evidence_status"])
        self.assertIn("only to the model identified", rep["evidence_status"])
        self.assertEqual(set(rep["human_scores"]["normal"].values()), {None})

    def test_suite_status_mentions_cases_that_were_not_run(self):
        rep = evalsuite.run(self.make(), styles=["normal"], simulated=False)
        if rep["not_run"]:
            self.assertIn("NOT RUN", rep["evidence_status"])
            self.assertIn("untested", rep["evidence_status"])

    def test_model_identity_is_scrubbed_of_paths_and_user_names(self):
        a = self.make()
        with mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value={"available": False, "error": r"ImportError: DLL load failed C:\Users\alice\AppData\x\llama.dll"}):
            ident = audit.model_identity(a)
        text = json.dumps(ident)
        self.assertNotIn("alice", text)
        self.assertNotIn("C:\\\\Users", text)
        self.assertIn("<path>", text)
        self.assertNotIn(str(self.d), text)

    def test_missing_model_is_exit_2_with_no_report_and_nothing_simulated(self):
        from jarvis import cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "persona", "audit"])
        self.assertEqual(rc, 2)
        self.assertIn("Cannot run the audit", err.getvalue())
        self.assertIn("nothing was simulated", err.getvalue())
        self.assertEqual(list((self.cfg.data_dir / "eval").glob("*")) if (self.cfg.data_dir / "eval").exists() else [], [])


if __name__ == "__main__":
    unittest.main()
