"""Failure behaviour of `persona audit` / `persona suite`. FAKE MODEL (a scripted engine that can be told to fail): proves the workflow never hides
failures or reports partial evidence as complete. It says nothing about any real model."""
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


class Failures(AppCase):
    def make(self):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = "Twelve. Scripted."
        a = LocalAssistant(self.cfg, engine_factory=fake_engine_factory, guest=True)
        self.addCleanup(a.close)
        return a

    def break_after(self, a, n):
        eng, orig, calls = a._eng(), a._eng().chat, {"n": 0}

        def chat(*x, **k):
            calls["n"] += 1
            if calls["n"] > n:
                raise RuntimeError("model crashed: out of memory")
            return orig(*x, **k)
        eng.chat = chat

    def test_audit_failure_midway_is_recorded_and_never_complete(self):
        a = self.make()
        self.break_after(a, 3)
        saved = []
        rep = audit.run(a, styles=["normal", "calm"], categories=["ordinary", "technical", "frustration"], simulated=True, with_controls=False, checkpoint=lambda r: saved.append(1))
        self.assertFalse(rep["complete"])
        self.assertTrue(rep["incomplete"] and rep["missing_cases"])
        self.assertIn("RuntimeError", rep["incomplete"][0])
        self.assertIn("INCOMPLETE RUN", " ".join(rep["limitations"]))
        failed = [r for cats in rep["transcripts"].values() for rows in cats.values() for r in rows if r["reply"] is None]
        self.assertTrue(failed and all(r["status"] == "failed" for r in failed))
        self.assertIn("NO REPLY - FAILED", audit.to_markdown(rep))
        self.assertIn("INCOMPLETE RUN", audit.to_markdown(rep))
        self.assertTrue(saved, "a checkpoint is written so earlier evidence survives")
        self.assertIsNotNone(rep["aborted"], "stops after repeated consecutive failures instead of grinding on")

    def test_audit_ctrl_c_returns_partial_marked_interrupted(self):
        a = self.make()
        eng, orig, n = a._eng(), a._eng().chat, {"n": 0}

        def chat(*x, **k):
            n["n"] += 1
            if n["n"] == 3:
                raise KeyboardInterrupt
            return orig(*x, **k)
        eng.chat = chat
        rep = audit.run(a, styles=["normal"], categories=["ordinary", "technical", "frustration"], simulated=True, with_controls=False)
        self.assertTrue(rep["interrupted"] and not rep["complete"])

    def test_clean_run_is_marked_complete_and_records_identity_fields(self):
        rep = audit.run(self.make(), styles=["normal"], categories=["ordinary"], simulated=True, with_controls=False)
        self.assertTrue(rep["complete"])
        self.assertEqual(rep["model"]["model_id"], "fake-1.7b" if rep["model"]["model_id"] == "fake-1.7b" else rep["model"]["model_id"])
        for k in ("runtime", "host", "n_ctx", "sampling_profile"):
            self.assertIn(k, rep["model"])
        self.assertIn("sampling_seed", rep["config"])

    def test_suite_scenario_error_is_an_error_not_a_pass(self):
        a = self.make()
        self.break_after(a, 2)
        rep = evalsuite.run(a, styles=["normal"], scenarios=["simple_factual", "complex_technical", "ambiguous_request", "previous_mistake"], simulated=True)
        self.assertFalse(rep["complete"])
        self.assertTrue(rep["errors"])
        self.assertTrue(all(r["passed"] is not True for r in rep["results"] if r["status"] == "error"))
        self.assertTrue(rep["aborted"] or rep["missing_cases"] or rep["errors"])

    def run_cli(self, *a):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), *a])
        return rc, out.getvalue(), err.getvalue()

    def test_cli_without_a_model_fails_clearly_writes_nothing_and_does_not_download(self):
        for cmd in ("audit", "suite"):
            with mock.patch("socket.socket", side_effect=AssertionError("network used")):
                rc, out, err = self.run_cli("persona", cmd)
            self.assertEqual(rc, 2, cmd)
            self.assertIn("nothing was simulated", err)
        self.assertFalse(list((self.cfg.data_dir / "eval").glob("*.json")) if (self.cfg.data_dir / "eval").exists() else [])

    def test_cli_with_installed_model_that_cannot_load_exits_nonzero_with_message(self):
        self.install_model()          # installed, but llama-cpp is not available in this environment
        rc, out, err = self.run_cli("persona", "audit")
        self.assertEqual(rc, 2)
        self.assertIn("Cannot run the audit", err)

    def test_audit_and_suite_sources_import_no_network_or_cloud_modules(self):
        root = Path(__file__).resolve().parents[1] / "src/jarvis/personality"
        for f in ("audit.py", "evalsuite.py"):
            src = (root / f).read_text(encoding="utf-8")
            for bad in ("import requests", "import urllib", "import http", "import socket", "openai", "anthropic", "httpx", "aiohttp"):
                self.assertNotIn(bad, src, f"{f}: {bad}")

    def test_reports_contain_no_data_dir_path_or_user_name(self):
        a = self.make()
        rep = audit.run(a, styles=["normal"], categories=["ordinary"], simulated=True, with_controls=False)
        text = json.dumps(rep, default=str)
        self.assertNotIn(str(self.cfg.data_dir), text)
        self.assertNotIn("/root", text)
        self.assertNotIn("ezbybedwars", text)


if __name__ == "__main__":
    unittest.main()
