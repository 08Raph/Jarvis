"""Prompt 7: regression checks that earlier behaviour is unchanged, plus the extension rules of the executor.

REAL: the real desktop tools and executor on this Linux machine with the SIMULATED desktop, and the real memory store. The complete earlier suite
(Stage D baseline: 621 passing, 2 skipped) is the main regression evidence and is run separately with the established command.
Nothing here proves Windows behaviour, a real model, or the internet."""
import json
import unittest
from pathlib import Path

from p7_common import P7, ROOT, ScriptedModel, call  # noqa: F401
from jarvis.desktop import schema as S
from jarvis.desktop.executor import Executor
from jarvis.desktop.policy import CATEGORIES, Settings
from jarvis.desktop.tools import SPECS
from jarvis.memory.personal import PersonalMemory
from jarvis.net import policy as NP

# the built-in tool names at Stage D (frozen on purpose: removing or renaming one must be a conscious decision)
STAGE_D_TOOLS = {"check_visible", "copy_path", "create_folder", "delete_path", "edit_file", "file_info", "file_search", "focus_window", "launch_app", "list_apps",
                 "list_windows", "mouse_click", "move_path", "press_keys", "read_file", "read_screen_text", "rename_path", "screenshot", "system_info",
                 "type_text", "ui_act", "ui_find", "ui_read", "undo", "wait", "wait_for_window", "write_file"}


class ExtensionRulesTests(P7):
    def test_stage_d_tool_set_is_still_present(self):
        self.assertEqual({sp.name for sp in SPECS}, STAGE_D_TOOLS)

    def test_extensions_cannot_replace_builtin_tools_or_use_forbidden_risks_or_unknown_categories(self):
        self.make7()
        base = [s for s in SPECS if s.name == "read_file"][0]
        import dataclasses
        for bad, why in [(dataclasses.replace(base), "exists"), (dataclasses.replace(base, name="zz_admin", risk=S.ADMIN), "risk"),
                         (dataclasses.replace(base, name="zz_ext", risk=S.EXTERNAL), "risk"), (dataclasses.replace(base, name="zz_cat", category="godmode"), "category"),
                         (dataclasses.replace(base, name="zz_nohandler", handler=None), "handler")]:
            with self.assertRaises(ValueError, msg=why):
                self.ex._add_spec(bad)

    def test_builtin_only_executor_has_none_of_the_new_tools(self):
        self.make()
        for n in ("run_tests", "run_code", "edit_code", "web_fetch", "web_search", "read_document", "repo_overview"):
            self.assertNotIn(n, self.ex.specs)
            self.assertEqual(self.run_tool(n).status, "invalid")

    def test_existing_file_tools_still_behave_with_extensions_loaded(self):
        self.make7(enable=("files_read", "files_write"))
        r = self.run_tool("write_file", path="a.txt", content="hello")
        self.assertEqual(r.status, "verified")
        self.assertEqual(self.run_tool("read_file", path="a.txt").outcome.untrusted_text, "hello")
        self.assertEqual(self.run_tool("write_file", path="a.txt", content="again").status, "failed")           # no silent overwrite
        d = self.run_tool("write_file", path="a.txt", content="again", overwrite=True)
        self.assertEqual(d.status, "verified")
        self.assertTrue(d.outcome.checkpoint)
        self.assertEqual(self.run_tool("undo", checkpoint=d.outcome.checkpoint).status, "verified")
        self.assertEqual((self.work / "a.txt").read_text(), "hello")

    def test_existing_protections_still_hold_with_extensions_loaded(self):
        self.make7(enable=("files_read", "files_write"))
        outside = self.d / "out.txt"
        outside.write_text("x")
        self.assertEqual(self.run_tool("read_file", path=str(outside)).status, "denied")
        self.assertIn(self.run_tool("write_file", path="../escape.txt", content="x").status, ("denied", "failed"))
        self.assertFalse((self.d / "escape.txt").exists())

    def test_the_ordinary_file_tools_still_refuse_source_files_that_only_the_code_tools_may_touch(self):
        self.make7(enable=("files_read", "files_write", "code_read"), projects=[self.work])
        (self.work / "mod.py").write_text("x = 1\n")
        r = self.run_tool("write_file", path=str(self.work / "mod.py"), content="x = 2\n", overwrite=True)
        self.assertNotEqual(r.status, "verified")
        self.assertEqual((self.work / "mod.py").read_text(), "x = 1\n")
        self.assertEqual(self.run_tool("read_lines", path=str(self.work / "mod.py")).status, "verified")

    def test_new_risks_always_confirm_and_nothing_gets_around_it(self):
        self.assertIn(S.EXECUTE, S.ALWAYS_CONFIRM)
        self.assertIn(S.NETWORK, S.ALWAYS_CONFIRM)
        self.make7(enable=("files_read", "plugins", "code_read", "code_edit", "code_run", "research_online"), net=None)
        for s in self.ex.specs.values():
            if s.category in ("code_run", "research_online", "plugins"):
                self.assertIn(s.risk, (S.EXECUTE, S.NETWORK), s.name)
            self.assertNotIn(s.risk, (S.ADMIN, S.EXTERNAL), s.name)

    def test_settings_roundtrip_keeps_project_folders_and_drops_bad_ones(self):
        s = Settings()
        proj = self.d / "p"
        proj.mkdir()
        s.project_folders = [str(proj)]
        back = Settings.from_dict(s.to_dict())
        self.assertEqual(back.project_folders, [str(proj)])
        bad = s.to_dict()
        bad["project_folders"] = ["/", "C:\\", str(proj), 5, None, "relative/path"]
        self.assertEqual(Settings.from_dict(bad).project_folders, [str(proj)])

    def test_hand_edited_settings_cannot_turn_on_the_new_categories_by_accident(self):
        s = Settings.from_dict({"enabled": {"code_run": "yes", "plugins": 1, "research_online": "true", "godmode": True}})
        for c in ("code_run", "plugins", "research_online"):
            self.assertIsNot(s.enabled.get(c), True, c)
        self.assertNotIn("godmode", s.enabled)

    def test_the_emergency_stop_still_stops_extension_tools(self):
        self.make7()
        self.tiny_project()
        self.env.stop.stop("test")
        for n, a in [("repo_overview", {"path": str(self.proj)}), ("run_code", {"code": "print(1)"})]:
            self.assertEqual(self.run_tool(n, **a).status, "stopped", n)
        self.assertEqual(self.asked, [])

    def test_extension_tool_calls_are_audited(self):
        self.make7()
        self.tiny_project()
        self.run_tool("repo_overview", path=str(self.proj))
        self.run_tool("edit_code", path=str(self.proj / "calc.py"), old_text="a - b", new_text="a + b")
        log = (self.profile / "audit.jsonl").read_text()
        self.assertIn("repo_overview", log)
        self.assertIn("edit_code", log)


class MemoryAndHonestyTests(unittest.TestCase):
    def test_personal_memory_still_works(self):
        m = PersonalMemory.guest()
        m.add("project", "Prompt 7 adds coding tools", source="user_said", confidence=0.9)
        self.assertTrue(m.search("coding tools"))

    def test_the_reserved_web_search_feature_still_has_no_implementation(self):
        self.assertIn("web_search", NP.FEATURES)
        for f in (ROOT / "src" / "jarvis").rglob("*.py"):
            if f.name == "policy.py" and "net" in f.parts:
                continue
            self.assertNotIn('open("web_search"', f.read_text(), str(f))

    def test_STATIC_docs_do_not_claim_windows_real_model_or_security_verification(self):
        review = (ROOT / "docs" / "PROMPT7_REVIEW.md")
        self.assertTrue(review.exists())
        t = review.read_text().lower()
        self.assertIn("not run on windows", t)
        self.assertIn("real qwen", t)
        self.assertIn("not a security", t)
        for bad in ("production-ready", "production ready", "fully secure", "guaranteed safe", "windows-verified"):
            self.assertNotIn(bad, t.replace("not " + bad, ""))


if __name__ == "__main__":
    unittest.main()
