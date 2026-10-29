"""Home screen HUD and always-on voice indicators: everything that does NOT need Qt.

EVIDENCE LABELS
  REAL        real code on Linux: the HUD state model and animation maths, the real VoiceSession/VoiceService worker threads, the real controller.
  FAKE AUDIO  SIMULATED: a fake microphone, recogniser and speaker (tests/p17_common.py) drive the real voice service. They prove our logic, never a real
              microphone, a Vosk model or a loudspeaker.
  STAND-IN Qt `jarvis.ui.window` is imported against recording stand-in widgets: this checks that the Home page's wiring runs and what text it sets. It cannot
              show that anything draws, how it looks, or that a real Qt call name is right (tests/test_p71_hud_qt.py renders it for real where PySide6 exists).
  NOT RUN     a person looking at the window; a real microphone or speaker; Windows 11."""
import ast
import importlib
import math
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p17_common import AppCase, Rig, ScriptRec, ThreadMic, ThreadSpk, wait_for  # noqa: E402
from p10_common import ROOT  # noqa: E402
from jarvis.app.transcript import Entry  # noqa: E402
from jarvis.ui import hud_model as M  # noqa: E402
from jarvis.voice.settings import VoiceSettings  # noqa: E402

SRC = ROOT / "src" / "jarvis"


def V(state="Listening", **kw):
    d = {"state": state, "mic_open": state == "Listening", "enabled": state != "Off", "failure_kind": "", "phase": "", "detail": "", "wake_phrase": "hey jarvis",
         "mic_muted": False, "can_retry": False}
    d.update(kw)
    return d


def L(phase="LISTENING", mic_open=True, level=0.0, wake_age=None):
    return {"alive": True, "mic_open": mic_open, "recognizer_loaded": mic_open, "phase": phase, "level": level, "wake_age": wake_age}


class ResolveStates(unittest.TestCase):
    """REAL: the table from measured facts to the state the HUD shows."""

    def test_every_state_is_reachable_from_some_input(self):
        got = {
            M.resolve_state(V("Off"), L("", False)),
            M.resolve_state(V("Starting"), L("", False)),
            M.resolve_state(V(), L("LISTENING")),
            M.resolve_state(V(), L("CAPTURING", wake_age=0.2)),
            M.resolve_state(V(), L("CAPTURING", wake_age=5.0)),
            M.resolve_state(V(), L("FOLLOW_UP")),
            M.resolve_state(V("Processing", phase="THINKING"), L("THINKING")),
            M.resolve_state(V("Speaking", phase="SPEAKING"), L("SPEAKING")),
            M.resolve_state(V("Muted", mic_open=False), L("MUTED", False)),
            M.resolve_state(V("Unavailable", failure_kind="microphone"), L("", False)),
            M.resolve_state(V("Unavailable", failure_kind="model"), L("", False)),
        }
        self.assertEqual(got, set(M.STATES))

    def test_listening_is_claimed_only_while_the_microphone_is_measured_open(self):
        self.assertEqual(M.resolve_state(V("Listening"), L("LISTENING", mic_open=False)), M.OFF)
        self.assertEqual(M.resolve_state(V("Listening", mic_open=False), {}), M.OFF)
        self.assertEqual(M.resolve_state(V("Listening"), L("LISTENING", mic_open=True)), M.LISTENING)

    def test_nothing_known_means_off_not_listening(self):
        self.assertEqual(M.resolve_state(None, None), M.OFF)
        self.assertEqual(M.resolve_state({}, {}), M.OFF)
        self.assertEqual(M.resolve_state("garbage", 7), M.OFF)

    def test_wake_flash_is_brief(self):
        self.assertEqual(M.resolve_state(V(), L("CAPTURING", wake_age=0.0)), M.WAKE)
        self.assertEqual(M.resolve_state(V(), L("CAPTURING", wake_age=M.WAKE_FLASH_SECONDS - 0.01)), M.WAKE)
        self.assertEqual(M.resolve_state(V(), L("CAPTURING", wake_age=M.WAKE_FLASH_SECONDS + 0.01)), M.CAPTURING)
        self.assertEqual(M.resolve_state(V(), L("LISTENING", wake_age=None)), M.LISTENING)

    def test_real_activity_outranks_the_listening_states(self):
        self.assertEqual(M.resolve_state(V(), L("LISTENING"), busy=True), M.THINKING)           # a typed message is being answered
        self.assertEqual(M.resolve_state(V(), L("LISTENING"), speaking=True), M.SPEAKING)        # the shared speaker reports it is speaking
        self.assertEqual(M.resolve_state(V("Off"), {}, busy=True), M.THINKING)                   # works with voice off too
        self.assertEqual(M.resolve_state(V("Unavailable", failure_kind="microphone"), {}, busy=True), M.THINKING)

    def test_busy_false_and_not_speaking_never_shows_thinking_or_speaking(self):
        for v in (V("Off"), V(), V("Muted", mic_open=False), V("Unavailable")):
            self.assertNotIn(M.resolve_state(v, L("LISTENING", v.get("mic_open", False))), (M.THINKING, M.SPEAKING))

    def test_a_microphone_that_is_retrying_is_unavailable_not_starting(self):
        self.assertEqual(M.resolve_state(V("Starting", mic_open=False, phase="RECOVERING"), L("RECOVERING", False)), M.MIC_UNAVAILABLE)
        self.assertEqual(M.resolve_state(V("Starting", mic_open=False), L("", False)), M.STARTING)

    def test_a_missing_speech_model_is_a_voice_error_not_a_microphone_fault(self):
        self.assertEqual(M.resolve_state(V("Unavailable", failure_kind="model"), {}), M.ERROR)
        self.assertEqual(M.resolve_state(V("Unavailable", failure_kind="other"), {}), M.ERROR)
        self.assertEqual(M.resolve_state(V("Unavailable", failure_kind="microphone"), {}), M.MIC_UNAVAILABLE)


class Describe(unittest.TestCase):
    def test_every_state_has_a_label_a_detail_and_a_look(self):
        for st in M.STATES:
            v = M.describe(st, V(detail="because"), L())
            self.assertTrue(v.label and v.sub and v.mic_text and v.wave_caption, st)
            self.assertIn(st, M.LOOKS)
            self.assertIn(v.accent, M.HUD)
            self.assertIn(v.mic_tone, M.HUD)

    def test_mic_on_is_printed_only_when_the_stream_is_measured_open(self):
        for st in M.STATES:
            for open_ in (False, True):
                v = M.describe(st, V(mic_open=open_), L(mic_open=open_))
                self.assertEqual(v.mic_open, open_)
                self.assertEqual(v.mic_text.startswith("MIC ON"), open_, (st, open_))

    def test_the_microphone_chip_says_why_it_is_off(self):
        self.assertIn("muted", M.describe(M.MUTED, V("Muted", mic_open=False), L(mic_open=False)).mic_text)
        self.assertIn("unavailable", M.describe(M.MIC_UNAVAILABLE, V("Unavailable"), L(mic_open=False)).mic_text)
        self.assertIn("starting", M.describe(M.STARTING, V("Starting"), L(mic_open=False)).mic_text)
        self.assertEqual(M.describe(M.OFF, V("Off"), L(mic_open=False)).mic_text, "MIC OFF")

    def test_the_waveform_caption_never_calls_a_pattern_audio(self):
        self.assertIn("not audio", M.describe(M.THINKING, V(), L()).wave_caption)
        self.assertIn("not audio", M.describe(M.SPEAKING, V(), L()).wave_caption)
        self.assertEqual(M.describe(M.LISTENING, V(), L()).wave_caption, "live microphone level")
        self.assertEqual(M.describe(M.MUTED, V("Muted", mic_open=False), L(mic_open=False)).wave_caption, "microphone closed")

    def test_the_wake_phrase_shown_is_the_saved_one_and_a_long_detail_is_cut(self):
        v = M.describe(M.LISTENING, V(wake_phrase="okay assistant"), L())
        self.assertIn("okay assistant", v.sub)
        e = M.describe(M.ERROR, V("Unavailable", detail="x" * 500), L(mic_open=False))
        self.assertLessEqual(len(e.sub), 150)
        self.assertEqual(M.describe(M.THINKING, V(), L(), loading=True).label, "LOADING MODEL")
        self.assertEqual(M.describe(M.THINKING, V(), L()).label, "THINKING")

    def test_the_stage_strip_names_only_what_is_true(self):
        self.assertEqual(M.stage_active(M.LISTENING), "LISTENING")
        self.assertEqual(M.stage_active(M.WAKE), "LISTENING")
        self.assertEqual(M.stage_active(M.THINKING), "THINKING")
        self.assertEqual(M.stage_active(M.SPEAKING), "SPEAKING")
        for st in (M.OFF, M.MUTED, M.STARTING, M.MIC_UNAVAILABLE, M.ERROR):
            self.assertIsNone(M.stage_active(st), st)


class Animation(unittest.TestCase):
    def run_hud(self, state, rms, seconds=1.0, wake_age=None, dt=0.033):
        h = M.Hud()
        for _ in range(int(seconds / dt)):
            h.advance(dt, state, rms, wake_age)
        return h

    def test_display_level_is_clamped_and_gates_room_hiss(self):
        self.assertEqual(M.display_level(0.0), 0.0)
        self.assertEqual(M.display_level(0.0005), 0.0)
        self.assertEqual(M.display_level(float("nan")), 0.0)
        self.assertEqual(M.display_level(None), 0.0)
        self.assertEqual(M.display_level(5.0), 1.0)
        self.assertLess(M.display_level(0.01), M.display_level(0.05))
        self.assertTrue(0.0 < M.display_level(0.02) < 1.0)

    def test_smoother_rises_fast_and_falls_slowly_and_settles_at_zero(self):
        s = M.Smoother()
        up = s.update(1.0, 0.05)
        self.assertGreater(up, 0.5)
        down = s.update(0.0, 0.05)
        self.assertGreater(down, up * 0.5)
        for _ in range(200):
            s.update(0.0, 0.05)
        self.assertEqual(s.value, 0.0)

    def test_the_waveform_follows_the_real_level_only_in_the_listening_states(self):
        loud = self.run_hud(M.LISTENING, 0.2)
        self.assertGreater(max(loud.wave.heights()), 0.3)
        quiet = self.run_hud(M.LISTENING, 0.0)
        self.assertEqual(max(quiet.wave.heights()), 0.0)
        for st in (M.MUTED, M.OFF, M.MIC_UNAVAILABLE, M.ERROR, M.STARTING):       # a closed microphone is flat even if a stale level is passed in
            self.assertEqual(max(self.run_hud(st, 0.5).wave.heights()), 0.0, st)

    def test_thinking_and_speaking_use_a_pattern_that_ignores_the_microphone(self):
        a = self.run_hud(M.THINKING, 0.0, 2.0)
        b = self.run_hud(M.THINKING, 0.9, 2.0)
        self.assertEqual(a.wave.heights(), b.wave.heights())
        self.assertGreater(max(a.wave.heights()), 0.0)
        sp = self.run_hud(M.SPEAKING, 0.0, 2.0)
        self.assertGreater(max(sp.wave.heights()), 0.1)

    def test_the_waveform_falls_to_nothing_after_the_microphone_closes(self):
        h = self.run_hud(M.LISTENING, 0.2)
        self.assertGreater(max(h.wave.heights()), 0.0)
        for _ in range(100):
            h.advance(0.033, M.MUTED, 0.2, None)
        self.assertEqual(max(h.wave.heights()), 0.0)

    def test_rings_turn_faster_when_thinking_than_when_idle_listening(self):
        self.assertGreater(M.LOOKS[M.THINKING].ring_dps, 5 * M.LOOKS[M.LISTENING].ring_dps)
        self.assertLessEqual(M.LOOKS[M.LISTENING].ring_dps, 6.0)         # slow and subtle
        self.assertLessEqual(M.LOOKS[M.LISTENING].pulse_hz, 0.3)
        self.assertGreater(M.LOOKS[M.WAKE].ring_dps, M.LOOKS[M.CAPTURING].ring_dps)       # the wake response is distinct
        self.assertNotEqual(M.LOOKS[M.WAKE].accent, M.LOOKS[M.LISTENING].accent)
        a, b = self.run_hud(M.LISTENING, 0.0, 2.0), self.run_hud(M.THINKING, 0.0, 2.0)
        self.assertGreater(b.ring_deg, 5 * a.ring_deg)

    def test_the_wake_flash_decays_and_only_exists_in_the_wake_state(self):
        self.assertAlmostEqual(self.run_hud(M.WAKE, 0.0, 0.1, wake_age=0.0).flash, 1.0, places=6)
        self.assertAlmostEqual(self.run_hud(M.WAKE, 0.0, 0.1, wake_age=M.WAKE_FLASH_SECONDS).flash, 0.0, places=6)
        self.assertEqual(self.run_hud(M.LISTENING, 0.0, 0.1, wake_age=0.0).flash, 0.0)

    def test_core_scale_stays_in_a_small_band(self):
        for st in M.STATES:
            h = self.run_hud(st, 1.0, 1.0, wake_age=0.0)
            self.assertTrue(1.0 <= h.core_scale() <= 1.35, (st, h.core_scale()))

    def test_ring_speeds_are_multiples_that_make_the_wrap_around_seamless(self):
        for _r, _w, mult, seg, arc in M.RINGS:
            self.assertEqual(round(mult * 43200) % 360, 0, mult)
            self.assertTrue(seg >= 1 and 0 < arc <= 360)

    def test_particles_are_deterministic_bounded_and_few(self):
        a, b = M.Particles(seed=3), M.Particles(seed=3)
        a.advance(1.0)
        b.advance(1.0)
        self.assertEqual(a.points(), b.points())
        self.assertLessEqual(len(a.points()), 64)
        for ang, rf, size, alpha in a.points():
            self.assertTrue(0 <= ang < 360 and 0 < rf <= 1 and 0 < size < 4 and 0 <= alpha <= 1)

    def test_huge_or_negative_time_steps_cannot_break_the_clock(self):
        h = M.Hud()
        for dt in (-5.0, 0.0, 1e9, float("inf") if False else 3600.0):
            h.advance(dt, M.LISTENING, 0.1, None)
        for v in h.wave.heights():
            self.assertTrue(0.0 <= v <= 1.0 and math.isfinite(v))
        self.assertTrue(math.isfinite(h.ring_deg) and math.isfinite(h.pulse_phase))

    def test_frame_rate_policy_saves_work(self):
        self.assertEqual(M.frame_interval_ms(M.LISTENING, visible=False), 0)       # hidden or minimised: no animation at all
        self.assertEqual(M.frame_interval_ms(M.THINKING, visible=True, reduce_motion=True), 500)
        self.assertEqual(M.frame_interval_ms(M.MUTED), 66)
        self.assertEqual(M.frame_interval_ms(M.OFF), 66)
        for st in (M.LISTENING, M.WAKE, M.CAPTURING, M.THINKING, M.SPEAKING):
            self.assertEqual(M.frame_interval_ms(st), 33)
        self.assertTrue(M.reduce_motion_requested({"JARVIS_REDUCE_MOTION": "1"}))
        self.assertFalse(M.reduce_motion_requested({}))
        self.assertFalse(M.reduce_motion_requested({"JARVIS_REDUCE_MOTION": "0"}))

    def test_a_frame_is_cheap(self):
        h = M.Hud()
        t0 = time.perf_counter()
        for _ in range(300):
            h.advance(0.033, M.SPEAKING, 0.1, None)
            h.wave.heights()
            h.particles.points()
        self.assertLess((time.perf_counter() - t0) / 300, 0.005)        # a few ms at most, on a slow CI machine


class Palette(unittest.TestCase):
    def test_hud_text_pairs_meet_contrast(self):
        for a, b, got, need in M.contrast_report():
            self.assertGreaterEqual(got, need, f"{a} on {b}")

    def test_nav_rail_pairs_meet_contrast(self):
        for a, b, got, need in M.nav_contrast():
            self.assertGreaterEqual(got, need, f"{a} on {b}")

    def test_style_sheets_are_scoped_and_use_only_palette_colours(self):
        css = M.home_stylesheet()
        self.assertIn("#homePage", css)
        self.assertIn(":focus", css)
        self.assertTrue(M.nav_rail_css().count("QListWidget#nav") >= 3)
        import re
        allowed = {c.lower() for c in list(M.HUD.values()) + list(M.NAV.values())} | {"#12304f", "#102238"}
        for c in re.findall(r"#[0-9a-fA-F]{6}\b", css + M.nav_rail_css()):
            self.assertIn(c.lower(), allowed, c)


class Panels(unittest.TestCase):
    def test_recent_conversation_keeps_the_last_four_and_drops_tool_lines(self):
        es = [Entry("user", "(voice) what time is it"), Entry("tool", "ran a thing"), Entry("assistant", "x" * 300), Entry("system", "info"), Entry("user", "hello"),
              Entry("assistant", "hi"), Entry("user", "again")]
        lines = M.recent_lines(M.recent_from_entries(es))
        self.assertEqual([l["who"] for l in lines], ["Jarvis", "You", "Jarvis", "You"])
        self.assertTrue(all(len(l["text"]) <= 90 for l in lines))
        self.assertEqual(M.recent_lines(M.recent_from_entries([Entry("user", "(voice) open spotify")]))[0]["text"], "open spotify")
        self.assertEqual(M.recent_lines(M.recent_from_entries([])), [])

    def test_privacy_lines_never_say_local_only_when_a_feature_is_on_or_unknown(self):
        head, tone, _t, detail = M.privacy_lines([])
        self.assertEqual(tone, "ok")
        self.assertIn("stay on this PC", detail)
        head, tone, title, detail = M.privacy_lines(["web_research"])
        self.assertEqual(tone, "warn")
        self.assertIn("web_research", head + detail)
        self.assertNotIn("No internet required", detail)
        self.assertIn("never sent", detail)
        head, tone, title, detail = M.privacy_lines(None)
        self.assertNotEqual(tone, "ok")
        self.assertNotIn("stay on this PC", detail)

    def test_quick_info_reports_what_was_measured(self):
        view = M.describe(M.LISTENING, V(), L())
        rows = dict((a, (b, c)) for a, b, c in M.quick_info({"model_ok": True, "model_text": "Installed and checked", "memory_count": 1, "online_on": []}, view))
        self.assertEqual(rows["Memory"][0], "1 saved item")
        self.assertEqual(rows["Microphone"][1], "ok")
        self.assertEqual(rows["Privacy"][0], "no online features on")
        bad = dict((a, (b, c)) for a, b, c in M.quick_info({"model_ok": False, "model_text": "No model installed", "memory_count": None, "guest": True, "online_on": ["a", "b"]}, M.describe(M.OFF)))
        self.assertEqual(bad["Local model"][1], "warn")
        self.assertIn("guest", bad["Memory"][0])
        self.assertEqual(bad["Privacy"], ("online ON: a, b", "warn"))
        self.assertEqual(dict((a, b) for a, b, _c in M.quick_info(None, M.describe(M.OFF)))["Local model"], "unknown")

    def test_resources_without_psutil_say_n_a_instead_of_guessing(self):
        r = M.Resources(load_psutil=False)
        out = r.sample()
        self.assertIsNone(out["cpu"])
        self.assertIsNone(out["ram"])
        self.assertTrue(out["disk"] is None or 0.0 <= out["disk"] <= 100.0)

    def test_resources_with_psutil_ignore_the_first_meaningless_cpu_reading(self):
        ps = mock.Mock()
        ps.cpu_percent.side_effect = [0.0, 37.5, 250.0]
        ps.virtual_memory.return_value = mock.Mock(percent=61.2)
        r = M.Resources(psutil_mod=ps)
        self.assertIsNone(r.sample()["cpu"])
        s2 = r.sample()
        self.assertEqual((s2["cpu"], s2["ram"]), (37.5, 61.2))
        self.assertEqual(r.sample()["cpu"], 100.0)          # clamped

    def test_a_failing_source_blanks_only_its_own_value(self):
        ps = mock.Mock()
        ps.cpu_percent.side_effect = RuntimeError("x")
        ps.virtual_memory.return_value = mock.Mock(percent=10.0)
        out = M.Resources(psutil_mod=ps, disk_path="/definitely/not/a/path").sample()
        self.assertEqual((out["cpu"], out["ram"], out["disk"]), (None, 10.0, None))

    def test_the_clock_text_has_a_time_and_a_date(self):
        t, d = M.clock_text(0)
        self.assertRegex(t, r"^\d{1,2}:\d{2} (AM|PM)$|^\d{1,2}:\d{2}.*$")
        self.assertTrue(d)


class LiveVoice(AppCase):
    """FAKE AUDIO through the REAL VoiceService/VoiceSession threads: the HUD reads what the service really reports."""

    def hud(self, r, **kw):
        live = r.c.voice_live()
        return M.resolve_state(r.state(), live, kw.get("busy", bool(r.c.busy)), kw.get("speaking", False)), live

    def test_voice_off_reads_off_and_live_never_creates_the_service_or_a_microphone(self):
        c = __import__("jarvis.app.controller", fromlist=["AppController"]).AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        self.assertIsNone(c.voice)
        live = c.voice_live()
        self.assertIsNone(c.voice)                                       # asking for the HUD numbers does not create the voice service
        self.assertEqual((live["mic_open"], live["level"], live["wake_age"], live["alive"]), (False, 0.0, None, False))
        self.assertEqual(M.resolve_state(c.voice_state(), live), M.OFF)

    def test_live_reads_no_file(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        with mock.patch.object(VoiceSettings, "load", side_effect=AssertionError("live() must not read voice.json")):
            for _ in range(50):
                r.c.voice_live()

    def test_listening_is_shown_only_once_the_microphone_is_open_and_the_model_loaded(self):
        gate = threading.Event()
        rec = ScriptRec()
        real_preload = rec.preload

        def slow():
            gate.wait(10)
            real_preload()
        rec.preload = slow
        r = Rig(self, rec=rec)
        r.c.voice_autostart()
        self.assertTrue(wait_for(lambda: r.state()["state"] == "Starting", 5, r.c))
        st, live = self.hud(r)
        self.assertEqual(st, M.STARTING)
        self.assertFalse(live["mic_open"])
        gate.set()
        self.assertTrue(r.wait_state("Listening"))
        st, live = self.hud(r)
        self.assertEqual((st, live["mic_open"], live["phase"]), (M.LISTENING, True, "LISTENING"))
        self.assertIsNone(live["wake_age"])

    def test_the_level_follows_the_fake_microphone_and_drops_to_zero_when_it_closes(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        self.assertTrue(wait_for(lambda: r.c.voice_live()["level"] < 0.01, 3, r.c))        # quiet frames: tiny level
        r.mic.loud = 100000
        self.assertTrue(wait_for(lambda: r.c.voice_live()["level"] > 0.2, 5, r.c), "the level never rose")
        r.mic.loud = 0
        r.c.voice_mute()
        self.assertTrue(r.wait_state("Muted"))
        live = r.c.voice_live()
        self.assertEqual((live["mic_open"], live["level"]), (False, 0.0))
        self.assertFalse(r.mic.is_open)                                                      # the stream itself is closed, not just ignored
        self.assertEqual(self.hud(r)[0], M.MUTED)
        self.assertEqual(M.describe(M.MUTED, r.state(), r.c.voice_live()).mic_text, "MIC OFF · muted")

    def test_the_wake_phrase_is_reported_with_its_age_and_the_command_is_captured(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        self.assertIsNone(r.c.voice_live()["wake_age"])
        r.rec.say("wake", "hey jarvis")
        self.assertTrue(wait_for(lambda: r.c.voice_live()["phase"] == "CAPTURING", 5, r.c))
        st, live = self.hud(r)
        age = live["wake_age"]
        self.assertIsNotNone(age)
        self.assertEqual(st, M.WAKE if age < M.WAKE_FLASH_SECONDS else M.CAPTURING)
        self.assertEqual(M.stage_active(st), "LISTENING")

    def test_background_speech_without_the_wake_phrase_is_not_a_command(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.mic.loud = 300
        r.rec.say("dictation", "delete everything now")            # recognised words that arrive without the wake phrase: the wake recogniser never sees dictation
        time.sleep(0.5)
        r.mic.loud = 0
        self.assertEqual(r.c.voice_live()["phase"], "LISTENING")
        self.assertEqual([e for e in r.c.transcript.entries if e.kind == "user"], [])
        self.assertIsNone(r.c.voice_live()["wake_age"])

    def test_thinking_is_shown_while_the_reply_is_produced_and_speaking_while_it_is_spoken(self):
        gate = threading.Event()
        r = Rig(self, spk=ThreadSpk(duration=1.5))
        svc = r.c._voice_service()

        def respond(text, cancel):
            gate.wait(10)
            return "all done", 1.0
        svc._respond = respond
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        t = threading.Thread(target=r.ask, args=("what time is it",), daemon=True)
        t.start()
        self.assertTrue(wait_for(lambda: r.c.voice_live()["phase"] == "THINKING", 8, r.c), "never reached THINKING")
        st, live = self.hud(r)
        self.assertEqual(st, M.THINKING)
        self.assertTrue(r.c.busy)
        gate.set()
        self.assertTrue(wait_for(lambda: r.c.voice_live()["phase"] == "SPEAKING", 8, r.c), "never reached SPEAKING")
        st, _ = self.hud(r)
        self.assertEqual(st, M.SPEAKING)
        self.assertEqual(M.describe(st, r.state(), r.c.voice_live()).wave_caption, "animation only · not audio")
        t.join(5)
        self.assertTrue(wait_for(lambda: r.c.voice_live()["phase"] in ("LISTENING", "FOLLOW_UP"), 8, r.c))
        self.assertIn(self.hud(r)[0], (M.LISTENING, M.FOLLOW_UP))

    def test_a_microphone_that_cannot_open_is_shown_as_unavailable_with_the_real_reason(self):
        mic = ThreadMic()
        mic.fail_open = True
        r = Rig(self, mic=mic)
        r.c.voice_autostart()
        self.assertTrue(wait_for(lambda: r.c.voice_live()["phase"] == "RECOVERING", 6, r.c))
        st, live = self.hud(r)
        self.assertEqual(st, M.MIC_UNAVAILABLE)
        self.assertFalse(live["mic_open"])
        v = M.describe(st, r.state(), live)
        self.assertEqual(v.mic_tone, "danger")
        self.assertIn("MIC OFF", v.mic_text)
        self.assertIn("device missing", r.state()["detail"])

    def test_no_microphone_device_is_a_microphone_fault_and_offers_retry(self):
        mic = ThreadMic()
        mic.device_list = []
        r = Rig(self, mic=mic)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Unavailable"))
        st = r.state()
        self.assertEqual(st["failure_kind"], "microphone")
        self.assertTrue(st["can_retry"])
        self.assertEqual(M.resolve_state(st, r.c.voice_live()), M.MIC_UNAVAILABLE)
        self.assertEqual(r.voice_threads_after_exit(), [])               # the worker is gone: nothing leaked

    def test_a_missing_speech_model_is_a_voice_error_that_names_the_cause(self):
        r = Rig(self, rec=ScriptRec(fail_preload="Vosk model folder not found: C:\\nope"))
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Unavailable"))
        st = r.state()
        self.assertEqual(st["failure_kind"], "model")
        self.assertEqual(M.resolve_state(st, r.c.voice_live()), M.ERROR)
        self.assertIn("Vosk model folder not found", M.describe(M.ERROR, st, r.c.voice_live()).sub)
        self.assertFalse(r.mic.is_open)

    def test_turning_voice_off_closes_the_microphone_and_the_thread_and_the_hud_reads_off(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.c.voice_disable()
        self.assertFalse(r.mic.is_open)
        self.assertEqual(r.voice_threads_after_exit(), [])
        self.assertEqual(self.hud(r)[0], M.OFF)
        self.assertEqual(r.mic.opens, r.mic.closes)                      # every open was closed

    def test_shutdown_while_listening_releases_the_microphone_and_every_thread(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.c.shutdown(3.0)
        self.assertFalse(r.mic.is_open)
        self.assertEqual(r.voice_threads_after_exit(), [])
        self.assertEqual(r.mic.opens, r.mic.closes)

    def test_hud_facts_are_measured_and_survive_a_broken_source(self):
        r = Rig(self)
        f = r.c.hud_facts()
        self.assertEqual(f["model_ok"], r.c.model_status().ok)             # whatever the real status is on this machine (no llama runtime on Linux CI), not a made-up one
        self.assertTrue(f["model_text"])
        self.assertEqual(f["online_on"], [])
        self.assertEqual(f["memory_count"], 0)
        self.assertFalse(f["guest"])
        with mock.patch.object(r.c, "memory_items", side_effect=OSError("locked")), mock.patch.object(r.c, "model_status", side_effect=RuntimeError("x")):
            f = r.c.hud_facts()
        self.assertIsNone(f["memory_count"])
        self.assertFalse(f["model_ok"])
        self.assertEqual(f["online_on"], [])                             # the other sources still answered

    # ---- always-on: a profile that never chose is listening for the wake phrase after the window's first start
    def fresh(self, mic=None, rec=None, guest=False):
        """A controller on a profile with NO voice.json (the user has never chosen), fake audio."""
        from jarvis.app.controller import AppController
        self.install_model()
        mic, rec, spk = mic or ThreadMic(), rec or ScriptRec(), ThreadSpk()
        c = AppController(self.cfg, guest=guest, speaker_factory=lambda: spk, mic_factory=lambda: mic, recognizer_factory=lambda vs_: rec)
        self.addCleanup(lambda: (c.shutdown(2.0), c.tasks.shutdown(30.0)))
        self.assertFalse((self.cfg.profile_dir / "voice.json").exists())
        return c, mic, rec

    def test_first_start_on_a_fresh_profile_listens_for_the_wake_phrase_without_any_button(self):
        c, mic, rec = self.fresh()
        c.voice_autostart()
        self.assertTrue(wait_for(lambda: c.voice_state()["state"] == "Listening", 6, c))
        st = c.voice_state()
        self.assertEqual((st["mode"], st["mic_open"], st["enabled"]), ("wake_word", True, True))
        saved = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        self.assertEqual((saved.input_mode, saved.listening_choice_made), ("wake_word", True))
        self.assertEqual(M.resolve_state(st, c.voice_live()), M.LISTENING)
        self.assertEqual(mic.opens, 1)

    def test_without_a_speech_model_the_microphone_is_never_opened(self):
        c, mic, rec = self.fresh(rec=ScriptRec(fail_preload="Vosk model folder not found"))
        c.voice_autostart()
        self.assertTrue(wait_for(lambda: c.voice_state()["state"] == "Unavailable", 6, c))
        self.assertEqual((mic.opens, mic.reads), (0, 0))
        self.assertEqual(M.resolve_state(c.voice_state(), c.voice_live()), M.ERROR)

    def test_without_a_microphone_device_nothing_is_opened_and_the_reason_is_shown(self):
        mic = ThreadMic()
        mic.device_list = []
        c, mic, rec = self.fresh(mic=mic)
        c.voice_autostart()
        self.assertTrue(wait_for(lambda: c.voice_state()["state"] == "Unavailable", 6, c))
        self.assertEqual(mic.opens, 0)
        self.assertEqual(M.resolve_state(c.voice_state(), c.voice_live()), M.MIC_UNAVAILABLE)

    def test_an_explicit_off_is_remembered_and_the_default_never_turns_it_back_on(self):
        c, mic, rec = self.fresh()
        c.voice_autostart()
        self.assertTrue(wait_for(lambda: c.voice_state()["state"] == "Listening", 6, c))
        c.voice_disable()
        self.assertFalse(mic.is_open)
        opens = mic.opens
        c.voice_autostart()                                   # what the next window start does
        c.voice_autostart()
        time.sleep(0.2)
        self.assertEqual((c.voice_state()["state"], mic.opens, c.voice_state()["enabled"]), ("Off", opens, False))

    def test_a_fresh_profile_that_is_turned_off_before_the_first_start_stays_off(self):
        c, mic, rec = self.fresh()
        c.voice_disable()                                     # chosen before the window ever started voice
        c.voice_autostart()
        time.sleep(0.2)
        self.assertEqual((mic.opens, c.voice_state()["enabled"]), (0, False))

    def test_a_guest_session_never_turns_listening_on_by_itself(self):
        c, mic, rec = self.fresh(guest=True)
        c.voice_autostart()
        time.sleep(0.2)
        self.assertEqual(mic.opens, 0)
        self.assertFalse((self.cfg.profile_dir / "voice.json").exists())

    def test_muted_stays_muted_across_the_automatic_start(self):
        c, mic, rec = self.fresh()
        c.voice_autostart()
        self.assertTrue(wait_for(lambda: c.voice_state()["state"] == "Listening", 6, c))
        c.voice_mute()
        self.assertTrue(wait_for(lambda: c.voice_state()["state"] == "Muted", 6, c))
        c.voice_autostart()
        time.sleep(0.2)
        self.assertEqual((c.voice_state()["state"], mic.is_open), ("Muted", False))

    def test_the_dataclass_default_is_off_with_no_recorded_choice(self):
        self.assertEqual((VoiceSettings().input_mode, VoiceSettings().listening_choice_made), ("off", False))
        self.assertFalse(VoiceSettings().muted)


# ---------------------------------------------------------------- the window's Home page against recording stand-in widgets
class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Stub()


class Stub(metaclass=_Meta):
    """Records every call made on it (`last[name] = args`) and returns another Stub, so any widget method can be called and chained."""

    def __init__(self, *a, **k):
        self.last = {}
        self.args = a

    def __getattr__(self, name):
        if name.startswith("__") or name in ("last", "args"):
            raise AttributeError(name)
        return _Method(self, name)

    def __or__(self, o):
        return self

    __ror__ = __or__


class _Method:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    def __call__(self, *a, **k):
        self.owner.last[self.name] = a
        return Stub()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(Stub(), name)

    def __or__(self, o):
        return self

    __ror__ = __or__


def _stub_qt():
    mods = {}
    for name in ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"):
        m = types.ModuleType(name)
        m.__getattr__ = lambda n: type(n, (Stub,), {}) if n[:1].isupper() else (_ for _ in ()).throw(AttributeError(n))
        mods[name] = m
    core = mods["PySide6.QtCore"]
    core.Signal = lambda *a, **k: Stub()
    core.Slot = lambda *a, **k: (lambda f: f)
    core.Qt = Stub()
    for n in ("QObject", "QSettings", "QTimer", "QUrl", "QPointF", "QRectF"):
        setattr(core, n, type(n, (Stub,), {}))
    mods["PySide6"].QtCore, mods["PySide6"].QtGui, mods["PySide6"].QtWidgets = core, mods["PySide6.QtGui"], mods["PySide6.QtWidgets"]
    return mods


class HomeGlue(unittest.TestCase):
    """STAND-IN Qt: the Home page builds, refreshes and sets the right text for each state. NOT a rendering test."""

    def setUp(self):
        patcher = mock.patch.dict(sys.modules, _stub_qt())
        patcher.start()
        self.addCleanup(patcher.stop)
        for m in ("jarvis.ui.window", "jarvis.ui.hud"):
            sys.modules.pop(m, None)
        self.W = importlib.import_module("jarvis.ui.window")
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in ("jarvis.ui.window", "jarvis.ui.hud")])
        w = self.win = object.__new__(self.W.MainWindow)
        self.ctl = mock.Mock()
        self.ctl.busy = False
        self.ctl.loading = False
        self.ctl.transcript.entries = []
        self.ctl.speech.is_speaking.return_value = False
        self.ctl.voice_state.return_value = V("Off", enabled=False, mic_open=False)
        self.ctl.voice_live.return_value = L("", False)
        self.ctl.hud_facts.return_value = {"model_ok": True, "model_text": "Installed and checked", "memory_count": 3, "online_on": [], "guest": False}
        w.ctl = self.ctl
        w.nav = Stub()
        w.statusBar = lambda: Stub()
        w._home_page()

    def text(self, widget):
        return widget.last["setText"][0]

    def refresh(self, voice, live, **ctl):
        self.ctl.voice_state.return_value = voice
        self.ctl.voice_live.return_value = live
        for k, v in ctl.items():
            setattr(self.ctl, k, v)
        self.win._home_facts_at = -1e9
        self.win._refresh_home()
        return self.win

    def test_home_is_the_first_page_and_has_a_refresh_method(self):
        self.assertEqual(self.W.PAGES[0], "Home")
        self.assertTrue(hasattr(self.W.MainWindow, "_refresh_home"))
        self.assertEqual(self.W._slug("Home"), "home")

    def test_voice_off_says_so_and_the_microphone_chip_is_off(self):
        w = self.refresh(V("Off", enabled=False, mic_open=False), L("", False))
        self.assertEqual(self.text(w.home_label), "VOICE OFF")
        self.assertEqual(self.text(w.home_mic), "MIC OFF")
        self.assertEqual(self.text(w.home_toggle), "Turn hands-free ON")
        self.assertEqual(w.home_mute.last["setEnabled"], (False,))
        self.assertEqual(w.home_retry.last["setVisible"], (False,))
        self.assertIn("no online features", self.text(w.home_privacy))

    def test_listening_turns_the_chip_on_and_offers_off_and_mute(self):
        w = self.refresh(V(), L("LISTENING"))
        self.assertEqual(self.text(w.home_label), "LISTENING…")
        self.assertEqual(self.text(w.home_mic), "MIC ON")
        self.assertIn(M.HUD["ok"], w.home_mic.last["setStyleSheet"][0])
        self.assertEqual(self.text(w.home_toggle), "Turn hands-free OFF")
        self.assertEqual(w.home_mute.last["setEnabled"], (True,))
        self.assertEqual(self.text(w.home_mute), "Mute microphone")
        self.assertIn("live microphone level", self.text(w.home_wave_cap))
        self.assertIn("●", w.home_stages.last["setText"][0])

    def test_muted_unavailable_and_error_are_each_shown_as_what_they_are(self):
        w = self.refresh(V("Muted", mic_open=False, mic_muted=True), L("MUTED", False))
        self.assertEqual((self.text(w.home_label), self.text(w.home_mic), self.text(w.home_mute)), ("MUTED", "MIC OFF · muted", "Unmute microphone"))
        w = self.refresh(V("Unavailable", mic_open=False, failure_kind="microphone", can_retry=True, detail="No microphone was found."), L("", False))
        self.assertEqual((self.text(w.home_label), self.text(w.home_mic)), ("MICROPHONE UNAVAILABLE", "MIC OFF · unavailable"))
        self.assertEqual(w.home_retry.last["setVisible"], (True,))
        self.assertIn("No microphone was found", self.text(w.home_sub))
        w = self.refresh(V("Unavailable", mic_open=False, failure_kind="model", can_retry=True, detail="No Vosk speech model folder is set."), L("", False))
        self.assertEqual(self.text(w.home_label), "VOICE ERROR")
        self.assertIn("Vosk", self.text(w.home_sub))

    def test_thinking_and_speaking_follow_the_controller_and_the_speaker(self):
        w = self.refresh(V("Off", enabled=False, mic_open=False), L("", False), busy=True)
        self.assertEqual(self.text(w.home_label), "THINKING")
        self.ctl.loading = True
        w = self.refresh(V("Off", enabled=False, mic_open=False), L("", False), busy=True, loading=True)
        self.assertEqual(self.text(w.home_label), "LOADING MODEL")
        self.ctl.speech.is_speaking.return_value = True
        w = self.refresh(V("Off", enabled=False, mic_open=False), L("", False), busy=False, loading=False)
        self.assertEqual(self.text(w.home_label), "SPEAKING")
        self.assertIn("not audio", self.text(w.home_wave_cap))
        self.assertIn("SPEAKING", w.home_stages.last["setText"][0])

    def test_the_provider_returns_state_level_and_wake_age_and_never_raises_into_the_painter(self):
        self.win._home_voice = V()
        self.ctl.voice_live.return_value = L("CAPTURING", level=0.12, wake_age=0.3)
        self.assertEqual(self.win._hud_provider(), (M.WAKE, 0.12, 0.3))
        self.ctl.speech.is_speaking.side_effect = RuntimeError("x")
        self.assertEqual(self.win._hud_provider()[0], M.WAKE)

    def test_panels_show_measured_facts_and_the_recent_conversation_is_escaped(self):
        self.ctl.transcript.entries = [Entry("user", "(voice) <b>hi</b> & bye"), Entry("assistant", "hello")]
        w = self.refresh(V(), L("LISTENING"))
        rows = [(self.text(n), self.text(v)) for n, v in w.home_qi]
        self.assertIn(("Memory", "3 saved items"), rows)
        self.assertIn(("Local model", "Installed and checked"), rows)
        html_text = w.home_recent.last["setText"][0]
        self.assertIn("&lt;b&gt;hi&lt;/b&gt; &amp; bye", html_text)
        self.assertNotIn("<b>hi</b>", html_text)

    def test_online_features_change_the_privacy_wording(self):
        self.ctl.hud_facts.return_value = {"model_ok": True, "model_text": "ok", "memory_count": 0, "online_on": ["web_research"], "guest": False}
        w = self.refresh(V(), L("LISTENING"))
        self.assertIn("Online features ON", self.text(w.home_privacy))
        self.assertNotIn("No internet required", w.home_local.last["setText"][0])

    def test_resource_bars_show_n_a_when_unmeasured(self):
        self.win._resources = M.Resources(load_psutil=False)
        w = self.refresh(V(), L("LISTENING"))
        self.assertEqual(self.text(w.home_bars["cpu"][1]), "n/a")
        self.assertEqual(w.home_bars["cpu"][0].last["setValue"], (0,))

    def test_toggle_and_mute_call_the_existing_controller_methods(self):
        self.ctl.voice_state.return_value = V("Off", enabled=False, mic_open=False)
        self.win._home_toggle()
        self.ctl.voice_enable.assert_called_once_with("wake_word")
        self.ctl.voice_state.return_value = V()
        self.win._home_toggle()
        self.ctl.voice_disable.assert_called_once_with()
        self.win._home_mute_toggle()
        self.ctl.voice_mute.assert_called_once_with()
        self.ctl.voice_state.return_value = V("Muted", mic_open=False, mic_muted=True)
        self.win._home_mute_toggle()
        self.ctl.voice_unmute.assert_called_once_with()

    def test_the_mute_shortcut_does_nothing_when_voice_is_off(self):
        self.ctl.voice_state.return_value = V("Off", enabled=False, mic_open=False)
        self.win._home_mute_shortcut()
        self.ctl.voice_mute.assert_not_called()
        self.ctl.voice_unmute.assert_not_called()

    def test_the_hud_widget_runs_frames_and_paints_without_error_against_stand_ins(self):
        hud = self.win.hud
        states = iter([M.LISTENING, M.WAKE, M.THINKING, M.SPEAKING, M.MUTED])
        cur = {"s": M.OFF}
        hud._provider = lambda: (cur["s"], 0.1, 0.1)
        hud.width, hud.height = (lambda: 400), (lambda: 360)
        for s in states:
            cur["s"] = s
            hud.step(0.033)
            hud._paint(Stub())
        self.assertEqual(hud.frames, 5)
        hud._provider = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        hud.step(0.033)
        self.assertEqual(hud.state, M.MUTED)                              # a broken provider keeps the last state
        hud._provider = lambda: ("not-a-state", 0.0, None)
        hud.step(0.033)
        self.assertEqual(hud.state, M.MUTED)


class StaticChecks(unittest.TestCase):
    """STATIC: the new modules keep the promises that are about what they must never do."""

    def imports(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module.split(".")[0])
        return out

    def test_the_hud_modules_import_no_network_audio_or_recording_library(self):
        banned = {"socket", "urllib", "http", "requests", "ssl", "ftplib", "smtplib", "websocket", "websockets", "aiohttp", "sounddevice", "wave", "pyaudio", "vosk", "subprocess"}
        for f in ("hud_model.py", "hud.py"):
            self.assertEqual(self.imports(SRC / "ui" / f) & banned, set(), f)

    def test_the_hud_modules_write_no_files(self):
        for f in ("hud_model.py", "hud.py"):
            src = (SRC / "ui" / f).read_text(encoding="utf-8")
            for needle in ("write_bytes", "write_text", ".write(", "open(", "sqlite3", "pickle"):
                self.assertNotIn(needle, src, f"{f}: {needle}")

    def test_the_service_keeps_only_a_number_for_the_level_never_audio(self):
        src = (SRC / "voice" / "session.py").read_text(encoding="utf-8")
        self.assertIn("self.level = rms(frame)", src)
        svc = (SRC / "app" / "voice_service.py").read_text(encoding="utf-8")
        self.assertIn('"audio_saved": False, "audio_leaves_device": False', svc)

    def test_the_home_page_has_the_controls_the_spec_asks_for(self):
        src = (SRC / "ui" / "window.py").read_text(encoding="utf-8")
        for needle in ('PAGES = ["Home", "Chat"', "Ctrl+Shift+M", "Turn hands-free ON", "Mute microphone", "Stop speaking", "voice_autostart"
                       if False else "HudWidget(provider=self._hud_provider)", "MIC OFF"):
            self.assertIn(needle, src if needle != "MIC OFF" else (SRC / "ui" / "hud_model.py").read_text(encoding="utf-8"), needle)
        app = (SRC / "ui" / "app.py").read_text(encoding="utf-8")
        self.assertIn("ctl.voice_autostart()", app)                      # enabled voice starts by itself when the window opens


if __name__ == "__main__":
    unittest.main()
