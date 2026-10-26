"""Shared helpers for the Prompt 7 tests (not a test module itself).

LABELS used in the Prompt 7 test files:
  REAL      real files, real processes, the real executor, and (when this Linux machine has one) the REAL sandbox.
  MOCKED    something outside Jarvis is replaced (the internet, DNS, the operating system, the availability of the sandbox).
            Proves Jarvis' decisions only; says nothing about the real internet, Windows or any real service.
  FAKE MODEL  a hand-written list of replies stands in for the language model. Says nothing about how Qwen behaves.
Nothing in these tests was run on Windows. No real model and no real internet were used."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_desktop_control import Harness, ScriptedModel, call      # noqa: E402,F401
from jarvis.desktop.executor import Executor                       # noqa: E402
from jarvis.dev import all_specs                                    # noqa: E402
from jarvis.dev import sandbox as SB                                # noqa: E402
from jarvis.net.policy import NetworkPolicy                         # noqa: E402

SANDBOX_OK, SANDBOX_WHY = SB.availability()
needs_sandbox = unittest.skipUnless(SANDBOX_OK, "no genuine sandbox on this machine: " + str(SANDBOX_WHY))


class P7(Harness):
    """Harness with the Prompt 7 tools registered. `projects` become project folders (must be set before the Env is built)."""

    def make7(self, enable=("files_read", "code_read", "code_edit", "code_run"), projects=None, net=None, plugins=True, **kw):
        self.proj = self.d / "proj"
        self.proj.mkdir(exist_ok=True)
        folders = [str(self.proj)] if projects is None else [str(p) for p in projects]
        self.make(enable=enable, project_folders=folders, **kw)
        if net is not None:
            self.env.net = net
        self.ex = Executor(self.env, self.audit, self._confirm, isolation="inline", extra_specs=all_specs(self.profile if plugins else None))
        return self.ex

    def reload_executor(self):
        self.ex = Executor(self.env, self.audit, self._confirm, isolation="inline", extra_specs=all_specs(self.profile))
        return self.ex

    def tiny_project(self, bug=True):
        (self.proj / "tests").mkdir(exist_ok=True)
        (self.proj / "tests" / "__init__.py").write_text("")
        (self.proj / "calc.py").write_text("def add(a, b):\n    return a - b\n" if bug else "def add(a, b):\n    return a + b\n")
        (self.proj / "tests" / "test_calc.py").write_text(
            "import unittest, calc\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n")
        return self.proj
