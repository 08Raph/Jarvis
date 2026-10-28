"""Pre-Stage-8 gate: failure handling of `persona audit` / `persona suite`, exercised through the REAL code paths (audit.run, evalsuite.run and the CLI
handlers, real file writes in a temp dir). The MODEL is a scripted FAKE (FakeLlama) that can be made to fail on chosen calls. Nothing here says anything
about a real model or about personality quality."""
import io
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402

from jarvis import cli  # noqa: E402
from jarvis.assistant import LocalAssistant  # noqa: E402
from jarvis.personality import audit, evalsuite  # noqa: E402

REAL_ASSISTANT = LocalAssistant
CATS = ["ordinary", "technical", "frustration", "disagreement", "uncertainty", "safety_sensitive"]      # single-turn categories: one model call per case
SUITE_SC = ["simple_factual", "complex_technical", "ambiguous_request", "frustrated_user", "humor_opportunity", "disagreement"]


class Base(AppCase):
    def make(self, fail_calls=(), exc=None, ctrl_c_at=None):
        self.install_model()
        FakeLlama.instances.clear()
        FakeLlama.reply = "Twelve. Scripted."
        a = REAL_ASSISTANT(self.cfg, engine_factory=fake_engine_factory, guest=True)
        self.addCleanup(a.close)
        eng, orig, n = a._eng(), a._eng().chat, {"n": 0}
        self.calls = n

        def chat(*x, **k):
            n["n"] += 1
            if ctrl_c_at and n["n"] == ctrl_c_at:
                raise KeyboardInterrupt
            if n["n"] in fail_calls:
                raise (exc or RuntimeError("model crashed"))
            return orig(*x, **k)
        eng.chat = chat
        return a

    def cli(self, *args, assistant_factory=None):
        out, err = io.StringIO(), io.StringIO()
        fac = assistant_factory or (lambda cfg, guest=False: REAL_ASSISTANT(cfg, engine_factory=fake_engine_factory, guest=guest))
        with mock.patch("jarvis.assistant.LocalAssistant", fac), mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), "persona", *args])
        return rc, out.getvalue(), err.getvalue()

    def eval_files(self, pattern="*"):
        d = self.cfg.data_dir / "eval"
        return sorted(d.glob(pattern)) if d.is_dir() else []


class AuditRunFailures(Base):
    def run_audit(self, a, **kw):
        return audit.run(a, styles=["normal"], categories=CATS, simulated=True, with_controls=False, **kw)

    def test_fails_before_the_first_response(self):
        a = self.make(fail_calls=range(1, 100))
        rep = self.run_audit(a)
        self.assertFalse(rep["complete"])
        self.assertEqual(self.calls["n"], 3, "stops after exactly 3 consecutive failures; does not grind through every case")
        self.assertTrue(rep["aborted"] and len(rep["incomplete"]) == 3)
        self.assertEqual(len(rep["missing_cases"]), len(CATS) + 1)        # + the defining prompt
        self.assertFalse(any(r["reply"] for cats in rep["transcripts"].values() for rows in cats.values() for r in rows))

    def test_fails_after_several_successes_keeps_the_good_transcripts(self):
        a = self.make(fail_calls=range(4, 100))
        rep = self.run_audit(a)
        ok = [c for c, rows in rep["transcripts"]["normal"].items() if rows and rows[0]["reply"]]
        self.assertEqual(ok, CATS[:3])
        self.assertFalse(rep["complete"])
        self.assertEqual(len(rep["missing_cases"]), len(CATS) + 1 - 3)
        self.assertTrue(rep["aborted"])

    def test_success_resets_the_consecutive_failure_counter(self):
        a = self.make(fail_calls={2, 3, 5, 6})        # pairs of failures separated by a success: never 3 in a row
        rep = self.run_audit(a)
        self.assertIsNone(rep["aborted"], "documented behaviour: a success resets the counter")
        self.assertEqual(len(rep["incomplete"]), 4)
        self.assertFalse(rep["complete"], "still incomplete: four cases failed")
        self.assertEqual(self.calls["n"], len(CATS) + 1)       # every case was attempted exactly once

    def test_ctrl_c_preserves_completed_transcripts_and_marks_incomplete(self):
        a = self.make(ctrl_c_at=3)
        saved = []
        rep = self.run_audit(a, checkpoint=lambda r: saved.append(1))
        self.assertTrue(rep["interrupted"])
        self.assertFalse(rep["complete"])
        done = [c for c, rows in rep["transcripts"]["normal"].items() if rows and rows[0]["reply"]]
        self.assertEqual(done, CATS[:2])
        self.assertEqual(len(rep["missing_cases"]), len(CATS) + 1 - 2)

    def test_complete_run_lists_nothing_missing(self):
        rep = self.run_audit(self.make())
        self.assertTrue(rep["complete"])
        self.assertEqual((rep["missing_cases"], rep["incomplete"], rep["aborted"], rep["interrupted"]), ([], [], None, False))

    def test_error_text_never_contains_paths_or_user_names(self):
        for msg in ("cannot open /home/alice/.local/share/Jarvis/models/q.gguf", r"cannot open C:\Users\alice\AppData\Jarvis\q.gguf", "no such file /Users/alice/Library/q.gguf",
                    r"bad share \\fileserver\alice\q.gguf"):
            a = self.make(fail_calls=range(1, 100), exc=OSError(msg))
            rep = self.run_audit(a)
            blob = json.dumps(rep, default=str) + audit.to_markdown(rep)
            self.assertNotIn("alice", blob, msg)
            self.assertIn("OSError", blob, "the error TYPE stays so the failure is diagnosable")
            self.assertIn("<path>", blob)

    def test_checkpoint_failure_is_recorded_and_does_not_lose_the_run(self):
        def bad(_):
            raise OSError("disk full")
        rep = self.run_audit(self.make(), checkpoint=bad)
        self.assertTrue(rep["complete"])
        self.assertTrue(rep["checkpoint_errors"] and "disk full" in rep["checkpoint_errors"][0])

    def test_identity_and_configuration_are_recorded_accurately(self):
        a = self.make()
        rep = self.run_audit(a)
        eng = a._eng()
        m = rep["model"]
        self.assertEqual(m["model_id"], a.model_id)
        self.assertEqual(m["n_ctx"], eng.n_ctx)
        self.assertEqual(m["sampling_profile"], eng.sampling_profile)
        self.assertEqual(m["file_name"], Path(eng.path).name)
        self.assertEqual(m["file_bytes"], Path(eng.path).stat().st_size)
        self.assertIs(rep["simulated_model"], True)
        rep2 = audit.run(a, styles=["normal"], categories=["ordinary"], simulated=False, with_controls=False, max_tokens=77, temperature=0.9)
        self.assertEqual((rep2["config"]["max_tokens"], rep2["config"]["temperature"], rep2["simulated_model"]), (77, 0.9, False))
        self.assertTrue(rep2["config"]["guest_session"])

    def test_no_network_during_audit_or_suite(self):
        a = self.make()
        boom = AssertionError("network access attempted")
        with mock.patch.object(socket, "socket", side_effect=boom), mock.patch.object(socket, "create_connection", side_effect=boom), \
                mock.patch.object(socket, "getaddrinfo", side_effect=boom):
            rep = self.run_audit(a)
            srep = evalsuite.run(a, styles=["normal"], scenarios=SUITE_SC[:2], simulated=True)
        self.assertTrue(rep["complete"] and srep["complete"])


class SuiteRunFailures(Base):
    def run_suite(self, a, **kw):
        return evalsuite.run(a, styles=["normal"], scenarios=SUITE_SC, simulated=True, **kw)

    def test_before_first_response_three_errors_then_stop(self):
        a = self.make(fail_calls=range(1, 100))
        rep = self.run_suite(a)
        self.assertFalse(rep["complete"])
        self.assertEqual(len(rep["errors"]), 3)
        self.assertTrue(rep["aborted"])
        self.assertEqual(len(rep["missing_cases"]), len(SUITE_SC) - 3)
        self.assertFalse(any(r["passed"] for r in rep["results"]))

    def test_after_several_successes_and_reset_on_success(self):
        rep = self.run_suite(self.make(fail_calls={2, 3, 5, 6}))
        self.assertIsNone(rep["aborted"])
        self.assertEqual(len(rep["errors"]), 4)
        self.assertFalse(rep["complete"])
        rep = self.run_suite(self.make(fail_calls=range(3, 100)))
        self.assertTrue(rep["aborted"])
        self.assertEqual(sum(1 for r in rep["results"] if r["status"] == "run"), 2)

    def test_ctrl_c_keeps_completed_results(self):
        rep = self.run_suite(self.make(ctrl_c_at=3))
        self.assertTrue(rep["interrupted"] and not rep["complete"])
        self.assertEqual(sum(1 for r in rep["results"] if r["status"] == "run"), 2)
        self.assertEqual(len(rep["missing_cases"]), len(SUITE_SC) - 2)

    def test_error_text_is_sanitised(self):
        rep = self.run_suite(self.make(fail_calls=range(1, 100), exc=OSError(r"cannot open C:\Users\alice\q.gguf")))
        self.assertNotIn("alice", json.dumps(rep, default=str))

    def test_checkpoint_failure_does_not_lose_the_run(self):
        rep = self.run_suite(self.make(), checkpoint=mock.Mock(side_effect=OSError("disk full")))
        self.assertTrue(rep["checkpoint_errors"])
        self.assertEqual(rep["passed"], rep["run"])


class CliBehaviour(Base):
    ARGS = ("audit", "--styles", "normal", "--categories", "ordinary", "technical", "--no-controls")

    def test_missing_model_exits_2_without_files(self):
        for cmd in (("audit",), ("suite",)):
            rc, out, err = self.cli(*cmd)
            self.assertEqual(rc, 2)
            self.assertIn("nothing was simulated", err)
        self.assertEqual(self.eval_files(), [])

    def test_unreadable_or_damaged_model_exits_2_without_files(self):
        self.install_model()
        gg = next(Path(self.cfg.models_dir).rglob("*.gguf"))
        gg.write_bytes(b"not a gguf")
        for cmd in (("audit",), ("suite",)):
            rc, out, err = self.cli(*cmd)
            self.assertEqual(rc, 2, err)
            self.assertIn("Cannot run", err)
        self.assertEqual(self.eval_files(), [])

    def test_successful_cli_run_exit_0_and_saves_json_and_markdown(self):
        self.install_model()
        FakeLlama.reply = "Scripted reply."
        rc, out, err = self.cli(*self.ARGS)
        self.assertEqual(rc, 0, err)
        js, md = self.eval_files("personality_audit_*.json"), self.eval_files("personality_audit_*.md")
        self.assertEqual((len(js), len(md)), (1, 1))
        self.assertEqual(self.eval_files("*PARTIAL*"), [], "own partial removed after a successful save")
        self.assertTrue(json.loads(js[0].read_text())["complete"])

    def test_failed_run_exit_1_and_files_say_incomplete(self):
        self.install_model()

        def fac(cfg, guest=False):
            a = REAL_ASSISTANT(cfg, engine_factory=fake_engine_factory, guest=guest)
            a._eng().chat = lambda *x, **k: (_ for _ in ()).throw(RuntimeError("model crashed"))
            return a
        rc, out, err = self.cli(*self.ARGS, assistant_factory=fac)
        self.assertEqual(rc, 1)
        self.assertIn("INCOMPLETE", err)
        rep = json.loads(self.eval_files("personality_audit_*.json")[0].read_text())
        self.assertFalse(rep["complete"])
        self.assertIn("INCOMPLETE RUN", self.eval_files("personality_audit_*.md")[0].read_text())

    def test_output_write_failure_exit_3_clear_message(self):
        self.install_model()
        (self.cfg.data_dir / "eval").write_text("a file where the folder should be")
        rc, out, err = self.cli(*self.ARGS)
        self.assertEqual(rc, 3)
        self.assertIn("could not be saved", err)
        self.assertIn("no earlier report was changed", err)
        rc, out, err = self.cli("suite", "--styles", "normal", "--scenarios", "simple_factual")
        self.assertEqual(rc, 3)
        self.assertIn("could not be saved", err)

    def test_rerun_never_overwrites_earlier_evidence(self):
        self.install_model()
        FakeLlama.reply = "Scripted reply."
        self.assertEqual(self.cli(*self.ARGS)[0], 0)
        first = self.eval_files("personality_audit_*.json")[0]
        before = first.read_bytes()
        for _ in range(3):
            self.assertEqual(self.cli(*self.ARGS)[0], 0)
        self.assertEqual(len(self.eval_files("personality_audit_*.json")), 4)
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(len(self.eval_files("personality_audit_*.md")), 4)

    def test_write_new_refuses_to_overwrite_an_existing_name(self):
        d = Path(self.tmp.name) / "e"
        a = evalsuite.write_new(d, "x", ".json", "one")
        b = evalsuite.write_new(d, "x", ".json", "two")
        self.assertNotEqual(a, b)
        self.assertEqual(a.read_text(), "one")

    def test_two_partial_files_from_different_runs_do_not_collide(self):
        self.install_model()

        def fac(cfg, guest=False):
            a = REAL_ASSISTANT(cfg, engine_factory=fake_engine_factory, guest=guest)
            a._eng().chat = lambda *x, **k: (_ for _ in ()).throw(KeyboardInterrupt())
            return a
        self.cli(*self.ARGS, assistant_factory=fac)
        names = {p.name for p in self.eval_files("*.json")}
        self.assertTrue(names)
        self.assertTrue(all("PARTIAL" not in n or n.startswith("personality_audit_20") for n in names))

    def test_cli_makes_no_network_connection_and_never_downloads(self):
        self.install_model()
        boom = AssertionError("network access attempted")
        with mock.patch.object(socket, "socket", side_effect=boom), mock.patch.object(socket, "create_connection", side_effect=boom):
            rc, out, err = self.cli(*self.ARGS)
            rc2, _, err2 = self.cli("suite", "--styles", "normal", "--scenarios", "simple_factual")
        self.assertEqual((rc, rc2), (0, 0), err + err2)


if __name__ == "__main__":
    unittest.main()
