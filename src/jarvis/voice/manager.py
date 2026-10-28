"""VoiceManager: which voice Jarvis speaks with.

* Discovery: asks every registered engine which voices it really has (never invents one) and groups them feminine / masculine / not specified.
* Preference: ONE global voice (voice.json `tts_voice`) plus an OPTIONAL per-personality-mode override (`voice_by_persona`). Choosing or changing a
  personality never changes the voice; a mode uses its own voice only if the user set one for that mode.
* Fallback: if the chosen voice is no longer installed the engine's default voice is used and the reason is reported (`state()["note"]`); nothing is silent.
  Exception: a voice of an engine with `fallback_ok = False` (Kokoro) is never replaced by another ENGINE's voice behind the person's back. If it cannot be
  used the resolution is "unavailable": nothing is spoken, the reason is reported, and the person chooses a way out on the Voice page (install/repair, or switch engine).
* It writes ONLY voice.json (atomic replace). Conversations, memory, personality and permissions are other files and are never opened here.
* Slow discovery (starting PowerShell) runs on a worker thread, never the interface thread. No network code lives here."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from jarvis.voice.engines import CATEGORIES, CATEGORY_LABELS, EngineError, EngineRegistry, TTSEngine, VoiceInfo, split_id
from jarvis.voice.settings import VoiceSettings


@dataclass
class Resolution:
    engine_id: str | None
    voice_name: str | None          # the engine's own voice name; None = the engine's default voice
    voice_id: str | None
    requested: str | None           # what the settings asked for (None = nothing chosen)
    source: str                     # "persona" | "global" | "default" | "none" | "unavailable" (the chosen engine/voice cannot be used and is not replaced)
    fallback: bool
    note: str

    def as_dict(self) -> dict:
        return {"engine": self.engine_id, "voice": self.voice_id, "voice_name": self.voice_name, "requested": self.requested, "source": self.source,
                "fallback": self.fallback, "note": self.note}


class VoiceManager:
    def __init__(self, cfg, registry: EngineRegistry, persona_provider: Callable[[], str | None] | None = None, guest: bool = False,
                 clock: Callable[[], float] = time.time):
        self.cfg, self.registry, self.guest = cfg, registry, guest
        self.persona_provider = persona_provider
        self._clock = clock
        self._lock = threading.Lock()
        self._voices: list[VoiceInfo] = []
        self._loaded = False
        self._errors: dict[str, str] = {}
        self._refreshed_at: float | None = None
        self._refreshing = False
        self._thread: threading.Thread | None = None
        self.last_resolution: Resolution | None = None
        self._speaker = ResolvingSpeaker(self)

    # ---------------- settings (the only file written is voice.json) ----------------
    @property
    def path(self) -> Path:
        return Path(self.cfg.profile_dir) / "voice.json"

    def settings(self) -> VoiceSettings:
        return VoiceSettings.load(self.path)

    def _save(self, vs: VoiceSettings) -> None:
        if self.guest:
            raise ValueError("Voice settings are not saved in a guest session.")
        vs.save(self.path)

    # ---------------- discovery ----------------
    def refresh(self) -> dict:
        """Ask every engine for its voices (blocking: run it on a worker). One failing engine does not hide the others."""
        voices: list[VoiceInfo] = []
        errors: dict[str, str] = {}
        for eng in self.registry.engines:
            ok, why = eng.available()
            if not ok:
                errors[eng.id] = why
                continue
            try:
                voices.extend(eng.list_voices())
            except EngineError as e:
                errors[eng.id] = str(e)
            except Exception as e:      # noqa: BLE001 - an engine bug must be a message, not a crash
                errors[eng.id] = f"{eng.title} could not list its voices ({type(e).__name__})."
        with self._lock:
            self._voices, self._errors, self._loaded, self._refreshed_at = voices, errors, True, self._clock()
        return self.catalog()

    def refresh_async(self, on_done: Callable[[], None] | None = None) -> bool:
        """Start discovery on a worker thread. Returns False if one is already running."""
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True

        def work():
            try:
                self.refresh()
            finally:
                with self._lock:
                    self._refreshing = False
                if on_done:
                    try:
                        on_done()
                    except Exception:      # noqa: BLE001
                        pass
        self._thread = threading.Thread(target=work, name="jarvis-voice-list", daemon=True)
        self._thread.start()
        return True

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.refresh()

    def voices(self) -> list[VoiceInfo]:
        with self._lock:
            return list(self._voices)

    def catalog(self) -> dict:
        """What the page shows: engines (with availability and requirements), voices grouped by category, and the saved choices. Cached; never blocks."""
        with self._lock:
            voices, loaded, errors, at, busy = list(self._voices), self._loaded, dict(self._errors), self._refreshed_at, self._refreshing
        vs = self.settings()
        groups = {c: [v.as_dict() for v in voices if v.category == c] for c in CATEGORIES}
        engines = []
        for e in self.registry.engines:
            d = e.describe()
            d["voice_count"] = sum(1 for v in voices if v.engine == e.id)
            d["error"] = errors.get(e.id, "")
            engines.append(d)
        sel_eng = split_id(vs.tts_voice)[0] if vs.tts_voice else None
        first = self.registry.first_available()
        return {"loaded": loaded, "refreshing": busy, "refreshed_at": at, "engines": engines, "categories": groups,
                "selected_engine": sel_eng if (sel_eng and self.registry.get(sel_eng)) else (first.id if first else None),
                "category_labels": dict(CATEGORY_LABELS), "count": len(voices), "selected": vs.tts_voice, "by_persona": dict(vs.voice_by_persona),
                "any_engine_available": self.registry.first_available() is not None}

    def find(self, voice_id: str | None, load: bool = True) -> VoiceInfo | None:
        """A voice by id. An old-style plain name (saved before engines existed) matches by name on any engine. load=False never starts discovery."""
        if not voice_id:
            return None
        if load:
            self.ensure_loaded()
        eng, name = split_id(voice_id)
        with self._lock:
            for v in self._voices:
                if (eng is None or v.engine == eng) and v.name.lower() == name.lower():
                    return v
        return None

    # ---------------- preference ----------------
    def apply(self, voice_id: str, persona: str | None = None) -> dict:
        """Save the choice. persona=None sets the global default; a persona id sets an optional override for that mode only. Only voice.json changes."""
        v = self.find(voice_id)
        if v is None:
            raise ValueError("That voice is not installed on this PC, so it was not applied.")
        vs = self.settings()
        if persona is None:
            vs.tts_voice = v.id
        else:
            self._check_persona(persona)
            by = dict(vs.voice_by_persona)
            by[persona] = v.id
            vs = replace(vs, voice_by_persona=by)
        self._save(vs)
        return {"ok": True, "voice": v.as_dict(), "scope": "global" if persona is None else persona}

    def select_engine(self, engine_id: str) -> dict:
        """Make `engine_id` the engine of the GLOBAL voice: saves its first listed voice (or, for an engine that lists none, its default voice) as the default.
        Refuses with the engine's own reason when it is not ready. Personality overrides are not touched."""
        eng = self.registry.get(engine_id)
        if eng is None:
            raise ValueError("That speech engine is not part of Jarvis.")
        ok, why = eng.available()
        if not ok:
            raise ValueError(f"{eng.title} is not ready, so it was not selected: {why}")
        self.refresh()
        voices = [v for v in self.voices() if v.engine == engine_id]
        vs = self.settings()
        cur = next((v for v in voices if v.id == vs.tts_voice), None)
        if cur is None:
            if not voices:
                raise ValueError(f"{eng.title} reports no voices, so it was not selected.")
            vs.tts_voice = voices[0].id
            self._save(vs)
        return {"ok": True, "engine": engine_id, "voice": vs.tts_voice}

    INPUT_FIELDS = ("mic_device", "wake_phrase", "vosk_model", "vad_min_rms")

    def set_input(self, **kw) -> VoiceSettings:
        """Validated change of the microphone choice, wake phrase, recogniser model folder and speech-detection floor. It can NOT change input_mode, so it can never turn listening on."""
        bad = set(kw) - set(self.INPUT_FIELDS)
        if bad:
            raise ValueError(f"unknown voice option(s): {sorted(bad)}")
        vs = replace(self.settings(), **kw).validate()
        self._save(vs)
        return vs

    def clear_persona_voice(self, persona: str) -> dict:
        self._check_persona(persona)
        vs = self.settings()
        by = dict(vs.voice_by_persona)
        by.pop(persona, None)
        self._save(replace(vs, voice_by_persona=by))
        return {"ok": True, "scope": persona}

    @staticmethod
    def _check_persona(persona: str) -> None:
        from jarvis.personality.styles import STYLES
        if persona not in STYLES:
            raise ValueError(f"unknown personality mode: {persona!r}")

    # ---------------- resolution ----------------
    def _persona(self) -> str | None:
        try:
            return self.persona_provider() if self.persona_provider else None
        except Exception:      # noqa: BLE001
            return None

    def resolve(self, persona: str | None = None, load: bool = True) -> Resolution:
        """Which engine + voice would speak right now, and why. Never raises. load=False (used by status displays) never starts discovery: if the voice list
        is not loaded yet it says so instead of checking."""
        vs = self.settings()
        persona = persona if persona is not None else self._persona()
        asks: list[tuple[str, str]] = []
        if persona and vs.voice_by_persona.get(persona):
            asks.append(("persona", vs.voice_by_persona[persona]))
        if vs.tts_voice:
            asks.append(("global", vs.tts_voice))
        notes: list[str] = []
        if asks and not load and not self._loaded:
            eng_id, name = split_id(asks[0][1])
            eng = self.registry.get(eng_id) or self.registry.first_available()
            return Resolution(eng.id if eng else None, name, asks[0][1], asks[0][1], asks[0][0], False, "The list of installed voices has not been read yet.")
        blocked: list[str] = []          # notes for asks whose engine must not be replaced by another engine
        for source, vid in asks:
            eng_id, name = split_id(vid)
            want = self.registry.get(eng_id) if eng_id else None
            who = "for this personality mode" if source == "persona" else "your chosen voice"
            if want is not None and not want.fallback_ok:
                ok0, why0 = want.available()
                if not ok0:
                    msg = f"The voice '{name}' ({who}) needs {want.title}, which is not ready: {why0}"
                    notes.append(msg)
                    blocked.append(msg)
                    continue
            v = self.find(vid, load)
            if v is None:
                msg = f"The voice '{name}' ({who}) is not installed."
                notes.append(msg)
                if want is not None and not want.fallback_ok:
                    blocked.append(msg)
                continue
            eng = self.registry.get(v.engine)
            ok, why = eng.available() if eng else (False, "engine not registered")
            if not ok:
                notes.append(f"The engine for '{v.name}' is unavailable ({why}).")
                continue
            note = (" ".join(notes) + " Using " + (("your default voice") if source == "global" else v.name) + " instead.").strip() if notes else ""
            res = Resolution(v.engine, v.name, v.id, asks[0][1], source, bool(notes), note)
            self.last_resolution = res
            return res
        if blocked:
            res = Resolution(None, None, None, asks[0][1], "unavailable", False,
                             " ".join(blocked) + " Jarvis will not speak with a different engine unless you choose one on the Voice page, so this reply was not spoken.")
            self.last_resolution = res
            return res
        eng = self.registry.first_available()
        if eng is None:
            why = next(iter(self._reasons()), "No speech engine is available.")
            res = Resolution(None, None, None, asks[0][1] if asks else None, "none", bool(asks), why)
        else:
            note = (" ".join(notes) + f" Using the default voice of {eng.title} instead.").strip() if notes else ""
            res = Resolution(eng.id, None, None, asks[0][1] if asks else None, "default", bool(notes), note)
        self.last_resolution = res
        return res

    def _reasons(self) -> list[str]:
        return [why for e in self.registry.engines for ok, why in [e.available()] if not ok and why]

    # ---------------- availability, speaker ----------------
    def available(self) -> tuple[bool, str]:
        if self.registry.first_available() is not None:
            return True, ""
        reasons = self._reasons()
        return False, reasons[0] if reasons else "No speech engine is registered."

    def speaker(self) -> "ResolvingSpeaker":
        return self._speaker

    def engine_speakers(self) -> list:
        return [e._spk for e in self.registry.engines if e._spk is not None]

    def reset_speakers(self) -> None:
        for e in self.registry.engines:
            e.reset_speaker()

    def state(self) -> dict:
        r = self.last_resolution or self.resolve(load=False)
        return {"resolution": r.as_dict(), "note": r.note, "fallback": r.fallback}

    def close(self) -> None:
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(2.0)
        self._speaker.stop()
        for e in self.registry.engines:
            try:
                e.close()
            except Exception:      # noqa: BLE001 - one engine failing to release must not stop the others
                pass


class ResolvingSpeaker:
    """What the rest of the app talks to. It has the same methods as a single speaker (speak / stop / is_speaking) but picks the engine and voice at the moment
    of speaking from the CURRENT settings and personality mode, so a voice change applies to the next sentence without restarting anything."""

    def __init__(self, manager: VoiceManager):
        self.m = manager
        self._active = None
        self.last_stop_error = ""

    def speak(self, text: str, rate: float, volume: float, voice: str | None = None) -> None:
        res = self.m.resolve()
        eng = self.m.registry.get(res.engine_id)
        if eng is None:
            raise EngineError(res.note or "No speech engine is available.")
        sp = eng.speaker()
        self._active = sp
        sp.speak(text, rate, volume, res.voice_name)

    def stop(self) -> None:
        """Silence every engine. Never raises into the caller; a failure is kept in `last_stop_error` so the caller can report it."""
        self.last_stop_error = ""
        for sp in self.m.engine_speakers():
            try:
                sp.stop()
            except Exception as e:      # noqa: BLE001
                self.last_stop_error = type(e).__name__

    def is_speaking(self) -> bool:
        for sp in self.m.engine_speakers():
            try:
                if sp.is_speaking():
                    return True
            except Exception:      # noqa: BLE001
                pass
        return False

    def exit_code(self) -> int | None:
        sp = self._active
        fn = getattr(sp, "exit_code", None)
        return fn() if callable(fn) else None
