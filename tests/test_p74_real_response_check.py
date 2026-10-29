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
