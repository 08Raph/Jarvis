"""Quick launch: acronyms (cs2), well-known Steam games not found by the scan, and a stale Roblox shortcut falling back to the program's own link / website. FAKE backend."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.app import quick_launch as QL  # noqa: E402
from test_p81_quick_launch import APPS, FakeBackend  # noqa: E402


class Cfg:
    def __init__(self, d):
        self.profile_dir, self.data_dir, self.app_allowlist = Path(d), Path(d), {}


class Backend(FakeBackend):
    def __init__(self, apps, fail_kinds=()):
        super().__init__(apps)
        self.fail_kinds, self.launched_apps = set(fail_kinds), []

    def launch(self, app, wait):
        self.launched_apps.append(app)
        if app["kind"] in self.fail_kinds:
            raise OSError("[WinError 2] The system cannot find the file specified")
        return super().launch(app, wait)


class Launch(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.cfg = Cfg(self.t.name)

    def run_cmd(self, text, backend):
        return QL.handle(text, self.cfg, confirm=None, backend=backend)

    def test_acronym_finds_the_installed_game(self):
        b = Backend(APPS)
        out = self.run_cmd("open CS2", b)
        self.assertIn("Counter-Strike 2", out)
        self.assertEqual(b.launched_apps[-1]["uri"], "steam://rungameid/730")

    def test_a_known_steam_game_that_the_scan_missed_is_started_through_steam(self):
        b = Backend([a for a in APPS if a["id"] != "steam:730"])
        out = self.run_cmd("open CS2", b)
        self.assertNotIn("couldn't find", out)
        self.assertEqual(b.launched_apps[-1]["uri"], "steam://rungameid/730")

    def test_unknown_names_are_still_not_guessed(self):
        out = self.run_cmd("open qwertyzzgame", Backend(APPS))
        self.assertIn("couldn't find", out)

    def test_a_stale_roblox_shortcut_falls_back_to_its_own_link_then_the_website(self):
        apps = APPS + [{"id": "lnk:roblox player.lnk", "name": "Roblox Player", "kind": "lnk", "path": "C:/x/Roblox Player.lnk", "target": "C:/old/RobloxPlayerLauncher.exe", "source": "Start menu shortcut"}]
        b = Backend(apps, fail_kinds={"lnk"})
        out = self.run_cmd("open roblox", b)
        self.assertNotIn("would not start", out)
        self.assertEqual(b.launched_apps[-1]["uri"], "roblox://")
        b = Backend(apps, fail_kinds={"lnk", "uri"})
        out = self.run_cmd("open roblox", b)
        self.assertIn("website", out)
        self.assertEqual(b.launched_apps[-1]["url"], "https://www.roblox.com/home")

    def test_failure_text_says_why(self):
        b = Backend(APPS, fail_kinds={"lnk"})
        out = self.run_cmd("open discord", b)
        self.assertIn("WinError 2", out) if "would not start" in out else self.assertIn("website", out)

    def test_acronym_helper(self):
        self.assertEqual(QL._acronym("Counter-Strike 2"), "cs2")
        self.assertEqual(QL._acronym("League of Legends"), "lol")


if __name__ == "__main__":
    unittest.main()
