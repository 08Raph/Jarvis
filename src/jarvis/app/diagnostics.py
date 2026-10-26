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
    "Which optional packages are present and which network features are switched on.",
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
        "optional_dependencies": status.optional_dependencies(),
        "runtime_checks": status.runtime_checks(),
        "legend": {"optional_dependencies": "true = the package was found on disk. It was NOT imported, so it can still fail to load.",
                   "runtime_checks": "measured: an import was attempted. loads=null means the package is absent and nothing was measured.",
                   "model.state": "a quick file check only; the model has not been loaded and no inference was run",
                   "features": "evidence says what kind of test exists; none of these rows is a measurement made on this PC"},
        "features": [{k: r[k] for k in ("id", "state", "evidence")} for r in status.feature_rows(cfg)],
        "network_features": _network(cfg),
        **_hardware_and_accel(cfg),
        "setup": _setup_summary(cfg),
        "fields_included": list(INCLUDED_FIELDS),
        "crash_log_tail": log_tail(cfg),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "Local report. Nothing was sent anywhere. It contains no conversations, memories or file contents.",
    }


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
