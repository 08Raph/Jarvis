"""A FAKE Windows layer for the operator tests. Nothing real is touched: windows, keys, power and PowerShell answers are scripted and recorded."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.operator.core import Operator  # noqa: E402
from jarvis.operator.ops import Ops  # noqa: E402


class FakeOps(Ops):
    is_windows = True

    def __init__(self):
        self.win = [
            {"hwnd": 1, "title": "Untitled - Notepad", "process": "notepad.exe", "minimized": False, "rect": [100, 100, 900, 700], "pid": 11},
            {"hwnd": 2, "title": "General - Discord", "process": "discord.exe", "minimized": False, "rect": [200, 150, 1000, 750], "pid": 12},
            {"hwnd": 3, "title": "Windows Security", "process": "securityhealthsystray.exe", "minimized": False, "rect": [0, 0, 500, 400], "pid": 13},
        ]
        self.fg = 1
        self.key_log = []
        self.power_log = []
        self.opened = []
        self.locked = 0
        self.placed = []
        self.runs = []
        self.run_answers = {}          # argv[0] (lowercase) -> (code, out, err) or callable(argv)
        self.ps_answers = []           # list of (substring, text)
        self.wallpaper = None
        self.hung = set()

    def windows(self):
        return [dict(w) for w in self.win]

    def foreground(self):
        return dict(next(w for w in self.win if w["hwnd"] == self.fg))

    def window_action(self, hwnd, action):
        w = next((x for x in self.win if x["hwnd"] == hwnd), None)
        if w is None:
            return
        if action == "minimize":
            w["minimized"] = True
        elif action == "restore":
            w["minimized"] = False
        elif action == "close":
            self.win.remove(w)

    def window_place(self, hwnd, x, y, w, h):
        self.placed.append((hwnd, x, y, w, h))
        for win in self.win:
            if win["hwnd"] == hwnd:
                win["rect"] = [x, y, x + w, y + h]

    def monitors(self):
        return [{"number": 1, "left": 0, "top": 0, "right": 1920, "bottom": 1080}, {"number": 2, "left": 1920, "top": 0, "right": 3840, "bottom": 1080}]

    def is_hung(self, hwnd):
        return hwnd in self.hung

    def keys(self, combo):
        self.key_log.append(list(combo))

    def power(self, action):
        self.power_log.append(action)

    def lock(self):
        self.locked += 1

    def set_wallpaper(self, path):
        self.wallpaper = path
        return True

    def open_target(self, target):
        self.opened.append(target)

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG" + b"0" * 200)

    def idle_seconds(self):
        return 42.0

    def run(self, argv, timeout=20.0):
        self.runs.append(list(argv))
        a = self.run_answers.get(Path(argv[0]).name.lower())
        if callable(a):
            return a(argv)
        if a is not None:
            return a
        return (0, "", "")

    def run_in(self, cwd, argv, timeout=60.0):
        self.runs.append(["(in " + str(cwd) + ")"] + list(argv))
        a = self.run_answers.get("run_in")
        return a(argv) if callable(a) else (a or (0, "", ""))

    def powershell(self, script, timeout=25.0):
        self.runs.append(["powershell", script])
        for sub, text in self.ps_answers:
            if sub in script:
                return text
        return ""


class OpCase(unittest.TestCase):
    """A temp 'home' with Downloads/Documents/Desktop, an Operator that is ON, confirmations answered by self.answer."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        for d in ("Downloads", "Documents", "Desktop", "Pictures"):
            (self.home / d).mkdir(parents=True)
        self.profile = Path(self._tmp.name) / "profile"
        self.answer = True
        self.asked = []
        self.fake = FakeOps()
        self.op = Operator(self.profile, confirm=self._confirm, ops=self.fake, home=self.home)
        self.op.turn_on()
        for cat in ("apps", "files_write", "archives", "backup", "settings", "power", "documents", "dev_write", "gaming", "workflows", "services"):
            self.op.set_category(cat, True)

    def _confirm(self, req):
        self.asked.append((req.tool, req.risk, req.description))
        return self.answer

    def write(self, rel, text="hello", where="Downloads"):
        p = self.home / where / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    @property
    def dl(self):
        return self.home / "Downloads"
