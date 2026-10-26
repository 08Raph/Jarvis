"""Act-observe-verify task runner.

A task is a list of steps. Each step is one tool call plus optional CHECKS: observation tool calls whose result must satisfy a
condition (e.g. file_info says exists, check_visible says found, ui_read value equals "3"). For each step the runner:
  1. checks for cancellation,  2. asks the executor to act (permission + confirmation happen there),
  3. observes with each check,  4. decides verified / not verified from what was observed,
  5. on failure: undoes this task's file changes if the step says rollback, then stops and reports the blocker.
History (redacted) is appended to tasks.jsonl in the profile folder; nothing is uploaded."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from jarvis.desktop import schema as S
from jarvis.desktop.executor import Executor, StepResult
from jarvis.desktop.policy import Context
from jarvis.desktop.stop import Stopped

OPS = ("equals", "not_equals", "contains", "not_contains", "true", "false", "exists", "gte", "lte")


class TaskError(ValueError):
    pass


def _get(obj, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


_MISSING = object()


@dataclass
class Check:
    tool: str
    args: dict
    field: str = "data.found"         # dotted path into the outcome (summary, verified, data.*, untrusted_text)
    op: str = "true"
    value: object = None
    retries: int = 0                  # re-observe this many times (apps take a moment to update)
    delay: float = 0.5

    def evaluate(self, outcome: S.Outcome) -> tuple[bool, str]:
        got = _get(outcome.to_dict(), self.field)
        if got is _MISSING:
            return False, f"{self.field} is missing from the observation"
        v = self.value
        ok = {"equals": lambda: got == v, "not_equals": lambda: got != v,
              "contains": lambda: str(v).lower() in str(got).lower(), "not_contains": lambda: str(v).lower() not in str(got).lower(),
              "true": lambda: got is True, "false": lambda: got is False, "exists": lambda: got is not None,
              "gte": lambda: float(got) >= float(v), "lte": lambda: float(got) <= float(v)}[self.op]()
        shown = got if not isinstance(got, str) else (got[:80] + "..." if len(got) > 80 else got)
        return bool(ok), f"{self.tool}: {self.field} = {shown!r} ({self.op} {v!r}: {'yes' if ok else 'no'})"


@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)
    description: str = ""
    checks: list = field(default_factory=list)
    on_fail: str = "stop"             # stop | rollback | continue


@dataclass
class Task:
    goal: str
    steps: list
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        if not isinstance(d, dict) or not isinstance(d.get("steps"), list) or not d["steps"]:
            raise TaskError("a task needs a goal and a non-empty list of steps")
        if len(d["steps"]) > 50:
            raise TaskError("at most 50 steps per task")
        steps = []
        for i, s in enumerate(d["steps"], 1):
            if not isinstance(s, dict) or not isinstance(s.get("tool"), str):
                raise TaskError(f"step {i} needs a 'tool'")
            checks = []
            for c in s.get("checks", []) or []:
                if not isinstance(c, dict) or c.get("op", "true") not in OPS or not isinstance(c.get("tool"), str):
                    raise TaskError(f"step {i}: each check needs a tool and an op from {OPS}")
                checks.append(Check(c["tool"], c.get("args") or {}, str(c.get("field", "data.found")), c.get("op", "true"),
                                    c.get("value"), int(min(max(c.get("retries", 0), 0), 20)), float(min(max(c.get("delay", 0.5), 0.05), 10))))
            if s.get("on_fail", "stop") not in ("stop", "rollback", "continue"):
                raise TaskError(f"step {i}: on_fail must be stop, rollback or continue")
            steps.append(Step(s["tool"], s.get("args") or {}, str(s.get("description", ""))[:300], checks, s.get("on_fail", "stop")))
        return cls(str(d.get("goal", ""))[:500], steps)


@dataclass
class StepReport:
    index: int
    step: Step
    action: StepResult
    checks: list = field(default_factory=list)       # (ok, text)
    verified: bool | None = None
    note: str = ""

    def label(self) -> str:
        """One plain word for what is known. Denial, failure, timeout, cancellation and 'could not check' are all different."""
        st = self.action.status
        if st == "stopped":
            return "CANCELLED"
        if st == "timeout":
            return "UNKNOWN"
        if st in ("denied", "declined", "invalid", "unsupported"):
            return st.upper()
        if st == "failed":
            return "FAILED"
        return {True: "VERIFIED", False: "NOT DONE", None: "UNVERIFIED"}[self.verified]

    def line(self) -> str:
        extra = f" - {self.note}" if self.note else ""
        return f"{self.index}. [{self.label()}] {self.step.description or self.step.tool}: {self.action.message}{extra}"


@dataclass
class TaskReport:
    task: Task
    status: str = "running"             # completed | completed_unverified | failed | blocked | cancelled | unknown (a step timed out)
    steps: list = field(default_factory=list)
    rolled_back: list = field(default_factory=list)
    blocker: str = ""
    warnings: list = field(default_factory=list)

    def text(self) -> str:
        head = {"completed": "Task completed and every step was verified.",
                "completed_unverified": "Task ran, but some steps could not be verified (see UNVERIFIED).",
                "failed": "Task stopped: a step did not achieve its result.",
                "blocked": "Task stopped: a step was not allowed or not confirmed.",
                "unknown": "Task stopped: a step timed out, so whether it happened is UNKNOWN (check before retrying).",
                "cancelled": "Task cancelled."}.get(self.status, self.status)
        out = [f"{head} (task {self.task.id})"] + [s.line() for s in self.steps]
        if self.blocker:
            out.append(f"Blocker: {self.blocker}")
        if self.rolled_back:
            out.append("Undone: " + "; ".join(self.rolled_back))
        for w in self.warnings:
            out.append(f"Warning: {w}")
        return "\n".join(out)


class Runner:
    def __init__(self, executor: Executor, history_path: Path | None, ctx: Context | None = None,
                 progress: Callable[[str], None] | None = None):
        self.ex, self.history_path = executor, history_path
        self.ctx = ctx or Context(guest=executor.env.guest)
        self.progress = progress or (lambda s: None)

    def preview(self, task: Task) -> list[dict]:
        out = []
        for i, s in enumerate(task.steps, 1):
            p = self.ex.preview(s.tool, s.args, Context(guest=self.ctx.guest))
            p["step"] = i
            p["checks"] = [f"{c.tool} -> {c.field} {c.op} {c.value!r}" for c in s.checks]
            out.append(p)
        return out

    def _observe(self, chk: Check) -> tuple[bool | None, str]:
        """(True, text) the check ran and agrees; (False, text) it ran and disagrees; (None, text) it could not be run at all
        (not allowed, timed out, unsupported) - that is 'unknown', never 'did not happen'."""
        spec = self.ex.specs.get(chk.tool)
        if spec is None or spec.risk != S.OBSERVE:
            return None, f"check tool '{chk.tool}' is not an observation tool"
        last, ran = "", True
        from jarvis.desktop import retry as R
        polls = R.decide(spec, chk.retries, R.MAX_CHECK_POLLS).allowed          # a check is repeated only if its tool has a trusted idempotency contract
        for attempt in range(polls + 1):
            r = self.ex.run(chk.tool, chk.args, self.ctx)
            if r.status == "stopped":
                raise Stopped(r.message)
            if r.outcome is None or r.status not in ("verified", "unverified"):
                last, ran = f"{chk.tool} could not observe: {r.status} - {r.message}", False
            else:
                try:
                    ok, last = chk.evaluate(r.outcome)
                    ran = True
                except (ValueError, TypeError, OverflowError) as e:
                    # e.g. "gte" on text: the check could not be compared at all. That is "unknown", not "it did not happen",
                    # and it must not abort the run and lose the report of what already happened.
                    ok, last, ran = False, f"{chk.tool}: {chk.field} could not be compared ({type(e).__name__}); the check is inconclusive", False
                if ok:
                    return True, last
            if attempt < polls:
                self.ex.env.stop.wait(chk.delay)
        return (False if ran else None), last

    def run(self, task: Task) -> TaskReport:
        rep = TaskReport(task)
        checkpoints: list[str] = []
        try:
            for i, step in enumerate(task.steps, 1):
                self.ex.env.stop.check()
                self.progress(f"step {i}/{len(task.steps)}: {step.description or step.tool}")
                res = self.ex.run(step.tool, step.args, self.ctx)
                sr = StepReport(i, step, res)
                rep.steps.append(sr)
                if res.outcome and res.outcome.checkpoint:
                    checkpoints.append(res.outcome.checkpoint)
                if res.status in ("denied", "declined", "invalid", "unsupported"):
                    sr.verified = False
                    rep.status, rep.blocker = "blocked", f"step {i} ({step.tool}) {res.status}: {res.message}"
                    break
                if res.status == "stopped":
                    raise Stopped(res.message)
                if res.status == "timeout":
                    sr.verified = None            # it may or may not have happened: unknown, and Jarvis will not guess or roll back
                    sr.note = "timed out; the result is UNKNOWN - check before retrying"
                    rep.status = "unknown"
                    rep.blocker = f"step {i} ({step.tool}) timed out; whether it happened is unknown"
                    self.progress(sr.line())
                    break
                if res.status == "failed":
                    sr.verified = False
                    sr.note = "the action itself failed"
                else:
                    results = [self._observe(c) for c in step.checks]
                    sr.checks = results
                    if step.checks:
                        sr.verified = False if any(ok is False for ok, _ in results) else \
                            None if any(ok is None for ok, _ in results) else True
                        sr.note = "; ".join(t for _, t in results)
                    else:
                        sr.verified = True if res.status == "verified" else None
                        if sr.verified is None:
                            sr.note = "no check could confirm this step"
                self.progress(sr.line())
                if sr.verified is False and step.on_fail != "continue":
                    rep.status = "failed"
                    rep.blocker = f"step {i} ({step.tool}): {sr.note or res.message}"
                    if step.on_fail == "rollback":
                        rep.rolled_back = self._rollback(checkpoints)
                    break
            else:
                vals = [s.verified for s in rep.steps]
                rep.status = "completed" if all(v is True for v in vals) else \
                    "failed" if any(v is False for v in vals) else "completed_unverified"
        except Stopped as e:
            rep.status, rep.blocker = "cancelled", str(e)
            self.ex.kill_running()
        try:
            self._save(rep)
        except Exception as e:  # noqa: BLE001 - the report of what happened is never lost because the history file failed
            rep.warnings.append(f"the task history could not be saved ({type(e).__name__})")
        return rep

    def _rollback(self, checkpoints: list[str]) -> list[str]:
        done = []
        for cid in reversed(checkpoints):
            try:
                done.append(self.ex.env.checkpoints.undo(cid))
            except Exception as e:  # noqa: BLE001
                done.append(f"could not undo {cid}: {e}")
        return done

    def _save(self, rep: TaskReport) -> None:
        if self.history_path is None:
            return
        rec = {"id": rep.task.id, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "goal": rep.task.goal, "status": rep.status,
               "blocker": rep.blocker, "rolled_back": rep.rolled_back,
               "steps": [{"index": s.index, "tool": s.step.tool, "verified": s.verified, "note": s.note, **s.action.to_dict()} for s in rep.steps]}
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_redact_obj(rec), default=str) + "\n")


def _redact_obj(o):
    from jarvis.memory.secrets import redact
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, dict):
        return {k: _redact_obj(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_redact_obj(v) for v in o]
    return o


def history(path: Path, n: int = 20) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out
