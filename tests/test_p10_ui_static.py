"""Prompt 10: checks of the Qt window code that DO NOT need Qt: contrast, syntax on every supported Python, undefined names, f-string portability,
control wiring by source inspection. Real Qt rendering was NOT possible here; the one test that builds the window is opt-in and is SKIPPED without
PySide6 (it is then the first thing to run on Windows: `python -m unittest tests.test_p10_ui_static` or `jarvis-gui.exe --selftest`)."""
import ast
import builtins
import io
import os
import py_compile
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path

from p10_common import ROOT
from jarvis.ui import theme


class ThemeTests(unittest.TestCase):
    def test_every_pair_meets_its_contrast_minimum_in_both_themes(self):
        for dark in (False, True):
            p = theme.palette(dark)
            for fg, bg, minimum in theme.PAIRS:
                self.assertGreaterEqual(theme.contrast(p[fg], p[bg]), minimum, f"{'dark' if dark else 'light'}: {fg} on {bg}")

    def test_contrast_function_known_values(self):
        self.assertAlmostEqual(theme.contrast("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(theme.contrast("#777777", "#777777"), 1.0, places=2)

    def test_message_kinds_have_distinct_backgrounds(self):
        k = theme.kind_colors(theme.palette(False))
        self.assertEqual(set(k), {"user", "assistant", "tool", "error", "confirm", "system"})
        self.assertEqual(len({v[0] for v in k.values()}), 6)

    def test_text_scaling_changes_font_size_and_focus_is_styled(self):
        s100 = theme.stylesheet(theme.palette(False), 1.0)
        s150 = theme.stylesheet(theme.palette(False), 1.5)
        self.assertIn("10.0pt", s100)
        self.assertIn("15.0pt", s150)
        self.assertIn(":focus", s100)
        self.assertEqual(list(theme.SCALES), ["100%", "125%", "150%"])


def undefined_names(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = set(dir(builtins)) | {"__file__", "__name__"}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            if not isinstance(n, ast.ClassDef):
                a = n.args
                for x in a.posonlyargs + a.args + a.kwonlyargs:
                    bound.add(x.arg)
                for x in (a.vararg, a.kwarg):
                    if x:
                        bound.add(x.arg)
        elif isinstance(n, ast.Lambda):
            a = n.args
            for x in a.posonlyargs + a.args + a.kwonlyargs:
                bound.add(x.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
    return sorted({(n.id, n.lineno) for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound})


class WindowCodeWithoutQt(unittest.TestCase):
    FILES = [ROOT / "src" / "jarvis" / "ui" / n for n in ("window.py", "app.py", "theme.py")]

    def test_compiles(self):
        for f in self.FILES + [ROOT / "packaging" / "entry_gui.py", ROOT / "packaging" / "manifest.py"]:
            with tempfile.TemporaryDirectory() as d:
                py_compile.compile(str(f), cfile=str(Path(d) / "x.pyc"), doraise=True)

    def test_no_undefined_names(self):
        """Catches typos in Qt code that is never executed here. A flat check (no scope analysis): it cannot prove the code is right."""
        for f in self.FILES:
            self.assertEqual(undefined_names(f), [], f.name)

    def test_window_uses_only_controller_for_actions(self):
        t = (ROOT / "src" / "jarvis" / "ui" / "window.py").read_text(encoding="utf-8")
        for banned in ("subprocess", "os.system", "import sqlite3", "MissionStore", "PersonalMemory", "Executor(", "requests", "urllib"):
            self.assertFalse(banned in t, f"window.py contains {banned}: business logic belongs in jarvis.app.controller")

    def test_every_button_is_connected_or_disabled(self):
        """Source inspection: each QPushButton created in window.py is either connected, set disabled, or used as a menu/toggle."""
        t = (ROOT / "src" / "jarvis" / "ui" / "window.py").read_text(encoding="utf-8")
        tree = ast.parse(t)
        made = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and getattr(n.value.func, "attr", getattr(n.value.func, "id", "")) in ("QPushButton", "_button", "button"):
                for tg in n.targets:
                    made[ast.unparse(tg)] = n.lineno
        self.assertTrue(made, "expected buttons in window.py")
        for name, line in made.items():
            wired = (f"{name}.clicked" in t) or (f"{name}.setEnabled(False)" in t) or (f"{name}.setCheckable" in t) or (f"{name}.toggled" in t) or (f"{name}.setMenu" in t)
            self.assertTrue(wired, f"button {name} (line {line}) is neither connected nor marked unavailable")


def nested_same_quote_fstrings(path: Path) -> list:
    """Python 3.12+ allows re-using the same quote inside an f-string expression; 3.11 (which this project supports) does not. Tokenizer-based, so it
    only works when run on 3.12+ (on 3.11 the file would not even compile, which is the failure it prevents)."""
    if sys.version_info < (3, 12):
        return []
    bad, stack = [], []
    with open(path, "rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type == tokenize.FSTRING_START:
                q = tok.string[-1] if tok.string[-3:] not in ('"""', "'''") else tok.string[-3:]
                if stack and stack[-1] == q:
                    bad.append((tok.start[0], q))
                stack.append(q)
            elif tok.type == tokenize.FSTRING_END:
                stack.pop()
            elif tok.type == tokenize.STRING and stack:
                q = tok.string.lstrip("rbuRBU")[0]
                if q == stack[-1]:
                    bad.append((tok.start[0], q))
    return bad


class Python311Portability(unittest.TestCase):
    def test_no_nested_same_quote_fstrings_in_shipped_code(self):
        problems = {}
        for base in ("src", "packaging", "scripts"):
            for f in (ROOT / base).rglob("*.py"):
                b = nested_same_quote_fstrings(f)
                if b:
                    problems[str(f.relative_to(ROOT))] = b
        self.assertEqual(problems, {})

    def test_the_checker_itself_detects_the_pattern(self):
        if sys.version_info < (3, 12):
            self.skipTest("tokenizer-based check needs 3.12+")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.py"
            f.write_text('d = {"a": 1}\nprint(f"{d["a"]}")\n', encoding="utf-8")
            self.assertTrue(nested_same_quote_fstrings(f))
            f.write_text("d = {'a': 1}\nprint(f\"{d['a']}\")\n", encoding="utf-8")
            self.assertEqual(nested_same_quote_fstrings(f), [])


@unittest.skipUnless(os.environ.get("JARVIS_QT_TEST") == "1", "opt-in: set JARVIS_QT_TEST=1 (needs PySide6)")
class QtSmoke(unittest.TestCase):
    def test_offscreen_selftest(self):
        try:
            import PySide6  # noqa: F401
        except ImportError:
            self.skipTest("PySide6 not installed")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from jarvis.ui import app
        self.assertEqual(app.selftest(), 0)


if __name__ == "__main__":
    unittest.main()
