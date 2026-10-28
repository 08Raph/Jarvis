"""Voice preview: say one short fixed sentence in a chosen voice, on a worker thread, with honest states.

States: idle -> loading -> (generating ->) playing -> finished | stopped | error. "generating" appears only for engines that synthesise before they play (Kokoro).
"finished" means ONLY that the speech engine ran the sample to its end and (when the engine reports one) exited with code 0. Jarvis cannot hear the
speakers, so it never says the sample "was heard". If the engine fails, cannot start, exits with an error, or takes too long, the state is "error" with
a plain reason. The audio output is shared with normal speech: starting a preview stops whatever Jarvis was saying, and Stop silences it at once.
No microphone is touched. No network is used."""
from __future__ import annotations

import threading
import time
from typing import Callable

from jarvis.voice.engines import EngineError
from jarvis.voice.manager import VoiceManager

PREVIEW_TEXT = "Hello, I am Jarvis. This is what my voice sounds like."
IDLE, LOADING, GENERATING, PLAYING, FINISHED, STOPPED, ERROR = "idle", "loading", "generating", "playing", "finished", "stopped", "error"
BUSY = (LOADING, GENERATING, PLAYING)
_PHASE_STATE = {"loading": (LOADING, "Loading the voice model (the first use after starting Jarvis can take a while)..."),
                "generating": (GENERATING, "Generating speech on this PC..."), "playing": (PLAYING, "Playing the sample...")}
TERMINAL = (FINISHED, STOPPED, ERROR)
FINISHED_NOTE = ("The speech engine reported that the sample finished. Jarvis cannot hear your speakers: if you heard nothing, check the output device and volume in Windows.")


def _finished_text(sp) -> str:
    """The honest end-of-sample text. For an engine that reports synthesis and playback separately (Kokoro) both facts are stated, with the measured times."""
    rep = getattr(sp, "report", None)
    r = rep() if callable(rep) else {}
    if not r or "synthesis_ok" not in r:
        return FINISHED_NOTE
    bits = [f"Kokoro generated {r.get('audio_seconds', 0)} s of audio on this PC" + ("" if not r.get("load_seconds") else f" (model load {r['load_seconds']} s)")
            + ("" if r.get("first_audio_seconds") is None else f"; first audio after {r['first_audio_seconds']} s") + ".",
            "All of it was handed to the audio output device without an error." if r.get("playback_ok") else "No audio was played."]
    return " ".join(bits) + " Jarvis cannot hear your speakers: if you heard nothing, check the output device and volume in Windows. Times are measured on this run only."


def _safe(e: BaseException) -> str:
    try:
        from jarvis.brains.service import _safe as s
        return s(e, 200)
    except Exception:      # noqa: BLE001
        return type(e).__name__


class VoicePreview:
    def __init__(self, manager: VoiceManager, on_change: Callable[[], None] | None = None, *, text: str = PREVIEW_TEXT, timeout: float = 30.0,
                 poll: float = 0.05, clock: Callable[[], float] = time.monotonic):
        self.m = manager
        self._on_change = on_change or (lambda: None)
        self.text, self.timeout, self.poll, self._clock = text, timeout, poll, clock
        self._lock = threading.Lock()
        self._state, self._message, self._voice = IDLE, "", None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._gen = 0
        self._speaker = None

    # ---------------- public ----------------
    def state(self) -> dict:
        with self._lock:
            return {"state": self._state, "message": self._message, "voice": self._voice, "busy": self._state in BUSY}

    def start(self, voice_id: str, rate: float | None = None, volume: float | None = None) -> dict:
        """Begin a preview (returns at once). Any preview already running is stopped first."""
        self.stop()
        vs = self.m.settings()
        if vs.speaker_muted:
            return self._set(ERROR, "The speaker is muted in Jarvis, so no sample was played. Unmute the speaker first.", voice_id)
        if not voice_id:
            return self._set(ERROR, "Choose a voice first.", None)
        rate = vs.rate if rate is None else rate
        volume = vs.volume if volume is None else volume
        if not (0.5 <= rate <= 2.0 and 0.0 <= volume <= 1.0):
            return self._set(ERROR, "Speed must be 50%-200% and volume 0%-100%.", voice_id)
        with self._lock:
            self._gen += 1
            gen = self._gen
        self._stop.clear()
        self._set(LOADING, "Preparing the voice...", voice_id)
        self._thread = threading.Thread(target=self._run, args=(gen, voice_id, rate, volume), name="jarvis-voice-preview", daemon=True)
        self._thread.start()
        return self.state()

    def stop(self) -> dict:
        """Silence the sample now and wait briefly for the worker to finish. Safe at any time."""
        self._stop.set()
        sp = self._speaker
        if sp is not None:
            try:
                sp.stop()
            except Exception:      # noqa: BLE001
                pass
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(2.0)
        return self.state()

    def close(self) -> None:
        self.stop()
        self._speaker = None

    # ---------------- worker ----------------
    def _set(self, state: str, message: str, voice, gen: int | None = None) -> dict:
        with self._lock:
            if gen is not None and gen != self._gen:
                return {"state": self._state, "message": self._message, "voice": self._voice, "busy": self._state in BUSY}
            self._state, self._message, self._voice = state, message, voice
        try:
            self._on_change()
        except Exception:      # noqa: BLE001
            pass
        return self.state()

    def _run(self, gen: int, voice_id: str, rate: float, volume: float) -> None:
        sp = None
        try:
            v = self.m.find(voice_id)
            if v is None:
                self._set(ERROR, "That voice is not installed on this PC, so there is nothing to play.", voice_id, gen)
                return
            eng = self.m.registry.get(v.engine)
            ok, why = eng.available() if eng else (False, "its speech engine is not registered")
            if not ok:
                self._set(ERROR, f"The speech engine for this voice is unavailable: {why}", voice_id, gen)
                return
            if self._stop.is_set():
                self._set(STOPPED, "Stopped before playing.", voice_id, gen)
                return
            sp = eng.speaker()
            self._speaker = sp
            self.m.speaker().stop()                      # one audio output: whatever Jarvis was saying ends now
            sp.speak(self.text, rate, volume, v.name)
            phased = hasattr(sp, "phase")
            shown = None
            if not phased:
                self._set(PLAYING, "Playing the sample...", voice_id, gen)
            t0 = self._clock()
            while True:
                if phased:
                    ph = sp.phase
                    if ph in _PHASE_STATE and ph != shown:
                        shown = ph
                        t0 = self._clock()                      # the time limit applies to each phase (a first model load on a slow PC is not a stuck engine)
                        st, msg = _PHASE_STATE[ph]
                        self._set(st, msg, voice_id, gen)
                if self._stop.is_set():
                    sp.stop()
                    self._set(STOPPED, "Stopped.", voice_id, gen)
                    return
                if not sp.is_speaking():
                    break
                if self._clock() - t0 > (max(self.timeout, 120.0) if phased else self.timeout):
                    sp.stop()
                    self._set(ERROR, "The sample took too long and was stopped. The speech engine may be stuck.", voice_id, gen)
                    return
                self._stop.wait(self.poll)
            code_fn = getattr(sp, "exit_code", None)
            code = code_fn() if callable(code_fn) else None
            why = getattr(sp, "last_error", "") or ""
            if code not in (None, 0):
                self._set(ERROR, why or f"The speech engine stopped with an error (code {code}). The sample was probably not played; the voice may be damaged or not usable.", voice_id, gen)
            else:
                self._set(FINISHED, _finished_text(sp), voice_id, gen)
        except EngineError as e:
            self._set(ERROR, str(e), voice_id, gen)
        except Exception as e:      # noqa: BLE001 - a TTS failure is a message, never a crash
            self._set(ERROR, f"The sample could not be played ({_safe(e)}).", voice_id, gen)
            self.m.reset_speakers()
        finally:
            if self.state()["busy"] and gen == self._gen:     # never leave a half-finished state or a running engine behind
                try:
                    if sp is not None:
                        sp.stop()
                finally:
                    self._set(ERROR, "The preview ended unexpectedly.", voice_id, gen)
