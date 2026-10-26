"""Command line for plugins, workflow skills and research settings. These commands are the ONLY way to install, review, activate or roll back
a plugin or skill: they are deliberately not tools the model can call."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parsers(sub) -> None:
    pl = sub.add_parser("plugin", help="code plugins: install, review, test, activate, roll back (docs/PROMPT7_REVIEW.md)")
    ps = pl.add_subparsers(dest="pcmd", required=True)
    ps.add_parser("list", help="installed plugins, active versions, test status")
    ps.add_parser("install", help="copy a plugin folder into Jarvis (inactive) and show its review").add_argument("folder")
    for n in ("review", "test"):
        p = ps.add_parser(n)
        p.add_argument("name")
        p.add_argument("version")
    a = ps.add_parser("activate", help="activate ONE version; you must pass the code hash you reviewed")
    a.add_argument("name")
    a.add_argument("version")
    a.add_argument("--hash", required=True, help="first 12+ characters of the code hash shown by `review`")
    a.add_argument("--accept-warnings", action="store_true")
    for n in ("deactivate", "rollback"):
        ps.add_parser(n).add_argument("name")
    sk = sub.add_parser("skill", help="workflow skills: saved, reviewed, versioned task lists")
    ss = sk.add_subparsers(dest="scmd", required=True)
    ss.add_parser("list")
    c = ss.add_parser("create", help="save a task file as a new DRAFT version")
    c.add_argument("name")
    c.add_argument("file")
    c.add_argument("--description", default="")
    c.add_argument("--from-run", default="", help="id of a COMPLETED run in the task history that this workflow came from")
    r = ss.add_parser("review", help="preview every step; --approve records that you read it")
    r.add_argument("name")
    r.add_argument("--version", type=int, required=True)
    r.add_argument("--approve", action="store_true")
    ac = ss.add_parser("activate")
    ac.add_argument("name")
    ac.add_argument("--version", type=int, required=True)
    for n in ("deactivate", "rollback", "run"):
        ss.add_parser(n).add_argument("name")
    rs = sub.add_parser("research", help="settings for the optional online research tools")
    rss = rs.add_subparsers(dest="rcmd", required=True)
    rss.add_parser("status")
    rss.add_parser("set-searxng", help="use YOUR OWN SearXNG instance for web_search").add_argument("url")


def run(args, cfg) -> int:
    if getattr(args, "guest", False):
        print("Guest mode cannot use plugins, skills or research settings.", file=sys.stderr)
        return 2
    try:
        return _run(args, cfg)
    except Exception as e:  # noqa: BLE001 - every failure is reported as a plain message
        from jarvis.dev.plugins import PluginError
        from jarvis.dev.skills import SkillError
        if isinstance(e, (PluginError, SkillError)):
            print(f"refused: {e}", file=sys.stderr)
            return 1
        raise


def _show_review(rev: dict) -> None:
    print(f"code hash: {rev['hash']}")
    for k, v in rev["facts"].items():
        print(f"  {k}: {v}")
    for b in rev["blockers"]:
        print(f"  BLOCKER: {b}")
    for w in rev["warnings"]:
        print(f"  warning: {w}")
    print("  => " + ("nothing blocks activation (this is not a safety guarantee; the sandbox is the boundary)" if rev["ok"] else "CANNOT be activated"))


def _run(args, cfg) -> int:
    if args.cmd == "plugin":
        from jarvis.dev.plugins import Store
        st = Store(cfg.profile_dir)
        c = args.pcmd
        if c == "list":
            rows = st.status()
            for r in rows:
                print(f"{r['name']}: versions {r['versions']} active={r['active']}")
                for v, t in r["tests"].items():
                    print(f"    {v}: {t}")
            _, problems = st.active_specs()
            for p in problems:
                print(f"  PROBLEM: {p}")
            if not rows:
                print("no plugins installed")
        elif c == "install":
            rev = st.install(args.folder)
            print(f"installed {rev['facts'].get('name')} {rev['facts'].get('version')} (INACTIVE). Review:")
            _show_review(rev)
        elif c == "review":
            _show_review(st.review_installed(args.name, args.version))
            print("test status: " + st.test_status(args.name, args.version))
        elif c == "test":
            rec = st.run_tests(args.name, args.version)
            print(f"{rec['passed']}/{rec['total']} passed (sandbox)")
            for r in rec["results"]:
                print(f"  test {r['n']}: {'ok' if r['ok'] else 'FAILED'} {r['note']}")
            return 0 if rec["passed"] == rec["total"] else 1
        elif c == "activate":
            st.activate(args.name, args.version, args.hash, args.accept_warnings)
            print(f"{args.name} {args.version} is ACTIVE as tool plugin_{args.name}. Enable the 'plugins' permission to use it: jarvis desktop enable plugins")
        elif c == "deactivate":
            st.deactivate(args.name)
            print(f"{args.name} deactivated")
        elif c == "rollback":
            print(f"{args.name} rolled back to {st.rollback(args.name)}")
        return 0
    if args.cmd == "skill":
        from jarvis.dev.skills import Skills, SkillError
        sk = Skills(cfg.profile_dir)
        c = args.scmd
        if c == "list":
            for r in sk.list():
                print(f"{r['name']}: versions {r['versions']} active={r['active']} - {r['status']}\n    {r['description']}")
            return 0
        if c == "create":
            try:
                task = json.loads(Path(args.file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                print(f"cannot read the task file: {e}", file=sys.stderr)
                return 2
            run_rec = None
            if args.from_run:
                from jarvis.desktop.task import history
                run_rec = next((h for h in history(cfg.profile_dir / "desktop_tasks.jsonl", 500) if h.get("id") == args.from_run), None)
                if run_rec is None:
                    raise SkillError(f"no run '{args.from_run}' in the task history")
            rec = sk.create(args.name, task, args.description, source=args.file, from_run=run_rec)
            print(f"saved {rec['name']} v{rec['version']} as a DRAFT (not reviewed, never run). Next: jarvis skill review {rec['name']} --version {rec['version']}")
            return 0
        if c in ("deactivate", "rollback"):
            if c == "deactivate":
                sk.deactivate(args.name)
                print("deactivated")
            else:
                print(f"rolled back to v{sk.rollback(args.name)}")
            return 0
        from jarvis.desktop.cli_desktop import build
        from jarvis.desktop.task import Runner
        env, ex = build(cfg, False, False)
        runner = Runner(ex, cfg.profile_dir / "desktop_tasks.jsonl", progress=lambda s: print(s, file=sys.stderr))
        if c == "review":
            info = sk.review(args.name, args.version, runner, approve=args.approve)
            for p in info["steps"]:
                print(f"step {p['step']}: {p['tool']} risk={p.get('risk', '?')} {'(asks you)' if p.get('needs_confirmation') else ''} {p.get('problem') or ''}".rstrip())
            for pr in info["problems"]:
                print(f"  PROBLEM: {pr}")
            print("approved: you have recorded that you read it" if args.approve else "(preview only; add --approve after reading)")
            return 1 if info["problems"] else 0
        if c == "activate":
            sk.activate(args.name, args.version)
            print(f"{args.name} v{args.version} is active")
            return 0
        if c == "run":
            rep, run_rec = sk.run(args.name, runner)
            print(rep.text())
            return 0 if rep.status == "completed" else 1
    if args.cmd == "research":
        from jarvis.net.policy import NetworkPolicy
        cfgf = cfg.profile_dir / "research_config.json"
        if args.rcmd == "set-searxng":
            import urllib.parse
            u = urllib.parse.urlsplit(args.url)
            if u.scheme != "https" or not u.hostname:
                print("the SearXNG address must start with https://", file=sys.stderr)
                return 2
            cfgf.parent.mkdir(parents=True, exist_ok=True)
            cfgf.write_text(json.dumps({"searxng_url": args.url.rstrip("/")}), encoding="utf-8")
            print(f"web_search with provider 'searxng' will ask {u.hostname} (it will see your search words).")
            return 0
        pol = NetworkPolicy.load(cfg.settings_path)
        print(f"internet feature 'web_research': {'ON' if pol.is_enabled('web_research') else 'off'}  (jarvis net enable web_research)")
        print("offline tools that always work: read_document, list_sources, verify_quote, cite_sources, compare_sources, search_code, analyze_error")
        try:
            print("searxng: " + json.loads(cfgf.read_text(encoding="utf-8")).get("searxng_url", "(not set)"))
        except (OSError, ValueError):
            print("searxng: (not set)")
        return 0
    return 1
