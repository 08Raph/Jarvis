"""The complete, fixed list of desktop tools. Anything not listed here cannot be done - there is deliberately no tool for
running commands, scripts, PowerShell or arbitrary programs, and none for changing security or system settings."""
from __future__ import annotations

import hashlib
import json
import stat as _stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.desktop import files as F
from jarvis.desktop import schema as S
from jarvis.desktop import sysinfo
from jarvis.desktop.apps import is_installer, match
from jarvis.desktop.backend import DesktopBackend
from jarvis.desktop.checkpoints import Checkpoints
from jarvis.desktop import paths as _paths
from jarvis.desktop.paths import PathMissing, PathRejected, SafePaths, default_protected
from jarvis.desktop.policy import Settings
from jarvis.desktop.schema import Arg, Outcome, ToolSpec
from jarvis.desktop.stop import EmergencyStop

SCREENSHOT_KEEP = 20


@dataclass
class Env:
    backend: DesktopBackend
    settings: Settings
    profile_dir: Path
    stop: EmergencyStop
    app_allowlist: dict = field(default_factory=dict)
    guest: bool = False
    extra_protected: tuple = ()        # more folders of Jarvis' own data (e.g. the shared data folder), beyond the profile folder
    net: object = None                 # jarvis.net.policy.NetworkPolicy (only the research tools use it); None = no internet at all
    ext: dict = field(default_factory=dict)   # state of extension tools (monitor, vision memory); tests put fake sources here

    def __post_init__(self):
        # Jarvis' own data (permissions, audit log, checkpoints, memory, settings) is never reachable through file tools.
        self.protected = tuple(default_protected(self.profile_dir)) + tuple(Path(x) for x in self.extra_protected)
        self.paths = SafePaths(self.settings.allowed_folders, protected=self.protected)
        # PROJECT folders are a separate list the user adds on purpose; only there may the code tools touch source files.
        self.code_paths = SafePaths(self.settings.project_folders, protected=self.protected, source_files=True)
        self.checkpoints = Checkpoints(self.profile_dir / "desktop_checkpoints")
        self._apps = None

    @property
    def screenshots_dir(self) -> Path:
        return self.profile_dir / "screenshots"

    def apps(self, refresh: bool = False) -> list[dict]:
        cache = self.profile_dir / "app_index.json"
        if self._apps is None and not refresh and not self.backend.simulated and cache.exists():
            try:
                self._apps = json.loads(cache.read_text(encoding="utf-8"))
            except ValueError:
                self._apps = None
        if self._apps is None or refresh:
            self._apps = self.backend.discover_apps(self.app_allowlist)
            if not self.backend.simulated and not self.guest:
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(self._apps), encoding="utf-8")
                except OSError:
                    pass
        return self._apps


# ----------------------------------------------------------------------------- helpers
def resolve_window(env: Env, window: str | None, hwnd: int | None) -> dict:
    wins = env.backend.windows()
    if hwnd:
        for w in wins:
            if w["hwnd"] == hwnd:
                return w
        raise LookupError(f"no open window with handle {hwnd}")
    if not window:
        fg = env.backend.foreground()
        for w in wins:
            if w["hwnd"] == fg:
                return w
        raise LookupError("no window named and no foreground window found")
    q = window.lower()
    exact = [w for w in wins if w["title"].lower() == q]
    hits = exact or [w for w in wins if q in w["title"].lower() or q == (w.get("process") or "")]
    if not hits:
        raise LookupError(f"no open window matches '{window}'")
    if len(hits) > 1:
        raise LookupError(f"{len(hits)} windows match '{window}': " + "; ".join(f"[{w['hwnd']}] ({w.get('process')})" for w in hits[:8])
                          + " - give the handle (hwnd) to choose; list_windows shows the titles")      # titles are outside text: not echoed in an error
    return hits[0]


def target_window(env: Env, a: dict) -> dict:
    try:
        return resolve_window(env, a.get("window"), a.get("hwnd"))
    except Exception:  # noqa: BLE001
        return {}


def target_process(env: Env, a: dict) -> str | None:
    return target_window(env, a).get("process")


def _sel(a: dict) -> dict:
    return {"name": a.get("name"), "control_type": a.get("control_type"), "automation_id": a.get("automation_id"),
            "index": a.get("index"), "exact": a.get("exact")}


def _focus_checked(env: Env, w: dict) -> None:
    if env.backend.foreground() != w["hwnd"] and not env.backend.focus(w["hwnd"]):
        raise RuntimeError(f"Windows did not bring '{w['title']}' to the front, so nothing was typed or clicked")
    if env.backend.foreground() != w["hwnd"]:
        raise RuntimeError("the target window is not in front; input was not sent")


# ----------------------------------------------------------------------------- system
def h_system_info(a, env):
    parts = a["parts"] or None
    bad = [p for p in parts or [] if p not in ("cpu", "memory", "storage", "network", "gpu")]
    if bad:
        raise S.ArgError(f"unknown part(s): {bad}")
    snap = sysinfo.snapshot(parts)
    return Outcome(True, "system information read" + (" (some values unavailable; see 'why')" if '"available": false' in json.dumps(snap) else ""),
                   verified=True, evidence="read from the operating system just now", data=snap)


def h_wait(a, env):
    env.stop.wait(a["seconds"])
    return Outcome(True, f"waited {a['seconds']}s", verified=True)


# ----------------------------------------------------------------------------- apps
def h_list_apps(a, env):
    apps = env.apps(refresh=a["refresh"])
    q = (a["query"] or "").strip()
    hits = match(apps, q) if q else apps
    shown = hits[: a["max_results"]]
    lines = "\n".join(f"{x['id']}  |  {x['name']}  ({x['source']})" for x in shown)
    return Outcome(True, f"{len(hits)} app(s) found" + (f" for '{q}'" if q else ""), verified=True,
                   evidence=f"{env.backend.name} app index", data={"apps": shown, "total": len(hits)}, untrusted_text=lines)


def _resolve_app(env, a) -> dict:
    hits = match(env.apps(), a["app"])
    if not hits:
        hits = match(env.apps(refresh=True), a["app"])
    if not hits:
        raise LookupError(f"no installed app matches '{a['app']}' (try list_apps)")
    if len(hits) > 1:
        raise LookupError(f"{len(hits)} apps match '{a['app']}': " + "; ".join(f"{h['id']} ({h['name']})" for h in hits[:8])
                          + " - use the id")
    return hits[0]


def launch_risk(a, env):
    try:
        return S.INSTALL if is_installer(_resolve_app(env, a)) else S.LAUNCH
    except Exception:  # noqa: BLE001
        return S.LAUNCH


def h_launch_app(a, env):
    app = _resolve_app(env, a)
    r = env.backend.launch(app, a["wait_seconds"])
    ok = bool(r.get("verified")) or bool(r.get("handed_off"))
    return Outcome(ok, (f"{app['name']} is running" if r.get("verified") else f"asked Windows to open {app['name']} (not confirmed on screen)" if ok
                        else f"could not confirm that {app['name']} started"),
                   verified=r.get("verified"), evidence=r.get("evidence", ""),
                   data={"app": app["id"], "name": app["name"], **{k: r[k] for k in ("hwnd", "pid", "title") if k in r}})


# ----------------------------------------------------------------------------- windows
def h_list_windows(a, env):
    wins = env.backend.windows()
    fg = env.backend.foreground()
    for w in wins:
        w["foreground"] = w["hwnd"] == fg
    lines = "\n".join(f"[{w['hwnd']}] {w['title']}  ({w.get('process')})" + ("  <- in front" if w["foreground"] else "") for w in wins)
    return Outcome(True, f"{len(wins)} open window(s)", verified=True, evidence="enumerated now", data={"windows": wins},
                   untrusted_text=lines)


def h_focus_window(a, env):
    w = resolve_window(env, a["window"], a["hwnd"])
    env.backend.focus(w["hwnd"])
    ok = env.backend.foreground() == w["hwnd"]
    return Outcome(ok, f"'{w['title']}' is in front" if ok else f"Windows did not bring '{w['title']}' to the front",
                   verified=ok, evidence="foreground window checked after the switch", data={"hwnd": w["hwnd"], "title": w["title"]},
                   untrusted_text=w["title"])


def h_wait_for_window(a, env):
    q = a["window"].lower()
    end = time.monotonic() + a["timeout"]
    while True:
        hits = [w for w in env.backend.windows() if q in w["title"].lower() or q == (w.get("process") or "")]
        if hits:
            w = hits[0]
            return Outcome(True, f"window '{w['title']}' is open" + (f" (+{len(hits) - 1} more match)" if len(hits) > 1 else ""),
                           verified=True, evidence="found in window list", untrusted_text="\n".join(h["title"] for h in hits),
                           data={"found": True, "hwnd": w["hwnd"], "title": w["title"], "matches": len(hits)})
        if time.monotonic() >= end:
            return Outcome(True, f"no window matching '{a['window']}' within {a['timeout']}s", verified=True,
                           evidence="window list checked until timeout", data={"found": False})
        env.stop.wait(0.25)


# ----------------------------------------------------------------------------- UI Automation
def h_ui_find(a, env):
    w = resolve_window(env, a["window"], a["hwnd"])
    els = env.backend.ui_find(w["hwnd"], _sel(a))[: a["max_results"]]
    lines = "\n".join(f"[{e['index']}] {e['control_type']} '{e['name']}'" + (f" id={e['automation_id']}" if e["automation_id"] else "")
                      + ("" if e["enabled"] else " (disabled)") for e in els)
    return Outcome(True, f"{len(els)} element(s) in '{w['title']}'", verified=True, evidence="UI Automation tree read now",
                   data={"hwnd": w["hwnd"], "elements": els}, untrusted_text=lines)


def h_ui_read(a, env):
    w = resolve_window(env, a["window"], a["hwnd"])
    r = env.backend.ui_read(w["hwnd"], _sel(a))
    return Outcome(True, f"read {r['control_type']} '{r['name']}'", verified=True, evidence="UI Automation property read",
                   data=r, untrusted_text=json.dumps({k: r.get(k) for k in ("name", "value", "toggle_state", "selected") if k in r}))


def ui_act_risk(a, env):
    name = (a.get("name") or "").lower()
    if a.get("action") == "invoke" and any(w in name for w in ("delete", "remove", "uninstall", "format", "erase", "send",
                                                                 "pay", "purchase", "buy", "submit", "sign out", "close account")):
        return S.DESTRUCTIVE          # "always confirm" - the button name suggests an irreversible effect
    return S.INPUT


def h_ui_act(a, env):
    w = resolve_window(env, a["window"], a["hwnd"])
    if a["action"] == "set_value" and a["value"] is None:
        raise S.ArgError("set_value needs 'value'")
    r = env.backend.ui_act(w["hwnd"], _sel(a), a["action"], a["value"])
    after = r.get("after", {})
    verified, ev = None, f"used the {r['pattern']} pattern"
    if a["action"] == "set_value":
        verified = after.get("value") == a["value"]
        ev += f"; value read back {'matches' if verified else 'does NOT match'}"
    elif a["action"] == "toggle" and "toggle_state" in after:
        verified, ev = True, ev + f"; toggle state is now {after['toggle_state']}"
    elif a["action"] == "invoke":
        ev += "; Windows accepted the action. Check the result with ui_read, check_visible or wait_for_window"
    ok = verified is not False
    return Outcome(ok, f"{a['action']} on {r['element']['control_type']} '{r['element']['name']}'" + ("" if ok else " did not take effect"),
                   verified=verified, evidence=ev, data={"element": r["element"], "after": after},
                   untrusted_text=json.dumps(after)[:2000])


# ----------------------------------------------------------------------------- keyboard / mouse
VK = {**{chr(c): c for c in range(0x41, 0x5B)}, **{str(d): 0x30 + d for d in range(10)},
      **{f"f{i}": 0x6F + i for i in range(1, 13)}, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
      "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
      "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}
MODS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10}
BLOCKED_COMBOS = {frozenset({"ctrl", "alt", "delete"}), frozenset({"ctrl", "shift", "esc"})}
DESTRUCTIVE_COMBOS = {frozenset({"alt", "f4"}), frozenset({"ctrl", "w"}), frozenset({"delete"}), frozenset({"shift", "delete"}),
                      frozenset({"ctrl", "d"})}


def parse_combo(combo: str) -> tuple[list[int], frozenset]:
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    if not parts or len(parts) > 4:
        raise S.ArgError("keys look like 'ctrl+s', 'enter' or 'alt+tab'")
    if any(p in ("win", "windows", "meta", "super", "cmd") for p in parts):
        raise S.ArgError("the Windows key is not allowed (it opens Run, Settings and other system tools)")
    names = frozenset("ctrl" if p == "control" else "esc" if p == "escape" else "delete" if p == "del" else p for p in parts)
    if names in BLOCKED_COMBOS:
        raise S.ArgError("that key combination is not allowed")
    *mods, key = parts
    if any(m not in MODS for m in mods):
        raise S.ArgError(f"modifiers must be ctrl, alt or shift (got {mods})")
    k = key.upper() if len(key) == 1 and key.isalpha() else key
    if k not in VK and key not in VK:
        raise S.ArgError(f"unknown key '{key}'")
    return [MODS[m] for m in mods] + [VK.get(k, VK.get(key))], names


def keys_risk(a, env):
    try:
        return S.DESTRUCTIVE if parse_combo(a.get("keys", ""))[1] in DESTRUCTIVE_COMBOS else S.INPUT
    except S.ArgError:
        return S.INPUT


def h_type_text(a, env):
    w = resolve_window(env, a["window"], a["hwnd"])
    _focus_checked(env, w)
    before = env.backend.focused_value()
    env.backend.type_text(a["text"])
    time.sleep(0.1)
    after = env.backend.focused_value()
    if after is not None and a["text"] in after and (before is None or after != before):
        v, ev = True, "the focused text box now contains the typed text (read back via UI Automation)"
    elif after is not None:
        v, ev = False, "the focused text box does not contain the typed text"
    else:
        v, ev = None, "keystrokes were sent; the result could not be read back (check with ui_read or check_visible)"
    return Outcome(v is not False, f"typed {len(a['text'])} character(s) into '{w['title']}'" if v is not False else "typing did not show up",
                   verified=v, evidence=ev, data={"hwnd": w["hwnd"]}, untrusted_text=w["title"])


def h_press_keys(a, env):
    vks, _ = parse_combo(a["keys"])
    w = resolve_window(env, a["window"], a["hwnd"])
    _focus_checked(env, w)
    env.backend.press(vks)
    return Outcome(True, f"pressed {a['keys']} in '{w['title']}'", verified=None,
                   evidence="keys were sent; check the effect with an observation tool", data={"hwnd": w["hwnd"]},
                   untrusted_text=w["title"])


def h_mouse_click(a, env):
    w = resolve_window(env, a["window"], a["hwnd"])
    l, t, r, b = w["rect"]
    x, y = l + a["x"], t + a["y"]
    if not (l <= x < r and t <= y < b):
        raise S.ArgError(f"({a['x']},{a['y']}) is outside the window ({r - l}x{b - t}); coordinates are relative to its top-left corner")
    _focus_checked(env, w)
    env.backend.click(x, y, a["button"], a["double"])
    return Outcome(True, f"clicked at ({a['x']},{a['y']}) in '{w['title']}'", verified=None,
                   evidence="click was sent; coordinates are a fallback - prefer ui_act. Check the effect with an observation tool",
                   data={"hwnd": w["hwnd"], "screen_x": x, "screen_y": y}, untrusted_text=w["title"])


# ----------------------------------------------------------------------------- screen
def _save_png(env, png: bytes) -> Path:
    d = env.screenshots_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / (time.strftime("%Y%m%d-%H%M%S-") + f"{int(time.time() * 1000) % 1000:03d}.png")
    p.write_bytes(png)
    for old in sorted(d.glob("*.png"))[:-SCREENSHOT_KEEP]:
        old.unlink(missing_ok=True)
    return p


def _guard_capture(env, hwnd):
    from jarvis.desktop import capture_guard
    why = capture_guard.guard(env.backend, hwnd)
    if why:
        raise PathRejected("not captured: " + why)


def h_screenshot(a, env):
    hwnd = None
    title = "whole screen"
    if a["window"] or a["hwnd"]:
        w = resolve_window(env, a["window"], a["hwnd"])
        hwnd, title = w["hwnd"], w["title"]
    _guard_capture(env, hwnd)
    width, height, png = env.backend.screenshot(hwnd)
    p = _save_png(env, png)
    ok = p.exists() and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    return Outcome(ok, f"screenshot of {title} saved ({width}x{height})", verified=ok, evidence="PNG file written and checked",
                   data={"path": str(p), "width": width, "height": height}, untrusted_text=title if hwnd else "")


def _ocr(env, a) -> tuple[dict, str]:
    hwnd, title = None, "whole screen"
    if a.get("window") or a.get("hwnd"):
        w = resolve_window(env, a.get("window"), a.get("hwnd"))
        hwnd, title = w["hwnd"], w["title"]
    _guard_capture(env, hwnd)
    _, _, png = env.backend.screenshot(hwnd)
    p = _save_png(env, png)
    return env.backend.ocr(p), title


def h_read_screen_text(a, env):
    res, title = _ocr(env, a)
    from jarvis.desktop import capture_guard
    lines, withheld = capture_guard.withhold_secrets(res.get("lines", []))
    text = "\n".join(l["text"] for l in lines)
    return Outcome(True, f"read {len(lines)} line(s) of text from {title} with {res.get('engine')}" + (f"; {withheld} line(s) withheld because they look like secrets" if withheld else ""),
                   verified=True, evidence="OCR of a fresh screenshot (OCR can misread)", data={"lines": lines, "engine": res.get("engine"), "withheld": withheld},
                   untrusted_text=text)


def h_check_visible(a, env):
    """Is this text visible in the window? UI Automation first (exact, semantic); OCR only if screen permission is on."""
    w = resolve_window(env, a["window"], a["hwnd"])
    q = a["text"].lower()
    try:
        els = env.backend.ui_find(w["hwnd"], {})
        for e in els:
            if not e["offscreen"] and (q in e["name"].lower()):
                return Outcome(True, f"'{a['text']}' is visible in '{w['title']}'", verified=True, evidence=f"UI Automation: {e['control_type']} '{e['name']}'",
                               data={"found": True, "method": "uia"}, untrusted_text=f"{w['title']}\n{e['name']}")
        for e in els:
            if e["control_type"] in ("edit", "text", "document") and not e["is_password"]:
                try:
                    v = env.backend.ui_read(w["hwnd"], {"automation_id": e["automation_id"], "name": e["name"], "exact": True,
                                                        "control_type": e["control_type"]})
                except LookupError:
                    continue
                if q in str(v.get("value") or "").lower():
                    return Outcome(True, f"'{a['text']}' is shown in '{w['title']}'", verified=True,
                                   evidence=f"UI Automation value of {e['control_type']} '{e['name']}'", data={"found": True, "method": "uia"}, untrusted_text=f"{w['title']}\n{e['name']}")
        uia_note = "not found through UI Automation"
    except Exception as e:  # noqa: BLE001
        uia_note = f"UI Automation unavailable ({e})"
    if env.settings.enabled.get("screen"):
        res, _ = _ocr(env, a)
        text = "\n".join(l["text"] for l in res.get("lines", []))
        found = q in text.lower()
        return Outcome(True, f"'{a['text']}' {'is' if found else 'is NOT'} visible in '{w['title']}'", verified=True,
                       evidence=f"{uia_note}; OCR ({res.get('engine')}) {'found' if found else 'did not find'} it",
                       data={"found": found, "method": "ocr"}, untrusted_text=w["title"])
    return Outcome(True, f"'{a['text']}' was not found in '{w['title']}'", verified=True,
                   evidence=f"{uia_note}; OCR not tried (screen permission is off)", data={"found": False, "method": "uia"}, untrusted_text=w["title"])


# ----------------------------------------------------------------------------- registry of specs
W = (Arg("window", "str", "part of the window title (or process name)", required=False, max_len=200, no_newlines=True),
     Arg("hwnd", "int", "exact window handle from list_windows", required=False, min=1))
SEL = (Arg("name", "str", "element name (visible label)", required=False, max_len=200, no_newlines=True),
       Arg("control_type", "str", "e.g. button, edit, checkbox, menuitem", required=False, max_len=30,
           choices=None),
       Arg("automation_id", "str", required=False, max_len=200, no_newlines=True),
       Arg("index", "int", "which match, when several match", required=False, min=0, max=500),
       Arg("exact", "bool", "name must match exactly", required=False, default=False))
PATH = lambda n="path", d="file or folder path inside an allowed folder": Arg(n, "str", d, max_len=1000, no_newlines=True)

SPECS: list[ToolSpec] = [
    ToolSpec("system_info", "CPU, memory, disk, network state and GPU metrics, with the source of each value", "system", S.OBSERVE,
             (Arg("parts", "list[str]", "subset of cpu, memory, storage, network, gpu", required=False, default=[], max_items=5),),
             h_system_info, timeout=30),
    ToolSpec("wait", "pause briefly so an app can finish opening or redrawing", "system", S.OBSERVE,
             (Arg("seconds", "float", min=0.1, max=30),), h_wait, timeout=35),
    ToolSpec("file_search", "find files/folders by name inside an allowed folder", "files_read", S.OBSERVE,
             (PATH("folder", "folder to search"), Arg("pattern", "str", "wildcard such as *.txt", required=False, default="*", max_len=100),
              Arg("name_contains", "str", required=False, default="", max_len=100),
              Arg("max_results", "int", required=False, default=50, min=1, max=500),
              Arg("max_depth", "int", required=False, default=6, min=1, max=20)), F.file_search, timeout=60, untrusted_output=True),
    ToolSpec("file_info", "whether a path exists, its size, date and sha256", "files_read", S.OBSERVE, (PATH(),), F.file_info),
    ToolSpec("read_file", "read a text file (up to 1 MB)", "files_read", S.OBSERVE,
             (PATH(), Arg("max_chars", "int", required=False, default=20000, min=1, max=200000)), F.read_file, untrusted_output=True),
    ToolSpec("create_folder", "create a folder (and missing parents)", "files_write", S.MODIFY, (PATH(),), F.create_folder),
    ToolSpec("write_file", "create a text file; replacing an existing one needs overwrite=true, confirmation and makes a backup",
             "files_write", S.MODIFY, (PATH(), Arg("content", "str", max_len=5_000_000),
                                       Arg("overwrite", "bool", required=False, default=False)), F.write_file, risk_for=None),
    ToolSpec("edit_file", "replace exact text in a text file (backup first; fails if the text is not found exactly)", "files_write",
             S.MODIFY, (PATH(), Arg("old_text", "str", max_len=100_000), Arg("new_text", "str", max_len=100_000),
                        Arg("occurrences", "int", "how many times old_text must appear", required=False, default=1, min=1, max=1000)),
             F.edit_file),
    ToolSpec("copy_path", "copy a file or folder", "files_write", S.MODIFY,
             (PATH("source"), PATH("destination"), Arg("overwrite", "bool", required=False, default=False)), F.copy_path, timeout=300),
    ToolSpec("move_path", "move a file or folder (never over an existing item)", "files_write", S.MODIFY,
             (PATH("source"), PATH("destination")), F.move_path, timeout=300),
    ToolSpec("rename_path", "rename a file or folder in place", "files_write", S.MODIFY,
             (PATH(), Arg("new_name", "str", max_len=255, no_newlines=True)), F.rename_path),
    ToolSpec("delete_path", "move a file or folder to Jarvis' recycle folder (recoverable with undo)", "files_write", S.DESTRUCTIVE,
             (PATH(),), F.delete_path, timeout=300),
    ToolSpec("undo", "reverse an earlier file change by its checkpoint id", "files_write", S.MODIFY,
             (Arg("checkpoint", "str", max_len=64, no_newlines=True),), F.undo, timeout=300),
    ToolSpec("list_apps", "find installed programs (Start menu, Store apps, your allow-list)", "apps", S.OBSERVE,
             (Arg("query", "str", required=False, default="", max_len=100, no_newlines=True),
              Arg("refresh", "bool", required=False, default=False), Arg("max_results", "int", required=False, default=30, min=1, max=500)),
             h_list_apps, timeout=60, untrusted_output=True),
    ToolSpec("launch_app", "start an installed program and check that its window appeared", "apps", S.LAUNCH,
             (Arg("app", "str", "app id or name from list_apps", max_len=300, no_newlines=True),
              Arg("wait_seconds", "float", required=False, default=10.0, min=1, max=60)), h_launch_app, timeout=75),
    ToolSpec("list_windows", "list open windows", "windows", S.OBSERVE, (), h_list_windows, windows_only=False, untrusted_output=True),
    ToolSpec("focus_window", "bring a window to the front (and check it is in front)", "windows", S.MODIFY, W, h_focus_window,
             untrusted_output=True),
    ToolSpec("wait_for_window", "wait until a window with this title is open", "windows", S.OBSERVE,
             (Arg("window", "str", max_len=200, no_newlines=True), Arg("timeout", "float", required=False, default=10.0, min=0.5, max=60)),
             h_wait_for_window, timeout=65, untrusted_output=True),
    ToolSpec("ui_find", "list UI elements (buttons, boxes, menus) in a window through accessibility", "ui", S.OBSERVE,
             W + SEL + (Arg("max_results", "int", required=False, default=50, min=1, max=500),), h_ui_find, timeout=30,
             untrusted_output=True),
    ToolSpec("ui_read", "read one element's name, value or on/off state", "ui", S.OBSERVE, W + SEL, h_ui_read, untrusted_output=True),
    ToolSpec("ui_act", "operate one element: invoke (press), toggle, select, expand, collapse, set_value, focus", "ui", S.INPUT,
             W + SEL + (Arg("action", "str", choices=("invoke", "toggle", "select", "expand", "collapse", "set_value", "focus")),
                        Arg("value", "str", required=False, max_len=5000)), h_ui_act, untrusted_output=True),
    ToolSpec("type_text", "type text into a window (it is brought to the front first)", "input", S.INPUT,
             W + (Arg("text", "str", max_len=2000),), h_type_text, untrusted_output=True),
    ToolSpec("press_keys", "press a key or shortcut such as ctrl+s or enter (no Windows key)", "input", S.INPUT,
             W + (Arg("keys", "str", max_len=40, no_newlines=True),), h_press_keys, untrusted_output=True),
    ToolSpec("mouse_click", "click at a point RELATIVE to a window's top-left corner (fallback when ui_act cannot be used)", "input",
             S.INPUT, W + (Arg("x", "int", min=0, max=20000), Arg("y", "int", min=0, max=20000),
                           Arg("button", "str", required=False, default="left", choices=("left", "right", "middle")),
                           Arg("double", "bool", required=False, default=False)), h_mouse_click, untrusted_output=True),
    ToolSpec("screenshot", "save a screenshot of a window or the screen (kept on this PC, last 20 only)", "screen", S.OBSERVE,
             W, h_screenshot, timeout=30, untrusted_output=True),
    ToolSpec("read_screen_text", "OCR the text visible in a window or on screen", "screen", S.OBSERVE, W, h_read_screen_text,
             timeout=60, untrusted_output=True),
    ToolSpec("check_visible", "check whether some text is visible in a window (accessibility first, then OCR)", "ui", S.OBSERVE,
             W + (Arg("text", "str", max_len=200),), h_check_visible, timeout=60, untrusted_output=True),
]

_RISK_FOR = {"write_file": F.write_risk, "copy_path": F.copy_risk, "launch_app": launch_risk, "ui_act": ui_act_risk,
             "press_keys": keys_risk}
_TARGETED = {"focus_window", "ui_act", "type_text", "press_keys", "mouse_click"}


def registry() -> dict[str, ToolSpec]:
    return {s.name: s for s in SPECS}


def risk_of(spec: ToolSpec, args: dict, env: Env) -> str:
    if spec.risk_fn is not None:
        return spec.risk_fn(args, env)
    f = _RISK_FOR.get(spec.name)
    return f(args, env) if f else spec.risk


PATH_ARGS = {"file_search": [("folder", False)], "file_info": [("path", False)], "read_file": [("path", False)],
             "create_folder": [("path", True)], "write_file": [("path", True)], "edit_file": [("path", True)],
             "copy_path": [("source", False), ("destination", False)], "move_path": [("source", False), ("destination", False)],
             "rename_path": [("path", False)], "delete_path": [("path", False)]}


def precheck(spec: ToolSpec, args: dict, env: Env) -> None:
    """Refuse bad paths BEFORE asking the user to confirm anything (raises PathRejected). Kept for callers; the executor now
    uses bind_action, which does these checks and also pins the result."""
    bind_action(spec, args, env)


# ----------------------------------------------------------------------------- binding a confirmation to the exact action
@dataclass(frozen=True)
class BoundAction:
    """The validated action in the exact form that will run: paths are the real resolved ones, a folder given as a copy/move
    destination is replaced by the file it will really create, and the risk is computed from THOSE values. The text shown to
    the user and the arguments that execute both come from this object; `fingerprint` is a digest of all of it, including the
    state of the files involved, so anything that changes before execution produces a different fingerprint."""
    tool: str
    risk: str
    args: dict
    effects: tuple
    fingerprint: str
    args_digest: str = ""      # digest of tool + pinned arguments only (what an isolated worker re-checks)


def args_digest(tool: str, args: dict) -> str:
    return hashlib.sha256(json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")).hexdigest()


def _state(p: Path):
    try:
        st = _paths._lstat(p)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError) as e:
        raise PathRejected(f"could not read the state of {p} ({type(e).__name__}); refused") from e
    return ["dir" if _stat.S_ISDIR(st.st_mode) else "file", int(st.st_size), int(st.st_mtime_ns)]


_PLAIN_NAME_BAD = '\\/:*?"<>|'


def bind_action(spec: ToolSpec, a: dict, env: Env) -> BoundAction:
    """Resolve everything that identifies the target, compute the true risk, and fingerprint it. Raises PathRejected."""
    args = dict(a)
    P = env.paths
    effects: list[str] = []
    state: dict = {}
    extra: list = []
    n = spec.name
    consequential = spec.risk != S.OBSERVE or n in _RISK_FOR or spec.risk_fn is not None

    if spec.bind is not None:
        # a tool added by an extension (code, research, plugins) pins its own targets
        pinned, eff, ex = spec.bind(args, env)
        args = dict(pinned)
        effects += [str(x) for x in eff]
        extra += list(ex)
    elif n in ("file_search", "file_info"):
        key = "folder" if n == "file_search" else "path"
        args[key] = str(P.resolve(args[key], must_exist=(n == "file_search"), allow_ancestor=True))
    elif n in ("read_file", "delete_path", "edit_file", "create_folder", "write_file"):
        must = n in ("read_file", "delete_path", "edit_file")
        w = n not in ("read_file", "delete_path")
        r = P.resolve(args["path"], must_exist=must, for_write=w)
        args["path"] = str(r)
        state["path"] = _state(r)
        if n == "write_file":
            effects.append(("replaces the existing file (a backup is made first): " if r.exists() else "creates the file: ") + str(r))
        elif n == "edit_file":
            effects.append(f"edits {r} (a backup is made first)")
        elif n == "delete_path":
            effects.append(f"moves {r} to Jarvis' recycle folder")
        elif n == "create_folder":
            effects.append(("folder already exists: " if r.exists() else "creates the folder: ") + str(r))
    elif n == "rename_path":
        new = args["new_name"]
        if any(c in new for c in _PLAIN_NAME_BAD) or new.strip() in ("", ".", ".."):
            raise PathRejected("the new name must be a plain file or folder name")
        src = P.resolve(args["path"], must_exist=True)
        dst = P.resolve(str(src.with_name(new)), for_write=src.is_file())
        args["path"] = str(src)
        state["path"], state["target"] = _state(src), _state(dst)
        effects.append(f"renames {src} to {dst}")
    elif n in ("copy_path", "move_path"):
        src0 = P.resolve(args["source"], must_exist=True)
        is_file = src0.is_file()
        src = P.resolve(args["source"], must_exist=True, for_write=True) if (n == "move_path" and is_file) else src0
        dst = P.resolve(args["destination"], for_write=is_file)
        hop = dst.is_dir() and (is_file or n == "move_path")
        if hop:
            dst = P.resolve(str(dst / src.name), for_write=is_file)
            if dst.is_dir():
                raise PathRejected(f"{dst} is a folder; choose a file name or another destination")
        args["source"], args["destination"] = str(src), str(dst)
        state["source"], state["destination"] = _state(src), _state(dst)
        verb = "copies" if n == "copy_path" else "moves"
        effects.append(f"{verb} {src} to {dst}" + (" (replacing what is there; a backup is made first)" if dst.exists() and n == "copy_path" else ""))
    elif n == "launch_app":
        # Pin the exact program: the model's words ("calculator") become the one discovered entry's id, and the question shows
        # what will really be started (shortcut file, program path or Store app id), not just the name that was typed.
        try:
            app = _resolve_app(env, args)
        except LookupError as e:
            raise PathMissing(str(e)) from e
        args["app"] = app["id"]
        what = app.get("target") or app.get("path") or app.get("aumid") or "(no path recorded)"
        via = f", via shortcut {app['path']}" if app.get("kind") == "lnk" and app.get("path") else ""
        effects.append(f"starts '{app.get('name')}' [{app.get('kind')}; found by: {app.get('source', '?')}] -> {what}{via}")
        extra += [app["id"], app.get("kind"), app.get("path"), app.get("target"), app.get("aumid")]
    elif n in _TARGETED:
        # Pin the exact window. The model's words ("notepad") become the one window handle that is resolved NOW; the question
        # shows that window's real title and program, and the handler later uses the handle, not the title. If the handle
        # closes, is reused by another program, or the title now matches a different window, the fingerprint no longer
        # matches and nothing runs without a fresh approval. (The title is shown but not fingerprinted: it changes while
        # people type; the handle, program name and program path are what identify the target.)
        try:
            win = resolve_window(env, args.get("window"), args.get("hwnd"))
        except LookupError as e:
            raise PathMissing(str(e)) from e
        args["hwnd"] = win["hwnd"]
        prog = win.get("process")
        effects.append(f"acts on window [{win['hwnd']}] '{win.get('title', '')}' of program {prog or '(unknown program)'}"
                       + (f" ({win['process_path']})" if win.get("process_path") else ""))
        extra += [win["hwnd"], prog, win.get("process_path")]
    elif n == "undo":
        from jarvis.desktop.checkpoints import CheckpointError
        try:
            rec = env.checkpoints.get(args["checkpoint"])
        except (CheckpointError, ValueError, OSError) as e:
            raise PathRejected(f"checkpoint cannot be used: {e}") from e
        orig = P.resolve(rec.get("original", ""))
        extra += [rec.get("kind"), str(orig), bool(rec.get("undone"))]
        state["original"] = _state(orig)
        if rec.get("kind") == "moved":
            moved = P.resolve(rec.get("moved_to", ""))
            extra.append(str(moved))
            state["moved_to"] = _state(moved)
            effects.append(f"moves {moved} back to {orig}")
        else:
            effects.append(f"undoes a '{rec.get('kind')}' change at {orig}")
    risk = risk_of(spec, args, env)
    if not consequential:
        state = {}
    payload = json.dumps({"tool": n, "risk": risk, "args": args, "state": state, "extra": extra}, sort_keys=True,
                         ensure_ascii=True, default=str)
    return BoundAction(n, risk, args, tuple(effects), hashlib.sha256(payload.encode("utf-8")).hexdigest(), args_digest(n, args))


def needs_target(spec: ToolSpec) -> bool:
    return spec.name in _TARGETED
