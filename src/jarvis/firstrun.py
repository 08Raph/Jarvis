"""First-run setup. Local only: creates folders, writes privacy-safe defaults, inspects hardware.
Makes no network connections. If the installer shipped a model and this profile has none, it is installed from that local file (jarvis.models.bundled)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jarvis import __version__
from jarvis.config import Config
from jarvis.hardware import detect_hardware, recommend
from jarvis.models.manager import ModelStore
from jarvis.net.policy import FEATURES, NetworkPolicy


def first_run(cfg: Config) -> dict:
    for d in (cfg.data_dir, cfg.models_dir, cfg.backups_dir, cfg.data_dir / "logs"):
        d.mkdir(parents=True, exist_ok=True)
    policy = NetworkPolicy.load(cfg.settings_path)
    if not cfg.settings_path.exists():
        policy.save(cfg.settings_path)  # all online features off
    from jarvis.models import bundled
    seed = bundled.seed(cfg)            # no-op unless a bundled model is present and this profile has none; local file copy only
    store = ModelStore(cfg.models_dir, policy)
    installed = [m["id"] for m in store.installed()]
    marker = cfg.data_dir / "first_run.json"
    created = (json.loads(marker.read_text(encoding="utf-8"))["created_at"]
               if marker.exists() else datetime.now(timezone.utc).isoformat())
    report = {
        "app_version": __version__, "created_at": created, "data_dir": str(cfg.data_dir.resolve()),
        "network": {f: policy.is_enabled(f) for f in FEATURES},
        "bundled_model": seed, "installed_models": installed, "active_model": store.active(),
        "hardware": recommend(detect_hardware(cfg.data_dir)),
        "next_steps": ([] if installed else
                       ["No chat model is installed, so the assistant cannot answer yet. Run `jarvis setup` (guided), or "
                        "`jarvis models fetch qwen3-1.7b-q4_k_m` to download the free model, then `jarvis selftest`."]),
    }
    marker.write_text(json.dumps({"created_at": created}), encoding="utf-8")
    return report
