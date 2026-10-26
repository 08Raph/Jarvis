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


class PluginReviewLinks(unittest.TestCase):
    """Windows CI run 7: a plugin folder holding a symbolic link whose target does not exist (always the case for '/etc/passwd' on Windows)
    crashed the review with FileNotFoundError instead of blocking it. The review must never follow a link."""
    def test_dangling_symlink_is_blocked_not_a_crash(self):
        import json
        import tempfile
        from jarvis.dev import plugins as PL
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "manifest.json").write_text(json.dumps({"name": "linky", "version": "1.0.0", "description": "x", "license": "MIT", "entry": "plugin.py",
                                                         "inputs": {"text": {"type": "str", "description": "t", "max_len": 50}}, "outputs": "n",
                                                         "permissions": ["compute"], "tests": []}), encoding="utf-8")
            (d / "plugin.py").write_text("def run(args):\n    return 'ok'\n", encoding="utf-8")
            try:
                (d / "evil.py").symlink_to(d / "does-not-exist.py")
            except OSError:
                self.skipTest("symlinks cannot be created here")
            try:
                rev = PL.review(d)
            except OSError as e:
                self.fail(f"review crashed on a dangling link: {e}")
            self.assertFalse(rev["ok"])
            self.assertTrue(any("symbolic link" in b for b in rev["blockers"]), rev["blockers"])


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

    def test_installer_script_uses_only_real_inno_setup_directives(self):
        # Run 5: ISCC rejected "InfoBeforeInstallFile" (the directive is InfoBeforeFile). Inno cannot run on Linux, so check names statically.
        known = {"AppId", "AppName", "AppVersion", "AppPublisher", "DefaultDirName", "DefaultGroupName", "PrivilegesRequired",
                 "PrivilegesRequiredOverridesAllowed", "MinVersion", "ArchitecturesAllowed", "ArchitecturesInstallIn64BitMode", "Compression",
                 "SolidCompression", "WizardStyle", "LicenseFile", "OutputDir", "OutputBaseFilename", "UninstallDisplayName",
                 "CloseApplications", "InfoBeforeFile", "InfoAfterFile", "SetupIconFile", "UninstallDisplayIcon", "AppPublisherURL"}
        text = (ROOT / "packaging/installer.iss").read_text(encoding="utf-8")
        sect = text.split("[Setup]", 1)[1].split("\n[", 1)[0]
        names = {l.split("=", 1)[0].strip() for l in sect.splitlines() if "=" in l and not l.lstrip().startswith(";")}
        self.assertEqual(names - known, set())

    def test_installer_exit_code_is_read_reliably_and_logged(self):
        # Run 6: the silent install reported "exit " (empty ExitCode, PS 5.1) and left no diagnosis.
        for name in ("clean_install_test.ps1", "ci_real_model_test.ps1"):
            t = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            sect = t[t.index('Check "silent_install"'):]
            sect = sect[:sect.index('\n}\n') if '\n}\n' in sect else 600]
            self.assertIn("$p.Handle", sect, name)
            self.assertIn("WaitForExit", sect, name)

    def test_ci_scripts_end_with_an_explicit_exit_and_the_workflow_resets_the_exit_code(self):
        # Run 7: every install check passed, yet the step failed because $LASTEXITCODE still held 2 from the expected-to-fail "chat" call.
        for name in ("clean_install_test.ps1", "ci_real_model_test.ps1"):
            t = (ROOT / "scripts" / name).read_text(encoding="utf-8").rstrip().splitlines()
            self.assertTrue(t[-1].startswith("exit 0"), name)
            self.assertIn("exit 1", "\n".join(t[-4:]), name)
        wf = (ROOT / ".github/workflows/build-windows.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(wf.count("$global:LASTEXITCODE = 0"), 2)

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
