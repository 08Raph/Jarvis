"""Command line for missions and schedules (docs/PROMPT9_REVIEW.md). Creating, approving, starting, scheduling and deleting are things YOU do here; the model can
only read mission status (tool `mission_status`)."""
from __future__ import annotations

from jarvis.security.private_file import write_private_text

import json
import sys
from datetime import timezone
from pathlib import Path

COMMANDS = ("mission", "schedule")


def add_parsers(sub) -> None:
    m = sub.add_parser("mission", help="plans that run step by step with checks (propose, review, approve, run, pause, cancel, history)")
    m.add_argument("--simulate", action="store_true", help="use the FAKE simulated desktop (nothing on this PC is touched)")
    ms = m.add_subparsers(dest="mcmd", required=True)
    ms.add_parser("new", help="save a plan from a JSON file as a DRAFT (nothing runs)").add_argument("file")
    p = ms.add_parser("plan", help="ask the local model for a DRAFT plan for a goal (nothing runs)")
    p.add_argument("goal")
    p.add_argument("--use-memory", action="store_true", help="let the planner see up to 5 matching saved memories; they are stored with the draft so you can inspect and remove them")
    ms.add_parser("list")
    for n in ("show", "review", "reject", "start", "pause", "cancel", "status", "delete"):
        ms.add_parser(n).add_argument("id")
    a = ms.add_parser("approve", help="approve THIS plan to be started once (consequential plans only); you must pass the plan hash shown by `review`")
    a.add_argument("id")
    a.add_argument("--hash", required=True)
    r = ms.add_parser("resume", help="continue a paused or failed mission; every step that did not clearly succeed needs your decision")
    r.add_argument("id")
    for d in ("retry", "accept", "skip"):
        r.add_argument(f"--{d}", action="append", default=[], metavar="STEP", help=f"{d} this step (repeatable)")
    rv = ms.add_parser("revise", help="replace the plan with a new JSON file (resets to draft and removes any approval)")
    rv.add_argument("id")
    rv.add_argument("file")
    h = ms.add_parser("history", help="recorded executions (redacted)")
    h.add_argument("id", nargs="?")
    h.add_argument("-n", type=int, default=30)
    h.add_argument("--delete", action="store_true", help="delete this history (all of it, or one mission's)")
    e = ms.add_parser("export", help="write one mission's record to a JSON file")
    e.add_argument("id")
    e.add_argument("--out", required=True)
    c = ms.add_parser("context", help="inspect or correct the remembered information stored with a mission")
    c.add_argument("id")
    c.add_argument("--add", help="add a note")
    c.add_argument("--remove", type=int, help="remove note number N")
    s = sub.add_parser("schedule", help="run approved missions later or repeatedly - ONLY while `schedule watch` / `run-due` is running")
    s.add_argument("--simulate", action="store_true")
    ss = s.add_subparsers(dest="scmd", required=True)
    ad = ss.add_parser("add")
    ad.add_argument("mission")
    g = ad.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", metavar="'YYYY-MM-DD HH:MM'")
    g.add_argument("--daily", metavar="HH:MM")
    g.add_argument("--weekly", metavar="HH:MM", help="with --weekdays")
    g.add_argument("--every-minutes", type=int)
    ad.add_argument("--weekdays", help="mon,thu")
    ad.add_argument("--tz", help="America/Toronto, UTC, or UTC-05:00 (default: this PC's current offset)")
    ad.add_argument("--missed", choices=("skip", "run_once"), default="skip")
    ad.add_argument("--grace", type=int, default=15, help="minutes a late run is still accepted")
    ss.add_parser("list")
    ss.add_parser("show").add_argument("id")
    ap = ss.add_parser("approve", help="the schedule's own approval for a mission that changes things: this plan hash, N runs, an expiry")
    ap.add_argument("id")
    ap.add_argument("--hash", required=True)
    ap.add_argument("--max-runs", type=int)
    ap.add_argument("--days", type=int)
    for n in ("pause", "resume", "cancel", "delete"):
        ss.add_parser(n).add_argument("id")
    ed = ss.add_parser("edit")
    ed.add_argument("id")
    ed.add_argument("--tz")
    ed.add_argument("--at")
    ed.add_argument("--time")
    ed.add_argument("--weekdays")
    ed.add_argument("--every-minutes", type=int)
    ss.add_parser("run-due", help="check once now and run what is due")
    w = ss.add_parser("watch", help="keep checking every few seconds in this window (Ctrl+C or `jarvis desktop stop` to end); this is the ONLY way schedules fire")
    w.add_argument("--poll-seconds", type=int, default=30)
    w.add_argument("--max-seconds", type=int, default=3600, help="stop watching after this long (max 86400)")
    sh = ss.add_parser("history")
    sh.add_argument("id", nargs="?")
    sh.add_argument("-n", type=int, default=30)


def _load_engine(cfg):
    from jarvis.inference.gguf_engine import GGUFEngine, RuntimeUnavailable
    from jarvis.models.manager import ModelStore
    store = ModelStore(cfg.models_dir)
    active = store.active()
    if active is None or store.kind(active) != "gguf":
        raise RuntimeError("No chat model is installed. Run `jarvis setup` or `jarvis models fetch qwen3-1.7b-q4_k_m`.")
    ok, problems = store.verify(active, quick=True)
    if not ok:
        raise RuntimeError(f"model failed its integrity check: {problems}")
    try:
        return GGUFEngine(store.path(active))
    except RuntimeUnavailable as e:
        raise RuntimeError(str(e)) from None


def _memory_context(cfg, goal):
    from jarvis.memory.personal import PersonalMemory
    try:
        mem = PersonalMemory.open(cfg.memory_db)
    except Exception:  # noqa: BLE001
        return []
    try:
        return [{"text": h["text"][:300], "source": f"memory:{h['id']}", "category": h["category"]} for h in mem.search(goal, limit=5, touch=False)]
    finally:
        mem.close()


def run(args, cfg, confirm=None, engine=None, clock=None) -> int:
    from jarvis.desktop import cli_desktop
    from jarvis.missions import model as M
    from jarvis.missions.runner import MissionError, MissionRunner
    from jarvis.missions.schedule import ScheduleError, Scheduler
    from jarvis.missions.store import MissionStore
    guest = bool(getattr(args, "guest", False))
    if guest:
        print("Missions and schedules are off in guest mode (nothing is stored).", file=sys.stderr)
        return 2
    store = MissionStore(cfg.profile_dir)
    try:
        if args.cmd == "schedule":
            return _schedule(args, cfg, store, confirm, clock)
        return _mission(args, cfg, store, confirm, engine)
    except (M.PlanError, M.StateError, MissionError, ScheduleError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _runner(cfg, args, store, confirm):
    from jarvis.desktop import cli_desktop
    from jarvis.missions.runner import MissionRunner
    env, ex = cli_desktop.build(cfg, False, getattr(args, "simulate", False), confirm=confirm or cli_desktop.console_confirm)
    cli_desktop._install_stop(env)
    return MissionRunner(ex, store, progress=lambda s: print(s, file=sys.stderr)), ex


def _print_review(r, m):
    from jarvis.missions import model as M
    print(f"Mission {m['id']}  state: {m['state']}  source: {m['source']}")
    print(f"Goal: {m['goal']}")
    print(f"Plan hash: {m['plan_hash']}   (needed to approve: first 12 characters)")
    print("THIS IS A PROPOSAL. Nothing below has been done." if not m["executions"] else f"({len(m['executions'])} earlier execution(s) are recorded; this listing is the plan, not their results)")
    conseq = r.is_consequential(m)
    print("This plan " + ("CHANGES things (approval required; each action still asks you when it runs)." if conseq else "only observes (no approval needed)."))
    for row in r.review(m):
        flag = "" if row["permission_on"] else f"  !! permission '{row['category']}' is OFF"
        ask = "  (will ask you)" if row["will_ask_you"] else ""
        print(f"  {row['step']}: {row['description'] or row['tool']}  [{row['tool']}, risk {row['risk']}, needs '{row['category']}']{ask}{flag}")
        if row["depends_on"]:
            print(f"      after: {', '.join(row['depends_on'])}")
        if row["expect"]:
            print(f"      expected result: {row['expect']}")
        for k, lab in (("preconditions", "before"), ("success_checks", "check")):
            for c in row[k]:
                print(f"      {lab}: {c}")
        if row["retries"] or row["retry_safe"]:
            print(f"      automatic retries: {row['retries_allowed']} (the plan asked for {row['retries']}; only the tool's own trusted registration can allow repeats - {row['retry_reason']})")
        if row["problem"] and row["permission_on"]:
            print(f"      note: {row['problem']}")
    if m["context"]:
        print("Remembered information used by the planner (jarvis mission context ID --remove N to correct):")
        for i, c in enumerate(m["context"], 1):
            print(f"  {i}. [{c.get('source', '?')}] {c['text']}")


def _mission(args, cfg, store, confirm, engine):
    from jarvis.missions import model as M
    from jarvis.missions.planner import PlannerError, propose
    from jarvis.missions.runner import MissionError
    c = args.mcmd
    if c == "list":
        store.recover_all()
        rows = store.list()
        for r in rows:
            print(f"{r['id']}  {r['state']:18} {r['executions']} run(s)  {r['goal']}{'  !! TAMPERED/DAMAGED' if r['tampered'] else ''}")
        print("(none)" if not rows else "")
        return 0
    if c in ("new", "plan", "revise"):
        runner, ex = _runner(cfg, args, store, confirm)
        ctx = []
        if c == "plan":
            ctx = _memory_context(cfg, args.goal) if args.use_memory else []
            try:
                eng = engine or _load_engine(cfg)
            except RuntimeError as e:
                print(str(e), file=sys.stderr)
                return 2
            try:
                defn, info = propose(lambda msgs: eng.chat(msgs, 1024, 0.2), args.goal, ex, ctx)
            except (PlannerError, M.PlanError) as e:
                print(f"The model did not produce a valid plan ({e}). Nothing was saved or run.", file=sys.stderr)
                return 1
            source = "model"
        else:
            try:
                raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                print(f"cannot read the plan file: {e}", file=sys.stderr)
                return 2
            defn, source = M.normalize(raw, ex.specs, lambda sp: __import__("jarvis.modes", fromlist=["x"]).blocked(cfg.profile_dir, sp)), "user_file"
        if c == "revise":
            m = store.load(args.id)
            if m["state"] in ("running",):
                raise MissionError("it is running")
            m.update(goal=defn["goal"], limits=defn["limits"], steps=defn["steps"], plan_hash=M.plan_hash(defn), approval=None, source=source if source != "user_file" else m["source"] + "+revised")
            M.set_state(m, "draft", "plan revised; approval removed")
        else:
            m = M.new_mission(defn, source, ctx)
        store.save(m)
        store.log({"mission": m["id"], "event": "draft_saved", "source": source})
        _print_review(runner, m)
        print(f"\nSaved as a DRAFT: {m['id']}. Nothing has run.")
        return 0
    m = store.load(args.id) if getattr(args, "id", None) and c not in ("history", "delete") else None
    if c in ("show", "status"):
        store.recover(args.id) if m["state"] == "running" else None
        m = store.load(args.id)
        print(M.summarize(m))
        if store.flagged(args.id, "cancel") or store.flagged(args.id, "pause"):
            print("(a cancel/pause request is waiting; it takes effect after the current step)")
        return 0
    if c == "review":
        from jarvis.missions import service
        runner, ex = _runner(cfg, args, store, confirm)
        _print_review(runner, m)
        if m["state"] == "draft" and runner.is_consequential(m):
            service.mark_reviewed(store, runner, m)
            print(f"\nTo approve this exact plan:  jarvis mission approve {m['id']} --hash {m['plan_hash'][:12]}")
        return 0
    if c == "approve":
        from jarvis.missions import service
        runner, ex = _runner(cfg, args, store, confirm)
        service.approve(store, runner, args.id, args.hash)
        print("Approved for one start. Every action still follows the permission switches and asks you when it runs.")
        return 0
    if c == "reject":
        from jarvis.missions import service
        service.reject(store, args.id)
        print("Rejected. Nothing was run.")
        return 0
    if c in ("start", "resume"):
        runner, ex = _runner(cfg, args, store, confirm)
        dec = {}
        if c == "resume":
            for d in ("retry", "accept", "skip"):
                for sid in getattr(args, d):
                    if sid in dec:
                        raise MissionError(f"step {sid} has two decisions")
                    dec[sid] = d
        m = runner.run(args.id, trigger="manual", decisions=dec)
        print(M.summarize(m))
        return 0 if m["state"] == "completed" else 1
    if c in ("pause", "cancel"):
        from jarvis.missions import service
        print((service.request_pause if c == "pause" else service.request_cancel)(store, args.id))
        return 0
    if c == "delete":
        if store.is_running_elsewhere(args.id):
            raise MissionError("it is running; cancel it first")
        sch = __import__("jarvis.missions.schedule", fromlist=["x"]).Scheduler(cfg.profile_dir, store)
        for s in sch.list():
            if s["mission"] == args.id:
                sch.delete_schedule(s["id"])
        print("Deleted (and its schedules)." if store.delete(args.id) else "No such mission.")
        store.delete_history(args.id)
        return 0
    if c == "history":
        if args.delete:
            print(f"Deleted {store.delete_history(args.id)} history record(s) and the stored executions.")
            return 0
        for r in store.history(args.id, args.n):
            print(json.dumps(r))
        return 0
    if c == "export":
        write_private_text(args.out, json.dumps(store.export(args.id), indent=2, default=str))
        print(f"Exported to {Path(args.out).resolve()} (redacted; plain JSON).")
        return 0
    if c == "context":
        if args.add:
            from jarvis.memory.secrets import find_secret
            if find_secret(args.add):
                raise MissionError("that note looks like a secret; it was not stored")
            m["context"].append({"text": args.add[:300], "source": "you"})
        if args.remove:
            if not 1 <= args.remove <= len(m["context"]):
                raise MissionError("no such note number")
            m["context"].pop(args.remove - 1)
        if args.add or args.remove:
            m["updated"] = M.now()
            store.save(m)
        for i, cx in enumerate(m["context"], 1):
            print(f"{i}. [{cx.get('source', '?')}] {cx['text']}")
        if not m["context"]:
            print("(no remembered information is stored with this mission)")
        return 0
    return 2


def _fmt(s):
    extra = {"once": f"at {s.get('at')}", "daily": f"daily {s.get('time')}", "weekly": f"weekly {','.join(['mon','tue','wed','thu','fri','sat','sun'][d] for d in s.get('weekdays', []))} {s.get('time')}",
             "interval": f"every {s.get('every_minutes')} min"}[s["kind"]]
    ap = s.get("approval")
    apt = f"approved for {ap['max_runs'] - ap['runs_used']} more run(s) until {ap['expires']}" if ap else "no approval"
    return f"{s['id']}  {s['state']:9} {extra} ({s['tz']})  mission {s['mission']}  runs {s['runs']}  {apt}" + (f"  [{s['reason']}]" if s.get("reason") else "")


def _schedule(args, cfg, store, confirm, clock):
    import time as _time
    from jarvis.missions.schedule import ScheduleError, Scheduler
    c = args.scmd
    needs_runner = c in ("add", "approve", "edit", "run-due", "watch")
    runner = ex = None
    if needs_runner:
        runner, ex = _runner(cfg, args, store, confirm)
    sch = Scheduler(cfg.profile_dir, store, clock)
    specs = ex.specs if ex else None
    if c == "add":
        kind = "once" if args.once else "daily" if args.daily else "weekly" if args.weekly else "interval"
        s = sch.create(args.mission, kind, tz=args.tz, at=args.once, time_=args.daily or args.weekly, weekdays=args.weekdays, every_minutes=args.every_minutes,
                       missed=args.missed, grace=args.grace, specs=specs)
        print(_fmt(s))
        from jarvis.missions import model as M
        if M.consequential(store.load(args.mission), specs):
            print(f"\nThis mission changes things, so the schedule will NOT run until you approve it:\n  jarvis schedule approve {s['id']} --hash {store.load(args.mission)['plan_hash'][:12]}")
        print("\nSchedules run only while `jarvis schedule watch` or `run-due` is running. Closed or asleep = nothing runs.")
        return 0
    if c == "list":
        rows = sch.list()
        print("\n".join(_fmt(s) for s in rows) if rows else "(no schedules)")
        return 0
    if c == "show":
        print(json.dumps(sch.get(args.id), indent=2))
        return 0
    if c == "approve":
        ap = sch.approve(args.id, args.hash, args.max_runs, args.days)
        print(f"Approved: this exact plan, at most {ap['max_runs']} run(s), until {ap['expires']}. It is checked again at every run, along with permissions and your confirmations.")
        return 0
    if c in ("pause", "resume", "cancel"):
        sch.set_state(args.id, {"pause": "paused", "resume": "active", "cancel": "cancelled"}[c], f"{c} by you")
        print(_fmt(sch.get(args.id)))
        return 0
    if c == "delete":
        sch.delete_schedule(args.id)
        print("Deleted.")
        return 0
    if c == "edit":
        print(_fmt(sch.edit(args.id, tz=args.tz, at=args.at, time_=args.time, weekdays=args.weekdays, every_minutes=args.every_minutes, specs=specs)))
        return 0
    if c == "history":
        for r in [h for h in store.history(None, 500) if h.get("event") == "schedule" and (not args.id or h.get("schedule") == args.id)][-args.n:]:
            print(json.dumps(r))
        return 0
    if c in ("run-due", "watch"):
        store.recover_all()
        sch.recover_claims()
        if ex.env.stop.is_set():
            raise ScheduleError("the emergency stop is active (`jarvis desktop clear-stop`)")

        def once():
            for e in sch.tick(runner):
                print(json.dumps(e))
        if c == "run-due":
            once()
            return 0
        poll = min(max(args.poll_seconds, 5), 3600)
        end = _time.monotonic() + min(max(args.max_seconds, 10), 86400)
        print(f"Watching every {poll} s for up to {args.max_seconds} s. Schedules fire ONLY while this window stays open. Stop: Ctrl+C or `jarvis desktop stop`.", file=sys.stderr)
        last_wall = _time.time()
        try:
            while _time.monotonic() < end:
                ex.env.stop.check()
                if _time.time() - last_wall > poll * 2 + 60:
                    print("note: time jumped (sleep or suspend?). Occurrences during the gap count as missed unless still inside their grace window.", file=sys.stderr)
                last_wall = _time.time()
                once()
                ex.env.stop.wait(poll)
        except Exception as e:  # noqa: BLE001 - Stopped or Ctrl+C
            print(f"Stopped watching ({type(e).__name__}).", file=sys.stderr)
        return 0
    return 2
