"""The Account page: connect to a Jarvis Online service, create an account, sign in, sign out, manage the account. A THIN view over
jarvis.online.account.AccountController (all rules and every network call live there and are tested without Qt).

Honest scope, also printed on the page: an account is OPTIONAL and only for the Jarvis Online service. The local assistant (chat, memory, voice, tools) never needs it,
never waits for it and never sends anything to it. No hosted service exists; the person must enter the address of one they run or trust.
STATUS: written without PySide6 in the authoring sandbox; the real-Qt tests (tests/test_p77_account_page_qt.py) run on the Windows CI job (offscreen). Nobody has used
this page on a real PC yet.
Rule: every visible control either performs the action it says or is disabled with a tooltip that says why."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QStackedWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from jarvis.online import rules
from jarvis.online.account import LOCAL_NOTE, AccountController, Result

CONNECT, SIGNIN, SIGNUP, ACCOUNT = range(4)


def _label(text="", *, muted=False, heading=False, name=""):
    l = QLabel(text)
    l.setWordWrap(True)
    l.setProperty("muted", muted)
    l.setProperty("heading", heading)
    if name:
        l.setObjectName(name)
        l.setAccessibleName(name.replace("_", " "))
    l.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return l


def _btn(text, slot, *, name, primary=False, danger=False, tip=""):
    b = QPushButton(text)
    b.setObjectName(name)
    b.setProperty("primary", primary)
    b.setProperty("danger", danger)
    b.setToolTip(tip)
    b.setAccessibleName(text.replace("&", "").rstrip(".… "))
    b.clicked.connect(lambda _checked=False: slot())
    return b


def _pw_field(name, accessible):
    e = QLineEdit()
    e.setEchoMode(QLineEdit.EchoMode.Password)
    e.setObjectName(name)
    e.setAccessibleName(accessible)
    e.setMaxLength(400)
    return e


def _text_field(name, accessible, placeholder="", maxlen=300):
    e = QLineEdit()
    e.setObjectName(name)
    e.setAccessibleName(accessible)
    e.setPlaceholderText(placeholder)
    e.setMaxLength(maxlen)
    return e


class _Mainthread(QObject):
    """Runs a function on the interface thread when emitted from a worker thread."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run)

    @Slot(object)
    def _run(self, fn):
        fn()


class Dialogs:
    """Every question the page asks the person. Replaced in tests so no modal window ever blocks a test."""

    def __init__(self, parent):
        self.parent = parent

    def confirm(self, title, text, yes="Yes", default_no=True) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self.parent)
        box.button(QMessageBox.StandardButton.Yes).setText(yes)
        box.setDefaultButton(QMessageBox.StandardButton.No if default_no else QMessageBox.StandardButton.Yes)
        return box.exec() == QMessageBox.StandardButton.Yes

    def password(self, title, text) -> str | None:
        pw, ok = QInputDialog.getText(self.parent, title, text, QLineEdit.EchoMode.Password)
        return pw if ok else None

    def text(self, title, text, initial="") -> str | None:
        v, ok = QInputDialog.getText(self.parent, title, text, QLineEdit.EchoMode.Normal, initial)
        return v if ok else None

    def show_text(self, title, body):
        dlg = QDialog(self.parent)
        dlg.setWindowTitle(title)
        dlg.resize(640, 520)
        v = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setObjectName("policy_text")
        tb.setAccessibleName(title)
        tb.setPlainText(body)
        v.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.exec()

    def info(self, title, text):
        QMessageBox.information(self.parent, title, text)

    def recovery_code(self, code):
        box = QMessageBox(QMessageBox.Icon.Information, "Save your recovery code",
                          "Your account was created.\n\nYOUR RECOVERY CODE (shown only once):\n\n    " + code +
                          "\n\nWrite it down somewhere safe. Without it a forgotten password cannot be recovered: there is no email service. "
                          "Jarvis does not store it.", QMessageBox.StandardButton.Ok, self.parent)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.button(QMessageBox.StandardButton.Ok).setText("I have saved it")
        box.exec()

    def recover(self, username) -> tuple | None:
        dlg = QDialog(self.parent)
        dlg.setWindowTitle("Forgot password")
        v = QVBoxLayout(dlg)
        v.addWidget(_label("Jarvis has NO email service, so it cannot send you a reset link. A forgotten password can be reset only with the recovery code shown when "
                           "the account was created. If you did not save it, the account cannot be recovered; you can create a new one.", muted=True))
        form = QFormLayout()
        u = _text_field("rc_user", "Username", maxlen=64)
        u.setText(username)
        c = _text_field("rc_code", "Recovery code", "XXXXX-XXXXX-XXXXX-XXXXX", 64)
        p1, p2 = _pw_field("rc_pass", "New password"), _pw_field("rc_pass2", "Repeat new password")
        for lab, w in (("Username", u), ("Recovery code", c), ("New password", p1), ("Repeat new password", p2)):
            form.addRow(lab, w)
        v.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Reset password")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return u.text(), c.text(), p1.text(), p2.text()

    def change_password(self) -> tuple | None:
        dlg = QDialog(self.parent)
        dlg.setWindowTitle("Change password")
        v = QVBoxLayout(dlg)
        v.addWidget(_label(rules.PASSWORD_REQUIREMENTS, muted=True))
        form = QFormLayout()
        a, b, c = _pw_field("cp_old", "Current password"), _pw_field("cp_new", "New password"), _pw_field("cp_new2", "Repeat new password")
        for lab, w in (("Current password", a), ("New password", b), ("Repeat new password", c)):
            form.addRow(lab, w)
        v.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return a.text(), b.text(), c.text()


class AccountPage(QWidget):
    """`run(fn, done)` runs fn off the interface thread and calls done(result) on it. `dialogs` answers questions. Both are injectable for tests."""

    def __init__(self, account: AccountController, *, guest: bool = False, run=None, dialogs=None, parent=None):
        super().__init__(parent)
        self.account, self.guest = account, guest
        self._mt = _Mainthread()
        self.run = run or self._run_threaded
        self.dialogs = dialogs or Dialogs(self)
        self.busy = False
        self._want_signup = False
        self.setObjectName("account_page")
        self.setAccessibleName("Account page")
        outer = QVBoxLayout(self)
        outer.addWidget(_label("Account", heading=True))
        self.local_note = _label(LOCAL_NOTE, muted=True, name="local_note")
        outer.addWidget(self.local_note)
        self.banner = _label("", name="banner")
        self.banner.setVisible(False)
        outer.addWidget(self.banner)
        self.stack = QStackedWidget()
        self.stack.setObjectName("account_stack")
        outer.addWidget(self.stack, 1)
        for build in (self._build_connect, self._build_signin, self._build_signup, self._build_account):
            self.stack.addWidget(build())
        self.status = _label("", name="status")
        self.status.setAccessibleDescription("Result of the last account action")
        outer.addWidget(self.status)
        self.refresh_view()

    # ------------------------------------------------------------------ building
    def _build_connect(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(_label("Connect to a Jarvis Online service", heading=True))
        v.addWidget(_label("There is no built-in or hosted service: enter the address of one you run or trust. Nothing is sent until you create an account or sign in, "
                           "and then only your sign-in details (and, if you use online chat, the messages you send there). Your local memory, files and microphone are never sent.", muted=True))
        self.url = _text_field("service_url", "Service address", "https://your-service.example", 300)
        v.addWidget(self.url)
        self.local_http = QCheckBox("This service runs on this same computer (development only: plain http to localhost)")
        self.local_http.setObjectName("local_http")
        v.addWidget(self.local_http)
        v.addWidget(_btn("Connect...", self.on_connect, name="connect_btn", primary=True, tip="Asks you to confirm before saving the address."))
        v.addStretch(1)
        return w

    def _build_signin(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(_label("Sign in", heading=True))
        self.si_service = _label("", muted=True, name="si_service")
        v.addWidget(self.si_service)
        form = QFormLayout()
        self.si_user = _text_field("si_user", "Username", "username", 64)
        self.si_pass = _pw_field("si_pass", "Password")
        self.si_user.returnPressed.connect(self.si_pass.setFocus)
        self.si_pass.returnPressed.connect(self.on_sign_in)
        form.addRow("Username", self.si_user)
        form.addRow("Password", self.si_pass)
        v.addLayout(form)
        self.si_show = QCheckBox("Show password"); self.si_show.setObjectName("si_show")
        self.si_show.toggled.connect(lambda on: self.si_pass.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self.si_remember = QCheckBox("Remember me on this PC"); self.si_remember.setObjectName("si_remember")
        v.addWidget(self.si_show)
        v.addWidget(self.si_remember)
        self.si_err = _label("", name="si_err")
        v.addWidget(self.si_err)
        row = QHBoxLayout()
        self.si_btn = _btn("Sign In", self.on_sign_in, name="si_btn", primary=True)
        row.addWidget(self.si_btn)
        row.addWidget(_btn("Forgot password?", self.on_forgot, name="si_forgot", tip="Explains what recovery is and is not available."))
        row.addWidget(_btn("Create account", lambda: self.show_signup(True), name="si_to_signup"))
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(_btn("Disconnect from this service", self.on_disconnect, name="si_disconnect", tip="Forgets the address and any saved sign-in on this PC."))
        v.addStretch(1)
        return w

    def _build_signup(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(_label("Create account", heading=True))
        form = QFormLayout()
        self.su_user = _text_field("su_user", "Username", "username", 64)
        self.su_display = _text_field("su_display", "Display name (optional)", "display name", 60)
        self.su_pass = _pw_field("su_pass", "Password")
        self.su_pass2 = _pw_field("su_pass2", "Confirm password")
        self.su_err: dict[str, QLabel] = {k: _label("", name=f"su_err_{k}") for k in ("username", "display_name", "password", "password2", "terms", "age")}
        rows = (("Username", self.su_user, "username", rules.USERNAME_REQUIREMENTS), ("Display name", self.su_display, "display_name", rules.DISPLAY_REQUIREMENTS),
                ("Password", self.su_pass, "password", rules.PASSWORD_REQUIREMENTS), ("Confirm password", self.su_pass2, "password2", ""))
        for lab, wid, key, hint in rows:
            col = QVBoxLayout()
            col.addWidget(wid)
            if hint:
                col.addWidget(_label(hint, muted=True))
            col.addWidget(self.su_err[key])
            form.addRow(lab, col)
        v.addLayout(form)
        self.su_show = QCheckBox("Show passwords"); self.su_show.setObjectName("su_show")
        self.su_show.toggled.connect(lambda on: [f.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password) for f in (self.su_pass, self.su_pass2)])
        v.addWidget(self.su_show)
        self.su_terms = QCheckBox("I accept the Terms and Privacy Policy"); self.su_terms.setObjectName("su_terms")
        v.addWidget(self.su_terms)
        v.addWidget(_btn("Read the Terms and Privacy Policy", self.on_read_terms, name="su_terms_read", tip="Loads the text from the service you connected to."))
        v.addWidget(self.su_err["terms"])
        self.su_age = QCheckBox("I am at least 16 years old"); self.su_age.setObjectName("su_age")
        v.addWidget(self.su_age)
        v.addWidget(self.su_err["age"])
        v.addWidget(_label("Learning from your conversations stays OFF. Creating an account does not turn it on.", muted=True))
        self.su_remember = QCheckBox("Remember me on this PC"); self.su_remember.setObjectName("su_remember")
        v.addWidget(self.su_remember)
        row = QHBoxLayout()
        self.su_btn = _btn("Create Account", self.on_sign_up, name="su_btn", primary=True)
        row.addWidget(self.su_btn)
        row.addWidget(_btn("Back to sign in", lambda: self.show_signup(False), name="su_to_signin"))
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        return w

    def _build_account(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(_label("Your account", heading=True))
        self.ac_name = _label("", name="ac_name")
        self.ac_user = _label("", name="ac_user")
        self.ac_expires = _label("", muted=True, name="ac_expires")
        for l in (self.ac_name, self.ac_user, self.ac_expires):
            v.addWidget(l)
        for text, slot, name, extra in (("Change display name...", self.on_rename, "ac_rename", {}), ("Change password...", self.on_change_password, "ac_change_pw", {}),
                                       ("Renew session", self.on_renew, "ac_renew", {"tip": "Swaps your session for a fresh one. Sessions also end by themselves."}),
                                       ("Sign out", self.on_sign_out, "ac_signout", {"primary": True}),
                                       ("Sign out everywhere", self.on_sign_out_all, "ac_signout_all", {"tip": "Ends every session of this account on every PC."}),
                                       ("Delete account...", self.on_delete, "ac_delete", {"danger": True, "tip": "Permanently deletes the account and its online data."}),
                                       ("Disconnect from this service", self.on_disconnect, "ac_disconnect", {"tip": "Signs out and forgets the address."})):
            v.addWidget(_btn(text, slot, name=name, **extra))
        v.addStretch(1)
        return w

    # ------------------------------------------------------------------ view state
    def refresh_view(self):
        """Choose what to show from local files only. Never touches the network."""
        if self.guest:
            self.stack.setCurrentIndex(CONNECT)
            for kind in (QPushButton, QLineEdit, QCheckBox):
                for wid in self.stack.currentWidget().findChildren(kind):
                    wid.setEnabled(False)
            self.set_status("Guest session: online accounts are not available. Nothing is saved or sent.", error=False)
            return
        st = self.account.state()
        if not st["connected"]:
            self.stack.setCurrentIndex(CONNECT)
        elif st["signed_in"]:
            self.stack.setCurrentIndex(ACCOUNT)
            p = st["profile"]
            self.ac_name.setText("Display name: " + (p.get("display_name") or "(loading...)"))
            self.ac_user.setText("Username: " + (p.get("username") or "(loading...)") + "   Service: " + st["url"])
            exp = p.get("session_expires_at")
            self.ac_expires.setText("Session valid until " + time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "")
        else:
            self.stack.setCurrentIndex(SIGNUP if self._want_signup else SIGNIN)
            self.si_service.setText("Service: " + st["url"])
        self.si_remember.setEnabled(st["remember_available"])
        self.su_remember.setEnabled(st["remember_available"])
        tip = "" if st["remember_available"] else "Unavailable: Jarvis can only keep the sign-in if Windows can encrypt it for your account. You will sign in each time."
        self.si_remember.setToolTip(tip)
        self.su_remember.setToolTip(tip)
        self.banner.setVisible(bool(st["expired"]))
        if st["expired"]:
            self.banner.setText("Your session expired or was ended elsewhere. Please sign in again.")

    def show_signup(self, on: bool):
        self._want_signup = on
        self.clear_errors()
        self.refresh_view()

    def set_status(self, text, error=True):
        self.status.setText(text)
        self.status.setProperty("error", bool(error and text))

    def clear_errors(self):
        self.si_err.setText("")
        for l in self.su_err.values():
            l.setText("")
        self.set_status("", False)

    def set_busy(self, on, text=""):
        self.busy = on
        for b in self.findChildren(QPushButton):
            b.setEnabled(not on)               # while a request runs nothing can be pressed twice; afterwards every button is live again (guest mode re-disables them in refresh_view)
        if on:
            self.set_status(text, error=False)
        else:
            self.refresh_view()

    def _show_fields(self, res: Result):
        for k, msg in res.fields.items():
            lab = self.su_err.get(k)
            if lab is not None:
                lab.setText(msg)
        if "username" in res.fields and self.stack.currentIndex() == SIGNIN:
            self.si_err.setText(res.fields["username"])

    # ------------------------------------------------------------------ running work off the interface thread
    def _run_threaded(self, fn, done):
        def work():
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001 - a worker never dies silently; the person sees a message
                r = Result(False, "error", f"Something unexpected happened ({type(e).__name__}). Your local Jarvis is unaffected.")
            self._mt.call.emit(lambda: done(r))
        threading.Thread(target=work, name="jarvis-account", daemon=True).start()

    def _do(self, busy_text, fn, done):
        if self.busy:
            return
        self.set_busy(True, busy_text)

        def finish(res):
            self.set_busy(False)
            done(res)
        self.run(fn, finish)

    # ------------------------------------------------------------------ actions (each one does what its button says)
    def on_connect(self):
        url = self.url.text().strip()
        if not self.dialogs.confirm("Allow this connection?",
                                    f"Project Jarvis will be allowed to contact:\n\n{url}\n\nYour sign-in and, if you chat there, your messages would go to that server. "
                                    "Nothing is sent until you sign in. Only connect to a service you trust.", yes="Allow connection"):
            self.set_status("Not connected.", False)
            return
        res = self.account.connect(url, confirmed=True, local_http=self.local_http.isChecked())
        self.set_status(res.message, not res.ok)
        self.refresh_view()

    def on_sign_in(self):
        self.clear_errors()
        user, pw, remember = self.si_user.text(), self.si_pass.text(), self.si_remember.isChecked()

        def done(res: Result):
            self.si_pass.clear()
            if res.ok:
                self.si_user.clear()
                self.set_status(res.message, False)
                self.refresh_view()
                return
            self.si_err.setText(res.message)
            self.set_status(res.message, True)
        self._do("Signing in...", lambda: self.account.sign_in(user, pw, remember), done)

    def on_sign_up(self):
        self.clear_errors()
        args = (self.su_user.text(), self.su_display.text(), self.su_pass.text(), self.su_pass2.text(), self.su_terms.isChecked(), self.su_age.isChecked(), self.su_remember.isChecked())

        def done(res: Result):
            self.su_pass.clear(); self.su_pass2.clear()
            if res.ok:
                for e in (self.su_user, self.su_display):
                    e.clear()
                self.su_terms.setChecked(False); self.su_age.setChecked(False)
                self._want_signup = False
                self.set_status(res.message, False)
                self.refresh_view()
                self.dialogs.recovery_code(res.data.get("recovery_code", ""))
                return
            self._show_fields(res)
            self.set_status(res.message, True)
        self._do("Creating the account...", lambda: self.account.sign_up(*args), done)

    def on_read_terms(self):
        def done(res: Result):
            if res.ok:
                self.dialogs.show_text("Terms and Privacy Policy", res.data["text"])
            else:
                self.set_status(res.message, True)
        self._do("Loading the terms...", self.account.load_terms, done)

    def on_forgot(self):
        got = self.dialogs.recover(self.si_user.text().strip())
        if not got:
            return
        self._do("Resetting the password...", lambda: self.account.recover_password(*got), lambda res: self._after_recover(res))

    def _after_recover(self, res: Result):
        self.set_status(res.message, not res.ok)
        if res.ok:
            self.dialogs.recovery_code(res.data.get("recovery_code", ""))

    def on_sign_out(self):
        if not self.dialogs.confirm("Sign out?", "Sign out of this account on this PC? Your local Jarvis keeps working.", yes="Sign out", default_no=False):
            return
        self._do("Signing out...", self.account.sign_out, lambda res: (self.set_status(res.message, False), self.refresh_view()))

    def on_sign_out_all(self):
        if not self.dialogs.confirm("Sign out everywhere?", "End every session of this account, on every PC?", yes="Sign out everywhere"):
            return
        self._do("Signing out everywhere...", lambda: self.account.sign_out(everywhere=True), lambda res: (self.set_status(res.message, False), self.refresh_view()))

    def on_rename(self):
        cur = self.account.state()["profile"].get("display_name", "")
        name = self.dialogs.text("Display name", "New display name:", cur)
        if name is None:
            return
        self._do("Saving...", lambda: self.account.set_display_name(name), lambda res: (self.set_status(res.message, not res.ok), self.refresh_view()))

    def on_change_password(self):
        got = self.dialogs.change_password()
        if not got:
            return
        self._do("Changing the password...", lambda: self.account.change_password(*got), lambda res: self.set_status(res.message, not res.ok))

    def on_renew(self):
        self._do("Renewing...", self.account.renew, lambda res: (self.set_status("Session renewed." if res.ok else res.message, not res.ok), self.refresh_view()))

    def on_delete(self):
        if not self.dialogs.confirm("Delete this account?", "This permanently deletes your account and everything stored for it on the service. It cannot be undone. "
                                    "Your local Jarvis data on this PC is NOT touched.", yes="Continue"):
            return
        pw = self.dialogs.password("Confirm deletion", "Enter your password to delete the account:")
        if pw is None:
            return
        self._do("Deleting the account...", lambda: self.account.delete_account(pw), lambda res: (self.set_status(res.message, not res.ok), self.refresh_view()))

    def on_disconnect(self):
        if not self.dialogs.confirm("Disconnect?", "Forget this service address and any saved sign-in on this PC? An account on the service is not deleted.", yes="Disconnect"):
            return
        res = self.account.disconnect()
        self._want_signup = False
        self.set_status(res.message, False)
        self.refresh_view()

    def check_session(self):
        """Called when the person opens this page: if a sign-in is saved, ask the service whether it is still valid (one request, to the address they confirmed)."""
        if self.guest or self.busy or not self.account.state()["signed_in"]:
            return
        self._do("Checking your session...", self.account.refresh, lambda res: (self.set_status("" if res.ok else res.message, not res.ok), self.refresh_view()))
