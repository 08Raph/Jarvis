"""Games: library detection (Steam, Epic), save-game folders, requirement checks, Gaming Mode with automatic restore. Never touches anti-cheat or game files."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, Unavailable, cap, human_size

GAMING_PLAN = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"       # Windows "High performance"
ANTICHEAT = {"easyanticheat.exe", "easyanticheat_eos.exe", "beservice.exe", "bedaisy.sys", "vgc.exe", "vgtray.exe", "faceitclient.exe", "faceit.exe", "eaanticheat.exe"}


def _steam_roots() -> list[Path]:
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        b = os.environ.get(env)
        if b:
            cands.append(Path(b) / "Steam")
    cands += [Path(f"{d}:/SteamLibrary") for d in "CDEFG"] + [Path(f"{d}:/Steam") for d in "DEFG"]
    return [c for c in cands if c.exists()]


def steam_libraries(steam_root: Path) -> list[Path]:
    libs = [steam_root / "steamapps"]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore")):
            p = Path(m.group(1).replace("\\\\", "\\")) / "steamapps"
            if p not in libs:
                libs.append(p)
    except OSError:
        pass
    return [l for l in libs if l.is_dir()]


def parse_acf(text: str) -> dict:
    out = {}
    for k in ("appid", "name", "installdir", "SizeOnDisk"):
        m = re.search(rf'"{k}"\s+"([^"]*)"', text)
        if m:
            out[k] = m.group(1)
    return out


def steam_games(roots: list[Path] | None = None) -> list[dict]:
    games = []
    for root in (roots if roots is not None else _steam_roots()):
        for lib in steam_libraries(root):
            for f in lib.glob("appmanifest_*.acf"):
                try:
                    d = parse_acf(f.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    continue
                if d.get("name"):
                    games.append({"launcher": "Steam", "name": d["name"], "appid": d.get("appid"), "folder": str(lib / "common" / d.get("installdir", "")), "size": int(d.get("SizeOnDisk", 0) or 0)})
    return games


def epic_games(manifest_dir: Path | None = None) -> list[dict]:
    d = manifest_dir or Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    out = []
    if d.is_dir():
        for f in d.glob("*.item"):
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
                out.append({"launcher": "Epic", "name": j.get("DisplayName"), "folder": j.get("InstallLocation"), "size": int(j.get("InstallSize", 0) or 0), "exe": j.get("LaunchExecutable")})
            except (OSError, ValueError):
                pass
    return out


@cap("games.list", "gaming", "List installed games (Steam and Epic)", LOW, (), False, "34")
def list_games(op, steam_roots=None, epic_dir=None) -> Outcome:
    g = sorted(steam_games(steam_roots) + epic_games(epic_dir), key=lambda x: (x["name"] or "").lower())
    if not g:
        return Outcome("info", "I don't see any Steam or Epic games installed (other launchers aren't supported yet).")
    return Outcome("info", f"{len(g)} game(s): " + "; ".join(f"{x['name']} ({x['launcher']}, {human_size(x['size'])})" for x in g[:25]), g)


@cap("games.launcher", "gaming", "Open Steam or the Epic Games Launcher", LOW, ("name",), True, "34")
def open_launcher(op, name: str = "steam") -> Outcome:
    n = name.lower()
    if "epic" in n:
        op.ops.open_target("com.epicgames.launcher://apps")
    elif "steam" in n:
        op.ops.open_target("steam://open/main")
    else:
        raise OperatorError("I can open Steam or the Epic Games Launcher.")
    return Outcome("unverified", f"Asked Windows to open {name}.")


def save_folders(home: Path) -> list[Path]:
    out = []
    for p in (home / "Saved Games", home / "Documents" / "My Games"):
        if p.is_dir():
            out += [c for c in p.iterdir() if c.is_dir()]
    return out


@cap("games.saves", "gaming", "Find save-game folders in the usual places", LOW, (), False, "34")
def saves(op) -> Outcome:
    s = save_folders(op.home)
    return Outcome("info", f"{len(s)} save folder(s): " + ", ".join(p.name for p in s[:15]) + ". (Many games keep saves elsewhere, e.g. AppData or Steam's cloud.)" if s else "I found no save folders in Saved Games or Documents\\My Games.", [str(p) for p in s])


@cap("games.backup_saves", "backup", "Back up a game's save folder", MEDIUM, ("game",), False, "34,8")
def backup_saves(op, game: str = "") -> Outcome:
    from jarvis.operator import backup
    hit = [p for p in save_folders(op.home) if game.lower() in p.name.lower()]
    if not hit:
        raise OperatorError(f"I can't find a save folder for {game} in the usual places.")
    return backup.create(op, source=str(hit[0]))


@cap("games.requirements", "gaming", "Compare this PC with a game's stated requirements", LOW, ("ram_gb", "disk_gb", "cores"), False, "34,33")
def requirements(op, ram_gb: float = 0, disk_gb: float = 0, cores: int = 0) -> Outcome:
    from jarvis.operator.system_ops import _psutil
    ps = _psutil()
    ram = ps.virtual_memory().total / 1024 ** 3
    cpu = ps.cpu_count(logical=True) or 0
    free = ps.disk_usage(str(op.home)).free / 1024 ** 3
    res = []
    if ram_gb:
        res.append(f"RAM {ram:.1f} GB vs {ram_gb:g} needed: {'OK' if ram >= ram_gb else 'TOO LOW'}")
    if cores:
        res.append(f"{cpu} threads vs {cores} needed: {'OK' if cpu >= cores else 'TOO LOW'}")
    if disk_gb:
        res.append(f"{free:.0f} GB free vs {disk_gb:g} needed: {'OK' if free >= disk_gb else 'NOT ENOUGH'}")
    return Outcome("info", "; ".join(res) + ". I can't read your graphics card's rating, so check the GPU line yourself." if res else "Tell me the RAM, free disk space or CPU threads the game asks for.")


def _gm_file(op) -> Path:
    return op.folder / "gaming_mode.json"


def _gpu_line(op) -> str:
    import shutil
    if shutil.which("nvidia-smi"):
        try:
            code, out, _e = op.ops.run(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total", "--format=csv,noheader"], 8)
            if code == 0 and out.strip():
                u, t, mu, mt = [x.strip() for x in out.splitlines()[0].split(",")]
                return f"GPU {u} busy, {t} C, memory {mu} of {mt}"
        except Exception:  # noqa: BLE001
            pass
    return "GPU use isn't readable here (only NVIDIA cards are supported)"


@cap("gaming.start", "gaming", "Start Gaming Mode (high-performance power plan, close your approved apps)", HIGH, (), True, "33")
def start(op) -> Outcome:
    if _gm_file(op).exists():
        return Outcome("info", "Gaming Mode is already on.")
    code, out, _e = op.ops.run(["powercfg.exe", "/getactivescheme"], 10)
    m = re.search(r"GUID:\s*([0-9a-fA-F\-]{36})", out)
    prev = m.group(1) if m else None
    if any(p.name.lower() in ANTICHEAT for p in _procs()):
        note = " An anti-cheat is running; I don't touch it or the game."
    else:
        note = ""
    closed = []
    from jarvis.operator import apps_ops
    for nm in op.state()["game_close_list"]:
        try:
            r = apps_ops.close_app(op, nm)
            if r.status == "verified" and "Closed" in r.message:
                closed.append(nm)
        except OperatorError:
            pass
    op.ops.run(["powercfg.exe", "/setactive", GAMING_PLAN], 10)
    code, out2, _e = op.ops.run(["powercfg.exe", "/getactivescheme"], 10)
    ok = GAMING_PLAN in out2.lower()
    _gm_file(op).write_text(json.dumps({"previous_plan": prev, "closed": closed, "started": time.time()}), encoding="utf-8")
    return Outcome("verified" if ok else "unverified", "Gaming Mode on: " + ("High performance power plan active" if ok else "I couldn't confirm the power plan changed (your PC may not have the High performance plan)") + (f"; closed {', '.join(closed)}" if closed else "") + "." + note, {"previous": prev, "closed": closed})


def _procs():
    from jarvis.operator.system_ops import _psutil
    ps = _psutil()
    out = []
    for p in ps.process_iter(["name"]):
        out.append(type("P", (), {"name": p.info["name"] or ""}))
    return out


@cap("gaming.stop", "gaming", "End Gaming Mode and restore the previous power plan", MEDIUM, (), True, "33")
def stop(op) -> Outcome:
    f = _gm_file(op)
    if not f.exists():
        return Outcome("info", "Gaming Mode isn't on.")
    d = json.loads(f.read_text(encoding="utf-8"))
    note = ""
    if d.get("previous_plan"):
        op.ops.run(["powercfg.exe", "/setactive", d["previous_plan"]], 10)
        code, out, _e = op.ops.run(["powercfg.exe", "/getactivescheme"], 10)
        ok = d["previous_plan"].lower() in out.lower()
        note = "Power plan restored." if ok else "I tried to restore the power plan but couldn't confirm it."
    f.unlink()
    closed = d.get("closed") or []
    return Outcome("verified" if "restored" in note else "unverified", f"Gaming Mode off. {note}" + (f" Apps I closed earlier ({', '.join(closed)}) are not reopened automatically; open them if you need them." if closed else ""))


@cap("gaming.status", "gaming", "Show live CPU, RAM and GPU figures for gaming", LOW, (), False, "33")
def status(op) -> Outcome:
    from jarvis.operator.system_ops import _psutil
    ps = _psutil()
    cpu = ps.cpu_percent(interval=0.5)
    vm = ps.virtual_memory()
    on = _gm_file(op).exists()
    t = ""
    try:
        temps = ps.sensors_temperatures()
        flat = [x.current for v in temps.values() for x in v if x.current]
        if flat:
            t = f", hottest sensor {max(flat):.0f} C"
    except (AttributeError, OSError):
        pass
    return Outcome("info", f"Gaming Mode {'ON' if on else 'off'}. CPU {cpu:.0f}%, RAM {vm.percent:.0f}%{t}. {_gpu_line(op)}. FPS: I can't measure it without a trusted tool like the game's own overlay, so I won't guess.", {"cpu": cpu})


@cap("gaming.set_close_list", "gaming", "Choose which apps Gaming Mode may close", MEDIUM, ("names",), False, "33")
def set_close_list(op, names=None) -> Outcome:
    lst = [n.strip().lower() for n in (names.split(",") if isinstance(names, str) else (names or [])) if n.strip()]
    op.set_game_close_list(lst)
    return Outcome("verified", "Gaming Mode may close: " + (", ".join(lst) if lst else "nothing") + ".")
