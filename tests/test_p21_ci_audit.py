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
        wf = (ROOT / ".github/workflows/build-windows.yml").read_text(encoding="utf-8")   # no PyYAML on the Windows build venv
        self.assertIn("timeout-minutes", wf)
        self.assertIn("if: always()", wf)
        self.assertIn("ci_real_model_test", wf)
        self.assertIn("Final gate", wf)

    def test_release_check_accepts_pyinstaller6_layout_but_not_private_keys(self):
        import tempfile
        sys.path.insert(0, str(ROOT / "scripts"))
        sys.path.insert(0, str(ROOT / "packaging"))
        import check_release_assets as C
        import manifest as MF
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for r in MF.REQUIRED_IN_DIST:
                f = d / r if r.endswith(".exe") else d / "_internal" / r
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"x" * (200_000 if r.endswith(".exe") else 1))
            (d / "_internal" / "api-ms-win-crt-private-l1-1-0.dll").write_bytes(b"1")
            (d / "_internal" / "qtgraphicaleffectsprivateplugin.dll").write_bytes(b"1")
            self.assertEqual(C.check_dist(d), [])
            (d / "jarvis_signing_private.hex").write_bytes(b"1")
            self.assertTrue(any("signing_private" in p for p in C.check_dist(d)))
            (d / "jarvis_signing_private.hex").unlink()
            (d / "jarvis.exe").unlink()
            self.assertTrue(any("jarvis.exe" in p for p in C.check_dist(d)))

    def test_windows_build_declares_the_time_zone_database(self):
        self.assertIn("tzdata", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn('collect_data_files("tzdata")', (ROOT / "packaging/jarvis.spec").read_text(encoding="utf-8"))

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
