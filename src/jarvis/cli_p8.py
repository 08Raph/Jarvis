"""Command line for operating modes, monitoring settings and Gaming settings. These are settings YOU change; none of them is a tool the model can call.
Choosing a mode or changing a metric list never turns a permission on and never records a consent (use `jarvis desktop enable ...` for that)."""
from __future__ import annotations

import json
import sys

COMMANDS = ("mode", "monitor", "gaming")


def add_parsers(sub) -> None:
    m = sub.add_parser("mode", help="operating mode: general, coding, research, gaming, productivity (restricts tools; never grants permissions)")
    ms = m.add_subparsers(dest="mcmd", required=True)
    ms.add_parser("list", help="the five modes and what each allows")
    ms.add_parser("show", help="one mode in full (tools, restrictions, behaviour)").add_argument("name")
    ms.add_parser("set", help="choose the mode (does not change any permission)").add_argument("name")
    ms.add_parser("clear", help="no mode restriction (the behaviour of earlier versions)")
    ms.add_parser("status", help="the current mode")
    mo = sub.add_parser("monitor", help="system monitoring settings (readings are taken only on request)")
    mos = mo.add_subparsers(dest="mocmd", required=True)
    mos.add_parser("status")
    c = mos.add_parser("config", help="how often a fresh reading is taken (cache) and which parts are read")
    c.add_argument("--interval", type=float, help="seconds, 1-300 (readings are reused for this long; there is no background polling)")
    c.add_argument("--metrics", help="comma list of: cpu,memory,disk,gpu,temperature")
    g = sub.add_parser("gaming", help="Gaming mode settings")
    gs = g.add_subparsers(dest="gcmd", required=True)
    gs.add_parser("status")
    gc = gs.add_parser("config", help="choose which metrics Gaming mode collects")
    gc.add_argument("--metrics", required=True, help="comma list of: detection,cpu,memory,gpu,fps (empty string switches all off)")


def run(args, cfg) -> int:
    pd = cfg.profile_dir
    if args.cmd == "mode":
        from jarvis import modes
        c = args.mcmd
        if c == "list":
            for m in modes.MODES.values():
                print(f"{m.name:13} {m.summary}  ({len(m.tools)} tools)")
            cur = modes.current(pd)
            print(f"\ncurrent: {cur.name if cur else 'none selected (no restriction; same permissions)'}")
            return 0
        if c == "show":
            if args.name not in modes.MODES:
                print(f"unknown mode; choose from {', '.join(modes.NAMES)}", file=sys.stderr)
                return 2
            print(json.dumps(modes.describe(modes.MODES[args.name]), indent=2))
            return 0
        if c == "set":
            if getattr(args, "guest", False):
                print("Guest mode cannot change settings.", file=sys.stderr)
                return 2
            try:
                m = modes.set_mode(pd, args.name)
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
            print(f"mode: {m.name}. This only limits which tools are offered; it changed no permission and recorded no consent.")
            return 0
        if c == "clear":
            modes.clear_mode(pd)
            print("mode cleared (no restriction; permissions unchanged)")
            return 0
        cur = modes.current(pd)
        print(f"current mode: {cur.name if cur else 'none selected'}")
        return 0
    if getattr(args, "guest", False) and ((args.cmd == "monitor" and args.mocmd == "config") or (args.cmd == "gaming" and args.gcmd == "config")):
        print("Guest mode cannot change settings.", file=sys.stderr)
        return 2
    if args.cmd == "monitor":
        from jarvis.monitor import system as M
        cur = M.MonitorConfig.load(pd)
        if args.mocmd == "config":
            if args.interval is not None:
                if not (M.MIN_INTERVAL <= args.interval <= M.MAX_INTERVAL):
                    print(f"interval must be {M.MIN_INTERVAL}-{M.MAX_INTERVAL} seconds", file=sys.stderr)
                    return 2
                cur.interval_s = args.interval
            if args.metrics is not None:
                want = [x.strip() for x in args.metrics.split(",") if x.strip()]
                bad = [x for x in want if x not in M.PARTS]
                if bad:
                    print(f"unknown metric(s) {bad}; choose from {', '.join(M.PARTS)}", file=sys.stderr)
                    return 2
                cur.parts = tuple(want)
            cur.save(pd)
        print(json.dumps({"interval_s": cur.interval_s, "metrics": list(cur.parts), "background_polling": False}, indent=2))
        return 0
    if args.cmd == "gaming":
        from jarvis.monitor import gaming as G
        cur = G.GamingConfig.load(pd)
        if args.gcmd == "config":
            want = [x.strip() for x in args.metrics.split(",") if x.strip()]
            bad = [x for x in want if x not in G.METRICS]
            if bad:
                print(f"unknown metric(s) {bad}; choose from {', '.join(G.METRICS)}", file=sys.stderr)
                return 2
            cur = G.GamingConfig(want)
            cur.save(pd)
        print(json.dumps({"metrics": list(cur.metrics), "known_games": len(G.CATALOG), "fps_source": "imported PresentMon/CapFrameX logs only"}, indent=2))
        return 0
    return 2
