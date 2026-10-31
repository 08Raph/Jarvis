"""Is the natural-voice engine (Kokoro) really inside this installation? Imports the packages and checks that espeak-ng's library and data files exist on disk.
Loads no model, plays no sound, opens no network connection. Used by the build (``jarvis.exe runtime --require-kokoro``) and safe to call anywhere."""
from __future__ import annotations

import importlib
import os

MODULES = ("kokoro_onnx", "onnxruntime", "espeakng_loader", "phonemizer", "sounddevice")


def check() -> dict:
    rows, ok = {}, True
    for m in MODULES:
        try:
            importlib.import_module(m)
            rows[m] = "ok"
        except Exception as e:      # noqa: BLE001 - report which one failed and why
            rows[m] = f"FAILED: {type(e).__name__}: {str(e)[:120]}"
            ok = False
    files = {}
    try:
        import espeakng_loader
        for name in ("get_library_path", "get_data_path"):
            p = getattr(espeakng_loader, name)()
            files[name] = {"exists": bool(p) and os.path.exists(p)}
            ok = ok and files[name]["exists"]
    except Exception as e:      # noqa: BLE001
        files["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        ok = False
    return {"ok": ok, "modules": rows, "espeak_files": files,
            "note": "The packages and espeak-ng files are present. This does not prove a voice can be spoken: that needs the Kokoro model files and a sound device."}
