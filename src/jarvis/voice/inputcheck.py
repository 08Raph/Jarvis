"""Read-only check of speech INPUT readiness: which microphones Windows lists, whether the chosen one is among them, and whether the local recogniser
(Vosk + a model folder the user installed) is present.

It never opens a microphone stream and never records: it only asks the audio library for the device list and looks at files. Therefore a "ready"
answer means "everything Jarvis needs appears to be there"; it does NOT prove that Windows allows this app to use the microphone, that nothing else is using
it, or that recognition is accurate. Those are only found out when listening is turned on."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Callable

from jarvis.voice.settings import VoiceSettings

NOTE = ("This check only lists devices and looks for files; it does not open the microphone. Whether Windows allows Jarvis to use it, and how well speech is "
        "recognised, is only known once listening is turned on.")
PRIVACY_HINT = "Windows Settings > Privacy & security > Microphone must allow desktop apps."


def vosk_installed() -> bool:
    try:
        return importlib.util.find_spec("vosk") is not None
    except (ImportError, ValueError):
        return False


def looks_like_vosk_model(folder: str | None) -> bool:
    if not folder:
        return False
    p = Path(folder)
    try:
        return p.is_dir() and any((p / n).is_dir() for n in ("am", "conf", "graph"))
    except OSError:
        return False


def check_input(vs: VoiceSettings, mic_factory: Callable | None = None, vosk_present: Callable[[], bool] = vosk_installed) -> dict:
    problems: list[str] = []
    mic_info = {"library_ok": False, "devices": [], "selected": vs.mic_device, "selected_found": None, "using": "system default" if vs.mic_device is None else vs.mic_device}
    try:
        from jarvis.voice.backends import BackendUnavailable, SoundDeviceMic
        mic = mic_factory() if mic_factory else SoundDeviceMic()
        try:
            devs = mic.devices() if hasattr(mic, "devices") else []
            mic_info["library_ok"] = True
            mic_info["devices"] = [{"index": d.get("index"), "name": str(d.get("name", "")), "channels": d.get("channels")} for d in devs]
        except BackendUnavailable as e:
            problems.append(str(e))
        except Exception as e:      # noqa: BLE001
            problems.append(f"The microphone list could not be read ({type(e).__name__}).")
    except Exception as e:      # noqa: BLE001
        problems.append(f"The audio library could not be started ({type(e).__name__}).")
    if mic_info["library_ok"]:
        if not mic_info["devices"]:
            problems.append("No microphone was found. Plug one in or switch it on, then check again. " + PRIVACY_HINT)
        elif vs.mic_device is not None:
            needle = str(vs.mic_device).lower()
            found = any(needle in d["name"].lower() or str(d["index"]) == str(vs.mic_device) for d in mic_info["devices"])
            mic_info["selected_found"] = found
            if not found:
                problems.append(f"The chosen microphone '{vs.mic_device}' is not connected. Pick another or use the system default.")
    rec = {"engine": "vosk", "installed": bool(vosk_present()), "model_folder": vs.vosk_model, "model_ok": looks_like_vosk_model(vs.vosk_model),
           "size_note": "On the publisher's model page (alphacephei.com/vosk/models) the small US English model is listed at 40M; larger models are much bigger. Jarvis never downloads one by itself.",
           "licence_note": "Each model has its own licence: that page lists the small US English model as Apache 2.0, while other models are GPL, AGPL or non-commercial. Check the licence of the model you choose. Free; works offline."}
    if not rec["installed"]:
        problems.append("The local speech recogniser (the 'vosk' Python package) is not installed.")
    if not vs.vosk_model:
        problems.append("No Vosk speech model folder is set. Download a small English model yourself, unzip it and choose its folder.")
    elif not rec["model_ok"]:
        problems.append(f"The folder set for the Vosk model does not look like an unpacked model: {vs.vosk_model}")
    return {"ready": not problems, "microphone": mic_info, "recognizer": rec, "problems": problems, "note": NOTE,
            "listening_enabled": vs.input_mode != "off", "wake_phrase": vs.wake_phrase, "mode": vs.input_mode}
