"""Desktop backends.

WindowsDesktop  - the real one: Win32 (ctypes), UI Automation (comtypes), Windows OCR. Written against documented APIs but
                  NOT executed on Windows by the author; docs/WINDOWS_TEST_CHECKLIST.md lists what to try.
PosixDesktop    - what can honestly work elsewhere: launching an allow-listed program and checking its process.
SimulatedDesktop- a FAKE desktop for tests and demos. Its windows and buttons are Python objects. Anything it reports is
                  about the simulation, never about a real PC; results carry backend="simulated".
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.desktop import apps as appdisc


class Unsupported(RuntimeError):
    pass


class DesktopBackend:
    name = "none"
    simulated = False

    def windows(self) -> list[dict]: raise Unsupported(f"{self.name}: listing windows is not supported here")
    def foreground(self) -> int: raise Unsupported(f"{self.name}: foreground window is not available here")
    def focus(self, hwnd: int) -> bool: raise Unsupported(f"{self.name}: switching windows is not supported here")
    def process_names(self) -> set[str]: return set()
    def elevated(self) -> bool: return False
    def discover_apps(self, allow: dict) -> list[dict]: return appdisc.from_allowlist(allow)
    def launch(self, app: dict, wait: float) -> dict: raise Unsupported(f"{self.name}: launching is not supported here")
    def ui_find(self, hwnd: int, sel: dict) -> list[dict]: raise Unsupported(f"{self.name}: UI Automation is not available here")
    def ui_read(self, hwnd: int, sel: dict) -> dict: raise Unsupported(f"{self.name}: UI Automation is not available here")
    def ui_act(self, hwnd: int, sel: dict, action: str, value: str | None) -> dict: raise Unsupported(f"{self.name}: UI Automation is not available here")
    def focused_value(self) -> str | None: return None
    def type_text(self, text: str) -> None: raise Unsupported(f"{self.name}: keyboard input is not supported here")
    def press(self, combo: list[int]) -> None: raise Unsupported(f"{self.name}: keyboard input is not supported here")
    def click(self, x: int, y: int, button: str, double: bool) -> None: raise Unsupported(f"{self.name}: mouse input is not supported here")
    def screenshot(self, hwnd: int | None) -> tuple[int, int, bytes]: raise Unsupported(f"{self.name}: screenshots are not supported here")
    def ocr(self, png_path: Path) -> dict:
        from jarvis.desktop.ocr import ocr_png
        return ocr_png(png_path)


def pick_element(found: list[dict], sel: dict) -> dict:
    """Exactly one element must match, or an index must say which. Guessing would click the wrong thing."""
    if not found:
        raise LookupError("no matching element (check name/control type with ui_find)")
    idx = sel.get("index")
    if idx is not None:
        if not 0 <= idx < len(found):
            raise LookupError(f"index {idx} is out of range: {len(found)} element(s) match")
        return found[idx]
    if len(found) > 1:
        listing = "; ".join(f"[{d['index']}] {d['control_type']} '{d['name']}'" for d in found[:10])
        raise LookupError(f"{len(found)} elements match; say which with index: {listing}")
    return found[0]


# =============================================================================== real Windows
class WindowsDesktop(DesktopBackend):
    name = "windows"

    def __init__(self):
        from jarvis.desktop import win32
        self.w = win32
        win32.set_dpi_aware()
        self._uia_els: dict = {}

    def windows(self): return self.w.top_windows()
    def foreground(self): return self.w.foreground()
    def focus(self, hwnd): return self.w.focus(hwnd)
    def process_names(self): return set(self.w.processes().values())
    def elevated(self): return self.w.is_elevated()

    def discover_apps(self, allow):
        return appdisc.discover(allow)

    def launch(self, app: dict, wait: float) -> dict:
        before = {w["hwnd"] for w in self.w.top_windows()}
        pid = None
        if app["kind"] == "exe":
            p = subprocess.Popen([app["path"]], close_fds=True)            # no shell, absolute path from the index
            pid = p.pid
        elif app["kind"] == "lnk":
            os.startfile(app["path"])                                       # ShellExecute of the Start-menu shortcut
        elif app["kind"] == "aumid":
            explorer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
            subprocess.Popen([str(explorer), "shell:AppsFolder\\" + app["aumid"]])
        elif app["kind"] == "uri":
            from jarvis.desktop import games
            if not games.allowed_uri(app.get("uri", "")):
                raise ValueError("refused: this is not a Steam or Epic Games launcher link")
            os.startfile(app["uri"])                                        # the launcher's own protocol link, handed to Windows
        elif app["kind"] in ("folder", "file", "settings"):
            from jarvis.desktop import places
            if app["kind"] == "settings":
                if not places.allowed_settings_uri(app.get("uri", "")):
                    raise ValueError("refused: only Windows Settings links are opened this way")
                os.startfile(app["uri"])
            elif app["kind"] == "folder":
                if not (Path(app.get("path", "")).is_dir() and places.is_inside_personal_roots(Path(app["path"]))):
                    raise ValueError("refused: only your own personal folders are opened this way")
                os.startfile(app["path"])
            else:
                if not places.file_allowed(Path(app.get("path", ""))):
                    raise ValueError("refused: only ordinary documents, pictures, music and videos from your personal folders are opened this way (never programs or scripts)")
                os.startfile(app["path"])
            return {"verified": False, "handed_off": True, "evidence": "handed to Windows to open with your usual program; Jarvis does not check that it appeared"}
        elif app["kind"] == "web":
            from jarvis.desktop import sites
            if not sites.is_https(app.get("url", "")):
                raise ValueError("refused: only https:// addresses are opened")
            os.startfile(app["url"])                                        # opens in the default browser
            return {"verified": False, "handed_off": True,
                    "evidence": "the address was handed to Windows to open in your default browser; Jarvis does not check that the page loaded"}
        else:
            raise ValueError("unknown app kind")
        target = (app.get("target") or "").lower()
        target_exe = target.rsplit("\\", 1)[-1] if target else None
        words = [w for w in app["name"].lower().split() if len(w) > 2]
        end = time.monotonic() + wait
        while time.monotonic() < end:
            time.sleep(0.25)
            for win in self.w.top_windows():
                if win["hwnd"] in before:
                    continue
                proc_path = (win.get("process_path") or "").lower()
                if (pid and win["pid"] == pid) or (target and proc_path == target) or \
                        (target_exe and win.get("process") == target_exe) or \
                        (words and all(w in win["title"].lower() for w in words[:2])):
                    return {"verified": True, "evidence": f"new window '{win['title']}' ({win.get('process')}, pid {win['pid']})",
                            "hwnd": win["hwnd"], "pid": win["pid"], "title": win["title"]}
            if pid and not self.w.pid_alive(pid):
                return {"verified": False, "evidence": f"process {pid} exited within {wait:.0f}s and no window appeared", "pid": pid}
        # Maybe it was already open (single-instance apps just activate the existing window).
        fg = self.w.foreground()
        if fg:
            info = self.w.window_info(fg)
            if (target_exe and info.get("process") == target_exe) or (words and all(w in info["title"].lower() for w in words[:2])):
                return {"verified": True, "evidence": f"existing window '{info['title']}' came to the front (app was already running)",
                        "hwnd": fg, "pid": info["pid"], "title": info["title"]}
        return {"verified": False, "evidence": f"no new window matching '{app['name']}' appeared within {wait:.0f}s", "pid": pid}

    def _find(self, hwnd, sel):
        from jarvis.desktop import uia
        return uia.find(hwnd, sel.get("name"), sel.get("control_type"), sel.get("automation_id"), bool(sel.get("exact")))

    def ui_find(self, hwnd, sel):
        return [d for _, d in self._find(hwnd, sel)]

    def _one(self, hwnd, sel):
        found = self._find(hwnd, sel)
        d = pick_element([d for _, d in found], sel)
        return found[d["index"]][0], d

    def ui_read(self, hwnd, sel):
        from jarvis.desktop import uia
        el, d = self._one(hwnd, sel)
        return {**d, **uia.read(el)}

    def ui_act(self, hwnd, sel, action, value):
        from jarvis.desktop import uia
        el, d = self._one(hwnd, sel)
        if not d["enabled"]:
            raise ValueError(f"'{d['name']}' is disabled")
        pattern = uia.act(el, action, value)
        time.sleep(0.15)
        try:
            after = uia.read(el)
        except Exception:  # noqa: BLE001 - element can disappear after being invoked (dialog closed)
            after = {"gone": True}
        return {"element": d, "pattern": pattern, "after": after}

    def focused_value(self):
        try:
            from jarvis.desktop import uia
            u, _ = uia._client()
            el = u.GetFocusedElement()
            if el.CurrentIsPassword:
                return None
            r = uia.read(el)
            return r.get("value")
        except Exception:  # noqa: BLE001
            return None

    def type_text(self, text): self.w.send_unicode(text)
    def press(self, combo):
        self.w.send_vk_sequence([(vk, False) for vk in combo] + [(vk, True) for vk in reversed(combo)])
    def click(self, x, y, button, double): self.w.click(x, y, button, double)
    def screenshot(self, hwnd): return self.w.capture(hwnd)


# =============================================================================== other OSes (tests, dev)
class PosixDesktop(DesktopBackend):
    name = sys.platform

    def __init__(self):
        self.children: list = []          # keep handles so started programs are not reaped as "zombies" unseen

    def process_names(self):
        out = set()
        proc = Path("/proc")
        if proc.exists():
            for p in proc.iterdir():
                if p.name.isdigit():
                    try:
                        out.add((p / "comm").read_text().strip().lower())
                    except OSError:
                        pass
        return out

    def launch(self, app: dict, wait: float) -> dict:
        if app["kind"] != "exe":
            raise Unsupported("only allow-listed programs can be started on this OS")
        p = subprocess.Popen([app["path"]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, start_new_session=True)
        self.children.append(p)
        time.sleep(min(wait, 1.0))
        code = p.poll()
        if code is None:
            return {"verified": True, "evidence": f"process {p.pid} is running after {min(wait, 1.0):.1f}s (window check not available on this OS)",
                    "pid": p.pid}
        return {"verified": False, "evidence": f"process {p.pid} exited immediately with code {code}", "pid": p.pid}


# =============================================================================== FAKE desktop (tests / demos only)
@dataclass
class SimElement:
    name: str
    control_type: str
    automation_id: str = ""
    value: str | None = None
    enabled: bool = True
    is_password: bool = False
    toggle: bool | None = None
    on_invoke: object = None          # callable(window) -> None


@dataclass
class SimWindow:
    hwnd: int
    title: str
    process: str
    pid: int
    elements: list = field(default_factory=list)
    rect: tuple = (100, 100, 600, 500)


class SimulatedDesktop(DesktopBackend):
    """FAKE. A tiny in-memory desktop with a Notepad-like and a Counter app. Clearly not a real PC."""
    name = "simulated"
    simulated = True

    def __init__(self, extra_processes: set[str] | None = None):
        self.wins: dict[int, SimWindow] = {}
        self._fg = 0
        self._next = 1000
        self.typed: list[str] = []
        self.clicks: list[tuple] = []
        self.extra_processes = set(extra_processes or ())
        self.catalog = {
            "sim:notepad": {"id": "sim:notepad", "name": "Sim Notepad", "kind": "sim", "target": None, "source": "simulator"},
            "sim:counter": {"id": "sim:counter", "name": "Sim Counter", "kind": "sim", "target": None, "source": "simulator"},
            "sim:broken": {"id": "sim:broken", "name": "Sim Broken App", "kind": "sim", "target": None, "source": "simulator"},
            "sim:setup": {"id": "sim:setup", "name": "Sim Setup Installer", "kind": "sim", "target": "c:\\sim\\setup.exe", "source": "simulator"},
        }
        self.focus_refused: set[int] = set()

    def _new_window(self, title, process, elements) -> SimWindow:
        self._next += 1
        w = SimWindow(self._next, title, process, self._next + 5000, elements)
        self.wins[w.hwnd] = w
        self._fg = w.hwnd
        return w

    def windows(self):
        return [{"hwnd": w.hwnd, "title": w.title, "class": "Sim", "pid": w.pid, "process": w.process, "process_path": None,
                 "visible": True, "minimized": False, "rect": list(w.rect)} for w in self.wins.values()]

    def foreground(self): return self._fg

    def focus(self, hwnd):
        if hwnd in self.wins and hwnd not in self.focus_refused:
            self._fg = hwnd
            return True
        return False

    def process_names(self): return {w.process for w in self.wins.values()} | self.extra_processes
    def discover_apps(self, allow): return list(self.catalog.values()) + appdisc.from_allowlist(allow)

    def launch(self, app, wait):
        if app["id"] == "sim:notepad":
            w = self._new_window("Untitled - Sim Notepad", "simnotepad.exe",
                                 [SimElement("Text editor", "edit", "15", value=""), SimElement("Save", "button", "save"),
                                  SimElement("Password", "edit", "pw", value="", is_password=True)])
            w.elements[1].on_invoke = lambda win: setattr(win, "title", "saved - Sim Notepad")
        elif app["id"] == "sim:counter":
            w = self._new_window("Sim Counter", "simcounter.exe",
                                 [SimElement("Count", "text", "count", value="0"), SimElement("Add one", "button", "add"),
                                  SimElement("Add", "menuitem", "addmenu"), SimElement("Dark mode", "checkbox", "dark", toggle=False),
                                  SimElement("Reset", "button", "reset", enabled=False)])

            def add(win):
                c = next(e for e in win.elements if e.automation_id == "count")
                c.value = str(int(c.value) + 1)
            w.elements[1].on_invoke = add
        elif app["id"] == "sim:broken":
            return {"verified": False, "evidence": "simulated app exited immediately with code 1", "pid": 4242}
        elif app["id"] == "sim:setup":
            w = self._new_window("Sim Setup", "setup.exe", [SimElement("Next", "button", "next")])
        else:
            raise Unsupported("the simulator only knows its own apps")
        return {"verified": True, "evidence": f"simulated window '{w.title}' appeared", "hwnd": w.hwnd, "pid": w.pid, "title": w.title}

    def _els(self, hwnd):
        if hwnd not in self.wins:
            raise LookupError("window no longer exists")
        return self.wins[hwnd].elements

    def _desc(self, i, e):
        return {"index": i, "name": e.name, "control_type": e.control_type, "automation_id": e.automation_id,
                "enabled": e.enabled, "offscreen": False, "is_password": e.is_password, "rect": [110, 110 + 30 * i, 300, 135 + 30 * i]}

    def ui_find(self, hwnd, sel):
        out = []
        for e in self._els(hwnd):
            d = self._desc(len(out), e)
            q = (sel.get("name") or "").lower()
            if sel.get("control_type") and e.control_type != sel["control_type"].lower():
                continue
            if sel.get("automation_id") and e.automation_id != sel["automation_id"]:
                continue
            if q and ((e.name.lower() != q) if sel.get("exact") else (q not in e.name.lower())):
                continue
            out.append(d)
        return out

    def _one(self, hwnd, sel):
        found = self.ui_find(hwnd, sel)
        d = pick_element(found, sel)
        e = next(e for e in self._els(hwnd) if e.name == d["name"] and e.automation_id == d["automation_id"])
        return e, d

    def _read(self, e):
        r = {"name": e.name}
        if e.is_password:
            r.update(value=None, note="password field: value not read")
        elif e.value is not None:
            r["value"] = e.value
        if e.toggle is not None:
            r["toggle_state"] = "on" if e.toggle else "off"
        return r

    def ui_read(self, hwnd, sel):
        e, d = self._one(hwnd, sel)
        return {**d, **self._read(e)}

    def ui_act(self, hwnd, sel, action, value):
        e, d = self._one(hwnd, sel)
        if not e.enabled:
            raise ValueError(f"'{e.name}' is disabled")
        if action == "invoke":
            if e.control_type not in ("button", "menuitem", "hyperlink", "splitbutton"):
                raise ValueError("this element cannot be invoked (no Invoke pattern)")
            if e.on_invoke:
                e.on_invoke(self.wins[hwnd])
            pattern = "Invoke"
        elif action == "toggle":
            if e.toggle is None:
                raise ValueError("this element has no on/off state (no Toggle pattern)")
            e.toggle = not e.toggle
            pattern = "Toggle"
        elif action == "set_value":
            if e.is_password:
                raise PermissionError("Jarvis does not type into password fields")
            if e.value is None or e.control_type != "edit":
                raise ValueError("this element does not accept a value (no writable Value pattern)")
            e.value = value or ""
            pattern = "Value"
        elif action == "focus":
            pattern = "SetFocus"
        else:
            raise ValueError(f"the simulator does not support {action}")
        return {"element": d, "pattern": pattern, "after": self._read(e)}

    def focused_value(self):
        w = self.wins.get(self._fg)
        if not w:
            return None
        ed = next((e for e in w.elements if e.control_type == "edit" and not e.is_password), None)
        return ed.value if ed else None

    def type_text(self, text):
        self.typed.append(text)
        w = self.wins.get(self._fg)
        ed = next((e for e in w.elements if e.control_type == "edit" and not e.is_password), None) if w else None
        if ed is not None:
            ed.value = (ed.value or "") + text

    def press(self, combo): self.typed.append(f"<keys {combo}>")
    def click(self, x, y, button, double): self.clicks.append((x, y, button, double))

    def screenshot(self, hwnd):
        from jarvis.desktop.png import encode_rgb
        w, h = 64, 48
        return w, h, encode_rgb(w, h, bytes([200, 200, 200]) * (w * h))

    def ocr(self, png_path):
        w = self.wins.get(self._fg)
        lines = [{"text": w.title, "rect": None}] + [{"text": f"{e.name} {e.value or ''}".strip(), "rect": None}
                                                      for e in w.elements if not e.is_password] if w else []
        return {"engine": "simulated OCR (fake)", "lines": lines}


def default_backend() -> DesktopBackend:
    return WindowsDesktop() if sys.platform == "win32" else PosixDesktop()
