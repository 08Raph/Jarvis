"""Quick launch ("open Fortnite", "open Instagram", "scan my PC"), the game/shortcut scan and the website table. FAKES: a fake desktop backend records what would be
started, and fake launcher files follow the layouts Epic and Steam document. Nothing is started and the network is not used; real Windows behaviour is NOT tested here."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase  # noqa: E402
from jarvis.app import quick_launch as QL  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.desktop import games, sites  # noqa: E402


class FakeBackend:
    name = "fake"
    simulated = False

    def __init__(self, apps=None, verified=True):
        self.apps, self.verified, self.launched = list(apps or []), verified, []

    def discover_apps(self, allow):
        return list(self.apps)

    def launch(self, app, wait):
        self.launched.append(app["id"])
        if app["kind"] == "web":
            return {"verified": False, "handed_off": True, "evidence": "handed over"}
        return {"verified": self.verified, "evidence": "fake window"}


APPS = [
    {"id": "epic:fortnite", "name": "Fortnite", "kind": "uri", "uri": games.epic_uri("Fortnite", "fn", "4fe7"), "source": "Epic Games Launcher", "target": None},
    {"id": "steam:730", "name": "Counter-Strike 2", "kind": "uri", "uri": "steam://rungameid/730", "source": "Steam", "target": None},
    {"id": "exe:notepad", "name": "notepad", "kind": "exe", "path": "C:/Windows/System32/notepad.exe", "target": "C:/Windows/System32/notepad.exe", "source": "your app allow-list (config)"},
    {"id": "lnk:google chrome.lnk", "name": "Google Chrome", "kind": "lnk", "path": "C:/x/Google Chrome.lnk", "target": "C:/chrome.exe", "source": "Start menu shortcut"},
    {"id": "lnk:discord.lnk", "name": "Discord", "kind": "lnk", "path": "C:/x/Discord.lnk", "target": "C:/discord.exe", "source": "Start menu shortcut"},
    {"id": "lnk:game setup.lnk", "name": "Some Game Setup", "kind": "lnk", "path": "C:/x/setup.lnk", "target": "C:/setup.exe", "source": "Start menu shortcut"},
]


class Parse(unittest.TestCase):
    def test_open_style_sentences(self):
        for text, target in (("open fortnite", "fortnite"), ("Hey Jarvis, open Instagram please", "Instagram"), ("can you launch the calculator", "calculator"),
                             ("go to youtube.com", "youtube.com"), ("OPEN my notepad!", "notepad"), ("could you open up discord for me", "discord")):
            c = QL.parse(text)
            self.assertIsNotNone(c, text)
            self.assertEqual((c.kind, c.target.lower()), ("open", target.lower()), text)
            self.assertTrue(c.strong)

    def test_weak_verbs_are_marked(self):
        c = QL.parse("play fortnite")
        self.assertEqual((c.kind, c.strong), ("open", False))

    def test_other_sentences_are_left_to_the_model(self):
        for text in ("how do I open a file in python", "what is the capital of France", "open the file report.docx", "start by explaining recursion in detail please now",
                     "open C:\\Windows\\System32\\cmd.exe", "open powershell", "tell me a joke", "", "run that script", "open it"):
            self.assertIsNone(QL.parse(text), text)

    def test_scan_list_and_search(self):
        self.assertEqual(QL.parse("scan my pc").kind, "scan")
        self.assertEqual(QL.parse("rescan my games").kind, "scan")
        self.assertEqual(QL.parse("what apps can you open").kind, "list")
        s = QL.parse("search youtube for lofi beats")
        self.assertEqual((s.kind, s.target, s.engine), ("search", "lofi beats", "youtube"))
        self.assertEqual(QL.parse("google best pizza near me").engine, "google")


class Resolve(unittest.TestCase):
    def test_installed_game_and_exact_names(self):
        self.assertEqual(QL.resolve(APPS, "fortnite")[1]["id"], "epic:fortnite")
        self.assertEqual(QL.resolve(APPS, "counter-strike 2")[1]["id"], "steam:730")
        self.assertEqual(QL.resolve(APPS, "calc")[0], "none")          # alias maps to 'calculator', which is not in this fake list
        self.assertEqual(QL.resolve(APPS, "chrome")[1]["id"], "lnk:google chrome.lnk")

    def test_websites(self):
        k, w = QL.resolve(APPS, "instagram")
        self.assertEqual((k, w["url"]), ("web", "https://www.instagram.com/"))
        k, w = QL.resolve(APPS, "example.com")
        self.assertEqual((k, w["url"]), ("web", "https://example.com"))

    def test_an_installed_app_named_exactly_beats_the_website(self):
        k, a = QL.resolve(APPS, "discord")
        self.assertEqual((k, a["kind"]), ("app", "lnk"))

    def test_unknown_and_ambiguous(self):
        self.assertEqual(QL.resolve(APPS, "zzzz unknown thing")[0], "none")
        many = [{"id": f"x{i}", "name": f"Great Game {i}", "kind": "lnk", "path": "p"} for i in range(5)]
        self.assertEqual(QL.resolve(many, "great game")[0], "many")


class Sites(unittest.TestCase):
    def test_only_https_addresses(self):
        self.assertIsNone(sites.https_url_for("javascript:alert(1)"))
        self.assertIsNone(sites.https_url_for("file:///c:/windows"))
        self.assertIsNone(sites.https_url_for("evil.exe"))
        self.assertTrue(sites.https_url_for("http://example.com")[1].startswith("https://"))
        self.assertTrue(all(u.startswith("https://") for u in sites.SITES.values()))
        self.assertFalse(sites.is_https("http://x.com"))
        self.assertFalse(sites.is_https("https://x.com/a b"))

    def test_search_urls_are_encoded(self):
        self.assertEqual(sites.search_url("a b&c"), "https://www.google.com/search?q=a+b%26c")
        self.assertIsNone(sites.search_url("x" * 300))


class GamesScan(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="jarvis-p81-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)

    def test_epic_manifests(self):
        m = self.d / "Manifests"
        m.mkdir()
        (m / "a.item").write_text(json.dumps({"DisplayName": "Fortnite", "AppName": "Fortnite", "CatalogNamespace": "fn", "CatalogItemId": "4fe75bbc",
                                              "InstallLocation": "C:/Epic/Fortnite"}), encoding="utf-8")
        (m / "b.item").write_text(json.dumps({"DisplayName": "Broken", "AppName": "B", "bIsIncompleteInstall": True}), encoding="utf-8")
        (m / "c.item").write_text("{not json", encoding="utf-8")
        (m / "d.item").write_text(json.dumps({"DisplayName": "Some DLC", "AppName": "D", "bIsApplication": False}), encoding="utf-8")
        out = games.from_epic([m])
        self.assertEqual([a["name"] for a in out], ["Fortnite"])
        self.assertEqual(out[0]["uri"], "com.epicgames.launcher://apps/fn%3A4fe75bbc%3AFortnite?action=launch&silent=true")
        self.assertTrue(games.allowed_uri(out[0]["uri"]))

    def test_steam_libraries_and_appmanifests(self):
        root = self.d / "Steam"
        (root / "steamapps").mkdir(parents=True)
        lib2 = self.d / "SteamLibrary"
        (lib2 / "steamapps").mkdir(parents=True)
        (root / "steamapps" / "libraryfolders.vdf").write_text('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"' + str(lib2).replace("\\", "\\\\") + '"\n\t}\n}\n', encoding="utf-8")
        (root / "steamapps" / "appmanifest_730.acf").write_text('"AppState"\n{\n\t"appid"\t\t"730"\n\t"name"\t\t"Counter-Strike 2"\n}\n', encoding="utf-8")
        (lib2 / "steamapps" / "appmanifest_228980.acf").write_text('"AppState"\n{\n\t"appid"\t\t"228980"\n\t"name"\t\t"Steamworks Common Redistributables"\n}\n', encoding="utf-8")
        (lib2 / "steamapps" / "appmanifest_570.acf").write_text('"AppState"\n{\n\t"appid"\t\t"570"\n\t"name"\t\t"Dota 2"\n}\n', encoding="utf-8")
        out = games.from_steam([root])
        self.assertEqual(sorted(a["name"] for a in out), ["Counter-Strike 2", "Dota 2"])
        self.assertEqual({a["uri"] for a in out}, {"steam://rungameid/730", "steam://rungameid/570"})

    def test_url_shortcuts_only_accept_launcher_links(self):
        sm = self.d / "sm"
        sm.mkdir()
        (sm / "Good Game.url").write_text("[InternetShortcut]\nURL=steam://rungameid/1\n", encoding="utf-8")
        (sm / "Evil.url").write_text("[InternetShortcut]\nURL=file:///C:/Windows/System32/calc.exe\n", encoding="utf-8")
        (sm / "Web.url").write_text("[InternetShortcut]\nURL=https://example.com\n", encoding="utf-8")
        out = games.from_shortcut_files([sm], [])
        self.assertEqual([a["name"] for a in out], ["Good Game"])

    def test_allowed_uri_rejects_everything_else(self):
        for bad in ("https://x.com", "file:///c:/x", "steam://rungameid/1 && calc", "steam://a\nb", "cmd:/c calc", "", None):
            self.assertFalse(games.allowed_uri(bad), bad)


class Handle(AppCase):
    def setUp(self):
        super().setUp()
        self.be = FakeBackend(APPS)
        self.asked = []

    def say(self, text, yes=True, guest=False):
        def confirm(req):
            self.asked.append(req.description)
            return yes
        return QL.handle(text, self.cfg, confirm=confirm, guest=guest, backend=self.be)

    def turn_on(self, ask=True):
        QL.set_state(self.cfg, enabled=True, confirm_each=ask)

    def test_off_by_default_explains_and_starts_nothing(self):
        self.assertIn("switch is off", self.say("open fortnite"))
        self.assertEqual(self.be.launched, [])
        self.assertIsNone(self.say("play something nice"))           # a weak sentence goes to the model
        self.assertIsNone(self.say("what is two plus two"))

    def test_open_a_game_asks_first_and_then_starts_it(self):
        self.turn_on()
        r = self.say("open fortnite")
        self.assertEqual(r, "Opened Fortnite.")
        self.assertEqual(self.be.launched, ["epic:fortnite"])
        self.assertIn("com.epicgames.launcher://", self.asked[0])

    def test_declining_starts_nothing(self):
        self.turn_on()
        self.assertIn("did not open", self.say("open fortnite", yes=False))
        self.assertEqual(self.be.launched, [])

    def test_a_broken_confirm_means_no(self):
        self.turn_on()
        r = QL.handle("open fortnite", self.cfg, confirm=lambda req: 1 / 0, backend=self.be)
        self.assertIn("did not open", r)
        self.assertEqual(self.be.launched, [])

    def test_no_question_when_the_person_turned_asking_off(self):
        self.turn_on(ask=False)
        self.assertEqual(self.say("open instagram"), "I asked Windows to open instagram in your browser. I can't check that the page loaded.")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.be.launched, ["web:instagram"])

    def test_unverified_start_is_reported_honestly(self):
        self.turn_on(ask=False)
        self.be.verified = False
        self.assertIn("haven't seen its window", self.say("launch notepad"))

    def test_installers_are_never_started(self):
        self.turn_on(ask=False)
        self.assertIn("installer", self.say("open game setup"))
        self.assertEqual(self.be.launched, [])

    def test_unknown_things_and_ambiguity(self):
        self.turn_on()
        self.assertIn("couldn't find", self.say("open zzzz thing"))
        self.assertIsNone(self.say("play zzzz thing"))
        self.be.apps = [{"id": f"x{i}", "name": f"Great Game {i}", "kind": "lnk", "path": "p"} for i in range(5)]
        self.assertIn("several matches", self.say("open great game", ))

    def test_scan_and_list_report_what_was_found(self):
        self.turn_on()
        r = self.say("scan my pc")
        self.assertIn("6 apps and games", r)
        self.assertIn("Fortnite", r)
        self.assertTrue((self.cfg.profile_dir / QL.INDEX_FILE).exists())
        self.assertIn("Fortnite", self.say("what apps can you open"))

    def test_search_opens_an_https_search(self):
        self.turn_on(ask=False)
        self.assertIn("browser", self.say("search youtube for lofi beats"))
        self.assertEqual(self.be.launched, ["web:search"])

    def test_the_emergency_stop_blocks_it(self):
        self.turn_on(ask=False)
        (self.cfg.profile_dir / "STOP").write_text("x", encoding="utf-8")
        self.assertIn("emergency stop", self.say("open fortnite"))
        self.assertEqual(self.be.launched, [])

    def test_guest_mode_never_launches(self):
        QL.set_state(self.cfg, enabled=True, confirm_each=False)
        self.assertIsNone(self.say("open fortnite", guest=True))
        self.assertEqual(self.be.launched, [])

    def test_every_attempt_is_in_the_action_log(self):
        self.turn_on()
        self.say("open fortnite")
        self.say("open instagram", yes=False)
        lines = [json.loads(x) for x in (self.cfg.profile_dir / "desktop_audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([x["status"] for x in lines], ["verified", "declined"])
        self.assertTrue(all(x["tool"] == "quick_launch" for x in lines))

    def test_it_does_not_touch_the_desktop_permissions_file(self):
        self.turn_on()
        self.say("open fortnite")
        self.assertFalse((self.cfg.profile_dir / "desktop_permissions.json").exists())

    def test_settings_default_and_round_trip(self):
        self.assertEqual(QL.get_state(self.cfg), {"enabled": False, "confirm_each": True})
        QL.set_state(self.cfg, enabled=True)
        self.assertEqual(QL.get_state(self.cfg), {"enabled": True, "confirm_each": True})
        (self.cfg.profile_dir / QL.STATE_FILE).write_text("{broken", encoding="utf-8")
        self.assertEqual(QL.get_state(self.cfg), {"enabled": False, "confirm_each": True})
        self.assertEqual(QL.get_state(self.cfg, guest=True), {"enabled": False, "confirm_each": True})


class ControllerWiring(AppCase):
    def ctl(self, ask):
        c = AppController(self.cfg, simulate=True, dispatch=lambda fn: fn(), ask_user=ask)
        c.ql_backend = FakeBackend(APPS)
        self.addCleanup(c.shutdown, 2.0)
        return c

    def wait(self, c):
        end = time.time() + 5
        while c.busy and time.time() < end:
            time.sleep(0.02)
        self.assertFalse(c.busy)

    def test_typed_command_is_confirmed_launched_and_answered_without_a_model(self):
        c = self.ctl(lambda req, resolve: resolve(True))
        c.quick_launch_set(on=True)
        self.assertTrue(c.send("open fortnite"))
        self.wait(c)
        self.assertEqual(c.ql_backend.launched, ["epic:fortnite"])
        texts = [(e.kind, e.text) for e in c.transcript.entries]
        self.assertIn(("assistant", "Opened Fortnite."), texts)
        self.assertIsNone(c.assistant)                                 # no model was loaded for this

    def test_declined_in_the_window_means_nothing_starts(self):
        c = self.ctl(lambda req, resolve: resolve(False))
        c.quick_launch_set(on=True)
        c.send("open fortnite")
        self.wait(c)
        self.assertEqual(c.ql_backend.launched, [])

    def test_voice_path_uses_it_too(self):
        c = self.ctl(lambda req, resolve: resolve(True))
        c.quick_launch_set(on=True, ask_each=False)
        reply, _rate = c._voice_respond("open instagram", threading.Event())
        self.assertIn("browser", reply)
        self.assertEqual(c.ql_backend.launched, ["web:instagram"])

    def test_the_controller_cannot_grant_desktop_permissions(self):
        c = self.ctl(None)
        for name in dir(c):
            self.assertFalse(name.startswith(("enable", "disable", "grant", "allow", "set_permission", "consent")), name)


if __name__ == "__main__":
    unittest.main()
