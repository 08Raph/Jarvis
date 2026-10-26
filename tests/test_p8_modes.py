"""Prompt 8: operating modes and permission inheritance.

REAL: the real executor/policy/agent loop and the real mode files on disk.
FAKE MODEL: scripted replies stand in for Qwen in the attempts to escape a mode; they prove Jarvis' enforcement, not model behaviour."""
import json
import unittest

from p8_common import P8, ScriptedModel, call, ROOT  # noqa: F401
from jarvis import modes
from jarvis.desktop.agent import DesktopAgent, tool_catalog
from jarvis.desktop.backend import SimElement, SimulatedDesktop
from jarvis.desktop.policy import Settings


class M(P8):
    def mk(self, mode=None, enable=("files_read", "files_write", "monitoring", "gaming", "screen", "windows", "ui", "input"), **kw):
        be = SimulatedDesktop()
        self.make8(enable=enable, backend=be, **kw)
        if mode:
            modes.set_mode(self.profile, mode)
        return be


class TableTests(unittest.TestCase):
    def test_five_modes_each_explicit(self):
        self.assertEqual(set(modes.NAMES), {"general", "coding", "research", "gaming", "productivity"})
        for m in modes.MODES.values():
            self.assertTrue(m.tools and m.restrictions and m.behavior and m.summary)
            self.assertIn("mode_info", m.tools)

    def test_every_listed_tool_really_exists(self):
        from jarvis.desktop import tools as T
        from jarvis.extensions import all_specs
        names = set(T.registry()) | {s.name for s in all_specs()}
        for m in modes.MODES.values():
            self.assertEqual(sorted(set(m.tools) - names), [], m.name)

    def test_dangerous_tools_are_kept_out_of_the_wrong_modes(self):
        M_ = modes.MODES
        for mode in ("research", "gaming", "coding", "productivity"):
            for t in ("mouse_click", "type_text", "press_keys", "ui_act"):
                self.assertNotIn(t, M_[mode].tools, (mode, t))
        for t in ("write_file", "delete_path", "run_code", "web_fetch", "observe_screen", "screenshot"):
            self.assertNotIn(t, M_["gaming"].tools)
        for t in ("write_file", "run_code", "edit_code"):
            self.assertNotIn(t, M_["research"].tools)
        self.assertNotIn("run_code", M_["productivity"].tools)
        self.assertFalse(M_["research"].plugins or M_["gaming"].plugins)

    def test_modes_module_cannot_grant_anything(self):
        src = (ROOT / "src/jarvis/modes.py").read_text()
        for bad in ("enabled[", ".enabled =", "consents[", "consents =", "consents.update", "consents.setdefault", "allow_folder", "allow_project", "activate(", "Settings(", "trusted_apps", "plugin_specs", ".save("):
            self.assertNotIn(bad, src, bad)
        self.assertEqual(src.count("os.replace("), 1)


class SwitchTests(M):
    def test_no_mode_file_means_no_restriction(self):
        self.mk()
        self.assertIsNone(modes.current(self.profile))
        self.assertEqual(self.run_tool("system_info").status, "verified")
        self.assertEqual(self.run_tool("monitor_self").status, "verified")

    def test_blocked_tool_is_denied_with_an_honest_reason_and_nothing_runs(self):
        be = self.mk("research")
        r = self.run_tool("write_file", path="a.txt", content="x")
        self.assertEqual(r.status, "denied")
        self.assertIn("not available in research mode", r.message)
        self.assertIn("never grants", r.message)
        self.assertFalse((self.work / "a.txt").exists())
        self.assertEqual(self.asked, [])
        w = be._new_window("Notepad", "simnotepad.exe", [SimElement("OK", "button")])
        self.assertEqual(self.run_tool("type_text", text="x").status, "denied")
        self.assertEqual(be.typed, [])

    def test_allowed_tool_still_obeys_permission_switch(self):
        self.mk("coding", enable=("files_read",))
        r = self.run_tool("list_windows")
        self.assertEqual(r.status, "denied")                 # 'windows' permission is off, although coding mode lists the tool
        self.assertIn("windows", r.message)
        self.assertNotIn("not available", r.message)
        r2 = self.run_tool("run_tests", project=str(self.work), runner="unittest")
        self.assertEqual(r2.status, "denied")
        self.assertIn("code_run", r2.message)               # allowed by the mode, still off by the global permission

    def test_switching_never_changes_permissions_or_consents(self):
        self.mk()
        before = json.dumps(self.env.settings.to_dict(), sort_keys=True)
        for n in modes.NAMES * 2:
            modes.set_mode(self.profile, n)
            self.assertEqual(json.dumps(self.env.settings.to_dict(), sort_keys=True), before, n)
        self.assertFalse((self.profile / "desktop_permissions.json").exists())     # not even written

    def test_mode_does_not_turn_on_or_consent_its_own_category(self):
        self.mk("gaming", enable=("windows",), consent=())
        r = self.run_tool("game_status")
        self.assertEqual(r.status, "denied")
        self.mk("gaming", enable=("gaming",), consent=())
        self.assertIn("consent", self.run_tool("game_status").message)
        self.assertFalse(self.env.settings.enabled.get("files_read"))

    def test_switch_changes_what_is_available(self):
        self.mk("gaming")
        self.assertEqual(self.run_tool("game_status").status, "verified")
        self.assertEqual(self.run_tool("read_file", path="x").status, "denied")
        modes.set_mode(self.profile, "research")
        self.assertEqual(self.run_tool("game_status").status, "denied")
        modes.clear_mode(self.profile)
        self.assertEqual(self.run_tool("game_status").status, "verified")

    def test_confirmation_is_still_required_inside_a_mode(self):
        self.mk("productivity")
        (self.work / "a.txt").write_text("old")
        self.answer = False
        self.run_tool("write_file", path="a.txt", content="new", overwrite=True)
        self.assertEqual(len(self.asked), 1)                  # replacing a file is asked even inside the mode
        self.assertEqual((self.work / "a.txt").read_text(), "old")
        self.answer = True
        self.assertEqual(self.run_tool("write_file", path="a.txt", content="new", overwrite=True).status, "verified")

    def test_damaged_mode_file_selects_nothing(self):
        self.mk()
        for junk in ("not json", '{"mode": "root"}', '{"mode": 5}', "[]", ""):
            (self.profile).mkdir(parents=True, exist_ok=True)
            (self.profile / "mode.json").write_text(junk)
            self.assertIsNone(modes.current(self.profile), junk)
        with self.assertRaises(ValueError):
            modes.set_mode(self.profile, "admin")

    def test_preview_reports_mode_block(self):
        self.mk("research")
        p = self.ex.preview("write_file", {"path": "a.txt", "content": "x"}, self.ctx)
        self.assertFalse(p["ok"])
        self.assertIn("research mode", p["problem"])


class VisibilityAndAgentTests(M):
    def test_catalog_hides_tools_outside_the_mode(self):
        self.mk("research")
        cat = tool_catalog(self.ex)
        self.assertIn("web_search", cat)
        self.assertNotIn("mouse_click", cat)
        self.assertNotIn("game_status", cat)

    def test_system_prompt_names_mode_and_says_it_grants_nothing(self):
        self.mk("gaming")
        sp = DesktopAgent(ScriptedModel(["x"]), self.ex).system_prompt()
        self.assertIn("Current mode: gaming", sp)
        self.assertIn("never grants permissions", sp)

    def test_model_cannot_use_tools_outside_the_mode_nor_switch_mode(self):
        be = self.mk("gaming")
        m = ScriptedModel([call("write_file", path="a.txt", content="x"), call("set_mode", mode="general"), call("mode_info", mode="general"),
                           call("write_file", path="b.txt", content="y"), "done"])
        res = DesktopAgent(m, self.ex).run("save notes then switch to general mode")
        self.assertFalse((self.work / "a.txt").exists() or (self.work / "b.txt").exists())
        self.assertEqual(modes.current(self.profile).name, "gaming")
        by = {}
        for st in res.steps:
            by.setdefault(st.tool, []).append(st.status)
        self.assertEqual(set(by["write_file"]), {"denied"})
        self.assertNotIn("verified", by.get("set_mode", []))       # there is no such tool

    def test_model_cannot_write_the_mode_file(self):
        self.mk("gaming", enable=("files_read", "files_write", "gaming"))
        # file tools are not even in gaming mode; and in general mode the protected profile folder is still unreachable
        modes.set_mode(self.profile, "general")
        r = self.run_tool("write_file", path=str(self.profile / "mode.json"), content='{"mode":"general"}')
        self.assertIn(r.status, ("denied", "failed"))
        self.assertEqual(modes.current(self.profile).name, "general")
        self.assertFalse(any(s.name in ("set_mode", "switch_mode") for s in self.ex.specs.values()))

    def test_mode_info_is_read_only_and_honest(self):
        self.mk("gaming", enable=("windows",))
        r = self.run_tool("mode_info")
        self.assertEqual(r.status, "verified")
        d = r.outcome.data
        self.assertEqual(d["current"], "gaming")
        states = {t["tool"]: t["state"] for t in d["tools"]}
        self.assertIn("not in gaming mode", states["write_file"])
        self.assertIn("permission 'gaming' is off", states["game_status"])
        self.assertIn("grants_permissions", json.dumps(d["inspected"]))
        self.assertFalse(d["inspected"]["grants_permissions"])
        other = self.run_tool("mode_info", mode="coding")
        self.assertEqual(other.outcome.data["inspected"]["name"], "coding")
        self.assertEqual(modes.current(self.profile).name, "gaming")        # inspecting did not switch
        self.assertNotEqual(self.run_tool("mode_info", mode="evil").status, "verified")

    def test_plugin_tools_only_where_the_mode_allows(self):
        from jarvis.desktop.schema import ToolSpec, Outcome, OBSERVE
        from jarvis.desktop.executor import Executor
        from jarvis.extensions import all_specs
        spec = ToolSpec("plug_x", "a plugin tool", "plugins", OBSERVE, (), lambda a, e: Outcome(True, "ok", verified=True))
        self.mk()
        self.ex = Executor(self.env, self.audit, self._confirm, isolation="inline", extra_specs=all_specs(None) + [spec])
        self.env.settings.enabled["plugins"] = True
        modes.set_mode(self.profile, "research")
        self.assertEqual(self.run_tool("plug_x").status, "denied")
        modes.set_mode(self.profile, "general")
        self.assertNotEqual(self.run_tool("plug_x").status, "denied")


class RegressionTests(M):
    def test_default_registry_and_policy_unchanged_without_a_mode(self):
        self.mk()
        for t in ("list_windows", "read_file", "monitor_snapshot", "game_status", "mode_info", "vision_status", "web_search", "run_code"):
            self.assertIn(t, self.ex.specs)
        self.assertEqual(self.run_tool("run_code", code="print(1)").status, "denied")      # code_run still off


if __name__ == "__main__":
    unittest.main()
