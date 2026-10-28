"""Stage 6 voice system: voice discovery and categories, saved choice, per-personality override, preview, speech output, microphone / recogniser checks,
interruption and cancellation, cleanup, integration with the assistant, unrelated data, and no cloud fallback.

LABELS (read before trusting any result): EVERY speaker, microphone and recogniser here is a FAKE (scripted objects). No sound was produced, no microphone
was opened, no Vosk model or Windows speech engine was used, PySide6 was not imported (the window is checked only against stand-in Qt modules and by reading
its source). These tests prove Jarvis's logic and wording; they do NOT prove that any real voice sounds right, that audio comes out of speakers, or that
speech recognition works on a real microphone. Those need real hardware / Windows (docs/STAGE6_VOICE_SYSTEM.md)."""
import hashlib
import importlib
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p17_common import Rig, ThreadMic, ThreadSpk, wait_for, AppCase  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.app.speech import SpeechOut  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.voice import engines as E, inputcheck, preview as P, viewmodel as VM  # noqa: E402
from jarvis.voice.backends import BackendUnavailable, SapiSpeaker  # noqa: E402
from jarvis.voice.manager import VoiceManager  # noqa: E402
from jarvis.voice.settings import VoiceSettings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

RAW = [{"name": "Alpha Voice", "culture": "en-US", "gender": "Female", "age": "Adult", "enabled": True},
       {"name": "Bravo Voice", "culture": "en-GB", "gender": "Male", "age": "Adult", "enabled": True},
       {"name": "Charlie Voice", "culture": "fr-FR", "gender": "Female", "age": "NotSet", "enabled": True},
       {"name": "Delta Voice", "culture": "en-AU", "gender": "Male", "age": "Adult", "enabled": True},
       {"name": "Echo Voice", "culture": "xx-ZZ", "gender": "Neutral", "age": "Adult", "enabled": True},
       {"name": "Zira Lookalike", "culture": "en-US", "gender": "", "age": "", "enabled": True},      # a feminine-sounding name with NO gender reported
       {"name": "Disabled Voice", "culture": "en-US", "gender": "Male", "age": "Adult", "enabled": False},
       {"name": "Alpha Voice", "culture": "en-US", "gender": "Female", "age": "Adult", "enabled": True}]       # duplicate


class FakeVoiceSpeaker:
    """FAKE speaker with a voice list: records what it was asked to say; 'speaks' for `duration` seconds; can fail."""
    def __init__(self, voices=None, duration=0.0, code=0):
        self.voices = RAW if voices is None else voices
        self.said, self.stops, self.duration, self.code = [], 0, duration, code
        self.fail_speak = None
        self.fail_stop = False
        self.fail_list = None
        self._until = 0.0

    def list_voices(self):
        if self.fail_list:
            raise self.fail_list
        return list(self.voices)

    def speak(self, text, rate, volume, voice):
        if self.fail_speak:
            raise self.fail_speak
        self.said.append((text, rate, volume, voice))
        self._until = time.monotonic() + self.duration

    def stop(self):
        self.stops += 1
        self._until = 0.0
        if self.fail_stop:
            raise RuntimeError("stop failed")

    def is_speaking(self):
        return time.monotonic() < self._until

    def exit_code(self):
        return None if self.is_speaking() else self.code


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(data_dir=Path(self.tmp.name) / "data")
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        self.sp = FakeVoiceSpeaker()
        self.style = None

    def sapi(self, speaker=None):
        return E.SapiEngine(speaker=speaker or self.sp, platform="win32")

    def mgr(self, engines=None, **kw):
        m = VoiceManager(self.cfg, E.EngineRegistry(engines or [self.sapi()]), persona_provider=lambda: self.style, **kw)
        self.addCleanup(m.close)
        return m

    def save(self, **kw):
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        for k, v in kw.items():
            setattr(vs, k, v)
        vs.save(self.cfg.profile_dir / "voice.json")


class Discovery(Base):
    def test_voices_come_only_from_what_the_engine_reports_and_are_grouped_by_the_engines_own_gender(self):
        m = self.mgr()
        cat = m.refresh()
        names = {c: [v["name"] for v in vs] for c, vs in cat["categories"].items()}
        self.assertEqual(names["feminine"], ["Alpha Voice", "Charlie Voice"])
        self.assertEqual(names["masculine"], ["Bravo Voice", "Delta Voice"])
        self.assertEqual(names["unspecified"], ["Echo Voice", "Zira Lookalike"])
        self.assertEqual(cat["count"], 6)                        # disabled and duplicate voices are not offered
        self.assertNotIn("Disabled Voice", sum(names.values(), []))

    def test_a_name_never_decides_the_category(self):
        m = self.mgr()
        m.refresh()
        self.assertEqual(m.find("sapi:Zira Lookalike").category, "unspecified")

    def test_each_voice_shows_language_accent_and_engine(self):
        m = self.mgr()
        m.refresh()
        v = m.find("sapi:Bravo Voice")
        self.assertEqual((v.language, v.language_label, v.engine, v.local, v.needs_internet), ("en-GB", "English (United Kingdom)", "sapi", True, False))
        self.assertEqual(m.find("sapi:Echo Voice").language_label, "xx-ZZ")        # an unknown code is shown as the code, not invented
        text = VM.voice_details(m.catalog(), "sapi:Bravo Voice")
        for part in ("Bravo Voice", "English (United Kingdom)", "Windows built-in speech", "runs on this PC", "Male"):
            self.assertIn(part, text)

    def test_category_comes_only_from_the_gender_word_the_engine_reports(self):
        for word, want in (("Female", "feminine"), ("FEMALE", "feminine"), ("female", "feminine"), ("Male", "masculine"), ("male", "masculine"),
                           ("Neutral", "unspecified"), ("NotSet", "unspecified"), ("", "unspecified"), (None, "unspecified"), ("Dragon", "unspecified")):
            self.assertEqual(E.category_of(word), want, word)

    def test_locale_labels(self):
        self.assertEqual(E.locale_label("en_US"), "English (United States)")
        self.assertEqual(E.locale_label("fr"), "French")
        self.assertEqual(E.locale_label(""), "")

    def test_no_engine_voices_means_no_voices_offered_and_a_clear_message(self):
        self.sp.voices = []
        m = self.mgr()
        cat = m.refresh()
        self.assertEqual(cat["count"], 0)
        self.assertIn("reported no installed voices", VM.empty_message(cat))
        self.assertEqual(VM.voice_items(cat, "feminine"), [])

    def test_a_failing_engine_does_not_hide_the_others_and_the_reason_is_kept(self):
        bad = FakeVoiceSpeaker()
        bad.fail_list = RuntimeError("powershell blocked")
        other = FakeVoiceSpeaker([{"name": "Other One", "culture": "en-US", "gender": "Male"}])
        m = self.mgr([E.SapiEngine(speaker=bad, platform="win32"), E.FactoryEngine(lambda: other, "other", "Other engine")])
        cat = m.refresh()
        self.assertEqual([v["id"] for v in cat["categories"]["masculine"]], ["other:Other One"])
        self.assertIn("could not list", next(e for e in cat["engines"] if e["id"] == "sapi")["error"])
        self.assertIn("could not list", VM.engines_text(cat))

    def test_on_a_machine_without_windows_speech_the_engine_is_unavailable_with_the_reason_and_nothing_is_listed(self):
        reg = E.default_registry("linux")
        m = VoiceManager(self.cfg, reg)
        cat = m.refresh()
        self.assertEqual(cat["count"], 0)
        self.assertFalse(cat["any_engine_available"])
        self.assertIn("needs Windows", VM.empty_message(cat))
        self.assertIn("answer as text only", VM.empty_message(cat))

    def test_engines_declare_local_offline_requirements_and_licence(self):
        d = E.SapiEngine(platform="win32").describe()
        self.assertTrue(d["local"] and not d["needs_internet"])
        self.assertIn("No download", d["size"])
        self.assertIn("Microsoft licence", d["licence"])

    def test_registry_is_modular_a_new_engine_is_one_class_and_ids_cannot_clash(self):
        reg = E.EngineRegistry([self.sapi()])
        reg.add(E.FactoryEngine(lambda: FakeVoiceSpeaker([{"name": "Zed", "culture": "en-US", "gender": "Female"}]), "zed"))
        m = VoiceManager(self.cfg, reg)
        self.assertEqual(m.refresh()["count"], 7)
        with self.assertRaises(ValueError):
            reg.add(E.FactoryEngine(lambda: None, "zed"))

    def test_sapi_list_output_is_parsed_with_gender_age_and_enabled_and_old_two_field_lines_still_work(self):
        out = mock.Mock(stdout="Alpha|en-US|Female|Adult|True\nOld Style|en-GB\nPipe|Name|en-CA|Male|Adult|False\n")
        spk = SapiSpeaker(popen=mock.Mock(), run=mock.Mock(return_value=out), exe=Path("powershell.exe"))
        v = spk.list_voices()
        self.assertEqual(v[0], {"name": "Alpha", "culture": "en-US", "gender": "Female", "age": "Adult", "enabled": True})
        self.assertEqual(v[1], {"name": "Old Style", "culture": "en-GB"})
        self.assertEqual((v[2]["name"], v[2]["enabled"]), ("Pipe|Name", False))

    def test_old_plain_voice_names_saved_before_engines_existed_still_resolve(self):
        self.save(tts_voice="Bravo Voice")
        r = self.mgr().resolve()
        self.assertEqual((r.engine_id, r.voice_name, r.fallback), ("sapi", "Bravo Voice", False))


class Preferences(Base):
    def test_apply_saves_the_voice_and_a_new_session_restores_it(self):
        m = self.mgr()
        m.refresh()
        self.assertEqual(m.apply("sapi:Delta Voice")["voice"]["name"], "Delta Voice")
        self.assertEqual(json.loads((self.cfg.profile_dir / "voice.json").read_text())["tts_voice"], "sapi:Delta Voice")
        fresh = self.mgr()                                      # "restart": a new manager reading only the saved file
        self.assertEqual(fresh.resolve().voice_name, "Delta Voice")
        self.assertEqual(fresh.catalog()["selected"], "sapi:Delta Voice")

    def test_applying_an_uninstalled_voice_is_refused_and_nothing_is_saved(self):
        m = self.mgr()
        with self.assertRaisesRegex(ValueError, "not installed"):
            m.apply("sapi:Nobody")
        self.assertFalse((self.cfg.profile_dir / "voice.json").exists())

    def test_applying_a_voice_keeps_every_other_voice_setting(self):
        self.save(rate=1.4, volume=0.3, wake_phrase="hello computer", mic_device="USB Mic", input_mode="off", pronunciations={"SQL": "sequel"})
        m = self.mgr()
        m.apply("sapi:Alpha Voice")
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        self.assertEqual((vs.rate, vs.volume, vs.wake_phrase, vs.mic_device, vs.input_mode, vs.pronunciations), (1.4, 0.3, "hello computer", "USB Mic", "off", {"SQL": "sequel"}))
        self.assertEqual(vs.tts_voice, "sapi:Alpha Voice")

    def test_voice_choice_is_independent_of_personality_mode(self):
        m = self.mgr()
        m.refresh()
        m.apply("sapi:Alpha Voice")
        from jarvis.personality.styles import STYLES
        for style in STYLES:                                     # every supported personality mode keeps the same voice
            self.style = style
            self.assertEqual(m.resolve().voice_name, "Alpha Voice", style)
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").tts_voice, "sapi:Alpha Voice")

    def test_any_voice_can_be_combined_with_any_mode_through_the_optional_override(self):
        from jarvis.personality.styles import STYLES
        m = self.mgr()
        m.refresh()
        m.apply("sapi:Alpha Voice")
        modes = list(STYLES)
        other = modes[1]
        m.apply("sapi:Bravo Voice", persona=other)
        self.style = other
        self.assertEqual((m.resolve().voice_name, m.resolve().source), ("Bravo Voice", "persona"))
        self.style = modes[0]
        self.assertEqual((m.resolve().voice_name, m.resolve().source), ("Alpha Voice", "global"))       # the global default is preserved
        m.clear_persona_voice(other)
        self.style = other
        self.assertEqual(m.resolve().voice_name, "Alpha Voice")
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").tts_voice, "sapi:Alpha Voice")

    def test_unknown_personality_mode_is_rejected(self):
        m = self.mgr()
        m.refresh()
        with self.assertRaisesRegex(ValueError, "unknown personality mode"):
            m.apply("sapi:Alpha Voice", persona="not-a-mode")
        with self.assertRaises(ValueError):
            m.clear_persona_voice("not-a-mode")

    def test_guest_sessions_cannot_save_a_voice(self):
        m = self.mgr(guest=True)
        m.refresh()
        with self.assertRaisesRegex(ValueError, "guest"):
            m.apply("sapi:Alpha Voice")
        self.assertFalse((self.cfg.profile_dir / "voice.json").exists())

    def test_settings_file_without_the_new_fields_loads_and_bad_new_fields_fall_back_to_defaults(self):
        (self.cfg.profile_dir / "voice.json").write_text(json.dumps({"rate": 1.2, "tts_voice": "Old"}), encoding="utf-8")
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        self.assertEqual((vs.rate, vs.voice_by_persona, vs.vad_min_rms), (1.2, {}, 0.01))
        (self.cfg.profile_dir / "voice.json").write_text(json.dumps({"rate": 1.2, "voice_by_persona": {"x": 5}}), encoding="utf-8")
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").rate, 1.0)
        for bad in ({"vad_min_rms": 5}, {"tts_voice": ""}, {"voice_by_persona": []}):
            with self.assertRaises(ValueError):
                VoiceSettings(**bad).validate()


class Fallback(Base):
    def test_a_missing_chosen_voice_falls_back_to_the_engine_default_and_says_so(self):
        self.save(tts_voice="sapi:Removed Voice")
        r = self.mgr().resolve()
        self.assertEqual((r.engine_id, r.voice_name, r.fallback), ("sapi", None, True))
        self.assertIn("'Removed Voice'", r.note)
        self.assertIn("not installed", r.note)
        self.assertIn("default voice", r.note)

    def test_a_missing_mode_voice_falls_back_to_the_global_voice_not_the_engine_default(self):
        from jarvis.personality.styles import STYLES
        mode = list(STYLES)[1]
        self.save(tts_voice="sapi:Alpha Voice", voice_by_persona={mode: "sapi:Gone"})
        self.style = mode
        r = self.mgr().resolve()
        self.assertEqual((r.voice_name, r.source, r.fallback), ("Alpha Voice", "global", True))
        self.assertIn("Gone", r.note)

    def test_a_voice_whose_engine_became_unavailable_uses_another_engine_and_says_so(self):
        gone = E.SapiEngine(platform="linux")
        alt = E.FactoryEngine(lambda: FakeVoiceSpeaker([]), "alt", "Alt engine")
        self.save(tts_voice="sapi:Alpha Voice")
        m = VoiceManager(self.cfg, E.EngineRegistry([gone, alt]))
        m._voices = [E.VoiceInfo("sapi:Alpha Voice", "sapi", "Alpha Voice", "en-US", "English (United States)", "feminine", "Female", "")]
        m._loaded = True
        r = m.resolve()
        self.assertEqual((r.engine_id, r.fallback), ("alt", True))
        self.assertIn("unavailable", r.note)

    def test_with_no_working_engine_there_is_no_voice_and_a_reason(self):
        self.save(tts_voice="sapi:Alpha Voice")
        m = VoiceManager(self.cfg, E.default_registry("linux"))
        r = m.resolve()
        self.assertIsNone(r.engine_id)
        self.assertIn("needs Windows", r.note)

    def test_status_display_never_triggers_a_slow_voice_listing(self):
        self.save(tts_voice="sapi:Alpha Voice")
        m = self.mgr()
        self.sp.fail_list = AssertionError("listing must not run for a status display")
        r = m.resolve(load=False)
        self.assertIn("not been read yet", r.note)
        m.state()
        m.catalog()


class Preview(Base):
    def run_preview(self, m, vid="sapi:Alpha Voice", wait=P.TERMINAL):
        pv = P.VoicePreview(m, poll=0.005)
        self.addCleanup(pv.close)
        states = []
        pv._on_change = lambda: states.append(pv.state()["state"])
        pv.start(vid)
        self.assertTrue(wait_for(lambda: pv.state()["state"] in wait, 5))
        return pv, states

    def test_successful_preview_goes_loading_playing_finished_and_speaks_the_fixed_sentence_in_the_chosen_voice(self):
        self.sp.duration = 0.05
        m = self.mgr()
        m.refresh()
        pv, states = self.run_preview(m, "sapi:Bravo Voice")
        self.assertEqual([s for i, s in enumerate(states) if i == 0 or s != states[i - 1]], ["loading", "playing", "finished"])
        text, rate, vol, voice = self.sp.said[0]
        self.assertEqual((text, voice), (P.PREVIEW_TEXT, "Bravo Voice"))
        self.assertEqual((rate, vol), (1.0, 0.8))
        self.assertIn("cannot hear your speakers", pv.state()["message"])
        self.assertNotRegex(VM.preview_text(pv.state()), r"(?i)(was|were|been) heard|jarvis heard|verified|you heard it")

    def test_preview_uses_the_saved_speed_and_volume(self):
        self.save(rate=1.5, volume=0.4)
        m = self.mgr()
        m.refresh()
        self.run_preview(m)
        self.assertEqual(self.sp.said[0][1:3], (1.5, 0.4))

    def test_engine_failure_is_an_error_not_a_success_and_cleans_up(self):
        self.sp.fail_speak = OSError("audio device unavailable")
        m = self.mgr()
        m.refresh()
        pv, states = self.run_preview(m)
        self.assertEqual(pv.state()["state"], "error")
        self.assertNotIn("finished", states)
        self.assertIn("could not be played", pv.state()["message"])
        self.assertFalse(pv.state()["busy"])

    def test_engine_exit_code_error_is_reported_as_failure(self):
        self.sp.code = 1
        m = self.mgr()
        m.refresh()
        pv, _ = self.run_preview(m)
        self.assertEqual(pv.state()["state"], "error")
        self.assertIn("code 1", pv.state()["message"])

    def test_a_stuck_engine_times_out_and_is_stopped(self):
        self.sp.duration = 60
        m = self.mgr()
        m.refresh()
        pv = P.VoicePreview(m, poll=0.005, timeout=0.1)
        self.addCleanup(pv.close)
        pv.start("sapi:Alpha Voice")
        self.assertTrue(wait_for(lambda: pv.state()["state"] == "error", 5))
        self.assertIn("too long", pv.state()["message"])
        self.assertGreaterEqual(self.sp.stops, 1)

    def test_stop_silences_the_sample_and_ends_the_worker(self):
        self.sp.duration = 60
        m = self.mgr()
        m.refresh()
        pv, _ = self.run_preview(m, wait=("playing",))
        pv.stop()
        self.assertEqual(pv.state()["state"], "stopped")
        self.assertGreaterEqual(self.sp.stops, 1)
        self.assertFalse(pv._thread.is_alive())

    def test_preview_while_the_speaker_is_muted_refuses_instead_of_pretending(self):
        self.save(speaker_muted=True)
        m = self.mgr()
        m.refresh()
        pv = P.VoicePreview(m)
        st = pv.start("sapi:Alpha Voice")
        self.assertEqual(st["state"], "error")
        self.assertIn("muted", st["message"])
        self.assertEqual(self.sp.said, [])

    def test_uninstalled_voice_or_unavailable_engine_gives_an_error(self):
        m = self.mgr()
        m.refresh()
        pv, _ = self.run_preview(m, "sapi:Nobody")
        self.assertIn("not installed", pv.state()["message"])
        m2 = VoiceManager(self.cfg, E.EngineRegistry([E.SapiEngine(platform="linux")]))
        m2._voices = [E.VoiceInfo("sapi:Alpha Voice", "sapi", "Alpha Voice", "", "", "feminine", "Female", "")]
        m2._loaded = True
        pv2 = P.VoicePreview(m2)
        pv2.start("sapi:Alpha Voice")
        self.assertTrue(wait_for(lambda: pv2.state()["state"] == "error", 5))
        self.assertIn("unavailable", pv2.state()["message"])

    def test_empty_choice_and_out_of_range_values_are_refused(self):
        m = self.mgr()
        pv = P.VoicePreview(m)
        self.assertEqual(pv.start("")["state"], "error")
        self.assertEqual(pv.start("sapi:Alpha Voice", rate=9)["state"], "error")
        self.assertEqual(pv.start("sapi:Alpha Voice", volume=-1)["state"], "error")

    def test_starting_a_second_preview_stops_the_first_and_a_stale_worker_cannot_overwrite_the_state(self):
        self.sp.duration = 60
        m = self.mgr()
        m.refresh()
        pv = P.VoicePreview(m, poll=0.005)
        self.addCleanup(pv.close)
        pv.start("sapi:Alpha Voice")
        self.assertTrue(wait_for(lambda: pv.state()["state"] == "playing", 5))
        first = pv._thread
        self.sp.duration = 0.02
        pv.start("sapi:Bravo Voice")
        self.assertTrue(wait_for(lambda: pv.state()["state"] == "finished", 5))
        self.assertFalse(first.is_alive())
        self.assertEqual(pv.state()["voice"], "sapi:Bravo Voice")

    def test_a_late_result_from_an_old_preview_is_ignored(self):
        m = self.mgr()
        pv = P.VoicePreview(m)
        pv._gen = 5
        pv._set(P.PLAYING, "current", "sapi:Alpha Voice")
        pv._set(P.ERROR, "stale worker", "sapi:Old", gen=4)
        self.assertEqual((pv.state()["state"], pv.state()["message"]), ("playing", "current"))
        pv._set(P.FINISHED, "ok", "sapi:Alpha Voice", gen=5)
        self.assertEqual(pv.state()["state"], "finished")

    def test_close_leaves_no_thread_running(self):
        self.sp.duration = 60
        m = self.mgr()
        m.refresh()
        pv, _ = self.run_preview(m, wait=("playing",))
        pv.close()
        self.assertFalse(pv._thread.is_alive())

    def test_preview_silences_normal_speech_first_so_there_is_one_audio_output(self):
        m = self.mgr()
        m.refresh()
        self.sp.duration = 0.01
        m.speaker().speak("a normal reply", 1.0, 0.8)
        before = self.sp.stops
        self.run_preview(m)
        self.assertGreater(self.sp.stops, before)


class SpeechOutput(Base):
    def out(self, **save):
        self.save(speak_in_window=True, **save)
        so = SpeechOut(self.cfg, speaker_factory=lambda: self.sp)
        self.addCleanup(so.stop)
        return so

    def test_spoken_reply_uses_the_saved_voice_rate_and_volume(self):
        so = self.out(rate=1.25, volume=0.5, tts_voice="x:Whatever")          # factory engine lists no voices -> default voice, reported
        self.assertTrue(so.speak("Hello there."))
        text, rate, vol, voice = self.sp.said[0]
        self.assertEqual((text, rate, vol), ("Hello there.", 1.25, 0.5))

    def test_changing_the_voice_applies_to_the_next_sentence_without_a_restart(self):
        reg = E.EngineRegistry([self.sapi()])
        so = SpeechOut(self.cfg, speaker_factory=lambda: self.sp)
        so.voices.registry = reg
        so.voices._speaker.m = so.voices
        self.save(speak_in_window=True)
        so.voices.refresh()
        so.voices.apply("sapi:Alpha Voice")
        so.speak("one")
        so.voices.apply("sapi:Delta Voice")
        so.speak("two")
        self.assertEqual([s[3] for s in self.sp.said], ["Alpha Voice", "Delta Voice"])

    def test_empty_or_unspeakable_text_is_not_spoken(self):
        so = self.out()
        self.assertFalse(so.speak(""))
        self.assertFalse(so.speak("   "))
        self.assertEqual(self.sp.said, [])

    def test_engine_failure_is_a_message_never_a_crash_and_the_speaker_is_rebuilt(self):
        so = self.out()
        self.sp.fail_speak = RuntimeError("device lost")
        self.assertFalse(so.speak("hello"))
        self.assertIn("Speech output failed", so.last_error)
        self.sp.fail_speak = None
        self.assertTrue(so.speak("hello again"))

    def test_off_by_default_and_muted_speaker_say_nothing(self):
        so = SpeechOut(self.cfg, speaker_factory=lambda: self.sp)
        self.assertFalse(so.speak("hello"))
        so = self.out(speaker_muted=True)
        self.assertFalse(so.speak("hello"))
        self.assertEqual(self.sp.said, [])

    def test_no_engine_gives_a_reason_and_text_only(self):
        self.save(speak_in_window=True)
        so = SpeechOut(self.cfg, platform="linux")
        self.assertFalse(so.speak("hello"))
        self.assertIn("needs Windows", so.last_error)

    def test_interrupted_playback_stops_immediately_and_a_stop_failure_is_reported(self):
        so = self.out()
        self.sp.duration = 60
        so.speak("a long reply")
        self.assertTrue(so.is_speaking())
        so.stop()
        self.assertFalse(so.is_speaking())
        self.sp.fail_stop = True
        so.speak("again")
        so.stop()
        self.assertIn("Could not stop", so.last_error)

    def test_a_fallback_voice_is_reported_while_speech_still_works(self):
        reg = E.EngineRegistry([self.sapi()])
        so = SpeechOut(self.cfg, speaker_factory=lambda: self.sp)
        so.voices.registry = reg
        self.save(speak_in_window=True, tts_voice="sapi:Removed")
        self.assertTrue(so.speak("hello"))
        self.assertIsNone(self.sp.said[0][3])
        self.assertTrue(so.voices.state()["fallback"])


class MicrophoneAndRecognizerChecks(Base):
    class Mic:
        def __init__(self, devs=None, error=None):
            self.devs, self.error, self.opened = devs, error, 0

        def devices(self):
            if self.error:
                raise self.error
            return self.devs or []

        def open(self, device):
            self.opened += 1
            raise AssertionError("the readiness check must never open the microphone")

    def vs(self, **kw):
        return VoiceSettings(**kw)

    def test_ready_when_microphone_listed_and_recogniser_present(self):
        model = Path(self.tmp.name) / "model"
        (model / "am").mkdir(parents=True)
        mic = self.Mic([{"index": 0, "name": "Desk Mic", "channels": 1}])
        rep = inputcheck.check_input(self.vs(vosk_model=str(model)), lambda: mic, lambda: True)
        self.assertTrue(rep["ready"], rep["problems"])
        self.assertEqual(mic.opened, 0)
        self.assertIn("does not open the microphone", rep["note"])
        self.assertIn("Ready", VM.input_report_text(rep))

    def test_missing_audio_library_missing_microphone_and_missing_selection_are_reported(self):
        rep = inputcheck.check_input(self.vs(), lambda: self.Mic(error=BackendUnavailable("sounddevice is not installed")), lambda: True)
        self.assertFalse(rep["ready"])
        self.assertIn("sounddevice is not installed", " ".join(rep["problems"]))
        rep = inputcheck.check_input(self.vs(), lambda: self.Mic([]), lambda: True)
        self.assertIn("No microphone was found", " ".join(rep["problems"]))
        rep = inputcheck.check_input(self.vs(mic_device="Gone Mic"), lambda: self.Mic([{"index": 0, "name": "Desk Mic", "channels": 1}]), lambda: True)
        self.assertIn("not connected", " ".join(rep["problems"]))
        self.assertFalse(rep["microphone"]["selected_found"])

    def test_missing_recogniser_or_model_folder_are_reported(self):
        mic = self.Mic([{"index": 0, "name": "Desk Mic", "channels": 1}])
        rep = inputcheck.check_input(self.vs(), lambda: mic, lambda: False)
        text = " ".join(rep["problems"])
        self.assertIn("'vosk' Python package) is not installed", text)
        self.assertIn("No Vosk speech model folder is set", text)
        rep = inputcheck.check_input(self.vs(vosk_model=str(Path(self.tmp.name) / "empty")), lambda: mic, lambda: True)
        self.assertIn("does not look like an unpacked model", " ".join(rep["problems"]))

    def test_the_default_microphone_is_labelled_and_choices_include_a_saved_but_unplugged_device(self):
        ch = VM.mic_choices({"microphone": {"devices": [{"name": "A"}]}}, "B")
        self.assertEqual([c[1] for c in ch], ["System default microphone", "A", "B (not connected)"])

    def test_nothing_in_the_check_downloads_or_uses_the_network(self):
        src = (ROOT / "src/jarvis/voice/inputcheck.py").read_text()
        self.assertNotRegex(src, r"^\s*(import|from)\s+(urllib|requests|socket|http|httpx|ssl)", )
        self.assertNotIn(".open(", src)


class ControllerIntegration(AppCase):
    """The real AppController with FAKE audio (ThreadMic / ScriptRec / ThreadSpk)."""

    def test_listening_is_never_started_by_voice_selection_checks_or_settings_changes(self):
        mic = ThreadMic()
        rig = Rig(self, mode="off", mic=mic)
        c = rig.c
        c.voice_input_check()
        self.assertTrue(wait_for(lambda: not c.voice_input["running"], 5, c))
        self.assertEqual((mic.opens, mic.reads, mic.is_open), (0, 0, False))
        c.voice_input_set(wake_phrase="hello jarvis there", mic_device="Fake Mic")
        r = c.voice_switch_mode("push_to_talk")
        self.assertFalse(r["ok"])
        self.assertEqual(c.speech.settings().input_mode, "off")
        self.assertEqual((mic.opens, mic.is_open), (0, False))
        self.assertEqual(c.voice_state()["state"], "Off")

    def test_input_settings_are_validated_saved_and_cannot_turn_listening_on(self):
        rig = Rig(self, mode="off")
        c = rig.c
        for bad in ({"wake_phrase": "jarvis"}, {"vad_min_rms": 9}, {"input_mode": "wake_word"}, {"nonsense": 1}):
            with self.assertRaises(ValueError):
                c.voice_input_set(**bad)
        c.voice_input_set(wake_phrase="hey jarvis", vad_min_rms=0.02)
        vs = c.speech.settings()
        self.assertEqual((vs.wake_phrase, vs.vad_min_rms, vs.input_mode), ("hey jarvis", 0.02, "off"))

    def test_start_mute_unmute_stop_with_visible_state(self):
        mic = ThreadMic()
        rig = Rig(self, mode="wake_word", mic=mic)
        c = rig.c
        c.voice_autostart()
        self.assertTrue(rig.wait_state("Listening"))
        self.assertTrue(mic.is_open)
        self.assertTrue(rig.state()["mic_open"])
        c.voice_mute()
        self.assertTrue(wait_for(lambda: not mic.is_open, 5, c))
        self.assertEqual(rig.state()["state"], "Muted")
        c.voice_unmute()
        self.assertTrue(rig.wait_state("Listening"))
        c.voice_disable()
        self.assertTrue(wait_for(lambda: not mic.is_open, 5, c))
        self.assertEqual(c.speech.settings().input_mode, "off")

    def test_missing_microphone_and_missing_engine_give_clear_unavailable_states(self):
        mic = ThreadMic()
        mic.device_list = []
        rig = Rig(self, mode="wake_word", mic=mic)
        rig.c.voice_autostart()
        self.assertTrue(rig.wait_state("Unavailable"))
        self.assertIn("No microphone was found", rig.state()["detail"])
        self.assertTrue(rig.state()["can_retry"])

    def test_broken_recogniser_is_reported_not_silently_listening(self):
        from p17_common import ScriptRec
        rig = Rig(self, mode="wake_word", rec=ScriptRec(fail_preload="Vosk could not load the model folder"))
        rig.c.voice_autostart()
        self.assertTrue(rig.wait_state("Unavailable"))
        self.assertIn("Vosk could not load", rig.state()["detail"])
        self.assertFalse(rig.mic.is_open)

    def test_push_to_talk_mode_and_switching_modes_only_while_enabled(self):
        rig = Rig(self, mode="push_to_talk")
        c = rig.c
        c.voice_autostart()
        self.assertTrue(rig.wait_state("Listening"))
        self.assertIn("Push-to-talk", rig.state()["detail"])
        r = c.voice_switch_mode("wake_word")
        self.assertTrue(r["ok"])
        self.assertEqual(c.speech.settings().input_mode, "wake_word")
        self.assertTrue(wait_for(lambda: c.voice_state()["state"] == "Listening" and "wake phrase" in c.voice_state()["detail"], 6, c))

    def test_a_restart_queued_before_closing_can_never_reopen_the_microphone_afterwards(self):
        mic = ThreadMic()
        rig = Rig(self, mode="wake_word", mic=mic)
        c = rig.c
        c.voice_autostart()
        self.assertTrue(rig.wait_state("Listening"))
        svc = c.voice
        c.shutdown(3.0)
        self.assertFalse(mic.is_open)
        svc.start()                                  # what a late queued restart would do
        svc.retry()
        time.sleep(0.2)
        self.assertFalse(svc.alive())
        self.assertFalse(mic.is_open)
        self.assertFalse(any(t.name == "jarvis-voice" for t in threading.enumerate()))

    def test_voice_cancel_stops_speech_and_returns_to_listening(self):
        spk = ThreadSpk(duration=30)
        rig = Rig(self, mode="wake_word", spk=spk)
        c = rig.c
        c.voice_autostart()
        self.assertTrue(rig.wait_state("Listening"))
        rig.ask("what is two plus two")
        self.assertTrue(wait_for(lambda: bool(spk.said), 8, c))
        c.stop_speaking()
        self.assertGreaterEqual(spk.stops, 1)
        self.assertTrue(rig.wait_state("Listening"))

    def test_hands_free_replies_use_the_selected_voice_and_the_switch_of_mode_does_not_change_it(self):
        spk = FakeVoiceSpeaker(duration=0.02)
        rig = Rig(self, mode="wake_word", spk=spk, settings={"tts_voice": "custom:Bravo Voice"})
        c = rig.c
        c.speech.voices.registry.get("custom")._spk = spk                    # the factory engine speaks through our recording speaker
        c.voice_autostart()
        self.assertTrue(rig.wait_state("Listening"))
        rig.ask("what is two plus two")
        self.assertTrue(wait_for(lambda: bool(spk.said), 8, c))
        self.assertEqual(spk.said[0][3], "Bravo Voice")
        before = json.loads((self.cfg.profile_dir / "voice.json").read_text())["tts_voice"]
        for style in [s["id"] for s in c.persona_state()["styles"] if s["available"]]:
            c.persona_set(style)
            self.assertEqual(c.voice_catalog()["selected"], before)
        c.persona_normal()

    def test_voice_page_data_for_the_window(self):
        rig = Rig(self, mode="off")
        c = rig.c
        cat = c.voice_catalog()
        self.assertEqual(set(cat["categories"]), {"feminine", "masculine", "unspecified"})
        self.assertFalse(cat["loaded"])                                      # nothing is listed until asked
        self.assertEqual(cat["persona"]["id"], c.persona.settings.style)
        c.voice_catalog_refresh()
        self.assertTrue(wait_for(lambda: c.voice_catalog()["loaded"], 5, c))

    def test_guest_cannot_change_voice_settings(self):
        rig = Rig(self, mode="off", guest=True)
        for fn in (lambda: rig.c.voice_apply("x:y"), lambda: rig.c.voice_input_set(wake_phrase="hey jarvis"), lambda: rig.c.voice_clear_persona("normal")):
            with self.assertRaisesRegex(ValueError, "guest"):
                fn()

    def test_shutdown_closes_preview_microphone_and_speech(self):
        mic = ThreadMic()
        rig = Rig(self, mode="wake_word", mic=mic)
        c = rig.c
        c.voice_autostart()
        self.assertTrue(rig.wait_state("Listening"))
        c.shutdown(3.0)
        self.assertFalse(mic.is_open)
        self.assertFalse(c.voice_preview.state()["busy"])


class UnrelatedDataSurvives(AppCase):
    def digests(self):
        out = {}
        for root in (self.cfg.data_dir,):
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.name not in ("voice.json", "voice.json.tmp", "mic.lock", "app.lock") and not p.name.endswith((".lock", "-wal", "-shm")):
                    out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def test_choosing_previewing_and_clearing_voices_changes_only_voice_json(self):
        rig = Rig(self, mode="off")
        c = rig.c
        c.persona_set("study" if "study" in [s["id"] for s in c.persona_state()["styles"]] else c.persona_state()["styles"][0]["id"])
        c.persona_normal()
        notes = self.cfg.data_dir / "notes.txt"
        notes.write_text("my private note", encoding="utf-8")
        mem = self.cfg.data_dir / "memory-sentinel.db"
        mem.write_bytes(b"memory bytes")
        perms = self.cfg.profile_dir / "permissions.json"
        perms.write_text('{"allow": ["x"]}', encoding="utf-8")
        before = self.digests()
        persona_before = (self.cfg.profile_dir / "persona.json").read_bytes() if (self.cfg.profile_dir / "persona.json").exists() else None
        c.speech.voices.registry.get("custom")._spk = FakeVoiceSpeaker([{"name": "Q", "culture": "en-US", "gender": "Female"}])
        c.speech.voices.refresh()
        c.voice_apply("custom:Q")
        c.voice_preview_start("custom:Q")
        self.assertTrue(wait_for(lambda: c.voice_preview.state()["state"] in P.TERMINAL, 5, c))
        c.voice_clear_persona(c.persona.settings.style)
        c.voice_input_set(wake_phrase="hey jarvis")
        self.assertEqual(self.digests(), before)
        self.assertEqual(perms.read_text(encoding="utf-8"), '{"allow": ["x"]}')
        if persona_before is not None:
            self.assertEqual((self.cfg.profile_dir / "persona.json").read_bytes(), persona_before)
        self.assertEqual(c.persona.settings.style, "normal")


class NoCloudFallback(unittest.TestCase):
    def test_voice_package_and_speech_output_import_no_network_module(self):
        bad = re.compile(r"^\s*(import|from)\s+(urllib|requests|socket|http|httpx|aiohttp|ssl|ftplib|smtplib|websocket|websockets|openai|anthropic|azure|boto3|elevenlabs|gtts|edge_tts)\b", re.M)
        files = [*(ROOT / "src/jarvis/voice").glob("*.py"), ROOT / "src/jarvis/app/speech.py", ROOT / "src/jarvis/app/voice_service.py"]
        for f in files:
            self.assertIsNone(bad.search(f.read_text(encoding="utf-8")), f.name)

    def test_no_cloud_voice_vendor_or_api_key_in_the_voice_code(self):
        pat = re.compile(r"elevenlabs|azure|polly|google\.cloud|cognitive|api[_-]?key|openai|speech\.microsoft|texttospeech", re.I)
        for f in [*(ROOT / "src/jarvis/voice").glob("*.py"), ROOT / "src/jarvis/app/speech.py"]:
            self.assertIsNone(pat.search(f.read_text(encoding="utf-8")), f.name)

    def test_every_engine_is_local_and_a_failed_engine_never_switches_to_an_online_one(self):
        for e in E.default_registry("win32").engines:
            self.assertTrue(e.local and not e.needs_internet, e.id)
        with tempfile.TemporaryDirectory() as t:
            cfg = Config(data_dir=Path(t) / "d")
            cfg.profile_dir.mkdir(parents=True, exist_ok=True)
            m = VoiceManager(cfg, E.default_registry("linux"))
            with mock.patch("socket.socket", side_effect=AssertionError("network used")):
                r = m.resolve()
                self.assertIsNone(r.engine_id)                              # no engine -> text only, never an online substitute
                m.refresh()
                m.catalog()

    def test_preview_listing_and_speaking_never_touch_the_network(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = Config(data_dir=Path(t) / "d")
            cfg.profile_dir.mkdir(parents=True, exist_ok=True)
            sp = FakeVoiceSpeaker()
            m = VoiceManager(cfg, E.EngineRegistry([E.SapiEngine(speaker=sp, platform="win32")]))
            pv = P.VoicePreview(m, poll=0.005)
            with mock.patch("socket.socket", side_effect=AssertionError("network used")):
                m.refresh()
                pv.start("sapi:Alpha Voice")
                self.assertTrue(wait_for(lambda: pv.state()["state"] in P.TERMINAL, 5))
            pv.close()
            self.assertEqual(pv.state()["state"], "finished")


class WindowWiring(unittest.TestCase):
    """Stand-in Qt modules and source reading only. Nothing is drawn; PySide6 was never run."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "src/jarvis/ui/window.py").read_text(encoding="utf-8")

    def window(self):
        from test_p34_readiness import _fake_qt
        p = mock.patch.dict(sys.modules, _fake_qt())
        p.start()
        self.addCleanup(p.stop)
        sys.modules.pop("jarvis.ui.window", None)
        self.addCleanup(lambda: sys.modules.pop("jarvis.ui.window", None))
        return importlib.import_module("jarvis.ui.window")

    def test_every_new_handler_exists_and_every_controller_call_exists(self):
        W = self.window()
        for name in set(re.findall(r"self\.(_v[ci]_\w+)", self.src)) - {"_vc_asked", "_vc_selected", "_vc_cat", "_vc_sig", "_vi_rep", "_vc_engine", "_vc_eng_sig"}:
            self.assertTrue(hasattr(W.MainWindow, name), name)
        for name in set(re.findall(r"self\.ctl\.(voice_\w+)", self.src)):
            self.assertTrue(hasattr(AppController, name), name)

    def test_the_page_offers_preview_stop_apply_refresh_and_the_categories(self):
        for label in ('"Preview Voice"', '"Stop"', '"Apply"', '"Refresh voices"', '"Check microphone and recogniser"', '"Hold to talk"', '"Save wake phrase"'):
            self.assertIn(label, self.src)
        self.assertIn("category_options", self.src)

    def test_texts_from_engines_and_files_are_plain_text_and_listing_waits_for_the_page(self):
        block = self.src.split("def _voice_selection_block", 1)[1].split("def _vc_refresh", 1)[0]
        for w in ("vc_engines", "vc_details", "vc_status", "vc_resolution", "vc_empty"):
            self.assertRegex(block, rf"self\.{w}\.setTextFormat\(Qt\.TextFormat\.PlainText\)")
        self.assertIn('self.stack.currentIndex() == PAGES.index("Voice")', self.src)

    def test_enable_uses_the_chosen_mode_and_the_check_never_opens_the_microphone_from_the_window(self):
        self.assertIn('self.ctl.voice_enable(self.vi_mode.currentData() or "wake_word")', self.src)
        win_voice = self.src.split("def _vi_check", 1)[1].split("def _vi_mic_chosen", 1)[0]
        self.assertNotIn("SoundDeviceMic", win_voice)

    def test_viewmodel_wording(self):
        self.assertIn("Choose a voice", VM.preview_text({"state": "idle"}))
        self.assertIn("Could not play", VM.preview_text({"state": "error", "message": "x"}))
        self.assertTrue(VM.preview_text({"state": "finished", "message": "m"}).startswith("Finished."))
        cat = {"categories": {"feminine": [{"id": "a:b", "name": "B", "language_label": "", "language": ""}], "masculine": [], "unspecified": []}}
        self.assertEqual(VM.category_options(cat), [("feminine", "Feminine (1)"), ("masculine", "Masculine (0)")])
        self.assertEqual(VM.voice_items(cat, "feminine"), [("a:b", "B - language not stated")])


if __name__ == "__main__":
    unittest.main()
