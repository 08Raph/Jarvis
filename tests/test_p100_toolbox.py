import sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.app.toolbox import Toolbox, parse_duration, safe_eval
from jarvis.voice.speakable import speakable, shorten


class T(unittest.TestCase):
    def setUp(self):
        self.now = [1_800_000_000.0]
        self.dir = Path(tempfile.mkdtemp())
        self.tb = Toolbox(self.dir, now=lambda: self.now[0])

    def test_durations(self):
        self.assertEqual(parse_duration("1 hour 30 minutes"), 5400)
        self.assertEqual(parse_duration("half an hour"), 1800)
        self.assertEqual(parse_duration("90 seconds"), 90)

    def test_timer_fires_and_survives_restart(self):
        self.assertIn("10 minutes", self.tb.handle("set a timer for 10 minutes"))
        self.assertEqual(self.tb.tick(), [])
        self.now[0] += 601
        again = Toolbox(self.dir, now=lambda: self.now[0])
        self.assertTrue(again.tick()[0].startswith("Timer done"))
        self.assertEqual(again.tick(), [])

    def test_cancel_and_list(self):
        self.tb.handle("timer 5 minutes")
        self.assertIn("1", self.tb.handle("list timers"))
        self.assertIn("Cancelled", self.tb.handle("cancel all timers"))

    def test_reminder(self):
        self.assertIn("call mom", self.tb.handle("remind me to call mom in 20 minutes"))
        self.now[0] += 1300
        self.assertIn("Reminder: call mom", self.tb.tick()[0])

    def test_math(self):
        self.assertEqual(self.tb.handle("what is 15% of 240"), "36.")
        self.assertEqual(self.tb.handle("calculate 12*(3+4)"), "84.")
        self.assertEqual(self.tb.handle("what's 2 to the power of 10"), "1,024.")
        self.assertIn("zero", self.tb.handle("10 divided by 0"))
        self.assertIsNone(self.tb.handle("what is the capital of france"))
        with self.assertRaises(Exception):
            safe_eval("__import__('os').system('x')")

    def test_units(self):
        self.assertIn("8.0467", self.tb.handle("convert 5 miles to km"))
        self.assertIn("22.2222", self.tb.handle("72 f to c"))
        self.assertIn("can't convert", self.tb.handle("convert 5 kg to km"))

    def test_lists_notes(self):
        self.tb.handle("add milk and eggs to my shopping list")
        self.assertIn("milk, eggs", self.tb.handle("show my shopping list"))
        self.tb.handle("note: buy stamps")
        self.assertIn("buy stamps", self.tb.handle("my notes"))

    def test_guest_keeps_nothing(self):
        g = Toolbox(self.dir / "g", guest=True, now=lambda: self.now[0])
        g.handle("note: secret")
        self.assertFalse((self.dir / "g").exists())

    def test_not_ours(self):
        for s in ("hello there", "open roblox", "tell me about the roman empire"):
            self.assertIsNone(self.tb.handle(s))


class Lists(unittest.TestCase):
    def test_all_steps_spoken(self):
        txt = "Here is how:\n\n1. Open Settings.\n\n2. Click Network.\n\n3. Turn it on.\n\n- one\n- two"
        sp = shorten(speakable(txt), "normal")
        for w in ("Step 1", "Step 2", "Step 3", "one.", "two."):
            self.assertIn(w, sp)


if __name__ == "__main__":
    unittest.main()
