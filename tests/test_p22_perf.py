"""Prompt 22 Stage 4B: logic of `jarvis perf` (src/jarvis/eval/perf.py). FAKE RUNTIME ONLY: these tests prove the measurement arithmetic, the separation of
load from generation, honesty about invalid runs and the bounded CLI - NOT real speed. The real measurement is the Windows workflow step 'Multi-token performance'."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import cli
from jarvis.config import Config
from jarvis.eval import perf
from jarvis.models.manager import ModelStore
from jarvis.net.policy import NetworkPolicy
from p10_common import make_gguf


class Clock:
    """Deterministic clock: each call advances by `step` seconds."""
    def __init__(self, step=0.1):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


class FakeLlm:
    n_ctx_seen = None

    def __init__(self, model_path, n_ctx, n_threads, verbose, **kw):
        self.resets = 0
        self.pieces = 60
        self.fail_after = None
        self.n_tokens = 0

    def reset(self):
        self.resets += 1
        self.n_tokens = 0

    def tokenize(self, b, add_bos=False):
        return list(range(max(1, len(b.split()))))

    def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
        def gen():
            yield {"choices": [{"delta": {"role": "assistant"}}]}                      # a role-only first chunk: no text, must not count
            for i in range(min(self.pieces, max_tokens)):
                if self.fail_after is not None and i >= self.fail_after:
                    raise RuntimeError("simulated runtime failure")
                self.n_tokens += 1
                yield {"choices": [{"delta": {"content": f"w{i} "}}]}
        return gen()


class PerfLogic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()
        store = ModelStore(self.cfg.models_dir, NetworkPolicy.load(self.cfg.settings_path))
        store.install_gguf(make_gguf(self.d / "m.gguf"), "fake", "Apache-2.0", "t", extra_meta=None)
        store.set_active("fake")
        self.eng = None

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self):
        from jarvis.inference.gguf_engine import GGUFEngine
        store = ModelStore(self.cfg.models_dir, NetworkPolicy.load(self.cfg.settings_path))
        return GGUFEngine(store.path("fake"), llama_factory=FakeLlm)

    def test_arithmetic_counts_text_chunks_only_and_excludes_prompt_evaluation(self):
        r = perf.one_run(self.engine(), "p", 40, clock=Clock(0.1))
        self.assertTrue(r["valid"], r)
        self.assertEqual(r["generated_tokens"], 40)                                   # the role-only chunk is not a token
        # clock: t0 = tick1; first token = tick2; last = tick41; end = tick42 (0.1 s steps)
        self.assertAlmostEqual(r["time_to_first_token_seconds"], 0.1, places=3)
        self.assertAlmostEqual(r["decode_seconds"], 3.9, places=3)
        self.assertAlmostEqual(r["sustained_tokens_per_second"], round(39 / 3.9, 2), places=2)     # (n-1)/(last-first), not n/total
        self.assertLess(r["overall_tokens_per_second"], r["sustained_tokens_per_second"] + 0.001)

    def test_context_is_reset_before_every_run(self):
        e = self.engine()
        perf.one_run(e, "p", 40)
        perf.one_run(e, "p", 40)
        self.assertEqual(e.llm.resets, 2)

    def test_too_few_tokens_is_invalid_with_a_reason(self):
        e = self.engine()
        e.llm.pieces = 5
        r = perf.one_run(e, "p", 40)
        self.assertFalse(r["valid"])
        self.assertIn("only 5 tokens", r["error"])

    def test_runtime_failure_mid_generation_is_recorded_not_raised(self):
        e = self.engine()
        e.llm.fail_after = 10
        r = perf.one_run(e, "p", 40)
        self.assertFalse(r["completed"])
        self.assertFalse(r["valid"])
        self.assertIn("simulated runtime failure", r["error"])
        self.assertEqual(r["generated_tokens"], 10)

    def test_measure_separates_load_warmup_and_runs_and_is_marked_simulated(self):
        res = perf.measure(self.cfg.data_dir, runs=3, warmup=1, max_tokens=40, llama_factory=FakeLlm)
        self.assertEqual(res["status"], "complete")
        self.assertTrue(res["simulated"])
        self.assertEqual(len(res["warmup"]), 1)
        self.assertEqual(len(res["runs"]), 3)
        self.assertIn("load_seconds", res["load"])
        self.assertEqual(res["summary"]["valid_runs"], 3)
        self.assertEqual(res["model"]["id"], "fake")
        self.assertEqual(res["execution"]["mode"], "cpu")
        self.assertIn("SIMULATED", perf.summary_text(res))
        json.dumps(res, default=str)

    def test_incomplete_when_a_run_is_invalid(self):
        class Short(FakeLlm):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.pieces = 3
        res = perf.measure(self.cfg.data_dir, runs=2, warmup=0, max_tokens=40, llama_factory=Short)
        self.assertEqual(res["status"], "incomplete")
        self.assertIn("only 3 tokens", res["error"])

    def test_no_model_is_a_failure_that_downloads_nothing(self):
        empty = Config(data_dir=self.d / "empty")
        empty.data_dir.mkdir()
        res = perf.measure(empty.data_dir, llama_factory=FakeLlm)
        self.assertEqual(res["status"], "failed")
        self.assertIn("Nothing was downloaded", res["error"])

    def test_load_failure_is_reported(self):
        def broken(**kw):
            raise OSError("cannot map file")
        res = perf.measure(self.cfg.data_dir, runs=1, warmup=0, llama_factory=broken)
        self.assertEqual(res["status"], "failed")
        self.assertIn("could not be loaded", res["error"])

    def test_cli_is_bounded_and_exit_codes_follow_the_status(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "perf", "--runs", "99"])
        self.assertEqual(rc, 2)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "perf", "--max-tokens", "100000"])
        self.assertEqual(rc, 2)
        # a model whose real runtime is not installed here -> failed (3), and perf.json is still written when --out is given
        o = self.d / "out"
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "perf", "--out", str(o), "--runs", "1", "--warmup", "0"])
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads((o / "perf.json").read_text(encoding="utf-8"))["status"], "failed")

    def test_memory_probe_does_not_raise(self):
        v = perf.peak_memory_mb()
        self.assertTrue(v is None or v > 0)


if __name__ == "__main__":
    unittest.main()
