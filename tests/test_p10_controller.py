"""Prompt 10: the window's controller (everything except drawing). REAL: files, SQLite, executor/policy, mission store, threads.
FAKE MODEL: FakeLlama streams a scripted string. The desktop backend is SIMULATED (simulate=True). These tests say nothing about the Qt window itself
(no Qt was available) or about the real Qwen model."""
import json
import threading
import time
import unittest
from unittest import mock

from p10_common import AppCase, FakeLlama, fake_engine_factory, ROOT  # noqa: F401
from jarvis.app import controller as C
from jarvis.app.controller import AppController, Ask, explain_error


def settle(ctl, timeout=10.0):
    """Wait for background work and deliver its callbacks (the role the Qt event loop plays in the real window)."""
    end = time.time() + timeout
    while time.time() < end:
        ctl.tasks.pump()
        if not ctl.tasks.running():
            ctl.tasks.pump()
            return
        time.sleep(0.01)
    raise AssertionError("background work did not finish")


class SlowLlama(FakeLlama):
    def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
        def gen():
            for i in range(300):
                time.sleep(0.01)
                yield {"choices": [{"delta": {"content": "word "}}]}
        return gen()


class ChatTests(AppCase):
    def ctl(self, **kw):
        c = AppController(self.cfg, simulate=True, **kw)
        self.addCleanup(c.shutdown, 1.0)
        return c

    def test_no_model_shows_reason_and_never_invents_a_reply(self):
        c = self.ctl()
        self.assertFalse(c.send("hello"))
        kinds = [e.kind for e in c.transcript.entries]
        self.assertEqual(kinds, ["user", "error"])
        self.assertIn("No chat model", c.transcript.entries[-1].text)
        self.assertFalse(any(e.kind == "assistant" for e in c.transcript.entries))
        self.assertEqual(c.tasks.running(), [])

    def test_fake_model_reply_streams_into_one_assistant_entry(self):
        self.install_model()
        c = self.ctl(engine_factory=fake_engine_factory)
        self.assertTrue(c.send("hi"))
        self.assertTrue(c.busy)
        self.assertFalse(c.send("again"), "a second message while busy must not start a second reply")
        settle(c)
        self.assertFalse(c.busy)
        a = [e for e in c.transcript.entries if e.kind == "assistant"]
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].text, FakeLlama.reply)

    def test_model_failure_is_reported_in_plain_words(self):
        self.install_model()

        def bad(p):
            raise MemoryError()
        c = self.ctl(engine_factory=bad)
        c.send("hi")
        settle(c)
        err = [e for e in c.transcript.entries if e.kind == "error"]
        self.assertTrue(err and "Not enough memory" in err[-1].text)
        self.assertFalse(any(e.kind == "assistant" and e.text for e in c.transcript.entries))
        self.assertFalse(c.busy)

    def test_stop_button_stops_generation_and_says_so(self):
        self.install_model()
        from jarvis.inference import gguf_engine as ge
        c = self.ctl(engine_factory=lambda p: ge.GGUFEngine(p, llama_factory=SlowLlama))
        c.send("long please")
        time.sleep(0.2)
        t0 = time.time()
        self.assertTrue(c.stop_generation())
        settle(c, 5)
        self.assertLess(time.time() - t0, 2.0)
        self.assertTrue(any(e.kind == "system" and "Stopped" in e.text for e in c.transcript.entries))

    def test_overlong_and_empty_input(self):
        c = self.ctl()
        self.assertFalse(c.send("   "))
        self.assertFalse(c.send("x" * 9000))
        self.assertEqual(c.transcript.entries[-1].kind, "error")

    def test_explain_error_scrubs_the_home_folder_from_model_load_errors(self):
        from pathlib import Path
        from jarvis.inference.gguf_engine import RuntimeUnavailable
        model = str(Path.home() / "ProjectJarvis" / "models" / "m" / "model.gguf")
        for msg in (f"The model file could not be loaded: ValueError: Failed to load model from file: {model}",):
            for exc in (RuntimeUnavailable(msg), "RuntimeUnavailable: " + msg):
                out = explain_error(exc)
                self.assertNotIn(str(Path.home()), out)
                self.assertIn("could not be loaded", out)

    def test_explain_error_hides_unknown_details(self):
        self.assertIn("Unexpected problem", explain_error("ZeroDivisionError: secret"))
        self.assertNotIn("secret", explain_error("ZeroDivisionError: secret"))


class ConfirmationTests(AppCase):
    """The window can only ADD a question; with no answer the result is NO."""

    def ctl(self, ask=None):
        c = AppController(self.cfg, simulate=True, dispatch=lambda fn: fn(), ask_user=ask)
        self.addCleanup(c.shutdown, 1.0)
        return c

    class Req:
        description = "delete a file"
        tool = "delete_file"
        risk = "destructive"

    def test_no_view_means_no(self):
        self.assertFalse(self.ctl(None).confirm(self.Req()))

    def test_yes_and_no_answers(self):
        self.assertTrue(self.ctl(lambda r, resolve: resolve(True)).confirm(self.Req()))
        self.assertFalse(self.ctl(lambda r, resolve: resolve(False)).confirm(self.Req()))

    def test_dialog_that_crashes_means_no(self):
        def boom(r, resolve):
            raise RuntimeError("dialog failed")
        self.assertFalse(self.ctl(boom).confirm(self.Req()))

    def test_timeout_means_no(self):
        c = self.ctl(lambda r, resolve: None)
        with mock.patch.object(C, "CONFIRM_TIMEOUT_S", 0.2):
            self.assertFalse(c.confirm(self.Req()))

    def test_closing_the_window_while_asking_means_no(self):
        c = self.ctl(lambda r, resolve: None)
        out = []
        t = threading.Thread(target=lambda: out.append(c.confirm(self.Req())))
        t.start()
        time.sleep(0.2)
        c.shutdown(1.0)
        t.join(3)
        self.assertEqual(out, [False])

    def test_request_and_answer_are_both_visible_in_the_transcript(self):
        c = self.ctl(lambda r, resolve: resolve(False))
        c.confirm(self.Req())
        kinds = [e.kind for e in c.transcript.entries]
        self.assertEqual(kinds, ["confirm", "system"])
        self.assertIn("NOT allowed", c.transcript.entries[-1].text)


class SafetyTests(AppCase):
    def ctl(self):
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        return c

    def test_permissions_are_shown_but_the_controller_cannot_change_them(self):
        c = self.ctl()
        p = c.permissions()
        self.assertTrue(p["rows"])
        self.assertEqual({r["category"] for r in p["rows"] if r["enabled"]}, {"system"}, "only read-only system info is on by default")
        self.assertIn("deliberately cannot change", p["how_to_change"])
        for name in dir(c):
            self.assertFalse(name.startswith(("enable", "disable", "grant", "allow", "set_permission", "consent")), name)

    def test_ui_source_never_writes_permission_settings(self):
        for f in ("window.py", "app.py"):
            t = (ROOT / "src" / "jarvis" / "ui" / f).read_text(encoding="utf-8")
            for banned in ("desktop.policy", "Settings.load", ".save(", "record_consent", ".enabled[", "allow_folder"):
                self.assertFalse(banned in t, f"{f} contains {banned}")
        t = (ROOT / "src" / "jarvis" / "app" / "controller.py").read_text(encoding="utf-8")
        self.assertNotIn(".save(", t)

    def test_emergency_stop_round_trip(self):
        c = self.ctl()
        self.assertFalse(c.stop_active())
        self.assertIn("Emergency stop is ON", c.emergency_stop())
        self.assertTrue(c.stop_active())
        self.assertTrue((self.cfg.profile_dir / "STOP").exists())       # same file `jarvis desktop stop` writes
        c.clear_stop()
        self.assertFalse(c.stop_active())

    def test_modes_only_change_the_mode_file(self):
        c = self.ctl()
        self.assertEqual(c.modes()["current"], "")
        self.assertIn("never grants", c.set_mode("coding"))
        self.assertEqual(c.modes()["current"], "coding")
        self.assertEqual({r["category"] for r in c.permissions()["rows"] if r["enabled"]}, {"system"})   # unchanged by the mode
        c.set_mode("")
        self.assertEqual(c.modes()["current"], "")

    def test_action_history_ignores_damaged_lines(self):
        c = self.ctl()
        self.assertEqual(c.action_history(), [])
        (self.cfg.profile_dir).mkdir(parents=True, exist_ok=True)
        (self.cfg.profile_dir / "desktop_audit.jsonl").write_text('{"tool":"a"}\nnot json\n{"tool":"b"}\n', encoding="utf-8")
        self.assertEqual([r["tool"] for r in c.action_history()], ["a", "b"])


class MemoryTests(AppCase):
    def test_review_edit_export_delete_and_forget_all(self):
        from jarvis.memory.personal import PersonalMemory
        self.cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        mem = PersonalMemory.open(self.cfg.memory_db)
        a = mem.add("explicit_preference", "I like tea")["id"]
        b = mem.add("explicit_preference", "I like walking")["id"]
        mem.close()
        answers = []
        c = AppController(self.cfg, simulate=True, dispatch=lambda fn: fn(), ask_user=lambda r, res: res(answers.pop(0)))
        self.addCleanup(c.shutdown, 1.0)
        self.assertEqual(len(c.memory_items()), 2)
        c.memory_update(a, "I like green tea")
        self.assertTrue(any("green tea" in i["text"] for i in c.memory_items()))
        out = c.memory_export(self.d / "export.json")
        self.assertIn("green tea", out.read_text(encoding="utf-8"))
        self.assertTrue(c.memory_delete(b))
        self.assertEqual(len(c.memory_items()), 1)
        answers.append(False)                                            # declined: nothing may be deleted
        self.assertIsNone(c.memory_forget_all())
        self.assertEqual(len(c.memory_items()), 1)
        answers.append(True)
        self.assertIsInstance(c.memory_forget_all(), str)
        self.assertEqual(len(c.memory_items()), 0)

    def test_guest_has_no_memory_to_manage(self):
        c = AppController(self.cfg, guest=True, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        with self.assertRaises(ValueError):
            c.memory_items()
        self.assertEqual(c.missions(), [])
        self.assertEqual(c.schedules(), [])


PLAN = {"goal": "look at the system", "steps": [{"id": "s1", "tool": "system_info", "args": {}, "description": "read system info", "expect": "numbers"}]}


class MissionTests(AppCase):
    def ctl(self):
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        return c

    def plan_file(self, plan=PLAN):
        f = self.d / "plan.json"
        f.write_text(json.dumps(plan), encoding="utf-8")
        return f

    def test_file_plan_becomes_a_draft_and_nothing_runs(self):
        c = self.ctl()
        r = c.mission_from_file(self.plan_file())
        self.assertEqual(r["state"], "draft")
        d = c.mission_detail(r["id"])
        self.assertEqual(d["executions"], 0)
        self.assertEqual(c.tasks.running(), [])

    def test_bad_plan_files_are_refused(self):
        c = self.ctl()
        with self.assertRaises(ValueError):
            c.mission_from_file(self.d / "missing.json")
        with self.assertRaises(Exception):
            c.mission_from_file(self.plan_file({"goal": "x", "steps": [{"id": "s1", "tool": "no_such_tool", "args": {}}]}))
        self.assertEqual(c.missions(), [])

    def test_run_in_background_records_a_result_in_the_transcript(self):
        c = self.ctl()
        mid = c.mission_from_file(self.plan_file())["id"]
        self.assertTrue(c.mission_start(mid))
        self.assertFalse(c.mission_start(mid), "the same mission cannot be started twice at once")
        settle(c)
        self.assertEqual(c.mission_detail(mid)["state"], "completed")
        self.assertTrue(any(e.kind == "tool" for e in c.transcript.entries))
        self.assertNotIn(mid, c.running_missions)

    def test_wrong_hash_cannot_approve(self):
        c = self.ctl()
        mid = c.mission_from_file(self.plan_file({"goal": "g", "steps": [{"id": "s1", "tool": "system_info", "args": {}, "description": "d", "expect": "e"}]}))["id"]
        c.mission_review(mid)
        with self.assertRaises(Exception):
            c.mission_approve(mid, "deadbeef0000")
        self.assertNotEqual(c.mission_detail(mid)["state"], "approved")

    def test_reject_and_cancel(self):
        c = self.ctl()
        mid = c.mission_from_file(self.plan_file())["id"]
        self.assertIn("Rejected", c.mission_reject(mid))
        mid2 = c.mission_from_file(self.plan_file())["id"]
        c.mission_cancel(mid2)
        self.assertEqual(c.mission_detail(mid2)["state"], "cancelled")

    def test_planning_with_a_scripted_model_only_saves_a_draft(self):
        self.install_model()
        FakeLlama.reply = json.dumps(PLAN)
        c = AppController(self.cfg, simulate=True, engine_factory=fake_engine_factory)
        self.addCleanup(c.shutdown, 1.0)
        self.assertTrue(c.mission_plan("check the system"))
        settle(c)
        ms = c.missions()
        self.assertEqual(len(ms), 1)
        self.assertEqual(c.mission_detail(ms[0]["id"])["state"], "draft")
        self.assertTrue(any("PROPOSAL" in e.text for e in c.transcript.entries))

    def test_planning_garbage_saves_nothing(self):
        self.install_model()
        FakeLlama.reply = "I cannot do that."
        c = AppController(self.cfg, simulate=True, engine_factory=fake_engine_factory)
        self.addCleanup(c.shutdown, 1.0)
        c.mission_plan("check")
        settle(c)
        self.assertEqual(c.missions(), [])
        self.assertTrue(any(e.kind == "error" and "Nothing was saved" in e.text for e in c.transcript.entries))


class ShutdownTests(AppCase):
    def test_shutdown_releases_lock_and_second_start_works(self):
        from jarvis.app import lifecycle
        lifecycle.prepare(self.cfg)
        c1 = AppController(self.cfg, simulate=True)
        self.assertTrue(c1.start())
        lock = self.cfg.profile_dir / "app.lock"
        self.assertTrue(lock.exists())
        self.assertEqual(c1.shutdown(1.0), {"still_running": []})
        self.assertFalse(lock.exists())
        c2 = AppController(self.cfg, simulate=True)
        self.assertTrue(c2.start())
        c2.shutdown(1.0)

    def test_shutdown_stops_a_running_reply_and_blocks_new_work(self):
        self.install_model()
        from jarvis.inference import gguf_engine as ge
        c = AppController(self.cfg, simulate=True, engine_factory=lambda p: ge.GGUFEngine(p, llama_factory=SlowLlama))
        c.send("long")
        time.sleep(0.1)
        self.assertEqual(c.shutdown(3.0), {"still_running": []})
        self.assertFalse(c.send("after close"))
        self.assertFalse(c.mission_start("m-x"))

    def test_repeated_launches_do_not_damage_stored_data(self):
        from jarvis.app import lifecycle
        from jarvis.memory.personal import PersonalMemory
        lifecycle.prepare(self.cfg)
        mem = PersonalMemory.open(self.cfg.memory_db)
        mem.add("explicit_preference", "keep this")
        mem.close()
        for _ in range(3):
            lifecycle.prepare(self.cfg)
            c = AppController(self.cfg, simulate=True)
            c.start()
            c.missions()
            c.shutdown(1.0)
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        self.assertEqual([i["text"] for i in c.memory_items()], ["keep this"])


if __name__ == "__main__":
    unittest.main()
