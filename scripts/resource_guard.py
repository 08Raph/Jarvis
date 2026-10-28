"""Stage 5: run ONE command (for example a benchmark) under resource limits, and stop it when a limit is hit.

This is the only place where the experiment's RAM and swapping stop conditions are enforced by code - and ONLY for commands started through this guard.
Nothing else in Jarvis enforces them. Limits (all checked about once a second, written to the log):
  * peak memory of the command and all its child processes (resident set, summed) above --max-rss-gb (default 13, read as 13 x 10^9 bytes = stricter than 13 GiB)
    -> stop at once;
  * memory pressure: system-wide available RAM below --min-available-gb (default 1.0) for --sustain samples in a row, or swap/page-file use growing by more than
    --swap-growth-gb (default 0.5) above its level at start for --sustain samples in a row -> stop. This is a HEURISTIC for "sustained swapping or severe slowdown":
    on Windows psutil reports page-file use, not paging rates;
  * wall time above --max-seconds -> stop;
  * --min-free-gb at start (default 25, with --free-path): too little free disk -> the command is NOT started.
If psutil is missing or a limit cannot be read the guard refuses to run (fail closed). It stops a command by terminating its process tree (then killing it after 5 s).

WHAT THIS DOES NOT GUARANTEE (read before relying on it):
  * Memory is the sum of the RESIDENT SET (working set) of the command and all its descendants, sampled every --interval seconds (default 1 s). It is not the
    commit charge; pages shared between processes or mapped from the model file may be counted more than once; a spike that starts and ends between two samples,
    or one large allocation that crosses the limit within a single interval, is not seen in time. A process or child that cannot be read is skipped (undercount);
    if the command itself cannot be read for --sustain samples in a row the guard stops it with MEASUREMENT_FAILED instead of running unmeasured.
  * "Available RAM" and "swap/page-file use" are SYSTEM-WIDE readings, so other programs can trigger them. They are a heuristic for "sustained swapping or severe
    slowdown", not a measurement of it; tokens/s is not watched.
  * Free disk is read ONCE, before the command starts. Nothing watches disk use while it runs.
  * Stopping is a hard kill (TerminateProcess on Windows). The command gets no chance to clean up: partial files, temporary or staging folders and half-written
    JSON results can remain. Use the guard for benchmark/perf commands. Do NOT wrap `models fetch` or `models add-gguf` in it: after any stop, run
    `models list` and `models verify` and remove leftovers by hand.
  * The guard's own exit code can equal the command's (a command that itself exits 7 or 8): the log's started / stop_reason / command_exit_code say which it was.
  * The log is written when the command starts (finished=false) and rewritten when it ends. finished=false in a log means the guard itself died.

Exit codes: the command's own exit code when it finished by itself (so 0/3/4 of the benchmarks pass through); 7 = stopped by the guard (see stop_reason in the log);
8 = the guard refused to start the command (missing psutil, bad or non-finite limits, not enough free disk, command not startable); 2 = an option was misspelled or not a number (argparse; the command was not started).

Usage: python scripts/resource_guard.py --log C:\\jarvis-eval\\out\\guard_bench-intel_qwen3-8b.json --free-path C:\\ -- python -m jarvis --data-dir C:\\jarvis-eval\\data bench-intel --model <id> ...
Linux-tested only with a fake sampler and a real psutil on Linux; behaviour on Windows has not been observed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

GB = 1_000_000_000
EXIT_STOPPED, EXIT_REFUSED = 7, 8


class GuardUnavailable(Exception):
    pass


class PsutilProvider:
    def __init__(self):
        try:
            import psutil
        except ImportError:
            raise GuardUnavailable("psutil is not installed: cannot measure memory, refusing to run without the guard") from None
        self.ps = psutil

    def tree_rss(self, pid: int) -> int | None:
        try:
            root = self.ps.Process(pid)
            procs = [root] + root.children(recursive=True)
            total = root.memory_info().rss          # the command itself must be readable; None below means "could not measure"
        except self.ps.Error:
            return None
        procs = procs[1:]
        for p in procs:
            try:
                total += p.memory_info().rss
            except self.ps.Error:
                pass
        return total

    def available(self) -> int:
        return int(self.ps.virtual_memory().available)

    def swap_used(self) -> int:
        return int(self.ps.swap_memory().used)

    def free_disk(self, path: str) -> int:
        return int(shutil.disk_usage(path).free)

    def kill_tree(self, pid: int) -> None:
        try:
            root = self.ps.Process(pid)
            procs = root.children(recursive=True) + [root]
        except self.ps.Error:
            return
        for p in procs:
            try:
                p.terminate()
            except self.ps.Error:
                pass
        _, alive = self.ps.wait_procs(procs, timeout=5)
        for p in alive:
            try:
                p.kill()
            except self.ps.Error:
                pass


def _finite_positive(*values) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in values)


def run_guard(cmd: list[str], log: Path | None, provider=None, max_rss_gb: float = 13.0, min_available_gb: float = 1.0, swap_growth_gb: float = 0.5,
              sustain: int = 3, max_seconds: float | None = None, min_free_gb: float | None = 25.0, free_path: str | None = None,
              interval: float = 1.0, popen=subprocess.Popen, sleep=time.sleep, clock=time.monotonic) -> int:
    sane = _finite_positive(max_rss_gb, min_available_gb, swap_growth_gb) and sustain >= 1 and interval > 0 and math.isfinite(interval) \
        and (max_seconds is None or _finite_positive(max_seconds)) and (min_free_gb is None or _finite_positive(min_free_gb))
    if not sane:      # NaN, infinity, zero or negative limits would silently disable a check: refuse instead
        max_rss_gb = min_available_gb = swap_growth_gb = 1.0
        min_free_gb = 1.0 if min_free_gb is not None else None
    rec = {"kind": "resource-guard", "finished": False, "started_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "command": f"{Path(cmd[0]).name} (+{len(cmd) - 1} arguments; not logged: they may contain paths)" if cmd else "",
           "limits": {"max_rss_bytes": int(max_rss_gb * GB), "min_available_bytes": int(min_available_gb * GB), "swap_growth_bytes": int(swap_growth_gb * GB),
                      "sustain_samples": sustain, "max_seconds": max_seconds, "min_free_bytes_at_start": int(min_free_gb * GB) if min_free_gb is not None else None},
           "enforced_by_this_guard": ["peak process-tree memory", "low available RAM (sustained)", "swap/page-file growth (sustained)", "wall time", "unreadable command (MEASUREMENT_FAILED)", "free disk at start (once)"],
           "stop_reason": None, "started": False}

    def write() -> None:
        if log is not None:
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(json.dumps(rec, indent=2), encoding="utf-8")
            except OSError:
                pass            # an unwritable log must not leave the command running unguarded or hide its exit code

    def finish(code: int) -> int:
        rec["guard_exit_code"] = code
        rec["finished"] = True
        write()
        return code

    if not cmd or not sane:
        rec["refused"] = "invalid arguments (every limit must be a finite number above zero)"
        return finish(EXIT_REFUSED)
    try:
        provider = provider or PsutilProvider()
        if min_free_gb is not None:
            if not free_path:
                raise GuardUnavailable("--free-path is required with --min-free-gb")
            free = provider.free_disk(free_path)
            rec["free_disk_bytes_at_start"] = free
            if free < min_free_gb * GB:
                rec["refused"] = f"free disk {free} bytes is below the required {int(min_free_gb * GB)}: command not started"
                return finish(EXIT_REFUSED)
        base_swap = provider.swap_used()
        provider.available()
    except (GuardUnavailable, OSError, ValueError) as exc:
        rec["refused"] = str(exc)
        return finish(EXIT_REFUSED)
    except Exception as exc:        # noqa: BLE001 - any failure to measure means we cannot guard
        rec["refused"] = f"cannot read system limits: {type(exc).__name__}"
        return finish(EXIT_REFUSED)
    try:
        proc = popen(cmd)
    except OSError:
        rec["refused"] = "the command could not be started"
        return finish(EXIT_REFUSED)
    rec["started"] = True
    write()         # evidence exists even if the guard itself is killed; finished=false marks that
    t0 = clock()
    peak = samples = low_n = swap_n = blind_n = 0
    min_avail = None
    max_swap = base_swap
    reason = None
    try:
        while proc.poll() is None:
            samples += 1
            rss, avail, swap = provider.tree_rss(proc.pid), provider.available(), provider.swap_used()
            blind_n = blind_n + 1 if rss is None else 0
            rss = rss or 0
            peak, max_swap = max(peak, rss), max(max_swap, swap)
            min_avail = avail if min_avail is None else min(min_avail, avail)
            low_n = low_n + 1 if avail < rec["limits"]["min_available_bytes"] else 0
            swap_n = swap_n + 1 if swap - base_swap > rec["limits"]["swap_growth_bytes"] else 0
            if rss > rec["limits"]["max_rss_bytes"]:
                reason = "PEAK_MEMORY_LIMIT_EXCEEDED"
            elif low_n >= sustain:
                reason = "LOW_AVAILABLE_MEMORY_SUSTAINED"
            elif swap_n >= sustain:
                reason = "SWAP_GROWTH_SUSTAINED"
            elif blind_n >= sustain and proc.poll() is None:
                reason = "MEASUREMENT_FAILED"           # the command cannot be read (for example access denied): running it unmeasured would defeat the guard
            elif max_seconds is not None and clock() - t0 > max_seconds:
                reason = "TIME_LIMIT_EXCEEDED"
            if reason:
                provider.kill_tree(proc.pid)
                try:
                    proc.wait(timeout=10)
                except Exception:       # noqa: BLE001 - recorded below
                    pass
                break
            sleep(interval)
    except BaseException:
        provider.kill_tree(proc.pid)        # never leave the command running if the guard itself is interrupted
        rec["stop_reason"] = "GUARD_INTERRUPTED"
        rec["peak_tree_rss_bytes"] = peak
        finish(EXIT_STOPPED)
        raise
    rec.update({"samples": samples, "peak_tree_rss_bytes": peak, "min_available_bytes": min_avail, "swap_used_at_start_bytes": base_swap,
                "max_swap_used_bytes": max_swap, "seconds": round(clock() - t0, 2), "stop_reason": reason})
    if reason:
        rec["command_exit_code"] = None         # the command was terminated by the guard; its own exit status is not meaningful
        return finish(EXIT_STOPPED)
    rec["command_exit_code"] = proc.returncode
    return finish(proc.returncode if proc.returncode is not None else EXIT_STOPPED)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="resource_guard.py", usage="resource_guard.py [options] -- command [args...]", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=Path, help="JSON log of the guarded run (written at start, rewritten at the end)")
    ap.add_argument("--max-rss-gb", type=float, default=13.0)
    ap.add_argument("--min-available-gb", type=float, default=1.0)
    ap.add_argument("--swap-growth-gb", type=float, default=0.5)
    ap.add_argument("--sustain", type=int, default=3)
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--min-free-gb", type=float, default=25.0)
    ap.add_argument("--free-path", help="drive or folder whose free space is checked before starting")
    ap.add_argument("--interval", type=float, default=1.0)
    if "--" not in argv:
        if any(x in ("-h", "--help") for x in argv):
            ap.parse_args(["--help"])           # prints the full help and exits 0
        print("usage: resource_guard.py [options] -- command [args...]   (the command goes after a bare --; use --help for the options)", file=sys.stderr)
        return EXIT_REFUSED
    i = argv.index("--")
    opts, cmd = argv[:i], argv[i + 1:]
    a = ap.parse_args(opts)
    code = run_guard(cmd, a.log, None, a.max_rss_gb, a.min_available_gb, a.swap_growth_gb, a.sustain, a.max_seconds, a.min_free_gb, a.free_path, a.interval)
    if code in (EXIT_STOPPED, EXIT_REFUSED):
        print(f"resource_guard: exit {code} - see the log" + (f" {a.log}" if a.log else ""), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
