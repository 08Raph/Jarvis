"""The Account page and the main window with REAL Qt on its OFFSCREEN platform. Runs only where PySide6 is installed (the Windows CI job has it; the authoring sandbox does not,
so these tests were never run by the author before the first CI run: a failure is information).

REAL: the widgets, clicks (QPushButton.click / QCheckBox.setChecked / QLineEdit.setText), the account controller, the Jarvis Online service code, SQLite, HTTP over 127.0.0.1.
FAKE (labelled): scrypt cost lowered; the dialogs (FakeDialogs) answer scripted values so no modal window blocks a headless run; the work runner is synchronous in most tests
(one test uses the real worker-thread path). WHAT THIS CANNOT SHOW: how the page looks to a person, DPI scaling, a screen reader, or use on a real PC."""
import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT / "src"), str(ROOT / "server"), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

HAVE_QT = importlib.util.find_spec("PySide6") is not None
if HAVE_QT:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PW = "correct horse battery"
PW2 = "another long passphrase 42"


class FakeDialogs:
    def __init__(self):
        self.answers = {"confirm": True, "password": PW, "text": "New Name", "recover": None, "change_password": None}
        self.seen = []

    def confirm(self, title, text, yes="Yes", default_no=True):
        self.seen.append(("confirm", title, text)); return self.answers["confirm"]

    def password(self, title, text):
        self.seen.append(("password", title)); return self.answers["password"]

    def text(self, title, text, initial=""):
        self.seen.append(("text", title)); return self.answers["text"]

    def show_text(self, title, body): self.seen.append(("show_text", title, body))
    def info(self, title, text): self.seen.append(("info", title, text))
    def recovery_code(self, code): self.seen.append(("recovery_code", code))
    def recover(self, username): self.seen.append(("recover", username)); return self.answers["recover"]
    def change_password(self): self.seen.append(("change_password",)); return self.answers["change_password"]

    def kinds(self): return [s[0] for s in self.seen]


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed: the real-Qt account page tests are skipped")
class RealQt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        from jarvis_server import security as sec
        from jarvis_server.__main__ import make_handler
        from jarvis_server.app import Service
        from jarvis_server.db import DB
        self._real = (sec.SCRYPT_N, sec.SCRYPT_R, sec.SCRYPT_P)
        sec.SCRYPT_N, sec.SCRYPT_R, sec.SCRYPT_P = 2 ** 10, 8, 1                          # FAKE: cheap hashing
        self.addCleanup(lambda: setattr(sec, "SCRYPT_N", self._real[0]) or setattr(sec, "SCRYPT_R", self._real[1]) or setattr(sec, "SCRYPT_P", self._real[2]))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "server.db")
        self.addCleanup(self.db.close)
        self.svc = Service(self.db, terms_text="Terms used by the page tests.")
        self.svc.limiter.allow = lambda *a, **k: True                                     # FAKE: no abuse limits (they have their own tests)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.svc, False))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.n = 0

    def page(self, guest=False, run="sync", url_connected=True):
        from jarvis.online import account as A
        from jarvis.online.tokenstore import TokenStore
        from jarvis.ui.account_page import AccountPage
        self.n += 1
        d = Path(self.tmp.name) / f"pc{self.n}"
        d.mkdir()
        acct = A.AccountController(d, d / "settings.json", tokens=A._Tokens(d / "online.token", TokenStore(d / "online.token", windows=False)), timeout=3.0)
        if url_connected:
            acct.connect(self.url, confirmed=True, local_http=True)
        dlg = FakeDialogs()
        kw = dict(run=lambda fn, done: done(fn())) if run == "sync" else {}
        pg = AccountPage(acct, guest=guest, dialogs=dlg, **kw)
        self.addCleanup(pg.deleteLater)
        pg.show()
        self.app.processEvents()
        return pg, acct, dlg

    def wait_idle(self, pg, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if not pg.busy:
                return True
            time.sleep(0.01)
        return False

    def fill_signup(self, pg, name="alice", pw=PW, pw2=None, terms=True, age=True, display=""):
        pg.su_user.setText(name); pg.su_display.setText(display); pg.su_pass.setText(pw); pg.su_pass2.setText(pw if pw2 is None else pw2)
        pg.su_terms.setChecked(terms); pg.su_age.setChecked(age)

    def register(self, pg, name="alice"):
        pg.show_signup(True)
        self.fill_signup(pg, name=name)
        pg.su_btn.click()

    # ------------------------------------------------------------ navigation
    def test_not_connected_shows_the_connect_screen_and_the_local_note(self):
        pg, acct, _ = self.page(url_connected=False)
        self.assertEqual(pg.stack.currentIndex(), 0)
        self.assertIn("works without an account", pg.local_note.text())
        self.assertTrue(pg.findChild(type(pg.si_btn), "connect_btn").isEnabled())

    def test_connecting_asks_first_and_declining_changes_nothing(self):
        pg, acct, dlg = self.page(url_connected=False)
        pg.url.setText(self.url); pg.local_http.setChecked(True)
        dlg.answers["confirm"] = False
        pg.findChild(type(pg.si_btn), "connect_btn").click()
        self.assertFalse(acct.state()["connected"])
        self.assertEqual(pg.stack.currentIndex(), 0)
        dlg.answers["confirm"] = True
        pg.findChild(type(pg.si_btn), "connect_btn").click()
        self.assertTrue(acct.state()["connected"])
        self.assertEqual(pg.stack.currentIndex(), 1)

    def test_https_is_required_for_a_remote_address(self):
        pg, acct, _ = self.page(url_connected=False)
        pg.url.setText("http://example.com")
        pg.findChild(type(pg.si_btn), "connect_btn").click()
        self.assertFalse(acct.state()["connected"])
        self.assertIn("https://", pg.status.text())

    def test_sign_in_and_sign_up_screens_link_to_each_other(self):
        pg, _, _ = self.page()
        self.assertEqual(pg.stack.currentIndex(), 1)
        pg.findChild(type(pg.si_btn), "si_to_signup").click()
        self.assertEqual(pg.stack.currentIndex(), 2)
        pg.findChild(type(pg.si_btn), "su_to_signin").click()
        self.assertEqual(pg.stack.currentIndex(), 1)

    def test_show_password_toggles_the_echo_mode(self):
        from PySide6.QtWidgets import QLineEdit
        pg, _, _ = self.page()
        self.assertEqual(pg.si_pass.echoMode(), QLineEdit.EchoMode.Password)
        pg.si_show.setChecked(True)
        self.assertEqual(pg.si_pass.echoMode(), QLineEdit.EchoMode.Normal)
        pg.si_show.setChecked(False)
        self.assertEqual(pg.si_pass.echoMode(), QLineEdit.EchoMode.Password)
        pg.su_show.setChecked(True)
        self.assertEqual((pg.su_pass.echoMode(), pg.su_pass2.echoMode()), (QLineEdit.EchoMode.Normal,) * 2)

    def test_remember_me_is_disabled_with_a_reason_where_the_token_cannot_be_encrypted(self):
        pg, _, _ = self.page()
        self.assertFalse(pg.si_remember.isEnabled())
        self.assertIn("encrypt", pg.si_remember.toolTip())

    # ------------------------------------------------------------ sign up
    def test_sign_up_through_the_widgets_creates_a_real_account_and_shows_the_recovery_code(self):
        pg, acct, dlg = self.page()
        self.register(pg)
        self.assertEqual(pg.stack.currentIndex(), 3)
        self.assertIn("alice", pg.ac_user.text())
        self.assertTrue(acct.state()["signed_in"])
        code = [s for s in dlg.seen if s[0] == "recovery_code"]
        self.assertEqual(len(code), 1)
        self.assertRegex(code[0][1], r"^[0-9A-F]{5}(-[0-9A-F]{5}){3}$")
        self.assertEqual(pg.su_pass.text(), "")                                              # the password boxes are emptied
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM users")[0], 1)

    def test_each_sign_up_problem_is_shown_on_its_own_field(self):
        pg, _, _ = self.page()
        pg.show_signup(True)
        self.fill_signup(pg, name="a b", pw="short", pw2="other", terms=False, age=False)
        pg.su_btn.click()
        for key in ("username", "password", "password2", "terms", "age"):
            self.assertNotEqual(pg.su_err[key].text(), "", key)
        self.assertEqual(pg.stack.currentIndex(), 2)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM users")[0], 0)

    def test_password_mismatch_and_weak_password_messages(self):
        pg, _, _ = self.page()
        pg.show_signup(True)
        self.fill_signup(pg, pw2=PW + "x")
        pg.su_btn.click()
        self.assertIn("do not match", pg.su_err["password2"].text())
        self.fill_signup(pg, pw="password1")
        pg.su_btn.click()
        self.assertIn("easy to guess", pg.su_err["password"].text())

    def test_an_existing_username_is_reported_on_the_username_field(self):
        pg, _, _ = self.page()
        self.register(pg)
        pg2, _, _ = self.page()
        self.register(pg2)
        self.assertEqual(pg2.stack.currentIndex(), 2)
        self.assertIn("not available", pg2.su_err["username"].text())

    def test_the_terms_button_loads_the_real_text_from_the_service(self):
        pg, _, dlg = self.page()
        pg.show_signup(True)
        pg.findChild(type(pg.si_btn), "su_terms_read").click()
        shown = [s for s in dlg.seen if s[0] == "show_text"]
        self.assertEqual(shown[0][2], "Terms used by the page tests.")

    # ------------------------------------------------------------ sign in / out
    def test_sign_in_with_the_right_password_then_sign_out_after_confirming(self):
        pg, acct, dlg = self.page()
        self.register(pg)
        pg.findChild(type(pg.si_btn), "ac_signout").click()
        self.assertEqual(pg.stack.currentIndex(), 1)
        self.assertFalse(acct.state()["signed_in"])
        pg.si_user.setText("alice"); pg.si_pass.setText(PW)
        pg.si_btn.click()
        self.assertEqual(pg.stack.currentIndex(), 3)
        self.assertEqual(pg.si_pass.text(), "")
        self.assertIn("confirm", dlg.kinds())

    def test_declining_the_sign_out_confirmation_keeps_the_session(self):
        pg, acct, dlg = self.page()
        self.register(pg)
        dlg.answers["confirm"] = False
        pg.findChild(type(pg.si_btn), "ac_signout").click()
        self.assertTrue(acct.state()["signed_in"])
        self.assertEqual(pg.stack.currentIndex(), 3)

    def test_invalid_credentials_show_one_generic_message_and_clear_the_password(self):
        pg, acct, _ = self.page()
        pg.si_user.setText("nobody"); pg.si_pass.setText("wrong-password-1")
        pg.si_btn.click()
        self.assertIn("Wrong username or password", pg.si_err.text())
        self.assertEqual(pg.si_pass.text(), "")
        self.assertEqual(pg.stack.currentIndex(), 1)
        self.assertFalse(acct.state()["signed_in"])

    def test_empty_sign_in_is_flagged(self):
        pg, _, _ = self.page()
        pg.si_btn.click()
        self.assertEqual(pg.stack.currentIndex(), 1)
        self.assertNotEqual(pg.status.text(), "")

    def test_server_unavailable_and_the_buttons_come_back(self):
        pg, acct, _ = self.page()
        self.httpd.shutdown(); self.httpd.server_close()
        pg.si_user.setText("alice"); pg.si_pass.setText(PW)
        pg.si_btn.click()
        self.assertIn("could not be reached", pg.si_err.text())
        self.assertTrue(pg.si_btn.isEnabled())                                               # not stuck in the busy state
        self.assertFalse(pg.busy)

    def test_session_expiry_shows_the_banner_and_the_sign_in_screen(self):
        pg, acct, _ = self.page()
        self.register(pg)
        self.db.q("DELETE FROM sessions")
        pg.check_session()
        self.assertEqual(pg.stack.currentIndex(), 1)
        self.assertTrue(pg.banner.isVisible())
        self.assertIn("expired", pg.banner.text())

    def test_the_real_worker_thread_path_finishes_and_updates_the_page(self):
        pg, acct, _ = self.page(run="thread")
        pg.show_signup(True)
        self.fill_signup(pg)
        pg.su_btn.click()
        self.assertTrue(pg.busy)                                                             # busy right after the click: buttons are locked
        self.assertFalse(pg.su_btn.isEnabled())
        self.assertTrue(self.wait_idle(pg))
        self.assertEqual(pg.stack.currentIndex(), 3)
        self.assertTrue(pg.findChild(type(pg.si_btn), "ac_signout").isEnabled())

    # ------------------------------------------------------------ account management
    def test_rename_change_password_renew_and_delete(self):
        pg, acct, dlg = self.page()
        self.register(pg)
        dlg.answers["text"] = "Ally"
        pg.findChild(type(pg.si_btn), "ac_rename").click()
        self.assertIn("Ally", pg.ac_name.text())
        dlg.answers["change_password"] = (PW, PW2, PW2)
        pg.findChild(type(pg.si_btn), "ac_change_pw").click()
        self.assertIn("Password changed", pg.status.text())
        pg.findChild(type(pg.si_btn), "ac_renew").click()
        self.assertIn("renewed", pg.status.text())
        dlg.answers["password"] = "not-the-password"
        pg.findChild(type(pg.si_btn), "ac_delete").click()
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM users")[0], 1)                    # wrong password: nothing deleted
        dlg.answers["password"] = PW2
        pg.findChild(type(pg.si_btn), "ac_delete").click()
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM users")[0], 0)
        self.assertEqual(pg.stack.currentIndex(), 1)

    def test_declining_the_delete_warning_deletes_nothing(self):
        pg, _, dlg = self.page()
        self.register(pg)
        dlg.answers["confirm"] = False
        pg.findChild(type(pg.si_btn), "ac_delete").click()
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM users")[0], 1)
        self.assertNotIn("password", dlg.kinds())                                            # not even asked for the password

    def test_forgot_password_explains_and_resets_with_the_recovery_code(self):
        pg, acct, dlg = self.page()
        self.register(pg)
        code = [s for s in dlg.seen if s[0] == "recovery_code"][0][1]
        pg.findChild(type(pg.si_btn), "ac_signout").click()
        dlg.answers["recover"] = ("alice", code, PW2, PW2)
        pg.findChild(type(pg.si_btn), "si_forgot").click()
        self.assertIn("Password reset", pg.status.text())
        pg.si_user.setText("alice"); pg.si_pass.setText(PW2)
        pg.si_btn.click()
        self.assertEqual(pg.stack.currentIndex(), 3)

    def test_disconnect_forgets_the_service(self):
        pg, acct, _ = self.page()
        pg.findChild(type(pg.si_btn), "si_disconnect").click()
        self.assertFalse(acct.state()["connected"])
        self.assertEqual(pg.stack.currentIndex(), 0)

    def test_guest_sessions_cannot_use_accounts(self):
        pg, acct, _ = self.page(guest=True)
        self.assertIn("Guest session", pg.status.text())
        self.assertFalse(pg.findChild(type(pg.si_btn), "connect_btn").isEnabled())

    # ------------------------------------------------------------ the main window
    def test_the_main_window_has_an_account_page_that_opens_without_any_network_call(self):
        from p17_common import AppCase
        from jarvis.app.controller import AppController
        from jarvis.ui import window as W

        class Case(AppCase):
            def runTest(self): pass
        case = Case(); case.setUp(); self.addCleanup(case.tearDown)
        case.install_model()
        ctl = AppController(case.cfg, simulate=True)
        self.addCleanup(ctl.shutdown, 2.0)
        win = W.MainWindow(ctl, W._Bridge())
        self.addCleanup(win.deleteLater)
        win.show(); self.app.processEvents()
        self.addCleanup(win.hide)
        self.assertEqual(win.nav.item(win.nav.count() - 1).text(), "Account")
        from unittest import mock
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("the window made a network request")):
            win.nav.setCurrentRow(W.PAGES.index("Account"))
            win.nav.setCurrentRow(0)
            win.nav.setCurrentRow(W.PAGES.index("Account"))
            win.refresh_all()
            self.app.processEvents()
        self.assertEqual(win.account_page.stack.currentIndex(), 0)                           # nothing configured: the connect screen


if __name__ == "__main__":
    unittest.main()
