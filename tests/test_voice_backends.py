"""Backend argument-handling tests with injected fakes. These do NOT prove real audio, Vosk or SAPI work."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis.voice.backends import BackendUnavailable, SapiSpeaker, SoundDeviceMic, VoskRecognizer
from jarvis.voice.session import MicError


class FakeStream:
    def __init__(self, **kw):
        self.kw, self.started, self.closed = kw, False, False

    def start(self): self.started = True
    def stop(self): self.started = False
    def close(self): self.closed = True
    def read(self, n): return np.zeros((n, 1), dtype=np.int16), False


class FakeSD:
    def __init__(self): self.stream = None
    def query_devices(self): return [{"name": "Speakers", "max_input_channels": 0}, {"name": "USB Mic", "max_input_channels": 1}]
    def InputStream(self, **kw):
        self.stream = FakeStream(**kw)
        return self.stream


class MicTests(unittest.TestCase):
    def test_lists_only_input_devices_and_opens_16k_mono(self):
        sd = FakeSD(); mic = SoundDeviceMic(sd)
        self.assertEqual([d["name"] for d in mic.devices()], ["USB Mic"])
        mic.open("usb")
        self.assertEqual((sd.stream.kw["samplerate"], sd.stream.kw["channels"], sd.stream.kw["device"]), (16000, 1, 1))
        self.assertEqual(mic.read().shape, (480,))
        mic.close()
        self.assertTrue(sd.stream.closed)                     # stream released, not merely ignored
        with self.assertRaises(MicError):
            mic.read()

    def test_unknown_device_and_missing_library(self):
        with self.assertRaises(MicError):
            SoundDeviceMic(FakeSD()).open("nonexistent")
        with self.assertRaises(BackendUnavailable):
            SoundDeviceMic()._lib() if "sounddevice" not in sys.modules else (_ for _ in ()).throw(BackendUnavailable())


class FakeKaldi:
    def __init__(self, model, rate, grammar=None): self.grammar = grammar; self.n = 0
    def AcceptWaveform(self, b): self.n += 1; return self.n % 2 == 0
    def Result(self): return json.dumps({"text": "hey jarvis"})
    def FinalResult(self): return json.dumps({"text": "open notepad"})
    def Reset(self): pass


class FakeVosk:
    Model = staticmethod(lambda p: object())
    KaldiRecognizer = FakeKaldi
    SetLogLevel = staticmethod(lambda n: None)


class VoskTests(unittest.TestCase):
    def test_wake_grammar_limited_and_text_returned(self):
        with tempfile.TemporaryDirectory() as d:
            r = VoskRecognizer(d, ["Hey Jarvis"], FakeVosk)
            f = np.zeros(480, dtype=np.int16)
            self.assertIsNone(r.feed(f, "wake"))
            self.assertEqual(r.feed(f, "wake"), "hey jarvis")
            self.assertEqual(json.loads(r._wake.grammar), ["hey jarvis", "[unk]"])
            self.assertEqual(r.flush(), "open notepad")

    def test_missing_model_is_explained_not_downloaded(self):
        r = VoskRecognizer("/definitely/not/here", ["hey jarvis"], FakeVosk)
        with self.assertRaises(BackendUnavailable) as cm:
            r.feed(np.zeros(480, dtype=np.int16), "wake")
        self.assertIn("does not download", str(cm.exception))


class FakeProc:
    def __init__(self, *a, **kw):
        self.args, self.kw, self.killed = a, kw, False
        self.written = b""
        outer = self
        class In:
            def write(self, b): outer.written += b
            def close(self): pass
        self.stdin = In()
    def poll(self): return -9 if self.killed else None
    def kill(self): self.killed = True


class SapiTests(unittest.TestCase):
    def setUp(self):
        self.procs = []
        def popen(*a, **kw):
            p = FakeProc(*a, **kw); self.procs.append(p); return p
        self.spk = SapiSpeaker(popen=popen, run=None, exe=Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"))

    def test_text_goes_via_stdin_not_command_line_and_settings_via_env(self):
        evil = '"; Remove-Item C:\\ -Recurse; "'
        self.spk.speak(evil, 1.5, 0.5, "Microsoft Hazel Desktop")
        p = self.procs[0]
        self.assertEqual(p.written, evil.encode())
        self.assertNotIn("Remove-Item", " ".join(p.args[0]))
        self.assertEqual(p.kw["env"]["JARVIS_TTS_VOICE"], "Microsoft Hazel Desktop")
        self.assertEqual(p.kw["env"]["JARVIS_TTS_VOLUME"], "50")
        self.assertEqual(p.kw["env"]["JARVIS_TTS_RATE"], "5")
        self.assertTrue(p.args[0][0].lower().endswith("powershell.exe"))

    def test_stop_kills_immediately_and_new_speech_replaces_old(self):
        self.spk.speak("one", 1, 1, None)
        self.assertTrue(self.spk.is_speaking())
        self.spk.stop()
        self.assertTrue(self.procs[0].killed)
        self.assertFalse(self.spk.is_speaking())
        self.spk.speak("a", 1, 1, None)
        self.spk.speak("b", 1, 1, None)
        self.assertTrue(self.procs[1].killed)

    def test_rate_mapping(self):
        self.assertEqual(SapiSpeaker.rate_to_sapi(1.0), 0)
        self.assertEqual(SapiSpeaker.rate_to_sapi(2.0), 10)
        self.assertEqual(SapiSpeaker.rate_to_sapi(0.5), -10)
        self.assertEqual(SapiSpeaker.rate_to_sapi(99), 10)


class CliTests(unittest.TestCase):
    def cli(self, d, *a):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        p = subprocess.run([sys.executable, "-W", "ignore", "-m", "jarvis", "--data-dir", d, *a],
                           capture_output=True, text=True, env=env, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_voice_settings_cli(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _ = self.cli(d, "voice", "show")
            self.assertEqual(json.loads(out.split("\n}")[0] + "\n}")["input_mode"], "off")
            self.assertEqual(self.cli(d, "voice", "set", "input_mode", "wake_word")[0], 0)
            self.assertEqual(self.cli(d, "voice", "set", "rate", "9")[0], 2)
            self.assertEqual(self.cli(d, "voice", "set", "volume", "0.3")[0], 0)
            self.assertEqual(self.cli(d, "voice", "set", "bogus", "1")[0], 2)
            self.assertEqual(self.cli(d, "voice", "pron-add", "SQL", "sequel")[0], 0)
            self.assertIn("sequel", self.cli(d, "voice", "pron-list")[1])

    def test_voice_run_refuses_while_input_off_and_opens_no_mic(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = self.cli(d, "voice", "run", "--vosk-model", d)
            self.assertEqual(rc, 2)
            self.assertIn("off", err.lower())

    def test_voice_backend_missing_is_clean_error(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = self.cli(d, "voice", "devices")
            self.assertIn(rc, (0, 2))        # 2 here (no sounddevice); 0 on a machine that has it
            self.assertNotIn("Traceback", err)

    def test_personality_cli(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.cli(d, "personality", "set-mode", "Concise Operator")[0], 0)
            self.assertIn("Concise Operator", self.cli(d, "personality", "show")[1])
            self.assertEqual(self.cli(d, "personality", "set-trait", "humor", "3")[0], 2)
            self.assertEqual(self.cli(d, "personality", "set-mode", "Nope")[0], 2)
            self.assertIn("Personality is style only", self.cli(d, "personality", "instructions")[1])


if __name__ == "__main__":
    unittest.main()
