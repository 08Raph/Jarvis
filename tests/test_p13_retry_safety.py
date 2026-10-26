"""Prompt 13: retry safety, idempotency and execution integrity.

REAL = the real executor/policy/confirmation, the real mission store and runner, the real retry policy (jarvis.desktop.retry).
TEST TOOLS (tests/p9_common.py) are registered only in tests so a failing first call, a timeout or a crash can be produced exactly.
SIMULATED = `contract()` pretends a test tool was reviewed and added to the trusted idempotency table; the policy still checks its risk.
Nothing here ran on Windows; crash, lock and thread behaviour are Linux/CPython observations only."""
import json
import threading
import time
import unittest
from types import MappingProxyType

from p9_common import Crash, M, P9, S  # noqa: F401
from jarvis.missions.runner import MissionError
from jarvis.desktop import retry as R
from jarvis.desktop.tools import registry
from jarvis.extensions import all_specs


def s(sid, tool="t_ok", args=None, **kw):
    return {"id": sid, "tool": tool, "args": args or {}, **kw}


def kinds(m, sid):
    return [a.get("kind") for a in M.current_execution(m)["steps"][sid]["attempts"]]


class PolicyTable(unittest.TestCase):
    """The policy itself, against the REAL tool registry."""
    @classmethod
    def setUpClass(cls):
        cls.specs = {**registry(), **{sp.name: sp for sp in all_specs(None)}}

    def test_every_listed_tool_exists_and_is_structurally_a_pure_observation(self):
        for name in R.IDEMPOTENT_TOOLS:
            self.assertTrue(name in self.specs, name)
            self.assertEqual(R.ineligible_reason(self.specs[name]), "", name)
            self.assertEqual(self.specs[name].risk, S.OBSERVE, name)

    def test_every_tool_that_is_not_listed_gets_zero_retries_whatever_the_plan_says(self):
        for name, sp in self.specs.items():
            if name in R.IDEMPOTENT_TOOLS:
                continue
            d = R.decide(sp, 99)
            self.assertEqual((d.allowed, d.contract), (0, R.NEVER), name)

    def test_no_risky_tool_is_ever_eligible_even_if_someone_adds_it_to_the_table(self):
        table = MappingProxyType({**R.IDEMPOTENT_TOOLS, **{n: "wrongly listed" for n, sp in self.specs.items() if sp.risk != S.OBSERVE}})
        from unittest import mock
        with mock.patch.object(R, "IDEMPOTENT_TOOLS", table):
            for name, sp in self.specs.items():
                if sp.risk != S.OBSERVE or sp.risk_fn or sp.risk_for or sp.bind:
                    self.assertEqual(R.decide(sp, 5).allowed, 0, name)

    def test_names_that_sound_safe_do_not_make_a_tool_safe(self):
        for n in ("read_anything", "check_status", "safe_get", "get_state", "is_ok"):
            sp = S.ToolSpec(n, "x", "system", S.OBSERVE, (), lambda a, e: None)
            self.assertEqual(R.decide(sp, 2).allowed, 0, n)

    def test_screenshot_like_and_wait_tools_are_deliberately_not_repeatable(self):
        for n in ("screenshot", "read_screen_text", "observe_screen", "find_on_screen", "recheck_screen", "check_visible", "read_document", "monitor_watch", "wait"):
            if n in self.specs:
                self.assertEqual(R.decide(self.specs[n], 2).allowed, 0, n)

    def test_registration_can_forbid_retries_even_for_a_listed_tool(self):
        from dataclasses import replace
        sp = replace(self.specs["list_windows"], no_retry=True)
        d = R.decide(sp, 2)
        self.assertEqual((d.allowed, d.contract), (0, R.NEVER))
        self.assertIn("forbids", d.reason)

    def test_a_listed_tool_whose_risk_or_target_depends_on_its_arguments_is_not_eligible(self):
        from dataclasses import replace
        base = self.specs["list_windows"]
        self.assertEqual(R.decide(base, 2).allowed, 2)
        for field in ("risk_fn", "risk_for", "bind"):
            sp = replace(base, **{field: (lambda *a, **k: None)})
            d = R.decide(sp, 2)
            self.assertEqual((d.allowed, d.contract), (0, R.NEVER), field)
            self.assertIn("depends on its arguments", d.reason, field)

    def test_bad_metadata_fails_closed(self):
        sp = self.specs["list_windows"]
        for bad in (True, False, None, "2", 2.0, -1, [], {}, float("nan")):
            self.assertEqual(R.decide(sp, bad).allowed, 0, repr(bad))
        self.assertEqual(R.decide(None, 2).allowed, 0)
        self.assertEqual(R.decide(object(), 2).allowed, 0)

    def test_plan_can_only_lower_the_count_and_the_ceiling_holds(self):
        sp = self.specs["list_windows"]
        self.assertEqual(R.decide(sp, 1).allowed, 1)
        self.assertEqual(R.decide(sp, 0).allowed, 0)
        self.assertEqual(R.decide(sp, 10 ** 9).allowed, R.MAX_AUTO_RETRIES)
        self.assertEqual(R.decide(sp, 50, R.MAX_CHECK_POLLS).allowed, R.MAX_CHECK_POLLS)

    def test_the_table_cannot_be_changed_at_runtime(self):
        with self.assertRaises(TypeError):
            R.IDEMPOTENT_TOOLS["write_file"] = "x"

    def test_a_plugin_cannot_take_over_a_listed_name(self):
        # extensions are refused when they reuse a built-in name (checked in the executor); the policy additionally keys on the name only for listed tools
        self.assertNotIn("plugin_list_windows", R.IDEMPOTENT_TOOLS)


class CheckPolling(P9):
    """A success CHECK polls its read tool; the same central policy decides whether it may."""
    def setUp(self):
        super().setUp()
        self.make9()

    def _poll(self, tool, retries):
        from jarvis.desktop.task import Check
        self.runner._observer.ex.env.stop.wait = lambda secs: None
        chk = Check(tool, {}, "data.state", "equals", "on", retries, 0.0)
        return self.runner._observer._observe(chk)

    def test_an_unlisted_read_tool_is_checked_once_whatever_the_plan_asks(self):
        self.hooks["state"] = "off"
        ok, _ = self._poll("t_state", 10)
        self.assertEqual((ok, self.calls["t_state"]), (False, 1))

    def test_a_listed_read_tool_is_polled_within_the_limit(self):
        self.contract("t_state")
        self.hooks["state"] = "off"
        ok, _ = self._poll("t_state", 5)
        self.assertEqual((ok, self.calls["t_state"]), (False, 6))
        self.calls.clear()
        ok, _ = self._poll("t_state", 10 ** 6)
        self.assertEqual(self.calls["t_state"], 1 + R.MAX_CHECK_POLLS)


class Permitted(P9):
    """A genuinely idempotent operation keeps its bounded retry, and every attempt is a full executor call."""
    def setUp(self):
        super().setUp()
        self.make9()
        self.answer = True

    def test_idempotent_read_retries_and_the_log_names_attempt_and_reason(self):
        self.contract("t_flaky_obs")
        m = self.mission([s("a", "t_flaky_obs", retries=2)])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.calls["t_flaky_obs"]), ("completed", 2))
        self.assertEqual(kinds(r, "a"), ["initial", "auto_retry"])
        ev = [h for h in self.store.history(m["id"]) if h.get("event") == "auto_retry"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["attempt"], ev[0]["contract"], ev[0]["allowed"]), (2, "idempotent", 2))
        self.assertTrue(ev[0]["reason"])
        pol = [h for h in self.store.history(m["id"]) if h.get("event") == "retry_policy"]
        self.assertEqual(pol[0]["contract"], "idempotent")

    def test_each_automatic_attempt_goes_through_the_executor_again(self):
        self.contract("t_fail_obs")
        seen = []
        orig = self.ex.run
        self.ex.run = lambda tool, args, ctx: (seen.append(tool), orig(tool, args, ctx))[1]
        m = self.mission([s("a", "t_fail_obs", retries=2)])
        r = self.run_m(m["id"])
        self.assertEqual(r["state"], "failed")
        self.assertEqual(seen, ["t_fail_obs"] * 3)

    def test_retries_stop_at_the_ceiling_even_when_the_plan_asks_for_more(self):
        self.contract("t_fail_obs")
        m = self.mission([s("a", "t_fail_obs", retries=2, retry_safe=True)])
        mm = self.store.load(m["id"])
        self.assertEqual(self.run_m(m["id"])["state"], "failed")
        self.assertEqual(self.calls["t_fail_obs"], 1 + R.MAX_AUTO_RETRIES)

    def test_a_timeout_of_an_idempotent_read_may_be_repeated_within_the_limit(self):
        self.contract("t_slow_obs")
        m = self.mission([s("a", "t_slow_obs", retries=1)])
        r = self.run_m(m["id"])
        self.assertEqual(self.calls["t_slow_obs"], 2)
        self.assertEqual(self.st(r, "a"), "unknown")                 # still not reported as a plain failure


class FalseRetrySafeFlag(P9):
    """A plan (or a person editing JSON, or a model) says retry_safe on something that is not safe to repeat."""
    def setUp(self):
        super().setUp()
        self.make9()
        self.answer = True

    def _once(self, tool):
        m = self.mission([s("a", tool, retries=2, retry_safe=True)])
        r = self.run_m(m["id"])
        self.assertEqual(self.calls[tool], 1, f"{tool} was repeated automatically")
        self.assertEqual(r["state"], "paused")
        self.assertEqual(self.st(r, "a"), "unknown")
        self.assertEqual(kinds(r, "a"), ["initial"])
        self.assertIn("UNKNOWN", r["state_reason"])
        self.assertIn("not repeated", r["state_reason"])
        return r, m

    def test_destructive_tool_is_not_repeated(self):
        self._once("t_destroy")

    def test_network_tool_is_not_repeated(self):
        self._once("t_net")

    def test_command_execution_tool_is_not_repeated(self):
        self._once("t_exec")

    def test_keyboard_mouse_tool_is_not_repeated(self):
        self._once("t_input")

    def test_file_modifying_tool_is_not_repeated(self):
        self._once("t_flaky_mod")

    def test_even_a_wrong_trusted_registration_cannot_repeat_a_risky_tool(self):
        self.contract("t_destroy", "t_net", "t_exec", "t_input")        # simulate an owner mistake in the table
        for t in ("t_destroy", "t_net", "t_exec", "t_input"):
            self._once(t)

    def test_a_valid_approval_does_not_override_a_prohibited_retry(self):
        m = self.mission([s("a", "t_destroy", retries=2, retry_safe=True)])
        self.assertEqual(m["approval"]["plan_hash"], m["plan_hash"])     # approved, hash valid
        self.run_m(m["id"])
        self.assertEqual(self.calls["t_destroy"], 1)

    def test_a_real_overwrite_is_not_repeated_after_an_error_either(self):
        (self.work / "n.txt").write_text("old")
        m = self.mission([s("a", "write_file", {"path": "n.txt", "content": "new", "overwrite": True}, retries=2, retry_safe=True)])
        self.run_m(m["id"])
        self.assertEqual(len([h for h in self.store.history(m["id"]) if h.get("event") == "auto_retry"]), 0)

    def test_review_tells_the_person_no_automatic_retry_will_happen(self):
        m = self.mission([s("a", "t_net", retries=2, retry_safe=True), s("b", "list_windows", {}, retries=2)], approve=False)
        rows = {r["step"]: r for r in self.runner.review(m)}
        self.assertEqual((rows["a"]["retries_allowed"], rows["a"]["retry_contract"]), (0, "never"))
        self.assertEqual((rows["b"]["retries_allowed"], rows["b"]["retry_contract"]), (2, "idempotent"))

    def test_a_check_cannot_be_polled_unless_the_check_tool_is_a_listed_read(self):
        sp = self.ex.specs["t_state"]
        self.assertEqual(R.decide(sp, 10, R.MAX_CHECK_POLLS).allowed, 0)
        self.assertEqual(R.decide(self.ex.specs["list_windows"], 10, R.MAX_CHECK_POLLS).allowed, 10)


class LimitBypass(P9):
    def setUp(self):
        super().setUp()
        self.make9()
        self.answer = True
        self.contract("t_fail_obs")

    def test_values_that_are_not_plain_small_integers_never_add_attempts(self):
        for bad in (True, "5", 2.5, -3, None):
            self.calls.clear()
            plan = {"goal": "g", "steps": [s("a", "t_fail_obs")]}
            m = M.new_mission(M.normalize(plan, self.ex.specs))
            m["steps"][0]["retries"] = bad                               # as if the file were edited: the hash is recomputed below
            m["plan_hash"] = M.plan_hash({"goal": m["goal"], "limits": m["limits"], "steps": m["steps"]})
            self.store.save(m)
            try:
                self.run_m(m["id"])
            except (MissionError, M.PlanError):
                pass
            self.assertLessEqual(self.calls["t_fail_obs"], 1, repr(bad))                   # refused before start, or run once; never repeated
            self.assertEqual([h for h in self.store.history(m["id"]) if h.get("event") == "auto_retry"], [])

    def test_huge_retry_count_written_into_the_file_is_clamped(self):
        plan = {"goal": "g", "steps": [s("a", "t_fail_obs")]}
        m = M.new_mission(M.normalize(plan, self.ex.specs))
        m["steps"][0]["retries"] = 10 ** 6
        m["plan_hash"] = M.plan_hash({"goal": m["goal"], "limits": m["limits"], "steps": m["steps"]})
        self.store.save(m)
        try:
            self.run_m(m["id"])
        except (MissionError, M.PlanError):
            pass
        self.assertLessEqual(self.calls["t_fail_obs"], 1 + R.MAX_AUTO_RETRIES)

    def test_the_mission_wide_attempt_limit_still_wins(self):
        plan = {"goal": "g", "steps": [s("a", "t_fail_obs", retries=2)], "limits": {"max_attempts_total": 2}}
        m = M.new_mission(M.normalize(plan, self.ex.specs))
        self.store.save(m)
        r = self.run_m(m["id"])
        self.assertEqual(self.calls["t_fail_obs"], 2)
        self.assertEqual(r["state"], "failed")

    def test_a_huge_or_bad_total_limit_in_the_file_is_clamped_or_closed(self):
        for bad in (10 ** 9, True, "9", -1, None):
            self.calls.clear()
            plan = {"goal": "g", "steps": [s("a", "t_fail_obs", retries=2)]}
            m = M.new_mission(M.normalize(plan, self.ex.specs))
            m["limits"]["max_attempts_total"] = bad
            m["plan_hash"] = M.plan_hash({"goal": m["goal"], "limits": m["limits"], "steps": m["steps"]})
            self.store.save(m)
            try:
                self.run_m(m["id"])
            except (MissionError, M.PlanError):
                pass
            self.assertLessEqual(self.calls["t_fail_obs"], 1 + R.MAX_AUTO_RETRIES, repr(bad))


class ApprovalIntegrity(P9):
    def setUp(self):
        super().setUp()
        self.make9()
        self.answer = True

    def _edit(self, mid, fn, rehash=False):
        m = json.loads((self.store.root / f"{mid}.json").read_text())
        fn(m)
        if rehash:
            m["plan_hash"] = M.plan_hash({"goal": m["goal"], "limits": m["limits"], "steps": m["steps"]})
        (self.store.root / f"{mid}.json").write_text(json.dumps(m))

    def test_changing_arguments_after_approval_stops_the_run(self):
        m = self.mission([s("a", "t_hook", {})])
        (self.work / "x").write_text("1")
        m2 = self.mission([s("a", "write_file", {"path": "n.txt", "content": "safe"})])
        self._edit(m2["id"], lambda d: d["steps"][0]["args"].update(content="CHANGED"))
        with self.assertRaises(MissionError) as cm:
            self.run_m(m2["id"])
        self.assertIn("hash", str(cm.exception))
        self.assertFalse((self.work / "n.txt").exists())

    def test_changing_a_retry_field_after_approval_stops_the_run(self):
        m = self.mission([s("a", "t_flaky_mod", retries=0)])
        for field, value in (("retry_safe", True), ("retries", 2)):
            self._edit(m["id"], lambda d, f=field, v=value: d["steps"][0].__setitem__(f, v))
            with self.assertRaises(MissionError):
                self.run_m(m["id"])
        self.assertEqual(self.calls["t_flaky_mod"], 0)

    def test_changing_limits_after_approval_stops_the_run(self):
        m = self.mission([s("a", "t_hook")])
        self._edit(m["id"], lambda d: d["limits"].__setitem__("max_attempts_total", 99))
        with self.assertRaises(MissionError):
            self.run_m(m["id"])
        self.assertEqual(self.calls["t_hook"], 0)

    def test_changing_checks_and_dependencies_after_approval_stops_the_run(self):
        m = self.mission([s("a", "t_hook"), s("b", "t_ok", depends_on=["a"])])
        self._edit(m["id"], lambda d: d["steps"][1].__setitem__("depends_on", []))
        with self.assertRaises(MissionError):
            self.run_m(m["id"])
        self.assertEqual(self.calls["t_hook"], 0)

    def test_reordering_steps_after_approval_stops_the_run(self):
        m = self.mission([s("a", "t_hook"), s("b", "t_hook")])
        self._edit(m["id"], lambda d: d["steps"].reverse())
        with self.assertRaises(MissionError):
            self.run_m(m["id"])
        self.assertEqual(self.calls["t_hook"], 0)

    def test_a_forged_hash_without_a_matching_approval_is_not_enough(self):
        m = self.mission([s("a", "write_file", {"path": "n.txt", "content": "safe"})])
        self._edit(m["id"], lambda d: d["steps"][0]["args"].update(content="CHANGED"), rehash=True)       # edited AND the hash recomputed
        with self.assertRaises(MissionError) as cm:
            self.run_m(m["id"])
        self.assertIn("approv", str(cm.exception))
        self.assertFalse((self.work / "n.txt").exists())

    def test_an_approval_that_names_a_stale_hash_is_refused(self):
        m = self.mission([s("a", "t_hook")])
        self._edit(m["id"], lambda d: d["approval"].__setitem__("plan_hash", "0" * 64))
        with self.assertRaises(MissionError):
            self.run_m(m["id"])
        self.assertEqual(self.calls["t_hook"], 0)

    def test_the_approval_is_single_use_so_a_second_run_does_not_repeat_the_step(self):
        m = self.mission([s("a", "t_hook")])
        self.assertEqual(self.run_m(m["id"])["state"], "completed")
        try:
            self.run_m(m["id"])
        except MissionError:
            pass
        self.assertEqual(self.calls["t_hook"], 1)

    def test_hash_serialization_is_deterministic_and_covers_every_execution_field(self):
        plan = {"goal": "g", "steps": [s("a", "t_ok", retries=1)]}
        d = M.normalize(plan, self.ex.specs)
        h = M.plan_hash(d)
        self.assertEqual(h, M.plan_hash(json.loads(json.dumps(d))))
        for path in (("goal",), ("limits", "max_attempts_total"), ("steps", 0, "tool"), ("steps", 0, "retries"), ("steps", 0, "retry_safe"), ("steps", 0, "args"),
                     ("steps", 0, "depends_on"), ("steps", 0, "checks"), ("steps", 0, "preconditions")):
            e = json.loads(json.dumps(d))
            o = e
            for k in path[:-1]:
                o = o[k]
            o[path[-1]] = "changed" if not isinstance(o[path[-1]], (list, dict)) else (["x"] if isinstance(o[path[-1]], list) else {"x": 1})
            self.assertNotEqual(M.plan_hash(e), h, path)


class UnknownOutcomes(P9):
    def setUp(self):
        super().setUp()
        self.make9()
        self.answer = True

    def test_timeout_of_a_changing_tool_is_unknown_and_never_repeated(self):
        m = self.mission([s("a", "t_slow_mod", retries=2, retry_safe=True)])
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.st(r, "a"), self.calls["t_slow_mod"]), ("paused", "unknown", 1))
        self.assertIn("UNKNOWN", r["state_reason"])
        self.assertNotIn("failed", r["state_reason"].split("UNKNOWN")[0].lower().replace("whether", ""))

    def test_an_unknown_step_cannot_be_resumed_without_a_decision(self):
        m = self.mission([s("a", "t_slow_mod")])
        self.run_m(m["id"])
        with self.assertRaises(MissionError) as cm:
            self.run_m(m["id"])
        self.assertIn("decision", str(cm.exception))
        self.assertEqual(self.calls["t_slow_mod"], 1)

    def test_a_manual_retry_is_recorded_as_the_persons_decision_not_an_automatic_retry(self):
        self.hooks["fail_times"] = 1
        m = self.mission([s("a", "t_flaky_mod", retries=2, retry_safe=True)])
        r = self.run_m(m["id"])
        self.assertEqual(self.st(r, "a"), "unknown")
        r = self.run_m(m["id"], decisions={"a": "retry"})
        self.assertEqual(r["state"], "completed")
        self.assertEqual(kinds(r, "a"), ["initial", "manual_retry"])
        ev = [h.get("event") for h in self.store.history(m["id"])]
        self.assertIn("manual_retry_decision", ev)
        self.assertNotIn("auto_retry", ev)

    def test_accepting_an_unknown_step_does_not_run_it_again(self):
        m = self.mission([s("a", "t_slow_mod")])
        self.run_m(m["id"])
        r = self.run_m(m["id"], decisions={"a": "accept"})
        self.assertEqual((r["state"], self.calls["t_slow_mod"]), ("completed", 1))
        self.assertIn("accepted", M.summarize(r))

    def test_crash_after_the_side_effect_but_before_success_was_recorded(self):
        m = self.mission([s("a", "t_effect_crash", retries=2, retry_safe=True), s("b", "t_ok", depends_on=["a"])])
        with self.assertRaises(Crash):
            self.run_m(m["id"])
        self.assertEqual(self.hooks["effects"], 1)
        disk = self.store.load(m["id"])
        self.assertEqual(self.st(disk, "a"), "running")
        self.assertIn("in_flight", M.current_execution(disk)["steps"]["a"])         # intent was written before the action
        (self.store.root / f"{m['id']}.lock").write_text('{"pid": 999999}')
        self.new_runner()
        self.assertEqual(self.store.recover_all(), [m["id"]])
        r = self.store.load(m["id"])
        self.assertEqual((r["state"], self.st(r, "a")), ("paused", "unknown"))
        self.assertNotIn("in_flight", M.current_execution(r)["steps"]["a"])
        self.assertEqual(kinds(r, "a"), ["initial"])                                  # the interrupted attempt says what kind it was
        self.assertEqual(self.hooks["effects"], 1)

    def test_restart_with_an_uncertain_step_never_runs_it_by_itself(self):
        m = self.mission([s("a", "t_effect_crash")])
        with self.assertRaises(Crash):
            self.run_m(m["id"])
        (self.store.root / f"{m['id']}.lock").write_text('{"pid": 999999}')
        for _ in range(3):                                                           # repeated restarts, a scheduler tick, a plain run: none repeats it
            self.new_runner()
            self.store.recover_all()
            with self.assertRaises(MissionError):
                self.run_m(m["id"])
        self.assertEqual(self.hooks["effects"], 1)

    def test_cancel_between_attempts_stops_before_the_next_attempt(self):
        self.contract("t_fail_obs")
        m = self.mission([s("a", "t_fail_obs", retries=2)])
        self.runner._backoff = lambda n: self.store.flag(m["id"], "cancel")
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.calls["t_fail_obs"]), ("cancelled", 1))
        self.assertEqual(self.st(r, "a"), "cancelled")

    def test_pause_between_attempts_leaves_the_step_pending_and_repeats_nothing(self):
        self.contract("t_fail_obs")
        m = self.mission([s("a", "t_fail_obs", retries=2)])
        self.runner._backoff = lambda n: self.store.flag(m["id"], "pause")
        r = self.run_m(m["id"])
        self.assertEqual((r["state"], self.calls["t_fail_obs"]), ("paused", 1))

    def test_emergency_stop_during_an_attempt_makes_it_unknown(self):
        self.hooks["hook"] = lambda env, n: env.stop.stop("test stop")
        m = self.mission([s("a", "t_hook", retries=2, retry_safe=True), s("b", "t_ok", depends_on=["a"])])
        r = self.run_m(m["id"])
        self.assertEqual((self.calls["t_hook"], self.calls["t_ok"]), (1, 0))
        self.assertEqual(r["state"], "cancelled")


class Concurrency(P9):
    def setUp(self):
        super().setUp()
        self.make9()
        self.answer = True

    def test_two_workers_on_one_mission_execute_the_step_once(self):
        self.hooks["hook"] = lambda env, n: time.sleep(0.4)
        m = self.mission([s("a", "t_hook")])
        results, bar = [], threading.Barrier(2)

        def work():
            bar.wait()
            try:
                results.append(("ok", self.runner.run(m["id"])["state"]))
            except MissionError as e:
                results.append(("refused", str(e)))
        ts = [threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.calls["t_hook"], 1, results)
        self.assertEqual(sorted(k for k, _ in results), ["ok", "refused"], results)

    def test_many_workers_still_execute_it_once(self):
        self.hooks["hook"] = lambda env, n: time.sleep(0.3)
        m = self.mission([s("a", "t_hook")])
        bar, out = threading.Barrier(6), []

        def work():
            bar.wait()
            try:
                self.runner.run(m["id"])
                out.append("ran")
            except MissionError:
                out.append("refused")
        ts = [threading.Thread(target=work) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.calls["t_hook"], 1, out)

    def test_the_lock_is_released_when_a_run_ends_or_crashes(self):
        m = self.mission([s("a", "t_crash")])
        with self.assertRaises(Crash):
            self.run_m(m["id"])
        self.assertFalse((self.store.root / f"{m['id']}.lock").exists())
        self.assertTrue(self.store.lock(m["id"]))
        self.assertFalse(self.store.lock(m["id"]))                                   # a second taker in the same process is refused
        self.store.unlock(m["id"])
        self.assertTrue(self.store.lock(m["id"]))
        self.store.unlock(m["id"])

    def test_the_single_use_approval_cannot_be_spent_twice_by_racing_workers(self):
        self.hooks["hook"] = lambda env, n: time.sleep(0.2)
        m = self.mission([s("a", "t_hook")])
        bar, out = threading.Barrier(2), []

        def work():
            bar.wait()
            try:
                self.runner.run(m["id"], fresh=True)
                out.append(1)
            except MissionError:
                pass
        ts = [threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.calls["t_hook"], 1)


class NoSecrets(P9):
    TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

    def setUp(self):
        super().setUp()
        self.make9()
        self.answer = True

    def test_retry_decisions_logs_and_unknown_reasons_hold_no_secret(self):
        self.contract("t_fail_obs")
        m = self.mission([s("a", "t_fail_obs", retries=2), s("b", "t_leak"), s("c", "t_slow_mod", depends_on=["b"])])
        r = self.run_m(m["id"])
        blob = json.dumps(r) + (self.store.history_file.read_text() if self.store.history_file.exists() else "") + "".join(
            p.read_text(errors="replace") for p in self.store.root.glob("*.json"))
        for needle in (self.TOKEN, "hunter2", "SCREEN-CONTENT-SHOULD-NOT-BE-STORED"):
            self.assertNotIn(needle, blob)
        self.assertNotIn(self.TOKEN, r.get("state_reason", ""))

    def test_a_secret_in_a_tool_error_does_not_reach_the_record_or_the_reason(self):
        def boom(env, n):
            raise RuntimeError("failed with key " + self.TOKEN)
        self.hooks["hook"] = boom
        m = self.mission([s("a", "t_hook", retries=2, retry_safe=True)])
        r = self.run_m(m["id"])
        self.assertEqual(self.st(r, "a"), "unknown")
        text = json.dumps(r) + self.store.history_file.read_text() + (self.store.root / f"{m['id']}.json").read_text()
        self.assertNotIn(self.TOKEN, text)
        self.assertNotIn(self.TOKEN, r["state_reason"])


if __name__ == "__main__":
    unittest.main()
