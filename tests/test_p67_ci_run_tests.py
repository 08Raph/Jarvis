"""scripts/ci_run_tests.py: exit codes, annotations, hard timeout with process-tree cleanup, reporting that cannot change the result.
Uses tiny throw-away test suites in a temp folder. The Windows branch (taskkill) is only checked as a command line, never run here."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_run_tests as R  # noqa: E402

PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n    @unittest.skip('why not')\n    def test_s(self): pass\n"
FAILING = "import unittest\nclass T(unittest.TestCase):\n    def test_bad(self): self.assertEqual(1, 2)\n    def test_boom(self): raise RuntimeError('x')\n"
HANGING = ("import subprocess, sys, unittest, pathlib\nclass T(unittest.TestCase):\n    def test_hang(self):\n"
           "        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
           "        pathlib.Path('child.pid').write_text(str(p.pid))\n        while True: pass\n")
EMPTY = "x = 1\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="jarvis-p67-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def suite(self, src):
        t = self.d / "tests"
        t.mkdir()
        (t / "test_x.py").write_text(src)

    def run_runner(self, timeout=60, extra=()):
        log = self.d / "out" / "t.log"
        out = io.StringIO()
        with redirect_stdout(out):
            code = R.main(["--title", "Unit", "--log", str(log), "--timeout", str(timeout), "--cwd", str(self.d), "--start-dir", "tests",
                           "--json", str(self.d / "r.json"), "--summary", str(self.d / "sum.md"), *extra])
        return code, out.getvalue()


class Helpers(unittest.TestCase):
    def test_escaping(self):
        self.assertEqual(R.esc_data("50%\r\nx"), "50%25%0D%0Ax")
        self.assertEqual(R.esc_prop("a:b,c"), "a%3Ab%2Cc")

    def test_annotation_is_one_line_and_keeps_the_end(self):
        out = io.StringIO()
        with redirect_stdout(out):
            R.annotate("error", "t", "line1\n" + "z" * 5000 + "END", limit=100)
        text = out.getvalue()
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertTrue(text.startswith("::error title=t::"))
        self.assertTrue(text.strip().endswith("END"))

    def test_taskkill_command(self):
        self.assertEqual(R.kill_command(123, True), ["taskkill", "/F", "/T", "/PID", "123"])

    def test_child_command_arms_faulthandler_before_the_hard_limit(self):
        code = R.child_command("tests", 1000)[3]
        self.assertIn("dump_traceback_later(450", code)
        self.assertIn("'-v'", code)

    def test_running_test_finder(self):
        lines = ["test_a (m.T.test_a) ... ok", "test_b (m.T.test_b) ... "]
        self.assertIn("test_b", R.running_test(lines))

    def test_decide_never_returns_success_without_a_pass_label_and_exit_zero(self):
        ok = {"label": "PASS"}
        self.assertEqual(R.decide({"started": True, "timed_out": False, "returncode": 0}, ok), 0)
        self.assertEqual(R.decide({"started": True, "timed_out": False, "returncode": 1}, ok), 2)
        self.assertEqual(R.decide({"started": True, "timed_out": False, "returncode": 0}, {"label": "NOT RUN"}), 2)
        self.assertEqual(R.decide({"started": True, "timed_out": False, "returncode": 1}, {"label": "FAIL"}), 1)
        self.assertEqual(R.decide({"started": True, "timed_out": True, "returncode": -9}, ok), 3)
        self.assertEqual(R.decide({"started": False}, ok), 4)


class Runs(Base):
    def test_passing_suite(self):
        self.suite(PASSING)
        code, out = self.run_runner()
        self.assertEqual(code, 0, out)
        self.assertIn("::notice title=Unit%3A PASS::", out)
        self.assertIn("skipped tests", out)
        data = json.loads((self.d / "r.json").read_text())
        self.assertEqual(data["exit"], 0)
        self.assertIn("Unit", (self.d / "sum.md").read_text())

    def test_failing_suite_names_the_tests(self):
        self.suite(FAILING)
        code, out = self.run_runner()
        self.assertEqual(code, 1, out)
        self.assertIn("::error title=Unit%3A failing tests::", out)
        self.assertIn("test_bad", out)
        self.assertIn("test_boom", out)

    def test_suite_that_runs_nothing_is_not_success(self):
        self.suite(EMPTY)
        code, out = self.run_runner()
        self.assertNotEqual(code, 0, out)

    def test_missing_start_dir_is_not_success(self):
        code, out = self.run_runner(extra=["--start-dir", "nope"])
        self.assertNotEqual(code, 0, out)

    def test_reporting_failure_cannot_change_the_exit_code(self):
        self.suite(FAILING)
        with mock.patch.object(R, "report", side_effect=RuntimeError("boom")):
            code, out = self.run_runner()
        self.assertEqual(code, 1)
        self.assertIn("diagnostic reporting failed", out)


@unittest.skipIf(os.name == "nt", "the POSIX process-group kill is tested here; the Windows branch is only a command line")
class Timeout(Base):
    def test_hang_is_killed_with_its_children_and_reported(self):
        self.suite(HANGING)
        t0 = time.monotonic()
        code, out = self.run_runner(timeout=12)
        self.assertEqual(code, 3, out)
        self.assertLess(time.monotonic() - t0, 60)
        self.assertIn("TIMED OUT", out)
        self.assertIn("test_hang", out)                 # the running test is named
        self.assertIn("child exited", out)
        self.assertIn("test_x.py", out)                 # faulthandler dump shows where it was stuck
        pid = int((self.d / "child.pid").read_text())
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        if alive:                                       # a zombie of ours is fine; a live sleeper is not
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
            except OSError:
                state = "gone"
            self.assertIn(state, ("Z", "gone"), "the grandchild survived the timeout")
        self.assertEqual(json.loads((self.d / "r.json").read_text())["exit"], 3)


if __name__ == "__main__":
    unittest.main()
