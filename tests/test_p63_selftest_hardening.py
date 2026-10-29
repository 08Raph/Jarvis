"""`jarvis selftest` hardening: durable started marker, per-stage atomic reports, a supervisor that times out / kills / detects a dead worker process,
a deterministic answer check, and recovery from an interrupted earlier run.

WHAT THESE TESTS ARE: the supervisor and the worker run as REAL separate processes, but the model runtime inside the worker is a FAKE `llama_cpp`
module (tests/selftest_fake_worker.py) and the model file is a synthetic GGUF header. They prove this program's own logic: staging, timeouts,
process killing, exit codes, report contents. A "crash" here is a fake worker killing itself or exiting with a crash-like code. They do NOT prove that the
real llama.cpp runtime loads or answers, how a real native crash looks on Windows, or that a real model passes the answer check."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from test_local_model import make_gguf

from jarvis import cli
from jarvis import selftest_core as core
from jarvis.config import Config
from jarvis.models.manager import ModelStore

HELPER = Path(__file__).resolve().parent / "selftest_fake_worker.py"
MODEL_ID = "fake"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg = Config(data_dir=self.root / "data")
        self.cfg.data_dir.mkdir()
        self.store = ModelStore(self.cfg.models_dir)
        self.store.install_gguf(make_gguf(self.root / "m.gguf"), MODEL_ID, "Apache-2.0", "t", extra_meta=None)
        self.store.set_active(MODEL_ID)
        self.report_path = self.cfg.data_dir / "selftest.json"

    def command(self, mode):
        def build(cfg, run_id, model_id):
            return [sys.executable, str(HELPER), mode, "--data-dir", str(cfg.data_dir), "selftest-worker", "--run-id", run_id, "--model-id", model_id]
        return build

    def run_core(self, mode, **kw):
        with mock.patch.object(core, "worker_command", self.command(mode)):
            return core.run_selftest(self.cfg, self.store, MODEL_ID, **kw)

    def run_cli(self, mode, *extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(core, "worker_command", self.command(mode)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "selftest", *extra])
        return rc, out.getvalue(), err.getvalue()

    def report(self):
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def pid_is_gone(self):
        pid = int((self.cfg.data_dir / "worker.pid").read_text())
        if sys.platform == "win32":
            return True          # the liveness probe below is POSIX only
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False


class StartedMarkerAndSuccess(Base):
    def test_a_started_marker_is_on_disk_before_the_model_is_loaded(self):
        self.run_core("ok_check_marker")
        seen = json.loads((self.cfg.data_dir / "marker_seen.txt").read_text(encoding="utf-8"))     # copied by the fake runtime AT model-load time
        self.assertEqual(seen["status"], "running")
        self.assertFalse(seen["passed"])
        self.assertEqual(seen["schema"], 2)
        self.assertRegex(seen["run_id"], r"^[0-9a-f]{12}$")
        self.assertIn(seen["stage"], ("started", "starting_worker", "runtime_check", "loading_model"))
        self.assertIn("started_at", seen)
        self.assertEqual(seen["model_id"], MODEL_ID)
        self.assertIn("started but has not finished", seen["error"])       # an interrupted run reads as a failed one to older readers

    def test_a_successful_run_writes_a_complete_final_report(self):
        rc, out, err = self.run_cli("ok")
        self.assertEqual(rc, 0)
        self.assertIn("SELFTEST PASSED", err)
        r = self.report()
        self.assertEqual((r["status"], r["passed"], r["stage"], r["exit_code"]), ("passed", True, "finished", 0))
        self.assertNotIn("error", r)
        self.assertEqual(r["runtime"]["version"], "0.0.0")
        self.assertEqual(r["answer_check"], {"name": "arith-2plus2-v1", "ok": True, "reason": "ok"})
        stages = [s["stage"] for s in r["stages"]]
        for want in ("started", "starting_worker", "runtime_check", "loading_model", "generating", "validating_output", "finished"):
            self.assertIn(want, stages)
        for key in ("run_id", "started_at", "finished_at", "updated_at", "model_id", "model_sha256", "app_version", "time", "load_seconds",
                    "generation_seconds", "output_tokens", "prompt_id", "limits"):
            self.assertIn(key, r)
        self.assertEqual(json.loads(out[out.index("{"):])["run_id"], r["run_id"])                      # what is printed is what was saved
        self.assertEqual(cli._selftest_state(self.cfg, self.store, MODEL_ID), "passed")
        for leftover in ("selftest.progress.json", "selftest.worker.json"):
            self.assertFalse((self.cfg.data_dir / leftover).exists())
        self.assertEqual([p.name for p in self.cfg.data_dir.glob("selftest.json.tmp*")], [])

    def test_the_fixed_prompt_and_long_text_are_not_stored(self):
        self.run_core("ok")
        text = self.report_path.read_text(encoding="utf-8")
        self.assertNotIn("What is 2+2", text)
        self.assertNotIn("helpful assistant", text)
        self.assertLessEqual(len(self.report()["answer"]), core.ANSWER_REPORT_CHARS)

    def test_a_reasoning_block_before_the_number_still_passes(self):
        self.assertEqual(self.run_cli("thinking")[0], 0)


class FailureKinds(Base):
    def test_model_load_failure(self):
        rc, out, err = self.run_cli("raise_load")
        self.assertEqual(rc, 3)
        r = self.report()
        self.assertEqual((r["status"], r["passed"], r["failure"]["kind"], r["failure"]["stage"]), ("failed", False, "load_failed", "loading_model"))
        self.assertIn("SELFTEST FAILED", err)

    def test_missing_runtime_is_reported_as_such(self):
        rc, out, err = self.run_cli("no_runtime")
        self.assertEqual(rc, 3)
        self.assertEqual(self.report()["failure"]["kind"], "runtime_unavailable")
        self.assertEqual(cli._LAST.get("err"), "runtime_unavailable")

    def test_empty_output_is_a_failure_with_the_old_exit_code(self):
        rc, _, _ = self.run_cli("empty")
        r = self.report()
        self.assertEqual((rc, r["passed"], r["failure"]["kind"]), (3, False, "empty_output"))

    def test_invalid_outputs_are_failures_with_their_own_exit_code(self):
        for mode in ("invalid", "control", "long", "wrong_and_right"):
            with self.subTest(mode=mode):
                rc, _, err = self.run_cli(mode)
                r = self.report()
                self.assertEqual((rc, r["status"], r["passed"], r["failure"]["kind"]), (6, "failed", False, "invalid_output"))
                self.assertFalse(r["answer_check"]["ok"])
                self.assertIn("SELFTEST FAILED", err)

    def test_an_exception_during_generation_is_a_failure_and_never_leaks_the_prompt(self):
        rc, out, err = self.run_cli("raise_gen")
        r = self.report()
        self.assertEqual((rc, r["failure"]["kind"], r["failure"]["stage"]), (3, "generation_failed", "generating"))
        self.assertNotIn("PRIVATE-PROMPT-TEXT", self.report_path.read_text(encoding="utf-8") + out + err)

    def test_a_worker_that_ends_without_a_result_is_not_a_pass(self):
        rc, _, _ = self.run_cli("silent_exit")
        r = self.report()
        self.assertEqual((rc, r["passed"], r["failure"]["kind"]), (5, False, "worker_no_result"))


class TimeoutsAndCrashes(Base):
    def test_a_hung_generation_times_out_and_the_worker_is_killed(self):
        t0 = time.monotonic()
        r = self.run_core("hang_gen", generate_timeout=1.5, load_timeout=30, poll=0.05)
        self.assertLess(time.monotonic() - t0, 25)
        self.assertEqual((r["status"], r["exit_code"], r["passed"], r["failure"]["kind"], r["failure"]["stage"]), ("timeout", 4, False, "timeout", "generating"))
        self.assertTrue(self.pid_is_gone())
        self.assertEqual(self.report()["status"], "timeout")

    def test_a_hung_load_times_out_and_the_worker_is_killed(self):
        r = self.run_core("hang_load", load_timeout=1.5, generate_timeout=30, poll=0.05)
        self.assertEqual((r["status"], r["exit_code"], r["failure"]["stage"]), ("timeout", 4, "loading_model"))
        self.assertTrue(self.pid_is_gone())

    def test_timeout_exit_code_through_the_cli(self):
        with mock.patch.object(core, "LOAD_TIMEOUT", 1.5), mock.patch.object(core, "GENERATE_TIMEOUT", 1.5):
            rc, _, err = self.run_cli("hang_gen")
        self.assertEqual(rc, 4)
        self.assertIn("SELFTEST FAILED", err)

    def test_a_crashing_worker_is_detected_by_the_supervisor(self):
        for mode, stage in (("crash_gen", "generating"), ("crash_load", "loading_model"), ("crash_before_runtime", "starting_worker")):
            with self.subTest(mode=mode):
                rc, _, err = self.run_cli(mode)
                r = self.report()
                info = f"rc={rc} status={r.get('status')!r} stage={r.get('failure', {}).get('stage')!r} kind={r.get('failure', {}).get('kind')!r} worker_exit={r.get('worker_exit')!r} stages={[x.get('stage') for x in r.get('stages', [])]} stderr={err[-300:]!r}"
                self.assertEqual((rc, r["status"], r["passed"], r["failure"]["kind"], r["failure"]["stage"]), (5, "crashed", False, "worker_crashed", stage), info)
                self.assertRegex(r["worker_exit"], r"exit|Windows status", info)        # "exit code 139" (POSIX) or "Windows status 0x... (name)"
                self.assertIn("native crash", r["failure"]["message"], info)

    @unittest.skipIf(sys.platform == "win32", "uses a POSIX signal")
    def test_a_worker_killed_by_the_system_is_reported_with_the_signal(self):
        r = self.run_core("kill_gen")
        self.assertEqual((r["status"], r["exit_code"]), ("crashed", 5))
        self.assertIn("signal 9", r["worker_exit"])

    def test_cancellation_stops_the_worker_and_is_recorded(self):
        pidfile = self.cfg.data_dir / "worker.pid"

        def interrupt(_):
            if not pidfile.exists():
                time.sleep(0.05)            # wait until the worker process is really running, then cancel
                return
            raise KeyboardInterrupt
        r = self.run_core("hang_gen", poll=0.05, sleep=interrupt)
        self.assertEqual((r["status"], r["exit_code"], r["passed"]), ("cancelled", 130, False))
        self.assertEqual(self.report()["status"], "cancelled")
        time.sleep(0.2)
        self.assertTrue(self.pid_is_gone())

    def test_a_crash_after_an_older_pass_does_not_leave_the_old_pass_behind(self):
        self.run_cli("ok")
        self.assertEqual(cli._selftest_state(self.cfg, self.store, MODEL_ID), "passed")
        self.run_cli("crash_gen")
        self.assertEqual(cli._selftest_state(self.cfg, self.store, MODEL_ID), "not_run")
        self.assertFalse(self.report()["passed"])

    def test_the_marker_failing_to_write_means_nothing_is_started(self):
        build = mock.Mock(side_effect=AssertionError("a worker must not start"))
        with mock.patch.object(core, "write_json_atomic", side_effect=OSError("disk full")), mock.patch.object(core, "worker_command", build):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["--data-dir", str(self.cfg.data_dir), "selftest"])
        self.assertEqual(rc, 3)
        build.assert_not_called()
        self.assertIn("nothing was loaded", err.getvalue())


class RecoveryAndAtomicity(Base):
    def test_an_interrupted_earlier_run_is_reported_and_replaced(self):
        self.report_path.write_text(json.dumps({"schema": 2, "run_id": "oldrun000001", "status": "running", "stage": "generating",
                                                 "started_at": "2026-10-28T10:00:00+00:00", "updated_at": "2026-10-28T10:00:05+00:00"}), encoding="utf-8")
        (self.cfg.data_dir / "selftest.json.tmp-999-abcdef").write_text("{half", encoding="utf-8")
        rc, _, err = self.run_cli("ok")
        r = self.report()
        self.assertEqual(rc, 0)
        self.assertNotEqual(r["run_id"], "oldrun000001")
        self.assertEqual((r["previous_run"]["status"], r["previous_run"]["run_id"], r["previous_run"]["last_stage"]), ("interrupted", "oldrun000001", "generating"))
        self.assertIn("did not finish", err)
        self.assertEqual([p.name for p in self.cfg.data_dir.glob("selftest.json.tmp*")], [])

    def test_a_damaged_earlier_report_is_replaced_not_fatal(self):
        self.report_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_cli("ok")[0], 0)
        self.assertEqual(self.report()["previous_run"]["status"], "unreadable")

    def test_a_finished_earlier_report_is_not_called_interrupted(self):
        self.run_cli("ok")
        self.run_cli("ok")
        self.assertNotIn("previous_run", self.report())

    def test_the_old_report_survives_a_failed_write(self):
        p = self.cfg.data_dir / "x.json"
        core.write_json_atomic(p, {"v": 1})
        with mock.patch.object(core.json, "dumps", side_effect=TypeError("boom")):
            with self.assertRaises(TypeError):
                core.write_json_atomic(p, {"v": 2})
        self.assertEqual(json.loads(p.read_text()), {"v": 1})
        self.assertEqual([q.name for q in self.cfg.data_dir.glob("x.json.tmp*")], [])

    def test_a_briefly_locked_report_is_retried(self):
        p = self.cfg.data_dir / "z.json"
        core.write_json_atomic(p, {"v": 1})
        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("in use by another process")
            return real(src, dst)
        with mock.patch.object(core.os, "replace", flaky):
            core.write_json_atomic(p, {"v": 2})
        self.assertEqual((json.loads(p.read_text()), len(calls)), ({"v": 2}, 3))
        with mock.patch.object(core.os, "replace", side_effect=PermissionError("locked")), self.assertRaises(PermissionError):
            core.write_json_atomic(p, {"v": 3})
        self.assertEqual(json.loads(p.read_text()), {"v": 2})
        self.assertEqual([q.name for q in self.cfg.data_dir.glob("z.json.tmp*")], [])

    def test_a_reader_never_sees_a_half_written_report(self):
        p = self.cfg.data_dir / "y.json"
        core.write_json_atomic(p, {"n": 0, "pad": "x" * 50000})
        stop, bad = threading.Event(), []

        refused = []

        def writer():
            try:
                for i in range(1, 150):
                    try:
                        core.write_json_atomic(p, {"n": i, "pad": "x" * 50000})
                    except PermissionError:
                        refused.append(i)       # Windows: the reader's open handle can outlast write_json_atomic's retries; that is not a torn read
            finally:
                stop.set()                      # ALWAYS end the reader loop (a writer that died used to leave this test spinning until the CI timeout)
        t = threading.Thread(target=writer, daemon=True)
        t.start()
        reads = 0
        deadline = time.monotonic() + 120
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.0005)                  # give the writer's replace a chance on Windows
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                bad.append(1)
            except OSError:
                pass            # Windows can refuse a read during the replace; a torn read would be a ValueError
            reads += 1
        t.join(30)
        self.assertFalse(t.is_alive(), "the writer thread did not finish")
        self.assertEqual(bad, [])
        self.assertGreater(reads, 0)


class PureFunctions(unittest.TestCase):
    def test_answer_check_table(self):
        good = ["4", "The answer is 4.", "2+2=4", "2 + 2 = 4", "Four", "four.", "The result of 2 + 2 is 4", "4.0", "**4**", "  4\n"]
        bad = ["", "   ", "5", "The answer is 7.", "14", "44", "4 or 5", "four or five", "I am not sure.", "2+2", "x4", "<|im_start|>4", "<think>4",
               "4 " + "word " * 60, "The answer is three."]
        for t in good:
            with self.subTest(good=t):
                self.assertTrue(core.check_answer(t)[0], t)
        for t in bad:
            with self.subTest(bad=t):
                ok, why = core.check_answer(t)
                self.assertFalse(ok, t)
                self.assertTrue(why)
        self.assertFalse(core.check_answer(None)[0])

    def test_describe_exit(self):
        self.assertEqual(core.describe_exit(-9), "terminated by signal 9")
        self.assertEqual(core.describe_exit(3221225477), "Windows status 0xC0000005 (access violation)")
        self.assertIn("illegal instruction", core.describe_exit(3221225501))
        self.assertEqual(core.describe_exit(139), "exit code 139")
        self.assertEqual(core.describe_exit(None), "no exit code")

    def test_timeout_arguments_are_bounded(self):
        for bad in ("1", "0", "99999", "abc"):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["selftest", "--load-timeout", bad])
        self.assertEqual(cli.build_parser().parse_args(["selftest", "--generate-timeout", "30"]).generate_timeout, 30.0)

    def test_the_default_worker_command_runs_this_program_again(self):
        cmd = core.worker_command(Config(data_dir=Path("D")), "abc", "m")
        self.assertEqual(cmd[-5:], ["selftest-worker", "--run-id", "abc", "--model-id", "m"])
        self.assertIn("selftest-worker", cmd)
        self.assertEqual(cmd[cmd.index("--data-dir") + 1], str(Path("D")))
        self.assertTrue(core._child_env().get("PYTHONPATH"))


if __name__ == "__main__":
    unittest.main()
