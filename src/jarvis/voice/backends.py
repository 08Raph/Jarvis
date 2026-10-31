"""Hardware/engine backends for the voice session. UNTESTED against real hardware in the authoring sandbox
(no audio device, no PortAudio, no Windows). Only their argument handling is tested, using injected fakes.

All three run entirely on the user's machine. None of them opens a network connection. Optional libraries are
imported lazily so the rest of the app works without them. Models are never downloaded here: the user installs a
Vosk model folder themselves (see docs/VOICE.md for licence notes).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import numpy as np

from jarvis.voice.session import MicError

SAMPLE_RATE = 16000
FRAME_SAMPLES = 480  # 30 ms
def hidden_window_kwargs() -> dict:
    """Keyword arguments that stop Windows from flashing a console window when a helper program is started from the windowed app.
    Empty on other systems (and in tests with fake process starters that take no such arguments)."""
    if sys.platform != "win32":
        return {}
    kw: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0              # SW_HIDE
        kw["startupinfo"] = si
    except AttributeError:
        pass
    return kw


POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


class BackendUnavailable(RuntimeError):
    pass


class SoundDeviceMic:
    """16 kHz mono int16 microphone via the `sounddevice` package (PortAudio). Closing the stream releases the device,
    so the Windows microphone-in-use indicator goes away when Jarvis mutes or sleeps."""

    def __init__(self, sd_module=None):
        self._sd = sd_module
        self._stream = None

    def _lib(self):
        if self._sd is None:
            try:
                import sounddevice as sd  # type: ignore
            except Exception as e:
                raise BackendUnavailable(f"sounddevice is not installed or PortAudio is missing ({e}). "
                                         "Install with: pip install sounddevice") from e
            self._sd = sd
        return self._sd

    def devices(self) -> list[dict]:
        sd = self._lib()
        return [{"index": i, "name": d["name"], "channels": d["max_input_channels"]}
                for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]

    def open(self, device: str | None) -> None:
        sd = self._lib()
        dev = None
        if device is not None:
            matches = [d["index"] for d in self.devices() if device.lower() in d["name"].lower() or str(d["index"]) == str(device)]
            if not matches:
                raise MicError(f"microphone {device!r} not found")
            dev = matches[0]
        try:
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                          blocksize=FRAME_SAMPLES, device=dev)
            self._stream.start()
        except Exception as e:
            self._stream = None
            raise MicError(str(e)) from e

    def read(self) -> np.ndarray:
        if self._stream is None:
            raise MicError("microphone is not open")
        try:
            data, overflowed = self._stream.read(FRAME_SAMPLES)
        except Exception as e:
            raise MicError(str(e)) from e
        return np.asarray(data, dtype=np.int16).reshape(-1)

    def close(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
            finally:
                s.close()


class VoskRecognizer:
    """Offline speech recognition with Vosk. Wake mode uses a grammar limited to the wake phrases (plus [unk]), which
    is far less error-prone and cheaper than open dictation. Needs a model directory the USER installed."""

    def __init__(self, model_dir: str | Path, wake_phrases: list[str], vosk_module=None):
        self.model_dir = Path(model_dir)
        self.phrases = [p.lower() for p in wake_phrases]
        self._vosk = vosk_module
        self._model = None
        self._wake = None
        self._dict = None

    def _ensure(self):
        if self._model is not None:
            return
        if self._vosk is None:
            try:
                import vosk  # type: ignore
            except Exception as e:
                raise BackendUnavailable(f"vosk is not installed ({e}). Install with: pip install vosk") from e
            self._vosk = vosk
        if not self.model_dir.is_dir():
            raise BackendUnavailable(f"Vosk model folder not found: {self.model_dir}. Download a model yourself and "
                                     "point to it (see docs/VOICE.md); Jarvis does not download one silently.")
        self._vosk.SetLogLevel(-1)
        self._model = self._vosk.Model(str(self.model_dir))
        self._wake = self._vosk.KaldiRecognizer(self._model, SAMPLE_RATE, json.dumps(self.phrases + ["[unk]"]))
        self._dict = self._vosk.KaldiRecognizer(self._model, SAMPLE_RATE)

    def preload(self) -> None:
        """Load the speech model NOW (on a worker thread) so a missing or broken model is reported before the app says it is listening.
        Raises BackendUnavailable with the real reason. Safe to call repeatedly."""
        try:
            self._ensure()
        except BackendUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 - vosk raises plain Exceptions for a folder that is not a model
            raise BackendUnavailable(f"Vosk could not load the model folder {self.model_dir} ({type(e).__name__}: {str(e)[:160]}). "
                                     "It must be an unpacked Vosk model (it contains folders such as am, conf, graph).") from e

    def feed(self, frame: np.ndarray, mode: str) -> str | None:
        self._ensure()
        rec = self._wake if mode == "wake" else self._dict
        if rec.AcceptWaveform(frame.astype(np.int16).tobytes()):
            text = json.loads(rec.Result()).get("text", "").strip()
            return text if text and text != "[unk]" else None
        return None

    def reset(self) -> None:
        if self._model is not None:
            self._wake.Reset()
            self._dict.Reset()

    def flush(self) -> str:
        if self._dict is None:
            return ""
        text = json.loads(self._dict.FinalResult()).get("text", "").strip()
        self._dict.Reset()
        return text


class SapiSpeaker:
    """Windows built-in speech (System.Speech / SAPI) through PowerShell. The reply text goes to the child process on
    STDIN and settings go through ENVIRONMENT variables, never into the command line, so reply text cannot inject
    commands. stop() kills the process, which silences audio immediately."""

    _SCRIPT = (
        "[Console]::InputEncoding=[Text.Encoding]::UTF8;"
        "$t=[Console]::In.ReadToEnd();"
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Rate=[int]$env:JARVIS_TTS_RATE;$s.Volume=[int]$env:JARVIS_TTS_VOLUME;"
        "if($env:JARVIS_TTS_VOICE){$s.SelectVoice($env:JARVIS_TTS_VOICE)};"
        "$s.Speak($t)"
    )
    _LIST = ("Add-Type -AssemblyName System.Speech;"
             "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices()|"
             "ForEach-Object{$_.VoiceInfo.Name+'|'+$_.VoiceInfo.Culture.Name+'|'+$_.VoiceInfo.Gender+'|'+$_.VoiceInfo.Age+'|'+$_.Enabled}")

    def __init__(self, popen: Callable = subprocess.Popen, run: Callable = subprocess.run, exe: Path = POWERSHELL):
        self._popen, self._run, self.exe = popen, run, str(exe)
        self._proc = None

    @staticmethod
    def rate_to_sapi(rate: float) -> int:
        """1.0 -> 0 (normal); 0.5 -> -10; 2.0 -> +10."""
        r = max(0.5, min(2.0, rate))
        return int(round((r - 1.0) * 10 if r >= 1.0 else (r - 1.0) * 20))

    def list_voices(self) -> list[dict]:
        out = self._run([self.exe, "-NoProfile", "-NonInteractive", "-Command", self._LIST],
                        capture_output=True, text=True, timeout=30, **hidden_window_kwargs())
        voices = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            parts = line.rsplit("|", 4) if line.count("|") >= 4 else line.split("|", 1)
            rec = {"name": parts[0], "culture": parts[1]}
            if len(parts) == 5:        # the engine's own words: Male / Female / Neutral / NotSet, Child / Teen / Adult / Senior / NotSet, True / False
                rec.update(gender=parts[2], age=parts[3], enabled=parts[4].strip().lower() != "false")
            voices.append(rec)
        return voices

    def speak(self, text: str, rate: float, volume: float, voice: str | None) -> None:
        self.stop()
        env = dict(os.environ, JARVIS_TTS_RATE=str(self.rate_to_sapi(rate)),
                   JARVIS_TTS_VOLUME=str(int(round(max(0.0, min(1.0, volume)) * 100))), JARVIS_TTS_VOICE=voice or "")
        self._proc = self._popen([self.exe, "-NoProfile", "-NonInteractive", "-Command", self._SCRIPT],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                                 **hidden_window_kwargs())
        try:
            self._proc.stdin.write(text.encode("utf-8"))
            self._proc.stdin.close()
        except OSError:
            pass

    def stop(self) -> None:
        p, self._proc = self._proc, None
        if p is not None and p.poll() is None:
            p.kill()

    def is_speaking(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def exit_code(self) -> int | None:
        """Exit code of the speech process once it ended on its own (0 = the engine ran to the end). None while it runs, after stop(), or if nothing was started."""
        p = self._proc
        return None if p is None else p.poll()
