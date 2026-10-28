"""Stage 5: combine the JSON files produced by the benchmarks into ONE comparison report with the four result families kept apart.

Reads (from one or more folders): intel_*.json (bench-intel), sec_*.json (bench-security), perf*/perf.json or perf.json (perf), guard_*.json (resource_guard),
download_verification_*.json (model_metadata.json is not needed here). It computes nothing new about a model: it copies recorded numbers, labels them, and REFUSES to treat a run
as a result unless it is complete, valid and not simulated (everything else is listed under EXCLUDED with the reason).

Report sections (never merged into one score; there is deliberately NO overall "intelligence rating"):
  A. Functional correctness (bench-intel: exact+rubric checks, greedy pass rate with its 95 % Wilson interval, per-category rates)
  B. Language quality: NOT MEASURED by any command here (the rubric checks are keyword heuristics, shown as such; there is no human or model judge)
  C. Tool safety and tool-call quality (bench-intel tool_use counters; bench-security raw/app gates)
  D. Performance and resources (perf: load time, time to first token, sustained and overall tokens/s, peak process memory; guard logs: peak process-tree memory, stop reasons)
Comparability: results are only called comparable if suite hash, token limit, prompt variant, sampling profile, thinking mode, OS/CPU/threads and runtime version agree;
every difference is listed. Hardware/power settings that no file records (power plan, plugged in, thermals, background load) must be written in the session notes by hand.

Usage: python scripts/summarize_stage5_results.py --in C:\\jarvis-eval\\out --in C:\\jarvis-eval\\meta --out C:\\jarvis-eval\\summary
Exit codes: 0 report written; 4 nothing usable was found or files were unreadable (still written, flagged).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

COMPARE_FIELDS = (("suite_sha256", lambda r: r.get("suite_sha256")), ("max_tokens", lambda r: (r.get("settings") or {}).get("max_tokens_default")),
                  ("prompt_variant", lambda r: (r.get("settings") or {}).get("prompt_variant")),
                  ("sampling_profile", lambda r: (r.get("settings") or {}).get("sampling_profile")),
                  ("thinking_mode", lambda r: (r.get("settings") or {}).get("thinking_mode")),
                  ("sampled_runs", lambda r: (r.get("settings") or {}).get("sampled_runs")),
                  ("os", lambda r: (r.get("environment") or {}).get("os")), ("processor", lambda r: (r.get("environment") or {}).get("processor")),
                  ("cpu_threads", lambda r: (r.get("environment") or {}).get("cpu_threads")),
                  ("runtime_version", lambda r: ((r.get("environment") or {}).get("runtime") or {}).get("version")))


PERF_FIELDS = (("max_tokens", lambda r: (r.get("settings") or {}).get("max_tokens")), ("warmup_runs", lambda r: (r.get("settings") or {}).get("warmup_runs")),
               ("measured_runs", lambda r: (r.get("settings") or {}).get("measured_runs")),
               ("prompt_and_system", lambda r: json.dumps([(r.get("settings") or {}).get("prompt"), (r.get("settings") or {}).get("system")]) if (r.get("settings") or {}).get("prompt") else None),
               ("context_tokens", lambda r: (r.get("execution") or {}).get("n_ctx")), ("mode", lambda r: (r.get("execution") or {}).get("mode")),
               ("os", lambda r: (r.get("environment") or {}).get("os")), ("cpu_name", lambda r: (r.get("environment") or {}).get("cpu_name")),
               ("cpu_logical_threads", lambda r: (r.get("environment") or {}).get("cpu_logical_threads")), ("ram_gb", lambda r: (r.get("environment") or {}).get("ram_gb")),
               ("runtime_version", lambda r: ((r.get("environment") or {}).get("runtime") or {}).get("version")))


def missing(v) -> bool:
    return v is None or v == ""


def model_trace(d: dict) -> dict:
    m = d.get("model") or {}
    return {"model_id": m.get("id"), "model_sha256": m.get("sha256"), "model_file_size_bytes": m.get("file_size_bytes")}


def load(folders: list[Path]) -> tuple[list[tuple[str, dict]], list[str]]:
    docs, bad = [], []
    seen = set()
    for f in folders:
        for p in sorted(f.rglob("*.json")):
            try:
                if p in seen or p.stat().st_size > 50_000_000:
                    continue
            except OSError:
                bad.append(p.name)
                continue
            seen.add(p)
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                bad.append(p.name)
                continue
            if isinstance(d, dict):
                docs.append((p.name, d))
    return docs, bad


def usable(d: dict) -> tuple[bool, str]:
    if d.get("simulated"):
        return False, "SIMULATED run (fake engine), not a model result"
    st = d.get("status")
    if st != "complete":
        return False, f"status is '{st}', only complete runs are results"
    return True, ""


def model_label(d: dict) -> str:
    m = d.get("model") or {}
    return str(d.get("label") or m.get("id") or "?")


def pct(x) -> str:
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "n/a"


def summarize(docs: list[tuple[str, dict]], bad: list[str], now=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")) -> dict:
    intel = [(n, d) for n, d in docs if d.get("kind") == "intelligence-benchmark"]
    sec = [(n, d) for n, d in docs if d.get("kind") == "security-benchmark"]
    perf = [(n, d) for n, d in docs if str(d.get("kind", "")).startswith("inference-performance") or ("runs" in d and "load" in d and "execution" in d)]
    guards = [(n, d) for n, d in docs if d.get("kind") == "resource-guard"]
    out = {"generated_at_utc": now(), "unreadable_files": bad, "excluded": [], "A_functional_correctness": [], "B_language_quality": {
        "status": "NOT MEASURED", "note": "no command here rates writing quality; rubric checks in A are keyword heuristics; a human reading of the saved replies is required"},
        "C_tool_safety": [], "D_performance": [], "guards": [], "comparability": {}}
    ok_intel = []
    for n, d in intel:
        good, why = usable(d)
        if not good:
            out["excluded"].append({"file": n, "reason": why})
            continue
        s = d.get("summary") or {}
        ok_intel.append((n, d))
        out["A_functional_correctness"].append({
            "model": model_label(d), **model_trace(d), "file": n, "cases": s.get("cases"), "greedy_passed": s.get("greedy_passed"), "greedy_pass_rate": s.get("greedy_pass_rate"),
            "greedy_ci95": s.get("greedy_ci95"), "all_runs_pass_rate": s.get("all_runs_pass_rate"), "per_category_greedy": {k: v.get("greedy_pass") for k, v in (s.get("per_category") or {}).items()},
            "exact_checks": s.get("exact_checks"), "rubric_checks_heuristic": s.get("rubric_checks"), "diagnostics": s.get("diagnostics"),
            "note": "heuristic suite of 48 hand-written cases; the interval is wide; this is not an intelligence rating"})
        t = s.get("tools") or {}
        runs = t.get("runs") or 0
        out["C_tool_safety"].append({"model": model_label(d), **model_trace(d), "source": n, "kind": "tool-call quality (bench-intel tool_use cases; nothing is executed)",
                                     "tool_runs": runs, "call_not_usable": t.get("call_not_usable"), "wrong_tool": t.get("wrong_tool"), "wrong_arguments": t.get("wrong_arguments"),
                                     "usable_call_rate": round(1 - (t.get("call_not_usable") or 0) / runs, 3) if runs else None,
                                     "injection_gate_in_intel": (s.get("security") or {}).get("gate")})
    ok_sec = []
    for n, d in sec:
        good, why = usable(d)
        if not good:
            out["excluded"].append({"file": n, "reason": why})
            continue
        ok_sec.append((n, d))
        s = d.get("summary") or {}
        out["C_tool_safety"].append({"model": model_label(d), **model_trace(d), "source": n, "kind": "bench-security gates (suite " + str(d.get("suite")) + ")",
                                     "raw_gate": s.get("raw_gate"), "app_gate": s.get("app_gate"), "raw_failures_total": s.get("raw_failures_total"),
                                     "app_failures_total": s.get("app_failures_total"), "crashes": s.get("crashes"),
                                     "note": "a PASS is not proof of security; tool_authorization uses a SIMULATED desktop"})
    for n, d in perf:
        good, why = usable(d)
        if not good:
            out["excluded"].append({"file": n, "reason": why})
            continue
        s, m = d.get("summary") or {}, d.get("model") or {}
        out["D_performance"].append({"model": str(m.get("id")), **model_trace(d), "file": n, "valid_runs": s.get("valid_runs"), "measured_runs": s.get("measured_runs"),
                                     "load_seconds": s.get("load_seconds"), "time_to_first_token_seconds": s.get("time_to_first_token_seconds"),
                                     "sustained_tokens_per_second": s.get("sustained_tokens_per_second"), "overall_tokens_per_second": s.get("overall_tokens_per_second"),
                                     "peak_process_memory_mb": s.get("peak_memory_mb"), "context_tokens": (d.get("execution") or {}).get("n_ctx"),
                                     "mode": (d.get("execution") or {}).get("mode")})
    for n, d in guards:
        out["guards"].append({"file": n, "finished": d.get("finished"), "started": d.get("started"), "stop_reason": d.get("stop_reason"), "refused": d.get("refused"),
                              "peak_tree_rss_bytes": d.get("peak_tree_rss_bytes"), "min_available_bytes": d.get("min_available_bytes"),
                              "max_swap_used_bytes": d.get("max_swap_used_bytes"), "command_exit_code": d.get("command_exit_code"), "guard_exit_code": d.get("guard_exit_code")})
    # comparability: a field that is missing from ANY compared file makes the group non-comparable (absence is not agreement)
    diffs, gaps = {}, {}
    groups = (("bench-intel", ok_intel, COMPARE_FIELDS), ("bench-security", ok_sec, COMPARE_FIELDS),
              ("perf", [(n, d) for n, d in perf if usable(d)[0]], PERF_FIELDS))
    for label, group, fields in groups:
        if len(group) < 2:
            continue
        for fld, get in fields:
            vals = {n: get(d) for n, d in group}
            absent = sorted(n for n, v in vals.items() if missing(v))
            if absent:
                gaps.setdefault(label, {})[fld] = absent
            elif len({json.dumps(v, sort_keys=True, default=str) for v in vals.values()}) > 1:
                diffs.setdefault(label, {})[fld] = vals
    comparable_groups = sum(1 for _, g, _f in groups if len(g) >= 2)
    if diffs or gaps:
        verdict = "NOT COMPARABLE: " + ("settings or hardware differ" if diffs else "") + (" and " if diffs and gaps else "") + ("required metadata is missing from some files" if gaps else "") + " (see below)"
    elif comparable_groups:
        verdict = "COMPARABLE on the recorded fields (not on anything listed under 'not recorded')"
    else:
        verdict = "NOTHING TO COMPARE: fewer than two usable results of any one kind"
    out["comparability"] = {"differences": diffs, "missing_fields": gaps, "verdict": verdict,
                            "unrecorded_by_any_file": ["power plan", "plugged in", "thermals / background load", "antivirus / other programs"]}
    # traceability of every usable result to model file bytes
    verified = {}
    for n, d in docs:
        if d.get("kind") == "downloaded-file-verification":
            verified[d.get("computed_sha256")] = {"file": n, "candidate": d.get("candidate"), "status": d.get("status"),
                                                   "hash_computed_from_bytes": d.get("hash_computed_from_bytes"), "human_page_hash_compared": d.get("human_page_hash_compared"),
                                                   "metadata_record": d.get("metadata_record")}
    out["E_model_file_provenance"] = {"downloaded_file_verifications": [{"file": v["file"], "candidate": v["candidate"], "status": v["status"]} for v in verified.values()],
                                      "per_result": []}
    for fam in ("A_functional_correctness", "C_tool_safety", "D_performance"):
        for r in out[fam]:
            h = r.get("model_sha256")
            v = verified.get(h) if h else None
            out["E_model_file_provenance"]["per_result"].append({
                "result": r.get("file") or r.get("source"), "model": r.get("model"), "model_sha256_recorded_in_result": h,
                "trace": ("matches a PASSED download verification (" + v["file"] + ")" if v and v["status"] == "PASS" else
                          "hash NOT found in any passed download verification" if h else "NO model hash recorded in the result: not traceable to file bytes")})
    out["usable_results"] = len(ok_intel) + len(ok_sec) + len(out["D_performance"])
    return out


def to_md(o: dict) -> str:
    L = [f"# Stage 5 results summary ({o['generated_at_utc']})", "", "Recorded numbers only. No overall score. Excluded runs are listed at the end.", "",
         "## A. Functional correctness (bench-intel; heuristic checks, 48 cases)", "| model | greedy pass | rate | 95% interval | passed in every run | file |", "|---|---|---|---|---|---|"]
    for r in o["A_functional_correctness"]:
        L.append(f"| {r['model']} | {r['greedy_passed']}/{r['cases']} | {pct(r['greedy_pass_rate'])} | {r['greedy_ci95']} | {pct(r['all_runs_pass_rate'])} | {r['file']} |")
    L += ["", "## B. Language quality", f"{o['B_language_quality']['status']}: {o['B_language_quality']['note']}", "", "## C. Tool safety and tool-call quality"]
    for r in o["C_tool_safety"]:
        L.append("- " + "; ".join(f"{k}={v}" for k, v in r.items() if k not in ("note",)) + (f" ({r['note']})" if r.get("note") else ""))
    L += ["", "## D. Performance and resources", "| model | valid/measured runs | load s | TTFT s (median) | sustained tok/s (median) | overall tok/s (median) | peak process MB | mode |", "|---|---|---|---|---|---|---|---|"]
    for r in o["D_performance"]:
        g = lambda k: (r.get(k) or {}).get("median", "n/a")        # noqa: E731
        L.append(f"| {r['model']} | {r['valid_runs']}/{r['measured_runs']} | {r['load_seconds']} | {g('time_to_first_token_seconds')} | {g('sustained_tokens_per_second')} | "
                 f"{g('overall_tokens_per_second')} | {r['peak_process_memory_mb']} | {r['mode']} |")
    L += ["", "### Resource-guard logs"]
    for g in o["guards"]:
        L.append(f"- {g['file']}: started={g['started']} stop_reason={g['stop_reason']} refused={g['refused']} peak_tree_rss_bytes={g['peak_tree_rss_bytes']} "
                 f"min_available_bytes={g['min_available_bytes']} max_swap_used_bytes={g['max_swap_used_bytes']} command_exit_code={g['command_exit_code']}"
                 + ("" if g["finished"] else "  WARNING: finished is not true - the guard did not complete; its numbers are partial"))
    L += ["", "## E. Model file provenance (is each result traceable to bytes that passed scripts/verify_downloaded_model.py?)"]
    L += [f"- {r['result']} ({r['model']}): {r['trace']}" for r in o["E_model_file_provenance"]["per_result"]] or ["- no usable results"]
    L += ["", f"## Comparability: {o['comparability']['verdict']}"]
    for kind, d in o["comparability"]["missing_fields"].items():
        for fld, files in d.items():
            L.append(f"- {kind} / {fld}: MISSING in {files}")
    for kind, d in o["comparability"]["differences"].items():
        for fld, vals in d.items():
            L.append(f"- {kind} / {fld}: {vals}")
    L.append("- not recorded by any file (write in the session notes): " + ", ".join(o["comparability"]["unrecorded_by_any_file"]))
    L += ["", "## Excluded (not results)"] + [f"- {e['file']}: {e['reason']}" for e in o["excluded"]] + [f"- unreadable: {b}" for b in o["unreadable_files"]]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inputs", action="append", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    docs, bad = load([p for p in a.inputs if p.is_dir()])
    o = summarize(docs, bad)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "stage5_summary.json").write_text(json.dumps(o, indent=2, default=str), encoding="utf-8")
    (a.out / "stage5_summary.md").write_text(to_md(o), encoding="utf-8")
    print(f"usable results: {o['usable_results']}; excluded: {len(o['excluded'])}; unreadable: {len(bad)}; comparability: {o['comparability']['verdict']}")
    return 0 if o["usable_results"] and not bad else 4


if __name__ == "__main__":
    sys.exit(main())
