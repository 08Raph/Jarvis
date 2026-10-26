"""Optional start-with-Windows (Prompt 17). SEPARATE from hands-free voice: this only launches the Jarvis window when you sign in to Windows.

Rules: OFF by default; never changed silently; turning it on needs an explicit confirmation and writes ONE visible value
(HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, name JarvisAssistant) that you can also see and remove in Windows Settings > Apps > Startup.
No scheduled task, no service, no administrator rights, no other registry key. Turning it off deletes exactly that value. On other systems it reports
'not supported' and does nothing. Even when it starts the window, the microphone stays off unless you separately enabled hands-free voice."""
from __future__ import annotations

import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "JarvisAssistant"


class RegistryBackend:
    """HKEY_CURRENT_USER Run key through winreg (Windows only)."""
    def _winreg(self):
        import winreg  # noqa: PLC0415 - exists on Windows only
        return winreg

    def get(self, name: str):
        w = self._winreg()
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_READ) as k:
                return w.QueryValueEx(k, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        w = self._winreg()
        with w.CreateKey(w.HKEY_CURRENT_USER, RUN_KEY) as k:
            w.SetValueEx(k, name, 0, w.REG_SZ, value)

    def delete(self, name: str) -> bool:
        w = self._winreg()
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_SET_VALUE) as k:
                w.DeleteValue(k, name)
            return True
        except FileNotFoundError:
            return False


def default_command() -> str:
    """The exact command Windows would run at sign-in: the installed windowed program if frozen, otherwise this Python running the window."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" -m jarvis gui'


class Startup:
    def __init__(self, backend=None, platform: str | None = None, command: str | None = None):
        self.backend = backend
        self.platform = platform or sys.platform
        self.command = command or default_command()

    def supported(self) -> bool:
        return self.backend is not None or self.platform == "win32"

    def _b(self):
        return self.backend if self.backend is not None else RegistryBackend()

    def status(self) -> dict:
        if not self.supported():
            return {"supported": False, "enabled": False, "command": "", "note": "Start with Windows is only available on Windows."}
        try:
            cur = self._b().get(VALUE_NAME)
        except Exception as e:  # noqa: BLE001
            return {"supported": True, "enabled": False, "command": "", "note": f"Could not read the Windows startup setting: {type(e).__name__}."}
        return {"supported": True, "enabled": cur is not None, "command": cur or self.command,
                "note": "Starts only the Jarvis window at Windows sign-in. The microphone stays off unless you enabled hands-free voice separately."}

    def enable(self, confirm: bool = False) -> dict:
        if not confirm:
            raise PermissionError("Starting with Windows needs your explicit confirmation. Nothing was changed.")
        if not self.supported():
            raise RuntimeError("Start with Windows is only available on Windows.")
        self._b().set(VALUE_NAME, self.command)
        return self.status()

    def disable(self) -> dict:
        if self.supported():
            self._b().delete(VALUE_NAME)
        return self.status()
