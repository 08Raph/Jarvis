"""Prompt 20: controller wiring of the wizard, the diagnostic report, and static checks of the installer and the (never displayed) wizard window.
Controller tests use real threads/files with a FAKE engine; nothing here displays a window, installs anything, or runs a real model."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.app import diagnostics
from jarvis.app import setup_engine as SE
from jarvis.app.controller import AppController
from jarvis.config import Config
from jarvis.eval import winvalidate
from jarvis.inference import accel
from p10_common import AppCase
from test_p10_controller import settle
from test_p20_setup import FakeLlama, FakeSampler, CPU_ONLY, MID


class ControllerWiring(AppCase):
    def ctl(self, simulate=False):
        c = AppController(self.cfg, simulate=simulate)
        self.addCleanup(c.shutdown, 1.0)
        return c

    def test_first_run_needs_setup_but_simulated_and_guest_do_not(self):
        self.assertTrue(self.ctl().needs_setup())
        self.assertFalse(self.ctl(simulate=True).needs_setup())
        g = AppController(self.cfg, guest=True); self.addCleanup(g.shutdown, 1.0)
        self.assertFalse(g.needs_setup())

    def test_preview_runs_without_network_or_model(self):
        p = self.ctl().setup_preview()
        self.assertIn("system", p); self.assertIn("models", p["model"])
        self.assertFalse(p["system"]["acceleration"]["acceleration_verified"])

    def test_run_setup_runs_in_background_and_reports_failure_readably(self):
        c = self.ctl()
        got, steps = [], []
        eng = SE.SetupEngine(self.cfg, c.setup_engine().store, c.setup_engine().policy, llama_factory=FakeLlama, hardware=CPU_ONLY,
                             runtime={"available": False, "error": "no runtime"})
        with mock.patch.object(c, "setup_engine", return_value=eng):
            self.assertTrue(c.run_setup(None, lambda s, f, m: steps.append(s), got.append))
            settle(c)
        self.assertEqual(got[0]["failed"]["step"], "system")
        self.assertIn("system", steps)

    def test_cancel_setup_cancels_worker(self):
        c = self.ctl()
        got = []

        class Slow:
            def run(self, model_id, confirm_download, cancel, progress):
                for _ in range(300):
                    if cancel.is_set():
                        return {"failed": {"step": "cancelled", "message": "x"}, "completed": False}
                    time.sleep(0.01)
                return {"completed": True}
        with mock.patch.object(c, "setup_engine", return_value=Slow()):
            c.run_setup(None, lambda *a: None, got.append)
            time.sleep(0.05)
            c.cancel_setup()
            settle(c)
        self.assertEqual(got[0]["failed"]["step"], "cancelled")

    def test_setup_engine_crash_is_reported_not_raised(self):
        c = self.ctl()
        got = []
        with mock.patch.object(c, "setup_engine", side_effect=RuntimeError("boom")):
            c.run_setup(None, lambda *a: None, got.append)
            settle(c)
        self.assertEqual(got[0]["failed"]["step"], "unexpected")


class DiagnosticReport(AppCase):
    def test_report_has_hardware_acceleration_and_field_list(self):
        d = diagnostics.collect(self.cfg)
        for k in ("hardware", "runtime", "acceleration", "tier", "setup", "fields_included"):
            self.assertIn(k, d)
        self.assertFalse(d["acceleration"]["acceleration_verified"])
        self.assertTrue(any("NOT included" in x for x in d["fields_included"]))

    def test_saved_gpu_choice_alone_does_not_claim_verification_without_backend(self):
        accel.save(self.cfg.models_dir, -1, verified=True)
        self.assertFalse(diagnostics.collect(self.cfg)["acceleration"]["acceleration_verified"])     # this PC has no GPU backend

    def test_verified_requires_saved_measurement_and_backend(self):
        accel.save(self.cfg.models_dir, -1, verified=True)
        with mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value={"available": True, "gpu_offload_supported": True}), \
             mock.patch("jarvis.hardware.detect_hardware", return_value=__import__("jarvis.hardware", fromlist=["Hardware"]).Hardware("W", 8, 32.0, 100.0, "RTX", 12.0)):
            self.assertTrue(diagnostics.collect(self.cfg)["acceleration"]["acceleration_verified"])
        accel.save(self.cfg.models_dir, -1, verified=False)
        with mock.patch("jarvis.inference.gguf_engine.runtime_info", return_value={"available": True, "gpu_offload_supported": True}):
            self.assertFalse(diagnostics.collect(self.cfg)["acceleration"]["acceleration_verified"])

    def test_export_contains_no_conversation_or_secret(self):
        (self.cfg.data_dir / "crash.log").write_text("api_key=sk-abcdefghijklmnopqrstuvwx1234567890 at C:\\Users\\Someone\\x")
        out = Path(tempfile.mkdtemp()) / "sub" / "r.json"
        diagnostics.export_report(self.cfg, out)
        text = out.read_text()
        json.loads(text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx1234567890", text)
        for k in ("conversations", "memories", "messages", "transcript"):
            self.assertNotIn(k, json.loads(text))

    def test_export_unwritable_destination_raises_oserror(self):
        with self.assertRaises(OSError):
            diagnostics.export_report(self.cfg, self.cfg.data_dir)      # a directory, not a file


class StaticChecks(unittest.TestCase):
    ISS = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    DLG = (ROOT / "src" / "jarvis" / "ui" / "setup_dialog.py").read_text(encoding="utf-8")
    APP = (ROOT / "src" / "jarvis" / "ui" / "app.py").read_text(encoding="utf-8")

    def test_installer_offers_to_launch_the_gui_wizard_not_a_console(self):
        self.assertIn('Filename: "{app}\\jarvis-gui.exe"; Description: "Launch Project JARVIS', self.ISS)
        self.assertNotIn("Open the setup guide", self.ISS)

    def test_installer_does_not_bundle_model_weights(self):
        self.assertNotIn(".gguf", self.ISS)

    def test_gui_opens_wizard_only_when_setup_needed(self):
        self.assertIn("if ctl.needs_setup():", self.APP)
        self.assertLess(self.APP.index("needs_setup"), self.APP.index("voice_autostart"))

    def test_wizard_window_compiles_and_has_cancel_retry_and_later(self):
        compile(self.DLG, "setup_dialog.py", "exec")
        for w in ("Cancel", "Retry", "Set up later", "Launch Jarvis", "pretrained third-party model", "CPU fallback"):
            self.assertIn(w, self.DLG)

    def test_wizard_never_marks_simulated_run_complete_in_ui(self):
        self.assertIn("simulated run and is not recorded as complete", self.DLG)

    def test_wizard_has_no_network_code_of_its_own(self):
        for bad in ("urllib", "requests", "socket", "http"):
            self.assertNotIn(f"import {bad}", self.DLG)


class SetupAutoCli(unittest.TestCase):
    def run_cli(self, *argv, run_result=None):
        import contextlib, io
        from jarvis import cli
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if run_result is None:
                rc = cli.main(["--data-dir", t, "setup-auto", *argv])
            else:
                with mock.patch.object(SE.SetupEngine, "run", return_value=run_result):
                    rc = cli.main(["--data-dir", t, "setup-auto", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_without_consent_nothing_is_downloaded_and_exit_is_nonzero(self):
        from jarvis.models.manager import ModelStore
        with mock.patch.object(ModelStore, "download_gguf", side_effect=AssertionError("must not download without --accept-download")), \
             mock.patch.object(SE.SetupEngine, "check_system", return_value={"can_continue": True, "warnings": [], "blockers": []}):
            rc, out, err = self.run_cli()
        self.assertEqual(rc, 3)

    def test_exit_zero_only_when_setup_completed_for_real(self):
        self.assertEqual(self.run_cli(run_result={"completed": True, "real": True})[0], 0)
        self.assertEqual(self.run_cli(run_result={"completed": False, "real": False})[0], 3)
        self.assertEqual(self.run_cli(run_result={"completed": False, "failed": {"step": "verify", "message": "x"}})[0], 3)


if __name__ == "__main__":
    unittest.main()
