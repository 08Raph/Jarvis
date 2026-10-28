"""scripts/verify_frozen_worker.py: the model-free post-build gate for the packaged selftest worker.

WHAT THESE TESTS ARE: the gate is run against STAND-IN executables (small POSIX shell scripts that run the real packaging/entry_cli.py, optionally with a
FAKE `llama_cpp`, optionally pretending `sys.frozen` is set). They prove the gate's own logic: that it passes on a correct dispatch and FAILS on each way the
packaged worker could be wrong (not frozen, hanging, wrong outcome, module missing, worker started when it should not be). They are NOT a PyInstaller
build, NOT a Windows run, and NOT real inference. The PowerShell hook is checked only as text (no PowerShell is available here)."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_frozen_worker as V  # noqa: E402

ENTRY = ROOT / "packaging" / "entry_cli.py"
FAKE_LLAMA = '__version__ = "0.0.0"\ndef llama_supports_gpu_offload():\n    return False\nclass Llama:\n    def __init__(self, **kw):\n        raise AssertionError("no model may be loaded")\n'


@unittest.skipIf(sys.platform == "win32", "the stand-in executables are POSIX shell scripts")
class GateAgainstStandIns(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        (self.root / "fake").mkdir()
        (self.root / "fake" / "llama_cpp.py").write_text(FAKE_LLAMA, encoding="utf-8")
        p = mock.patch.object(tempfile, "tempdir", str(self.tmp))
        p.start()
        self.addCleanup(p.stop)

    def exe(self, frozen=True, fake_runtime=False, prelude=""):
        path = f'"{ROOT / "src"}"' + (f':"{self.root / "fake"}"' if fake_runtime else "")
        run = (f"import sys, runpy; sys.frozen = True; sys.argv[0] = r'{ENTRY}'; runpy.run_path(r'{ENTRY}', run_name='__main__')" if frozen
               else f"import sys, runpy; sys.argv[0] = r'{ENTRY}'; runpy.run_path(r'{ENTRY}', run_name='__main__')")
        p = self.root / "jarvis"
        p.write_text(f'#!/bin/sh\nexport PYTHONPATH={path}\n{prelude}\nexec "{sys.executable}" -c "{run}" "$@"\n', encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    def gate(self, exe, *flags):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = V.main(["--exe", exe, "--timeout", "60", *flags])
        return rc, out.getvalue(), err.getvalue()

    def leftovers(self):
        return [p.name for p in self.tmp.iterdir() if p.name.startswith(V.PREFIX)]

    def test_correct_dispatch_with_a_runtime_present_passes_and_cleans_up(self):
        rc, out, err = self.gate(self.exe(fake_runtime=True))
        self.assertEqual(rc, 0, out + err)
        self.assertIn("WORKER SMOKE: PASS", out)
        self.assertIn("PASS  worker: the llama.cpp runtime imports in the packaged app", out)
        self.assertEqual(self.leftovers(), [])

    def test_a_build_without_the_runtime_passes_only_when_that_is_allowed(self):
        exe = self.exe()
        rc_ok, out_ok, _ = self.gate(exe, "--allow-no-runtime")
        self.assertEqual(rc_ok, 0, out_ok)
        rc_bad, out_bad, _ = self.gate(exe)
        self.assertEqual(rc_bad, 1)
        self.assertIn("FAIL  worker: outcome is the expected one", out_bad)

    def test_it_fails_when_the_program_does_not_report_being_frozen(self):
        rc, out, _ = self.gate(self.exe(frozen=False, fake_runtime=True))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  readiness: this is the frozen build", out)
        self.assertEqual(self.gate(self.exe(frozen=False, fake_runtime=True), "--not-frozen")[0], 0)

    def test_it_fails_cleanly_when_the_program_exits_with_an_error(self):
        rc, out, _ = self.gate(self.exe(prelude="exit 1"))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  readiness: diagnostics runs", out)
        self.assertIn("WORKER SMOKE: FAIL", out)
        self.assertEqual(self.leftovers(), [])

    def test_a_hanging_program_is_stopped_at_the_time_limit(self):
        exe = self.exe(prelude="exec sleep 60")
        t0 = time.monotonic()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = V.main(["--exe", exe, "--timeout", "1"])
        self.assertEqual(rc, 1)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertIn("no result within 1 s", out.getvalue())

    def test_a_worker_that_gives_an_answer_is_rejected(self):
        # a stand-in whose worker "succeeds": the gate must not accept an outcome that implies a model was used
        fake = self.root / "ok_worker.py"
        fake.write_text("import json, sys\n"
                        "a = sys.argv[1:]\n"
                        "d = a[a.index('--data-dir') + 1]\nrid = a[a.index('--run-id') + 1]\n"
                        "open(d + '/selftest.worker.json', 'w').write(json.dumps({'run_id': rid, 'outcome': 'ok', 'answer': '4'}))\n", encoding="utf-8")
        real = self.exe(fake_runtime=True)
        wrapper = self.root / "jarvis2"
        wrapper.write_text(f'#!/bin/sh\ncase " $* " in *" selftest-worker "*) exec "{sys.executable}" "{fake}" "$@";; esac\nexec "{real}" "$@"\n', encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        rc, out, _ = self.gate(str(wrapper))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  worker: outcome is the expected one", out)

    def test_missing_executable_and_bad_timeout_are_usage_errors(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(V.main(["--exe", str(self.root / "nope.exe")]), 2)
            self.assertEqual(V.main(["--exe", self.exe(), "--timeout", "0"]), 2)


class GateLogic(unittest.TestCase):
    def report(self, modules, missing=()):
        return json.dumps({"frozen": True, "packaged_app": {"modules": {"missing": list(missing), "modules": modules}}})

    def readiness(self, text, rc=0):
        g = V.Gate()
        with mock.patch.object(V, "run_exe", return_value=(rc, text, "")), contextlib.redirect_stdout(io.StringIO()):
            V.check_readiness(g, "x", Path("d"), 5, True)
        return g

    def test_readiness_passes_when_the_worker_module_is_present(self):
        self.assertTrue(self.readiness(self.report([{"module": V.MODULE, "present": True}])).ok)

    def test_readiness_fails_when_the_worker_module_is_absent_from_the_list_or_the_build(self):
        self.assertFalse(self.readiness(self.report([{"module": "jarvis.cli", "present": True}])).ok)
        self.assertFalse(self.readiness(self.report([{"module": V.MODULE, "present": False}], missing=[V.MODULE])).ok)

    def test_readiness_fails_on_unreadable_output(self):
        self.assertFalse(self.readiness("not json").ok)

    def test_a_started_worker_or_report_without_a_model_fails_the_cli_check(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "selftest.worker.json").write_text("{}", encoding="utf-8")
        g = V.Gate()
        with mock.patch.object(V, "run_exe", return_value=(2, "", "")), contextlib.redirect_stdout(io.StringIO()):
            V.check_cli_without_model(g, "x", tmp, 5)
        self.assertFalse(g.ok)

    def test_safe_remove_only_deletes_prefixed_folders_directly_under_the_temp_folder(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        with mock.patch.object(tempfile, "tempdir", str(base)):
            other = base / "important"
            other.mkdir()
            mine = base / (V.PREFIX + "x")
            mine.mkdir()
            nested = other / (V.PREFIX + "y")
            nested.mkdir()
            self.assertFalse(V.safe_remove(other))
            self.assertFalse(V.safe_remove(nested))
            self.assertTrue(V.safe_remove(mine))
            self.assertTrue(other.exists() and nested.exists() and not mine.exists())


class BuildScriptHook(unittest.TestCase):
    """TEXT-LEVEL ONLY: no PowerShell exists here, so the script was not parsed or run."""

    def test_the_gate_runs_after_the_exe_exists_and_before_the_installer(self):
        s = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        i_gui, i_rt = s.index("jarvis-gui.exe -ArgumentList"), s.index("jarvis.exe runtime --require")
        i_gate, i_vc, i_iscc = s.index("verify_frozen_worker.py"), s.index("check_vc_runtime.py"), s.index("/DAppVersion=")
        self.assertTrue(i_gui < i_rt < i_gate < i_vc < i_iscc)
        self.assertIn('if ($SkipLocalRuntime) { $workerArgs = " --allow-no-runtime" }', s)
        self.assertEqual(s.count("verify_frozen_worker.py"), 1)


if __name__ == "__main__":
    unittest.main()
