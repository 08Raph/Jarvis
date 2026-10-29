"""Run the unit suite for CI with a HARD timeout, process-tree cleanup and diagnostics that survive in GitHub's visible logs (stdlib only).

Why: the log download of a finished run may be unreachable for the person watching, and a hung test used to end only as "timed out after 30 minutes" with no
clue which test. This runner
  * runs `unittest discover -v` in a child process (merged output goes to a log file), with faulthandler armed so a stuck run dumps every thread's stack;
  * on the hard timeout kills the WHOLE process tree (taskkill /T on Windows, the child's process group elsewhere), then reports the last lines, the best-guess
    running test and the stack dump;
  * prints failing test names, counts and skipped tests into the log, as GitHub annotations (::error / ::notice) and into the job summary;
  * decides its exit code BEFORE any reporting and never lowers it: reporting problems are printed and ignored.
Exit codes: 0 every test ran and passed (child exit 0 AND the log says OK) | 1 failures or errors | 2 not run, unreadable result or an inconsistent one (never 0)
| 3 hard timeout (tree killed) | 4 the suite could not be started.
Usage: python scripts/ci_run_tests.py --title T --log LOG --timeout SECONDS [--json OUT] [--summary FILE] [--start-dir tests] [--tail 40]"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_summarize_unittest as S  # noqa: E402

SKIPPED = re.compile(r"^(\S+ \([^)]*\)).*?\.\.\. skipped '(.*)'\s*$", re.M)
RESULT_TOKENS = re.compile(r"\.\.\. (ok|FAIL|ERROR|skipped|expected failure|unexpected success)")
EXIT_PASS, EXIT_FAILED, EXIT_NOT_RUN, EXIT_TIMEOUT, EXIT_NO_START = 0, 1, 2, 3, 4


def esc_data(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def esc_prop(s: str) -> str:
    return esc_data(s).replace(":", "%3A").replace(",", "%2C")


def annotate(level: str, title: str, message: str, limit: int = 3500) -> None:
    """A GitHub workflow command. The message is truncated (from the front, so the END of a long text is kept) and escaped."""
    if len(message) > limit:
        message = "...[cut] " + message[-limit:]
    print(f"::{level} title={esc_prop(title)}::{esc_data(message)}", flush=True)


def child_command(start_dir: str, timeout: float) -> list[str]:
    dump_after = max(5, int(timeout * 0.45))      # first stack dump well before the hard kill; repeated at the same interval
    argv = ["unittest", "discover", "-s", start_dir, "-v"] + (["--durations", "15"] if sys.version_info >= (3, 12) else [])
    code = ("import faulthandler, sys, unittest; faulthandler.dump_traceback_later(%d, repeat=True, file=sys.stderr); unittest.main(module=None, argv=%r)"
            % (dump_after, argv))
    return [sys.executable, "-u", "-c", code]


def kill_command(pid: int, windows: bool) -> list[str]:
    return ["taskkill", "/F", "/T", "/PID", str(pid)] if windows else []


def kill_tree(proc: subprocess.Popen) -> str:
    """Stop the child and everything it started. Returns a short statement of what was done and whether the child is gone."""
    how = ""
    try:
        if os.name == "nt":
            r = subprocess.run(kill_command(proc.pid, True), capture_output=True, timeout=60)
            how = f"taskkill /F /T exit {r.returncode}"
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            how = "SIGKILL to the process group"
    except Exception as exc:  # noqa: BLE001
        how = f"tree kill failed ({type(exc).__name__}); falling back to killing the child only"
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=30)
        return how + "; child exited"
    except subprocess.TimeoutExpired:
        return how + "; CHILD STILL RUNNING"


def tail_lines(path: Path, n: int, max_bytes: int = 200_000) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-n:]


def running_test(lines: list[str]) -> str:
    """Best effort: the last verbose line that has started but shows no result yet."""
    for line in reversed(lines):
        if line.rstrip().endswith("...") or (" ... " in line and not RESULT_TOKENS.search(line)):
            return line.strip()[:300]
    return "(could not tell from the output)"


def failure_blocks(text: str, keep: int = 14) -> list[tuple[str, str]]:
    """[(header, tail)] for each FAIL:/ERROR: block of unittest's final report: the header line and the LAST `keep` lines (where the assertion or
    exception message is). Safe text only: this is the test's own output. Test code that prints secrets would show up here, so none should."""
    blocks, cur = [], None
    for line in text.splitlines():
        if line.startswith("=" * 70):
            if cur:
                blocks.append(cur)
            cur = []
        elif line.startswith("-" * 70) and cur is not None and len(cur) > 1 and cur[0].startswith(("FAIL:", "ERROR:")) and line.strip() == "-" * 70 and cur[-1] == "":
            continue
        elif cur is not None:
            if line.startswith("Ran ") and line[4:5].isdigit():
                blocks.append(cur)
                cur = None
            else:
                cur.append(line)
    if cur:
        blocks.append(cur)
    out = []
    for b in blocks:
        if b and b[0].startswith(("FAIL:", "ERROR:")):
            body = [x for x in b[1:] if x.strip() and not x.startswith("-" * 20)]
            out.append((b[0], "\n".join(body[-keep:])))
    return out


def stack_dump(text: str) -> str:
    i = text.rfind("Timeout (")
    return text[i:] if i >= 0 else ""


def run_suite(start_dir: str, log: Path, timeout: float, tail: int, cwd: str | None) -> dict:
    kw: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with open(log, "wb") as lf:
        try:
            proc = subprocess.Popen(child_command(start_dir, timeout), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, cwd=cwd, **kw)
        except OSError as exc:
            return {"started": False, "error": f"{type(exc).__name__}"}
        next_beat = t0 + 120
        timed_out = False
        while proc.poll() is None:
            now = time.monotonic()
            if now - t0 > timeout:
                timed_out = True
                break
            if now >= next_beat:
                print(f"[{int(now - t0)}s] still running; last started test: {running_test(tail_lines(log, 12))}", flush=True)
                next_beat = now + 120
            time.sleep(1)
        cleanup = ""
        if timed_out:
            cleanup = kill_tree(proc)
    return {"started": True, "timed_out": timed_out, "returncode": proc.returncode, "cleanup": cleanup, "seconds": round(time.monotonic() - t0, 1)}


def decide(run: dict, parsed: dict) -> int:
    """The exit code. Only 'the child exited 0 AND the log says OK with at least one test' is success."""
    if not run.get("started"):
        return EXIT_NO_START
    if run["timed_out"]:
        return EXIT_TIMEOUT
    if parsed["label"] == "FAIL":
        return EXIT_FAILED
    if run["returncode"] == 0 and parsed["label"] == "PASS":
        return EXIT_PASS
    return EXIT_NOT_RUN


def report(title: str, run: dict, parsed: dict, code: int, log: Path, tail: int, summary: str | None) -> None:
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    lines = tail_lines(log, tail)
    skipped = [f"{n}: {r}" for n, r in SKIPPED.findall(text)]
    counts = f"ran={parsed['ran']} passed={parsed['passed']} failed={parsed['failed']} errors={parsed['errors']} skipped={parsed['skipped']}"
    label = "TIMED OUT" if code == EXIT_TIMEOUT else parsed["label"]
    print(f"{title}: {label}  {counts}  child exit={run.get('returncode')}  seconds={run.get('seconds')}  runner exit={code}", flush=True)
    for f in parsed["failing"]:
        print(f"  {f}", flush=True)
    print(f"::group::last {len(lines)} lines of the test output", flush=True)
    for line in lines:
        print(line, flush=True)
    print("::endgroup::", flush=True)
    if run.get("timed_out"):
        rt = running_test(lines)
        dump = stack_dump(text)
        print(f"TIMEOUT: killed after the hard limit. Running test (best effort): {rt}", flush=True)
        print(f"Cleanup: {run.get('cleanup')}", flush=True)
        if dump:
            print("::group::faulthandler stack dump (every thread, last dump before the kill)", flush=True)
            print(dump, flush=True)
            print("::endgroup::", flush=True)
        annotate("error", f"{title}: TIMED OUT", f"Hard limit reached; the process tree was killed ({run.get('cleanup')}). Counts so far: {counts}. Running test (best effort): {rt}\n--- last lines ---\n" + "\n".join(lines[-25:]))
        if dump:
            annotate("error", f"{title}: thread stacks at timeout", dump)
    elif code == EXIT_NO_START:
        annotate("error", f"{title}: not started", str(run.get("error")))
    elif code == EXIT_FAILED:
        chunk, size = [], 0
        for f in parsed["failing"]:
            if size + len(f) > 3000 and chunk:
                annotate("error", f"{title}: failing tests", "\n".join(chunk))
                chunk, size = [], 0
            chunk.append(f)
            size += len(f) + 1
        if chunk:
            annotate("error", f"{title}: failing tests", "\n".join(chunk))
        annotate("error", f"{title}: counts", counts)
        for head, tail_text in failure_blocks(text)[:12]:
            annotate("error", f"{title}: {head[:90]}", f"{head}\n{tail_text}", limit=1800)
    elif code == EXIT_NOT_RUN:
        annotate("error", f"{title}: NOT RUN / inconsistent result", f"{parsed.get('reason') or 'child exit ' + str(run.get('returncode')) + ' with label ' + parsed['label']}. Counts: {counts}\n--- last lines ---\n" + "\n".join(lines[-25:]))
    else:
        annotate("notice", f"{title}: PASS", counts)
    if skipped:
        annotate("notice", f"{title}: skipped tests ({len(skipped)})", "\n".join(skipped[:40]))
    if summary:
        md = S.markdown(parsed, title)
        extra = []
        if run.get("timed_out"):
            extra += [f"**Hard timeout.** Process tree killed: {run.get('cleanup')}.", f"Running test (best effort): `{running_test(lines)}`", ""]
            if stack_dump(text):
                extra += ["```", stack_dump(text)[-3500:], "```", ""]
        if code != EXIT_PASS:
            extra += ["Last output lines:", "```"] + lines[-30:] + ["```", ""]
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md + "\n".join(extra) + "\n")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="Unit tests")
    ap.add_argument("--log", required=True)
    ap.add_argument("--timeout", type=float, required=True, help="hard limit in seconds for the whole suite")
    ap.add_argument("--start-dir", default="tests")
    ap.add_argument("--cwd")
    ap.add_argument("--json")
    ap.add_argument("--summary")
    ap.add_argument("--tail", type=int, default=40)
    a = ap.parse_args(argv)
    log = Path(a.log)
    run = run_suite(a.start_dir, log, a.timeout, a.tail, a.cwd)
    try:
        parsed = S.parse(S.read_text(log)) if run.get("started") else S.parse("")
    except Exception as exc:  # noqa: BLE001
        parsed = S.parse("")
        parsed["reason"] = f"the log could not be parsed ({type(exc).__name__})"
    code = decide(run, parsed)          # decided here; nothing below can change it
    try:
        report(a.title, run, parsed, code, log, a.tail, a.summary)
        if a.json:
            Path(a.json).write_text(json.dumps({"exit": code, "run": run, "result": parsed}, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"diagnostic reporting failed ({type(exc).__name__}: {exc}); the test result above is unchanged (runner exit {code})", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
