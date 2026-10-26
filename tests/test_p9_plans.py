"""Prompt 9: plan validation, the mission store, the state machine, history, redaction and deletion.
REAL: real validation against the real tool registry, real files in a temp profile. No model, no Windows."""
import json
import unittest

from p9_common import P9, M, MissionStore, ROOT, ScriptedModel, call  # noqa: F401
from jarvis import modes
from jarvis.missions import store as ST


def step(sid="s1", tool="file_info", args=None, **kw):
    return {"id": sid, "tool": tool, "args": args if args is not None else {"path": "a.txt"}, **kw}


class PlanValidation(P9):
    def setUp(self):
        super().setUp()
        self.make9()

    def bad(self, plan, *words):
        with self.assertRaises(M.PlanError) as cm:
            M.normalize(plan, self.ex.specs)
        text = " | ".join(cm.exception.problems)
        for w in words:
            self.assertIn(w, text)
        return cm.exception.problems

    def test_valid_plan_gets_ids_defaults_and_a_stable_hash(self):
        d = M.normalize({"goal": "g", "steps": [{"tool": "file_info", "args": {"path": "a.txt"}}, {"tool": "system_info", "depends_on": ["s1"]}]}, self.ex.specs)
        self.assertEqual([s["id"] for s in d["steps"]], ["s1", "s2"])
        self.assertEqual(d["limits"]["max_total_seconds"], 900)
        self.assertEqual(M.plan_hash(d), M.plan_hash(json.loads(json.dumps(d))))
        d2 = json.loads(json.dumps(d))
        d2["steps"][0]["args"]["path"] = "b.txt"
        self.assertNotEqual(M.plan_hash(d), M.plan_hash(d2))

    def test_structure_errors(self):
        self.bad("not an object", "object")
        self.bad({"steps": [step()]}, "goal")
        self.bad({"goal": "g"}, "non-empty list")
        self.bad({"goal": "g", "steps": []}, "non-empty list")
        self.bad({"goal": "x" * 501, "steps": [step()]}, "too long")
        self.bad({"goal": "g", "steps": ["nope"]}, "must be an object")
        self.bad({"goal": "g", "steps": [step(sid="Bad Id")]}, "id must be")
        self.bad({"goal": "g", "steps": [step(), step()]}, "duplicate step id")
        self.bad({"goal": "g", "steps": [{"id": "s1"}]}, "needs a tool")
        self.bad({"goal": "g", "steps": [step(args="x")]}, "args must be an object")

    def test_too_many_steps(self):
        self.bad({"goal": "g", "steps": [step(sid=f"s{i}") for i in range(31)]}, "too many steps")

    def test_missing_dependency_self_dependency_and_cycles(self):
        self.bad({"goal": "g", "steps": [step(depends_on=["zzz"])]}, "not a step of this plan")
        self.bad({"goal": "g", "steps": [step(depends_on=["s1"])]}, "depends on itself")
        p = self.bad({"goal": "g", "steps": [step("a", depends_on=["c"]), step("b", depends_on=["a"]), step("c", depends_on=["b"])]}, "circle")
        self.assertTrue(any("->" in x for x in p))

    def test_unregistered_tools_and_invalid_arguments_are_rejected_up_front(self):
        self.bad({"goal": "g", "steps": [step(tool="run_command", args={"cmd": "format c:"})]}, "not a registered tool")
        for t in ("shell", "powershell", "eval", "set_permission", "enable_category", "approve_plan"):
            self.bad({"goal": "g", "steps": [step(tool=t, args={})]}, "not a registered tool")
        self.bad({"goal": "g", "steps": [step(args={"path": 5})]}, "invalid arguments")
        self.bad({"goal": "g", "steps": [step(args={"path": "a", "evil": 1})]}, "invalid arguments")

    def test_mode_restrictions_apply_to_plans(self):
        modes.set_mode(self.profile, "research")
        with self.assertRaises(M.PlanError) as cm:
            M.normalize({"goal": "g", "steps": [step(tool="write_file", args={"path": "a", "content": "x"})]}, self.ex.specs, lambda sp: modes.blocked(self.profile, sp))
        self.assertIn("research mode", str(cm.exception))

    def test_secrets_are_refused_in_goal_and_arguments(self):
        key = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        self.bad({"goal": "use " + key, "steps": [step()]}, "secret")
        self.bad({"goal": "g", "steps": [step(tool="write_file", args={"path": "a.txt", "content": key})]}, "secret")

    def test_limits_retries_and_checks_are_bounded_and_validated(self):
        self.bad({"goal": "g", "steps": [step()], "limits": {"max_total_seconds": 99999}}, "limit")
        self.bad({"goal": "g", "steps": [step()], "limits": {"max_total_seconds": True}}, "limit")
        self.bad({"goal": "g", "steps": [step()], "limits": {"nope": 1}}, "unknown limit")
        self.bad({"goal": "g", "steps": [step(retries=3)]}, "retries")
        self.bad({"goal": "g", "steps": [step(retries=True)]}, "retries")
        self.bad({"goal": "g", "steps": [step(checks=[{"tool": "write_file", "args": {"path": "a", "content": "x"}}])]}, "observation tool")
        self.bad({"goal": "g", "steps": [step(checks=[{"tool": "file_info", "args": {"path": "a"}, "op": "explode"}])]}, "op must be")
        self.bad({"goal": "g", "steps": [step(checks=[{"tool": "nothing"}])]}, "observation tool")

    def test_all_problems_are_listed_not_just_the_first(self):
        p = self.bad({"goal": "", "steps": [step(tool="nope"), step("s2", depends_on=["x"])]}, "goal", "not a registered", "not a step")
        self.assertGreaterEqual(len(p), 3)

    def test_order_is_dependency_respecting_and_stable(self):
        d = M.normalize({"goal": "g", "steps": [step("c", depends_on=["b"]), step("b", depends_on=["a"]), step("a"), step("z")]}, self.ex.specs)
        self.assertEqual(M.order(d["steps"]), ["a", "b", "c", "z"])

    def test_consequential_detection(self):
        ro = M.normalize({"goal": "g", "steps": [step(), step("s2", tool="system_info", args={})]}, self.ex.specs)
        rw = M.normalize({"goal": "g", "steps": [step(), step("s2", tool="write_file", args={"path": "a.txt", "content": "x"})]}, self.ex.specs)
        self.assertFalse(M.consequential(ro, self.ex.specs))
        self.assertTrue(M.consequential(rw, self.ex.specs))
        self.assertTrue(M.consequential({"steps": [{"tool": "unknown_tool"}]}, self.ex.specs))

    def test_unattended_rules(self):
        d = M.normalize({"goal": "g", "steps": [step(), step("s2", tool="web_search", args={"query": "python"})]}, self.ex.specs)
        self.assertTrue(M.unattended_problems(d, self.ex.specs))
        d2 = M.normalize({"goal": "g", "steps": [step()]}, self.ex.specs)
        self.assertEqual(M.unattended_problems(d2, self.ex.specs), [])


class StateMachineAndStore(P9):
    def setUp(self):
        super().setUp()
        self.make9()

    def fresh(self):
        return self.mission([step()])

    def test_a_new_mission_is_a_draft_and_unrun_summary_says_proposal(self):
        m = self.fresh()
        self.assertEqual(m["state"], "draft")
        self.assertEqual(m["executions"], [])
        t = M.summarize(m)
        self.assertIn("NOT been run", t)
        self.assertIn("proposal only", t)

    def test_illegal_transitions_are_refused(self):
        m = self.fresh()
        for bad in ("completed", "failed", "paused"):
            with self.assertRaises(M.StateError):
                M.set_state(m, bad)
        with self.assertRaises(M.StateError):
            M.set_state(m, "exploded")
        M.set_state(m, "cancelled")
        with self.assertRaises(M.StateError):
            M.set_state(m, "running")
        M.set_state(m, "draft")

    def test_every_documented_state_exists(self):
        for s in ("draft", "awaiting_approval", "running", "paused", "completed", "failed", "cancelled"):
            self.assertIn(s, M.STATES)

    def test_roundtrip_keeps_the_hash_even_though_hashes_look_like_tokens(self):
        m = self.fresh()
        again = self.store.load(m["id"])
        self.assertEqual(again["plan_hash"], m["plan_hash"])
        self.assertFalse(again.get("tampered"))

    def test_editing_the_saved_steps_is_detected(self):
        m = self.fresh()
        f = self.store._path(m["id"])
        d = json.loads(f.read_text())
        d["steps"][0]["args"]["path"] = "other.txt"
        f.write_text(json.dumps(d))
        self.assertTrue(self.store.load(m["id"])["tampered"])
        with self.assertRaises(Exception):
            self.runner.run(m["id"])
        self.assertTrue(self.store.list()[0]["tampered"])

    def test_damaged_files_and_hostile_ids(self):
        m = self.fresh()
        self.store._path(m["id"]).write_text("{not json")
        with self.assertRaises(M.StateError):
            self.store.load(m["id"])
        self.assertEqual(self.store.list()[0]["state"], "damaged")
        for bad in ("../x", "m-1", "", "m-20260101-120000-zzzzzz", "..\\..\\x"):
            with self.assertRaises(M.StateError):
                self.store._path(bad)

    def test_writes_are_atomic_no_temp_files_left(self):
        self.fresh()
        self.assertEqual([p.name for p in self.store.root.glob(".m-*")], [])

    def test_proposals_are_not_executions(self):
        m = self.fresh()
        self.assertIsNone(M.current_execution(m))
        self.assertEqual(self.calls["t_ok"], 0)


class HistoryRedactionDeletion(P9):
    KEY = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

    def setUp(self):
        super().setUp()
        self.make9()

    def test_secrets_and_screen_like_text_are_not_stored(self):
        m = self.mission([step("a", "t_leak", {})])
        self.run_m(m["id"])
        blob = "".join(p.read_text() for p in self.store.root.glob("*") if p.is_file())
        self.assertNotIn(self.KEY, blob)
        self.assertNotIn("SCREEN-CONTENT-SHOULD-NOT-BE-STORED", blob)
        self.assertNotIn("hunter2", blob)
        self.assertIn("[removed]", blob)

    def test_audit_log_also_has_no_leak(self):
        m = self.mission([step("a", "t_leak", {})])
        self.run_m(m["id"])
        audit = (self.profile / "audit.jsonl").read_text()
        self.assertNotIn(self.KEY, audit)
        self.assertNotIn("SCREEN-CONTENT", audit)

    def test_history_is_inspectable_and_per_mission(self):
        a, b = self.mission([step("a", "t_ok", {})]), self.mission([step("a", "t_ok", {})])
        self.run_m(a["id"])
        self.run_m(b["id"])
        self.assertEqual({h["mission"] for h in self.store.history()}, {a["id"], b["id"]})
        self.assertEqual({h["mission"] for h in self.store.history(a["id"])}, {a["id"]})

    def test_delete_one_missions_history_and_all(self):
        a, b = self.mission([step("a", "t_ok", {})]), self.mission([step("a", "t_ok", {})])
        self.run_m(a["id"])
        self.run_m(b["id"])
        self.assertGreater(self.store.delete_history(a["id"]), 0)
        self.assertEqual(self.store.history(a["id"]), [])
        self.assertEqual(self.store.load(a["id"])["executions"], [])
        self.assertTrue(self.store.history(b["id"]))
        self.store.delete_history(None)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(self.store.load(b["id"])["executions"], [])

    def test_delete_mission_removes_file_and_flags(self):
        m = self.mission([step("a", "t_ok", {})])
        self.store.flag(m["id"], "cancel")
        self.assertTrue(self.store.delete(m["id"]))
        self.assertEqual(list(self.store.root.glob(f"{m['id']}*")), [])
        self.assertFalse(self.store.delete(m["id"]))

    def test_delete_all_wipes_missions_folder(self):
        self.mission([step("a", "t_ok", {})])
        self.assertEqual(self.store.delete_all(), 1)
        self.assertFalse(self.store.root.exists())

    def test_export_is_redacted(self):
        m = self.mission([step("a", "t_leak", {})])
        self.run_m(m["id"])
        out = json.dumps(self.store.export(m["id"]))
        self.assertNotIn(self.KEY, out)

    def test_previous_completion_is_distinct_from_a_new_execution(self):
        m = self.mission([step("a", "t_ok", {})])
        self.run_m(m["id"])
        self.run_m(m["id"])                       # read-only mission: a second FRESH execution
        m = self.store.load(m["id"])
        self.assertEqual([e["n"] for e in m["executions"]], [1, 2])
        self.assertNotEqual(m["executions"][0]["id"], m["executions"][1]["id"])
        self.assertEqual(self.calls["t_ok"], 2)
        self.assertIn("execution #2", M.summarize(m))

    def test_mission_files_never_hold_untrusted_tool_text_keys(self):
        m = self.mission([step("a", "t_leak", {})])
        self.run_m(m["id"])
        self.assertNotIn("untrusted_text", self.store._path(m["id"]).read_text())

    def test_no_secret_in_stored_context_note(self):
        d = M.normalize({"goal": "g", "steps": [step()]}, self.ex.specs)
        m = M.new_mission(d, "user", [{"text": "token " + self.KEY, "source": "you"}])
        self.store.save(m)
        self.assertNotIn(self.KEY, self.store._path(m["id"]).read_text())


if __name__ == "__main__":
    unittest.main()
