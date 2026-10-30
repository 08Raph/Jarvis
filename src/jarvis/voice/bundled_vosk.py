"""The small English speech-recognition model (Vosk ``vosk-model-small-en-us-0.15``) that ships INSIDE the installer, and how it is set up on this PC.
No network is involved anywhere in this module.

WHAT SHIPS: one folder, ``<app folder>/bundled_models/vosk-model-small-en-us-0.15/`` (about 40 MB). On first start it is COPIED into the user's data folder
(``<data>/voice_models/``) and, only if no speech model is set yet, ``vosk_model`` in this profile's voice.json is pointed at that copy. Nothing else in the
voice settings is touched: the microphone, mute, wake phrase and the "never chosen" state of listening stay exactly as they were.

WHAT IS CHECKED: the whole folder is compared with ``PIN_TREE_SHA256`` (a digest over every file's relative path, size and SHA-256) and ``PIN_FILE_COUNT``, both
compiled into this program, never read from a file next to the model. While the pin is empty the seed refuses, so an unpinned folder can never be installed.

HONESTY: the pin is the value OBSERVED on a genuine download from the model's publisher (alphacephei.com, see docs/VERIFICATION_LEDGER.md). The publisher did
not sign it, so this is a fixed observed value, not publisher-confirmed provenance.

RULES: seeded once per profile (marker ``bundled_vosk_seed.json``); never put back after you change or clear the setting; never replaces a speech model you set."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

MODEL_NAME = "vosk-model-small-en-us-0.15"
BUNDLE_DIRNAME = "bundled_models"
INSTALL_DIRNAME = "voice_models"
MARKER = "bundled_vosk_seed.json"
SOURCE_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
MODELS_PAGE = "https://alphacephei.com/vosk/models"
LICENSE_NOTE = ("Published by Alpha Cephei (alphacephei.com/vosk/models). The licence shown on that page for this model is recorded in "
                "docs/VERIFICATION_LEDGER.md; the full text is in the model folder if the publisher included one.")
# --- pins (filled in from the first genuine download; see scripts/fetch_vosk_model.py --probe) ---
ZIP_PIN_BYTES = 0
ZIP_PIN_SHA256 = ""
PIN_FILE_COUNT = 0
PIN_TREE_SHA256 = ""
ENV_OVERRIDE = "JARVIS_BUNDLED_MODELS_DIR"      # shared with jarvis.models.bundled (tests only); the pin is still enforced


def pinned() -> bool:
    return bool(PIN_TREE_SHA256) and PIN_FILE_COUNT > 0


def tree_digest(folder: Path) -> tuple[str, int]:
    """(digest, file_count) over every file under ``folder``: sorted ``relative/posix/path size sha256`` lines. Symbolic links are not followed or accepted."""
    folder = Path(folder)
    lines: list[str] = []
    for p in sorted(folder.rglob("*"), key=lambda q: q.relative_to(folder).as_posix()):
        if p.is_symlink():
            return "symlink-found:" + p.relative_to(folder).as_posix(), -1
        if p.is_file():
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            lines.append(f"{p.relative_to(folder).as_posix()} {p.stat().st_size} {h.hexdigest()}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(), len(lines)


def folder_matches_pin(folder: Path) -> bool:
    if not pinned() or not Path(folder).is_dir():
        return False
    digest, count = tree_digest(folder)
    return digest == PIN_TREE_SHA256 and count == PIN_FILE_COUNT


def bundle_root() -> Path | None:
    from jarvis.models import bundled
    return bundled.bundle_root()


def bundled_dir(root: Path | None = None) -> Path | None:
    root = root if root is not None else bundle_root()
    if root is None:
        return None
    d = Path(root) / MODEL_NAME
    return d if d.is_dir() else None


def _marker(cfg) -> Path:
    return Path(cfg.profile_dir) / MARKER


def install_dir(cfg) -> Path:
    return Path(cfg.data_dir) / INSTALL_DIRNAME / MODEL_NAME


def _voice_json(cfg) -> Path:
    return Path(cfg.profile_dir) / "voice.json"


def pending(cfg, root: Path | None = None) -> bool:
    """True when a bundled speech model is present and would be set up by ``seed``. Cheap: reads no model bytes."""
    return bundled_dir(root) is not None and not _marker(cfg).exists()


def seed(cfg, root: Path | None = None) -> dict:
    """Set up the bundled speech model if it is pending. Returns a small report; never raises for the normal 'nothing to do' cases."""
    src = bundled_dir(root)
    if src is None:
        return {"state": "absent", "detail": "this copy of Jarvis has no bundled speech model"}
    if _marker(cfg).exists():
        return {"state": "already-done", "detail": "the bundled speech model was already set up once on this profile; it is not put back"}
    if not pinned():
        return {"state": "refused", "detail": "the bundled speech model has no pinned checksum in this build; nothing was installed"}
    if not folder_matches_pin(src):
        return {"state": "refused", "detail": "the bundled speech model does not match the checksum pinned in the program; nothing was installed"}
    from jarvis.voice.inputcheck import looks_like_vosk_model
    from jarvis.voice.settings import VoiceSettings
    vpath = _voice_json(cfg)
    if vpath.exists():
        try:
            json.loads(vpath.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            return {"state": "refused", "detail": "voice.json could not be read, so it was left untouched and nothing was changed"}
    dst = install_dir(cfg)
    try:
        if not folder_matches_pin(dst):
            tmp = dst.with_name(dst.name + ".tmp-" + uuid.uuid4().hex[:8])
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copytree(src, tmp)
                if not folder_matches_pin(tmp):
                    raise OSError("the copy does not match the pinned checksum")
                if dst.exists():
                    shutil.rmtree(dst)
                os.replace(tmp, dst)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        vs = VoiceSettings.load(vpath)
        applied = False
        if not (vs.vosk_model and looks_like_vosk_model(vs.vosk_model)):
            vs.vosk_model = str(dst)
            vs.save(vpath)
            applied = True
        Path(cfg.profile_dir).mkdir(parents=True, exist_ok=True)
        _marker(cfg).write_text(json.dumps({"seeded_at": datetime.now(timezone.utc).isoformat(), "model": MODEL_NAME,
                                            "tree_sha256": PIN_TREE_SHA256, "setting_applied": applied}), encoding="utf-8")
    except OSError as e:
        return {"state": "refused", "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"state": "installed", "model": MODEL_NAME, "setting_applied": applied, "path": str(dst),
            "detail": "set up from the folder shipped with the installer; no network was used"}
