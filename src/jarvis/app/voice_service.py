"""Hands-free voice service (Prompt 17): ONE worker thread that owns the microphone, the local recognizer and the speaker.

What it does: opens the microphone only when the user has turned hands-free voice ON, loads the local Vosk model (on the worker thread, never the
interface thread), waits for the wake phrase, sends the recognised words through the SAME assistant path as typed chat (so the selected personality,
safety rules and executor permissions apply unchanged), speaks the reply through the shared local speaker and goes back to listening.

What it never does: save raw audio, send audio anywhere, import the desktop executor, or answer a confirmation. A spoken sentence is only text for
the assistant; actions still need the existing approval dialog, which a voice cannot click. Status is MEASURED: "Listening" is reported only while
the worker thread is alive, the microphone stream is open and the recognizer is loaded.

Honest limits: while the model is thinking the microphone is read and discarded (a spoken "stop" is not understood then; the Stop button and the
window's Mute work). Cancelling speech stops the speaker; cancelling a reply stops generation at the next piece. They are different operations."""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable

from jarvis.app.lifecycle import InstanceLock
from jarvis.voice import session as S
from jarvis.voice.settings import VoiceSettings
from jarvis.voice.wake import normalize

# state names shown to the user
OFF, STARTING, LISTENING, PROCESSING, SPEAKING, MUTED, UNAVAILABLE = "Off", "Starting", "Listening", "Processing", "Speaking", "Muted", "Unavailable"

SPOKEN_NORMAL = {"return to normal", "back to normal", "go back to normal", "normal mode", "normal style", "style off", "stop roleplay", "exit roleplay"}


class Unavailable(RuntimeError):
    pass


class VoiceService:
    def __init__(self, cfg, *, responder: Callable[[str], tuple[str, float]], speech, guest: bool = False,
                 on_change: Callable[[], None] | None = None, on_user: Callable[[str], None] | None = None,
                 on_reply: Callable[[str], None] | None = None, mic_factory: Callable | None = None,
                 recognizer_factory: Callable | None = None, begin_turn: Callable[[], bool] | None = None,
                 end_turn: Callable[[], None] | None = None, rate_factor: Callable[[], float] | None = None,
                 clock: Callable[[], float] = time.monotonic, frame_seconds: float = 0.03):
        self.cfg, self.guest = cfg, guest
        self._respond = responder              # text -> (reply, speech_rate_hint); runs on a helper thread
        self.speech = speech                   # app.speech.SpeechOut (shared speaker)
        self._on_change = on_change or (lambda: None)
        self._on_user = on_user or (lambda t: None)
        self._on_reply = on_reply or (lambda t: None)
        self._mic_factory, self._rec_factory = mic_factory, recognizer_factory
        self._begin_turn = begin_turn or (lambda: True)
        self._end_turn = end_turn or (lambda: None)
        self._last_rate = 1.0
        self._rate_factor = rate_factor or (lambda: self._last_rate)
        self._clock, self._dt = clock, frame_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cmds: queue.Queue = queue.Queue()
        self._session: S.VoiceSession | None = None
        self._mic = None
        self._rec = None
        self._rec_loaded = False
        self._preflight_done = False
        self._failure = ""                     # why voice is Unavailable (real reason)
        self._note = ""
        self._cancel_turn = threading.Event()
        self._mic_lock = InstanceLock(Path(cfg.profile_dir) / "mic.lock")
        self.last_error = ""
        self.degraded_text_only = ""
        self._dead = False                     # set when the application is closing: a queued restart must never reopen the microphone after that

    # ---------------- settings (persisted here, not in the controller) ----------------
    @property
    def path(self) -> Path:
        return Path(self.cfg.profile_dir) / "voice.json"

    def settings(self) -> VoiceSettings:
        return VoiceSettings.load(self.path)

    def _save(self, vs: VoiceSettings) -> None:
        if self.guest:
            raise ValueError("Voice settings are not saved in a guest session.")
        vs.save(self.path)

    def enabled(self) -> bool:
        return self.settings().input_mode != "off"

    # ---------------- state, measured ----------------
    def alive(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def state(self) -> dict:
        vs = self.settings()
        ses = self._session
        mic_open = bool(ses is not None and ses.mic_open)
        st = OFF
        detail = "Hands-free voice is off. The microphone is not in use."
        if self._failure:
            st, detail = UNAVAILABLE, self._failure
        elif vs.input_mode != "off" and vs.muted and not mic_open:
            st, detail = MUTED, "Microphone muted: capture is stopped. Nothing is being heard."
        elif self.alive():
            if ses is None or not self._preflight_done:
                st, detail = STARTING, "Starting: checking the microphone and loading the local speech model..."
            else:
                ss = ses.state
                if ss in (S.LISTENING, S.FOLLOW_UP, S.CAPTURING):
                    if mic_open and self._rec_loaded:
                        st = LISTENING
                        detail = {S.LISTENING: f"Listening for the wake phrase: \"{vs.wake_phrase}\"." if vs.input_mode == "wake_word" else "Push-to-talk: hold the button to speak.",
                                  S.FOLLOW_UP: "Listening for your follow-up (no wake phrase needed).",
                                  S.CAPTURING: "Hearing you..."}[ss]
                    else:
                        st, detail = UNAVAILABLE, "The microphone stream is not open."
                elif ss == S.THINKING:
                    st, detail = PROCESSING, "Thinking about what you said."
                elif ss == S.SPEAKING:
                    st, detail = SPEAKING, "Speaking."
                elif ss == S.MUTED:
                    st, detail = MUTED, "Microphone muted: capture is stopped."
                elif ss == S.RECOVERING:
                    st, detail = STARTING, "Microphone problem; retrying. " + (ses.error_message or "")
                elif ss == S.ERROR:
                    st, detail = UNAVAILABLE, ses.error_message or "Microphone unavailable."
                elif ss == S.SLEEPING:
                    st, detail = OFF, "Asleep: microphone closed."
                else:
                    st, detail = STARTING, "Starting..."
        elif vs.input_mode != "off":
            st, detail = OFF, "Hands-free voice is enabled but not running yet."
        return {"state": st, "detail": detail, "mic_open": mic_open, "mode": vs.input_mode, "mic_muted": vs.muted,
                "speaker_muted": vs.speaker_muted, "thread_alive": self.alive(), "recognizer_loaded": self._rec_loaded,
                "can_retry": st == UNAVAILABLE, "wake_phrase": vs.wake_phrase, "volume": vs.volume, "rate": vs.rate,
                "note": self.degraded_text_only or self._note, "audio_saved": False, "audio_leaves_device": False,
                "enabled": vs.input_mode != "off"}

    def _changed(self) -> None:
        try:
            self._on_change()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- lifecycle ----------------
    def start(self) -> dict:
        """Idempotent: a second call while running (or starting) does nothing. Returns the current state at once; slow work happens on the worker."""
        with self._lock:
            if self._dead:
                return self.state()
            if self.guest:
                self._failure = "Voice is not available in a guest session."
                return self.state()
            if self.alive():
                return self.state()
            vs = self.settings()
            if vs.input_mode == "off":
                return self.state()
            self._failure = ""
            self._note = ""
            self._stop.clear()
            self._cancel_turn.clear()
            self._rec_loaded = False
            self._preflight_done = False
            self._session = None
            while not self._cmds.empty():
                try:
                    self._cmds.get_nowait()
                except queue.Empty:
                    break
            self._thread = threading.Thread(target=self._run, name="jarvis-voice", daemon=True)
            self._thread.start()
        self._changed()
        return self.state()

    def retire(self) -> None:
        """The application is closing: from now on start() does nothing (so a restart queued earlier cannot reopen the microphone)."""
        self._dead = True

    def enable(self, mode: str = "wake_word") -> dict:
        if mode not in ("wake_word", "push_to_talk"):
            raise ValueError("hands-free mode must be wake_word or push_to_talk")
        if self.guest:
            return self.start()                      # reports "not available in a guest session"; nothing is saved
        vs = self.settings()
        vs.input_mode = mode
        self._save(vs)
        return self.start()

    def disable(self, timeout: float = 5.0) -> dict:
        """Turn hands-free OFF (saved): stops the worker, closes the microphone, releases the lock."""
        vs = self.settings()
        if vs.input_mode != "off" and not self.guest:
            vs.input_mode = "off"
            self._save(vs)
        self.shutdown(timeout)
        self._failure = ""
        self._changed()
        return self.state()

    def retry(self, timeout: float = 5.0) -> dict:
        self.shutdown(timeout)
        self._failure = ""
        return self.start()

    def shutdown(self, timeout: float = 5.0) -> dict:
        """Stop the worker, close the microphone and recognizer, silence the speaker. Returns {"leaked_thread": bool}."""
        self._stop.set()
        self._cancel_turn.set()
        self._cmds.put(("noop",))
        try:
            self.speech.stop()
        except Exception:  # noqa: BLE001
            pass
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        leaked = bool(t is not None and t.is_alive())
        if not leaked:
            self._thread = None
            self._session = None
            self._rec_loaded = False
            self._preflight_done = False
        return {"leaked_thread": leaked}

    # ---------------- commands (thread-safe; applied by the worker) ----------------
    def mic_mute(self) -> dict:
        """Mic mute: capture STOPS (the stream is closed) and the choice is saved, so a restart does not silently unmute."""
        vs = self.settings()
        vs.muted = True
        if not self.guest:
            self._save(vs)
        self._cmds.put(("mute",))
        self._cancel_turn.set()
        self._changed()
        return self.state()

    def mic_unmute(self) -> dict:
        vs = self.settings()
        vs.muted = False
        if not self.guest:
            self._save(vs)
        if self.alive():
            self._cmds.put(("unmute",))
            self._changed()
            return self.state()
        return self.start()

    def speaker_mute(self, on: bool) -> dict:
        vs = self.settings()
        vs.speaker_muted = bool(on)
        if not self.guest:
            self._save(vs)
        if on:
            try:
                self.speech.stop()
            except Exception:  # noqa: BLE001
                pass
        self._cmds.put(("speaker_mute", bool(on)))
        self._changed()
        return self.state()

    def cancel(self) -> None:
        """Silence speech NOW (direct, not queued), abandon the reply being produced, and return to listening."""
        self._cancel_turn.set()
        try:
            self.speech.stop()
        except Exception:  # noqa: BLE001
            pass
        self._cmds.put(("cancel",))

    def ptt_press(self) -> None:
        self._cmds.put(("ptt_press",))

    def ptt_release(self) -> None:
        self._cmds.put(("ptt_release",))

    def wake_now(self) -> None:
        self._cmds.put(("wake_now",))

    # ---------------- worker ----------------
    def _pump(self) -> None:
        ses = self._session
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            if ses is None:
                continue
            k = cmd[0]
            try:
                if k == "mute":
                    ses.mute()
                elif k == "unmute":
                    ses.s.muted = False
                    if ses.state == S.MUTED:
                        ses.unmute()
                elif k == "speaker_mute":
                    ses.set_speaker_muted(cmd[1])
                elif k == "cancel":
                    ses.cancel()
                elif k == "ptt_press":
                    ses.ptt_press()
                elif k == "ptt_release":
                    ses.ptt_release()
                elif k == "wake_now":
                    ses.wake()
            except Exception as e:  # noqa: BLE001 - one bad command must not kill the loop
                self.last_error = f"voice command {k} failed: {type(e).__name__}"
            self._changed()

    def _preflight(self, vs: VoiceSettings):
        """Everything slow or fallible, on the worker thread. Raises Unavailable with the real reason."""
        from jarvis.voice.backends import BackendUnavailable, SoundDeviceMic, VoskRecognizer
        if not self._mic_lock.acquire():
            raise Unavailable("Another Jarvis process is already using the microphone (see mic.lock in your profile folder). Close it, then press Retry.")
        mic = self._mic_factory() if self._mic_factory else SoundDeviceMic()
        if not vs.muted:
            try:
                devs = mic.devices() if hasattr(mic, "devices") else [1]
            except BackendUnavailable as e:
                raise Unavailable(str(e)) from e
            except Exception as e:  # noqa: BLE001
                raise Unavailable(f"Could not list microphones: {type(e).__name__}: {str(e)[:160]}") from e
            if not devs:
                raise Unavailable("No microphone was found. Plug one in or enable it in Windows Settings > Privacy & security > Microphone, then press Retry.")
        if self._rec_factory is not None:
            rec = self._rec_factory(vs)
        else:
            if not vs.vosk_model:
                raise Unavailable("No Vosk speech model folder is set. Download a small English Vosk model yourself, unzip it, and set its folder with: jarvis voice set vosk_model <folder>.")
            if not Path(vs.vosk_model).is_dir():
                raise Unavailable(f"The Vosk model folder was not found: {vs.vosk_model}")
            rec = VoskRecognizer(vs.vosk_model, vs.all_wake_phrases())
        try:
            if hasattr(rec, "preload"):
                rec.preload()
        except BackendUnavailable as e:
            raise Unavailable(str(e)) from e
        ok, why = self.speech.available()
        if not ok and vs.output_mode != "text_only":
            self.degraded_text_only = f"Replies will be shown as text only: {why}"
        return mic, rec

    def _run(self) -> None:
        ses = None
        try:
            vs = self.settings()
            try:
                mic, rec = self._preflight(vs)
            except Unavailable as e:
                self._failure = str(e)
                self.last_error = str(e)
                return
            except Exception as e:  # noqa: BLE001
                self._failure = f"Voice could not start: {type(e).__name__}: {str(e)[:200]}"
                return
            self._mic, self._rec, self._rec_loaded = mic, rec, True
            if self.degraded_text_only:
                vs.output_mode = "text_only"                    # this run only; not saved
            ses = S.VoiceSession(vs, mic, rec, self.speech.speaker(), self._turn, on_event=self._event, clock=self._clock,
                                 frame_seconds=self._dt, rate_factor=self._rate_factor)
            self._session = ses
            self._preflight_done = True
            ses.start()
            self._changed()
            while not self._stop.is_set():
                self._pump()
                if ses.state in (S.OFF, S.MUTED, S.SLEEPING, S.ERROR):
                    self._stop.wait(0.05)
                    continue
                ses.tick()
                if ses.state == S.RECOVERING:
                    self._stop.wait(0.05)
        except Exception as e:  # noqa: BLE001 - the worker must report, not die silently
            self._failure = f"Voice stopped unexpectedly: {type(e).__name__}: {str(e)[:200]}"
            self.last_error = self._failure
        finally:
            try:
                if ses is not None:
                    ses.stop()                             # closes the microphone, silences the speaker
            except Exception:  # noqa: BLE001
                pass
            try:
                if self._mic is not None and hasattr(self._mic, "close"):
                    self._mic.close()
            except Exception:  # noqa: BLE001
                pass
            self._mic_lock.release()
            self._changed()

    def _event(self, kind: str, payload) -> None:
        if kind == "error":
            self.last_error = str(payload)
        self._changed()

    # ---------------- one voice turn (runs on the worker, inside VoiceSession._handle_text) ----------------
    def _turn(self, text: str) -> str:
        norm = " ".join(normalize(text))
        if norm in SPOKEN_NORMAL:
            text = "/normal"                                # the exact command the assistant already understands
        self._on_user(text if text != "/normal" else "/normal (spoken: return to normal)")
        if not self._begin_turn():
            msg = "I am still answering your typed message. Ask again in a moment."
            self._on_reply(msg)
            return msg
        self._cancel_turn.clear()
        box: dict = {}

        def work():
            try:
                box["reply"], box["rate"] = self._respond(text, self._cancel_turn)
            except Exception as e:  # noqa: BLE001
                box["err"] = e

        t = threading.Thread(target=work, name="jarvis-voice-turn", daemon=True)
        t.start()
        try:
            while t.is_alive():
                self._pump()
                if self._stop.is_set() or self._cancel_turn.is_set():
                    self._cancel_turn.set()
                    t.join(2.0)
                    box.setdefault("reply", "")
                    break
                ses = self._session
                if ses is not None and ses.mic_open:
                    try:
                        ses.observe(self._mic.read())       # drain and DISCARD (kept nowhere) so the audio buffer cannot grow
                    except Exception:  # noqa: BLE001
                        self._stop.wait(self._dt)
                else:
                    self._stop.wait(self._dt)
            t.join(0.1)
        finally:
            self._end_turn()
        if "err" in box:
            raise box["err"]
        reply = box.get("reply", "") or ""
        try:
            self._last_rate = float(box.get("rate", 1.0))
        except (TypeError, ValueError):
            self._last_rate = 1.0
        if self._cancel_turn.is_set():
            self._on_reply("(cancelled)")
            self._cancel_turn.clear()
            return ""
        self._on_reply(reply)
        return reply
