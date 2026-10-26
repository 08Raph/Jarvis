"""Prompt 10: version, packaging manifest, build checker, installer script rules, command-line entry points and the upgrade/data-preservation logic.
REAL: files and scripts in this repository. NOT RUN: PyInstaller, Inno Setup, a Windows machine - these tests only check the CONFIGURATION is consistent and
that the checker fails clearly; they prove nothing about a built executable or installer."""
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from p10_common import AppCase, ROOT
import check_release_assets as CRA
import manifest as MF
import release_tools as RT
from jarvis import cli, version


class VersionTests(unittest.TestCase):
    def test_single_authoritative_version(self):
        import jarvis
        self.assertEqual(jarvis.__version__, version.__version__)
        self.assertEqual(CRA.read_version(), version.__version__)
        py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(re.search(r'^version\s*=\s*"([^"]+)"', py, re.M).group(1), version.__version__)
        self.assertRegex(version.__version__, r"^\d+\.\d+\.\d+$")

    def test_channel_is_not_claiming_production(self):
        self.assertIn(version.RELEASE_CHANNEL, ("preview", "alpha", "beta", "dev"))
        rn = (ROOT / "docs" / "RELEASE_NOTES.md").read_text(encoding="utf-8")
        self.assertIn(version.__version__, rn)
        self.assertIn("Not production-ready", rn)
        self.assertIn("No Windows executable or installer has been built", rn)

    def test_build_script_and_installer_take_version_from_one_place(self):
        ps = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("from jarvis.version import __version__", ps)
        self.assertIn("/DAppVersion=$ver", ps)

    def test_version_command_matches(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["version"])
        self.assertIn(version.__version__, out.getvalue())

    def test_python_support_range_in_metadata(self):
        py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.11"', py.replace("'", '"'))


class ManifestAndChecker(unittest.TestCase):
    def test_source_check_passes_on_this_tree(self):
        self.assertEqual(CRA.check_source(), [])

    def test_every_manifest_file_exists(self):
        for src, _ in MF.DATAS:
            if src != "build_info.json":
                self.assertTrue((ROOT / src).is_file(), src)
        for d in MF.DOCS:
            self.assertTrue((ROOT / "docs" / d).is_file(), d)

    def test_maintainer_docs_and_secrets_are_not_shipped(self):
        for d in MF.DOCS:
            self.assertFalse(d.startswith("PROMPT") or d.endswith("CHECKLIST.md") or "REVIEW" in d, d)
        for name in ("*.gguf", "*.pt", "*.safetensors", "*.pem", "*.key", "*.sqlite"):
            self.assertIn(name, MF.FORBIDDEN_PATTERNS)

    def test_missing_mandatory_file_blocks_the_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shutil.copytree(ROOT / "src", root / "src")
            for sub in ("packaging", "scripts", "docs"):
                shutil.copytree(ROOT / sub, root / sub)
            for f in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "config.sample.toml"):
                shutil.copy(ROOT / f, root / f)
            self.assertEqual(CRA.check_source(root), [])
            (root / "LICENSE").unlink()
            (root / "docs" / "MODEL_DISTRIBUTION.md").unlink()
            probs = " | ".join(CRA.check_source(root))
            self.assertIn("LICENSE", probs)
            self.assertIn("MODEL_DISTRIBUTION.md", probs)

    def test_version_mismatch_blocks_the_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for sub in ("src", "packaging", "scripts", "docs"):
                shutil.copytree(ROOT / sub, root / sub)
            for f in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "config.sample.toml"):
                shutil.copy(ROOT / f, root / f)
            py = (root / "pyproject.toml").read_text(encoding="utf-8")
            (root / "pyproject.toml").write_text(re.sub(r'(?m)^version = "[^"]+"', 'version = "9.9.9"', py), encoding="utf-8")
            self.assertTrue(any("does not match" in p for p in CRA.check_source(root)))

    def test_missing_mandatory_package_is_reported(self):
        self.assertEqual(CRA.missing_packages(["json", "os"]), [])
        self.assertEqual(CRA.missing_packages(["definitely_not_installed_pkg_xyz"]), ["definitely_not_installed_pkg_xyz"])
        out = io.StringIO()
        with mock.patch.object(CRA, "missing_packages", return_value=["PySide6"]), contextlib.redirect_stdout(out):
            rc = CRA.main(["--packages"])
        self.assertEqual(rc, 1)
        self.assertIn("BUILD BLOCKED", out.getvalue())
        self.assertIn("PySide6", out.getvalue())

    def make_dist(self, d: Path):
        d.mkdir(parents=True)
        for exe in ("jarvis.exe", "jarvis-gui.exe"):
            (d / exe).write_bytes(b"MZ" + b"\0" * 200_000)           # TEST FIXTURE of the right size - NOT an executable
        for f in ("LICENSE", "THIRD_PARTY_NOTICES.md", "build_info.json", "config.sample.toml"):
            (d / f).write_text("x")
        (d / "docs").mkdir()
        for f in ("RELEASE_NOTES.md", "MODEL_DISTRIBUTION.md"):
            (d / "docs" / f).write_text("x")

    def test_dist_checker_accepts_a_complete_folder_and_rejects_bad_ones(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "ProjectJarvis"
            self.make_dist(d)
            self.assertEqual(CRA.check_dist(d), [])
            (d / "model.gguf").write_bytes(b"w")
            (d / "memory.sqlite").write_bytes(b"w")
            (d / "tests").mkdir()
            (d / "_internal").mkdir()
            (d / "_internal" / "numpy_tests").mkdir()
            (d / "_internal" / "tests").mkdir()                       # a third-party package's own tests folder is allowed
            probs = " | ".join(CRA.check_dist(d))
            for needle in ("model.gguf", "memory.sqlite", "top of the build: tests"):
                self.assertIn(needle, probs)
            self.assertNotIn("_internal", probs)
            (d / "jarvis.exe").write_bytes(b"MZ")
            self.assertIn("not a real PyInstaller executable", " ".join(CRA.check_dist(d)))
            (d / "jarvis-gui.exe").unlink()
            self.assertIn("jarvis-gui.exe", " ".join(CRA.check_dist(d)))
        self.assertIn("not found", CRA.check_dist(Path("/nonexistent/dist"))[0])

    def test_spec_uses_manifest_builds_both_exes_and_fails_clearly(self):
        spec = (ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
        for needle in ("import manifest", "BUILD BLOCKED", "entry_gui.py", "entry_cli.py", "MERGE(", 'name="jarvis-gui"', 'name="jarvis"',
                       "console=False", "manifest.EXCLUDES", "MANDATORY_PACKAGES_FOR_RELEASE"):
            self.assertIn(needle, spec)
        compile(spec, "jarvis.spec", "exec")                          # syntax only; PyInstaller names are resolved at build time
        self.assertNotIn("(os.path.join(root, \"docs\"), \"docs\")", spec, "the whole docs folder (maintainer notes, checklists) must not be bundled")

    def test_gui_entry_point_is_windowed_and_has_selftest(self):
        t = (ROOT / "packaging" / "entry_gui.py").read_text(encoding="utf-8")
        self.assertIn("--selftest", t)
        self.assertIn("freeze_support", t) if "multiprocessing" in t else None

    def test_ci_workflow_builds_with_the_script_and_uploads_checksums(self):
        w = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
        self.assertIn("build_windows.ps1", w)
        self.assertIn("SHA256SUMS.txt", w)
        self.assertIn("NOT YET RUN", w)

    def test_build_script_runs_every_gate_in_order(self):
        ps = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        order = ["check_release_assets.py --packages", "unittest discover", "release_tools.py build-info", "check_release_assets.py --source", "PyInstaller packaging",
                 "check_release_assets.py --dist", "Start-Process -FilePath .\\dist\\ProjectJarvis\\jarvis-gui.exe", "check_vc_runtime.py", "/DAppVersion=$ver", "release_tools.py sha256sums"]
        pos = [ps.find(x) for x in order]
        self.assertNotIn(-1, pos, dict(zip(order, pos)))
        self.assertEqual(pos, sorted(pos), "gates must run in this order")
        self.assertIn(".[dev,build,desktop,ui,docs,monitor]", ps)

    def test_build_info_contents_and_reproducibility_input(self):
        bi = RT.build_info(ROOT, env={"SOURCE_DATE_EPOCH": "1700000000"})
        self.assertEqual(bi["version"], version.__version__)
        self.assertEqual(bi["built_utc"], "2023-11-14T22:13:20Z")
        self.assertEqual(bi["channel"], version.RELEASE_CHANNEL)
        self.assertEqual(len(bi["spec_sha256"]), 64)
        self.assertIn("Not a signature", bi["note"])
        json.dumps(bi)
        self.assertEqual(RT.build_info(ROOT, env={"SOURCE_DATE_EPOCH": "1700000000"})["built_utc"], bi["built_utc"])


class InstallerScriptRules(unittest.TestCase):
    """Static checks of packaging/installer.iss (Inno Setup was NOT run)."""
    T = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")

    def test_per_user_no_admin_by_default(self):
        self.assertIn("PrivilegesRequired=lowest", self.T)

    def test_no_startup_persistence_or_system_changes(self):
        low = self.T.lower()
        for bad in ("[registry]", "startup", "runonce", "schtasks", "[service", "sc.exe", "netsh", "reg add", "setx", "firewall"):
            self.assertNotIn(bad, low.replace("; nothing here starts project jarvis at login", ""), bad)
        run = low.split("[run]")[1].split("[code]")[0]
        for line in run.splitlines():
            if line.strip().startswith("filename"):
                self.assertIn("postinstall", line, "every [Run] entry must be an optional post-install checkbox")
                self.assertIn("skipifsilent", line)

    def test_shortcuts_are_disclosed_and_desktop_one_is_unchecked(self):
        self.assertIn('Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked', self.T)
        self.assertIn("InfoBeforeFile=..\\docs\\INSTALLER_NOTICE.txt", self.T)
        notice = (ROOT / "docs" / "INSTALLER_NOTICE.txt").read_text(encoding="utf-8")
        for needle in ("Start menu", "desktop shortcut only if you tick", "Does NOT start", "LOCALAPPDATA", "Uninstalling asks"):
            self.assertIn(needle, notice)

    def test_user_data_is_outside_the_program_folder_and_only_it_is_deleted(self):
        self.assertIn("{localappdata}\\ProjectJarvis", self.T)
        self.assertEqual(re.findall(r"DelTree\(([^,]+),", self.T), ["DataDir()"])
        self.assertIn("MB_DEFBUTTON2", self.T)                       # the safe answer (keep) is the default
        self.assertIn("not UninstallSilent", self.T)                  # silent uninstall never deletes data
        self.assertNotIn("DelTree({app}", self.T)

    def test_upgrade_backs_up_before_replacing_and_no_data_in_files_section(self):
        files = self.T.split("[Files]")[1].split("[Tasks]")[0]
        self.assertIn("ssInstall", self.T)
        self.assertIn("'backup'", self.T)
        self.assertNotIn("localappdata", files.lower())
        self.assertIn("{app}", files)

    def test_app_identity_and_versioning(self):
        self.assertIn("AppName=Project JARVIS", self.T)
        self.assertIn("AppVersion={#AppVersion}", self.T)
        self.assertIn("MinVersion=10.0.17763", self.T)
        self.assertEqual(version.MIN_WINDOWS, "10.0.17763")

    def test_gui_is_the_main_shortcut(self):
        self.assertRegex(self.T, r'Name: "\{group\}\\Project JARVIS"; Filename: "\{app\}\\jarvis-gui\.exe"')


class CommandLineEntryPoints(AppCase):
    def run_cli(self, *a):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), *a])
        return rc, out.getvalue(), err.getvalue()

    def test_gui_without_pyside6_explains_instead_of_crashing(self):
        from jarvis.ui import app
        with mock.patch.object(app, "qt_available", return_value=False), mock.patch.object(app, "_report") as rep:
            rc, out, err = self.run_cli("gui")
        self.assertEqual(rc, 3)
        self.assertIn("PySide6", rep.call_args[0][1])
        self.assertIn("command-line features", rep.call_args[0][1])

    def test_gui_with_unusable_data_dir_gives_actionable_error(self):
        f = self.d / "afile"
        f.write_text("x")
        out, err = io.StringIO(), io.StringIO()
        from jarvis.ui import app
        with mock.patch.object(app, "_report") as rep, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(f), "gui"])
        self.assertEqual(rc, 2)
        self.assertIn("What to do", rep.call_args[0][1])

    def test_diagnostics_is_json_redacted_and_makes_no_network_call(self):
        import socket
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")):
            rc, out, err = self.run_cli("diagnostics")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertIn("model", json.dumps(data).lower())
        self.assertNotIn(str(Path.home()), out)


class UpgradeAndDataPreservation(AppCase):
    """Simulates what an upgrade does to the DATA folder (nothing). The installer's file replacement itself was not run."""

    def test_new_version_start_keeps_every_user_file(self):
        from jarvis.app import lifecycle
        lifecycle.prepare(self.cfg)
        files = {"memory.sqlite": b"mem", "settings.json": b"{}", "models/active.json": b"{}", "missions/m1.json": b"{}", "schedules/s1.json": b"{}",
                 "logs/app.log": b"log", "backups/b1.sqlite": b"bk"}
        for rel, content in files.items():
            p = self.cfg.data_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        with mock.patch.object(version, "__version__", "9.9.9"):
            for _ in range(2):
                lifecycle.prepare(self.cfg)
        for rel, content in files.items():
            self.assertEqual((self.cfg.data_dir / rel).read_bytes(), content, rel)

    def test_default_data_dir_is_per_user_and_outside_the_program_folder(self):
        from jarvis.cli import default_data_dir
        d = default_data_dir()
        self.assertEqual(d.name, "ProjectJarvis")
        self.assertTrue(str(d).startswith(str(Path.home())) or "LOCALAPPDATA" in __import__("inspect").getsource(default_data_dir))
        self.assertNotIn("Program Files", str(d))
        self.assertNotIn(str(ROOT), str(d))


class DocsAndHonesty(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_model_distribution_doc_is_precise(self):
        t = self.read("docs/MODEL_DISTRIBUTION.md")
        for needle in ("Qwen3-1.7B", "Apache-2.0", "ggml-org/Qwen3-1.7B-GGUF", "Pretrained interim model", "never retrain", "not been verified with the real model",
                       "no_model", "runtime_missing", "model_invalid", "never sends your prompt"):
            self.assertIn(needle.lower(), t.lower(), needle)

    def test_no_weights_or_secrets_in_the_source_tree(self):
        for pat in ("*.gguf", "*.safetensors", "*.pem", "*.key", "*.sqlite", "*.pt"):
            self.assertEqual([str(p) for p in ROOT.rglob(pat) if not ({".git", ".venv", "build", "dist"} & set(p.relative_to(ROOT).parts))], [], pat)
        self.assertEqual((ROOT / "src" / "jarvis" / "telemetry" / "endpoint.txt").read_text(encoding="utf-8").strip(), "")

    def test_distribution_doc_no_longer_claims_there_is_no_window(self):
        t = self.read("docs/WINDOWS_DISTRIBUTION.md")
        self.assertNotIn("There is still no desktop window", t)
        self.assertIn("never been run", t)

    def test_checklist_has_the_prompt10_section_and_matrix_rows(self):
        t = self.read("docs/WINDOWS_TEST_CHECKLIST.md")
        self.assertIn("6k", t)
        for needle in ("jarvis-gui.exe --selftest", "SHA256SUMS", "Uninstall", "Ctrl+1"):
            self.assertIn(needle, t)

    def test_changelog_and_review_document_the_status(self):
        self.assertIn("Prompt 10", self.read("CHANGELOG.md"))
        r = self.read("docs/PROMPT10_REVIEW.md")
        for needle in ("source-only", "Release verification matrix", "NOT DONE", "Actual Windows build", "Clean-machine"):
            self.assertIn(needle, r)
        self.assertNotRegex(r.lower(), r"(is|are) production[- ]ready")

    def test_privacy_doc_mentions_the_window_and_diagnostics(self):
        t = self.read("docs/PRIVACY_AND_NETWORK.md")
        self.assertIn("jarvis diagnostics", t)
        self.assertIn("window", t.lower())


if __name__ == "__main__":
    unittest.main()
