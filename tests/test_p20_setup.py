"""Setup wizard engine tests (Prompt 20). The model, network and hardware here are FAKES/simulations: they prove the logic and the honesty rules
(fallbacks, cancellation, refusing to mark a simulated run complete), NOT real inference, real downloads or GPU speed."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.app import setup_engine as SE
from jarvis.config import Config
from jarvis.eval import winvalidate
from jarvis.hardware import Hardware
from jarvis.inference import accel
from jarvis.inference import gguf_engine as ge
from jarvis.models import catalog
from jarvis.models.manager import IntegrityError, ModelStore
from jarvis.net.policy import NetworkPolicy
from p10_common import make_gguf

MID = "qwen3-1.7b-q4_k_m"


class FakeLlama:
    """FAKE MODEL. Speed depends on the GPU-layers request so the decision logic can be exercised."""
    gpu_fail = False
    gpu_tps_delay = 0.002
    cpu_tps_delay = 0.02

    def __init__(self, **kw):
        self.layers = kw.get("n_gpu_layers", 0)
        if self.layers and FakeLlama.gpu_fail:
            raise RuntimeError("CUDA error: out of memory")

    def tokenize(self, b, add_bos=False):
        return list(range(len(b.split())))

    def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
        text = "The sea is wide. It is blue and deep today."
        if not stream:
            return {"choices": [{"message": {"content": "ready"}}]}
        d = FakeLlama.gpu_tps_delay if self.layers else FakeLlama.cpu_tps_delay

        def gen():
            for w in text.split(" "):
                time.sleep(d)
                yield {"choices": [{"delta": {"content": w + " "}}]}
        return gen()


class FakeSampler:
    util = 80.0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def summary(self):
        return {"peak_process_rss_gb": 1.0, "gpu_util_max_percent": FakeSampler.util}


RTX = Hardware("Windows 11", 16, 32.0, 300.0, "NVIDIA GeForce RTX 4070 SUPER", 12.0)
CPU_ONLY = Hardware("Windows 11", 8, 16.0, 300.0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()
        self.policy = NetworkPolicy.load(self.cfg.settings_path)
        self.store = ModelStore(self.cfg.models_dir, self.policy)
        FakeLlama.gpu_fail = False
        FakeSampler.util = 80.0
        self.addCleanup(self.tmp.cleanup)

    def engine(self, hw=CPU_ONLY, rt=None, **kw):
        return SE.SetupEngine(self.cfg, self.store, self.policy, llama_factory=FakeLlama, hardware=hw,
                              runtime=rt or {"available": True, "version": "fake", "gpu_offload_supported": False}, sleep=lambda s: None, **kw)

    def install(self, mid=MID):
        self.store.install_gguf(make_gguf(self.d / "m.gguf"), mid, "Apache-2.0", "t")
        self.store.set_active(mid)


class SystemStep(Base):
    def test_blocks_when_runtime_missing(self):
        r = self.engine(rt={"available": False, "error": "ModuleNotFoundError: llama_cpp"}).check_system()
        self.assertFalse(r["can_continue"]); self.assertIn("llama-cpp-python", r["blockers"][0])

    def test_blocks_on_tiny_disk(self):
        r = self.engine(hw=Hardware("W", 4, 8.0, 1.0)).check_system()
        self.assertFalse(r["can_continue"])

    def test_gpu_present_but_no_backend_is_a_warning_not_acceleration(self):
        r = self.engine(hw=RTX).check_system()
        self.assertTrue(r["can_continue"])
        self.assertTrue(r["acceleration"]["gpu_detected"]); self.assertFalse(r["acceleration"]["backend_available"])
        self.assertFalse(r["acceleration"]["acceleration_verified"])
        self.assertTrue(any("GPU" in w for w in r["warnings"]))

    def test_not_windows_is_said_plainly(self):
        with mock.patch("platform.system", return_value="Linux"):
            self.assertIsNone(self.engine().check_system()["windows_ok"])

    def test_old_windows_blocks(self):
        with mock.patch("platform.system", return_value="Windows"), mock.patch("platform.version", return_value="6.1.7601"):
            self.assertFalse(self.engine().check_system()["can_continue"])


class ModelStep(Base):
    def test_recommends_confirmed_model_with_license_and_size(self):
        r = self.engine().recommend()
        self.assertEqual(r["recommended"], MID)
        row = next(x for x in r["models"] if x["id"] == MID)
        self.assertEqual(row["license"], "Apache-2.0"); self.assertGreater(row["download_gb"], 1)
        self.assertFalse(next(x for x in r["models"] if x["id"] == "qwen3-4b-q4_k_m")["metadata_confirmed"])      # still not read from the host's API
        self.assertTrue(next(x for x in r["models"] if x["id"] == "qwen3-8b-q4_k_m")["metadata_confirmed"])      # corrected to the publisher's own repository (page-read)

    def test_too_little_memory_gives_reason_not_crash(self):
        r = self.engine(hw=Hardware("W", 2, 2.0, 50.0)).recommend()
        self.assertIsNone(r["recommended"])


class DownloadStep(Base):
    def fake_download(self, times_fail=0, integrity=False, cancel_at=None):
        calls = {"n": 0}

        def dl(m, progress=None, trust_unpinned=False):
            calls["n"] += 1
            if calls["n"] <= times_fail:
                raise ConnectionError("connection reset")
            if integrity:
                raise IntegrityError("SHA-256 mismatch")
            for done in (10, 50, 100):
                if progress:
                    progress(done, 100)
            return self.store.install_gguf(make_gguf(self.d / "dl.gguf"), m.id, "Apache-2.0", "t")
        return dl, calls

    def test_success_installs_and_selects(self):
        dl, calls = self.fake_download()
        seen = []
        with mock.patch.object(self.store, "download_gguf", dl):
            r = self.engine().download(MID, lambda a, b: seen.append((a, b)))
        self.assertTrue(r["ok"]); self.assertEqual(self.store.active(), MID); self.assertEqual(seen[-1], (100, 100))

    def test_retries_network_errors_then_succeeds(self):
        dl, calls = self.fake_download(times_fail=2)
        with mock.patch.object(self.store, "download_gguf", dl):
            r = self.engine().download(MID)
        self.assertTrue(r["ok"]); self.assertEqual(calls["n"], 3)

    def test_gives_up_with_clear_message(self):
        dl, calls = self.fake_download(times_fail=99)
        with mock.patch.object(self.store, "download_gguf", dl):
            r = self.engine().download(MID, attempts=2)
        self.assertFalse(r["ok"]); self.assertEqual(r["kind"], "network"); self.assertEqual(calls["n"], 2)
        self.assertIsNone(self.store.active())

    def test_integrity_failure_is_not_retried_and_nothing_installed(self):
        dl, calls = self.fake_download(integrity=True)
        with mock.patch.object(self.store, "download_gguf", dl):
            r = self.engine().download(MID)
        self.assertEqual(r["kind"], "integrity"); self.assertEqual(calls["n"], 1); self.assertIsNone(self.store.active())

    def test_cancel_stops_download(self):
        ev = threading.Event()
        dl, _ = self.fake_download()
        with mock.patch.object(self.store, "download_gguf", dl):
            r = self.engine().download(MID, lambda a, b: ev.set(), ev)
        self.assertEqual(r["kind"], "cancelled"); self.assertIsNone(self.store.active())

    def test_not_enough_disk(self):
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10_000_000)):
            r = self.engine().download(MID)
        self.assertEqual(r["kind"], "disk_space")

    def test_unknown_model(self):
        self.assertEqual(self.engine().download("nope")["kind"], "unknown_model")

    def test_already_installed_skips_download(self):
        self.install()
        with mock.patch.object(self.store, "download_gguf", side_effect=AssertionError("must not download")):
            self.assertTrue(self.engine().download(MID)["already_installed"])


class VerifyStep(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(winvalidate, "Sampler", FakeSampler)
        p.start(); self.addCleanup(p.stop)
        self.install()

    def test_cpu_only_pc_verifies_cpu_mode_and_saves_it(self):
        v = self.engine().verify(MID)
        self.assertTrue(v["ok"]); self.assertEqual(v["mode"], "cpu"); self.assertFalse(v["gpu"]["tried"])
        self.assertEqual(accel.gpu_layers(self.store.dir), 0)
        self.assertFalse(v["real"])                       # a fake factory is never a real verification

    def test_gpu_used_only_when_measured_faster_with_activity(self):
        v = self.engine(hw=RTX, rt={"available": True, "gpu_offload_supported": True}).verify(MID)
        self.assertTrue(v["gpu"]["tried"]); self.assertTrue(v["gpu_verified"]); self.assertEqual(v["mode"], "gpu")
        self.assertEqual(accel.gpu_layers(self.store.dir), -1)
        agree = next(c for c in v["checks"] if c["name"] == "configured mode matches what was verified")
        self.assertTrue(agree["ok"]); self.assertIn("-1 GPU layers", agree["detail"])
        eng = ge.GGUFEngine(self.store.path(MID), llama_factory=FakeLlama)        # the app path follows the saved choice
        self.assertEqual(eng.gpu_layers_requested, -1)

    def test_fast_but_no_gpu_activity_keeps_cpu(self):
        FakeSampler.util = 0.0
        v = self.engine(hw=RTX, rt={"available": True, "gpu_offload_supported": True}).verify(MID)
        self.assertFalse(v["gpu_verified"]); self.assertEqual(v["mode"], "cpu"); self.assertEqual(accel.gpu_layers(self.store.dir), 0)

    def test_gpu_not_faster_keeps_cpu(self):
        FakeLlama.gpu_tps_delay = FakeLlama.cpu_tps_delay
        try:
            v = self.engine(hw=RTX, rt={"available": True, "gpu_offload_supported": True}).verify(MID)
        finally:
            FakeLlama.gpu_tps_delay = 0.002
        self.assertEqual(v["mode"], "cpu")

    def test_gpu_failure_falls_back_to_cpu_with_reason(self):
        FakeLlama.gpu_fail = True
        v = self.engine(hw=RTX, rt={"available": True, "gpu_offload_supported": True}).verify(MID)
        self.assertTrue(v["ok"]); self.assertEqual(v["mode"], "cpu"); self.assertIn("out of memory", v["gpu"]["error"])
        self.assertIn("CPU mode is used", v["gpu_note"])

    def test_env_override_that_disagrees_with_verified_mode_is_caught(self):
        with mock.patch.dict("os.environ", {"JARVIS_GPU_LAYERS": "-1"}):
            v = self.engine().verify(MID)           # CPU-only PC, but the environment forces GPU layers on the app's engine
        agree = next(c for c in v["checks"] if c["name"] == "configured mode matches what was verified")
        self.assertFalse(agree["ok"]); self.assertFalse(v["ok"])

    def test_gpu_detected_without_backend_is_not_tried(self):
        v = self.engine(hw=RTX).verify(MID)
        self.assertFalse(v["gpu"]["tried"]); self.assertTrue(v["gpu"]["detected"]); self.assertFalse(v["gpu"]["backend_available"])

    def test_model_load_failure_is_reported(self):
        def boom(**kw):
            raise MemoryError("out of memory")
        eng = SE.SetupEngine(self.cfg, self.store, self.policy, llama_factory=boom, hardware=CPU_ONLY, runtime={"available": True})
        v = eng.verify(MID)
        self.assertFalse(v["ok"]); self.assertIn("MemoryError", json.dumps(v["checks"])); self.assertIn("recovery", v)
        failed = [c for c in v["checks"] if not c["ok"]]
        self.assertEqual(failed[0]["name"], "model loads on CPU")

    def test_tampered_model_file_fails_integrity(self):
        (self.store.path(MID) / "model.gguf").write_bytes(b"GGUF" + b"x" * 5000)
        v = self.engine().verify(MID)
        self.assertFalse(v["ok"]); self.assertEqual(v["checks"][0]["name"], "model file integrity (full SHA-256)")

    def test_runtime_missing_fails_with_recovery(self):
        v = self.engine(rt={"available": False, "error": "no llama"}).verify(MID)
        self.assertFalse(v["ok"]); self.assertIn("Reinstall", v["recovery"])

    def test_cancel_before_start(self):
        ev = threading.Event(); ev.set()
        with self.assertRaises(SE.Cancelled):
            self.engine().verify(MID, ev)


class Wizard(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(winvalidate, "Sampler", FakeSampler)
        p.start(); self.addCleanup(p.stop)

    def dl(self, m, progress=None, trust_unpinned=False):
        return self.store.install_gguf(make_gguf(self.d / "dl.gguf"), m.id, "Apache-2.0", "t")

    def test_full_run_with_fakes_never_completes_setup(self):
        steps = []
        with mock.patch.object(self.store, "download_gguf", self.dl):
            rep = self.engine().run(confirm_download=lambda info: True, progress=lambda s, f, m: steps.append(s))
        self.assertNotIn("failed", rep); self.assertFalse(rep["real"]); self.assertFalse(rep["completed"])
        self.assertTrue(self.engine().needs_setup())
        self.assertTrue({"system", "model", "download", "verify"} <= set(steps))
        self.assertTrue((self.cfg.data_dir / SE.REPORT_FILE).is_file())

    def test_declined_download_downloads_nothing(self):
        with mock.patch.object(self.store, "download_gguf", side_effect=AssertionError("no download without consent")):
            rep = self.engine().run(confirm_download=lambda info: False)
        self.assertEqual(rep["failed"]["step"], "download")

    def test_consent_dialog_gets_size_and_license(self):
        got = {}
        with mock.patch.object(self.store, "download_gguf", self.dl):
            self.engine().run(confirm_download=lambda info: got.update(info) or True)
        self.assertEqual(got["license"], "Apache-2.0"); self.assertIn("license_url", got); self.assertGreater(got["download_gb"], 1)

    def test_model_that_does_not_fit_is_not_downloaded_or_replaced(self):
        eng = self.engine(hw=Hardware("W", 4, 4.0, 100.0))
        with mock.patch.object(self.store, "download_gguf", side_effect=AssertionError("must not download")):
            rep = eng.run("qwen3-8b-q4_k_m", confirm_download=lambda i: True)
        self.assertIn("will not fit", rep["failed"]["message"]); self.assertIsNone(self.store.active())

    def test_system_blocker_stops_before_any_download(self):
        eng = self.engine(rt={"available": False, "error": "x"})
        rep = eng.run(confirm_download=lambda i: True)
        self.assertEqual(rep["failed"]["step"], "system")

    def test_real_flag_requires_no_overrides(self):
        eng = SE.SetupEngine(self.cfg, self.store, self.policy)
        self.assertTrue(eng.real)
        self.assertFalse(self.engine().real)

    def test_state_file_damage_means_setup_needed_not_crash(self):
        (self.cfg.data_dir / SE.STATE_FILE).write_text("{not json")
        self.assertTrue(self.engine().needs_setup())

    def test_completed_state_for_removed_model_needs_setup_again(self):
        self.install()
        eng = self.engine()
        eng._save_state({"completed": True, "model_id": MID, "version_major": "0"})
        self.assertFalse(eng.needs_setup())
        self.store.remove(MID)
        self.assertTrue(eng.needs_setup())

    def test_damaged_model_file_means_setup_needed_again(self):
        self.install()
        eng = self.engine()
        eng._save_state({"completed": True, "model_id": MID, "version_major": "0"})
        self.assertFalse(eng.needs_setup())
        (self.store.path(MID) / "model.gguf").write_bytes(b"GGUF")            # truncated: size no longer matches the record
        self.assertTrue(eng.needs_setup())

    def test_cancel_midway_reports_cancelled(self):
        ev = threading.Event()

        def dl(m, progress=None, trust_unpinned=False):
            ev.set()
            progress(1, 2)
        with mock.patch.object(self.store, "download_gguf", dl):
            rep = self.engine().run(confirm_download=lambda i: True, cancel=ev)
        self.assertEqual(rep["failed"]["kind"], "cancelled")

    def test_report_contains_no_home_path(self):
        with mock.patch.object(self.store, "download_gguf", self.dl):
            self.engine().run(confirm_download=lambda i: True)
        text = (self.cfg.data_dir / SE.REPORT_FILE).read_text()
        self.assertNotIn(str(Path.home()) + "/", text)


class AccelFile(unittest.TestCase):
    def test_damaged_file_means_cpu(self):
        with tempfile.TemporaryDirectory() as t:
            accel.path_for(t).write_text("garbage")
            self.assertEqual(accel.gpu_layers(t), 0)
            accel.path_for(t).write_text(json.dumps({"gpu_layers": True}))
            self.assertEqual(accel.gpu_layers(t), 0)

    def test_precedence_argument_env_file(self):
        with tempfile.TemporaryDirectory() as t:
            md = Path(t) / "models"; d = md / "m"; d.mkdir(parents=True)
            (d / "model.gguf").write_bytes(b"GGUF" + b"\0" * 64)
            accel.save(md, -1, verified=True)
            with mock.patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop("JARVIS_GPU_LAYERS", None)
                self.assertEqual(ge.GGUFEngine(d, llama_factory=FakeLlama).gpu_layers_requested, -1)
                self.assertEqual(ge.GGUFEngine(d, llama_factory=FakeLlama, n_gpu_layers=0).gpu_layers_requested, 0)
                os.environ["JARVIS_GPU_LAYERS"] = "0"
                self.assertEqual(ge.GGUFEngine(d, llama_factory=FakeLlama).gpu_layers_requested, 0)


if __name__ == "__main__":
    unittest.main()
