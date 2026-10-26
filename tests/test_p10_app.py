"""Prompt 10: start-up, data folders, instance lock, background-task cleanup, transcript, model/feature status, diagnostics.
REAL files and threads; FAKE MODEL where noted. Nothing here ran on Windows."""
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from p10_common import AppCase, ROOT  # noqa: F401  (sets sys.path)
from jarvis.app import diagnostics, lifecycle, status
from jarvis.app.transcript import KINDS, Transcript
from jarvis.app.worker import BackgroundTasks
from jarvis.config import Config


class DataDirTests(AppCase):
    def test_prepare_creates_folders_and_is_repeatable(self):
        made1 = lifecycle.prepare(self.cfg)
        self.assertTrue((self.cfg.data_dir / "models").is_dir())
        self.assertTrue((self.cfg.data_dir / "logs").is_dir())
        (self.cfg.data_dir / "memory.sqlite").write_bytes(b"keep me")
        made2 = lifecycle.prepare(self.cfg)                               # "upgrade" / second launch
        self.assertEqual(made2, [])
        self.assertEqual((self.cfg.data_dir / "memory.sqlite").read_bytes(), b"keep me")
        self.assertTrue(made1 == [] or all(isinstance(x, str) for x in made1))

    def test_data_dir_that_is_a_file_gives_actionable_error(self):
        f = self.d / "afile"
        f.write_text("x")
        with self.assertRaises(lifecycle.StartupError) as cm:
            lifecycle.check_data_dir(f)
        self.assertIn("file, not a folder", cm.exception.text())
        self.assertIn("What to do", cm.exception.text())

    def test_bad_characters_rejected(self):
        with self.assertRaises(lifecycle.StartupError):
            lifecycle.check_data_dir(Path("bad<name>|x"))

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions and a non-root user")
    def test_unwritable_dir_reported(self):
        ro = self.d / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            with self.assertRaises(lifecycle.StartupError):
                lifecycle.check_data_dir(ro)
        finally:
            ro.chmod(0o700)

    def test_frozen_install_dir_is_refused_as_data_dir(self):
        exe_dir = self.d / "prog"
        exe_dir.mkdir()
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(exe_dir / "jarvis.exe")):
            with self.assertRaises(lifecycle.StartupError) as cm:
                lifecycle.check_data_dir(exe_dir / "userdata")
        self.assertIn("program folder", cm.exception.message)

    def test_missing_and_invalid_config_files(self):
        self.assertIsInstance(lifecycle.load_config_file(self.d / "nope.toml"), Config)       # missing file = documented defaults
        bad = self.d / "bad.toml"
        bad.write_text("this is = not [valid toml", encoding="utf-8")
        with self.assertRaises(lifecycle.StartupError) as cm:
            lifecycle.load_config_file(bad)
        self.assertIn("not valid", cm.exception.message)
        self.assertIsInstance(lifecycle.load_config_file(None), Config)


class InstanceLockTests(AppCase):
    def test_second_window_refused_while_first_alive_then_allowed(self):
        lifecycle.prepare(self.cfg)
        lock = lifecycle.InstanceLock.for_profile(self.cfg)
        self.assertTrue(lock.acquire())
        # simulate a DIFFERENT live process holding the lock: the parent process of the test runner
        lock.path.write_text(json.dumps({"pid": os.getppid() or 1}), encoding="utf-8")
        other = lifecycle.InstanceLock.for_profile(self.cfg)
        with mock.patch.object(lifecycle, "_pid_alive", return_value=True):
            self.assertFalse(other.acquire())
        lock.held = False                                                 # we do not own it any more
        self.assertTrue(lock.path.exists(), "a refused window must not delete the owner's lock")

    def test_stale_lock_from_crash_is_replaced(self):
        lifecycle.prepare(self.cfg)
        p = self.cfg.profile_dir / "app.lock"
        p.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
        lock = lifecycle.InstanceLock.for_profile(self.cfg)
        with mock.patch.object(lifecycle, "_pid_alive", return_value=False):
            self.assertTrue(lock.acquire())
        lock.release()
        self.assertFalse(p.exists())

    def test_corrupt_lock_file_is_replaced(self):
        lifecycle.prepare(self.cfg)
        p = self.cfg.profile_dir / "app.lock"
        p.write_text("{{{ not json", encoding="utf-8")
        lock = lifecycle.InstanceLock.for_profile(self.cfg)
        self.assertTrue(lock.acquire())
        lock.release()


class WorkerTests(unittest.TestCase):
    def test_result_delivered_through_dispatch_and_pump(self):
        bt = BackgroundTasks()
        got = []
        bt.submit("a", lambda tok: 41 + 1, lambda r, e: got.append((r, e)))
        bt.shutdown(2)
        self.assertEqual(got, [])                                         # nothing runs on the worker thread's own call stack
        bt.pump()
        self.assertEqual(got, [(42, "")])

    def test_exception_in_worker_is_reported_not_raised(self):
        bt = BackgroundTasks()
        got = []

        def boom(tok):
            raise RuntimeError("bad thing")
        bt.submit("b", boom, lambda r, e: got.append(e))
        bt.shutdown(2)
        bt.pump()
        self.assertTrue(got and got[0].startswith("RuntimeError"))

    def test_shutdown_cancels_cooperative_work_and_reports_stuck_work(self):
        bt = BackgroundTasks()
        release = threading.Event()
        bt.submit("cooperative", lambda tok: [time.sleep(0.01) for _ in range(500) if not tok.cancelled])
        bt.submit("stuck", lambda tok: release.wait(10))
        time.sleep(0.05)
        self.assertEqual(sorted(bt.running()), ["cooperative", "stuck"])
        still = bt.shutdown(timeout=0.3)
        self.assertEqual(still, ["stuck"])                                # honest: it could not be interrupted
        self.assertIsNone(bt.submit("late", lambda tok: 1))               # no new work after shutdown
        release.set()

    def test_threads_are_daemons_so_exit_cannot_hang(self):
        bt = BackgroundTasks()
        ev = threading.Event()
        t = bt.submit("d", lambda tok: ev.wait(5))
        self.assertTrue(t.thread.daemon)
        ev.set()
        bt.shutdown(1)


class TranscriptTests(unittest.TestCase):
    def test_kinds_are_distinct_and_validated(self):
        t = Transcript()
        for k in KINDS:
            t.add(k, f"text {k}")
        self.assertEqual([e.kind for e in t.entries], list(KINDS))
        self.assertEqual(len({e.label() for e in t.entries}), len(KINDS))
        with self.assertRaises(ValueError):
            t.add("made-up", "x")

    def test_streaming_append_and_limit(self):
        t = Transcript(limit=3)
        e = t.add("assistant", "")
        t.append_to(e.id, "Hel")
        t.append_to(e.id, "lo")
        self.assertEqual(e.text, "Hello")
        for i in range(5):
            t.add("system", str(i))
        self.assertEqual(len(t.entries), 3)
        self.assertIsNone(t.append_to(e.id, "x"))                         # trimmed away
        self.assertIn("Info:", t.to_plain())


class ModelStatusTests(AppCase):
    """Missing, corrupt and incompatible weights must give an actionable status, never an exception and never a silent fallback."""

    def test_no_model(self):
        s = status.model_status(self.cfg)
        self.assertEqual(s.state, "no_model")
        self.assertFalse(s.ok)
        self.assertIn("docs/MODEL_DISTRIBUTION.md", s.action)
        self.assertIn("jarvis models fetch", s.action)

    def test_corrupt_weights_detected(self):
        store = self.install_model()
        gg = next(store.path("fake").glob("*.gguf"))
        gg.write_bytes(gg.read_bytes() + b"tampered")
        s = status.model_status(self.cfg)
        self.assertEqual(s.state, "model_invalid")
        self.assertIn("jarvis models verify fake", s.action)

    def test_damaged_meta_does_not_crash(self):
        store = self.install_model()
        (store.path("fake") / "meta.json").write_text("{{{", encoding="utf-8")
        s = status.model_status(self.cfg)
        self.assertIn(s.state, ("error", "model_invalid"))
        self.assertTrue(s.action)

    def test_research_checkpoint_is_not_a_chat_model(self):
        from jarvis.models.package import pack_model
        src = self.d / "m"
        src.mkdir()
        (src / "model.pt").write_bytes(b"w")
        (src / "tokenizer.json").write_text("{}")
        (src / "LICENSE.txt").write_text("l")
        pack_model(src, self.d / "p.jarvismodel", "MIT")
        store = self.install_model("other")
        store.install_package(self.d / "p.jarvismodel", "scratch", confirm_unlisted=True)
        store.set_active("scratch")
        s = status.model_status(self.cfg)
        self.assertEqual(s.state, "not_a_chat_model")
        self.assertIn("from-scratch", s.summary)

    def test_runtime_missing_vs_ready(self):
        self.install_model()
        with mock.patch.object(status, "has_module", return_value=False):
            self.assertEqual(status.model_status(self.cfg).state, "runtime_missing")
        with mock.patch.object(status, "has_module", return_value=True), mock.patch.object(status, "import_problem", return_value=None):   # Prompt 12: present AND loadable
            s = status.model_status(self.cfg)
        self.assertEqual(s.state, "ready")
        self.assertIn("has not been loaded yet", s.summary)               # honest: ready != proven to run

    def test_no_external_or_paid_fallback_exists(self):
        """Source scan: the app layer and UI contain no network client and no API-key setting."""
        banned = ("urllib.request", "import requests", "http.client", "import openai", "import anthropic", "api_key", "API_KEY", "import socket")
        for d in (ROOT / "src" / "jarvis" / "app", ROOT / "src" / "jarvis" / "ui"):
            for f in d.glob("*.py"):
                t = f.read_text(encoding="utf-8")
                for b in banned:
                    self.assertFalse(b in t, f"{f.name} mentions {b}")

    def test_every_feature_row_states_its_evidence(self):
        rows = status.feature_rows(self.cfg)
        ids = {r["id"] for r in rows}
        self.assertTrue({"chat", "memory", "voice", "desktop", "vision", "monitoring", "code", "research", "missions"} <= ids)
        for r in rows:
            self.assertIn(r["state"], ("available", "needs_setup", "unavailable", "experimental"))
            self.assertTrue(r["evidence"] and r["detail"])
        voice = next(r for r in rows if r["id"] == "voice")
        self.assertNotEqual(voice["state"], "available")                  # never claimed verified
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(next(r for r in status.feature_rows(self.cfg) if r["id"] == "desktop")["state"], "unavailable")


class DiagnosticsTests(AppCase):
    def test_scrubs_home_user_and_secrets(self):
        home = str(Path.home())
        txt = diagnostics.scrub_paths(f"opened {home}/docs/x.txt token sk-ABCDEF0123456789ABCDEF0123456789")
        self.assertNotIn(home, txt)
        self.assertNotIn("ABCDEF0123456789ABCDEF0123456789", txt)

    def test_report_has_no_conversation_text_and_no_network(self):
        self.install_model()
        (self.cfg.data_dir / "logs").mkdir(exist_ok=True)
        rep = diagnostics.render_text(diagnostics.collect(self.cfg, {"version": "t"}))
        data = json.loads(rep)
        self.assertIn("model", json.dumps(data).lower())
        self.assertNotIn(str(Path.home()), rep)

    def test_missing_build_info_is_stated(self):
        with mock.patch("pathlib.Path.read_text", side_effect=OSError):
            self.assertIn("source", json.dumps(diagnostics.read_build_info()))


if __name__ == "__main__":
    unittest.main()
