"""Wake greeting, the pause before answering, "stop"/talk-over interrupting, several wake phrases and live settings. FAKE mic/recogniser/speaker: proves the state machine, not real audio."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_voice import LOUD, QUIET, make, run  # noqa: E402
from jarvis.voice import session as S  # noqa: E402
from jarvis.voice.settings import VoiceSettings  # noqa: E402


class PartialRec:
    """FakeRec plus a partial() like Vosk's."""
    def __init__(self, base):
        self.base, self.partial_text = base, ""

    def __getattr__(self, n):
        return getattr(self.base, n)

    def partial(self):
        return self.partial_text


def wake(sess, rec, clk, phrase="hey jarvis"):
    rec.queue.append(("wake", phrase))
    run(sess, clk, QUIET, 0.1)


class Greeting(unittest.TestCase):
    def test_greeting_is_spoken_after_the_wake_phrase_then_the_request_is_heard(self):
        sess, mic, rec, spk, clk, ev = make(wake_greeting_on=True, wake_greeting="Hello sir, how may I assist you?", post_speech_guard_seconds=0.0)
        sess.start()
        wake(sess, rec, clk)
        self.assertEqual(sess.state, S.SPEAKING)
        self.assertEqual(spk.said[0][0], "Hello sir, how may I assist you?")
        self.assertTrue(any(k == "greeting" for k, _ in ev))
        spk.speaking = False
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.FOLLOW_UP)                    # no wake phrase needed for the request
        run(sess, clk, LOUD, 0.3)
        rec.queue.append(("dictation", "open notepad"))
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.SPEAKING)
        self.assertEqual(spk.said[1][0], "Certainly. It is done.")

    def test_greeting_works_even_with_no_follow_up_window_configured(self):
        sess, mic, rec, spk, clk, ev = make(wake_greeting_on=True, follow_up_seconds=0, post_speech_guard_seconds=0.0)
        sess.start()
        wake(sess, rec, clk)
        spk.speaking = False
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.FOLLOW_UP)
        run(sess, clk, QUIET, 9.0)
        self.assertEqual(sess.state, S.FOLLOW_UP)                    # at least 10 s to start talking
        run(sess, clk, QUIET, 2.0)
        self.assertEqual(sess.state, S.LISTENING)

    def test_no_greeting_when_off_empty_muted_or_text_only(self):
        for kw in ({"wake_greeting_on": False}, {"wake_greeting_on": True, "wake_greeting": ""}, {"wake_greeting_on": True, "speaker_muted": True},
                   {"wake_greeting_on": True, "output_mode": "text_only"}):
            sess, mic, rec, spk, clk, ev = make(**kw)
            sess.start()
            wake(sess, rec, clk)
            self.assertEqual(sess.state, S.CAPTURING, kw)
            self.assertEqual(spk.said, [], kw)

    def test_no_greeting_when_the_request_came_in_the_same_breath(self):
        sess, mic, rec, spk, clk, ev = make(wake_greeting_on=True)
        sess.start()
        rec.queue.append(("wake", "hey jarvis open chrome"))
        run(sess, clk, QUIET, 0.1)
        self.assertFalse(any(s[0].startswith("Hello") for s in spk.said))

    def test_talking_over_the_greeting_is_heard(self):
        sess, mic, rec, spk, clk, ev = make(wake_greeting_on=True, barge_in="smart")
        sess.start()
        wake(sess, rec, clk)
        self.assertEqual(sess.state, S.SPEAKING)
        rec.queue.append(("dictation", "open the calculator"))
        run(sess, clk, QUIET, 0.1)
        self.assertGreaterEqual(spk.stops, 1)
        self.assertIn(sess.state, (S.CAPTURING, S.THINKING, S.SPEAKING))


class Pause(unittest.TestCase):
    def test_it_waits_for_the_whole_pause_before_answering(self):
        sess, mic, rec, spk, clk, ev = make(wait_after_speech_seconds=5.0)
        sess.start()
        wake(sess, rec, clk)
        run(sess, clk, LOUD, 0.5)
        rec.queue.append(("dictation", "i would like to"))        # the recogniser ends a piece at a short pause
        run(sess, clk, QUIET, 2.5)                                # you think for 2.5 s
        self.assertEqual(sess.state, S.CAPTURING)
        self.assertEqual(spk.said, [])
        run(sess, clk, LOUD, 0.5)
        rec.queue.append(("dictation", "open the calculator"))
        run(sess, clk, QUIET, 4.0)
        self.assertEqual(sess.state, S.CAPTURING)                 # still inside the pause
        heard = []
        sess._emit = lambda k, p: heard.append((k, p))
        run(sess, clk, QUIET, 1.5)
        self.assertIn(("heard", "i would like to open the calculator"), heard)
        self.assertIn(sess.state, (S.SPEAKING, S.THINKING))

    def test_stop_alone_needs_no_waiting(self):
        sess, mic, rec, spk, clk, ev = make(wait_after_speech_seconds=5.0)
        sess.start()
        wake(sess, rec, clk)
        rec.queue.append(("dictation", "never mind"))
        run(sess, clk, QUIET, 0.2)
        self.assertEqual(sess.state, S.LISTENING)

    def test_zero_pause_keeps_the_old_quick_behaviour(self):
        sess, mic, rec, spk, clk, ev = make(wait_after_speech_seconds=0.0)
        sess.start()
        wake(sess, rec, clk)
        run(sess, clk, LOUD, 0.3)
        rec.queue.append(("dictation", "what time is it"))
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.SPEAKING)


class Interrupt(unittest.TestCase):
    def speaking_session(self, reply="The weather is mild and sunny today."):
        sess, mic, rec, spk, clk, ev = make(barge_in="smart", reply=reply, follow_up_seconds=0, post_speech_guard_seconds=0.0)
        rec2 = PartialRec(rec)
        sess.rec = rec2
        sess.start()
        wake(sess, rec, clk)
        run(sess, clk, LOUD, 0.3)
        rec.queue.append(("dictation", "how is the weather"))
        run(sess, clk, QUIET, 0.1)
        self.assertEqual(sess.state, S.SPEAKING)
        return sess, rec, rec2, spk, clk, ev

    def test_saying_stop_silences_jarvis(self):
        sess, rec, rec2, spk, clk, ev = self.speaking_session()
        rec.queue.append(("dictation", "stop"))
        run(sess, clk, QUIET, 0.1)
        self.assertGreaterEqual(spk.stops, 1)
        self.assertEqual(sess.state, S.LISTENING)

    def test_talking_over_jarvis_cuts_it_off_and_keeps_your_words(self):
        sess, rec, rec2, spk, clk, ev = self.speaking_session()
        rec2.partial_text = "actually open the calculator"
        run(sess, clk, LOUD, 0.1)
        self.assertGreaterEqual(spk.stops, 1)
        self.assertEqual(sess.state, S.CAPTURING)
        self.assertTrue(any(k == "barge_in" for k, _ in ev))

    def test_jarvis_own_echo_does_not_interrupt_it(self):
        sess, rec, rec2, spk, clk, ev = self.speaking_session()
        stops = spk.stops
        rec2.partial_text = "the weather is mild"
        run(sess, clk, LOUD, 0.3)
        rec.queue.append(("dictation", "the weather is mild and sunny today"))
        run(sess, clk, LOUD, 0.1)
        self.assertEqual(spk.stops, stops)
        self.assertEqual(sess.state, S.SPEAKING)

    def test_stop_while_thinking_cancels_the_reply(self):
        sess, mic, rec, spk, clk, ev = make(barge_in="smart")
        rec.queue.append(("dictation", "stop"))
        sess.observe(QUIET)
        self.assertTrue(sess.take_cancel())
        self.assertFalse(sess.take_cancel())

    def test_off_means_off(self):
        sess, mic, rec, spk, clk, ev = make(barge_in="off", reply="The weather is mild and sunny today.", follow_up_seconds=0)
        sess.start()
        wake(sess, rec, clk)
        run(sess, clk, LOUD, 0.3)
        rec.queue.append(("dictation", "how is the weather"))
        run(sess, clk, QUIET, 0.1)
        rec.queue.append(("dictation", "stop"))
        run(sess, clk, LOUD, 0.2)
        self.assertEqual(sess.state, S.SPEAKING)


class WakePhrases(unittest.TestCase):
    def test_several_phrases_the_first_is_main(self):
        vs = VoiceSettings().set_wake_phrases(["Hey Jarvis", "okay jarvis", "yo jarvis", "hey jarvis"])
        self.assertEqual(vs.wake_phrase, "hey jarvis")
        self.assertEqual(vs.wake_alternatives, ["okay jarvis", "yo jarvis"])
        self.assertEqual(vs.all_wake_phrases(), ["hey jarvis", "okay jarvis", "yo jarvis"])

    def test_one_word_empty_or_too_many_are_refused(self):
        for bad in (["jarvis"], [], ["hey jarvis", "computer"], [f"hey jarvis{n}" for n in range(30)], ["a b c d e f g"]):
            with self.assertRaises(ValueError, msg=str(bad)):
                VoiceSettings().set_wake_phrases(bad)

    def test_each_phrase_wakes_it(self):
        sess, mic, rec, spk, clk, ev = make(wake_phrase="hey jarvis", wake_alternatives=["okay jarvis", "yo computer"])
        sess.start()
        wake(sess, rec, clk, "yo computer")
        self.assertEqual(sess.state, S.CAPTURING)


class LiveSettings(unittest.TestCase):
    def test_changes_reach_a_running_session(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        new = VoiceSettings(input_mode="wake_word", tts_voice="sapi:Zira", rate=1.4, volume=0.3, wake_greeting="Yes boss?", wait_after_speech_seconds=2.0, barge_in="off")
        sess.apply_live(new)
        self.assertEqual((sess.s.tts_voice, sess.s.rate, sess.s.volume, sess.s.wake_greeting, sess.s.wait_after_speech_seconds, sess.s.barge_in),
                         ("sapi:Zira", 1.4, 0.3, "Yes boss?", 2.0, "off"))
        self.assertEqual(sess.s.input_mode, "wake_word")           # the listener's mode is not touched

    def test_old_saved_files_get_the_new_defaults(self):
        import json
        import tempfile
        p = Path(tempfile.mkdtemp()) / "voice.json"
        p.write_text(json.dumps({"input_mode": "wake_word", "barge_in": "wake_word_only", "silence_end_seconds": 0.9}), encoding="utf-8")
        vs = VoiceSettings.load(p)
        self.assertEqual((vs.barge_in, vs.wait_after_speech_seconds, vs.wake_greeting_on), ("smart", 5.0, True))


if __name__ == "__main__":
    unittest.main()
