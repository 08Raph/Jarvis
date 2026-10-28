"""Shared helpers for the Import Brain tests (not a test module).

LABELS: SYNTHETIC GGUF = a tiny file with a real GGUF header layout and NO weights (build_gguf). FAKE ENGINE / FAKE LLAMA = scripted objects that
count loads and closes; they exercise Jarvis's own switching, ownership and recovery logic and are NOT inference. SIMULATED HARDWARE = a Hardware
record with chosen numbers; it proves the classification logic, not any real PC. No real model, runtime, GPU or Windows machine is involved."""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.config import Config                       # noqa: E402
from jarvis.hardware import Hardware                   # noqa: E402
from jarvis.models.manager import ModelStore           # noqa: E402
from jarvis.net.policy import NetworkPolicy            # noqa: E402


def _s(x: str) -> bytes:
    b = x.encode()
    return struct.pack("<Q", len(b)) + b


def build_gguf(path, arch="qwen3", name="Synthetic 1B", layers=24, embd=2048, heads=16, kv_heads=8, key_len=128, ctx=8192, file_type=15,
               chat_template="{{ messages }}", pad=2000, version=3, experts=None, size=52_000_000, tensors=1,
               general_type=None, split_count=None, tok="gpt2", pre="qwen2", kv_lora_rank=None):
    """SYNTHETIC GGUF: real header layout, no weights. Sizes/shape fields are whatever the test says."""
    kv: list[tuple[str, int, object]] = [("general.architecture", 8, arch), ("general.name", 8, name),
                                         (f"{arch}.block_count", 4, layers), (f"{arch}.embedding_length", 4, embd),
                                         (f"{arch}.attention.head_count", 4, heads), (f"{arch}.attention.head_count_kv", 4, kv_heads),
                                         (f"{arch}.attention.key_length", 4, key_len), (f"{arch}.context_length", 4, ctx)]
    if file_type is not None:
        kv.append(("general.file_type", 4, file_type))
    if experts:
        kv.append((f"{arch}.expert_count", 4, experts))
    for k, v in (("general.type", general_type), ("tokenizer.ggml.model", tok), ("tokenizer.ggml.pre", pre)):
        if v:
            kv.append((k, 8, v))
    if split_count:
        kv.append(("split.count", 2, split_count))     # uint16
    if kv_lora_rank:
        kv.append((f"{arch}.attention.kv_lora_rank", 4, kv_lora_rank))
    if chat_template:
        kv.append(("tokenizer.chat_template", 8, chat_template))
    body = b""
    for k, t, v in kv:
        body += _s(k) + struct.pack("<I", t) + (_s(v) if t == 8 else struct.pack("<H" if t == 2 else "<I", v))
    Path(path).write_bytes(b"GGUF" + struct.pack("<I", version) + struct.pack("<Q", tensors) + struct.pack("<Q", len(kv)) + body + b"\0" * pad)
    if size:
        with open(path, "r+b") as f:       # sparse padding: a believable file size without writing megabytes
            f.truncate(max(size, Path(path).stat().st_size))
    return Path(path)


def hw(ram=32.0, avail=24.0, disk=200.0, gpu=None, vram=None, avx2=True):
    """SIMULATED hardware."""
    return Hardware(os="Simulated", cpu_threads=8, ram_gb=ram, free_disk_gb=disk, gpu_name=gpu, vram_gb=vram, avx2=avx2, ram_available_gb=avail)


class FakeEngine:
    """FAKE ENGINE: not inference. Records loads/closes so ownership and recovery can be asserted."""
    loaded = 0          # engines alive right now
    peak = 0
    created: list = []
    fail_for: set = set()
    empty_for: set = set()
    reply = "Two plus two equals four."

    def __init__(self, model_dir):
        d = Path(model_dir)
        self.model_id = d.name
        if self.model_id in FakeEngine.fail_for:
            raise RuntimeError("simulated load failure")
        self.llm = object()
        self.closed = False
        self.load_seconds = 0.01
        self.diag = {"calls": 0, "completion_tokens": 0}
        FakeEngine.loaded += 1
        FakeEngine.peak = max(FakeEngine.peak, FakeEngine.loaded)
        FakeEngine.created.append(self)

    def chat(self, messages, max_tokens=512, temperature=0.7):
        if self.closed:
            raise RuntimeError("closed")
        if self.model_id in FakeEngine.empty_for:
            return "   "
        self.diag["calls"] += 1
        self.diag["completion_tokens"] += 6
        return FakeEngine.reply

    def stream(self, messages, max_tokens=512, temperature=0.7):
        yield self.chat(messages)

    def close(self):
        if not self.closed:
            self.closed = True
            FakeEngine.loaded -= 1

    @classmethod
    def reset(cls):
        cls.loaded, cls.peak, cls.created, cls.fail_for, cls.empty_for = 0, 0, [], set(), set()


class FakeHost:
    """FAKE host: stands in for the chat assistant's engine slot."""
    def __init__(self):
        self.engine = None
        self.model_id = None
        self.events = []

    def loaded_engine(self):
        return self.engine

    def release_engine(self):
        self.events.append("release")
        if self.engine is not None:
            self.engine.close()
        self.engine = None

    def install_engine(self, engine, model_id):
        self.events.append(f"install:{model_id}")
        self.engine, self.model_id = engine, model_id


class BrainCase(unittest.TestCase):
    def setUp(self):
        from jarvis.brains.service import BrainManager
        FakeEngine.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.policy = NetworkPolicy()
        self.store = ModelStore(self.cfg.models_dir, self.policy)
        self.host = FakeHost()
        self.files = self.d / "files"
        self.files.mkdir()
        self.mgr = self.make(hw())
        self.BrainManager = BrainManager

    def make(self, hardware, runtime=None, factory=FakeEngine, setup=None):
        from jarvis.brains.service import BrainManager
        return BrainManager(self.cfg, self.store, self.policy, host=self.host, engine_factory=factory, hardware=hardware,
                            runtime=runtime or {"available": True, "version": "test", "gpu_offload_supported": False}, setup_engine=setup)

    def gguf(self, fname="a.gguf", **kw):
        return build_gguf(self.files / fname, **kw)

    def imp(self, fname="a.gguf", **kw):
        r = self.mgr.import_file(self.gguf(fname, **kw))
        self.assertTrue(r["ok"], r)
        return r["model_id"]
