"""Controller-level voice options: greeting, pause, interrupting, several wake phrases, and changes reaching a running listener."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.voice.settings import VoiceSettings  # noqa: E402


class Controls(AppCase):
    def ctl(self):
        c = AppController(self.cfg, simulate=True, dispatch=lambda fn: fn())
        self.addCleanup(c.shutdown, 2.0)
        return c

    def saved(self):
        return VoiceSettings.load(self.cfg.profile_dir / "voice.json")

    def test_options_defaults_and_set(self):
        c = self.ctl()
        o = c.voice_options()
        self.assertEqual((o["wait_after_speech_seconds"], o["barge_in"], o["wake_greeting_on"]), (5.0, "smart", True))
        c.voice_options_set(wake_greeting="Yes boss?", wait_after_speech_seconds=3.0, barge_in="off")
        v = self.saved()
        self.assertEqual((v.wake_greeting, v.wait_after_speech_seconds, v.barge_in), ("Yes boss?", 3.0, "off"))

    def test_bad_values_and_unknown_names_change_nothing(self):
        c = self.ctl()
        c.voice_options_set(wake_greeting="Hi")
        for bad in ({"wait_after_speech_seconds": 99}, {"barge_in": "loud"}, {"wake_greeting": "x" * 500}, {"input_mode": "wake_word"}, {"muted": True}):
            with self.assertRaises(ValueError, msg=str(bad)):
                c.voice_options_set(**bad)
        self.assertEqual(self.saved().wake_greeting, "Hi")
        self.assertEqual(self.saved().input_mode, "off")                   # this path can never turn listening on

    def test_wake_phrases_several(self):
        c = self.ctl()
        r = c.voice_wake_phrases_set(["Hey Jarvis", "okay jarvis", "wake up jarvis"])
        self.assertEqual(r["wake_phrases"], ["hey jarvis", "okay jarvis", "wake up jarvis"])
        self.assertEqual(self.saved().all_wake_phrases(), r["wake_phrases"])
        with self.assertRaises(ValueError):
            c.voice_wake_phrases_set(["jarvis"])
        self.assertEqual(self.saved().all_wake_phrases(), r["wake_phrases"])

    def test_guest_cannot_change_them(self):
        c = AppController(self.cfg, simulate=True, dispatch=lambda fn: fn(), guest=True)
        self.addCleanup(c.shutdown, 2.0)
        with self.assertRaises(ValueError):
            c.voice_options_set(wake_greeting="x")
        with self.assertRaises(ValueError):
            c.voice_wake_phrases_set(["hey jarvis", "okay jarvis"])


if __name__ == "__main__":
    unittest.main()


from p17_common import Rig, wait_for  # noqa: E402


class LiveWhileRunning(AppCase):
    def test_changing_the_voice_and_options_reach_the_running_listener(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        ses = r.c.voice._session
        r.c.voice_options_set(wake_greeting="Yes boss?", wait_after_speech_seconds=2.0)
        self.assertEqual((ses.s.wake_greeting, ses.s.wait_after_speech_seconds), ("Yes boss?", 2.0))
        r.c.voice_setting(rate=1.5, volume=0.4)
        self.assertEqual((ses.s.rate, ses.s.volume), (1.5, 0.4))
        from dataclasses import replace
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        replace(vs, tts_voice="sapi:Zira").save(self.cfg.profile_dir / "voice.json")
        r.c.voice._session  # keep reference
        r.c._voice_live()
        self.assertEqual(ses.s.tts_voice, "sapi:Zira")

    def test_new_wake_phrases_restart_the_listener_with_them(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        res = r.c.voice_wake_phrases_set(["hey jarvis", "okay jarvis"])
        self.assertTrue(res["restarting"])
        self.assertTrue(wait_for(lambda: r.state()["state"] == "Listening" and r.c.voice._session.s.wake_alternatives == ["okay jarvis"], 8, r.c))
