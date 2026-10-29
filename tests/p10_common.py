"""Shared helpers for the Prompt 10 tests (not a test module).

LABELS: REAL = real files, SQLite, executor/policy/confirmation, mission store, lock files, packaging manifest and scripts.
FAKE MODEL = FakeLlama (scripted replies, no real inference). NO Qt, NO Windows, NO real Qwen weights, NO PyInstaller/Inno Setup were available."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "scripts"))

from jarvis.config import Config            # noqa: E402
from jarvis.inference import gguf_engine as ge  # noqa: E402
from jarvis.models.manager import ModelStore  # noqa: E402


def make_gguf(path, pad=1000, version=3):
    """Synthetic GGUF header (same layout the Prompt 5 tests use)."""
    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kvs = {"general.architecture": "qwen3", "general.name": "Fake 1.7B", "general.license": "apache-2.0"}
    body = b"".join(s(k) + struct.pack("<I", 8) + s(v) for k, v in kvs.items())
    body += s("tokenizer.ggml.tokens") + struct.pack("<I", 9) + struct.pack("<I", 8) + struct.pack("<Q", 2) + s("a") + s("b")
    Path(path).write_bytes(b"GGUF" + struct.pack("<I", version) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs) + 1) + body + b"\0" * pad)
    return path


class FakeLlama:
    """FAKE MODEL: streams a fixed reply. Not real inference."""
    reply = "Hello from the scripted fake model."
    instances = []

    def __init__(self, model_path, n_ctx, n_threads, verbose):
        self.calls = []
        FakeLlama.instances.append(self)

    def tokenize(self, b, add_bos=False):
        return list(range(len(b.split())))

    def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
        self.calls.append(messages)
        t = FakeLlama.reply
        if not stream:
            return {"choices": [{"message": {"content": t}}]}
        return iter({"choices": [{"delta": {"content": t[i:i + 4]}}]} for i in range(0, len(t), 4))


def fake_engine_factory(p):
    return ge.GGUFEngine(p, llama_factory=FakeLlama)


class AppCase(unittest.TestCase):
    # Seconds the safety net below waits for a controller's background tasks before it fails the test NAMING the task.
    CLEANUP_WAIT = 30.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()
        FakeLlama.instances.clear()
        FakeLlama.reply = "Hello from the scripted fake model."
        self.addCleanup(self.tmp.cleanup)
        # Safety net for the temp-folder cleanup on Windows. AppController.shutdown(timeout) deliberately returns WITHOUT closing memory.sqlite when a task is still
        # running after `timeout` (it never closes a database a live worker may be using). Most tests call shutdown(1.0); on a slow runner a task can outlive that, the
        # database stays open, and tmp.cleanup then fails with PermissionError [WinError 32] on memory.sqlite although the test body passed (CI run 40795644979, whose
        # unit-test step took 661 s against 268-359 s in every other run). Cleanups run last-in-first-out, so this one, registered after tmp.cleanup, runs BEFORE the
        # folder is deleted: it waits (bounded, by condition) for each controller's tasks and then closes the database, or fails naming the task that never ended.
        # It never calls shutdown() itself and it cannot hide a failure: assertions inside the test body are unaffected.
        self._controllers = []
        from jarvis.app.controller import AppController
        original = AppController.__init__

        def tracking(ctl, *a, **k):
            original(ctl, *a, **k)
            self._controllers.append(ctl)
        patcher = mock.patch.object(AppController, "__init__", tracking)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._settle_controllers)

    def _settle_controllers(self) -> None:
        stuck = []
        for ctl in self._controllers:
            left = ctl.tasks.shutdown(self.CLEANUP_WAIT)
            if left:
                stuck.append(left)
            elif ctl.assistant is not None:
                ctl.assistant.close()                      # idempotent: an earlier shutdown() may already have closed it
        if stuck:
            raise AssertionError(f"background task(s) still running {self.CLEANUP_WAIT:.0f} s after the test finished: {stuck}")

    def install_model(self, mid="fake"):
        store = ModelStore(self.cfg.models_dir)
        store.install_gguf(make_gguf(self.d / "m.gguf"), mid, "Apache-2.0", "t", extra_meta=None)
        store.set_active(mid)
        return store
