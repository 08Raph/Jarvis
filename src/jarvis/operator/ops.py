"""The thin layer that touches Windows. Capability modules never call Windows or start programs themselves; they call these methods, so tests can replace
them with a fake and every external program is started from a fixed argument list (never through a shell, never with text from a webpage or file)."""
from __future__ import annotations

import subprocess
import sys

from jarvis.operator.core import Unavailable

VK = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2, "vol_up": 0xAF, "vol_down": 0xAE, "mute": 0xAD,
      "win": 0x5B, "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "tab": 0x09, "d": 0x44, "s": 0x53, "left": 0x25, "right": 0x27, "f4": 0x73, "r": 0x52, "g": 0x47}
SW = {"minimize": 6, "maximize": 3, "restore": 9}
POWER_COMMANDS = {
    "restart": ["shutdown.exe", "/r", "/t", "30"],
    "shutdown": ["shutdown.exe", "/s", "/t", "30"],
    "signout": ["shutdown.exe", "/l"],
    "hibernate": ["shutdown.exe", "/h"],
    "cancel": ["shutdown.exe", "/a"],
    "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
}


class Ops:
    is_windows = False

    def run(self, argv: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
        """Run a program from a FIXED argument list (no shell). Returns (exit code, stdout, stderr)."""
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise Unavailable(f"{argv[0]} is not installed or not on this PC.") from None
        except subprocess.TimeoutExpired:
            raise Unavailable(f"{argv[0]} took longer than {int(timeout)} seconds, so I stopped waiting.") from None
        return p.returncode, p.stdout or "", p.stderr or ""

    def run_in(self, cwd, argv: list[str], timeout: float = 60.0) -> tuple[int, str, str]:
        """Like run() but inside a folder (used for project tests)."""
        try:
            p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, shell=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise Unavailable(f"{argv[0]} is not installed or not on this PC.") from None
        except subprocess.TimeoutExpired:
            raise Unavailable(f"{argv[0]} took longer than {int(timeout)} seconds, so I stopped waiting.") from None
        return p.returncode, p.stdout or "", p.stderr or ""

    def powershell(self, script: str, timeout: float = 25.0) -> str:
        """`script` must be a literal written in this package (plus validated numbers/names), never user text."""
        if not self.is_windows:
            raise Unavailable("That needs Windows PowerShell, which this system doesn't have.")
        code, out, err = self.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], timeout)
        if code != 0:
            raise Unavailable("Windows could not answer that (" + (err.strip().splitlines() or ["no detail"])[0][:140] + ").")
        return out

    # the Windows-only actions: the base class says plainly that they are unavailable
    def _no(self, what: str):
        raise Unavailable(f"{what} only works on Windows.")

    def windows(self) -> list[dict]:
        self._no("Listing windows")

    def foreground(self) -> dict | None:
        self._no("Reading the active window")

    def window_action(self, hwnd: int, action: str) -> None:
        self._no("Changing windows")

    def window_place(self, hwnd: int, x: int, y: int, w: int, h: int) -> None:
        self._no("Moving windows")

    def monitors(self) -> list[dict]:
        self._no("Reading screens")

    def is_hung(self, hwnd: int) -> bool:
        self._no("Checking for frozen windows")

    def keys(self, combo: list[str]) -> None:
        self._no("Pressing keys")

    def power(self, action: str) -> None:
        self._no("Power actions")

    def lock(self) -> None:
        self._no("Locking Windows")

    def set_wallpaper(self, path: str) -> bool:
        self._no("Changing the wallpaper")

    def open_target(self, target: str) -> None:
        self._no("Opening that")

    def screenshot(self, path: str) -> None:
        self._no("Screenshots")

    def idle_seconds(self) -> float | None:
        return None


class WindowsOps(Ops):
    is_windows = True

    def __init__(self):
        from jarvis.desktop import win32
        self.w = win32

    def windows(self):
        return self.w.top_windows()

    def foreground(self):
        h = self.w.foreground()
        return self.w.window_info(h) if h else None

    def window_action(self, hwnd, action):
        u = self.w.user32
        if action in SW:
            u.ShowWindow(hwnd, SW[action])
        elif action == "close":
            u.PostMessageW(hwnd, 0x0010, 0, 0)          # WM_CLOSE: the program may ask to save, exactly like clicking the X
        elif action == "front":
            self.w.focus(hwnd)
        else:
            raise ValueError(action)

    def window_place(self, hwnd, x, y, w, h):
        u = self.w.user32
        u.ShowWindow(hwnd, 9)
        if not u.SetWindowPos(hwnd, None, int(x), int(y), int(w), int(h), 0x0004 | 0x0010):
            raise OSError("Windows refused to move the window (it may belong to a program running as administrator).")

    def monitors(self):
        return self.w.monitors()

    def is_hung(self, hwnd):
        return bool(self.w.user32.IsHungAppWindow(hwnd))

    def keys(self, combo):
        down = [(VK[k], False) for k in combo]
        up = [(VK[k], True) for k in reversed(combo)]
        self.w.send_vk_sequence(down + up)

    def power(self, action):
        if action not in POWER_COMMANDS:
            raise ValueError(action)
        code, _o, err = self.run(POWER_COMMANDS[action], 15)
        if code != 0:
            raise OSError((err.strip() or "Windows refused")[:140])

    def lock(self):
        if not self.w.user32.LockWorkStation():
            raise OSError("Windows refused to lock.")

    def set_wallpaper(self, path):
        import ctypes
        return bool(ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 3))

    def open_target(self, target):
        import os
        os.startfile(target)  # noqa: S606 - only fixed ms-settings/mailto/https targets and checked paths are ever passed here

    def screenshot(self, path):
        w, h, bgra = self.w.capture(None)
        from pathlib import Path
        Path(path).write_bytes(self.w.bgra_to_png(w, h, bgra))

    def idle_seconds(self):
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        li = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return None
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0)


def default_ops() -> Ops:
    if sys.platform == "win32":
        try:
            return WindowsOps()
        except Exception:  # noqa: BLE001
            return Ops()
    return Ops()
