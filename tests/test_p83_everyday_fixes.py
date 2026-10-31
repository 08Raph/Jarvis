"""Repeat loops, the real clock, and program labels that must not be read aloud."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase  # noqa: E402,F401
from jarvis import clockfacts, replyguard  # noqa: E402
from jarvis.voice.speakable import speakable  # noqa: E402

AT = datetime(2026, 10, 31, 16, 52, tzinfo=timezone(timedelta(hours=-4)))


class Clock(unittest.TestCase):
    def test_time_and_date_questions(self):
        now = lambda: AT  # noqa: E731
        self.assertEqual(clockfacts.answer("what time is it", now), "It's 4:52 PM.")
        self.assertEqual(clockfacts.answer("Hey Jarvis, what's the time?", now), "It's 4:52 PM.")
        self.assertEqual(clockfacts.answer("what's the date", now), "Today is Saturday, October 31, 2026.")
        self.assertEqual(clockfacts.answer("What day is it today?", now), "Today is Saturday, October 31, 2026.")

    def test_other_questions_go_to_the_model(self):
        for t in ("what time does the movie start", "what time zone is Tokyo in", "write a poem about time", "", "x" * 200, "what is the time complexity of quicksort"):
            self.assertIsNone(clockfacts.answer(t, lambda: AT), t)

    def test_midnight_and_noon(self):
        self.assertEqual(clockfacts.answer("what time is it", lambda: AT.replace(hour=0, minute=5)), "It's 12:05 AM.")
        self.assertEqual(clockfacts.answer("what time is it", lambda: AT.replace(hour=12, minute=0)), "It's 12:00 PM.")

    def test_prompt_line_has_the_exact_time(self):
        line = clockfacts.prompt_line(AT)
        self.assertIn("Saturday, October 31, 2026, 4:52 PM", line)
        self.assertIn("UTC-04:00", line)


class Loops(unittest.TestCase):
    def test_detects_and_trims(self):
        text = "Sure. I can help with that. " * 4
        self.assertTrue(replyguard.repeating_unit(text))
        out, looped = replyguard.trim_loop(text)
        self.assertTrue(looped)
        self.assertEqual(out.count("I can help with that."), 1)

    def test_normal_text_is_untouched(self):
        for t in ("Hello there, how are you doing today? I am fine.", "ha ha ha ha ha", "", "one two three four five six seven eight nine ten"):
            self.assertEqual(replyguard.repeating_unit(t), 0, t)
            self.assertEqual(replyguard.trim_loop(t), (t, False))

    def test_sentence_loop(self):
        self.assertTrue(replyguard.sentence_loop("Okay. I will do it now! I will do it now! I will do it now!"))
        self.assertFalse(replyguard.sentence_loop("Yes. Yes. Yes."))      # too short to count


class Labels(unittest.TestCase):
    def test_style_labels_are_not_spoken(self):
        self.assertEqual(speakable("[Debate mode] Hello, let us argue."), "Hello, let us argue.")
        self.assertEqual(speakable("[Flirty roleplay] hi there"), "hi there")
        self.assertEqual(speakable("[Fiction: conspiracy comedy] The pigeons know."), "The pigeons know.")

    def test_every_style_label_is_removed(self):
        from jarvis.personality.styles import STYLES
        for st in STYLES.values():
            if st.label_reply:
                self.assertEqual(speakable(f"{st.label_reply} Hello."), "Hello.", st.id)


class Scripted:
    def __init__(self, text):
        self.text, self.seen = text, []

    def chat(self, messages, max_tokens=512, temperature=0.7):
        self.seen.append(messages)
        return self.text

    def stream(self, messages, max_tokens=512, temperature=0.7):
        self.seen.append(messages)
        for i in range(0, len(self.text), 5):
            yield self.text[i:i + 5]


def make(engine):
    import tempfile
    from jarvis import assistant as A
    from jarvis.config import Config
    from jarvis.memory.personal import PersonalMemory
    a = A.LocalAssistant(Config(data_dir=Path(tempfile.mkdtemp())), guest=True, memory=PersonalMemory.guest(), engine=engine)
    a.now = lambda: AT
    return a


class AssistantWiring(unittest.TestCase):
    def test_time_question_is_answered_from_the_clock_without_the_model(self):
        eng = Scripted("It is 3 o'clock.")
        a = make(eng)
        self.assertEqual(a.reply("what time is it"), "It's 4:52 PM.")
        self.assertEqual("".join(a.reply_stream("what's the date")), "Today is Saturday, October 31, 2026.")
        self.assertEqual(eng.seen, [])

    def test_the_real_time_is_in_the_system_prompt(self):
        eng = Scripted("Fine.")
        a = make(eng)
        a.reply("how long until 6 pm?")
        self.assertIn("4:52 PM", eng.seen[0][0]["content"])

    def test_a_looping_stream_is_stopped(self):
        eng = Scripted("Here is the answer. " + "I will do it now! " * 40)
        a = make(eng)
        out = "".join(a.reply_stream("do the thing please"))
        self.assertLess(out.count("I will do it now!"), 6)
        self.assertIn("repeating", a.last_notice)
        self.assertLess(a.history[-1]["content"].count("I will do it now!"), 6)

    def test_sampling_has_a_repeat_penalty(self):
        from jarvis.inference.gguf_engine import GGUFEngine
        e = GGUFEngine.__new__(GGUFEngine)
        e.sampling = {}
        self.assertEqual(e._gen_kwargs(0.7), {"repeat_penalty": 1.12})
        class Strict:
            def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
                pass
        e.llm = Strict()
        self.assertEqual(e._gen_kwargs(0.7), {})                       # a runtime without the option is not sent it
        e.llm = None
        self.assertEqual(e._gen_kwargs(0), {})
        e.sampling = {"top_p": 0.8, "repeat_penalty": 1.05}
        self.assertEqual(e._gen_kwargs(0.7), {"top_p": 0.8, "repeat_penalty": 1.05})


if __name__ == "__main__":
    unittest.main()
