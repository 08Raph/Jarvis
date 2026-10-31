"""Computer Operator gate: off by default, permissions, confirmations by risk, emergency stop, guest, audit without secrets, path rules."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from operator_fakes import FakeOps, OpCase  # noqa: E402
from jarvis.operator import core  # noqa: E402
from jarvis.operator.core import CATEGORIES, Operator  # noqa: E402


class Gate(unittest.TestCase):
    def make(self, **kw):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        home = Path(t.name) / "h"
        (home / "Downloads").mkdir(parents=True)
        fake = FakeOps()
        asked = []
        op = Operator(Path(t.name) / "p", ops=fake, home=home, confirm=lambda r: (asked.append(r), True)[1], **kw)
        return op, fake, asked, home

    def test_everything_is_off_until_turned_on(self):
        op, fake, asked, _h = self.make()
        out = op.run("system.uptime")
        self.assertEqual(out.status, "off")
        self.assertIn("off", out.message.lower())
        self.assertEqual(fake.key_log, [])

    def test_turning_on_grants_only_the_read_and_everyday_categories(self):
        op, *_ = self.make()
        op.turn_on()
        st = op.state()["categories"]
        for cat, (_label, default) in CATEGORIES.items():
            self.assertEqual(st[cat], default, cat)
        self.assertFalse(st["power"])
        self.assertFalse(st["files_write"])
        self.assertTrue(st["system_read"])

    def test_a_category_that_is_off_is_denied_and_named(self):
        op, fake, asked, _h = self.make()
        op.turn_on()
        out = op.run("power.lock")
        self.assertEqual(out.status, "denied")
        self.assertEqual(fake.locked, 0)
        self.assertEqual(asked, [])
        op.set_category("power", True)
        self.assertEqual(op.run("power.lock").status, "unverified")
        self.assertEqual(fake.locked, 1)

    def test_risk_levels_decide_the_confirmation(self):
        op, fake, asked, _h = self.make()
        op.turn_on()
        op.set_category("power", True)
        op.run("media.key", key="next")                         # low: no question
        self.assertEqual(asked, [])
        op.run("power.lock")                                    # medium: asks while "ask before medium" is on
        self.assertEqual(len(asked), 1)
        op.set_confirm_medium(False)
        op.run("power.lock")
        self.assertEqual(len(asked), 1)
        op.run("power.restart")                                 # high: ALWAYS asks, even with medium confirmation off
        self.assertEqual(len(asked), 2)
        self.assertEqual(asked[-1].risk, "high")
        self.assertEqual(fake.power_log, ["restart"])

    def test_declined_or_unanswerable_high_risk_does_nothing(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        fake = FakeOps()
        op = Operator(Path(t.name), ops=fake, home=Path(t.name), confirm=None)
        op.turn_on()
        op.set_category("power", True)
        self.assertEqual(op.run("power.shutdown").status, "declined")
        self.assertEqual(fake.power_log, [])
        op.confirm = lambda r: (_ for _ in ()).throw(RuntimeError("dialog broke"))
        self.assertEqual(op.run("power.shutdown").status, "declined")
        self.assertEqual(fake.power_log, [])

    def test_emergency_stop_beats_everything(self):
        op, fake, asked, _h = self.make()
        op.turn_on()
        (op.folder / "STOP").write_text("x")
        out = op.run("media.key", key="next")
        self.assertEqual(out.status, "stopped")
        self.assertEqual(fake.key_log, [])

    def test_pause_and_cancel(self):
        op, fake, *_ = self.make()
        op.turn_on()
        op.pause()
        self.assertEqual(op.run("media.key", key="next").status, "paused")
        op.resume()
        self.assertTrue(op.run("media.key", key="next").ok)
        op.cancel_all()
        self.assertEqual(op.run("media.key", key="next").status, "stopped")
        self.assertTrue(op.run("media.key", key="next").ok)           # the cancel was used up

    def test_guest_can_do_nothing_and_saves_nothing(self):
        op, fake, asked, _h = self.make(guest=True)
        self.assertEqual(op.run("media.key", key="next").status, "denied")
        op.turn_on()
        self.assertFalse(op.is_on())
        self.assertFalse(op.state_file.exists())
        self.assertFalse(op.audit_file.exists())

    def test_unknown_capability_and_bad_arguments_are_plain_messages(self):
        op, *_ = self.make()
        op.turn_on()
        self.assertEqual(op.run("nothing.here").status, "unavailable")
        out = op.run("media.key", key="explode")
        self.assertNotEqual(out.status, "verified")
        self.assertNotIn("Traceback", out.message)
        out = op.run("files.search", bogus_arg=1)
        self.assertEqual(out.status, "failed")

    def test_audit_records_decisions_without_secrets_or_contents(self):
        op, fake, asked, home = self.make()
        op.turn_on()
        op.set_category("documents", True)
        op.run("docs.create", path="note.txt", text="my password is hunter2hunter2")
        rows = op.audit_tail(10)
        blob = json.dumps(rows)
        self.assertNotIn("hunter2", blob)
        last = rows[-1]
        self.assertEqual(last["cap"], "docs.create")
        self.assertIn(last["decision"], ("confirmed", "auto"))
        self.assertIn("args", last)
        self.assertTrue(all(k in last for k in ("t", "risk", "status")))

    def test_every_capability_belongs_to_a_known_permission_and_has_a_title(self):
        reg = core.load_all()
        self.assertGreater(len(reg), 120)
        for c in reg.values():
            self.assertIn(c.category, CATEGORIES, c.id)
            self.assertTrue(c.title and c.id, c.id)

    def test_no_capability_runs_a_shell(self):
        root = Path(core.__file__).parent
        for f in root.glob("*.py"):
            t = f.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", t, f.name)
            self.assertNotIn("os.system(", t, f.name)
            self.assertNotIn("eval(", t.replace("literal_eval", ""), f.name)


class Paths(OpCase):
    def test_paths_must_be_inside_allowed_folders(self):
        outside = self.home.parent / "elsewhere"
        outside.mkdir()
        (outside / "x.txt").write_text("x")
        for bad in (str(outside / "x.txt"), "C:\\Windows\\System32\\cmd.exe", "\\\\server\\share\\a.txt", "con", "a.txt:stream", "../../etc/passwd"):
            with self.assertRaises(core.PathRefused, msg=bad):
                self.op.check_path(bad)

    def test_a_symlink_that_leads_outside_is_refused(self):
        outside = self.home.parent / "secret"
        outside.mkdir()
        (outside / "s.txt").write_text("s")
        try:
            (self.dl / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not allowed here")
        with self.assertRaises(core.PathRefused):
            self.op.check_path(self.dl / "link" / "s.txt")

    def test_relative_names_resolve_inside_allowed_folders(self):
        self.write("report.txt")
        self.assertEqual(self.op.check_path("report.txt"), (self.dl / "report.txt").resolve())

    def test_extra_folders_cannot_be_a_drive_home_or_system_folder(self):
        for bad in (self.home, Path(self.home.anchor)):
            with self.assertRaises(core.PathRefused):
                self.op.add_folder(str(bad))
        extra = self.home / "Projects"
        extra.mkdir()
        self.op.add_folder(str(extra))
        self.assertIn(extra.resolve(), self.op.roots())
        self.op.remove_folder(str(extra.resolve()))
        self.assertNotIn(extra.resolve(), self.op.roots())

    def test_allowed_folder_itself_cannot_be_modified(self):
        with self.assertRaises(core.PathRefused):
            self.op.check_path(self.dl, write=True)


if __name__ == "__main__":
    unittest.main()
