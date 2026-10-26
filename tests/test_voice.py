"""Voice logic tests using FAKE mic/recognizer/speaker. They prove the state machine, not real audio."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis.voice import session as S
from jarvis.voice.settings import VoiceSettings
from jarvis.voice.speakable import apply_pronunciations, shorten, speakable
from jarvis.voice.vad import EnergyVAD
from jarvis.voice.wake import match_wake

LOUD = (np.ones(480) * 8000).astype(np.int16)
QUIET = (np.ones(480) * 20).astype(np.int16)


class FakeMic:
    def __init__(self):
        self.opened = 0
        self.closed = 0
        self.is_open = False
        self.fail_reads = 0
        self.fail_opens = 0

    def open(self, device):
        if self.fail_opens > 0:
            self.fail_opens -= 1
            raise S.MicError("device missing")
        self.opened += 1
        self.is_open = True

    def read(self):
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise S.MicError("read failed")
        return QUIET

    def close(self):
        self.closed += 1
        self.is_open = False


class FakeRec:
    """Returns scripted text: queue entries are (mode, text); returned on the next feed of that mode."""
    def __init__(self):
        self.queue = []
        self.flush_text = ""
        self.resets = 0

    def feed(self, frame, mode):
        if self.queue and self.queue[0][0] == mode:
            return self.queue.pop(0)[1]
        return None

    def reset(self):
        self.resets += 1

    def flush(self):
        t, self.flush_text = self.flush_text, ""
        return t


class FakeSpk:
    def __init__(self):
        self.said = []
        self.speaking = False
        self.stops = 0

    def speak(self, text, rate, volume, voice):
        self.said.append((text, rate, volume, voice))
        self.speaking = True

    def stop(self):
        self.stops += 1
        self.speaking = False

    def is_speaking(self):
        return self.speaking


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(**kw):
    reply = kw.pop("reply", "Certainly. It is done.")
    s = VoiceSettings(input_mode=kw.pop("input_mode", "wake_word"), **kw)
    mic, rec, spk, clk = FakeMic(), FakeRec(), FakeSpk(), Clock()
    events = []
    sess = S.VoiceSession(s, mic, rec, spk, lambda t: reply if isinstance(reply, str) else reply(t),
                          on_event=lambda k, p: events.append((k, p)), clock=clk)
    return sess, mic, rec, spk, clk, events


def run(sess, clk, frame, seconds):
    for _ in range(int(seconds / 0.03)):
        clk.t += 0.03
        sess.step(frame)


class WakeTests(unittest.TestCase):
    def test_exact_and_fuzzy_and_midsentence(self):
        p = ["hey jarvis", "hey computer"]
        self.assertEqual(match_wake("Hey Jarvis", p)[0], "hey jarvis")
        self.assertEqual(match_wake("hey jarvis what time is it", p)[1], "what time is it")
        self.assertEqual(match_wake("hey jarvas", p, 0.5)[0], "hey jarvis")
        self.assertIsNone(match_wake("hey jarvas", p, 0.0)[0])           # strict sensitivity rejects
        self.assertIsNone(match_wake("tell her hey jarvis", p)[0])        # not at the start
        self.assertIsNone(match_wake("hay darwin", p, 0.5)[0])
        self.assertEqual(match_wake("hey computer", p)[0], "hey computer")

    def test_one_word_wake_rejected(self):
        with self.assertRaises(ValueError):
            VoiceSettings(wake_phrase="jarvis").validate()


class SettingsTests(unittest.TestCase):
    def test_defaults_are_safe(self):
        s = VoiceSettings()
        self.assertEqual(s.input_mode, "off")
        self.assertEqual(s.barge_in, "wake_word_only")

    def test_validation_and_roundtrip_and_corrupt(self):
        for bad in ({"rate": 5}, {"volume": 2}, {"input_mode": "always"}, {"sensitivity": -1}):
            with self.assertRaises(ValueError):
                VoiceSettings(**bad).validate()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "voice.json"
            VoiceSettings(input_mode="push_to_talk", rate=1.2, pronunciations={"SQL": "sequel"}).save(p)
            self.assertEqual(VoiceSettings.load(p).pronunciations, {"SQL": "sequel"})
            p.write_text("{not json")
            self.assertEqual(VoiceSettings.load(p).input_mode, "off")
            p.write_text('{"rate": 99}')
            self.assertEqual(VoiceSettings.load(p).rate, 1.0)


class SpeakableTests(unittest.TestCase):
    def test_logs_code_paths_secrets_never_spoken(self):
        raw = ("Done.\n2026-10-25 10:00:01 INFO loaded\nTraceback (most recent call last):\n"
               "  File \"x.py\", line 3, in f\n```python\nprint(1)\n```\nSee C:\\Users\\me\\a.txt and /home/me/b.txt "
               "and https://example.com/x key a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6")
        out = speakable(raw)
        for bad in ("Traceback", "INFO", "print(1)", "C:\\Users", "/home/me", "https://", "a1b2c3d4"):
            self.assertNotIn(bad, out)
        self.assertIn("Done.", out)

    def test_pronunciation_and_length(self):
        self.assertEqual(apply_pronunciations("Use SQL.", {"SQL": "sequel"}), "Use sequel.")
        txt = "One. Two. Three. Four."
        self.assertTrue(shorten(txt, "brief").startswith("One."))
        self.assertNotIn("Two", shorten(txt, "brief"))
        self.assertIn("Three.", shorten(txt, "normal"))
        self.assertNotIn("Four", shorten(txt, "normal"))
        self.assertEqual(shorten(txt, "detailed"), txt)


class VadTests(unittest.TestCase):
    def test_speech_vs_silence(self):
        v = EnergyVAD()
        self.assertFalse(v.is_speech(QUIET))
        self.assertTrue(v.is_speech(LOUD))


class SessionTests(unittest.TestCase):
    def test_off_input_refuses_to_start_and_opens_no_mic(self):
        sess, mic, *_ = make(input_mode="off")
        with self.assertRaises(RuntimeError):
            sess.start()
        self.assertEqual(mic.opened, 0)

    def test_wake_then_command_then_speech_then_followup(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        self.assertEqual(sess.state, S.LISTENING)
        rec.queue.append(("wake", "hey jarvis"))
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.CAPTURING)
        run(sess, clk, LOUD, 0.3)                      # user talks...
        rec.queue.append(("dictation", "what time is it"))
        run(sess, clk, QUIET, 0.1)                     # ...recognizer finalizes after they stop
        self.assertEqual(sess.state, S.SPEAKING)
        self.assertEqual(spk.said[0][0], "Certainly. It is done.")
        spk.speaking = False
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.FOLLOW_UP)
        run(sess, clk, QUIET, 0.7)                     # Prompt 17: wait out the post-speech echo guard (0.6 s)
        run(sess, clk, LOUD, 0.3)
        rec.queue.append(("dictation", "and tomorrow"))   # follow-up needs NO wake word
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(len(spk.said), 2)
        self.assertIn(("heard", "and tomorrow"), ev)

    def test_wake_with_inline_command(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        rec.queue.append(("wake", "hey jarvis what time is it"))
        run(sess, clk, QUIET, 0.1)
        self.assertIn(("heard", "what time is it"), ev)
        self.assertEqual(sess.state, S.SPEAKING)

    def test_followup_window_expires(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=2.0)
        sess.start()
        rec.queue.append(("wake", "hey jarvis what time is it"))
        run(sess, clk, QUIET, 0.1)
        spk.speaking = False
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.FOLLOW_UP)
        run(sess, clk, QUIET, 2.5)
        self.assertEqual(sess.state, S.LISTENING)

    def test_no_wake_no_response_and_no_speech_noise(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        rec.queue.append(("wake", "what a lovely day"))
        run(sess, clk, LOUD, 2)
        self.assertEqual(sess.state, S.LISTENING)
        self.assertFalse(spk.said)

    def test_capture_silence_ends_utterance_via_flush(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        rec.queue.append(("wake", "hey jarvis"))
        run(sess, clk, QUIET, 0.1)
        rec.flush_text = "open notepad"
        run(sess, clk, LOUD, 0.3)
        run(sess, clk, QUIET, 1.2)
        self.assertIn(("heard", "open notepad"), ev)

    def test_capture_timeout_returns_to_listening(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        rec.queue.append(("wake", "hey jarvis"))
        run(sess, clk, QUIET, 0.1)
        run(sess, clk, QUIET, 7)
        self.assertEqual(sess.state, S.LISTENING)

    def test_mute_closes_stream_and_blocks_everything(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        self.assertTrue(mic.is_open)
        sess.mute()
        self.assertFalse(mic.is_open)
        self.assertEqual(sess.state, S.MUTED)
        rec.queue.append(("wake", "hey jarvis what time is it"))
        run(sess, clk, LOUD, 1)
        sess.tick()
        self.assertFalse(spk.said)
        sess.ptt_press()
        self.assertFalse(mic.is_open)              # PTT cannot bypass mute
        with self.assertRaises(RuntimeError):
            VoiceSession = S.VoiceSession
            VoiceSession(VoiceSettings(), mic, rec, spk, lambda t: "").start()
        sess.unmute()
        self.assertTrue(mic.is_open)

    def test_muted_setting_prevents_start(self):
        sess, mic, *_ = make(muted=True)
        sess.start()
        self.assertEqual(sess.state, S.MUTED)
        self.assertEqual(mic.opened, 0)

    def test_stop_command_cancels_speech_immediately(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0)
        sess.start()
        rec.queue.append(("wake", "hey jarvis what time is it"))
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.SPEAKING)
        stops = spk.stops
        rec.queue.append(("wake", "stop"))
        run(sess, clk, QUIET, 0.1)
        self.assertGreater(spk.stops, stops)
        self.assertFalse(spk.speaking)
        self.assertEqual(sess.state, S.LISTENING)

    def test_wake_word_barge_in_interrupts_then_captures(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0)
        sess.start()
        rec.queue.append(("wake", "hey jarvis what time is it"))
        run(sess, clk, QUIET, 0.1)
        rec.queue.append(("wake", "hey jarvis"))
        run(sess, clk, QUIET, 0.1)
        self.assertFalse(spk.speaking)
        self.assertEqual(sess.state, S.CAPTURING)

    def test_speech_barge_in_only_when_enabled(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0)
        sess.start()
        rec.queue.append(("wake", "hey jarvis what time is it"))
        run(sess, clk, QUIET, 0.1)
        run(sess, clk, LOUD, 0.5)                    # default barge_in=wake_word_only: loud noise must not interrupt
        self.assertTrue(spk.speaking)
        sess2, mic2, rec2, spk2, clk2, ev2 = make(follow_up_seconds=0, barge_in="speech")
        sess2.start()
        rec2.queue.append(("wake", "hey jarvis what time is it"))
        run(sess2, clk2, QUIET, 0.1)
        run(sess2, clk2, LOUD, 0.5)
        self.assertFalse(spk2.speaking)

    def test_does_not_speak_over_user(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0)
        sess.start()
        sess.step(LOUD)                               # user is talking right now
        sess._set(S.THINKING)                         # Prompt 17: _deliver now ignores replies unless the session is still THINKING (as in the real flow)
        sess._deliver("Reply that must not be spoken now")
        self.assertFalse(spk.said)
        self.assertEqual(sess.state, S.CAPTURING)

    def test_sleep_by_voice_closes_mic_and_wake_reopens(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        rec.queue.append(("wake", "hey jarvis go to sleep"))
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.SLEEPING)
        self.assertFalse(mic.is_open)
        sess.wake()
        self.assertEqual(sess.state, S.LISTENING)
        self.assertTrue(mic.is_open)

    def test_idle_auto_sleep(self):
        sess, mic, rec, spk, clk, ev = make(sleep_after_seconds=10)
        sess.start()
        run(sess, clk, QUIET, 11)
        self.assertEqual(sess.state, S.SLEEPING)
        self.assertFalse(mic.is_open)

    def test_mic_failure_recovers_with_backoff(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        mic.fail_reads = 1
        sess.tick()
        self.assertEqual(sess.state, S.RECOVERING)
        self.assertFalse(mic.is_open)
        sess.tick()                                   # too early to retry
        self.assertEqual(sess.state, S.RECOVERING)
        clk.t += 1.1
        sess.tick()
        self.assertEqual(sess.state, S.LISTENING)
        self.assertTrue(any(k == "error" for k, _ in ev))

    def test_mic_failure_gives_up_with_clear_message(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        mic.fail_reads = 1
        mic.fail_opens = 99
        sess.tick()
        for _ in range(20):
            clk.t += 40
            sess.tick()
        self.assertEqual(sess.state, S.ERROR)
        self.assertIn("Microphone unavailable", sess.error_message)

    def test_mic_failure_during_speech_silences_speaker(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0)
        sess.start()
        rec.queue.append(("wake", "hey jarvis what time is it"))
        run(sess, clk, QUIET, 0.1)
        mic.fail_reads = 1
        sess.tick()
        self.assertFalse(spk.speaking)
        self.assertEqual(sess.state, S.RECOVERING)

    def test_push_to_talk(self):
        sess, mic, rec, spk, clk, ev = make(input_mode="push_to_talk")
        sess.start()
        rec.queue.append(("wake", "hey jarvis what time is it"))   # wake words are ignored in PTT mode
        run(sess, clk, QUIET, 0.2)
        self.assertEqual(sess.state, S.LISTENING)
        sess.ptt_press()
        self.assertEqual(sess.state, S.CAPTURING)
        run(sess, clk, LOUD, 3)                                    # does not end on its own while held
        self.assertEqual(sess.state, S.CAPTURING)
        rec.flush_text = "turn it up"
        sess.ptt_release()
        self.assertIn(("heard", "turn it up"), ev)

    def test_ptt_interrupts_speech(self):
        sess, mic, rec, spk, clk, ev = make(input_mode="push_to_talk", follow_up_seconds=0)
        sess.start()
        sess.ptt_press()
        rec.flush_text = "hello"
        sess.ptt_release()
        self.assertTrue(spk.speaking)
        sess.ptt_press()
        self.assertFalse(spk.speaking)

    def test_output_modes(self):
        sess, mic, rec, spk, clk, ev = make(output_mode="text_only", follow_up_seconds=0)
        sess.start()
        rec.queue.append(("wake", "hey jarvis hello"))
        run(sess, clk, QUIET, 0.1)
        self.assertFalse(spk.said)
        self.assertTrue(any(k == "reply_text" for k, _ in ev))
        sess, mic, rec, spk, clk, ev = make(output_mode="voice_only", follow_up_seconds=0)
        sess.start()
        rec.queue.append(("wake", "hey jarvis hello"))
        run(sess, clk, QUIET, 0.1)
        self.assertTrue(spk.said)
        self.assertFalse(any(k == "reply_text" for k, _ in ev))

    def test_logs_never_reach_speaker_and_settings_applied(self):
        reply = "All good.\nERROR something internal\n2026-10-25 10:00 DEBUG x\nUse SQL."
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0, reply=reply, rate=1.3, volume=0.4,
                                            pronunciations={"SQL": "sequel"}, response_length="detailed")
        sess.start()
        rec.queue.append(("wake", "hey jarvis go"))
        run(sess, clk, QUIET, 0.1)
        text, rate, vol, _ = spk.said[0]
        self.assertNotIn("ERROR", text)
        self.assertNotIn("DEBUG", text)
        self.assertIn("sequel", text)
        self.assertEqual((rate, vol), (1.3, 0.4))

    def test_responder_exception_is_not_spoken_raw(self):
        def boom(t):
            raise RuntimeError("secret internal path C:\\x")
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0, reply=boom)
        sess.start()
        rec.queue.append(("wake", "hey jarvis go"))
        run(sess, clk, QUIET, 0.1)
        self.assertNotIn("secret", spk.said[0][0])
        self.assertNotIn("C:", spk.said[0][0])


if __name__ == "__main__":
    unittest.main()
