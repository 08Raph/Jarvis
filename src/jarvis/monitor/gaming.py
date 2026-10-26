"""Gaming mode: honest, read-only, and separate from desktop automation.

WHAT IT DOES: recognises known games among the running programs; shows CPU / memory / GPU readings from real sources (see monitor.system);
imports frame-time logs that a tool YOU ran (PresentMon, CapFrameX, ...) wrote, and computes FPS statistics from them; compares runs; keeps
per-game profiles of your own notes and preferences.
WHAT IT DOES NOT DO: it sends no keys or clicks, reads no game memory, hooks or injects nothing, touches no anti-cheat or DRM, changes no
system or game setting (a profile's suggestions are text for YOU to apply), and measures no FPS itself - without an imported log, FPS and frame
time are reported as UNAVAILABLE and the tool says what is needed. An FPS "improvement" is only reported when repeated runs show a
difference bigger than their own run-to-run variation; otherwise it says so."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
import time
from pathlib import Path

from jarvis.desktop import schema as S
from jarvis.desktop.paths import PathMissing, PathRejected
from jarvis.desktop.policy import ANTI_CHEAT_PROCESSES
from jarvis.desktop.schema import Arg, Outcome, ToolSpec
from jarvis.monitor import readings as RD
from jarvis.monitor import system as M

CONFIG_FILE = "gaming.json"
METRICS = ("detection", "cpu", "memory", "gpu", "fps")
MAX_LOG_BYTES, MAX_ROWS, MIN_FRAMES = 60 * 1024 * 1024, 600_000, 30
MIN_RUNS = 3
PROFILE_DIR = "game_profiles"
SLUG = re.compile(r"^[a-z][a-z0-9_\-]{1,40}$")
# Known games (executable name, lower case). `anticheat` means the game normally runs with anti-cheat; Jarvis never looks at such windows.
CATALOG = {
    "csgo.exe": ("Counter-Strike (legacy)", True), "cs2.exe": ("Counter-Strike 2", True), "valorant-win64-shipping.exe": ("VALORANT", True),
    "fortniteclient-win64-shipping.exe": ("Fortnite", True), "r5apex.exe": ("Apex Legends", True), "overwatch.exe": ("Overwatch", True),
    "rainbowsix.exe": ("Rainbow Six Siege", True), "pubg.exe": ("PUBG", True), "tslgame.exe": ("PUBG", True), "destiny2.exe": ("Destiny 2", True),
    "eldenring.exe": ("Elden Ring", True), "gta5.exe": ("Grand Theft Auto V", True), "cyberpunk2077.exe": ("Cyberpunk 2077", False),
    "witcher3.exe": ("The Witcher 3", False), "minecraft.exe": ("Minecraft", False), "javaw.exe": ("Minecraft (Java)", False),
    "hl2.exe": ("Half-Life 2", False), "factorio.exe": ("Factorio", False), "stardew valley.exe": ("Stardew Valley", False),
    "hades.exe": ("Hades", False), "terraria.exe": ("Terraria", False), "rocketleague.exe": ("Rocket League", True),
    "leagueoflegends.exe": ("League of Legends", True), "dota2.exe": ("Dota 2", True), "skyrimse.exe": ("Skyrim SE", False),
}
GENERIC_PROCS = {"javaw.exe"}      # also runs non-games, so it is only a weak match
CHEAT_WORDS = re.compile(r"(?i)\b(aim\s?bot|aim\s?assist hack|wall\s?hack|trigger\s?bot|no[\s-]?recoil|recoil (control|script|macro|compensation)|esp\b|radar hack|"
                         r"bypass\w*\s+(the\s+)?(anti[\s-]?cheat|drm|vac|battleye|easy anti)|inject\w*|dll injection|memory (editing|hack|write)|cheat\w*|hack\w*|"
                         r"spoof\w*|rapid[\s-]?fire|auto[\s-]?(aim|fire|shoot)|macro (for|to) (win|advantage))\b")
ANTICHEAT_NOTE = ("This game uses anti-cheat. Jarvis does not capture its window, send it input, read its memory or interact with its protection; "
                  "only whole-PC counters (CPU, RAM, GPU) and logs you import are used.")


class GamingError(ValueError):
    pass


def refuse_cheating(text: str) -> None:
    if CHEAT_WORDS.search(text or ""):
        raise PathRejected("Jarvis does not help with cheats, aimbots, recoil scripts, memory editing, injection or bypassing anti-cheat/DRM, "
                         "and will not store notes or settings for them")


# ------------------------------------------------------------------ config (written by the command line only)
class GamingConfig:
    def __init__(self, metrics=METRICS):
        self.metrics = tuple(metrics)

    @classmethod
    def load(cls, profile_dir) -> "GamingConfig":
        try:
            raw = json.loads((Path(profile_dir) / CONFIG_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        m = raw.get("metrics") if isinstance(raw, dict) else None
        return cls([x for x in m if x in METRICS] if isinstance(m, list) else METRICS)

    def save(self, profile_dir) -> None:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        (Path(profile_dir) / CONFIG_FILE).write_text(json.dumps({"metrics": list(self.metrics)}), encoding="utf-8")


# ------------------------------------------------------------------ detection
def detect(env) -> dict:
    try:
        procs = {p.lower() for p in env.backend.process_names()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"the list of running programs is not available here ({type(e).__name__})", "games": [], "anticheat_running": []}
    try:
        fg = env.backend.foreground()
        fgp = next((w["process"].lower() for w in env.backend.windows() if w["hwnd"] == fg and w.get("process")), None)
    except Exception:  # noqa: BLE001
        fgp = None
    games = []
    for exe in sorted(procs & set(CATALOG)):
        name, ac = CATALOG[exe]
        games.append({"exe": exe, "name": name, "anticheat": ac, "foreground": exe == fgp, "match": "weak (also used by other programs)" if exe in GENERIC_PROCS else "exact program name"})
    return {"ok": True, "why": "", "games": games, "anticheat_running": sorted(procs & ANTI_CHEAT_PROCESSES)}


def capture_protection(env):
    """A function for the screen-capture guard: windows of anti-cheat games (and any game while anti-cheat software runs) are never captured."""
    def protected(proc: str, running) -> str | None:
        proc = (proc or "").lower()
        if proc in CATALOG and CATALOG[proc][1]:
            return "this is a game that uses anti-cheat; Jarvis does not look at it"
        if proc in CATALOG and (set(running or ()) & ANTI_CHEAT_PROCESSES):
            return "anti-cheat software is running next to this game; Jarvis does not look at it"
        return None
    return protected


# ------------------------------------------------------------------ frame-time logs (imported, never measured here)
FRAME_COLS = ("frametime", "msbetweenpresents", "msbetweendisplaychange")
APP_COLS = ("application", "processname", "process")


def _pct(sorted_vals: list[float], p: float) -> float:
    k = max(0, min(len(sorted_vals) - 1, math.ceil(p / 100 * len(sorted_vals)) - 1))
    return sorted_vals[k]


def parse_frametimes(path: Path) -> dict:
    """Parse a PresentMon / CapFrameX style CSV (milliseconds per frame). Raises GamingError with the reason when it cannot be trusted."""
    size = path.stat().st_size
    if size > MAX_LOG_BYTES:
        raise GamingError(f"the log is {size // 1024 // 1024} MB; the limit is {MAX_LOG_BYTES // 1024 // 1024} MB")
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise GamingError("this is not a text/CSV file")
    sha = hashlib.sha256(raw).hexdigest()
    rows = csv.reader(raw.decode("utf-8-sig", errors="replace").splitlines())
    try:
        head = next(rows)
    except StopIteration:
        raise GamingError("the file is empty") from None
    low = [h.strip().lower() for h in head]
    col = next((low.index(c) for c in FRAME_COLS if c in low), None)
    if col is None:
        raise GamingError("no frame-time column found (expected 'FrameTime' or 'MsBetweenPresents', in milliseconds, as written by PresentMon or CapFrameX)")
    appc = next((low.index(c) for c in APP_COLS if c in low), None)
    vals, bad, apps = [], 0, set()
    for i, r in enumerate(rows):
        if i >= MAX_ROWS:
            raise GamingError(f"more than {MAX_ROWS} rows; split the log")
        if len(r) <= col:
            bad += 1
            continue
        try:
            v = float(r[col])
        except ValueError:
            bad += 1
            continue
        if not math.isfinite(v) or v <= 0 or v > 10_000:
            bad += 1
            continue
        vals.append(v)
        if appc is not None and len(r) > appc and len(apps) < 5:
            apps.add(r[appc][:60])
    if len(vals) < MIN_FRAMES:
        raise GamingError(f"only {len(vals)} valid frame(s); at least {MIN_FRAMES} are needed for meaningful statistics")
    if bad > 0.05 * (len(vals) + bad):
        raise GamingError(f"{bad} of {len(vals) + bad} rows are invalid (not a positive number of milliseconds under 10000); the log is not trusted")
    s = sorted(vals)
    total_ms = sum(vals)
    worst1 = s[-max(1, len(s) // 100):]
    return {"frames": len(vals), "invalid_rows": bad, "duration_s": round(total_ms / 1000, 2), "sha256": sha, "column": head[col], "applications": sorted(apps),
            "avg_fps": round(len(vals) / (total_ms / 1000), 2), "frametime_ms_avg": round(statistics.fmean(vals), 3), "frametime_ms_median": round(_pct(s, 50), 3),
            "frametime_ms_p95": round(_pct(s, 95), 3), "frametime_ms_p99": round(_pct(s, 99), 3), "frametime_ms_max": round(s[-1], 3),
            "fps_1pct_low": round(1000 / _pct(s, 99), 2), "fps_1pct_low_avg": round(1000 / statistics.fmean(worst1), 2)}


def stat_readings(st: dict, name: str) -> list[RD.Reading]:
    src = f"imported log {name} (sha256 {st['sha256'][:12]}, {st['frames']} frames)"
    m = "computed from the imported log's frame times"
    return [RD.Reading("fps_average", st["avg_fps"], "fps", "imported", src, RD.now_iso(), m), RD.Reading("fps_1pct_low", st["fps_1pct_low"], "fps", "imported", src, RD.now_iso(), m),
            RD.Reading("frametime_average", st["frametime_ms_avg"], "ms", "imported", src, RD.now_iso(), m), RD.Reading("frametime_p99", st["frametime_ms_p99"], "ms", "imported", src, RD.now_iso(), m)]


def compare_runs(a: list[dict], b: list[dict], la: str, lb: str) -> dict:
    """Is B different from A? Only a difference larger than the runs' own spread counts, and only with >= MIN_RUNS runs of each."""
    fa, fb = [x["avg_fps"] for x in a], [x["avg_fps"] for x in b]
    ma, mb = statistics.fmean(fa), statistics.fmean(fb)
    sa = statistics.stdev(fa) if len(fa) > 1 else None
    sb = statistics.stdev(fb) if len(fb) > 1 else None
    diff = mb - ma
    out = {"a": {"label": la, "runs": len(fa), "mean_fps": round(ma, 2), "stdev_fps": None if sa is None else round(sa, 2)},
           "b": {"label": lb, "runs": len(fb), "mean_fps": round(mb, 2), "stdev_fps": None if sb is None else round(sb, 2)},
           "difference_fps": round(diff, 2), "difference_percent": round(100 * diff / ma, 2) if ma else None}
    if len(fa) < MIN_RUNS or len(fb) < MIN_RUNS:
        out["conclusion"] = (f"NOT ENOUGH RUNS: {len(fa)} vs {len(fb)}; at least {MIN_RUNS} of each are needed before any difference can be called real. "
                             "No improvement or regression is claimed.")
        out["reproducible"] = False
        return out
    spread = max(sa or 0.0, sb or 0.0)
    if abs(diff) > 2 * spread and abs(diff) > 0:
        out["reproducible"] = True
        out["conclusion"] = (f"'{lb}' averaged {abs(diff):.2f} fps {'higher' if diff > 0 else 'lower'} than '{la}' ({abs(out['difference_percent'] or 0):.1f} %), more than twice the "
                             f"run-to-run spread ({spread:.2f} fps) across {len(fa)} and {len(fb)} runs. This is a measurement on this PC with these logs only; "
                             "it does not show that the setting caused it (other things may have changed between runs).")
    else:
        out["reproducible"] = False
        out["conclusion"] = (f"NO RELIABLE DIFFERENCE: {abs(diff):.2f} fps is not more than twice the run-to-run spread ({spread:.2f} fps). "
                             "No improvement or regression is claimed.")
    return out


# ------------------------------------------------------------------ profiles (Jarvis-side notes only)
def profiles_dir(env) -> Path:
    return env.profile_dir / PROFILE_DIR


def load_profile(env, gid: str) -> dict:
    if not SLUG.match(gid):
        raise PathRejected("a game id is 2-41 characters: a lowercase letter, then lowercase letters, digits, - or _")
    try:
        d = json.loads((profiles_dir(env) / f"{gid}.json").read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except OSError:
        raise PathMissing(f"no profile '{gid}'") from None
    except ValueError:
        pass
    raise PathMissing(f"profile '{gid}' is unreadable")


def list_profiles(env) -> list[dict]:
    out = []
    for f in sorted(profiles_dir(env).glob("*.json")) if profiles_dir(env).is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": f.stem, "game": str(d.get("game", ""))[:80], "exe": str(d.get("exe", ""))[:80]})
        except (OSError, ValueError, AttributeError):
            continue
    return out


# ------------------------------------------------------------------ tools
def _cfg(env) -> GamingConfig:
    return GamingConfig.load(env.profile_dir)


def _availability(env, cfg: GamingConfig) -> list[dict]:
    mon = M.RealSource() if env.ext.get("monitor_source") is None else env.ext["monitor_source"]
    rows = []
    for m in METRICS:
        if m not in cfg.metrics:
            rows.append({"metric": m, "state": "disabled by you", "how": "jarvis gaming config --metrics ..."})
        elif m == "fps":
            rows.append({"metric": "fps / frame time", "state": "only from an imported log", "how": "Jarvis cannot measure FPS itself. Record a run with PresentMon or CapFrameX (free, "
                                                                                                    "separate programs), put the CSV in an allowed folder, then use game_fps_import"})
        elif m == "gpu":
            try:
                mon.gpu()
                rows.append({"metric": "gpu", "state": "available", "how": "nvidia-smi (NVIDIA drivers)"})
            except Exception as e:  # noqa: BLE001
                rows.append({"metric": "gpu", "state": "unavailable", "how": str(e)[:160] + " (needs NVIDIA drivers with nvidia-smi; other vendors are not supported yet)"})
        elif m == "detection":
            rows.append({"metric": "game detection", "state": "available (known program names only)", "how": "running program names compared with a built-in list and your profiles"})
        else:
            rows.append({"metric": m, "state": "available", "how": "operating-system counters (psutil adds more detail)"})
    return rows


def h_status(a, env) -> Outcome:
    cfg = _cfg(env)
    det = detect(env) if "detection" in cfg.metrics else {"ok": False, "why": "game detection is switched off in your gaming settings", "games": [], "anticheat_running": []}
    avail = _availability(env, cfg)
    lines = []
    if not det["ok"]:
        lines.append("detection: " + det["why"])
    elif not det["games"]:
        lines.append("no known game is running (the list is limited; an unknown game will not be recognised)")
    for g in det["games"]:
        lines.append(f"detected: {g['name']} ({g['exe']}), match: {g['match']}{', in front' if g['foreground'] else ''}{'; ' + ANTICHEAT_NOTE if g['anticheat'] else ''}")
    if det["anticheat_running"]:
        lines.append("anti-cheat software running: " + ", ".join(det["anticheat_running"]) + ". Jarvis stays read-only and away from it.")
    lines += [f"{r['metric']}: {r['state']} - {r['how']}" for r in avail]
    return Outcome(True, f"{len(det['games'])} known game(s) detected; metric availability listed", verified=True if det["ok"] else None,
                   evidence="running program names compared with a fixed list" if det["ok"] else "", data={"detection": det, "metrics": avail, "enabled_metrics": list(cfg.metrics)},
                   untrusted_text="\n".join(lines))


def h_metrics(a, env) -> Outcome:
    from jarvis.monitor.tools import monitor_for
    cfg = _cfg(env)
    mon = monitor_for(env)
    want = [p for p in ("cpu", "memory", "gpu") if p in cfg.metrics]
    rs: list[RD.Reading] = []
    if want:
        rs, _ = mon.snapshot(want)
    if "fps" in cfg.metrics:
        rs = rs + [RD.na("fps", "fps", "none", "FPS and frame time are not measured by Jarvis; import a PresentMon / CapFrameX log with game_fps_import")]
    if not rs:
        return Outcome(False, "no metric is switched on in your gaming settings (jarvis gaming config)", verified=None, evidence="")
    det = detect(env) if "detection" in cfg.metrics else {"games": []}
    ok = any(r.kind != "unavailable" for r in rs)
    ctx = ("a known game is running: " + ", ".join(g["name"] for g in det["games"])) if det["games"] else "no known game was detected, so these are whole-PC readings, not the game's own"
    return Outcome(ok, f"{sum(r.kind != 'unavailable' for r in rs)} real reading(s); {ctx}", verified=True if ok else None,
                   evidence="whole-PC counters read just now; they are not per-game and not FPS", data={"readings": [r.to_dict() for r in rs], "context": ctx},
                   untrusted_text="\n".join(r.line() for r in rs))


def _log_path(env, raw):
    if not env.settings.enabled.get("files_read"):
        raise PathRejected("reading a log file also needs the 'files_read' permission (jarvis desktop enable files_read); Gaming mode does not grant it")
    return env.paths.resolve(raw, must_exist=True)


def _bind_import(a, env):
    p = _log_path(env, a["path"])
    return dict(a, path=str(p)), [f"reads the frame-time log {p} (read-only, up to {MAX_LOG_BYTES // 1024 // 1024} MB)", "nothing is changed or sent"], [str(p)]


def h_import(a, env) -> Outcome:
    p = _log_path(env, a["path"])
    try:
        st = parse_frametimes(p)
    except GamingError as e:
        return Outcome(False, f"the log was not used: {e}", verified=True, evidence="parsed the file; nothing was estimated")
    rs = stat_readings(st, p.name)
    txt = "\n".join(r.line() for r in rs) + f"\nframes: {st['frames']}, invalid rows dropped: {st['invalid_rows']}, duration: {st['duration_s']} s, column: {st['column']}"
    return Outcome(True, f"{st['frames']} frames: average {st['avg_fps']} fps, 1 % low {st['fps_1pct_low']} fps (from YOUR log; Jarvis did not measure it)", verified=True,
                   evidence="computed from the imported file; the numbers are only as good as the tool that wrote it", data={"stats": st, "readings": [r.to_dict() for r in rs]}, untrusted_text=txt)


def _bind_compare(a, env):
    refuse_cheating(a["a_label"] + " " + a["b_label"])
    ps = [str(_log_path(env, x)) for x in a["a_logs"] + a["b_logs"]]
    return dict(a, a_logs=ps[:len(a["a_logs"])], b_logs=ps[len(a["a_logs"]):]), [f"reads {len(ps)} frame-time log(s) (read-only) and compares '{a['a_label']}' with '{a['b_label']}'", "nothing is changed or sent"], ps


def h_compare(a, env) -> Outcome:
    refuse_cheating(a["a_label"] + " " + a["b_label"])
    runs = {"a": [], "b": []}
    for k, names in (("a", a["a_logs"]), ("b", a["b_logs"])):
        for n in names:
            try:
                runs[k].append(parse_frametimes(Path(n)))
            except GamingError as e:
                return Outcome(False, f"a log was not used ({Path(n).name}): {e}", verified=True, evidence="parsed the files; nothing was estimated")
    shas = [r["sha256"] for r in runs["a"] + runs["b"]]
    if len(set(shas)) != len(shas):
        return Outcome(False, "the same log was given more than once; repeated runs must be separate recordings", verified=True, evidence="compared file hashes")
    c = compare_runs(runs["a"], runs["b"], a["a_label"], a["b_label"])
    return Outcome(True, c["conclusion"], verified=True if c["reproducible"] else None, evidence=f"{len(runs['a'])} and {len(runs['b'])} separate imported runs; verdict rule: difference > 2x run-to-run spread, >= {MIN_RUNS} runs each",
                   data=c, untrusted_text=json.dumps(c, indent=1))


def h_plist(a, env) -> Outcome:
    ps = list_profiles(env)
    return Outcome(True, f"{len(ps)} game profile(s)", verified=True, evidence="read the profile folder", data={"profiles": ps}, untrusted_text="\n".join(f"{p['id']}: {p['game']} ({p['exe']})" for p in ps))


def h_pget(a, env) -> Outcome:
    d = load_profile(env, a["id"])
    return Outcome(True, f"profile {a['id']}", verified=True, evidence="read the profile file", data={"profile": d}, untrusted_text=json.dumps(d, indent=1)[:6000])


def _bind_psave(a, env):
    if not SLUG.match(a["id"]):
        raise PathRejected("a game id is 2-41 characters: a lowercase letter, then lowercase letters, digits, - or _")
    for t in [a["game"], a["notes"], a["exe"]] + list(a["suggestions"]):
        refuse_cheating(t)
    exists = (profiles_dir(env) / f"{a['id']}.json").exists()
    return a, [f"{'replaces' if exists else 'creates'} the Jarvis-side game profile '{a['id']}' (notes and suggestions only)",
               "it changes NO game, driver or Windows setting; the suggestions are text for you to apply yourself if you wish"], [a["id"], str(exists)]


def h_psave(a, env) -> Outcome:
    for t in [a["game"], a["notes"], a["exe"]] + list(a["suggestions"]):
        refuse_cheating(t)
    d = profiles_dir(env)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": a["id"], "game": a["game"], "exe": a["exe"].lower(), "notes": a["notes"], "suggestions": list(a["suggestions"]),
           "note": "Suggestions are for you to apply manually; Jarvis never changes game or system settings.", "updated": RD.now_iso()}
    f = d / f"{a['id']}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    tmp.replace(f)
    back = load_profile(env, a["id"])
    return Outcome(back.get("game") == a["game"], f"profile '{a['id']}' saved (no system setting was changed)", verified=back.get("game") == a["game"], evidence="re-read the saved file",
                   data={"id": a["id"]})


def specs() -> list[ToolSpec]:
    s = lambda *a, **k: ToolSpec(*a, **k)
    return [
        s("game_status", "Gaming mode: detected game (known programs only) and which metrics are available, unavailable or need extra software", "gaming", S.OBSERVE, (), h_status, timeout=30, untrusted_output=True),
        s("game_metrics", "Gaming mode: whole-PC CPU / memory / GPU readings (real sources only) and a clear 'FPS unavailable' unless imported", "gaming", S.OBSERVE, (), h_metrics, timeout=40),
        s("game_fps_import", "compute FPS / frame-time statistics from a PresentMon or CapFrameX CSV log in an allowed folder (needs files_read too)", "gaming", S.OBSERVE,
          (Arg("path", "str", max_len=500, no_newlines=True),), h_import, timeout=60, untrusted_output=True, bind=_bind_import),
        s("game_compare", "compare repeated FPS logs of two settings; reports a difference only if it is reproducible", "gaming", S.OBSERVE,
          (Arg("a_label", "str", max_len=60, no_newlines=True), Arg("a_logs", "list[str]", max_len=500, max_items=10), Arg("b_label", "str", max_len=60, no_newlines=True),
           Arg("b_logs", "list[str]", max_len=500, max_items=10)), h_compare, timeout=120, untrusted_output=True, bind=_bind_compare),
        s("game_profile_list", "list your saved game profiles", "gaming", S.OBSERVE, (), h_plist, timeout=15, untrusted_output=True),
        s("game_profile_get", "read one saved game profile", "gaming", S.OBSERVE, (Arg("id", "str", max_len=41, no_newlines=True),), h_pget, timeout=15, untrusted_output=True),
        s("game_profile_save", "save notes and manual suggestions for a game (never changes any setting)", "gaming", S.MODIFY,
          (Arg("id", "str", max_len=41, no_newlines=True), Arg("game", "str", max_len=80, no_newlines=True), Arg("exe", "str", required=False, default="", max_len=80, no_newlines=True),
           Arg("notes", "str", required=False, default="", max_len=1000), Arg("suggestions", "list[str]", required=False, default=[], max_len=200, max_items=10)),
          h_psave, timeout=15, bind=_bind_psave),
    ]
