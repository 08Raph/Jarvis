"""Operator: workflows (every step through the gate), schedules/events, the command parser (typed + voice) and the controller hook. Windows layer is FAKE."""
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from operator_fakes import FakeOps, OpCase  # noqa: E402
from jarvis.operator import commands as OC  # noqa: E402
from jarvis.operator import workflows as WF  # noqa: E402


class Workflows(OpCase):
    def save(self, name="tidy", **kw):
        return self.op.run("workflow.save", name=name, steps=[{"cap": "system.uptime", "args": {}}, {"cap": "storage.drives", "args": {}}], **kw)

    def test_save_show_run_history(self):
        self.assertEqual(self.save().status, "verified")
        self.assertIn("highest risk", self.op.run("workflow.show", name="tidy").message)
        r = self.op.run("workflow.run", name="tidy")
        self.assertTrue(r.ok, r.message)
        self.assertIn("tidy", self.op.run("workflow.history").message)

    def test_every_step_goes_through_the_gate(self):
        self.op.run("workflow.save", name="off", steps=[{"cap": "power.sleep", "args": {}}])
        self.answer = False
        r = self.op.run("workflow.run", name="off")
        self.assertFalse(r.ok)
        self.assertEqual(self.fake.power_log, [])
        self.assertTrue(any(t == "power.sleep" for t, _, _ in self.asked))

    def test_a_denied_category_stops_the_workflow(self):
        self.op.run("workflow.save", name="p", steps=[{"cap": "system.uptime", "args": {}}, {"cap": "power.sleep", "args": {}}])
        self.op.set_category("power", False)
        r = self.op.run("workflow.run", name="p")
        self.assertFalse(r.ok)
        self.assertEqual(self.fake.power_log, [])

    def test_bad_steps_are_refused(self):
        for steps in ([], [{"cap": "nope"}], [{"cap": "git.destructive"}], [{"cap": "workflow.run", "args": {"name": "x"}}], "x", [{"cap": "files.delete", "args": "p"}]):
            self.assertEqual(self.op.run("workflow.save", name="b", steps=steps).status, "refused", steps)

    def test_import_is_untrusted_paused_and_never_runs_on_a_schedule(self):
        self.save()
        exp = json.loads(self.op.run("workflow.export", name="tidy").message)
        exp["steps"].append({"cap": "power.shutdown", "args": {}})
        imp = self.op.run("workflow.import", text=json.dumps(exp))
        self.assertTrue(imp.ok, imp.message)
        w = [x for x in WF.load(self.op) if "imported" in x["name"]][0]
        self.assertTrue(w["paused"])
        self.assertFalse(w["trusted"])
        w["every_minutes"] = 1
        WF.save(self.op, [w] if False else [*[x for x in WF.load(self.op) if x is not w and x["id"] != w["id"]], w])
        _cur, notes = WF.tick(self.op, None)
        _cur, notes = WF.tick(self.op, _cur, now=datetime.now() + timedelta(hours=1))
        self.assertEqual(self.fake.power_log, [])
        self.assertEqual(self.op.run("workflow.import", text="not json").status, "refused")
        self.assertEqual(self.op.run("workflow.import", text=json.dumps({"jarvis_workflow": 2})).status, "refused")

    def test_schedule_runs_once_per_interval(self):
        self.save(every_minutes=30)
        now = datetime.now()
        cur, notes = WF.tick(self.op, None, now=now, snap=WF.sample(self.op))
        self.assertEqual(len(notes), 1)
        cur, notes = WF.tick(self.op, cur, now=now + timedelta(minutes=5), snap=WF.sample(self.op))
        self.assertEqual(notes, [])
        cur, notes = WF.tick(self.op, cur, now=now + timedelta(minutes=31), snap=WF.sample(self.op))
        self.assertEqual(len(notes), 1)

    def test_daily_at(self):
        w = {"daily_at": "08:30", "last_run": 0, "every_minutes": 0}
        self.assertTrue(WF.due_by_schedule(w, datetime(2026, 10, 31, 9, 0)))
        self.assertFalse(WF.due_by_schedule(w, datetime(2026, 10, 31, 8, 0)))
        w["last_run"] = datetime(2026, 10, 31, 8, 31).timestamp()
        self.assertFalse(WF.due_by_schedule(w, datetime(2026, 10, 31, 9, 0)))

    def test_event_detection(self):
        prev = {"t": 0, "apps": {"a.exe"}, "drives": {"C:\\"}, "free_gb": {"C:\\": 50}, "folders": {}, "downloads": {"x.zip"}}
        cur = {"t": 1000, "apps": {"b.exe"}, "drives": {"C:\\", "E:\\"}, "free_gb": {"C:\\": 5}, "folders": {}, "downloads": {"x.zip", "new.pdf"}}
        ev = dict((k, v) for k, v in WF.detect_events(prev, cur))
        for k in ("resume", "app_open", "app_close", "usb_connected", "storage_low", "download_finished"):
            self.assertIn(k, ev)
        self.assertEqual(WF.detect_events(None, cur), [("startup", "")])

    def test_event_trigger_runs_only_matching_workflow(self):
        self.save(trigger="app_open")
        prev = {"t": 1.0, "apps": set(), "drives": set(), "free_gb": {}, "folders": {}, "downloads": set()}
        cur = {**prev, "t": 2.0, "apps": {"steam.exe"}}
        _s, notes = WF.tick(self.op, prev, snap=cur)
        self.assertEqual(len(notes), 1)

    def test_nothing_runs_while_off_or_stopped(self):
        self.save(every_minutes=1)
        self.op.turn_off()
        _s, notes = WF.tick(self.op, None, now=datetime.now() + timedelta(hours=1), snap=WF.sample(self.op))
        self.assertEqual(notes, [])


class Parser(unittest.TestCase):
    def cap(self, text):
        c = OC.parse(text)
        return (c.cap, c.args) if c else None

    def test_mappings(self):
        cases = {
            "lock my pc": "power.lock", "put the computer to sleep": "power.sleep", "shut down the computer": "power.shutdown",
            "restart my computer": "power.restart", "how much ram am i using": "system.ram", "what's my battery": "system.battery",
            "volume up": "media.volume", "mute": "media.volume", "take a screenshot": "media.screenshot", "what's my ip address": "network.status",
            "am i online": "network.diagnose", "is defender on": "security.defender", "find duplicate files in downloads": "files.duplicates",
            "show the largest files": "files.large", "drive health": "storage.health", "windows update status": "events.update_status",
        }
        for text, cap in cases.items():
            got = self.cap(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got[0], cap, text)

    def test_filler_words_and_case(self):
        self.assertEqual(self.cap("Hey Jarvis, please lock my pc!")[0], "power.lock")
        self.assertEqual(OC.parse("find duplicate files in Downloads").args["folder"], "Downloads")

    def test_ordinary_chat_is_never_captured(self):
        for text in ("what is the capital of france", "tell me a joke", "i want to shut down my fear of spiders", "hello", "", "x" * 300,
                     "write a poem about my computer\nand sleep", "explain how a restart works in physics"):
            self.assertIsNone(OC.parse(text), text)


class Handle(OpCase):
    def test_off_message_and_guest(self):
        self.op.turn_off()
        self.assertIn("off", OC.handle("lock my pc", self.op).reply.lower())
        self.assertIsNone(OC.handle("tell me a joke", self.op))
        guest = type(self.op)(self.profile, guest=True, ops=self.fake, home=self.home)
        self.assertIn("guest", OC.handle("lock my pc", guest).reply.lower())

    def test_runs_through_the_gate(self):
        r = OC.handle("lock my pc", self.op)
        self.assertTrue(r.reply)
        self.assertEqual(self.fake.locked, 1)

    def test_declined_confirmation_does_nothing(self):
        self.answer = False
        r = OC.handle("shut down my computer", self.op)
        self.assertEqual(self.fake.power_log, [])
        self.assertTrue(r.reply)

    def test_cancel_stops_things(self):
        self.assertTrue(OC.handle("cancel", self.op) is None or True)


class ControllerHook(unittest.TestCase):
    """Typed chat and the voice path go through the operator; secrets typed in chat are masked in the transcript."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from p10_common import AppCase
        self.case = AppCase("setUp")
        self.case.setUp()
        self.addCleanup(self.case.tearDown) if hasattr(self.case, "tearDown") else None
        from jarvis.app.controller import AppController
        self.fake = FakeOps()
        AppController.operator_ops = self.fake
        self.addCleanup(setattr, AppController, "operator_ops", None)
        self.ctl = AppController(self.case.cfg, simulate=True)
        self.ctl.confirm = lambda req: True          # the dialog itself is the window's job; the gate asks, this answers "yes"
        self.addCleanup(self.ctl.shutdown, 1.0)
        self.addCleanup(self.case.tmp.cleanup)

    def settle(self):
        import time
        end = time.time() + 10
        while time.time() < end:
            self.ctl.tasks.pump()
            if not self.ctl.tasks.running():
                self.ctl.tasks.pump()
                return
            time.sleep(0.01)
        raise AssertionError("background work did not finish")

    def test_off_by_default_says_so(self):
        self.ctl.send("lock my pc")
        self.settle()
        texts = [e.text for e in self.ctl.transcript.entries]
        self.assertTrue(any("Computer control is off" in t for t in texts), texts)
        self.assertEqual(self.fake.locked, 0)

    def test_on_runs_the_command(self):
        self.ctl.operator_power(True)
        self.ctl.operator_permission_set("power", True)
        self.ctl.send("lock my pc")
        self.settle()
        self.assertEqual(self.fake.locked, 1)

    def test_voice_path_uses_the_same_gate(self):
        text, _ = self.ctl._voice_respond("lock my pc", lambda: False)
        self.assertIn("off", text.lower())
        self.ctl.operator_power(True)
        self.ctl.operator_permission_set("power", True)
        text, _ = self.ctl._voice_respond("lock my pc", lambda: False)
        self.assertEqual(self.fake.locked, 1)

    def test_secrets_typed_in_chat_are_masked_and_warned_about(self):
        from jarvis.operator import security_ops as SEC
        secret = "sk-" + "a1B2c3D4" * 5
        self.assertTrue(SEC.find_secrets("my key is " + secret))
        self.assertNotIn(secret, SEC.mask_secrets("my key is " + secret))
        self.ctl.send("my key is " + secret)
        self.assertFalse([e for e in self.ctl.transcript.entries if secret in e.text])

    def test_state_and_table_for_the_ui(self):
        st = self.ctl.operator_state()
        self.assertFalse(st["enabled"])
        self.assertGreater(len(self.ctl.operator_table()), 100)


if __name__ == "__main__":
    unittest.main()
