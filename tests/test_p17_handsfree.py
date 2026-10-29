"""Prompt 17: automatic voice start, always-listening hands-free loop, privacy, isolation.

EVIDENCE LABELS
  REAL        real code/files on Linux (threads, locks, JSON, sockets blocked). 'REAL (this sandbox)' measures what this machine has.
  FAKE AUDIO  SIMULATED: fake microphone/recognizer/speaker driven by a real worker thread and the real VoiceSession; proves our logic only.
  FAKE MODEL  scripted FakeLlama.
  STATIC      source inspection.
  NOT RUN     real microphone, Vosk model, SAPI speech, Qwen, Qt window displayed, Windows 11. Nothing here claims otherwise."""
import ast
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p17_common import (AppCase, FakeLlama, LOUD, QUIET, Rig, ScriptRec, ThreadMic, ThreadSpk, settle, wait_for)  # noqa: E402
from p10_common import ROOT  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.voice import session as S  # noqa: E402
from jarvis.voice.settings import VoiceSettings  # noqa: E402
from restriction_lifted import setUpModule, tearDownModule  # noqa: E402,F401  (these tests exercise the dormant adult definitions)

SRC = ROOT / "src" / "jarvis"


class AutoStart(AppCase):
    """FAKE AUDIO (threads real): the window calls voice_autostart() once after it is shown."""
    def test_enabled_voice_starts_by_itself_and_reports_listening_only_when_measured(self):
        r = Rig(self)
        self.assertEqual(r.state()["state"], "Off")                      # nothing started yet
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        st = r.state()
        self.assertTrue(st["mic_open"] and st["recognizer_loaded"] and st["thread_alive"])
        self.assertTrue(r.rec.loaded)

    def test_disabled_voice_never_touches_the_microphone(self):
        r = Rig(self, mode="off")
        r.c.voice_autostart()
        time.sleep(0.2)
        self.assertEqual(r.state()["state"], "Off")
        self.assertEqual((r.mic.opens, r.mic.reads), (0, 0))
        self.assertFalse(r.c.voice.alive())

    def test_default_settings_keep_the_microphone_off(self):
        self.assertEqual(VoiceSettings().input_mode, "off")
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        self.assertEqual(c.voice_autostart()["state"], "Off")

    def test_init_is_idempotent_one_thread_one_loop(self):
        r = Rig(self)
        for _ in range(5):
            r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        for _ in range(5):
            r.c.voice_autostart()
            r.c.voice_enable("wake_word")
        voice_threads = [t for t in r.voice_threads() if t.name == "jarvis-voice"]      # this test's own threads only
        self.assertEqual(len(voice_threads), 1)
        self.assertEqual(r.mic.opens, 1)

    def test_initialisation_runs_off_the_calling_thread(self):
        """The interface thread must not wait for the model to load: autostart returns while preload is still blocked."""
        gate = threading.Event()
        rec = ScriptRec()
        orig = rec.preload

        def slow():
            gate.wait(5)
            orig()
        rec.preload = slow
        r = Rig(self, rec=rec)
        t0 = time.time()
        st = r.c.voice_autostart()
        self.assertLess(time.time() - t0, 0.5)
        self.assertEqual(st["state"], "Starting")
        self.assertNotEqual(r.state()["state"], "Listening")             # never Listening before the model is loaded
        gate.set()
        self.assertTrue(r.wait_state("Listening"))

    def test_guest_session_never_starts_voice(self):
        c = AppController(self.cfg, guest=True, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        self.assertIsNone(c.voice_autostart())
        self.assertEqual(c.voice_enable("wake_word")["state"], "Unavailable")

    def test_muted_setting_survives_restart_and_nothing_is_captured(self):
        r = Rig(self, settings={"muted": True})
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Muted"))
        time.sleep(0.15)
        self.assertEqual((r.mic.opens, r.mic.reads), (0, 0))              # muted at startup: the device is never opened
        self.assertTrue(r.state()["mic_muted"])


class Conversation(AppCase):
    """FAKE AUDIO + FAKE MODEL: wake phrase -> question -> spoken answer -> back to listening, repeated without any button."""
    def test_three_questions_without_pressing_anything(self):
        r = Rig(self)
        FakeLlama.reply = "Certainly. Here is the answer."
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        for i in range(3):
            r.ask(f"question number {i} please")
            self.assertTrue(wait_for(lambda i=i: len(r.spk.said) == i + 1, 6, r.c), f"turn {i} not spoken")
            self.assertTrue(r.wait_state("Listening"), f"not back to listening after turn {i}")
        self.assertEqual(len(r.spk.said), 3)
        self.assertIn("Certainly", r.spk.said[0][0])
        settle(r.c)
        users = [e.text for e in r.c.transcript.entries if e.kind == "user"]
        self.assertEqual(len(users), 3)
        self.assertTrue(all(u.startswith("(voice) ") for u in users))
        self.assertEqual(len([e for e in r.c.transcript.entries if e.kind == "assistant"]), 3)

    def test_voice_uses_the_selected_personality_prompt(self):
        r = Rig(self)
        r.c.persona_set("coach")
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.ask("help me stay on track today")
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        sent = json.dumps(FakeLlama.instances[-1].calls[-1])
        self.assertIn("coach", sent.lower())
        from jarvis.personality.persona import SAFETY_BLOCK
        self.assertIn(SAFETY_BLOCK.strip()[:40], sent)                    # the fixed safety block rides along on the voice path too

    def test_speech_rate_follows_the_style(self):
        r = Rig(self)
        r.c.persona_set("calm")
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.ask("help me relax now")
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        self.assertLess(r.spk.said[0][1], 1.0)                            # calm speaks slower than the saved rate (first turn uses the hint after the model ran)

    def test_code_and_urls_are_not_read_aloud(self):
        r = Rig(self)
        FakeLlama.reply = "Run this:\n```python\nprint('secret')\n```\nSee https://example.com/very/long/path for details."
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.ask("show me some code please")
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        said = r.spk.said[0][0]
        self.assertNotIn("print", said)
        self.assertNotIn("https://", said)

    def test_text_only_degradation_when_speaker_missing(self):
        """No local TTS: Listening still works, replies are text, and the state says why."""
        r = Rig(self)
        with mock.patch.object(r.c.speech, "available", return_value=(False, "Speech output needs Windows (SAPI through PowerShell).")):
            r.c.voice_autostart()
            self.assertTrue(r.wait_state("Listening"))
            r.ask("what is two plus two")
            self.assertTrue(wait_for(lambda: any(e.kind == "assistant" for e in r.c.transcript.entries), 6, r.c))
        self.assertEqual(r.spk.said, [])
        self.assertIn("text only", r.state()["note"])

    def test_spoken_normal_returns_to_normal_and_is_not_sent_as_roleplay(self):
        r = Rig(self)
        r.c.persona_adult(True, confirmed=True)
        r.c.persona_set("savage")
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.ask("return to normal")
        self.assertTrue(wait_for(lambda: r.c.persona_state()["settings"]["style"] == "normal", 6, r.c))
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        calls = sum(len(i.calls) for i in FakeLlama.instances)
        self.assertEqual(calls, 0, "the exact /normal command must be handled without the model")

    def test_busy_typed_chat_blocks_a_voice_turn_politely(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.c.busy = True
        r.ask("are you there right now")
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        self.assertIn("still answering", r.spk.said[0][0])
        r.c.busy = False

    def test_model_missing_is_spoken_as_a_real_reason_not_a_fake_answer(self):
        r = Rig(self, engine=False)
        import shutil
        shutil.rmtree(self.cfg.models_dir, ignore_errors=True)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.ask("hello there my friend")
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        self.assertNotIn("Hello from the scripted", r.spk.said[0][0])


class Controls(AppCase):
    """FAKE AUDIO: mute, disable, cancel, retry, shutdown."""
    def listening(self, **kw):
        r = Rig(self, **kw)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        return r

    def test_mic_mute_really_stops_capture(self):
        r = self.listening()
        reads = r.mic.reads
        r.c.voice_mute()
        self.assertTrue(r.wait_state("Muted"))
        self.assertTrue(wait_for(lambda: not r.mic.is_open, 3))
        time.sleep(0.1)
        after = r.mic.reads
        time.sleep(0.15)
        self.assertEqual(r.mic.reads, after, "frames are still being read while muted")
        self.assertFalse(r.state()["mic_open"])
        self.assertGreater(r.mic.closes, 0)
        r.rec.say("wake", "hey jarvis")                                    # even a perfect wake phrase does nothing while muted
        time.sleep(0.2)
        self.assertEqual(r.state()["state"], "Muted")

    def test_mute_is_saved_and_unmute_resumes_listening(self):
        r = self.listening()
        r.c.voice_mute()
        self.assertTrue(r.wait_state("Muted"))
        self.assertTrue(VoiceSettings.load(self.cfg.profile_dir / "voice.json").muted)
        r.c.voice_unmute()
        self.assertTrue(r.wait_state("Listening"))
        self.assertFalse(VoiceSettings.load(self.cfg.profile_dir / "voice.json").muted)

    def test_speaker_mute_is_separate_from_mic_mute(self):
        r = self.listening()
        r.c.voice_speaker_mute(True)
        self.assertEqual(r.state()["state"], "Listening")                  # still listening
        self.assertTrue(r.state()["speaker_muted"] and not r.state()["mic_muted"])
        r.ask("say something to me please")
        self.assertTrue(wait_for(lambda: any(e.kind == "assistant" for e in r.c.transcript.entries), 6, r.c))
        self.assertEqual(r.spk.said, [])                                   # text shown, nothing spoken
        self.assertTrue(r.wait_state("Listening"))
        r.c.voice_speaker_mute(False)
        r.ask("and now speak to me again")
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))

    def test_speaker_mute_silences_speech_in_progress(self):
        spk = ThreadSpk(duration=5.0)
        r = self.listening(spk=spk)
        r.ask("tell me a long story please")
        self.assertTrue(wait_for(lambda: spk.said, 6, r.c))
        stops = spk.stops
        r.c.voice_speaker_mute(True)
        self.assertGreater(spk.stops, stops)

    def test_disable_releases_the_microphone_thread_and_lock(self):
        r = self.listening()
        r.c.voice_disable()
        self.assertTrue(wait_for(lambda: not r.mic.is_open, 3))
        self.assertEqual([t.name for t in r.voice_threads_after_exit() if t.name == "jarvis-voice"], [])
        self.assertFalse((self.cfg.profile_dir / "mic.lock").exists())
        self.assertEqual(r.state()["state"], "Off")
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").input_mode, "off")
        reads = r.mic.reads
        time.sleep(0.15)
        self.assertEqual(r.mic.reads, reads)
        r.c.voice_autostart()                                              # disabled stays disabled after a "restart"
        time.sleep(0.1)
        self.assertEqual(r.mic.opens, 1)

    def test_shutdown_closes_everything_without_leaks(self):
        r = self.listening()
        r.ask("one question before closing")
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        r.c.shutdown(3.0)
        self.assertFalse(r.c.voice_thread_leaked)
        self.assertFalse(r.mic.is_open)
        self.assertEqual([t.name for t in r.voice_threads_after_exit()], [])
        self.assertFalse((self.cfg.profile_dir / "mic.lock").exists())

    def test_cancel_stops_speech_immediately_and_returns_to_listening(self):
        spk = ThreadSpk(duration=30.0)
        r = self.listening(spk=spk)
        r.ask("give me a very long answer")
        self.assertTrue(wait_for(lambda: spk.said, 6, r.c))
        self.assertTrue(r.wait_state("Speaking"))
        stops = spk.stops
        r.c.stop_speaking()
        self.assertGreater(spk.stops, stops)                                # direct stop, not queued behind the loop
        self.assertTrue(r.wait_state("Listening"))

    def test_stop_and_return_to_normal_and_emergency_stop_silence_voice(self):
        for how in ("stop_generation", "persona_normal", "emergency_stop"):
            with self.subTest(how=how):
                spk = ThreadSpk(duration=30.0)
                r = self.listening(spk=spk)
                r.ask("tell me something long please")
                self.assertTrue(wait_for(lambda: spk.said, 6, r.c))
                stops = spk.stops
                getattr(r.c, how)()
                self.assertGreater(spk.stops, stops)
                self.assertTrue(r.wait_state("Listening"))
                r.c.shutdown(2.0)

    def test_cancel_during_inference_abandons_the_reply_and_speaks_nothing(self):
        gate = threading.Event()
        orig = FakeLlama.create_chat_completion

        def slow(self_, messages, max_tokens, temperature, stream=False):
            self_.calls.append(messages)
            gate.wait(3)
            return iter([{"choices": [{"delta": {"content": "late answer"}}]}])
        with mock.patch.object(FakeLlama, "create_chat_completion", slow):
            r = self.listening()
            self.addCleanup(gate.set)       # runs BEFORE the controller shutdown: a failed early assertion must not leave the chat thread blocked in gate.wait(3)
            r.ask("a question that takes a long time")
            self.assertTrue(r.wait_state("Processing"))
            r.c.stop_speaking()
            gate.set()
            self.assertTrue(r.wait_state("Listening"))
            end = time.time() + 5
            while r.c.busy and time.time() < end:      # the worker thread may need longer than 0.2 s on a loaded Windows runner
                time.sleep(0.05)
        self.assertEqual(r.spk.said, [])
        self.assertFalse(r.c.busy)

    def test_mute_during_inference_leaves_the_mic_closed_and_state_muted(self):
        gate = threading.Event()

        def slow(self_, messages, max_tokens, temperature, stream=False):
            self_.calls.append(messages)
            gate.wait(3)
            return iter([{"choices": [{"delta": {"content": "late answer"}}]}])
        with mock.patch.object(FakeLlama, "create_chat_completion", slow):
            r = self.listening()
            self.addCleanup(gate.set)       # runs BEFORE the controller shutdown: a failed early assertion must not leave the chat thread blocked in gate.wait(3)
            r.ask("a question that takes a long time")
            self.assertTrue(r.wait_state("Processing"))
            r.c.voice_mute()
            gate.set()
            self.assertTrue(r.wait_state("Muted"))
            time.sleep(0.2)
        self.assertFalse(r.mic.is_open)
        self.assertEqual(r.spk.said, [])
        self.assertEqual(r.state()["state"], "Muted")

    def test_push_to_talk_mode_needs_no_wake_phrase(self):
        r = Rig(self, mode="push_to_talk")
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.c.voice_ptt(True)
        self.assertTrue(wait_for(lambda: r.c.voice._session.state == S.CAPTURING, 3, r.c))
        r.rec.flush_text = "what time is it now"
        r.c.voice_ptt(False)
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))


class Failures(AppCase):
    """FAKE AUDIO (+ REAL (this sandbox) for the real backends): every failure is reported with its reason and never shows Listening."""
    def test_no_microphone_device(self):
        mic = ThreadMic()
        mic.device_list = []
        r = Rig(self, mic=mic)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Unavailable"))
        st = r.state()
        self.assertIn("No microphone", st["detail"])
        self.assertTrue(st["can_retry"] and not st["mic_open"])
        self.assertEqual([t.name for t in r.voice_threads_after_exit() if t.name == "jarvis-voice"], [])
        self.assertFalse((self.cfg.profile_dir / "mic.lock").exists())

    def test_missing_speech_model_files(self):
        r = Rig(self, rec=ScriptRec(fail_preload="Vosk could not load the model folder X."))
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Unavailable"))
        self.assertIn("Vosk", r.state()["detail"])
        self.assertEqual(r.mic.opens, 0)                                   # never opened the microphone for a recognizer that cannot work
        self.assertNotEqual(r.state()["state"], "Listening")

    def test_real_backends_in_this_sandbox_report_why(self):
        """REAL (this sandbox): no PortAudio / Vosk here. Default factories must yield Unavailable with an installable reason, not a crash."""
        self.install_model()
        VoiceSettings(input_mode="wake_word", vosk_model=str(self.d / "nope")).save(self.cfg.profile_dir / "voice.json")
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        c.voice_autostart()
        self.assertTrue(wait_for(lambda: c.voice_state()["state"] == "Unavailable", 5, c))
        why = c.voice_state()["detail"].lower()
        self.assertTrue("sounddevice" in why or "portaudio" in why or "microphone" in why or "vosk" in why, why)

    def test_vosk_folder_not_set_gives_instructions(self):
        VoiceSettings(input_mode="wake_word").save(self.cfg.profile_dir / "voice.json")
        mic = ThreadMic()
        c = AppController(self.cfg, simulate=True, mic_factory=lambda: mic)
        self.addCleanup(c.shutdown, 1.0)
        c.voice_autostart()
        self.assertTrue(wait_for(lambda: c.voice_state()["state"] == "Unavailable", 5, c))
        self.assertIn("jarvis voice set vosk_model", c.voice_state()["detail"])

    def test_retry_after_fixing_the_cause_reaches_listening(self):
        mic = ThreadMic()
        mic.device_list = []
        r = Rig(self, mic=mic)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Unavailable"))
        mic.device_list = [{"index": 0, "name": "Fake Mic", "channels": 1}]
        r.c.voice_retry()
        self.assertTrue(r.wait_state("Listening"))

    def test_microphone_unplugged_while_listening_recovers(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.mic.fail_reads = True
        self.assertTrue(wait_for(lambda: r.state()["state"] != "Listening", 3, r.c))
        self.assertFalse(r.state()["mic_open"])                            # never "Listening" with a dead device
        self.assertIn(r.state()["state"], ("Starting", "Unavailable"))
        r.mic.fail_reads = False
        self.assertTrue(r.wait_state("Listening", 8))                      # first retry delay is 1 s

    def test_microphone_that_stays_dead_ends_unavailable_not_listening(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        with mock.patch.object(S, "RETRY_DELAYS", (0.01, 0.01)):
            r.mic.fail_reads = True
            r.mic.fail_open = True
            self.assertTrue(r.wait_state("Unavailable", 8))
        self.assertFalse(r.state()["mic_open"])
        self.assertTrue(r.state()["can_retry"])

    def test_another_process_holding_the_microphone_lock(self):
        r = Rig(self)
        lock = self.cfg.profile_dir / "mic.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        import os
        lock.write_text(json.dumps({"pid": os.getppid(), "since": "x"}))
        with mock.patch("jarvis.app.lifecycle._pid_alive", return_value=True):
            r.c.voice_autostart()
            self.assertTrue(r.wait_state("Unavailable"))
        self.assertIn("already using the microphone", r.state()["detail"])
        self.assertEqual(r.mic.opens, 0)

    def test_responder_crash_does_not_kill_the_loop(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        with mock.patch.object(FakeLlama, "create_chat_completion", side_effect=RuntimeError("boom")):
            r.ask("please crash the model now")
            self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        self.assertNotIn("boom", r.spk.said[0][0])                         # internal errors are not read aloud
        self.assertTrue(r.wait_state("Listening"))


# ----------------------------------------------------------------------------------------------------------------------------------------
# Session-level guards (FAKE AUDIO, synchronous frame stepping: deterministic)
# ----------------------------------------------------------------------------------------------------------------------------------------
from test_voice import make, run  # noqa: E402
from test_voice import LOUD as VLOUD, QUIET as VQUIET  # noqa: E402


class SessionGuards(unittest.TestCase):
    def test_wake_activation_and_return_to_listening(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0, post_speech_guard_seconds=0.0)
        sess.start()
        rec.queue.append(("wake", "hey jarvis what time is it"))
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(sess.state, S.SPEAKING)
        spk.speaking = False
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(sess.state, S.LISTENING)

    def test_false_wake_phrases_do_not_activate(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        for text in ("hey there", "jarvis", "hello world", "he jar", "okay google", "hey service"):
            rec.queue.append(("wake", text))
            run(sess, clk, VQUIET, 0.1)
            self.assertEqual(sess.state, S.LISTENING, text)
        self.assertEqual(spk.said, [])

    def test_empty_transcription_returns_to_listening(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        rec.queue.append(("wake", "hey jarvis"))
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(sess.state, S.CAPTURING)
        run(sess, clk, VLOUD, 0.3)
        rec.flush_text = ""
        run(sess, clk, VQUIET, 1.2)                                        # silence ends the utterance; the recognizer returns nothing
        self.assertEqual(sess.state, S.LISTENING)
        self.assertEqual(spk.said, [])

    def test_no_speech_after_wake_times_out(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        rec.queue.append(("wake", "hey jarvis"))
        run(sess, clk, VQUIET, 0.1)
        run(sess, clk, VQUIET, S.NO_SPEECH_TIMEOUT + 0.5)
        self.assertEqual(sess.state, S.LISTENING)

    def test_duplicate_utterance_is_not_processed_twice(self):
        calls = []
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0, post_speech_guard_seconds=0.0, reply=lambda t: (calls.append(t), "Done.")[1])
        sess.start()
        rec.queue.append(("wake", "hey jarvis open the report"))
        run(sess, clk, VQUIET, 0.1)
        spk.speaking = False
        run(sess, clk, VQUIET, 0.1)
        rec.queue.append(("wake", "hey jarvis open the report"))           # the same words again within 2 s (an echo or a recogniser repeat)
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(len(calls), 1)
        self.assertIn("duplicate utterance", [p for k, p in ev if k == "ignored"])

    def test_same_words_later_are_accepted(self):
        calls = []
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0, post_speech_guard_seconds=0.0, reply=lambda t: (calls.append(t), "Done.")[1])
        sess.start()
        rec.queue.append(("wake", "hey jarvis open the report"))
        run(sess, clk, VQUIET, 0.1)
        spk.speaking = False
        run(sess, clk, VQUIET, 2.5)
        rec.queue.append(("wake", "hey jarvis open the report"))
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(len(calls), 2)

    def test_jarvis_does_not_answer_its_own_voice(self):
        calls = []
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=8.0, post_speech_guard_seconds=0.0,
                                            reply=lambda t: (calls.append(t), "The meeting starts at three in the afternoon today.")[1])
        sess.start()
        rec.queue.append(("wake", "hey jarvis when is the meeting"))
        run(sess, clk, VQUIET, 0.1)
        spk.speaking = False
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(sess.state, S.FOLLOW_UP)
        run(sess, clk, VLOUD, 0.3)                                         # the speaker's echo reaches the microphone and is "recognised"
        rec.queue.append(("dictation", "the meeting starts at three in the afternoon"))
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(len(calls), 1, "Jarvis answered its own spoken reply")
        self.assertTrue(any(k == "ignored" for k, _ in ev))

    def test_wake_phrase_inside_its_own_reply_does_not_self_trigger_or_interrupt(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0, barge_in="wake_word_only", post_speech_guard_seconds=0.0,
                                            reply="You can always say hey jarvis to wake me.")
        sess.start()
        rec.queue.append(("wake", "hey jarvis tell me how to wake you"))
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(sess.state, S.SPEAKING)
        stops = spk.stops
        rec.queue.append(("wake", "hey jarvis"))                           # heard from the speaker, not the user
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(sess.state, S.SPEAKING)
        self.assertEqual(spk.stops, stops)

    def test_real_wake_word_barge_in_still_interrupts_other_speech(self):
        """SIMULATED barge-in: a wake phrase that is NOT part of the reply stops the speech and starts listening."""
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0, barge_in="wake_word_only", post_speech_guard_seconds=0.0, reply="The weather is mild today.")
        sess.start()
        rec.queue.append(("wake", "hey jarvis weather"))
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(sess.state, S.SPEAKING)
        rec.queue.append(("wake", "hey jarvis"))
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(sess.state, S.CAPTURING)
        self.assertFalse(spk.speaking)

    def test_frames_right_after_speech_are_ignored(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=8.0, post_speech_guard_seconds=0.6)
        sess.start()
        rec.queue.append(("wake", "hey jarvis hello"))
        run(sess, clk, VQUIET, 0.1)
        spk.speaking = False
        run(sess, clk, VQUIET, 0.06)
        self.assertEqual(sess.state, S.FOLLOW_UP)
        fed = rec.resets
        run(sess, clk, VLOUD, 0.3)                                         # inside the 0.6 s guard: ignored entirely
        self.assertFalse(sess._heard_speech)
        run(sess, clk, VQUIET, 0.5)
        run(sess, clk, VLOUD, 0.1)                                         # after the guard: heard
        self.assertTrue(sess._heard_speech)

    def test_speaker_muted_session_speaks_nothing_but_answers_in_text(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0, speaker_muted=True)
        sess.start()
        rec.queue.append(("wake", "hey jarvis hello there"))
        run(sess, clk, VQUIET, 0.1)
        self.assertEqual(spk.said, [])
        self.assertTrue(any(k == "reply_text" for k, _ in ev))
        self.assertEqual(sess.state, S.LISTENING)

    def test_mute_through_session_closes_device_and_ignores_wake(self):
        sess, mic, rec, spk, clk, ev = make()
        sess.start()
        sess.mute()
        self.assertFalse(sess.mic_open)
        rec.queue.append(("wake", "hey jarvis"))
        for _ in range(10):
            sess.tick()
        self.assertEqual(sess.state, S.MUTED)

    def test_reply_arriving_after_mute_is_dropped(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0)
        sess.start()
        sess._set(S.THINKING)
        sess.mute()
        sess._deliver("late reply")
        self.assertEqual(spk.said, [])
        self.assertEqual(sess.state, S.MUTED)


class SettingsAndBackends(unittest.TestCase):
    def test_new_settings_validate(self):
        for bad in ({"speaker_muted": "yes"}, {"muted": 1}, {"post_speech_guard_seconds": -1}, {"post_speech_guard_seconds": 99}, {"post_speech_guard_seconds": True}):
            with self.assertRaises(ValueError, msg=str(bad)):
                VoiceSettings(**bad).validate()
        VoiceSettings(speaker_muted=True, muted=True, post_speech_guard_seconds=0).validate()

    def test_old_voice_json_without_new_fields_still_loads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "voice.json"
            p.write_text(json.dumps({"input_mode": "wake_word", "rate": 1.2}))
            vs = VoiceSettings.load(p)
            self.assertEqual((vs.input_mode, vs.rate, vs.speaker_muted, vs.muted), ("wake_word", 1.2, False, False))

    def test_vosk_preload_reports_a_broken_model_folder(self):
        import tempfile
        from jarvis.voice.backends import BackendUnavailable, VoskRecognizer
        fake = mock.MagicMock()
        fake.Model.side_effect = Exception("Failed to create a model")
        with tempfile.TemporaryDirectory() as d:
            rec = VoskRecognizer(d, ["hey jarvis"], vosk_module=fake)
            with self.assertRaises(BackendUnavailable) as cm:
                rec.preload()
            self.assertIn("unpacked Vosk model", str(cm.exception))

    def test_vosk_preload_missing_folder_and_package(self):
        from jarvis.voice.backends import BackendUnavailable, VoskRecognizer
        with self.assertRaises(BackendUnavailable):
            VoskRecognizer(self.id() + "/nope", ["hey jarvis"], vosk_module=mock.MagicMock()).preload()
        with mock.patch.dict(sys.modules, {"vosk": None}):
            with self.assertRaises(BackendUnavailable):
                VoskRecognizer("/tmp", ["hey jarvis"]).preload()

    def test_vosk_preload_loads_once_and_feeds(self):
        import tempfile
        from jarvis.voice.backends import VoskRecognizer
        fake = mock.MagicMock()
        with tempfile.TemporaryDirectory() as d:
            rec = VoskRecognizer(d, ["hey jarvis"], vosk_module=fake)
            rec.preload()
            rec.preload()
            self.assertEqual(fake.Model.call_count, 1)


class StartupSetting(AppCase):
    """REAL logic with an injected in-memory registry (the real Windows registry was NOT touched: NOT RUN on Windows)."""
    class Reg:
        def __init__(self):
            self.values = {}
            self.calls = []

        def get(self, n):
            self.calls.append(("get", n))
            return self.values.get(n)

        def set(self, n, v):
            self.calls.append(("set", n))
            self.values[n] = v

        def delete(self, n):
            self.calls.append(("delete", n))
            return self.values.pop(n, None) is not None

    def test_off_by_default_and_not_supported_off_windows(self):
        from jarvis.app.startup import Startup
        st = Startup(platform="linux").status()
        self.assertFalse(st["supported"] or st["enabled"])
        r = self.Reg()
        self.assertFalse(Startup(backend=r).status()["enabled"])
        self.assertEqual([c for c in r.calls if c[0] != "get"], [])

    def test_enable_needs_explicit_confirmation(self):
        from jarvis.app.startup import Startup
        r = self.Reg()
        with self.assertRaises(PermissionError):
            Startup(backend=r).enable()
        with self.assertRaises(PermissionError):
            Startup(backend=r).enable(confirm=False)
        self.assertEqual(r.values, {})

    def test_enable_writes_one_visible_value_and_disable_removes_it(self):
        from jarvis.app.startup import VALUE_NAME, Startup
        r = self.Reg()
        s = Startup(backend=r, command='"C:\\Jarvis\\jarvis-gui.exe"')
        st = s.enable(confirm=True)
        self.assertTrue(st["enabled"])
        self.assertEqual(list(r.values), [VALUE_NAME])
        self.assertIn("jarvis-gui.exe", r.values[VALUE_NAME])
        self.assertNotIn("--confirm", r.values[VALUE_NAME])
        st = s.disable()
        self.assertFalse(st["enabled"])
        self.assertEqual(r.values, {})
        s.disable()                                                        # disabling twice is safe

    def test_startup_does_not_enable_the_microphone(self):
        from jarvis.app.startup import Startup
        r = self.Reg()
        VoiceSettings().save(self.cfg.profile_dir / "voice.json")
        Startup(backend=r).enable(confirm=True)
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").input_mode, "off")

    def test_controller_start_with_windows_is_consent_gated_and_guest_blocked(self):
        r = self.Reg()
        c = AppController(self.cfg, simulate=True, startup_backend=r)
        self.addCleanup(c.shutdown, 1.0)
        self.assertFalse(c.startup_state()["enabled"])
        with self.assertRaises(PermissionError):
            c.startup_enable()
        self.assertEqual(r.values, {})
        c.startup_enable(confirmed=True)
        self.assertTrue(c.startup_state()["enabled"])
        c.startup_disable()
        self.assertFalse(c.startup_state()["enabled"])
        g = AppController(self.cfg, guest=True, simulate=True, startup_backend=r)
        self.addCleanup(g.shutdown, 1.0)
        with self.assertRaises(ValueError):
            g.startup_enable(confirmed=True)

    def test_opening_the_window_controller_never_touches_the_registry(self):
        r = self.Reg()
        c = AppController(self.cfg, simulate=True, startup_backend=r)
        self.addCleanup(c.shutdown, 1.0)
        c.voice_autostart()
        c.readiness()
        self.assertEqual([x for x in r.calls if x[0] in ("set", "delete")], [])

    def test_cli_enable_without_confirm_changes_nothing(self):
        from jarvis import cli
        with mock.patch("jarvis.app.startup.Startup.enable", side_effect=PermissionError("needs confirmation")) as m:
            rc = cli.main(["startup", "enable"])
        self.assertEqual(rc, 2)
        self.assertIs(m.call_args.kwargs.get("confirm"), False)

    def test_registry_backend_uses_only_the_current_user_run_key(self):
        src = (SRC / "app" / "startup.py").read_text()
        self.assertIn("HKEY_CURRENT_USER", src)
        for bad in ("HKEY_LOCAL_MACHINE", "schtasks", "sc create", "Task Scheduler", "RunOnce", "Services"):
            self.assertNotIn(bad, src)


class PrivacyAndIsolation(AppCase):
    """STATIC + REAL."""
    VOICE_FILES = [SRC / "app" / "voice_service.py", SRC / "app" / "speech.py", *sorted((SRC / "voice").glob("*.py"))]

    def test_only_the_service_and_backends_open_the_microphone(self):
        hits = []
        for f in SRC.rglob("*.py"):
            t = f.read_text(encoding="utf-8")
            if "InputStream" in t or "SoundDeviceMic(" in t:
                hits.append(f.relative_to(SRC).as_posix())
        self.assertEqual(sorted(hits), sorted(["app/readiness.py", "app/voice_service.py", "cli.py", "voice/backends.py", "voice/inputcheck.py"]), hits)
        self.assertNotIn(".open(", (SRC / "voice" / "inputcheck.py").read_text())      # the read-only microphone check lists devices and never opens one
        self.assertNotIn("SoundDeviceMic().open", (SRC / "app" / "readiness.py").read_text())
        self.assertNotIn(".open(", (SRC / "app" / "readiness.py").read_text())
        win = (SRC / "ui" / "window.py").read_text()
        for bad in ("sounddevice", "SoundDevice", "InputStream", "vosk"):
            self.assertNotIn(bad, win)

    def test_no_raw_audio_is_written_anywhere(self):
        forbidden = ("import wave", "from wave", "soundfile", "scipy.io.wavfile", ".wav", "tofile(", "np.save", "numpy.save", "write_bytes", "pyaudio", "audio_log", "recordings")
        for f in self.VOICE_FILES:
            t = f.read_text(encoding="utf-8")
            for bad in forbidden:
                self.assertNotIn(bad, t, f"{f.name} mentions {bad}")
            tree = ast.parse(t)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open" and len(node.args) > 1 and "w" in str(getattr(node.args[1], "value", "")):
                    self.fail(f"{f.name} opens a file for writing")

    def test_a_conversation_leaves_no_audio_files_in_the_profile(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.ask("record nothing about this please")
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        r.c.shutdown(2.0)
        files = [p.suffix.lower() for p in self.d.rglob("*") if p.is_file()]
        for ext in (".wav", ".mp3", ".flac", ".ogg", ".raw", ".pcm", ".m4a"):
            self.assertNotIn(ext, files)

    def test_no_network_modules_in_voice_code(self):
        for f in self.VOICE_FILES:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                for m in mods:
                    self.assertNotIn(m.split(".")[0], {"socket", "urllib", "requests", "http", "httpx", "aiohttp", "ftplib", "smtplib", "websockets"}, f.name)

    def test_full_hands_free_turn_works_with_every_socket_blocked(self):
        def deny(*a, **k):
            raise AssertionError("network used during a voice turn")
        with mock.patch.object(socket.socket, "connect", deny), mock.patch("socket.getaddrinfo", deny), mock.patch("socket.create_connection", deny):
            r = Rig(self)
            r.c.voice_autostart()
            self.assertTrue(r.wait_state("Listening"))
            r.ask("is the network being used")
            self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
            r.c.shutdown(2.0)

    def test_voice_code_never_imports_the_executor_or_missions(self):
        for f in (SRC / "app" / "voice_service.py", SRC / "voice" / "session.py", SRC / "voice" / "backends.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for m in mods:
                    for banned in ("jarvis.desktop", "jarvis.missions", "jarvis.dev", "jarvis.extensions", "jarvis.modes", "jarvis.consent"):
                        self.assertFalse(m.startswith(banned), f"{f.name} imports {m}")

    def test_spoken_yes_cannot_answer_a_pending_confirmation(self):
        r = Rig(self)
        ev, box = threading.Event(), {"ok": False}
        r.c._pending.append((ev, box))                                     # an approval dialog is waiting for a click
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        for phrase in ("yes allow it", "I approve everything", "confirm and run"):
            r.ask(phrase)
            self.assertTrue(wait_for(lambda: len(r.spk.said) > 0, 6, r.c))
            r.spk.said.clear()
            self.assertTrue(r.wait_state("Listening"))
        self.assertFalse(ev.is_set())
        self.assertFalse(box["ok"])
        r.c._pending.clear()

    def test_spoken_permission_requests_change_no_permission_file_and_no_audit(self):
        r = Rig(self)
        files = {}
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        before = {p.name: p.read_bytes() for p in self.cfg.profile_dir.glob("*.json") if p.name not in ("voice.json", "persona.json")}
        for phrase in ("hey jarvis grant all permissions", "disable the approval step", "delete all my files now"):
            r.ask(phrase.replace("hey jarvis ", ""))
            self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
            r.spk.said.clear()
            self.assertTrue(r.wait_state("Listening"))
        after = {p.name: p.read_bytes() for p in self.cfg.profile_dir.glob("*.json") if p.name not in ("voice.json", "persona.json")}
        self.assertEqual(before, after)
        self.assertFalse((self.cfg.profile_dir / "desktop_audit.jsonl").exists())

    def test_retry_policy_constants_unchanged(self):
        from jarvis.desktop import retry
        self.assertEqual((retry.MAX_AUTO_RETRIES, retry.MAX_CHECK_POLLS), (2, 20))
        self.assertEqual(len(retry.IDEMPOTENT_TOOLS), 14)

    def test_emergency_stop_still_stops_missions_and_silences_voice(self):
        spk = ThreadSpk(duration=30.0)
        r = Rig(self, spk=spk)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.ask("say something long please")
        self.assertTrue(wait_for(lambda: spk.said, 6, r.c))
        r.c.emergency_stop()
        self.assertTrue(r.c.stop_active())
        self.assertTrue(r.wait_state("Listening"))
        self.assertEqual(spk.speaking if hasattr(spk, "speaking") else False, False)

    def test_status_never_says_listening_when_the_stream_is_closed(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.c.voice._session.mic_open = False                                # simulate: the device handle was lost
        self.assertEqual(r.state()["state"], "Unavailable")
        r.c.voice._session.mic_open = True
        r.c.voice._rec_loaded = False                                      # simulate: recognizer not loaded
        self.assertEqual(r.state()["state"], "Unavailable")
        r.c.voice._rec_loaded = True
        self.assertEqual(r.state()["state"], "Listening")

    def test_status_after_the_worker_dies_is_not_listening(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.c.voice._stop.set()
        r.c.voice._thread.join(3)
        self.assertNotEqual(r.state()["state"], "Listening")

    def test_readiness_row_reflects_the_saved_setting(self):
        from jarvis.app import readiness as RD
        VoiceSettings().save(self.cfg.profile_dir / "voice.json")
        self.assertEqual({r["id"]: r for r in RD.voice_levels(self.cfg)}["mic_in_window"]["state"], "no")
        VoiceSettings(input_mode="wake_word").save(self.cfg.profile_dir / "voice.json")
        row = {r["id"]: r for r in RD.voice_levels(self.cfg)}["mic_in_window"]
        self.assertEqual(row["state"], "yes")
        self.assertIn("only the saved setting", row["detail"])
        self.assertEqual({r["id"]: r for r in RD.voice_levels(self.cfg)}["hardware_verified"]["state"], "not verified")


class WindowStatic(unittest.TestCase):
    """STATIC. The Qt window was NEVER displayed (no PySide6/display here)."""
    def test_voice_page_exists_and_every_call_goes_to_a_real_controller_method(self):
        src = (SRC / "ui" / "window.py").read_text()
        tree = ast.parse(src)
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute) and n.value.attr == "ctl"}
        missing = sorted(a for a in used if not hasattr(AppController, a) and a not in ("on_update", "ask_user", "cfg", "transcript", "tasks", "speech", "persona", "voice", "busy", "guest", "loading"))
        self.assertEqual(missing, [])
        for needed in ("voice_state", "voice_enable", "voice_disable", "voice_mute", "voice_unmute", "voice_speaker_mute", "voice_retry", "voice_setting",
                       "startup_state", "startup_enable", "startup_disable", "persona_normal", "stop_speaking"):
            self.assertIn("ctl." + needed, src, needed)
        self.assertIn('"Voice"', src)
        self.assertIn("_refresh_voice", src)

    def test_start_with_windows_checkbox_asks_first_and_defaults_to_no(self):
        src = (SRC / "ui" / "window.py").read_text()
        i = src.index("def _startup_toggle")
        body = src[i:i + 1600]
        self.assertIn("QMessageBox.question", body)
        self.assertIn("QMessageBox.StandardButton.No)", body)
        self.assertIn("confirmed=True", body)

    def test_autostart_is_called_once_after_show_and_only_there(self):
        src = (SRC / "ui" / "app.py").read_text()
        self.assertEqual(src.count("voice_autostart()"), 1)
        self.assertLess(src.index("win.show()"), src.index("voice_autostart()"))
        self.assertNotIn("voice_autostart", (SRC / "ui" / "window.py").read_text())

    def test_window_modules_compile(self):
        import py_compile
        for f in ("ui/window.py", "ui/app.py", "app/voice_service.py", "app/startup.py"):
            py_compile.compile(str(SRC / f), doraise=True)


class ControllerLifecycle(AppCase):
    def test_enable_disable_cycles_do_not_leak_threads_or_locks(self):
        r = Rig(self)
        for _ in range(4):
            r.c.voice_enable("wake_word")
            self.assertTrue(r.wait_state("Listening"))
            r.c.voice_disable()
            self.assertEqual(r.state()["state"], "Off")
        self.assertEqual([t.name for t in r.voice_threads_after_exit() if t.name == "jarvis-voice"], [])
        self.assertEqual(r.mic.opens, r.mic.closes)
        self.assertFalse((self.cfg.profile_dir / "mic.lock").exists())

    def test_settings_are_restored_on_restart_without_silent_changes(self):
        r = Rig(self, settings={"speaker_muted": True, "rate": 1.3, "wake_phrase": "hey computer"})
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.c.shutdown(2.0)
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        self.assertEqual((vs.input_mode, vs.speaker_muted, vs.rate, vs.wake_phrase), ("wake_word", True, 1.3, "hey computer"))
        r2 = Rig(self, settings={"speaker_muted": True, "rate": 1.3, "wake_phrase": "hey computer"})
        r2.c.voice_autostart()
        self.assertTrue(r2.wait_state("Listening"))
        self.assertTrue(r2.state()["speaker_muted"])

    def test_voice_setting_updates_rate_and_validates(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.c.voice_setting(rate=1.5, volume=0.4)
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        self.assertEqual((vs.rate, vs.volume), (1.5, 0.4))
        self.assertEqual(vs.input_mode, "wake_word")                       # unchanged
        with self.assertRaises(ValueError):
            r.c.voice_setting(rate=9)
        with self.assertRaises(ValueError):
            r.c.voice_setting(input_mode="off")

    def test_typed_chat_and_voice_share_one_assistant_and_one_persona(self):
        r = Rig(self)
        r.c.persona_set("coach")
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.ask("hello from voice")
        self.assertTrue(wait_for(lambda: r.spk.said, 6, r.c))
        self.assertIs(r.c.assistant.persona, r.c.persona)
        self.assertEqual(len(FakeLlama.instances), 1)

    def test_closed_controller_refuses_new_voice_turns(self):
        r = Rig(self)
        r.c.shutdown(1.0)
        self.assertFalse(r.c._voice_begin())


class ExtraGuards(AppCase):
    def test_typed_chat_speech_respects_speaker_mute(self):
        """FAKE AUDIO: the speaker mute silences read-aloud replies in typed chat too (it is one speaker)."""
        spk = ThreadSpk()
        VoiceSettings(speak_in_window=True, speaker_muted=True).save(self.cfg.profile_dir / "voice.json")
        c = AppController(self.cfg, simulate=True, speaker_factory=lambda: spk)
        self.addCleanup(c.shutdown, 1.0)
        self.assertFalse(c.speech.speak("hello there"))
        self.assertEqual(spk.said, [])
        VoiceSettings(speak_in_window=True, speaker_muted=False).save(self.cfg.profile_dir / "voice.json")
        self.assertTrue(c.speech.speak("hello there"))

    def test_cli_voice_run_refuses_when_the_window_already_owns_the_microphone(self):
        """REAL lock file: `jarvis voice run` and the window's hands-free voice never open the microphone at the same time."""
        import io
        import os
        from contextlib import redirect_stderr
        from jarvis import cli
        from jarvis.app.lifecycle import InstanceLock
        VoiceSettings(input_mode="wake_word").save(self.cfg.profile_dir / "voice.json")
        lock = InstanceLock(self.cfg.profile_dir / "mic.lock")
        lock.path.write_text(json.dumps({"pid": os.getppid(), "since": "x"}))
        err = io.StringIO()
        with mock.patch("jarvis.app.lifecycle._pid_alive", return_value=True), mock.patch("jarvis.cli._voice_run_locked") as inner, redirect_stderr(err):
            rc = cli._voice_run(mock.MagicMock(guest=False, vosk_model=None), self.cfg, VoiceSettings.load(self.cfg.profile_dir / "voice.json"))
        self.assertEqual(rc, 2)
        inner.assert_not_called()
        self.assertIn("already using the microphone", err.getvalue())

    def test_cli_voice_run_releases_the_lock_when_done(self):
        from jarvis import cli
        VoiceSettings(input_mode="wake_word").save(self.cfg.profile_dir / "voice.json")
        with mock.patch("jarvis.cli._voice_run_locked", return_value=0):
            rc = cli._voice_run(mock.MagicMock(guest=False, vosk_model=None), self.cfg, VoiceSettings.load(self.cfg.profile_dir / "voice.json"))
        self.assertEqual(rc, 0)
        self.assertFalse((self.cfg.profile_dir / "mic.lock").exists())

    def test_reply_is_not_spoken_over_a_user_who_started_talking_during_inference(self):
        """FAKE AUDIO: frames seen while the model is thinking keep the voice detector current."""
        gate = threading.Event()

        def slow(self_, messages, max_tokens, temperature, stream=False):
            self_.calls.append(messages)
            gate.wait(3)
            return iter([{"choices": [{"delta": {"content": "late answer"}}]}])
        with mock.patch.object(FakeLlama, "create_chat_completion", slow):
            r = Rig(self)
            self.addCleanup(gate.set)       # see the other two tests: unblock the fake model before the shutdown cleanup, whatever happens
            r.c.voice_autostart()
            self.assertTrue(r.wait_state("Listening"))
            r.ask("a question that takes a while")
            self.assertTrue(r.wait_state("Processing"))
            r.mic.loud = 400                                               # the user starts talking while the model is still thinking
            time.sleep(0.15)
            gate.set()
            time.sleep(0.4)
        self.assertEqual(r.spk.said, [])


class NegativeControlFollowUps(AppCase):
    """Added after the first negative-control run left four mutations undetected (M7, M16, M31 here; M23 was an equivalent mutation)."""
    def test_session_speaker_mute_silences_speech_in_progress_and_leaves_mic_alone(self):
        sess, mic, rec, spk, clk, ev = make(follow_up_seconds=0, post_speech_guard_seconds=0.0)
        sess.start()
        rec.queue.append(("wake", "hey jarvis tell me something"))
        run(sess, clk, VQUIET, 0.1)
        self.assertTrue(spk.speaking)
        stops = spk.stops
        sess.set_speaker_muted(True)
        self.assertGreater(spk.stops, stops)
        self.assertFalse(spk.speaking)
        self.assertTrue(sess.mic_open)                                     # the microphone is untouched by a speaker mute
        self.assertTrue(sess.s.speaker_muted and not sess.s.muted)

    def test_cancel_while_the_user_is_mid_utterance_returns_to_listening(self):
        r = Rig(self)
        r.c.voice_autostart()
        self.assertTrue(r.wait_state("Listening"))
        r.rec.say("wake", "hey jarvis")
        self.assertTrue(wait_for(lambda: r.c.voice._session.state == S.CAPTURING, 5, r.c))
        r.c.voice.cancel()
        self.assertTrue(wait_for(lambda: r.c.voice._session.state == S.LISTENING, 2, r.c), "cancel did not leave the capture state")

    def test_start_with_windows_checkbox_only_enables_after_a_yes(self):
        src = (SRC / "ui" / "window.py").read_text()
        i = src.index("def _startup_toggle")
        body = src[i:i + 2200]
        gate = body.index("if r != QMessageBox.StandardButton.Yes:")
        self.assertIn("return", body[gate:gate + 140])
        self.assertLess(gate, body.index("startup_enable(confirmed=True)"))


if __name__ == "__main__":
    unittest.main()
