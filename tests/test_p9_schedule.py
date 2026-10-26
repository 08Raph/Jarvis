"""Prompt 9: scheduling. REAL scheduler, store, runner, executor; a FAKE CLOCK (tests/p9_common.Clock) and TEST TOOLS. Schedules fire only while Jarvis is
checking (`schedule watch` / `run-due`); a "restart" here is a new Scheduler/Runner object over the same files in one process, NOT a real process restart."""
import json
import os
import unittest
from unittest import mock
from datetime import datetime, timezone

from p9_common import Clock, M, P9, ROOT, MissionStore  # noqa: F401
from jarvis.missions import schedule as SC
from jarvis.missions.schedule import ScheduleError, Scheduler


def s(sid, tool="t_ok", args=None, **kw):
    return {"id": sid, "tool": tool, "args": args or {}, **kw}


class Base(P9):
    def setUp(self):
        super().setUp()
        self.make9()
        self.clk = Clock("2026-06-01T12:00:00Z")
        self.sched = Scheduler(self.profile, self.store, clock=self.clk)

    def restart(self):
        self.new_runner()
        self.sched = Scheduler(self.profile, self.store, clock=self.clk)

    def tick(self):
        return self.sched.tick(self.runner, self.clk())

    def statuses(self, ev):
        return [e["status"] for e in ev]


class Validation(Base):
    def test_one_time_in_the_past_is_refused(self):
        m = self.mission([s("a")])
        with self.assertRaises(ScheduleError):
            self.sched.create(m["id"], "once", tz="UTC", at="2026-06-01 11:00")
        with self.assertRaises(ScheduleError):
            self.sched.create(m["id"], "once", tz="UTC", at="soon")

    def test_bad_timezone_and_kind(self):
        m = self.mission([s("a")])
        with self.assertRaises(ScheduleError):
            self.sched.create(m["id"], "once", tz="Mars/Olympus", at="2026-06-02 09:00")
        with self.assertRaises(ScheduleError):
            self.sched.create(m["id"], "yearly", tz="UTC")

    def test_interval_minimums(self):
        ro = self.mission([s("a")])
        with self.assertRaises(ScheduleError):
            self.sched.create(ro["id"], "interval", tz="UTC", every_minutes=1)
        self.sched.create(ro["id"], "interval", tz="UTC", every_minutes=5, specs=self.ex.specs)
        rw = self.mission([s("a", "t_hook")])
        with self.assertRaises(ScheduleError):
            self.sched.create(rw["id"], "interval", tz="UTC", every_minutes=5, specs=self.ex.specs)

    def test_unattended_forbidden_steps_cannot_be_scheduled(self):
        m = self.mission([s("a", "run_code", {"code": "print(1)"})], approve=False)
        with self.assertRaises(ScheduleError) as cm:
            self.sched.create(m["id"], "daily", tz="UTC", time_="09:00", specs=self.ex.specs)
        self.assertIn("cannot be scheduled", str(cm.exception))

    def test_network_and_execute_risks_are_never_scheduled_whatever_their_category(self):
        import dataclasses
        from jarvis.desktop import schema as S
        base = self.ex.specs["t_ok"]
        for risk in (S.NETWORK, S.EXECUTE, S.INSTALL):
            specs = {"t_ok": dataclasses.replace(base, risk=risk)}
            m = M.normalize({"goal": "g", "steps": [{"id": "a", "tool": "t_ok", "args": {}}]}, self.ex.specs)
            self.assertTrue(M.unattended_problems(m, specs), risk)
        self.assertEqual(M.unattended_problems(m, {"t_ok": base}), [])

    def test_weekly_needs_days_and_daily_needs_time(self):
        m = self.mission([s("a")])
        with self.assertRaises(ScheduleError):
            self.sched.create(m["id"], "weekly", tz="UTC", time_="09:00")
        with self.assertRaises(ScheduleError):
            self.sched.create(m["id"], "daily", tz="UTC")

    def test_schedule_for_a_tampered_mission_is_refused(self):
        m = self.mission([s("a")])
        p = self.store._path(m["id"])
        d = json.loads(p.read_text())
        d["steps"][0]["tool"] = "t_hook"
        p.write_text(json.dumps(d))
        with self.assertRaises(ScheduleError):
            self.sched.create(m["id"], "once", tz="UTC", at="2026-06-02 09:00")


class Persistence(Base):
    def test_schedules_survive_a_restart(self):
        m = self.mission([s("a")])
        sc = self.sched.create(m["id"], "daily", tz="Europe/Paris", time_="09:30")
        self.restart()
        got = self.sched.get(sc["id"])
        self.assertEqual((got["tz"], got["time"], got["state"]), ("Europe/Paris", "09:30", "active"))

    def test_pause_resume_cancel_edit_and_listing(self):
        m = self.mission([s("a")])
        sc = self.sched.create(m["id"], "daily", tz="UTC", time_="09:30")
        self.sched.set_state(sc["id"], "paused", "me")
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.sched.get(sc["id"])["state"], "paused")
        self.sched.set_state(sc["id"], "active")
        self.sched.edit(sc["id"], time_="10:45")
        self.assertEqual(self.sched.get(sc["id"])["time"], "10:45")
        self.sched.set_state(sc["id"], "cancelled")
        self.assertEqual(self.sched.get(sc["id"])["state"], "cancelled")
        self.assertEqual(len(self.sched.list()), 1)

    def test_corrupt_schedule_file_does_not_crash_and_is_not_silently_trusted(self):
        m = self.mission([s("a")])
        self.sched.create(m["id"], "daily", tz="UTC", time_="09:30")
        (self.profile / "missions" / "schedules.json").write_text("{not json")
        try:
            lst = self.sched.list()
        except ScheduleError:
            return
        self.assertEqual(lst, [])


class TimeZones(Base):
    def occ(self, s_, a, b):
        p = lambda x: datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return [o.strftime("%Y-%m-%dT%H:%M") for o in SC.occurrences(s_, p(a), p(b))]

    def test_daily_follows_local_wall_clock_across_dst(self):
        s_ = {"kind": "daily", "tz": "America/New_York", "time": "09:00", "weekdays": list(range(7))}
        self.assertEqual(self.occ(s_, "2026-03-07T00:00:00Z", "2026-03-10T00:00:00Z"), ["2026-03-07T14:00", "2026-03-08T13:00", "2026-03-09T13:00"])

    def test_nonexistent_local_time_shifts_forward(self):
        s_ = {"kind": "daily", "tz": "America/New_York", "time": "02:30", "weekdays": list(range(7))}
        got = self.occ(s_, "2026-03-08T00:00:00Z", "2026-03-09T00:00:00Z")
        self.assertEqual(len(got), 1)

    def test_ambiguous_local_time_runs_once(self):
        s_ = {"kind": "daily", "tz": "America/New_York", "time": "01:30", "weekdays": list(range(7))}
        self.assertEqual(len(self.occ(s_, "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")), 1)

    def test_one_time_in_a_zone(self):
        m = self.mission([s("a")])
        sc = self.sched.create(m["id"], "once", tz="Asia/Tokyo", at="2026-06-01 22:00")       # = 13:00 UTC
        self.clk.set("2026-06-01T12:30:00Z")
        self.assertEqual(self.tick(), [])
        self.clk.set("2026-06-01T13:01:00Z")
        self.assertEqual(self.statuses(self.tick()), ["mission_completed"])

    def test_fixed_offset_and_utc(self):
        self.assertIsNotNone(SC.get_tz("UTC"))
        self.assertIsNotNone(SC.get_tz("+05:30"))

    def test_missing_tzdata_gives_a_clear_error(self):
        with mock.patch("zoneinfo.ZoneInfo", side_effect=Exception("no tzdata")):
            with self.assertRaises(ScheduleError) as cm:
                SC.get_tz("Europe/Paris")
        self.assertIn("time zone", str(cm.exception).lower())

    def test_weekly_days(self):
        s_ = {"kind": "weekly", "tz": "UTC", "time": "08:00", "weekdays": [0, 3]}          # Mon, Thu
        self.assertEqual(self.occ(s_, "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z"), ["2026-06-01T08:00", "2026-06-04T08:00"])


class FiringAndMissedRuns(Base):
    def test_once_fires_when_due_not_before_and_only_once(self):
        m = self.mission([s("a")])
        sc = self.sched.create(m["id"], "once", tz="UTC", at="2026-06-01 12:30")
        self.clk.set("2026-06-01T12:29:00Z")
        self.assertEqual(self.tick(), [])
        self.clk.set("2026-06-01T12:31:00Z")
        self.assertEqual(self.statuses(self.tick()), ["mission_completed"])
        self.clk.set("2026-06-01T12:40:00Z")
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.sched.get(sc["id"])["state"], "done")
        self.assertEqual(self.calls["t_ok"], 1)

    def test_missed_run_beyond_grace_is_skipped_by_default(self):
        m = self.mission([s("a")])
        sc = self.sched.create(m["id"], "once", tz="UTC", at="2026-06-01 12:30")
        self.clk.set("2026-06-01T15:00:00Z")                           # Jarvis was closed / asleep
        ev = self.tick()
        self.assertEqual(self.statuses(ev), ["missed"])
        self.assertEqual(self.calls["t_ok"], 0)
        self.assertEqual(self.sched.get(sc["id"])["state"], "done")

    def test_missed_run_with_run_once_policy_runs_once_late_and_says_so(self):
        m = self.mission([s("a")])
        self.sched.create(m["id"], "once", tz="UTC", at="2026-06-01 12:30", missed="run_once")
        self.clk.set("2026-06-01T15:00:00Z")
        ev = self.tick()
        self.assertIn("mission_completed", self.statuses(ev))
        self.assertTrue(any("late" in e["detail"] for e in ev))
        self.assertEqual(self.calls["t_ok"], 1)

    def test_many_missed_occurrences_do_not_replay_en_masse(self):
        m = self.mission([s("a")])
        self.sched.create(m["id"], "interval", tz="UTC", every_minutes=10, missed="run_once", specs=self.ex.specs)
        self.clk.add(hours=10)                                          # a long sleep: 60 occurrences were missed
        ev = self.tick()
        self.assertEqual(self.calls["t_ok"], 1)
        self.assertGreaterEqual(len([e for e in ev if e["status"] == "missed"]), 1)

    def test_clock_going_backwards_does_not_refire(self):
        m = self.mission([s("a")])
        self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        self.clk.set("2026-06-01T12:06:00Z")
        self.tick()
        self.clk.set("2026-06-01T11:00:00Z")                           # clock set back
        self.assertEqual(self.tick(), [])
        self.clk.set("2026-06-01T12:07:00Z")
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.calls["t_ok"], 1)

    def test_paused_schedule_does_not_replay_after_resume(self):
        m = self.mission([s("a")])
        sc = self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        self.sched.set_state(sc["id"], "paused")
        self.clk.set("2026-06-01T12:06:00Z")
        self.tick()
        self.sched.set_state(sc["id"], "active")
        self.clk.set("2026-06-01T12:07:00Z")
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.calls["t_ok"], 0)

    def test_recurring_daily_runs_each_day_with_separate_executions(self):
        m = self.mission([s("a")])
        self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        for day in ("01", "02", "03"):
            self.clk.set(f"2026-06-{day}T12:06:00Z")
            self.assertEqual(self.statuses(self.tick()), ["mission_completed"])
        r = self.store.load(m["id"])
        self.assertEqual(len(r["executions"]), 3)
        self.assertTrue(all(e["trigger"].startswith("schedule:") for e in r["executions"]))
        self.assertEqual(self.calls["t_ok"], 3)


class RestartAndDuplicates(Base):
    def test_restart_does_not_rerun_an_occurrence(self):
        m = self.mission([s("a")])
        self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        self.clk.set("2026-06-01T12:06:00Z")
        self.tick()
        self.restart()
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.calls["t_ok"], 1)

    def test_even_if_the_cursor_is_lost_the_claim_prevents_a_duplicate(self):
        m = self.mission([s("a")])
        sc = self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        self.clk.set("2026-06-01T12:06:00Z")
        self.tick()
        p = self.profile / "missions" / "schedules.json"
        d = json.loads(p.read_text())
        for x in d["schedules"]:
            x["cursor"] = "2026-06-01T12:00:00Z"                        # simulate a stale cursor (e.g. restored backup)
        p.write_text(json.dumps(d))
        self.restart()
        ev = self.tick()
        self.assertEqual(self.statuses(ev), ["duplicate_prevented"])
        self.assertEqual(self.calls["t_ok"], 1)

    def test_crash_during_a_scheduled_run_is_marked_unknown_and_not_rerun(self):
        from p9_common import Crash
        m = self.mission([s("a", "t_crash")], approve=False)
        # t_crash is a changing tool, so give the schedule its own approval
        self.approve(m["id"])
        sc = self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        self.sched.approve(sc["id"], self.store.load(m["id"])["plan_hash"][:16], max_runs=3)
        self.clk.set("2026-06-01T12:06:00Z")
        with self.assertRaises(Crash):
            self.tick()
        p = self.profile / "missions" / "schedules.json"
        d = json.loads(p.read_text())
        for c in d["claims"].values():
            c["pid"] = 999999                                            # the process that made the claim is gone
        p.write_text(json.dumps(d))
        (self.store.root / f"{m['id']}.lock").write_text('{"pid": 999999}')
        self.restart()
        self.store.recover_all()
        self.assertEqual(self.sched.recover_claims(), 1)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.calls["t_crash"], 1)
        self.assertEqual(list(json.loads(p.read_text())["claims"].values())[0]["status"], "interrupted_unknown")

    def test_two_processes_cannot_both_claim_the_same_occurrence(self):
        m = self.mission([s("a")])
        sc = self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        other = Scheduler(self.profile, MissionStore(self.profile), clock=self.clk)
        self.clk.set("2026-06-01T12:06:00Z")
        a = self.tick()
        b = other.tick(self.runner, self.clk())
        self.assertEqual(self.statuses(a), ["mission_completed"])
        self.assertEqual(b, [])
        self.assertEqual(self.calls["t_ok"], 1)


class ApprovalsForRecurring(Base):
    def setup_conseq(self, kind="daily", **kw):
        m = self.mission([s("a", "t_hook")])
        sc = self.sched.create(m["id"], kind, tz="UTC", **({"time_": "12:05"} if kind == "daily" else kw), specs=self.ex.specs)
        return m, sc

    def test_consequential_schedule_without_its_own_approval_does_not_run(self):
        m, sc = self.setup_conseq()
        self.clk.set("2026-06-01T12:06:00Z")
        ev = self.tick()
        self.assertEqual(self.statuses(ev), ["refused"])
        self.assertIn("approval missing", ev[0]["detail"])
        self.assertEqual(self.calls["t_hook"], 0)
        self.assertEqual(self.sched.get(sc["id"])["state"], "paused")

    def test_mission_approval_alone_is_not_schedule_authority(self):
        m, sc = self.setup_conseq()
        self.approve(m["id"])                                           # the one-time approval of a manual run
        self.clk.set("2026-06-01T12:06:00Z")
        self.assertEqual(self.statuses(self.tick()), ["refused"])
        self.assertEqual(self.calls["t_hook"], 0)
        self.assertEqual(self.store.load(m["id"])["approval"]["uses_left"], 1)    # and it was not consumed

    def test_wrong_hash_is_rejected(self):
        m, sc = self.setup_conseq()
        with self.assertRaises(ScheduleError):
            self.sched.approve(sc["id"], "0" * 16)
        with self.assertRaises(ScheduleError):
            self.sched.approve(sc["id"], self.store.load(m["id"])["plan_hash"][:4])

    def test_approved_schedule_runs_and_run_limit_ends_it(self):
        m, sc = self.setup_conseq()
        self.sched.approve(sc["id"], self.store.load(m["id"])["plan_hash"][:16], max_runs=2)
        for day in ("01", "02", "03"):
            self.clk.set(f"2026-06-{day}T12:06:00Z")
            self.tick()
        self.assertEqual(self.calls["t_hook"], 2)
        got = self.sched.get(sc["id"])
        self.assertEqual(got["state"], "paused")
        self.assertIn("approve again", got["reason"])

    def test_run_limit_is_enforced_even_if_the_schedule_is_still_active(self):
        m, sc = self.setup_conseq()
        self.sched.approve(sc["id"], self.store.load(m["id"])["plan_hash"][:16], max_runs=2)
        f = self.profile / "missions" / "schedules.json"
        d = json.loads(f.read_text())
        d["schedules"][0]["approval"]["runs_used"] = 2                  # limit reached, schedule (e.g. hand-edited) still active
        f.write_text(json.dumps(d))
        self.clk.set("2026-06-01T12:06:00Z")
        ev = self.tick()
        self.assertEqual(self.statuses(ev)[-1], "refused")
        self.assertIn("used up", ev[-1]["detail"])
        self.assertEqual(self.calls["t_hook"], 0)

    def test_approval_expires(self):
        m, sc = self.setup_conseq()
        self.sched.approve(sc["id"], self.store.load(m["id"])["plan_hash"][:16], max_runs=50, days=1)
        self.clk.set("2026-06-01T12:06:00Z")
        self.tick()
        self.clk.set("2026-06-03T12:06:00Z")
        ev = self.tick()
        self.assertEqual(self.statuses(ev)[-1], "refused")
        self.assertIn("expired", ev[-1]["detail"])
        self.assertEqual(self.calls["t_hook"], 1)

    def test_reauthorisation_resumes_after_expiry(self):
        m, sc = self.setup_conseq()
        h = self.store.load(m["id"])["plan_hash"][:16]
        self.sched.approve(sc["id"], h, days=1)
        self.clk.set("2026-06-03T12:06:00Z")
        self.tick()
        self.sched.approve(sc["id"], h, max_runs=5, days=10)
        self.assertEqual(self.sched.get(sc["id"])["state"], "active")
        self.clk.set("2026-06-04T12:06:00Z")
        self.assertEqual(self.statuses(self.tick()), ["mission_completed"])

    def test_limits_on_the_approval_itself(self):
        m, sc = self.setup_conseq()
        h = self.store.load(m["id"])["plan_hash"][:16]
        for kw in ({"max_runs": 0}, {"max_runs": 51}, {"days": 0}, {"days": 91}):
            with self.assertRaises(ScheduleError):
                self.sched.approve(sc["id"], h, **kw)

    def test_mission_edited_after_schedule_approval_stops_runs(self):
        m, sc = self.setup_conseq()
        self.sched.approve(sc["id"], self.store.load(m["id"])["plan_hash"][:16])
        mm = self.store.load(m["id"])
        mm["steps"][0]["description"] = "now does something else"
        d = M.normalize({"goal": mm["goal"], "steps": mm["steps"]}, self.ex.specs)
        mm["steps"], mm["plan_hash"] = d["steps"], M.plan_hash(d)
        self.store.save(mm)
        self.clk.set("2026-06-01T12:06:00Z")
        ev = self.tick()
        self.assertEqual(self.statuses(ev), ["refused"])
        self.assertEqual(self.calls["t_hook"], 0)

    def test_permission_is_rechecked_at_run_time(self):
        m, sc = self.setup_conseq()
        self.sched.approve(sc["id"], self.store.load(m["id"])["plan_hash"][:16])
        self.env.settings.enabled["system"] = False
        self.clk.set("2026-06-01T12:06:00Z")
        ev = self.tick()
        self.assertEqual(self.statuses(ev), ["mission_failed"])
        self.assertEqual(self.calls["t_hook"], 0)

    def test_unattended_declined_confirmation_is_not_auto_accepted(self):
        (self.work / "n.txt").write_text("old")
        mm = self.mission([s("a", "write_file", {"path": "n.txt", "content": "new", "overwrite": True})])
        sc = self.sched.create(mm["id"], "daily", tz="UTC", time_="12:05", specs=self.ex.specs)
        self.sched.approve(sc["id"], self.store.load(mm["id"])["plan_hash"][:16])
        self.answer = False                                              # nobody is there to confirm
        self.clk.set("2026-06-01T12:06:00Z")
        self.assertEqual(self.statuses(self.tick()), ["mission_failed"])
        self.assertEqual((self.work / "n.txt").read_text(), "old")

    def test_editing_to_fire_more_often_removes_the_approval(self):
        m = self.mission([s("a", "t_hook")])
        sc = self.sched.create(m["id"], "interval", tz="UTC", every_minutes=120, specs=self.ex.specs)
        self.sched.approve(sc["id"], self.store.load(m["id"])["plan_hash"][:16])
        got = self.sched.edit(sc["id"], every_minutes=60, specs=self.ex.specs)
        self.assertIsNone(got["approval"])
        self.assertEqual(got["state"], "paused")
        with self.assertRaises(ScheduleError):
            self.sched.edit(sc["id"], every_minutes=10, specs=self.ex.specs)

    def test_schedule_refuses_a_running_or_paused_mission(self):
        m = self.mission([s("a")])
        sc = self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        mm = self.store.load(m["id"])
        M.set_state(mm, "running", "x")
        self.store.save(mm)
        self.clk.set("2026-06-01T12:06:00Z")
        self.assertEqual(self.statuses(self.tick()), ["refused"])
        self.assertEqual(self.calls["t_ok"], 0)

    def test_scheduled_run_does_not_change_global_permissions(self):
        before = self.env.settings.to_dict()
        m, sc = self.setup_conseq()
        self.sched.approve(sc["id"], self.store.load(m["id"])["plan_hash"][:16])
        self.clk.set("2026-06-01T12:06:00Z")
        self.tick()
        self.assertEqual(self.env.settings.to_dict(), before)

    def test_no_background_service_in_the_package(self):
        t = (ROOT / "src/jarvis/missions/schedule.py").read_text()
        for bad in ("schtasks", "winreg", "daemon", "sched.scheduler", "Timer(", "threading.Thread"):
            self.assertNotIn(bad, t, bad)


class ScheduleDataLifecycle(Base):
    def test_delete_mission_schedules_and_forget_all(self):
        m = self.mission([s("a")])
        self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        self.clk.set("2026-06-01T12:06:00Z")
        self.tick()
        self.sched.delete_all()
        self.assertEqual(self.sched.list(), [])
        f = self.profile / "missions" / "schedules.json"
        self.assertTrue(not f.exists() or not json.loads(f.read_text())["claims"])

    def test_history_has_no_secrets(self):
        m = self.mission([s("a")])
        self.sched.create(m["id"], "daily", tz="UTC", time_="12:05")
        self.clk.set("2026-06-01T12:06:00Z")
        self.tick()
        txt = "".join(p.read_text() for p in (self.profile / "missions").glob("*") if p.is_file())
        self.assertNotIn("sk-", txt)


if __name__ == "__main__":
    unittest.main()
