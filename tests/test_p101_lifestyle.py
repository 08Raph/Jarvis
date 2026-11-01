import sys, tempfile, threading, unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.app.hostctl import Host, HostUnavailable
from jarvis.app.lifestyle import parse_moment, next_repeat
from jarvis.app.toolbox import Toolbox, needs_host


class FakeHost:
    def __init__(self):
        self.m, self.v, self.clip = False, 40, "my street 1"

    def get_mute(self): return self.m
    def set_mute(self, o): self.m = o; return o
    def get_volume(self): return self.v
    def set_volume(self, p): self.v = p; return p
    def clip_get(self): return self.clip
    def clip_set(self, t): self.clip = t


class Broken(FakeHost):
    def set_mute(self, o): raise HostUnavailable("no audio device")


BASE = datetime(2026, 11, 1, 18, 40).timestamp()          # a Sunday


class T(unittest.TestCase):
    def setUp(self):
        self.now = [BASE]
        self.host = FakeHost()
        self.dir = Path(tempfile.mkdtemp())
        self.tb = Toolbox(self.dir, now=lambda: self.now[0], host=self.host)
        self.tb.step_runner = lambda s: self.tb.handle(s) or f"(ran {s})"

    def test_mute_and_unmute(self):
        self.assertEqual(self.tb.handle("mute"), "Muted.")
        self.assertTrue(self.host.m)
        self.assertIn("muted", self.tb.handle("am i muted"))
        self.assertEqual(self.tb.handle("unmute"), "Sound is back on.")
        self.assertFalse(self.host.m)
        self.tb.handle("turn the sound off")
        self.assertTrue(self.host.m)
        self.tb.handle("turn the sound back on")
        self.assertFalse(self.host.m)
        self.tb.handle("toggle mute")
        self.assertTrue(self.host.m)

    def test_volume(self):
        self.assertIn("55", self.tb.handle("set volume to 55"))
        self.assertEqual(self.host.v, 55)
        self.tb.handle("volume up")
        self.assertEqual(self.host.v, 65)
        self.tb.handle("turn it down by 20")
        self.assertEqual(self.host.v, 45)
        self.assertIn("0 to 100", self.tb.handle("set volume to 150"))
        self.host.m = True
        self.tb.handle("louder")
        self.assertFalse(self.host.m)                    # changing the volume also brings the sound back

    def test_sound_failure_is_said(self):
        tb = Toolbox(self.dir / "b", host=Broken())
        self.assertIn("couldn't change the sound", tb.handle("mute"))

    def test_habits_streak(self):
        self.tb.handle("i drank water")
        self.now[0] += 86400
        self.assertIn("Streak: 2", self.tb.handle("log habit water"))
        self.assertIn("already", self.tb.handle("i drank water"))
        self.assertIn("2 day streak", self.tb.handle("my habits"))
        self.now[0] += 3 * 86400
        self.assertIn("0 day", self.tb.handle("what's my water streak").replace("0 day", "0 day"))

    def test_moments(self):
        now = datetime(2026, 11, 1, 18, 40)
        ts, has = parse_moment("tomorrow at 3 pm", now)
        self.assertEqual(datetime.fromtimestamp(ts), datetime(2026, 11, 2, 15, 0))
        self.assertEqual(datetime.fromtimestamp(parse_moment("friday 9:30", now)[0]), datetime(2026, 11, 6, 9, 30))
        self.assertEqual(datetime.fromtimestamp(parse_moment("next sunday at noon", now)[0]), datetime(2026, 11, 8, 12, 0))
        self.assertEqual(datetime.fromtimestamp(parse_moment("at 5 pm", now)[0]), datetime(2026, 11, 2, 17, 0))     # already past today: tomorrow
        self.assertIsNone(parse_moment("whenever", now))

    def test_calendar(self):
        self.assertIn("Added dentist", self.tb.handle("add event dentist tomorrow at 3 pm"))
        self.assertIn("dentist", self.tb.handle("what's on tomorrow"))
        self.assertIn("nothing", self.tb.handle("what's on friday").lower())
        self.assertIn("dentist", self.tb.handle("what's my next event"))
        self.assertIn("Removed 1", self.tb.handle("remove event dentist"))
        self.assertIn("what and when", self.tb.handle("add event dentist"))

    def test_event_reminder_fires(self):
        self.tb.handle("add event dentist tomorrow at 3 pm")
        self.now[0] = datetime(2026, 11, 2, 14, 46).timestamp()
        self.assertIn("dentist starts in 15 minutes", " ".join(self.tb.tick()))

    def test_repeating_reminders(self):
        self.assertIn("every day at 8:00 PM", self.tb.handle("remind me every day at 8 pm to take my vitamins"))
        self.assertIn("every weekday", self.tb.handle("remind me every weekday at 7 am to check email"))
        self.assertIn("every 2 hours", self.tb.handle("remind me every 2 hours to stretch"))
        self.now[0] = datetime(2026, 11, 1, 20, 41).timestamp()
        msgs = self.tb.tick()
        self.assertIn("Reminder: take my vitamins.", msgs)
        self.assertIn("Reminder: stretch.", msgs)
        self.assertEqual(self.tb.tick(), [])
        self.assertIn("Stopped", self.tb.handle("stop repeating reminders about stretch"))
        rep = {"kind": "weekly", "days": [0], "h": 9, "m": 0}
        self.assertEqual(datetime.fromtimestamp(next_repeat(rep, BASE)), datetime(2026, 11, 2, 9, 0))

    def test_repeats_survive_restart(self):
        self.tb.handle("remind me every day at 8 pm to take my vitamins")
        again = Toolbox(self.dir, now=lambda: self.now[0], host=self.host)
        self.assertIn("vitamins", again.handle("list repeating reminders"))

    def test_clipboard_and_snippets(self):
        self.assertIn("Saved your address", self.tb.handle("save this as my address"))
        self.host.clip = "something else"
        self.assertIn("address snippet is on the clipboard", self.tb.handle("copy my address snippet"))
        self.assertEqual(self.host.clip, "my street 1")
        self.host.clip = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
        self.assertIn("won't save", self.tb.handle("save this as my key"))
        self.assertIn("address", self.tb.handle("list snippets"))
        self.assertIn("Deleted", self.tb.handle("delete my address snippet"))

    def test_clipboard_history_memory_only(self):
        self.tb.note_clipboard("ignored while off")
        self.tb.handle("start clipboard history")
        self.tb.note_clipboard("first")
        self.tb.note_clipboard("second")
        self.assertIn("1. second", self.tb.handle("show clipboard history"))
        self.tb.handle("restore my previous copy")
        self.assertEqual(self.host.clip, "first")
        self.assertNotIn("second", (self.dir / "toolbox.json").read_text(encoding="utf-8"))

    def test_flashcards(self):
        self.tb.handle("add flashcard capital of France = Paris")
        self.assertIn("1 flashcard", self.tb.handle("how many cards"))
        self.assertIn("capital of France", self.tb.handle("quiz me"))
        self.assertIn("Correct", self.tb.handle("the answer is paris"))
        self.tb.handle("next card")
        self.assertIn("The answer is Paris", self.tb.handle("i don't know"))
        self.tb.handle("next card")
        self.assertIn("Not quite", self.tb.handle("it's London"))
        self.assertIn("Quiz over", self.tb.handle("stop quiz"))

    def test_focus_mode(self):
        self.assertIn("Focus mode on for 25 minutes", self.tb.handle("start focus mode"))
        self.now[0] += 1501
        msgs = self.tb.tick()
        self.assertIn("25 minutes logged", msgs[0])
        self.assertIn("25 minutes", self.tb.handle("focus stats"))
        self.assertIn("isn't on", self.tb.handle("stop focus mode"))

    def test_focus_stop_logs_elapsed(self):
        self.tb.handle("start focus mode for 1 hour")
        self.now[0] += 600
        self.assertIn("10 minutes", self.tb.handle("stop focus mode"))
        self.assertEqual(self.tb.tick(), [])                         # its timer is gone

    def test_meeting_mode_mutes_and_restores(self):
        self.assertIn("muted", self.tb.handle("start meeting mode").lower())
        self.assertTrue(self.host.m)
        self.assertIn("Sound is back on", self.tb.handle("end meeting mode"))
        self.assertFalse(self.host.m)

    def test_timed_meeting_ends_by_itself(self):
        self.tb.handle("start meeting mode for 30 minutes")
        self.now[0] += 1801
        self.assertIn("Meeting mode ended", self.tb.tick()[0])
        for _ in range(50):
            if not self.host.m:
                break
            threading.Event().wait(0.02)
        self.assertFalse(self.host.m)

    def test_scenes(self):
        self.assertIn("Sound is back on", self.tb.handle("movie mode"))
        self.assertEqual(self.host.v, 60)
        self.assertIn("Saved the gaming scene", self.tb.handle("create scene gaming: open discord, open steam, set volume to 50"))
        r = self.tb.handle("gaming mode")
        self.assertIn("(ran open discord)", r)
        self.assertEqual(self.host.v, 50)
        self.assertIn("gaming mode", self.tb.handle("list scenes"))
        self.assertIn("Deleted", self.tb.handle("delete scene gaming"))

    def test_scene_cannot_recurse(self):
        self.tb.handle("create scene loop: movie mode")
        self.assertNotIn("Traceback", self.tb.handle("loop mode"))

    def test_game_mode_for_game_tracks_playtime(self):
        r = self.tb.handle("game mode for CS2")
        self.assertIn("(ran open CS2)", r)
        self.now[0] += 3600
        self.assertIn("1 hour", self.tb.handle("i'm done playing"))
        self.assertIn("CS2 1 hour", self.tb.handle("what did i play this week"))
        self.assertIn("1 hour", self.tb.handle("how long did i play cs2 this week"))

    def test_playtime_switch_closes_previous(self):
        self.tb.handle("i'm playing Roblox")
        self.now[0] += 120
        self.assertIn("ended your earlier session", self.tb.handle("i'm playing Minecraft"))

    def test_alerts_once_then_rearm(self):
        vals = {"battery": 18, "plugged": False, "disk_free": 50, "ram": 50}
        self.tb.sensors = lambda: dict(vals)
        self.tb.handle("turn on pc alerts")
        self.assertIn("Battery is at 18", self.tb.tick()[0])
        self.now[0] += 61
        self.assertEqual(self.tb.tick(), [])                         # said once
        vals["battery"] = 9
        self.now[0] += 61
        self.assertIn("critically", self.tb.tick()[0])
        vals.update(plugged=True)
        self.now[0] += 61
        self.tb.tick()
        vals.update(plugged=False, battery=15)
        self.now[0] += 61
        self.assertIn("Battery is at 15", self.tb.tick()[0])         # re-armed after charging
        vals.update(disk_free=5, ram=95, battery=80)
        self.now[0] += 61
        out = " ".join(self.tb.tick())
        self.assertIn("5 percent free", out)
        self.assertIn("Memory is almost full", out)

    def test_alerts_off_by_default(self):
        self.tb.sensors = lambda: {"battery": 3, "plugged": False}
        self.assertEqual(self.tb.tick(), [])

    def test_good_morning_briefing(self):
        self.tb.handle("add event dentist today at 11 pm")
        self.tb.handle("start tracking guitar")
        r = self.tb.handle("good morning")
        self.assertTrue(r.startswith("Good morning."))
        self.assertIn("dentist", r)
        self.assertIn("guitar", r)

    def test_tick_never_waits_for_a_slow_request(self):
        self.tb._lock.acquire()
        res = []
        th = threading.Thread(target=lambda: res.append(self.tb.tick()))
        th.start()
        th.join(2)
        self.tb._lock.release()
        self.assertFalse(th.is_alive())
        self.assertEqual(res, [[]])

    def test_guest_writes_nothing(self):
        g = Toolbox(self.dir / "g", guest=True, host=self.host)
        g.handle("i drank water")
        g.handle("add flashcard a = b")
        self.assertFalse((self.dir / "g").exists())

    def test_needs_host(self):
        for s in ("mute", "set volume to 50", "movie mode", "save this as my address"):
            self.assertTrue(needs_host(s))
        for s in ("set a timer for 5 minutes", "what is 2 plus 2"):
            self.assertFalse(needs_host(s))

    def test_other_sentences_still_fall_through(self):
        for s in ("tell me about the roman empire", "open roblox", "mute the microphone please explain", "what is the capital of france"):
            self.assertIsNone(self.tb.handle(s))


class HostScripts(unittest.TestCase):
    def test_not_windows_says_so(self):
        h = Host()
        h.is_windows = False
        for call in (h.get_mute, lambda: h.set_mute(True), h.get_volume, lambda: h.set_volume(10), h.clip_get, lambda: h.clip_set("x")):
            with self.assertRaises(HostUnavailable):
                call()

    def test_scripts_are_fixed_and_numeric(self):
        seen = []
        h = Host()
        h.is_windows = True
        h._ps = lambda script, timeout=25.0: seen.append(script) or "0.55"
        self.assertEqual(h.set_volume(150), 55)
        self.assertIn("= 1.00;", seen[0])                           # clamped, written as a number
        h.set_volume("30")
        self.assertIn("= 0.30;", seen[1])


if __name__ == "__main__":
    unittest.main()
