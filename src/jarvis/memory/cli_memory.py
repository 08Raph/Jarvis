"""`jarvis memory ...` commands: inspect, edit, export, delete and approve what Jarvis has saved about you."""
from __future__ import annotations

from jarvis.security.private_file import write_private_text

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.memory.personal import CATEGORIES, MemoryError_, NO_RETRAINING_STATEMENT, PersonalMemory, ProcedureNotVerified
from jarvis.memory.secrets import SecretRejected


def add_parsers(sub) -> None:
    me = sub.add_parser("memory", help="see, edit, export and delete what Jarvis has saved about you (stays on this PC)")
    ms = me.add_subparsers(dest="memcmd", required=True)
    ms.add_parser("where", help="show where this profile's data is stored")
    ml = ms.add_parser("list", help="list saved memories")
    ml.add_argument("--category", choices=CATEGORIES)
    ml.add_argument("--all", action="store_true", help="include superseded and pending items")
    ms.add_parser("pending", help="suggestions waiting for your approval")
    msh = ms.add_parser("show")
    msh.add_argument("id", type=int)
    ma = ms.add_parser("add", help="save a memory yourself")
    ma.add_argument("category", choices=CATEGORIES)
    ma.add_argument("text")
    ma.add_argument("--project")
    ma.add_argument("--expires-hours", type=float, help="forget automatically after this many hours")
    me2 = ms.add_parser("edit")
    me2.add_argument("id", type=int)
    me2.add_argument("text")
    for n in ("delete", "approve", "reject"):
        p = ms.add_parser(n)
        p.add_argument("id", type=int)
    fa = ms.add_parser("forget-all", help="delete ALL memories, saved conversations, notes and logs in this profile")
    fa.add_argument("--yes", action="store_true")
    fa.add_argument("--include-backups", action="store_true", help="also delete this profile's automatic database backups")
    fa.add_argument("--include-undo-points", action="store_true", help="also delete the undo points (backups of files Jarvis changed); you can then no longer undo those changes")
    se = ms.add_parser("search")
    se.add_argument("query")
    ex = ms.add_parser("export", help="write everything saved in this profile to a JSON file")
    ex.add_argument("--out", default="jarvis-memory-export.json")
    su = ms.add_parser("summaries", help="turn automatic end-of-chat summaries on or off (off by default)")
    su.add_argument("setting", nargs="?", choices=["on", "off", "status"], default="status")
    ms.add_parser("learning", help="what has changed about how Jarvis treats you, and what has not (no model retraining)")
    ps = ms.add_parser("procedure-start", help="register a procedure you are about to try (saved only after you verify it worked)")
    ps.add_argument("name")
    ps.add_argument("steps", nargs="+")
    pv = ms.add_parser("procedure-verify", help="record the result of a registered procedure")
    pv.add_argument("run_id", type=int)
    g = pv.add_mutually_exclusive_group(required=True)
    g.add_argument("--worked", action="store_true")
    g.add_argument("--failed", action="store_true")
    ms.add_parser("procedures", help="saved procedures and success/failure counts")
    sk = ms.add_parser("skill-add", help="save a reusable instruction")
    sk.add_argument("name")
    sk.add_argument("instructions")
    si = ms.add_parser("skill-improve", help="save a new version of a skill (the old one is kept as superseded)")
    si.add_argument("name")
    si.add_argument("instructions")
    si.add_argument("--reason", default="edited by you")


def _fmt(r: dict) -> str:
    exp = f", expires {r['expires_at'][:16]}" if r.get("expires_at") else ""
    proj = f", project {r['project']}" if r.get("project") else ""
    return (f"#{r['id']} [{r['category']}] {r['text']}\n      source: {r['source']}, confidence {r['confidence']:.2f}, "
            f"status {r['status']}, saved {r['created_at'][:16]}{exp}{proj}")


# Folders in the profile that hold COPIES of the user's documents, web pages, project files or program output.
DERIVED_DATA_DIRS = ("research_sources", "code_snapshots", "code_runs")
# Undo points: full copies of files Jarvis changed, kept so changes can be reversed. Kept by default (deleting them removes the undo), removed on request.
UNDO_DIRS = ("desktop_checkpoints", "code_backups")


def forget_all_profile(cfg, mem, include_backups: bool = False, include_undo_points: bool = False) -> str:
    """Delete everything this profile saved (shared by `memory forget-all` and the desktop window). Returns the plain-language result."""
    mem.forget_everything()
    import shutil
    for f in ("desktop_audit.jsonl", "desktop_audit.1.jsonl", "desktop_tasks.jsonl", "app_index.json"):
        (cfg.profile_dir / f).unlink(missing_ok=True)
    shutil.rmtree(cfg.profile_dir / "screenshots", ignore_errors=True)
    # Copies of what Jarvis read or ran for you (Prompt 7): stored document/web text, project snapshots and run outputs. Found by the Prompt 11 audit:
    # these were NOT removed by forget-all before.
    for d in DERIVED_DATA_DIRS:
        shutil.rmtree(cfg.profile_dir / d, ignore_errors=True)
    if include_undo_points:
        for d in UNDO_DIRS:
            shutil.rmtree(cfg.profile_dir / d, ignore_errors=True)
    from jarvis.missions.store import MissionStore
    MissionStore(cfg.profile_dir).delete_all()                      # missions, schedules and their history (Prompt 9)
    removed = 0
    if include_backups and cfg.backups_dir.exists():
        for f in cfg.backups_dir.glob(f"{cfg.memory_db.stem}-*.sqlite"):
            f.unlink(missing_ok=True)
            removed += 1
    return ("Everything in this profile's memory was deleted from the live database."
            + (f" {removed} backup file(s) were deleted too." if include_backups else
               f" Backups made earlier are separate files that still contain the old data; add --include-backups or delete them from {cfg.backups_dir}.")
            + " Desktop-control logs, task history, missions, schedules, screenshots, stored research sources, project snapshots and code-run outputs were deleted."
            + (" Undo points (backups of files Jarvis changed) were deleted too." if include_undo_points else
               f" File backups made before Jarvis changed your files (undo points) are kept in {cfg.profile_dir / 'desktop_checkpoints'} and"
               f" {cfg.profile_dir / 'code_backups'}; add --include-undo-points to delete them (you then cannot undo those changes).")
            + " Export files you saved yourself are not tracked: delete those too. Overwriting is not guaranteed on SSDs or some filesystems.")


def run(args, cfg) -> int:
    if getattr(args, "guest", False):
        print("Guest sessions save nothing, so there is no memory to manage. Run without --guest.", file=sys.stderr)
        return 2
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    mem = PersonalMemory.open(cfg.memory_db)
    try:
        return _run(args, cfg, mem)
    except (MemoryError_, ProcedureNotVerified) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        mem.close()


def _run(args, cfg, mem: PersonalMemory) -> int:
    c = args.memcmd
    if c == "where":
        size = cfg.memory_db.stat().st_size if cfg.memory_db.exists() else 0
        print(json.dumps({"profile": cfg.profile, "memory_database": str(cfg.memory_db), "size_bytes": size,
                          "personality_and_voice_settings": str(cfg.profile_dir),
                          "backups": str(cfg.backups_dir),
                          "shared_by_all_profiles": "downloaded AI models (not personal) in " + str(cfg.models_dir),
                          "note": "Stored on this PC only, inside this Windows account's profile folder. The file is NOT encrypted: "
                                  "it is protected only by Windows file permissions. Nothing is uploaded. Backups of the database "
                                  "hold the same data; they do not include personality/voice settings or downloaded models."}, indent=2))
        return 0
    if c == "list":
        rows = mem.list(args.category, status=None if args.all else "active")
        if not rows:
            print("Nothing saved yet.")
        for r in rows:
            print(_fmt(r))
        return 0
    if c == "pending":
        rows = mem.list(status="pending")
        print("\n".join(_fmt(r) for r in rows) if rows else "Nothing is waiting for approval.")
        return 0
    if c == "show":
        r = mem.get(args.id)
        if not r:
            print("no such memory", file=sys.stderr)
            return 2
        print(json.dumps(r, indent=2, default=str))
        return 0
    if c == "add":
        exp = datetime.now(timezone.utc) + timedelta(hours=args.expires_hours) if args.expires_hours else None
        try:
            r = mem.add(args.category, args.text, source="user_said", project=args.project, expires_at=exp)
        except SecretRejected as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Saved memory #{r['id']}. Remove it any time with: jarvis memory delete {r['id']}")
        return 0
    if c == "edit":
        try:
            r = mem.update(args.id, args.text)
        except SecretRejected as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(_fmt(r))
        return 0
    if c == "delete":
        ok = mem.delete(args.id)
        print("Deleted." if ok else "No such memory.")
        return 0 if ok else 2
    if c == "approve":
        print(_fmt(mem.approve(args.id)))
        return 0
    if c == "reject":
        ok = mem.reject(args.id)
        print("Rejected and deleted." if ok else "No such pending suggestion.")
        return 0 if ok else 2
    if c == "forget-all":
        if not args.yes:
            print("This deletes every saved memory, conversation, note and log in this profile. Run again with --yes to confirm.",
                  file=sys.stderr)
            return 2
        print(forget_all_profile(cfg, mem, args.include_backups, getattr(args, 'include_undo_points', False)))
        return 0
    if c == "search":
        hits = mem.search(args.query, limit=8, touch=False)
        print("\n".join(_fmt(h) + f"\n      match score {h['score']}" for h in hits) if hits else "No saved memory matches that.")
        return 0
    if c == "export":
        out = Path(args.out)
        write_private_text(out, json.dumps(mem.export(), indent=2, default=str))
        print(f"Exported to {out.resolve()} (plain JSON; it contains your personal memory, so keep it somewhere private).")
        return 0
    if c == "summaries":
        from jarvis.assistant import set_summaries, summaries_enabled
        if args.setting in ("on", "off"):
            set_summaries(cfg, args.setting == "on")
        on = summaries_enabled(cfg)
        print(f"End-of-chat summaries are {'ON' if on else 'OFF'}." + (
            " When you close a chat of 3+ messages, Jarvis asks the LOCAL model for a 3-sentence note (secrets removed) and saves it as "
            "a visible memory. Nothing is sent anywhere." if on else " Chats are not summarised or saved."))
        return 0
    if c == "learning":
        print(json.dumps(mem.learning_report(), indent=2))
        print(NO_RETRAINING_STATEMENT)
        return 0
    if c == "procedure-start":
        rid = mem.record_run(args.name, "unverified", steps=args.steps)
        print(f"Registered run #{rid} of \"{args.name}\". After you try it: jarvis memory procedure-verify {rid} --worked  (or --failed)")
        return 0
    if c == "procedure-verify":
        row = mem.db.execute("SELECT * FROM workflow_runs WHERE id=?", (args.run_id,)).fetchone()
        if not row or row["outcome"] != "unverified":
            print("No unverified run with that number.", file=sys.stderr)
            return 2
        steps = json.loads(row["steps"])
        if args.failed:
            mem.resolve_run(args.run_id, "failure", "user_confirmed")
            print("Recorded as a failure; it will not be saved as a procedure.")
            return 0
        mem.resolve_run(args.run_id, "success", "user_confirmed")
        r = mem.save_procedure(row["name"], steps, args.run_id)
        print(f"Verified by you and saved as procedure memory #{r['id']}.")
        return 0
    if c == "procedures":
        for r in mem.list("procedure"):
            m = r["meta"]
            print(f"#{r['id']} {m.get('name')}: worked {m.get('successes', 0)}x, failed {m.get('failures', 0)}x  | steps: " +
                  " -> ".join(m.get("steps", [])))
        if not mem.list("procedure"):
            print("No verified procedures saved yet.")
        return 0
    if c == "skill-add":
        r = mem.save_skill(args.name, args.instructions)
        print(f"Saved skill #{r['id']} (version {r['meta']['version']}).")
        return 0
    if c == "skill-improve":
        r = mem.improve_skill(args.name, args.instructions, args.reason, by_user=True)
        print(f"Saved version {r['meta']['version']} as #{r['id']}; the previous version is kept as superseded.")
        return 0
    return 2
