"""Quick launch beyond programs: personal folders, Windows Settings pages and ordinary files. FAKES only: temporary folders stand in for the user's profile; nothing is opened."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase  # noqa: E402,F401
from jarvis.app import quick_launch as QL  # noqa: E402
from jarvis.desktop import places  # noqa: E402


class PlacesTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        for n in ("Downloads", "Documents", "Pictures"):
            (self.home / n).mkdir()
        self.roots = [self.home / n for n in ("Downloads", "Documents", "Pictures")]
        (self.home / "Documents" / "tax return 2025.pdf").write_text("x")
        (self.home / "Documents" / "setup.exe").write_text("x")
        (self.home / "Downloads" / "run me.ps1").write_text("x")
        (self.home / "Pictures" / "beach.png").write_text("x")
        (self.home / "outside.txt").write_text("x")

    def tearDown(self):
        self.td.cleanup()

    def test_folder_entry(self):
        e = places.folder_entry("downloads", self.roots)
        self.assertEqual(e["kind"], "folder")
        self.assertEqual(Path(e["path"]).name, "Downloads")
        self.assertIsNone(places.folder_entry("windows", self.roots))
        self.assertIsNone(places.folder_entry("videos", self.roots))      # not present on this fake profile

    def test_settings_only_ms_settings(self):
        e = places.settings_entry("wifi")
        self.assertTrue(places.allowed_settings_uri(e["uri"]))
        for bad in ("http://x", "ms-settings:a b", "cmd:calc", "ms-settings:x;calc", "", "file:///c:/windows"):
            self.assertFalse(places.allowed_settings_uri(bad), bad)
        self.assertTrue(all(places.allowed_settings_uri(v) for v in places.SETTINGS.values()))

    def test_files_found_and_programs_refused(self):
        hits = places.find_files("tax return", self.roots)
        self.assertEqual([h["name"] for h in hits], ["tax return 2025.pdf"])
        self.assertEqual(places.find_files("setup", self.roots), [])          # .exe never offered
        self.assertEqual(places.find_files("run me", self.roots), [])         # .ps1 never offered
        self.assertEqual(places.find_files("outside", self.roots), [])        # outside personal folders
        self.assertEqual(places.find_files("", self.roots), [])

    def test_file_allowed_rules(self):
        self.assertTrue(places.file_allowed(self.home / "Pictures" / "beach.png", self.roots))
        self.assertFalse(places.file_allowed(self.home / "Documents" / "setup.exe", self.roots))
        self.assertFalse(places.file_allowed(self.home / "outside.txt", self.roots))
        self.assertFalse(places.file_allowed(self.home / "Documents" / "missing.pdf", self.roots))

    def test_symlink_not_followed(self):
        link = self.home / "Documents" / "link.txt"
        try:
            link.symlink_to(self.home / "outside.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertFalse(places.file_allowed(link, self.roots))
        self.assertEqual(places.find_files("link", self.roots), [])

    def test_deny_list_covers_dangerous_types(self):
        for s in (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".lnk", ".url", ".reg", ".js", ".dll"):
            self.assertIn(s, places.DENY_SUFFIXES)

    def test_resolve_prefers_programs_then_places(self):
        idx = [{"id": "exe:notepad", "name": "notepad", "kind": "exe", "path": "C:/n.exe", "target": "C:/n.exe", "source": "x"}]
        self.assertEqual(QL.resolve(idx, "notepad")[1]["id"], "exe:notepad")
        kind, e = QL.resolve(idx, "bluetooth settings")
        self.assertEqual((kind, e["kind"]), ("app", "settings"))


if __name__ == "__main__":
    unittest.main()
