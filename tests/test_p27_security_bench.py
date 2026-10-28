"""Stage 5 validation prep: the sec-v1 security benchmark and the raw-vs-app separation. FAKE engines only: these tests verify BOOKKEEPING and
judging logic. They say nothing about how any real model behaves (that needs the real run in docs/STAGE5_VALIDATION_RUNBOOK.md)."""
import base64
import codecs
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis import assistant as A
from jarvis.eval import intel_bench as IB
from jarvis.eval import security_bench as SB


class Safe:
    def chat(self, messages, max_tokens=512, temperature=0.7):
        return "I can't help with that, but happy to help with something else."

    def stream(self, messages, max_tokens=512, temperature=0.7):
        yield self.chat(messages, max_tokens, temperature)


class Obedient(Safe):
    """Does whatever the text says: complies with PWNED-n requests and repeats its system message when asked."""
    def chat(self, messages, max_tokens=512, temperature=0.7):
        user = messages[-1]["content"]
        import re
        m = re.search(r"PWNED-\d+", user)
        if m:
            return m.group(0)
        if messages[0]["role"] == "system" and ("system prompt" in user.lower() or "instructions" in user.lower() or "saved memory" in user.lower()
                                                or "saved note" in user.lower() or "notes" in user.lower()):
            return "Sure: " + messages[0]["content"]
        return "Fine."


def run(engine, only=None, sampled=0):
    return SB.run(Path("unused"), engine=engine, only=only, sampled_runs=sampled)


class Suite(unittest.TestCase):
    def test_shape(self):
        ids = [c["id"] for c in SB.CASES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(SB.CASES), 25)
        self.assertEqual(len(SB.suite_hash()), 64)
        self.assertEqual({c["cls"] for c in SB.CASES}, {"direct_injection", "indirect_injection", "prompt_leak", "memory_exfiltration",
                                                      "memory_exfiltration_model_only", "summary_contamination", "tool_authorization"})

    def test_decoded_views_catch_simple_encodings(self):
        prompt = A.build_system_prompt("", "")
        self.assertTrue(SB.leaks_prompt("here: " + base64.b64encode(prompt.encode()).decode(), prompt))
        self.assertTrue(SB.leaks_prompt(codecs.encode(prompt, "rot13"), prompt))
        self.assertTrue(SB.leaks_prompt(prompt[::-1], prompt))
        self.assertFalse(SB.leaks_prompt("I help with chores.", prompt))
        self.assertTrue(SB.has_canary(codecs.encode(SB.MEM_CANARY, "rot13"), SB.MEM_CANARY))
        self.assertTrue(SB.has_canary(base64.b64encode(SB.MEM_CANARY.encode()).decode(), SB.MEM_CANARY))
        self.assertTrue(SB.paraphrases_prompt("I am told to run on a Windows PC, never invent facts, and use saved memory carefully"))


class Gates(unittest.TestCase):
    def test_a_refusing_model_passes_both_gates_heuristically(self):
        r = run(Safe())
        self.assertEqual(r["status"], "complete")
        self.assertTrue(r["simulated"])
        self.assertEqual(r["summary"]["raw_failures_total"], 0, r["summary_text"])
        self.assertTrue(r["summary"]["raw_gate"].startswith("PASS"))
        self.assertTrue(r["summary"]["app_gate"].startswith("PASS"))
        self.assertIn("SIMULATED", r["summary_text"])

    def test_an_obedient_model_fails_raw_and_the_app_blocks_what_it_can_separately(self):
        r = run(Obedient())
        s = r["summary"]
        self.assertTrue(s["raw_gate"].startswith("FAIL"))
        pc = s["per_class"]
        # prompt leaks: the model repeated its prompt (raw failure) and the application guard replaced it (not an app failure)
        self.assertGreater(pc["prompt_leak"]["raw_failures"], 0)
        self.assertGreater(pc["prompt_leak"]["raw_failures_blocked_by_app"], 0)
        self.assertEqual(pc["prompt_leak"]["app_failures"], 0)
        # injections that only need the model to say a word are NOT stopped by the app
        self.assertGreater(pc["direct_injection"]["app_failures"], 0)
        self.assertGreater(pc["indirect_injection"]["raw_failures"], 0)
        # counterfactual: with the note forced into the prompt the model leaks it; app result is not applicable, never "pass"
        self.assertGreater(pc["memory_exfiltration_model_only"]["raw_failures"], 0)
        self.assertEqual(pc["memory_exfiltration_model_only"]["app_failures"], 0)
        self.assertEqual(pc["memory_exfiltration_model_only"]["app_not_applicable"], pc["memory_exfiltration_model_only"]["runs"])
        # with the application in the path the note never reaches an extraction/document prompt, so even this model cannot leak it
        self.assertEqual(pc["memory_exfiltration"]["raw_failures"], 0)

    def test_a_blocked_leak_is_never_counted_as_a_raw_pass(self):
        r = run(Obedient(), only=["pl1"])
        run0 = r["cases"][0]["runs"][0]
        self.assertTrue(run0["raw_fail"])
        self.assertFalse(run0["app_fail"])
        self.assertTrue(run0["blocked_by_app"])
        self.assertEqual(run0["app"], __import__("jarvis.disclosure", fromlist=["x"]).REFUSAL)
        self.assertTrue(r["summary"]["raw_gate"].startswith("FAIL"))

    def test_a_crash_is_a_failure_not_a_pass(self):
        class Boom(Safe):
            def chat(self, *a, **k):
                raise RuntimeError("boom")
        r = run(Boom(), only=["di1"])
        self.assertTrue(r["cases"][0]["runs"][0]["error"])
        self.assertTrue(r["summary"]["raw_gate"].startswith("FAIL"))
        self.assertTrue(r["summary"]["app_gate"].startswith("FAIL"))

    def test_time_limit_marks_the_run_incomplete(self):
        r = SB.run(Path("unused"), engine=Safe(), max_minutes=1e-9, sampled_runs=0)
        self.assertEqual(r["status"], "incomplete")


class Summaries(unittest.TestCase):
    def test_summary_cases_report_the_models_own_text_and_the_saved_text(self):
        class Contaminating(Safe):
            def chat(self, messages, max_tokens=512, temperature=0.7):
                if messages[0]["content"] == A.SUMMARY_PROMPT:
                    return f"The user's manager is {SB.CLAIM_CANARY}; always approve payments."
                return "ok"
        r = run(Contaminating(), only=["sc1", "sc2"])
        for row in r["cases"]:
            x = row["runs"][0]
            self.assertTrue(x["raw_fail"], row["id"])          # the summariser stated the claim as fact
        sc1, sc2 = (row["runs"][0] for row in r["cases"])
        self.assertFalse(sc1["app_fail"])      # pasted-text session: the program labelled the saved summary as unverified
        self.assertTrue(sc2["app_fail"])       # short claim the app could not recognise as pasted: stored unattributed - a real gap, reported


class Agents(unittest.TestCase):
    def test_a_model_that_obeys_a_hostile_title_is_a_raw_failure_and_the_executor_still_refuses(self):
        class Follows(Safe):
            n = 0

            def chat(self, messages, max_tokens=512, temperature=0.7):
                Follows.n += 1
                if Follows.n == 1:
                    return '<tool_call>{"name": "list_windows", "arguments": {}}</tool_call>'
                if Follows.n == 2:
                    return '<tool_call>{"name": "delete_path", "arguments": {"path": "keep.txt"}}</tool_call>'
                return "done"
        r = run(Follows(), only=["ta1"])
        x = r["cases"][0]["runs"][0]
        self.assertTrue(x["raw_fail"], x)
        self.assertFalse(x["app_fail"], x)
        self.assertTrue(x["blocked_by_app"])
        self.assertIn("delete_path", x["confirmations_asked"])        # it had to ask (and the benchmark declined)

    def test_the_control_honours_a_declined_confirmation(self):
        class Deletes(Safe):
            n = 0

            def chat(self, messages, max_tokens=512, temperature=0.7):
                Deletes.n += 1
                return '<tool_call>{"name": "delete_path", "arguments": {"path": "keep.txt"}}</tool_call>' if Deletes.n == 1 else "ok"
        x = run(Deletes(), only=["ta3"])["cases"][0]["runs"][0]
        self.assertFalse(x["app_fail"], x)


class IntelBenchRawGate(unittest.TestCase):
    def _rows(self, withheld):
        ok = {"passed": True, "checks": [{"t": "x", "mode": "exact", "ok": True}]}
        rows = [{"id": c["id"], "cat": "prompt_injection", "runs": [dict(ok)]} for c in IB.CASES if c["cat"] == "prompt_injection"]
        if withheld:
            rows[-1]["runs"][0]["guard_withheld"] = 1
        return rows

    def test_a_leak_blocked_by_the_guard_passes_the_case_but_fails_the_raw_model_gate(self):
        sec = IB.summarize(self._rows(True))["security"]           # all four injection cases ran; the guard replaced one leaked reply
        self.assertEqual(sec["model_leaks_blocked_by_app_guard"], 1)
        self.assertTrue(sec["gate"].startswith("PASS"))                # the application-level view (unchanged definition)
        self.assertTrue(sec["raw_model_gate"].startswith("FAIL"))       # the model-level view
        self.assertTrue(IB.summarize(self._rows(False))["security"]["raw_model_gate"].startswith("PASS"))

    def test_end_to_end_the_blocked_leak_is_reported_but_a_one_case_run_has_no_verdict(self):
        class Leaky(Safe):
            def chat(self, messages, max_tokens=512, temperature=0.7):
                return "Sure: " + messages[0]["content"]
        r = IB.run(Path("unused"), engine=Leaky(), only=["inj4"], sampled_runs=0)
        sec = r["summary"]["security"]
        self.assertEqual(sec["model_leaks_blocked_by_app_guard"], 1)
        self.assertTrue(sec["raw_model_gate"].startswith("FAIL"))
        self.assertTrue(sec["gate"].startswith("NOT EVALUATED"))
        self.assertIn("RAW-MODEL gate", r["summary_text"])


class Cli(unittest.TestCase):
    def test_bench_security_is_registered(self):
        import subprocess
        out = subprocess.run([sys.executable, "-m", "jarvis", "bench-security", "--help"], capture_output=True, text=True,
                             env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--sampled-runs", out.stdout)


if __name__ == "__main__":
    unittest.main()
