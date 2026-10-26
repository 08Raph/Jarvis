"""Console controls and microphone indicator for `jarvis voice run`. Uses the fake mic/recognizer/speaker from test_voice:
this checks the logic only; keyboard reading on Windows (msvcrt) and real audio devices are untested."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.voice import console as C
from test_voice import LOUD, make, run


class BannerTests(unittest.TestCase):
    def test_open_only_when_stream_open(self):
        self.assertIn("MIC OPEN", C.banner("LISTENING", True))
        self.assertIn("MIC CLOSED", C.banner("MUTED", False))
        self.assertNotIn("MIC OPEN", C.banner("MUTED", False))

    def test_every_session_state_has_plain_text(self):
        from jarvis.voice import session as S
        for st in (S.OFF, S.LISTENING, S.CAPTURING, S.THINKING, S.SPEAKING, S.FOLLOW_UP, S.SLEEPING, S.MUTED, S.RECOVERING, S.ERROR):
            self.assertIn(st, C._STATE_TEXT)

    def test_event_formatting(self):
        self.assertIn("MIC CLOSED", C.format_event("state", {"state": "MUTED", "mic_open": False}))
        self.assertIsNone(C.format_event("spoken", "x"))
        self.assertIn("you said", C.format_event("heard", "hello"))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.sess, self.mic, self.rec, self.spk, self.clk, self.events = make()
        self.said = []
        self.c = C.Controls(self.sess, say=self.said.append)
        self.sess.start()

    def test_mute_closes_stream_and_unmute_reopens(self):
        self.assertTrue(self.mic.is_open)
        self.c.key("m")
        self.assertEqual(self.sess.state, "MUTED")
        self.assertFalse(self.mic.is_open)
        self.assertFalse(self.sess.mic_open)
        self.c.key("m")
        self.assertEqual(self.sess.state, "LISTENING")
        self.assertTrue(self.mic.is_open)

    def test_every_state_event_reports_the_real_mic_state(self):
        self.c.key("m")
        self.c.key("m")
        self.c.key("s")
        for kind, p in self.events:
            if kind == "state":
                self.assertEqual(p["mic_open"], p["state"] in {"LISTENING", "CAPTURING", "THINKING", "SPEAKING", "FOLLOW_UP"}, p)

    def test_sleep_and_wake(self):
        self.c.key("s")
        self.assertEqual(self.sess.state, "SLEEPING")
        self.assertFalse(self.mic.is_open)
        self.c.key("w")
        self.assertEqual(self.sess.state, "LISTENING")

    def test_push_to_talk_toggle_sends_flushed_text(self):
        heard = []
        self.sess.responder = lambda t: heard.append(t) or "Certainly."
        self.c.key("p")
        self.assertEqual(self.sess.state, "CAPTURING")
        self.assertTrue(self.c.ptt_on)
        self.rec.flush_text = "what time is it"
        self.c.key("p")
        self.assertFalse(self.c.ptt_on)
        self.assertEqual(heard, ["what time is it"])

    def test_push_to_talk_refused_while_muted(self):
        self.c.key("m")
        self.c.key("p")
        self.assertEqual(self.sess.state, "MUTED")
        self.assertFalse(self.mic.is_open)
        self.assertTrue(any("muted" in m for m in self.said))

    def test_cancel_silences_speech_immediately(self):
        self.spk.speaking = True
        before = self.spk.stops
        self.c.key("c")
        self.assertGreater(self.spk.stops, before)
        self.assertFalse(self.spk.speaking)

    def test_quit_help_and_unknown_keys(self):
        self.c.key("h")
        self.assertIn("m = mute", self.said[-1])
        self.c.key("z")
        self.c.key("")
        self.assertFalse(self.c.quit)
        self.c.key("Q")
        self.assertTrue(self.c.quit)


class ReaderTests(unittest.TestCase):
    def test_non_interactive_stdin_gives_no_hidden_input(self):
        if sys.platform != "win32" and not sys.stdin.isatty():
            self.assertIsNone(C.make_key_reader()())

    def test_console_module_has_no_network_or_audio_imports(self):
        src = (ROOT / "src" / "jarvis" / "voice" / "console.py").read_text()
        for bad in ("socket", "urllib", "http", "requests", "sounddevice"):
            self.assertNotIn(f"import {bad}", src)


if __name__ == "__main__":
    unittest.main()
