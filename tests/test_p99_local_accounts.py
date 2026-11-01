"""Local accounts (Log in / Sign up on this PC, no server). Real files in a temp folder; no network; no Qt."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.online import localaccounts as LA  # noqa: E402

GOOD = "correct horse battery"


class Accounts(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        p = mock.patch.dict(LA.SCRYPT, {"n": 2 ** 10})                    # fast hashing for the tests; the real cost is 2**15
        p.start()
        self.addCleanup(p.stop)
        self.a = LA.LocalAccounts(Path(self.t.name))

    def signup(self, name="alex", pw=GOOD, **kw):
        return self.a.sign_up(name, kw.get("display", "Alex"), pw, kw.get("pw2", pw), kw.get("ok", True), kw.get("stay", False))

    def test_sign_up_signs_in_and_shows_a_recovery_code_once(self):
        r = self.signup()
        self.assertTrue(r.ok, r.message)
        self.assertRegex(r.data["recovery_code"], r"^[A-Z2-7]{4}(-[A-Z2-7]{4}){3}$")
        self.assertEqual(self.a.current()["display"], "Alex")
        self.assertNotIn(r.data["recovery_code"].replace("-", ""), (Path(self.t.name) / LA.FILE).read_text())

    def test_passwords_and_codes_are_stored_only_as_hashes(self):
        r = self.signup()
        raw = (Path(self.t.name) / LA.FILE).read_text()
        self.assertNotIn(GOOD, raw)
        self.assertIn("scrypt$", raw)

    def test_the_rules_are_the_same_as_the_online_ones(self):
        self.assertIn("username", self.a.validate_signup("A!", "", GOOD, GOOD, True))
        self.assertIn("password", self.a.validate_signup("alex", "", "short", "short", True))
        self.assertIn("password2", self.a.validate_signup("alex", "", GOOD, GOOD + "x", True))
        self.assertIn("accepted", self.a.validate_signup("alex", "", GOOD, GOOD, False))
        self.assertEqual(self.a.validate_signup("alex", "", GOOD, GOOD, True), {})

    def test_duplicate_username_is_refused_case_insensitively(self):
        self.signup()
        self.assertEqual(self.signup("ALEX").code, "invalid")

    def test_log_in_log_out(self):
        self.signup()
        self.a.sign_out()
        self.assertIsNone(self.a.current())
        r = self.a.sign_in("Alex", GOOD)
        self.assertTrue(r.ok)
        self.assertEqual(self.a.current()["user"], "alex")

    def test_wrong_password_and_unknown_user_give_the_same_message(self):
        self.signup()
        self.a.sign_out()
        a = self.a.sign_in("alex", "wrong password!!")
        b = self.a.sign_in("nobody", "whatever password")
        self.assertEqual((a.code, a.message), (b.code, b.message))

    def test_five_failures_lock_the_account_for_a_while(self):
        self.signup()
        self.a.sign_out()
        for _ in range(LA.MAX_FAILURES):
            self.a.sign_in("alex", "wrong password!!")
        r = self.a.sign_in("alex", GOOD)
        self.assertFalse(r.ok)
        with mock.patch("time.time", return_value=__import__("time").time() + LA.LOCK_SECONDS + 5):
            self.assertTrue(self.a.sign_in("alex", GOOD).ok)

    def test_stay_signed_in_survives_a_restart_and_log_out_ends_it(self):
        self.signup(stay=True)
        again = LA.LocalAccounts(Path(self.t.name))
        self.assertEqual(again.current()["user"], "alex")
        again.sign_out()
        self.assertIsNone(LA.LocalAccounts(Path(self.t.name)).current())

    def test_without_stay_signed_in_a_restart_signs_out(self):
        self.signup(stay=False)
        self.assertIsNone(LA.LocalAccounts(Path(self.t.name)).current())

    def test_a_forged_session_file_does_not_sign_anyone_in(self):
        self.signup()
        (Path(self.t.name) / LA.SESSION_FILE).write_text(json.dumps({"user": "alex", "token": "forged"}))
        self.assertIsNone(LA.LocalAccounts(Path(self.t.name)).current())

    def test_change_password_needs_the_current_one(self):
        self.signup()
        self.assertFalse(self.a.change_password("nope nope nope", "another long pass", "another long pass").ok)
        self.assertTrue(self.a.change_password(GOOD, "another long pass", "another long pass").ok)
        self.a.sign_out()
        self.assertFalse(self.a.sign_in("alex", GOOD).ok)
        self.assertTrue(self.a.sign_in("alex", "another long pass").ok)

    def test_recovery_code_resets_the_password_once_and_gives_a_new_code(self):
        code = self.signup().data["recovery_code"]
        self.a.sign_out()
        self.assertFalse(self.a.recover("alex", "AAAA-AAAA-AAAA-AAAA", "brand new password", "brand new password").ok)
        r = self.a.recover("alex", code.lower(), "brand new password", "brand new password")
        self.assertTrue(r.ok, r.message)
        self.assertNotEqual(r.data["recovery_code"], code)
        self.assertFalse(self.a.recover("alex", code, "yet another password", "yet another password").ok)       # the old code is spent
        self.assertTrue(self.a.sign_in("alex", "brand new password").ok)

    def test_delete_needs_the_password_and_removes_only_the_account(self):
        self.signup()
        self.assertFalse(self.a.delete_account("wrong password!!").ok)
        self.assertTrue(self.a.delete_account(GOOD).ok)
        self.assertFalse(self.a.has_accounts())
        self.assertIsNone(self.a.current())

    def test_guest_sessions_cannot_make_or_use_accounts(self):
        g = LA.LocalAccounts(Path(self.t.name), guest=True)
        self.assertEqual(g.sign_up("alex", "", GOOD, GOOD, True).code, "guest")
        self.assertEqual(g.sign_in("alex", GOOD).code, "guest")
        self.assertFalse((Path(self.t.name) / LA.FILE).exists())

    def test_no_network_and_no_assistant_imports(self):
        src = Path(LA.__file__).read_text(encoding="utf-8")
        for bad in ("import socket", "urllib", "http.client", "requests", "jarvis.assistant", "jarvis.inference", "jarvis.memory"):
            self.assertNotIn(bad, src)

    def test_the_account_page_has_no_address_or_connect_screen(self):
        src = (Path(__file__).resolve().parents[1] / "src" / "jarvis" / "ui" / "local_account_page.py").read_text(encoding="utf-8").lower()
        for bad in ("https://", "http://", "accountcontroller", "connect_btn", "urlsplit", "qurl"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
