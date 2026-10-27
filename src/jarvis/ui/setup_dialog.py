"""First-run setup wizard window (Prompt 20). A THIN view over AppController.setup_preview / run_setup (all logic and honesty rules live in
jarvis/app/setup_engine.py and are tested without Qt).
STATUS: written on Linux without PySide6; it has NEVER been displayed. Its logic is covered by tests/test_p20_setup.py through the controller; the
layout, buttons and progress display are unverified until run on Windows (docs/WINDOWS_VALIDATION_P20.md)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout)

from jarvis import version as V


class SetupDialog(QDialog):
    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self.preview: dict = {}
        self.running = False
        self._close_after_cancel = False
        self.setWindowTitle(f"{V.APP_NAME} setup")
        self.setMinimumWidth(640)
        v = QVBoxLayout(self)
        h = QLabel("Set up the local AI")
        h.setProperty("heading", True)
        v.addWidget(h)
        self.intro = QLabel("Jarvis runs its AI on this PC. This checks your computer, picks a suitable free model, downloads it once, and tests that it really answers. "
                            "Nothing you type is sent anywhere.")
        self.intro.setWordWrap(True)
        v.addWidget(self.intro)
        self.system = QPlainTextEdit()
        self.system.setReadOnly(True)
        self.system.setAccessibleName("This PC")
        v.addWidget(self.system, 2)
        self.choice = QComboBox()
        self.choice.setAccessibleName("Model")
        self.choice.currentIndexChanged.connect(self._model_changed)
        v.addWidget(self.choice)
        self.model_info = QLabel("")
        self.model_info.setWordWrap(True)
        v.addWidget(self.model_info)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.step = QLabel("")
        self.step.setWordWrap(True)
        v.addWidget(self.step)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setAccessibleName("Setup results")
        self.result.setVisible(False)
        v.addWidget(self.result, 2)
        row = QHBoxLayout()
        self.go = QPushButton("Download and set up")
        self.go.setProperty("primary", True)
        self.go.clicked.connect(self._start)
        self.go.setEnabled(False)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self._cancel)
        self.launch = QPushButton("Launch Jarvis")
        self.launch.setProperty("primary", True)
        self.launch.setVisible(False)
        self.launch.clicked.connect(self.accept)
        self.later = QPushButton("Set up later")
        self.later.setToolTip("Jarvis opens without a model; chat will say so. You can run setup again from the Status page.")
        self.later.clicked.connect(self.reject)
        for b in (self.go, self.cancel, self.launch, self.later):
            row.addWidget(b)
        v.addLayout(row)
        self.step.setText("Checking this PC...")
        self.ctl.tasks.submit("setup-preview", lambda tok: self.ctl.setup_preview(), self._preview_done)

    # ---------------------------------------------------------------- step 1-2
    def _preview_done(self, res, err):
        if err or not isinstance(res, dict):
            self.step.setText(f"The check could not run: {err}")
            return
        self.preview = res
        s, m = res["system"], res["model"]
        hw, acc = s["hardware"], s["acceleration"]
        lines = [f"Windows: {s['windows']}", f"CPU threads: {hw['cpu_threads']}   RAM: {hw['ram_gb'] and round(hw['ram_gb'], 1)} GB   Free disk: {hw['free_disk_gb'] and round(hw['free_disk_gb'], 1)} GB",
                 f"GPU: {hw['gpu_name'] or 'none detected'}" + (f" ({hw['vram_gb']:.0f} GB video memory)" if hw.get("vram_gb") else ""),
                 f"GPU detected: {acc['gpu_detected']}   GPU-capable AI runtime present: {acc['backend_available']}   GPU speed-up verified: {acc['acceleration_verified']}",
                 f"AI runtime: {'present, version ' + str(s['runtime'].get('version')) if s['runtime'].get('available') else 'MISSING - ' + str(s['runtime'].get('error', ''))}",
                 f"Hardware tier: {s['tier']}", acc["message"]]
        lines += [f"Warning: {w}" for w in s["warnings"]] + [f"Problem: {b}" for b in s["blockers"]]
        self.system.setPlainText("\n".join(lines))
        self.choice.clear()
        for r in m["models"]:
            tag = "" if r["metadata_confirmed"] else " (size/licence not confirmed)"
            fit = "" if r["fits"] else " - will not fit this PC"
            self.choice.addItem(f"{r['title']}{tag}{fit}", r["id"])
        if m["recommended"]:
            self.choice.setCurrentIndex(max(0, self.choice.findData(m["recommended"])))
        self.step.setText("Review the details, then press the button to continue." if s["can_continue"] else "Setup cannot continue until the problem above is fixed.")
        self.go.setEnabled(bool(s["can_continue"]) and bool(m["models"]))
        self._model_changed()

    def _row(self):
        mid = self.choice.currentData()
        return next((r for r in self.preview.get("model", {}).get("models", []) if r["id"] == mid), None)

    def _model_changed(self, *_):
        r = self._row()
        if not r:
            return
        if r["installed"]:
            self.model_info.setText(f"{r['title']} is already installed. Setup will check its files and test it.")
            self.go.setText("Check and finish setup")
            return
        self.model_info.setText(f"{r['title']}: download about {r['download_gb']} GB from its publisher (once). Licence: {r['license']} ({r['license_url']}). "
                                f"Estimated memory needed: {r['memory_need_gb_estimate']} GB. This is a pretrained third-party model, not made by Jarvis."
                                + ("" if r["metadata_confirmed"] else " Its size and licence have not been confirmed by this build; check the publisher's page."))
        self.go.setText(f"Download {r['download_gb']} GB and set up")
        self.go.setEnabled(bool(r["fits"]) and self.preview.get("system", {}).get("can_continue", False))

    # ---------------------------------------------------------------- run
    def _start(self):
        r = self._row()
        if not r or self.running:
            return
        self.running = True
        self.go.setEnabled(False)
        self.choice.setEnabled(False)
        self.later.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.result.setVisible(False)
        ok = self.ctl.run_setup(r["id"], self._progress, self._done)
        if not ok:
            self._done({"failed": {"step": "start", "message": "Jarvis is shutting down."}, "completed": False})

    def _progress(self, step, frac, msg):
        self.step.setText(f"{step.capitalize()}: {msg}")
        if frac is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))

    def _cancel(self):
        if self.running:
            self.ctl.cancel_setup()
            self.step.setText("Cancelling...")
        else:
            self.reject()

    def reject(self):
        """Closing the window (X, Esc, Set up later) while setup is running must not leave a hidden download running that later switches the model on:
        cancel it first and close once the worker has stopped (`_done`)."""
        if self.running:
            self._close_after_cancel = True
            self.ctl.cancel_setup()
            self.step.setText("Cancelling...")
            return
        super().reject()

    def _done(self, rep):
        self.running = False
        if getattr(self, "_close_after_cancel", False):
            self._close_after_cancel = False
            super().reject()
            return
        self.bar.setRange(0, 1000)
        self.bar.setVisible(False)
        self.result.setVisible(True)
        self.later.setEnabled(True)
        lines = []
        f = rep.get("failed")
        v = rep.get("steps", {}).get("verify", {})
        for c in v.get("checks", []):
            lines.append(f"[{'ok' if c['ok'] else 'FAILED'}] {c['name']}: {c['detail']}")
        if f:
            lines.append("")
            lines.append(f"Setup did not finish ({f['step']}): {f['message']}")
            if f.get("recovery"):
                lines.append(f.get("recovery"))
            self.step.setText("Setup did not finish. You can retry, or set up later.")
            self.go.setText("Retry")
            self.go.setEnabled(True)
            self.choice.setEnabled(True)
        else:
            sm = rep.get("summary", {})
            lines += ["", f"Model: {sm.get('model')} ({sm.get('license')}), pretrained by its publisher.", f"Mode: {sm.get('mode', '?').upper()}. {sm.get('gpu_note', '')}",
                      f"Speed on this PC (CPU): {sm.get('tokens_per_second_cpu')} tokens/s", "CPU fallback: available.",
                      "Not yet checked here: voice, desktop control and offline start-up (see the Status page)."]
            if rep.get("completed"):
                self.step.setText("Setup finished. The model answered a real question on this PC.")
                self.launch.setVisible(True)
                self.go.setVisible(False)
            else:
                self.step.setText("This was a simulated run and is not recorded as complete.")
        self.result.setPlainText("\n".join(lines))
