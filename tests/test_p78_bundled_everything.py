"""The 'everything inside the installer' work: the bundled model seed, the release-asset exception for exactly one pinned file, the local-only
sign-in server command, and the build/installer wiring. FAKES: a tiny synthetic GGUF stands in for the real model (the real file's size and SHA-256 are
pinned in jarvis.models.bundled; their real values are checked only by the build, on Windows CI). Nothing here uses the network."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "server"))

from p10_common import make_gguf  # noqa: E402

import check_release_assets as CRA  # noqa: E402
import manifest as MF  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.models import bundled as B  # noqa: E402
from jarvis.models.manager import ModelStore  # noqa: E402


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-p78-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "app" / B.BUNDLE_DIRNAME
        (self.root / B.MODEL_ID).mkdir(parents=True)
        self.src = make_gguf(self.root / B.MODEL_ID / B.FILENAME, pad=2000)
        self.cfg = Config(data_dir=self.tmp / "data")
        self.cfg.data_dir.mkdir()
        self.cfg.models_dir.mkdir(parents=True, exist_ok=True)
        # the real pins describe the real 1.28 GB file; for the tests the pins describe the tiny synthetic file
        for patch in (mock.patch.object(B, "PIN_BYTES", self.src.stat().st_size), mock.patch.object(B, "PIN_SHA256", sha(self.src))):
            patch.start()
            self.addCleanup(patch.stop)


class SeedTests(Base):
    def test_installs_verifies_and_activates_on_a_fresh_profile(self):
        self.assertTrue(B.pending(self.cfg, self.root))
        r = B.seed(self.cfg, self.root)
        self.assertEqual(r["state"], "installed", r)
        self.assertTrue(r["activated"])
        store = ModelStore(self.cfg.models_dir)
        self.assertEqual(store.active(), B.MODEL_ID)
        ok, problems = store.verify(B.MODEL_ID)
        self.assertTrue(ok, problems)
        self.assertEqual(sha(store.gguf_file(B.MODEL_ID)), B.PIN_SHA256)

    def test_the_record_does_not_claim_the_publisher_confirmed_the_hash(self):
        B.seed(self.cfg, self.root)
        meta = ModelStore(self.cfg.models_dir).meta(B.MODEL_ID)
        self.assertIs(meta["verified_origin"], False)
        self.assertEqual(meta["origin"], "bundled-with-installer")
        self.assertIs(meta["checksum_reference"]["publisher_confirmed"], False)
        self.assertEqual(meta["license"], "Apache-2.0")

    def test_second_call_does_nothing_and_a_removed_model_is_not_put_back(self):
        B.seed(self.cfg, self.root)
        self.assertEqual(B.seed(self.cfg, self.root)["state"], "already-done")
        ModelStore(self.cfg.models_dir).remove(B.MODEL_ID)
        self.assertFalse(B.pending(self.cfg, self.root))
        self.assertEqual(B.seed(self.cfg, self.root)["state"], "already-done")
        self.assertEqual(ModelStore(self.cfg.models_dir).installed(), [])

    def test_a_profile_that_already_has_a_model_is_left_alone(self):
        other = make_gguf(self.tmp / "other.gguf", pad=10)
        ModelStore(self.cfg.models_dir).install_gguf(other, "my-model", "MIT", "MIT licence text")
        r = B.seed(self.cfg, self.root)
        self.assertEqual(r["state"], "skipped")
        self.assertEqual([m["id"] for m in ModelStore(self.cfg.models_dir).installed()], ["my-model"])

    def test_a_swapped_file_with_the_wrong_hash_is_refused_and_nothing_is_installed(self):
        make_gguf(self.src, pad=2001)                       # same kind of file, different bytes -> different size AND hash
        r = B.seed(self.cfg, self.root)
        self.assertEqual(r["state"], "refused", r)
        self.assertEqual(ModelStore(self.cfg.models_dir).installed(), [])
        self.assertFalse((self.cfg.data_dir / B.MARKER).exists(), "a refusal must not count as having seeded")

    def test_same_size_wrong_hash_is_refused(self):
        data = bytearray(self.src.read_bytes())
        data[-1] ^= 0xFF                                    # keeps the size, changes the hash
        self.src.write_bytes(bytes(data))
        self.assertEqual(B.seed(self.cfg, self.root)["state"], "refused")
        self.assertEqual(ModelStore(self.cfg.models_dir).installed(), [])

    def test_a_json_file_next_to_the_model_cannot_vouch_for_it(self):
        make_gguf(self.src, pad=2001)
        (self.root / B.MODEL_ID / "bundled.json").write_text(json.dumps({"sha256": sha(self.src), "bytes": self.src.stat().st_size}))
        self.assertEqual(B.seed(self.cfg, self.root)["state"], "refused")

    def test_no_bundle_means_nothing_happens(self):
        self.assertEqual(B.seed(self.cfg, self.tmp / "nowhere")["state"], "absent")
        self.assertFalse(B.pending(self.cfg, self.tmp / "nowhere"))

    def test_does_not_replace_an_active_choice(self):
        other = make_gguf(self.tmp / "o.gguf", pad=10)
        s = ModelStore(self.cfg.models_dir)
        s.install_gguf(other, "mine", "MIT", "MIT text")
        s.remove("mine")                                    # no installed model left, but ...
        s.install_gguf(other, "mine2", "MIT", "MIT text")
        s.set_active("mine2")
        B.seed(self.cfg, self.root)                         # ... a model exists, so the seed is skipped
        self.assertEqual(s.active(), "mine2")

    def test_seed_uses_no_network(self):
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network used")), \
             mock.patch("socket.socket.connect", side_effect=AssertionError("network used")):
            self.assertEqual(B.seed(self.cfg, self.root)["state"], "installed")

    def test_first_run_installs_it_and_reports_it(self):
        from jarvis.firstrun import first_run
        with mock.patch.dict(os.environ, {B.ENV_OVERRIDE: str(self.root)}):
            rep = first_run(self.cfg)
        self.assertEqual(rep["bundled_model"]["state"], "installed")
        self.assertEqual(rep["active_model"], B.MODEL_ID)
        self.assertEqual(rep["next_steps"], [])

    def test_first_run_without_a_bundle_is_unchanged(self):
        from jarvis.firstrun import first_run
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(B.ENV_OVERRIDE, None)
            rep = first_run(self.cfg)
        self.assertEqual(rep["bundled_model"]["state"], "absent")
        self.assertTrue(rep["next_steps"])

    def test_the_real_pins_are_the_ones_recorded_in_the_ledger(self):
        text = (ROOT / "src" / "jarvis" / "models" / "bundled.py").read_text(encoding="utf-8")     # source text: setUp patches the module's values
        wf = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(encoding="utf-8")
        for pin in ("1282439264", "d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5"):
            self.assertIn(pin, text)
            self.assertIn(pin, wf)


class DistCheckTests(Base):
    def make_dist(self, with_model=True):
        d = self.tmp / "dist"
        for f in ("jarvis.exe", "jarvis-gui.exe"):
            (d).mkdir(exist_ok=True)
            (d / f).write_bytes(b"MZ" + b"\0" * 200_000)
        (d / "docs").mkdir(exist_ok=True)
        for f in ("LICENSE", "THIRD_PARTY_NOTICES.md", "build_info.json", "config.sample.toml", "docs/RELEASE_NOTES.md", "docs/MODEL_DISTRIBUTION.md"):
            (d / f).write_text("x", encoding="utf-8")
        if with_model:
            dst = d / MF.BUNDLED_MODEL_RELPATH
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.src, dst)
        return d

    def test_the_pinned_model_at_the_allowed_path_passes(self):
        d = self.make_dist()
        self.assertEqual(CRA.check_dist(d, require_bundled_model=True), [])

    def test_without_the_model_it_passes_unless_required(self):
        d = self.make_dist(with_model=False)
        self.assertEqual(CRA.check_dist(d), [])
        self.assertTrue(any("required but missing" in p for p in CRA.check_dist(d, require_bundled_model=True)))

    def test_a_wrong_hash_blocks_the_build(self):
        d = self.make_dist()
        f = d / MF.BUNDLED_MODEL_RELPATH
        data = bytearray(f.read_bytes())
        data[-1] ^= 1
        f.write_bytes(bytes(data))
        self.assertTrue(any("SHA-256" in p for p in CRA.check_dist(d)))

    def test_a_wrong_size_blocks_the_build(self):
        d = self.make_dist()
        (d / MF.BUNDLED_MODEL_RELPATH).write_bytes(b"GGUF")
        self.assertTrue(any("pinned size" in p for p in CRA.check_dist(d)))

    def test_any_other_weights_file_is_still_forbidden(self):
        d = self.make_dist()
        (d / "other.gguf").write_bytes(b"x")
        (d / "_internal").mkdir()
        (d / "_internal" / "w.safetensors").write_bytes(b"x")
        (d / B.BUNDLE_DIRNAME / "extra.txt").write_text("x")
        probs = "\n".join(CRA.check_dist(d))
        self.assertIn("other.gguf", probs)
        self.assertIn("w.safetensors", probs)
        self.assertIn("extra.txt", probs)

    def test_the_same_file_name_somewhere_else_is_forbidden(self):
        d = self.make_dist(with_model=False)
        (d / "model.gguf").write_bytes(b"x")
        self.assertTrue(any("model.gguf" in p for p in CRA.check_dist(d)))


class WiringTests(unittest.TestCase):
    def test_source_checks_still_pass(self):
        self.assertEqual(CRA.check_source(), [])

    def test_the_installer_stores_the_model_uncompressed_and_does_not_start_anything(self):
        iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn('Excludes: "bundled_models\\*"', iss)
        self.assertRegex(iss, r'bundled_models\\\*"; DestDir: "\{app\}\\bundled_models"; Flags: [^\n]*nocompression[^\n]*skipifsourcedoesntexist')
        self.assertIn("local-server", iss)
        for bad in ("[Registry]", "RunOnce", "schtasks", "[Service"):
            self.assertNotIn(bad, iss)

    def test_the_build_script_bundles_only_when_asked_and_verifies_the_pin(self):
        ps = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$BundleModel", ps)
        self.assertIn("if ($BundleModel)", ps)
        self.assertIn("models fetch qwen3-1.7b-q4_k_m --yes", ps)
        self.assertIn("--require-bundled-model", ps)
        self.assertEqual(len(re.findall(r"models fetch", ps)), 1, "exactly one download in the build")

    def test_the_one_click_bat_builds_everything(self):
        bat = (ROOT / "build_installer.bat").read_text(encoding="utf-8")
        for flag in ("-WithVoice", "-BundleModel"):
            self.assertIn(flag, bat)

    def test_ci_never_downloads_the_model_unless_a_person_types_the_consent(self):
        wf = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(encoding="utf-8")
        self.assertIn("bundle_model_consent", wf)
        self.assertIn("yes-bundle-qwen3-1.7b-q4_k_m", wf)
        build = wf.split("name: Build with scripts/build_windows.ps1")[1].split("- id: unit")[0]
        self.assertIn("-BundleModel", build)
        self.assertRegex(build, r"inputs\.bundle_model_consent == 'yes-bundle-qwen3-1\.7b-q4_k_m'")

    def test_the_spec_freezes_the_server_and_sees_server_folder(self):
        spec = (ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
        self.assertIn("manifest.SERVER_PACKAGE", spec)
        self.assertIn('os.path.join(root, "server")', spec)
        for src, dest in MF.DATAS:
            if dest == "jarvis_server":
                self.assertTrue((ROOT / src).is_file(), src)

    def test_the_installer_notice_no_longer_says_no_model_is_included(self):
        n = (ROOT / "docs" / "INSTALLER_NOTICE.txt").read_text(encoding="utf-8")
        self.assertNotIn("No AI model is included", n)
        self.assertIn("Qwen3-1.7B", n)


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-p78s-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = Config(data_dir=self.tmp)

    def run_with(self, port, fake_main):
        from jarvis.online import localserver
        fake = mock.Mock(main=fake_main)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(localserver, "_import_server", return_value=fake), redirect_stdout(out), redirect_stderr(err):
            rc = localserver.run_local_server(self.cfg, port=port)
        return rc, out.getvalue(), err.getvalue()

    def test_it_binds_loopback_only_and_has_no_host_option(self):
        seen = {}

        def fake_main(argv):
            seen["argv"] = argv
            return 0
        rc, out, _ = self.run_with(8765, fake_main)
        self.assertEqual(rc, 0)
        a = seen["argv"]
        self.assertEqual(a[a.index("--host") + 1], "127.0.0.1")
        self.assertIn("--no-telemetry", a)
        self.assertEqual(Path(a[a.index("--db") + 1]), self.tmp / "online" / "local_server.db")
        self.assertNotIn("--tls-cert", a)
        self.assertIn("http://127.0.0.1:8765", out)

    def test_the_command_has_no_host_option(self):
        src = (ROOT / "src" / "jarvis" / "cli.py").read_text(encoding="utf-8")
        block = src.split('ls = sub.add_parser("local-server"')[1].split("\n    sub.add_parser")[0].split("\n    sa =")[0]
        self.assertIn("--port", block)
        self.assertNotIn("--host", block)

    def test_bad_port_and_port_in_use_are_explained(self):
        rc, _, err = self.run_with(80, lambda argv: 0)
        self.assertEqual(rc, 2)

        def busy(argv):
            raise OSError(10048, "Only one usage of each socket address is normally permitted")
        rc, _, err = self.run_with(8765, busy)
        self.assertEqual(rc, 4)
        self.assertIn("8766", err)

    def test_it_really_serves_the_account_api_on_loopback(self):
        """A real server on an ephemeral port, driven by the real client code path used by the window (stdlib only)."""
        import threading
        from http.server import ThreadingHTTPServer
        from jarvis_server.app import Service
        from jarvis_server.backends import load_backend
        from jarvis_server.db import DB
        from jarvis_server.__main__ import make_handler
        svc = Service(DB(str(self.tmp / "t.db")), load_backend(None), None, telemetry_enabled=False)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc, False))
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(), svc.db.close() if hasattr(svc.db, "close") else None))
        self.assertEqual(httpd.server_address[0], "127.0.0.1")
        import urllib.request
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(url + "/v1/health", timeout=10) as r:
            self.assertEqual(r.status, 200)
        with urllib.request.urlopen(url + "/v1/terms", timeout=10) as r:
            self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
