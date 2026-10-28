"""`jarvis selftest`: load the selected model, generate one short reply, and record exactly what happened.

DESIGN (what it does and does not guarantee)
* The native model code (llama.cpp through llama-cpp-python) runs ONLY in a separate WORKER PROCESS. This process, the SUPERVISOR, never imports the
  runtime. That is the only reliable way to (a) stop a load or generation that hangs: a native call cannot be interrupted from a thread, so the
  supervisor ends the whole worker process; and (b) notice a native crash: if the worker dies (access violation, illegal instruction, out of memory
  kill) the supervisor is still alive to say so. A process cannot record its own crash after it is gone.
* `selftest.json` gets a durable "started" marker BEFORE any model is loaded and is rewritten (atomically: temporary file, flush, fsync, os.replace)
  at every stage. If the SUPERVISOR itself is killed (console closed, power loss, Task Manager) the file keeps the last stage written and
  status "running"; the next run reports that as an interrupted previous run. A hard kill of the supervisor can also leave the worker running until
  its short generation ends; this design does not prevent that.
* The pass rule is a deterministic check of the reply to a fixed arithmetic prompt (see `check_answer`). Passing shows that, on this PC, the runtime
  loaded this model file, generated text, and the text was a sensible answer to a trivial question. It does not show answer quality in general,
  speed under load, the GUI, voice, tools, or Windows installer behaviour.
* Offline: nothing here uses the network, downloads a model, or falls back to any service.
* Privacy: the fixed prompt is identified by `prompt_id` and is not logged; the reply (to that fixed prompt) is stored cut to ANSWER_REPORT_CHARS;
  native error text is reduced to its type, or scrubbed and cut, before it is stored.

Exit codes: 0 passed; 2 no usable model selected (decided by the caller); 3 integrity, runtime, load or generation failure, or an empty reply
(unchanged from earlier versions); 4 timed out (the worker was stopped); 5 the worker ended abnormally (a native crash is suspected);
6 the reply was not a valid answer; 130 cancelled by the user (Ctrl+C)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 2
REPORT_NAME = "selftest.json"
PROGRESS_NAME = "selftest.progress.json"
RESULT_NAME = "selftest.worker.json"
PROMPT_ID = "arith-2plus2-v1"
MESSAGES = [{"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is 2+2? Answer with just the number."}]
MAX_TOKENS = 48
LOAD_TIMEOUT = 600.0         # seconds from worker start until generation begins (first load of a large file from a slow disk or through antivirus can be slow)
GENERATE_TIMEOUT = 180.0     # seconds for the 48-token generation itself
ANSWER_REPORT_CHARS = 160
ANSWER_PASS_TO_SUPERVISOR_CHARS = 400
ERROR_REPORT_CHARS = 300
MAX_STAGE_HISTORY = 12

EXIT_PASSED, EXIT_NO_MODEL, EXIT_FAILED, EXIT_TIMEOUT, EXIT_CRASHED, EXIT_INVALID, EXIT_CANCELLED = 0, 2, 3, 4, 5, 6, 130

LIMITS = ("A native crash is detected only because the model runs in a separate worker process; if this supervising process is itself killed, the "
          "report keeps the last stage written and status 'running'. Passing proves one short arithmetic answer on this PC, nothing more.")

_NTSTATUS = {0xC0000005: "access violation", 0xC000001D: "illegal instruction (the CPU may lack an instruction the runtime needs)",
             0xC0000409: "fast-fail / stack buffer overrun", 0xC0000374: "heap corruption", 0xC0000017: "not enough memory",
             0xC000013A: "interrupted (Ctrl+C or console closed)", 0xC0000142: "a DLL failed to initialise", 0xC0000135: "a required DLL was not found"}
_OTHER_NUMBER_WORDS = re.compile(r"\b(zero|three|five|six|seven|eight|nine|ten|eleven|twelve|twenty)\b", re.I)
_CONTROL_TOKENS = re.compile(r"<\|[^>\n]{1,40}\|>|</?think>", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\w)")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------------------------------------------- files
def write_json_atomic(path: Path, obj) -> None:
    """Write `obj` so a reader sees the old file or the new one, never a half-written file: temporary file in the same folder, flush, fsync, replace."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2, default=str))
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:           # Windows: another program (antivirus, an editor, the window) briefly has the old file open
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cleanup_leftovers(d: Path) -> None:
    """Remove only this feature's own temporary and transient files from an earlier run."""
    for pattern in (REPORT_NAME + ".tmp-*", PROGRESS_NAME + "*", RESULT_NAME + "*"):
        for p in Path(d).glob(pattern):
            try:
                p.unlink()
            except OSError:
                pass


def previous_incomplete(path: Path) -> dict | None:
    """What an earlier report says about a run that never finished. None when there is no report or it ended normally (any final status)."""
    p = Path(path)
    if not p.exists():
        return None
    old = _read_json(p)
    if not isinstance(old, dict):
        return {"status": "unreadable", "note": "The previous selftest.json could not be read; it was replaced."}
    if old.get("status") == "running":
        return {"status": "interrupted", "run_id": old.get("run_id"), "last_stage": old.get("stage"),
                "started_at": old.get("started_at"), "last_update": old.get("updated_at"),
                "note": "An earlier self-test started but never finished (the process or the PC stopped, or it crashed). Its last stage is recorded here."}
    return None


# ---------------------------------------------------------------------------------------------------------------- the check
def check_answer(text) -> tuple[bool, str]:
    """Deterministic validity check for the reply to 'What is 2+2? Answer with just the number.' It does not depend on exact wording or on one chat template.
    VALID: a short reply (at most 200 characters) that gives the number 4 (as a numeral, optionally 4.0, or the word 'four') and mentions no number
    other than the operand 2 and the result 4 (digits or number words), and contains no chat-template control tokens or reasoning tags.
    INVALID: empty, runaway/long, control tokens such as <|im_start|> or <think>, a wrong number, or no 4 at all. 'The answer is 4.', '4', '2+2=4' and
    'Four' are valid; '14', '5', '4 or 5' and 'I am not sure' are not. It judges form and the single correct fact, not style."""
    t = (text or "").strip()
    if not t:
        return False, "empty"
    if len(t) > 200:
        return False, "too long for a one-number answer"
    if _CONTROL_TOKENS.search(t):
        return False, "contains chat-template control tokens or reasoning tags"
    values = {float(x) for x in _NUMBER.findall(t)}
    if values - {2.0, 4.0} or _OTHER_NUMBER_WORDS.search(t):
        return False, "mentions a number other than 2 and 4"
    if 4.0 in values or re.search(r"\bfour\b", t, re.I):
        return True, "ok"
    return False, "does not give the number 4"


def describe_exit(rc) -> str:
    if rc is None:
        return "no exit code"
    if rc < 0:
        return f"terminated by signal {-rc}"
    if rc >= 0x80000000:
        name = _NTSTATUS.get(rc)
        return f"Windows status 0x{rc:08X}" + (f" ({name})" if name else "")
    return f"exit code {rc}"


# ---------------------------------------------------------------------------------------------------------------- the worker (runs in its own process)
def run_worker(cfg, store, run_id: str, model_id: str) -> int:
    """Entry of the hidden `selftest-worker` command. Loads the model and generates once. It reports each stage to the supervisor through small
    files and returns 0 whenever it could write an outcome (success or a handled failure). Anything else, including a native crash, shows up
    to the supervisor as an abnormal exit or a missing result."""
    d = Path(cfg.data_dir)

    history: list[str] = []

    def progress(stage: str) -> None:
        history.append(stage)          # the whole list is kept so the supervisor records even a stage that was over between two of its polls
        write_json_atomic(d / PROGRESS_NAME, {"run_id": run_id, "stage": stage, "history": list(history), "pid": os.getpid(), "at": now_iso()})

    def finish(**fields) -> int:
        write_json_atomic(d / RESULT_NAME, {"run_id": run_id, **fields})
        return 0

    eng = None
    out: dict = {}
    try:
        from jarvis.disclosure import scrub_error_text
        from jarvis.inference.gguf_engine import GGUFEngine, RuntimeUnavailable, runtime_info
        progress("runtime_check")
        rt = runtime_info()
        out["runtime"] = rt
        if not rt.get("available"):
            return finish(outcome="runtime_unavailable", error=scrub_error_text(str(rt.get("error", "")), ERROR_REPORT_CHARS), **out)
        progress("loading_model")
        try:
            eng = GGUFEngine(store.path(model_id))
        except RuntimeUnavailable as exc:
            msg = scrub_error_text(str(exc), ERROR_REPORT_CHARS)
            return finish(outcome="runtime_unavailable" if "not available" in msg else "load_failed", error=msg, **out)
        except Exception as exc:  # noqa: BLE001
            return finish(outcome="load_failed", error=f"{type(exc).__name__}: " + scrub_error_text(str(exc), ERROR_REPORT_CHARS), **out)
        out.update(load_seconds=round(eng.load_seconds, 2), gpu_layers_requested=eng.gpu_layers_requested)
        progress("generating")
        t0 = time.perf_counter()
        try:
            text = eng.chat(MESSAGES, max_tokens=MAX_TOKENS, temperature=0.0)
        except Exception as exc:  # noqa: BLE001
            return finish(outcome="generation_failed", error=f"{type(exc).__name__} while generating", **out)
        dt = time.perf_counter() - t0
        n = eng._count(text) if text else 0
        return finish(outcome="ok", answer=(text or "")[:ANSWER_PASS_TO_SUPERVISOR_CHARS], generation_seconds=round(dt, 2), output_tokens=n,
                      tokens_per_second=round(n / dt, 2) if dt > 0 and n else None,
                      truncated=bool((getattr(eng, "diag", {}) or {}).get("truncated")), **out)
    except Exception as exc:  # noqa: BLE001 - never leave the supervisor guessing about a Python-level failure
        try:
            return finish(outcome="worker_error", error=f"{type(exc).__name__}", **out)
        except Exception:  # noqa: BLE001
            return 1
    finally:
        closer = getattr(eng, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass


def _child_env() -> dict:
    """The worker must run THIS copy of the program (not whichever `jarvis` another install on the PATH provides). When running from source, put the folder
    that contains the package first on its import path. In a PyInstaller build the executable carries everything and nothing is added."""
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def worker_command(cfg, run_id: str, model_id: str) -> list[str]:
    """How the supervisor starts the worker: this program again with the hidden `selftest-worker` command. In a PyInstaller build sys.executable IS the program."""
    base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "jarvis"]
    return base + ["--data-dir", str(cfg.data_dir), "selftest-worker", "--run-id", run_id, "--model-id", model_id]


# ---------------------------------------------------------------------------------------------------------------- the supervisor
class _Report:
    """Owns selftest.json. The first write must succeed (no durable marker, no model load); later write failures are noted and tolerated."""
    def __init__(self, path: Path, rec: dict):
        self.path, self.rec, self.write_failed = Path(path), rec, False

    def flush(self, strict: bool = False) -> None:
        self.rec["updated_at"] = now_iso()
        try:
            write_json_atomic(self.path, self.rec)
        except OSError:
            if strict:
                raise
            self.write_failed = True

    def stage(self, name: str, status: str = "running") -> None:
        hist = self.rec.setdefault("stages", [])
        hist.append({"stage": name, "status": status, "at": now_iso()})
        del hist[:-MAX_STAGE_HISTORY]
        self.rec["stage"] = name
        self.flush()


def _scrub(text: str) -> str:
    """Remove the home folder, the user name and secret-looking text from a message that will be stored in the shareable report."""
    try:
        from jarvis.app.diagnostics import scrub_paths
        return scrub_paths(text)
    except Exception:  # noqa: BLE001
        return text


def _stderr_tail(f) -> str:
    try:
        f.flush()
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - 1200))
        raw = f.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    return " ".join(_scrub(raw).split())[-ERROR_REPORT_CHARS:]


def _merge_progress(prog, run_id: str, seen: list[str], rep: "_Report") -> str | None:
    """Record every stage the worker has reported that has not been recorded yet, in order. Returns the newest stage, or None if there is nothing new."""
    if not (isinstance(prog, dict) and prog.get("run_id") == run_id):
        return None
    names = [x for x in (prog.get("history") or []) if isinstance(x, str)] or ([prog["stage"]] if isinstance(prog.get("stage"), str) else [])
    newest = None
    for name in names[len(seen):]:
        seen.append(name)
        rep.stage(name)
        newest = name
    return newest


def _kill(proc) -> bool:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=15)
        return True
    except Exception:  # noqa: BLE001
        return proc.poll() is not None


def run_selftest(cfg, store, model_id: str, *, load_timeout: float = LOAD_TIMEOUT, generate_timeout: float = GENERATE_TIMEOUT,
                 poll: float = 0.2, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Run the self-test of `model_id` (already integrity-checked by the caller) and return the final report dict (also saved as selftest.json).
    Raises OSError only if the started marker cannot be written; nothing is loaded in that case."""
    from jarvis.version import __version__
    d = Path(cfg.data_dir)
    path = d / REPORT_NAME
    prev = previous_incomplete(path)
    cleanup_leftovers(d)
    try:
        sha = json.loads((store.path(model_id) / "meta.json").read_text(encoding="utf-8")).get("package_sha256")
    except (OSError, ValueError, AttributeError):
        sha = None
    run_id = uuid.uuid4().hex[:12]
    started = now_iso()
    rec = {"schema": SCHEMA, "run_id": run_id, "status": "running", "stage": "started", "stages": [], "started_at": started, "updated_at": started,
           "time": started, "model_id": model_id, "model_sha256": sha, "app_version": __version__, "prompt_id": PROMPT_ID, "passed": False,
           "exit_code": None, "error": "This self-test started but has not finished (it is still running, or it was interrupted).",
           "timeouts_seconds": {"load": load_timeout, "generate": generate_timeout}, "limits": LIMITS}
    if prev:
        rec["previous_run"] = prev
    rep = _Report(path, rec)
    rep.flush(strict=True)                         # the durable started marker: written BEFORE any worker or model exists
    rep.stage("started")

    result: dict | None = None
    status, kind, message, code = "failed", "unknown", "", EXIT_FAILED
    stage_seen = "starting_worker"
    worker_stages: list[str] = []
    stderr_f = tempfile.TemporaryFile()
    proc = None
    t_start = clock()
    try:
        rep.stage("starting_worker")
        cmd = worker_command(cfg, run_id, model_id)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr_f, creationflags=flags, env=_child_env())
        except OSError as exc:
            kind, message = "worker_start_failed", f"The test process could not be started ({type(exc).__name__})."
        else:
            gen_t0 = None
            rc = None
            while True:
                rc = proc.poll()
                newest = _merge_progress(_read_json(d / PROGRESS_NAME), run_id, worker_stages, rep)
                if newest:
                    stage_seen = newest
                    if "generating" in worker_stages and gen_t0 is None:
                        gen_t0 = clock()
                if rc is not None:
                    break
                if gen_t0 is None and clock() - t_start > load_timeout:
                    status, kind, code = "timeout", "timeout", EXIT_TIMEOUT
                    message = f"No generation started within {load_timeout:g} seconds (last stage: {stage_seen}). The test process was stopped."
                elif gen_t0 is not None and clock() - gen_t0 > generate_timeout:
                    status, kind, code = "timeout", "timeout", EXIT_TIMEOUT
                    message = f"Generation did not finish within {generate_timeout:g} seconds. The test process was stopped."
                if status == "timeout":
                    if not _kill(proc):
                        message += " (The process could not be confirmed stopped; check Task Manager.)"
                    break
                sleep(poll)
            if status != "timeout":
                newest = _merge_progress(_read_json(d / PROGRESS_NAME), run_id, worker_stages, rep)     # the worker may have advanced after the last poll
                if newest:
                    stage_seen = newest
                result = _read_json(d / RESULT_NAME)
                if not (isinstance(result, dict) and result.get("run_id") == run_id and isinstance(result.get("outcome"), str)):
                    result = None
                if rc != 0 or result is None:
                    status, kind, code = "crashed", "worker_crashed" if rc != 0 else "worker_no_result", EXIT_CRASHED
                    tail = _stderr_tail(stderr_f)
                    message = (f"The test process ended abnormally ({describe_exit(rc)}) in stage '{stage_seen}'; a native crash in the runtime or model "
                               "is suspected." if rc != 0 else f"The test process ended without reporting a result (stage '{stage_seen}').")
                    if rc != 0 and result is not None:
                        message += " It had reported a result first."
                    if tail:
                        rec["worker_stderr_tail"] = tail
                    rec["worker_exit"] = describe_exit(rc)
    except KeyboardInterrupt:
        status, kind, code, message = "cancelled", "cancelled", EXIT_CANCELLED, f"Cancelled by the user in stage '{stage_seen}'."
        if proc is not None:
            _kill(proc)
        result = None
    finally:
        try:
            stderr_f.close()
        except OSError:
            pass

    if result is not None and status not in ("timeout", "crashed", "cancelled"):
        rt = result.get("runtime")
        if isinstance(rt, dict):
            rec["runtime"] = rt
        for k in ("load_seconds", "gpu_layers_requested", "generation_seconds", "output_tokens", "tokens_per_second", "truncated"):
            if k in result:
                rec[k] = result[k]
        outcome = result["outcome"]
        if outcome == "ok":
            rep.stage("validating_output")
            ans = str(result.get("answer") or "")
            ok, why = check_answer(ans)
            rec["answer"] = ans[:ANSWER_REPORT_CHARS]
            rec["answer_mentions_4"] = "4" in ans
            rec["answer_check"] = {"name": PROMPT_ID, "ok": ok, "reason": why}
            if ok:
                status, kind, code, message = "passed", "", EXIT_PASSED, ""
            elif why == "empty":
                status, kind, code, message = "failed", "empty_output", EXIT_FAILED, "The model produced no text."
            else:
                status, kind, code, message = "failed", "invalid_output", EXIT_INVALID, f"The reply was not a valid answer to the test question ({why})."
        else:
            status, kind, code = "failed", outcome, EXIT_FAILED
            message = _scrub(str(result.get("error") or outcome))[:ERROR_REPORT_CHARS]

    rec.update(status=status, passed=(status == "passed"), exit_code=code, elapsed_seconds=round(clock() - t_start, 2), finished_at=now_iso())
    if rec["passed"]:
        rec.pop("error", None)
        rec["stage"] = "finished"
        rec["failure"] = None
    else:
        failed_stage = "validating_output" if kind in ("invalid_output", "empty_output") else stage_seen
        rec["stage"] = failed_stage
        rec["error"] = message or kind
        rec["failure"] = {"kind": kind, "stage": failed_stage, "message": message}
    rep.stage(rec["stage"], status)
    if rep.write_failed:
        rec["report_note"] = "Some report updates could not be written to disk."
    for name in (PROGRESS_NAME, RESULT_NAME):
        try:
            (d / name).unlink()
        except OSError:
            pass
    rep.flush()
    return rec
