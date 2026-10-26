"""Other-PC portability: launcher behaviour (double-click, crashes) and static checks of the Windows packaging files.
The Windows-only parts (console detection, AVX2 query, PowerShell, Inno Setup) cannot run in this Linux sandbox."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis import hardware, launcher
from jarvis.inference import gguf_engine


class LauncherTests(unittest.TestCase):
    def _run(self, main, argv, ours, data_dir):
        prompts = []
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = launcher.run(main, argv, input_fn=lambda m: prompts.append(m) or "", ours=lambda: ours, data_dir=data_dir)
        return rc, prompts, buf.getvalue()

    def test_bare_launch_runs_setup_and_pauses_only_when_console_is_ours(self):
        seen = []
        with tempfile.TemporaryDirectory() as d:
            rc, prompts, _ = self._run(lambda a: seen.append(a) or 0, [], True, Path(d))
            self.assertEqual((rc, seen[-1], len(prompts)), (0, ["setup"], 1))
            rc, prompts, _ = self._run(lambda a: seen.append(a) or 0, [], False, Path(d))   # started from a shell
            self.assertEqual((rc, len(prompts)), (0, 0))

    def test_explicit_commands_pass_through_and_never_pause(self):
        seen = []
        with tempfile.TemporaryDirectory() as d:
            rc, prompts, _ = self._run(lambda a: seen.append(a) or 0, ["version"], True, Path(d))
            self.assertEqual((rc, seen[-1], prompts), (0, ["version"], []))

    def test_argparse_exit_is_returned_not_raised(self):
        def bad(_):
            raise SystemExit(2)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._run(bad, ["x"], False, Path(d))[0], 2)

    def test_crash_is_explained_logged_locally_and_window_waits(self):
        def boom(_):
            raise RuntimeError("kaboom")
        with tempfile.TemporaryDirectory() as d:
            rc, prompts, out = self._run(boom, ["chat"], True, Path(d))
            self.assertEqual((rc, len(prompts)), (1, 1))
            self.assertIn("unexpected problem", out)
            self.assertIn("Nothing was sent anywhere", out)
            log = (Path(d) / "crash.log").read_text(encoding="utf-8")
            self.assertIn("kaboom", log)
            self.assertIn("Traceback", log)

    def test_unwritable_crash_log_does_not_mask_the_message(self):
        def boom(_):
            raise RuntimeError("x")
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "file"
            blocker.write_text("not a directory")
            rc, _, out = self._run(boom, ["chat"], False, blocker / "sub")
            self.assertEqual(rc, 1)
            self.assertIn("unexpected problem", out)

    def test_console_detection_is_false_off_windows(self):
        if sys.platform != "win32":
            self.assertFalse(launcher.console_is_ours())

    def test_entry_point_uses_launcher(self):
        src = (ROOT / "packaging" / "entry_cli.py").read_text()
        self.assertIn("from jarvis.launcher import run", src)
        self.assertIn("run(main)", src)


class HardwareRuntimeTests(unittest.TestCase):
    def test_avx2_unknown_off_windows(self):
        if sys.platform != "win32":
            self.assertIsNone(hardware.cpu_has_avx2())

    def test_missing_avx2_adds_honest_warning(self):
        with mock.patch.object(hardware, "cpu_has_avx2", return_value=False):
            hw = hardware.detect_hardware(".")
        self.assertIs(hw.avx2, False)
        self.assertTrue(any("AVX2" in n for n in hw.notes))

    def test_runtime_info_reports_avx2_and_warns(self):
        fake = mock.MagicMock(__version__="9.9")
        with mock.patch.dict(sys.modules, {"llama_cpp": fake}), mock.patch.object(hardware, "cpu_has_avx2", return_value=False):
            info = gguf_engine.runtime_info()
        self.assertTrue(info["available"])
        self.assertIs(info["cpu_avx2"], False)
        self.assertIn("AVX2", info["warning"])

    def test_endpoint_found_through_meipass_when_frozen(self):
        from jarvis.telemetry import client
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "jarvis" / "telemetry").mkdir(parents=True)
            (Path(d) / "jarvis" / "telemetry" / "endpoint.txt").write_text("https://example.org", encoding="utf-8")
            with mock.patch.object(client, "__file__", str(Path(d) / "nowhere" / "client.py")), \
                    mock.patch.object(sys, "_MEIPASS", d, create=True):
                self.assertEqual(client.get_endpoint(), "https://example.org")


class WindowsPackagingStaticTests(unittest.TestCase):
    def test_installer_arch_is_parameterised_for_old_inno_6(self):
        iss = (ROOT / "packaging" / "installer.iss").read_text()
        self.assertIn("ArchitecturesAllowed={#ArchName}", iss)
        self.assertIn('#define ArchName "x64compatible"', iss)
        build = (ROOT / "scripts" / "build_windows.ps1").read_text()
        self.assertIn("/DArchName=$archName", build)
        self.assertIn('$archName = "x64"', build)

    def test_build_runs_import_table_vc_check_before_installer_and_fails_on_missing(self):
        b = (ROOT / "scripts" / "build_windows.ps1").read_text()
        self.assertIn("check_vc_runtime.py", b)
        self.assertLess(b.index("check_vc_runtime.py"), b.index("installer.iss"))
        self.assertIn("Sysnative", b)   # a 32-bit PowerShell must not copy 32-bit DLLs
        self.assertNotIn("Write-Host \"note: $dll not found", b)   # no silent skipping of a missing DLL

    def test_test_gate_uses_the_runner_that_was_actually_run_and_skip_is_loud(self):
        b = (ROOT / "scripts" / "build_windows.ps1").read_text()
        self.assertNotIn("-m pytest", b)
        self.assertIn("if ($SkipTests)", b)
        self.assertIn("Do not release a build made this way", b)

    def test_clean_install_test_checks_vc_runtime_and_bare_launch(self):
        t = (ROOT / "scripts" / "clean_install_test.ps1").read_text()
        self.assertIn("vc_runtime_bundled", t)
        self.assertIn("bare_launch_runs_setup_and_exits", t)

    def test_clean_install_script_ps51_hazards_handled(self):
        t = (ROOT / "scripts" / "clean_install_test.ps1").read_text()
        self.assertIn("first-run | Out-String) | ConvertFrom-Json", t)   # PS 5.1 cannot parse multi-line JSON from the pipeline
        self.assertIn("Start-Sleep", t)                                  # uninstaller may hand over to a temp copy
        self.assertIn("*.gguf", t)

    def test_spec_collects_portaudio_package_and_first_run_hint_is_current(self):
        self.assertIn("_sounddevice_data", ((ROOT / "packaging" / "jarvis.spec").read_text() + (ROOT / "packaging" / "manifest.py").read_text()))   # Prompt 10: asset lists moved from the spec to packaging/manifest.py
        from jarvis.config import Config
        from jarvis.firstrun import first_run
        with tempfile.TemporaryDirectory() as d:
            hint = " ".join(first_run(Config(data_dir=Path(d) / "x"))["next_steps"])
        self.assertIn("jarvis setup", hint)
        self.assertNotIn(".jarvismodel", hint)

    def test_wizard_download_reports_progress_and_says_not_yet_loaded(self):
        from jarvis import setup_wizard
        from jarvis.models import catalog

        class FakeStore:
            def download_gguf(self, model, progress=None, **kw):
                for i in range(1, 11):
                    progress(i * 100, 1000)
                return {"id": model.id}

            def set_active(self, mid):
                pass

        class P:
            def grant_once(self, f):
                import contextlib
                return contextlib.nullcontext()

        lines = []
        setup_wizard._download_flow(None, FakeStore(), P(), catalog.BUILTIN[0], {"verdict": "ok"},
                                    lambda q: "yes", lines.append)
        text = "\n".join(lines)
        self.assertIn("50%", text)
        self.assertIn("NOT been loaded yet", text)

    def test_text_files_are_ascii_safe_for_windows_powershell_5(self):
        # Windows PowerShell 5.1 reads BOM-less files as ANSI, so non-ASCII in .ps1/.iss/.bat corrupts them.
        for rel in ("scripts/build_windows.ps1", "scripts/clean_install_test.ps1", "packaging/installer.iss", "build_installer.bat"):
            data = (ROOT / rel).read_bytes()
            self.assertTrue(all(b < 128 for b in data), rel)


if __name__ == "__main__":
    unittest.main()
