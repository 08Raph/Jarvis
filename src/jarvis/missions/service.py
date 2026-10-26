"""The mission actions a person can take, in one place, shared by the command line and the desktop window so the rules (hash-bound single-use
approval, tamper refusal, honest cancel messages) exist exactly once."""
from __future__ import annotations

from jarvis.missions import model as M
from jarvis.missions.runner import MissionError


def mark_reviewed(store, runner, m: dict) -> dict:
    """Looking at a plan that changes things moves it to awaiting_approval. (Reviewing a read-only plan changes nothing.)"""
    if m["state"] == "draft" and runner.is_consequential(m):
        M.set_state(m, "awaiting_approval", "reviewed; waiting for your approval")
        store.save(m)
    return m


def approve(store, runner, mid: str, hash_prefix: str) -> dict:
    m = store.load(mid)
    if len(hash_prefix) < 12 or not m["plan_hash"].startswith(hash_prefix.lower()):
        raise MissionError("the hash does not match the current plan; review the plan and copy the hash again")
    if m["state"] not in ("draft", "awaiting_approval"):
        raise MissionError(f"a {m['state']} mission cannot be approved")
    try:
        runner.validate(m)
    except M.PlanError as e:
        raise MissionError("the plan is not valid now: " + "; ".join(e.problems[:3])) from None
    m["approval"] = {"at": M.now(), "plan_hash": m["plan_hash"], "uses_left": 1, "by": "user"}
    M.set_state(m, "approved", "approved by you for ONE start")
    store.save(m)
    store.log({"mission": m["id"], "event": "approved", "plan_hash": m["plan_hash"][:12]})
    return m


def reject(store, mid: str) -> dict:
    m = store.load(mid)
    M.set_state(m, "cancelled", "rejected by you")
    store.save(m)
    store.log({"mission": m["id"], "event": "rejected"})
    return m


def request_pause(store, mid: str) -> str:
    if not store.is_running_elsewhere(mid):
        raise MissionError("the mission is not running, so there is nothing to pause")
    store.flag(mid, "pause")
    return "Pause requested. It takes effect when the current step finishes: a step that is already running cannot be interrupted."


def request_cancel(store, mid: str) -> str:
    if store.is_running_elsewhere(mid):
        store.flag(mid, "cancel")
        return ("Cancel requested. It takes effect when the current step finishes: a step that is already running cannot be interrupted "
                "(use the emergency stop to stop at the next check; that step's result is then recorded as it ended).")
    m = store.load(mid)
    if m["state"] == "running":
        store.recover(mid)
        m = store.load(mid)
    M.set_state(m, "cancelled", "cancelled by you")
    store.save(m)
    store.log({"mission": m["id"], "event": "cancelled"})
    return "Cancelled. Nothing is running."


def save_draft(store, executor, profile_dir, raw: dict, source: str = "user_file", context: list | None = None) -> dict:
    """Validate a plan (a dict from a file or from the model) against the real tool registry and mode, and store it as a DRAFT. Nothing runs."""
    from jarvis import modes
    defn = raw if source == "model" else M.normalize(raw, executor.specs, lambda sp: modes.blocked(profile_dir, sp))   # model plans were validated by the planner
    m = M.new_mission(defn, source, context or [])
    store.save(m)
    store.log({"mission": m["id"], "event": "draft_saved", "source": source})
    return m
