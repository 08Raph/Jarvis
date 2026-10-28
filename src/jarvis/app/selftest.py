"""The report behind `jarvis-gui.exe --selftest` (Qt-free, so it can be tested anywhere).

What a self-test run records, per check: PASS / FAIL / INFO / NOT_RUN and a KIND:
  genuine   - the check really executed in this process (a file lookup, a real import, a real offscreen Qt widget, a real preflight run);
  simulated - the check used a stand-in (the controller's simulate mode replies without any model);
  not_run   - something this self-test does not do, listed so that its absence is visible.
Nothing here downloads, installs or loads a model, changes the default model or the catalog, or needs administrator rights. The file and module checks
are the SAME functions the Status page's diagnostics report and the preflight "App files" check use (jarvis.app.diagnostics): there is one implementation.

Exit codes of the program: 0 = every check passed, 1 = at least one check failed, 3 = Qt (PySide6) could not be loaded at all (nothing window-related ran).
Output files (written to the temp folder; a windowed exe has no console): jarvis-gui-selftest.txt (first line PASS or FAIL ..., what build_windows.ps1
reads) and jarvis-gui-selftest.json (every check)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

PASS, FAIL, INFO, NOT_RUN = "PASS", "FAIL", "INFO", "NOT_RUN"
GENUINE, SIMULATED, NOT_DONE = "genuine", "simulated", "not_run"
EXIT_PASS, EXIT_FAIL, EXIT_NO_QT = 0, 1, 3
TXT_NAME, JSON_NAME = "jarvis-gui-selftest.txt", "jarvis-gui-selftest.json"
MAX_DETAIL = 400


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.exit_override: int | None = None

    def add(self, cid: str, title: str, result: str, kind: str, detail: str = "") -> None:
        from jarvis.preflight import history
        self.checks.append({"id": cid, "title": title, "result": result, "kind": kind, "detail": history.scrub_text(detail)[:MAX_DETAIL]})

    def failed(self) -> list[str]:
        return [c["id"] for c in self.checks if c["result"] == FAIL]

    def exit_code(self) -> int:
        if self.exit_override is not None:
            return self.exit_override
        return EXIT_FAIL if self.failed() else EXIT_PASS

    def first_line(self) -> str:
        bad = self.failed()
        if self.exit_override == EXIT_NO_QT:
            return "FAIL Qt (PySide6) could not be loaded; no window check ran"
        return "PASS" if not bad and self.exit_code() == EXIT_PASS else f"FAIL {len(bad)} check(s) failed: {', '.join(bad)}"

    def text(self) -> str:
        lines = [self.first_line(), ""]
        for c in self.checks:
            lines.append(f"[{c['result']}] ({c['kind']}) {c['id']}: {c['detail']}")
        lines += ["", "PASS here covers only the checks listed above, run on THIS PC. 'genuine' = really executed; 'simulated' = a stand-in was used;",
                  "'not_run' = not done by this self-test. A pass does not show that a model loads or answers."]
        return "\n".join(lines) + "\n"

    def as_dict(self) -> dict:
        return {"kind": "gui-selftest", "schema": 1, "result": self.first_line(), "exit_code": self.exit_code(), "checks": self.checks}


def check_files(rep: Report) -> None:
    from jarvis.app import diagnostics
    r = diagnostics.packaged_files()
    if r["missing_required"]:
        rep.add("app_files", "App files present", FAIL, GENUINE, f"{len(r['missing_required'])} missing ({r['mode']}): " + ", ".join(r["missing_required"][:10]))
    else:
        rep.add("app_files", "App files present", PASS, GENUINE, f"{r['checked']} expected files looked up in the {r['mode']} layout; none required is missing. Existence only: contents were not checked.")


def check_modules(rep: Report) -> None:
    from jarvis.app import diagnostics
    r = diagnostics.required_modules(import_them=True)
    bad = [m for m in r["modules"] if not m["present"]]
    if bad:
        rep.add("modules", "Program modules import", FAIL, GENUINE, "; ".join(f"{m['module']}: {m['problem']}" for m in bad[:6]))
    else:
        rep.add("modules", "Program modules import", PASS, GENUINE, f"{len(r['modules'])} modules, including the preflight and the readiness page, were really imported.")


def validate_record(rec) -> list[str]:
    """Problems in an OFFLINE preflight record (empty list = well-formed). It checks the shape and the honesty rules, not the PC's own readiness:
    a low-disk or no-runtime finding is information about the PC, not a defect of the app."""
    from jarvis.preflight import runner
    if not isinstance(rec, dict):
        return ["no result record was produced"]
    problems = []
    checks = rec.get("checks") or []
    ids = [c.get("id") for c in checks]
    want = [sid for sid, _ in runner.STEPS]
    if ids != want:
        problems.append(f"checks {ids} differ from the expected {want}")
    allowed = {runner.PASS, runner.PROBLEM, runner.UNAVAILABLE, runner.NOT_RUN, runner.INFO}
    for c in checks:
        if c.get("status") == runner.ERROR:
            problems.append(f"check {c.get('id')} crashed: {c.get('detail')}")
        elif c.get("status") not in allowed:
            problems.append(f"check {c.get('id')} has an unknown status {c.get('status')!r}")
    meta = next((c for c in checks if c.get("id") == "metadata"), {})
    if rec.get("include_online") or meta.get("status") != runner.NOT_RUN:
        problems.append("the offline self-test must not run the online check")
    if rec.get("statements") != runner.STATEMENTS:
        problems.append("the record's statements differ from the fixed 'nothing was downloaded/loaded/changed' statements")
    try:
        if runner.verdict(checks, False)["code"] != rec["verdict"]["code"]:
            problems.append("the verdict is not the one derived from the checks")
    except (KeyError, TypeError):
        problems.append("the record has no verdict")
    return problems


def check_preflight(rep: Report, rec, cfg, expect_saved: bool = True) -> None:
    """Judge the record produced by a real offline preflight run (started through the controller's worker, as the button does)."""
    from jarvis.preflight import history
    probs = validate_record(rec)
    if expect_saved and not probs:
        try:
            saved = [r.get("run_id") for r in history.list_runs(cfg)]
        except OSError as e:
            saved = []
            probs.append(f"the history could not be listed ({type(e).__name__})")
        if rec.get("run_id") not in saved:
            probs.append("the run was not saved to the results history")
    if probs:
        rep.add("preflight_offline", "Run Safe Preflight (offline part)", FAIL, GENUINE, "; ".join(probs))
        return
    findings = {c["id"]: c["status"] for c in rec["checks"]}
    rep.add("preflight_offline", "Run Safe Preflight (offline part)", PASS, GENUINE,
            f"Ran on the background worker, all {len(findings)} checks reported, result saved to the history, no internet used. This PC's findings (not app defects): "
            + ", ".join(f"{k}={v}" for k, v in findings.items()) + f"; verdict {rec['verdict']['code']}.")


def run_preflight_via_controller(rep: Report, ctl, cfg, pump, shown=None, timeout: float = 180.0, clock=time.monotonic, sleep=time.sleep) -> None:
    """Start the OFFLINE preflight exactly as the button does (ctl.preflight_start on the background worker), keep `pump()` (the interface's event
    processing) running until it finishes or `timeout` seconds pass, then judge the result. `shown()` -> (rows in the result table, verdict text) lets the
    caller confirm that the page itself displays the result. A start that is refused, a timeout and a worker error are all FAIL, never a pass."""
    cid, title = "preflight_offline", "Run Safe Preflight (offline part)"
    started = ctl.preflight_start(False, False)
    t0 = clock()
    while started and ctl.preflight_state()["running"] and clock() - t0 < timeout:
        pump()
        sleep(0.02)
    pump()
    st = ctl.preflight_state()
    if not started:
        rep.add(cid, title, FAIL, GENUINE, "the preflight did not start: " + str(st.get("message")))
    elif st["running"]:
        ctl.preflight_cancel()
        rep.add(cid, title, FAIL, GENUINE, f"the preflight did not finish within {int(timeout)} seconds")
    elif st["error"]:
        rep.add(cid, title, FAIL, GENUINE, str(st["error"]))
    else:
        check_preflight(rep, st["result"], cfg)
        if shown is not None:
            rows, label = shown()
            want = len(st["result"]["checks"]) if st["result"] else -1
            rep.add("qt_result_table", "Result is shown in the page", PASS if rows == want else FAIL, GENUINE,
                    f"{rows} result rows are in the table (expected {want}); verdict label: '{str(label)[:60]}'")


def add_limits(rep: Report) -> None:
    rep.add("model_inference", "Model loads and answers", NOT_RUN, NOT_DONE, "Not done: the controller runs in simulate mode here, so any reply is a stand-in. No model is loaded and nothing is downloaded or installed.")
    rep.add("online_check", "Online model-host check", NOT_RUN, NOT_DONE, "Not done: the self-test never contacts the internet.")


def write(rep: Report, out_dir) -> Path:
    """Write the .txt (first line PASS or FAIL ...) and the .json next to each other, atomically. Returns the .txt path; raises OSError if it cannot."""
    d = Path(out_dir)
    for name, text in ((JSON_NAME, json.dumps(rep.as_dict(), indent=2)), (TXT_NAME, rep.text())):
        p = d / name
        tmp = d / (name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    return d / TXT_NAME


def mark_running(out_dir) -> None:
    """Called first: a crash that kills the process can then never leave an older PASS behind."""
    try:
        (Path(out_dir) / TXT_NAME).write_text("RUNNING (this self-test has not finished)\n", encoding="utf-8")
    except OSError:
        pass
