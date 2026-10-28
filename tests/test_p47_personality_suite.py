"""Stage 7: personality specs, controls, persistence, switching and the 15-scenario x 17-personality suite.

LABELS: REAL = Persona/safety/spec code, real prompt assembly in LocalAssistant, real PersonalMemory (temp dir), real settings files.
FAKE MODEL = FakeLlama scripted replies (no real inference). FAKE AUDIO = scripted speakers. The suite checks that the PROGRAM does its part
(spec reached the model, overrides fired, memory rules, history, settings); it does not and cannot judge tone, charm or human-likeness."""
import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402

from jarvis.assistant import LocalAssistant  # noqa: E402
from jarvis.personality import evalsuite  # noqa: E402
from jarvis.personality.persona import PERSONA_FILE, SAFETY_BLOCK, Persona, PersonaSettings  # noqa: E402
from jarvis.personality.spec import CONTROLS, LEVEL_RANGE, SPEC_VERSION, SPECS, spec_guidance, validate_specs  # noqa: E402
from jarvis.personality.styles import STYLES  # noqa: E402
from restriction_lifted import setUpModule, tearDownModule  # noqa: E402,F401  (these tests exercise the dormant adult definitions)

REQUIRED = ["normal", "professional", "companion", "technical", "concise", "humorous", "savage", "flirty", "sexy", "debate", "coach", "romantic",
            "conspiracy", "explainer", "therapist", "tutor", "calm"]
ADULT = {"savage", "flirty", "sexy", "romantic"}


class Specs(unittest.TestCase):
    def test_all_17_modes_exist_and_specs_are_complete(self):
        self.assertEqual(set(STYLES), set(REQUIRED))
        self.assertEqual(len(STYLES), 17)
        self.assertEqual(validate_specs(STYLES), [])
        self.assertEqual(set(SPECS), set(STYLES))

    def test_adult_modes_are_exactly_the_designated_ones(self):
        self.assertEqual({k for k, v in STYLES.items() if v.adult_only}, ADULT)

    def test_specs_are_meaningfully_different(self):
        texts = {sid: spec_guidance(sid) for sid in STYLES}
        self.assertEqual(len(set(texts.values())), 17)
        for field in ("tone", "on_frustration", "explanation"):
            vals = [getattr(SPECS[s], field) for s in STYLES if s != "normal"]
            self.assertGreaterEqual(len(set(vals)), 14, field)

    def test_identity_limits_hold_under_every_offset(self):
        for sid, sp in SPECS.items():
            for off in (-2, 2):
                eff = sp.effective({c: off for c in CONTROLS})
                for c in CONTROLS:
                    lo, hi = sp.limits.get(c, LEVEL_RANGE)
                    self.assertTrue(lo <= eff[c] <= hi, (sid, c))
        self.assertLessEqual(SPECS["concise"].effective({"verbosity": 2})["verbosity"], 2)
        self.assertGreaterEqual(SPECS["humorous"].effective({"humor": -2})["humor"], 3)
        self.assertLessEqual(SPECS["calm"].effective({"humor": 2})["humor"], 2)

    def test_controls_change_the_guidance_but_not_the_identity_text(self):
        a, b = spec_guidance("normal", {"humor": 2, "verbosity": -2}), spec_guidance("normal", {})
        self.assertNotEqual(a, b)
        self.assertEqual(SPECS["normal"].tone in a, SPECS["normal"].tone in b)
        self.assertIn(SPECS["savage"].tone, spec_guidance("savage", {c: -2 for c in CONTROLS}))

    def test_spec_text_has_no_permission_or_catchphrase_machinery(self):
        src = (Path(__file__).resolve().parents[1] / "src/jarvis/personality/spec.py").read_text(encoding="utf-8")
        for bad in ("import jarvis.desktop", "from jarvis.desktop", "executor", "policy", "missions"):
            self.assertNotIn(bad, src.replace("holds no permission, and nothing here imports the executor, tools, policy or missions", ""))


class Settings(AppCase):
    def pdir(self):
        return Path(self.tmp.name) / "profile"

    def test_controls_persist_and_survive_reload(self):
        p = Persona(self.pdir())
        p.set_style("companion")
        p.set_control("humor", 2)
        p.set_control("verbosity", -1)
        q = Persona(self.pdir())
        self.assertEqual((q.settings.style, q.settings.humor, q.settings.verbosity), ("companion", 2, -1))
        self.assertEqual(json.loads((self.pdir() / PERSONA_FILE).read_text())["schema_version"], SPEC_VERSION)

    def test_invalid_control_values_are_rejected_and_not_saved(self):
        p = Persona(self.pdir())
        for bad in (3, -3, 1.5, "2", True):
            with self.assertRaises(ValueError):
                p.set_control("humor", bad)
        with self.assertRaises(ValueError):
            p.set_control("nonsense", 1)
        self.assertEqual(p.settings.humor, 0)

    def test_old_settings_file_without_stage7_fields_loads_with_defaults(self):
        self.pdir().mkdir(parents=True)
        (self.pdir() / PERSONA_FILE).write_text(json.dumps({"style": "debate", "argument": 3}))
        p = Persona(self.pdir())
        self.assertEqual((p.settings.style, p.settings.argument, p.settings.humor), ("debate", 3, 0))
        self.assertEqual(p.load_note, "")

    def test_missing_corrupt_future_and_out_of_range_files_fall_back_safely(self):
        self.pdir().mkdir(parents=True)
        for raw in ("", "{not json", "[]", json.dumps({"style": "savage", "adult_optin": False}), json.dumps({"style": "normal", "humor": 9}),
                    json.dumps({"style": "nope"}), json.dumps({"schema_version": 999}), b"\xff\xfe\x00"):
            f = self.pdir() / PERSONA_FILE
            f.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
            p = Persona(self.pdir())
            self.assertEqual((p.settings.style, p.settings.adult_optin, p.settings.humor), ("normal", False, 0), repr(raw))
        self.assertEqual(Persona(Path(self.tmp.name) / "absent").settings.style, "normal")

    def test_adult_modes_locked_until_opt_in_and_opt_out_leaves_them(self):
        p = Persona(self.pdir())
        for sid in ADULT:
            with self.assertRaises(ValueError):
                p.set_style(sid)
        p.set_adult_optin(True)
        for sid in ADULT:
            p.set_style(sid)
            self.assertEqual(p.settings.style, sid)
        p.set_adult_optin(False)
        self.assertEqual(p.settings.style, "normal")
        listing = {d["id"]: d["available"] for d in p.listing()}
        self.assertTrue(all(not listing[s] for s in ADULT) and all(listing[s] for s in set(REQUIRED) - ADULT))

    def test_minor_cue_switches_adult_modes_off_for_sexy_too(self):
        p = Persona(self.pdir())
        p.set_adult_optin(True)
        p.set_style("sexy")
        t = p.observe("by the way I'm 16")
        self.assertEqual((p.settings.style, p.settings.adult_optin), ("normal", False))
        self.assertIn("Adult-only styles were switched off", t.notice)

    def test_every_style_composes_with_safety_block_and_spec(self):
        p = Persona(None, persist=False)
        p.set_adult_optin(True)
        for sid in STYLES:
            p.set_style(sid)
            g = p.observe("hello there").guidance
            self.assertIn(SAFETY_BLOCK, g, sid)
            self.assertIn(f"BEHAVIOUR SPEC v{SPEC_VERSION} ({sid})", g, sid)
            self.assertTrue(g.rstrip().endswith(SAFETY_BLOCK), "safety rules come last: no control can move them")

    def test_no_control_setting_removes_the_safety_block(self):
        p = Persona(None, persist=False)
        p.set_adult_optin(True)
        for sid in STYLES:
            p.set_style(sid)
            for c in CONTROLS:
                p.set_control(c, 2)
            self.assertIn(SAFETY_BLOCK, p.observe("x").guidance)
            p.reset_controls()


class Suite(AppCase):
    """FAKE MODEL. 15 scenarios x 17 personalities through the real LocalAssistant."""
    def make(self, **kw):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = "Paris is the capital of France."
        a = LocalAssistant(self.cfg, engine_factory=fake_engine_factory, **kw)
        self.addCleanup(a.close)
        return a

    def test_the_whole_matrix_passes_with_a_fake_model(self):
        a = self.make()
        rep = evalsuite.run(a, simulated=True, seed_memory=lambda t: a.memory.add("explicit_preference", t),
                            voice=lambda text, rate: False)
        self.assertEqual(rep["total"], 17 * 15)
        self.assertEqual(rep["failed"], [], rep["failed"][:5])
        self.assertEqual(rep["not_run"], [])
        self.assertTrue(rep["simulated_model"])
        self.assertEqual(len(rep["scenarios"]), 15)
        self.assertTrue(rep["needs_human_review"])

    def test_scenarios_without_harness_are_reported_not_run_never_passed(self):
        a = self.make()
        rep = evalsuite.run(a, styles=["normal"], scenarios=["memory_retrieval", "voice_engine_failure"], simulated=True)
        self.assertEqual(rep["run"], 0)
        self.assertEqual(len(rep["not_run"]), 2)

    def test_suite_detects_a_broken_pipeline(self):
        a = self.make()
        orig = a.persona.__class__._compose
        a.persona.__class__._compose = lambda self, s, profile: "no spec here"
        try:
            rep = evalsuite.run(a, styles=["savage", "calm"], scenarios=["simple_factual", "frustrated_user"], simulated=True)
        finally:
            a.persona.__class__._compose = orig
        self.assertEqual(len(rep["failed"]), 4)

    def test_suite_detects_text_that_unlocks_adult_mode(self):
        a = self.make()
        orig = a.persona.__class__.observe

        def evil(self, text, profile=None):
            self.settings = PersonaSettings(**{**asdict(self.settings), "adult_optin": True})
            return orig(self, text, profile)
        a.persona.__class__.observe = evil
        try:
            rep = evalsuite.run(a, styles=["normal"], scenarios=["ignore_safety_rules"], simulated=True)
        finally:
            a.persona.__class__.observe = orig
        self.assertEqual(rep["failed"], ["normal/ignore_safety_rules"])


class Behaviour(AppCase):
    """FAKE MODEL. Specific cross-cutting behaviours beyond the matrix."""
    def make(self, **kw):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = "ok"
        a = LocalAssistant(self.cfg, engine_factory=fake_engine_factory, **kw)
        self.addCleanup(a.close)
        return a

    def last_sys(self):
        return FakeLlama.instances[0].calls[-1][0]["content"]

    def test_switching_is_immediate_and_keeps_history_and_memory(self):
        a = self.make()
        a.memory.add("explicit_preference", "my favourite colour is teal")
        a.reply("hello")
        a.persona.set_style("technical")
        a.reply("what is my favourite colour?")
        self.assertIn("(technical)", self.last_sys())
        self.assertIn("teal", self.last_sys().lower())
        a.persona.set_style("concise")
        a.reply("again")
        self.assertIn("(concise)", self.last_sys())
        self.assertNotIn("(technical)", self.last_sys())
        self.assertGreaterEqual(len(a.history), 6)
        self.assertEqual(len(FakeLlama.instances), 1, "the model was not reloaded for a personality change")

    def test_control_change_applies_to_the_next_message(self):
        a = self.make()
        a.reply("hi")
        before = self.last_sys()
        a.persona.set_control("humor", 2)
        a.reply("hi again")
        self.assertNotEqual(before, self.last_sys())

    def test_edgy_modes_do_not_receive_saved_notes(self):
        a = self.make()
        a.memory.add("explicit_preference", "my favourite colour is teal")
        a.persona.set_adult_optin(True)
        for sid in ("savage", "flirty", "sexy", "romantic"):
            a.persona.set_style(sid)
            a.reply("what is my favourite colour?")
            self.assertNotIn("teal", self.last_sys().lower(), sid)

    def test_companion_gets_saved_notes_normally_and_forbids_fake_feelings(self):
        a = self.make()
        a.memory.add("explicit_preference", "my favourite colour is teal")
        a.persona.set_style("companion")
        a.reply("what is my favourite colour?")
        s = self.last_sys()
        self.assertIn("teal", s.lower())
        self.assertIn("Claiming human feelings", s)

    def test_no_text_postprocessing_beyond_label_and_guard(self):
        a = self.make()
        FakeLlama.reply = "Plain scripted words."
        for sid in ("normal", "humorous", "professional", "companion", "calm", "concise"):
            a.persona.set_style(sid)
            self.assertEqual(a.reply("hello"), "Plain scripted words.", sid)

    def test_model_error_leaves_personality_and_history_usable(self):
        a = self.make()
        a.persona.set_style("technical")
        a.reply("hello")
        eng = a._eng()
        orig = eng.chat
        eng.chat = lambda *x, **k: (_ for _ in ()).throw(RuntimeError("model crashed"))
        with self.assertRaises(RuntimeError):
            a.reply("again")
        eng.chat = orig
        self.assertEqual(a.persona.settings.style, "technical")
        self.assertEqual(a.reply("and again"), "ok")

    def test_voice_speed_is_the_only_personality_effect_on_speech(self):
        p = Persona(None, persist=False)
        p.set_adult_optin(True)
        for sid, st in STYLES.items():
            p.set_style(sid)
            t = p.observe("hello")
            self.assertEqual(t.speech_rate, max(0.5, min(2.0, st.speech_rate)))
        self.assertLess(STYLES["calm"].speech_rate, 1.0)
        self.assertGreater(STYLES["concise"].speech_rate, 1.0)


class Surfaces(AppCase):
    """REAL controller (simulate=True: no Qt, no audio) and REAL CLI parsing."""
    def run_cli(self, *a):
        import io
        from unittest import mock
        from jarvis import cli
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), *a])
        return rc, out.getvalue(), err.getvalue()

    def test_cli_controls_persist_and_reject_bad_values(self):
        self.assertEqual(self.run_cli("persona", "control", "humor", "2")[0], 0)
        self.assertEqual(self.run_cli("persona", "control", "humor", "5")[0], 2)
        self.assertEqual(self.run_cli("persona", "control", "wit", "1")[0], 2)
        rc, out, _ = self.run_cli("persona", "show")
        self.assertEqual(json.loads(out)["settings"]["humor"], 2)

    def test_cli_lists_all_17_and_sexy_is_locked_without_opt_in(self):
        rc, out, _ = self.run_cli("persona", "list")
        self.assertEqual(len(json.loads(out)), 17)
        self.assertEqual(self.run_cli("persona", "set", "sexy")[0], 2)

    def test_controller_controls_switch_and_state(self):
        from jarvis.app.controller import AppController
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        c.persona_set("humorous")
        c.persona_control("humor", -2)
        st = c.persona_state()
        self.assertEqual(st["settings"]["humor"], -2)
        self.assertEqual(st["effective_controls"]["humor"], 3, "Humorous keeps its identity: humour cannot be turned below its floor")
        self.assertIn("small local model", st["limitation"])
        self.assertEqual(len(st["styles"]), 17)
        c.persona_controls_reset()
        self.assertEqual(c.persona_state()["settings"]["humor"], 0)

    def test_voice_override_accepts_every_new_personality_id(self):
        from jarvis.app.controller import AppController
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        for sid in ("companion", "humorous", "sexy"):
            try:
                c.speech.voices._check_persona(sid)
            except Exception as e:  # noqa: BLE001
                self.fail(f"{sid}: {e}")


if __name__ == "__main__":
    unittest.main()
