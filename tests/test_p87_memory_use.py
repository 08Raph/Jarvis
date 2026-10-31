"""Memory that is used and easy to fill: facts Jarvis hears are kept, always offered to the model, and notes can be typed in."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.memory import interaction as I  # noqa: E402
from jarvis.memory.personal import PersonalMemory  # noqa: E402


class About(unittest.TestCase):
    def test_plain_facts_are_recognised(self):
        self.assertEqual(I._about_fact("I live in Toronto"), ("about.location", "Lives in Toronto."))
        self.assertEqual(I._about_fact("I work as a nurse.")[1], "Works as/at nurse.")
        self.assertEqual(I._about_fact("i love pizza")[1], "Likes pizza.")
        self.assertEqual(I._about_fact("I hate mondays")[1], "Dislikes mondays.")
        k, t = I._about_fact("my favorite colour is dark blue")
        self.assertEqual(t, "Favorite colour: dark blue.")
        self.assertTrue(k.startswith("about.favorite."))

    def test_questions_sensitive_and_vague_are_skipped(self):
        for t in ("do I live in Toronto?", "I like it when you answer fast", "I like that", "I live in a house with my diagnosed condition", "I love going to church",
                  "I work for a political party", "I hate how this works", "I'm a bit tired", "I like to move it move it every single day okay"):
            self.assertIsNone(I._about_fact(t), t)


class Agent(unittest.TestCase):
    def setUp(self):
        self.mem = PersonalMemory.open(Path(tempfile.mkdtemp()) / "m.db")
        self.agent = I.MemoryAgent(self.mem, None)

    def tearDown(self):
        self.mem.close()

    def test_saved_announced_and_the_chat_continues(self):
        out = self.agent.process("I live in Toronto")
        self.assertFalse(out.handled)                       # the model still answers
        self.assertEqual(out.kind, "about_saved")
        self.assertIn("memory #", out.notice)
        self.assertEqual([r["text"] for r in self.mem.list("approved_context")], ["Lives in Toronto."])

    def test_a_new_location_replaces_the_old_one(self):
        self.agent.process("I live in Toronto")
        self.agent.process("I live in Montreal")
        self.assertEqual([r["text"] for r in self.mem.list("approved_context")], ["Lives in Montreal."])

    def test_facts_are_always_in_the_prompt_even_for_unrelated_questions(self):
        self.agent.process("I live in Toronto")
        self.agent.process("I love pizza")
        block = self.mem.context_block("what is two plus two")
        self.assertIn("Lives in Toronto.", block)
        self.assertIn("Likes pizza.", block)

    def test_an_ordinary_unrelated_note_still_stays_out(self):
        self.mem.add("approved_context", "The shed key is under the blue pot.", source="user_said")
        self.assertNotIn("shed", self.mem.context_block("what is two plus two"))

    def test_secrets_are_refused(self):
        out = self.agent.process("my favorite password is hunter2hunter2")
        self.assertEqual(self.mem.list("approved_context"), [])


class Controller(AppCase):
    def test_typed_note_is_saved_and_listed(self):
        c = AppController(self.cfg, simulate=True, dispatch=lambda fn: fn())
        self.addCleanup(c.shutdown, 2.0)
        row = c.memory_add("I prefer metric units")
        self.assertEqual(row["category"], "approved_context")
        self.assertIn("I prefer metric units", [r["text"] for r in c.memory_items()])
        with self.assertRaises(Exception):
            c.memory_add("my api key is sk-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF")


if __name__ == "__main__":
    unittest.main()
