"""Static guards for the tray/background code (real Qt is not available in the authoring sandbox; the Windows CI crashed when a test window hid itself to a tray)."""
import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "ui"


class TrayGuards(unittest.TestCase):
    def test_no_tray_icon_is_created_while_the_window_is_built(self):
        s = (SRC / "background_ui.py").read_text(encoding="utf-8")
        init = s[s.index("class BackgroundManager"):s.index("def _ensure_tray")]
        self.assertNotIn("QSystemTrayIcon(", init)

    def test_tray_mode_only_exists_when_the_application_turned_quit_on_last_window_off(self):
        s = (SRC / "background_ui.py").read_text(encoding="utf-8")
        self.assertIn("QApplication.quitOnLastWindowClosed()", s)
        w = (SRC / "window.py").read_text(encoding="utf-8")
        self.assertRegex(w, r"if not QApplication\.quitOnLastWindowClosed\(\):\s*\n\s*QTimer\.singleShot\(0, QApplication\.quit\)")
        self.assertIn("setQuitOnLastWindowClosed(False)", (SRC / "app.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
