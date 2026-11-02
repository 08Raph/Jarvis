import os, sys, tempfile, unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from jarvis.app.toolbox import Toolbox
from test_p101_lifestyle import FakeHost

BASE = datetime(2026, 11, 2, 13, 10).timestamp()           # a Monday


class T(unittest.TestCase):
    def setUp(self):
        self.now = [BASE]
        self.dir = Path(tempfile.mkdtemp())
        self.host = FakeHost()
        self.tb = Toolbox(self.dir / "p", now=lambda: self.now[0], host=self.host)
        self.opened = []

        def runner(s):
            if s.startswith("open "):
                self.opened.append(s[5:])
                return "I couldn't find “x”." if "zzz" in s else f"Opened {s[5:]}."
            return self.tb.handle(s) or f"(ran {s})"
        self.tb.step_runner = runner

    def test_learns_nickname_from_a_failed_launch(self):
        self.tb.note_launch_failure("cs 2")
        r = self.tb.handle("it's Counter-Strike 2")
        self.assertIn("now means Counter-Strike 2", r)
        self.assertEqual(self.tb.apply_aliases("open cs 2"), "open Counter-Strike 2")
        self.assertEqual(self.tb.apply_aliases("tell me about cs 2"), "tell me about cs 2")        # only launch commands are rewritten

    def test_wrong_guess_is_not_saved(self):
        self.tb.note_launch_failure("cs 2")
        self.assertIn("couldn't find", self.tb.handle("it's zzz"))
        self.assertEqual(self.tb.apply_aliases("open cs 2"), "open cs 2")

    def test_old_failure_is_forgotten(self):
        self.tb.note_launch_failure("cs 2")
        self.now[0] += 400
        self.assertIsNone(self.tb.handle("it's Counter-Strike 2"))

    def test_alias_survives_restart_and_can_be_forgotten(self):
        self.tb.handle("when i say cs two open Counter-Strike 2")
        again = Toolbox(self.dir / "p", now=lambda: self.now[0], host=self.host)
        self.assertEqual(again.apply_aliases("open cs two"), "open Counter-Strike 2")
        self.assertIn("Forgot", again.handle("forget the nickname cs two"))
        self.assertEqual(again.apply_aliases("open cs two"), "open cs two")

    def test_corrections_for_the_microphone(self):
        self.tb.handle("when i say see ess two it means CS2")
        self.assertEqual(self.tb.apply_corrections("open see ess two now"), "open CS2 now")
        self.assertEqual(self.tb.apply_corrections("seesaw"), "seesaw")

    def test_setup_check(self):
        self.assertIn("all 5 checks passed", self.tb.handle("check my setup"))
        self.tb.probes = {"Mic": lambda: (False, "no microphone found")}
        r = self.tb.handle("test jarvis")
        self.assertIn("5 of 6 passed", r)
        self.assertIn("Mic: no microphone found", r)

    def test_setup_check_survives_a_crashing_probe(self):
        def boom():
            raise RuntimeError("kaput")
        self.tb.probes = {"Bad": boom}
        self.assertIn("Bad: kaput", self.tb.handle("check my setup"))

    def test_loan_and_savings(self):
        self.assertIn("386.66 a month", self.tb.handle("payment on a 20000 loan at 6% for 5 years"))
        self.assertIn("31,056", self.tb.handle("if i save $200 a month at 5% for 10 years"))
        self.assertIn("1,628.89", self.tb.handle("compound interest on 1000 at 5% for 10 years"))
        self.assertIn("59 days", self.tb.handle("days between 2026-01-01 and 2026-03-01"))

    def test_currency_uses_only_your_rates(self):
        self.assertIn("don't have a rate", self.tb.handle("convert 100 usd to cad"))
        self.tb.handle("set rate usd to cad 1.35")
        self.assertIn("135.00 CAD", self.tb.handle("convert 100 usd to cad"))
        self.assertIn("74.07 USD", self.tb.handle("convert 100 cad to usd"))                    # the inverse works too

    def test_text_tasks(self):
        self.assertIn("Summarize", self.tb.text_task("Summarize my clipboard"))
        self.assertIn("French", self.tb.text_task("translate my clipboard to french"))
        self.assertIn("grammar", self.tb.text_task("fix the grammar of what I copied"))
        self.assertIsNone(self.tb.text_task("summarize the roman empire"))

    def test_find_files(self):
        home = self.dir / "h" / "Downloads"
        home.mkdir(parents=True)
        pdf = home / "Invoice March.pdf"
        pdf.write_text("x")
        old = home / "old.pdf"
        old.write_text("x")
        os_t = BASE - 10 * 86400
        os.utime(old, (os_t, os_t))
        os.utime(pdf, (BASE - 600, BASE - 600))
        (home / "pic.png").write_text("y")
        self.tb.roots = [home]
        r = self.tb.handle("find the pdf i downloaded today")
        self.assertIn("Invoice March.pdf", r)
        self.assertNotIn("old.pdf", r)
        self.assertIn("old.pdf", self.tb.handle("find my pdf files"))
        self.assertIn("didn't find", self.tb.handle("find the video i made yesterday"))
        self.assertIn("Downloads", self.tb.handle("what's taking up my space"))

    def test_find_does_not_hijack_other_sentences(self):
        self.assertIsNone(self.tb.handle("find out why the sky is blue"))

    def test_auto_playtime(self):
        running = {"cs2.exe"}
        self.tb.procs = lambda: set(running)
        self.assertIn("Automatic playtime is on", self.tb.handle("turn on auto playtime"))
        self.tb.autoplay_check()
        self.now[0] += 1800
        running.clear()
        self.tb.autoplay_check()
        self.assertIn("CS2 30 minutes", self.tb.handle("playtime"))

    def test_auto_playtime_respects_a_manual_session(self):
        self.tb.handle("i'm playing Minecraft")
        self.tb.procs = lambda: {"cs2.exe"}
        self.tb.handle("turn on auto playtime")
        self.tb.autoplay_check()
        self.assertEqual(len([s for s in self.tb._state()["playtime"] if s["end"] is None]), 1)

    def test_custom_game_program(self):
        self.tb.handle("track program mygame.exe as My Game")
        self.tb.procs = lambda: {"mygame.exe"}
        self.tb.handle("turn on auto playtime")
        self.tb.autoplay_check()
        self.assertEqual(self.tb._state()["playtime"][-1]["game"], "My Game")

    def test_followups(self):
        self.tb.handle("time in tokyo")
        self.assertIn("Paris", self.tb.handle("and in paris"))
        self.tb.handle("days until christmas")
        self.assertIn("New Year", self.tb.handle("and new year"))
        self.tb.handle("is 97 prime")
        self.assertIn("91 is not prime", self.tb.handle("and 91"))
        self.assertIsNone(self.tb.handle("and then what"))

    def test_followup_expires(self):
        self.tb.handle("time in tokyo")
        self.now[0] += 700
        self.assertIsNone(self.tb.handle("and in paris"))

    def test_scheduled_scene(self):
        self.assertIn("work mode every weekday at 8:00 AM", self.tb.handle("every weekday at 8 am run work mode"))
        self.assertEqual(self.tb.take_due_scenes(), [])
        self.now[0] = datetime(2026, 11, 3, 8, 0, 5).timestamp()
        self.tb.tick()
        self.assertEqual(self.tb.take_due_scenes(), ["work"])
        self.assertEqual(self.tb.take_due_scenes(), [])
        self.assertIn("work mode", self.tb.handle("list repeating reminders"))

    def test_old_scene_is_not_run_after_a_long_gap(self):
        self.tb.handle("run movie mode every day at 9 pm")
        self.now[0] += 3 * 86400
        self.tb.tick()
        self.assertEqual(self.tb.take_due_scenes(), [])                    # Jarvis was closed: no surprise movie mode

    def test_unknown_scene_is_refused(self):
        self.assertIn("don't have a scene", self.tb.handle("every day at 8 am run party mode"))

    def test_wake_word_and_speed(self):
        got = {}
        self.tb.actions = {"wake": lambda p: got.update(wake=p), "rate_get": lambda: 1.0, "rate_set": lambda r: got.update(rate=r)}
        self.assertIn("hey computer", self.tb.handle("change wake word to hey computer"))
        self.assertEqual(got["wake"], "hey computer")
        self.assertIn("0.8x", self.tb.handle("speak slower"))
        self.assertEqual(got["rate"], 0.8)
        self.tb.handle("speak normally")
        self.assertEqual(got["rate"], 1.0)
        self.assertIn("plain words", self.tb.handle("change wake word to !!!"))

    def test_not_ours(self):
        for s in ("remember that my cat is called Miso", "I'm playing guitar later", "tell me a story about a dragon", "check the weather", "every dog has its day"):
            self.assertIsNone(self.tb.handle(s))


if __name__ == "__main__":
    unittest.main()
