"""Shared fakes for Prompt 17 (FAKE AUDIO: they prove our logic only, never a real microphone, Vosk model or speaker)."""
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402,F401
from test_p10_controller import settle  # noqa: E402,F401
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.voice import session as S  # noqa: E402
from jarvis.voice.settings import VoiceSettings  # noqa: E402

LOUD = (np.ones(480) * 8000).astype(np.int16)
QUIET = (np.ones(480) * 20).astype(np.int16)


class ThreadMic:
    def __init__(self):
        self.loud = 0
        self.is_open = False
        self.opens = self.closes = self.reads = 0
        self.fail_open = False
        self.fail_reads = False
        self.device_list = [{"index": 0, "name": "Fake Mic", "channels": 1}]
        self.lock = threading.Lock()

    def devices(self):
        return list(self.device_list)

    def open(self, device):
        if self.fail_open:
            raise S.MicError("device missing")
        self.is_open = True
        self.opens += 1

    def read(self):
        time.sleep(0.002)
        if not self.is_open:
            raise S.MicError("microphone is not open")
        if self.fail_reads:
            raise S.MicError("device unplugged")
        self.reads += 1
        with self.lock:
            if self.loud > 0:
                self.loud -= 1
                return LOUD
        return QUIET

    def close(self):
        if self.is_open:
            self.closes += 1
        self.is_open = False


class ScriptRec:
    """Returns scripted text. queue entries: (mode, text)."""
    def __init__(self, fail_preload=None):
        self.queue = []
        self.lock = threading.Lock()
        self.flush_text = ""
        self.loaded = False
        self.fail_preload = fail_preload
        self.fed = 0

    def preload(self):
        if self.fail_preload:
            from jarvis.voice.backends import BackendUnavailable
            raise BackendUnavailable(self.fail_preload)
        self.loaded = True

    def say(self, mode, text):
        with self.lock:
            self.queue.append((mode, text))

    def feed(self, frame, mode):
        self.fed += 1
        with self.lock:
            if self.queue and self.queue[0][0] == mode:
                return self.queue.pop(0)[1]
        return None

    def reset(self):
        pass

    def flush(self):
        return self.flush_text


class ThreadSpk:
    def __init__(self, duration=0.05):
        self.said = []
        self.duration = duration
        self._until = 0.0
        self.stops = 0

    def speak(self, text, rate, volume, voice):
        self.said.append((text, rate, volume, voice))
        self._until = time.monotonic() + self.duration

    def stop(self):
        self.stops += 1
        self._until = 0.0

    def is_speaking(self):
        return time.monotonic() < self._until


def wait_for(pred, timeout=6.0, ctl=None):
    end = time.time() + timeout
    while time.time() < end:
        if ctl is not None:
            ctl.tasks.pump()
        if pred():
            return True
        time.sleep(0.01)
    return False


class Rig:
    """A controller wired to fake audio, a scripted model and a hands-free voice.json (no guard delay so tests are quick)."""
    def __init__(self, case, *, mode="wake_word", rec=None, mic=None, spk=None, settings=None, engine=True, **kw):
        # Threads named jarvis-voice that already exist (a slow-to-exit leftover of an EARLIER test in the same process) are not this test's business:
        # compare Thread objects, never names alone and never idents (an ident can be reused after a thread exits).
        self._voice_threads_before = {t for t in threading.enumerate() if t.name.startswith("jarvis-voice")}
        self.mic = mic or ThreadMic()
        self.rec = rec or ScriptRec()
        self.spk = spk or ThreadSpk()
        vs = VoiceSettings(input_mode=mode, post_speech_guard_seconds=0.0, follow_up_seconds=0.0, **(settings or {}))
        vs.save(case.cfg.profile_dir / "voice.json")
        case.install_model()
        self.c = AppController(case.cfg, engine_factory=fake_engine_factory if engine else None, speaker_factory=lambda: self.spk,
                               mic_factory=lambda: self.mic, recognizer_factory=lambda vs_: self.rec, **kw)
        case.addCleanup(self.c.shutdown, 2.0)

    def voice_threads(self, prefix: str = "jarvis-voice") -> list:
        """Live voice threads that were started after this Rig was created (i.e. by this test's own controller)."""
        return [t for t in threading.enumerate() if t.name.startswith(prefix) and t.is_alive() and t not in self._voice_threads_before]

    def state(self):
        return self.c.voice_state()

    def wait_state(self, name, timeout=6.0):
        return wait_for(lambda: self.state()["state"] == name, timeout, self.c)

    def ask(self, text, cancel_after=None):
        """Simulate: wake phrase, then the user speaks `text` (loud frames) and stops."""
        self.rec.say("wake", "hey jarvis")
        assert wait_for(lambda: self.c.voice._session is not None and self.c.voice._session.state == S.CAPTURING, 5, self.c), "wake not detected"
        self.mic.loud = 8
        assert wait_for(lambda: self.mic.loud == 0, 5, self.c)            # the user finishes talking (silence follows), then the recognizer finalizes
        self.rec.say("dictation", text)
