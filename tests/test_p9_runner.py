"""Prompt 9: the execution engine. REAL executor/policy/confirmation/audit and the real mission store; TEST TOOLS (tests/p9_common.py) give exactly controlled
failures, timeouts, ambiguity and crashes. Nothing ran on Windows."""
import time
import unittest

from p9_common import Crash, M, MissionStore, P9, ROOT, MissionRunner  # noqa: F401
from jarvis.missions.runner import MissionError


def s(sid, tool="t_ok", args=None, **kw):
    return {"id": sid, "tool": tool, "args": args or {}, **kw}


class Happy(P9):
    def setUp(self):
        super().setUp()
        self.make9()

    def test_act_observe_verify_and_the_summary_comes_from_records(self):
        m = self.mission([s("a", "write_file", {"path": "n.txt", "content": "hi"}, description="write",
                            checks=[{"tool": "file_info", "args": {"path": "n.txt"}, "field": "data.exists", "op": "true"}]),
                          s("b", "read_file", {"path": "n.txt"}, depends_on=["a"], description="read back",
                            checks=[{"tool": "read_file", "args": {"path": "n.txt"}, "field": "untrusted_text", "op": "contains", "value": "hi"}])])
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "completed")
        self.assertEqual((self.work / "n.txt").read_text(), "hi")
        ex = M.current_execution(r)
        self.assertEqual([ex["steps"][k]["status"] for k in "ab"], ["succeeded", "succeeded"])
        att = ex["steps"]["a"]["attempts"][0]
        self.assertTrue(att["independent_check"])
        self.assertEqual(att["verification"], "verified")
        self.assertIn("2 of 2 step(s) done and verified", M.summarize(r))

    def test_dependency_order_beats_written_order(self):
        order = []
        self.hooks["hook"] = lambda env, n: order.append("t_hook")
        m = self.mission([s("late", "t_hook", depends_on=["early"]), s("early", "t_ok")])
        self.run_m(m["id"])
        self.assertEqual(M.current_execution(self.store.load(m["id"]))["steps"]["early"]["attempts"][0]["message"], "ok #1")
        self.assertEqual(order, ["t_hook"])
        ex = M.current_execution(self.store.load(m["id"]))
        self.assertLess(ex["steps"]["early"]["started"], ex["steps"]["late"]["started"] + "z")

    def test_each_attempt_records_request_acceptance_observation_and_verdict(self):
        m = self.mission([s("a", "t_ok")])
        att = M.current_execution(self.run_m(m["id"]))["steps"]["a"]["attempts"][0]
        for k in ("n", "at", "action_status", "message", "evidence", "tool_verified", "checks", "verification"):
            self.assertIn(k, att)
        self.assertEqual((att["action_status"], att["tool_verified"], att["verification"]), ("verified", True, "verified"))

    def test_read_only_mission_runs_without_approval_but_consequential_does_not(self):
        ro = self.mission([s("a", "t_ok")])
        self.assertEqual(self.run_m(ro["id"])["state"], "completed")
        rw = self.mission([s("a", "t_hook")], approve=False)
        with self.assertRaises(MissionError):
            self.run_m(rw["id"])
        self.assertEqual(self.calls["t_hook"], 0)


class Failures(P9):
    def setUp(self):
        super().setUp()
        self.make9()

    def test_failed_step_stops_the_mission_and_skips_dependents(self):
        self.contract("t_fail_obs")                                     # a pure read that failed: the failure is definite (nothing could have changed)
        m = self.mission([s("a", "t_fail_obs"), s("b", "t_ok", depends_on=["a"]), s("c", "t_ok")])
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertEqual([self.st(r, k) for k in "abc"], ["failed", "skipped", "pending"])
        self.assertEqual(self.calls["t_ok"], 0)
        self.assertIn("failed", r["state_reason"])

    def test_error_after_a_changing_tool_started_is_unknown_not_failed(self):
        """Prompt 13 (replaces the old assumption that any tool error is a definite failure): the tool began, then raised. Nothing proves it changed
        nothing, so the step is UNKNOWN and the mission waits for a decision; it is never repeated automatically."""
        m = self.mission([s("a", "t_fail_mod"), s("b", "t_ok", depends_on=["a"]), s("c", "t_ok")])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.st(r, "a"), self.st(r, "b")), ("paused", "unknown", "skipped"))
        self.assertEqual(self.calls["t_fail_mod"], 1)
        self.assertIn("UNKNOWN", r["state_reason"])

    def test_permission_denied_at_execution_time(self):
        m = self.mission([s("a", "write_file", {"path": "n.txt", "content": "x"})])
        self.env.settings.enabled["files_write"] = False                # revoked AFTER approval
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.st(r, "a"), "blocked")
        self.assertFalse((self.work / "n.txt").exists())
        self.assertIn("denied", r["state_reason"])

    def test_permission_revoked_between_steps(self):
        self.hooks["hook"] = lambda env, n: env.settings.enabled.__setitem__("files_write", False)
        m = self.mission([s("a", "t_hook"), s("b", "write_file", {"path": "n.txt", "content": "x"}, depends_on=["a"])])
        r = self.run_m(m["id"])
        self.assertEqual([self.st(r, "a"), self.st(r, "b")], ["succeeded", "blocked"])
        self.assertFalse((self.work / "n.txt").exists())

    def test_declined_confirmation_is_a_stop_not_a_retry(self):
        m = self.mission([s("a", "write_file", {"path": "n.txt", "content": "x"}, retries=2, retry_safe=True)])
        (self.work / "n.txt").write_text("old")
        m = self.mission([s("a", "write_file", {"path": "n.txt", "content": "new", "overwrite": True}, retries=2, retry_safe=True)])
        self.answer = False
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertEqual((self.work / "n.txt").read_text(), "old")
        self.assertEqual(len(M.current_execution(r)["steps"]["a"]["attempts"]), 1)
        self.assertEqual(len(self.asked), 1)

    def test_tool_removed_or_plan_invalid_at_start_nothing_runs(self):
        m = self.mission([s("a", "t_hook")])
        self.extra = [e for e in self.extra if e.name != "t_hook"]       # the tool is gone after approval
        self.new_runner()
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertIn("NOT started", r["state_reason"])
        self.assertEqual(self.calls["t_hook"], 0)
        self.assertEqual(r["executions"], [])

    def test_mode_switched_after_approval_blocks_start(self):
        from jarvis import modes
        m = self.mission([s("a", "write_file", {"path": "n.txt", "content": "x"})])
        modes.set_mode(self.profile, "research")
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertIn("research mode", r["state_reason"])
        self.assertFalse((self.work / "n.txt").exists())

    def test_precondition_false_blocks_the_step(self):
        m = self.mission([s("a", "t_hook", preconditions=[{"tool": "file_info", "args": {"path": "missing.txt"}, "field": "data.exists", "op": "true"}])])
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.st(r, "a"), "blocked")
        self.assertEqual(self.calls["t_hook"], 0)
        (self.work / "missing.txt").write_text("x")
        m2 = self.mission([s("a", "t_hook", preconditions=[{"tool": "file_info", "args": {"path": "missing.txt"}, "field": "data.exists", "op": "true"}])])
        self.assertEqual(self.run_m(m2["id"])["state"], "completed")

    def test_precondition_that_cannot_be_checked_pauses_without_acting(self):
        self.env.settings.enabled["files_read"] = False
        m = self.mission([s("a", "t_hook", preconditions=[{"tool": "file_info", "args": {"path": "x"}, "field": "data.exists", "op": "true"}])])
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "paused")
        self.assertEqual(self.calls["t_hook"], 0)


class VerificationAndRetries(P9):
    def setUp(self):
        super().setUp()
        self.make9()

    def check(self, want):
        return [{"tool": "t_state", "args": {}, "field": "data.state", "op": "equals", "value": want}]

    def test_failed_verification_stops_and_is_not_retried_for_changes(self):
        self.hooks["state"] = "off"
        m = self.mission([s("a", "t_hook", checks=self.check("on"), retries=2, retry_safe=True), s("b", "t_ok", depends_on=["a"])])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.st(r, "a"), self.st(r, "b")), ("failed", "failed", "skipped"))
        self.assertEqual(self.calls["t_hook"], 1)                       # the action may have happened: never repeated automatically
        self.assertIn("NOT repeated automatically", r["state_reason"] + " ".join(M.current_execution(r)["notes"]))

    def test_successful_independent_verification(self):
        self.hooks["state"] = "on"
        m = self.mission([s("a", "t_hook", checks=self.check("on"))])
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "completed")
        self.assertTrue(M.current_execution(r)["steps"]["a"]["attempts"][0]["independent_check"])

    def test_unavailable_verification_pauses_as_ambiguous(self):
        m = self.mission([s("a", "t_unverified"), s("b", "t_ok", depends_on=["a"])])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.st(r, "a"), self.st(r, "b")), ("paused", "unverified", "skipped"))
        self.assertEqual(M.current_execution(r)["steps"]["a"]["attempts"][0]["verification"], "unavailable")
        self.assertNotIn("DONE and VERIFIED", [l for l in M.summarize(r).splitlines() if " a " in l][0])

    def test_inconclusive_check_pauses(self):
        self.env.settings.enabled["files_read"] = False
        m = self.mission([s("a", "t_hook", checks=[{"tool": "file_info", "args": {"path": "x"}, "field": "data.exists", "op": "true"}])])
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "paused")
        self.assertEqual(M.current_execution(r)["steps"]["a"]["attempts"][0]["verification"], "inconclusive")

    def test_tool_self_report_alone_is_labelled_not_independent(self):
        m = self.mission([s("a", "t_hook")])
        att = M.current_execution(self.run_m(m["id"]))["steps"]["a"]["attempts"][0]
        self.assertFalse(att["independent_check"])
        self.assertEqual(att["verification"], "verified")

    def test_a_plan_flag_cannot_make_a_changing_tool_repeat(self):
        """Prompt 13: REPLACES test_retry_safe_action_retried_until_success_within_limit, which encoded the unsafe rule that a `retry_safe` flag written in the
        plan lets a MODIFY tool be repeated. Now the registered tool decides, and a changing tool is never repeated by itself."""
        self.hooks["fail_times"] = 2
        m = self.mission([s("a", "t_flaky_mod", retries=2, retry_safe=True)])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.st(r, "a"), self.calls["t_flaky_mod"]), ("paused", "unknown", 1))
        rec = M.current_execution(r)["steps"]["a"]
        self.assertEqual(len(rec["attempts"]), 1)
        self.assertEqual(rec["retry_policy"]["allowed"], 0)

    def test_retries_of_a_repeatable_read_stop_at_the_limit(self):
        self.contract("t_flaky_obs")
        self.hooks["fail_times"] = 99
        m = self.mission([s("a", "t_flaky_obs", retries=2)])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.calls["t_flaky_obs"]), ("failed", 3))

    def test_changing_step_without_retry_safe_is_not_retried(self):
        self.hooks["fail_times"] = 1
        m = self.mission([s("a", "t_flaky_mod", retries=2)])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.st(r, "a"), self.calls["t_flaky_mod"]), ("paused", "unknown", 1))

    def test_repeatable_read_retries_without_any_plan_flag(self):
        self.contract("t_flaky_obs")
        self.hooks["fail_times"] = 2
        m = self.mission([s("a", "t_flaky_obs", retries=2)])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.calls["t_flaky_obs"]), ("completed", 3))
        self.assertEqual([a["kind"] for a in M.current_execution(r)["steps"]["a"]["attempts"]], ["initial", "auto_retry", "auto_retry"])

    def test_refusals_are_never_retried(self):
        self.env.settings.enabled["files_write"] = False
        m = self.mission([s("a", "write_file", {"path": "n.txt", "content": "x"}, retries=2, retry_safe=True)])
        self.run_m(m["id"])
        self.assertEqual(len(self.store.load(m["id"])["executions"][0]["steps"]["a"]["attempts"]), 1)

    def test_timeout_of_a_change_is_unknown_and_not_retried(self):
        self.hooks["sleep"] = 1.0
        m = self.mission([s("a", "t_slow_mod", retries=2, retry_safe=True), s("b", "t_ok", depends_on=["a"])])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.st(r, "a"), self.st(r, "b")), ("paused", "unknown", "skipped"))
        self.assertEqual(self.calls["t_slow_mod"], 1)
        self.assertIn("UNKNOWN", r["state_reason"])

    def test_timeout_of_a_repeatable_read_is_retried_within_limit(self):
        self.contract("t_slow_obs")
        self.hooks["sleep"] = 1.0
        m = self.mission([s("a", "t_slow_obs", retries=1)])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.calls["t_slow_obs"]), ("paused", 2))

    def test_attempt_limit_for_the_whole_mission(self):
        self.contract("t_flaky_obs")
        self.hooks["fail_times"] = 99
        m = self.mission([s("a", "t_flaky_obs", retries=2), s("b", "t_flaky_obs", retries=2)], max_attempts_total=2)
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertLessEqual(self.calls["t_flaky_obs"], 2)

    def test_total_time_limit(self):
        ticks = iter(range(0, 10_000, 600))
        self.runner.clock = lambda: next(ticks)
        m = self.mission([s("a", "t_ok"), s("b", "t_ok", depends_on=["a"]), s("c", "t_ok", depends_on=["b"])], max_total_seconds=900)
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertIn("time limit", r["state_reason"])
        self.assertLess(self.calls["t_ok"], 3)


class ControlsAndRecovery(P9):
    def setUp(self):
        super().setUp()
        self.make9()

    def test_cancel_requested_during_a_step_takes_effect_after_it_and_says_so(self):
        m = self.mission([s("a", "t_hook"), s("b", "t_ok", depends_on=["a"])])
        self.hooks["hook"] = lambda env, n: self.store.flag(m["id"], "cancel")
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "cancelled")
        self.assertEqual((self.st(r, "a"), self.calls["t_ok"]), ("succeeded", 0))          # the running step could not be interrupted; its result was kept
        self.assertEqual(self.st(r, "b"), "pending")
        self.assertIn("could not be interrupted", " ".join(M.current_execution(r)["notes"]))
        self.assertFalse(self.store.flagged(m["id"], "cancel"))

    def test_pause_then_resume_does_not_repeat_finished_steps(self):
        m = self.mission([s("a", "t_hook"), s("b", "t_ok", depends_on=["a"])])
        self.hooks["hook"] = lambda env, n: self.store.flag(m["id"], "pause")
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.st(r, "a"), self.st(r, "b")), ("paused", "succeeded", "pending"))
        self.hooks["hook"] = None
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "completed")
        self.assertEqual((self.calls["t_hook"], self.calls["t_ok"]), (1, 1))
        self.assertEqual(len(r["executions"]), 1)                      # resumed the same execution

    def test_emergency_stop_mid_run(self):
        m = self.mission([s("a", "t_hook"), s("b", "t_ok", depends_on=["a"])])
        self.hooks["hook"] = lambda env, n: env.stop.stop("test stop")
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "cancelled")
        self.assertEqual(self.calls["t_ok"], 0)
        with self.assertRaises(MissionError):
            self.run_m(m["id"])                                         # refuses to start while the stop is active

    def test_emergency_stop_before_start(self):
        m = self.mission([s("a", "t_ok")])
        self.env.stop.stop("pre")
        with self.assertRaises(MissionError):
            self.run_m(m["id"])
        self.assertEqual(self.calls["t_ok"], 0)

    def test_crash_mid_step_leaves_unknown_and_nothing_is_repeated_after_restart(self):
        m = self.mission([s("a", "t_crash"), s("b", "t_ok", depends_on=["a"])])
        with self.assertRaises(Crash):
            self.run_m(m["id"])
        on_disk = self.store.load(m["id"])
        self.assertEqual((on_disk["state"], self.st(on_disk, "a")), ("running", "running"))       # intent was saved BEFORE acting
        (self.store.root / f"{m['id']}.lock").write_text('{"pid": 999999}')                       # the process that held the lock is gone
        self.new_runner()
        self.assertEqual(self.store.recover_all(), [m["id"]])
        r = self.store.load(m["id"])
        self.assertEqual((r["state"], self.st(r, "a")), ("paused", "unknown"))
        self.assertEqual(self.calls["t_crash"], 1)
        with self.assertRaises(MissionError) as cm:
            self.run_m(m["id"])                                         # a decision is needed; it will NOT silently run again
        self.assertIn("decision", str(cm.exception))
        self.assertEqual(self.calls["t_crash"], 1)
        r = self.run_m(m["id"], decisions={"a": "accept"})
        self.assertEqual(r["state"], "completed")
        self.assertEqual((self.calls["t_crash"], self.calls["t_ok"]), (1, 1))
        self.assertIn("accepted", M.summarize(r))

    def test_running_mission_with_a_live_owner_is_not_touched_by_recovery(self):
        m = self.mission([s("a", "t_ok")])
        mm = self.store.load(m["id"])
        M.set_state(mm, "running", "x")
        self.store.save(mm)
        self.store.lock(m["id"])
        import os
        (self.store.root / f"{m['id']}.lock").write_text(f'{{"pid": {os.getppid()}}}')
        self.assertEqual(self.store.recover_all(), [])
        self.assertEqual(self.store.load(m["id"])["state"], "running")
        with self.assertRaises(MissionError):
            self.run_m(m["id"])

    def test_decisions_retry_accept_skip(self):
        self.hooks["fail_times"] = 0
        self.contract("t_fail_obs")                                     # c fails definitely (a pure read), so it cannot be 'accepted'
        m = self.mission([s("a", "t_unverified"), s("b", "t_ok", depends_on=["a"]), s("c", "t_fail_obs")])
        r = self.run_m(m["id"])
        self.assertEqual(self.st(r, "a"), "unverified")
        with self.assertRaises(MissionError):
            self.run_m(m["id"])                                         # no decision
        with self.assertRaises(MissionError):
            self.run_m(m["id"], decisions={"a": "explode"})
        with self.assertRaises(MissionError):
            self.run_m(m["id"], decisions={"b": "retry"})               # b is skipped; needs no decision
        r = self.run_m(m["id"], decisions={"a": "accept"})
        self.assertEqual((self.st(r, "b"), self.st(r, "c"), r["state"]), ("succeeded", "failed", "failed"))
        with self.assertRaises(MissionError):
            self.run_m(m["id"], decisions={"c": "accept"})              # a definite failure cannot be 'accepted'
        r = self.run_m(m["id"], decisions={"c": "skip"})
        self.assertEqual(r["state"], "completed")
        self.assertIn("skipped", r["state_reason"])

    def test_retry_decision_runs_the_step_again_and_records_it(self):
        self.hooks["sleep"] = 1.0
        m = self.mission([s("a", "t_slow_mod")])
        self.run_m(m["id"])
        self.hooks["sleep"] = 0.0
        r = self.run_m(m["id"], decisions={"a": "retry"})
        self.assertEqual(r["state"], "completed")
        self.assertEqual(len(M.current_execution(r)["steps"]["a"]["attempts"]), 2)
        self.assertEqual(self.calls["t_slow_mod"], 2)

    def test_second_process_cannot_run_the_same_mission(self):
        m = self.mission([s("a", "t_ok")])
        import os
        (self.store.root).mkdir(exist_ok=True)
        (self.store.root / f"{m['id']}.lock").write_text(f'{{"pid": {os.getppid()}}}')
        with self.assertRaises(MissionError):
            self.run_m(m["id"])


class ApprovalAndAuthority(P9):
    def setUp(self):
        super().setUp()
        self.make9()

    def test_approval_is_single_use_and_bound_to_the_plan(self):
        m = self.mission([s("a", "t_hook")])
        self.run_m(m["id"])
        with self.assertRaises(MissionError):
            self.run_m(m["id"])                                         # needs a fresh approval
        self.approve(m["id"]) if False else None
        mm = self.store.load(m["id"])
        M.set_state(mm, "draft")
        self.store.save(mm)
        with self.assertRaises(MissionError):
            self.run_m(m["id"])
        self.assertEqual(self.calls["t_hook"], 1)

    def test_approval_for_a_different_plan_is_useless(self):
        m = self.mission([s("a", "t_hook")])
        mm = self.store.load(m["id"])
        mm["approval"]["plan_hash"] = "0" * 64
        self.store.save(mm)
        with self.assertRaises(MissionError):
            self.run_m(m["id"])
        self.assertEqual(self.calls["t_hook"], 0)

    def test_running_a_mission_never_changes_permissions_or_consents(self):
        before = self.env.settings.to_dict()
        m = self.mission([s("a", "t_hook")])
        self.run_m(m["id"])
        self.assertEqual(self.env.settings.to_dict(), before)

    def test_approval_cannot_enable_a_disabled_category(self):
        self.env.settings.enabled["code_run"] = False
        m = self.mission([s("a", "run_code", {"code": "print(1)"})])
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertFalse(self.env.settings.enabled["code_run"])

    def test_plan_cannot_smuggle_approval_or_state(self):
        d = M.normalize({"goal": "g", "state": "approved", "approval": {"uses_left": 9}, "steps": [s("a", "t_hook", approved=True, uses_left=5)]}, self.ex.specs)
        m = M.new_mission(d)
        self.assertEqual((m["state"], m["approval"]), ("draft", None))
        self.assertNotIn("approved", m["steps"][0])

    def test_no_code_or_shell_execution_in_the_mission_package(self):
        for f in (ROOT / "src/jarvis/missions").glob("*.py"):
            t = f.read_text()
            for bad in ("subprocess", "os.system", "eval(", "exec(", "shell=True", "importlib", "__import__", "pickle", "marshal"):
                self.assertNotIn(bad, t, (f.name, bad))

    def test_mission_package_makes_no_network_calls(self):
        for f in (ROOT / "src/jarvis/missions").glob("*.py"):
            t = f.read_text()
            for bad in ("urllib", "socket", "requests", "http.client", "smtplib"):
                self.assertNotIn(bad, t, (f.name, bad))


if __name__ == "__main__":
    unittest.main()
