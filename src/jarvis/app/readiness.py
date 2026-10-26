"""Layered, honest readiness (Prompt 16). A file existing is NOT readiness. Each level states what was actually MEASURED and what was not.

Model levels, in order: file found -> runtime installed -> runtime loads -> model loaded and an inference completed (a RECORDED `jarvis selftest`
result for this exact model) -> real tool calling verified -> offline operation verified -> personality adherence checked on the real model.
The last three have no automatic proof in this program, so they say 'not verified' unless a recorded report exists, and even then the text says
what the report does and does not show. Nothing here loads a model, opens a microphone, plays sound or touches the network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jarvis.app import status
from jarvis.config import Config

YES, NO, UNKNOWN = "yes", "no", "not verified"


def _row(id_: str, label: str, state: str, detail: str) -> dict:
    return {"id": id_, "label": label, "state": state, "detail": detail}


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def model_levels(cfg: Config) -> list[dict]:
    ms = status.model_status(cfg)
    file_ok = ms.state in ("ready", "runtime_missing") and bool(ms.model_id)
    rows = [_row("model_file", "Model file found and passes the quick file check", YES if file_ok else NO,
                 ms.summary if not file_ok else f"'{ms.model_id}' is installed. This says nothing about whether it loads or answers.")]
    installed = status.has_module("llama_cpp")
    rows.append(_row("runtime_installed", "Runtime package (llama-cpp-python) installed", YES if installed else NO,
                     "Found on disk." if installed else "Not installed in this copy of Jarvis."))
    problem = status.import_problem("llama_cpp") if installed else "not installed"
    rows.append(_row("runtime_loads", "Runtime library loads (import attempted)", YES if (installed and not problem) else NO,
                     "Import succeeded." if installed and not problem else (problem or "not measured")))
    st = _read_json(cfg.data_dir / "selftest.json")
    loaded = NO
    detail = "No recorded self-test. Run `jarvis selftest` (it really loads the model and generates text)."
    if isinstance(st, dict) and ms.model_id:
        same = st.get("model_id") == ms.model_id
        if st.get("passed") and same:
            loaded, detail = YES, f"A recorded self-test on {st.get('time', 'an earlier date')} loaded this model and produced text. This is a past record, not a live check."
        elif same:
            detail = "The last recorded self-test FAILED: " + str(st.get("error", "no text was produced"))[:200]
        else:
            detail = "The recorded self-test was for a different model. Run `jarvis selftest` again."
    rows.append(_row("model_inference", "Model loaded and an inference completed", loaded, detail))
    rows.append(_row("tool_calling", "Real tool calling verified with the model", UNKNOWN,
                     "No recorded real-model tool-calling test exists. Tool calls are only tested with a scripted fake model so far."))
    rows.append(_row("offline", "Offline operation verified", UNKNOWN,
                     "Nothing records an offline run on this PC. Network access is off by default and tests with the network blocked passed on Linux, but that is not a Windows offline check."))
    pv = _read_json(cfg.data_dir / "persona_validation.json")
    if isinstance(pv, dict) and pv.get("model_id") == ms.model_id and ms.model_id:
        rows.append(_row("persona_adherence", "Personality styles checked with the real model", UNKNOWN,
                         f"A self-check ran on {pv.get('time', '?')} ({pv.get('automatic_checks_passed', '?')}/{pv.get('automatic_checks_total', '?')} automatic checks passed). "
                         "Automatic checks are limited (facts kept, labels, return to Normal, length); whether each style SOUNDS right needs a person to read the saved outputs."))
    else:
        rows.append(_row("persona_adherence", "Personality styles checked with the real model", UNKNOWN,
                         "Not run. After `jarvis selftest` passes, run `jarvis persona selfcheck` and read the outputs."))
    return rows


def model_headline(cfg: Config) -> str:
    """One honest sentence for the banner. Never says 'ready' on the strength of a file."""
    rows = {r["id"]: r for r in model_levels(cfg)}
    if rows["model_file"]["state"] != YES:
        return rows["model_file"]["detail"]
    if rows["runtime_loads"]["state"] != YES:
        return "A model file is installed, but the local runtime is not usable: " + rows["runtime_loads"]["detail"]
    if rows["model_inference"]["state"] != YES:
        return "Model file and runtime found, but the model has not been confirmed to load and answer on this PC (run `jarvis selftest`)."
    return "A recorded self-test shows the model loaded and answered. Tool calling, offline use and personality adherence are not verified."


def voice_levels(cfg: Config, vs=None) -> list[dict]:
    from jarvis.voice.settings import VoiceSettings
    vs = vs or VoiceSettings.load(cfg.profile_dir / "voice.json")
    rows = []
    sd_prob = status.import_problem("sounddevice") if status.has_module("sounddevice") else "not installed"
    rows.append(_row("mic_library", "Microphone library (sounddevice and PortAudio) loads", NO if sd_prob else YES, sd_prob or "Import succeeded."))
    if sd_prob is None:
        try:
            from jarvis.voice.backends import SoundDeviceMic
            n = len(SoundDeviceMic().devices())
            rows.append(_row("mic_devices", "Microphone devices listed", YES if n else NO,
                             f"{n} input device(s) listed. Listing devices does not record anything." if n else "The system lists no input device."))
        except Exception as e:  # noqa: BLE001 - a driver error must show as a status, not a crash
            rows.append(_row("mic_devices", "Microphone devices listed", NO, f"{type(e).__name__}: {str(e)[:160]}"))
    else:
        rows.append(_row("mic_devices", "Microphone devices listed", UNKNOWN, "Not measured because the microphone library does not load."))
    v_prob = status.import_problem("vosk") if status.has_module("vosk") else "not installed"
    rows.append(_row("vosk_package", "Speech recognition package (Vosk) loads", NO if v_prob else YES, v_prob or "Import succeeded."))
    mp = vs.vosk_model
    if not mp:
        rows.append(_row("vosk_model", "Vosk model folder", NO, "None set. Download a model yourself, then `jarvis voice set vosk_model FOLDER` (see docs/VOICE.md). Jarvis never downloads one silently."))
    elif Path(mp).is_dir():
        rows.append(_row("vosk_model", "Vosk model folder", YES, f"Folder exists: {mp}. It has not been loaded or checked as a valid model."))
    else:
        rows.append(_row("vosk_model", "Vosk model folder", NO, f"Folder not found: {mp}"))
    from jarvis.voice.backends import POWERSHELL
    sapi = sys.platform == "win32" and Path(POWERSHELL).exists()
    rows.append(_row("tts_engine", "Speech output (Windows SAPI through PowerShell) available", YES if sapi else NO,
                     "Windows and PowerShell found. A voice has not been played." if sapi else "Needs Windows with PowerShell; this system does not have it."))
    rows.append(_row("speak_in_window", "This window speaks replies aloud", YES if vs.speak_in_window else NO,
                     "On (you turned it on)." if vs.speak_in_window else "Off (default). Turn it on in the Personality page."))
    if vs.input_mode == "off":
        rows.append(_row("mic_in_window", "This window uses the microphone (hands-free voice)", NO,
                         "Off (default). The microphone is not opened. Turn hands-free ON on the Voice page; it is never turned on for you."))
    else:
        rows.append(_row("mic_in_window", "This window uses the microphone (hands-free voice)", YES,
                         f"Enabled ({vs.input_mode}" + (", currently muted: the microphone is closed" if vs.muted else "") + "). The Voice page shows whether it is actually open; this row is only the saved setting."))
    rows.append(_row("hardware_verified", "Speech recognition, speaker output, wake-word accuracy, latency, interruption", UNKNOWN,
                     "Never run on real audio hardware. Only logic with fake audio was tested."))
    return rows
