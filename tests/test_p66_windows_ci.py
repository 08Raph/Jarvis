"""The Windows CI additions: scripts/ci_summarize_unittest.py, scripts/ci_check_selftest_report.py and .github/workflows/windows-ci.yml.

WHAT THESE TESTS ARE: they check the scripts' logic and the workflow file's SHAPE (triggers, permissions, timeouts, no secrets, no scheduled runs, the model
download only behind its consent value, the files it calls exist). GitHub Actions and Windows are not available where they run: they do NOT prove the workflow
runs, that its PowerShell is valid, or that anything passes on a Windows runner. Only a real run on GitHub can show that."""
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_check_selftest_report as J  # noqa: E402
import ci_summarize_unittest as S  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "windows-ci.yml"
CONSENT = "yes-download-qwen3-1.7b-q4_k_m"

OK_LOG = "....\n----------------------------------------------------------------------\nRan 2843 tests in 194.507s\n\nOK (skipped=6)\n"
FAILED_LOG = ("FAIL: test_a (m.T.test_a)\nTraceback...\n\n======\nERROR: test_b (m.T.test_b)\nboom\n\n"
              "----------------------------------------------------------------------\nRan 100 tests in 3.2s\n\nFAILED (failures=1, errors=2, skipped=3)\n")


class Summarizer(unittest.TestCase):
    def test_counts_come_from_unittests_own_lines(self):
        r = S.parse(OK_LOG)
        self.assertEqual((r["label"], r["ran"], r["passed"], r["skipped"], r["failed"], r["errors"]), ("PASS", 2843, 2837, 6, 0, 0))

    def test_failures_and_errors_are_never_counted_as_passed(self):
        r = S.parse(FAILED_LOG)
        self.assertEqual((r["label"], r["ran"], r["failed"], r["errors"], r["skipped"], r["passed"]), ("FAIL", 100, 1, 2, 3, 94))
        self.assertEqual(r["failing"], ["FAIL: test_a (m.T.test_a)", "ERROR: test_b (m.T.test_b)"])

    def test_a_log_without_a_result_is_not_run_not_pass(self):
        for text in ("", "Traceback (most recent call last):\n", "Ran 5 tests in 1.0s\n"):
            with self.subTest(text=text[:20]):
                self.assertEqual(S.parse(text)["label"], "NOT RUN")

    def test_zero_tests_is_not_a_pass(self):
        self.assertEqual(S.parse("Ran 0 tests in 0.000s\n\nNO TESTS RAN\n")["label"], "NOT RUN")

    def test_windows_powershell_utf16_logs_with_crlf_are_read(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        f = tmp / "u.log"
        f.write_bytes(OK_LOG.replace("\n", "\r\n").encode("utf-16"))
        self.assertEqual(S.parse(S.read_text(f))["ran"], 2843)

    def test_cli_exit_codes_json_and_summary(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for text, code in ((OK_LOG, 0), (FAILED_LOG, 1), ("junk", 2)):
            log = tmp / "x.log"
            log.write_text(text, encoding="utf-8")
            summ = tmp / "s.md"
            with mock.patch("sys.stdout", new=io.StringIO()):
                rc = S.main([str(log), "--json", str(tmp / "o.json"), "--summary", str(summ), "--title", "T"])
            self.assertEqual(rc, code)
            self.assertIn(("PASS", "FAIL", "NOT RUN")[code], summ.read_text(encoding="utf-8"))
            self.assertIn("label", json.loads((tmp / "o.json").read_text(encoding="utf-8")))
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(S.main([str(tmp / "missing.log")]), 2)


def passing_report(**over):
    r = {"schema": 2, "run_id": "0123456789ab", "status": "passed", "passed": True, "exit_code": 0, "failure": None, "model_id": "m1", "model_sha256": "ab" * 32,
         "answer": "The answer is 4.", "answer_mentions_4": True, "answer_check": {"name": "arith-2plus2-v1", "ok": True, "reason": "ok"},
         "runtime": {"available": True, "version": "0.3.36"}, "load_seconds": 3.2, "generation_seconds": 1.1, "output_tokens": 6, "tokens_per_second": 5.4,
         "stages": [{"stage": n, "status": "running"} for n in ("started", "starting_worker", "runtime_check", "loading_model", "generating", "validating_output", "finished")]}
    r.update(over)
    return r


class ReportJudge(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def judge(self, rep, code=0, model="m1"):
        return J.judge(rep, code, model, self.d)[0]

    def test_a_consistent_real_pass_is_pass(self):
        self.assertEqual(self.judge(passing_report()), "PASS")

    def test_missing_model_and_missing_runtime_are_blocked_not_failed(self):
        self.assertEqual(self.judge(None, 2), "BLOCKED")
        self.assertEqual(self.judge({"status": "failed", "failure": {"kind": "runtime_unavailable", "message": "No module named llama_cpp"}}, 3), "BLOCKED")

    def test_every_other_failure_is_fail(self):
        for kind, status in (("load_failed", "failed"), ("generation_failed", "failed"), ("empty_output", "failed"), ("invalid_output", "failed"),
                             ("timeout", "timeout"), ("worker_crashed", "crashed"), ("worker_no_result", "crashed"), ("worker_start_failed", "failed"), ("cancelled", "cancelled")):
            with self.subTest(kind=kind):
                rep = {"status": status, "failure": {"kind": kind, "stage": "loading_model", "message": "x"}, "worker_exit": "Windows status 0xC0000005 (access violation)"}
                self.assertEqual(self.judge(rep, 3), "FAIL")

    def test_a_pass_whose_report_contradicts_itself_is_fail(self):
        bad = {"exit code": (passing_report(), 3), "answer check": (passing_report(answer_check={"ok": False}), 0),
               "no 4": (passing_report(answer_mentions_4=False), 0), "model id": (passing_report(model_id="other"), 0),
               "runtime": (passing_report(runtime={"available": False}), 0), "zero load": (passing_report(load_seconds=0), 0),
               "no tokens": (passing_report(output_tokens=0), 0), "worker_exit present": (passing_report(worker_exit="exit code 0"), 0),
               "stage missing": (passing_report(stages=[{"stage": "started"}]), 0), "schema": (passing_report(schema=1), 0),
               "failure set": (passing_report(failure={"kind": "x"}), 0), "empty answer": (passing_report(answer=" "), 0)}
        for name, (rep, code) in bad.items():
            with self.subTest(name):
                self.assertEqual(self.judge(rep, code), "FAIL")

    def test_leftover_transient_files_fail_a_pass(self):
        (self.d / "selftest.worker.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.judge(passing_report()), "FAIL")

    def test_the_judge_accepts_a_report_made_by_the_real_supervisor(self):
        """Feed it the report the REAL supervisor writes (fake runtime, real child process): the field names and shapes must line up."""
        from test_local_model import make_gguf
        from test_p63_selftest_hardening import HELPER
        from jarvis import selftest_core as core
        from jarvis.config import Config
        from jarvis.models.manager import ModelStore
        cfg = Config(data_dir=self.d / "data")
        cfg.data_dir.mkdir()
        store = ModelStore(cfg.models_dir)
        store.install_gguf(make_gguf(self.d / "m.gguf"), "fake", "Apache-2.0", "t", extra_meta=None)
        store.set_active("fake")

        def cmd(c, run_id, model_id):
            return [sys.executable, str(HELPER), "slow_load", "--data-dir", str(c.data_dir), "selftest-worker", "--run-id", run_id, "--model-id", model_id]
        with mock.patch.object(core, "worker_command", cmd):
            res = core.run_selftest(cfg, store, "fake")
        self.assertEqual(res["exit_code"], 0, res)
        self.assertEqual(J.judge(json.loads((cfg.data_dir / "selftest.json").read_text(encoding="utf-8")), 0, "fake", cfg.data_dir)[0], "PASS")

    def test_cli_exit_codes(self):
        (self.d / "selftest.json").write_text(json.dumps(passing_report()), encoding="utf-8")
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(J.main(["--data-dir", str(self.d), "--exit-code", "0", "--model-id", "m1"]), 0)
            self.assertEqual(J.main(["--data-dir", str(self.d), "--exit-code", "0", "--model-id", "zzz"]), 1)
            self.assertEqual(J.main(["--data-dir", str(self.d / "nowhere"), "--exit-code", "2", "--model-id", "m1"]), 3)


class WorkflowShape(unittest.TestCase):
    text = WORKFLOW.read_text(encoding="utf-8")

    def test_text_level_safety_rules(self):
        t = self.text
        self.assertNotIn("secrets.", t)
        self.assertNotIn("schedule:", t)
        self.assertNotIn("pull_request_target", t)
        self.assertNotIn("self-hosted", t)
        self.assertNotRegex(t, r"gh release|action-gh-release|softprops|actions/create-release")
        self.assertNotRegex(t, r"(?m)^\s+contents:\s*write")
        self.assertNotIn("-SkipLocalRuntime", t, "the CI build must contain the runtime")
        for m in re.findall(r"uses:\s*(\S+)", t):
            self.assertRegex(m, r"^actions/[a-z-]+@v\d+$", "only first-party, version-constrained actions")

    def test_a_model_is_downloaded_only_in_the_consented_step(self):
        t = self.text
        self.assertEqual(len(re.findall(r"models fetch", t)), 1)
        step = t[t.index("id: real"):t.index("- name: Cleanup")]
        self.assertIn("models fetch", step)
        self.assertIn(CONSENT, step)
        self.assertIn("github.event_name == 'workflow_dispatch'", step)
        self.assertGreater(t.index("models fetch"), t.index("id: real"))

    def test_every_script_the_workflow_calls_exists(self):
        for name in set(re.findall(r"scripts[\\/]([A-Za-z0-9_]+\.(?:py|ps1))", self.text)):
            self.assertTrue((ROOT / "scripts" / name).is_file(), name)

    def test_nothing_is_uploaded_cached_or_staged_as_a_workflow_artifact(self):
        """The account's Actions artifact storage may be full: no artifact upload, no cache, no staging for upload. Output stays in the run log and summary."""
        t = self.text
        self.assertEqual(sorted(set(re.findall(r"uses:\s*(\S+)", t))), ["actions/checkout@v4", "actions/setup-python@v5"])
        for needle in ("upload-artifact", "download-artifact", "actions/cache", "retention-days", "ci_stage_upload", "upload-staging", "cache:", "cache-dependency-path"):
            self.assertNotIn(needle, t, needle)

    def test_results_stay_visible_in_the_run_log_and_summary(self):
        t = self.text
        self.assertEqual(t.count("scripts/ci_run_tests.py") + t.count("scripts\\ci_run_tests.py"), 2)   # Linux and Windows both go through the hard-timeout runner
        self.assertGreaterEqual(t.count("GITHUB_STEP_SUMMARY"), 5)
        self.assertIn("ci-logs/unittest-linux.log", t)
        self.assertIn("ci-logs\\unittest-windows.log", t)
        self.assertIn("PASS / FAIL / NOT RUN / PARTIAL", t)
        self.assertIn("ci_check_selftest_report.py", t)                          # PASS / FAIL / BLOCKED for the optional real-model step
        self.assertIn("Final gate", t)

    @unittest.skipUnless(__import__("importlib").util.find_spec("yaml"), "PyYAML is not installed")
    def test_runner_limits_nest_inside_step_and_job_limits_and_failures_stay_visible(self):
        import yaml
        d = yaml.safe_load(self.text)
        for job, step_id, name in (("linux-tests", None, "Full unit suite"), ("windows-package", "unit", None)):
            steps = d["jobs"][job]["steps"]
            st = next(x for x in steps if (x.get("id") == step_id if step_id else x.get("name") == name))
            m = re.search(r"--timeout (\d+)", st["run"])
            self.assertTrue(m, job)
            self.assertLess(int(m.group(1)), st["timeout-minutes"] * 60, job)          # the runner times out (and reports) before GitHub kills the step
            self.assertLessEqual(st["timeout-minutes"], d["jobs"][job]["timeout-minutes"])
            self.assertNotIn("|| true", st["run"])
            self.assertNotIn("set +e", st["run"])
        win = d["jobs"]["windows-package"]["steps"]
        self.assertIn("$LASTEXITCODE", next(x for x in win if x.get("id") == "unit")["run"])
        final = win[-1]
        self.assertIn("steps.unit.outcome == 'failure'", final["if"])
        # the masked steps must state their verdict in an annotation, not only in a (masked) conclusion
        self.assertIn("Packaged worker gate: PASS", next(x for x in win if x.get("id") == "gate")["run"])
        self.assertIn("Silent install check: PASS", next(x for x in win if x.get("id") == "install")["run"])
        linux = d["jobs"]["linux-tests"]["steps"]
        self.assertEqual(linux[-1].get("if"), "always()")

    @unittest.skipUnless(__import__("importlib").util.find_spec("yaml"), "PyYAML is not installed")
    def test_parsed_structure(self):
        import yaml
        d = yaml.safe_load(self.text)
        on = d.get(True) or d.get("on")
        self.assertEqual(set(on), {"pull_request", "push", "workflow_dispatch"})
        self.assertEqual(on["push"]["branches"], ["main"])
        self.assertEqual(on["pull_request"]["paths"], on["push"]["paths"])
        self.assertEqual(d["permissions"], {"contents": "read"})
        self.assertEqual(set(d["jobs"]), {"linux-tests", "windows-package", "vosk-probe"})
        for job in d["jobs"].values():
            for step in job["steps"]:
                self.assertTrue(set(step.get("uses", "x")) and "upload" not in str(step.get("uses", "")) and "cache" not in str(step.get("uses", "")))
                self.assertNotIn("cache", step.get("with", {}))
        for name, job in d["jobs"].items():
            self.assertIn("timeout-minutes", job, name)
            self.assertIn(job["runs-on"], ("ubuntu-24.04", "windows-2022"))
            for step in job["steps"]:
                if step.get("id") in ("build", "unit", "gate", "install", "real"):
                    self.assertIn("timeout-minutes", step, step.get("id"))
        self.assertIn("real_inference_consent", on["workflow_dispatch"]["inputs"])
        self.assertEqual(on["workflow_dispatch"]["inputs"]["real_inference_consent"]["default"], "")
        self.assertTrue(d["concurrency"]["cancel-in-progress"])


if __name__ == "__main__":
    unittest.main()
