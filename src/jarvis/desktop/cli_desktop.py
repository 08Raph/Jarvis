"""`jarvis desktop ...` and `jarvis agent "..."` commands."""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from jarvis.desktop import policy as P
from jarvis.desktop.paths import default_protected
from jarvis.desktop.audit import AuditLog
from jarvis.desktop.executor import ConfirmRequest, Executor
from jarvis.desktop.stop import EmergencyStop
from jarvis.desktop.task import Runner, Task, TaskError, history
from jarvis.desktop.tools import Env, registry


def add_parsers(sub) -> None:
    d = sub.add_parser("desktop", help="permission-controlled control of this PC: apps, windows, files, UI (docs/DESKTOP_CONTROL.md)")
    d.add_argument("--simulate", action="store_true", help="use the FAKE simulated desktop (safe demo; nothing on this PC is touched)")
    ds = d.add_subparsers(dest="dcmd", required=True)
    ds.add_parser("status", help="permissions, allowed folders and what works on this PC")
    for n in ("enable", "disable"):
        p = ds.add_parser(n, help=f"{n} permission categories: " + ", ".join(P.CATEGORIES))
        p.add_argument("categories", nargs="+")
        if n == "enable":
            p.add_argument("--yes", action="store_true", help="agree to the notice for screen / monitoring / gaming without being asked (you still see the notice)")
    for n in ("allow-folder", "remove-folder"):
        ds.add_parser(n).add_argument("folder")
    for n in ("allow-project", "remove-project"):
        ds.add_parser(n, help="project folders the coding tools may read/edit (separate from allowed folders)").add_argument("folder")
    for n in ("trust-app", "untrust-app"):
        ds.add_parser(n, help="apps that may start without asking each time").add_argument("app_id")
    ds.add_parser("input-confirm", help="ask before every keystroke/click action, or once per window per task").add_argument(
        "mode", choices=("every_action", "once_per_task"))
    t = ds.add_parser("tools", help="list every tool, its risk and permission")
    t.add_argument("--json", action="store_true")
    r = ds.add_parser("run", help="run ONE tool (with the normal checks and confirmations)")
    r.add_argument("tool")
    r.add_argument("--args", default="{}", help='JSON object, e.g. \'{"path": "notes.txt"}\'')
    tk = ds.add_parser("task", help="run a task file (JSON list of steps with checks)")
    tk.add_argument("file")
    tk.add_argument("--preview", action="store_true", help="show what each step would need, without doing anything")
    for n in ("history", "audit"):
        ds.add_parser(n).add_argument("-n", type=int, default=20)
    ds.add_parser("checkpoints", help="backups/undo points made before file changes")
    ds.add_parser("undo").add_argument("checkpoint")
    ds.add_parser("stop", help="EMERGENCY STOP: makes any running Jarvis desktop task stop (creates a STOP file)")
    ds.add_parser("clear-stop", help="remove the STOP file so tasks can run again")

    ag = sub.add_parser("agent", help="let the local AI model operate this PC for one goal, step by step, with your approval")
    ag.add_argument("goal")
    ag.add_argument("--max-steps", type=int, default=12)
    ag.add_argument("--simulate", action="store_true", help="use the FAKE simulated desktop")
    sub.add_parser("_desktop-worker", help=argparse.SUPPRESS)


def _settings_path(cfg) -> Path:
    return cfg.profile_dir / P.FILE_NAME


def _protected(cfg) -> list:
    """Jarvis' own data folders: never allowed as a work folder, never reachable by file tools."""
    out = list(default_protected(cfg.profile_dir))
    for extra in (cfg.data_dir, cfg.model_dir, cfg.models_dir):
        if extra not in out:
            out.append(extra)
    return out


def console_confirm(req: ConfirmRequest) -> bool:
    print("\n=== Jarvis wants to do this ===", file=sys.stderr)
    print(req.description, file=sys.stderr)
    try:
        ans = input("Type  yes  to allow, anything else to refuse: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return ans.strip().lower() == "yes"


def build(cfg, guest: bool, simulate: bool, confirm=console_confirm, backend=None, isolation=None):
    from jarvis.desktop.backend import SimulatedDesktop, default_backend
    settings = P.Settings.load(_settings_path(cfg), protected=_protected(cfg)) if not guest else P.Settings()
    if backend is None:
        backend = SimulatedDesktop() if simulate else default_backend()
    profile_dir = cfg.profile_dir
    stop = EmergencyStop(profile_dir / "STOP")
    from jarvis.net.policy import NetworkPolicy
    net = NetworkPolicy() if guest else NetworkPolicy.load(cfg.settings_path)
    env = Env(backend, settings, profile_dir, stop, app_allowlist=dict(cfg.app_allowlist), guest=guest,
              extra_protected=tuple(_protected(cfg)), net=net)
    audit = AuditLog(None if guest else profile_dir / "desktop_audit.jsonl")
    from jarvis.extensions import all_specs
    ex = Executor(env, audit, confirm, isolation=isolation, extra_specs=all_specs(None if guest else profile_dir))
    return env, ex


def _install_stop(env) -> None:
    try:
        signal.signal(signal.SIGINT, lambda *_: env.stop.stop("Ctrl+C"))
    except ValueError:
        pass                                   # not in the main thread (tests)
    if env.stop.start_hotkey():
        print(f"[emergency stop: press {EmergencyStop.HOTKEY}, Ctrl+C, or run `jarvis desktop stop` in another window]", file=sys.stderr)
    else:
        print("[emergency stop: Ctrl+C, or run `jarvis desktop stop` in another window]", file=sys.stderr)


def _status(cfg, env) -> dict:
    b = env.backend
    out = {"profile": cfg.profile, "backend": b.name + (" (FAKE simulator)" if b.simulated else ""),
           "permissions": {c: {"on": env.settings.enabled.get(c, False), "means": m} for c, m in P.CATEGORIES.items()},
           "allowed_folders": env.settings.allowed_folders, "trusted_apps": env.settings.trusted_apps,
           "input_confirm": env.settings.input_confirm, "settings_file": str(_settings_path(cfg)),
           "emergency_stop_active": env.stop.is_set(), "consents_recorded": dict(env.settings.consents)}
    from jarvis import modes as _modes
    _cur = _modes.current(cfg.profile_dir)
    out["mode"] = _cur.name if _cur else None
    try:
        out["running_as_administrator"] = b.elevated()
    except Exception:  # noqa: BLE001
        out["running_as_administrator"] = None
    if sys.platform != "win32" and not b.simulated:
        out["note"] = "Not Windows: only files, system info and launching allow-listed programs work here."
    return out


def run(args, cfg) -> int:
    if args.cmd == "_desktop-worker":
        from jarvis.desktop.executor import worker_main
        return worker_main()
    guest = bool(getattr(args, "guest", False))
    if args.cmd == "agent":
        return _agent(args, cfg, guest)
    c = args.dcmd
    sp = _settings_path(cfg)
    if c in ("enable", "disable", "allow-folder", "remove-folder", "allow-project", "remove-project", "trust-app", "untrust-app", "input-confirm"):
        if guest:
            print("Guest mode cannot change desktop permissions.", file=sys.stderr)
            return 2
        s = P.Settings.load(sp, protected=_protected(cfg))
        try:
            if c in ("enable", "disable"):
                bad = [x for x in args.categories if x not in P.CATEGORIES]
                if bad:
                    print(f"unknown categories {bad}; choose from {', '.join(P.CATEGORIES)}", file=sys.stderr)
                    return 2
                from jarvis import consent as _consent
                for x in args.categories:
                    if c == "enable" and x in _consent.NOTICES and not _consent.ask(x, assume_yes=getattr(args, "yes", False)):
                        print(f"{x}: left OFF (consent not given)")
                        continue
                    s.enabled[x] = c == "enable"
                    if c == "enable":
                        _consent.record(s, x)
                    else:
                        _consent.withdraw(s, x)
                    print(f"{x}: {'ON' if c == 'enable' else 'off'} - {P.CATEGORIES[x]}")
                if c == "enable" and "files_write" in args.categories and not s.allowed_folders:
                    print("No folder is allowed yet; add one with: jarvis desktop allow-folder \"C:\\Users\\you\\Documents\\JarvisWork\"")
            elif c == "allow-folder":
                print(f"allowed: {s.allow_folder(args.folder, protected=_protected(cfg))}")
            elif c == "allow-project":
                print(f"project allowed: {s.allow_project(args.folder, protected=_protected(cfg))}")
            elif c == "remove-project":
                r = str(Path(args.folder).resolve())
                s.project_folders = [f for f in s.project_folders if f.lower() != r.lower()]
                print(f"project removed: {r}")
            elif c == "remove-folder":
                r = str(Path(args.folder).resolve())
                s.allowed_folders = [f for f in s.allowed_folders if f.lower() != r.lower()]
                print(f"removed: {r}")
            elif c == "trust-app":
                s.trusted_apps = sorted(set(s.trusted_apps) | {args.app_id.lower()})
                print(f"{args.app_id} may start without asking (installers always ask)")
            elif c == "untrust-app":
                s.trusted_apps = [a for a in s.trusted_apps if a != args.app_id.lower()]
                print(f"{args.app_id} will ask before starting")
            else:
                s.input_confirm = args.mode
                print(f"input confirmation: {args.mode}")
        except P.PathRejected as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        s.save(sp)
        return 0
    if c == "stop":
        (cfg.profile_dir).mkdir(parents=True, exist_ok=True)
        (cfg.profile_dir / "STOP").write_text("stop", encoding="utf-8")
        print("EMERGENCY STOP set. Running desktop tasks stop at their next check; new ones refuse to start. "
              "Run `jarvis desktop clear-stop` to allow tasks again.")
        return 0
    if c == "clear-stop":
        (cfg.profile_dir / "STOP").unlink(missing_ok=True)
        print("Emergency stop cleared.")
        return 0
    if c == "tools":
        specs = registry()
        if args.json:
            print(json.dumps([s.json_schema() | {"category": s.category} for s in specs.values()], indent=2))
        else:
            for s in specs.values():
                print(f"{s.name:18} {s.risk:12} [{s.category}] {s.description}")
        return 0
    if c == "history":
        for h in history(cfg.profile_dir / "desktop_tasks.jsonl", args.n):
            print(f"{h['at']} {h['id']} {h['status']}: {h['goal']}" + (f" | {h['blocker']}" if h.get("blocker") else ""))
        return 0
    if c == "audit":
        for rec in AuditLog(cfg.profile_dir / "desktop_audit.jsonl").tail(args.n):
            print(json.dumps(rec, ensure_ascii=False))
        return 0

    env, ex = build(cfg, guest, args.simulate)
    if c == "status":
        print(json.dumps(_status(cfg, env), indent=2))
        return 0
    if c == "checkpoints":
        for r in env.checkpoints.list():
            print(f"{r['id']}  {r['kind']:8} {r['original']}" + ("  (undone)" if r.get("undone") else ""))
        return 0
    if env.stop.is_set():
        print("Emergency stop is active (`jarvis desktop clear-stop` to allow tasks again).", file=sys.stderr)
        return 3
    _install_stop(env)
    runner = Runner(ex, None if guest else cfg.profile_dir / "desktop_tasks.jsonl", progress=lambda s: print(s, file=sys.stderr))
    if c in ("run", "undo"):
        tool, a = ("undo", {"checkpoint": args.checkpoint}) if c == "undo" else (args.tool, None)
        if a is None:
            try:
                a = json.loads(args.args)
            except ValueError as e:
                print(f"--args must be a JSON object: {e}", file=sys.stderr)
                return 2
        rep = runner.run(Task(f"{tool} (single step)", [__import__("jarvis.desktop.task", fromlist=["Step"]).Step(tool, a)]))
        st = rep.steps[0].action if rep.steps else None
        print(rep.text())
        if st and st.outcome and st.outcome.data:
            print(json.dumps(st.outcome.data, indent=2, default=str))
        if st and st.outcome and st.outcome.untrusted_text and tool in ("read_file", "read_screen_text", "file_search"):
            print("----- content (from outside Jarvis) -----")
            print(st.outcome.untrusted_text)
        return 0 if rep.status in ("completed", "completed_unverified") else 1
    if c == "task":
        try:
            task = Task.from_dict(json.loads(Path(args.file).read_text(encoding="utf-8")))
        except (OSError, ValueError, TaskError) as e:
            print(f"cannot read task: {e}", file=sys.stderr)
            return 2
        if args.preview:
            for p in runner.preview(task):
                state = "OK" if p.get("ok") else "BLOCKED"
                print(f"step {p['step']}: {p['tool']} [{state}] risk={p.get('risk', '?')} "
                      f"{'needs your confirmation' if p.get('needs_confirmation') else ''} {p.get('problem') or ''}".rstrip())
                for ch in p["checks"]:
                    print(f"     check: {ch}")
            print("(preview only: nothing was done)")
            return 0
        rep = runner.run(task)
        print(rep.text())
        return 0 if rep.status == "completed" else 1
    return 1


def _agent(args, cfg, guest, engine=None, confirm=console_confirm, backend=None) -> int:
    from jarvis.desktop.agent import DesktopAgent
    if engine is None:
        from jarvis.inference.gguf_engine import GGUFEngine, RuntimeUnavailable
        from jarvis.models.manager import ModelStore
        store = ModelStore(cfg.models_dir)
        active = store.active()
        if active is None or store.kind(active) != "gguf":
            print("No chat model is installed. Run `jarvis setup` or `jarvis models fetch qwen3-1.7b-q4_k_m`.", file=sys.stderr)
            return 2
        ok, problems = store.verify(active, quick=True)
        if not ok:
            print(f"model failed its integrity check: {problems}", file=sys.stderr)
            return 3
        try:
            engine = GGUFEngine(store.path(active))
        except RuntimeUnavailable as e:
            print(str(e), file=sys.stderr)
            return 2
    env, ex = build(cfg, guest, args.simulate, confirm=confirm, backend=backend)
    if env.stop.is_set():
        print("Emergency stop is active (`jarvis desktop clear-stop`).", file=sys.stderr)
        return 3
    _install_stop(env)
    agent = DesktopAgent(lambda msgs: engine.chat(msgs, 512, 0.2), ex, max_steps=max(1, min(args.max_steps, 30)),
                         progress=lambda s: print(s, file=sys.stderr))
    res = agent.run(args.goal)
    print(res.text())
    return 0 if all(s.status == "verified" for s in res.steps) and not res.stopped_reason else 1
