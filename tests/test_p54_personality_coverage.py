"""Integration tests (FAKE MODEL, real application code): all 17 personalities are reachable through every application pathway, switching keeps history,
and no personality gains a permission. These prove wiring, NOT that any personality sounds distinct or good."""
import ast
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402

from jarvis import cli  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.assistant import LocalAssistant  # noqa: E402
from jarvis.personality.persona import Persona  # noqa: E402
from jarvis.personality.spec import SPECS  # noqa: E402
from jarvis.personality.styles import STYLES  # noqa: E402
from restriction_lifted import setUpModule, tearDownModule  # noqa: E402,F401  (these tests exercise the dormant adult definitions)

ALL = list(STYLES)
ADULT = {"savage", "flirty", "sexy", "romantic"}


class Reachability(AppCase):
    def test_all_17_have_explicit_specs_and_are_listed(self):
        self.assertEqual(len(ALL), 17)
        self.assertEqual(set(SPECS), set(ALL))
        self.assertEqual([d["id"] for d in Persona(None, persist=False).listing()], ALL)

    def test_reachable_through_persona_api_and_controller(self):
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        for sid in ALL:
            if sid in ADULT:
                with self.assertRaises(ValueError):
                    c.persona_set(sid)
        c.persona_adult(True, confirmed=True)
        for sid in ALL:
            c.persona_set(sid)
            self.assertEqual(c.persona_state()["settings"]["style"], sid)
            self.assertEqual(c.persona_preview(sid)["id"], sid)

    def test_reachable_through_the_cli(self):
        def run(*a):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                return cli.main(["--data-dir", str(self.cfg.data_dir), "persona", *a])
        for sid in ADULT:
            self.assertEqual(run("set", sid), 2, sid)
        self.assertEqual(run("adult", "on", "--confirm-adult"), 0)
        for sid in ALL:
            self.assertEqual(run("set", sid), 0, sid)
            self.assertEqual(Persona(self.cfg.profile_dir).settings.style, sid)

    def test_each_style_reaches_the_model_with_its_own_spec(self):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = "ok"
        a = LocalAssistant(self.cfg, engine_factory=fake_engine_factory, guest=True)
        self.addCleanup(a.close)
        a.persona.set_adult_optin(True)
        for sid in ALL:
            a.persona.set_style(sid)
            a.reply("tell me something")
            self.assertIn(f"({sid}):", FakeLlama.instances[0].calls[-1][0]["content"], sid)

    def test_switching_through_all_17_keeps_every_earlier_message(self):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = "ok"
        a = LocalAssistant(self.cfg, engine_factory=fake_engine_factory, guest=True, max_history_messages=200)
        self.addCleanup(a.close)
        a.persona.set_adult_optin(True)
        users = []
        for i, sid in enumerate(ALL):
            a.persona.set_style(sid)
            users.append(f"message number {i}")
            a.reply(users[-1])
            sent = [m["content"] for m in FakeLlama.instances[0].calls[-1] if m["role"] == "user"]
            self.assertEqual(sent, users, f"history lost after switching to {sid}")
        self.assertEqual(len(FakeLlama.instances), 1, "no model reload on a personality switch")


class NoExtraPermissions(unittest.TestCase):
    def test_personality_package_imports_no_tool_executor_policy_or_network_code(self):
        root = Path(__file__).resolve().parents[1] / "src/jarvis/personality"
        banned = ("jarvis.desktop", "jarvis.missions", "jarvis.tools", "jarvis.executor", "jarvis.policy", "jarvis.net", "socket", "urllib", "http", "requests", "subprocess", "os.system")
        for f in sorted(root.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [n.name for n in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for n in names:
                    for b in banned:
                        self.assertFalse(n == b or n.startswith(b + "."), f"{f.name} imports {n}")

    def test_assistant_exposes_no_tool_or_approval_hook_that_a_style_could_use(self):
        for name in ("executor", "run_tool", "tools", "approve", "grant", "confirm"):
            self.assertFalse(hasattr(LocalAssistant, name), name)

    def test_every_style_prompt_ends_with_the_fixed_safety_block_and_states_no_new_abilities(self):
        p = Persona(None, persist=False)
        p.set_adult_optin(True)
        for sid in ALL:
            p.set_style(sid)
            g = p.observe("hello").guidance
            self.assertIn("does not give you any new ability or permission", g)
            self.assertIn("cannot approve or run any action", g)


if __name__ == "__main__":
    unittest.main()
