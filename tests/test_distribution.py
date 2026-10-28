import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis.config import Config
from jarvis.models.package import pack_model
from jarvis.setup_wizard import run_wizard


def scripted(answers):
    it = iter(answers)
    out = []
    return (lambda prompt: next(it)), out.append, out


class WizardTests(unittest.TestCase):
    def test_no_model_skip_is_honest_and_changes_nothing_online(self):
        with tempfile.TemporaryDirectory() as d:
            ask, say, out = scripted([""])
            self.assertEqual(run_wizard(Config(data_dir=Path(d)), ask, say), 0)
            text = "\n".join(out)
            self.assertIn("none installed", text)
            self.assertIn("cannot answer without one", text)
            self.assertIn("every online feature is OFF", text)
            self.assertIn("Voice is OFF", text)

    def test_noninteractive_eof_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            def ask(_):
                raise EOFError
            self.assertEqual(run_wizard(Config(data_dir=Path(d)), ask, lambda s: None), 0)

    def test_install_model_with_hash_and_unverified_confirmation(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "m"
            src.mkdir()
            (src / "model.pt").write_bytes(b"w")
            (src / "tokenizer.json").write_text("{}")
            (src / "LICENSE.txt").write_text("l")
            pkg = Path(d) / "My Model.jarvismodel"
            pack_model(src, pkg, "MIT")
            sha = hashlib.sha256(pkg.read_bytes()).hexdigest()
            cfg = Config(data_dir=Path(d) / "data")
            ask, say, out = scripted([f'"{pkg}"', sha])
            run_wizard(cfg, ask, say)
            self.assertIn("Installed and selected", "\n".join(out))
            # unverified: declining must install nothing
            cfg2 = Config(data_dir=Path(d) / "data2")
            ask, say, out = scripted([str(pkg), "", "no"])
            run_wizard(cfg2, ask, say)
            self.assertIn("Skipped", "\n".join(out))
            self.assertNotIn("Installed", "\n".join(out))
            # wrong hash is rejected
            cfg3 = Config(data_dir=Path(d) / "data3")
            ask, say, out = scripted([str(pkg), "0" * 64])
            run_wizard(cfg3, ask, say)
            self.assertIn("Could not install", "\n".join(out))

    def test_cli_setup_with_closed_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            p = subprocess.run([sys.executable, "-W", "ignore", "-m", "jarvis", "--data-dir", d, "setup"],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertNotIn("Traceback", p.stderr)


class DistributionStaticTests(unittest.TestCase):
    def test_installer_targets_windows10_and_exposes_setup_guide(self):
        iss = (ROOT / "packaging" / "installer.iss").read_text()
        self.assertIn("MinVersion=10.0.17763", iss)
        self.assertIn('{app}\\jarvis.exe"" setup', iss)
        self.assertIn("desktopicon", iss)
        self.assertNotIn("model.pt", iss)

    def test_ci_workflow_and_bat_present(self):
        wf = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text()
        self.assertIn("windows-2022", wf)
        self.assertIn("clean_install_test.ps1", wf)
        self.assertNotRegex(wf, r"(?i)secrets\.|api[_-]?key")   # no paid services or credentials
        self.assertIn("build_windows.ps1", (ROOT / "build_installer.bat").read_text())

    def test_build_script_is_robust_on_other_machines(self):
        b = (ROOT / "scripts" / "build_windows.ps1").read_text()
        self.assertIn("-CheckOnly", b)
        self.assertIn("Inno Setup 6_is1", b)                       # finds ISCC without it being on PATH
        self.assertIn("ISCC.exe", b)
        self.assertNotIn('Run "iscc ', b)                          # no bare iscc call that fails when not on PATH
        self.assertIn("Out-File -Encoding ascii", b)               # no UTF-16 from PS 5.1 redirection
        self.assertIn("--data-dir", b)                             # smoke test never touches the real data folder
        self.assertLess(b.index("-m unittest discover"), b.index("-m PyInstaller"))
        self.assertLess(b.index("-m PyInstaller"), b.index("ProjectJarvis-Setup-$ver.exe"))
        self.assertEqual(b.count("{"), b.count("}"))               # crude balance check; no PowerShell here to parse it
        self.assertEqual(b.count("("), b.count(")"))

    def test_build_installs_and_verifies_local_ai_runtime(self):
        b = (ROOT / "scripts" / "build_windows.ps1").read_text()
        self.assertIn("llama-cpp-python==$RuntimeVersion --only-binary=:all:", b)         # prebuilt wheel, exact version: no compiler for normal users, no drift
        self.assertIn("abetlen.github.io/llama-cpp-python/whl/cpu", b)
        self.assertIn("runtime --require", b)                             # frozen exe is checked for the runtime
        self.assertLess(b.index("llama-cpp-python"), b.index("-m unittest discover"))
        self.assertIn('"llama_cpp"', ((ROOT / "packaging" / "jarvis.spec").read_text() + (ROOT / "packaging" / "manifest.py").read_text()))   # Prompt 10: asset lists moved from the spec to packaging/manifest.py
        self.assertIn("llama-cpp-python", (ROOT / "pyproject.toml").read_text())

    def test_build_can_bake_in_optional_telemetry_url_and_always_resets_it(self):
        b = (ROOT / "scripts" / "build_windows.ps1").read_text()
        self.assertIn("-TelemetryUrl", b)
        self.assertIn("^https://", b)
        self.assertIn("finally", b)
        self.assertLess(b.index("WriteAllText($epFile, $TelemetryUrl"), b.index("-m PyInstaller"))
        self.assertGreater(b.rindex('WriteAllText($epFile, ""'), b.index("-m PyInstaller"))
        self.assertEqual((ROOT / "src" / "jarvis" / "telemetry" / "endpoint.txt").read_text().strip(), "")
        self.assertIn("endpoint.txt", ((ROOT / "packaging" / "jarvis.spec").read_text() + (ROOT / "packaging" / "manifest.py").read_text()))   # Prompt 10: asset lists moved from the spec to packaging/manifest.py

    def test_entry_point_survives_legacy_console_encoding(self):
        e = (ROOT / "packaging" / "entry_cli.py").read_text()
        self.assertIn("reconfigure(errors=\"replace\")", e)
        compile(e, "entry_cli.py", "exec")

    def test_gpu_probe_uses_absolute_paths_on_windows(self):
        import inspect
        from jarvis import hardware
        src = inspect.getsource(hardware._nvidia_exe)
        nt_branch = src.split('if os.name == "nt":')[1].split("return shutil.which")[0]
        self.assertNotIn("shutil.which", nt_branch)          # Windows branch never searches cwd/PATH
        self.assertIn("System32", nt_branch)

    def test_spec_bundles_optional_voice_libs_conditionally(self):
        spec = (ROOT / "packaging" / "jarvis.spec").read_text()
        compile(spec, "jarvis.spec", "exec")
        self.assertIn("sounddevice", spec + (ROOT / "packaging" / "manifest.py").read_text())   # Prompt 10: list moved to manifest.py
        self.assertIn("except Exception", spec)


if __name__ == "__main__":
    unittest.main()
