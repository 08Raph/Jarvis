"""The startup Log in / Sign up window: when it opens (logic, no Qt) and how it is wired (source checks; the window itself is built by the Windows self-test)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis.online.localaccounts import LocalAccounts

GOOD = "Sup3r-Secret-pass!"


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def acc(self, guest=False):
        return LocalAccounts(self.dir, guest=guest)

    def signup(self, stay):
        r = self.acc().sign_up("sam_01", "Sam", GOOD, GOOD, True, stay=stay)
        self.assertTrue(r.ok, r.message)

    def test_fresh_pc_asks(self):
        self.assertTrue(self.acc().should_welcome())

    def test_kept_sign_in_never_asks(self):
        self.signup(stay=True)
        self.assertFalse(self.acc().should_welcome())                  # a new LocalAccounts = a new start of Jarvis

    def test_not_kept_asks_again_next_start(self):
        self.signup(stay=False)
        self.assertTrue(self.acc().should_welcome())

    def test_login_with_stay_then_restart(self):
        self.signup(stay=False)
        a = self.acc()
        self.assertTrue(a.sign_in("sam_01", GOOD, stay=True).ok)
        self.assertFalse(self.acc().should_welcome())

    def test_logout_asks_again(self):
        self.signup(stay=True)
        a = self.acc()
        self.assertFalse(a.should_welcome())
        a.current()
        a.sign_out()
        self.assertTrue(self.acc().should_welcome())

    def test_dont_ask_again_and_turning_it_back_on(self):
        a = self.acc()
        a.set_ask_at_start(False)
        self.assertFalse(self.acc().should_welcome())
        a.set_ask_at_start(True)
        self.assertTrue(self.acc().should_welcome())

    def test_guest_never_asks_and_writes_nothing(self):
        g = self.acc(guest=True)
        g.set_ask_at_start(True)
        self.assertFalse(g.should_welcome())
        self.assertFalse((self.dir / "welcome.json").exists())

    def test_damaged_setting_file_asks(self):
        (self.dir / "welcome.json").write_text("{nope")
        self.assertTrue(self.acc().should_welcome())


class Wiring(unittest.TestCase):
    APP = (ROOT / "src/jarvis/ui/app.py").read_text(encoding="utf-8")
    DLG = (ROOT / "src/jarvis/ui/welcome_dialog.py").read_text(encoding="utf-8")
    PAGE = (ROOT / "src/jarvis/ui/local_account_page.py").read_text(encoding="utf-8")

    def test_login_window_comes_before_the_model_setup_and_never_in_simulation(self):
        self.assertIn("should_welcome()", self.APP)
        self.assertLess(self.APP.index("should_welcome()"), self.APP.index("if ctl.needs_setup():"))
        self.assertIn("not simulate and win.account_ctl.should_welcome()", self.APP)

    def test_selftest_builds_the_window(self):
        self.assertIn("WelcomeDialog(win.account_ctl, win)", self.APP)

    def test_dialog_closes_on_success_and_can_be_skipped(self):
        self.assertIn("self.accept()", self.DLG)
        self.assertIn("Continue without an account", self.DLG)
        self.assertIn("Don't ask me again", self.DLG)
        self.assertIn("compact=True", self.DLG)

    def test_startup_defaults_to_staying_signed_in(self):
        self.assertIn("self.li_stay.setChecked(self.compact)", self.PAGE)
        self.assertIn("self.su_stay.setChecked(self.compact)", self.PAGE)

    def test_no_address_or_link_anywhere(self):
        for src in (self.DLG, self.PAGE):
            for bad in ("http://", "https://", "QUrl", "urlsplit", "AccountController", "connect_btn"):
                self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
