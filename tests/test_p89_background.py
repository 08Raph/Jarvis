import tempfile
import unittest
from pathlib import Path

from jarvis.app.background import BackgroundOptions


class BackgroundOptionsTests(unittest.TestCase):
    def test_defaults_keep_running_no_wallpaper(self):
        with tempfile.TemporaryDirectory() as d:
            o = BackgroundOptions(Path(d))
            self.assertEqual(o.get(), {"on_close": "background", "wallpaper": False})
            self.assertTrue(o.keep_running_on_close())

    def test_set_and_persist_and_reject_bad_values(self):
        with tempfile.TemporaryDirectory() as d:
            BackgroundOptions(Path(d)).set(on_close="quit", wallpaper=True)
            o = BackgroundOptions(Path(d))
            self.assertEqual(o.get(), {"on_close": "quit", "wallpaper": True})
            o.set(on_close="explode")
            self.assertEqual(o.get()["on_close"], "quit")
            self.assertFalse(o.keep_running_on_close())

    def test_damaged_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "background.json").write_text("{nope", encoding="utf-8")
            self.assertEqual(BackgroundOptions(Path(d)).get()["on_close"], "background")

    def test_guest_never_keeps_running_or_saves(self):
        with tempfile.TemporaryDirectory() as d:
            o = BackgroundOptions(Path(d), guest=True)
            o.set(on_close="background", wallpaper=True)
            self.assertEqual(o.get(), {"on_close": "quit", "wallpaper": False})
            self.assertFalse((Path(d) / "background.json").exists())


if __name__ == "__main__":
    unittest.main()
