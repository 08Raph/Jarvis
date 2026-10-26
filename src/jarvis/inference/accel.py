"""Remembered acceleration choice (Prompt 20). One small JSON file next to the installed models, written by the setup wizard after it has MEASURED
CPU against GPU on this PC. The engine reads it only when neither an explicit argument nor JARVIS_GPU_LAYERS says otherwise, so every part of the
app (chat, self-test, missions) follows the same rule. Missing, damaged or unreadable file = CPU (0 layers); it is never an error."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

FILE = "acceleration.json"


def path_for(models_dir: str | Path) -> Path:
    return Path(models_dir) / FILE


def load(models_dir: str | Path) -> dict:
    try:
        d = json.loads(path_for(models_dir).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("gpu_layers"), int) and not isinstance(d.get("gpu_layers"), bool):
            return d
    except (OSError, ValueError):
        pass
    return {"gpu_layers": 0}


def gpu_layers(models_dir: str | Path) -> int:
    return int(load(models_dir)["gpu_layers"])


def save(models_dir: str | Path, gpu_layers_: int, *, verified: bool, evidence: dict | None = None) -> None:
    d = {"gpu_layers": int(gpu_layers_), "verified": bool(verified), "evidence": evidence or {},
         "written": datetime.now(timezone.utc).isoformat(),
         "note": "Written by the setup wizard after measuring CPU against GPU on this PC. Delete this file to return to CPU."}
    p = path_for(models_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(p)
