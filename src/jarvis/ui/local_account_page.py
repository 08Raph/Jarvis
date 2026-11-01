"""The Account page: a real Log in / Sign up screen. Accounts live on this PC (jarvis.online.localaccounts): no address to type, no server, nothing sent anywhere.

PySide6 is not available in the authoring sandbox; the logic is tested without Qt (tests/test_p99_local_accounts.py). The page keeps the names ``stack`` (0 = log in, 1 = sign up,
2 = signed in, 3 = forgot password) used by the window and its tests. Every control does what it says."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from jarvis.online import rules
from jarvis.online.localaccounts import LocalAccounts

LOGIN, SIGNUP, SIGNED_IN, FORGOT = range(4)


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


def _field(name, accessible, placeholder="", password=False):
    e = QLineEdit()
    e.setObjectName(name)
    e.setAccessibleName(accessible)
    e.setPlaceholderText(placeholder)
    e.setMaxLength(400 if password else 120)
    if password:
        e.setEchoMode(QLineEdit.EchoMode.Password)
    return e


class LocalAccountPage(QWidget):
    def __init__(self, accounts: LocalAccounts, guest: bool = False, parent=None, on_change=None):
        super().__init__(parent)
        self.acc, self.guest, self.on_change = accounts, guest, on_change
        self.stack = QStackedWidget()
        lay = QVBoxLayout(self)
        lay.addWidget(_label("Account", heading=True))
        self.status = _label("", muted=True, name="account_status")
        lay.addWidget(self.status)
        lay.addWidget(self.stack, 1)
        self.stack.addWidget(self._login_view())
        self.stack.addWidget(self._signup_view())
        self.stack.addWidget(self._signed_in_view())
        self.stack.addWidget(self._forgot_view())
        self.refresh_view()
        if self.acc.current() is None and not guest:
            self._show_start()

    # ------------------------------------------------------------------ views
    def _login_view(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Log in", heading=True))
        form = QFormLayout()
        self.li_user = _field("li_user", "Username", "your username")
        self.li_pass = _field("li_pass", "Password", password=True)
        form.addRow("Username", self.li_user)
        form.addRow("Password", self.li_pass)
        v.addLayout(form)
        self.li_stay = QCheckBox("Keep me signed in on this PC")
        self.li_stay.setObjectName("li_stay")
        v.addWidget(self.li_stay)
        self.li_msg = _label("", name="li_message")
        v.addWidget(self.li_msg)
        row = QHBoxLayout()
        self.li_btn = _btn("Log in", self._do_login, name="login_btn", primary=True)
        row.addWidget(self.li_btn)
        row.addWidget(_btn("Create an account", lambda: self._show(SIGNUP), name="goto_signup_btn"))
        row.addWidget(_btn("Forgot password", lambda: self._show(FORGOT), name="goto_forgot_btn"))
        row.addStretch(1)
        v.addLayout(row)
        self.li_pass.returnPressed.connect(self._do_login)
        v.addWidget(_label("Accounts are stored on this PC only. Jarvis works without one.", muted=True))
        v.addStretch(1)
        return w

    def _signup_view(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Create an account", heading=True))
        form = QFormLayout()
        self.su_user = _field("su_user", "Username", "3-32 letters, digits, . _ -")
        self.su_display = _field("su_display", "Display name", "optional")
        self.su_pass = _field("su_pass", "Password", password=True)
        self.su_pass2 = _field("su_pass2", "Repeat password", password=True)
        for lab, f in (("Username", self.su_user), ("Display name", self.su_display), ("Password", self.su_pass), ("Repeat password", self.su_pass2)):
            form.addRow(lab, f)
        v.addLayout(form)
        v.addWidget(_label(rules.PASSWORD_REQUIREMENTS, muted=True))
        self.su_ok = QCheckBox("I understand this account is stored on this PC only, does not lock or encrypt my chats, and cannot be recovered without the recovery code.")
        self.su_ok.setObjectName("su_accept")
        v.addWidget(self.su_ok)
        self.su_stay = QCheckBox("Keep me signed in on this PC")
        v.addWidget(self.su_stay)
        self.su_msg = _label("", name="su_message")
        v.addWidget(self.su_msg)
        row = QHBoxLayout()
        self.su_btn = _btn("Sign up", self._do_signup, name="signup_btn", primary=True)
        row.addWidget(self.su_btn)
        row.addWidget(_btn("I already have an account", lambda: self._show(LOGIN), name="goto_login_btn"))
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        return w

    def _signed_in_view(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.in_title = _label("", heading=True, name="signed_in_title")
        v.addWidget(self.in_title)
        form = QFormLayout()
        self.in_display = _field("in_display", "Display name")
        form.addRow("Display name", self.in_display)
        v.addLayout(form)
        v.addWidget(_btn("Save display name", self._do_display, name="display_btn"))
        v.addWidget(_label("Change password", heading=True))
        f2 = QFormLayout()
        self.cp_old, self.cp_new, self.cp_new2 = _field("cp_old", "Current password", password=True), _field("cp_new", "New password", password=True), _field("cp_new2", "Repeat new password", password=True)
        for lab, f in (("Current password", self.cp_old), ("New password", self.cp_new), ("Repeat new password", self.cp_new2)):
            f2.addRow(lab, f)
        v.addLayout(f2)
        v.addWidget(_btn("Change password", self._do_change_pw, name="change_pw_btn"))
        self.in_msg = _label("", name="in_message")
        v.addWidget(self.in_msg)
        row = QHBoxLayout()
        row.addWidget(_btn("Log out", self._do_logout, name="logout_btn", primary=True))
        row.addWidget(_btn("Delete this account", self._do_delete, name="delete_btn", danger=True, tip="Asks for your password. Your chats and memory are not touched."))
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        return w

    def _forgot_view(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Forgot password", heading=True))
        v.addWidget(_label("There is no email service, so a forgotten password can only be reset with the recovery code shown once when the account was created. "
                           "Without it the account cannot be recovered; you can create a new one.", muted=True))
        form = QFormLayout()
        self.fg_user, self.fg_code = _field("fg_user", "Username"), _field("fg_code", "Recovery code", "XXXX-XXXX-XXXX-XXXX")
        self.fg_new, self.fg_new2 = _field("fg_new", "New password", password=True), _field("fg_new2", "Repeat new password", password=True)
        for lab, f in (("Username", self.fg_user), ("Recovery code", self.fg_code), ("New password", self.fg_new), ("Repeat new password", self.fg_new2)):
            form.addRow(lab, f)
        v.addLayout(form)
        self.fg_msg = _label("", name="fg_message")
        v.addWidget(self.fg_msg)
        row = QHBoxLayout()
        row.addWidget(_btn("Reset password", self._do_recover, name="recover_btn", primary=True))
        row.addWidget(_btn("Back to log in", lambda: self._show(LOGIN), name="back_btn"))
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        return w

    # ------------------------------------------------------------------ plumbing
    def _show(self, index: int) -> None:
        self.stack.setCurrentIndex(index)

    def _show_start(self) -> None:
        """No account on this PC yet: start on Sign up; otherwise on Log in."""
        self._show(LOGIN if self.acc.has_accounts() else SIGNUP)

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.acc.current())
            except Exception:  # noqa: BLE001 - a listener problem never breaks the page
                pass

    def check_session(self) -> None:
        """Kept for the window: there is no service to contact, so this only re-reads the local sign-in."""
        self.refresh_view()

    def refresh_view(self) -> None:
        if self.guest:
            self.status.setText("Guest session: accounts are not available.")
            for b in (self.li_btn, self.su_btn):
                b.setEnabled(False)
            self._show(LOGIN)
            return
        cur = self.acc.current()
        if cur:
            self.status.setText(f"Signed in as {cur['display']} ({cur['user']}).")
            self.in_title.setText(f"Hello, {cur['display']}")
            self.in_display.setText(cur["display"])
            self._show(SIGNED_IN)
        else:
            self.status.setText("You are not signed in. Jarvis works without an account." if self.acc.has_accounts() else "No account yet. Create one, or keep using Jarvis without.")
            if self.stack.currentIndex() == SIGNED_IN:
                self._show(LOGIN)

    @staticmethod
    def _clear(*fields) -> None:
        for f in fields:
            f.clear()

    def _recovery_box(self, code: str, title: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Information, title,
                          "YOUR RECOVERY CODE (shown only once):\n\n    " + code + "\n\nWrite it down somewhere safe. Without it a forgotten password cannot be recovered. "
                          "Jarvis stores only a scrambled copy and cannot show it again.", QMessageBox.StandardButton.Ok, self)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.button(QMessageBox.StandardButton.Ok).setText("I have saved it")
        box.exec()

    # ------------------------------------------------------------------ actions
    def _do_login(self) -> None:
        r = self.acc.sign_in(self.li_user.text(), self.li_pass.text(), self.li_stay.isChecked())
        self.li_pass.clear()
        self.li_msg.setText(r.message)
        if r.ok:
            self.li_msg.setText("")
            self._clear(self.li_user)
            self.refresh_view()
            self._changed()

    def _do_signup(self) -> None:
        r = self.acc.sign_up(self.su_user.text(), self.su_display.text(), self.su_pass.text(), self.su_pass2.text(), self.su_ok.isChecked(), self.su_stay.isChecked())
        self._clear(self.su_pass, self.su_pass2)
        self.su_msg.setText(r.message)
        if r.ok:
            self._clear(self.su_user, self.su_display)
            self.su_ok.setChecked(False)
            self.su_msg.setText("")
            self.refresh_view()
            self._recovery_box(r.data["recovery_code"], "Save your recovery code")
            self._changed()

    def _do_logout(self) -> None:
        self.acc.sign_out()
        self.in_msg.setText("")
        self.refresh_view()
        self._show_start()
        self._changed()

    def _do_display(self) -> None:
        r = self.acc.set_display_name(self.in_display.text())
        self.in_msg.setText(r.message)
        self.refresh_view()
        self._changed()

    def _do_change_pw(self) -> None:
        r = self.acc.change_password(self.cp_old.text(), self.cp_new.text(), self.cp_new2.text())
        self._clear(self.cp_old, self.cp_new, self.cp_new2)
        self.in_msg.setText(r.message)

    def _do_delete(self) -> None:
        if QMessageBox.question(self, "Delete account", "Delete this account from this PC? This cannot be undone. Your chats and memory are not touched.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        from PySide6.QtWidgets import QInputDialog
        pw, ok = QInputDialog.getText(self, "Delete account", "Type your password to confirm:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        r = self.acc.delete_account(pw)
        self.in_msg.setText(r.message)
        if r.ok:
            self.refresh_view()
            self._show_start()
            self._changed()

    def _do_recover(self) -> None:
        r = self.acc.recover(self.fg_user.text(), self.fg_code.text(), self.fg_new.text(), self.fg_new2.text())
        self._clear(self.fg_new, self.fg_new2)
        self.fg_msg.setText(r.message)
        if r.ok:
            self._clear(self.fg_code)
            self._recovery_box(r.data["recovery_code"], "Your new recovery code")
            self._show(LOGIN)
