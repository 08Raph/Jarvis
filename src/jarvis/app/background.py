"""What happens when the Jarvis window is closed or minimized, and the optional wallpaper window. Two small choices, stored on this PC only
(<profile>/background.json):

* on_close  "background" (default): closing the window hides it to the tray icon; Jarvis keeps running and, if hands-free listening is on, keeps listening.
            "quit": closing the window ends Jarvis completely (the microphone is closed, nothing keeps running).
* wallpaper False/True: show the animated Jarvis display on the desktop behind your icons. It reads the same state as the window.

This module holds only the choices; the window and tray are in ui/. It touches no desktop permission and opens no microphone."""
from __future__ import annotations

import json
from pathlib import Path

ON_CLOSE = ("background", "quit")
DEFAULTS = {"on_close": "background", "wallpaper": False}


class BackgroundOptions:
    def __init__(self, folder: Path, guest: bool = False):
        self.path = Path(folder) / "background.json"
        self.guest = guest

    def get(self) -> dict:
        out = dict(DEFAULTS)
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                if d.get("on_close") in ON_CLOSE:
                    out["on_close"] = d["on_close"]
                out["wallpaper"] = bool(d.get("wallpaper", False))
        except (OSError, ValueError):
            pass
        if self.guest:
            out["on_close"] = "quit"          # a guest session never keeps running unseen
            out["wallpaper"] = False
        return out

    def set(self, **kw) -> dict:
        cur = self.get()
        if kw.get("on_close") in ON_CLOSE:
            cur["on_close"] = kw["on_close"]
        if "wallpaper" in kw:
            cur["wallpaper"] = bool(kw["wallpaper"])
        if self.guest:
            return cur
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(cur), encoding="utf-8")
        except OSError:
            pass
        return cur

    def keep_running_on_close(self) -> bool:
        return self.get()["on_close"] == "background"
