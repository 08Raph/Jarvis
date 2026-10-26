"""The mission engine: act, observe, verify, and stop honestly.

For every step: persist 'running' BEFORE acting (so a crash leaves a trace and nothing is repeated blindly), ask the executor (which checks the
permission switches, consent, mode, validation and your confirmation at that moment), record what the tool actually returned, observe with the step's
checks, and decide: verified / failed / inconclusive / unavailable. The first step that is not clearly a success stops the mission:
  * refused, declined, invalid, unsupported, precondition false, verification failed  -> mission FAILED (dependent steps skipped)
  * timed out, interrupted, ran-but-unverifiable, inconclusive check                  -> mission PAUSED, waiting for your decision
Automatic retries are decided ONLY by jarvis.desktop.retry from the registered tool (a closed list of pure observations), never by `retry_safe` or `retries` in the
plan file. A step that may already have happened is never repeated unless you say so; your own 'retry' decision is recorded as a manual retry."""
from __future__ import annotations

import time
from typing import Callable

from jarvis import modes
from jarvis.desktop import retry as R
from jarvis.memory.secrets import redact
from jarvis.desktop import schema as S
from jarvis.desktop.policy import Context
from jarvis.desktop.stop import Stopped
from jarvis.desktop.task import Check, Runner
from jarvis.missions import model as M
from jarvis.missions.store import MissionStore

REFUSED = ("denied", "declined", "invalid", "unsupported")
NEEDS_DECISION = ("unknown", "unverified", "failed", "blocked", "cancelled", "running")
DECISIONS = ("retry", "accept", "skip")


class MissionError(ValueError):
    pass


class _Interrupt(Exception):
    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind, self.reason = kind, reason


def _check_from(d: dict) -> Check:
    return Check(d["tool"], d["args"], d["field"], d["op"], d["value"], d["retries"], d["delay"])


class MissionRunner:
    def __init__(self, executor, store: MissionStore, ctx: Context | None = None, progress: Callable[[str], None] | None = None, clock=time.monotonic):
        self.ex, self.store = executor, store
        self.ctx = ctx or Context(guest=executor.env.guest)
        self.progress = progress or (lambda s: None)
        self.clock = clock
        self._observer = Runner(executor, None, self.ctx)

    # ------------------------------------------------------------ preview / review (nothing runs)
    def review(self, m: dict) -> list[dict]:
        """Per step: tool, what it needs (permission category, risk), whether that permission is on right now, whether it will ask you, and any problem.
        This is a PREVIEW of a proposal; no tool is run."""
        from jarvis.desktop.policy import CATEGORIES
        out = []
        for st in m["steps"]:
            spec = self.ex.specs.get(st["tool"])
            p = self.ex.preview(st["tool"], st["args"], Context(guest=self.ctx.guest))
            cat = spec.category if spec else "?"
            out.append({"step": st["id"], "tool": st["tool"], "description": st["description"], "expect": st["expect"], "depends_on": st["depends_on"],
                        "category": cat, "permission_on": bool(self.ex.env.settings.enabled.get(cat, False)), "permission_means": CATEGORIES.get(cat, "?"),
                        "risk": p.get("risk") or (spec.risk if spec else "?"), "will_ask_you": p.get("needs_confirmation"), "problem": p.get("problem", ""),
                        "preconditions": [f"{c['tool']} {c['field']} {c['op']} {c['value']!r}" for c in st["preconditions"]],
                        "success_checks": [f"{c['tool']} {c['field']} {c['op']} {c['value']!r}" for c in st["checks"]],
                        "retries": st["retries"], "retry_safe": st["retry_safe"], **self._retry_view(spec, st)})
        return out

    @staticmethod
    def _retry_view(spec, st) -> dict:
        d = R.decide(spec, st.get("retries", 0))
        return {"retries_allowed": d.allowed, "retry_contract": d.contract, "retry_reason": d.reason}

    def validate(self, m: dict) -> dict:
        return M.normalize({"goal": m["goal"], "limits": m["limits"], "steps": m["steps"]}, self.ex.specs, lambda sp: modes.blocked(self.ex.env.profile_dir, sp))

    def is_consequential(self, m: dict) -> bool:
        return M.consequential(m, self.ex.specs)

    # ------------------------------------------------------------ controls
    def _control(self, m: dict) -> None:
        if self.ex.env.stop.is_set():
            raise _Interrupt("stop", "emergency stop: " + (self.ex.env.stop.reason or "stopped"))
        if self.store.flagged(m["id"], "cancel"):
            raise _Interrupt("cancel", "cancelled by you")
        if self.store.flagged(m["id"], "pause"):
            raise _Interrupt("pause", "paused by you")

    # ------------------------------------------------------------ entry point
    def run(self, mid: str, trigger: str = "manual", decisions: dict | None = None, authority: str | None = None, fresh: bool = False) -> dict:
        """Start (or resume) a mission and run it until it finishes, pauses, fails or is cancelled. Returns the saved mission.
        `authority` is set only by the scheduler after it validated a schedule's own approval; nothing a model or plan says can set it."""
        decisions = decisions or {}
        peek = self.store.load(mid)                                  # only to notice a crashed earlier run; it may be stale by the time we hold the lock
        if peek["state"] == "running" and self.store.recover(mid):   # a previous Jarvis process died while running it
            pass
        if self.ex.env.stop.is_set():
            raise MissionError("the emergency stop is active (`jarvis desktop clear-stop` to remove it)")
        if not self.store.lock(mid):
            raise MissionError("this mission is already running in another Jarvis process")
        try:
            # Read the record only NOW, while holding the lock: another worker may have run (and used the single-use approval) since the peek above.
            m = self.store.load(mid)
            if m.get("tampered"):
                raise MissionError("the steps of this mission were changed after the plan was recorded (hash mismatch); review and approve it again")
            return self._run_locked(m, trigger, decisions, authority, fresh)
        finally:
            self.store.unlock(mid)
            self.store.clear_flag(mid, "pause")

    def _run_locked(self, m, trigger, decisions, authority, fresh):
        state = m["state"]
        conseq = self.is_consequential(m)
        resume = (not fresh) and state in ("paused", "failed") and M.current_execution(m) is not None and M.current_execution(m)["state"] in ("paused", "failed")
        if fresh and state == "paused":
            raise MissionError("this mission is paused waiting for your decision; a new execution will not start on top of it")
        if state == "running":
            raise MissionError("this mission is already marked running")
        if state in ("awaiting_approval",) or (state == "draft" and conseq and authority is None):
            raise MissionError("this plan has not been approved; review it and approve it first (`jarvis mission review` / `approve`)")
        if state == "cancelled":
            raise MissionError("this mission was cancelled; make it a draft again to run it")
        # re-validate against the CURRENT registry, mode and arguments before anything runs
        try:
            self.validate(m)
        except M.PlanError as e:
            self._fail_before_start(m, "the plan is no longer valid: " + "; ".join(e.problems[:4]))
            return m
        if resume:
            ex = M.current_execution(m)
            if conseq and authority is None and not (m.get("approval") and m["approval"].get("plan_hash") == m["plan_hash"]):
                raise MissionError("this consequential plan is not approved (the approval does not match the current plan)")
            self._apply_decisions(m, ex, decisions)
            ex["state"] = "running"
            ex["notes"].append(f"resumed ({trigger})")
        else:
            if conseq and authority is None:
                ap = m.get("approval")
                if not ap or ap.get("plan_hash") != m["plan_hash"] or ap.get("uses_left", 0) < 1:
                    raise MissionError("this consequential plan needs a current approval for exactly this plan; approvals are single-use (`jarvis mission approve ID --hash ...`)")
                ap["uses_left"] -= 1
            if decisions:
                raise MissionError("decisions apply only when resuming an execution that stopped")
            ex = M.new_execution(m, trigger if authority is None else f"{trigger}")
            if authority:
                ex["notes"].append(f"started under {authority} (its own approval was checked just now)")
        M.set_state(m, "running", f"execution #{ex['n']} ({trigger})")
        self.store.save(m)
        self.store.log({"mission": m["id"], "execution": ex["id"], "event": "started", "trigger": trigger})
        budget_start = self.clock()
        try:
            self._loop(m, ex, budget_start)
            self._finish(m, ex)
        except _Interrupt as it:
            self._interrupted(m, ex, it)
        except Stopped as e:
            self._interrupted(m, ex, _Interrupt("stop", "emergency stop: " + str(e)))
        ex["elapsed_s"] = round(ex.get("elapsed_s", 0) + (self.clock() - budget_start), 2)
        ex["finished"] = M.now() if ex["state"] in ("completed", "failed", "cancelled") else None
        self.store.save(m)
        self.store.log({"mission": m["id"], "execution": ex["id"], "event": "ended", "state": m["state"], "steps": {k: v["status"] for k, v in ex["steps"].items()}})
        if m["state"] == "cancelled":
            self.store.clear_flag(m["id"], "cancel")
        return m

    def _fail_before_start(self, m, why):
        M.set_state(m, "failed", "NOT started: " + why, force=True)
        self.store.save(m)
        self.store.log({"mission": m["id"], "event": "not_started", "reason": why})

    def _apply_decisions(self, m, ex, decisions):
        by = {s["id"]: s for s in m["steps"]}
        for sid, d in decisions.items():
            if sid not in ex["steps"]:
                raise MissionError(f"there is no step '{sid}'")
            if d not in DECISIONS:
                raise MissionError(f"decision for {sid} must be one of {DECISIONS}")
            rec = ex["steps"][sid]
            if rec["status"] not in NEEDS_DECISION:
                raise MissionError(f"step {sid} is {rec['status']}; it does not need a decision")
            if d == "accept" and rec["status"] not in ("unverified", "unknown"):
                raise MissionError(f"step {sid} {rec['status']}: only a step that ran but could not be verified can be accepted; retry or skip it")
            rec["decision"] = d
            if d == "retry":
                rec["status"] = "pending"
                rec["manual_retry"] = True                         # the next attempt is recorded as YOUR retry, not an automatic one
                ex["notes"].append(f"you chose to retry {sid}; it may be done twice if the first attempt took effect")
                self.store.log({"mission": m["id"], "execution": ex["id"], "step": sid, "event": "manual_retry_decision", "by": "user decision"})
            elif d == "skip":
                rec["status"] = "skipped"
            else:
                ex["notes"].append(f"you accepted {sid} without verification")
        open_ = [sid for sid, r in ex["steps"].items() if r["status"] in NEEDS_DECISION and not r.get("decision")]
        if open_:
            raise MissionError("these steps need your decision (--retry / --accept / --skip): " + ", ".join(open_))
        for sid, r in ex["steps"].items():                # steps skipped only because a dependency stopped the run are re-evaluated
            if r["status"] == "skipped" and not r.get("decision"):
                r["status"] = "pending"

    # ------------------------------------------------------------ the loop
    def _satisfied(self, r) -> bool:
        return r["status"] == "succeeded" or (r["status"] in ("unverified", "unknown") and r.get("decision") == "accept")

    def _loop(self, m, ex, t0):
        byid = {s["id"]: s for s in m["steps"]}
        lim = m["limits"]
        attempts_total = sum(len(r["attempts"]) for r in ex["steps"].values())
        for sid in M.order(m["steps"]):
            s, rec = byid[sid], ex["steps"][sid]
            if self._satisfied(rec) or rec["status"] == "skipped":
                continue
            self._control(m)
            if self.clock() - t0 + ex.get("elapsed_s", 0) > lim["max_total_seconds"]:
                self._stop_mission(m, ex, "failed", f"the mission time limit ({lim['max_total_seconds']} s) was reached before step {sid}; nothing further was started")
                return
            if not all(self._satisfied(ex["steps"][d]) for d in s["depends_on"]):
                rec["status"], rec["finished"] = "skipped", M.now()
                continue
            # preconditions: observations that must hold BEFORE acting
            for c in s["preconditions"]:
                ok, text = self._observer._observe(_check_from(c))
                if ok is not True:
                    rec["status"] = "blocked" if ok is False else "unknown"
                    rec["attempts"].append(self._attempt(rec, "precondition", ("precondition not met: " if ok is False else "precondition could not be checked: ") + text, "", None, [], "unavailable"))
                    if ok is False:
                        self._stop_mission(m, ex, "failed", f"step {sid}: precondition not met, so it was not run")
                    else:
                        self._stop_mission(m, ex, "paused", f"step {sid}: a precondition could not be checked; nothing was run for this step")
                    return
            outcome = self._run_step(m, ex, s, rec, attempts_total, lim)
            attempts_total = sum(len(r["attempts"]) for r in ex["steps"].values())
            self.progress(f"{sid} [{M.LABEL[rec['status']]}] {s['description'] or s['tool']}")
            if rec["status"] == "succeeded":
                self._control(m)                        # a cancel/pause asked for DURING the step takes effect now; its result is already recorded
                continue
            kind, why = outcome
            self._stop_mission(m, ex, kind, why)
            return

    def _attempt(self, rec, status, message, evidence, tool_verified, checks, verification, error="", kind="initial"):
        return {"n": len(rec["attempts"]) + 1, "kind": kind, "at": M.now(), "action_status": status, "message": str(message)[:300], "evidence": str(evidence)[:200],
                "tool_verified": tool_verified, "checks": [{"ok": ok, "text": str(t)[:200]} for ok, t in checks], "verification": verification, "error": str(error)[:200]}

    def _retry_event(self, m, ex, s, rec, n, why):
        self.store.log({"mission": m["id"], "execution": ex["id"], "step": s["id"], "event": "auto_retry", "attempt": n, "reason": why,
                        "contract": rec["retry_policy"]["contract"], "allowed": rec["retry_policy"]["allowed"]})

    def _run_step(self, m, ex, s, rec, attempts_total, lim):
        spec = self.ex.specs[s["tool"]]
        dec = R.decide(spec, s.get("retries", 0))             # the ONLY source of retry permission: the registered tool, not the plan
        rec["retry_policy"] = dec.as_record()
        contract = dec.contract == R.IDEMPOTENT
        if dec.requested or s.get("retry_safe"):
            self.store.log({"mission": m["id"], "execution": ex["id"], "step": s["id"], "event": "retry_policy", **dec.as_record(),
                            "plan_retry_safe_flag": "not used for authorization"})
        max_attempts = 1 + dec.allowed
        total_cap = lim["max_attempts_total"] if isinstance(lim.get("max_attempts_total"), int) and not isinstance(lim.get("max_attempts_total"), bool) else 0
        total_cap = max(0, min(total_cap, M.HARD_LIMITS["max_attempts_total"]))
        made = 0
        kind = "manual_retry" if rec.pop("manual_retry", False) else "initial"
        rec["started"] = rec["started"] or M.now()
        while True:
            try:
                self._control(m)
            except _Interrupt as it:
                if made:                                       # nothing is in flight; the earlier attempts of a repeatable step changed nothing
                    rec["status"] = "pending" if it.kind == "pause" else "cancelled"
                raise
            if attempts_total + made >= total_cap:
                rec["status"] = "failed"
                return "failed", f"the mission attempt limit ({total_cap}) was reached"
            rec["status"] = "running"
            rec["in_flight"] = {"n": len(rec["attempts"]) + 1, "kind": kind, "at": M.now()}
            self.store.save(m)                           # intent recorded BEFORE the action: a crash now leaves an UNKNOWN step, never a silent repeat
            res = self.ex.run(s["tool"], s["args"], self.ctx)
            made += 1
            rec.pop("in_flight", None)
            msg, ev = redact(res.message), redact(res.outcome.evidence if res.outcome else "")    # nothing a tool said is shown or stored unredacted
            tv = res.outcome.verified if res.outcome else None
            if res.status == "stopped":
                rec["attempts"].append(self._attempt(rec, "stopped", msg, ev, tv, [], "unavailable", kind=kind))
                rec["status"] = "unknown" if res.started else "cancelled"
                raise _Interrupt("stop", "emergency stop" + (": the step had already started, so its result is UNKNOWN" if res.started else ""))
            if res.status in REFUSED:
                rec["attempts"].append(self._attempt(rec, res.status, msg, ev, tv, [], "unavailable", kind=kind))
                rec["status"], rec["finished"] = "blocked", M.now()
                return "failed", f"step {s['id']} ({s['tool']}) was {res.status}: {msg}"
            if res.status == "timeout":
                rec["attempts"].append(self._attempt(rec, "timeout", msg, ev, tv, [], "unavailable", kind=kind))
                if made < max_attempts:                    # only a tool with a trusted idempotency contract can get here
                    self._retry_event(m, ex, s, rec, made + 1, "timeout of a repeatable read")
                    self._backoff(made)
                    kind = "auto_retry"
                    continue
                rec["status"], rec["finished"] = "unknown", M.now()
                return "paused", f"step {s['id']} ({s['tool']}) timed out; whether it happened is UNKNOWN. It was not repeated. Check, then resume with --retry, --accept or --skip"
            if res.status == "failed":
                rec["attempts"].append(self._attempt(rec, "failed", msg, ev, tv, [], "unavailable", error=msg, kind=kind))
                if not contract and res.started:
                    # The tool began and then reported an error. For anything that is not a pure read, the error does not prove that nothing changed.
                    rec["status"], rec["finished"] = "unknown", M.now()
                    return "paused", (f"step {s['id']} ({s['tool']}) reported an error after it had started ({msg}); whether it changed anything is UNKNOWN. "
                                      "It was not repeated. Check, then resume with --retry, --accept or --skip")
                if made < max_attempts:
                    self._retry_event(m, ex, s, rec, made + 1, "failed read")
                    self._backoff(made)
                    kind = "auto_retry"
                    continue
                rec["status"], rec["finished"] = "failed", M.now()
                return "failed", f"step {s['id']} ({s['tool']}) failed: {msg}" + ("" if made < 2 else f" (after {made} attempts)")
            # the tool accepted the request and returned an observation: now verify independently
            checks = [self._observer._observe(_check_from(c)) for c in s["checks"]]
            if any(ok is False for ok, _ in checks) or tv is False:
                ver = "failed"
            elif any(ok is None for ok, _ in checks):
                ver = "inconclusive"
            elif checks and all(ok is True for ok, _ in checks):
                ver = "verified"
            elif tv is True:
                ver = "verified"
            else:
                ver = "unavailable"
            rec["attempts"].append(self._attempt(rec, res.status, msg, ev, tv, checks, ver, kind=kind))
            rec["attempts"][-1]["independent_check"] = bool(checks) and all(ok is True for ok, _ in checks)
            if ver == "verified":
                rec["status"], rec["finished"] = "succeeded", M.now()
                return "ok", ""
            if ver == "failed":
                if made < max_attempts:
                    self._retry_event(m, ex, s, rec, made + 1, "read did not match its check")
                    self._backoff(made)
                    kind = "auto_retry"
                    continue
                rec["status"], rec["finished"] = "failed", M.now()
                why = "; ".join(t for _, t in checks if _ is False) or msg
                return "failed", (f"step {s['id']} ({s['tool']}) did not achieve its result: {why}" +
                                  ("" if contract else ". The action may have taken effect, so it was NOT repeated automatically"))
            if made < max_attempts:
                self._retry_event(m, ex, s, rec, made + 1, "result could not be verified")
                self._backoff(made)
                kind = "auto_retry"
                continue
            rec["status"], rec["finished"] = "unverified", M.now()
            return "paused", (f"step {s['id']} ({s['tool']}) ran, but its result could not be verified ("
                              f"{'a check was inconclusive' if ver == 'inconclusive' else 'no check or tool evidence confirms it'}). Resume with --accept, --retry or --skip")

    def _backoff(self, n):
        self.ex.env.stop.wait(min(0.5 * n, 3.0))

    # ------------------------------------------------------------ endings
    def _stop_mission(self, m, ex, kind, why):
        for sid, r in ex["steps"].items():                 # steps waiting on the one that stopped
            if r["status"] == "pending" and any(d in ex["steps"] and ex["steps"][d]["status"] in ("failed", "blocked", "unknown", "unverified", "cancelled", "skipped")
                                               for d in next(s for s in m["steps"] if s["id"] == sid)["depends_on"]):
                r["status"] = "skipped"
        ex["state"] = kind
        ex["notes"].append(why)
        M.set_state(m, kind, why)

    def _finish(self, m, ex):
        if m["state"] != "running":
            return
        if all(self._satisfied(r) or (r["status"] == "skipped" and r.get("decision") == "skip") for r in ex["steps"].values()):
            ex["state"] = "completed"
            M.set_state(m, "completed", "every step finished and was verified" if all(r["status"] == "succeeded" for r in ex["steps"].values()) else
                        "finished with steps you accepted or skipped; see the summary")
        else:
            self._stop_mission(m, ex, "failed", "the mission ended without finishing every step")

    def _interrupted(self, m, ex, it: _Interrupt):
        if it.kind == "pause":
            ex["state"] = "paused"
            ex["notes"].append(it.reason)
            M.set_state(m, "paused", it.reason)
        else:
            for r in ex["steps"].values():
                if r["status"] == "running":
                    r["status"] = "unknown"
            ex["state"] = "cancelled"
            ex["notes"].append(it.reason + ("" if it.kind != "cancel" else "; a step that was already running could not be interrupted, its result was recorded"))
            M.set_state(m, "cancelled", it.reason)
            if it.kind == "stop":
                self.ex.kill_running()
