"""One loaded model object must never be used by two requests at the same time (llama-cpp-python's Llama is not thread-safe), a
stopped or failed reply must release it, and shutting the engine down must not free it underneath a reply that is still running.

Everything here uses a FAKE runtime object and a synthetic GGUF header in a temporary folder: it tests this program's own locking logic on
the machine running the tests. It does NOT prove anything about the real llama.cpp runtime, a real model, or Windows."""
import gc
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from test_local_model import make_gguf

from jarvis.inference import gguf_engine as G
from jarvis.inference.gguf_engine import GGUFEngine, InferenceError


def _reply(text="ok"):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {}}


class FakeLlama:
    """Records how many calls are inside the model at once. `gate` (an Event) lets a test hold a streamed reply open."""
    instances: list = []

    def __init__(self, **kw):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.gate: threading.Event | None = None
        self.started = threading.Event()
        self.close_calls = 0
        self.fail_after_first = False
        FakeLlama.instances.append(self)

    def tokenize(self, b, add_bos=False):
        return list(b)

    def _enter(self):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _leave(self):
        with self.lock:
            self.active -= 1

    def close(self):
        self.close_calls += 1

    def create_chat_completion(self, stream=False, **kw):
        if not stream:
            self._enter()
            try:
                time.sleep(0.05)
                return _reply()
            finally:
                self._leave()
        return self._stream()

    def _stream(self):
        self._enter()
        try:
            self.started.set()
            yield {"choices": [{"delta": {"content": "hello there, friend "}}]}
            if self.fail_after_first:
                raise RuntimeError("native failure containing the prompt")
            if self.gate is not None:
                self.gate.wait(5)
            yield {"choices": [{"delta": {"content": "world"}}]}
        finally:
            self._leave()


class EngineSerializationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        make_gguf(self.root / "model.gguf")
        FakeLlama.instances = []
        self.eng = GGUFEngine(self.root, llama_factory=FakeLlama)
        self.llm = FakeLlama.instances[-1]
        self.msgs = [{"role": "user", "content": "hi"}]

    def test_concurrent_chats_never_overlap_inside_the_model(self):
        ts = [threading.Thread(target=self.eng.chat, args=(self.msgs,)) for _ in range(4)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        self.assertFalse(any(t.is_alive() for t in ts))
        self.assertEqual(self.llm.max_active, 1)

    def test_a_chat_waits_for_a_running_stream_on_another_thread(self):
        self.llm.gate = threading.Event()
        it = self.eng.stream(self.msgs)
        first = next(it)                       # the stream now holds the model
        self.assertTrue(self.llm.started.is_set())
        done = threading.Event()
        out = []
        t = threading.Thread(target=lambda: (out.append(self.eng.chat(self.msgs)), done.set()))
        t.start()
        time.sleep(0.2)
        self.assertFalse(done.is_set(), "the second request must wait while the first reply is running")
        self.assertEqual(self.llm.max_active, 1)
        self.llm.gate.set()
        rest = "".join(it)
        t.join(10)
        self.assertTrue(done.is_set())
        self.assertEqual(out, ["ok"])
        self.assertEqual(self.llm.max_active, 1)
        self.assertEqual(first + rest, "hello there, friend world")

    def test_a_stopped_stream_releases_the_model(self):
        self.llm.gate = threading.Event()
        self.llm.gate.set()
        it = self.eng.stream(self.msgs)
        next(it)
        it.close()                              # what stopping a reply does
        self.assertEqual(self.eng.chat(self.msgs), "ok")

    def test_an_abandoned_stream_releases_the_model_when_dropped(self):
        it = self.eng.stream(self.msgs)
        next(it)
        del it
        gc.collect()
        self.assertEqual(self.eng.chat(self.msgs), "ok")

    def test_a_failing_stream_releases_the_model_and_hides_the_native_message(self):
        self.llm.fail_after_first = True
        with self.assertRaises(InferenceError) as cm:
            list(self.eng.stream(self.msgs))
        self.assertNotIn("prompt", str(cm.exception))
        self.assertEqual(self.eng.chat(self.msgs), "ok")

    def test_a_nested_call_on_the_same_thread_is_refused_not_deadlocked(self):
        result = {}

        def run():
            it = self.eng.stream(self.msgs)
            next(it)
            try:
                self.eng.chat(self.msgs)
            except InferenceError as e:
                result["err"] = str(e)
            it.close()
        t = threading.Thread(target=run)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "a same-thread nested request must not hang")
        self.assertIn("already producing a reply", result.get("err", ""))

    def test_close_does_not_free_the_model_under_a_running_reply(self):
        self.llm.gate = threading.Event()
        it = self.eng.stream(self.msgs)
        first = next(it)
        with mock.patch.object(G, "CLOSE_WAIT_SECONDS", 0.2):
            self.eng.close()
        self.assertEqual(self.llm.close_calls, 0, "native close while a reply is running could crash the process")
        with self.assertRaises(InferenceError):
            self.eng.chat(self.msgs)            # no new request is accepted after close
        self.llm.gate.set()
        self.assertEqual(first + "".join(it), "hello there, friend world")  # the running reply is allowed to finish

    def test_a_request_waiting_when_close_happens_gets_a_clear_error(self):
        self.llm.gate = threading.Event()
        it = self.eng.stream(self.msgs)
        next(it)
        errs = []

        def waiter():
            try:
                self.eng.chat(self.msgs)
            except InferenceError as e:
                errs.append(str(e))
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        with mock.patch.object(G, "CLOSE_WAIT_SECONDS", 0.1):
            self.eng.close()
        self.llm.gate.set()
        list(it)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(errs), 1)
        self.assertIn("shut down", errs[0])
        self.assertEqual(self.llm.max_active, 1)

    def test_close_when_idle_frees_once_and_is_safe_twice(self):
        self.eng.close()
        self.eng.close()
        self.assertEqual(self.llm.close_calls, 1)
        with self.assertRaises(InferenceError):
            self.eng.chat(self.msgs)

    def test_two_threads_closing_at_once_free_the_model_only_once(self):
        ts = [threading.Thread(target=self.eng.close) for _ in range(6)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        self.assertFalse(any(t.is_alive() for t in ts))
        self.assertEqual(self.llm.close_calls, 1)

    def test_an_engine_built_without_init_still_gets_a_lock(self):
        e = GGUFEngine.__new__(GGUFEngine)
        e.llm = None
        with self.assertRaises(InferenceError):
            e.chat(self.msgs)


if __name__ == "__main__":
    unittest.main()
