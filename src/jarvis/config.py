"""Configuration loader (TOML, stdlib tomllib)."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
DEFAULT_PROFILE = "default"


def valid_profile(name: str) -> str:
    """Profile names become folder names, so they are restricted (no paths, no dots, no reserved Windows names)."""
    n = (name or "").strip().lower()
    if not PROFILE_RE.fullmatch(n) or n in {"con", "prn", "aux", "nul", "profiles", "models", "backups", "logs"} \
            or re.fullmatch(r"(com|lpt)[1-9]", n):
        raise ValueError("profile names use 1-32 letters, digits, '-' or '_' (and cannot be a reserved word)")
    return n


@dataclass
class Config:
    data_dir: Path = Path("jarvis_data")
    model_dir: Path = Path("jarvis_data/model")
    max_new_tokens: int = 128
    temperature: float = 0.8
    top_k: int = 40
    app_allowlist: dict[str, str] = field(default_factory=lambda: {"notepad": "notepad.exe", "calculator": "calc.exe"})
    profile: str = DEFAULT_PROFILE      # application profile: separates personal memory/preferences on one Windows account

    @property
    def profile_dir(self) -> Path:
        """Where THIS profile's personal data lives. The default profile keeps the original layout (data_dir itself), so
        existing installs are unchanged; other profiles live in data_dir/profiles/<name>/. Models are shared (not personal)."""
        if self.profile == DEFAULT_PROFILE:
            return self.data_dir
        return self.data_dir / "profiles" / valid_profile(self.profile)

    @property
    def memory_db(self) -> Path:
        return self.profile_dir / "memory.sqlite"

    @property
    def provenance_db(self) -> Path:
        return self.data_dir / "provenance.sqlite"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def backups_dir(self) -> Path:
        return self.profile_dir / "backups"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    if path is None or not Path(path).exists():
        return cfg
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    paths = raw.get("paths", {})
    gen = raw.get("generation", {})
    if "data_dir" in paths:
        cfg.data_dir = Path(paths["data_dir"])
        cfg.model_dir = cfg.data_dir / "model"
    if "model_dir" in paths:
        cfg.model_dir = Path(paths["model_dir"])
    cfg.max_new_tokens = int(gen.get("max_new_tokens", cfg.max_new_tokens))
    cfg.temperature = float(gen.get("temperature", cfg.temperature))
    cfg.top_k = int(gen.get("top_k", cfg.top_k))
    if "apps" in raw:
        cfg.app_allowlist = {str(k).lower(): str(v) for k, v in raw["apps"].items()}
    if not (0.0 < cfg.temperature <= 2.0) or cfg.max_new_tokens < 1 or cfg.top_k < 0:
        raise ValueError("invalid generation settings")
    return cfg
