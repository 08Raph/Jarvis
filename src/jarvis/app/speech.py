"""Spoken replies for the window (Prompt 16; Prompt 17 added the shared speaker and speaker mute). Reuses the existing SapiSpeaker and speakable()/shorten()/pronunciations; invents no engine.

OFF by default (voice.json `speak_in_window`). It only SPEAKS: this module never opens a microphone (the hands-free microphone lives in voice_service.py). Text goes to the speaker after speakable() has
removed code, paths, links and long identifiers. Any speaker failure is caught and reported as text; speech can always be stopped, and stop() is
safe to call at any time (emergency stop, return to Normal, closing the window)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from jarvis.config import Config
from jarvis.voice.settings import VoiceSettings
from jarvis.voice.speakable import apply_pronunciations, shorten, speakable


class SpeechOut:
    def __init__(self, cfg: Config, speaker_factory: Callable | None = None, platform: str | None = None):
        self.cfg = cfg
        self._factory = speaker_factory
        self._platform = platform or sys.platform
        self._spk = None
        self.last_error = ""

    def settings(self) -> VoiceSettings:
        return VoiceSettings.load(self.cfg.profile_dir / "voice.json")

    def update(self, **kw) -> VoiceSettings:
        """Change ONLY the window-speech options (speak_in_window, rate, volume); validated before anything is written. Microphone options are not reachable."""
        from dataclasses import replace
        bad = set(kw) - {"speak_in_window", "rate", "volume"}
        if bad:
            raise ValueError(f"unknown voice option(s): {sorted(bad)}")
        vs = replace(self.settings(), **kw).validate()
        vs.save(self.cfg.profile_dir / "voice.json")
        return vs

    def available(self) -> tuple[bool, str]:
        if self._factory is not None:
            return True, ""
        if self._platform != "win32":
            return False, "Speech output needs Windows (SAPI through PowerShell)."
        from jarvis.voice.backends import POWERSHELL
        if not Path(POWERSHELL).exists():
            return False, "Windows PowerShell was not found."
        return True, ""

    def enabled(self) -> bool:
        return self.settings().speak_in_window and self.available()[0]

    def speaker(self):
        """The one shared speaker (the hands-free voice service uses it too, so there is never a second audio output)."""
        if self._spk is None:
            if self._factory is not None:
                self._spk = self._factory()
            else:
                from jarvis.voice.backends import SapiSpeaker
                self._spk = SapiSpeaker()
        return self._spk

    def speak(self, text: str, rate_factor: float = 1.0) -> bool:
        """Speak `text` if (and only if) the user turned spoken replies on and the engine exists. Returns True when speech was started."""
        self.last_error = ""
        vs = self.settings()
        if not vs.speak_in_window or vs.speaker_muted:
            return False
        ok, why = self.available()
        if not ok:
            self.last_error = why
            return False
        say = shorten(apply_pronunciations(speakable(text or ""), vs.pronunciations), vs.response_length)
        if not say:
            return False
        try:
            f = float(rate_factor)
            f = f if f == f else 1.0
        except (TypeError, ValueError):
            f = 1.0
        rate = max(0.5, min(2.0, vs.rate * f))
        try:
            self.speaker().speak(say, rate, vs.volume, vs.tts_voice)
            return True
        except Exception as e:  # noqa: BLE001 - a TTS failure must be a message, never a crash
            self.last_error = f"Speech output failed: {type(e).__name__}: {str(e)[:160]}"
            self._spk = None
            return False

    def stop(self) -> None:
        sp = self._spk
        if sp is not None:
            try:
                sp.stop()
            except Exception as e:  # noqa: BLE001
                self.last_error = f"Could not stop speech cleanly: {type(e).__name__}"

    def is_speaking(self) -> bool:
        try:
            return bool(self._spk is not None and self._spk.is_speaking())
        except Exception:  # noqa: BLE001
            return False
