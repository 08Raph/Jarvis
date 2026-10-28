"""Turn a `python -m unittest` log into honest counts for CI (stdlib only).

Usage: python scripts/ci_summarize_unittest.py LOG [--json OUT.json] [--summary SUMMARY.md] [--title TEXT]
Reads what unittest itself printed ("Ran N tests", "OK (skipped=N)" / "FAILED (failures=A, errors=B, skipped=C)"), never guesses. Exit code:
0 = PASS (the run finished and reported OK), 1 = FAIL (unittest reported FAILED), 2 = NOT RUN (no readable result: the tests did not run to the end).
passed = ran - failures - errors - skipped - unexpected successes. Skipped tests are reported as skipped, never as passed."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RAN = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s\s*$", re.M)
RESULT = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
HEAD = re.compile(r"^(FAIL|ERROR): (.+)$", re.M)


def read_text(path: Path) -> str:
    """Windows PowerShell 5.1 writes `*>` redirections as UTF-16; accept that as well as UTF-8."""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw.count(b"\x00") > len(raw) // 4:
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8-sig", "replace")


def parse(text: str) -> dict:
    ran = RAN.findall(text)
    res = RESULT.findall(text)
    if not ran or not res:
        return {"label": "NOT RUN", "reason": "the log has no 'Ran N tests' line and a final OK/FAILED line", "ran": 0, "passed": 0, "failed": 0, "errors": 0,
                "skipped": 0, "expected_failures": 0, "unexpected_successes": 0, "seconds": None, "failing": []}
    n, secs = int(ran[-1][0]), float(ran[-1][1])
    verdict, detail = res[-1]
    counts = {"failures": 0, "errors": 0, "skipped": 0, "expected failures": 0, "unexpected successes": 0}
    for part in (detail or "").split(","):
        m = re.fullmatch(r"\s*([a-z ]+)=(\d+)\s*", part)
        if m and m.group(1) in counts:
            counts[m.group(1)] = int(m.group(2))
    bad = counts["failures"] + counts["errors"]
    passed = n - bad - counts["skipped"] - counts["unexpected successes"]
    failing = [f"{k}: {v}" for k, v in HEAD.findall(text)][:200]
    label = "PASS" if verdict == "OK" and bad == 0 and n > 0 else "FAIL"
    return {"label": label, "reason": "", "ran": n, "passed": passed, "failed": counts["failures"], "errors": counts["errors"], "skipped": counts["skipped"],
            "expected_failures": counts["expected failures"], "unexpected_successes": counts["unexpected successes"], "seconds": secs, "failing": failing}


def markdown(r: dict, title: str) -> str:
    lines = [f"### {title}: **{r['label']}**", ""]
    if r["label"] == "NOT RUN":
        lines.append(f"Not run to completion: {r['reason']}.")
    else:
        lines += ["| ran | passed | failed | errors | skipped |", "|---|---|---|---|---|",
                  f"| {r['ran']} | {r['passed']} | {r['failed']} | {r['errors']} | {r['skipped']} |", ""]
        if r["failing"]:
            lines += ["Failing tests (first 40):", ""] + [f"- `{x}`" for x in r["failing"][:40]]
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--json")
    ap.add_argument("--summary", help="append a markdown table to this file (use $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--title", default="Unit tests")
    a = ap.parse_args(argv)
    try:
        r = parse(read_text(Path(a.log)))
    except OSError as exc:
        r = parse("")
        r["reason"] = f"the log could not be read ({type(exc).__name__})"
    print(f"{a.title}: {r['label']}  ran={r['ran']} passed={r['passed']} failed={r['failed']} errors={r['errors']} skipped={r['skipped']}" + (f"  ({r['reason']})" if r["reason"] else ""))
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2), encoding="utf-8")
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(markdown(r, a.title))
    return {"PASS": 0, "FAIL": 1}.get(r["label"], 2)


if __name__ == "__main__":
    sys.exit(main())
