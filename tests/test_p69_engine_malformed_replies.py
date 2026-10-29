"""GGUFEngine against a runtime that returns something that is not a chat completion (FAKE runtime: this proves OUR parsing and error handling only; real
llama.cpp is not available here and is never claimed). A malformed reply must surface as InferenceError (which the app turns into a plain message), never as a
raw KeyError/IndexError/TypeError, and must release the engine lock so the next request works. Also: shutdown and failure paths around loading."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jarvis.inference import gguf_engine as ge  # noqa: E402
from test_local_model import make_gguf  # noqa: E402

MSG = [{"role": "user", "content": "hi"}]
GOOD = {"choices": [{"message": {"content": "The answer is 4."}, "finish_reason": "stop"}], "usage": {"completion_tokens": 4}}


class Scripted:
    def __init__(self, resp=None, chunks=None, **kw):
        self.resp, self.chunks, self.closed = resp, chunks, False

    def tokenize(self, b, add_bos=False):
        return list(b)

    def create_chat_completion(self, messages=None, stream=False, **kw):
        return iter(self.chunks) if stream else self.resp

    def close(self):
        self.closed = True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        make_gguf(self.d / "model.gguf")

    def engine(self, **kw):
        return ge.GGUFEngine(self.d, llama_factory=lambda **k: Scripted(**kw))


class NonStreaming(Base):
    BAD = {"empty choices": {"choices": []}, "no choices": {}, "no message": {"choices": [{"finish_reason": "stop"}]}, "not a dict": "oops",
           "content is a number": {"choices": [{"message": {"content": 123}}]}, "choices is None": {"choices": None}}

    def test_malformed_replies_are_inference_errors_and_the_engine_stays_usable(self):
        for name, resp in self.BAD.items():
            with self.subTest(name):
                e = self.engine(resp=resp)
                with self.assertRaises(ge.InferenceError) as cm:
                    e.chat(MSG)
                self.assertIn("unexpected format", str(cm.exception))
                e.llm.resp = GOOD                          # the very next request works: the generation lock was released
                self.assertEqual(e.chat(MSG), "The answer is 4.")

    def test_null_content_is_an_empty_reply_not_a_crash(self):
        self.assertEqual(self.engine(resp={"choices": [{"message": {"content": None}}]}).chat(MSG), "")

    def test_the_error_text_never_contains_the_reply_or_the_prompt(self):
        e = self.engine(resp={"choices": [{"message": {"content": {"secret": "PRIVATE-REPLY-TEXT"}}}]})
        with self.assertRaises(ge.InferenceError) as cm:
            e.chat([{"role": "user", "content": "PRIVATE-PROMPT-TEXT"}])
        self.assertNotIn("PRIVATE", str(cm.exception))


class Streaming(Base):
    def test_malformed_chunks_are_inference_errors_and_release_the_lock(self):
        for name, chunks in {"no choices key": [{}], "not a dict": ["x"], "content is a number": [{"choices": [{"delta": {"content": 5}}]}]}.items():
            with self.subTest(name):
                e = self.engine(chunks=chunks)
                with self.assertRaises(ge.InferenceError):
                    "".join(e.stream(MSG))
                e.llm.chunks = [{"choices": [{"delta": {"content": "ok"}}]}]
                self.assertEqual("".join(e.stream(MSG)), "ok")

    def test_chunks_without_text_are_skipped(self):
        chunks = [{"choices": [{"delta": {"role": "assistant"}}]}, {"choices": []}, {"choices": [{"delta": None}]}, {"choices": [{"delta": {"content": None}}]},
                  {"choices": [{"delta": {"content": "Four."}}]}]
        self.assertEqual("".join(self.engine(chunks=chunks).stream(MSG)), "Four.")


class LoadingAndShutdown(Base):
    def test_load_error_text_is_wrapped_and_names_no_prompt(self):
        def boom(**kw):
            raise ValueError("llama_model_load: error loading model")
        with self.assertRaises(ge.RuntimeUnavailable) as cm:
            ge.GGUFEngine(self.d, llama_factory=boom)
        self.assertIn("could not be loaded", str(cm.exception))

    def test_a_damaged_meta_json_is_refused_and_the_model_file_untouched(self):
        (self.d / "meta.json").write_text("{not json", encoding="utf-8")
        before = (self.d / "model.gguf").read_bytes()
        with self.assertRaises(ge.RuntimeUnavailable) as cm:
            ge.GGUFEngine(self.d, llama_factory=lambda **k: Scripted())
        self.assertIn("meta.json", str(cm.exception))
        self.assertEqual((self.d / "model.gguf").read_bytes(), before)

    def test_close_twice_frees_once_and_a_closed_engine_refuses_to_answer(self):
        e = self.engine(resp=GOOD)
        llm = e.llm
        e.close()
        e.close()
        self.assertTrue(llm.closed)
        with self.assertRaises(ge.InferenceError) as cm:
            e.chat(MSG)
        self.assertIn("shut down", str(cm.exception))

    def test_close_while_a_reply_is_streaming_does_not_free_the_model_underneath_it(self):
        started, release = threading.Event(), threading.Event()

        def chunks():
            started.set()
            release.wait(5)
            yield {"choices": [{"delta": {"content": "late"}}]}
        e = self.engine()
        e.llm.chunks = chunks()
        out = []
        t = threading.Thread(target=lambda: out.append("".join(e.stream(MSG))), daemon=True)
        t.start()
        self.assertTrue(started.wait(5))
        llm = e.llm
        closer = threading.Thread(target=e.close, daemon=True)
        closer.start()
        closer.join(0.3)
        self.assertFalse(llm.closed, "the native model was freed while a reply was still being generated")
        release.set()
        t.join(5)
        closer.join(10)
        self.assertEqual(out, ["late"])
        self.assertTrue(llm.closed)


if __name__ == "__main__":
    unittest.main()
