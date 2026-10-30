"""Voice settings: validated, persisted as JSON in the user's data dir. Nothing here touches audio or network."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

INPUT_MODES = ("wake_word", "push_to_talk", "off")
OUTPUT_MODES = ("voice_and_text", "voice_only", "text_only")
BARGE_IN = ("off", "wake_word_only", "speech")  # how the user may interrupt while Jarvis is speaking
LENGTHS = ("brief", "normal", "detailed")


@dataclass
class VoiceSettings:
    input_mode: str = "off"                 # microphone is OFF until the user turns it on
    wake_phrase: str = "hey jarvis"
    wake_alternatives: list[str] = field(default_factory=lambda: ["hey computer", "okay assistant"])
    sensitivity: float = 0.5                # 0 strict .. 1 lenient (fuzzy-match tolerance)
    mic_device: str | None = None           # None = system default
    muted: bool = False
    output_mode: str = "voice_and_text"
    tts_voice: str | None = None
    rate: float = 1.0                       # 0.5 .. 2.0
    volume: float = 0.8                     # 0 .. 1
    response_length: str = "normal"
    barge_in: str = "wake_word_only"        # safe default: no echo cancellation exists, so speech barge-in is opt-in
    follow_up_seconds: float = 8.0
    max_utterance_seconds: float = 20.0
    silence_end_seconds: float = 0.9
    sleep_after_seconds: float = 0.0        # 0 = keep listening while enabled; >0 closes the mic after this much idle time
    pronunciations: dict[str, str] = field(default_factory=dict)
    vosk_model: str | None = None           # folder of a Vosk model: one the USER installed, or the small English model copied from the installer on first start (jarvis.voice.bundled_vosk); never downloaded by Jarvis
    speaker_muted: bool = False             # speaker mute (nothing is spoken; text still shown). Separate from the microphone mute `muted`
    post_speech_guard_seconds: float = 0.6  # after Jarvis finishes speaking, frames are ignored this long so its own echo is not heard as the user
    speak_in_window: bool = False           # the window reads replies aloud (Windows SAPI) only when this is turned on
    voice_by_persona: dict[str, str] = field(default_factory=dict)   # OPTIONAL per-personality-mode voice ids; empty = every mode uses `tts_voice` (the global default)
    listening_choice_made: bool = False     # False = the user has never chosen: the window's first start turns wake-word listening ON (it still needs a sound device and a Vosk model before any microphone is opened). True once the user turns it ON or OFF themselves, so an OFF choice is never overridden
    vad_min_rms: float = 0.01               # speech-detection floor (0.001..0.2): higher = needs louder speech to count as speaking

    def validate(self) -> "VoiceSettings":
        def need(cond, msg):
            if not cond:
                raise ValueError(msg)
        need(self.input_mode in INPUT_MODES, f"input_mode must be one of {INPUT_MODES}")
        need(self.output_mode in OUTPUT_MODES, f"output_mode must be one of {OUTPUT_MODES}")
        need(self.barge_in in BARGE_IN, f"barge_in must be one of {BARGE_IN}")
        need(self.response_length in LENGTHS, f"response_length must be one of {LENGTHS}")
        need(0.0 <= self.sensitivity <= 1.0, "sensitivity must be 0..1")
        need(0.5 <= self.rate <= 2.0, "rate must be 0.5..2.0")
        need(0.0 <= self.volume <= 1.0, "volume must be 0..1")
        need(0.0 <= self.follow_up_seconds <= 60, "follow_up_seconds must be 0..60")
        need(1.0 <= self.max_utterance_seconds <= 120, "max_utterance_seconds must be 1..120")
        need(0.2 <= self.silence_end_seconds <= 5, "silence_end_seconds must be 0.2..5")
        need(self.sleep_after_seconds == 0 or self.sleep_after_seconds >= 10, "sleep_after_seconds must be 0 or >= 10")
        need(len(self.wake_phrase.split()) >= 2, "wake phrase needs at least two words (one word triggers falsely)")
        for p in self.wake_alternatives:
            need(len(p.split()) >= 2, f"wake alternative {p!r} needs at least two words")
        need(all(isinstance(k, str) and k and isinstance(v, str) for k, v in self.pronunciations.items()),
             "pronunciations must map non-empty words to text")
        need(self.vosk_model is None or (isinstance(self.vosk_model, str) and 0 < len(self.vosk_model) <= 500 and "\x00" not in self.vosk_model), "vosk_model must be a folder path or empty")
        need(isinstance(self.speak_in_window, bool), "speak_in_window must be true or false")
        need(self.tts_voice is None or (isinstance(self.tts_voice, str) and 0 < len(self.tts_voice) <= 200 and "\x00" not in self.tts_voice), "tts_voice must be a voice id or empty")
        need(isinstance(self.voice_by_persona, dict) and len(self.voice_by_persona) <= 40
             and all(isinstance(k, str) and 0 < len(k) <= 40 and isinstance(v, str) and 0 < len(v) <= 200 and "\x00" not in v for k, v in self.voice_by_persona.items()),
             "voice_by_persona must map personality mode ids to voice ids")
        need(isinstance(self.vad_min_rms, (int, float)) and not isinstance(self.vad_min_rms, bool) and 0.001 <= self.vad_min_rms <= 0.2, "vad_min_rms must be 0.001..0.2")
        need(isinstance(self.listening_choice_made, bool), "listening_choice_made must be true or false")
        need(isinstance(self.speaker_muted, bool), "speaker_muted must be true or false")
        need(isinstance(self.muted, bool), "muted must be true or false")
        need(isinstance(self.post_speech_guard_seconds, (int, float)) and not isinstance(self.post_speech_guard_seconds, bool) and 0.0 <= self.post_speech_guard_seconds <= 3.0, "post_speech_guard_seconds must be 0..3")
        return self

    def all_wake_phrases(self) -> list[str]:
        return [self.wake_phrase, *self.wake_alternatives]

    def save(self, path: str | Path) -> None:
        self.validate()
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(p)

    @classmethod
    def load(cls, path: str | Path) -> "VoiceSettings":
        """Missing or corrupt file -> safe defaults (mic off). Unknown keys ignored; invalid values rejected to defaults."""
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            names = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in raw.items() if k in names}).validate()
        except (ValueError, TypeError, OSError):
            return cls()
