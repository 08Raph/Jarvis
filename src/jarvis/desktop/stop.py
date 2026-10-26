"""Emergency stop. Three ways to trigger it, all checked before every step and while waiting:
  * Ctrl+C in the console, or the `stop()` call (GUI/voice "stop");
  * a STOP file in the profile folder (`jarvis desktop stop` from a second window creates it);
  * on Windows, the global hotkey Ctrl+Alt+Pause (registered with RegisterHotKey; untested on real Windows).
A running tool worker process is killed on stop."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path


class Stopped(Exception):
    pass


class EmergencyStop:
    HOTKEY = "Ctrl+Alt+Pause"

    def __init__(self, stop_file: Path | None = None):
        self._ev = threading.Event()
        self.stop_file = Path(stop_file) if stop_file else None
        self.reason = ""
        self._hotkey_thread = None

    def stop(self, reason: str = "stop requested") -> None:
        self.reason = reason
        self._ev.set()

    def is_set(self) -> bool:
        if not self._ev.is_set() and self.stop_file is not None and self.stop_file.exists():
            self.stop("emergency stop file present")
        return self._ev.is_set()

    def check(self) -> None:
        if self.is_set():
            raise Stopped(self.reason or "stopped")

    def wait(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        self.check()

    def clear(self) -> None:
        self._ev.clear()
        self.reason = ""
        if self.stop_file is not None:
            self.stop_file.unlink(missing_ok=True)

    def start_hotkey(self) -> bool:
        """Windows only. Returns False when the hotkey could not be registered (another app owns it, or not Windows)."""
        if sys.platform != "win32" or self._hotkey_thread is not None:
            return False
        ok = threading.Event()
        result = {"ok": False}

        def loop():
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_PAUSE, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x13, 0x0312
            result["ok"] = bool(user32.RegisterHotKey(None, 0xA11, MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, VK_PAUSE))
            ok.set()
            if not result["ok"]:
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.stop(f"emergency stop hotkey ({self.HOTKEY})")
        self._hotkey_thread = threading.Thread(target=loop, name="jarvis-stop-hotkey", daemon=True)
        self._hotkey_thread.start()
        ok.wait(2)
        return result["ok"]
