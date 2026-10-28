"""Speech-output engines behind one small interface, so another engine can be added later by writing one class and registering it (no other file changes).

Honest scope: two engines are shipped. Windows' built-in speech (SAPI / System.Speech, through PowerShell) runs on this PC and needs no internet, download or account.
The optional local Kokoro neural engine (kokoro_engine.py, Stage 6.5) needs a pip-installed runtime and a model the person chooses to download. Neither was run
against real speech hardware, real Windows or the real Kokoro model in the authoring sandbox (Linux, no audio device): the code is tested with fakes only.
Voices are listed from what the engine itself reports; this module invents no voice names. The gender category is the engine's own `Gender` field
(Male -> masculine, Female -> feminine); a voice that does not report one is "unspecified" and is never guessed from its name.

Engines are registered in code. Loading engine code from a user folder is deliberately NOT supported (that would run untrusted files)."""
from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

FEMININE, MASCULINE, UNSPECIFIED = "feminine", "masculine", "unspecified"
CATEGORIES = (FEMININE, MASCULINE, UNSPECIFIED)
CATEGORY_LABELS = {FEMININE: "Feminine", MASCULINE: "Masculine", UNSPECIFIED: "Not specified by the engine"}

# BCP-47 pieces -> English display names, used ONLY to turn a voice's own locale code (for example en-GB) into words. Unknown codes are shown as the code.
_LANG = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "sv": "Swedish", "da": "Danish",
         "nb": "Norwegian", "no": "Norwegian", "fi": "Finnish", "pl": "Polish", "ru": "Russian", "uk": "Ukrainian", "cs": "Czech", "tr": "Turkish", "el": "Greek",
         "he": "Hebrew", "ar": "Arabic", "hi": "Hindi", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "id": "Indonesian", "th": "Thai", "vi": "Vietnamese",
         "ca": "Catalan", "hu": "Hungarian", "ro": "Romanian"}
_REGION = {"US": "United States", "GB": "United Kingdom", "AU": "Australia", "CA": "Canada", "IN": "India", "IE": "Ireland", "ZA": "South Africa",
           "NZ": "New Zealand", "ES": "Spain", "MX": "Mexico", "AR": "Argentina", "CO": "Colombia", "FR": "France", "BE": "Belgium", "CH": "Switzerland",
           "DE": "Germany", "AT": "Austria", "IT": "Italy", "PT": "Portugal", "BR": "Brazil", "NL": "Netherlands", "SE": "Sweden", "DK": "Denmark",
           "NO": "Norway", "FI": "Finland", "PL": "Poland", "RU": "Russia", "JP": "Japan", "KR": "South Korea", "CN": "China", "TW": "Taiwan", "HK": "Hong Kong",
           "SG": "Singapore", "PH": "Philippines"}


class EngineError(RuntimeError):
    """A speech engine could not do what was asked; the message is plain text safe to show."""


@dataclass(frozen=True)
class VoiceInfo:
    id: str                       # "<engine>:<name>", the value saved in the settings
    engine: str
    name: str                     # the engine's own name for the voice
    language: str                 # locale code as reported, for example "en-GB" ("" if the engine did not say)
    language_label: str           # words made from that code, for example "English (United Kingdom)"
    category: str                 # feminine / masculine / unspecified
    gender_reported: str          # exactly what the engine said ("Female", "Male", "Neutral", "" ...)
    age: str                      # as reported ("Adult", "Child" ...) or ""
    local: bool = True            # runs on this PC
    needs_internet: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def locale_label(code: str) -> str:
    code = (code or "").replace("_", "-").strip()
    if not code:
        return ""
    parts = code.split("-")
    lang = _LANG.get(parts[0].lower())
    region = next((_REGION.get(p.upper()) for p in parts[1:] if p.upper() in _REGION), None)
    if lang and region:
        return f"{lang} ({region})"
    return lang or code


def category_of(gender: str | None) -> str:
    g = (gender or "").strip().lower()
    if g in ("female", "feminine", "woman"):
        return FEMININE
    if g in ("male", "masculine", "man"):
        return MASCULINE
    return UNSPECIFIED


def make_id(engine: str, name: str) -> str:
    return f"{engine}:{name}"


def split_id(voice_id: str) -> tuple[str | None, str]:
    """('sapi', 'Name') for 'sapi:Name'; (None, 'Name') for an old-style plain name saved before engines existed."""
    if ":" in voice_id:
        e, n = voice_id.split(":", 1)
        if e and e.replace("-", "").replace("_", "").isalnum():
            return e, n
    return None, voice_id


class TTSEngine:
    """Interface. Subclass, set the descriptive fields, implement available/list_voices/_make_speaker."""
    id = "base"
    title = "Speech engine"
    local = True
    needs_internet = False
    requirements = ""
    licence_note = ""
    size_note = ""
    fallback_ok = True      # False: a voice of this engine is never replaced by another engine's voice unless the person chooses that (see VoiceManager.resolve)

    def __init__(self):
        self._spk = None

    def available(self) -> tuple[bool, str]:
        return False, "not implemented"

    def list_voices(self) -> list[VoiceInfo]:
        return []

    def _make_speaker(self):
        raise EngineError("not implemented")

    def speaker(self):
        """The one shared speaker of this engine (speak(text, rate, volume, voice_name), stop(), is_speaking(), optional exit_code())."""
        if self._spk is None:
            self._spk = self._make_speaker()
        return self._spk

    def reset_speaker(self) -> None:
        sp, self._spk = self._spk, None
        if sp is not None:
            try:
                sp.stop()
            except Exception:      # noqa: BLE001
                pass

    def close(self) -> None:
        """Release what the engine holds (called at shutdown). Default: just silence its speaker."""
        sp = self._spk
        if sp is not None:
            try:
                sp.stop()
            except Exception:      # noqa: BLE001
                pass

    def describe(self) -> dict:
        ok, why = self.available()
        return {"id": self.id, "title": self.title, "available": ok, "note": why, "local": self.local, "needs_internet": self.needs_internet,
                "requirements": self.requirements, "licence": self.licence_note, "size": self.size_note}


class SapiEngine(TTSEngine):
    id = "sapi"
    title = "Windows built-in speech (SAPI)"
    requirements = ("Windows with Windows PowerShell. Uses the voices installed in Windows (System.Speech lists the classic desktop voices; newer 'natural' voices that "
                    "Windows 11 keeps in a different store may not appear). Add more voices in Windows Settings > Time & language > Speech.")
    licence_note = "Part of Windows; nothing is bundled or downloaded by Project JARVIS. Each voice has its own Microsoft licence."
    size_note = "No download."

    def __init__(self, speaker=None, platform: str | None = None):
        super().__init__()
        self._given = speaker
        self._platform = platform or sys.platform

    def available(self) -> tuple[bool, str]:
        if self._given is not None:
            return True, ""
        if self._platform != "win32":
            return False, "Speech output needs Windows (SAPI through PowerShell)."
        from jarvis.voice.backends import POWERSHELL
        if not Path(POWERSHELL).exists():
            return False, "Windows PowerShell was not found."
        return True, ""

    def _make_speaker(self):
        if self._given is not None:
            return self._given
        from jarvis.voice.backends import SapiSpeaker
        return SapiSpeaker()

    def list_voices(self) -> list[VoiceInfo]:
        ok, why = self.available()
        if not ok:
            raise EngineError(why)
        try:
            raw = self.speaker().list_voices()
        except Exception as e:      # noqa: BLE001 - PowerShell missing, blocked, timed out...
            raise EngineError(f"Windows could not list its voices ({type(e).__name__}).") from e
        out: list[VoiceInfo] = []
        seen: set[str] = set()
        for r in raw or []:
            name = str(r.get("name") or "").strip()
            if not name or name in seen or r.get("enabled") is False:
                continue
            seen.add(name)
            code = str(r.get("culture") or "")
            g = str(r.get("gender") or "")
            out.append(VoiceInfo(id=make_id(self.id, name), engine=self.id, name=name, language=code, language_label=locale_label(code),
                                 category=category_of(g), gender_reported=g, age=str(r.get("age") or "").replace("NotSet", ""), local=True))
        return out


class FactoryEngine(TTSEngine):
    """Wraps a speaker object made by a caller-supplied factory (tests, or a future embedding). Lists voices only if the speaker can."""
    def __init__(self, factory: Callable, engine_id: str = "custom", title: str = "Custom speaker"):
        super().__init__()
        self.id, self.title, self._factory = engine_id, title, factory

    def available(self) -> tuple[bool, str]:
        return True, ""

    def _make_speaker(self):
        return self._factory()

    def list_voices(self) -> list[VoiceInfo]:
        sp = self.speaker()
        if not hasattr(sp, "list_voices"):
            return []
        out = []
        for r in sp.list_voices() or []:
            name = str(r.get("name") or "").strip()
            if name:
                code = str(r.get("culture") or r.get("language") or "")
                g = str(r.get("gender") or "")
                out.append(VoiceInfo(id=make_id(self.id, name), engine=self.id, name=name, language=code, language_label=locale_label(code),
                                     category=category_of(g), gender_reported=g, age=str(r.get("age") or "")))
        return out


class EngineRegistry:
    def __init__(self, engines: list[TTSEngine] | None = None):
        self._engines: list[TTSEngine] = []
        for e in engines or []:
            self.add(e)

    def add(self, engine: TTSEngine) -> None:
        if any(e.id == engine.id for e in self._engines):
            raise ValueError(f"an engine with id {engine.id!r} is already registered")
        self._engines.append(engine)

    def get(self, engine_id: str | None) -> TTSEngine | None:
        return next((e for e in self._engines if e.id == engine_id), None)

    @property
    def engines(self) -> list[TTSEngine]:
        return list(self._engines)

    def first_available(self) -> TTSEngine | None:
        return next((e for e in self._engines if e.available()[0]), None)


def default_registry(platform: str | None = None, kokoro_data_dir: str | Path | None = None) -> EngineRegistry:
    """Windows speech, plus Kokoro when a data folder is given (its model files live there). Kokoro is registered even when its runtime or model is missing,
    so the Voice page can explain what is missing; it is simply 'unavailable' until installed."""
    engines: list[TTSEngine] = [SapiEngine(platform=platform)]
    if kokoro_data_dir is not None:
        from jarvis.voice.kokoro_engine import KokoroEngine
        engines.append(KokoroEngine(kokoro_data_dir))
    return EngineRegistry(engines)
