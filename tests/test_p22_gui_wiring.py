"""Static regression checks for the Stage 4A GUI test wiring (Linux cannot run Qt or Windows UI Automation; these only prove the pieces stay wired)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(p):
    return (ROOT / p).read_text(encoding="utf-8")


class GuiWiring(unittest.TestCase):
    def test_script_uses_real_clicks_not_internal_calls(self):
        s = read("scripts/ci_gui_wizard_test.ps1")
        for needle in ("mouse_event", "SetCursorPos", "System.Windows.Automation", "SendKeys", "CopyFromScreen"):
            self.assertIn(needle, s)
        for banned in ("setup-auto", "run_setup", "InvokePattern"):
            self.assertNotIn(banned, s)

    def test_script_covers_required_scenarios(self):
        s = read("scripts/ci_gui_wizard_test.ps1")
        for sc in ("S1_details_consent_and_set_up_later", "S2_cancel_button", "S3_failure_retry_cancel_close",
                   "S4_full_setup_then_question", "S5_reopen_after_success", "S6_integrity_failure_then_retry"):
            self.assertIn(sc, s)
        self.assertIn("before consent", s)
        self.assertIn("Setup finished", s)

    def test_script_no_secrets(self):
        s = read("scripts/ci_gui_wizard_test.ps1")
        self.assertIsNone(re.search(r"(ghp_|hf_[A-Za-z0-9]{10}|Bearer )", s))

    def test_workflow_has_gui_and_perf_steps(self):
        w = read(".github/workflows/build-windows.yml")
        self.assertIn("ci_gui_wizard_test.ps1", w)
        self.assertIn("perf --out ci-logs", w)
        self.assertIn("reuse_installer_run_id", w)
        self.assertIn("steps.gui.outcome", w)
        self.assertIn("ci_real_model_test.ps1", w)  # Stage 3 evidence step kept

    def test_wizard_close_cancels_running_setup(self):
        s = read("src/jarvis/ui/setup_dialog.py")
        self.assertIn("def reject(self)", s)
        m = re.search(r"def reject\(self\):(.*?)\n    def ", s, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("self.ctl.cancel_setup()", body)
        self.assertIn("_close_after_cancel", body)
        self.assertIn("_close_after_cancel", s.split("def _done", 1)[1])


if __name__ == "__main__":
    unittest.main()
