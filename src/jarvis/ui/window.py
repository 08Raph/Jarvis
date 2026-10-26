"""The PySide6 window. A THIN view: every action calls AppController (src/jarvis/app/controller.py), which calls the existing Jarvis code.
STATUS: this file has been syntax- and name-checked on Linux but has NEVER been run with PySide6 (not installable in the authoring sandbox) and never on
Windows. docs/PROMPT10_REVIEW.md and docs/WINDOWS_TEST_CHECKLIST.md section 6k list what must be checked by running it.
Rule for this file: a visible control either works or is disabled with a tooltip that says why."""
from __future__ import annotations

import html
import os
import sys

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from jarvis import version as V
from jarvis.app.controller import AppController, explain_error
from jarvis.ui import theme

PAGES = ["Chat", "Missions", "Schedules", "Memory", "Personality", "Voice", "Safety", "Status"]


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
        self.setMinimumSize(900, 600)
        self.resize(1100, 720)
        ctl.on_update = self.on_update
        ctl.ask_user = self.ask_user
        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(170)
        self.nav.setAccessibleName("Sections")
        for n in PAGES:
            self.nav.addItem(n)
        self.stack = QStackedWidget()
        self.pages = {"Chat": self._chat_page(), "Missions": self._missions_page(), "Schedules": self._schedules_page(),
                      "Memory": self._memory_page(), "Personality": self._personality_page(), "Voice": self._voice_page(), "Safety": self._safety_page(), "Status": self._status_page()}
        for n in PAGES:
            self.stack.addWidget(self.pages[n])
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
        QApplication.instance().setStyleSheet(theme.stylesheet(self.pal, scale))
        self.render_chat()

    def _goto(self, row):
        if row >= 0:
            self.stack.setCurrentIndex(row)
            self.refresh(PAGES[row])

    # ------------------------------------------------------------ updates from the controller (always on the interface thread)
    def on_update(self, topic):
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

    def refresh_all(self):
        for n in PAGES:
            self.refresh(n)

    def refresh(self, name):
        try:
            getattr(self, "_refresh_" + name.lower())()
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

    def _refresh_safety(self):
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
                           "Normal is the default. Adult-only styles stay locked until you opt in below.", muted=True))
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
        self.persona_adult = QCheckBox("I am 18 or older and want adult-only styles (Savage, Flirty, Romantic) available")
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
            lock = "" if d["available"] else "  (locked: needs adult opt-in)"
            self.persona_combo.addItem(d["label"] + (" [roleplay]" if d["roleplay"] else "") + lock, d["id"])
            if d["current"]:
                self.persona_combo.setCurrentIndex(self.persona_combo.count() - 1)
        self.persona_combo.blockSignals(False)
        for k, sp in self.persona_spins.items():
            sp.blockSignals(True)
            sp.setValue(st["settings"][k])
            sp.blockSignals(False)
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
        su = self.ctl.startup_state()
        self.startup_box.blockSignals(True)
        self.startup_box.setChecked(su["enabled"])
        self.startup_box.setEnabled(su["supported"])
        self.startup_box.blockSignals(False)
        self.startup_note.setText(su["note"] + ((" Command: " + su["command"]) if su["enabled"] and su["command"] else ""))

    def _voice_enable(self):
        try:
            self.ctl.voice_enable("wake_word")
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
        res = self.ctl.shutdown(timeout=3.0)
        if res["still_running"]:
            QMessageBox.information(self, V.APP_NAME, "These were still running and could not be interrupted: " + ", ".join(res["still_running"])
                                    + ". They end by themselves; their results are recorded in the mission history.")
        e.accept()
