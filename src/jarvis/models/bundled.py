"""The model that ships INSIDE the installer, and how it is installed on this PC. No network is involved anywhere in this module.

WHAT SHIPS: one file, the Qwen3-1.7B Q4_K_M GGUF, at ``<app folder>/bundled_models/qwen3-1.7b-q4_k_m/model.gguf``. It is a SEED: on first run it is copied into
the user's own model folder (``%LOCALAPPDATA%/ProjectJarvis/models``), so upgrades and uninstalls of the program never touch the installed model.

WHAT IS CHECKED before anything is installed: the file must have exactly the pinned size and SHA-256 below, a valid GGUF header and the ``qwen3`` architecture.
The pin is compared in this code, never with a value read from a file next to the model, so a swapped file cannot vouch for itself.

HONESTY: the SHA-256 is the value OBSERVED on a genuine download of the publisher's file (see docs/VERIFICATION_LEDGER.md). The publisher did not confirm it
independently, so the installed record says ``verified_origin: false`` and names this as its origin.

RULES: the seed is installed only when this profile has no installed model and has never been seeded. If you later remove the model it is NOT put back
(the marker ``bundled_seed.json`` records that this was done once). It is activated only if no brain is active yet."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

MODEL_ID = "qwen3-1.7b-q4_k_m"
BUNDLE_DIRNAME = "bundled_models"
FILENAME = "model.gguf"
PIN_BYTES = 1282439264
PIN_SHA256 = "d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5"
LICENSE_SPDX = "Apache-2.0"
LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0"
SOURCE_REPO = "ggml-org/Qwen3-1.7B-GGUF"
MARKER = "bundled_seed.json"
ENV_OVERRIDE = "JARVIS_BUNDLED_MODELS_DIR"      # lets tests (and nobody else, in practice) point at a folder; the pin is still enforced


def bundle_root() -> Path | None:
    """The folder that holds the bundled models, or None when this copy of Jarvis has none (a source checkout, or a build made without them)."""
    env = os.environ.get(ENV_OVERRIDE)
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        for cand in (base / BUNDLE_DIRNAME, base.parent / BUNDLE_DIRNAME):
            if cand.is_dir():
                return cand
    return None


def bundled_file(root: Path | None = None) -> Path | None:
    root = root if root is not None else bundle_root()
    if root is None:
        return None
    f = root / MODEL_ID / FILENAME
    return f if f.is_file() else None


def _marker(cfg) -> Path:
    return Path(cfg.data_dir) / MARKER


def pending(cfg, root: Path | None = None) -> bool:
    """True when a bundled model is present and would be installed by ``seed``. Cheap: reads no model bytes."""
    if bundled_file(root) is None or _marker(cfg).exists():
        return False
    from jarvis.models.manager import ModelStore
    try:
        return not ModelStore(cfg.models_dir).installed()
    except OSError:
        return False


def seed(cfg, root: Path | None = None) -> dict:
    """Install the bundled model if it is pending. Returns a small report; never raises for the normal 'nothing to do' cases.
    A damaged or wrong bundled file is reported (``state: refused``) and nothing is installed."""
    f = bundled_file(root)
    if f is None:
        return {"state": "absent", "detail": "this copy of Jarvis has no bundled model"}
    if _marker(cfg).exists():
        return {"state": "already-done", "detail": "the bundled model was already installed once on this profile; it is not put back"}
    from jarvis.models.errors import IntegrityError
    from jarvis.models.gguf import GGUFError
    from jarvis.models.manager import ModelStore
    store = ModelStore(cfg.models_dir)
    existing = [m["id"] for m in store.installed()]
    if existing:
        return {"state": "skipped", "detail": "this profile already has an installed model: " + ", ".join(existing)}
    if f.stat().st_size != PIN_BYTES:
        return {"state": "refused", "detail": f"the bundled file is {f.stat().st_size} bytes, not the expected {PIN_BYTES}; nothing was installed"}
    lic = (f"This model is distributed by its publisher under {LICENSE_SPDX}. Full text: {LICENSE_URL}\n"
           f"Model page: https://huggingface.co/{SOURCE_REPO}\nIt was shipped inside the Project JARVIS installer.")
    try:
        meta = store.install_gguf(
            f, MODEL_ID, LICENSE_SPDX, lic, source_url=f"bundled-with-installer:{SOURCE_REPO}", expected_sha256=PIN_SHA256,
            expected_size_bytes=PIN_BYTES, expected_architecture="qwen3",
            extra_meta={"catalog_id": MODEL_ID, "no_think_suffix": True, "context_tokens": 4096, "origin": "bundled-with-installer",
                        "source_repo": SOURCE_REPO,
                        "verified_origin": False,
                        "checksum_reference": {"source": "SHA-256 observed on a genuine download of the publisher's file, pinned in the program",
                                               "independent_of_host": False, "publisher_confirmed": False}})
    except (IntegrityError, GGUFError, OSError) as e:      # DiskSpaceError is an IntegrityError; a damaged header is a GGUFError
        return {"state": "refused", "detail": str(e)[:300]}
    activated = False
    if store.active() is None:
        try:
            store.set_active(MODEL_ID)
            activated = True
        except IntegrityError:
            activated = False
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    _marker(cfg).write_text(json.dumps({"seeded_at": datetime.now(timezone.utc).isoformat(), "model_id": MODEL_ID, "sha256": PIN_SHA256,
                                        "activated": activated}), encoding="utf-8")
    return {"state": "installed", "model_id": MODEL_ID, "activated": activated, "size_bytes": meta.get("size_bytes"),
            "detail": "installed from the file shipped with the installer; no network was used"}
