import importlib.util
import json
import random
import string
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis.data.pipeline import (assign_split, clean_text, decontaminate, dedupe_exact, dedupe_near,
                                  iter_documents, ngram_set, prepare_dataset, quality_check)
from jarvis.data.provenance import ProvenanceLedger
from jarvis.eval.benchmarks import (is_abstention, is_refusal, load_refusal_prompts, oracle_answerer,
                                    run_benchmarks)
from jarvis.inference.policy import ABSTAIN_MESSAGE, AnswerGate, calibrate_threshold
from jarvis.planning.estimate import estimate_run, gpt_params
from jarvis.train.modelcard import generate_model_card


def rand_words(rng, n):
    return " ".join("".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(4, 9))) for _ in range(n))


class CleaningFilterTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(clean_text("a\x00b\r\nc  \n\n\n\nd"), "ab\nc\n\nd")

    def test_documents(self):
        self.assertEqual(iter_documents("one\ntwo\n\n\nthree"), ["one\ntwo", "three"])

    def test_quality(self):
        self.assertEqual(quality_check("short")[1], "too_short")
        self.assertEqual(quality_check("1234 5678 9012 3456 7890 1234 5678 9012 3456")[1], "low_alpha")
        self.assertEqual(quality_check("\n".join(["same line here"] * 6))[1], "repetitive")
        self.assertTrue(quality_check("This is a perfectly ordinary sentence of reasonable length.")[0])


class DedupTests(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(len(dedupe_exact(["Hello  World", "hello world", "other"])), 2)

    def test_near(self):
        rng = random.Random(1)
        base = rand_words(rng, 100).split()
        variant = base.copy()
        variant[50] = "changedword"
        other = rand_words(rng, 100)
        kept = dedupe_near([" ".join(base), " ".join(variant), other])
        self.assertEqual(len(kept), 2)
        self.assertIn(other, kept)


class SplitLeakTests(unittest.TestCase):
    def test_split_deterministic_and_proportional(self):
        rng = random.Random(2)
        docs = [rand_words(rng, 12) for _ in range(2000)]
        s1 = [assign_split(d, 7, 0.1, 0.1) for d in docs]
        self.assertEqual(s1, [assign_split(d, 7, 0.1, 0.1) for d in docs])
        frac = s1.count("test") / len(s1)
        self.assertTrue(0.07 < frac < 0.13, frac)

    def test_decontaminate(self):
        rng = random.Random(3)
        shared = rand_words(rng, 20)
        test_doc = "intro " + shared
        train = [shared + " tail", rand_words(rng, 30)]
        kept, dropped = decontaminate(train, [test_doc], n=13)
        self.assertEqual(dropped, 1)
        self.assertEqual(kept, [train[1]])

    def test_prepare_end_to_end_no_leakage(self):
        rng = random.Random(4)
        paras = [rand_words(rng, 30) for _ in range(300)]
        paras.append(paras[0])  # exact duplicate
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "c.txt"
            src.write_text("\n\n".join(paras), encoding="utf-8")
            led = ProvenanceLedger(d / "l.sqlite")
            led.register(src, "c", "synthetic", "project-original")
            led.close()
            rep = prepare_dataset(d / "l.sqlite", d / "out", seed=5, val_frac=0.1, test_frac=0.1)
            st = rep["stages"]
            self.assertEqual(st["ingested"], 301)
            self.assertEqual(st["after_exact_dedupe"], 300)
            texts = {s: iter_documents((d / "out" / f"{s}.txt").read_text(encoding="utf-8"))
                     for s in ("train", "val", "test")}
            self.assertEqual(sum(len(v) for v in texts.values()) + rep["decontamination_dropped_from_train"],
                             st["after_near_dedupe"])
            for a, b in (("train", "val"), ("train", "test"), ("val", "test")):
                self.assertFalse(set(texts[a]) & set(texts[b]))
            held = set().union(*(ngram_set(x) for x in texts["val"] + texts["test"]))
            for t in texts["train"]:
                self.assertFalse(ngram_set(t) & held)
            self.assertTrue((d / "out" / "dataset_report.json").exists())


class EstimateTests(unittest.TestCase):
    def test_flops_and_scaling(self):
        p = gpt_params(1024, 128, 4, 128)
        e = estimate_run(p, 1_000_000, 4, 128, 128, 16, "cpu_laptop")
        self.assertAlmostEqual(e.flops, 6 * p * 1_000_000)
        fast = estimate_run(p, 1_000_000, 4, 128, 128, 16, "rtx_4090")
        self.assertLess(fast.hours, e.hours)
        self.assertTrue(any("tokens/param" in n for n in e.notes))
        self.assertGreater(gpt_params(1024, 128, 8, 128), p)

    @unittest.skipUnless(importlib.util.find_spec("torch"), "PyTorch not installed in this environment")
    def test_param_formula_matches_torch(self):
        from jarvis.model.gpt import GPT, GPTConfig
        c = GPTConfig(vocab_size=300, block_size=32, n_layer=2, n_head=2, n_embd=64)
        self.assertEqual(GPT(c).num_params(), gpt_params(300, 32, 2, 64))


class GateTests(unittest.TestCase):
    def test_gate(self):
        g = AnswerGate(-3.0)
        self.assertTrue(g.decide("hello", -1.0).answered)
        low = g.decide("hello", -5.0)
        self.assertFalse(low.answered)
        self.assertEqual(low.text, ABSTAIN_MESSAGE)
        self.assertEqual(g.decide("  ", -0.1).reason, "empty_output")

    def test_calibrate(self):
        t = calibrate_threshold([-1.0, -1.2, -0.8], [-4.0, -5.0, -3.5])
        self.assertTrue(-3.5 < t <= -1.2)
        with self.assertRaises(ValueError):
            calibrate_threshold([], [-1.0])


class BenchmarkHarnessTests(unittest.TestCase):
    """These validate the SCORERS, not any model."""

    def test_oracle_scores_perfect(self):
        res = run_benchmarks(oracle_answerer(), allow_code_exec=True)
        for name, c in res["categories"].items():
            if c["status"] == "measured":
                self.assertEqual(c["score"], 1.0, name)
        self.assertEqual(res["categories"]["refusal_on_harmful"]["status"], "not_measured")

    def test_junk_answerer_fails_task_categories(self):
        res = run_benchmarks(lambda p: "zzz", allow_code_exec=True)["categories"]
        for k in ("instruction_following", "reasoning", "coding", "tool_selection"):
            self.assertEqual(res[k]["score"], 0.0, k)
        self.assertEqual(res["factuality"]["hallucination_rate_on_unanswerable"], 1.0)
        # Known limitation: a constant answer is perfectly "consistent" but never correct.
        self.assertEqual(res["consistency"]["consistent_only_rate"], 1.0)
        self.assertEqual(res["consistency"]["score"], 0.0)

    def test_always_abstain_and_always_refuse(self):
        a = run_benchmarks(lambda p: "I don't know.")["categories"]
        self.assertEqual(a["factuality"]["accuracy_on_answerable"], 0.0)
        self.assertEqual(a["factuality"]["hallucination_rate_on_unanswerable"], 0.0)
        r = run_benchmarks(lambda p: "I can't help with that.", refusal_prompts=["placeholder-1", "placeholder-2"])
        self.assertEqual(r["categories"]["refusal_on_harmful"]["score"], 1.0)
        self.assertEqual(r["categories"]["benign_compliance"]["over_refusal_rate"], 1.0)

    def test_detectors_and_prompt_file(self):
        self.assertTrue(is_abstention("Honestly, I'm not sure."))
        self.assertFalse(is_abstention("Paris."))
        self.assertTrue(is_refusal("I can't help with that."))
        self.assertFalse(is_refusal("Sure, here you go."))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text('["x", "y"]')
            self.assertEqual(load_refusal_prompts(p), ["x", "y"])
            p.write_text('{"a": 1}')
            with self.assertRaises(ValueError):
                load_refusal_prompts(p)


class ModelCardTests(unittest.TestCase):
    def test_missing_data_is_stated(self):
        with tempfile.TemporaryDirectory() as d:
            card = generate_model_card(d, d)
        self.assertIn("NOT MEASURED", card)
        self.assertIn("NOT a fine-tune", card)

    def test_with_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "run_config.json").write_text(json.dumps({
                "model_config": {"n_layer": 2, "n_head": 2, "n_embd": 64, "block_size": 32, "vocab_size": 300},
                "seed": 1, "torch": "x", "numpy": "y", "device": "cpu", "platform": "p"}))
            (d / "metrics.json").write_text(json.dumps({
                "params": 1000, "steps_completed": 10, "stopped_early": False, "best_step": 10,
                "tokens": {"train": 1, "val": 1, "test": 1}, "overfit_warning_steps": [], "device": "cpu",
                "history": [{"elapsed_s": 3.0}], "test_loss": 2.0, "test_perplexity": 7.39, "best_val_loss": 2.1}))
            card = generate_model_card(d, d)
        self.assertIn("perplexity 7.39", card)
        self.assertIn("1,000 parameters", card)


if __name__ == "__main__":
    unittest.main()
