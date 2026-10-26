"""Shared helpers for the Prompt 11 tests (not a test module). Same labels as p10_common: REAL files/threads/sockets (loopback only), FAKE MODEL scripted."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "scripts"))

from p10_common import AppCase, FakeLlama, fake_engine_factory, make_gguf  # noqa: E402,F401
