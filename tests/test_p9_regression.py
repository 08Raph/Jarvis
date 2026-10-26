"""Prompt 9: regression guards for Prompt 6/7/8 behaviour. The complete earlier suite (Prompt 8 baseline) is the main evidence and is run separately; these tests
freeze the facts that adding missions must not change. REAL code, SIMULATED desktop. Nothing here ran on Windows."""
import unittest

from p9_common import P9, ROOT  # noqa: F401
from jarvis import modes
from jarvis.desktop import schema as S
from jarvis.desktop.policy import ALWAYS_ASK_TOOLS, CATEGORIES, CONSENT_REQUIRED, DEFAULT_ON, Settings
from jarvis.desktop.tools import SPECS
from jarvis.extensions import all_specs

# frozen at the Prompt 8 archive (name -> (category, risk)); changing one must be a conscious decision
BUILTIN = {'system_info': ('system', 'observe'), 'wait': ('system', 'observe'), 'file_search': ('files_read', 'observe'), 'file_info': ('files_read', 'observe'),
           'read_file': ('files_read', 'observe'), 'create_folder': ('files_write', 'modify'), 'write_file': ('files_write', 'modify'), 'edit_file': ('files_write', 'modify'),
           'copy_path': ('files_write', 'modify'), 'move_path': ('files_write', 'modify'), 'rename_path': ('files_write', 'modify'), 'delete_path': ('files_write', 'destructive'),
           'undo': ('files_write', 'modify'), 'list_apps': ('apps', 'observe'), 'launch_app': ('apps', 'launch'), 'list_windows': ('windows', 'observe'),
           'focus_window': ('windows', 'modify'), 'wait_for_window': ('windows', 'observe'), 'ui_find': ('ui', 'observe'), 'ui_read': ('ui', 'observe'), 'ui_act': ('ui', 'input'),
           'type_text': ('input', 'input'), 'press_keys': ('input', 'input'), 'mouse_click': ('input', 'input'), 'screenshot': ('screen', 'observe'),
           'read_screen_text': ('screen', 'observe'), 'check_visible': ('ui', 'observe')}
PROMPT8_EXT = {'analyze_error', 'cite_sources', 'compare_sources', 'diff_snapshot', 'edit_code', 'find_on_screen', 'game_compare', 'game_fps_import', 'game_metrics',
               'game_profile_get', 'game_profile_list', 'game_profile_save', 'game_status', 'git_info', 'list_sources', 'mode_info', 'monitor_processes', 'monitor_self',
               'monitor_snapshot', 'monitor_watch', 'observe_screen', 'read_document', 'read_lines', 'recheck_screen', 'repo_overview', 'run_code', 'run_tests', 'search_code',
               'snapshot_project', 'undo_code_edit', 'verify_fix', 'verify_quote', 'vision_status', 'web_fetch', 'web_search', 'write_code'}


class Frozen(P9):
    def test_builtin_tools_unchanged(self):
        self.assertEqual({s.name: (s.category, s.risk) for s in SPECS}, BUILTIN)

    def test_prompt9_adds_exactly_one_extension_tool(self):
        import tempfile
        from pathlib import Path
        names = {s.name for s in all_specs(Path(tempfile.mkdtemp()))}
        self.assertEqual(names - PROMPT8_EXT, {"mission_status"})
        self.assertTrue(PROMPT8_EXT <= names)

    def test_permission_model_unchanged(self):
        self.assertEqual(set(CATEGORIES), {"system", "files_read", "files_write", "apps", "windows", "ui", "input", "screen", "code_read", "code_edit", "code_run",
                                           "research_online", "monitoring", "gaming", "plugins"})
        self.assertEqual(DEFAULT_ON, {"system"})
        self.assertEqual(CONSENT_REQUIRED, ("monitoring", "gaming"))
        self.assertEqual(ALWAYS_ASK_TOOLS, {"observe_screen", "find_on_screen", "recheck_screen"})
        s = Settings()
        self.assertEqual([c for c, v in s.enabled.items() if v], ["system"])

    def test_modes_unchanged(self):
        self.assertEqual(list(modes.NAMES), ["general", "coding", "research", "gaming", "productivity"])

    def test_no_mission_category_or_risk_was_added(self):
        self.assertNotIn("missions", CATEGORIES)
        self.assertEqual({sp.name for sp in self.make9() and self.ex.specs.values() if sp.category == "system" and "mission" in sp.name}, {"mission_status"})

    def test_mission_tool_follows_the_mode_rules(self):
        """Read-only mission status is available in general/coding/research/productivity; Gaming mode keeps its strict allow-list and does not include it."""
        self.make9()
        for mode in modes.NAMES:
            modes.set_mode(self.profile, mode)
            blocked = modes.blocked(self.profile, self.ex.specs["mission_status"])
            self.assertEqual(bool(blocked), mode == "gaming", mode)


class EarlierBehaviourStillHolds(P9):
    def setUp(self):
        super().setUp()
        self.make9(enable=("files_read", "files_write"))

    def test_p6_confirmation_and_protection_rules(self):
        (self.work / "a.txt").write_text("x")
        self.answer = False
        r = self.run_tool("delete_path", path="a.txt")
        self.assertNotEqual(r.status, "verified")
        self.assertTrue((self.work / "a.txt").exists())
        outside = self.d / "out.txt"
        outside.write_text("x")
        self.assertEqual(self.run_tool("read_file", path=str(outside)).status, "denied")

    def test_p6_emergency_stop_still_blocks_direct_tool_runs(self):
        self.env.stop.stop("t")
        self.assertEqual(self.run_tool("system_info").status, "stopped")

    def test_p7_extension_rules_hold_with_mission_tools_loaded(self):
        import dataclasses
        base = SPECS[0]
        for bad in (dataclasses.replace(base), dataclasses.replace(base, name="zz", risk=S.ADMIN), dataclasses.replace(base, name="zy", risk=S.EXTERNAL)):
            with self.assertRaises(ValueError):
                self.ex._add_spec(bad)

    def test_p8_monitoring_and_gaming_still_need_recorded_consent(self):
        self.env.settings.enabled["monitoring"] = True
        self.assertEqual(self.run_tool("monitor_snapshot", parts=["memory"]).status, "denied")

    def test_p8_mode_blocks_still_apply_to_direct_runs(self):
        modes.set_mode(self.profile, "research")
        self.assertEqual(self.run_tool("write_file", path="b.txt", content="x").status, "denied")
        self.assertFalse((self.work / "b.txt").exists())



class DocsTests(unittest.TestCase):
    def test_STATIC_review_states_the_required_facts_and_makes_no_overclaims(self):
        f = ROOT / "docs" / "PROMPT9_REVIEW.md"
        self.assertTrue(f.exists())
        low = f.read_text(encoding="utf-8").lower()
        for must in ("not run on windows", "real qwen", "not a security", "implementation complete", "pending", "what was mocked", "known limitations", "next recommended milestone",
                     "exact command", "sha-256", "closed", "asleep", "restart", "cannot be interrupted", "negative controls", "scripted", "no background service"):
            self.assertIn(must, low, must)
        for bad in ("production-ready", "production ready", "fully secure", "guaranteed safe", "windows-verified", "fully verified"):
            self.assertNotIn(bad, low.replace("not " + bad, ""), bad)

    def test_STATIC_checklist_changelog_and_privacy_updated(self):
        self.assertIn("## 6j. Prompt 9", (ROOT / "docs" / "WINDOWS_TEST_CHECKLIST.md").read_text(encoding="utf-8"))
        self.assertIn("Prompt 9", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")[:900])
        priv = (ROOT / "docs" / "PRIVACY_AND_NETWORK.md").read_text(encoding="utf-8")
        for w in ("Missions", "Schedules", "Mission history"):
            self.assertIn(w, priv)

    def test_STATIC_cli_help_documents_when_schedules_run(self):
        t = (ROOT / "src" / "jarvis" / "cli_missions.py").read_text(encoding="utf-8")
        self.assertIn("ONLY while", t)
        self.assertIn("EMERGENCY", (ROOT / "src" / "jarvis" / "desktop" / "cli_desktop.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
