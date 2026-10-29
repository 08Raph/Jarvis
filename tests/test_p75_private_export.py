"""Regression: files that hold exported personal data are created owner-only (POSIX), including when overwriting a wider-permission file."""
import json, os, stat, subprocess, sys, tempfile, unittest
from pathlib import Path

from p41_common import *  # noqa: F401,F403  (puts src on the path like the other tests)
from jarvis.security.private_file import write_private_text


@unittest.skipIf(os.name == "nt", "POSIX permission bits; on Windows the file inherits the profile folder's ACL (not tested here)")
class OwnerOnly(unittest.TestCase):
    def test_new_file_is_0600(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_private_text(Path(d) / "x.json", "{}")
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_existing_wide_file_is_tightened_and_content_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"; p.write_text("old"); os.chmod(p, 0o644)
            write_private_text(p, "new")
            self.assertEqual(p.read_text(), "new"); self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_the_memory_export_command_uses_it(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            out = Path(d) / "out.json"
            r = subprocess.run([sys.executable, "-m", "jarvis", "--data-dir", d, "memory", "export", "--out", str(out)], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("memories", json.loads(out.read_text(encoding="utf-8")))
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
