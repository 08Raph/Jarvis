"""Prompt 12 tests. LABELS: REAL = real files/SQLite/executor code on this (Linux) machine. SIMULATED = another machine or Windows behaviour is
faked by passing values in or patching; such a test shows the LOGIC reacts correctly, NOT that Windows behaves that way. Nothing here ran on
Windows, with Qt, PyInstaller, Inno Setup, PowerShell or a real Qwen model."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase, ROOT  # noqa: E402  (also sets sys.path for src, scripts, packaging)

import check_build_env as CBE  # noqa: E402
import check_release_assets as CRA  # noqa: E402
import release_tools as RT  # noqa: E402
from jarvis.app import diagnostics, status  # noqa: E402
from jarvis.inference import gguf_engine as ge  # noqa: E402


def by_name(results):
    return {r["check"]: r for r in results}


class BuildEnvironmentCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_sim(self, **kw):
        return by_name(CBE.run_checks(ROOT, **kw))

    def test_REAL_this_checkout_has_no_blocking_failure_and_internet_is_not_claimed(self):
        r = by_name(CBE.run_checks(ROOT))
        self.assertEqual([k for k, v in r.items() if v["state"] == "fail"], [])
        self.assertEqual(r["internet for pip"]["state"], "not_run")        # never reported as ok without being tested
        self.assertEqual(r["project files"]["state"], "ok")

    def test_SIMULATED_unsupported_python_and_32bit_fail(self):
        self.assertEqual(self.run_sim(version_info=(3, 10, 9))["python version"]["state"], "fail")
        self.assertEqual(self.run_sim(version_info=(3, 14, 0))["python version"]["state"], "fail")
        self.assertEqual(self.run_sim(version_info=(3, 12, 4))["python version"]["state"], "ok")
        self.assertEqual(self.run_sim(is_64bit=False)["python is 64-bit"]["state"], "fail")

    def test_SIMULATED_disk_space_levels(self):
        self.assertEqual(self.run_sim(free_bytes=int(1e9))["free disk space"]["state"], "fail")
        self.assertEqual(self.run_sim(free_bytes=int(5e9))["free disk space"]["state"], "warn")
        self.assertEqual(self.run_sim(free_bytes=int(50e9))["free disk space"]["state"], "ok")

    def test_SIMULATED_other_os_is_a_warning_not_a_pass(self):
        self.assertEqual(self.run_sim(platform_name="linux")["operating system"]["state"], "warn")
        self.assertEqual(self.run_sim(platform_name="win32")["operating system"]["state"], "ok")

    def test_REAL_bad_paths_warn(self):
        long = Path(self.tmp.name) / ("x" * 90)
        long.mkdir()
        for need in ("pyproject.toml", "packaging/jarvis.spec", "packaging/manifest.py", "packaging/installer.iss", "scripts/build_windows.ps1", "src/jarvis/version.py"):
            f = long / need
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        r = by_name(CBE.run_checks(long))
        self.assertEqual(r["path length"]["state"], "warn")
        one = Path(self.tmp.name) / "OneDrive" / "uniçode"
        one.mkdir(parents=True)
        r2 = by_name(CBE.run_checks(one))
        self.assertEqual(r2["path characters"]["state"], "warn")
        self.assertEqual(r2["path is not a synced folder"]["state"], "warn")
        self.assertEqual(r2["project files"]["state"], "fail")             # an incomplete copy is refused

    def test_main_exit_code_follows_failures(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(CBE, "run_checks", return_value=[CBE._r("x", "fail", "bad")]):
                self.assertEqual(CBE.main([]), 1)
            with mock.patch.object(CBE, "run_checks", return_value=[CBE._r("x", "warn", "meh"), CBE._r("y", "not_run", "?", "not checked")]):
                self.assertEqual(CBE.main([]), 0)

    def test_STATIC_build_script_calls_the_new_safeguards(self):
        """Text check only: PowerShell is not available here, so the script has NEVER been parsed or run."""
        t = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        for needle in ("check_build_env.py", "pip freeze --exclude-editable", "build-manifest", "--manifest dist\\build_manifest.json", "-LockFile"):
            self.assertIn(needle, t)
        self.assertLess(t.index("check_build_env.py"), t.index("-m venv .venv"))                  # preflight runs before anything is created
        self.assertLess(t.index('foreach ($d in @("build", "dist"))'), t.index("PyInstaller packaging"))   # outputs emptied before the build
        self.assertNotIn("if ($Clean) { foreach ($d in @(\"build\", \"dist\"))", t)               # emptying is no longer optional


class BuildManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.dist = Path(self.tmp.name) / "dist" / "ProjectJarvis"
        (self.root / "packaging").mkdir(parents=True)
        (self.root / "packaging" / "jarvis.spec").write_text("spec")
        (self.root / "packaging" / "manifest.py").write_text("m")
        (self.dist / "docs").mkdir(parents=True)
        for n, c in (("jarvis.exe", b"MZ" + b"0" * 10), ("jarvis-gui.exe", b"MZ" + b"1" * 10), ("LICENSE", b"mit"), ("docs/a.md", b"doc")):
            (self.dist / n).write_bytes(c)
        self.inst = Path(self.tmp.name) / "dist" / "installer"
        self.inst.mkdir()
        (self.inst / "ProjectJarvis-Setup-0.6.0.exe").write_bytes(b"installer")
        self.out = Path(self.tmp.name) / "dist" / "build_manifest.json"

    def make(self, **kw):
        m = RT.build_manifest(self.root, self.dist, self.inst, env={"SOURCE_DATE_EPOCH": "1700000000"}, versions=kw.get("versions", {"torch": None, "PySide6": "6.7.0"}))
        self.out.write_text(json.dumps(m), encoding="utf-8")
        return m

    def test_REAL_manifest_records_versions_timestamp_and_every_hash(self):
        m = self.make()
        self.assertEqual(m["built_utc"], "2023-11-14T22:13:20Z")                  # SOURCE_DATE_EPOCH honoured
        self.assertEqual(m["dependencies"]["PySide6"], "6.7.0")
        self.assertEqual(m["file_count"], 4)
        self.assertEqual(set(m["executables"]), {"jarvis.exe", "jarvis-gui.exe"})
        self.assertEqual(len(m["installer"]["ProjectJarvis-Setup-0.6.0.exe"]["sha256"]), 64)
        self.assertEqual(m["files"]["LICENSE"], CRA.sha256_of(self.dist / "LICENSE"))
        self.assertTrue(m["python"].count(".") >= 1)
        self.assertFalse(m["torch_bundled"])
        self.assertEqual(CRA.verify_build_manifest(self.out, self.dist), [])

    def test_REAL_verification_detects_change_removal_and_addition(self):
        self.make()
        (self.dist / "LICENSE").write_bytes(b"tampered")
        (self.dist / "docs" / "a.md").unlink()
        (self.dist / "extra.txt").write_text("new")
        probs = "\n".join(CRA.verify_build_manifest(self.out, self.dist))
        self.assertIn("changed since the manifest was written: LICENSE", probs)
        self.assertIn("listed in the manifest but missing: docs/a.md", probs)
        self.assertIn("present but not in the manifest: extra.txt", probs)

    def test_REAL_unreadable_or_foreign_manifest_is_a_problem_not_a_pass(self):
        self.out.write_text("{not json", encoding="utf-8")
        self.assertTrue(CRA.verify_build_manifest(self.out, self.dist))
        self.out.write_text(json.dumps({"schema": 2, "files": {}}), encoding="utf-8")
        self.assertTrue(CRA.verify_build_manifest(self.out, self.dist))
        self.assertTrue(CRA.verify_build_manifest(self.out.with_name("none.json"), self.dist))

    def test_REAL_tree_hash_cannot_be_edited_to_hide_a_change(self):
        m = self.make()
        (self.dist / "LICENSE").write_bytes(b"tampered")
        m["files"]["LICENSE"] = CRA.sha256_of(self.dist / "LICENSE")          # attacker fixes the per-file entry but not the tree hash
        self.out.write_text(json.dumps(m), encoding="utf-8")
        self.assertIn("tree_sha256", "\n".join(CRA.verify_build_manifest(self.out, self.dist)))

    def test_REAL_machine_paths_in_text_files_are_found_and_blocked(self):
        (self.dist / "build_info.json").write_text(json.dumps({"path": str(self.root)}), encoding="utf-8")
        self.assertEqual(CRA.scan_machine_paths(self.dist, [str(self.root)]), ["build_info.json"])
        probs = CRA.check_dist(self.dist, [str(self.root)])
        self.assertTrue(any("build machine" in p for p in probs))
        m = self.make()
        self.assertEqual(m["machine_paths_in_text_files"], ["build_info.json"])

    def test_REAL_model_weights_in_the_build_are_flagged(self):
        big = self.dist / "model.gguf"
        with open(big, "wb") as f:
            f.truncate(60_000_000)                                             # sparse file, no real disk use
        self.assertTrue(any("gguf" in p for p in CRA.check_dist(self.dist)))     # forbidden pattern
        self.assertEqual(self.make()["model_weights_in_build"], ["model.gguf"])

    def test_REAL_torch_bundling_is_reported_from_the_files(self):
        (self.dist / "_internal" / "torch").mkdir(parents=True)
        (self.dist / "_internal" / "torch" / "x.pyd").write_bytes(b"1")
        self.assertTrue(self.make(versions={"torch": "2.4.0"})["torch_bundled"])
        self.assertFalse(self.make(versions={"torch": None})["torch_bundled"])

    def test_REAL_installed_versions_marks_missing_packages_as_none(self):
        v = RT.installed_versions(["definitely-not-installed-xyz", "numpy"])
        self.assertIsNone(v["definitely-not-installed-xyz"])
        self.assertTrue(v["numpy"])

    def test_REAL_build_info_now_lists_dependency_versions(self):
        self.assertIn("dependencies", RT.build_info(ROOT, env={}))


class RuntimeIsMeasuredNotAssumed(AppCase):
    def test_SIMULATED_present_but_unloadable_runtime_is_not_reported_ready(self):
        """On Windows a package can exist and still fail to load (missing DLL). The window must not say 'ready'."""
        self.install_model()
        status._IMPORT_CACHE.clear()
        real = status.importlib.import_module

        def boom(name, *a, **k):
            if name == "llama_cpp":
                raise ImportError("DLL load failed while importing _llama: The specified module could not be found.")
            return real(name, *a, **k)
        with mock.patch.object(status, "has_module", return_value=True), mock.patch.object(status.importlib, "import_module", boom):
            s = status.model_status(self.cfg)
            rc = status.runtime_checks()
        status._IMPORT_CACHE.clear()
        self.assertEqual(s.state, "runtime_missing")
        self.assertIn("could not be loaded", s.summary)
        self.assertIn("Visual C++", s.action)
        self.assertIs(rc["llama_cpp"]["loads"], False)
        self.assertIn("DLL load failed", rc["llama_cpp"]["problem"])

    def test_REAL_absent_package_is_unmeasured_not_false(self):
        status._IMPORT_CACHE.clear()
        rc = status.runtime_checks()
        if not status.has_module("llama_cpp"):
            self.assertIsNone(rc["llama_cpp"]["loads"])

    def test_REAL_import_result_is_cached_so_the_window_does_not_reload_libraries(self):
        status._IMPORT_CACHE.clear()
        calls = []
        real = status.importlib.import_module
        with mock.patch.object(status.importlib, "import_module", lambda n, *a, **k: (calls.append(n), real("json"))[1]):
            status.import_problem("whatever_a")
            status.import_problem("whatever_a")
        status._IMPORT_CACHE.clear()
        self.assertEqual(calls, ["whatever_a"])

    def test_REAL_diagnostics_separates_found_on_disk_from_measured(self):
        d = diagnostics.collect(self.cfg, build_info={})
        self.assertIn("runtime_checks", d)
        self.assertIn("NOT imported", d["legend"]["optional_dependencies"])
        self.assertIn("measured", d["legend"]["runtime_checks"])
        text = diagnostics.render_text(d)
        self.assertNotIn(str(Path.home()), text.replace("~", ""))      # the home folder never appears

    def test_REAL_damaged_meta_json_is_a_clear_runtime_error_and_file_is_kept(self):
        d = self.d / "m"
        d.mkdir()
        (d / "model.gguf").write_bytes(b"GGUF")
        (d / "meta.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(ge.RuntimeUnavailable) as cm:
            ge.GGUFEngine(d, llama_factory=lambda **k: object())
        self.assertIn("meta.json", str(cm.exception))
        self.assertEqual((d / "meta.json").read_text(encoding="utf-8"), "{broken")
        (d / "meta.json").write_text("[1,2]", encoding="utf-8")
        with self.assertRaises(ge.RuntimeUnavailable):
            ge.GGUFEngine(d, llama_factory=lambda **k: object())


class StartupConsistency(unittest.TestCase):
    def run_gui_main(self, env):
        from jarvis.ui import app as A
        seen = {}
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("jarvis.cli.default_data_dir", return_value=Path(t)), \
                mock.patch.object(A, "run_gui", side_effect=lambda cfg, **k: seen.update(cfg=cfg) or 0), \
                mock.patch.object(A, "_report", side_effect=lambda title, text: seen.update(report=(title, text))):
            os.environ.pop("JARVIS_PROFILE", None) if "JARVIS_PROFILE" not in env else None
            rc = A.gui_main()
        return rc, seen

    def test_REAL_window_uses_the_same_profile_rule_as_the_command_line(self):
        rc, seen = self.run_gui_main({"JARVIS_PROFILE": "work"})
        self.assertEqual((rc, seen["cfg"].profile), (0, "work"))
        rc, seen = self.run_gui_main({})
        self.assertEqual(seen["cfg"].profile, "default")

    def test_REAL_invalid_profile_setting_is_explained_and_nothing_starts(self):
        rc, seen = self.run_gui_main({"JARVIS_PROFILE": "../evil"})
        self.assertEqual(rc, 2)
        self.assertNotIn("cfg", seen)
        self.assertIn("JARVIS_PROFILE", seen["report"][1])

    def test_REAL_crash_message_does_not_pretend_a_log_was_saved(self):
        from jarvis import launcher
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(launcher, "write_crash_log", return_value=None):
            rc = launcher.run(lambda a: (_ for _ in ()).throw(RuntimeError("boom")), argv=["x"], input_fn=lambda p: "", ours=lambda: False)
        self.assertEqual(rc, 1)
        self.assertIn("could not be saved", buf.getvalue())
        self.assertNotIn("saved on this PC only: None", buf.getvalue())

    def test_SIMULATED_qt_failing_to_load_gives_a_plain_message(self):
        from jarvis.ui import app as A
        from jarvis.config import Config
        seen = {}
        with tempfile.TemporaryDirectory() as t, mock.patch.object(A, "qt_available", return_value=True), \
                mock.patch.object(A, "_report", side_effect=lambda ti, tx: seen.update(r=(ti, tx))), \
                mock.patch.dict(sys.modules, {"PySide6": None, "PySide6.QtWidgets": None}):
            rc = A.run_gui(Config(data_dir=Path(t)))
        self.assertEqual(rc, 3)
        self.assertIn("Visual C++", seen["r"][1])

    def test_REAL_sample_config_no_longer_pretends_to_be_read(self):
        t = (ROOT / "config.sample.toml").read_text(encoding="utf-8")
        self.assertIn("does NOT read", t)
        self.assertIn("forward slashes", t)
        import tomllib
        tomllib.loads(t)                                              # still valid TOML


class ConfirmationAnswersNeverApproveByAccident(unittest.TestCase):
    def test_REAL_only_exact_true_or_matching_approval_counts(self):
        from jarvis.desktop.executor import Approval, _check_approval
        fp = "a" * 64
        for bad in ("yes", "True", 1, 2, [True], {"approved": True}, object(), Approval(True, "b" * 64), Approval(1, fp), Approval("yes", fp), None, False):
            self.assertFalse(_check_approval(bad, fp)[0], repr(bad))
        self.assertTrue(_check_approval(True, fp)[0])
        self.assertTrue(_check_approval(Approval(True, fp), fp)[0])


if __name__ == "__main__":
    unittest.main()
