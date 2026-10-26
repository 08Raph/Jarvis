"""Prompt 20 tests: the real-PC evidence collector and the opt-in GPU-layers setting. Fake models only: these prove the plumbing and the honesty rules,
not model quality or GPU speed."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis.eval import winvalidate as wv
from jarvis.inference import gguf_engine as ge


class FakeKw:
    last = None

    def __init__(self, **kw):
        FakeKw.last = kw

    def tokenize(self, b, add_bos=False):
        return list(range(len(b.split())))

    def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
        t = "TOOL: get_time" if "TOOL:" in messages[0]["content"] else "ready"
        if not stream:
            return {"choices": [{"message": {"content": t}}]}
        return iter({"choices": [{"delta": {"content": t}}]} for _ in range(1))


def mdir(t):
    d = Path(t) / "m"
    d.mkdir()
    (d / "model.gguf").write_bytes(b"GGUF" + b"\0" * 64)
    return d


class GpuLayersSetting(unittest.TestCase):
    def test_default_is_cpu_only_and_not_passed(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_GPU_LAYERS", None)
            e = ge.GGUFEngine(mdir(t), llama_factory=FakeKw)
            self.assertNotIn("n_gpu_layers", FakeKw.last)
            self.assertEqual(e.gpu_layers_requested, 0)

    def test_env_var_opt_in(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"JARVIS_GPU_LAYERS": "-1"}):
            e = ge.GGUFEngine(mdir(t), llama_factory=FakeKw)
            self.assertEqual(FakeKw.last["n_gpu_layers"], -1)
            self.assertEqual(e.gpu_layers_requested, -1)

    def test_explicit_argument_beats_env(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"JARVIS_GPU_LAYERS": "-1"}):
            e = ge.GGUFEngine(mdir(t), llama_factory=FakeKw, n_gpu_layers=0)
            self.assertEqual(e.gpu_layers_requested, 0)

    def test_bad_env_value_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"JARVIS_GPU_LAYERS": "lots"}):
            with self.assertRaises(ge.RuntimeUnavailable) as c:
                ge.GGUFEngine(mdir(t), llama_factory=FakeKw)
            self.assertIn("JARVIS_GPU_LAYERS", str(c.exception))


class Collector(unittest.TestCase):
    def test_env_report_has_the_facts_a_reviewer_needs(self):
        with tempfile.TemporaryDirectory() as t:
            r = wv.environment(Path(t))
        for k in ("jarvis_version", "python", "platform", "hardware", "runtime", "packages", "nvidia_smi", "acceleration_state", "plan"):
            self.assertIn(k, r)
        self.assertFalse(r["acceleration_state"]["acceleration_verified"])

    def test_infer_without_model_reports_error_not_success(self):
        with tempfile.TemporaryDirectory() as t:
            r = wv.infer(Path(t), 0)
        self.assertIn("no model", r["error"])
        self.assertNotIn("validation", r)

    def test_infer_with_fake_factory_is_flagged_simulated(self):
        with tempfile.TemporaryDirectory() as t:
            d = mdir(t)
            store = mock.Mock(active=lambda: "x", path=lambda i: d, verify=lambda i: (True, []))
            with mock.patch.object(wv, "_open_store", return_value=(store, "x")):
                r = wv.infer(Path(t), -1, llama_factory=FakeKw)
        self.assertTrue(r["validation"]["simulated"])
        self.assertFalse(r["validation"]["real_validation_passed"])
        self.assertEqual(r["gpu_layers_requested"], -1)
        self.assertIn("SIMULATED", r["report_text"])
        self.assertIn("multi_turn_recalls_name", r["content_checks"])
        self.assertFalse(r["content_checks"]["multi_turn_recalls_name"])      # the fake does not remember: a failing check is recorded, not hidden

    def test_main_returns_nonzero_when_no_model(self):
        with tempfile.TemporaryDirectory() as t, mock.patch("sys.stdout"):
            self.assertEqual(wv.main(["--out", t, "--data-dir", t, "infer"]), 1)
            self.assertTrue((Path(t) / "infer_layers0.json").is_file())


class Compare(unittest.TestCase):
    def run_cmp(self, cpu, gpu):
        with tempfile.TemporaryDirectory() as t:
            a, b = Path(t) / "c.json", Path(t) / "g.json"
            a.write_text(json.dumps(cpu)); b.write_text(json.dumps(gpu))
            return wv.compare(a, b)

    def base(self, layers, tps, util=None, real=True, backend=True):
        return {"gpu_layers_requested": layers, "median_tokens_per_second": tps, "validation": {"real_inference_passed": real},
                "runtime": {"gpu_offload_supported": backend}, "resources": {"gpu_util_max_percent": util}}

    def test_verified_only_with_real_runs_speedup_and_gpu_activity(self):
        r = self.run_cmp(self.base(0, 10.0), self.base(-1, 40.0, util=70))
        self.assertTrue(r["acceleration_verified"]); self.assertEqual(r["speedup"], 4.0)

    def test_fast_but_no_gpu_activity_is_not_verified(self):
        self.assertFalse(self.run_cmp(self.base(0, 10.0), self.base(-1, 40.0, util=0))["acceleration_verified"])

    def test_no_speedup_is_not_verified(self):
        self.assertFalse(self.run_cmp(self.base(0, 10.0), self.base(-1, 10.5, util=60))["acceleration_verified"])

    def test_simulated_runs_never_verify(self):
        self.assertFalse(self.run_cmp(self.base(0, 10.0, real=False), self.base(-1, 40.0, util=70, real=False))["acceleration_verified"])

    def test_gpu_run_with_zero_layers_is_not_a_gpu_run(self):
        self.assertFalse(self.run_cmp(self.base(0, 10.0), self.base(0, 40.0, util=70))["acceleration_verified"])


if __name__ == "__main__":
    unittest.main()
