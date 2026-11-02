"""The window that opens when Jarvis starts and nobody is signed in: a clean Log in / Sign up card. It never appears when a kept sign-in is found, in a guest session, or after
"Don't ask me again". Closing it, or "Continue without an account", simply carries on: Jarvis works without an account.

PySide6 is not available in the authoring sandbox; the decision (``LocalAccounts.should_welcome``) is tested without Qt and this window is built by the self-test on Windows."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from jarvis import version as V
from jarvis.online.localaccounts import LocalAccounts
from jarvis.ui.local_account_page import LocalAccountPage


class WelcomeDialog(QDialog):
    def __init__(self, accounts: LocalAccounts, parent=None):
        super().__init__(parent)
        self.acc = accounts
        self.setObjectName("welcome_dialog")
        self.setWindowTitle(f"Welcome to {V.APP_NAME}")
        self.setModal(True)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 20)
        lay.setSpacing(10)
        title = QLabel(f"Welcome to {V.APP_NAME}")
        title.setProperty("heading", True)
        title.setObjectName("welcome_title")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(title)
        sub = QLabel("Log in to pick up where you left off, or create an account. Accounts stay on this PC; nothing is sent anywhere.")
        sub.setWordWrap(True)
        sub.setProperty("muted", True)
        sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(sub)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(line)
        self.page = LocalAccountPage(accounts, parent=self, on_change=self._changed, compact=True)
        lay.addWidget(self.page, 1)
        row = QHBoxLayout()
        self.dont_ask = QCheckBox("Don't ask me again")
        self.dont_ask.setObjectName("welcome_dont_ask")
        row.addWidget(self.dont_ask)
        row.addStretch(1)
        self.skip_btn = QPushButton("Continue without an account")
        self.skip_btn.setObjectName("welcome_skip_btn")
        self.skip_btn.clicked.connect(lambda _checked=False: self._skip())
        row.addWidget(self.skip_btn)
        lay.addLayout(row)

    def _changed(self, current) -> None:
        if current:                      # logged in or signed up: done, the window closes by itself
            self.accept()

    def _skip(self) -> None:
        if self.dont_ask.isChecked():
            self.acc.set_ask_at_start(False)
        self.reject()
