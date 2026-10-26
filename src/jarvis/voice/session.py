"""Voice session controller: a frame-driven state machine. All hardware sits behind small interfaces
(Mic, Recognizer, Speaker) so the logic is testable with fakes. No network access of any kind lives here.

States (always visible via on_event("state", ...), including whether the microphone stream is open):
  OFF, LISTENING (wake word), CAPTURING (user speaking), THINKING, SPEAKING, FOLLOW_UP, SLEEPING, MUTED,
  RECOVERING (mic failed, retrying with backoff), ERROR (gave up; needs the user).
The microphone stream is open ONLY in LISTENING, CAPTURING, THINKING, SPEAKING, FOLLOW_UP and RECOVERING-probes.
Mute and sleep close the stream itself rather than ignoring frames.
"""
from __future__ import annotations

import time
from typing import Callable, Protocol

import numpy as np

from jarvis.voice.settings import VoiceSettings
from jarvis.voice.speakable import apply_pronunciations, shorten, speakable
from jarvis.voice.vad import EnergyVAD
from jarvis.voice.wake import match_wake, normalize

OFF, LISTENING, CAPTURING, THINKING, SPEAKING, FOLLOW_UP, SLEEPING, MUTED, RECOVERING, ERROR = (
    "OFF", "LISTENING", "CAPTURING", "THINKING", "SPEAKING", "FOLLOW_UP", "SLEEPING", "MUTED", "RECOVERING", "ERROR")
MIC_OPEN_STATES = {LISTENING, CAPTURING, THINKING, SPEAKING, FOLLOW_UP}

CANCEL_WORDS = {"stop", "cancel", "never mind", "nevermind", "be quiet", "quiet"}
SLEEP_WORDS = {"go to sleep", "sleep", "stop listening"}
NO_SPEECH_TIMEOUT = 6.0
BARGE_FRAMES = 3
RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)


class MicError(RuntimeError):
    pass


class Mic(Protocol):
    def open(self, device: str | None) -> None: ...
    def read(self) -> np.ndarray: ...      # one int16 mono frame; raises MicError on failure
    def close(self) -> None: ...


class Recognizer(Protocol):
    def feed(self, frame: np.ndarray, mode: str) -> str | None: ...   # mode "wake" or "dictation"; text when final
    def reset(self) -> None: ...
    def flush(self) -> str: ...                                       # final text of the current dictation


class Speaker(Protocol):
    def speak(self, text: str, rate: float, volume: float, voice: str | None) -> None: ...  # non-blocking
    def stop(self) -> None: ...                                       # must silence immediately
    def is_speaking(self) -> bool: ...


class VoiceSession:
    def __init__(self, settings: VoiceSettings, mic: Mic, recognizer: Recognizer, speaker: Speaker,
                 responder: Callable[[str], str], on_event: Callable[[str, object], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, frame_seconds: float = 0.03,
                 rate_factor: Callable[[], float] | None = None):
        self.s = settings.validate()
        self.mic, self.rec, self.spk, self.responder = mic, recognizer, speaker, responder
        self._emit = on_event or (lambda k, p: None)
        self.clock, self.dt = clock, frame_seconds
        self.vad = EnergyVAD()
        self.state = OFF
        self.mic_open = False
        self._t0 = self._last_activity = self.clock()
        self._heard_speech = False
        self._silence = 0.0
        self._barge = 0
        self._ptt = False
        self._pending_reply: str | None = None
        self._last_speech = False
        self._retry = 0
        self._next_retry = 0.0
        self.error_message = ""
        self._rate_factor = rate_factor      # personality hint (e.g. slower for the calm style); clamped, never raises
        self._guard_until = 0.0              # frames before this time are ignored (echo of our own speech)
        self._spoken = ""                    # what Jarvis last said aloud (normalised words), for the echo and self-wake filters
        self._spoken_at = -1e9
        self._last_key = ""                  # the last accepted utterance, to refuse an immediate duplicate
        self._last_key_t = -1e9

    # ---------- state / mic plumbing ----------
    def _set(self, state: str, note: str = "") -> None:
        if state != self.state:
            self.state = state
            self._t0 = self.clock()
            self._emit("state", {"state": state, "mic_open": self.mic_open, "note": note})

    def _open_mic(self) -> bool:
        if self.mic_open:
            return True
        try:
            self.mic.open(self.s.mic_device)
            self.mic_open = True
            return True
        except MicError as e:
            self._mic_failed(str(e))
            return False
        except Exception as e:  # driver-specific exceptions must not crash the session
            self._mic_failed(f"{type(e).__name__}: {e}")
            return False

    def _close_mic(self) -> None:
        if self.mic_open:
            try:
                self.mic.close()
            except Exception:
                pass
            self.mic_open = False

    def _mic_failed(self, msg: str) -> None:
        self._close_mic()
        self.spk.stop()
        self.rec.reset()
        if self._retry >= len(RETRY_DELAYS):
            self.error_message = f"Microphone unavailable: {msg}. Check the device in Windows Sound settings, then press Start."
            self._set(ERROR, self.error_message)
            self._emit("error", self.error_message)
            return
        self._next_retry = self.clock() + RETRY_DELAYS[self._retry]
        self._retry += 1
        self._set(RECOVERING, f"microphone problem ({msg}); retrying")
        self._emit("error", f"microphone problem: {msg}")

    # ---------- user controls ----------
    def start(self) -> None:
        if self.s.input_mode == "off":
            raise RuntimeError("Voice input is off. Enable it explicitly in voice settings first.")
        if self.s.muted:
            self._set(MUTED)
            return
        self._retry = 0
        if self._open_mic():
            self.rec.reset()
            self._last_activity = self.clock()
            self._set(LISTENING)

    def stop(self) -> None:
        self.spk.stop()
        self._close_mic()
        self.rec.reset()
        self._set(OFF)

    def mute(self) -> None:
        self.s.muted = True
        self.spk.stop()
        self._close_mic()
        self.rec.reset()
        self._set(MUTED)

    def unmute(self) -> None:
        self.s.muted = False
        if self.state == MUTED:
            self._set(OFF)
            if self.s.input_mode != "off":
                self.start()

    def sleep(self) -> None:
        self.spk.stop()
        self._close_mic()
        self.rec.reset()
        self._set(SLEEPING, "microphone closed; press Wake or the push-to-talk key")

    def wake(self) -> None:
        if self.state in (SLEEPING, ERROR, OFF) and not self.s.muted:
            self.start()

    def cancel(self) -> None:
        """Stop speaking/capturing immediately and return to listening."""
        self.spk.stop()
        self._pending_reply = None
        self.rec.reset()
        self._heard_speech = False
        if self.state in (CAPTURING, THINKING, SPEAKING, FOLLOW_UP):
            self._set(LISTENING, "cancelled")

    def ptt_press(self) -> None:
        if self.s.muted or self.s.input_mode == "off":
            return
        self.spk.stop()  # never talk over the user
        if not self.mic_open and not self._open_mic():
            return
        self._ptt = True
        self.rec.reset()
        self._heard_speech = False
        self._silence = 0.0
        self._set(CAPTURING, "push-to-talk")

    def ptt_release(self) -> None:
        if self._ptt:
            self._ptt = False
            self._finish_capture(self.rec.flush())

    # ---------- main step ----------
    def tick(self) -> None:
        """Call repeatedly (once per audio frame)."""
        if self.state in (OFF, MUTED, SLEEPING, ERROR):
            return
        if self.state == RECOVERING:
            if self.clock() >= self._next_retry and self._open_mic():
                self.rec.reset()
                self._retry = 0
                self._set(LISTENING, "microphone recovered")
            return
        try:
            frame = self.mic.read()
        except MicError as e:
            self._mic_failed(str(e))
            return
        except Exception as e:
            self._mic_failed(f"{type(e).__name__}: {e}")
            return
        self.step(frame)

    def step(self, frame: np.ndarray) -> None:
        now = self.clock()
        speech = self.vad.is_speech(frame)
        self._last_speech = speech
        st = self.state
        if now < self._guard_until and st in (LISTENING, FOLLOW_UP):
            return                       # just finished speaking: do not listen to our own echo
        if st == LISTENING:
            self._listening(frame, speech, now)
        elif st == CAPTURING:
            self._capturing(frame, speech, now)
        elif st == FOLLOW_UP:
            self._follow_up(frame, speech, now)
        elif st == SPEAKING:
            self._speaking(frame, speech, now)
        elif st == THINKING and self._pending_reply is not None:
            self._maybe_speak(speech)

    def _listening(self, frame, speech, now):
        if speech:
            self._last_activity = now
        if self.s.input_mode == "wake_word":
            text = self.rec.feed(frame, "wake")
            if text:
                phrase, rest = match_wake(text, self.s.all_wake_phrases(), self.s.sensitivity)
                self.rec.reset()
                if phrase:
                    self._emit("wake", phrase)
                    if rest:
                        self._handle_text(rest)
                    else:
                        self._begin_capture()
                    return
        if self.s.sleep_after_seconds and now - self._last_activity >= self.s.sleep_after_seconds:
            self.sleep()

    def _begin_capture(self):
        self.rec.reset()
        self._heard_speech = False
        self._silence = 0.0
        self._set(CAPTURING)

    def _capturing(self, frame, speech, now):
        text = self.rec.feed(frame, "dictation")
        if self._ptt:
            return  # push-to-talk ends only on key release
        if speech:
            self._heard_speech, self._silence = True, 0.0
        else:
            self._silence += self.dt
        too_long = now - self._t0 >= self.s.max_utterance_seconds
        done = text is not None or (self._heard_speech and self._silence >= self.s.silence_end_seconds) or too_long
        if done:
            self._finish_capture(text if text is not None else self.rec.flush())
        elif not self._heard_speech and now - self._t0 >= NO_SPEECH_TIMEOUT:
            self.rec.reset()
            self._set(LISTENING, "heard nothing")

    def _finish_capture(self, text: str) -> None:
        self.rec.reset()
        text = (text or "").strip()
        if not text:
            self._set(LISTENING, "heard nothing")
            return
        self._handle_text(text)

    def _echo_of_own_speech(self, norm: str) -> bool:
        words = norm.split()
        if len(words) < 3 or self.clock() - self._spoken_at > 15.0 or not self._spoken:
            return False
        said = set(self._spoken.split())
        return sum(1 for w in words if w in said) / len(words) >= 0.8

    def _handle_text(self, text: str) -> None:
        norm = " ".join(normalize(text))
        now = self.clock()
        if norm and norm == self._last_key and now - self._last_key_t < 2.0:
            self._emit("ignored", "duplicate utterance")
            if self.state in (CAPTURING, FOLLOW_UP):
                self._set(LISTENING, "duplicate ignored")
            return
        if self._echo_of_own_speech(norm):
            self._emit("ignored", "sounds like Jarvis's own voice")
            if self.state in (CAPTURING, FOLLOW_UP):
                self._set(LISTENING, "echo ignored")
            return
        self._last_key, self._last_key_t = norm, now
        self._emit("heard", text)
        if norm in CANCEL_WORDS:
            self.cancel()
            return
        if norm in SLEEP_WORDS:
            self.sleep()
            return
        self._set(THINKING)
        try:
            reply = self.responder(text)
        except Exception as e:  # a failing responder must not kill the voice loop or be read aloud raw
            self._emit("error", f"assistant error: {type(e).__name__}")
            reply = "Sorry, something went wrong. Details are in the log."
        self._deliver(reply)

    def _deliver(self, reply: str) -> None:
        if self.state != THINKING:       # muted, cancelled, put to sleep or stopped while the reply was being produced: say nothing, change nothing
            self._pending_reply = None
            return
        if self.s.output_mode != "voice_only":
            self._emit("reply_text", reply)  # full text, on screen
        if self.s.output_mode == "text_only" or self.s.speaker_muted:
            self._after_reply()
            return
        say = apply_pronunciations(shorten(speakable(reply), self.s.response_length), self.s.pronunciations)
        if not say:
            self._after_reply()
            return
        self._pending_reply = say
        self._maybe_speak(speech_now=self._last_speech)

    def _speech_rate(self) -> float:
        try:
            f = float(self._rate_factor()) if self._rate_factor else 1.0
        except Exception:   # noqa: BLE001 - a bad hint must never break speech
            f = 1.0
        return max(0.5, min(2.0, self.s.rate * (f if f == f else 1.0)))

    def _maybe_speak(self, speech_now: bool) -> None:
        if speech_now:  # the user is talking: do not speak over them; treat it as a new utterance
            self._pending_reply = None
            self._begin_capture()
            return
        say, self._pending_reply = self._pending_reply, None
        self._barge = 0
        self._spoken, self._spoken_at = " ".join(normalize(say)), self.clock()
        self.spk.speak(say, self._speech_rate(), self.s.volume, self.s.tts_voice)
        self._emit("spoken", say)
        self._set(SPEAKING)

    def _speaking(self, frame, speech, now):
        mode = self.s.barge_in
        if mode == "wake_word_only" and self.s.input_mode == "wake_word":
            text = self.rec.feed(frame, "wake")
            if text:
                phrase, rest = match_wake(text, self.s.all_wake_phrases(), self.s.sensitivity)
                norm = " ".join(normalize(text))
                if phrase and " ".join(normalize(phrase)) in self._spoken:
                    phrase = None                    # our own reply contains the wake phrase: that is us, not the user
                if phrase or norm in CANCEL_WORDS:
                    self.spk.stop()
                    self.rec.reset()
                    if norm in CANCEL_WORDS:
                        self._set(LISTENING, "cancelled")
                    else:
                        self._emit("wake", phrase)
                        self._begin_capture()
                    return
        elif mode == "speech":
            self._barge = self._barge + 1 if speech else 0
            if self._barge >= BARGE_FRAMES:
                self.spk.stop()
                self._begin_capture()
                return
        if not self.spk.is_speaking():
            self._after_reply()

    def observe(self, frame: np.ndarray) -> None:
        """Frames that arrive while the reply is being produced are not recognised, but the voice detector still sees them, so a reply is not
        spoken over a user who started talking in the meantime."""
        self._last_speech = self.vad.is_speech(frame)

    def set_speaker_muted(self, on: bool) -> None:
        """Speaker mute only: silences speech now and from here on; the microphone state is untouched."""
        self.s.speaker_muted = bool(on)
        if on:
            self.spk.stop()

    def _after_reply(self):
        self.rec.reset()
        self._guard_until = self.clock() + max(0.0, float(self.s.post_speech_guard_seconds))
        if self.s.follow_up_seconds > 0:
            self._heard_speech, self._silence = False, 0.0
            self._set(FOLLOW_UP)
        else:
            self._last_activity = self.clock()
            self._set(LISTENING)

    def _follow_up(self, frame, speech, now):
        text = self.rec.feed(frame, "dictation")
        if speech:
            self._heard_speech, self._silence = True, 0.0
            self._t0_speech = now
        elif self._heard_speech:
            self._silence += self.dt
        if text is not None or (self._heard_speech and self._silence >= self.s.silence_end_seconds):
            self._finish_capture(text if text is not None else self.rec.flush())
        elif not self._heard_speech and now - self._t0 >= self.s.follow_up_seconds:
            self.rec.reset()
            self._last_activity = now
            self._set(LISTENING, "follow-up window closed")
