"""Windows desktop control: windows, snapping, virtual desktops, power, media keys, volume, screenshots."""
from __future__ import annotations

import re
import time

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, cap

PROTECTED_WINDOW_PROCESSES = {"winlogon.exe", "csrss.exe", "lsass.exe", "services.exe", "msmpeng.exe", "securityhealthsystray.exe", "taskmgr.exe", "regedit.exe",
                              "consent.exe", "logonui.exe", "windowsdefender.exe", "mpdefendercoreservice.exe"}


def _pick(op, window: str | None) -> dict:
    """The window named (title or program contains the words), or the active one."""
    wins = op.ops.windows()
    jarvis_self = ("jarvis",)
    if not window or window.lower() in ("this", "it", "active", "current", "the active window", "this window"):
        fg = op.ops.foreground()
        if not fg:
            raise OperatorError("I can't tell which window is active.")
        return fg
    q = window.lower().strip()
    exact = [w for w in wins if w["title"].lower() == q or (w.get("process") or "").rsplit(".", 1)[0] == q]
    hits = exact or [w for w in wins if q in w["title"].lower() or q in (w.get("process") or "")]
    hits = [w for w in hits if not (q not in jarvis_self and "jarvis" in w["title"].lower())] or hits
    if not hits:
        raise OperatorError(f"I don't see a window called {window}.")
    return hits[0]


def _refuse_protected(w: dict) -> None:
    if (w.get("process") or "") in PROTECTED_WINDOW_PROCESSES:
        raise OperatorError("I leave that window alone: it belongs to Windows security or a sign-in/system tool.")


@cap("window.list", "windows", "List open windows", LOW, ("",), True, "1")
def window_list(op) -> Outcome:
    ws = [w for w in op.ops.windows() if w["title"]]
    lines = [f"{w['title'][:60]} ({(w.get('process') or '?').replace('.exe', '')}){' - minimized' if w.get('minimized') else ''}" for w in ws[:25]]
    return Outcome("info", f"{len(ws)} windows open: " + "; ".join(lines) if ws else "No windows are open.", ws)


@cap("window.active", "windows", "Tell the active window and application", LOW, (), True, "1,55")
def window_active(op) -> Outcome:
    fg = op.ops.foreground()
    if not fg:
        return Outcome("info", "I can't tell which window is active.")
    return Outcome("info", f"The active window is \"{fg['title']}\" ({(fg.get('process') or 'unknown').replace('.exe', '')}).", fg)


@cap("window.action", "windows", "Minimize, maximize, restore or close a window", lambda a: MEDIUM if a.get("action") == "close" else LOW, ("window", "action"), True, "1")
def window_action(op, window: str | None = None, action: str = "minimize") -> Outcome:
    if action not in ("minimize", "maximize", "restore", "close", "front"):
        raise OperatorError("I can minimize, maximize, restore, close or bring a window to the front.")
    w = _pick(op, window)
    _refuse_protected(w)
    op.ops.window_action(w["hwnd"], action)
    time.sleep(0.25)
    after = {x["hwnd"]: x for x in op.ops.windows()}.get(w["hwnd"])
    if action == "close":
        gone = after is None
        return Outcome("verified" if gone else "unverified", f"Asked \"{w['title'][:50]}\" to close." + ("" if gone else " It is still open (it may be asking to save)."))
    if action == "minimize":
        ok = bool(after and after.get("minimized"))
    elif action == "restore":
        ok = bool(after and not after.get("minimized"))
    else:
        ok = after is not None
    return Outcome("verified" if ok else "unverified", f"{action.capitalize()}d \"{w['title'][:50]}\"." if ok else f"I asked Windows to {action} \"{w['title'][:50]}\" but could not confirm it.")


SNAPS = {"left": (0, 0, .5, 1), "right": (.5, 0, .5, 1), "top": (0, 0, 1, .5), "bottom": (0, .5, 1, .5),
         "top left": (0, 0, .5, .5), "top right": (.5, 0, .5, .5), "bottom left": (0, .5, .5, .5), "bottom right": (.5, .5, .5, .5), "full": (0, 0, 1, 1)}


@cap("window.snap", "windows", "Snap a window to a side or corner of its screen", LOW, ("window", "where"), True, "1")
def window_snap(op, window: str | None = None, where: str = "left") -> Outcome:
    if where not in SNAPS:
        raise OperatorError("Snap to: " + ", ".join(SNAPS) + ".")
    w = _pick(op, window)
    _refuse_protected(w)
    mons = op.ops.monitors()
    cx = (w["rect"][0] + w["rect"][2]) // 2
    cy = (w["rect"][1] + w["rect"][3]) // 2
    mon = next((m for m in mons if m["left"] <= cx < m["right"] and m["top"] <= cy < m["bottom"]), mons[0] if mons else None)
    if mon is None:
        raise OperatorError("I can't read your screens.")
    fx, fy, fw, fh = SNAPS[where]
    mw, mh = mon["right"] - mon["left"], mon["bottom"] - mon["top"]
    x, y, ww, hh = mon["left"] + int(fx * mw), mon["top"] + int(fy * mh), int(fw * mw), int(fh * mh)
    op.ops.window_place(w["hwnd"], x, y, ww, hh)
    after = {q["hwnd"]: q for q in op.ops.windows()}.get(w["hwnd"])
    ok = bool(after and all(abs(a - b) <= 24 for a, b in zip(after["rect"], [x, y, x + ww, y + hh])))
    return Outcome("verified" if ok else "unverified", f"Snapped \"{w['title'][:50]}\" to the {where}." if ok else "I moved it but the window did not end up where I asked (some windows keep a minimum size).")


@cap("window.resize", "windows", "Resize or move a window", LOW, ("window", "x", "y", "width", "height"), True, "1")
def window_resize(op, window: str | None = None, x: int | None = None, y: int | None = None, width: int | None = None, height: int | None = None) -> Outcome:
    w = _pick(op, window)
    _refuse_protected(w)
    r = w["rect"]
    nx = int(x if x is not None else r[0])
    ny = int(y if y is not None else r[1])
    nw = int(width if width is not None else r[2] - r[0])
    nh = int(height if height is not None else r[3] - r[1])
    if not (100 <= nw <= 20000 and 60 <= nh <= 20000):
        raise OperatorError("That size doesn't make sense for a window.")
    op.ops.window_place(w["hwnd"], nx, ny, nw, nh)
    return Outcome("unverified", f"Resized \"{w['title'][:50]}\" to {nw} by {nh}.")


@cap("window.to_monitor", "windows", "Move a window to another screen", LOW, ("window", "screen"), True, "1")
def window_to_monitor(op, window: str | None = None, screen: int = 2) -> Outcome:
    from jarvis.desktop import win32
    w = _pick(op, window)
    _refuse_protected(w)
    res = win32.move_window_to_monitor(w["hwnd"], int(screen))
    return Outcome("unverified", f"Moved \"{w['title'][:50]}\" to screen {res['screen']}.", res)


@cap("window.check_frozen", "windows", "Find windows that have stopped responding", LOW, (), True, "1,16")
def window_check_frozen(op) -> Outcome:
    hung = [w for w in op.ops.windows() if op.ops.is_hung(w["hwnd"])]
    if not hung:
        return Outcome("info", "Every open window is responding.")
    return Outcome("info", "Not responding: " + ", ".join(f"{w['title'][:40]} ({(w.get('process') or '?').replace('.exe', '')})" for w in hung) + ". Give them a minute; I only close a program if you ask.", hung)


def _keys(op, combo: list[str], what: str) -> Outcome:
    op.ops.keys(combo)
    return Outcome("unverified", f"{what} (a key shortcut was sent; I can't check the result).")


@cap("desktop.new", "windows", "Create a virtual desktop", LOW, (), True, "1")
def desk_new(op) -> Outcome:
    return _keys(op, ["win", "ctrl", "d"], "Created a new virtual desktop")


@cap("desktop.next", "windows", "Switch to the next virtual desktop", LOW, (), True, "1")
def desk_next(op) -> Outcome:
    return _keys(op, ["win", "ctrl", "right"], "Switched to the next desktop")


@cap("desktop.prev", "windows", "Switch to the previous virtual desktop", LOW, (), True, "1")
def desk_prev(op) -> Outcome:
    return _keys(op, ["win", "ctrl", "left"], "Switched to the previous desktop")


@cap("desktop.close", "windows", "Close the current virtual desktop", MEDIUM, (), True, "1")
def desk_close(op) -> Outcome:
    return _keys(op, ["win", "ctrl", "f4"], "Closed the current virtual desktop (its windows move to the neighbouring one)")


@cap("desktop.task_view", "windows", "Open Task View", LOW, (), True, "1")
def task_view(op) -> Outcome:
    return _keys(op, ["win", "tab"], "Opened Task View")


@cap("desktop.start_menu", "windows", "Open the Start menu", LOW, (), True, "1")
def start_menu(op) -> Outcome:
    return _keys(op, ["win"], "Opened the Start menu")


@cap("desktop.search", "windows", "Open Windows Search", LOW, (), True, "1")
def win_search(op) -> Outcome:
    return _keys(op, ["win", "s"], "Opened Windows Search")


@cap("desktop.show_desktop", "windows", "Show the desktop", LOW, (), True, "1")
def show_desktop(op) -> Outcome:
    return _keys(op, ["win", "d"], "Showed the desktop")


# ----------------------------------------------------------------------------- media and volume
@cap("media.key", "media", "Play, pause, skip or stop media", LOW, ("key",), True, "35")
def media_key(op, key: str = "play_pause") -> Outcome:
    if key not in ("play_pause", "next", "prev", "stop"):
        raise OperatorError("I can play/pause, skip, go back or stop.")
    op.ops.keys([key])
    names = {"play_pause": "Toggled play/pause", "next": "Skipped to the next track", "prev": "Went back a track", "stop": "Stopped media"}
    return Outcome("unverified", names[key] + " (a media key was sent; Windows doesn't tell me what played).")


@cap("media.volume", "media", "Change the volume (each step is about 2 percent)", LOW, ("direction", "steps"), True, "35,15")
def media_volume(op, direction: str = "up", steps: int = 5) -> Outcome:
    if direction not in ("up", "down", "mute"):
        raise OperatorError("Volume can go up, down or be muted.")
    steps = max(1, min(int(steps), 50))
    key = {"up": "vol_up", "down": "vol_down", "mute": "mute"}[direction]
    for _ in range(1 if direction == "mute" else steps):
        op.ops.keys([key])
    return Outcome("unverified", "Toggled mute." if direction == "mute" else f"Volume {direction} by about {steps * 2} percent.")


@cap("media.set_volume", "media", "Set the volume to a level", LOW, ("percent",), True, "35,15")
def media_set_volume(op, percent: int = 30) -> Outcome:
    p = max(0, min(int(percent), 100))
    for _ in range(50):
        op.ops.keys(["vol_down"])
    for _ in range(round(p / 2)):
        op.ops.keys(["vol_up"])
    return Outcome("unverified", f"Set the volume to about {p} percent. Windows doesn't let me read it back, so this is an estimate.")


@cap("media.screenshot", "media", "Take a screenshot (saved in Pictures/Jarvis screenshots)", LOW, (), True, "35,12,33")
def media_screenshot(op) -> Outcome:
    folder = op.home / "Pictures" / "Jarvis screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / time.strftime("screenshot-%Y%m%d-%H%M%S.png")
    op.ops.screenshot(str(f))
    ok = f.exists() and f.stat().st_size > 100
    return Outcome("verified" if ok else "failed", f"Saved a screenshot: {f.name}." if ok else "The screenshot file was not created.", str(f))


@cap("media.record_screen", "media", "Start or stop a screen recording with the Windows Game Bar", MEDIUM, ("action",), True, "35,10")
def media_record(op, action: str = "toggle") -> Outcome:
    op.ops.keys(["win", "alt", "r"])
    return Outcome("unverified", "Sent the Windows recording shortcut (Win+Alt+R). Windows shows its own recording indicator; Game Bar must be enabled. I can't confirm it started.")


# ----------------------------------------------------------------------------- session and power
@cap("power.lock", "power", "Lock Windows", MEDIUM, (), True, "3")
def power_lock(op) -> Outcome:
    op.ops.lock()
    return Outcome("unverified", "Locked Windows.")


@cap("power.sleep", "power", "Put the PC to sleep", HIGH, (), True, "3")
def power_sleep(op) -> Outcome:
    op.ops.power("sleep")
    return Outcome("unverified", "Putting the PC to sleep.")


@cap("power.hibernate", "power", "Hibernate the PC", HIGH, (), True, "3")
def power_hibernate(op) -> Outcome:
    op.ops.power("hibernate")
    return Outcome("unverified", "Hibernating (works only if hibernation is enabled in Windows).")


@cap("power.restart", "power", "Restart Windows (30 second delay, can be cancelled)", HIGH, (), True, "3")
def power_restart(op) -> Outcome:
    op.ops.power("restart")
    return Outcome("unverified", "Windows will restart in 30 seconds. Say \"cancel the shutdown\" to stop it. Save your work.")


@cap("power.shutdown", "power", "Shut down Windows (30 second delay, can be cancelled)", HIGH, (), True, "3")
def power_shutdown(op) -> Outcome:
    op.ops.power("shutdown")
    return Outcome("unverified", "Windows will shut down in 30 seconds. Say \"cancel the shutdown\" to stop it. Save your work.")


@cap("power.signout", "power", "Sign out of Windows", HIGH, (), True, "3")
def power_signout(op) -> Outcome:
    op.ops.power("signout")
    return Outcome("unverified", "Signing out.")


@cap("power.cancel", "power", "Cancel a pending shutdown or restart", LOW, (), True, "3")
def power_cancel(op) -> Outcome:
    try:
        op.ops.power("cancel")
    except OSError:
        return Outcome("info", "There was no shutdown waiting to be cancelled.")
    return Outcome("unverified", "Cancelled the pending shutdown or restart.")


@cap("power.idle", "system_read", "Check whether the PC is idle", LOW, (), True, "3")
def power_idle(op) -> Outcome:
    s = op.ops.idle_seconds()
    if s is None:
        return Outcome("unavailable", "I can't read the idle time here.")
    return Outcome("info", f"The PC has been idle for {int(s)} seconds." if s >= 5 else "You're using the PC right now.", s)
