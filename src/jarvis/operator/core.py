"""The gate every operator capability passes through: switch, permission category, risk, confirmation, stop/pause, audit and honest result status."""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

LOW, MEDIUM, HIGH = 1, 2, 3
RISK_NAMES = {LOW: "low", MEDIUM: "medium", HIGH: "high"}

# permission category -> (label, granted when you first turn control on)
CATEGORIES: dict[str, tuple[str, bool]] = {
    "system_read": ("Read system information (CPU, RAM, processes, battery, uptime)", True),
    "storage": ("Read drives and storage use", True),
    "network": ("Read network status and run connectivity checks", True),
    "security_read": ("Read security status (Defender, firewall, signatures)", True),
    "events": ("Read Windows event logs and update/driver information", True),
    "files_read": ("Search and read files inside allowed folders", True),
    "windows": ("Switch, minimize, maximize, snap and move windows; virtual desktops", True),
    "media": ("Media keys and volume", True),
    "apps": ("Close or restart applications you name", False),
    "files_write": ("Create, move, organize and delete (to the recycle folder) files in allowed folders", False),
    "archives": ("Create and extract ZIP archives", False),
    "backup": ("Back up and restore files", False),
    "settings": ("Open Settings pages, change wallpaper and brightness", False),
    "power": ("Lock, sleep, restart, shut down, sign out", False),
    "documents": ("Create and edit documents, spreadsheets, PDFs and images (always as new copies)", False),
    "dev_read": ("Read git repositories and project files", False),
    "dev_write": ("Commit and push with git; run project tests", False),
    "gaming": ("Gaming Mode and game library", False),
    "tasks": ("Tasks, reminders, local calendar, drafts", True),
    "workflows": ("Save and run workflows and automations", False),
    "services": ("Start, stop or restart Windows services", False),
}

SECRET_ARG_NAMES = {"text", "body", "content", "password", "token", "secret", "message", "paragraphs", "rows"}
_SECRET_RE = re.compile(r"(?i)(password|passwd|pwd|token|secret|api[_-]?key|bearer)\s*[:=]\s*\S+")


class OperatorError(Exception):
    """A refusal or problem with a plain-language message."""


class Unavailable(OperatorError):
    """The feature cannot run here (not Windows, missing library, missing tool)."""


class PathRefused(OperatorError):
    pass


@dataclass
class Outcome:
    status: str          # verified | unverified | info | failed | denied | declined | refused | off | stopped | unavailable | paused
    message: str
    data: Any = None

    @property
    def ok(self) -> bool:
        return self.status in ("verified", "unverified", "info")


@dataclass
class Capability:
    id: str
    category: str
    title: str
    fn: Callable[..., Outcome]
    risk: int | Callable[[dict], int] = LOW
    params: tuple = ()
    windows_only: bool = False
    spec: str = ""                      # spec section numbers this capability serves, e.g. "1,2"

    def risk_for(self, args: dict) -> int:
        try:
            return int(self.risk(args)) if callable(self.risk) else int(self.risk)
        except Exception:  # noqa: BLE001 - an unreadable argument is treated as the highest risk
            return HIGH


REGISTRY: dict[str, Capability] = {}


def cap(cid: str, category: str, title: str, risk=LOW, params: tuple = (), windows_only: bool = False, spec: str = ""):
    def deco(fn):
        assert category in CATEGORIES, category
        REGISTRY[cid] = Capability(cid, category, title, fn, risk, params, windows_only, spec)
        return fn
    return deco


def load_all() -> dict[str, Capability]:
    from jarvis.operator import (apps_ops, archives, backup, dev_ops, docs_ops, events_ops, files_ops, gaming, images_ops, network_ops, security_ops,  # noqa: F401
                                 settings_ops, storage, system_ops, tasks_ops, windows_ops, workflows)
    return REGISTRY


def redact(v: Any, limit: int = 80) -> str:
    s = _SECRET_RE.sub(lambda m: m.group(1) + "=<hidden>", str(v))
    return s if len(s) <= limit else s[:limit] + "..."


def summarize_args(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        out[k] = f"<{len(str(v))} chars>" if k in SECRET_ARG_NAMES else redact(v)
    return out


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Operator:
    AUDIT_MAX_LINES = 3000

    def __init__(self, folder: Path, *, guest: bool = False, confirm: Callable | None = None, ops=None, home: Path | None = None):
        self.folder = Path(folder)
        self.guest = guest
        self.confirm = confirm
        self.home = Path(home) if home else Path.home()
        from jarvis.operator.ops import default_ops
        self.ops = ops if ops is not None else default_ops()
        self.cancel = threading.Event()
        self.paused = threading.Event()
        self._lock = threading.Lock()
        self.current: str = ""
        self.recent: list[dict] = []
        load_all()

    # ---------------------------------------------------------------- stored choices
    @property
    def state_file(self) -> Path:
        return self.folder / "operator.json"

    def state(self) -> dict:
        d = {"enabled": False, "categories": {}, "confirm_medium": True, "folders": [], "protected_apps": [], "game_close_list": []}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                d["enabled"] = bool(raw.get("enabled", False))
                d["confirm_medium"] = bool(raw.get("confirm_medium", True))
                d["categories"] = {k: bool(v) for k, v in dict(raw.get("categories", {})).items() if k in CATEGORIES}
                d["folders"] = [str(x) for x in raw.get("folders", []) if isinstance(x, str)][:50]
                d["game_close_list"] = [str(x).lower() for x in raw.get("game_close_list", []) if isinstance(x, str)][:30]
        except (OSError, ValueError):
            pass
        if self.guest:
            d["enabled"] = False
        return d

    def _write(self, d: dict) -> None:
        if self.guest:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(d), encoding="utf-8")
        except OSError:
            pass

    def is_on(self) -> bool:
        return self.state()["enabled"]

    def turn_on(self) -> dict:
        d = self.state()
        d["enabled"] = True
        for k, (_label, default) in CATEGORIES.items():
            d["categories"].setdefault(k, default)
        self._write(d)
        self.audit("operator.on", LOW, "allowed", "info", "computer control turned on")
        return self.state()

    def turn_off(self) -> dict:
        d = self.state()
        d["enabled"] = False
        self._write(d)
        self.audit("operator.off", LOW, "allowed", "info", "computer control turned off")
        return self.state()

    def set_category(self, name: str, on: bool) -> dict:
        if name not in CATEGORIES:
            raise ValueError("unknown permission")
        d = self.state()
        d["categories"][name] = bool(on)
        self._write(d)
        self.audit("operator.permission", LOW, "allowed", "info", f"{name} {'granted' if on else 'revoked'}")
        return self.state()

    def reset_permissions(self) -> dict:
        d = self.state()
        d["categories"] = {k: v[1] for k, v in CATEGORIES.items()}
        d["folders"] = []
        d["game_close_list"] = []
        d["confirm_medium"] = True
        self._write(d)
        self.audit("operator.permission", LOW, "allowed", "info", "permissions reset to defaults")
        return self.state()

    def set_confirm_medium(self, on: bool) -> dict:
        d = self.state()
        d["confirm_medium"] = bool(on)
        self._write(d)
        return self.state()

    def add_folder(self, path: str) -> dict:
        p = Path(path)
        self._check_root_candidate(p)
        d = self.state()
        s = str(p.resolve())
        if s not in d["folders"]:
            d["folders"].append(s)
        self._write(d)
        self.audit("operator.folder", LOW, "allowed", "info", "folder added")
        return self.state()

    def remove_folder(self, path: str) -> dict:
        d = self.state()
        d["folders"] = [f for f in d["folders"] if f != str(path)]
        self._write(d)
        return self.state()

    def set_game_close_list(self, names: list[str]) -> dict:
        d = self.state()
        d["game_close_list"] = [n.strip().lower() for n in names if n.strip()][:30]
        self._write(d)
        return self.state()

    def granted(self, category: str) -> bool:
        return bool(self.state()["categories"].get(category, CATEGORIES[category][1] if category in CATEGORIES else False))

    # ---------------------------------------------------------------- folders
    FORBIDDEN_PARTS = ("windows", "program files", "program files (x86)", "programdata", "system32", "$recycle.bin", "system volume information")

    def _check_root_candidate(self, p: Path) -> None:
        rp = p.resolve()
        if not rp.is_dir():
            raise PathRefused("That is not a folder.")
        if rp == Path(rp.anchor) or rp == self.home.resolve():
            raise PathRefused("A whole drive or your whole user folder cannot be allowed. Pick a specific folder.")
        if any(part.lower() in self.FORBIDDEN_PARTS for part in rp.parts[1:2] + rp.parts[1:]):
            raise PathRefused("System folders cannot be allowed.")

    def roots(self) -> list[Path]:
        out: list[Path] = []
        for name in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"):
            p = self.home / name
            if p.is_dir():
                out.append(p.resolve())
        for f in self.state()["folders"]:
            p = Path(f)
            if p.is_dir():
                out.append(p.resolve())
        return out

    def check_path(self, path: str | Path, *, must_exist: bool = True, write: bool = False) -> Path:
        s = str(path).strip().strip('"')
        if not s or "\x00" in s:
            raise PathRefused("No path was given.")
        if s.startswith("\\\\") or s.startswith("//"):
            raise PathRefused("Network (UNC) paths are not used.")
        if re.search(r"[A-Za-z]:.*:", s) or re.search(r"(^|[\\/])(con|prn|aux|nul|com\d|lpt\d)([\\/.]|$)", s, re.I):
            raise PathRefused("That path is not allowed (stream or device name).")
        p = Path(s).expanduser()
        if not p.is_absolute():
            named = [r for r in self.roots() if r.name.lower() == s.lower().strip("\\/")]
            if named:
                p = named[0]
        if not p.is_absolute():
            for root in self.roots():
                cand = root / s
                if cand.exists():
                    p = cand
                    break
            else:
                p = (self.roots()[0] / s) if self.roots() else p.resolve()
        rp = p.resolve()
        if not any(rp == r or r in rp.parents for r in self.roots()):
            raise PathRefused("That is outside the folders you allowed (Desktop, Documents, Downloads, Pictures, Videos, Music and any you added on the Operator page).")
        if write and rp in self.roots():
            raise PathRefused("That is one of the allowed folders themselves; pick something inside it.")
        if must_exist and not rp.exists():
            raise PathRefused("I can't find that.")
        return rp

    def find_in_roots(self, name: str, kinds: str = "any", limit: int = 5) -> list[Path]:
        """Look a file or folder up BY NAME inside the allowed folders (shallow first)."""
        q = name.strip().strip('"').lower()
        hits: list[Path] = []
        for root in self.roots():
            for dirpath, dirs, files in _walk(root, 4):
                pool = ([] if kinds == "file" else dirs) + ([] if kinds == "dir" else files)
                for n in pool:
                    low = n.lower()
                    if low == q or low.rsplit(".", 1)[0] == q or (len(q) > 3 and q in low):
                        hits.append(Path(dirpath) / n)
                if len(hits) >= limit * 4:
                    break
        hits.sort(key=lambda h: (h.name.lower() != q and h.stem.lower() != q, len(h.parts)))
        return hits[:limit]

    # ---------------------------------------------------------------- audit
    @property
    def audit_file(self) -> Path:
        return self.folder / "operator_audit.jsonl"

    def audit(self, cap_id: str, risk: int, decision: str, status: str, note: str = "", args: dict | None = None) -> None:
        rec = {"t": _now(), "cap": cap_id, "risk": RISK_NAMES.get(risk, "?"), "decision": decision, "status": status, "note": redact(note, 160)}
        if args:
            rec["args"] = summarize_args(args)
        self.recent.append(rec)
        del self.recent[:-200]
        if self.guest:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self.audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            if self.audit_file.stat().st_size > 2_000_000:
                lines = self.audit_file.read_text(encoding="utf-8").splitlines()[-self.AUDIT_MAX_LINES // 2:]
                self.audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    def audit_tail(self, n: int = 100) -> list[dict]:
        try:
            lines = self.audit_file.read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    # ---------------------------------------------------------------- the gate
    def stop_active(self) -> bool:
        return (self.folder / "STOP").exists()

    def describe(self, c: Capability, args: dict) -> str:
        bits = ", ".join(f"{k}={redact(v, 60)}" for k, v in args.items() if k not in SECRET_ARG_NAMES and v not in (None, "", []))
        return f"{c.title}" + (f" ({bits})" if bits else "")

    def run(self, cap_id: str, **args) -> Outcome:
        c = REGISTRY.get(cap_id)
        if c is None:
            return Outcome("unavailable", f"I don't have a capability called {cap_id}.")
        risk = c.risk_for(args)
        if self.guest:
            return self._done(c, risk, "denied", Outcome("denied", "Computer control is not available in a guest session."), args)
        if self.stop_active():
            return self._done(c, risk, "stopped", Outcome("stopped", "The emergency stop is on, so I did nothing. Clear it on the Safety page first."), args)
        if not self.is_on():
            return self._done(c, risk, "off", Outcome("off", "Computer control is off. Turn it on from the Operator page first."), args)
        if not self.granted(c.category):
            return self._done(c, risk, "denied", Outcome("denied", f"That needs the permission \"{CATEGORIES[c.category][0]}\", which is off. Turn it on from the Operator page."), args)
        if self.cancel.is_set():
            self.cancel.clear()
            return self._done(c, risk, "stopped", Outcome("stopped", "Cancelled."), args)
        if self.paused.is_set():
            return self._done(c, risk, "paused", Outcome("paused", "Computer control is paused. Resume it from the Operator page."), args)
        decision = "auto"
        need = risk >= HIGH or (risk == MEDIUM and self.state()["confirm_medium"])
        if need:
            ok = False
            if self.confirm is not None:
                try:
                    ok = bool(self.confirm(SimpleNamespace(description=self.describe(c, args), tool=c.id, risk=RISK_NAMES[risk])))
                except Exception:  # noqa: BLE001 - a broken dialog means no
                    ok = False
            if not ok:
                return self._done(c, risk, "declined", Outcome("declined", "Okay, I did not do that."), args)
            decision = "confirmed"
        if c.windows_only and not self.ops.is_windows:
            return self._done(c, risk, decision, Outcome("unavailable", f"\"{c.title}\" only works on Windows."), args)
        self.current = c.title
        try:
            out = c.fn(self, **args)
            if not isinstance(out, Outcome):
                out = Outcome("failed", "The action returned nothing I can check.")
        except OperatorError as e:
            out = Outcome("unavailable" if isinstance(e, Unavailable) else "refused", str(e))
        except TypeError as e:
            out = Outcome("failed", "I was missing something for that: " + str(e)[:120])
        except Exception as e:  # noqa: BLE001 - never a traceback in front of the user
            out = Outcome("failed", f"That failed ({type(e).__name__}: {str(e)[:140]}).")
        finally:
            self.current = ""
        return self._done(c, risk, decision, out, args)

    def _done(self, c: Capability, risk: int, decision: str, out: Outcome, args: dict) -> Outcome:
        self.audit(c.id, risk, decision, out.status, out.message[:160], args)
        return out

    # ---------------------------------------------------------------- pause / cancel (used by workflows and the page)
    def pause(self) -> None:
        self.paused.set()
        self.audit("operator.pause", LOW, "allowed", "info", "paused")

    def resume(self) -> None:
        self.paused.clear()
        self.audit("operator.resume", LOW, "allowed", "info", "resumed")

    def cancel_all(self) -> None:
        self.cancel.set()
        self.audit("operator.cancel", LOW, "allowed", "info", "cancel requested")

    # ---------------------------------------------------------------- helpers for capability modules
    def table(self) -> list[dict]:
        """One row per capability for the Operator page and the matrix."""
        rows = []
        for c in sorted(REGISTRY.values(), key=lambda x: (x.category, x.id)):
            rows.append({"id": c.id, "title": c.title, "category": c.category, "risk": RISK_NAMES[c.risk_for({}) if not callable(c.risk) else HIGH] if not callable(c.risk) else "varies",
                         "windows_only": c.windows_only, "granted": self.granted(c.category)})
        return rows


def _walk(root: Path, depth: int):
    import os
    base = len(Path(root).parts)
    for dirpath, dirs, files in os.walk(root):
        if len(Path(dirpath).parts) - base >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        yield dirpath, dirs, files
