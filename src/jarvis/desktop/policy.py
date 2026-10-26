"""Who may do what. Permissions are per profile, OFF by default except reading basic system information, and stored in
desktop_permissions.json next to that profile's memory. Confirmation rules are fixed in code; settings can make Jarvis
MORE careful but never skip confirmation for destructive, installer or administrator actions."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.desktop import schema as S
from jarvis.desktop.paths import PathRejected, check_root

CATEGORIES = {
    "system": "read CPU, memory, disk, network and GPU information",
    "files_read": "search and read files inside your allowed folders",
    "files_write": "create, edit, copy, move, rename and (recoverably) delete files inside your allowed folders",
    "apps": "find and start programs installed on this PC",
    "windows": "list and switch between open windows",
    "ui": "read and operate buttons, menus and text boxes through Windows accessibility (UI Automation)",
    "input": "send keystrokes and mouse clicks to a window you approve",
    "screen": "take screenshots and read text from them (OCR); screenshots stay on this PC",
    "code_read": "inspect, read and search code inside your PROJECT folders, and analyse error output (offline)",
    "code_edit": "edit and create source files inside your PROJECT folders (backup first, you approve each change)",
    "code_run": "run tests and code snippets in the sandbox (needs a working sandbox; you approve each run)",
    "research_online": "fetch web pages / search the web for sources (also needs the 'web_research' internet feature; you approve each request)",
    "monitoring": "read-only readings of CPU, memory, disk, GPU, processes and Jarvis' own usage, on request (nothing runs in the background; needs your recorded consent)",
    "gaming": "Gaming mode: detect known games, import frame-time logs from tools you ran, keep game profiles; no input, no automation (needs your recorded consent)",
    "plugins": "run plugins and skills you reviewed and activated (each run is approved; plugins run only in the sandbox)",
}
DEFAULT_ON = {"system"}
ALWAYS_ASK_TOOLS = {"observe_screen", "find_on_screen", "recheck_screen"}    # Prompt 8 vision tools: every capture is asked, even though it only observes
CONSENT_REQUIRED = ("monitoring", "gaming")      # also need a recorded consent (jarvis desktop enable <name> asks for it)
FILE_NAME = "desktop_permissions.json"

# Programs Jarvis will never type into, click in or operate, whatever the settings say.
PROTECTED_PROCESSES = {
    "consent.exe",               # UAC prompt
    "credentialuibroker.exe", "lsass.exe", "logonui.exe", "winlogon.exe",
    "securityhealthsystray.exe", "securityhealthhost.exe", "msmpeng.exe", "mpcmdrun.exe",
    "windowsdefender.exe", "smartscreen.exe", "taskmgr.exe", "regedit.exe", "mmc.exe", "gpedit.exe",
    "keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe",
}
# Typing or clicking into these would let Jarvis run arbitrary commands, which is exactly what it has no tool for.
TERMINAL_PROCESSES = {"cmd.exe", "powershell.exe", "pwsh.exe", "powershell_ise.exe", "windowsterminal.exe", "wt.exe",
                      "conhost.exe", "openconsole.exe", "bash.exe", "wsl.exe", "wslhost.exe", "mintty.exe", "python.exe",
                      "pythonw.exe", "py.exe", "jarvis.exe", "wscript.exe", "cscript.exe", "mshta.exe"}
RUN_DIALOG_TITLES = {"run"}
# If one of these anti-cheat programs is running, Jarvis refuses to send ANY input or UI actions. Automating input
# while anti-cheat is active can get an account banned and is exactly what those systems exist to stop.
ANTI_CHEAT_PROCESSES = {
    "easyanticheat.exe", "easyanticheat_eos.exe", "beservice.exe", "beservice_x64.exe", "vgc.exe", "vgtray.exe",
    "faceit.exe", "faceitservice.exe", "esportal.exe", "gameguard.exe", "ucldr_battlegrounds_gl.exe", "ricochet.exe",
    "xigncode.exe", "nprotect.exe", "eac_launcher.exe", "mhyprot2.sys",
}


class PermissionDenied(Exception):
    pass


@dataclass
class Settings:
    enabled: dict = field(default_factory=lambda: {c: c in DEFAULT_ON for c in CATEGORIES})
    allowed_folders: list = field(default_factory=list)
    project_folders: list = field(default_factory=list)   # folders whose source files the code tools may read/edit (separate on purpose)
    trusted_apps: list = field(default_factory=list)       # app ids that may start without asking each time
    input_confirm: str = "every_action"                     # or "once_per_task"
    consents: dict = field(default_factory=dict)            # category -> ISO time the user agreed (monitoring, gaming); written by the CLI only
    version: int = 1

    @classmethod
    def load(cls, path: Path, protected=()) -> "Settings":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls.from_dict(raw, protected=protected)

    def to_dict(self) -> dict:
        return {"version": self.version, "enabled": dict(self.enabled), "allowed_folders": list(self.allowed_folders),
                "project_folders": list(self.project_folders),
                "trusted_apps": list(self.trusted_apps), "input_confirm": self.input_confirm, "consents": dict(self.consents)}

    @classmethod
    def from_dict(cls, raw, protected=()) -> "Settings":
        """Untrusted input (a hand-edited file, or a message to the worker): every field is re-checked. `protected` lists
        Jarvis' own data folders, which can never be an allowed folder (nor contain one)."""
        s = cls()
        if not isinstance(raw, dict):
            return s
        en = raw.get("enabled", {})
        if isinstance(en, dict):
            for c in CATEGORIES:
                if isinstance(en.get(c), bool):
                    s.enabled[c] = en[c]
        folders = []
        for f in raw.get("allowed_folders", []) if isinstance(raw.get("allowed_folders"), list) else []:
            try:
                folders.append(str(check_root(f, protected)))   # a hand-edited file cannot sneak in C:\Windows or Jarvis' own data
            except (PathRejected, OSError, TypeError):
                pass
        s.allowed_folders = folders
        projects = []
        for f in raw.get("project_folders", []) if isinstance(raw.get("project_folders"), list) else []:
            try:
                projects.append(str(check_root(f, protected)))
            except (PathRejected, OSError, TypeError):
                pass
        s.project_folders = projects
        s.trusted_apps = [str(a)[:200] for a in raw.get("trusted_apps", []) if isinstance(a, str)][:100]
        cons = raw.get("consents")
        if isinstance(cons, dict):
            s.consents = {c: str(v)[:40] for c, v in cons.items() if c in CONSENT_REQUIRED and isinstance(v, str) and v}
        if raw.get("input_confirm") in ("every_action", "once_per_task"):
            s.input_confirm = raw["input_confirm"]
        return s

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)

    def allow_project(self, folder: str, protected=()) -> str:
        r = str(check_root(folder, protected))
        if r not in self.project_folders:
            self.project_folders.append(r)
        return r

    def allow_folder(self, folder: str, protected=()) -> str:
        r = str(check_root(folder, protected))
        if r not in self.allowed_folders:
            self.allowed_folders.append(r)
        return r


@dataclass
class Context:
    """Facts about the current task that change what needs confirmation."""
    guest: bool = False
    agent: bool = False                   # a language model is choosing the steps (not a fixed task file / command)
    tainted: bool = False                 # untrusted text (file/screen/window/web) has been seen by the model in this task
    input_approved_for: set = field(default_factory=set)   # windows approved for input in this task (once_per_task)
    target_process: str | None = None     # process name of the window an input/UI action targets
    target_title: str | None = None
    running_processes: set | None = None  # lower-case process names, when known
    app_id: str | None = None


@dataclass
class Decision:
    allowed: bool
    confirm: bool
    reason: str = ""


def decide(spec: S.ToolSpec, risk: str, settings: Settings, ctx: Context) -> Decision:
    if risk == S.ADMIN:
        return Decision(False, False, "Jarvis never performs administrator actions, never answers UAC prompts and never "
                                      "changes security settings. Do this yourself if you want it.")
    if risk == S.EXTERNAL:
        return Decision(False, False, "Jarvis has no tool that sends anything off this PC.")
    if ctx.guest and spec.category != "system":
        return Decision(False, False, "Desktop control is off in guest mode.")
    if not settings.enabled.get(spec.category, False):
        return Decision(False, False, f"Permission '{spec.category}' ({CATEGORIES.get(spec.category, '?')}) is off. "
                                      f"Turn it on with: jarvis desktop enable {spec.category}")
    if spec.category in CONSENT_REQUIRED and not settings.consents.get(spec.category):
        return Decision(False, False, f"Permission '{spec.category}' is switched on but your consent was not recorded. Run: jarvis desktop enable {spec.category} "
                                      "(it shows exactly what is collected and asks you to agree).")
    if spec.category in ("input", "ui") and risk in (S.INPUT, S.DESTRUCTIVE):
        procs = ctx.running_processes or set()
        # Fail closed: if the list of running programs or the owner of the target window cannot be determined, the checks below
        # cannot be made, so nothing is sent.
        if not procs:
            return Decision(False, False, "Jarvis could not list the running programs, so it cannot check for anti-cheat or "
                                          "security software and will not send input or operate apps.")
        if not ctx.target_process:
            return Decision(False, False, "Jarvis could not tell which program owns the target window, so it will not send input "
                                          "or operate it.")
        ac = sorted(procs & ANTI_CHEAT_PROCESSES)
        if ac:
            return Decision(False, False, f"Anti-cheat software is running ({', '.join(ac)}). Jarvis does not send input "
                                          "or operate apps while it is active.")
        if ctx.target_process and ctx.target_process.lower() in PROTECTED_PROCESSES:
            return Decision(False, False, f"{ctx.target_process} is a security, sign-in or password program; Jarvis never operates it.")
        if ctx.target_process and ctx.target_process.lower() in TERMINAL_PROCESSES or \
                (ctx.target_title or "").strip().lower() in RUN_DIALOG_TITLES:
            return Decision(False, False, "Jarvis does not type or click into command prompts, terminals, script hosts or the Run "
                                          "dialog: that would let it run arbitrary commands.")
    confirm = risk in S.ALWAYS_CONFIRM
    if risk == S.LAUNCH:
        confirm = confirm or not (ctx.app_id and ctx.app_id in settings.trusted_apps)
    if risk == S.INPUT:
        key = (ctx.target_process or "?").lower()
        confirm = confirm or settings.input_confirm == "every_action" or key not in ctx.input_approved_for
    if spec.category in ("code_edit", "code_run", "plugins", "research_online", "gaming") and risk != S.OBSERVE:
        confirm = True                    # Prompt 7: changing a project, running code, plugins and every web request are always asked
    if spec.name in ALWAYS_ASK_TOOLS:
        confirm = True                    # screen capture is never silent
    if ctx.tainted and risk != S.OBSERVE:
        confirm = True                    # text from outside was read in this task: ask before anything that changes state
    return Decision(True, confirm, "")
