"""How `jarvis selftest` starts its worker process when the program is a PyInstaller build, and that the packaged entry point really dispatches it.

WHAT THESE TESTS ARE: no PyInstaller and no Windows are available where they run. A "frozen" build is EMULATED: `sys.frozen` and `sys.executable` are
patched, and `sys.executable` points at a small shell script that runs the REAL packaging/entry_cli.py (the same file jarvis.exe is built from) with a FAKE
`llama_cpp` module. So these tests prove this program's own command construction, argument parsing, dispatch, no-fallback and exit-status logic. They do NOT
prove that PyInstaller bundles the right files, that jarvis.exe can start itself on Windows, or anything about the real llama.cpp runtime or a real model."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from test_local_model import make_gguf

from jarvis import cli, launcher
from jarvis import selftest_core as core
from jarvis.app import diagnostics
from jarvis.config import Config
from jarvis.models.manager import ModelStore

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "packaging" / "entry_cli.py"
MODEL_ID = "fake"
FROZEN_EXE = r"C:\Program Files\Project Jarvis\jarvis.exe"
sys.path.insert(0, str(ROOT / "packaging"))
import manifest  # noqa: E402

FAKE_LLAMA = textwrap.dedent('''
    import os
    from pathlib import Path
    __version__ = "0.0.0"
    def llama_supports_gpu_offload():
        return False
    class Llama:
        def __init__(self, **kw):
            Path(os.environ["JV_MARK"], "llama_pid.txt").write_text(str(os.getpid()))
        def tokenize(self, b, add_bos=False):
            return list(b)
        def create_chat_completion(self, messages=None, stream=False, **kw):
            return {"choices": [{"message": {"content": "The answer is 4."}, "finish_reason": "stop"}], "usage": {"completion_tokens": 4}}
''')


class CommandConstruction(unittest.TestCase):
    cfg = Config(data_dir=Path("C:/Users/A B/jarvis data") if os.name == "nt" else Path("/tmp/a b/jarvis data"))

    def test_source_mode_starts_python_dash_m_jarvis(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            cmd = core.worker_command(self.cfg, "r1", "m 1")
        self.assertEqual(cmd, [sys.executable, "-m", "jarvis", "--data-dir", str(self.cfg.data_dir), "selftest-worker", "--run-id", "r1", "--model-id", "m 1"])

    def test_frozen_mode_starts_the_packaged_executable_itself_without_dash_m(self):
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", FROZEN_EXE):
            cmd = core.worker_command(self.cfg, "r1", "m 1")
        self.assertEqual(cmd, [FROZEN_EXE, "--data-dir", str(self.cfg.data_dir), "selftest-worker", "--run-id", "r1", "--model-id", "m 1"])
        self.assertNotIn("-m", cmd)

    def test_the_arguments_in_either_mode_parse_back_to_the_worker_command(self):
        for frozen, skip in ((False, 3), (True, 1)):
            with self.subTest(frozen=frozen), mock.patch.object(sys, "frozen", frozen, create=True):
                cmd = core.worker_command(self.cfg, "abc123", "my model")
                a = cli.build_parser().parse_args(cmd[skip:])
                self.assertEqual((a.cmd, a.run_id, a.model_id, a.data_dir), ("selftest-worker", "abc123", "my model", str(self.cfg.data_dir)))

    def test_frozen_environment_adds_no_import_path_and_source_environment_does(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "X"}), mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(core._child_env()["PYTHONPATH"], "X")
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(sys, "frozen", True, create=True):
            os.environ.pop("PYTHONPATH", None)
            self.assertNotIn("PYTHONPATH", core._child_env())
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertTrue(core._child_env()["PYTHONPATH"].startswith(str(ROOT / "src")))


class Dispatch(unittest.TestCase):
    """The packaged entry point is launcher.run(cli.main). A worker invocation has arguments, so it must neither open the guided setup nor wait for Enter,
    and must never start the supervisor again."""

    def args(self):
        return ["--data-dir", "D", "selftest-worker", "--run-id", "r9", "--model-id", "mm"]

    def run_entry(self, worker_result, eof_on_stdin=False):
        called = {}

        def fake_worker(cfg, store, run_id, model_id):
            called.update(data_dir=str(cfg.data_dir), run_id=run_id, model_id=model_id)
            if isinstance(worker_result, BaseException):
                raise worker_result
            return worker_result
        asked = []

        def ask(prompt):
            asked.append(prompt)
            if eof_on_stdin:
                raise EOFError
            return ""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(core, "run_worker", fake_worker), \
                mock.patch.object(core, "run_selftest", side_effect=AssertionError("the supervisor must not start inside a worker")), \
                mock.patch.object(cli, "_selftest", side_effect=AssertionError("the supervisor must not start inside a worker")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = launcher.run(cli.main, argv=self.args(), input_fn=ask, ours=lambda: False if not eof_on_stdin else True, data_dir=tmp)
        return rc, called, asked

    def test_worker_arguments_reach_run_worker_and_nothing_else_starts(self):
        rc, called, asked = self.run_entry(0)
        self.assertEqual((rc, called["run_id"], called["model_id"]), (0, "r9", "mm"))
        self.assertTrue(called["data_dir"].endswith("D"))
        self.assertEqual(asked, [], "a worker must never pause for Enter")

    def test_the_workers_exit_status_is_passed_through(self):
        self.assertEqual(self.run_entry(1)[0], 1)

    def test_an_unexpected_error_in_the_worker_is_a_nonzero_exit_not_a_hang(self):
        # The launcher may offer "Press Enter" when it believes the console is its own (a hidden console of a worker looks like that). The supervisor starts
        # the worker with standard input closed (subprocess.DEVNULL), so that prompt gets end-of-file at once; this test models exactly that.
        rc, _, asked = self.run_entry(RuntimeError("boom"), eof_on_stdin=True)
        self.assertEqual(rc, 1)
        self.assertLessEqual(len(asked), 1)


class FrozenEmulation(unittest.TestCase):
    """The REAL worker_command and the REAL packaging/entry_cli.py, started through a stand-in 'jarvis.exe' script."""

    def setUp(self):
        if sys.platform == "win32":
            self.skipTest("the stand-in executable is a POSIX shell script")
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg = Config(data_dir=self.root / "data dir")
        self.cfg.data_dir.mkdir()
        self.store = ModelStore(self.cfg.models_dir)
        self.store.install_gguf(make_gguf(self.root / "m.gguf"), MODEL_ID, "Apache-2.0", "t", extra_meta=None)
        self.store.set_active(MODEL_ID)
        (self.root / "fake").mkdir()
        (self.root / "fake" / "llama_cpp.py").write_text(FAKE_LLAMA, encoding="utf-8")
        self.marks = self.root / "marks"
        self.marks.mkdir()

    def exe(self, body=None):
        p = self.root / "jarvis"
        body = body or f'exec "{sys.executable}" "{ENTRY}" "$@"'
        p.write_text(f'#!/bin/sh\nexport PYTHONPATH="{ROOT / "src"}:{self.root / "fake"}"\nexport JV_MARK="{self.marks}"\n{body}\n', encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    def run_frozen(self, exe, **kw):
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe), \
                mock.patch.object(core, "run_worker", side_effect=AssertionError("inference must never run in the supervising process")):
            return core.run_selftest(self.cfg, self.store, MODEL_ID, **kw)

    def test_a_pass_comes_from_a_separate_process_started_through_the_packaged_entry_point(self):
        self.assertNotIn("llama_cpp", sys.modules)
        res = self.run_frozen(self.exe())
        self.assertEqual((res["status"], res["exit_code"], res["passed"]), ("passed", 0, True), res.get("error"))
        child = int((self.marks / "llama_pid.txt").read_text())
        self.assertNotEqual(child, os.getpid(), "the model must be loaded by the worker, not by the supervisor")
        self.assertNotIn("llama_cpp", sys.modules)
        self.assertEqual(json.loads((self.cfg.data_dir / "selftest.json").read_text(encoding="utf-8"))["status"], "passed")

    def test_an_executable_that_cannot_start_fails_and_never_falls_back_to_the_parent(self):
        res = self.run_frozen(str(self.root / "no-such-jarvis.exe"))
        self.assertEqual((res["status"], res["exit_code"], res["failure"]["kind"]), ("failed", 3, "worker_start_failed"))
        self.assertFalse((self.marks / "llama_pid.txt").exists())
        self.assertNotIn("llama_cpp", sys.modules)

    def test_a_worker_that_dies_without_a_result_is_reported_as_crashed(self):
        res = self.run_frozen(self.exe("exit 139"))
        self.assertEqual((res["status"], res["exit_code"], res["failure"]["kind"]), ("crashed", 5, "worker_crashed"))
        self.assertFalse((self.marks / "llama_pid.txt").exists())

    def test_a_worker_that_hangs_is_stopped_at_the_load_timeout(self):
        res = self.run_frozen(self.exe("exec sleep 60"), load_timeout=1.5, generate_timeout=5)
        self.assertEqual((res["status"], res["exit_code"], res["failure"]["kind"]), ("timeout", 4, "timeout"))

    def test_the_cli_command_uses_the_same_frozen_path_and_exit_code(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", self.exe()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "selftest"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("SELFTEST PASSED", err.getvalue())


class PackagingLists(unittest.TestCase):
    def test_the_worker_module_is_in_both_mirrored_lists(self):
        self.assertIn("jarvis.selftest_core", manifest.HIDDEN_REQUIRED)
        self.assertEqual(set(diagnostics.REQUIRED_MODULES), set(manifest.HIDDEN_REQUIRED))

    def test_the_packaged_readiness_check_finds_and_imports_it_and_the_worker_entry_exists(self):
        rows = {r["module"]: r for r in diagnostics.required_modules(import_them=True)["modules"]}
        self.assertTrue(rows["jarvis.selftest_core"]["imported"], rows["jarvis.selftest_core"])
        self.assertTrue(callable(core.run_worker) and callable(core.run_selftest))


if __name__ == "__main__":
    unittest.main()
