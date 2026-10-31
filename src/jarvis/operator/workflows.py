"""Reusable workflows, schedules and event triggers. A workflow is a named list of capability steps. Running it passes EVERY step through the normal gate, so a workflow can
never do more than the permissions you have granted right now, and it cannot grant itself anything. Imported workflows are marked untrusted and always confirm first."""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime

from jarvis.operator import core
from jarvis.operator.core import HIGH, LOW, MEDIUM, RISK_NAMES, Outcome, OperatorError, cap

TRIGGERS = ("startup", "resume", "storage_low", "folder_changed", "app_open", "app_close", "usb_connected", "download_finished")
FORBIDDEN_IN_WORKFLOWS = {"git.destructive"}


def _file(op):
    return op.folder / "workflows.json"


def load(op) -> list[dict]:
    try:
        d = json.loads(_file(op).read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def save(op, items: list[dict]) -> None:
    if op.guest:
        return
    op.folder.mkdir(parents=True, exist_ok=True)
    _file(op).write_text(json.dumps(items), encoding="utf-8")


def _find(items, key: str) -> dict | None:
    k = (key or "").lower().strip()
    return next((w for w in items if w["id"] == k or w["name"].lower() == k), None) or next((w for w in items if k and k in w["name"].lower()), None)


def validate_steps(steps) -> list[dict]:
    core.load_all()
    if not isinstance(steps, list) or not steps or len(steps) > 40:
        raise OperatorError("A workflow needs between 1 and 40 steps.")
    out = []
    for s in steps:
        if not isinstance(s, dict) or s.get("cap") not in core.REGISTRY or s.get("cap") in FORBIDDEN_IN_WORKFLOWS or s.get("cap", "").startswith("workflow."):
            raise OperatorError(f"Step {s.get('cap') if isinstance(s, dict) else s!r} is not something a workflow may run.")
        args = s.get("args", {})
        if not isinstance(args, dict):
            raise OperatorError("Step arguments must be a list of name/value pairs.")
        out.append({"cap": s["cap"], "args": {str(k): v for k, v in args.items() if isinstance(v, (str, int, float, bool, list, type(None)))}})
    return out


def needs(steps: list[dict]) -> dict:
    cats = sorted({core.REGISTRY[s["cap"]].category for s in steps})
    risk = max(core.REGISTRY[s["cap"]].risk_for(s["args"]) for s in steps)
    return {"permissions": cats, "highest_risk": RISK_NAMES[risk]}


@cap("workflow.save", "workflows", "Save a workflow", LOW, ("name", "steps", "description", "every_minutes", "daily_at", "trigger"), False, "46,43")
def wf_save(op, name: str = "", steps=None, description: str = "", every_minutes: int = 0, daily_at: str = "", trigger: str = "", trigger_arg: str = "") -> Outcome:
    if not name.strip():
        raise OperatorError("Give the workflow a name.")
    st = validate_steps(steps)
    if daily_at and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", daily_at):
        raise OperatorError("daily_at looks like 08:30.")
    if trigger and trigger not in TRIGGERS:
        raise OperatorError("Triggers: " + ", ".join(TRIGGERS) + ".")
    items = load(op)
    w = _find(items, name)
    new = {"id": w["id"] if w else uuid.uuid4().hex[:8], "name": name.strip()[:60], "description": description[:200], "steps": st, "paused": False, "trusted": True,
           "every_minutes": int(every_minutes or 0), "daily_at": daily_at, "trigger": trigger, "trigger_arg": trigger_arg[:200], "last_run": 0, "needs": needs(st)}
    if w:
        items[items.index(w)] = {**w, **new}
    else:
        items.append(new)
    save(op, items)
    return Outcome("verified" if _find(load(op), name) or op.guest else "failed", f"Saved the workflow \"{new['name']}\" ({len(st)} steps; needs {', '.join(new['needs']['permissions'])}).", new)


@cap("workflow.list", "workflows", "List workflows and what each needs", LOW, (), False, "46,44")
def wf_list(op) -> Outcome:
    items = load(op)
    if not items:
        return Outcome("info", "You have no saved workflows.")
    return Outcome("info", "; ".join(f"{w['name']} ({len(w['steps'])} steps, {w['needs']['highest_risk']} risk" + (", paused" if w["paused"] else "") + (", scheduled" if w["every_minutes"] or w["daily_at"] or w["trigger"] else "") + ")" for w in items), items)


@cap("workflow.show", "workflows", "Show exactly what a workflow will do", LOW, ("name",), False, "46")
def wf_show(op, name: str = "") -> Outcome:
    w = _find(load(op), name)
    if not w:
        raise OperatorError("I can't find that workflow.")
    return Outcome("info", f"{w['name']}: " + " -> ".join(f"{core.REGISTRY[s['cap']].title if s['cap'] in core.REGISTRY else s['cap']}" for s in w["steps"]) + f". Needs: {', '.join(w['needs']['permissions'])}; highest risk {w['needs']['highest_risk']}.", w)


def _history(op, rec: dict) -> None:
    if op.guest:
        return
    try:
        with (op.folder / "workflow_history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def history(op, n: int = 50) -> list[dict]:
    try:
        lines = (op.folder / "workflow_history.jsonl").read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    return [json.loads(x) for x in lines if x.strip().startswith("{")]


def run_workflow(op, w: dict, reason: str = "manual") -> Outcome:
    results = []
    for i, s in enumerate(w["steps"], 1):
        while op.paused.is_set() and not op.cancel.is_set():
            time.sleep(0.2)
        if op.cancel.is_set() or op.stop_active():
            op.cancel.clear()
            results.append({"step": i, "cap": s["cap"], "status": "stopped"})
            break
        out = op.run(s["cap"], **s["args"])
        results.append({"step": i, "cap": s["cap"], "status": out.status, "message": out.message[:140]})
        if not out.ok:
            break
    done = sum(1 for r in results if r["status"] in ("verified", "unverified", "info"))
    ok = done == len(w["steps"])
    _history(op, {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "workflow": w["name"], "reason": reason, "ok": ok, "steps": results})
    verified = all(r["status"] in ("verified", "info") for r in results) and ok
    last = results[-1] if results else {}
    return Outcome("verified" if verified else ("unverified" if ok else "failed"),
                   f"Workflow \"{w['name']}\": {done} of {len(w['steps'])} steps done." + ("" if ok else f" Stopped at step {len(results)}: {last.get('message') or last.get('status')}"), results)


@cap("workflow.run", "workflows", "Run a saved workflow", lambda a: LOW, ("name",), False, "46,43")
def wf_run(op, name: str = "") -> Outcome:
    items = load(op)
    w = _find(items, name)
    if not w:
        raise OperatorError("I can't find that workflow.")
    if not w.get("trusted", True):
        ok = False
        if op.confirm is not None:
            from types import SimpleNamespace
            try:
                ok = bool(op.confirm(SimpleNamespace(description=f"Run the IMPORTED workflow \"{w['name']}\": " + wf_show(op, w["id"]).message, tool="workflow.run", risk="high")))
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Outcome("declined", "Okay, I did not run the imported workflow.")
    return run_workflow(op, w, "manual")


def _set(op, name: str, **kw) -> dict:
    items = load(op)
    w = _find(items, name)
    if not w:
        raise OperatorError("I can't find that workflow.")
    w.update(kw)
    save(op, items)
    return w


@cap("workflow.pause", "workflows", "Pause or resume a workflow's schedule", LOW, ("name", "paused"), False, "44,46")
def wf_pause(op, name: str = "", paused: bool = True) -> Outcome:
    w = _set(op, name, paused=bool(paused))
    return Outcome("verified", f"\"{w['name']}\" is {'paused' if paused else 'active again'}.")


@cap("workflow.delete", "workflows", "Delete a workflow", MEDIUM, ("name",), False, "46")
def wf_delete(op, name: str = "") -> Outcome:
    items = load(op)
    w = _find(items, name)
    if not w:
        raise OperatorError("I can't find that workflow.")
    items.remove(w)
    save(op, items)
    return Outcome("verified" if not _find(load(op), w["id"]) else "failed", f"Deleted \"{w['name']}\".")


@cap("workflow.duplicate", "workflows", "Duplicate a workflow", LOW, ("name", "new_name"), False, "46")
def wf_dup(op, name: str = "", new_name: str = "") -> Outcome:
    items = load(op)
    w = _find(items, name)
    if not w:
        raise OperatorError("I can't find that workflow.")
    c = {**json.loads(json.dumps(w)), "id": uuid.uuid4().hex[:8], "name": (new_name or w["name"] + " copy")[:60], "paused": True}
    items.append(c)
    save(op, items)
    return Outcome("verified", f"Duplicated as \"{c['name']}\" (paused until you resume it).")


@cap("workflow.export", "workflows", "Export a workflow as text you can share", LOW, ("name",), False, "46")
def wf_export(op, name: str = "") -> Outcome:
    w = _find(load(op), name)
    if not w:
        raise OperatorError("I can't find that workflow.")
    payload = {"jarvis_workflow": 1, "name": w["name"], "description": w["description"], "steps": w["steps"]}
    return Outcome("info", json.dumps(payload), payload)


@cap("workflow.import", "workflows", "Import a workflow someone shared (it stays untrusted and asks before every run)", MEDIUM, ("text",), False, "46")
def wf_import(op, text: str = "") -> Outcome:
    try:
        d = json.loads(text)
    except ValueError:
        raise OperatorError("That isn't a workflow export.") from None
    if not isinstance(d, dict) or d.get("jarvis_workflow") != 1:
        raise OperatorError("That isn't a workflow export.")
    st = validate_steps(d.get("steps"))
    items = load(op)
    w = {"id": uuid.uuid4().hex[:8], "name": str(d.get("name", "Imported"))[:50] + " (imported)", "description": str(d.get("description", ""))[:200], "steps": st, "paused": True, "trusted": False,
         "every_minutes": 0, "daily_at": "", "trigger": "", "trigger_arg": "", "last_run": 0, "needs": needs(st)}
    items.append(w)
    save(op, items)
    return Outcome("verified", f"Imported \"{w['name']}\". It is paused, can't run on a schedule, and will ask you before every run. It needs: {', '.join(w['needs']['permissions'])}.", w)


@cap("workflow.history", "workflows", "Show what workflows did recently", LOW, ("count",), False, "46,58")
def wf_history(op, count: int = 10) -> Outcome:
    h = history(op, int(count))
    return Outcome("info", "; ".join(f"{x['t']} {x['workflow']} {'OK' if x['ok'] else 'FAILED'} ({x['reason']})" for x in h) if h else "No workflow has run yet.", h)


# ------------------------------------------------------------------------------------------------ schedules and events
def sample(op, now: float | None = None) -> dict:
    """What the PC looks like right now, for event detection. Failures give empty values, never errors."""
    snap = {"t": now or time.time(), "apps": set(), "drives": set(), "free_gb": {}, "folders": {}, "downloads": set()}
    try:
        from jarvis.operator.system_ops import _psutil
        ps = _psutil()
        snap["apps"] = {(p.info["name"] or "").lower() for p in ps.process_iter(["name"])}
        for part in ps.disk_partitions(all=False):
            snap["drives"].add(part.mountpoint)
            try:
                snap["free_gb"][part.mountpoint] = ps.disk_usage(part.mountpoint).free / 1024 ** 3
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    for r in op.roots():
        try:
            if r.name == "Downloads":
                snap["downloads"] = {p.name for p in r.iterdir() if p.is_file() and not p.name.lower().endswith((".crdownload", ".part", ".tmp", ".download"))}
            snap["folders"][str(r)] = sorted((p.name, int(p.stat().st_mtime)) for p in r.iterdir())[:2000]
        except OSError:
            pass
    return snap


def detect_events(prev: dict | None, cur: dict, op=None, arg_filter: str = "") -> list[tuple[str, str]]:
    ev: list[tuple[str, str]] = []
    if prev is None:
        return [("startup", "")]
    if cur["t"] - prev["t"] > 600:
        ev.append(("resume", ""))                 # the PC (or Jarvis) was asleep or suspended for a while
    for a in sorted(cur["apps"] - prev["apps"]):
        ev.append(("app_open", a))
    for a in sorted(prev["apps"] - cur["apps"]):
        ev.append(("app_close", a))
    for d in sorted(cur["drives"] - prev["drives"]):
        ev.append(("usb_connected", d))
    for m, free in cur["free_gb"].items():
        if free < 10 and prev["free_gb"].get(m, 99) >= 10:
            ev.append(("storage_low", m))
    for f, sig in cur["folders"].items():
        if f in prev["folders"] and prev["folders"][f] != sig:
            ev.append(("folder_changed", f))
    for n in sorted(cur["downloads"] - prev["downloads"]):
        ev.append(("download_finished", n))
    return ev


def due_by_schedule(w: dict, now: datetime) -> bool:
    last = w.get("last_run", 0)
    if w.get("every_minutes") and now.timestamp() - last >= w["every_minutes"] * 60:
        return True
    if w.get("daily_at"):
        h, m = map(int, w["daily_at"].split(":"))
        today = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return now >= today and last < today.timestamp()
    return False


def tick(op, prev: dict | None, now: datetime | None = None, snap: dict | None = None) -> tuple[dict, list[str]]:
    """Run due scheduled workflows and event triggers. Returns (new snapshot, notes). Does nothing unless computer control is on."""
    now = now or datetime.now()
    cur = snap if snap is not None else sample(op, now.timestamp())
    notes: list[str] = []
    if not op.is_on() or op.stop_active() or op.paused.is_set():
        return cur, notes
    items = load(op)
    events = detect_events(prev, cur, op)
    changed = False
    for w in items:
        if w["paused"] or not w.get("trusted", True):
            continue
        reason = ""
        if due_by_schedule(w, now):
            reason = "schedule"
        elif w.get("trigger"):
            for kind, arg in events:
                if kind == w["trigger"] and (not w.get("trigger_arg") or w["trigger_arg"].lower() in arg.lower()):
                    reason = f"event: {kind} {arg}".strip()
                    break
        if reason:
            w["last_run"] = now.timestamp()
            changed = True
            out = run_workflow(op, w, reason)
            notes.append(f"{w['name']}: {out.message}")
    if changed:
        save(op, items)
    from jarvis.operator import tasks_ops
    for t in tasks_ops.due_reminders(op, now.timestamp()):
        notes.append(f"Reminder: {t['title']}")
    return cur, notes
