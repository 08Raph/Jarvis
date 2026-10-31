"""The small English speech model that ships inside the installer: pin check, first-start set-up, the release-asset exception for exactly one pinned folder, the
fetch script's safety checks and the build/CI wiring. FAKES: a tiny synthetic model folder stands in for the real one (its real digest is pinned in
jarvis.voice.bundled_vosk after a genuine download on CI). Nothing here uses the network."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for sub in ("tests", "scripts", "packaging"):
    sys.path.insert(0, str(ROOT / sub))

import check_release_assets as CRA  # noqa: E402
import fetch_vosk_model as FV  # noqa: E402
import manifest as MF  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.voice import bundled_vosk as V  # noqa: E402
from jarvis.voice.settings import VoiceSettings  # noqa: E402


def make_model(folder: Path) -> Path:
    for rel, data in (("am/final.mdl", b"AM" * 50), ("conf/model.conf", b"--x=1\n"), ("graph/HCLG.fst", b"G" * 300), ("README", b"fake readme")):
        f = folder / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    return folder


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-p79-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "app" / V.BUNDLE_DIRNAME
        self.src = make_model(self.root / V.MODEL_NAME)
        self.cfg = Config(data_dir=self.tmp / "data")
        self.cfg.data_dir.mkdir()
        digest, count = V.tree_digest(self.src)
        for patch in (mock.patch.object(V, "PIN_TREE_SHA256", digest), mock.patch.object(V, "PIN_FILE_COUNT", count)):
            patch.start()
            self.addCleanup(patch.stop)


class SeedTests(Base):
    def test_copies_and_sets_the_setting_on_a_fresh_profile(self):
        self.assertTrue(V.pending(self.cfg, self.root))
        r = V.seed(self.cfg, self.root)
        self.assertEqual(r["state"], "installed", r)
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        self.assertEqual(vs.vosk_model, str(V.install_dir(self.cfg)))
        self.assertTrue((V.install_dir(self.cfg) / "am" / "final.mdl").is_file())
        self.assertFalse(V.pending(self.cfg, self.root))

    def test_changes_nothing_else_in_the_voice_settings(self):
        VoiceSettings(muted=True, wake_phrase="hey computer", listening_choice_made=False).save(self.cfg.profile_dir / "voice.json")
        V.seed(self.cfg, self.root)
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        self.assertTrue(vs.muted)
        self.assertEqual(vs.wake_phrase, "hey computer")
        self.assertFalse(vs.listening_choice_made)

    def test_never_replaces_a_speech_model_the_user_set(self):
        mine = make_model(self.tmp / "mine")
        VoiceSettings(vosk_model=str(mine)).save(self.cfg.profile_dir / "voice.json")
        r = V.seed(self.cfg, self.root)
        self.assertEqual(r["state"], "installed")
        self.assertFalse(r["setting_applied"])
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").vosk_model, str(mine))

    def test_done_once_and_not_put_back(self):
        V.seed(self.cfg, self.root)
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        vs.vosk_model = None
        vs.save(self.cfg.profile_dir / "voice.json")
        self.assertEqual(V.seed(self.cfg, self.root)["state"], "already-done")
        self.assertIsNone(VoiceSettings.load(self.cfg.profile_dir / "voice.json").vosk_model)

    def test_a_changed_file_is_refused_and_nothing_is_written(self):
        (self.src / "am" / "final.mdl").write_bytes(b"tampered")
        r = V.seed(self.cfg, self.root)
        self.assertEqual(r["state"], "refused")
        self.assertFalse((self.cfg.profile_dir / "voice.json").exists())
        self.assertFalse(V.install_dir(self.cfg).exists())

    def test_an_extra_file_is_refused(self):
        (self.src / "extra.txt").write_text("x")
        self.assertEqual(V.seed(self.cfg, self.root)["state"], "refused")

    def test_no_pin_means_refused(self):
        with mock.patch.object(V, "PIN_TREE_SHA256", ""):
            self.assertEqual(V.seed(self.cfg, self.root)["state"], "refused")

    def test_unreadable_voice_json_is_left_alone(self):
        p = self.cfg.profile_dir / "voice.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(V.seed(self.cfg, self.root)["state"], "refused")
        self.assertEqual(p.read_text(encoding="utf-8"), "{not json")

    def test_absent_without_a_bundle(self):
        self.assertEqual(V.seed(self.cfg, self.tmp / "nowhere")["state"], "absent")

    def test_uses_no_network(self):
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network used")), \
             mock.patch("socket.socket.connect", side_effect=AssertionError("network used")):
            self.assertEqual(V.seed(self.cfg, self.root)["state"], "installed")

    def test_first_run_reports_it(self):
        from jarvis.firstrun import first_run
        with mock.patch.dict(os.environ, {V.ENV_OVERRIDE: str(self.root)}):
            rep = first_run(self.cfg)
        self.assertEqual(rep["bundled_voice_model"]["state"], "installed")

    def test_the_seeded_folder_is_a_usable_vosk_model(self):
        from jarvis.voice.inputcheck import looks_like_vosk_model
        V.seed(self.cfg, self.root)
        self.assertTrue(looks_like_vosk_model(str(V.install_dir(self.cfg))))


class DistCheckTests(Base):
    def make_dist(self):
        d = self.tmp / "dist"
        d.mkdir()
        for f in ("jarvis.exe", "jarvis-gui.exe"):
            (d / f).write_bytes(b"MZ" + b"\0" * 200_000)
        (d / "docs").mkdir()
        for f in ("LICENSE", "THIRD_PARTY_NOTICES.md", "build_info.json", "config.sample.toml", "docs/RELEASE_NOTES.md", "docs/MODEL_DISTRIBUTION.md"):
            (d / f).write_text("x", encoding="utf-8")
        return d

    def with_model(self, d):
        shutil.copytree(self.src, d / MF.BUNDLED_VOSK_RELDIR)
        return d

    def test_the_pinned_folder_passes(self):
        d = self.with_model(self.make_dist())
        self.assertEqual(CRA.check_dist(d, require_bundled_voice_model=True), [])

    def test_missing_is_fine_unless_required(self):
        d = self.make_dist()
        self.assertEqual(CRA.check_dist(d), [])
        self.assertTrue(any("required but missing" in p for p in CRA.check_dist(d, require_bundled_voice_model=True)))

    def test_a_modified_or_extra_file_blocks_the_build(self):
        d = self.with_model(self.make_dist())
        (d / MF.BUNDLED_VOSK_RELDIR / "am" / "final.mdl").write_bytes(b"x")
        self.assertTrue(any("does not match the pin" in p for p in CRA.check_dist(d)))
        (d / MF.BUNDLED_VOSK_RELDIR / "am" / "final.mdl").write_bytes(b"AM" * 50)      # restored; now add an extra file
        self.assertEqual(CRA.check_dist(d), [])
        (d / MF.BUNDLED_VOSK_RELDIR / "extra.bin").write_bytes(b"x")
        self.assertTrue(CRA.check_dist(d))

    def test_an_unpinned_folder_blocks_the_build(self):
        d = self.with_model(self.make_dist())
        with mock.patch.object(V, "PIN_TREE_SHA256", ""):
            self.assertTrue(any("no pinned checksum" in p for p in CRA.check_dist(d)))

    def test_other_files_in_the_bundle_folder_are_still_refused(self):
        d = self.with_model(self.make_dist())
        (d / "bundled_models" / "other.txt").write_text("x")
        self.assertTrue(any("unexpected file" in p for p in CRA.check_dist(d)))

    def test_the_build_manifest_records_the_folder(self):
        import release_tools as RT
        d = self.with_model(self.make_dist())
        m = RT.build_manifest(ROOT, d, None, env={}, versions={})
        self.assertEqual(m["bundled_voice_model"]["tree_sha256"], V.PIN_TREE_SHA256)
        self.assertEqual(m["model_weights_in_build"], [])


class FetchScriptTests(Base):
    def make_zip(self, names=None):
        z = self.tmp / "m.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for f in sorted(self.src.rglob("*")):
                if f.is_file():
                    zf.write(f, f"{V.MODEL_NAME}/{f.relative_to(self.src).as_posix()}")
            for n in names or []:
                zf.writestr(n, "x")
        return z

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = FV.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def pins(self, z):
        return (mock.patch.object(V, "ZIP_PIN_BYTES", z.stat().st_size), mock.patch.object(V, "ZIP_PIN_SHA256", FV.sha256_file(z)))

    def test_verified_zip_is_written_where_asked(self):
        z = self.make_zip()
        for p in self.pins(z):
            p.start()
            self.addCleanup(p.stop)
        code, _, err = self.run_main("--zip", str(z), "--out", str(self.tmp / "o"))
        self.assertEqual(code, 0, err)
        self.assertEqual(V.tree_digest(self.tmp / "o" / V.MODEL_NAME), (V.PIN_TREE_SHA256, V.PIN_FILE_COUNT))

    def test_refuses_while_unpinned(self):
        z = self.make_zip()
        with mock.patch.object(V, "PIN_TREE_SHA256", ""):
            code, _, err = self.run_main("--zip", str(z), "--out", str(self.tmp / "o"))
        self.assertEqual(code, 2)
        self.assertFalse((self.tmp / "o").exists())

    def test_wrong_zip_hash_is_refused(self):
        z = self.make_zip()
        with mock.patch.object(V, "ZIP_PIN_BYTES", 1), mock.patch.object(V, "ZIP_PIN_SHA256", "0" * 64):
            code, _, err = self.run_main("--zip", str(z), "--out", str(self.tmp / "o"))
        self.assertEqual(code, 3)

    def test_path_traversal_in_the_zip_is_refused(self):
        z = self.make_zip(["../evil.txt"])
        with self.assertRaises(SystemExit):
            self.run_main("--zip", str(z), "--out", str(self.tmp / "o"))
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_probe_prints_the_values_and_enforces_nothing(self):
        z = self.make_zip()
        with mock.patch.object(FV, "page_licence_lines", return_value=["(test)"]), mock.patch.object(V, "PIN_TREE_SHA256", ""), \
             mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "", "GITHUB_STEP_SUMMARY": ""}):
            code, out, _ = self.run_main("--zip", str(z), "--probe")
        self.assertEqual(code, 0)
        rep, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])
        self.assertEqual(rep["file_count"], 4)
        self.assertEqual(rep["zip_sha256"], FV.sha256_file(z))


class RealPinTests(unittest.TestCase):
    def test_the_real_pins_are_filled_in_and_match_the_ledger(self):
        src = (ROOT / "src" / "jarvis" / "voice" / "bundled_vosk.py").read_text(encoding="utf-8")      # source text: other classes patch the module values
        ledger = (ROOT / "docs" / "VERIFICATION_LEDGER.md").read_text(encoding="utf-8")
        for needle in ("41205931", "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498", "PIN_FILE_COUNT = 14",
                       "4750b6f6db7fc6e28ac4318db4832305c51fe1a49cb5c7e45bab33bc79dab852"):
            self.assertIn(needle, src)
        self.assertIn("4750b6f6db7fc6e28ac4318db4832305c51fe1a49cb5c7e45bab33bc79dab852", ledger)
        self.assertIn("30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498", ledger)

    def test_the_licence_notice_is_shipped(self):
        self.assertIn("vosk-model-small-en-us-0.15", (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8"))
        import release_tools as RT
        self.assertIn("vosk-model-small-en-us-0.15", RT.generate_notices(Path(tempfile.mkdtemp()) / "n.md"))      # the build regenerates the file from this generator


class WiringTests(unittest.TestCase):
    def test_build_script_and_installer_wiring(self):
        ps = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$BundleVoiceModel", ps)
        self.assertIn("fetch_vosk_model.py --out dist", ps)
        self.assertIn("--require-bundled-voice-model", ps)
        self.assertIn("-BundleVoiceModel", (ROOT / "build_installer.bat").read_text(encoding="utf-8"))
        iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("bundled_models", iss)

    def test_ci_download_is_gated_by_typed_consent_and_manual_runs_only(self):
        wf = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(encoding="utf-8")
        self.assertIn("yes-bundle-vosk-small-en", wf)
        self.assertIn("yes-probe-vosk-small-en", wf)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.vosk_probe_consent", wf)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.bundle_voice_model_consent", wf)

    def test_the_app_module_has_no_network_code(self):
        src = (ROOT / "src" / "jarvis" / "voice" / "bundled_vosk.py").read_text(encoding="utf-8")
        for bad in ("urlopen", "import urllib", "import requests", "import socket", "http.client"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
