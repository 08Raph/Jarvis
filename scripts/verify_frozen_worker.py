"""Post-build gate for the PACKAGED selftest worker (run by scripts/build_windows.ps1 after PyInstaller, before the installer).

It starts the built jarvis.exe (a few short child processes, each with a time limit, in a throw-away data folder) and checks, WITHOUT loading any model:
  1. readiness   `jarvis.exe diagnostics` says it is a frozen build and reports jarvis.selftest_core (the worker's module) present, nothing missing;
  2. cli         `jarvis.exe selftest` with no model selected exits 2 at once and starts no worker (no report, no worker files);
  3. dispatch    `jarvis.exe selftest-worker ...` (what the supervisor runs) is dispatched to the worker, not to the GUI/setup/supervisor: it exits 0,
                 writes its result file for OUR run id, stops at "no model.gguf" (so nothing was loaded) and records the stages runtime_check and
                 loading_model. With the llama.cpp runtime present the outcome must be load_failed and the runtime must be reported available.

WHAT THIS DOES NOT PROVE: that a real model loads or answers, that inference works, that the supervising `jarvis.exe selftest` starts the packaged
worker successfully end to end (that needs a real model: docs/STAGE7_FIRST_REAL_RUN_CHECKLIST.md), or how a real native crash looks. The supervisor
never runs inference itself (the code has no such path); this script cannot observe that from outside, the Linux tests of the code do.

Standard library only. Exit code 0 = every check passed, 1 = a check failed, 2 = bad arguments. The data folder is always a new folder under the
system temp folder and is removed afterwards (never anything else)."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

PREFIX = "jarvis-worker-smoke-"
MODULE = "jarvis.selftest_core"
NO_MODEL_ID = "worker-smoke-no-such-model"
WORKER_FILES = ("selftest.json", "selftest.progress.json", "selftest.worker.json")


class Gate:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""), flush=True)
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.rows) and all(r[1] for r in self.rows)


def run_exe(exe: str, data_dir: Path, args: list[str], timeout: float):
    """(returncode, stdout, stderr) or (None, '', reason) when it could not start or ran past the limit (it is killed). Standard input is closed, as the
    supervisor does for its worker."""
    cmd = [exe, "--data-dir", str(data_dir), *args]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", f"no result within {timeout:g} s (the process was stopped)"
    except OSError as exc:
        return None, "", f"could not start ({type(exc).__name__})"
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def read_json(path: Path):
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def check_readiness(g: Gate, exe: str, d: Path, timeout: float, require_frozen: bool) -> None:
    rc, out, err = run_exe(exe, d, ["diagnostics"], timeout)
    if rc != 0:
        g.check("readiness: diagnostics runs", False, f"exit {rc}; {err.strip()[:200]}")
        return
    try:
        rep = json.loads(out)
        mods = rep["packaged_app"]["modules"]
        rows = {r["module"]: r for r in mods["modules"]}
    except (ValueError, KeyError, TypeError):
        g.check("readiness: diagnostics output is the expected report", False, "could not read packaged_app.modules")
        return
    g.check("readiness: diagnostics runs", True)
    if require_frozen:
        g.check("readiness: this is the frozen build", rep.get("frozen") is True, f"frozen={rep.get('frozen')!r}")
    row = rows.get(MODULE)
    g.check(f"readiness: {MODULE} is listed and present", bool(row and row.get("present")), "" if row else "not in the module list")
    g.check("readiness: no required module is missing", mods.get("missing") == [], f"missing={mods.get('missing')!r}")


def check_cli_without_model(g: Gate, exe: str, d: Path, timeout: float) -> None:
    rc, out, err = run_exe(exe, d, ["selftest"], timeout)
    g.check("cli: selftest with no model exits 2", rc == 2, f"exit {rc}; {err.strip()[:160]}")
    present = [n for n in WORKER_FILES if (d / n).exists()]
    g.check("cli: no worker was started and no report written", not present, f"found: {present}" if present else "")


def check_worker(g: Gate, exe: str, d: Path, timeout: float, allow_no_runtime: bool) -> None:
    run_id = uuid.uuid4().hex[:12]
    rc, out, err = run_exe(exe, d, ["selftest-worker", "--run-id", run_id, "--model-id", NO_MODEL_ID], timeout)
    if not g.check("worker: exits 0 after writing an outcome", rc == 0, f"exit {rc}; {err.strip()[:200]}"):
        return
    res = read_json(d / "selftest.worker.json")
    if not g.check("worker: result file written", res is not None):
        return
    g.check("worker: result is for this run id", res.get("run_id") == run_id, f"{res.get('run_id')!r}")
    outcome = res.get("outcome")
    runtime = res.get("runtime") if isinstance(res.get("runtime"), dict) else {}
    if outcome == "load_failed":
        g.check("worker: stopped at the missing model file (nothing was loaded)", "no model.gguf" in str(res.get("error", "")), str(res.get("error", ""))[:120])
        g.check("worker: the llama.cpp runtime imports in the packaged app", runtime.get("available") is True, f"runtime={runtime.get('available')!r} {runtime.get('error', '')}"[:200])
        want_stages = ["runtime_check", "loading_model"]
    elif outcome == "runtime_unavailable" and allow_no_runtime:
        g.check("worker: runtime reported unavailable (allowed: build without the local AI runtime)", runtime.get("available") is False)
        want_stages = ["runtime_check"]
    else:
        g.check("worker: outcome is the expected one", False, f"outcome={outcome!r}" + ("" if allow_no_runtime else " (expected load_failed)"))
        return
    g.check("worker: produced no answer and no generation", not any(k in res for k in ("answer", "generation_seconds", "output_tokens")))
    prog = read_json(d / "selftest.progress.json")
    ok_prog = prog is not None and prog.get("run_id") == run_id and prog.get("history") == want_stages and isinstance(prog.get("pid"), int)
    g.check("worker: stage history is what the code writes", ok_prog, f"{(prog or {}).get('history')!r}, expected {want_stages!r}")
    if prog:
        g.check("worker: it ran in its own process", prog.get("pid") != os.getpid(), f"pid {prog.get('pid')}")
    g.check("worker: it did not write the supervisor's report", not (d / "selftest.json").exists())


def safe_remove(d: Path) -> bool:
    """Delete the folder only if it is a direct child of the system temp folder with this script's prefix."""
    try:
        if d.parent.resolve() == Path(tempfile.gettempdir()).resolve() and d.name.startswith(PREFIX) and d.is_dir() and not d.is_symlink():
            shutil.rmtree(d)
            return True
    except OSError:
        pass
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Model-free smoke test of the packaged selftest worker.")
    ap.add_argument("--exe", required=True, help="path to the built jarvis.exe (the console executable)")
    ap.add_argument("--allow-no-runtime", action="store_true", help="the build deliberately has no llama.cpp runtime (-SkipLocalRuntime)")
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds allowed for each step (default 180)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary data folder for inspection")
    ap.add_argument("--not-frozen", action="store_true", help="TESTS ONLY: do not require that the program reports being frozen")
    a = ap.parse_args(argv)
    exe = Path(a.exe)
    if not exe.is_file():
        print(f"ERROR: not a file: {a.exe}", file=sys.stderr)
        return 2
    if not 1 <= a.timeout <= 3600:
        print("ERROR: --timeout must be between 1 and 3600", file=sys.stderr)
        return 2
    d = Path(tempfile.mkdtemp(prefix=PREFIX))
    g = Gate()
    try:
        print(f"Packaged worker smoke test (no model is loaded). Temporary data folder: {d}", flush=True)
        check_readiness(g, str(exe), d, a.timeout, require_frozen=not a.not_frozen)
        check_cli_without_model(g, str(exe), d, a.timeout)
        check_worker(g, str(exe), d, a.timeout, a.allow_no_runtime)
    finally:
        if a.keep:
            print(f"Kept: {d}")
        elif not safe_remove(d):
            print(f"note: could not remove {d}; delete it yourself", file=sys.stderr)
    print("WORKER SMOKE: " + ("PASS" if g.ok else "FAIL") + " (dispatch only; this does not prove a real model loads or answers)")
    return 0 if g.ok else 1


if __name__ == "__main__":
    sys.exit(main())
