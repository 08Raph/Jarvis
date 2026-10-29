"""Local-model behaviour through the REAL controller, assistant, GGUFEngine and ModelStore, with a SCRIPTED stand-in for the llama.cpp runtime.

LABELS
  SCRIPTED RUNTIME (every test except RealRuntime): the llama.cpp object is a scripted fake. These tests check OUR logic: error handling, what is shown, what is remembered,
    recovery. They say nothing about whether a real model loads or answers well.
  REAL RUNTIME (RealRuntime): loads a genuine GGUF through llama-cpp-python and generates. It is SKIPPED unless JARVIS_REAL_MODEL_TEST=1, JARVIS_REAL_MODEL_PATH=<a .gguf> and
    llama_cpp is importable, and it was NOT run in the authoring sandbox (no runtime and no model reachable there). A skip is not a pass.
"""
import importlib.util
import json
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from p10_common import AppCase, FakeLlama, engine_for_gguf_file, make_gguf  # noqa: E402
from test_p10_controller import settle  # noqa: E402
from jarvis.app import status  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.inference import gguf_engine as ge  # noqa: E402

SECRET = "PROMPT-SECRET-7731"


class Scripted(FakeLlama):
    """mode: ok | empty | crash_mid_stream | slow. `built` counts constructions; `seen` keeps the messages of each call."""
    mode = "ok"
    built = 0
    seen: list = []
    started = threading.Event()

    def __init__(self, *a, **k):
        Scripted.built += 1
        super().__init__(*a, **k)

    def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
        Scripted.seen.append([dict(m) for m in messages])
        m = Scripted.mode
        if m == "empty":
            return iter([{"choices": [{"delta": {"content": ""}}]}]) if stream else {"choices": [{"message": {"content": ""}}]}
        if m == "crash_mid_stream":
            def g():
                yield {"choices": [{"delta": {"content": "a partial answer that is long enough to be shown"}}]}
                raise RuntimeError(f"native failure while decoding {messages[-1]['content']}")
            return g()
        if m == "slow":
            def g():
                Scripted.started.set()
                for _ in range(400):
                    time.sleep(0.01)
                    yield {"choices": [{"delta": {"content": "word "}}]}
            return g()
        return super().create_chat_completion(messages, max_tokens, temperature, stream)


def reset():
    Scripted.mode, Scripted.built, Scripted.seen = "ok", 0, []
    Scripted.started.clear()


class ThroughTheController(AppCase):
    def setUp(self):
        super().setUp()
        reset()
        self.install_model()
        self.c = AppController(self.cfg, simulate=True, engine_factory=lambda p: ge.GGUFEngine(p, llama_factory=Scripted))
        self.addCleanup(self.c.shutdown, 2.0)

    def ask(self, text, mode="ok"):
        Scripted.mode = mode
        self.assertTrue(self.c.send(text))
        settle(self.c, 10)

    def kinds(self):
        return [(e.kind, e.text) for e in self.c.transcript.entries]

    def test_a_good_reply_is_shown_and_remembered(self):
        self.ask("hello")
        self.assertEqual(self.kinds()[-1], ("assistant", "Hello from the scripted fake model."))
        self.assertEqual([m["role"] for m in self.c.assistant.history], ["user", "assistant"])

    def test_an_empty_reply_shows_an_honest_error_with_no_blank_bubble_and_the_next_reply_works(self):
        self.ask("hello", "empty")
        self.assertEqual(self.kinds(), [("user", "hello"), ("error", "The model returned no text.")])
        self.assertFalse(self.c.busy)
        self.ask("again")                                                   # recovery
        self.assertEqual(self.kinds()[-1], ("assistant", "Hello from the scripted fake model."))

    def test_a_runtime_crash_mid_reply_is_reported_without_the_prompt_and_the_next_reply_works(self):
        self.ask(f"please repeat {SECRET}", "crash_mid_stream")
        self.assertFalse(self.c.busy)
        last = self.kinds()[-1]
        self.assertEqual(last[0], "error")
        self.assertIn("failed while generating", last[1])
        self.assertNotIn(SECRET, " ".join(t for _, t in self.kinds() if _ == "error"))
        self.ask("are you back?")
        self.assertEqual(self.kinds()[-1], ("assistant", "Hello from the scripted fake model."))

    def test_stop_ends_the_reply_says_so_and_the_next_reply_works(self):
        Scripted.mode = "slow"
        self.assertTrue(self.c.send("write a very long story"))
        self.assertTrue(Scripted.started.wait(30), "generation never started")
        t0 = time.monotonic()
        self.assertTrue(self.c.stop_generation())
        settle(self.c, 10)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertFalse(self.c.busy)
        self.assertTrue(any(k == "system" and "Stopped" in t for k, t in self.kinds()))
        self.ask("are you still there?")                                   # the engine's lock was released by the stop
        self.assertEqual(self.kinds()[-1], ("assistant", "Hello from the scripted fake model."))

    def test_a_model_that_fails_to_load_is_reported_and_loading_is_retried_next_time(self):
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("Failed to load model from file")
            return Scripted(**kw)
        self.c.engine_factory = lambda p: ge.GGUFEngine(p, llama_factory=flaky)
        self.assertTrue(self.c.send("hello"))
        settle(self.c, 10)
        self.assertEqual(self.kinds()[-1][0], "error")
        self.assertIn("could not be loaded", self.kinds()[-1][1])
        self.assertFalse(self.c.busy or self.c.loading)
        self.ask("try again")
        self.assertEqual(self.kinds()[-1], ("assistant", "Hello from the scripted fake model."))
        self.assertEqual(calls["n"], 2)


class DamagedFilesAreRefusedBeforeTheRuntimeIsTouched(AppCase):
    """Real ModelStore verification + controller; the runtime constructor is counted to prove it was never reached."""

    def check(self, name, damage, expect):
        reset()
        self.install_model(name)
        p = self.cfg.models_dir / name / "model.gguf"
        damage(p)
        ms = status.model_status(self.cfg)
        self.assertFalse(ms.ok)
        self.assertEqual(ms.state, "model_invalid")
        self.assertIn(expect, ms.summary)
        c = AppController(self.cfg, simulate=True, engine_factory=lambda d: ge.GGUFEngine(d, llama_factory=Scripted))
        self.addCleanup(c.shutdown, 2.0)
        c.send("hello")
        settle(c, 10)
        self.assertEqual(Scripted.built, 0, "the runtime must not be asked to load a file that already failed the check")
        self.assertEqual(c.transcript.entries[-1].kind, "error")
        self.assertFalse(any(e.kind == "assistant" and e.text for e in c.transcript.entries))
        self.assertFalse(c.busy)

    def test_bytes_that_are_not_a_gguf(self):
        self.check("garbage", lambda p: p.write_bytes(os.urandom(p.stat().st_size)), "not a GGUF file")

    def test_a_truncated_file(self):
        self.check("short", lambda p: p.write_bytes(p.read_bytes()[:10]), "size changed")

    def test_a_missing_file(self):
        self.check("gone", lambda p: p.unlink(), "missing")


class StatusIsTruthful(AppCase):
    def test_nothing_installed_says_so_and_never_ready(self):
        ms = status.model_status(self.cfg)
        self.assertFalse(ms.ok)
        self.assertEqual(ms.state, "no_model")

    def test_an_installed_model_without_the_runtime_is_not_called_ready(self):
        self.install_model()
        with mock.patch.object(status, "has_module", lambda name: False if name == "llama_cpp" else True):
            ms = status.model_status(self.cfg)
        self.assertFalse(ms.ok)
        self.assertEqual(ms.state, "runtime_missing")

    def test_ready_only_claims_the_quick_check_and_that_nothing_is_loaded_yet(self):
        self.install_model()
        with mock.patch.object(status, "has_module", lambda name: True), mock.patch.object(status, "import_problem", lambda name: ""):
            ms = status.model_status(self.cfg)
        self.assertTrue(ms.ok)
        self.assertIn("has not been loaded yet", ms.summary)


class OptInHelper(AppCase):
    def test_a_gguf_file_is_wrapped_as_a_linked_model_folder_the_engine_accepts(self):
        f = self.d / "qwen3-test.gguf"
        make_gguf(f)
        fake_module = types.SimpleNamespace(Llama=Scripted)
        with mock.patch.dict(sys.modules, {"llama_cpp": fake_module}):
            eng = engine_for_gguf_file(self, f)
        self.assertEqual(eng.path, f)
        self.assertTrue(eng.no_think)                                       # a Qwen file name turns the Qwen3 no-think suffix on
        with self.assertRaises(ge.RuntimeUnavailable):
            ge.GGUFEngine(f, llama_factory=Scripted)                         # the old call shape (the file itself) is what the opt-in tests used to do

    def test_the_no_think_suffix_follows_the_architecture_in_the_file_not_its_name(self):
        qwen = self.d / "model.gguf"                                         # a Qwen3 file with an unhelpful name
        make_gguf(qwen)
        other = self.d / "qwen3-but-actually-llama.gguf"                     # a misleading name on a different architecture (same length as "qwen3")
        other.write_bytes(qwen.read_bytes().replace(b"qwen3", b"llama"))
        with mock.patch.dict(sys.modules, {"llama_cpp": types.SimpleNamespace(Llama=Scripted)}):
            self.assertTrue(engine_for_gguf_file(self, qwen).no_think)
            self.assertFalse(engine_for_gguf_file(self, other).no_think)


HAVE_RUNTIME = importlib.util.find_spec("llama_cpp") is not None
REAL = os.environ.get("JARVIS_REAL_MODEL_TEST") == "1" and bool(os.environ.get("JARVIS_REAL_MODEL_PATH")) and HAVE_RUNTIME


@unittest.skipUnless(REAL, "REAL-RUNTIME TEST NOT RUN: needs llama-cpp-python, JARVIS_REAL_MODEL_TEST=1 and JARVIS_REAL_MODEL_PATH=<a .gguf>. A skip is NOT a pass.")
class RealRuntime(AppCase):
    """GENUINE llama.cpp + a GENUINE model file. Proves load, reply, streaming (stopped and to the end) and reuse after a stop on THIS machine with THIS model; says nothing
    about answer quality. Set JARVIS_REAL_MODEL_REPORT=<file.json> to record the model identity, runtime version and timings. See docs/REAL_INFERENCE_CHECKLIST.md."""

    def test_load_reply_stream_stop_and_reuse(self):
        import ctypes
        import hashlib
        import llama_cpp
        from jarvis.models import gguf as G
        path = Path(os.environ["JARVIS_REAL_MODEL_PATH"])
        self.assertTrue(path.is_file(), f"JARVIS_REAL_MODEL_PATH is not an existing file: {path}")
        info = G.summarize(G.read_header(path))
        self.assertGreater(info["tensor_count"], 0, "this file has no weight tensors (a vocabulary-only or synthetic file cannot generate text)")
        eng = engine_for_gguf_file(self, path)
        self.addCleanup(eng.close)
        # the runtime behind the engine must be the genuine llama.cpp binding: a loaded shared library and a real Llama object, not a stand-in module
        self.assertIsInstance(getattr(llama_cpp.llama_cpp, "_lib", None), ctypes.CDLL, "llama_cpp is not backed by a loaded llama.cpp shared library")
        self.assertIsInstance(eng.llm, llama_cpp.Llama)
        ask = [{"role": "user", "content": "Reply with one short sentence about the sea."}]
        t0 = time.perf_counter()
        text = eng.chat(ask, max_tokens=48, temperature=0.0)
        t_chat = time.perf_counter() - t0
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip(), "the real model returned no text")
        # streaming, stopped: close the generator after the first piece; the engine's lock must be released
        pieces, t1 = [], time.perf_counter()
        stream = eng.stream(ask, max_tokens=48, temperature=0.0)
        t_first = None
        for piece in stream:
            t_first = t_first or (time.perf_counter() - t1)
            pieces.append(piece)
            if len("".join(pieces)) > 3:
                break
        stream.close()
        self.assertTrue(pieces, "the real model streamed nothing")
        again = eng.chat(ask, max_tokens=16, temperature=0.0)
        self.assertTrue(again.strip(), "the model could not answer after a stopped stream")
        # streaming, to the end
        full = "".join(eng.stream(ask, max_tokens=16, temperature=0.0))
        self.assertTrue(full.strip(), "a stream run to the end produced no text")
        report = os.environ.get("JARVIS_REAL_MODEL_REPORT")
        if report:
            sha = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    sha.update(block)
            Path(report).write_text(json.dumps({
                "model_file": path.name, "model_bytes": path.stat().st_size, "model_sha256": sha.hexdigest(),
                "gguf": {k: info[k] for k in ("name", "architecture", "quantization", "context_length", "tensor_count", "block_count")},
                "llama_cpp_version": getattr(llama_cpp, "__version__", "unknown"), "gpu_offload_supported": ge.runtime_info().get("gpu_offload_supported"),
                "n_ctx": eng.n_ctx, "n_gpu_layers_requested": eng.gpu_layers_requested,
                "load_seconds": round(eng.load_seconds, 2), "chat_seconds": round(t_chat, 2), "first_stream_piece_seconds": round(t_first or 0.0, 2),
                "reply_chars": len(text), "first_stream_piece": pieces[0][:40], "reply_after_stop_chars": len(again), "full_stream_chars": len(full)}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
