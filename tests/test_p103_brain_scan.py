"""Startup brain scan: Jarvis asks to install a model ONLY when no installed brain is usable. Deleting the small model and importing a bigger one must not bring the question back."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.app import setup_engine as SE
from jarvis.config import Config
from jarvis.models.manager import ModelStore
from jarvis.net.policy import NetworkPolicy
from p10_common import make_gguf


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()
        self.policy = NetworkPolicy.load(self.cfg.settings_path)
        self.store = ModelStore(self.cfg.models_dir, self.policy)

    def real(self):
        return SE.SetupEngine(self.cfg, self.store, self.policy)            # no fakes: the real rule

    def add(self, mid, activate=False):
        self.store.install_gguf(make_gguf(self.d / f"{mid}.gguf"), mid, "Apache-2.0", "t")
        if activate:
            self.store.set_active(mid)

    def test_no_brain_asks(self):
        self.assertTrue(self.real().needs_setup())

    def test_replaced_brain_does_not_ask(self):
        self.add("qwen3-1.7b-q4_k_m", activate=True)
        eng = self.real()
        self.assertFalse(eng.needs_setup())
        eng._save_state({"completed": True, "model_id": "qwen3-1.7b-q4_k_m", "version_major": "0"})
        self.store.remove("qwen3-1.7b-q4_k_m")
        self.add("qwen3-8b-q4_k_m")                                         # imported, never made "the" setup model
        self.assertFalse(self.real().needs_setup())
        self.assertEqual(self.store.active(), "qwen3-8b-q4_k_m")            # and it is the one in use now
        self.assertEqual(self.real().load_state()["model_id"], "qwen3-8b-q4_k_m")

    def test_asks_again_only_when_nothing_is_left(self):
        self.add("qwen3-8b-q4_k_m", activate=True)
        self.assertFalse(self.real().needs_setup())
        self.store.remove("qwen3-8b-q4_k_m")
        self.assertTrue(self.real().needs_setup())

    def test_a_damaged_brain_is_skipped_for_a_good_one(self):
        self.add("good-model", activate=True)
        self.add("bad-model")
        (self.store.path("bad-model") / "model.gguf").write_bytes(b"x")
        self.assertEqual(self.real().usable_models(), ["good-model"])
        self.assertFalse(self.real().needs_setup())

    def test_newest_brain_is_chosen_when_the_selection_is_gone(self):
        import os
        self.add("old-model", activate=True)
        self.add("new-model")
        os.utime(self.store.path("old-model") / "meta.json", (1_000, 1_000))
        (self.store.dir / "active.json").unlink()
        self.assertFalse(self.real().needs_setup())
        self.assertEqual(self.store.active(), "new-model")

    def test_two_brains_keep_the_active_one(self):
        self.add("a-model", activate=True)
        self.add("b-model")
        self.assertFalse(self.real().needs_setup())
        self.assertEqual(self.store.active(), "a-model")


if __name__ == "__main__":
    unittest.main()
