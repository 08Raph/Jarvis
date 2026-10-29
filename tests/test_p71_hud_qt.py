"""Home screen HUD rendered with REAL Qt on its OFFSCREEN platform. Runs only where PySide6 is installed (the Windows CI build has it; the authoring sandbox does not, so
these tests were NEVER executed before being committed: they are the first real-Qt evidence the HUD gets, and a failure here is information, not a surprise to hide).

What this proves when it passes: the widget and the Home page construct, paint non-blank frames, react to state and level, stop their timer when hidden, and the
window wires the buttons to the controller (with FAKE audio and a scripted fake model).
What it can NOT prove: how it looks to a person, real screen scaling, a real microphone or speaker, Windows 11 behaviour."""
import importlib
import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_QT = importlib.util.find_spec("PySide6") is not None
if HAVE_QT:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from p17_common import AppCase, Rig, wait_for  # noqa: E402
from jarvis.ui import hud_model as M  # noqa: E402


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed: the real-Qt HUD tests are skipped")
class RealQt(AppCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def image(self, hud):
        hud.resize(400, 400)
        return hud.grab().toImage()

    def colours(self, img):
        return {img.pixel(x, y) for x in range(0, img.width(), 7) for y in range(0, img.height(), 7)}

    def bright(self, img, x0, x1, y0, y1):
        n = 0
        for x in range(x0, x1):
            for y in range(y0, y1):
                c = img.pixelColor(x, y)
                n += (c.red() + c.green() + c.blue()) > 300
        return n

    def make(self, provider=None, **kw):
        from jarvis.ui.hud import HudWidget
        h = HudWidget(provider=provider, **kw)
        self.addCleanup(h.deleteLater)
        return h

    # ------------------------------------------------------------ the widget
    def test_a_frame_is_painted_and_is_not_blank(self):
        h = self.make(lambda: (M.LISTENING, 0.0, None))
        for _ in range(5):
            h.step(0.033)
        img = self.image(h)
        self.assertEqual((img.width(), img.height()), (400, 400))
        self.assertGreater(len(self.colours(img)), 40)                # glow, rings, text: many distinct colours, not one flat fill

    def test_each_state_paints_and_they_do_not_all_look_alike(self):
        shots = {}
        for st in M.STATES:
            h = self.make(lambda st=st: (st, 0.0, 0.1 if st == M.WAKE else None))
            for _ in range(20):
                h.step(0.033)
            img = self.image(h)
            self.assertGreater(len(self.colours(img)), 20, st)
            shots[st] = [img.pixel(x, y) for x in range(0, 400, 25) for y in range(0, 400, 25)]
        self.assertNotEqual(shots[M.LISTENING], shots[M.WAKE])
        self.assertNotEqual(shots[M.LISTENING], shots[M.MUTED])
        self.assertNotEqual(shots[M.THINKING], shots[M.ERROR])

    def test_the_waveform_responds_to_the_level_and_is_flat_without_it(self):
        loud = self.make(lambda: (M.LISTENING, 0.2, None))
        for _ in range(40):
            loud.step(0.033)
        flat = self.make(lambda: (M.LISTENING, 0.0, None))
        for _ in range(40):
            flat.step(0.033)
        a, b = self.image(loud), self.image(flat)
        # the band just outside the core on the horizontal axis: tall bars when loud, only the faint baseline when silent
        self.assertGreater(self.bright(a, 250, 330, 150, 250), self.bright(b, 250, 330, 150, 250))

    def test_a_muted_microphone_stays_flat_even_if_a_level_is_supplied(self):
        h = self.make(lambda: (M.MUTED, 0.9, None))
        for _ in range(40):
            h.step(0.033)
        self.assertEqual(max(h.model.wave.heights()), 0.0)

    def test_state_changes_are_announced_once(self):
        cur = {"s": M.OFF}
        h = self.make(lambda: (cur["s"], 0.0, None))
        seen = []
        h.stateChanged.connect(seen.append)
        h.step(0.033)
        cur["s"] = M.LISTENING
        h.step(0.033)
        h.step(0.033)
        cur["s"] = M.THINKING
        h.step(0.033)
        self.assertEqual(seen, [M.LISTENING, M.THINKING])

    def test_a_broken_provider_neither_raises_nor_invents_a_state(self):
        def boom():
            raise RuntimeError("x")
        h = self.make(boom)
        h.step(0.033)
        self.assertEqual(h.state, M.OFF)
        self.image(h)

    def test_the_timer_runs_only_while_the_widget_is_visible(self):
        h = self.make(lambda: (M.LISTENING, 0.0, None))
        self.assertFalse(h.timer.isActive())
        h.show()
        self.app.processEvents()
        self.assertTrue(h.timer.isActive())
        self.assertEqual(h.timer.interval(), 66)                      # starts in the quiet state (OFF) until the first frame reads the provider
        h.step(0.033)
        self.assertEqual(h.timer.interval(), 33)
        h.hide()
        self.app.processEvents()
        self.assertFalse(h.timer.isActive())
        h.close()

    def test_reduce_motion_slows_the_frame_rate(self):
        h = self.make(lambda: (M.THINKING, 0.0, None), reduce_motion=True)
        h.show()
        self.app.processEvents()
        h.step(0.033)
        self.assertEqual(h.timer.interval(), 500)
        h.close()

    def test_the_timer_drives_real_frames(self):
        h = self.make(lambda: (M.LISTENING, 0.0, None))
        h.show()
        self.assertTrue(wait_for(lambda: (self.app.processEvents() or True) and h.frames >= 3, 3.0))
        h.close()

    # ------------------------------------------------------------ the window
    def real_window_module(self):
        """Import `jarvis.ui.window` fresh against the REAL PySide6. Several earlier test modules import it against stand-in Qt modules (p34, p42, p45, p50, p70) and
        can leave that module cached in sys.modules or bound on the `jarvis.ui` package, so `from jarvis.ui import window` could hand this test a stand-in (CI runs
        40814195606 and 40815815932). Drop any cached copy, import again, and refuse to continue if the result is not built on the real Qt."""
        import jarvis.ui as pkg
        from unittest import mock
        import PySide6.QtWidgets as real_widgets
        self.assertFalse(isinstance(real_widgets, mock.Mock), "PySide6.QtWidgets in sys.modules is a stand-in, not the real Qt")
        for name in ("window", "hud"):                       # window.py imports hud at import time, so a stand-in hud would be inherited too
            sys.modules.pop("jarvis.ui." + name, None)
            if name in vars(pkg):
                delattr(pkg, name)
        W = importlib.import_module("jarvis.ui.window")
        self.assertIs(W.HudWidget, importlib.import_module("jarvis.ui.hud").HudWidget)
        self.assertFalse(isinstance(W.HudWidget, mock.Mock), "jarvis.ui.hud was imported against a stand-in Qt")
        self.assertIs(W.QApplication, real_widgets.QApplication, "jarvis.ui.window was not built on the real PySide6")
        return W

    def window(self, rig=None, **kw):
        from jarvis.app.controller import AppController
        W = self.real_window_module()
        if rig is None:
            self.install_model()
            ctl = AppController(self.cfg, simulate=True, **kw)
            self.addCleanup(ctl.shutdown, 2.0)
        else:
            ctl = rig.c
        bridge = W._Bridge()
        win = W.MainWindow(ctl, bridge)
        self.addCleanup(win.deleteLater)
        win.show()
        self.app.processEvents()
        self.addCleanup(win.hide)                  # not close(): closeEvent can open a modal question if a task is running, which would hang a headless test
        return win, ctl

    def test_home_is_the_first_page_and_shows_voice_off_and_mic_off_by_default(self):
        win, ctl = self.window()
        self.assertEqual(win.nav.item(0).text(), "Home")
        self.assertEqual(win.stack.currentIndex(), 0)
        win._refresh_home()
        self.assertEqual(win.home_mic.text(), "MIC OFF")
        self.assertEqual(win.home_label.text(), "VOICE OFF")
        self.assertEqual(win.home_toggle.text(), "Turn hands-free ON")
        self.assertFalse(win.home_mute.isEnabled())
        self.assertTrue(win.home_timer.isActive())
        win.nav.setCurrentRow(1)
        self.assertFalse(win.home_timer.isActive())                    # the 1-second refresh stops when Home is not the page shown
        win.nav.setCurrentRow(0)
        self.assertTrue(win.home_timer.isActive())

    def test_every_page_still_builds_and_refreshes_with_home_added(self):
        win, _ = self.window()
        from jarvis.ui import window as W
        self.assertEqual([win.nav.item(i).text() for i in range(win.nav.count())], list(W.PAGES))
        for n in W.PAGES:
            getattr(win, "_refresh_" + W._slug(n))()

    def test_the_home_screen_follows_a_fake_microphone_end_to_end(self):
        rig = Rig(self, mode="off")                                      # hands-free is OFF until the button is pressed
        win, ctl = self.window(rig)
        self.assertEqual(win.home_label.text(), "VOICE OFF")
        win.home_toggle.click()                                         # the button the user presses once
        self.assertTrue(wait_for(lambda: (self.app.processEvents() or True) and rig.state()["state"] == "Listening", 6, ctl))
        win._refresh_home()
        self.assertEqual((win.home_mic.text(), win.home_label.text()), ("MIC ON", "LISTENING…"))
        self.assertEqual(win.home_toggle.text(), "Turn hands-free OFF")
        for _ in range(3):
            win.hud.step(0.033)
        self.assertEqual(win.hud.state, M.LISTENING)
        rig.mic.loud = 100000
        self.assertTrue(wait_for(lambda: (win.hud.step(0.033) or True) and win.hud.model.level.value > 0.2, 5, ctl), "the HUD level never followed the microphone")
        rig.mic.loud = 0
        win.home_mute.click()
        self.assertTrue(wait_for(lambda: (self.app.processEvents() or True) and rig.state()["state"] == "Muted", 6, ctl))
        win._refresh_home()
        self.assertEqual((win.home_mic.text(), win.home_label.text(), win.home_mute.text()), ("MIC OFF · muted", "MUTED", "Unmute microphone"))
        self.assertFalse(rig.mic.is_open)
        win.hud.step(0.033)
        self.assertEqual(win.hud.state, M.MUTED)
        win.home_mute.click()
        self.assertTrue(wait_for(lambda: (self.app.processEvents() or True) and rig.state()["state"] == "Listening", 6, ctl))
        win.home_toggle.click()                                         # turn hands-free OFF: microphone and thread are released
        self.assertTrue(wait_for(lambda: (self.app.processEvents() or True) and rig.state()["state"] == "Off", 6, ctl))
        self.assertFalse(rig.mic.is_open)
        self.assertEqual(rig.voice_threads_after_exit(), [])

    def test_a_missing_speech_model_is_shown_on_the_home_screen_with_the_reason(self):
        from p17_common import ScriptRec
        rig = Rig(self, rec=ScriptRec(fail_preload="Vosk model folder not found: C:\\nope"))
        win, ctl = self.window(rig)
        ctl.voice_autostart()
        self.assertTrue(wait_for(lambda: (self.app.processEvents() or True) and rig.state()["state"] == "Unavailable", 6, ctl))
        win._refresh_home()
        self.assertEqual(win.home_label.text(), "VOICE ERROR")
        self.assertIn("Vosk model folder not found", win.home_sub.text())
        self.assertFalse(win.home_retry.isHidden())

    def test_the_home_page_renders_to_an_image_with_every_panel(self):
        win, _ = self.window()
        win.resize(1100, 720)
        win._refresh_home()
        self.app.processEvents()
        img = win.pages["Home"].grab().toImage()
        # the page fills the area beside the navigation rail (CI run 40817425745 measured 890 px in a 1100 px window; a fixed 900 was a miscalculated guess)
        self.assertGreaterEqual(img.width(), win.width() - win.nav.width() - 40)
        self.assertGreaterEqual(img.height(), 400)
        self.assertGreater(len(self.colours(img)), 50)
        for w in (win.home_label, win.home_act, win.home_recent, win.home_local, win.home_stages, win.hud):
            self.assertTrue(w.isVisible(), w.accessibleName() or w.objectName())
        self.assertEqual(sorted(win.home_bars), ["cpu", "disk", "ram"])


if __name__ == "__main__":
    unittest.main()
