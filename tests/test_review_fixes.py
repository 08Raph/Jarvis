"""Regression tests for problems found in the Milestone 3 review."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis.data.pipeline import prepare_dataset
from jarvis.data.provenance import ProvenanceLedger
from jarvis.eval.benchmarks import oracle_answerer, run_benchmarks
from jarvis.memory.store import MemoryStore
from jarvis.tools.registry import build_default_registry, safe_eval
from jarvis.train.modelcard import generate_model_card


class CalculatorBoundsTests(unittest.TestCase):
    def test_nested_power_is_rejected_quickly(self):
        t = time.time()
        with self.assertRaises(ValueError):
            safe_eval("(((10**100)**100)**100)**100")
        self.assertLess(time.time() - t, 1.0)

    def test_normal_powers_still_work(self):
        self.assertEqual(safe_eval("2**10"), 1024)
        self.assertEqual(safe_eval("10**20"), 10**20)
        self.assertEqual(safe_eval("2**-1"), 0.5)


class LedgerIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_removed_content_cannot_be_reregistered(self):
        f = self.d / "a.txt"
        f.write_text("licensed text that was later removed on request")
        led = ProvenanceLedger(self.d / "l.db")
        sid = led.register(f, "a", "o", "MIT")
        led.mark_removed(sid, "takedown")
        with self.assertRaises(ValueError):
            led.register(f, "a-again", "o", "MIT")
        self.assertEqual(led.approved(), [])
        led.close()

    def test_pipeline_rejects_file_changed_after_approval(self):
        f = self.d / "b.txt"
        f.write_text("an approved paragraph that is long enough to count as a document here\n")
        led = ProvenanceLedger(self.d / "l.db")
        led.register(f, "b", "o", "MIT")
        led.close()
        f.write_text("swapped in after approval, different unreviewed content entirely and long enough\n")
        with self.assertRaises(RuntimeError):
            prepare_dataset(self.d / "l.db", self.d / "out")


class ToolLaunchTests(unittest.TestCase):
    def test_bare_name_is_not_resolved_from_cwd(self):
        mem = MemoryStore()
        reg = build_default_registry(mem, {"fake": "definitely_not_a_real_program.exe"}, platform="win32")
        r = reg.run("open_app", {"name": "fake"}, confirm=lambda t, a: True)
        self.assertEqual(r.status, "error")
        self.assertIn("not found", r.error)

    def test_absolute_allowlisted_path_launches(self):
        true_bin = Path("/bin/true")
        if not true_bin.is_file():
            self.skipTest("/bin/true not available")
        mem = MemoryStore()
        reg = build_default_registry(mem, {"t": str(true_bin)}, platform="win32")
        r = reg.run("open_app", {"name": "t"}, confirm=lambda t, a: True)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(mem.actions()[-1]["status"], "ok")


class CodeExecutionGateTests(unittest.TestCase):
    def test_coding_not_run_by_default(self):
        called = []

        def spy(prompt):
            called.append(prompt)
            return "def add(a, b):\n    return a + b"

        res = run_benchmarks(spy)
        self.assertEqual(res["categories"]["coding"]["status"], "not_measured")

    def test_coding_runs_when_enabled(self):
        res = run_benchmarks(oracle_answerer(), allow_code_exec=True)
        self.assertEqual(res["categories"]["coding"]["score"], 1.0)


class ModelCardHonestyTests(unittest.TestCase):
    def test_untrained_card_does_not_claim_training(self):
        with tempfile.TemporaryDirectory() as d:
            card = generate_model_card(d, d)
        self.assertIn("No trained model was found", card)


if __name__ == "__main__":
    unittest.main()
