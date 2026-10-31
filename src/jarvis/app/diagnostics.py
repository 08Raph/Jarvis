"""A diagnostics report you can read, copy and send if you choose. Built from local facts only: nothing is uploaded, personal text (conversations,
memory, file contents) is never included, secret-looking strings are redacted, and your Windows user name is replaced in paths."""
from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from pathlib import Path

from jarvis import version as V
from jarvis.app import status
from jarvis.config import Config


def scrub_paths(text: str) -> str:
    """Replace the home folder (which contains the Windows user name) and redact secret-looking text."""
    try:
        from jarvis.disclosure import scrub_error_text    # long quoted payloads (a prompt copied into an error) are dropped
        text = "\n".join(scrub_error_text(l, 2000) if len(l) > 300 else l for l in text.splitlines())
    except Exception:  # noqa: BLE001
        pass
    home = str(Path.home())
    for h in {home, home.replace("\\", "/")}:
        if h and len(h) > 3:
            text = text.replace(h, "~")
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if len(user) >= 3:
        text = text.replace(user, "<user>")
    try:
        from jarvis.memory.secrets import redact
        text = redact(text)
    except Exception:  # noqa: BLE001
        pass
    return text


def log_tail(cfg: Config, name: str = "crash.log", max_chars: int = 4000) -> str:
    p = Path(cfg.data_dir) / name
    try:
        return scrub_paths(p.read_text(encoding="utf-8", errors="replace")[-max_chars:])
    except OSError:
        return ""


INCLUDED_FIELDS = (
    "App name, version and release channel; whether this is the packaged app; Python and Windows versions; the data folder (your user name replaced).",
    "Hardware: CPU thread count, RAM, free disk space, GPU name and video memory (NVIDIA only), and whether the CPU reports AVX2.",
    "AI runtime: whether it loads, its version, and whether it was built with a GPU backend. Acceleration: detected / backend available / verified, and the saved mode.",
    "Model: its id, a quick file check (not a full hash), and the result of the last setup (checks passed or failed, speeds). Never the model's replies to you.",
    "Brains: how many models are in the library, which is active, whether each is registered in place or stored by Jarvis, its architecture and quantization, and its last test result (pass/fail and time). Never file paths or replies.",
    "The newest 'System & Model Readiness' result (its verdict and time only).",
    "Which of the app's own files and program modules are present (names only), which optional packages are present and which network features are switched on.",
    "The tail of crash.log with secret-looking text removed.",
    "NOT included: conversations, memories, files, documents, screenshots, audio, API keys or passwords.")


def _hardware_and_accel(cfg: Config) -> dict:
    out: dict = {}
    try:
        from jarvis.hardware import detect_hardware
        from jarvis.inference import accel
        from jarvis.inference.gguf_engine import runtime_info
        from jarvis.models import selector
        hw = detect_hardware(cfg.data_dir if Path(cfg.data_dir).exists() else ".")
        rt = runtime_info()
        out["hardware"] = {k: v for k, v in hw.__dict__.items()}
        out["runtime"] = rt
        out["acceleration"] = selector.acceleration_state(hw, rt)
        saved = accel.load(cfg.models_dir)
        out["acceleration"]["saved_mode"] = {k: v for k, v in saved.items() if k in ("gpu_layers", "verified", "written")}
        # "verified" comes only from the setup wizard's own measurement (GPU faster than CPU with GPU activity seen) and only while the backend is still there
        out["acceleration"]["acceleration_verified"] = bool(saved.get("verified") and saved.get("gpu_layers") and out["acceleration"]["backend_available"])
        if out["acceleration"]["acceleration_verified"]:
            out["acceleration"]["message"] = "GPU acceleration was verified by the setup measurement on this PC (see setup.gpu)."
        out["tier"] = selector.hardware_tier(hw.ram_gb, hw.vram_gb, out["acceleration"]["backend_available"])
    except Exception as e:  # noqa: BLE001 - a diagnostics page must never crash
        out["hardware_error"] = f"{type(e).__name__}: {e}"
    return out


def _brains_summary(cfg: Config) -> dict:
    """Library overview from the model records and brains.json only (no hashing, no loading). File paths and model replies are deliberately left out."""
    import json
    try:
        from jarvis.models.manager import ModelStore
        store = ModelStore(cfg.models_dir)
        rep = store.installed_report()
        try:
            tests = json.loads((Path(cfg.data_dir) / "brains.json").read_text(encoding="utf-8")).get("last_tests", {})
        except (OSError, ValueError, AttributeError):
            tests = {}
        rows = []
        for m in rep["models"]:
            g = m.get("gguf") if isinstance(m.get("gguf"), dict) else {}
            t = tests.get(m.get("id")) or {}
            rows.append({"id": m.get("id"), "origin": m.get("origin", "catalog" if m.get("catalog_id") else "unknown"), "registered_in_place": isinstance(m.get("linked"), dict),
                         "architecture": g.get("architecture"), "quantization": g.get("quantization"), "size_bytes": m.get("size_bytes"),
                         "last_test": ("passed" if t.get("ok") else "failed" if t else "never tested"), "last_test_at": t.get("at")})
        return {"installed": len(rows), "active": store.active(), "damaged_entries": [d["id"] for d in rep["damaged"]], "models": rows}
    except Exception as e:  # noqa: BLE001 - a diagnostics page must never crash
        return {"error": f"could not read the brain library ({type(e).__name__})"}


def _setup_summary(cfg: Config) -> dict:
    import json
    res: dict = {}
    try:
        st = json.loads((Path(cfg.data_dir) / "setup_state.json").read_text(encoding="utf-8"))
        res["state"] = {k: st.get(k) for k in ("completed", "model_id", "mode", "gpu_verified", "real", "finished", "failed")}
    except (OSError, ValueError):
        res["state"] = "setup has not been run on this PC (or its record is unreadable)"
    try:
        rp = json.loads((Path(cfg.data_dir) / "setup_report.json").read_text(encoding="utf-8"))
        v = rp.get("steps", {}).get("verify", {})
        res["last_checks"] = v.get("checks"); res["cpu"] = v.get("cpu"); res["gpu"] = v.get("gpu"); res["gpu_note"] = v.get("gpu_note")
    except (OSError, ValueError):
        pass
    return res


# ---------------------------------------------------------------------------------------------- packaged-app files and modules
# What the built app must contain. packaging/manifest.py is the build-time source of truth and is NOT shipped in the app, so this list mirrors it;
# tests/test_p35_validation.py fails if the two ever differ. The same functions feed the diagnostics report, the preflight "App files" check
# and `jarvis-gui.exe --selftest`, so there is exactly one implementation.
PACKAGED_FILES = (
    "jarvis/telemetry/endpoint.txt", "jarvis/desktop/ocr_winrt.ps1", "config.sample.toml", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "build_info.json",
    "docs/RELEASE_NOTES.md", "docs/TROUBLESHOOTING.md", "docs/MODEL_DISTRIBUTION.md", "docs/PRIVACY_AND_NETWORK.md", "docs/LOCAL_MODEL.md",
    "docs/DESKTOP_CONTROL.md", "docs/MEMORY.md", "docs/VOICE.md", "docs/WINDOWS_DISTRIBUTION.md", "docs/IN_APP_PREFLIGHT.md", "docs/QUICK_LAUNCH.md", "docs/GPL_NOTICE.md",
    "jarvis_server/terms_en.md", "jarvis_server/learning_notice_en.md")      # the local sign-in server's text files (a source checkout keeps them in server/)
GENERATED_AT_BUILD = ("build_info.json",)          # written by the build; a source checkout legitimately has none
REQUIRED_MODULES = ("jarvis.cli", "jarvis.ui.app", "jarvis.ui.window", "jarvis.app.controller", "jarvis.missions.runner", "jarvis.desktop.cli_desktop",
                    "jarvis.preflight.runner", "jarvis.preflight.metadata", "jarvis.preflight.hf_fetch", "jarvis.preflight.history",
                    "jarvis.brains.service", "jarvis.brains.viewmodel", "jarvis.brains.compat", "jarvis.brains.archs", "jarvis.models.gguf_vocab",
                    "jarvis.voice.engines", "jarvis.voice.manager", "jarvis.voice.preview", "jarvis.voice.inputcheck", "jarvis.voice.viewmodel", "jarvis.voice.kokoro_engine", "jarvis.models.kokoro_files",
                    "jarvis.selftest_core")


def _resource_roots() -> tuple[str, Path, Path]:
    """(mode, folder holding the `jarvis/...` data files, folder holding the other files). Frozen: PyInstaller's data folder (`_internal` for
    PyInstaller 6). Source checkout: src/ and the project folder."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
        return "packaged", base, base
    root = Path(__file__).resolve().parents[3]
    return "source", root / "src", root


def packaged_files() -> dict:
    """Which files the app expects next to itself exist. A file check only (nothing is opened). Paths in the result are relative, never absolute."""
    mode, pkg_base, other_base = _resource_roots()
    rows = []
    for rel in PACKAGED_FILES:
        base = pkg_base if rel.startswith("jarvis/") else other_base
        if rel.startswith("jarvis_server/") and mode == "source":
            base = other_base / "server"
        required = mode == "packaged" or rel not in GENERATED_AT_BUILD
        try:
            present = (base / rel).is_file()
        except OSError:
            present = False
        rows.append({"file": rel, "present": present, "required": required})
    missing = [r["file"] for r in rows if r["required"] and not r["present"]]
    return {"mode": mode, "checked": len(rows), "missing_required": missing, "files": rows}


def required_modules(import_them: bool = False) -> dict:
    """Are the modules the frozen app loads lazily present? import_them=False only looks for them (cheap, used by the report); True really imports
    each one (a measurement; used by the self-test). A module whose import fails is reported with a short, path-free reason."""
    rows = []
    for name in REQUIRED_MODULES:
        if import_them:
            problem = status.import_problem(name)
            rows.append({"module": name, "present": problem is None, "imported": problem is None, "problem": problem})
        else:
            found = status.has_module(name)
            rows.append({"module": name, "present": found, "imported": None, "problem": None if found else "not found"})
    return {"imported": import_them, "missing": [r["module"] for r in rows if not r["present"]], "modules": rows}


def collect(cfg: Config, build_info: dict | None = None) -> dict:
    ms = status.model_status(cfg)
    try:
        free = round(shutil.disk_usage(cfg.data_dir if Path(cfg.data_dir).exists() else Path.home()).free / 1e9, 1)
    except OSError:
        free = None
    info = build_info if build_info is not None else read_build_info()
    return {
        "app": V.APP_NAME, "version": V.__version__, "channel": V.RELEASE_CHANNEL,
        "build": info,
        "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False)),
        "platform": platform.platform(), "machine": platform.machine(),
        "windows_supported_minimum": V.MIN_WINDOWS,
        "data_dir": scrub_paths(str(cfg.data_dir)), "profile": cfg.profile, "free_disk_gb": free,
        "model": {"state": ms.state, "summary": ms.summary, "action": ms.action, "id": ms.model_id},
        "packaged_app": {"files": packaged_files(), "modules": required_modules(False)},
        "last_readiness_preflight": _last_preflight(cfg),
        "optional_dependencies": status.optional_dependencies(),
        "runtime_checks": status.runtime_checks(),
        "legend": {"packaged_app": "files: existence only. modules: found on disk, NOT imported. A missing entry means this copy of Jarvis is incomplete (reinstall it).",
                   "optional_dependencies": "true = the package was found on disk. It was NOT imported, so it can still fail to load.",
                   "runtime_checks": "measured: an import was attempted. loads=null means the package is absent and nothing was measured.",
                   "model.state": "a quick file check only; the model has not been loaded and no inference was run",
                   "features": "evidence says what kind of test exists; none of these rows is a measurement made on this PC"},
        "features": [{k: r[k] for k in ("id", "state", "evidence")} for r in status.feature_rows(cfg)],
        "network_features": _network(cfg),
        **_hardware_and_accel(cfg),
        "setup": _setup_summary(cfg),
        "brains": _brains_summary(cfg),
        "fields_included": list(INCLUDED_FIELDS),
        "crash_log_tail": log_tail(cfg),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "Local report. Nothing was sent anywhere. It contains no conversations, memories or file contents.",
    }


def _last_preflight(cfg: Config) -> dict:
    """The newest saved 'System & Model Readiness' result, summarised (already scrubbed when it was saved). {} if there is none."""
    try:
        from jarvis.preflight import history
        runs = history.list_runs(cfg, limit=1)
    except Exception:  # noqa: BLE001 - a diagnostics page must never crash
        return {}
    if not runs:
        return {}
    r = runs[0]
    return {k: r.get(k) for k in ("run_id", "readable", "started_at_utc", "app_version", "include_online", "verdict", "checks")}


def _network(cfg: Config) -> dict:
    try:
        from jarvis.net.policy import FEATURES, NetworkPolicy
        pol = NetworkPolicy.load(cfg.settings_path)
        return {f: bool(pol.is_enabled(f)) for f in FEATURES}
    except Exception:  # noqa: BLE001
        return {}


def read_build_info() -> dict:
    """build_info.json is written by scripts/build_windows.ps1 next to the frozen app; a source checkout has none, and the report says so."""
    import json
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).parent / "build_info.json")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "build_info.json")
    cands.append(Path(__file__).resolve().parents[3] / "build_info.json")
    for c in cands:
        try:
            return json.loads(c.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {"source": "source checkout or unrecorded build (no build_info.json)"}


def render_text(d: dict) -> str:
    import json
    return scrub_paths(json.dumps(d, indent=2, default=str))


def export_report(cfg: Config, dest: str | Path) -> Path:
    """Write the redacted report to `dest` (a file path the person chose). Returns the path. Raises OSError if it cannot be written."""
    p = Path(dest)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_text(collect(cfg)), encoding="utf-8")
    return p
