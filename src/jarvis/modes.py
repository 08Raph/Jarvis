"""Operating modes: General, Coding, Research, Gaming, Productivity.

A mode is a FIXED, inspectable table (below, and `jarvis mode show NAME`): which tools the assistant may even try, what it must not do, and how it
should behave. A mode only ever REMOVES tools from view. It never turns a permission on, never records a consent, never approves anything,
never activates a plugin or skill, and never loosens the global policy: every tool that is allowed in a mode still goes through the same
permission switches, consent check, confirmation, audit and emergency stop as before. Choosing a mode is something YOU do on the command line
(`jarvis mode set NAME`); there is no tool by which the model can switch modes, and the model cannot write the mode file (it lives in Jarvis' own
protected data folder).

No mode selected (no mode.json) = the behaviour of earlier versions: every registered tool, subject to the same policy."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MODE_FILE = "mode.json"

_DESKTOP_READ = ("mission_status", "system_info", "list_windows", "list_apps", "check_visible", "wait", "wait_for_window", "file_info", "file_search", "read_file")
_DESKTOP_WRITE = ("write_file", "edit_file", "create_folder", "copy_path", "move_path", "rename_path", "delete_path", "undo")
_DESKTOP_CONTROL = ("launch_app", "focus_window", "ui_find", "ui_read", "ui_act", "type_text", "press_keys", "mouse_click")
_SCREEN = ("screenshot", "read_screen_text", "vision_status", "observe_screen", "find_on_screen", "recheck_screen")
_MONITOR = ("monitor_snapshot", "monitor_processes", "monitor_self", "monitor_watch")
_CODE = ("repo_overview", "read_lines", "search_code", "analyze_error", "git_info", "diff_snapshot", "verify_fix", "snapshot_project", "edit_code", "write_code",
         "undo_code_edit", "run_tests", "run_code")
_RESEARCH = ("read_document", "list_sources", "verify_quote", "cite_sources", "compare_sources", "web_fetch", "web_search")
_GAME = ("game_status", "game_metrics", "game_fps_import", "game_compare", "game_profile_list", "game_profile_get", "game_profile_save")


@dataclass(frozen=True)
class Mode:
    name: str
    summary: str
    tools: frozenset
    plugins: bool            # may reviewed-and-activated plugin/skill tools appear? (they still need their own activation, review and per-run approval)
    restrictions: str
    behavior: str


def _m(name, summary, tools, plugins, restrictions, behavior) -> Mode:
    return Mode(name, summary, frozenset(tools) | {"mode_info"}, plugins, restrictions, behavior)


MODES: dict[str, Mode] = {m.name: m for m in (
    _m("general", "ordinary assistant tasks on this PC",
       _DESKTOP_READ + _DESKTOP_WRITE + _DESKTOP_CONTROL + _SCREEN + _MONITOR + _RESEARCH, True,
       "No code-editing/running tools and no game tools. Everything else follows the permission switches exactly as before.",
       "Answer directly where possible; use tools only for what the user asked; state plainly what a tool result did and did not confirm."),
    _m("coding", "inspect a project, make small reviewed edits, run its tests",
       _DESKTOP_READ + _CODE + ("monitor_self", "read_document") + ("undo",), True,
       "No mouse, keyboard, window-control or screen tools; no web access; no game tools. Code tools work only in project folders you added.",
       "Read before editing, change as little as possible, show what changed, run the tests, and report test results exactly - never claim a fix works unless the tests ran and passed."),
    _m("research", "source-backed research and document analysis",
       _DESKTOP_READ + _RESEARCH, False,
       "Read-only: no file changes, no code, no desktop control, no screen capture, no game tools, no plugins. Web requests still need the 'web_research' feature and your approval each time.",
       "Cite a source for each claim, quote only what verify_quote confirms, say when sources disagree or are missing, and separate what the sources say from your own inference."),
    _m("gaming", "approved gaming metrics and profiles (no automation)",
       ("system_info", "list_windows") + _MONITOR + _GAME + ("vision_status",), False,
       "No keyboard, mouse, UI automation, window control, screen capture, file changes, code, web or plugins. Never touches game processes or anti-cheat. FPS comes only from logs you import.",
       "Report only real readings and say which are unavailable and why; never claim an FPS improvement without reproducible runs; suggest settings as text for the user to apply, never apply them."),
    _m("productivity", "documents, notes, folders and approved workflows",
       _DESKTOP_READ + _DESKTOP_WRITE + _RESEARCH[:5] + ("launch_app",), True,
       "No keyboard, mouse or UI automation, no screen capture, no code running, no game tools, no web access. File changes stay inside your allowed folders and are asked each time.",
       "Keep work organised and reversible: say what will change before it changes, prefer copies over overwrites, and report what actually happened."),
)}
NAMES = tuple(MODES)


def _file(profile_dir) -> Path:
    return Path(profile_dir) / MODE_FILE


def current(profile_dir) -> Mode | None:
    """The mode the user chose, or None (no restriction). A damaged or hand-edited file selects nothing rather than something broader or narrower by accident."""
    try:
        raw = json.loads(_file(profile_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    name = raw.get("mode") if isinstance(raw, dict) else None
    return MODES.get(name) if isinstance(name, str) else None


def blocked(profile_dir, spec) -> str | None:
    """Reason a tool is not available in the current mode, or None."""
    m = current(profile_dir)
    if m is None or spec.name in m.tools:
        return None
    if spec.category == "plugins" and m.plugins:
        return None
    return (f"'{spec.name}' is not available in {m.name} mode ({m.restrictions}) Switching mode is done by the user on the command line "
            f"(jarvis mode set NAME); a mode never grants or removes permissions by itself.")


def set_mode(profile_dir, name: str) -> Mode:
    """Called by the command line only (a tool cannot reach this)."""
    if name not in MODES:
        raise ValueError(f"unknown mode '{name}'; choose from {', '.join(NAMES)}")
    d = Path(profile_dir)
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".mode-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mode": name, "set": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
    os.replace(tmp, _file(profile_dir))
    return MODES[name]


def clear_mode(profile_dir) -> None:
    try:
        _file(profile_dir).unlink()
    except FileNotFoundError:
        pass


def describe(m: Mode) -> dict:
    return {"name": m.name, "summary": m.summary, "tools": sorted(m.tools), "plugin_tools_allowed": m.plugins, "restrictions": m.restrictions, "behavior": m.behavior,
            "grants_permissions": False}


def tool_states(env, specs: dict, m: Mode | None) -> list[dict]:
    """Per tool: why it is or is not usable right now (mode, permission switch, consent)."""
    from jarvis.desktop.policy import CONSENT_REQUIRED
    out = []
    for name, spec in sorted(specs.items()):
        if m is not None and name not in m.tools and not (spec.category == "plugins" and m.plugins):
            st = f"not in {m.name} mode"
        elif not env.settings.enabled.get(spec.category, False):
            st = f"permission '{spec.category}' is off (jarvis desktop enable {spec.category})"
        elif spec.category in CONSENT_REQUIRED and not env.settings.consents.get(spec.category):
            st = f"permission '{spec.category}' needs your recorded consent"
        else:
            st = "available (still asks for confirmation where its risk requires it)"
        out.append({"tool": name, "category": spec.category, "state": st})
    return out


def system_prompt_addendum(profile_dir) -> str:
    m = current(profile_dir)
    if m is None:
        return ""
    return (f"\n\nCurrent mode: {m.name} - {m.summary}.\nBehaviour: {m.behavior}\nRestrictions: {m.restrictions}\n"
            "A mode never grants permissions. If a tool is missing or denied, say so honestly and do not look for a way around it.")


# ------------------------------------------------------------------ read-only tool
def _h_info(a, env):
    from jarvis.desktop.schema import Outcome
    m = current(env.profile_dir)
    which = a["mode"] or (m.name if m else "")
    shown = MODES.get(which) if which else None
    if a["mode"] and shown is None:
        return Outcome(False, f"unknown mode '{a['mode']}'; modes: {', '.join(NAMES)}", verified=True, evidence="looked up the fixed mode table")
    specs = env.ext.get("specs") or {}
    data = {"current": m.name if m else None, "modes": list(NAMES), "inspected": describe(shown) if shown else None,
            "tools": tool_states(env, specs, shown if a["mode"] else m)}
    lines = [f"current mode: {m.name if m else 'none selected (no mode restriction; the same permissions apply)'}"]
    if shown:
        lines += [f"{shown.name}: {shown.summary}", "restrictions: " + shown.restrictions, "behaviour: " + shown.behavior, "A mode never grants permissions."]
    lines += [f"{t['tool']}: {t['state']}" for t in data["tools"]]
    return Outcome(True, f"mode: {m.name if m else 'none selected'}", verified=True, evidence="read the fixed mode table and your permission switches", data=data,
                   untrusted_text="\n".join(lines))


def specs():
    from jarvis.desktop import schema as S
    return [S.ToolSpec("mode_info", "show the current operating mode, what it allows and restricts, and why each tool is or is not usable (read-only; cannot change mode)", "system", S.OBSERVE,
                       (S.Arg("mode", "str", "inspect another mode instead", required=False, default="", max_len=20, no_newlines=True),), _h_info, timeout=10)]
