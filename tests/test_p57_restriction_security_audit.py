"""Security audit of the adult-mode restriction. Real application code (Persona, LocalAssistant, AppController, cli, audit, evalsuite, selfcheck); the model is a
SCRIPTED FAKE and no Qt is involved, so nothing here says anything about real generation or the real window. Runs with the restriction ON."""
import contextlib
import io
import json
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from restriction_on import setUpModule, tearDownModule  # noqa: E402,F401  (the mechanism under test needs the restriction ON)
from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.assistant import LocalAssistant  # noqa: E402
from jarvis.personality import audit, evalsuite, selfcheck, styles  # noqa: E402
from jarvis.personality.persona import PERSONA_FILE, Persona, PersonaSettings  # noqa: E402
from jarvis.personality.styles import STYLES  # noqa: E402

ADULT = ["savage", "flirty", "sexy", "romantic"]
SRC = Path(__file__).resolve().parent.parent / "src"
TESTS = Path(__file__).resolve().parent


class Base(AppCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(styles.ADULT_MODES_RESTRICTED, "order contamination: another test module left the restriction lifted")

    def guest(self):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = "Twelve. Plain scripted reply."
        a = LocalAssistant(self.cfg, engine_factory=fake_engine_factory, guest=True)
        self.addCleanup(a.close)
        return a

    def profile_persona(self, **saved):
        d = self.d / "pp"
        d.mkdir(exist_ok=True)
        if saved:
            (d / PERSONA_FILE).write_text(json.dumps(saved), encoding="utf-8")
        return Persona(d), d / PERSONA_FILE


class ScopedUnlock(Base):
    def test_unlock_is_read_only_and_off_by_default(self):
        p, _ = self.profile_persona()
        self.assertFalse(p.review_unlock)
        self.assertTrue(p.restricted())
        with self.assertRaises(AttributeError):
            p.review_unlock = True

    def test_scope_is_restored_after_exception_and_keyboard_interrupt(self):
        p, _ = self.profile_persona()
        for exc in (RuntimeError, KeyboardInterrupt, SystemExit):
            with self.assertRaises(exc):
                with p.review_scope():
                    self.assertFalse(p.restricted())
                    raise exc()
            self.assertTrue(p.restricted(), exc)
            with self.assertRaises(ValueError):
                p.set_style("savage")

    def test_scopes_nest_and_a_disabled_scope_changes_nothing(self):
        p, _ = self.profile_persona()
        with p.review_scope():
            with p.review_scope():
                pass
            self.assertFalse(p.restricted())            # the inner exit must not re-lock the outer run
            with p.review_scope(False):
                self.assertFalse(p.restricted())        # a disabled inner scope neither locks nor unlocks
        self.assertTrue(p.restricted())
        with p.review_scope(False):
            self.assertTrue(p.restricted())

    def test_another_thread_sharing_the_object_never_sees_the_unlock(self):
        p, _ = self.profile_persona()
        seen, err = {}, []
        inside, done = threading.Event(), threading.Event()

        def other():
            inside.wait(5)
            seen["restricted"] = p.restricted()
            try:
                p.set_style("sexy")
            except ValueError as e:
                err.append(str(e))
            done.set()
        t = threading.Thread(target=other)
        t.start()
        with p.review_scope():
            inside.set()
            done.wait(5)
            self.assertFalse(p.restricted())            # the owning thread still has it
        t.join(5)
        self.assertTrue(seen["restricted"])
        self.assertEqual(len(err), 1)

    def test_two_threads_each_with_a_scope_do_not_end_each_others(self):
        p, _ = self.profile_persona()
        a_in, b_exit, a_still = threading.Event(), threading.Event(), []

        def b():
            with p.review_scope():
                a_in.set()
            b_exit.set()
        t = threading.Thread(target=b)
        with p.review_scope():
            t.start()
            b_exit.wait(5)
            a_still.append(p.restricted())
        t.join(5)
        self.assertEqual(a_still, [False])
        self.assertTrue(p.restricted())

    def test_nothing_is_written_to_disk_during_a_review_even_for_a_profile_persona(self):
        p, f = self.profile_persona(style="calm", language="German")
        before = f.read_bytes()
        with p.review_scope():
            p.set_adult_optin(True)
            p.set_style("flirty")
            self.assertEqual(p.settings.style, "flirty")
        self.assertEqual(f.read_bytes(), before)
        q = Persona(f.parent)
        self.assertEqual((q.settings.style, q.settings.adult_optin), ("calm", False))

    def test_in_memory_adult_style_left_behind_is_answered_in_normal_after_the_scope(self):
        p, _ = self.profile_persona()
        with p.review_scope():
            p.settings = PersonaSettings(style="savage", adult_optin=True)
        t = p.observe("hello")
        self.assertEqual(t.style, "normal")


class UnlockCannotLeak(Base):
    def test_audit_suite_selfcheck_relock_after_a_model_failure(self):
        for fn in (lambda a: audit.run(a, categories=["ordinary"], simulated=True, with_controls=False, include_restricted=True),
                   lambda a: evalsuite.run(a, scenarios=[evalsuite.SCENARIOS[0]], simulated=True, include_restricted=True)):
            a = self.guest()
            with mock.patch.object(a, "reply", side_effect=RuntimeError("model died")), mock.patch.object(a, "reply_stream", side_effect=RuntimeError("model died"), create=True):
                rep = fn(a)
            self.assertFalse(rep["complete"])
            self.assertTrue(a.persona.restricted())

    def test_selfcheck_relocks_when_it_raises(self):
        a = self.guest()
        with mock.patch.object(a, "reply", side_effect=RuntimeError("model died")):
            with self.assertRaises(RuntimeError):
                selfcheck.run(a, styles=["savage"], include_restricted=True)
        self.assertTrue(a.persona.restricted())

    def test_keyboard_interrupt_inside_a_run_relocks(self):
        a = self.guest()
        with mock.patch.object(evalsuite, "run_scenario", side_effect=KeyboardInterrupt):
            rep = evalsuite.run(a, scenarios=[evalsuite.SCENARIOS[0]], simulated=True, include_restricted=True)
        self.assertTrue(rep["interrupted"])
        self.assertTrue(a.persona.restricted())

    def test_report_writing_failure_leaves_the_restriction_on(self):
        a = self.guest()
        rep = audit.run(a, categories=["ordinary"], simulated=True, with_controls=False, include_restricted=True)
        self.assertTrue(a.persona.restricted())          # the run is finished before any report is written, so a failing save cannot leave it open
        with mock.patch.object(evalsuite, "write_new", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                audit.save(rep, self.cfg.data_dir)
        self.assertTrue(a.persona.restricted())

    def test_a_normal_session_started_while_a_review_runs_is_restricted_and_untouched(self):
        a = self.guest()
        pdir = self.cfg.profile_dir
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / PERSONA_FILE).write_text(json.dumps({"style": "calm", "adult_optin": True}), encoding="utf-8")
        before = (pdir / PERSONA_FILE).read_bytes()
        with a.persona.review_scope():
            self.assertFalse(a.persona.restricted())
            c = AppController(self.cfg, simulate=True)
            self.addCleanup(c.shutdown, 1.0)
            self.assertTrue(c.persona.restricted())
            self.assertTrue(c.persona_state()["restriction"]["active"])
            self.assertTrue(all(not d["available"] for d in c.persona_state()["styles"] if d["adult_only"]))
            with self.assertRaises(ValueError):
                c.persona_set("savage")
            b = LocalAssistant(self.cfg, engine_factory=fake_engine_factory)
            self.addCleanup(b.close)
            self.assertTrue(b.persona.restricted())
        self.assertEqual((pdir / PERSONA_FILE).read_bytes(), before)

    def test_two_audits_on_two_assistants_are_independent(self):
        a1, a2 = self.guest(), self.guest()
        with a1.persona.review_scope():
            self.assertTrue(a2.persona.restricted())
        self.assertTrue(a1.persona.restricted())

    def test_a_restore_after_the_run_is_checked_by_the_style_gate_too(self):
        a = self.guest()
        audit.run(a, styles=["savage"], categories=["ordinary"], simulated=True, with_controls=False, include_restricted=True)
        a.reply("hello", max_tokens=8)
        self.assertEqual(a.last_turn.style, "normal")


class FlagAcceptance(Base):
    def run_cli(self, *argv):            # returns (exit code, stdout, stderr)
        from jarvis import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--data-dir", str(self.cfg.data_dir), *argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_flag_is_rejected_by_every_command_that_should_not_have_it(self):
        for argv in (["persona", "set", "savage"], ["persona", "set", "calm"], ["persona", "normal"], ["persona", "list"], ["persona", "preview", "savage"],
                     ["persona", "adult", "on", "--confirm-adult"], ["persona", "control", "humor", "1"], ["persona", "reset"], ["persona", "option", "language", "x"],
                     ["chat"], ["voice"], ["selftest"], ["models", "list"]):
            with self.subTest(argv=argv):
                rc, _, err = self.run_cli(*argv, "--include-restricted-modes")
                self.assertEqual(rc, 2, argv)
                self.assertRegex(err, "unrecognized arguments|required|invalid choice")
        self.assertFalse((self.cfg.profile_dir / PERSONA_FILE).exists())

    def test_flag_is_accepted_only_by_audit_suite_and_selfcheck(self):
        from jarvis import cli
        accepting = set()
        for cmd in ("audit", "suite", "selfcheck", "set", "list", "adult", "control", "normal", "reset", "option", "preview"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["persona", cmd, "--help"])
            if "--include-restricted-modes" in out.getvalue():
                accepting.add(cmd)
        self.assertEqual(accepting, {"audit", "suite", "selfcheck"})

    def test_flag_in_the_environment_or_a_config_file_does_nothing(self):
        (self.cfg.data_dir).mkdir(parents=True, exist_ok=True)
        (self.cfg.data_dir / "config.json").write_text(json.dumps({"include_restricted_modes": True, "review_unlock": True, "ADULT_MODES_RESTRICTED": False}), encoding="utf-8")
        with mock.patch.dict("os.environ", {"INCLUDE_RESTRICTED_MODES": "1", "JARVIS_INCLUDE_RESTRICTED_MODES": "1", "ADULT_MODES_RESTRICTED": "0"}):
            rc, _, err = self.run_cli("persona", "set", "savage")
        self.assertEqual(rc, 2)
        self.assertIn("temporarily unavailable", err)


class SelectionPaths(Base):
    """Every route that can change or load the selected personality, each with a restricted value."""
    def test_load_paths_accept_hostile_types_without_selecting_or_unlocking(self):
        values = ["true", "True", 1, 0, None, [True], {"a": 1}, 1.0, "", "yes"]
        n = 0
        for v in values:
            for style in ("savage", "flirty", "sexy", "romantic"):
                d = self.d / f"h{n}"
                d.mkdir()
                n += 1
                (d / PERSONA_FILE).write_text(json.dumps({"style": style, "adult_optin": v, "saved_adult_style": style, "roast": 3}), encoding="utf-8")
                p = Persona(d)
                self.assertEqual(p.settings.style, "normal", (v, style))
                self.assertIsNot(p.settings.adult_optin, 1)
                self.assertFalse(STYLES[p.settings.style].adult_only)
                self.assertEqual(p.settings.saved_adult_style, "" if v is not True else style)
        for st in (None, 3, ["savage"], {"x": "savage"}, "Savage", " savage", "savage\x00", "savаge"):
            d = self.d / f"s{n}"
            d.mkdir()
            n += 1
            (d / PERSONA_FILE).write_text(json.dumps({"style": st, "adult_optin": True, "saved_adult_style": st}), encoding="utf-8")
            p = Persona(d)
            self.assertEqual(p.settings.style, "normal", st)
            self.assertEqual(p.settings.saved_adult_style, "", st)

    def test_loading_never_rewrites_the_file_and_keeps_unrelated_valid_settings(self):
        p, f = self.profile_persona(style="romantic", adult_optin=True, language="German", level="advanced", roast=2, humor=1, minutes=15)
        raw = f.read_bytes()
        mtime = f.stat().st_mtime_ns
        q = Persona(f.parent)
        self.assertEqual((q.settings.style, q.settings.saved_adult_style, q.settings.adult_optin), ("normal", "romantic", True))
        self.assertEqual((q.settings.language, q.settings.level, q.settings.roast, q.settings.humor, q.settings.minutes), ("German", "advanced", 2, 1, 15))
        self.assertEqual((f.read_bytes(), f.stat().st_mtime_ns), (raw, mtime))

    def test_corrupt_adult_gate_fails_closed_and_resets_intensities(self):
        p, f = self.profile_persona(style="savage", adult_optin="true", roast=3, profanity=3, language="German")
        self.assertEqual((p.settings.style, p.settings.adult_optin, p.settings.language), ("normal", False, "German"))
        self.assertNotIn(p.settings.roast, (3,))
        self.assertNotIn(p.settings.profanity, (3,))

    def test_reset_clears_everything_including_the_remembered_choice(self):
        p, f = self.profile_persona(style="savage", adult_optin=True)
        self.assertEqual(p.settings.saved_adult_style, "savage")
        p.reset()
        self.assertEqual((p.settings.style, p.settings.adult_optin, p.settings.saved_adult_style), ("normal", False, ""))

    def test_controller_paths_all_refuse_and_nothing_is_written(self):
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        c.persona_adult(True, confirmed=True)
        f = c.persona.path
        before = f.read_bytes()
        for sid in ADULT:
            with self.assertRaises(ValueError):
                c.persona_set(sid)
            with self.assertRaises(ValueError):
                c.persona_option(style=sid)
        self.assertEqual(f.read_bytes(), before)
        for name, args in (("persona_level", ("roast", 3)), ("persona_control", ("humor", 1)), ("persona_controls_reset", ()), ("persona_normal", ()), ("persona_reset", ())):
            getattr(c, name)(*args)
            self.assertFalse(STYLES[c.persona.settings.style].adult_only)

    def test_voice_selection_by_personality_id_cannot_select_a_personality(self):
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        try:
            c.voice_apply("sapi:none", "savage")
        except Exception:      # noqa: BLE001 - the voice may not exist; the point is the personality
            pass
        self.assertEqual(c.persona.settings.style, "normal")

    def test_safety_override_and_stop_still_work_with_an_adult_style_forced_in_memory(self):
        p, _ = self.profile_persona()
        p.settings = PersonaSettings(style="savage", adult_optin=True)
        t = p.observe("I want to kill myself")
        self.assertTrue(t.overridden)
        self.assertEqual(t.style, "normal")
        self.assertTrue(t.guidance.rstrip().endswith(__import__("jarvis.personality.persona", fromlist=["SAFETY_BLOCK"]).SAFETY_BLOCK.rstrip()))
        p.settings = PersonaSettings(style="flirty", adult_optin=True)
        t = p.observe("please stop, go back to normal")
        self.assertEqual(t.style, "normal")

    def test_a_style_cannot_change_tool_permissions(self):
        for rel in ("personality/persona.py", "personality/styles.py", "personality/spec.py", "personality/safety.py"):
            text = (SRC / "jarvis" / rel).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"^\s*(from|import)\s+jarvis\.(desktop|missions|tools|policy)", text, re.M), rel)


class HelperIsTestOnly(unittest.TestCase):
    def test_production_never_imports_the_helper(self):
        for f in SRC.rglob("*.py"):
            self.assertNotIn("restriction_lifted", f.read_text(encoding="utf-8"), f)

    def test_the_helper_restores_the_constant_even_when_a_test_fails(self):
        import restriction_lifted as rl
        self.assertTrue(styles.ADULT_MODES_RESTRICTED)
        rl.setUpModule()
        try:
            self.assertFalse(styles.ADULT_MODES_RESTRICTED)
        finally:
            rl.tearDownModule()
        self.assertTrue(styles.ADULT_MODES_RESTRICTED)

    def test_every_module_using_the_helper_is_listed_and_p55_p56_p57_never_do(self):
        users = sorted(p.name for p in TESTS.glob("test_*.py") if re.search(r"^\s*from restriction_lifted import", p.read_text(encoding="utf-8"), re.M) and p.name != Path(__file__).name)
        self.assertEqual(users, ["test_p15_persona.py", "test_p16_completion.py", "test_p17_handsfree.py", "test_p47_personality_suite.py", "test_p48_audit_regressions.py",
                                 "test_p49_audit_workflow.py", "test_p50_personality_window_logic.py", "test_p53_safety_eval.py", "test_p54_personality_coverage.py"])

    def test_restriction_tests_pass_when_run_after_a_lifted_module_in_either_order(self):
        for order in (["test_p53_safety_eval", "test_p55_adult_restriction", "test_p57_restriction_security_audit.HelperIsTestOnly.test_production_never_imports_the_helper"],
                      ["test_p55_adult_restriction", "test_p53_safety_eval"]):
            r = subprocess.run([sys.executable, "-B", "-m", "unittest", *order], cwd=str(TESTS), capture_output=True, text=True,
                               env={**__import__("os").environ, "PYTHONPATH": str(SRC)}, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])


if __name__ == "__main__":
    unittest.main()
