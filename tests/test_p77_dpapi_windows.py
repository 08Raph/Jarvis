"""REAL Windows DPAPI round trip for the saved sign-in token. Runs only on Windows (the Windows CI job); on Linux it is skipped, and the logic around it is tested with a
labelled stand-in in test_p77_account_controller. Before the first Windows CI run the DPAPI code had NEVER been executed."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis.online.tokenstore import TokenStore  # noqa: E402


@unittest.skipUnless(os.name == "nt", "DPAPI exists only on Windows: skipped here, runs on the Windows CI job")
class RealDpapi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "online.token"

    def test_the_token_is_encrypted_on_disk_and_comes_back(self):
        tok = "tok-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-abcdefg"
        s = TokenStore(self.path)
        self.assertTrue(s.windows)
        self.assertTrue(s.save(tok))
        raw = self.path.read_text(encoding="ascii")
        self.assertTrue(raw.startswith("dpapi:"))
        self.assertNotIn(tok, raw)
        self.assertNotIn(tok.encode().hex(), raw)
        self.assertEqual(TokenStore(self.path).load(), tok)               # a new object (a restarted app) reads it back

    def test_a_damaged_file_is_ignored_not_trusted(self):
        s = TokenStore(self.path)
        s.save("secret-token-value-123456789012345678901234")
        raw = self.path.read_text(encoding="ascii")
        self.path.write_text(raw[:-6] + "00ff00", encoding="ascii")
        self.assertIsNone(TokenStore(self.path).load())

    def test_a_plain_file_is_never_accepted_on_windows(self):
        self.path.write_text("plain:not-encrypted-token", encoding="ascii")
        self.assertIsNone(TokenStore(self.path).load())

    def test_clear_removes_it(self):
        s = TokenStore(self.path)
        s.save("secret-token-value-123456789012345678901234")
        s.clear()
        self.assertFalse(self.path.exists())
        self.assertIsNone(s.load())


if __name__ == "__main__":
    unittest.main()
