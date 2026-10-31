"""Quick launch: "open Fortnite", "open Instagram", "open Notepad", "scan my PC" typed in the chat or spoken after the wake phrase.

WHAT IT IS: a small, strict command reader plus a launcher. It understands only a few sentence shapes (open / launch / start ..., search ... , scan my PC, what can you open).
Everything else goes to the normal language model unchanged. The model never chooses what is started: the words are matched against the list of things found on THIS PC
(Start-menu programs, Store apps, Epic and Steam games, shortcuts) and a built-in list of well-known websites.

WHAT IT CAN START: programs and games found by the scan, and https:// websites in the default browser. Installers are never started from here. Nothing is typed,
clicked or read: it only starts things. The emergency-stop file is honoured.

SWITCHES: its own settings file (``quick_launch.json`` in the profile folder), OFF until the person turns it on in the window, and by default Jarvis asks "Allow this once?"
before every launch. It does not touch the advanced desktop-control permissions (``desktop_permissions.json``) and cannot grant itself anything there.
Every attempt is written to the same local action log as desktop actions (``desktop_audit.jsonl``)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jarvis.desktop import apps as appdisc
from jarvis.desktop import places, sites

STATE_FILE = "quick_launch.json"
INDEX_FILE = "quick_launch_index.json"
INDEX_MAX_AGE_S = 24 * 3600
OFF_MESSAGE = ("I can open apps, games and websites for you, but that switch is off. Turn on “Let Jarvis open apps, games and websites” on the "
               "Safety page (it asks before each one unless you change that).")

_LEAD = re.compile(r"^(?:(?:hey|ok|okay)\s+jarvis[,.!]?\s*|jarvis[,.!]?\s*)", re.I)
_FILLER = re.compile(r"^(?:please\s+|kindly\s+|can you\s+|could you\s+|would you\s+|will you\s+|i want you to\s+|i want to\s+|i'd like you to\s+|go ahead and\s+)+", re.I)
_TAIL = re.compile(r"(?:\s+(?:for me|please|now|thanks|thank you|right now))+$", re.I)
_STRONG = ("open", "launch", "go to", "goto", "take me to", "visit", "fire up", "boot up", "bring up", "pull up")
_WEAK = ("start", "run", "play", "load")
_OPEN = re.compile(r"^(?P<verb>" + "|".join(re.escape(v) for v in _STRONG + _WEAK) + r")\s+(?:up\s+)?(?:the\s+|my\s+|a\s+)?(?P<target>.+)$", re.I)
_SEARCH = re.compile(r"^(?:search|google|look up)\s+(?:(?P<eng>youtube|google)\s+)?(?:for\s+)?(?P<q>.+?)(?:\s+on\s+(?P<eng2>youtube|google))?$", re.I)
_SCAN = re.compile(r"^(?:scan|rescan|refresh|update|index)\s+(?:my\s+|the\s+)?(?:pc|computer|laptop|apps|games|programs|installed (?:apps|programs|games)|app list|machine)\b", re.I)
_LIST = re.compile(r"^(?:what|which)\s+(?:apps|games|programs|things|sites|websites)\s+(?:do i have|can you open|can you launch|can you start|are installed|do you see)|"
                   r"^(?:what can you open|list (?:my )?(?:apps|games|programs))", re.I)
_NOT_A_TARGET = re.compile(r"\b(?:file|files|folder|document|documents|how|why|what|where|when|which|that|this|it|them|something|anything|one|code|script|"
                           r"command|terminal|prompt|powershell|cmd|registry)\b", re.I)


@dataclass
class Command:
    kind: str                 # open | search | scan | list
    target: str = ""
    strong: bool = True       # a clear "open"-style verb; weak verbs (start/run/play) are left to the model unless the target is found on this PC
    engine: str = "google"


@dataclass
class LaunchRequest:
    """What the person is asked to approve (same fields the window's approval box reads)."""
    description: str
    tool: str = "quick_launch"
    risk: str = "launch"
    category: str = "apps"
    args: dict | None = None
    fingerprint: str = ""


# ------------------------------------------------------------------------------ reading what was said
def _clean(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "").strip())
    t = _LEAD.sub("", t)
    t = _FILLER.sub("", t)
    return t.strip(" ,.!?\"'")


def parse(text: str) -> Command | None:
    t = _clean(text)
    if not t or len(t) > 160 or "\n" in t:
        return None
    if _SCAN.match(t):
        return Command("scan")
    if _LIST.search(t):
        return Command("list")
    m = _SEARCH.match(t)
    if m and len(m["q"].split()) <= 12:
        eng = (m["eng"] or m["eng2"] or "google").lower()
        return Command("search", m["q"].strip(), True, eng)
    m = _OPEN.match(t)
    if not m:
        return None
    target = _TAIL.sub("", m["target"]).strip(" ,.!?\"'")
    verb = m["verb"].lower()
    if not target or len(target.split()) > 6 or len(target) > 80 or _NOT_A_TARGET.search(target) or "\\" in target or ":" in target.replace("://", ""):
        return None
    if re.match(r"^(?:by|with|to|from|in|on|about|over|at|for|as|an|some)\b", target, re.I) or (verb not in _STRONG and len(target.split()) > 4):
        return None
    return Command("open", target, verb in _STRONG)


# ------------------------------------------------------------------------------ settings (its own file)
def _state_path(cfg) -> Path:
    return Path(cfg.profile_dir) / STATE_FILE


def get_state(cfg, guest: bool = False) -> dict:
    st = {"enabled": False, "confirm_each": True}
    if guest:
        return st
    try:
        raw = json.loads(_state_path(cfg).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            st["enabled"] = raw.get("enabled") is True
            st["confirm_each"] = raw.get("confirm_each") is not False
    except (OSError, ValueError):
        pass
    return st


def set_state(cfg, *, enabled: bool | None = None, confirm_each: bool | None = None, guest: bool = False) -> dict:
    st = get_state(cfg, guest)
    if guest:
        return st
    if enabled is not None:
        st["enabled"] = bool(enabled)
    if confirm_each is not None:
        st["confirm_each"] = bool(confirm_each)
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, **st}, indent=2), encoding="utf-8")
    tmp.replace(p)
    return st


# ------------------------------------------------------------------------------ what is on this PC
def _backend(backend=None):
    if backend is not None:
        return backend
    from jarvis.desktop.backend import default_backend
    return default_backend()


def scan(cfg, backend=None) -> list[dict]:
    """Look for programs, Store apps, games and shortcuts (read-only) and remember the list in the profile folder."""
    apps = list(_backend(backend).discover_apps(dict(cfg.app_allowlist)))
    try:
        p = Path(cfg.profile_dir) / INDEX_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"at": time.time(), "apps": apps}), encoding="utf-8")
    except OSError:
        pass
    return apps


def load_index(cfg, backend=None, refresh: bool = False) -> list[dict]:
    p = Path(cfg.profile_dir) / INDEX_FILE
    if not refresh:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and time.time() - float(raw.get("at", 0)) < INDEX_MAX_AGE_S and isinstance(raw.get("apps"), list):
                return raw["apps"]
        except (OSError, ValueError, TypeError):
            pass
    return scan(cfg, backend)


_ALIASES = {"calc": "calculator", "chrome": "google chrome", "edge": "microsoft edge", "word": "microsoft word", "excel": "microsoft excel",
            "fortnite": "fortnite", "vs code": "visual studio code", "vscode": "visual studio code", "explorer": "file explorer", "files": "file explorer"}


def resolve(index: list[dict], target: str) -> tuple[str, object]:
    """('app', entry) | ('web', entry) | ('many', [entries]) | ('none', None). Prefers an installed program that is named exactly; a well-known site wins over a loose partial match."""
    q = re.sub(r"\s+", " ", target.strip().lower())
    q = _ALIASES.get(q, q)
    exact = [a for a in index if a["name"].lower() == q]
    site = sites.https_url_for(target)
    if exact:
        return "app", exact[0]
    for entry in (places.folder_entry(target), places.settings_entry(target)):
        if entry:
            return "app", entry
    if site:
        name, url = site
        return "web", {"id": "web:" + name, "name": name, "kind": "web", "url": url, "source": "website"}
    hits = [a for a in appdisc.match(index, q) if not appdisc.UNINSTALLER_NAME.search(a["name"])]
    if not hits:
        files = places.find_files(target)           # last resort: an ordinary file in your own folders (documents, pictures, music, videos)
        if len(files) == 1:
            return "app", files[0]
        return ("many", files) if files else ("none", None)
    starts = [a for a in hits if a["name"].lower().startswith(q)]
    pool = starts or hits
    if len(pool) == 1:
        return "app", pool[0]
    best = min(len(a["name"]) for a in pool)
    shortest = [a for a in pool if len(a["name"]) == best]
    if len(shortest) == 1 and len(pool) <= 3:
        return "app", shortest[0]
    return "many", sorted(pool, key=lambda a: a["name"].lower())[:6]


# ------------------------------------------------------------------------------ doing it
def _what_will_start(app: dict) -> str:
    return app.get("url") or app.get("uri") or app.get("target") or app.get("path") or app.get("aumid") or "(no path recorded)"



def _audit(cfg, guest: bool, **rec) -> None:
    try:
        from jarvis.desktop.audit import AuditLog
        AuditLog(None if guest else Path(cfg.profile_dir) / "desktop_audit.jsonl").write(event="end", risk="launch", tool="quick_launch", **rec)
    except Exception:      # noqa: BLE001 - the answer is still given; a missing log line must not block it
        pass


def _launch(app: dict, cfg, guest: bool, confirm, backend, st: dict, spoken: str) -> str:
    name = app["name"]
    if (Path(cfg.profile_dir) / "STOP").exists():
        return "The emergency stop is on, so I did not open anything. Clear it on the Safety page first."
    if appdisc.is_installer(app):
        _audit(cfg, guest, args={"app": app["id"]}, status="denied", summary=f"{name} looks like an installer; not started from a chat command")
        return f"{name} looks like an installer, so I won't start it from a chat command."
    if st["confirm_each"]:
        req = LaunchRequest(f"Open {name}?\nIt will start: {_what_will_start(app)}\n(You said: “{spoken[:100]}”)", args={"app": app["id"], "name": name})
        ok = False
        try:
            ok = bool(confirm(req)) if confirm is not None else False
        except Exception:      # noqa: BLE001 - a broken question box means no
            ok = False
        if not ok:
            _audit(cfg, guest, args={"app": app["id"]}, status="declined", summary=f"you did not allow opening {name}")
            return f"Okay, I did not open {name}."
    try:
        res = _backend(backend).launch(app, 8.0)
    except Exception as e:      # noqa: BLE001
        _audit(cfg, guest, args={"app": app["id"]}, status="failed", summary=f"{type(e).__name__}: {str(e)[:150]}")
        return f"Windows would not start {name} ({type(e).__name__}). Nothing was opened."
    verified, handed = bool(res.get("verified")), bool(res.get("handed_off"))
    _audit(cfg, guest, args={"app": app["id"], "name": name}, status="verified" if verified else "unverified", verified=verified,
           summary=("opened " if verified else "asked Windows to open ") + name, evidence=str(res.get("evidence", ""))[:300])
    if verified:
        return f"Opened {name}."
    if handed:
        return f"I asked Windows to open {name} in your browser. I can't check that the page loaded."
    return f"I asked Windows to start {name}, but I haven't seen its window yet. Launchers and games can take a while."


def _summary(index: list[dict]) -> str:
    by: dict[str, int] = {}
    for a in index:
        by[a.get("source", "other")] = by.get(a.get("source", "other"), 0) + 1
    games = [a["name"] for a in index if a.get("source", "").startswith(("Epic", "Steam", "Desktop game", "Start menu game"))]
    parts = ", ".join(f"{n} from {s}" for s, n in sorted(by.items(), key=lambda kv: -kv[1])[:5])
    out = f"I can see {len(index)} apps and games on this PC ({parts})."
    if games:
        out += " Games: " + ", ".join(sorted(games, key=str.lower)[:12]) + ("…" if len(games) > 12 else ".")
    return out + " I can also open well-known websites such as Instagram, YouTube and Google."


def handle(text: str, cfg, *, confirm: Callable | None, guest: bool = False, backend=None) -> str | None:
    """The reply to show and speak, or None when this is not a quick-launch command (then the normal assistant answers)."""
    cmd = parse(text)
    if cmd is None:
        return None
    st = get_state(cfg, guest)
    if not st["enabled"]:
        return OFF_MESSAGE if cmd.strong and not guest else None
    if cmd.kind == "scan":
        idx = scan(cfg, backend)
        return _summary(idx)
    if cmd.kind == "list":
        return _summary(load_index(cfg, backend))
    if cmd.kind == "search":
        url = sites.search_url(cmd.target, cmd.engine)
        if not url:
            return None
        return _launch({"id": "web:search", "name": f"a {cmd.engine.title()} search for “{cmd.target[:60]}”", "kind": "web", "url": url, "source": "website"},
                       cfg, guest, confirm, backend, st, text)
    index = load_index(cfg, backend)
    kind, found = resolve(index, cmd.target)
    if kind == "none":
        index = load_index(cfg, backend, refresh=True)           # it may have been installed since the last scan
        kind, found = resolve(index, cmd.target)
    if kind == "none":
        return f"I couldn't find “{cmd.target}” on this PC. Say “scan my PC” to refresh my list, or tell me the name as it appears in the Start menu." if cmd.strong else None
    if kind == "many":
        return "I found several matches: " + ", ".join(a["name"] for a in found) + ". Which one do you want? Say the full name."
    return _launch(found, cfg, guest, confirm, backend, st, text)
