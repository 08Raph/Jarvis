"""Applications: inventory, versions, running state, close and restart (politely), updates check pointer."""
from __future__ import annotations

import re
import time

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, Unavailable, cap
from jarvis.operator.system_ops import PROTECTED_PROCESSES, _psutil

INSTALLER_NAMES = re.compile(r"(?i)(setup|install|uninstall|unins\d*|update)\b.*\.(exe|msi)$")


def _installed() -> list[dict]:
    try:
        import winreg
    except ImportError:
        raise Unavailable("The installed-program list can only be read on Windows.") from None
    out: dict[str, dict] = {}
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as s:
                                def g(n):
                                    try:
                                        return str(winreg.QueryValueEx(s, n)[0])
                                    except OSError:
                                        return ""
                                name = g("DisplayName")
                                if name and not g("SystemComponent") == "1":
                                    out.setdefault(name.lower(), {"name": name, "version": g("DisplayVersion"), "publisher": g("Publisher"), "location": g("InstallLocation")})
                        except OSError:
                            pass
            except OSError:
                pass
    return sorted(out.values(), key=lambda d: d["name"].lower())


@cap("apps.installed", "system_read", "List installed programs with versions and publishers", LOW, ("query",), True, "9")
def installed(op, query: str = "") -> Outcome:
    rows = _installed()
    if query:
        rows = [r for r in rows if query.lower() in r["name"].lower()]
    msg = f"{len(rows)} installed program(s)" + (": " + "; ".join(f"{r['name']} {r['version']}".strip() for r in rows[:20]) if rows else ".")
    return Outcome("info", msg, rows)


@cap("apps.running", "system_read", "Check whether a program is running", LOW, ("name",), False, "9")
def running(op, name: str = "") -> Outcome:
    ps = _psutil()
    q = name.lower().strip()
    hits = sorted({p.info["name"] for p in ps.process_iter(["name"]) if p.info["name"] and q and q in p.info["name"].lower()})
    return Outcome("info", f"Yes, {', '.join(hits[:5])} is running." if hits else f"I don't see {name} running.", hits)


def _match(name: str):
    ps = _psutil()
    q = name.lower().strip().removesuffix(".exe")
    out = []
    for p in ps.process_iter(["pid", "name", "exe"]):
        n = (p.info["name"] or "").lower()
        if n.removesuffix(".exe") == q or (len(q) > 3 and q in n.removesuffix(".exe")):
            out.append(p)
    return out


def _guard(p) -> None:
    n = (p.info["name"] or "").lower()
    if n in PROTECTED_PROCESSES or "jarvis" in n:
        raise OperatorError(f"I won't close {p.info['name']}: it is part of Windows or Jarvis itself.")


@cap("apps.close", "apps", "Close a program politely (it can still ask to save)", MEDIUM, ("name",), False, "9,1")
def close_app(op, name: str = "") -> Outcome:
    if not name.strip():
        raise OperatorError("Tell me which program to close.")
    procs = _match(name)
    if not procs:
        return Outcome("info", f"{name} isn't running.")
    for p in procs:
        _guard(p)
    ps = _psutil()
    for p in procs:
        try:
            if op.ops.is_windows:
                code, _o, _e = op.ops.run(["taskkill.exe", "/PID", str(p.pid)], 10)       # without /F: a polite close request
            else:
                p.terminate()
        except (ps.NoSuchProcess, ps.AccessDenied):
            pass
    time.sleep(1.2)
    left = [p for p in procs if _alive(p)]
    return Outcome("verified" if not left else "unverified",
                   f"Closed {name}." if not left else f"Asked {name} to close, but it is still running (it may be asking you to save). I did not force it.")


def _alive(p) -> bool:
    try:
        return p.is_running() and p.status() != "zombie"
    except Exception:  # noqa: BLE001
        return False


@cap("apps.force_close", "apps", "Force a program to close (unsaved work is lost)", HIGH, ("name",), False, "9")
def force_close(op, name: str = "") -> Outcome:
    procs = _match(name)
    if not procs:
        return Outcome("info", f"{name} isn't running.")
    for p in procs:
        _guard(p)
    for p in procs:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.6)
    left = [p for p in procs if _alive(p)]
    return Outcome("verified" if not left else "failed", f"Force-closed {name}." if not left else f"{name} is still running (it may run as administrator).")


@cap("apps.restart", "apps", "Restart a program", HIGH, ("name",), True, "9")
def restart_app(op, name: str = "") -> Outcome:
    procs = _match(name)
    if not procs:
        raise OperatorError(f"{name} isn't running, so I don't know where it lives. Open it normally.")
    p = procs[0]
    _guard(p)
    exe = p.info.get("exe") or ""
    if not exe or INSTALLER_NAMES.search(exe.rsplit("\\", 1)[-1]):
        raise OperatorError("I can't restart that one safely.")
    close = close_app(op, name)
    if not close.ok or close.status != "verified":
        return Outcome("failed", "It didn't close, so I did not start a second copy. " + close.message)
    op.ops.open_target(exe)
    time.sleep(1.5)
    ok = bool(_match(name))
    return Outcome("verified" if ok else "failed", f"Restarted {name}." if ok else f"I closed {name} but it did not come back.")
