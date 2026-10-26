"""Mission data model, plan validation and the state machine. Pure data and rules: nothing here runs a tool."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone

from jarvis.desktop import schema as S
from jarvis.desktop.task import OPS
from jarvis.memory.secrets import find_secret

STATES = ("draft", "awaiting_approval", "approved", "running", "paused", "completed", "failed", "cancelled")
TRANSITIONS = {
    "draft": {"awaiting_approval", "approved", "running", "cancelled"},          # approved/running directly only for plans with no consequential step
    "awaiting_approval": {"draft", "approved", "cancelled"},
    "approved": {"running", "draft", "cancelled"},
    "running": {"paused", "completed", "failed", "cancelled"},
    "paused": {"running", "failed", "cancelled"},
    "failed": {"running", "draft", "cancelled"},                                 # running = resume with explicit per-step decisions
    "completed": {"draft", "running"},                                           # running = a FRESH execution, only with a valid approval (single use) or schedule approval
    "cancelled": {"draft"},
}
STEP_STATES = ("pending", "running", "succeeded", "failed", "skipped", "unverified", "unknown", "cancelled", "blocked")
DONE_STEP = {"succeeded"}
MAX_STEPS, MAX_RETRIES = 30, 2
DEFAULT_LIMITS = {"max_total_seconds": 900, "max_steps": 30, "max_attempts_total": 60}
HARD_LIMITS = {"max_total_seconds": 3600, "max_steps": MAX_STEPS, "max_attempts_total": 100}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
MISSION_ID_RE = re.compile(r"^m-\d{8}-\d{6}-[0-9a-f]{6}$")
UNATTENDED_FORBIDDEN = {S.EXECUTE, S.INSTALL, S.NETWORK}      # may never be scheduled to run without you present


class PlanError(ValueError):
    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


class StateError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return time.strftime("m-%Y%m%d-%H%M%S-", time.gmtime()) + uuid.uuid4().hex[:6]


# ------------------------------------------------------------------ plan definition
def _clean_check(c, where, problems, specs):
    if not isinstance(c, dict) or not isinstance(c.get("tool"), str):
        problems.append(f"{where}: a check needs a tool")
        return None
    if c.get("op", "true") not in OPS:
        problems.append(f"{where}: op must be one of {OPS}")
        return None
    spec = specs.get(c["tool"]) if specs is not None else None
    if specs is not None and (spec is None or spec.risk != S.OBSERVE):
        problems.append(f"{where}: check tool '{c['tool']}' must be a registered observation tool")
        return None
    out = {"tool": c["tool"], "args": c.get("args") or {}, "field": str(c.get("field", "data.found")), "op": c.get("op", "true"), "value": c.get("value"),
           "retries": int(min(max(c.get("retries", 0) or 0, 0), 10)), "delay": float(min(max(c.get("delay", 0.5) or 0.5, 0.05), 10))}
    if spec is not None:
        try:
            spec.validate(out["args"])
        except S.ArgError as e:
            problems.append(f"{where}: check arguments invalid: {e}")
    return out


def normalize(plan: dict, specs: dict | None = None, mode_block=None) -> dict:
    """Validate `plan` (a dict as written by a person or proposed by a model) and return the canonical definition.
    Raises PlanError listing EVERY problem. With `specs` (the executor's registry) it also checks that each tool is registered, allowed in the current mode
    and that its arguments are valid; without it only the structure is checked."""
    problems: list[str] = []
    if not isinstance(plan, dict):
        raise PlanError("a plan must be an object with a goal and steps")
    goal = plan.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        problems.append("the plan needs a goal (text)")
    elif len(goal) > 500:
        problems.append("the goal is too long (500 characters)")
    elif find_secret(goal):
        problems.append("the goal contains something that looks like a secret; remove it")
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError(problems + ["the plan needs a non-empty list of steps"])
    limits = dict(DEFAULT_LIMITS)
    for k, v in (plan.get("limits") or {}).items() if isinstance(plan.get("limits"), dict) else []:
        if k not in DEFAULT_LIMITS:
            problems.append(f"unknown limit '{k}'")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= HARD_LIMITS[k]:
            problems.append(f"limit {k} must be a number from 1 to {HARD_LIMITS[k]}")
        else:
            limits[k] = v
    if len(raw_steps) > min(limits["max_steps"], MAX_STEPS):
        problems.append(f"too many steps ({len(raw_steps)}; the limit is {min(limits['max_steps'], MAX_STEPS)})")
    steps, seen = [], set()
    for i, s in enumerate(raw_steps[:MAX_STEPS + 1], 1):
        w = f"step {i}"
        if not isinstance(s, dict):
            problems.append(f"{w}: must be an object")
            continue
        sid = s.get("id", f"s{i}")
        if not isinstance(sid, str) or not ID_RE.match(sid):
            problems.append(f"{w}: id must be a lowercase letter then up to 15 lowercase letters, digits or _")
            continue
        if sid in seen:
            problems.append(f"{w}: duplicate step id '{sid}'")
            continue
        seen.add(sid)
        w = f"step {sid}"
        tool, args = s.get("tool"), s.get("args") or {}
        if not isinstance(tool, str):
            problems.append(f"{w}: needs a tool")
            continue
        if not isinstance(args, dict):
            problems.append(f"{w}: args must be an object")
            continue
        if find_secret(json.dumps(args, default=str)):
            problems.append(f"{w}: the arguments contain something that looks like a secret; plans are stored and shown, so secrets are not allowed in them")
        if specs is not None:
            spec = specs.get(tool)
            if spec is None:
                problems.append(f"{w}: '{tool}' is not a registered tool")
            else:
                if mode_block:
                    why = mode_block(spec)
                    if why:
                        problems.append(f"{w}: {why}")
                try:
                    spec.validate(args)
                except S.ArgError as e:
                    problems.append(f"{w}: invalid arguments for {tool}: {e}")
        deps = s.get("depends_on", []) or []
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            problems.append(f"{w}: depends_on must be a list of step ids")
            deps = []
        retries = s.get("retries", 0)
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= MAX_RETRIES:
            problems.append(f"{w}: retries must be 0-{MAX_RETRIES}")
            retries = 0
        pre = [c for c in (_clean_check(c, f"{w} precondition", problems, specs) for c in (s.get("preconditions") or [])) if c]
        chk = [c for c in (_clean_check(c, f"{w} success check", problems, specs) for c in (s.get("checks") or [])) if c]
        steps.append({"id": sid, "tool": tool, "args": copy.deepcopy(args), "description": str(s.get("description", ""))[:300], "expect": str(s.get("expect", ""))[:300],
                      "depends_on": list(dict.fromkeys(deps)), "preconditions": pre, "checks": chk, "retries": retries, "retry_safe": bool(s.get("retry_safe", False))})
    ids = {s["id"] for s in steps}
    for s in steps:
        for d in s["depends_on"]:
            if d == s["id"]:
                problems.append(f"step {s['id']} depends on itself")
            elif d not in ids:
                problems.append(f"step {s['id']} depends on '{d}', which is not a step of this plan")
    cyc = find_cycle(steps)
    if cyc:
        problems.append("the steps depend on each other in a circle: " + " -> ".join(cyc))
    if problems:
        raise PlanError(problems)
    return {"goal": goal.strip(), "limits": limits, "steps": steps}


def find_cycle(steps: list[dict]) -> list[str] | None:
    deps = {s["id"]: [d for d in s["depends_on"] if d != s["id"]] for s in steps}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(n):
        state[n] = 1
        stack.append(n)
        for d in deps.get(n, []):
            if d not in deps:
                continue
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if state.get(d) is None:
                r = visit(d)
                if r:
                    return r
        stack.pop()
        state[n] = 2
        return None
    for n in deps:
        if state.get(n) is None:
            r = visit(n)
            if r:
                return r
    return None


def order(steps: list[dict]) -> list[str]:
    """A dependency-respecting order that keeps the written order where possible (stable topological sort)."""
    done: list[str] = []
    pending = [s["id"] for s in steps]
    byid = {s["id"]: s for s in steps}
    while pending:
        for sid in pending:
            if all(d in done for d in byid[sid]["depends_on"]):
                done.append(sid)
                pending.remove(sid)
                break
        else:
            raise PlanError("circular dependencies")
    return done


def plan_hash(defn: dict) -> str:
    """SHA-256 over the canonical JSON of {"goal", "limits", "steps"}: keys sorted, default separators, ASCII-escaped, non-JSON values via str().
    `steps` is an ordered list, so a changed ORDER changes the hash; every step field (tool, args, depends_on, preconditions, checks, retries, retry_safe,
    description, expect) is inside it, and so are all limits. NOT covered: state, approval, history, executions, context, timestamps (they are bookkeeping).
    TRUST MODEL: this is an integrity check against accidental or model/UI-made edits. It is not a signature. The hash and the approval record sit unsigned
    in the same user-writable file, so a process running as the same user can edit the steps AND the hash AND the approval together. The check still
    holds for any change that leaves them inconsistent, and the retry policy does not depend on the hash at all (see jarvis.desktop.retry)."""
    return hashlib.sha256(json.dumps({"goal": defn["goal"], "limits": defn["limits"], "steps": defn["steps"]}, sort_keys=True, default=str).encode()).hexdigest()


def consequential(defn: dict, specs: dict) -> bool:
    """True when any step could change something (anything but a pure observation). An unknown tool counts as consequential."""
    return any(specs.get(s["tool"]) is None or specs[s["tool"]].risk != S.OBSERVE or specs[s["tool"]].risk_fn is not None for s in defn["steps"])


def unattended_problems(defn: dict, specs: dict) -> list[str]:
    out = []
    for s in defn["steps"]:
        sp = specs.get(s["tool"])
        if sp is None:
            out.append(f"{s['id']}: unknown tool")
        elif sp.risk in UNATTENDED_FORBIDDEN or sp.category in ("code_run", "plugins", "research_online"):
            out.append(f"{s['id']}: {s['tool']} runs code, installs software or uses the internet, which is never scheduled")
    return out


# ------------------------------------------------------------------ mission record
def new_mission(defn: dict, source: str = "user", context: list | None = None) -> dict:
    t = now()
    return {"id": new_id(), "version": 1, "state": "draft", "goal": defn["goal"], "limits": defn["limits"], "steps": defn["steps"], "plan_hash": plan_hash(defn),
            "source": source, "context": list(context or []), "created": t, "updated": t, "state_reason": "", "approval": None, "executions": [], "history": [(t, "draft", "created")]}


def set_state(m: dict, new: str, reason: str = "", force: bool = False) -> None:
    """`force` exists for exactly one case: recording that a run could not even start (the plan became invalid). It never moves a mission towards running."""
    if new not in STATES:
        raise StateError(f"unknown state {new}")
    if new != m["state"] and new not in TRANSITIONS[m["state"]] and not (force and new == "failed"):
        raise StateError(f"a mission that is {m['state']} cannot become {new}")
    m["state"], m["state_reason"], m["updated"] = new, reason[:300], now()
    m["history"] = (m["history"] + [(m["updated"], new, reason[:200])])[-60:]


def current_execution(m: dict) -> dict | None:
    return m["executions"][-1] if m["executions"] else None


def new_execution(m: dict, trigger: str) -> dict:
    n = (m["executions"][-1]["n"] + 1) if m["executions"] else 1
    ex = {"id": f"{m['id']}#{n}", "n": n, "trigger": trigger, "started": now(), "finished": None, "state": "running", "plan_hash": m["plan_hash"], "elapsed_s": 0.0, "notes": [],
          "steps": {s["id"]: {"status": "pending", "attempts": [], "started": None, "finished": None, "decision": None} for s in m["steps"]}}
    m["executions"] = (m["executions"] + [ex])[-20:]
    return ex


LABEL = {"succeeded": "DONE and VERIFIED", "failed": "FAILED", "skipped": "SKIPPED", "unverified": "RAN, NOT VERIFIED", "unknown": "UNKNOWN (may or may not have happened)",
         "cancelled": "CANCELLED", "blocked": "BLOCKED", "pending": "NOT RUN", "running": "RUNNING"}


def summarize(m: dict) -> str:
    """The final report. Built ONLY from recorded execution results, never from the plan and never written by a model."""
    ex = current_execution(m)
    if ex is None:
        return f"Mission {m['id']} ({m['state']}): '{m['goal']}'. It has NOT been run; the steps below are a proposal only.\n" + \
            "\n".join(f"  {s['id']}: {s['description'] or s['tool']} (proposed)" for s in m["steps"])
    st = ex["steps"]
    counts = {k: sum(1 for v in st.values() if v["status"] == k) for k in STEP_STATES}
    head = {"completed": "COMPLETED", "failed": "FAILED", "cancelled": "CANCELLED", "paused": "PAUSED (needs your guidance)", "running": "RUNNING"}.get(ex["state"], ex["state"].upper())
    lines = [f"Mission {m['id']} execution #{ex['n']} ({ex['trigger']}): {head}. Goal: {m['goal']}",
             f"  {counts['succeeded']} of {len(st)} step(s) done and verified; {counts['unverified']} ran but could not be verified; {counts['failed']} failed; "
             f"{counts['unknown']} unknown; {counts['blocked']} blocked; {counts['skipped']} skipped; {counts['cancelled']} cancelled; {counts['pending']} not run."]
    for s in m["steps"]:
        r = st.get(s["id"], {})
        att = r.get("attempts", [])
        last = att[-1] if att else None
        line = f"  {s['id']} [{LABEL.get(r.get('status'), r.get('status'))}] {s['description'] or s['tool']}"
        if last:
            line += f" - {last['message'][:160]}" + (f" (attempt {len(att)})" if len(att) > 1 else "")
        if r.get("decision"):
            line += f" [you chose: {r['decision']}]"
        lines.append(line)
    if ex["notes"]:
        lines += ["  note: " + n for n in ex["notes"][-6:]]
    if ex["state"] == "completed" and (counts["unverified"] or any(v.get("decision") == "accept" for v in st.values())):
        lines.append("  NOTE: completed, but not every step was independently verified (see above).")
    if m["state_reason"]:
        lines.append("  state: " + m["state_reason"])
    return "\n".join(lines)
