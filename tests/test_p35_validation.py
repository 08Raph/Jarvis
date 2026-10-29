"""Windows-EXE validation hardening (Stage 5): packaged files/modules check, the "App files" preflight check, the self-test report and exit codes,
NetworkPolicy restoration on every exit path, verdicts for every status, scrubber regression, and the diagnostics integration.

What is REAL here: the diagnostics functions, the runner, history files, NetworkPolicy, the controller's background worker, the self-test report and its
exit-code logic (run with Qt deliberately made unimportable), a fake PyInstaller-style folder built from packaging/manifest.py.
What is FAKE: the machine (scripted Probe), the model host (scripted fetch functions), and the whole Qt part of the self-test (PySide6 is not installable in the
authoring sandbox). NOTHING here ran on Windows, built an EXE or rendered a Qt widget."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "packaging"))

import manifest  # noqa: E402
from p10_common import AppCase  # noqa: E402
from test_p34_readiness import FakeProbe, good_fetch  # noqa: E402
from jarvis.app import diagnostics, selftest as S, status  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.net.policy import NetworkPolicy  # noqa: E402
from jarvis.preflight import history, metadata, runner  # noqa: E402


class Probe35(FakeProbe):
    """FakeProbe plus a scripted answer to 'are the app's own files and modules present?'."""

    def __init__(self, missing_files=(), missing_modules=(), **kw):
        super().__init__(**kw)
        self.mf, self.mm = list(missing_files), list(missing_modules)

    def app_files(self):
        return {"files": {"mode": "packaged", "checked": 17, "missing_required": self.mf, "files": []},
                "modules": {"imported": False, "missing": self.mm, "modules": [{"module": f"m{i}"} for i in range(10)]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()

    def go(self, **kw):
        kw.setdefault("probe", Probe35())
        kw.setdefault("policy", NetworkPolicy())
        return runner.run_preflight(self.cfg, **kw)

    @staticmethod
    def st(rec, cid):
        return next(c for c in rec["checks"] if c["id"] == cid)


# ------------------------------------------------------------------ a defect found by probing the history scrubber
class ScrubberRegression(unittest.TestCase):
    def test_a_bearer_token_value_is_removed_not_just_the_word_before_it(self):
        out = history.scrub_text("request failed: Authorization: Bearer abc.def.ghi-123 (retry)")
        self.assertNotIn("abc.def.ghi-123", out)
        out = history.scrub({"detail": "Authorization: Basic dXNlcjpwYXNz and token=hf_abcdefghijklmnop"})["detail"]
        self.assertNotIn("dXNlcjpwYXNz", out)
        self.assertNotIn("hf_abcdefghijklmnop", out)

    def test_ordinary_text_and_published_hashes_survive(self):
        rec = history.scrub({"d": "Windows 11 (build 22631), 64-bit. 31.9 GB installed.", "sha": "a" * 64, "rev": "b" * 40, "v": "10.0.22631"})
        self.assertEqual(rec, {"d": "Windows 11 (build 22631), 64-bit. 31.9 GB installed.", "sha": "a" * 64, "rev": "b" * 40, "v": "10.0.22631"})


# ------------------------------------------------------------------ packaged files / modules (one implementation, mirrored from the manifest)
class PackagedFiles(unittest.TestCase):
    def bundle(self, root: Path) -> Path:
        """What PyInstaller puts in its data folder, built from the build's own list (packaging/manifest.py)."""
        for src, dest in manifest.DATAS:
            f = root / dest / Path(src).name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")
        for doc in manifest.DOCS:
            f = root / "docs" / doc
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")
        return root

    def frozen(self, root: Path):
        p1 = mock.patch.object(sys, "frozen", True, create=True)
        p2 = mock.patch.object(sys, "_MEIPASS", str(root), create=True)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_the_runtime_lists_mirror_the_build_manifest(self):
        shipped = {(dest + "/" if dest != "." else "") + Path(src).name for src, dest in manifest.DATAS} | {"docs/" + d for d in manifest.DOCS}
        self.assertEqual(set(diagnostics.PACKAGED_FILES), shipped)
        self.assertEqual(set(diagnostics.REQUIRED_MODULES), set(manifest.HIDDEN_REQUIRED))
        self.assertEqual(set(diagnostics.GENERATED_AT_BUILD) - set(diagnostics.PACKAGED_FILES), set())

    def test_a_complete_pyinstaller_style_folder_is_complete_from_an_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as b, tempfile.TemporaryDirectory() as elsewhere:
            self.frozen(self.bundle(Path(b) / "_internal"))
            old = os.getcwd()
            os.chdir(elsewhere)
            try:
                r = diagnostics.packaged_files()
            finally:
                os.chdir(old)           # Windows cannot delete a folder that is the current directory, so leave it BEFORE the temp folders are removed
            self.assertEqual((r["mode"], r["missing_required"]), ("packaged", []))
            self.assertEqual(r["checked"], len(manifest.DATAS) + len(manifest.DOCS))

    def test_missing_files_are_reported_by_name_and_build_info_is_required_when_packaged(self):
        with tempfile.TemporaryDirectory() as b:
            root = self.bundle(Path(b) / "_internal")
            self.frozen(root)
            (root / "docs" / "IN_APP_PREFLIGHT.md").unlink()
            (root / "build_info.json").unlink()
            (root / "jarvis" / "telemetry" / "endpoint.txt").unlink()
            r = diagnostics.packaged_files()
            self.assertEqual(sorted(r["missing_required"]), sorted(["docs/IN_APP_PREFLIGHT.md", "build_info.json", "jarvis/telemetry/endpoint.txt"]))

    def test_the_result_never_contains_an_absolute_path(self):
        with tempfile.TemporaryDirectory() as b:
            self.frozen(self.bundle(Path(b) / "_internal"))
            text = json.dumps(diagnostics.packaged_files())
            self.assertNotIn(b, text)
        self.assertNotIn(str(ROOT), json.dumps(diagnostics.packaged_files()))

    def test_in_a_source_checkout_everything_but_the_generated_build_info_is_required_and_present(self):
        r = diagnostics.packaged_files()
        self.assertEqual((r["mode"], r["missing_required"]), ("source", []))
        self.assertFalse(next(x for x in r["files"] if x["file"] == "build_info.json")["required"])

    def test_required_modules_are_found_and_a_failing_import_is_reported_without_a_path(self):
        self.assertEqual(diagnostics.required_modules(False)["missing"], [])
        with mock.patch.dict(status._IMPORT_CACHE, clear=True):
            r = diagnostics.required_modules(True)
        if status.has_module("PySide6"):
            self.assertEqual(r["missing"], [])
        else:
            self.assertEqual(r["missing"], ["jarvis.ui.window"])        # the only required module that needs Qt at import time
            self.assertIn("PySide6", next(m for m in r["modules"] if m["module"] == "jarvis.ui.window")["problem"])
        self.assertTrue(r["imported"])

    def test_a_module_that_cannot_be_found_is_listed(self):
        with mock.patch.object(diagnostics, "REQUIRED_MODULES", ("jarvis.preflight.runner", "jarvis.preflight.does_not_exist")):
            r = diagnostics.required_modules(False)
        self.assertEqual(r["missing"], ["jarvis.preflight.does_not_exist"])


class DiagnosticsIntegration(AppCase):
    def test_the_report_contains_the_packaged_app_section_and_says_what_it_means(self):
        d = diagnostics.collect(self.cfg, build_info={})
        self.assertEqual(d["packaged_app"]["files"]["missing_required"], [])
        self.assertIn("packaged_app", d["legend"])
        self.assertEqual(d["last_readiness_preflight"], {})
        self.assertTrue(any("readiness" in f.lower() for f in diagnostics.INCLUDED_FIELDS))

    def test_the_newest_saved_preflight_is_summarised_with_its_verdict_only(self):
        runner.run_preflight(self.cfg, probe=Probe35(), policy=NetworkPolicy())
        d = diagnostics.collect(self.cfg, build_info={})
        last = d["last_readiness_preflight"]
        self.assertEqual((last["verdict"], last["readable"], last["include_online"]), ("PARTIAL", True, False))
        self.assertEqual(last["checks"], len(runner.STEPS))
        self.assertNotIn("detail", last)
        self.assertNotIn("blockers", json.dumps(last))


# ------------------------------------------------------------------ the new preflight check, and the verdict for every status
class AppFilesCheck(Base):
    def test_present_files_and_modules_pass_and_say_what_was_looked_at(self):
        rec = self.go()
        c = self.st(rec, "app_files")
        self.assertEqual((c["status"], c["exit_code"], c["needs_internet"]), ("PASS", 0, False))
        self.assertIn("not imported", c["detail"])

    def test_missing_files_are_a_problem_naming_them_and_the_verdict_is_not_passed(self):
        rec = self.go(probe=Probe35(missing_files=["docs/IN_APP_PREFLIGHT.md"], missing_modules=["jarvis.preflight.history"]))
        c = self.st(rec, "app_files")
        self.assertEqual((c["status"], c["exit_code"]), ("PROBLEM", 4))
        self.assertIn("IN_APP_PREFLIGHT.md", c["detail"])
        self.assertIn("jarvis.preflight.history", c["detail"])
        self.assertIn("reinstall", c["detail"])
        self.assertEqual(rec["verdict"]["code"], "PROBLEMS")
        self.assertEqual(self.st(rec, "disk")["status"], "PASS")        # the other checks still ran

    def test_many_missing_items_are_summarised(self):
        rec = self.go(probe=Probe35(missing_files=[f"f{i}" for i in range(12)]))
        self.assertIn("and 4 more", self.st(rec, "app_files")["detail"])

    def test_a_crash_while_looking_is_an_error_not_a_pass(self):
        class Boom(Probe35):
            def app_files(self):
                raise RuntimeError("boom")
        rec = self.go(probe=Boom())
        self.assertEqual(self.st(rec, "app_files")["status"], "ERROR")
        self.assertEqual(rec["verdict"]["code"], "PROBLEMS")

    def test_the_check_is_in_the_fixed_order_after_python_and_is_offline(self):
        ids = [s for s, _ in runner.STEPS]
        self.assertEqual(ids.index("app_files"), ids.index("python") + 1)
        self.assertEqual(len(ids), len(runner.TITLES))
        self.assertEqual(set(ids), set(runner.TITLES))
        self.assertEqual([c["id"] for c in self.go()["checks"]], ids)


class VerdictTable(unittest.TestCase):
    @staticmethod
    def v(*statuses, cancelled=False):
        return runner.verdict([{"status": s} for s in statuses], cancelled)["code"]

    def test_every_status_combination(self):
        P, PR, U, N, E, I = "PASS", "PROBLEM", "UNAVAILABLE", "NOT_RUN", "ERROR", "INFO"
        self.assertEqual(self.v(P, I, P), "PASSED")
        self.assertEqual(self.v(P, N), "PARTIAL")
        self.assertEqual(self.v(P, U), "INCOMPLETE")
        self.assertEqual(self.v(P, U, N), "INCOMPLETE")            # unavailable outranks not-run
        self.assertEqual(self.v(P, PR), "PROBLEMS")
        self.assertEqual(self.v(P, E), "PROBLEMS")
        self.assertEqual(self.v(U, PR, N), "PROBLEMS")            # a problem outranks everything but cancellation
        self.assertEqual(self.v(E, U), "PROBLEMS")
        self.assertEqual(self.v(P, PR, cancelled=True), "CANCELLED")
        self.assertEqual(self.v(P, P, cancelled=True), "CANCELLED")

    def test_only_all_passing_statuses_give_passed(self):
        for bad in ("PROBLEM", "UNAVAILABLE", "NOT_RUN", "ERROR"):
            self.assertNotEqual(self.v("PASS", "PASS", bad), "PASSED", bad)

    def test_the_stored_verdict_is_the_one_derived_from_the_stored_checks(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = Config(data_dir=Path(t) / "data")
            rec = runner.run_preflight(cfg, probe=Probe35(missing_files=["x"]), policy=NetworkPolicy())
            self.assertEqual(rec["verdict"]["code"], runner.verdict(rec["checks"], False)["code"])


# ------------------------------------------------------------------ NetworkPolicy is restored on EVERY way out of the online step
class NetworkRestoration(Base):
    def online(self, fetch, policy=None, **kw):
        policy = policy if policy is not None else NetworkPolicy()
        return self.go(include_online=True, online_consent=True, fetch=fetch, policy=policy, **kw), policy

    def assertOthersUntouched(self, policy):
        self.assertEqual([f for f, on in policy.enabled.items() if on and f != "model_download"], [])

    def test_success(self):
        rec, p = self.online(good_fetch())
        self.assertEqual(self.st(rec, "metadata")["status"], "PASS")
        self.assertFalse(p.is_enabled("model_download"))
        self.assertOthersUntouched(p)

    def test_host_unavailable(self):
        def down(repo):
            raise metadata.ApiUnavailable("network error")
        rec, p = self.online(down)
        self.assertEqual(self.st(rec, "metadata")["status"], "UNAVAILABLE")
        self.assertFalse(p.is_enabled("model_download"))

    def test_an_unexpected_exception_in_the_fetch_is_an_error_and_the_permission_is_still_restored(self):
        def boom(repo):
            raise RuntimeError("unexpected")
        rec, p = self.online(boom)
        self.assertEqual(self.st(rec, "metadata")["status"], "ERROR")
        self.assertEqual(rec["verdict"]["code"], "PROBLEMS")
        self.assertFalse(p.is_enabled("model_download"))

    def test_an_interrupt_escapes_but_the_permission_is_restored(self):
        def stop(repo):
            raise KeyboardInterrupt
        p = NetworkPolicy()
        with self.assertRaises(KeyboardInterrupt):
            self.online(stop, policy=p)
        self.assertFalse(p.is_enabled("model_download"))

    def test_cancellation_in_the_middle(self):
        calls = {"n": 0}
        base = good_fetch()

        def fetch(repo):
            calls["n"] += 1
            return base(repo)
        rec, p = self.online(fetch, should_stop=lambda: calls["n"] >= 1)
        self.assertEqual(rec["verdict"]["code"], "CANCELLED")
        self.assertFalse(p.is_enabled("model_download"))

    def test_a_permission_that_was_already_on_stays_on_on_every_path(self):
        def down(repo):
            raise metadata.ApiUnavailable("x")

        def boom(repo):
            raise RuntimeError("x")
        for fetch in (good_fetch(), down, boom):
            p = NetworkPolicy()
            p.enable("model_download")
            self.online(fetch, policy=p)
            self.assertTrue(p.is_enabled("model_download"))

    def test_no_online_request_and_no_permission_change_without_the_online_step(self):
        p = NetworkPolicy()
        seen = []
        rec = self.go(policy=p, fetch=lambda repo: seen.append(repo))
        self.assertEqual((seen, rec["include_online"]), ([], False))
        self.assertFalse(any(p.enabled.values()))

    def test_the_test_would_notice_a_policy_that_does_not_restore(self):
        """Proves the assertions above can fail: with a grant that never switches the feature off again, the success-path assertion is violated."""
        from contextlib import contextmanager

        @contextmanager
        def leaky(self, feature, cost_acknowledged=False):
            self.enable(feature, cost_acknowledged)
            yield
        with mock.patch.object(NetworkPolicy, "grant_once", leaky):
            rec, p = self.online(good_fetch())
        self.assertTrue(p.is_enabled("model_download"))


# ------------------------------------------------------------------ the self-test report, record validation and exit codes (Qt-free parts)
def good_record(cfg, **kw):
    kw.setdefault("probe", Probe35())
    kw.setdefault("policy", NetworkPolicy())
    return runner.run_preflight(cfg, **kw)


class SelfTestReport(Base):
    def test_exit_codes_and_first_line(self):
        r = S.Report()
        r.add("a", "A", S.PASS, S.GENUINE, "fine")
        r.add("b", "B", S.INFO, S.SIMULATED, "stand-in")
        r.add("c", "C", S.NOT_RUN, S.NOT_DONE, "not done")
        self.assertEqual((r.exit_code(), r.first_line()), (0, "PASS"))
        r.add("d", "D", S.FAIL, S.GENUINE, "broken")
        self.assertEqual(r.exit_code(), 1)
        self.assertTrue(r.first_line().startswith("FAIL 1 check(s) failed: d"))
        r.exit_override = S.EXIT_NO_QT
        self.assertEqual(r.exit_code(), 3)
        self.assertTrue(r.first_line().startswith("FAIL Qt"))

    def test_not_run_and_simulated_entries_are_not_counted_as_passes_and_are_labelled(self):
        r = S.Report()
        S.add_limits(r)
        r.add("m", "mode", S.INFO, S.SIMULATED, "simulate")
        txt = r.text()
        self.assertIn("[NOT_RUN] (not_run) model_inference", txt)
        self.assertIn("[INFO] (simulated) m", txt)
        self.assertIn("does not show that a model loads", txt)
        self.assertEqual([c["kind"] for c in r.as_dict()["checks"]], ["not_run", "not_run", "simulated"])

    def test_details_are_scrubbed_and_bounded(self):
        r = S.Report()
        r.add("x", "X", S.FAIL, S.GENUINE, "failed at Authorization: Bearer secret-value-123 " + "z" * 2000)
        d = r.checks[0]["detail"]
        self.assertNotIn("secret-value-123", d)
        self.assertLessEqual(len(d), S.MAX_DETAIL)

    def test_write_makes_both_files_atomically(self):
        r = S.Report()
        r.add("a", "A", S.PASS, S.GENUINE, "ok")
        p = S.write(r, self.d)
        self.assertEqual(p.name, "jarvis-gui-selftest.txt")
        self.assertTrue(p.read_text(encoding="utf-8").startswith("PASS"))
        j = json.loads((self.d / "jarvis-gui-selftest.json").read_text(encoding="utf-8"))
        self.assertEqual((j["kind"], j["exit_code"]), ("gui-selftest", 0))
        self.assertEqual(sorted(x.name for x in self.d.glob("jarvis-gui-selftest*")), ["jarvis-gui-selftest.json", "jarvis-gui-selftest.txt"])

    def test_file_and_module_checks_pass_in_the_source_tree_and_fail_when_something_is_missing(self):
        r = S.Report()
        S.check_files(r)
        self.assertEqual(r.checks[0]["result"], S.PASS)
        r2 = S.Report()
        with mock.patch.object(diagnostics, "packaged_files", return_value={"mode": "packaged", "checked": 17, "missing_required": ["docs/IN_APP_PREFLIGHT.md"], "files": []}):
            S.check_files(r2)
        self.assertEqual((r2.checks[0]["result"], r2.exit_code()), (S.FAIL, 1))
        self.assertIn("IN_APP_PREFLIGHT.md", r2.checks[0]["detail"])
        r3 = S.Report()
        with mock.patch.object(diagnostics, "required_modules", return_value={"imported": True, "missing": ["jarvis.preflight.runner"],
                                                                                "modules": [{"module": "jarvis.preflight.runner", "present": False, "problem": "ImportError: x"}]}):
            S.check_modules(r3)
        self.assertEqual(r3.checks[0]["result"], S.FAIL)


class ValidateRecord(Base):
    def test_a_real_offline_record_is_well_formed_and_unsaved_or_saved_is_judged(self):
        rec = good_record(self.cfg)
        self.assertEqual(S.validate_record(rec), [])
        r = S.Report()
        S.check_preflight(r, rec, self.cfg)
        self.assertEqual(r.checks[0]["result"], S.PASS)
        self.assertIn("no internet used", r.checks[0]["detail"])

    def test_findings_about_this_pc_are_not_selftest_failures(self):
        rec = good_record(self.cfg, probe=Probe35(free=1_000_000_000, ram=4.0, runtime={"available": False, "error": "missing"}))
        self.assertEqual(self.st(rec, "disk")["status"], "PROBLEM")
        self.assertEqual(S.validate_record(rec), [])

    def test_each_kind_of_malformed_record_is_caught(self):
        base = good_record(self.cfg)
        import copy

        def mutated(fn):
            r = copy.deepcopy(base)
            fn(r)
            return S.validate_record(r)
        self.assertTrue(S.validate_record(None))
        self.assertTrue(mutated(lambda r: r["checks"].pop()))
        self.assertTrue(any("crashed" in p for p in mutated(lambda r: r["checks"][0].update(status="ERROR"))))     # named as a crash, not just "unknown"
        self.assertTrue(mutated(lambda r: r["checks"][0].update(status="MAYBE")))
        self.assertTrue(mutated(lambda r: r.update(include_online=True)))
        self.assertTrue(mutated(lambda r: next(c for c in r["checks"] if c["id"] == "metadata").update(status="PASS")))
        self.assertTrue(mutated(lambda r: r["statements"].update(model_downloaded=True)))
        self.assertTrue(mutated(lambda r: r["verdict"].update(code="PASSED")))
        self.assertTrue(mutated(lambda r: r.pop("verdict")))

    def test_a_run_missing_from_the_history_fails(self):
        rec = good_record(self.cfg)
        r = S.Report()
        with mock.patch.object(history, "list_runs", return_value=[]):
            S.check_preflight(r, rec, self.cfg)
        self.assertEqual(r.checks[0]["result"], S.FAIL)
        self.assertIn("not saved", r.checks[0]["detail"])


class ViaController(AppCase):
    """The loop the packaged self-test uses, against the real controller and worker (pump = the worker's own queue instead of Qt events)."""

    def ctl(self):
        c = AppController(self.cfg, simulate=True)
        self.addCleanup(c.shutdown, 1.0)
        return c

    def run_it(self, c, shown=None, timeout=15.0, fn=None):
        real = runner.run_preflight

        def spy(cfg, **kw):
            return fn(cfg, **kw) if fn else real(cfg, probe=Probe35(), policy=NetworkPolicy(), **kw)
        rep = S.Report()
        with mock.patch.object(runner, "run_preflight", spy):
            S.run_preflight_via_controller(rep, c, self.cfg, c.tasks.pump, shown=shown, timeout=timeout)
        return rep

    def test_a_successful_offline_run_passes_and_the_page_rows_are_compared(self):
        rep = self.run_it(self.ctl(), shown=lambda: (len(runner.STEPS), "PARTIAL: x"))
        self.assertEqual([(c["id"], c["result"]) for c in rep.checks], [("preflight_offline", "PASS"), ("qt_result_table", "PASS")])
        self.assertEqual(rep.exit_code(), 0)

    def test_a_page_that_shows_the_wrong_number_of_rows_fails(self):
        rep = self.run_it(self.ctl(), shown=lambda: (0, ""))
        self.assertEqual(rep.exit_code(), 1)
        self.assertEqual(rep.failed(), ["qt_result_table"])

    def test_a_worker_error_is_a_failure_with_the_reason(self):
        def boom(cfg, **kw):
            raise RuntimeError("worker exploded")
        rep = self.run_it(self.ctl(), fn=boom)
        self.assertEqual(rep.failed(), ["preflight_offline"])
        self.assertIn("RuntimeError", rep.checks[0]["detail"])

    def test_a_refused_start_is_a_failure(self):
        c = self.ctl()
        c.preflight["running"] = False
        with mock.patch.object(c, "preflight_start", return_value=False):
            rep = S.Report()
            S.run_preflight_via_controller(rep, c, self.cfg, c.tasks.pump)
        self.assertEqual(rep.failed(), ["preflight_offline"])

    def test_a_run_that_never_finishes_times_out_and_is_cancelled(self):
        c = self.ctl()
        gate = threading.Event()

        def slow(cfg, should_stop=None, **kw):
            gate.wait(5)
            return {"verdict": {"code": "CANCELLED", "text": "t"}, "checks": []}
        self.addCleanup(gate.set)
        rep = self.run_it(c, timeout=0.3, fn=slow)
        self.assertEqual(rep.failed(), ["preflight_offline"])
        self.assertIn("did not finish", rep.checks[0]["detail"])
        gate.set()

    def test_the_run_uses_no_online_check(self):
        seen = {}
        real = runner.run_preflight

        def spy(cfg, **kw):
            seen.update(kw)
            return real(cfg, probe=Probe35(), policy=NetworkPolicy(), **{k: v for k, v in kw.items() if k not in ("probe", "policy")})
        rep = self.run_it(self.ctl(), fn=spy)
        self.assertFalse(seen.get("include_online"))
        self.assertFalse(seen.get("online_consent"))
        self.assertEqual(rep.exit_code(), 0)


class SelfTestProgram(Base):
    """selftest() itself, with PySide6 made unimportable (it is not installed here anyway). The Qt part is NOT exercised."""

    def blocked(self):
        p = mock.patch.dict(sys.modules, {"PySide6": None, "PySide6.QtWidgets": None})
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {})
        e.start()
        self.addCleanup(e.stop)

    def test_without_qt_the_exit_code_is_3_and_the_files_say_why(self):
        self.blocked()
        from jarvis.ui.app import selftest
        out = self.d / "out"
        out.mkdir()
        code = selftest(out)
        self.assertEqual(code, 3)
        txt = (out / "jarvis-gui-selftest.txt").read_text(encoding="utf-8")
        self.assertTrue(txt.startswith("FAIL Qt"), txt[:80])
        self.assertFalse(txt.startswith("PASS"))
        j = json.loads((out / "jarvis-gui-selftest.json").read_text(encoding="utf-8"))
        by = {c["id"]: c for c in j["checks"]}
        self.assertEqual(by["qt_load"]["result"], "FAIL")
        self.assertEqual(by["app_files"]["result"], "PASS")        # the file check still ran and is reported on its own
        self.assertNotIn("qt_window", by)                         # nothing window-related is claimed
        self.assertEqual(j["exit_code"], 3)

    def test_a_stale_pass_is_replaced_at_the_very_start(self):
        self.blocked()
        from jarvis.ui.app import selftest
        out = self.d / "out"
        out.mkdir()
        (out / "jarvis-gui-selftest.txt").write_text("PASS\n", encoding="utf-8")
        seen = {}
        real = S.check_files

        def spy(rep):
            seen["during"] = (out / "jarvis-gui-selftest.txt").read_text(encoding="utf-8")
            real(rep)
        with mock.patch.object(S, "check_files", spy):
            selftest(out)
        self.assertTrue(seen["during"].startswith("RUNNING"))

    def test_a_crash_inside_the_checks_is_recorded_not_raised_and_never_passes(self):
        self.blocked()
        from jarvis.ui.app import selftest
        out = self.d / "out"
        out.mkdir()
        with mock.patch.object(S, "check_files", side_effect=RuntimeError("kaboom")):
            code = selftest(out)
        self.assertNotEqual(code, 0)
        j = json.loads((out / "jarvis-gui-selftest.json").read_text(encoding="utf-8"))
        self.assertEqual(next(c for c in j["checks"] if c["id"] == "selftest_crash")["result"], "FAIL")

    def test_an_unwritable_output_folder_does_not_hide_the_exit_code(self):
        self.blocked()
        from jarvis.ui.app import selftest
        self.assertEqual(selftest(self.d / "no" / "such" / "folder"), 3)

    def test_the_self_test_never_uses_the_real_data_folder_or_the_network(self):
        src = (ROOT / "src" / "jarvis" / "ui" / "app.py").read_text(encoding="utf-8")
        body = src[src.index("def _qt_checks"):]
        for needle in ("subprocess", "urllib", "socket", "default_data_dir", "os.system", "shell=True"):
            self.assertNotIn(needle, body, needle)
        self.assertIn("TemporaryDirectory", body)
        self.assertIn("preflight_start(False, False)", (ROOT / "src" / "jarvis" / "app" / "selftest.py").read_text(encoding="utf-8"))

    def test_the_gui_entry_point_returns_the_self_test_exit_code(self):
        t = (ROOT / "packaging" / "entry_gui.py").read_text(encoding="utf-8")
        self.assertIn("raise SystemExit(selftest())", t)

    def test_the_build_script_still_reads_pass_from_the_first_line(self):
        ps = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("StartsWith(\"PASS\")", ps)
        self.assertIn("jarvis-gui-selftest.txt", ps)


class WindowSource(unittest.TestCase):
    def test_the_page_offers_the_diagnostics_and_never_a_process_or_network_call(self):
        w = (ROOT / "src" / "jarvis" / "ui" / "window.py").read_text(encoding="utf-8")
        self.assertIn("def _pf_diag", w)
        self.assertIn('PAGES.index("Status")', w)
        self.assertIn("Show diagnostics", w)
        for needle in ("subprocess", "urllib", "socket", "os.system"):
            self.assertNotIn(needle, w)

    def test_the_new_modules_are_in_the_package_so_the_exe_contains_them(self):
        self.assertTrue((ROOT / "src" / "jarvis" / "app" / "selftest.py").is_file())
        self.assertIn("scripts", manifest.FORBIDDEN_TOP_LEVEL)           # scripts/ is excluded from the EXE, so nothing here may depend on it
        hidden = set(manifest.HIDDEN_REQUIRED)
        self.assertTrue({"jarvis.preflight.runner", "jarvis.preflight.history", "jarvis.preflight.metadata", "jarvis.preflight.hf_fetch"} <= hidden)


if __name__ == "__main__":
    unittest.main()
