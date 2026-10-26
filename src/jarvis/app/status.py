"""Honest status of the model, the runtime and each feature. A feature is only shown as 'available' when its dependencies are present; and
EVERY row carries what has actually been verified, so the window never suggests that something works when it was only written or tested on Linux."""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass

from jarvis.config import Config

# Evidence labels (see docs/PROMPT10_REVIEW.md, verification matrix)
V_LINUX_SIM = "tested on Linux with a simulated desktop/fake inputs; NOT verified on Windows 11"
V_LINUX_FAKE_MODEL = "tested on Linux with a scripted fake model; NOT verified with the real Qwen model or on Windows 11"
V_UNTESTED = "written, but never run on real hardware/Windows"


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


_IMPORT_CACHE: dict[str, str | None] = {}


def import_problem(name: str, refresh: bool = False) -> str | None:
    """None when `name` can really be imported; otherwise a short description. This is a MEASUREMENT (the import is attempted, once, and cached),
    unlike has_module(), which only looks for the package on disk. On Windows a package can be present and still fail to load (a missing DLL,
    a Visual C++ runtime that is not installed), so 'present' must never be reported as 'works'."""
    if not refresh and name in _IMPORT_CACHE:
        return _IMPORT_CACHE[name]
    try:
        importlib.import_module(name)
        res = None
    except ImportError as e:                       # includes ModuleNotFoundError and Windows DLL load failures
        res = f"{type(e).__name__}: {str(e)[:200]}"
    except Exception as e:  # noqa: BLE001 - a broken native library can fail in other ways
        res = f"{type(e).__name__}: {str(e)[:200]}"
    _IMPORT_CACHE[name] = res
    return res


@dataclass
class ModelStatus:
    state: str            # ready | no_model | runtime_missing | model_invalid | not_a_chat_model | error
    summary: str
    action: str = ""      # what the user can do about it
    model_id: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ready"


def model_status(cfg: Config) -> ModelStatus:
    """Never raises. Does NOT load the model (loading takes seconds and memory); it checks that loading has a chance."""
    from jarvis.models.manager import ModelStore
    from jarvis.net.policy import NetworkPolicy
    try:
        store = ModelStore(cfg.models_dir, NetworkPolicy.load(cfg.settings_path))
        active = store.active()
    except Exception as e:  # noqa: BLE001 - damaged models folder / active.json / meta.json
        return ModelStatus("error", f"The models folder could not be read ({type(e).__name__}).",
                           "Run `jarvis models list` to see the problem, or reinstall the model with `jarvis setup`.")
    if active is None:
        return ModelStatus("no_model", "No chat model is installed, so the assistant cannot answer yet.",
                           "Run `jarvis setup` (guided), or `jarvis models fetch qwen3-1.7b-q4_k_m` (needs internet once, asks first), "
                           "or `jarvis models add-gguf FILE` for a .gguf file you already have. See docs/MODEL_DISTRIBUTION.md.")
    try:
        if store.kind(active) != "gguf":
            return ModelStatus("not_a_chat_model", f"The selected model '{active}' is a from-scratch research checkpoint, not a chat model.",
                               "Install the free chat model: `jarvis models fetch qwen3-1.7b-q4_k_m`.", active)
        ok, problems = store.verify(active, quick=True)
    except Exception as e:  # noqa: BLE001
        return ModelStatus("model_invalid", f"Model '{active}' could not be checked ({type(e).__name__}).",
                           "Reinstall it: `jarvis models remove` then `jarvis models fetch ...`.", active)
    if not ok:
        return ModelStatus("model_invalid", f"Model '{active}' failed its integrity check: {'; '.join(problems)}.",
                           f"Run `jarvis models verify {active}`; if it fails, reinstall the model (your memory is not affected).", active)
    if not has_module("llama_cpp"):
        return ModelStatus("runtime_missing", "The local AI runtime (llama-cpp-python) is not installed in this copy of Jarvis.",
                           "Use the official installer build, or `pip install llama-cpp-python`. See docs/LOCAL_MODEL.md.", active)
    broken = import_problem("llama_cpp")
    if broken:
        return ModelStatus("runtime_missing", f"The local AI runtime (llama-cpp-python) is present but could not be loaded ({broken}).",
                           "On Windows this is usually a missing Visual C++ runtime or a CPU without AVX2. Run `jarvis runtime` for details and see docs/LOCAL_MODEL.md.", active)
    return ModelStatus("ready", f"Model '{active}' is installed and passed the quick file check. It has not been loaded yet; "
                                "the first message loads it (this takes a few seconds and a few GB of memory).", "", active)


def feature_rows(cfg: Config, settings=None) -> list[dict]:
    """One row per user-visible capability: {id, title, state, detail, evidence}. state: available | needs_setup | unavailable | experimental."""
    def mod(name, why):
        return has_module(name), why
    ms = model_status(cfg)
    voice_ok = has_module("sounddevice") and has_module("vosk")
    win = sys.platform == "win32"
    rows = [
        {"id": "chat", "title": "Chat with the local model", "state": "available" if ms.ok else "needs_setup",
         "detail": ms.summary + (" " + ms.action if ms.action else ""), "evidence": V_LINUX_FAKE_MODEL},
        {"id": "memory", "title": "Personal memory (review, edit, export, delete)", "state": "available",
         "detail": "Stored locally in your profile folder; not encrypted.", "evidence": "tested on Linux with real SQLite files"},
        {"id": "voice", "title": "Voice (microphone, spoken replies)", "state": "needs_setup" if not voice_ok else "experimental",
         "detail": ("Needs sounddevice and vosk plus a Vosk model. " if not voice_ok else "Packages present. ") + "Hands-free voice is OFF by default; when you turn it on (Voice page) the window opens the microphone only while enabled and not muted. Replies can be read aloud. Presence of packages is not proof they work: see the Status page.",
         "evidence": V_UNTESTED},
        {"id": "desktop", "title": "Desktop operations (files, apps, windows, UI, input)", "state": "experimental" if win else "unavailable",
         "detail": "Only on Windows, each category is OFF until you enable it with `jarvis desktop enable ...`; risky actions ask every time." if win
         else "Windows only; this system is not Windows, so only the simulated test desktop exists.", "evidence": V_LINUX_SIM},
        {"id": "vision", "title": "Screen observation and OCR", "state": "experimental" if win else "unavailable",
         "detail": "OCR and accessibility only; there is no image-understanding model. Asked on every capture.", "evidence": V_LINUX_SIM},
        {"id": "monitoring", "title": "System monitoring and Gaming mode", "state": "available" if has_module("psutil") else "needs_setup",
         "detail": "Read-only readings; needs recorded consent. FPS only from logs you import. psutil " + ("found." if has_module("psutil") else "not installed (optional)."),
         "evidence": V_LINUX_SIM},
        {"id": "code", "title": "Coding tools", "state": "experimental", "detail": "Sandboxed tests/snippets; permissions off by default; every change is asked.",
         "evidence": V_LINUX_SIM},
        {"id": "research", "title": "Research and sources", "state": "needs_setup", "detail": "Needs the internet feature 'web_research' (off by default) for any web request.",
         "evidence": "tested on Linux with local fixtures only; no real internet request"},
        {"id": "missions", "title": "Missions and schedules", "state": "experimental",
         "detail": "Plans need your approval; schedules run only while Jarvis is open and checking.", "evidence": V_LINUX_SIM},
        {"id": "updates", "title": "Update check", "state": "needs_setup", "detail": "Off unless you run `jarvis update-check`; nothing is installed automatically.",
         "evidence": "tested with fake manifests"},
    ]
    return rows


def optional_dependencies() -> dict:
    """{name: found on disk}. This does NOT prove the package loads; see runtime_checks() for the measured import of the ones that matter."""
    names = {"PySide6": "PySide6", "llama_cpp": "llama_cpp", "sounddevice": "sounddevice", "vosk": "vosk", "psutil": "psutil",
             "comtypes": "comtypes", "pypdf": "pypdf", "torch": "torch"}
    return {label: has_module(mod) for label, mod in names.items()}


def runtime_checks() -> dict:
    """Measured (import attempted) state of the two native runtimes the product depends on: {"llama_cpp": {"loads": bool|None, "problem": str}, ...}.
    loads is None when the package is not installed (nothing was measured)."""
    out = {}
    for mod in ("llama_cpp", "PySide6"):
        if not has_module(mod):
            out[mod] = {"loads": None, "problem": "not installed (nothing was measured)"}
            continue
        p = import_problem(mod)
        out[mod] = {"loads": p is None, "problem": p or ""}
    return out
