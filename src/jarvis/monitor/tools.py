"""Monitoring tools (category `monitoring`, read-only). They measure only when called; there is no background collector."""
from __future__ import annotations

import statistics

from jarvis.desktop import schema as S
from jarvis.desktop.schema import Arg, Outcome, ToolSpec
from jarvis.monitor import system as M
from jarvis.monitor.readings import Reading


def monitor_for(env) -> M.Monitor:
    mon = env.ext.get("monitor")
    cfg = M.MonitorConfig.load(env.profile_dir)
    if mon is None:
        mon = env.ext["monitor"] = M.Monitor(env.ext.get("monitor_source"), cfg)
    mon.config = cfg                       # the user's latest choices (file edited by the command line only)
    return mon


def _summary(rs: list[Reading]) -> str:
    n_un = sum(r.kind == "unavailable" for r in rs)
    return f"{len(rs) - n_un} reading(s)" + (f", {n_un} unavailable (see the reason on each)" if n_un else "")


def _outcome(msg, rs, extra=None, text="") -> Outcome:
    data = {"readings": [r.to_dict() for r in rs], **(extra or {})}
    ok = any(r.kind != "unavailable" for r in rs)
    return Outcome(ok, f"{msg}: {_summary(rs)}", verified=True if ok else None,
                   evidence="read from the operating system / drivers just now; each reading says its source, unit and time" if ok else "no source could answer",
                   data=data, untrusted_text=text or "\n".join(r.line() for r in rs))


def _parts(a):
    parts = a["parts"] or None
    bad = [p for p in parts or [] if p not in M.PARTS]
    if bad:
        raise S.ArgError(f"unknown part(s) {bad}; choose from {list(M.PARTS)}")
    return parts


def h_snapshot(a, env) -> Outcome:
    mon = monitor_for(env)
    rs, meta = mon.snapshot(_parts(a))
    return _outcome("system readings", rs, {"cached": meta["cached"], "not_selected": meta["not_selected"], "min_interval_s": mon.config.interval_s})


def h_processes(a, env) -> Outcome:
    mon = monitor_for(env)
    if "cpu" not in mon.config.parts and "memory" not in mon.config.parts:
        return Outcome(False, "process information is not among the metrics you chose (jarvis monitor config)", verified=None, evidence="")
    rs, rows = M.process_readings(mon.src, a["limit"], a["sort"])
    text = "\n".join(f"{r['name']} (pid {r['pid']}): cpu {r['cpu_percent']} %, memory {r['memory_mb']} MB" for r in rows)
    out = _outcome("top processes", rs, {"processes": rows, "sort": a["sort"]}, text)
    out.summary += " - process names are outside text and are shown as data"
    return out


def h_self(a, env) -> Outcome:
    return _outcome("Jarvis' own usage", M.own_readings(monitor_for(env).src))


def h_watch(a, env) -> Outcome:
    mon = monitor_for(env)
    series = mon.watch(_parts(a), a["interval"], a["samples"], stop=env.stop.is_set)
    env.stop.check()
    flat = {}
    for rs in series:
        for r in rs:
            if r.kind == "measured" and isinstance(r.value, (int, float)):
                flat.setdefault((r.name, r.unit), []).append(r.value)
    stats = [{"name": k[0], "unit": k[1], "samples": len(v), "min": min(v), "max": max(v), "mean": round(statistics.fmean(v), 3)} for k, v in flat.items()]
    last = series[-1] if series else []
    out = _outcome(f"{len(series)} sample(s)", last, {"samples_taken": len(series), "statistics": stats,
                                                    "series": [[r.to_dict() for r in rs] for rs in series]})
    out.evidence += "; min/max/mean are DERIVED from those measurements"
    return out


PARTS_ARG = Arg("parts", "list[str]", "any of cpu, memory, disk, gpu, temperature (default: the ones you chose)", required=False, default=None, max_len=20, max_items=5)


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("monitor_snapshot", "read CPU, memory, disk, GPU and temperature readings with units, time and source (read-only)", "monitoring", S.OBSERVE,
                 (PARTS_ARG,), h_snapshot, timeout=40),
        ToolSpec("monitor_processes", "list the busiest processes by CPU or memory: name, pid, CPU %, memory MB (read-only; cannot end them)", "monitoring", S.OBSERVE,
                 (Arg("limit", "int", required=False, default=10, min=1, max=M.MAX_PROCESSES), Arg("sort", "str", required=False, default="cpu", choices=("cpu", "memory"))),
                 h_processes, timeout=40, untrusted_output=True),
        ToolSpec("monitor_self", "Jarvis' own CPU, memory and thread usage (read-only)", "monitoring", S.OBSERVE, (), h_self, timeout=20),
        ToolSpec("monitor_watch", "take a bounded series of readings (at most 60 samples / 5 minutes, interval 1-300 s) and report min/max/mean", "monitoring", S.OBSERVE,
                 (PARTS_ARG, Arg("interval", "float", required=False, default=5.0, min=M.MIN_INTERVAL, max=M.MAX_INTERVAL),
                  Arg("samples", "int", required=False, default=5, min=1, max=M.MAX_WATCH_SAMPLES)), h_watch, timeout=330),
    ]
