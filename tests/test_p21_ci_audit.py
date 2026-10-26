"""Prompt 21: regression tests for defects found while auditing the (never previously run) Windows CI scripts. Linux static/unit checks only."""
import io
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class CliNetworkErrors(unittest.TestCase):
    def run_fetch(self, exc):
        import tempfile
        from jarvis import cli
        with tempfile.TemporaryDirectory() as d:
            err, out = io.StringIO(), io.StringIO()
            with mock.patch("jarvis.models.manager.ModelStore.download_gguf", side_effect=exc), redirect_stderr(err), redirect_stdout(out):
                code = cli.main(["--data-dir", d, "models", "fetch", "qwen3-1.7b-q4_k_m", "--yes"])
        return code, err.getvalue()

    def test_urlerror_is_reported_as_download_failed(self):
        code, err = self.run_fetch(urllib.error.URLError("blocked by firewall"))
        self.assertEqual(code, 3)
        self.assertIn("Download failed", err)

    def test_connection_and_timeout_errors_too(self):
        for exc in (ConnectionRefusedError("x"), TimeoutError("x")):
            code, err = self.run_fetch(exc)
            self.assertEqual(code, 3)
            self.assertIn("Download failed", err)

    def test_policy_refusal_is_not_reported_as_a_network_failure(self):
        from jarvis.net.policy import NetworkDisabled
        code, err = self.run_fetch(NetworkDisabled("network feature 'model_download' is disabled"))
        self.assertEqual(code, 3)
        self.assertNotIn("Download failed", err)   # otherwise the CI firewall proof could pass without any firewall


class CiScripts(unittest.TestCase):
    def test_workflow_has_timeouts_and_collects_logs_always(self):
        import yaml
        wf = yaml.safe_load((ROOT / ".github/workflows/build-windows.yml").read_text(encoding="utf-8"))
        job = wf["jobs"]["build"]
        self.assertIn("timeout-minutes", job)
        self.assertTrue(any(s.get("if") == "always()" and "upload-artifact" in s.get("uses", "") for s in job["steps"]))
        self.assertTrue(any("ci_real_model_test" in s.get("run", "") for s in job["steps"]))

    def test_ci_script_does_not_pipe_a_foreach_statement(self):
        s = (ROOT / "scripts/ci_real_model_test.ps1").read_text(encoding="utf-8")
        line = [l for l in s.splitlines() if "artifact_facts" in l][0]
        self.assertNotIn("{ foreach", line)   # a foreach *statement* cannot be piped in PowerShell 5.1

    def test_path_is_restored_after_the_no_python_check(self):
        s = (ROOT / "scripts/ci_real_model_test.ps1").read_text(encoding="utf-8")
        line = [l for l in s.splitlines() if "no_python_needed" in l][0]
        self.assertIn("finally", line)

    def test_build_script_run_helper_is_not_killed_by_native_stderr(self):
        b = (ROOT / "scripts/build_windows.ps1").read_text(encoding="utf-8")
        fn = b[b.index("function Run("):b.index("# ---- Preflight")]
        self.assertIn('$ErrorActionPreference = "Continue"', fn)     # PS 5.1: stderr lines are terminating errors under Stop
        self.assertIn("$LASTEXITCODE", fn)
        self.assertIn("finally", fn)

    def test_block_proof_requires_a_network_level_error(self):
        s = (ROOT / "scripts/ci_real_model_test.ps1").read_text(encoding="utf-8")
        self.assertIn('notmatch "Download failed"', s)


if __name__ == "__main__":
    unittest.main()
