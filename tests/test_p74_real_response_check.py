"""scripts/ci_real_response_check.py: the acceptance check for 'the current application answered from a GENUINE model'.

Everything here is FAKE-backed (a synthetic GGUF header and a stand-in application script). These tests show the CHECKER's logic, including that it rejects
the project's own synthetic fixture and fake applications; they say nothing about real inference. The one PASS path needs deliberately lowered thresholds and a
--allow-test-command flag, both of which the real CI step never uses."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
from p41_common import build_gguf  # noqa: E402

_spec = importlib.util.spec_from_file_location("ci_real_response_check", HERE.parent / "scripts" / "ci_real_response_check.py")
CHK = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CHK)

MID = "test-model"                    # NOT in the catalogue, so the size-against-catalogue check is skipped here; it has its own test below
CAT = "qwen3-1.7b-q4_k_m"
APP = """import sys
args = sys.argv[1:]
sys.stderr.write("[local model: %(mid)s; everything runs on this PC]\\n")
%(body)s
"""


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name) / "data"

    def model(self, **kw):
        p = self.d / "models" / MID / "model.gguf"
        p.parent.mkdir(parents=True)
        build_gguf(p, **kw)
        return p

    def app(self, body):
        p = Path(self.tmp.name) / "fake_app.py"
        p.write_text(APP % {"mid": MID, "body": body}, encoding="utf-8")
        return p

    def run_check(self, app=None, lowered=False, extra=()):
        argv = ["--data-dir", str(self.d), "--model-id", MID, "--timeout", "30"]
        if app is not None:
            argv += ["--cmd", f"{sys.executable} {app}", "--allow-test-command"]
        if lowered:
            argv += ["--min-tensors", "10", "--min-bytes", "1000"]
        out = Path(self.tmp.name) / "r.json"
        argv += ["--json", str(out), *extra]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = CHK.main(argv)
        rep = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        return rc, buf.getvalue(), rep


class Rejects(Case):
    def test_the_projects_own_synthetic_fixture_is_rejected_by_the_default_thresholds(self):
        self.model()                                                       # 1 tensor, 52 MB, "Synthetic 1B": exactly the fixture other tests use
        app = self.app('print("The capital of France is Paris.")')
        rc, text, rep = self.run_check(app)
        self.assertEqual(rc, 1, text)
        for needle in ("tensor_count", "synthetic"):
            self.assertIn(needle, rep["why"].lower())

    def test_a_zero_tensor_header_only_file_is_rejected(self):
        self.model(tensors=0, size=3000)
        rc, text, rep = self.run_check(self.app('print("Paris")'), lowered=True)
        self.assertEqual(rc, 1, text)
        self.assertIn("tensor_count", rep["why"])

    def test_a_missing_model_is_blocked_not_failed_and_not_passed(self):
        rc, text, _ = self.run_check(self.app('print("Paris")'))
        self.assertEqual(rc, 3, text)
        self.assertTrue(text.startswith("BLOCKED"))

    def test_this_python_interpreter_is_not_accepted_as_the_application(self):
        self.model(tensors=100, size=2000, name="Qwen3 1.7B")
        argv = ["--data-dir", str(self.d), "--model-id", MID, "--cmd", sys.executable, "--min-tensors", "10", "--min-bytes", "1000"]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = CHK.main(argv)
        self.assertEqual(rc, 1)
        self.assertIn("stand-in application", buf.getvalue())

    def test_a_synthetic_name_is_rejected_even_when_size_and_tensor_thresholds_are_met(self):
        self.model(tensors=100, size=2000, name="Synthetic 1B")
        rc, text, rep = self.run_check(self.app('print("Paris")'), lowered=True)
        self.assertEqual(rc, 1, text)
        self.assertIn("synthetic", rep["why"].lower())

    def test_a_small_file_is_rejected_on_size_alone(self):
        self.model(tensors=100, size=2000, name="Qwen3 1.7B")              # enough tensors, genuine-looking name, but only a few KB
        rc, text, rep = self.run_check(self.app('print("Paris")'), extra=["--min-tensors", "10", "--min-bytes", "5000"])
        self.assertEqual(rc, 1, text)
        self.assertIn("bytes, below", rep["why"])

    def test_a_pinned_sha256_that_does_not_match_is_rejected_and_one_that_matches_passes(self):
        p = self.model(tensors=100, size=2000, name="Qwen3 1.7B")
        import hashlib
        good = hashlib.sha256(p.read_bytes()).hexdigest()
        app = self.app('print("Paris")')
        rc, text, rep = self.run_check(app, lowered=True, extra=["--expect-sha256", "0" * 64])
        self.assertEqual(rc, 1, text)
        self.assertIn("not the expected", rep["why"])
        rc, text, rep = self.run_check(app, lowered=True, extra=["--expect-sha256", good.upper()])
        self.assertEqual(rc, 0, text)

    def test_exact_size_pin(self):
        self.model(tensors=100, size=2000, name="Qwen3 1.7B")
        size = (self.d / "models" / MID / "model.gguf").stat().st_size
        app = self.app('print("Paris")')
        rc, text, rep = self.run_check(app, lowered=True, extra=["--expect-bytes", str(size + 1)])
        self.assertEqual(rc, 1, text)
        self.assertIn("not the expected", rep["why"])
        rc, text, rep = self.run_check(app, lowered=True, extra=["--expect-bytes", str(size)])
        self.assertEqual(rc, 0, text)

    def test_runtime_version_must_match_what_the_selftest_recorded_and_the_commit_is_recorded(self):
        self.model(tensors=100, size=2000, name="Qwen3 1.7B")
        app = self.app('print("Paris")')
        rc, text, rep = self.run_check(app, lowered=True, extra=["--expect-runtime", "0.3.36"])
        self.assertEqual(rc, 1, text)                                       # no selftest.json at all: the version cannot be read
        self.assertIn("runtime version", rep["why"])
        (self.d / "selftest.json").write_text(json.dumps({"runtime": {"available": True, "version": "0.3.35"}}), encoding="utf-8")
        rc, text, rep = self.run_check(app, lowered=True, extra=["--expect-runtime", "0.3.36"])
        self.assertEqual(rc, 1, text)
        self.assertIn("0.3.35", rep["why"])
        (self.d / "selftest.json").write_text(json.dumps({"runtime": {"available": True, "version": "0.3.36"}}), encoding="utf-8")
        rc, text, rep = self.run_check(app, lowered=True, extra=["--expect-runtime", "0.3.36", "--commit", "abc1234"])
        self.assertEqual(rc, 0, text)
        self.assertEqual((rep["runtime_version"], rep["commit"]), ("0.3.36", "abc1234"))
        self.assertIn("runtime 0.3.36", rep["why"])

    def test_annotations_are_printed_for_ci_and_escape_newlines(self):
        self.model(tensors=100, size=2000, name="Qwen3 1.7B")
        (self.d / "selftest.json").write_text('{"runtime": {"available": true, "version": "0.3.36"}}\n', encoding="utf-8")
        rc, text, _ = self.run_check(self.app('print("Paris")'), lowered=True, extra=["--annotate"])
        self.assertEqual(rc, 0, text)
        self.assertIn("::notice title=Real selftest.json::", text)
        self.assertIn("::notice title=Real response through the application: PASS::", text)
        self.assertNotIn("\n::", text.split("::notice title=Real response", 1)[1].split("\n", 1)[1] if "\n" in text.split("::notice title=Real response", 1)[1] else "")
        rc, text, _ = self.run_check(self.app("pass"), lowered=True, extra=["--annotate"])
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Real response through the application: FAIL::", text)

    def test_not_executed_mode_checks_nothing_and_says_so(self):
        rc, text, _ = self.run_check(None, extra=["--annotate", "--not-executed", "the selftest did not pass (verdict 1)"])
        self.assertEqual(rc, 0)
        self.assertIn("::warning title=Real response through the application: NOT EXECUTED::", text)
        self.assertIn("did not pass", text)


class TheWorkflowEdit(unittest.TestCase):
    """Text checks of .github/workflows/windows-ci.yml (no YAML library needed, so they run on every runner)."""
    WF = (HERE.parent / ".github" / "workflows" / "windows-ci.yml").read_text(encoding="utf-8")

    def real_step(self):
        i = self.WF.index("      - id: real")
        j = self.WF.index("      - name: Evidence annotations", i)
        return self.WF[i:j]

    def test_the_checker_runs_inside_the_consent_gated_step_and_only_there(self):
        step = self.real_step()
        self.assertIn("inputs.real_inference_consent == 'yes-download-qwen3-1.7b-q4_k_m'", step)
        self.assertIn("github.event_name == 'workflow_dispatch'", step)
        self.assertEqual(self.WF.count("ci_real_response_check.py"), 2)               # the executed call and the NOT EXECUTED call, both in the real step
        self.assertEqual(step.count("ci_real_response_check.py"), 2)

    def test_the_run_pins_size_hash_runtime_and_commit_and_downloads_only_the_documented_model(self):
        step = self.real_step()
        for needle in ("--expect-bytes 1282439264", "--expect-sha256 d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5",
                       "--expect-runtime 0.3.36", "--commit $env:GITHUB_SHA", '$id = "qwen3-1.7b-q4_k_m"', "models fetch $id --yes"):
            self.assertIn(needle, step)
        self.assertEqual(step.count("models fetch"), 1)

    def test_the_checker_only_runs_when_the_selftest_passed_and_otherwise_says_not_executed(self):
        step = self.real_step()
        self.assertIn("if ($verdict -eq 0) {", step)
        self.assertIn("--not-executed", step)
        self.assertLess(step.index("ci_check_selftest_report.py"), step.index("ci_real_response_check.py"))

    def test_the_default_consent_is_still_empty_and_no_trigger_was_added(self):
        self.assertIn('default: ""', self.WF.split("real_inference_consent:")[1][:600])
        head = self.WF.split("concurrency:")[0]
        self.assertEqual(head.count("workflow_dispatch:"), 1)
        self.assertEqual(self.WF.count("yes-download-qwen3-1.7b-q4_k_m"), self.WF.count("yes-download-qwen3-1.7b-q4_k_m"))


class AgainstTheCatalogue(Case):
    def test_a_catalogued_model_id_whose_file_is_far_from_the_catalogue_size_is_rejected(self):
        p = self.d / "models" / CAT / "model.gguf"
        p.parent.mkdir(parents=True)
        build_gguf(p, tensors=400, size=5000, name="Qwen3 1.7B")           # plausible tensor count, but 5 KB instead of ~1.28 GB
        argv = ["--data-dir", str(self.d), "--model-id", CAT, "--min-tensors", "10", "--min-bytes", "1000", "--cmd", sys.executable, "--allow-test-command"]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = CHK.main(argv)
        self.assertEqual(rc, 1)
        self.assertIn("catalogue", buf.getvalue())


class JudgesTheReply(Case):
    def setUp(self):
        super().setUp()
        self.model(tensors=100, size=2000, name="Qwen3 1.7B")

    def assertFails(self, body, needle):
        rc, text, rep = self.run_check(self.app(body), lowered=True)
        self.assertEqual(rc, 1, text)
        self.assertIn(needle, rep["why"])

    def test_empty_reply(self):
        self.assertFails("pass", "empty")

    def test_reply_without_the_expected_word(self):
        self.assertFails('print("The capital is a city in Europe.")', "does not contain")

    def test_nonzero_exit_even_with_a_right_looking_reply(self):
        self.assertFails('print("Paris"); sys.exit(3)', "exit code")

    def test_traceback(self):
        self.assertFails('print("Paris"); sys.stderr.write("Traceback (most recent call last):\\n")', "traceback")

    def test_the_no_text_error_is_a_failure(self):
        self.assertFails('print("Paris"); sys.stderr.write("The model returned no text.\\n")', "no text")

    def test_missing_local_model_banner(self):
        p = Path(self.tmp.name) / "nobanner.py"
        p.write_text('print("Paris")\n', encoding="utf-8")
        rc, text, rep = self.run_check(p, lowered=True)
        self.assertEqual(rc, 1, text)
        self.assertIn("banner", rep["why"])

    def test_a_hang_is_a_failure_not_a_pass(self):
        rc, text, rep = self.run_check(self.app("import time; time.sleep(60)"), lowered=True, extra=["--timeout", "1"])
        self.assertEqual(rc, 1, text)
        self.assertIn("time limit", rep["why"])

    def test_pass_path_with_lowered_thresholds_only(self):
        """TEST-ONLY pass: a stand-in application and a synthetic file with LOWERED thresholds. It proves the checker can say PASS, not that anything real ran."""
        rc, text, rep = self.run_check(self.app('print("The capital of France is Paris.")'), lowered=True)
        self.assertEqual(rc, 0, text)
        self.assertEqual(rep["verdict"], "PASS")
        self.assertEqual(rep["tensor_count"], 100)
        self.assertEqual(len(rep["model_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
