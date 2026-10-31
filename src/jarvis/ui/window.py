"""The PySide6 window. A THIN view: every action calls AppController (src/jarvis/app/controller.py), which calls the existing Jarvis code.
STATUS: the authoring sandbox has no PySide6, so this file is checked there only by syntax/name checks and stand-in Qt classes. It has run under real PySide6 on hosted Windows CI
(offscreen render tests on every push; a click-through of the wizard and chat in an older run, see docs/VERIFICATION_LEDGER.md) but NOT by a person on a real PC. docs/PROMPT10_REVIEW.md and docs/WINDOWS_TEST_CHECKLIST.md section 6k list what must be checked by running it.
Rule for this file: a visible control either works or is disabled with a tooltip that says why."""
from __future__ import annotations

import html
import os
import sys
import time

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
                               QScrollArea, QSplitter, QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from jarvis import version as V
from jarvis.app.controller import AppController, explain_error
from jarvis.brains import viewmodel as BVM
from jarvis.ui import hud_model as HM
from jarvis.online.account import AccountController
from jarvis.ui import theme
from jarvis.ui.account_page import AccountPage
from jarvis.ui.hud import HudWidget
from jarvis.voice import viewmodel as VVM

READINESS = "System & Model Readiness"
IMPORT_BRAIN = "Import Brain"
ACCOUNT = "Account"
SCROLL_PAGES = ("Personality", "Voice", "Safety", "Status", READINESS, IMPORT_BRAIN)       # pages that are longer than a small window
PAGES = ["Home", "Chat", "Missions", "Schedules", "Memory", "Personality", "Voice", "Safety", "Status", READINESS, IMPORT_BRAIN, ACCOUNT]


def _slug(name: str) -> str:
    """'System & Model Readiness' -> 'system_model_readiness' (the suffix of the page's _refresh_ method)."""
    return "_".join("".join(ch if ch.isalnum() else " " for ch in name.lower()).split())


class _Bridge(QObject):
    """Lets worker threads run a function on the interface thread (queued signal)."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run)          # a bound @Slot of this QObject: delivered on THIS object's (interface) thread when emitted from a worker

    @Slot(object)
    def _run(self, fn):
        fn()


class _Input(QPlainTextEdit):
    submit = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.submit.emit()
            return
        super().keyPressEvent(e)


def _btn(text, slot, *, primary=False, danger=False, tip="", enabled=True):
    b = QPushButton(text)
    b.setProperty("primary", primary)
    b.setProperty("danger", danger)
    b.setToolTip(tip)
    b.setEnabled(enabled)
    b.clicked.connect(slot)
    return b


def _label(text, *, muted=False, heading=False, wrap=True):
    l = QLabel(text)
    l.setWordWrap(wrap)
    l.setProperty("muted", muted)
    l.setProperty("heading", heading)
    return l


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    return t


def _fill(t: QTableWidget, rows):
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t.setItem(r, c, QTableWidgetItem(str(v)))
    t.resizeColumnsToContents()


class MainWindow(QMainWindow):
    def __init__(self, ctl: AppController, bridge: _Bridge):
        super().__init__()
        self.ctl, self.bridge = ctl, bridge
        self.settings = QSettings("ProjectJarvis", "Window")
        self.setWindowTitle(f"{V.APP_NAME} {V.__version__} ({V.RELEASE_CHANNEL})")
        self.setMinimumSize(960, 640)
        self.resize(1100, 720)
        ctl.on_update = self.on_update
        ctl.ask_user = self.ask_user
        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(210)
        self.nav.setAccessibleName("Sections")
        for n in PAGES:
            self.nav.addItem(n)
        self.stack = QStackedWidget()
        self.pages = {"Home": self._home_page(), "Chat": self._chat_page(), "Missions": self._missions_page(), "Schedules": self._schedules_page(),
                      "Memory": self._memory_page(), "Personality": self._personality_page(), "Voice": self._voice_page(), "Safety": self._safety_page(), "Status": self._status_page(),
                      READINESS: self._readiness_page(), IMPORT_BRAIN: self._import_brain_page(), ACCOUNT: self._account_page()}
        for n in PAGES:
            # Long pages scroll instead of squeezing their controls on top of each other when the window is small or the display is scaled.
            self.stack.addWidget(self._scrolled(self.pages[n]) if n in SCROLL_PAGES else self.pages[n])
        lay.addWidget(self.nav)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.nav.currentRowChanged.connect(self._goto)
        for i in range(len(PAGES)):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda i=i: self.nav.setCurrentRow(i))
        self.nav.setCurrentRow(0)
        self.statusBar().showMessage("Ready. Nothing runs in the background except what you start.")
        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self.ctl.run_due_schedules)           # started ONLY by the checkbox on the Schedules page
        self._apply_theme()
        self.refresh_all()

    @staticmethod
    def _scrolled(page):
        sa = QScrollArea()
        sa.setWidgetResizable(True)                 # the page keeps at least its own minimum size and scrolls when there is less room
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setWidget(page)
        return sa

    # ------------------------------------------------------------ theme / scale
    def _apply_theme(self):
        dark = False
        try:
            dark = QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        except Exception:  # noqa: BLE001 - older Qt without colorScheme
            pass
        self.dark = dark
        self.pal = theme.palette(dark)
        scale = theme.SCALES.get(str(self.settings.value("text_scale", "100%")), 1.0)
        QApplication.instance().setStyleSheet(theme.stylesheet(self.pal, scale) + HM.nav_rail_css())
        self.render_chat()

    def _goto(self, row):
        if row >= 0:
            self.stack.setCurrentIndex(row)
            self.refresh(PAGES[row])
            if PAGES[row] == ACCOUNT:
                self.account_page.check_session()          # the person opened the page: one request to the service they confirmed, only if a sign-in is saved
            if PAGES[row] == "Home":
                self.home_timer.start()
            else:
                self.home_timer.stop()          # the 1-second refresh of the Home panels runs only while Home is the page being shown

    # ------------------------------------------------------------ updates from the controller (always on the interface thread)
    def on_update(self, topic):
        if topic in ("voice", "busy", "model", "persona"):
            self.refresh("Home")
        if topic == "transcript" and hasattr(self, "home_recent"):
            self._home_recent_only()
        if topic in ("transcript", "busy"):
            self.render_chat()
        if topic in ("missions", "busy"):
            self.refresh("Missions")
        if topic == "schedules":
            self.refresh("Schedules")
        if topic in ("safety", "modes"):
            self.refresh("Safety")
        if topic == "persona":
            self.refresh("Personality")
            self.refresh("Chat")
            self.refresh("Voice")
        if topic == "voice":
            self.refresh("Voice")
        if topic == "preflight":
            self.refresh(READINESS)
        if topic == "brains":
            self.refresh(IMPORT_BRAIN)
        if topic == "model":
            self.refresh("Chat")
            self.refresh("Status")

    def refresh_all(self):
        for n in PAGES:
            self.refresh(n)

    def refresh(self, name):
        try:
            getattr(self, "_refresh_" + _slug(name))()
        except Exception as e:  # noqa: BLE001 - a page that cannot refresh says so; it never closes the app
            self.statusBar().showMessage(f"{name}: {explain_error(e)}", 8000)

    def error_box(self, e):
        QMessageBox.warning(self, V.APP_NAME, explain_error(e))

    # ------------------------------------------------------------ confirmation dialog (worker asks, person answers, default = No)
    def ask_user(self, req, resolve):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Jarvis needs your approval")
        box.setText(getattr(req, "description", str(req)))
        risk = getattr(req, "risk", "")
        box.setInformativeText((f"Risk: {risk}. " if risk else "") + "Nothing happens unless you choose Allow. Closing this window means No.")
        yes = box.addButton("Allow this once", QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("Don't allow", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        resolve(box.clickedButton() is yes)


    # ------------------------------------------------------------ Home: the HUD. A view only: every value comes from the controller's measured state.
    def _panel(self, title):
        f = QFrame()
        f.setObjectName("panel")
        v = QVBoxLayout(f)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        t = QLabel(title.upper())
        t.setStyleSheet(f"color: {HM.HUD['accent']}; font-weight: 600; background: transparent;")
        v.addWidget(t)
        return f, v

    def _home_page(self):
        w = QWidget()
        w.setObjectName("homePage")
        w.setStyleSheet(HM.home_stylesheet())
        self._home_voice = {}
        self._home_facts = {}
        self._home_facts_at = -1e9
        self._home_res_at = -1e9
        self._home_res = {}
        self._home_view = HM.describe(HM.OFF)
        self._home_css = {}
        self._resources = HM.Resources()
        self.home_timer = QTimer(self)
        self.home_timer.setInterval(1000)
        self.home_timer.timeout.connect(lambda: self.refresh("Home"))
        outer = QVBoxLayout(w)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(10)
        # header: name, privacy status, the unmistakable microphone chip, clock
        head = QHBoxLayout()
        title = QLabel("JARVIS")
        title.setStyleSheet("font-size: 20pt; font-weight: 600;")
        tag = _label("YOUR PERSONAL AI ASSISTANT", muted=True, wrap=False)
        self.home_privacy = _label("", wrap=False)
        self.home_privacy.setAccessibleName("Privacy status")
        self.home_mic = QLabel("MIC OFF")
        self.home_mic.setAccessibleName("Microphone indicator")
        self.home_clock = QLabel("")
        self.home_clock.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.home_clock.setAccessibleName("Time")
        head.addWidget(title)
        head.addSpacing(12)
        head.addWidget(tag)
        head.addStretch(1)
        head.addWidget(self.home_privacy)
        head.addSpacing(12)
        head.addWidget(self.home_mic)
        head.addSpacing(12)
        head.addWidget(self.home_clock)
        outer.addLayout(head)
        # body: the HUD and its caption on the left, three panels on the right
        body = QHBoxLayout()
        body.setSpacing(14)
        centre = QVBoxLayout()
        self.hud = HudWidget(provider=self._hud_provider)
        self.hud.stateChanged.connect(lambda _s: self.refresh("Home"))
        centre.addWidget(self.hud, 1)
        self.home_label = QLabel("")
        self.home_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.home_label.setAccessibleName("Jarvis state")
        self.home_sub = _label("", muted=True)
        self.home_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.home_sub.setAccessibleName("Jarvis state detail")
        self.home_wave_cap = _label("", muted=True)
        self.home_wave_cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.home_wave_cap.setAccessibleName("Waveform source")
        centre.addWidget(self.home_label)
        centre.addWidget(self.home_sub)
        centre.addWidget(self.home_wave_cap)
        ctl_row = QHBoxLayout()
        ctl_row.addStretch(1)
        self.home_toggle = _btn("Turn hands-free ON", self._home_toggle, primary=True,
                                tip="Hands-free ON: the microphone opens and Jarvis listens for the wake phrase, on this PC only; audio is not saved. It starts by itself when Jarvis opens, until you turn it OFF.")
        self.home_mute = _btn("Mute microphone", self._home_mute_toggle,
                              tip="Closes the microphone stream (Ctrl+Shift+M). Saved: stays muted after a restart.")
        self.home_stop = _btn("Stop speaking", self.ctl.stop_speaking, tip="Silences Jarvis now and returns to listening.")
        self.home_retry = _btn("Retry", self._voice_retry, tip="Try to start voice again after fixing the problem shown above.")
        for b in (self.home_toggle, self.home_mute, self.home_stop, self.home_retry):
            ctl_row.addWidget(b)
        ctl_row.addStretch(1)
        centre.addLayout(ctl_row)
        body.addLayout(centre, 1)
        side = QVBoxLayout()
        side.setSpacing(10)
        act, av = self._panel("Current activity")
        self.home_act = _label("", wrap=True)
        self.home_act.setAccessibleName("Current activity")
        av.addWidget(self.home_act)
        side.addWidget(act)
        qi, qv = self._panel("Quick info")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        self.home_qi = []
        for r in range(4):
            n, val = _label("", muted=True, wrap=False), _label("", wrap=True)
            grid.addWidget(n, r, 0)
            grid.addWidget(val, r, 1)
            self.home_qi.append((n, val))
        grid.setColumnStretch(1, 1)
        qv.addLayout(grid)
        side.addWidget(qi)
        rc, rv = self._panel("Recent conversation")
        self.home_recent = QLabel("")
        self.home_recent.setTextFormat(Qt.TextFormat.RichText)
        self.home_recent.setWordWrap(True)
        self.home_recent.setAccessibleName("Recent conversation")
        rv.addWidget(self.home_recent)
        rv.addWidget(_btn("View full history", lambda: self.nav.setCurrentRow(PAGES.index("Chat")), tip="Opens the Chat page."))
        side.addWidget(rc)
        side.addStretch(1)
        sidew = QWidget()
        sidew.setLayout(side)
        sidew.setFixedWidth(310)
        body.addWidget(sidew)
        outer.addLayout(body, 1)
        # footer: local-AI / privacy box, the honest stage strip, resources
        foot = QHBoxLayout()
        foot.setSpacing(14)
        lp, lv = self._panel("Local AI")
        self.home_local = _label("", muted=True)
        self.home_local.setAccessibleName("Local AI and privacy")
        lv.addWidget(self.home_local)
        foot.addWidget(lp, 2)
        self.home_stages = QLabel("")
        self.home_stages.setTextFormat(Qt.TextFormat.RichText)
        self.home_stages.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.home_stages.setAccessibleName("Stage")
        foot.addWidget(self.home_stages, 2)
        sp, sv = self._panel("System resources")
        self.home_bars = {}
        for key, name in (("cpu", "CPU"), ("ram", "RAM"), ("disk", "DISK")):
            row = QHBoxLayout()
            lab = _label(name, muted=True, wrap=False)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setAccessibleName(name + " usage")
            val = _label("n/a", wrap=False)
            val.setMinimumWidth(48)
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(lab)
            row.addWidget(bar, 1)
            row.addWidget(val)
            sv.addLayout(row)
            self.home_bars[key] = (bar, val)
        foot.addWidget(sp, 2)
        outer.addLayout(foot)
        QShortcut(QKeySequence("Ctrl+Shift+M"), self, activated=self._home_mute_shortcut)
        return w

    def _hud_provider(self):
        """Called once per animation frame: in-memory values only (no file, no device). Returns (state, microphone level, wake age)."""
        live = self.ctl.voice_live()
        try:
            speaking = bool(self.ctl.speech.is_speaking())
        except Exception:  # noqa: BLE001
            speaking = False
        st = HM.resolve_state(self._home_voice, live, bool(self.ctl.busy), speaking)
        return st, float(live.get("level") or 0.0), live.get("wake_age")

    def _style_once(self, widget, css):
        """setStyleSheet re-polishes the widget; the Home panels refresh every second, so only touch it when the style really changed."""
        if self._home_css.get(id(widget)) != css:
            self._home_css[id(widget)] = css
            widget.setStyleSheet(css)

    def _home_toggle(self):
        try:
            if self.ctl.voice_state()["enabled"]:
                self.ctl.voice_disable()
            else:
                self.ctl.voice_enable("wake_word")
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _home_mute_toggle(self):
        st = self.ctl.voice_state()
        if not st["enabled"]:
            self.statusBar().showMessage("Hands-free voice is off, so there is no microphone to mute.", 5000)
            return
        self.ctl.voice_unmute() if st["mic_muted"] else self.ctl.voice_mute()

    def _home_mute_shortcut(self):
        self._home_mute_toggle()

    def _home_recent_only(self):
        lines = HM.recent_lines(HM.recent_from_entries(self.ctl.transcript.entries))
        if not lines:
            self.home_recent.setText(f"<span style='color:{HM.HUD['muted']}'>Nothing said yet.</span>")
            return
        self.home_recent.setText("<br>".join(
            f"<b style='color:{HM.HUD['accent'] if l['who'] == 'Jarvis' else HM.HUD['text']}'>{html.escape(l['who'])}</b> "
            f"<span style='color:{HM.HUD['muted']}'>{html.escape(l['stamp'])}</span><br>{html.escape(l['text'])}" for l in lines))

    def _refresh_home(self):
        now = time.monotonic()
        self._home_voice = self.ctl.voice_state()
        if now - self._home_facts_at >= 2.0:
            self._home_facts, self._home_facts_at = self.ctl.hud_facts(), now
        if now - self._home_res_at >= 0.9:
            self._home_res, self._home_res_at = self._resources.sample(), now
        live = self.ctl.voice_live()
        try:
            speaking = bool(self.ctl.speech.is_speaking())
        except Exception:  # noqa: BLE001
            speaking = False
        st = HM.resolve_state(self._home_voice, live, bool(self.ctl.busy), speaking)
        view = HM.describe(st, self._home_voice, live, loading=bool(self.ctl.loading))
        self._home_view = view
        H = HM.HUD
        accent = H.get(view.accent, H["accent"])
        self.home_label.setText(view.label)
        self._style_once(self.home_label, f"font-size: 18pt; font-weight: 600; color: {accent};")
        self.home_sub.setText(view.sub)
        self.home_wave_cap.setText("Waveform: " + view.wave_caption)
        tone = H.get(view.mic_tone, H["off"])
        self.home_mic.setText(view.mic_text)
        self._style_once(self.home_mic, f"font-weight: 700; padding: 4px 14px; border: 2px solid {tone}; border-radius: 13px; color: {tone}; background: transparent;")
        self.home_clock.setText("\n".join(HM.clock_text()))
        v = self._home_voice
        on = bool(v.get("enabled"))
        self.home_toggle.setText("Turn hands-free OFF" if on else "Turn hands-free ON")
        self.home_toggle.setProperty("primary", not on)
        self.home_toggle.style().unpolish(self.home_toggle)
        self.home_toggle.style().polish(self.home_toggle)
        self.home_mute.setEnabled(on)
        self.home_mute.setText("Unmute microphone" if v.get("mic_muted") else "Mute microphone")
        self.home_retry.setVisible(bool(v.get("can_retry")))
        self.home_act.setText(f"<b style='color:{accent}'>{html.escape(view.label)}</b><br>{html.escape(view.sub)}")
        self.home_act.setTextFormat(Qt.TextFormat.RichText)
        for (n, val), (name, text, t) in zip(self.home_qi, HM.quick_info(self._home_facts, view)):
            n.setText(name)
            val.setText(text)
            self._style_once(val, f"color: {H.get(t, H['text'])}; background: transparent;")
        self._home_recent_only()
        head, htone, ltitle, ldetail = HM.privacy_lines(self._home_facts.get("online_on"))
        self.home_privacy.setText("● " + head)
        self._style_once(self.home_privacy, f"color: {H.get(htone, H['off'])}; background: transparent;")
        self.home_local.setText(f"<b style='color:{H['text']}'>{html.escape(ltitle)}</b><br>{html.escape(ldetail)}")
        self.home_local.setTextFormat(Qt.TextFormat.RichText)
        active = HM.stage_active(st)
        self.home_stages.setText("&nbsp;&nbsp;&nbsp;".join(
            (f"<b style='color:{H['accent']}'>● {s}</b>" if s == active else f"<span style='color:{H['muted']}'>○ {s}</span>") for s in HM.STAGES))
        for key, (bar, val) in self.home_bars.items():
            x = self._home_res.get(key)
            bar.setValue(0 if x is None else int(round(x)))
            val.setText("n/a" if x is None else f"{int(round(x))}%")
        self.hud.setAccessibleDescription(f"{view.label}. {view.sub} {view.mic_text}.")

    # ------------------------------------------------------------ Chat
    def _chat_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.model_banner = _label("", muted=False)
        self.model_banner.setAccessibleName("Model status")
        self.chat_view = QTextBrowser()
        self.chat_view.setAccessibleName("Conversation")
        self.chat_view.setOpenLinks(False)
        self.chat_input = _Input()
        self.chat_input.setAccessibleName("Message")
        self.chat_input.setPlaceholderText("Type a message. Ctrl+Enter sends.")
        self.chat_input.setFixedHeight(90)
        self.chat_input.submit.connect(self._send)
        row = QHBoxLayout()
        self.send_btn = _btn("Send", self._send, primary=True, tip="Send (Ctrl+Enter)")
        self.stop_btn = _btn("Stop", self.ctl.stop_generation, tip="Stop the reply and any speech. The text already produced stays.")
        self.new_btn = _btn("New conversation", self._new_conv, tip="Clear this window and the assistant's short-term history. Saved memory is not touched.")
        row.addWidget(self.send_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.new_btn)
        row.addStretch(1)
        self.persona_banner = _label("", wrap=False)
        self.persona_banner.setAccessibleName("Current personality mode")
        self.normal_btn = _btn("Return to Normal", self._persona_normal, tip="One click: switch off any special style and go back to the default.")
        prow = QHBoxLayout()
        prow.addWidget(self.persona_banner, 1)
        prow.addWidget(self.normal_btn)
        v.addWidget(self.model_banner)
        v.addLayout(prow)
        v.addWidget(self.chat_view, 1)
        v.addWidget(self.chat_input)
        v.addLayout(row)
        return w

    def _refresh_chat(self):
        ms = self.ctl.model_status()
        rd = self.ctl.readiness()
        self.model_banner.setText(rd["headline"] + ((" " + ms.action) if (ms.action and not ms.ok) else ""))
        st = self.ctl.persona_state()
        self.persona_banner.setText(st["indicator"] + ("   (roleplay or specialised: wording only, permissions unchanged)" if st["settings"]["style"] != "normal" else ""))
        self.normal_btn.setEnabled(st["settings"]["style"] != "normal")
        self.render_chat()

    def _send(self):
        text = self.chat_input.toPlainText()
        if self.ctl.send(text):
            self.chat_input.clear()

    def _new_conv(self):
        self.ctl.new_conversation()

    def render_chat(self):
        if not hasattr(self, "chat_view"):
            return
        kinds = theme.kind_colors(self.pal)
        parts = []
        if not self.ctl.transcript.entries:
            parts.append(f"<p style='color:{self.pal['text_muted']}'>No messages yet. Type below to talk to the local model. "
                         "If no model is installed, you will see what to do. Nothing in this window is simulated.</p>")
        for e in self.ctl.transcript.entries:
            bg, fg = kinds[e.kind]
            body = html.escape(e.text).replace("\n", "<br>") or "<i>...</i>"
            parts.append(f"<table width='100%' style='margin:4px 0'><tr><td style='background:{bg};padding:8px;border-radius:6px'>"
                         f"<b style='color:{fg}'>{html.escape(e.label())}</b> <span style='color:{self.pal['text_muted']}'>{e.stamp()}</span><br>"
                         f"<span style='color:{self.pal['text']}'>{body}</span></td></tr></table>")
        self.chat_view.setHtml("".join(parts))
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())
        busy = self.ctl.busy
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.new_btn.setEnabled(not busy)
        self.statusBar().showMessage(("Loading the model..." if self.ctl.loading else "Jarvis is answering...") if busy else "Ready.")

    # ------------------------------------------------------------ Missions
    def _missions_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Missions", heading=True))
        v.addWidget(_label("A mission is a plan you review and approve. A plan on this page is a PROPOSAL until it has run; the record below shows only what actually happened.", muted=True))
        sp = QSplitter(Qt.Orientation.Horizontal)
        self.mission_list = QListWidget()
        self.mission_list.setAccessibleName("Missions")
        self.mission_list.currentRowChanged.connect(self._mission_selected)
        sp.addWidget(self.mission_list)
        self.mission_view = QTextBrowser()
        self.mission_view.setAccessibleName("Mission details")
        sp.addWidget(self.mission_view)
        sp.setStretchFactor(1, 3)
        v.addWidget(sp, 1)
        row = QHBoxLayout()
        self.m_open = _btn("Open plan file...", self._mission_open, tip="Load a plan from a JSON file as a DRAFT (nothing runs).")
        self.m_plan = _btn("Plan with local model...", self._mission_plan, tip="Ask the local model to PROPOSE a draft. It cannot run or approve anything.")
        self.m_approve = _btn("Approve this plan", self._mission_approve, primary=True)
        self.m_reject = _btn("Reject", self._mission_reject)
        self.m_start = _btn("Start", self._mission_start)
        self.m_pause = _btn("Pause", self._mission_pause, tip="Takes effect after the current step; a running step cannot be interrupted.")
        self.m_cancel = _btn("Cancel", self._mission_cancel, tip="Takes effect after the current step; a running step cannot be interrupted.")
        for b in (self.m_open, self.m_plan, self.m_approve, self.m_reject, self.m_start, self.m_pause, self.m_cancel):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self._mission_ids = []
        self._detail = None
        return w

    def _refresh_missions(self):
        rows = self.ctl.missions()
        self._mission_ids = [r["id"] for r in rows]
        cur = self.mission_list.currentRow()
        self.mission_list.blockSignals(True)
        self.mission_list.clear()
        for r in rows:
            self.mission_list.addItem(f"{r['state'].upper():18} {r['goal'][:60]}")
        self.mission_list.blockSignals(False)
        if not rows:
            self.mission_view.setHtml(f"<p style='color:{self.pal['text_muted']}'>No missions yet. Open a plan file, or ask the local model to propose a plan. "
                                      "Plans that change anything need your approval before they can start.</p>")
            self._set_mission_buttons(None)
            return
        self.mission_list.setCurrentRow(min(max(cur, 0), len(rows) - 1))
        self._mission_selected(self.mission_list.currentRow())

    def _sel_id(self):
        r = self.mission_list.currentRow()
        return self._mission_ids[r] if 0 <= r < len(self._mission_ids) else None

    def _mission_selected(self, row):
        mid = self._sel_id()
        if mid is None:
            self._set_mission_buttons(None)
            return
        try:
            d = self.ctl.mission_detail(mid)
        except Exception as e:  # noqa: BLE001
            self.mission_view.setPlainText(explain_error(e))
            self._set_mission_buttons(None)
            return
        self._detail = d
        lines = [f"<h3>{html.escape(d['goal'])}</h3>", f"<p><b>State:</b> {d['state']} &nbsp; <b>Plan hash:</b> <code>{d['plan_hash'][:12]}</code> "
                 f"&nbsp; {'<b>CHANGES things: approval required</b>' if d['consequential'] else 'observation only'}"
                 f"{' &nbsp; <b style=color:' + self.pal['danger'] + '>FILE WAS CHANGED AFTER THE PLAN WAS RECORDED</b>' if d['tampered'] else ''}</p>",
                 "<p><b>Plan (a proposal until it has run):</b></p><ol>"]
        for r in d["rows"]:
            warn = "" if r["permission_on"] else f" <b style='color:{self.pal['danger']}'>(permission '{html.escape(r['category'])}' is OFF)</b>"
            ask = " <i>(will ask you)</i>" if r["will_ask_you"] else ""
            lines.append(f"<li><b>{html.escape(r['description'] or r['tool'])}</b> [{html.escape(r['tool'])}, risk {html.escape(str(r['risk']))}]{ask}{warn}"
                         + (f"<br>expected: {html.escape(r['expect'])}" if r["expect"] else "") + "</li>")
        lines.append("</ol><p><b>What the record shows:</b></p><pre style='white-space:pre-wrap'>" + html.escape(d["summary"]) + "</pre>")
        self.mission_view.setHtml("".join(lines))
        self._set_mission_buttons(d)

    def _set_mission_buttons(self, d):
        busy = self.ctl.busy
        self.m_plan.setEnabled(not busy)
        self.m_approve.setEnabled(bool(d and d["can_approve"] and d["consequential"]))
        self.m_approve.setToolTip("" if (d and d["consequential"]) else "Only plans that change things need approval.")
        self.m_reject.setEnabled(bool(d and d["can_approve"]))
        self.m_start.setEnabled(bool(d and d["can_start"] and not d["running"]))
        self.m_pause.setEnabled(bool(d and d["can_pause"]))
        self.m_cancel.setEnabled(bool(d and d["can_cancel"]))

    def _mission_open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open a plan file", "", "Plan (*.json)")
        if path:
            try:
                self.ctl.mission_from_file(path)
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    def _mission_plan(self):
        goal, ok = QInputDialog.getText(self, "Plan with the local model", "Goal (one sentence):")
        if ok and goal.strip():
            if not self.ctl.mission_plan(goal.strip()):
                self.error_box(RuntimeError("Jarvis is busy."))

    def _mission_approve(self):
        mid = self._sel_id()
        if not mid or not self._detail:
            return
        seen = self._detail["plan_hash"]
        fresh = self.ctl.mission_detail(mid)
        if fresh["plan_hash"] != seen:
            QMessageBox.warning(self, V.APP_NAME, "The plan changed since you opened it. Review it again before approving.")
            self._mission_selected(self.mission_list.currentRow())
            return
        r = QMessageBox.question(self, "Approve this plan?",
                                 f"Approve exactly this plan ({seen[:12]}) to be started ONCE?\n\nApproval does not enable any permission. "
                                 "Each action still follows your permission switches and asks you when it runs.",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            try:
                self.statusBar().showMessage(self.ctl.mission_approve(mid, seen), 8000)
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    def _mission_reject(self):
        mid = self._sel_id()
        if mid:
            try:
                self.statusBar().showMessage(self.ctl.mission_reject(mid), 8000)
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    def _mission_start(self):
        mid = self._sel_id()
        if mid:
            self.ctl.mission_start(mid)
            self.statusBar().showMessage("Mission started. The result is added to the Chat page and shown here.", 8000)

    def _mission_pause(self):
        self._say(lambda: self.ctl.mission_pause(self._sel_id()))

    def _mission_cancel(self):
        self._say(lambda: self.ctl.mission_cancel(self._sel_id()))

    def _say(self, fn):
        try:
            QMessageBox.information(self, V.APP_NAME, fn())
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    # ------------------------------------------------------------ Schedules
    def _schedules_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Schedules", heading=True))
        v.addWidget(_label("Schedules run ONLY while Jarvis is open and checking. There is no background service: if Jarvis is closed or the PC is asleep, "
                           "missed runs are skipped (or run once, late, if you chose that).", muted=True))
        self.sched_run = QCheckBox("Run due schedules while this window is open")
        self.sched_run.setToolTip("Off by default. When on, this window checks every 30 seconds and starts due missions.")
        self.sched_run.toggled.connect(lambda on: self.timer.start() if on else self.timer.stop())
        v.addWidget(self.sched_run)
        self.sched_table = _table(["Schedule", "Mission", "Kind", "State", "Time zone", "Approval", "Last result"])
        self.sched_table.setAccessibleName("Schedules")
        v.addWidget(self.sched_table, 1)
        self.sched_empty = _label("No schedules yet. Add one for a mission you have already reviewed.", muted=True)
        v.addWidget(self.sched_empty)
        row = QHBoxLayout()
        self.s_add = _btn("Add schedule...", self._sched_add)
        self.s_approve = _btn("Approve recurring run...", self._sched_approve, tip="Needed for missions that change things; limited to N runs and an expiry date.")
        self.s_pause = _btn("Pause", lambda: self._sched_state("paused"))
        self.s_resume = _btn("Resume", lambda: self._sched_state("active"))
        self.s_cancel = _btn("Cancel", lambda: self._sched_state("cancelled"))
        self.s_delete = _btn("Delete", self._sched_delete)
        for b in (self.s_add, self.s_approve, self.s_pause, self.s_resume, self.s_cancel, self.s_delete):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self._sched_ids = []
        return w

    def _refresh_schedules(self):
        rows = self.ctl.schedules()
        self._sched_ids = [s["id"] for s in rows]
        out = []
        for s in rows:
            ap = s.get("approval")
            out.append([s["id"], s["mission"], s["kind"], s["state"], s["tz"],
                        (f"{ap['max_runs'] - ap['runs_used']} run(s) left, until {ap['expires'][:10]}" if ap else "none"),
                        (s.get("last") or {}).get("status", "-")])
        _fill(self.sched_table, out)
        self.sched_empty.setVisible(not rows)

    def _sel_sched(self):
        r = self.sched_table.currentRow()
        return self._sched_ids[r] if 0 <= r < len(self._sched_ids) else None

    def _sched_state(self, state):
        sid = self._sel_sched()
        if sid:
            try:
                self.ctl.schedule_set_state(sid, state)
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    def _sched_delete(self):
        sid = self._sel_sched()
        if sid and QMessageBox.question(self, V.APP_NAME, f"Delete schedule {sid}? The mission itself is kept.",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                self.ctl.schedule_delete(sid)
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    def _sched_add(self):
        missions = self.ctl.missions()
        if not missions:
            QMessageBox.information(self, V.APP_NAME, "Create a mission first (Missions page).")
            return
        from jarvis.missions.schedule import local_default_tz_name
        dlg = QDialog(self)
        dlg.setWindowTitle("Add schedule")
        f = QFormLayout(dlg)
        mission = QComboBox()
        for m in missions:
            mission.addItem(f"{m['goal'][:50]} ({m['id']})", m["id"])
        kind = QComboBox()
        kind.addItems(["once", "daily", "weekly", "interval"])
        when = QLineEdit()
        when.setPlaceholderText("once: 2026-12-01 09:30   daily/weekly: 09:30")
        days = QLineEdit()
        days.setPlaceholderText("weekly only, e.g. mon,thu")
        every = QSpinBox()
        every.setRange(0, 7 * 24 * 60)
        every.setSuffix(" min (interval only)")
        tz = QLineEdit(local_default_tz_name())
        missed = QComboBox()
        missed.addItems(["skip", "run_once"])
        for lab, wdg in (("Mission", mission), ("Kind", kind), ("When", when), ("Weekdays", days), ("Every", every), ("Time zone", tz), ("If missed", missed)):
            f.addRow(lab, wdg)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            k = kind.currentText()
            try:
                self.ctl.schedule_add(mission.currentData(), k, tz=tz.text().strip(), at=when.text().strip() if k == "once" else "",
                                      time_=when.text().strip() if k in ("daily", "weekly") else "", weekdays=days.text().strip(),
                                      every_minutes=every.value(), missed=missed.currentText())
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    def _sched_approve(self):
        sid = self._sel_sched()
        if not sid:
            return
        sch = next((s for s in self.ctl.schedules() if s["id"] == sid), None)
        if not sch:
            return
        d = self.ctl.mission_detail(sch["mission"])
        runs, ok1 = QInputDialog.getInt(self, "Approve recurring run", f"Plan {d['hash_hint']}: allow how many runs? (1-50)", 5, 1, 50)
        if not ok1:
            return
        days, ok2 = QInputDialog.getInt(self, "Approve recurring run", "Approval expires after how many days? (1-90)", 30, 1, 90)
        if ok2:
            try:
                self.ctl.schedule_approve(sid, d["plan_hash"], runs, days)
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    # ------------------------------------------------------------ Memory
    def _memory_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Memory", heading=True))
        v.addWidget(_label("What Jarvis has saved about you, on this PC only (not encrypted). You can correct, delete or export any of it. Nothing here retrains a model.", muted=True))
        self.mem_table = _table(["#", "Category", "Text", "Source"])
        self.mem_table.setAccessibleName("Saved memories")
        v.addWidget(self.mem_table, 1)
        self.mem_empty = _label("", muted=True)
        v.addWidget(self.mem_empty)
        row = QHBoxLayout()
        self.mem_edit = _btn("Edit...", self._mem_edit)
        self.mem_del = _btn("Delete", self._mem_delete)
        self.mem_exp = _btn("Export to file...", self._mem_export)
        self.mem_all = _btn("Forget everything...", self._mem_forget, danger=True, tip="Deletes ALL memory, logs, missions and schedules in this profile (asks first).")
        for b in (self.mem_edit, self.mem_del, self.mem_exp, self.mem_all):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self._mem_ids = []
        return w

    def _refresh_memory(self):
        if self.ctl.guest:
            _fill(self.mem_table, [])
            self.mem_empty.setText("Guest session: nothing is saved, so there is no memory to manage.")
            self.mem_empty.setVisible(True)
            for b in (self.mem_edit, self.mem_del, self.mem_exp, self.mem_all):
                b.setEnabled(False)
                b.setToolTip("Unavailable in a guest session.")
            return
        rows = self.ctl.memory_items()
        self._mem_ids = [r["id"] for r in rows]
        _fill(self.mem_table, [[r["id"], r["category"], r["text"], r.get("source", "")] for r in rows])
        self.mem_empty.setText("Nothing saved yet. Tell Jarvis 'remember that ...' in the chat and it appears here.")
        self.mem_empty.setVisible(not rows)

    def _sel_mem(self):
        r = self.mem_table.currentRow()
        return self._mem_ids[r] if 0 <= r < len(self._mem_ids) else None

    def _mem_edit(self):
        mid = self._sel_mem()
        if mid is None:
            return
        cur = next((r["text"] for r in self.ctl.memory_items() if r["id"] == mid), "")
        text, ok = QInputDialog.getMultiLineText(self, "Edit memory", "Text:", cur)
        if ok and text.strip():
            try:
                self.ctl.memory_update(mid, text.strip())
                self._refresh_memory()
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    def _mem_delete(self):
        mid = self._sel_mem()
        if mid is not None and QMessageBox.question(self, V.APP_NAME, f"Delete memory #{mid}?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                self.ctl.memory_delete(mid)
                self._refresh_memory()
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    def _mem_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export memory", "jarvis-memory-export.json", "JSON (*.json)")
        if path:
            try:
                p = self.ctl.memory_export(path)
                QMessageBox.information(self, V.APP_NAME, f"Saved to {p}. The file contains your personal memory as plain text; keep it private.")
            except Exception as e:  # noqa: BLE001
                self.error_box(e)

    def _mem_forget(self):
        def work(token):
            return self.ctl.memory_forget_all()

        def done(res, err):
            if err:
                self.error_box(RuntimeError(err))
            elif res:
                QMessageBox.information(self, V.APP_NAME, res)
            self.refresh_all()
        self.ctl.tasks.submit("forget-all", work, done)

    # ------------------------------------------------------------ Safety
    def _safety_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Safety", heading=True))
        top = QHBoxLayout()
        self.stop_all = _btn("EMERGENCY STOP", self._estop, danger=True, tip="Stops missions and desktop tasks at their next check. An operation already in progress cannot be interrupted.")
        self.clear_stop_btn = _btn("Clear stop", self._clear_stop)
        self.stop_state = _label("", wrap=False)
        top.addWidget(self.stop_all)
        top.addWidget(self.clear_stop_btn)
        top.addWidget(self.stop_state, 1)
        v.addLayout(top)
        self.perm_note = _label("", muted=True)
        v.addWidget(self.perm_note)
        self.perm_table = _table(["Permission", "What it allows", "State"])
        self.perm_table.setAccessibleName("Permissions (read-only)")
        v.addWidget(self.perm_table, 2)
        v.addWidget(self._quick_launch_box())
        mrow = QHBoxLayout()
        mrow.addWidget(_label("Mode:", wrap=False))
        self.mode_combo = QComboBox()
        self.mode_combo.setAccessibleName("Mode")
        mrow.addWidget(self.mode_combo)
        mrow.addWidget(_btn("Apply mode", self._apply_mode, tip="A mode only hides tools. It never grants a permission."))
        mrow.addStretch(1)
        v.addLayout(mrow)
        self.mode_text = _label("", muted=True)
        v.addWidget(self.mode_text)
        v.addWidget(_label("Recent actions (from the local audit log):"))
        self.hist_table = _table(["When", "Tool", "Status", "Summary"])
        self.hist_table.setAccessibleName("Action history")
        v.addWidget(self.hist_table, 2)
        return w

    def _quick_launch_box(self):
        f = QFrame()
        f.setObjectName("panel")
        v = QVBoxLayout(f)
        v.addWidget(_label("Quick launch: open apps, games and websites", heading=True))
        v.addWidget(_label("Say or type things like \u201copen Fortnite\u201d, \u201copen Instagram\u201d or \u201cscan my PC\u201d. Jarvis only starts things it found on this PC "
                           "(programs, Epic and Steam games, shortcuts) or well-known websites. It cannot type, click or read anything this way, and installers are never started. "
                           "This is separate from the advanced desktop permissions above.", muted=True))
        self.ql_on = QCheckBox("Let Jarvis open apps, games and websites when I ask (chat and voice)")
        self.ql_on.setAccessibleName("Quick launch on or off")
        self.ql_on.toggled.connect(lambda on: self.ctl.quick_launch_set(on=on))
        v.addWidget(self.ql_on)
        self.ql_ask = QCheckBox("Ask me before each one (recommended; turn off for fully hands-free voice)")
        self.ql_ask.setAccessibleName("Ask before each launch")
        self.ql_ask.toggled.connect(lambda on: self.ctl.quick_launch_set(ask_each=on))
        v.addWidget(self.ql_ask)
        row = QHBoxLayout()
        row.addWidget(_btn("Scan my PC now", self.ctl.quick_launch_scan, tip="Looks for installed programs, Store apps, Epic and Steam games and shortcuts. Read-only."))
        self.ql_note = _label("", muted=True)
        row.addWidget(self.ql_note, 1)
        v.addLayout(row)
        return f

    def _refresh_quick_launch(self):
        st = self.ctl.quick_launch_state()
        for box, val in ((self.ql_on, st["enabled"]), (self.ql_ask, st["confirm_each"])):
            box.blockSignals(True)
            box.setChecked(bool(val))
            box.blockSignals(False)
        self.ql_on.setEnabled(not st["guest"])
        self.ql_ask.setEnabled(not st["guest"])
        self.ql_note.setText("Not available in guest mode." if st["guest"] else st["note"])

    def _refresh_safety(self):
        self._refresh_quick_launch()
        p = self.ctl.permissions()
        self.perm_note.setText(p["how_to_change"] + (f"  Allowed folders: {', '.join(p['allowed_folders']) or 'none'}."))
        _fill(self.perm_table, [[r["category"], r["description"], ("ON" if r["enabled"] else "off") + ((" (consent recorded)" if r["consent_recorded"] else " (needs consent)") if r["needs_consent"] and r["enabled"] else "")]
                                for r in p["rows"]])
        m = self.ctl.modes()
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        self.mode_combo.addItem("(no mode)", "")
        for d in m["available"]:
            self.mode_combo.addItem(d["name"], d["name"])
        idx = self.mode_combo.findData(m["current"])
        self.mode_combo.setCurrentIndex(max(idx, 0))
        self.mode_combo.blockSignals(False)
        cur = next((d for d in m["available"] if d["name"] == m["current"]), None)
        self.mode_text.setText(cur["summary"] if cur else "No mode selected: every tool your permissions allow is available.")
        on = self.ctl.stop_active()
        self.stop_state.setText("EMERGENCY STOP IS ON - tasks will not run." if on else "Emergency stop is off.")
        self.clear_stop_btn.setEnabled(on)
        hist = self.ctl.action_history(50)
        _fill(self.hist_table, [[h.get("at", ""), h.get("tool", ""), h.get("status", h.get("event", "")), h.get("summary", h.get("error", ""))] for h in reversed(hist)])

    def _estop(self):
        QMessageBox.information(self, V.APP_NAME, self.ctl.emergency_stop())

    def _clear_stop(self):
        self.ctl.clear_stop()

    def _apply_mode(self):
        try:
            self.statusBar().showMessage(self.ctl.set_mode(self.mode_combo.currentData()), 8000)
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    # ------------------------------------------------------------ Personality
    def _personality_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Personality", heading=True))
        v.addWidget(_label("A style changes how Jarvis words things. It never changes permissions, approvals or what Jarvis is able to do. "
                           "Normal is the default. Savage, Flirty, Sexy and Romantic are temporarily unavailable (see the note below).", muted=True))
        self.persona_restriction = _label("", muted=True)
        self.persona_restriction.setAccessibleName("Why some styles are unavailable")
        v.addWidget(self.persona_restriction)
        self.persona_now = _label("", wrap=False)
        self.persona_now.setAccessibleName("Current mode")
        v.addWidget(self.persona_now)
        row = QHBoxLayout()
        row.addWidget(_label("Style:", wrap=False))
        self.persona_combo = QComboBox()
        self.persona_combo.setAccessibleName("Personality style")
        row.addWidget(self.persona_combo, 1)
        row.addWidget(_btn("Apply style", self._persona_apply))
        row.addWidget(_btn("Return to Normal", self._persona_normal, primary=True))
        v.addLayout(row)
        self.persona_preview_btn = _btn("Preview selected style", self._persona_preview, tip="Shows what the style does and the exact instructions the model receives. It does not fake a sample reply.")
        v.addWidget(self.persona_preview_btn)
        self.persona_text = QTextBrowser()
        self.persona_text.setAccessibleName("Style preview")
        v.addWidget(self.persona_text, 1)
        form = QFormLayout()
        self.persona_spins = {}
        for key, lo, label in (("roast", 1, "Roast intensity (1-3)"), ("profanity", 0, "Profanity (0 none - 3 strong)"), ("flirt", 1, "Flirtation (1-3)"),
                               ("argument", 1, "Argument intensity (1-3)"), ("motivation", 1, "Motivation intensity (1-3)")):
            sp = QSpinBox()
            sp.setRange(lo, 3)
            sp.setAccessibleName(label)
            sp.editingFinished.connect(lambda k=key, s=sp: self._persona_level(k, s.value()))
            self.persona_spins[key] = sp
            form.addRow(label, sp)
        v.addLayout(form)
        v.addWidget(_label("Customize the current style (0 = the style as designed; each step shifts it, but the style keeps its character):", heading=True))
        cform = QFormLayout()
        self.persona_ctl_spins = {}
        for key, label in (("humor", "Humor"), ("warmth", "Warmth"), ("directness", "Directness"), ("formality", "Formality"), ("verbosity", "Verbosity")):
            sp = QSpinBox()
            sp.setRange(-2, 2)
            sp.setAccessibleName(label + " adjustment")
            sp.editingFinished.connect(lambda k=key, s=sp: self._persona_control(k, s.value()))
            self.persona_ctl_spins[key] = sp
            cform.addRow(label + " (-2 less ... +2 more)", sp)
        v.addLayout(cform)
        self.persona_limit = _label("", muted=True)
        v.addWidget(self.persona_limit)
        self.persona_adult = QCheckBox("I am 18 or older and want adult-only styles (Savage, Flirty, Sexy, Romantic) available once they are released")
        self.persona_adult.setAccessibleName("Adult opt-in")
        self.persona_adult.clicked.connect(self._persona_adult)
        v.addWidget(self.persona_adult)
        v.addWidget(_btn("Reset personality settings to defaults", self._persona_reset))
        v.addWidget(_label("Speech for typed chat (replies only; hands-free microphone settings are on the Voice page):", heading=True))
        self.speak_box = QCheckBox("Read replies aloud (Windows speech, off by default)")
        self.speak_box.setAccessibleName("Read replies aloud")
        self.speak_box.clicked.connect(self._speak_toggle)
        v.addWidget(self.speak_box)
        self.speech_note = _label("", muted=True)
        v.addWidget(self.speech_note)
        srow = QHBoxLayout()
        srow.addWidget(_label("Speaking speed (%):", wrap=False))
        self.speech_rate = QSpinBox()
        self.speech_rate.setRange(50, 200)
        self.speech_rate.setAccessibleName("Speaking speed percent")
        self.speech_rate.editingFinished.connect(self._speech_rate_changed)
        srow.addWidget(self.speech_rate)
        srow.addWidget(_btn("Stop speaking", self.ctl.stop_speaking, tip="Silences speech immediately."))
        srow.addStretch(1)
        v.addLayout(srow)
        return w

    def _refresh_personality(self):
        st = self.ctl.persona_state()
        self.persona_now.setText("Current: " + st["indicator"] + (("   " + st["note"]) if st["note"] else ""))
        self.persona_combo.blockSignals(True)
        self.persona_combo.clear()
        for d in st["styles"]:
            lock = "" if d["available"] else ("  (temporarily unavailable: safety review pending)" if d.get("restricted") else "  (locked: needs adult opt-in)")
            self.persona_combo.addItem(d["label"] + (" [roleplay]" if d["roleplay"] else "") + lock, d["id"])
            if d["current"]:
                self.persona_combo.setCurrentIndex(self.persona_combo.count() - 1)
        self.persona_combo.blockSignals(False)
        for k, sp in self.persona_spins.items():
            sp.blockSignals(True)
            sp.setValue(st["settings"][k])
            sp.blockSignals(False)
        for k, sp in self.persona_ctl_spins.items():
            sp.blockSignals(True)
            sp.setValue(st["settings"][k])
            sp.blockSignals(False)
        rs = st["restriction"]
        self.persona_restriction.setText(rs["reason"] + ((" Your saved choice (" + rs["saved_choice_kept"] + ") is kept.") if rs["saved_choice_kept"] else "")
                                         + " Turning on the adult opt-in does not make them available while this applies." if rs["active"] else "")
        self.persona_limit.setText(st["limitation"])
        self.persona_adult.blockSignals(True)
        self.persona_adult.setChecked(st["settings"]["adult_optin"])
        self.persona_adult.blockSignals(False)
        sp = self.ctl.speech_state()
        self.speak_box.blockSignals(True)
        self.speak_box.setChecked(sp["speak_in_window"])
        self.speak_box.blockSignals(False)
        self.speech_rate.blockSignals(True)
        self.speech_rate.setValue(int(round(sp["rate"] * 100)))
        self.speech_rate.blockSignals(False)
        self.speech_note.setText(("Speech engine found (not yet played). " if sp["engine_available"] else "Speech engine unavailable: " + sp["engine_note"] + " ")
                                 + "Speed is multiplied by the style (Calm is slower). Microphone: " + sp["microphone"] + ".")

    # ------------------------------------------------------------ Voice (hands-free). The controller owns the microphone; this page only shows measured state and calls it.
    def _voice_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Hands-free voice", heading=True))
        self.voice_state_lbl = _label("", wrap=False)
        self.voice_state_lbl.setAccessibleName("Voice state")
        self.voice_mic_lbl = _label("", wrap=False)
        self.voice_mic_lbl.setAccessibleName("Microphone indicator")
        self.voice_detail = _label("", muted=True)
        self.voice_detail.setAccessibleName("Voice status detail")
        v.addWidget(self.voice_state_lbl)
        v.addWidget(self.voice_mic_lbl)
        v.addWidget(self.voice_detail)
        row = QHBoxLayout()
        self.voice_enable_btn = _btn("Turn hands-free ON", self._voice_enable, primary=True,
                                     tip="Opens the microphone and listens for the wake phrase. Audio is processed on this PC only and is not saved.")
        self.voice_disable_btn = _btn("Turn hands-free OFF", self._voice_disable, tip="Stops listening and closes the microphone completely. Saved: it stays off after a restart.")
        self.voice_mute_btn = _btn("Mute microphone", self._voice_mute_toggle, tip="Stops capturing audio (the microphone is closed). Saved: stays muted after a restart.")
        self.voice_retry_btn = _btn("Retry", self._voice_retry, tip="Try to start voice again after fixing the problem shown above.")
        for b in (self.voice_enable_btn, self.voice_disable_btn, self.voice_mute_btn, self.voice_retry_btn):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        row2 = QHBoxLayout()
        self.voice_spk_mute = QCheckBox("Mute speaker (Jarvis stays silent; replies still appear as text; the microphone is unaffected)")
        self.voice_spk_mute.setAccessibleName("Mute speaker")
        self.voice_spk_mute.clicked.connect(self._voice_speaker_mute)
        row2.addWidget(self.voice_spk_mute)
        v.addLayout(row2)
        row3 = QHBoxLayout()
        row3.addWidget(_label("Speaking speed (%):", wrap=False))
        self.voice_rate = QSpinBox()
        self.voice_rate.setRange(50, 200)
        self.voice_rate.setAccessibleName("Voice speaking speed percent")
        self.voice_rate.editingFinished.connect(lambda: self._voice_setting(rate=self.voice_rate.value() / 100.0))
        row3.addWidget(self.voice_rate)
        row3.addWidget(_label("Volume (%):", wrap=False))
        self.voice_volume = QSpinBox()
        self.voice_volume.setRange(0, 100)
        self.voice_volume.setAccessibleName("Voice volume percent")
        self.voice_volume.editingFinished.connect(lambda: self._voice_setting(volume=self.voice_volume.value() / 100.0))
        row3.addWidget(self.voice_volume)
        row3.addWidget(_btn("Stop speaking", self.ctl.stop_speaking, tip="Silences Jarvis now and returns to listening."))
        row3.addStretch(1)
        v.addLayout(row3)
        self._voice_selection_block(v)
        self._voice_input_block(v)
        v.addWidget(_label("Personality used for spoken answers:", heading=True))
        self.voice_persona = _label("", wrap=False)
        self.voice_persona.setAccessibleName("Current personality for voice")
        prow = QHBoxLayout()
        prow.addWidget(self.voice_persona, 1)
        prow.addWidget(_btn("Return to Normal", self._persona_normal, tip="Back to the default style now; cancels the reply being produced and silences speech. You can also say: return to normal."))
        v.addLayout(prow)
        v.addWidget(_label("Change the style on the Personality page. A style changes wording only; it never gives Jarvis a new permission, and speaking cannot approve an action.", muted=True))
        v.addWidget(_label("Start with Windows (separate from voice):", heading=True))
        self.startup_box = QCheckBox("Start the Jarvis window when I sign in to Windows (the microphone stays off unless hands-free is ON)")
        self.startup_box.setAccessibleName("Start with Windows")
        self.startup_box.clicked.connect(self._startup_toggle)
        v.addWidget(self.startup_box)
        self.startup_note = _label("", muted=True)
        v.addWidget(self.startup_note)
        v.addWidget(_label("Privacy: speech is recognised on this PC by the Vosk model you installed. The wake detector listens to short audio pieces in memory only and keeps nothing; "
                           "no audio is saved and none leaves this PC. While the model is thinking, audio is discarded, so a spoken 'stop' is not understood then: use Stop or Mute.", muted=True))
        v.addStretch(1)
        return w

    # ---- Stage 6: voice selection, preview, microphone and wake phrase (PySide6 NOT available in the authoring sandbox: never run; logic is in jarvis.voice.viewmodel)
    def _voice_selection_block(self, v):
        self._vc_asked = False
        self._vc_sig = None
        self._vc_selected = None
        self._vc_cat = {}
        self._vc_engine = None
        self._vc_eng_sig = None
        v.addWidget(_label("Voice", heading=True))
        self.vc_engines = _label("", muted=True)
        self.vc_engines.setTextFormat(Qt.TextFormat.PlainText)
        self.vc_engines.setAccessibleName("Speech engines")
        v.addWidget(self.vc_engines)
        row = QHBoxLayout()
        row.addWidget(_label("Engine:", wrap=False))
        self.vc_engine = QComboBox()
        self.vc_engine.setAccessibleName("Speech engine")
        self.vc_engine.setMinimumWidth(260)
        self.vc_engine.currentIndexChanged.connect(self._vc_engine_changed)
        row.addWidget(self.vc_engine, 1)
        self.vc_to_sapi_btn = _btn("Use Windows speech", self._vc_use_sapi, tip="Makes Windows built-in speech your default voice engine. Kokoro files stay installed.")
        row.addWidget(self.vc_to_sapi_btn)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(_label("Type:", wrap=False))
        self.vc_category = QComboBox()
        self.vc_category.setAccessibleName("Voice type")
        self.vc_category.currentIndexChanged.connect(self._vc_category_changed)
        row.addWidget(self.vc_category)
        row.addWidget(_label("Voice:", wrap=False))
        self.vc_voice = QComboBox()
        self.vc_voice.setAccessibleName("Voice")
        self.vc_voice.setMinimumWidth(260)
        self.vc_voice.currentIndexChanged.connect(self._vc_voice_changed)
        row.addWidget(self.vc_voice, 1)
        row.addWidget(_btn("Refresh voices", self._vc_refresh, tip="Ask the speech engine which voices are installed on this PC."))
        v.addLayout(row)
        self.vc_empty = _label("", muted=True)
        self.vc_empty.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(self.vc_empty)
        self.vc_details = _label("", wrap=True)
        self.vc_details.setTextFormat(Qt.TextFormat.PlainText)
        self.vc_details.setAccessibleName("Voice details")
        v.addWidget(self.vc_details)
        row = QHBoxLayout()
        self.vc_preview_btn = _btn("Preview Voice", self._vc_preview, primary=True, tip="Plays a short fixed sentence in the selected voice. Nothing is saved.")
        self.vc_stop_btn = _btn("Stop", self._vc_stop, tip="Silences the sample at once.")
        self.vc_apply_btn = _btn("Apply", self._vc_apply, tip="Saves the selected voice. It is used for the next thing Jarvis says and after a restart.")
        for b in (self.vc_preview_btn, self.vc_stop_btn, self.vc_apply_btn):
            row.addWidget(b)
        self.vc_progress = QProgressBar()
        self.vc_progress.setRange(0, 0)
        self.vc_progress.setMaximumHeight(10)
        self.vc_progress.setVisible(False)
        self.vc_progress.setAccessibleName("Voice preview loading")
        row.addWidget(self.vc_progress, 1)
        v.addLayout(row)
        self.vc_status = _label("", muted=True)
        self.vc_status.setTextFormat(Qt.TextFormat.PlainText)
        self.vc_status.setAccessibleName("Voice preview status")
        v.addWidget(self.vc_status)
        self.vc_scope = QCheckBox("")
        self.vc_scope.setAccessibleName("Apply to current personality mode only")
        v.addWidget(self.vc_scope)
        row = QHBoxLayout()
        self.vc_clear_btn = _btn("Use my default voice for this mode again", self._vc_clear, tip="Removes the voice set for the current personality mode only.")
        row.addWidget(self.vc_clear_btn)
        row.addStretch(1)
        v.addLayout(row)
        self.vc_resolution = _label("", wrap=True)
        self.vc_resolution.setTextFormat(Qt.TextFormat.PlainText)
        self.vc_resolution.setAccessibleName("Voice in use")
        v.addWidget(self.vc_resolution)
        self.vc_persona = _label("", muted=True)
        self.vc_persona.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(self.vc_persona)
        self.vc_recovery = _label("", wrap=True)
        self.vc_recovery.setTextFormat(Qt.TextFormat.PlainText)
        self.vc_recovery.setAccessibleName("Voice recovery options")
        v.addWidget(self.vc_recovery)
        v.addWidget(_label("Kokoro local voice", heading=True))
        self.vk_text = _label("", muted=True, wrap=True)
        self.vk_text.setTextFormat(Qt.TextFormat.PlainText)
        self.vk_text.setAccessibleName("Kokoro status")
        v.addWidget(self.vk_text)
        row = QHBoxLayout()
        self.vk_install_btn = _btn("Install / repair Kokoro model...", self._vk_install, tip="Downloads the missing Kokoro model files after you confirm. Nothing is downloaded without that.")
        self.vk_cancel_btn = _btn("Cancel download", self._vk_cancel, tip="Stops the download and removes the partial files.")
        self.vk_check_btn = _btn("Check model files", self._vk_check, tip="Re-reads the installed files and compares them with what was recorded when they were installed.")
        for b in (self.vk_install_btn, self.vk_cancel_btn, self.vk_check_btn):
            row.addWidget(b)
        self.vk_progress = QProgressBar()
        self.vk_progress.setRange(0, 100)
        self.vk_progress.setMaximumHeight(10)
        self.vk_progress.setVisible(False)
        self.vk_progress.setAccessibleName("Kokoro download progress")
        row.addWidget(self.vk_progress, 1)
        v.addLayout(row)
        self.vk_job = _label("", muted=True, wrap=True)
        self.vk_job.setTextFormat(Qt.TextFormat.PlainText)
        self.vk_job.setAccessibleName("Kokoro download status")
        v.addWidget(self.vk_job)

    def _voice_input_block(self, v):
        self._vi_rep = None
        v.addWidget(_label("Microphone and listening", heading=True))
        row = QHBoxLayout()
        row.addWidget(_label("Microphone:", wrap=False))
        self.vi_mic = QComboBox()
        self.vi_mic.setAccessibleName("Microphone")
        self.vi_mic.setMinimumWidth(260)
        self.vi_mic.activated.connect(self._vi_mic_chosen)
        row.addWidget(self.vi_mic, 1)
        row.addWidget(_btn("Check microphone and recogniser", self._vi_check, tip="Lists microphones and looks for the speech recogniser. Does NOT open the microphone or record."))
        v.addLayout(row)
        self.vi_report = _label("", muted=True)
        self.vi_report.setTextFormat(Qt.TextFormat.PlainText)
        self.vi_report.setAccessibleName("Microphone check result")
        v.addWidget(self.vi_report)
        row = QHBoxLayout()
        row.addWidget(_label("Listening mode:", wrap=False))
        self.vi_mode = QComboBox()
        self.vi_mode.setAccessibleName("Listening mode")
        for key, label in VVM.MODE_LABELS:
            self.vi_mode.addItem(label, key)
        self.vi_mode.activated.connect(self._vi_mode_chosen)
        row.addWidget(self.vi_mode)
        self.vi_ptt = _btn("Hold to talk", lambda: None, tip="Push-to-talk: hold this button while you speak (only in push-to-talk mode while hands-free is ON).")
        self.vi_ptt.pressed.connect(lambda: self.ctl.voice_ptt(True))
        self.vi_ptt.released.connect(lambda: self.ctl.voice_ptt(False))
        row.addWidget(self.vi_ptt)
        row.addStretch(1)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(_label("Wake phrase:", wrap=False))
        self.vi_wake = QLineEdit()
        self.vi_wake.setAccessibleName("Wake phrase")
        self.vi_wake.setMaxLength(60)
        row.addWidget(self.vi_wake, 1)
        row.addWidget(_btn("Save wake phrase", self._vi_wake_save, tip="Two or more words, for example: hey jarvis. Applies at once if hands-free is running (it restarts)."))
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(_label("Speech model folder (Vosk):", wrap=False))
        self.vi_model = QLineEdit()
        self.vi_model.setAccessibleName("Vosk model folder")
        row.addWidget(self.vi_model, 1)
        row.addWidget(_btn("Browse...", self._vi_model_browse, tip="Choose the unpacked Vosk model folder you downloaded. Jarvis never downloads one for you."))
        row.addWidget(_btn("Save", self._vi_model_save))
        v.addLayout(row)

    def _vc_refresh(self):
        self._vc_asked = True
        self.ctl.voice_catalog_refresh()

    def _vc_category_changed(self, _i=None):
        self._vc_fill_voices()

    def _vc_engine_changed(self, _i=None):
        """Browsing only: shows that engine's voices. Nothing is saved until Apply."""
        eng = self.vc_engine.currentData()
        if eng and eng != self._vc_engine:
            self._vc_engine = eng
            self._vc_sig = None
            self._refresh_voice_selection()

    def _vc_use_sapi(self):
        try:
            self.ctl.voice_select_engine("sapi")
            self._vc_engine, self._vc_sig = "sapi", None
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _vk_install(self):
        r = QMessageBox.question(self, "Download the Kokoro voice model?", VVM.kokoro_confirm_text(self._vc_cat),
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self.ctl.kokoro_install_confirmed()

    def _vk_cancel(self):
        self.ctl.kokoro_cancel()

    def _vk_check(self):
        self.ctl.kokoro_check()

    def _vc_voice_changed(self, _i=None):
        self._vc_selected = self.vc_voice.currentData()
        self.vc_details.setText(VVM.voice_details(self._vc_cat, self._vc_selected))
        self._vc_buttons()

    def _vc_fill_voices(self):
        cat = self._vc_cat
        key = self.vc_category.currentData()
        items = VVM.voice_items(cat, key, self._vc_engine) if key else []
        self.vc_voice.blockSignals(True)
        self.vc_voice.clear()
        for vid, label in items:
            self.vc_voice.addItem(label, vid)
        want = self._vc_selected
        idx = next((i for i, (vid, _l) in enumerate(items) if vid == want), 0 if items else -1)
        if idx >= 0:
            self.vc_voice.setCurrentIndex(idx)
        self.vc_voice.blockSignals(False)
        self._vc_selected = self.vc_voice.currentData() if items else None
        self.vc_details.setText(VVM.voice_details(cat, self._vc_selected))
        self._vc_buttons()

    def _vc_buttons(self):
        busy = bool(self._vc_cat.get("preview", {}).get("busy"))
        have = bool(self._vc_selected)
        self.vc_preview_btn.setEnabled(have and not busy)
        self.vc_stop_btn.setEnabled(busy)
        self.vc_apply_btn.setEnabled(have)

    def _vc_preview(self):
        if self._vc_selected:
            self.ctl.voice_preview_start(self._vc_selected)

    def _vc_stop(self):
        self.ctl.voice_preview_stop()

    def _vc_apply(self):
        if not self._vc_selected:
            return
        try:
            persona = self._vc_cat.get("persona", {}).get("id") if self.vc_scope.isChecked() else None
            self.ctl.voice_apply(self._vc_selected, persona)
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _vc_clear(self):
        try:
            self.ctl.voice_clear_persona(self._vc_cat.get("persona", {}).get("id"))
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _vi_check(self):
        self.ctl.voice_input_check()

    def _vi_mic_chosen(self, _i=None):
        try:
            self.ctl.voice_input_set(mic_device=self.vi_mic.currentData())
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _vi_mode_chosen(self, _i=None):
        try:
            self.ctl.voice_switch_mode(self.vi_mode.currentData())
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _vi_wake_save(self):
        try:
            self.ctl.voice_input_set(wake_phrase=self.vi_wake.text().strip().lower())
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _vi_model_browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose the unpacked Vosk model folder")
        if folder:
            self.vi_model.setText(folder)

    def _vi_model_save(self):
        try:
            self.ctl.voice_set_model_folder(self.vi_model.text())
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _refresh_voice_selection(self):
        if self.stack.currentIndex() == PAGES.index("Voice") and not self._vc_asked:
            cat0 = self.ctl.voice_catalog()
            if not cat0["loaded"] and not cat0["refreshing"]:
                self._vc_asked = True
                self.ctl.voice_catalog_refresh()          # only when the page is open: listing voices starts Windows PowerShell
        cat = self.ctl.voice_catalog()
        self._vc_cat = cat
        self.vc_engines.setText(VVM.engines_text(cat))
        self.vc_empty.setText(VVM.empty_message(cat))
        eng_opts = VVM.engine_options(cat)
        if tuple(eng_opts) != self._vc_eng_sig:
            self._vc_eng_sig = tuple(eng_opts)
            self.vc_engine.blockSignals(True)
            self.vc_engine.clear()
            for eid, label in eng_opts:
                self.vc_engine.addItem(label, eid)
            self.vc_engine.blockSignals(False)
        if self._vc_engine not in [e for e, _l in eng_opts]:
            self._vc_engine = cat.get("selected_engine") or (eng_opts[0][0] if eng_opts else None)
            self._vc_sig = None
        j = self.vc_engine.findData(self._vc_engine)
        if j >= 0 and self.vc_engine.currentIndex() != j:
            self.vc_engine.blockSignals(True)
            self.vc_engine.setCurrentIndex(j)
            self.vc_engine.blockSignals(False)
        sig = (tuple(sorted(v["id"] for vs_ in cat["categories"].values() for v in vs_)), cat.get("selected"), self._vc_engine)
        if sig != self._vc_sig:                            # rebuild the lists only when the voices or the saved choice changed (keeps an open drop-down open)
            self._vc_sig = sig
            opts = VVM.category_options(cat, self._vc_engine)
            keep = self.vc_category.currentData()
            want = VVM.category_of_voice(cat, self._vc_selected or cat.get("selected")) or keep
            self.vc_category.blockSignals(True)
            self.vc_category.clear()
            for key, label in opts:
                self.vc_category.addItem(label, key)
            i = next((n for n, (k, _l) in enumerate(opts) if k == want), 0)
            self.vc_category.setCurrentIndex(i)
            self.vc_category.blockSignals(False)
            if self._vc_selected is None:
                self._vc_selected = cat.get("selected")
            self._vc_fill_voices()
        pv = cat["preview"]
        self.vc_status.setText(VVM.preview_text(pv))
        self.vc_progress.setVisible(pv["state"] in ("loading", "generating") or cat["refreshing"])
        self._vc_buttons()
        self.vc_scope.setText(VVM.apply_scope_label(cat))
        self.vc_clear_btn.setEnabled(bool(cat["persona"]["has_own_voice"]))
        self.vc_resolution.setText(VVM.resolution_text(cat))
        self.vc_persona.setText(VVM.persona_text(cat))
        self.vc_recovery.setText(VVM.recovery_text(cat))
        ks = self.ctl.kokoro_state()
        self.vk_text.setText(VVM.kokoro_text(cat))
        self.vk_job.setText(VVM.kokoro_job_text(ks))
        kst = ((ks.get("status") or {}).get("model") or {}).get("state")
        self.vk_install_btn.setEnabled(bool(ks["present"]) and not ks["running"] and kst != "ready")
        self.vk_cancel_btn.setEnabled(bool(ks["running"]) and ks.get("op") == "install")
        self.vk_check_btn.setEnabled(bool(ks["present"]) and not ks["running"] and kst in ("ready", "damaged"))
        self.vk_progress.setVisible(bool(ks["running"]))
        if ks["running"]:
            frac = ks.get("fraction")
            self.vk_progress.setRange(0, 100 if frac is not None else 0)
            if frac is not None:
                self.vk_progress.setValue(int(frac * 100))
        self.vc_to_sapi_btn.setEnabled(any(e["id"] == "sapi" and e["available"] for e in cat["engines"]))
        rep = self.ctl.voice_input_state()
        self._vi_rep = rep["result"]
        self.vi_report.setText(VVM.input_report_text(rep["result"], rep["running"], rep["error"]))
        self.vi_mic.blockSignals(True)
        self.vi_mic.clear()
        for val, label in VVM.mic_choices(rep["result"], rep["mic_device"]):
            self.vi_mic.addItem(label, val)
        j = self.vi_mic.findData(rep["mic_device"])
        self.vi_mic.setCurrentIndex(j if j >= 0 else 0)
        self.vi_mic.blockSignals(False)
        k = self.vi_mode.findData(rep["input_mode"] if rep["input_mode"] != "off" else self.vi_mode.currentData() or "wake_word")
        if k >= 0:
            self.vi_mode.setCurrentIndex(k)
        self.vi_ptt.setEnabled(rep["input_mode"] == "push_to_talk")
        if not self.vi_wake.hasFocus():
            self.vi_wake.setText(rep["wake_phrase"])
        if not self.vi_model.hasFocus():
            self.vi_model.setText(rep["model_folder"] or "")

    def _refresh_voice(self):
        st = self.ctl.voice_state()
        self.voice_state_lbl.setText("State: " + st["state"])
        self.voice_mic_lbl.setText("Microphone: " + ("OPEN (capturing audio)" if st["mic_open"] else "CLOSED (no audio is captured)"))
        self.voice_detail.setText(st["detail"] + (("  " + st["note"]) if st["note"] else ""))
        on = st["enabled"]
        self.voice_enable_btn.setEnabled(not on)
        self.voice_disable_btn.setEnabled(on)
        self.voice_mute_btn.setEnabled(on)
        self.voice_mute_btn.setText("Unmute microphone" if st["mic_muted"] else "Mute microphone")
        self.voice_retry_btn.setEnabled(bool(st["can_retry"]))
        self.voice_spk_mute.blockSignals(True)
        self.voice_spk_mute.setChecked(st["speaker_muted"])
        self.voice_spk_mute.blockSignals(False)
        for sp, val in ((self.voice_rate, st["rate"]), (self.voice_volume, st["volume"])):
            sp.blockSignals(True)
            sp.setValue(int(round(val * 100)))
            sp.blockSignals(False)
        self.voice_persona.setText("Current: " + self.ctl.persona_state()["indicator"])
        self._refresh_voice_selection()
        su = self.ctl.startup_state()
        self.startup_box.blockSignals(True)
        self.startup_box.setChecked(su["enabled"])
        self.startup_box.setEnabled(su["supported"])
        self.startup_box.blockSignals(False)
        self.startup_note.setText(su["note"] + ((" Command: " + su["command"]) if su["enabled"] and su["command"] else ""))

    def _voice_enable(self):
        try:
            self.ctl.voice_enable(self.vi_mode.currentData() or "wake_word")
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _voice_disable(self):
        self.ctl.voice_disable()

    def _voice_mute_toggle(self):
        st = self.ctl.voice_state()
        self.ctl.voice_unmute() if st["mic_muted"] else self.ctl.voice_mute()

    def _voice_retry(self):
        self.ctl.voice_retry()

    def _voice_speaker_mute(self, checked):
        self.ctl.voice_speaker_mute(bool(checked))

    def _voice_setting(self, **kw):
        try:
            self.ctl.voice_setting(**kw)
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _startup_toggle(self, checked):
        try:
            if checked:
                r = QMessageBox.question(self, "Start with Windows?", "Jarvis will add one entry to your Windows sign-in list (the Startup apps list in Windows) so its window opens when you sign in. "
                                         "It does not turn the microphone on by itself. You can undo this here at any time.\n\nTurn it on?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if r != QMessageBox.StandardButton.Yes:
                    self.refresh("Voice")
                    return
                self.statusBar().showMessage(self.ctl.startup_enable(confirmed=True), 8000)
            else:
                self.statusBar().showMessage(self.ctl.startup_disable(), 8000)
        except Exception as e:  # noqa: BLE001
            self.error_box(e)
        self.refresh("Voice")

    def _speak_toggle(self, checked):
        try:
            self.statusBar().showMessage(self.ctl.voice_set(speak_in_window=bool(checked)), 8000)
        except Exception as e:  # noqa: BLE001
            self.error_box(e)
            self.refresh("Personality")

    def _speech_rate_changed(self):
        try:
            self.ctl.voice_set(rate=self.speech_rate.value() / 100.0)
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _persona_apply(self):
        try:
            self.statusBar().showMessage(self.ctl.persona_set(self.persona_combo.currentData()), 8000)
        except ValueError as e:                 # includes the restricted-style reason: shown plainly, nothing changed
            self.statusBar().showMessage(str(e), 12000)
            self._refresh_personality()
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _persona_normal(self):
        self.statusBar().showMessage(self.ctl.persona_normal(), 8000)

    def _persona_reset(self):
        self.statusBar().showMessage(self.ctl.persona_reset(), 8000)

    def _persona_level(self, key, value):
        try:
            self.ctl.persona_level(key, value)
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _persona_control(self, key, value):
        try:
            self.ctl.persona_control(key, value)
        except Exception as e:  # noqa: BLE001
            self.error_box(e)

    def _persona_adult(self, checked):
        try:
            self.statusBar().showMessage(self.ctl.persona_adult(bool(checked), confirmed=bool(checked)), 8000)
        except Exception as e:  # noqa: BLE001
            self.error_box(e)
            self.refresh("Personality")

    def _persona_preview(self):
        try:
            pv = self.ctl.persona_preview(self.persona_combo.currentData())
        except Exception as e:  # noqa: BLE001
            self.error_box(e)
            return
        if pv.get("restricted"):
            self.persona_text.setHtml(f"<h3>{html.escape(pv['label'])}</h3><p>{html.escape(pv['summary'])}</p>"
                                      f"<p><b>Temporarily unavailable:</b> {html.escape(pv['lock_reason'])}</p>")
            return
        lock = "<p><b>Locked:</b> needs the adult opt-in.</p>" if pv["needs_opt_in"] else ""
        self.persona_text.setHtml(f"<h3>{html.escape(pv['label'])}</h3><p>{html.escape(pv['summary'])}</p>{lock}<p>{html.escape(pv['memory'])}</p>"
                                  f"<p><b>Instructions the model receives:</b></p><pre>{html.escape(pv['model_instructions'])}</pre>")

    # ------------------------------------------------------------ Status / settings / diagnostics
    def _status_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label("Status and settings", heading=True))
        self.about = _label("")
        v.addWidget(self.about)
        self.feat_table = _table(["Feature", "State", "Details", "What has been verified"])
        self.feat_table.setAccessibleName("Features and their verification status")
        v.addWidget(self.feat_table, 2)
        v.addWidget(_label("Model and voice readiness (what was measured; 'not verified' means no test has proved it):"))
        self.ready_table = _table(["Level", "State", "Detail"])
        self.ready_table.setAccessibleName("Model and voice readiness levels")
        v.addWidget(self.ready_table, 2)
        row = QHBoxLayout()
        row.addWidget(_label("Text size:", wrap=False))
        self.scale = QComboBox()
        self.scale.setAccessibleName("Text size")
        self.scale.addItems(list(theme.SCALES))
        self.scale.setCurrentText(str(self.settings.value("text_scale", "100%")))
        self.scale.currentTextChanged.connect(self._set_scale)
        row.addWidget(self.scale)
        row.addWidget(_btn("Open data folder", self._open_data, tip="Opens the folder holding your models, memory, missions and logs."))
        row.addWidget(_btn("Run AI setup again", self._run_setup, tip="Checks this PC, lets you pick or re-test the local model, and measures CPU against GPU. Downloads only after you press the download button."))
        row.addWidget(_btn("Export diagnostic report", self._export_diag, tip="Saves a report you can read first: hardware, runtime, acceleration and setup results. It has no conversations, memories or files."))
        row.addWidget(_btn("Copy diagnostics", self._copy_diag, tip="Copies a local report with no conversations or memories; secrets and your user name are removed."))
        row.addStretch(1)
        v.addLayout(row)
        self.diag = QPlainTextEdit()
        self.diag.setReadOnly(True)
        self.diag.setAccessibleName("Diagnostics report")
        v.addWidget(self.diag, 2)
        return w

    def _refresh_status(self):
        self.about.setText(f"{V.APP_NAME} {V.__version__} - channel: {V.RELEASE_CHANNEL}. Local only: this window sends nothing anywhere. "
                           "The chat model is a pretrained open model (Qwen3 1.7B) running on your PC; it is not the original from-scratch model.")
        _fill(self.feat_table, [[r["title"], r["state"], r["detail"], r["evidence"]] for r in self.ctl.features()])
        rd = self.ctl.readiness()
        _fill(self.ready_table, [[r["label"], r["state"], r["detail"]] for r in rd["model"] + rd["voice"]])
        self.diag.setPlainText(self.ctl.diagnostics_text())

    # ------------------------------------------------------------ System & Model Readiness (safe preflight; downloads nothing)
    def _readiness_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label(READINESS, heading=True))
        v.addWidget(_label("A safe preflight for the model tests. It only LOOKS: no model is downloaded, no AI runtime is installed, no model is loaded, and your "
                           "default model and settings are not changed. Checks marked 'internet' contact huggingface.co and read only the public description of "
                           "the model files. Nothing here proves the model will run; see 'Not yet verified' below."))
        self.pf_online = QCheckBox("Also check the model host's published information (needs internet - contacts huggingface.co, downloads no model)")
        self.pf_online.setChecked(True)
        self.pf_online.setAccessibleName("Include the online model information check")
        v.addWidget(self.pf_online)
        row = QHBoxLayout()
        self.pf_run = _btn("Run Safe Preflight", self._pf_run, primary=True, tip="Runs the read-only checks below. You are asked before anything uses the internet.")
        self.pf_cancel = _btn("Cancel", self._pf_cancel, tip="Stops after the current step. A request already sent to the model host finishes first (up to 20 seconds).", enabled=False)
        row.addWidget(self.pf_run)
        row.addWidget(self.pf_cancel)
        row.addWidget(_btn("Open results folder", self._pf_open, tip="Opens the folder with the saved results of earlier runs."))
        row.addWidget(_btn("Show diagnostics", self._pf_diag, tip="Opens the Status page: a diagnostic report (hardware, runtime, the app's own files) you can read, copy or save. Nothing is sent anywhere."))
        row.addStretch(1)
        v.addLayout(row)
        self.pf_bar = QProgressBar()
        self.pf_bar.setRange(0, 100)
        self.pf_bar.setAccessibleName("Preflight progress")
        v.addWidget(self.pf_bar)
        self.pf_current = _label("Not run yet.")
        self.pf_current.setTextFormat(Qt.TextFormat.PlainText)       # text that came from outside the app is never interpreted as markup
        self.pf_current.setAccessibleName("Current preflight step")
        v.addWidget(self.pf_current)
        self.pf_verdict = _label("")
        self.pf_verdict.setTextFormat(Qt.TextFormat.PlainText)
        self.pf_verdict.setAccessibleName("Preflight result")
        v.addWidget(self.pf_verdict)
        self.pf_table = _table(["Check", "Needs internet", "Result", "Detail"])
        self.pf_table.setAccessibleName("Preflight checks")
        v.addWidget(self.pf_table, 3)
        v.addWidget(_label("Model files (published by the host; not independently verified; nothing downloaded):"))
        self.pf_models = _table(["Model file", "Repository", "Size in bytes (published)", "Published SHA-256", "Status"])
        self.pf_models.setAccessibleName("Model metadata")
        v.addWidget(self.pf_models, 2)
        v.addWidget(_label("Not yet verified:"))
        self.pf_blockers = QPlainTextEdit()
        self.pf_blockers.setReadOnly(True)
        self.pf_blockers.setAccessibleName("Remaining blockers")
        v.addWidget(self.pf_blockers, 2)
        v.addWidget(_label("Earlier runs:"))
        self.pf_hist = _table(["Started (UTC)", "Result", "Online check", "App version"])
        self.pf_hist.setAccessibleName("Earlier preflight runs")
        v.addWidget(self.pf_hist, 1)
        return w

    def _refresh_system_model_readiness(self):
        st = self.ctl.preflight_state()
        running = bool(st["running"])
        self.pf_run.setEnabled(not running)
        self.pf_cancel.setEnabled(running)
        self.pf_online.setEnabled(not running)
        self.pf_bar.setValue(int(round(100 * float(st["fraction"]))))
        res = st["result"]
        if running:
            self.pf_current.setText("Running: " + str(st["current"]))
        elif st["error"]:
            self.pf_current.setText(str(st["error"]))
        elif st["message"]:
            self.pf_current.setText(str(st["message"]))
        elif res:
            self.pf_current.setText("Finished.")
        else:
            self.pf_current.setText("Not run yet.")
        if res:
            v = res["verdict"]
            note = "" if not res.get("history_error") else "  " + res["history_error"]
            saved = "  (Guest session: this result is not saved.)" if res.get("guest_session_not_saved") else ""
            self.pf_verdict.setText(f"{v['code']}: {v['text']}{note}{saved}")
            _fill(self.pf_table, [[c["title"], "yes" if c["needs_internet"] else "no", c["label"], c["detail"]] for c in res["checks"]])
            _fill(self.pf_models, [[m.get("file"), m.get("repo"), m.get("size_bytes") if m.get("size_bytes") is not None else "unknown", m.get("published_sha256") or "unknown",
                                    m.get("status") + (" (" + ", ".join(m.get("problems") or []) + ")" if m.get("problems") else "")] for m in res.get("models", [])])
            self.pf_blockers.setPlainText("\n".join("- " + b for b in res.get("blockers", [])))
        elif not running:
            self.pf_verdict.setText("")
        _fill(self.pf_hist, [[h.get("started_at_utc") or h["run_id"], h.get("verdict") or "unreadable record", "yes" if h.get("include_online") else "no", h.get("app_version") or ""]
                             for h in self.ctl.preflight_history()])

    def _pf_run(self):
        online, consent = self.pf_online.isChecked(), False
        if online:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Use the internet for this check?")
            box.setText("Jarvis will contact huggingface.co once to read the public description of the model files (names, sizes, published checksums). "
                        "No model is downloaded and nothing about you or your PC is sent except what any web request reveals (your internet address).")
            allow = box.addButton("Allow and run all checks", QMessageBox.ButtonRole.AcceptRole)
            offline = box.addButton("Run offline checks only", QMessageBox.ButtonRole.ActionRole)
            cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(cancel)
            box.setEscapeButton(cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is cancel or clicked is None:
                self.statusBar().showMessage("Nothing was started.", 5000)
                return
            online, consent = (clicked is allow), (clicked is allow)
            if clicked is offline:
                online = False
        if not self.ctl.preflight_start(online, consent):
            self.statusBar().showMessage(str(self.ctl.preflight_state()["message"] or "The preflight did not start."), 8000)

    def _pf_cancel(self):
        self.ctl.preflight_cancel()

    def _pf_diag(self):
        self.nav.setCurrentRow(PAGES.index("Status"))

    def _pf_open(self):
        folder = self.ctl.preflight_folder()
        if not os.path.isdir(folder):
            QMessageBox.information(self, V.APP_NAME, "No preflight has been saved yet.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(folder)):
            QMessageBox.information(self, V.APP_NAME, "Could not open the folder. It is the 'preflight' folder inside your Jarvis data folder.")

    # ------------------------------------------------------------ Import Brain (the logic is in jarvis.brains; this page only shows it and calls the controller)
    def _account_page(self):
        self.account_ctl = AccountController.from_config(self.ctl.cfg)
        self.account_page = AccountPage(self.account_ctl, guest=bool(self.ctl.guest), parent=self)
        return self.account_page

    def _refresh_account(self):
        self.account_page.refresh_view()                   # local files only: opening the window never contacts any service

    def _import_brain_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_label(IMPORT_BRAIN, heading=True))
        v.addWidget(_label("Choose, import, test and switch the local AI model ('brain') that Jarvis uses. Everything stays on this PC. Jarvis never falls back to a "
                           "cloud model or a paid service. A model file is only read, never run."))
        self._ib_ids, self._ib_cat_ids, self._ib_handled, self._ib_dialog_open = [], [], None, False
        tabs = QTabWidget()
        tabs.setAccessibleName("Import Brain sections")
        self.ib_tabs = tabs
        # -- Active Brain
        t = QWidget()
        tv = QVBoxLayout(t)
        self.ib_active = _label("Reading the brain library...")
        self.ib_active.setTextFormat(Qt.TextFormat.PlainText)
        self.ib_active.setAccessibleName("Active brain")
        tv.addWidget(self.ib_active)
        row = QHBoxLayout()
        self.ib_deactivate = _btn("Deactivate", self._ib_deactivate, tip="Releases the brain from memory. Nothing is deleted; chat will say that no brain is active.")
        self.ib_previous = _btn("Return to previous brain", self._ib_previous, tip="Switches back to the brain that was active before the last switch (tested first).")
        row.addWidget(self.ib_deactivate)
        row.addWidget(self.ib_previous)
        row.addWidget(_btn("Refresh", self._ib_refresh, tip="Re-reads the library, this PC's hardware and the model list."))
        row.addStretch(1)
        tv.addLayout(row)
        tv.addStretch(1)
        tabs.addTab(t, "Active Brain")
        # -- Import Model
        t = QWidget()
        tv = QVBoxLayout(t)
        tv.addWidget(_label("Add a .gguf model file you already have. By default it is registered where it is (not copied). Only its header is read to check it."))
        row = QHBoxLayout()
        self.ib_path = QLineEdit()
        self.ib_path.setPlaceholderText("Path to a .gguf file")
        self.ib_path.setAccessibleName("Model file path")
        row.addWidget(self.ib_path, 1)
        row.addWidget(_btn("Browse...", self._ib_browse))
        tv.addLayout(row)
        self.ib_name = QLineEdit()
        self.ib_name.setPlaceholderText("Optional display name")
        self.ib_name.setAccessibleName("Display name")
        tv.addWidget(self.ib_name)
        self.ib_copy = QCheckBox("Copy the file into Jarvis's model folder (uses disk space). Unticked: register it where it is.")
        self.ib_copy.setAccessibleName("Copy the file")
        tv.addWidget(self.ib_copy)
        row = QHBoxLayout()
        self.ib_inspect_btn = _btn("Check file", self._ib_inspect, tip="Reads the file header and says what it is and whether this PC can run it. Changes nothing.")
        self.ib_import_btn = _btn("Import", self._ib_import, primary=True, tip="Adds the model to your library. It is NOT activated automatically.")
        row.addWidget(self.ib_inspect_btn)
        row.addWidget(self.ib_import_btn)
        row.addStretch(1)
        tv.addLayout(row)
        self.ib_inspect_out = QPlainTextEdit()
        self.ib_inspect_out.setReadOnly(True)
        self.ib_inspect_out.setAccessibleName("File check result")
        tv.addWidget(self.ib_inspect_out, 1)
        tabs.addTab(t, "Import Model")
        # -- Browse Models
        t = QWidget()
        tv = QVBoxLayout(t)
        tv.addWidget(_label("Models Jarvis knows how to download. A download starts only when you press Download and confirm. It is checked afterwards and is NOT activated automatically."))
        self.ib_cat = _table(["Name", "Repository", "Architecture", "Quantization", "Size", "License", "Memory needed", "On this PC", "Installed"])
        self.ib_cat.setAccessibleName("Models available to download")
        self.ib_cat.itemSelectionChanged.connect(self._ib_cat_selected)
        tv.addWidget(self.ib_cat, 2)
        self.ib_cat_detail = QPlainTextEdit()
        self.ib_cat_detail.setReadOnly(True)
        self.ib_cat_detail.setAccessibleName("Model details")
        tv.addWidget(self.ib_cat_detail, 2)
        row = QHBoxLayout()
        self.ib_download_btn = _btn("Download...", self._ib_download, primary=True, tip="Shows size, license, disk space and warnings first; nothing is downloaded until you confirm.")
        row.addWidget(self.ib_download_btn)
        row.addStretch(1)
        tv.addLayout(row)
        tabs.addTab(t, "Browse Models")
        # -- Installed Brains
        t = QWidget()
        tv = QVBoxLayout(t)
        self.ib_table = _table(["Name", "Origin", "Architecture", "Quantization", "Size", "Status", "On this PC", "Last test"])
        self.ib_table.setAccessibleName("Installed brains")
        self.ib_table.itemSelectionChanged.connect(self._ib_selected)
        tv.addWidget(self.ib_table, 2)
        self.ib_detail = QPlainTextEdit()
        self.ib_detail.setReadOnly(True)
        self.ib_detail.setAccessibleName("Brain details")
        tv.addWidget(self.ib_detail, 2)
        row = QHBoxLayout()
        self.ib_test_btn = _btn("Test", self._ib_test, tip="Loads the selected brain and makes it answer a real question.")
        self.ib_activate_btn = _btn("Activate", self._ib_activate, primary=True,
                                    tip="Tests the brain first; the switch happens only if it answers. Otherwise your current brain is kept.")
        self.ib_remove_btn = _btn("Remove...", self._ib_remove_selected, danger=True, tip="Opens Remove Model for the selected brain. Asks before anything is removed.")
        for b in (self.ib_test_btn, self.ib_activate_btn, self.ib_remove_btn):
            row.addWidget(b)
        row.addStretch(1)
        tv.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(_label("Context window (tokens):"))
        self.ib_ctx = QSpinBox()
        self.ib_ctx.setRange(512, 131072)
        self.ib_ctx.setSingleStep(512)
        self.ib_ctx.setValue(4096)
        self.ib_ctx.setAccessibleName("Context window in tokens")
        self.ib_ctx.setToolTip("How much conversation the brain can see at once. Larger uses more memory. Applies the next time the brain is loaded.")
        row.addWidget(self.ib_ctx)
        row.addWidget(_btn("Set", self._ib_set_context, tip="Saves this limit for the selected brain (never above what the model supports)."))
        row.addStretch(1)
        tv.addLayout(row)
        tabs.addTab(t, "Installed Brains")
        # -- Test Brain
        t = QWidget()
        tv = QVBoxLayout(t)
        tv.addWidget(_label("Loads the brain selected in Installed Brains and asks it a real question. The answer shown below was generated by the model, not by Jarvis."))
        self.ib_test_target = _label("No brain selected.")
        self.ib_test_target.setTextFormat(Qt.TextFormat.PlainText)
        tv.addWidget(self.ib_test_target)
        row = QHBoxLayout()
        self.ib_test_btn2 = _btn("Run test", self._ib_test, primary=True)
        row.addWidget(self.ib_test_btn2)
        row.addStretch(1)
        tv.addLayout(row)
        self.ib_test_out = QPlainTextEdit()
        self.ib_test_out.setReadOnly(True)
        self.ib_test_out.setAccessibleName("Test result")
        tv.addWidget(self.ib_test_out, 1)
        tabs.addTab(t, "Test Brain")
        # -- Hardware and Diagnostics
        t = QWidget()
        tv = QVBoxLayout(t)
        self.ib_hw = QPlainTextEdit()
        self.ib_hw.setReadOnly(True)
        self.ib_hw.setAccessibleName("Hardware and diagnostics")
        tv.addWidget(self.ib_hw, 1)
        tv.addWidget(_btn("Refresh", self._ib_refresh))
        tabs.addTab(t, "Hardware and Diagnostics")
        # -- Remove Model
        t = QWidget()
        tv = QVBoxLayout(t)
        tv.addWidget(_label("Removing a model from the library and deleting a file are different. Pick a brain in Installed Brains, then press Remove there. "
                            "You are shown exactly what will happen and must confirm."))
        self.ib_remove_info = _label("No brain selected.")
        self.ib_remove_info.setTextFormat(Qt.TextFormat.PlainText)
        tv.addWidget(self.ib_remove_info)
        self.ib_delete_file = QCheckBox("Also delete the model file from my disk (cannot be undone)")
        self.ib_delete_file.setAccessibleName("Also delete the file")
        tv.addWidget(self.ib_delete_file)
        tv.addStretch(1)
        tabs.addTab(t, "Remove Model")
        v.addWidget(tabs, 1)
        self.ib_bar = QProgressBar()
        self.ib_bar.setRange(0, 100)
        self.ib_bar.setAccessibleName("Brain operation progress")
        v.addWidget(self.ib_bar)
        row = QHBoxLayout()
        self.ib_status = _label("")
        self.ib_status.setTextFormat(Qt.TextFormat.PlainText)       # replies, paths and reasons come from outside: never interpreted as markup
        self.ib_status.setAccessibleName("Brain operation status")
        row.addWidget(self.ib_status, 1)
        self.ib_cancel = _btn("Cancel", self._ib_cancel, tip="Stops the running operation. A model that is already loading finishes loading first and is then released.", enabled=False)
        row.addWidget(self.ib_cancel)
        v.addLayout(row)
        return w

    def _ib_selected_id(self):
        r = self.ib_table.currentRow()
        return self._ib_ids[r] if 0 <= r < len(self._ib_ids) else None

    def _ib_refresh_buttons(self, running):
        sid = self._ib_selected_id()
        for b in (self.ib_test_btn, self.ib_test_btn2, self.ib_activate_btn, self.ib_remove_btn):
            b.setEnabled(not running and sid is not None)
        self.ib_deactivate.setEnabled(not running)
        self.ib_previous.setEnabled(not running)
        self.ib_import_btn.setEnabled(not running)
        self.ib_inspect_btn.setEnabled(not running)
        self.ib_download_btn.setEnabled(not running and 0 <= self.ib_cat.currentRow() < len(self._ib_cat_ids))
        self.ib_cancel.setEnabled(running)

    def _refresh_import_brain(self):
        st = self.ctl.brain_state()
        data, running = st["data"], bool(st["running"])
        if data is None and not st["refreshing"] and self.stack.currentIndex() == PAGES.index(IMPORT_BRAIN):
            self.ctl.brain_refresh()           # only when the page is open: the library and hardware are read when you look, not at start-up
        if data:
            self.ib_active.setText(BVM.active_text(data["summary"]))
            inst = data["installed"]
            keep = self._ib_selected_id()
            self._ib_ids = [b["id"] for b in inst]
            _fill(self.ib_table, BVM.installed_rows(inst))
            if keep in self._ib_ids:
                self.ib_table.selectRow(self._ib_ids.index(keep))
            self._ib_cat_ids = [c["id"] for c in data["catalog"]]
            _fill(self.ib_cat, BVM.catalog_rows(data["catalog"]))
            self.ib_hw.setPlainText(BVM.hardware_text(data["diagnostics"]))
        else:
            self.ib_active.setText(BVM.active_text(None))
        self.ib_bar.setValue(int(round(100 * st["fraction"])) if st["fraction"] is not None else 0)
        res = st["result"]
        if running:
            self.ib_status.setText(f"Working ({st['op']}): {st['current']}")
        elif st["message"]:
            self.ib_status.setText(str(st["message"]))
        else:
            self.ib_status.setText(BVM.result_text(res))
        self._ib_refresh_buttons(running)
        self._ib_on_result(res, running)

    def _ib_on_result(self, res, running):
        """Follow-up dialogs that need the result of an earlier step (download details, warnings, removal preview). Each result is handled once."""
        if running or not res or res is self._ib_handled or self._ib_dialog_open:
            return
        self._ib_handled = res
        op, mid = res.get("op"), self.ctl.brain_state()["model_id"]
        if op == "inspect":
            self.ib_inspect_out.setPlainText(self._ib_inspect_text(res))
        elif op == "import":
            self.ib_inspect_out.setPlainText(self._ib_inspect_text(res) if res.get("gguf") else BVM.result_text(res))
        elif op in ("test", "activate", "previous") and res.get("kind") == "needs_confirmation":
            if self._ib_confirm("Use this brain anyway?", "\n".join(res.get("warnings") or []) + "\n\nThis model may be too heavy or uncertain on this PC. Continue?", "Continue"):
                self.ctl.brain_run(op, model_id=mid, accept_warnings=True)
        elif op in ("test",) and res.get("test"):
            self.ib_test_out.setPlainText(BVM.result_text(res))
        elif op == "preview-download" and res.get("model_id"):
            if res.get("ok") is False and not res.get("warnings"):
                return
            if self._ib_confirm("Download this model?", BVM.download_text(res), "Download"):
                self.ctl.brain_run("download", model_id=res["model_id"], consent=True)
        elif op == "preview-remove" and res.get("ok"):
            delete = self.ib_delete_file.isChecked() and bool(res.get("file_deletion"))
            text = BVM.remove_text(res) + ("\n\nThe file WILL be deleted." if delete else "\n\nYour files will not be deleted.")
            if res.get("is_active"):
                QMessageBox.information(self, V.APP_NAME, text)
            elif self._ib_confirm("Remove this model?", text, "Remove"):
                self.ctl.brain_run("remove", model_id=res["id"], confirmed=True, delete_file=delete)

    def _ib_inspect_text(self, res):
        if not res.get("ok") and not res.get("gguf"):
            return BVM.result_text(res)
        g, c = res.get("gguf") or {}, res.get("compat")
        head = [f"File: {res.get('file_name', '')}", f"Format: {res.get('format', 'GGUF')}", f"Architecture: {g.get('architecture') or 'unknown'}   "
                f"Quantization: {g.get('quantization') or 'unknown'}   Size: {BVM.size_text(res.get('size_bytes'))}",
                f"On this PC: {BVM.compat_text(c)}", BVM.reasons_text(c), res.get("verification", ""), BVM.result_text(res) if "model_id" in res or not res.get("ok") else ""]
        return "\n".join(x for x in head if x)

    def _ib_confirm(self, title, text, yes_label):
        self._ib_dialog_open = True
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(title)
            box.setText(text)
            yes = box.addButton(yes_label, QMessageBox.ButtonRole.AcceptRole)
            no = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(no)
            box.setEscapeButton(no)
            box.exec()
            return box.clickedButton() is yes
        finally:
            self._ib_dialog_open = False

    def _ib_start(self, op, **kw):
        if not self.ctl.brain_run(op, **kw):
            self.statusBar().showMessage(str(self.ctl.brain_state()["message"] or "That did not start."), 8000)

    def _ib_browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a GGUF model file", "", "GGUF models (*.gguf);;All files (*)")
        if path:
            self.ib_path.setText(path)

    def _ib_inspect(self):
        self._ib_start("inspect", path=self.ib_path.text().strip())

    def _ib_import(self):
        self._ib_start("import", path=self.ib_path.text().strip(), name=self.ib_name.text().strip() or None, copy=self.ib_copy.isChecked())

    def _ib_selected(self):
        sid = self._ib_selected_id()
        data = self.ctl.brain_state()["data"]
        rec = next((b for b in (data or {}).get("installed", []) if b["id"] == sid), None)
        self.ib_detail.setPlainText(BVM.installed_detail(rec) if rec else "")
        if rec and rec.get("context_tokens"):
            self.ib_ctx.setValue(int(rec["context_tokens"]))
        self.ib_test_target.setText(f"Selected brain: {rec['name']}" if rec else "No brain selected.")
        self.ib_remove_info.setText(f"Selected brain: {rec['name']}  ({rec['id']})" if rec else "No brain selected.")
        self._ib_refresh_buttons(bool(self.ctl.brain_state()["running"]))

    def _ib_cat_selected(self):
        r = self.ib_cat.currentRow()
        data = self.ctl.brain_state()["data"]
        if data and 0 <= r < len(data["catalog"]):
            self.ib_cat_detail.setPlainText(BVM.catalog_detail(data["catalog"][r]))
        self._ib_refresh_buttons(bool(self.ctl.brain_state()["running"]))

    def _ib_download(self):
        r = self.ib_cat.currentRow()
        if 0 <= r < len(self._ib_cat_ids):
            self._ib_start("preview-download", model_id=self._ib_cat_ids[r])

    def _ib_test(self):
        sid = self._ib_selected_id()
        if sid:
            self._ib_start("test", model_id=sid)

    def _ib_activate(self):
        sid = self._ib_selected_id()
        if sid:
            self._ib_start("activate", model_id=sid)

    def _ib_set_context(self):
        sid = self._ib_selected_id()
        if sid:
            self._ib_start("context", model_id=sid, n_ctx=int(self.ib_ctx.value()))

    def _ib_deactivate(self):
        self._ib_start("deactivate")

    def _ib_previous(self):
        self._ib_start("previous")

    def _ib_remove_selected(self):
        sid = self._ib_selected_id()
        if sid:
            self.ib_tabs.setCurrentIndex(6)
            self._ib_start("preview-remove", model_id=sid)

    def _ib_refresh(self):
        self.ctl.brain_refresh()

    def _ib_cancel(self):
        self.ctl.brain_cancel()

    def _set_scale(self, text):
        self.settings.setValue("text_scale", text)
        self._apply_theme()

    def _open_data(self):
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ctl.cfg.data_dir))):
            QMessageBox.information(self, V.APP_NAME, f"Could not open the folder. It is: {self.ctl.cfg.data_dir}")

    def _run_setup(self):
        from jarvis.ui.setup_dialog import SetupDialog
        SetupDialog(self.ctl, self).exec()
        self.refresh_all()

    def _export_diag(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save diagnostic report", "jarvis-diagnostics.json", "JSON (*.json)")
        if not path:
            return
        try:
            where = self.ctl.export_diagnostic_report(path)
        except OSError as e:
            QMessageBox.information(self, V.APP_NAME, f"The report could not be saved: {e}")
            return
        QMessageBox.information(self, V.APP_NAME, f"Saved to {where}. Open it in a text editor to see exactly what it contains; "
                                "the list of included fields is at the end of the report.")

    def _copy_diag(self):
        QApplication.clipboard().setText(self.ctl.diagnostics_text())
        self.statusBar().showMessage("Diagnostics copied.", 5000)

    # ------------------------------------------------------------ closing
    def closeEvent(self, e):
        running = self.ctl.tasks.running()
        if running:
            r = QMessageBox.question(self, "Close Jarvis?",
                                     "Work is still running: " + ", ".join(running) + ".\n\nIt will be asked to stop. A step that is already in progress cannot be "
                                     "interrupted and may finish after the window closes.\n\nClose anyway?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.timer.stop()
        self.home_timer.stop()
        self.hud.timer.stop()                  # no frame or refresh may run against a controller that is shutting down
        res = self.ctl.shutdown(timeout=3.0)
        if res["still_running"]:
            QMessageBox.information(self, V.APP_NAME, "These were still running and could not be interrupted: " + ", ".join(res["still_running"])
                                    + ". They end by themselves; their results are recorded in the mission history.")
        e.accept()
