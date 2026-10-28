"""Judge the report of a REAL `jarvis selftest` run (stdlib only). Used by the opt-in real-inference steps of .github/workflows/windows-ci.yml.

Usage: python scripts/ci_check_selftest_report.py --data-dir DIR --exit-code N --model-id ID [--summary FILE]
It keeps three outcomes apart, as the project requires:
  BLOCKED  the check could not happen: no model selected / report missing (exit 2), or the llama.cpp runtime is unavailable.
  FAIL     the runtime and model were there and the run failed: load, generation, empty or invalid output, timeout, crash, worker start problem, cancelled,
           or a "pass" whose report contradicts itself.
  PASS     ONLY when a real model was loaded in the worker process, generated a reply, the reply passed the project's answer check, and the report, exit code
           and file system all agree. This is one arithmetic answer on one runner; it says nothing about quality, the GUI, voice or other PCs.
Exit code: 0 PASS, 1 FAIL, 3 BLOCKED."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TRANSIENT = ("selftest.progress.json", "selftest.worker.json")


def judge(report, exit_code: int, model_id: str, data_dir: Path) -> tuple[str, str]:
    if exit_code == 2 or report is None:
        return "BLOCKED", f"model missing: exit code {exit_code}, " + ("no report was written" if report is None else "no usable model selected")
    fail = report.get("failure") if isinstance(report.get("failure"), dict) else {}
    kind = fail.get("kind")
    if kind == "runtime_unavailable":
        return "BLOCKED", "runtime unavailable: the llama.cpp runtime did not import in the worker (" + str(fail.get("message", ""))[:160] + ")"
    if report.get("status") != "passed":
        why = f"status {report.get('status')!r}, failure kind {kind!r}, stage {fail.get('stage')!r}: {str(fail.get('message', ''))[:200]}"
        if report.get("worker_exit"):
            why += f"; worker exit: {report['worker_exit']}"
        return "FAIL", "inference failed: " + why
    problems = []
    if exit_code != 0 or report.get("exit_code") != 0:
        problems.append(f"exit code {exit_code} / report exit_code {report.get('exit_code')!r} is not 0")
    if report.get("passed") is not True or report.get("failure") is not None:
        problems.append("passed is not true or failure is not null")
    if report.get("schema") != 2:
        problems.append(f"unexpected schema {report.get('schema')!r}")
    ac = report.get("answer_check") if isinstance(report.get("answer_check"), dict) else {}
    if ac.get("ok") is not True or report.get("answer_mentions_4") is not True or not str(report.get("answer", "")).strip():
        problems.append("the answer did not pass the answer check")
    if report.get("model_id") != model_id or not report.get("model_sha256"):
        problems.append(f"model_id {report.get('model_id')!r} / model_sha256 missing")
    rt = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
    if rt.get("available") is not True:
        problems.append("runtime not recorded as available")
    ls = report.get("load_seconds")
    if not isinstance(ls, (int, float)) or ls <= 0:
        problems.append(f"load_seconds {ls!r} is not a positive number")
    tokens = report.get("output_tokens")
    if not isinstance(tokens, int) or tokens <= 0:
        problems.append(f"output_tokens {tokens!r} is not a positive integer")
    for k in ("worker_exit", "worker_stderr_tail", "previous_run"):
        if k in report:
            problems.append(f"unexpected field {k!r} in a passing report")
    names = [x.get("stage") for x in report.get("stages", []) if isinstance(x, dict)]
    for w in ("started", "starting_worker", "loading_model", "generating", "validating_output"):
        if w not in names:
            problems.append(f"stage {w!r} was not recorded")
    left = [n for n in TRANSIENT if (data_dir / n).exists()]
    if left:
        problems.append(f"transient files left behind: {left}")
    if problems:
        return "FAIL", "the report claims a pass but: " + "; ".join(problems)
    return "PASS", (f"a real model ({model_id}) loaded in the worker process and its reply passed the answer check; load {ls}s, {report.get('tokens_per_second')} tokens/s; "
                    "one arithmetic answer on one runner, not a quality or compatibility result")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--exit-code", type=int, required=True, help="the exit code of `jarvis selftest`")
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--summary")
    a = ap.parse_args(argv)
    d = Path(a.data_dir)
    try:
        v = json.loads((d / "selftest.json").read_text(encoding="utf-8"))
        report = v if isinstance(v, dict) else None
    except (OSError, ValueError):
        report = None
    label, why = judge(report, a.exit_code, a.model_id, d)
    line = f"REAL INFERENCE (frozen worker): {label} - {why}"
    print(line)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(f"### Real inference through the packaged worker: **{label}**\n\n{why}\n\n")
    return {"PASS": 0, "FAIL": 1}.get(label, 3)


if __name__ == "__main__":
    sys.exit(main())
