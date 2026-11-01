"""A saved speech-model folder that has disappeared is recovered from the shipped copy (local files only), or fails with an honest message."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.voice import bundled_vosk as B  # noqa: E402


class Cfg:
    def __init__(self, d):
        self.data_dir, self.profile_dir = Path(d) / "data", Path(d) / "profile"


class Recover(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.cfg = Cfg(self.t.name)
        self.bundle = Path(self.t.name) / "bundled_models"
        (self.bundle / B.MODEL_NAME / "am").mkdir(parents=True)
        (self.bundle / B.MODEL_NAME / "am" / "final.mdl").write_text("x")
        d, n = B.tree_digest(self.bundle / B.MODEL_NAME)
        p1 = mock.patch.object(B, "PIN_TREE_SHA256", d)
        p2 = mock.patch.object(B, "PIN_FILE_COUNT", n)
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_recovers_from_the_shipped_copy_and_never_uses_the_network(self):
        got = B.recover(self.cfg, self.bundle)
        self.assertEqual(got, B.install_dir(self.cfg))
        self.assertTrue((got / "am" / "final.mdl").exists())

    def test_uses_the_copy_already_in_the_data_folder(self):
        B.recover(self.cfg, self.bundle)
        self.assertEqual(B.recover(self.cfg, Path(self.t.name) / "nothing"), B.install_dir(self.cfg))

    def test_a_changed_copy_is_refused(self):
        (self.bundle / B.MODEL_NAME / "am" / "final.mdl").write_text("tampered")
        self.assertIsNone(B.recover(self.cfg, self.bundle))

    def test_nothing_shipped_gives_none(self):
        self.assertIsNone(B.recover(self.cfg, Path(self.t.name) / "empty"))


if __name__ == "__main__":
    unittest.main()
