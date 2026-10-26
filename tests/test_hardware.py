import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis.hardware import TIERS, Hardware, assess, detect_hardware, recommend


def hw(ram, disk=500.0):
    return Hardware(os="test", cpu_threads=4, ram_gb=ram, free_disk_gb=disk)


class HardwareTests(unittest.TestCase):
    def test_detect_runs_here_and_is_sane(self):
        h = detect_hardware(".")
        self.assertGreaterEqual(h.cpu_threads, 1)
        self.assertTrue(h.ram_gb is None or h.ram_gb > 0.5)
        self.assertTrue(h.free_disk_gb is None or h.free_disk_gb > 0)

    def test_tiers_are_ordered_and_sized(self):
        p = [t.params for t in TIERS]
        self.assertEqual(p, sorted(p))
        self.assertLess(TIERS[0].params, 2e6)
        self.assertTrue(100e6 < TIERS[2].params < 120e6)

    def test_recommend_scales_with_ram(self):
        self.assertEqual(recommend(hw(4.0))["recommended"], "jarvis-lm-base")  # estimates are small
        self.assertIsNone(recommend(hw(0.4))["recommended"])
        v = {a["tier"]: a["verdict"] for a in recommend(hw(0.4))["assessments"]}
        self.assertEqual(v["jarvis-lm-base"], "too_large")

    def test_tight_unknown_and_disk(self):
        base = TIERS[2]
        self.assertEqual(assess(hw(base.min_ram_gb + 0.1), base)["verdict"], "tight")
        self.assertEqual(assess(hw(None), base)["verdict"], "unknown")
        self.assertEqual(assess(hw(64.0, disk=0.2), base)["verdict"], "too_large")


if __name__ == "__main__":
    unittest.main()
