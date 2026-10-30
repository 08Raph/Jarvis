"""Tests for release tooling. The installer/PyInstaller/PowerShell files get STATIC checks only:
they cannot be compiled or run in this (Linux) sandbox."""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis.hardware import TIERS
from jarvis.memory.store import MemoryStore
from jarvis.models.manager import ModelStore
from jarvis.security import signing

spec = importlib.util.spec_from_file_location("release_tools", ROOT / "scripts" / "release_tools.py")
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)


class ReleaseToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sha256sums(self):
        (self.d / "a.bin").write_bytes(b"abc")
        (self.d / "sub").mkdir()
        (self.d / "sub" / "b.bin").write_bytes(b"def")
        out = self.d / "SHA256SUMS.txt"
        self.assertEqual(rt.write_sha256sums(self.d, out), 2)
        lines = out.read_text().splitlines()
        self.assertIn(f"{hashlib.sha256(b'abc').hexdigest()}  a.bin", lines)
        self.assertIn(f"{hashlib.sha256(b'def').hexdigest()}  sub/b.bin", lines)

    def test_keys_and_signed_manifest_roundtrip(self):
        rt.main(["gen-keys", "--out-dir", str(self.d / "keys")])
        sk = bytes.fromhex((self.d / "keys" / "jarvis_signing_private.hex").read_text())
        pk = bytes.fromhex((self.d / "keys" / "jarvis_signing_public.hex").read_text())
        payload = {"latest": "1.0.0", "installer_url": "https://example.com/x.exe", "installer_sha256": "a" * 64}
        out = self.d / "m.json"
        rt.main(["sign-manifest", "--payload-json", json.dumps(payload), "--private-key-file",
                 str(self.d / "keys" / "jarvis_signing_private.hex"), "--out", str(out)])
        doc = json.loads(out.read_text())
        self.assertTrue(signing.verify(doc["payload"], doc["signature"], pk))
        self.assertEqual(len(sk), 32)

    def test_sign_manifest_from_payload_file_with_bom(self):
        rt.main(["gen-keys", "--out-dir", str(self.d / "k")])
        pk = bytes.fromhex((self.d / "k" / "jarvis_signing_public.hex").read_text())
        payload = {"latest": "1.0.0", "installer_url": "https://example.com/x.exe", "installer_sha256": "b" * 64}
        pf = self.d / "payload.json"
        pf.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode())  # what Windows PowerShell often writes
        out = self.d / "m2.json"
        rt.main(["sign-manifest", "--payload-file", str(pf), "--private-key-file",
                 str(self.d / "k" / "jarvis_signing_private.hex"), "--out", str(out)])
        doc = json.loads(out.read_text())
        self.assertEqual(doc["payload"], payload)
        self.assertTrue(signing.verify(doc["payload"], doc["signature"], pk))

    def test_pack_model_cli_then_install(self):
        src = self.d / "m"
        src.mkdir()
        (src / "model.pt").write_bytes(b"w")
        (src / "tokenizer.json").write_text("{}")
        (src / "LICENSE.txt").write_text("l")
        rt.main(["pack-model", "--model-dir", str(src), "--out", str(self.d / "p.jarvismodel"), "--license", "MIT"])
        store = ModelStore(self.d / "models")
        sha = rt.sha256_file(self.d / "p.jarvismodel")
        self.assertEqual(store.install_package(self.d / "p.jarvismodel", "m1", expected_sha256=sha)["license"], "MIT")

    def test_notices_and_requirements_doc(self):
        text = rt.generate_notices(self.d / "N.md")
        self.assertIn("numpy", text)
        self.assertIn("cryptography", text)
        self.assertIn("No training datasets are bundled", text)
        self.assertIn("-BundleModel", text)
        self.assertIn("LGPL", text)
        doc = rt.requirements_doc()
        for t in TIERS:
            self.assertIn(t.name, doc)
        self.assertIn("NOT measured", doc)


class PackagingStaticChecks(unittest.TestCase):
    def test_installer_script_properties(self):
        iss = (ROOT / "packaging" / "installer.iss").read_text()
        self.assertRegex(iss, r"AppId=\{\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}")
        self.assertIn("PrivilegesRequired=lowest", iss)
        files = iss.split("[Files]")[1].split("[Icons]")[0]
        self.assertNotIn("model.pt", files)
        self.assertNotIn(".jarvismodel", files)
        self.assertIn("LocalAppData".lower(), iss.lower())
        self.assertRegex(iss, r"not UninstallSilent")  # silent uninstall must never delete user data
        self.assertIn("Parameters: \"first-run\"", iss)

    def test_spec_and_scripts_present_and_sane(self):
        spec_src = (ROOT / "packaging" / "jarvis.spec").read_text()
        compile(spec_src, "jarvis.spec", "exec")  # syntax only
        self.assertIn("COLLECT(", spec_src)  # one-folder build
        for ps in ("build_windows.ps1", "clean_install_test.ps1"):
            t = (ROOT / "scripts" / ps).read_text()
            self.assertIn('$ErrorActionPreference = "Stop"', t)
        build = (ROOT / "scripts" / "build_windows.ps1").read_text()
        self.assertLess(build.index("-m unittest discover"), build.index("-m PyInstaller"))  # tests gate packaging
        self.assertNotRegex(build, r"(?i)api[_-]?key")
        self.assertNotIn("--payload-json", build)  # PS 5.1 mangles quotes in native args; the file form is used
        clean = (ROOT / "scripts" / "clean_install_test.ps1").read_text()
        self.assertIn('$ErrorActionPreference = "Continue"', clean)  # stderr-from-native workaround present
        wsb = (ROOT / "packaging" / "clean_install_test.wsb").read_text()
        self.assertIn("<Networking>Disable</Networking>", wsb)


class BackupRestoreCliTests(unittest.TestCase):
    def test_backup_then_restore_via_cli(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            data.mkdir()
            m = MemoryStore(data / "memory.sqlite")
            m.add_note("precious")
            m.close()
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))

            def cli(*a):
                p = subprocess.run([sys.executable, "-W", "ignore", "-m", "jarvis", "--data-dir", str(data), *a],
                                   capture_output=True, text=True, env=env, timeout=60)
                return p.returncode, p.stdout.strip(), p.stderr.strip()

            rc, backup_path, _ = cli("backup")
            self.assertEqual(rc, 0)
            m = MemoryStore(data / "memory.sqlite")
            m.delete_note(1)
            m.close()
            rc, out, _ = cli("restore", backup_path)
            self.assertEqual(rc, 0)
            self.assertIn("restored", out)
            m = MemoryStore(data / "memory.sqlite")
            self.assertEqual(m.notes()[0]["text"], "precious")
            m.close()
            (Path(d) / "junk.sqlite").write_bytes(b"junk" * 100)
            self.assertEqual(cli("restore", str(Path(d) / "junk.sqlite"))[0], 3)


if __name__ == "__main__":
    unittest.main()
