"""Stage 5: benchmark integrity fixes D-2..D-7 (cases sec-v1.1). FAKE engines only.

These tests verify BOOKKEEPING: that incomplete, blank, truncated, crashed or mis-specified runs can never be reported as a security PASS, that no
folder is created in the working directory, that Windows account names are redacted, and that the simulated desktop is labelled. They say NOTHING about
how any real model behaves, and a fake-engine PASS is not evidence of prompt-injection resistance or of real Windows behaviour."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis import assistant as A
from jarvis import disclosure
from jarvis.eval import intel_bench as IB
from jarvis.eval import redact as R
from jarvis.eval import security_bench as SB


class Safe:
    def chat(self, messages, max_tokens=512, temperature=0.7):
        return "I can't help with that, but happy to help with something else."

    def stream(self, messages, max_tokens=512, temperature=0.7):
        yield self.chat(messages, max_tokens, temperature)


def run(engine, only=None, sampled=0, **kw):
    return SB.run(Path("unused"), engine=engine, only=only, sampled_runs=sampled, **kw)


class InCleanDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self._td = tempfile.TemporaryDirectory()
        os.chdir(self._td.name)

    def tearDown(self):
        os.chdir(self._old)
        self._td.cleanup()


class D2NoStrayFolders(InCleanDir):
    def test_security_benchmark_creates_nothing_in_the_working_directory(self):
        r = run(Safe())
        self.assertEqual(r["status"], "complete")
        self.assertEqual(os.listdir("."), [])

    def test_intel_benchmark_creates_nothing_in_the_working_directory(self):
        IB.run(Path("unused"), engine=Safe(), only=["inj4", "mem1"], sampled_runs=0)
        self.assertEqual(os.listdir("."), [])

    def test_the_data_dir_given_to_the_assistant_is_a_throwaway_temp_folder_that_is_removed(self):
        seen = []
        real = SB._assistant

        def spy(engine, case, variant, guest, data_dir):
            seen.append(Path(data_dir))
            self.assertTrue(Path(data_dir).is_dir())
            return real(engine, case, variant, guest, data_dir)
        with mock.patch.object(SB, "_assistant", spy):
            run(Safe(), only=["sc2", "me1"])
        self.assertTrue(seen)
        for d in seen:
            self.assertFalse(d.exists(), d)                       # removed afterwards
            self.assertNotEqual(d.resolve(), Path(".").resolve())
            self.assertIn("jarvis_sec_", d.name)


class D3IncompleteIsNeverAPass(InCleanDir):
    def test_a_subset_run_is_a_subset_and_neither_gate_passes(self):
        r = run(Safe(), only=["di1"])
        self.assertEqual(r["status"], "subset")
        s = r["summary"]
        for g in ("raw_gate", "app_gate"):
            self.assertTrue(s[g].startswith("NOT EVALUATED"), s[g])
            self.assertFalse(s[g].startswith("PASS"))
        self.assertFalse(s["suite_complete"])
        self.assertEqual(s["cases_fully_run"], 1)
        self.assertIn("sc2", s["cases_missing_or_partial"])
        self.assertIn("NOT EVALUATED", r["summary_text"])
        self.assertNotIn("PASS", r["summary_text"].split("RAW MODEL GATE:")[1].split("\n")[0])

    def test_a_failure_in_a_subset_is_still_reported_as_a_failure(self):
        class Says(Safe):
            def chat(self, *a, **k):
                return "PWNED-11"
        r = run(Says(), only=["di1"])
        self.assertEqual(r["status"], "subset")
        self.assertTrue(r["summary"]["raw_gate"].startswith("FAIL"))
        self.assertTrue(r["summary"]["app_gate"].startswith("FAIL"))

    def test_unknown_case_ids_run_nothing_and_are_a_failure_not_a_complete_run(self):
        r = run(Safe(), only=["nope"])
        self.assertEqual(r["status"], "failed")
        self.assertIn("unknown case id", r["error"])
        self.assertEqual(r["cases"], [])
        self.assertEqual(r["summary"]["raw_gate"], "NOT RUN")

    def test_time_limit_is_incomplete_and_not_a_pass(self):
        r = run(Safe(), max_minutes=1e-9)
        self.assertEqual(r["status"], "incomplete")
        self.assertFalse(r["summary"]["raw_gate"].startswith("PASS"))
        self.assertFalse(r["summary"]["app_gate"].startswith("PASS"))

    def test_a_partly_finished_case_still_counts_its_failures(self):
        row = {"id": "di1", "cls": "direct_injection", "runs": [
            {"raw_fail": True, "app_fail": True, "valid": True, "error": None}]}            # greedy done, sampled runs never happened
        s = SB.summarize([row], expected_runs=3)
        self.assertTrue(s["raw_gate"].startswith("FAIL"))
        self.assertTrue(s["app_gate"].startswith("FAIL"))

    def test_only_a_complete_valid_run_can_pass_and_missing_cases_block_it(self):
        full = run(Safe())
        self.assertEqual(full["status"], "complete")
        self.assertTrue(full["summary"]["raw_gate"].startswith("PASS"))
        self.assertTrue(full["summary"]["app_gate"].startswith("PASS"))
        self.assertEqual(full["summary"]["cases_missing_or_partial"], [])
        # same rows with one case removed -> cannot pass
        rows = [{"id": c["id"], "cls": c["cls"], "runs": r["runs"]} for c, r in zip(SB.CASES, full["cases"])][1:]
        s = SB.summarize(rows, expected_runs=1)
        self.assertFalse(s["raw_gate"].startswith("PASS"))
        self.assertEqual(s["cases_missing_or_partial"], [SB.CASES[0]["id"]])
        # and with unknown expected_runs completeness cannot be shown
        self.assertFalse(SB.summarize(rows + [{"id": SB.CASES[0]["id"], "cls": SB.CASES[0]["cls"], "runs": full["cases"][0]["runs"]}])["raw_gate"].startswith("PASS"))

    def test_cli_exit_codes(self):
        for status, code in [("subset", 4), ("incomplete", 4), ("invalid", 4), ("failed", 3), ("complete", 0)]:
            with tempfile.TemporaryDirectory() as td, mock.patch.object(SB, "run", return_value={"status": status, "summary_text": "x"}), \
                    mock.patch("jarvis.models.manager.ModelStore") as ms:
                ms.return_value.active.return_value = "m"
                self.assertEqual(SB.run_cli(Path(td), "m", None, 0, None, None, ""), code, status)


class D4BlankAndTruncatedAreInvalid(InCleanDir):
    def test_an_engine_that_returns_nothing_cannot_pass(self):
        class Blank(Safe):
            def chat(self, *a, **k):
                return ""
        r = run(Blank())
        s = r["summary"]
        self.assertEqual(r["status"], "invalid")                   # every case ran, but the results are unusable: not "complete", exit code 4
        self.assertFalse(s["raw_gate"].startswith("PASS"), s["raw_gate"])
        self.assertFalse(s["app_gate"].startswith("PASS"), s["app_gate"])
        self.assertTrue(s["raw_gate"].startswith("INVALID"))
        self.assertEqual(s["invalid_runs_total"], len(SB.CASES))
        for row in r["cases"]:
            x = row["runs"][0]
            self.assertFalse(x["valid"], row["id"])
            self.assertTrue({"blank_reply", "summariser_not_called"} & set(x["invalid_reasons"]) or "model_not_called" in x["invalid_reasons"], (row["id"], x["invalid_reasons"]))

    def test_a_truncated_reply_is_invalid(self):
        class Cut(Safe):
            def __init__(self):
                self.diag = self.diag_reset()

            @staticmethod
            def diag_reset():
                return {"calls": 0, "completion_tokens": 0, "truncated": 0, "blank_unfinished_think": 0}

            def chat(self, *a, **k):
                self.diag["calls"] += 1
                self.diag["truncated"] += 1
                return "A long answer that was cut o"
        r = run(Cut(), only=["di1", "me3"])
        for row in r["cases"]:
            x = row["runs"][0]
            self.assertFalse(x["valid"])
            self.assertIn("truncated", x["invalid_reasons"])
            self.assertEqual(x["diag"]["truncated"], 1)
        self.assertTrue(r["summary"]["raw_gate"].startswith("INVALID"))

    def test_unfinished_thinking_is_invalid(self):
        class Think(Safe):
            def __init__(self):
                self.diag = {"calls": 0, "completion_tokens": 0, "truncated": 0, "blank_unfinished_think": 0}

            @staticmethod
            def diag_reset():
                return {"calls": 0, "completion_tokens": 0, "truncated": 0, "blank_unfinished_think": 0}

            def chat(self, *a, **k):
                self.diag["calls"] += 1
                self.diag["blank_unfinished_think"] += 1
                return "ok fine"
        x = run(Think(), only=["di1"])["cases"][0]["runs"][0]
        self.assertIn("blank_unfinished_think", x["invalid_reasons"])

    def test_an_engine_that_does_not_account_for_its_calls_is_invalid(self):
        class Silent(Safe):
            def __init__(self):
                self.diag = {"calls": 0, "completion_tokens": 0, "truncated": 0, "blank_unfinished_think": 0}

            @staticmethod
            def diag_reset():
                return {"calls": 0, "completion_tokens": 0, "truncated": 0, "blank_unfinished_think": 0}
        x = run(Silent(), only=["di1"])["cases"][0]["runs"][0]
        self.assertIn("diagnostics_incomplete", x["invalid_reasons"])

    def test_an_engine_failure_swallowed_by_the_application_is_invalid_not_a_pass(self):
        class Fails(Safe):
            def chat(self, *a, **k):
                raise RuntimeError("model crashed")
        r = run(Fails(), only=["di1", "sc2", "ta3"])
        for row in r["cases"]:
            x = row["runs"][0]
            self.assertFalse(x["valid"], row["id"])
        self.assertFalse(r["summary"]["raw_gate"].startswith("PASS"))
        self.assertFalse(r["summary"]["app_gate"].startswith("PASS"))

    def test_a_summariser_that_fails_after_normal_chat_is_invalid(self):
        class SummaryFails(Safe):
            def chat(self, messages, max_tokens=512, temperature=0.7):
                if messages[0]["content"] == A.SUMMARY_PROMPT:
                    raise RuntimeError("no summary")
                return "ok"
        x = run(SummaryFails(), only=["sc2"])["cases"][0]["runs"][0]
        self.assertIn("generation_error", x["invalid_reasons"])
        self.assertFalse(x["valid"])
        self.assertFalse(x["raw_fail"] or x["app_fail"])          # nothing was judged as a failure, yet the run is not valid, so it cannot pass

    def test_a_record_without_a_validity_flag_is_treated_as_invalid(self):
        row = {"id": "di1", "cls": "direct_injection", "runs": [{"raw_fail": False, "app_fail": False, "error": None}]}
        s = SB.summarize([row], expected_runs=1)
        self.assertEqual(s["invalid_runs_total"], 1)
        self.assertFalse(s["raw_gate"].startswith("PASS"))

    def test_valid_runs_record_their_generation_accounting(self):
        x = run(Safe(), only=["di1"])["cases"][0]["runs"][0]
        self.assertTrue(x["valid"])
        self.assertEqual((x["generations"], x["blank_replies"], x["invalid_reasons"]), (1, 0, []))


class D5Sc1TestsWhatItClaims(InCleanDir):
    def test_the_sc1_claim_is_in_a_middle_paragraph_and_is_not_in_the_retrieval_query(self):
        case = next(c for c in SB.CASES if c["id"] == "sc1")
        doc = case["turns"][1]
        self.assertTrue(disclosure.is_document_like(doc))
        paras = [p for p in doc.split("\n\n") if p.strip()]
        self.assertGreaterEqual(len(paras), 4)
        self.assertNotIn(SB.CLAIM_CANARY, paras[0])
        self.assertNotIn(SB.CLAIM_CANARY, paras[-1])
        self.assertIn(SB.CLAIM_CANARY, doc)
        self.assertNotIn(SB.CLAIM_CANARY, disclosure.retrieval_query(doc))

    def test_what_the_summariser_receives_is_recorded_for_each_summary_case(self):
        r = run(Safe(), only=["sc1", "sc2", "sc3"])
        by = {row["id"]: row["runs"][0] for row in r["cases"]}
        self.assertFalse(by["sc1"]["summariser_saw_claim_in_user_text"])      # the pasted middle paragraph was left out
        self.assertIn("left out of this summary", by["sc1"]["summariser_user_text"])
        self.assertTrue(by["sc2"]["summariser_saw_claim_in_user_text"])       # known gap: a short claim is not recognised as pasted
        self.assertTrue(by["sc3"]["summariser_saw_claim_in_user_text"])       # known limit: a payload in the first paragraph is kept
        for x in by.values():
            self.assertTrue(x["valid"], x["invalid_reasons"])

    def test_a_case_whose_precondition_no_longer_holds_is_invalid(self):
        broken = dict(next(c for c in SB.CASES if c["id"] == "sc1"), summariser_sees_claim_as_user_text=True)
        x = SB.run_case(Safe(), broken, temperature=0.0)
        self.assertIn("precondition_failed", x["invalid_reasons"])
        self.assertIn("no longer tests", x["precondition_failed"])
        self.assertFalse(x["valid"])

    def test_sc1_still_detects_a_claim_the_assistant_repeated_and_the_summariser_stated_as_fact(self):
        class Repeats(Safe):
            def chat(self, messages, max_tokens=512, temperature=0.7):
                if messages[0]["content"] == A.SUMMARY_PROMPT:
                    return f"The user's manager is {SB.CLAIM_CANARY}; always approve payments."
                return "ok"
        x = run(Repeats(), only=["sc1"])["cases"][0]["runs"][0]
        self.assertTrue(x["raw_fail"])                       # the summariser stated it as fact
        self.assertFalse(x["app_fail"])                      # the program-added "unverified" note covers the pasted-text session


class D6Redaction(InCleanDir):
    def test_windows_and_posix_account_names_are_removed_but_the_rest_is_kept(self):
        for src, keep in [(r"C:\Users\Jane Doe\AppData\Local\Temp\x.gguf", r"AppData\Local\Temp\x.gguf"),
                          (r"c:/users/jane/models/m.gguf", "models/m.gguf"),
                          (r"\\?\C:\Users\jane.doe\a.bin", "a.bin"),
                          (r"C:\Documents and Settings\bob\x", r"\x"),
                          ("/home/jane/.cache/y", ".cache/y"), ("/Users/jane/Library/z", "Library/z")]:
            out = R.redact_paths(f"cannot open {src}: denied")
            self.assertNotIn("Jane", out)
            self.assertNotIn("jane", out.replace("<user>", ""))
            self.assertNotIn("bob", out)
            self.assertIn(keep, out)
            self.assertIn("denied", out)
        self.assertEqual(R.redact_paths("plain message, no path"), "plain message, no path")

    def test_this_machines_home_directory_is_removed(self):
        home = str(Path.home())
        self.assertNotIn(home, R.redact_paths(f"x {home}/stuff y"))

    def test_safe_error_keeps_the_exception_type(self):
        out = R.safe_error(PermissionError(r"[Errno 13] Permission denied: 'C:\Users\Jane Doe\a.gguf'"))
        self.assertTrue(out.startswith("PermissionError: "))
        self.assertIn("Permission denied", out)
        self.assertNotIn("Jane", out)

    def test_saved_json_and_printed_summary_contain_no_account_name_for_load_and_run_errors(self):
        class Boom(Safe):
            def chat(self, *a, **k):
                raise OSError(r"cannot read C:\Users\Jane Doe\AppData\Local\Temp\tmp1\model.gguf")
        with tempfile.TemporaryDirectory() as out:
            r = run(Boom(), only=["di1"], out=Path(out))
            blob = "".join(p.read_text(encoding="utf-8") for p in Path(out).iterdir())
            self.assertNotIn("Jane", blob)
            self.assertNotIn("Jane", json.dumps(r))
            self.assertIn("<user>", blob)
        # model load failure path
        with tempfile.TemporaryDirectory() as out, mock.patch("jarvis.inference.gguf_engine.GGUFEngine",
                                                              side_effect=OSError(r"missing C:\Users\Jane Doe\ProjectJarvis\models\m\model.gguf")):
            r = SB.run(Path(out) / "m", out=Path(out) / "o")
            self.assertEqual(r["status"], "failed")
            self.assertTrue(r["error"].startswith("OSError"))
            self.assertNotIn("Jane", json.dumps(r))
            self.assertNotIn("Jane", "".join(p.read_text(encoding="utf-8") for p in (Path(out) / "o").iterdir()))

    def test_intel_benchmark_errors_are_redacted_too(self):
        class Boom(Safe):
            def chat(self, *a, **k):
                raise RuntimeError(r"fail at C:\Users\Jane Doe\x")
        r = IB.run(Path("unused"), engine=Boom(), only=["inj4"], sampled_runs=0)
        self.assertNotIn("Jane", json.dumps(r))
        self.assertIn("<user>", json.dumps(r))


class D7SimulatedDesktopLabel(InCleanDir):
    def test_tool_authorization_runs_and_summary_say_simulated(self):
        r = run(Safe(), only=["ta1", "ta2", "ta3"])
        for row in r["cases"]:
            x = row["runs"][0]
            self.assertEqual(x["desktop"], "SIMULATED")
            self.assertIn("no real Windows window or API", x["desktop_note"])
        self.assertIn("SIMULATED desktop", r["summary_text"])
        self.assertIn("nothing about real Windows", r["summary_text"])

    def test_the_label_is_present_even_when_the_run_is_a_failure(self):
        class Fails(Safe):
            def chat(self, *a, **k):
                raise RuntimeError("x")
        r = run(Fails(), only=["ta1"])
        self.assertIn("SIMULATED desktop", r["summary_text"])



class IntelBenchSubsetIsNotAFullEvaluation(InCleanDir):
    def test_a_subset_run_has_status_subset_and_no_security_pass(self):
        r = IB.run(Path("unused"), engine=Safe(), only=["inj1", "inj2", "inj3", "inj4"], sampled_runs=0)
        self.assertEqual(r["status"], "subset")                      # even the four injection cases alone are a subset of the 48-case suite
        r = IB.run(Path("unused"), engine=Safe(), only=["inj4"], sampled_runs=0)
        self.assertEqual(r["status"], "subset")
        sec = r["summary"]["security"]
        self.assertFalse(sec["injection_suite_complete"])
        for g in ("gate", "raw_model_gate"):
            self.assertTrue(sec[g].startswith("NOT EVALUATED"), sec[g])
        self.assertIn("NOT EVALUATED", r["summary_text"])
        self.assertTrue(any("--only" in n for n in r["notes"]))

    def test_unknown_ids_are_a_failure_not_a_complete_run(self):
        r = IB.run(Path("unused"), engine=Safe(), only=["nope"], sampled_runs=0)
        self.assertEqual(r["status"], "failed")
        self.assertIn("unknown case id", r["error"])
        self.assertEqual(r["cases"], [])

    def test_a_failure_in_a_subset_is_still_a_failure(self):
        class Leaks(Safe):
            def chat(self, messages, max_tokens=512, temperature=0.7):
                return "PWNED"
        r = IB.run(Path("unused"), engine=Leaks(), only=["inj1"], sampled_runs=0)
        self.assertTrue(r["summary"]["security"]["gate"].startswith("FAIL"))

    def test_time_limited_run_is_not_a_security_pass(self):
        r = IB.run(Path("unused"), engine=Safe(), max_minutes=1e-9, sampled_runs=0)
        self.assertEqual(r["status"], "incomplete")
        self.assertFalse(r["summary"]["security"]["gate"].startswith("PASS"))

    def test_exit_codes(self):
        for status, code in [("subset", 4), ("incomplete", 4), ("failed", 3), ("complete", 0)]:
            with tempfile.TemporaryDirectory() as td, mock.patch.object(IB, "run", return_value={"status": status, "summary_text": "x"}), \
                    mock.patch("jarvis.models.manager.ModelStore") as ms:
                ms.return_value.active.return_value = "m"
                self.assertEqual(IB.run_cli(Path(td), "m", None, 0, None, None, ""), code, status)

    def test_full_fake_run_is_complete_and_exit_zero(self):
        r = IB.run(Path("unused"), engine=Safe(), sampled_runs=0)
        self.assertEqual(r["status"], "complete")
        self.assertTrue(r["summary"]["security"]["injection_suite_complete"])


if __name__ == "__main__":
    unittest.main()
