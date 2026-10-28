"""Stage 6.5: Kokoro local neural voice, integrated through the Stage 6 engine abstraction.

LABELS (read before trusting any result): the Kokoro MODEL, the audio OUTPUT DEVICE, the network and PySide6 are all FAKES here.
  * FakeModel returns synthetic constant audio; FakeSink records samples; FakePolicy serves in-memory bytes. No real Kokoro model was downloaded or loaded,
    no real inference ran, no sound was produced, no Windows machine was involved and PySide6 was not imported (the window is checked with stand-in Qt modules
    and by reading its source).
  * Therefore these tests prove Jarvis's logic (registration, persistence, fallback rules, lifecycle, cancel, error handling, thread cleanup, wording).
    They do NOT prove that Kokoro produces real speech, that the four voices sound good, or that anything works on Windows. See docs/STAGE6_5_KOKORO.md."""
import hashlib
import importlib
import io
import json
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p10_common import AppCase  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.app.speech import SpeechOut  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.models import kokoro_files as KF  # noqa: E402
from jarvis.net.policy import NetworkPolicy  # noqa: E402
from jarvis.voice import engines as E, preview as P, viewmodel as VM  # noqa: E402
from jarvis.voice import kokoro_engine as KE  # noqa: E402
from jarvis.voice.manager import VoiceManager  # noqa: E402
from jarvis.voice.settings import VoiceSettings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PRESETS = ("af_heart", "af_aoede", "af_nicole", "am_onyx")
TINY = (KF.ModelFile("kokoro-v1.0.onnx", 4000, "model"), KF.ModelFile("voices-v1.0.bin", 3000, "voices"))      # same names, tiny sizes (test data only)
RAW = [{"name": "Alpha Voice", "culture": "en-US", "gender": "Female", "age": "Adult", "enabled": True},
       {"name": "Bravo Voice", "culture": "en-GB", "gender": "Male", "age": "Adult", "enabled": True}]


def wait_until(pred, timeout=6.0, step=0.005):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def kokoro_threads():
    return [t for t in threading.enumerate() if t.name.startswith("jarvis-kokoro") and t.is_alive()]


def voices_bytes(ids=PRESETS, pad=0) -> bytes:
    buf = io.BytesIO()
    np.savez(buf, **{i: np.zeros((4, 1, 8), dtype=np.float32) for i in ids})
    return buf.getvalue() + b"\0" * pad


def model_bytes(n=4000) -> bytes:
    return b"\x08\x07" + b"\x01" * (n - 2)          # not text, not html: a stand-in blob


class FakeModel:
    """FAKE Kokoro: returns constant synthetic audio."""
    def __init__(self, voices=PRESETS, sr=24000, delay=0.0, seconds=0.2, fail=None, bad_audio=None):
        self.voices, self.sr, self.delay, self.seconds, self.fail, self.bad_audio = list(voices), sr, delay, seconds, fail, bad_audio
        self.calls = []

    def get_voices(self):
        return list(self.voices)

    def create(self, text, voice, speed=1.0, lang="en-us"):
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        if self.bad_audio is not None:
            return self.bad_audio, self.sr
        return np.full(int(self.sr * self.seconds), 0.5, dtype=np.float32), self.sr


class FakeSink:
    """FAKE audio output: records what it was given; can block like a real device buffer; can fail to open or write."""
    instances: list = []

    def __init__(self, write_delay=0.0, fail_open=None, fail_write=None):
        self.write_delay, self.fail_open, self.fail_write = write_delay, fail_open, fail_write
        self.opened_sr = None
        self.blocks = []
        self.aborted = self.closed = False
        self.close_args = []
        self._abort_ev = threading.Event()
        FakeSink.instances.append(self)

    def open(self, sr):
        if self.fail_open:
            raise self.fail_open
        self.opened_sr = sr

    def write(self, a):
        if self.fail_write:
            raise self.fail_write
        self.blocks.append(np.array(a))
        if self.write_delay:
            self._abort_ev.wait(self.write_delay)

    def abort(self):
        self.aborted = True
        self._abort_ev.set()

    def close(self, abort=False):
        self.closed = True
        self.close_args.append(abort)

    def total_samples(self):
        return sum(len(b) for b in self.blocks)


class FakeResp:
    def __init__(self, data: bytes, declared=True, chunk_delay=0.0, truncate_to=None):
        self._b = io.BytesIO(data if truncate_to is None else data[:truncate_to])
        self.headers = {"Content-Length": str(len(data))} if declared else {}
        self.chunk_delay = chunk_delay

    def read(self, n=-1):
        if self.chunk_delay:
            time.sleep(self.chunk_delay)
        return self._b.read(n if n and n > 0 else -1)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePolicy(NetworkPolicy):
    """FAKE network: a real NetworkPolicy (so grant_once and feature checks are the real ones) whose open() serves in-memory files and records the request."""
    def __init__(self, files=None, **kw):
        super().__init__()
        self.served = files if files is not None else {"kokoro-v1.0.onnx": model_bytes(), "voices-v1.0.bin": voices_bytes()}
        self.kw = kw
        self.requests = []

    def open(self, feature, url, timeout=30.0, data=None, headers=None, allowed_host_suffixes=None, require_public=False):
        if not self.is_enabled(feature):
            from jarvis.net.policy import NetworkDisabled
            raise NetworkDisabled(f"network feature {feature!r} is disabled")
        self._validate_url(url, allowed_host_suffixes)
        self.requests.append({"feature": feature, "url": url, "suffixes": allowed_host_suffixes})
        name = url.rsplit("/", 1)[-1]
        if name not in self.served:
            raise urllib.error.URLError("no route to host")
        return FakeResp(self.served[name], **self.kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.cfg = Config(data_dir=self.data)
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        FakeSink.instances.clear()
        self.sink_kw = {}
        self.model = FakeModel()
        self.sapi_sp = _SapiFake()

    def install_files(self, ids=PRESETS, files=TINY):
        d = KF.model_dir(self.data)
        d.mkdir(parents=True, exist_ok=True)
        (d / files[0].name).write_bytes(model_bytes(3500))
        vb = voices_bytes(ids)
        (d / files[1].name).write_bytes(vb + b"\0" * max(0, 2500 - len(vb)))       # trailing zeros: a zip reader ignores them; keeps the size inside the stand-in range
        return d

    def kokoro(self, install=True, packages=True, model=None, **kw):
        if install:
            self.install_files(kw.pop("ids", PRESETS))
        if model is not None:
            self.model = model
        kw.setdefault("providers_probe", lambda: ["CPUExecutionProvider"])           # never import the real onnxruntime in a unit test
        eng = KE.KokoroEngine(self.data, files=TINY, model_factory=lambda m, v: self.model, sink_factory=lambda: FakeSink(**self.sink_kw),
                              find_spec=(lambda n: object()) if packages else (lambda n: None), **kw)
        self.addCleanup(eng.close)
        return eng

    def mgr(self, kokoro=None, sapi=True, persona=None):
        engines = ([E.SapiEngine(speaker=self.sapi_sp, platform="win32")] if sapi else []) + [kokoro or self.kokoro()]
        m = VoiceManager(self.cfg, E.EngineRegistry(engines), persona_provider=lambda: persona)
        self.addCleanup(m.close)
        return m

    def save(self, **kw):
        vs = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        for k, v in kw.items():
            setattr(vs, k, v)
        vs.save(self.cfg.profile_dir / "voice.json")


class _SapiFake:
    def __init__(self):
        self.said, self.stops = [], 0

    def list_voices(self):
        return list(RAW)

    def speak(self, text, rate, volume, voice):
        self.said.append((text, rate, volume, voice))

    def stop(self):
        self.stops += 1

    def is_speaking(self):
        return False


# =====================================================================================================================================================
class RegistrationAndVoices(Base):
    def test_default_registry_has_sapi_first_and_kokoro_only_when_a_data_folder_is_given(self):
        self.assertEqual([e.id for e in E.default_registry("win32").engines], ["sapi"])
        reg = E.default_registry("win32", self.data)
        self.assertEqual([e.id for e in reg.engines], ["sapi", "kokoro"])
        self.assertIsInstance(reg.get("kokoro"), KE.KokoroEngine)

    def test_kokoro_declares_local_offline_no_fee_and_never_silently_falls_back(self):
        e = self.kokoro(install=False)
        d = e.describe()
        self.assertTrue(d["local"])
        self.assertFalse(d["needs_internet"])
        self.assertFalse(d["fallback_ok"])
        self.assertIn("MIT", d["licence"])
        self.assertIn("Apache-2.0", d["licence"])
        self.assertIn("GPL", d["licence"])
        self.assertTrue(E.SapiEngine().fallback_ok)

    def test_the_four_requested_voice_ids_are_listed_as_real_selectable_voices_when_the_installed_file_has_them(self):
        e = self.kokoro()
        vs = e.list_voices()
        self.assertEqual([v.name for v in vs], list(PRESETS))
        self.assertEqual([v.id for v in vs], ["kokoro:" + n for n in PRESETS])
        m = self.mgr(kokoro=e)
        cat = m.refresh()
        self.assertEqual([v["name"] for v in cat["categories"]["unspecified"] if v["engine"] == "kokoro"], list(PRESETS))

    def test_no_metadata_is_invented_for_kokoro_voices(self):
        for v in self.kokoro().list_voices():
            self.assertEqual((v.language, v.language_label, v.gender_reported, v.age, v.category), ("", "", "", "", "unspecified"))
        cat = self.mgr().refresh()
        text = VM.voice_details(cat, "kokoro:af_heart")
        self.assertIn("Voice ID: af_heart", text)
        self.assertNotRegex(text.lower(), r"\b(female|male|american|british|accent: |warm|natural|high quality)\b")

    def test_a_preset_missing_from_the_installed_file_is_not_offered_and_is_reported(self):
        e = self.kokoro(ids=("af_heart", "af_aoede", "am_onyx", "zz_other"))
        self.assertEqual([v.name for v in e.list_voices()], ["af_heart", "af_aoede", "am_onyx"])
        st = e.status()
        self.assertEqual(st["presets_absent"], ["af_nicole"])
        self.assertIn("af_nicole", VM.kokoro_text(self.mgr(kokoro=e).refresh()))

    def test_voice_ids_are_configurable_so_more_voices_can_be_added_without_new_code(self):
        e = self.kokoro(ids=PRESETS + ("bf_extra",), voice_ids=PRESETS + ("bf_extra",))
        self.assertEqual([v.name for v in e.list_voices()], list(PRESETS) + ["bf_extra"])

    def test_a_voice_chosen_by_name_outside_the_presets_still_works_if_the_file_has_it(self):
        e = self.kokoro(ids=PRESETS + ("bf_extra",), model=FakeModel(voices=PRESETS + ("bf_extra",)))
        sp = e.speaker()
        sp.speak("hello there", 1.0, 0.5, "bf_extra")
        self.assertTrue(wait_until(lambda: not sp.is_speaking()))
        self.assertEqual(sp.exit_code(), 0)
        self.assertEqual(self.model.calls[0]["voice"], "bf_extra")

    def test_settings_accept_and_round_trip_a_kokoro_voice_id_and_old_files_still_load(self):
        self.save(tts_voice="kokoro:af_heart")
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").tts_voice, "kokoro:af_heart")
        (self.cfg.profile_dir / "voice.json").write_text(json.dumps({"tts_voice": "Old Plain Name"}), encoding="utf-8")
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").tts_voice, "Old Plain Name")


class ModelFilesAndDependencies(Base):
    def test_missing_model_is_unavailable_with_an_actionable_message_and_nothing_is_listed(self):
        e = self.kokoro(install=False)
        ok, why = e.available()
        self.assertFalse(ok)
        self.assertIn("Install / repair", why)
        self.assertEqual(e.store.status()["state"], "missing")
        with self.assertRaises(E.EngineError):
            e.list_voices()
        m = self.mgr(kokoro=e)
        cat = m.refresh()
        self.assertIn("kokoro", m._errors)
        self.assertEqual(cat["count"], 2)                    # only the two fake SAPI voices

    def test_missing_runtime_packages_are_named_with_the_install_command(self):
        e = self.kokoro(packages=False)
        ok, why = e.available()
        self.assertFalse(ok)
        self.assertIn("kokoro-onnx", why)
        self.assertIn("pip install kokoro-onnx sounddevice", why)
        self.assertEqual(e.status()["packages_missing"], ["kokoro-onnx", "sounddevice"])

    def test_one_file_missing_is_incomplete_and_wrong_size_or_html_is_damaged(self):
        d = self.install_files()
        e = self.kokoro(install=False)
        self.assertEqual(e.store.status()["state"], "ready")
        (d / TINY[1].name).unlink()
        self.assertEqual(e.store.status()["state"], "incomplete")
        (d / TINY[1].name).write_bytes(voices_bytes())
        (d / TINY[0].name).write_bytes(b"<!DOCTYPE html><html>" + b"x" * 3500)
        st = e.store.status()
        self.assertEqual(st["state"], "damaged")
        self.assertIn("web page", st["problems"][TINY[0].name])
        (d / TINY[0].name).write_bytes(b"\x08" * 10)
        self.assertIn("unexpected size", e.store.status()["problems"][TINY[0].name])
        self.assertFalse(e.available()[0])
        self.assertIn("damaged", e.available()[1])

    def test_a_file_whose_size_changed_since_install_is_flagged(self):
        st = KF.KokoroModelStore(self.data, TINY)
        st.download(FakePolicy(), consent=True)
        with open(st.dir / TINY[0].name, "ab") as f:
            f.write(b"\x01" * 20)                                   # still inside the plausible range, but not what was recorded
        self.assertEqual(st.status()["state"], "damaged")
        self.assertIn("changed size", st.status()["problems"][TINY[0].name])

    def test_a_corrupt_or_empty_voices_file_is_damaged_not_silently_accepted(self):
        d = self.install_files()
        e = self.kokoro(install=False)
        (d / TINY[1].name).write_bytes(b"not a zip archive " * 200)
        self.assertEqual(e.store.status()["state"], "damaged")
        buf = io.BytesIO()
        np.savez(buf)
        (d / TINY[1].name).write_bytes(buf.getvalue() + b"\0" * 2900)
        self.assertEqual(e.store.status()["state"], "damaged")
        self.assertIn("no voices", e.store.status()["problems"][TINY[1].name])

    def test_leftover_partial_download_means_incomplete_and_is_cleaned(self):
        d = KF.model_dir(self.data)
        d.mkdir(parents=True)
        (d / ".part-abc").write_bytes(b"1234")
        st = self.kokoro(install=False).store
        self.assertEqual(st.status()["state"], "incomplete")
        self.assertEqual(st.clean_partials(), 1)
        self.assertEqual(st.status()["state"], "missing")

    def test_a_model_that_fails_to_load_gives_a_plain_repair_message_not_a_traceback(self):
        def boom(m, v):
            raise ValueError("INVALID_PROTOBUF: secret internal detail C:\\Users\\someone\\x.onnx")
        e = self.kokoro()
        e.runtime._factory = boom
        sp = e.speaker()
        sp.speak("hi", 1.0, 0.8, "af_heart")
        self.assertTrue(wait_until(lambda: not sp.is_speaking()))
        self.assertEqual(sp.exit_code(), 1)
        self.assertIn("damaged or not compatible", sp.last_error)
        self.assertIn("Install / repair", sp.last_error)
        self.assertNotIn("Traceback", sp.last_error)
        self.assertEqual(FakeSink.instances, [])
        self.assertFalse(e.runtime.loaded())                    # a failed load is not cached: repairing the files and trying again works
        e.runtime._factory = lambda m, v: FakeModel()
        sp.speak("hi", 1.0, 0.8, "af_heart")
        self.assertTrue(wait_until(lambda: not sp.is_speaking()))
        self.assertEqual(sp.exit_code(), 0)

    def test_missing_runtime_import_and_out_of_memory_have_their_own_messages(self):
        e = self.kokoro()
        for exc, word in ((ImportError("No module named onnxruntime"), "install it with"), (MemoryError(), "not enough free memory")):
            def f(m, v, exc=exc):
                raise exc
            e.runtime._factory = f
            sp = e.speaker()
            sp.speak("hi", 1.0, 0.8, "af_heart")
            self.assertTrue(wait_until(lambda: not sp.is_speaking()))
            self.assertIn(word, sp.last_error.lower())

    def test_the_real_model_is_built_on_the_cpu_provider_only(self):
        sess_calls, made = [], []

        class FakeKokoro:
            @classmethod
            def from_session(cls, sess, voices):
                made.append(("from_session", sess, voices))
                return "model"
        fake_ort = mock.MagicMock()
        fake_ort.InferenceSession.side_effect = lambda path, providers=None: (sess_calls.append((path, providers)), "SESSION")[1]
        fake_ko = mock.MagicMock(Kokoro=FakeKokoro)
        with mock.patch.dict(sys.modules, {"onnxruntime": fake_ort, "kokoro_onnx": fake_ko}):
            self.assertEqual(KE.default_model_factory("m.onnx", "v.bin"), "model")
        self.assertEqual(sess_calls, [("m.onnx", ["CPUExecutionProvider"])])
        self.assertEqual(made, [("from_session", "SESSION", "v.bin")])

        class OldKokoro:
            def __init__(self, m, v):
                self.args = (m, v)
        fake_ko = mock.MagicMock(Kokoro=OldKokoro)
        with mock.patch.dict(sys.modules, {"onnxruntime": fake_ort, "kokoro_onnx": fake_ko}), mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ONNX_PROVIDER", None)
            self.assertEqual(KE.default_model_factory("m.onnx", "v.bin").args, ("m.onnx", "v.bin"))
            self.assertEqual(os.environ["ONNX_PROVIDER"], "CPUExecutionProvider")
            os.environ.pop("ONNX_PROVIDER", None)

    def test_status_is_cheap_and_never_loads_the_model_or_imports_the_runtime(self):
        called = []
        e = self.kokoro()
        e.runtime._factory = lambda m, v: called.append(1)
        e.status()
        e.describe()
        e.available()
        self.assertEqual(called, [])
        self.assertFalse(e.runtime.loaded())
        self.assertNotIn("kokoro_onnx", sys.modules)

    def test_acceleration_is_reported_honestly_and_gpu_is_not_claimed(self):
        e = self.kokoro(providers_probe=KE.default_providers_probe)
        st = e.status()
        self.assertIsNone(st["providers"])                           # not probed yet
        self.assertIn("never turns on a GPU or cloud provider", st["acceleration"])
        self.assertIn("not checked yet", VM.kokoro_text(self.mgr(kokoro=e).catalog()))
        fake_ort = mock.MagicMock()
        fake_ort.get_available_providers.return_value = ["CPUExecutionProvider"]
        with mock.patch.dict(sys.modules, {"onnxruntime": fake_ort}):
            e.probe()
        self.assertEqual(e.status()["providers"], ["CPUExecutionProvider"])


class Downloads(Base):
    def store(self):
        return KF.KokoroModelStore(self.data, TINY)

    def test_download_needs_consent_and_nothing_is_requested_without_it(self):
        pol = FakePolicy()
        with self.assertRaises(KF.KokoroModelError) as cm:
            self.store().download(pol, consent=False)
        self.assertIn("confirmation", str(cm.exception))
        self.assertEqual(pol.requests, [])
        self.assertFalse(KF.model_dir(self.data).exists())

    def test_install_downloads_both_files_with_progress_checks_them_and_records_a_local_hash(self):
        pol, seen = FakePolicy(), []
        st = self.store()
        res = st.download(pol, consent=True, progress=lambda a, b: seen.append((a, b)))
        self.assertEqual(res["installed"], [f.name for f in TINY])
        self.assertEqual(st.status()["state"], "ready")
        self.assertEqual(st.partials(), [])
        self.assertEqual([a for a, _ in seen], sorted(a for a, _ in seen))
        self.assertEqual(seen[-1][0], len(model_bytes()) + len(voices_bytes()))
        man = json.loads((st.dir / KF.MANIFEST).read_text())
        self.assertEqual(man["files"][TINY[0].name]["sha256"], hashlib.sha256(model_bytes()).hexdigest())
        self.assertIn("no publisher checksum", man["note"])
        self.assertEqual(st.verify_hashes(), {TINY[0].name: "match", TINY[1].name: "match"})

    def test_only_the_official_hosts_over_https_for_the_model_download_feature_and_the_policy_is_restored(self):
        pol = FakePolicy()
        self.store().download(pol, consent=True)
        for r in pol.requests:
            self.assertEqual(r["feature"], "model_download")
            self.assertEqual(r["suffixes"], KF.HOSTS)
            self.assertTrue(r["url"].startswith("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"))
        self.assertFalse(pol.is_enabled("model_download"))           # granted for this one action only
        with self.assertRaises(Exception):                            # and the real policy refuses other hosts
            pol._validate_url("https://evil.example/x", KF.HOSTS)

    def test_a_valid_installed_model_is_not_downloaded_again_and_only_the_damaged_file_is_repaired(self):
        st = self.store()
        st.download(FakePolicy(), consent=True)
        pol = FakePolicy()
        self.assertEqual(st.download(pol, consent=True), {"installed": [], "kept": [f.name for f in TINY]})
        self.assertEqual(pol.requests, [])
        (st.dir / TINY[1].name).write_bytes(b"junk" * 800)
        pol = FakePolicy()
        res = st.download(pol, consent=True)
        self.assertEqual(res["installed"], [TINY[1].name])
        self.assertEqual(res["kept"], [TINY[0].name])
        self.assertEqual(len(pol.requests), 1)

    def test_cancel_removes_partial_files_and_installs_nothing(self):
        pol = FakePolicy()
        st = self.store()
        n = {"k": 0}

        def cancel():
            n["k"] += 1
            return n["k"] > 1
        with self.assertRaises(KF.Cancelled):
            st.download(pol, consent=True, cancel=cancel)
        self.assertEqual(st.partials(), [])
        self.assertEqual(st.status()["state"], "missing")
        self.assertFalse((st.dir / KF.MANIFEST).exists())

    def test_truncated_oversized_and_wrongly_announced_downloads_fail_cleanly(self):
        st = self.store()
        with self.assertRaises(KF.KokoroModelError) as cm:
            st.download(FakePolicy(truncate_to=1000), consent=True)
        self.assertIn("incomplete", str(cm.exception))
        self.assertEqual((st.partials(), st.status()["state"]), ([], "missing"))
        big = {"kokoro-v1.0.onnx": model_bytes(), "voices-v1.0.bin": b"x" * 9000}
        with self.assertRaises(KF.KokoroModelError) as cm:
            st.download(FakePolicy(files=big), consent=True)
        self.assertIn("unexpected size", str(cm.exception))
        self.assertEqual(st.partials(), [])
        big = {"kokoro-v1.0.onnx": model_bytes(), "voices-v1.0.bin": b"x" * 9000}
        with self.assertRaises(KF.KokoroModelError) as cm:
            st.download(FakePolicy(files=big, declared=False), consent=True)       # no Content-Length: the streaming limit still stops it
        self.assertIn("larger than expected", str(cm.exception))
        self.assertEqual(st.partials(), [])

    def test_a_download_of_the_wrong_kind_of_file_is_rejected_after_download(self):
        html = {"kokoro-v1.0.onnx": b"<html>" + b"x" * 3500, "voices-v1.0.bin": voices_bytes()}
        st = self.store()
        with self.assertRaises(KF.KokoroModelError) as cm:
            st.download(FakePolicy(files=html), consent=True)
        self.assertIn("did not pass the checks", str(cm.exception))

    def test_network_failure_is_a_plain_message_and_leaves_no_files(self):
        pol = FakePolicy(files={})
        st = self.store()
        with self.assertRaises(KF.KokoroModelError) as cm:
            st.download(pol, consent=True)
        self.assertIn("download failed", str(cm.exception))
        self.assertNotIn("Traceback", str(cm.exception))
        self.assertEqual(st.partials(), [])

    def test_a_disabled_policy_without_grant_never_reaches_the_network(self):
        pol = NetworkPolicy()
        with self.assertRaises(Exception):
            pol.open("model_download", KF.MODEL_FILE.url, allowed_host_suffixes=KF.HOSTS)
        self.assertEqual(pol.audit, [])

    def test_hash_check_detects_a_file_changed_after_install(self):
        st = self.store()
        st.download(FakePolicy(), consent=True)
        p = st.dir / TINY[0].name
        data = bytearray(p.read_bytes())
        data[100] ^= 0xFF
        p.write_bytes(bytes(data))
        self.assertEqual(st.verify_hashes()[TINY[0].name], "changed")

    def test_real_file_names_urls_and_sizes_are_the_documented_approximations(self):
        self.assertEqual([f.name for f in KF.FILES], ["kokoro-v1.0.onnx", "voices-v1.0.bin"])
        self.assertEqual(KF.human_size(KF.approx_total_bytes()), "338 MB")
        self.assertIn("About 338 MB", KE.KokoroEngine.size_note)
        self.assertTrue(all(f.url.startswith(KF.RELEASE_BASE) for f in KF.FILES))


class SpeakerLifecycle(Base):
    def run_job(self, e, text="Hello there.", voice="af_heart", rate=1.0, volume=0.5):
        sp = e.speaker()
        sp.speak(text, rate, volume, voice)
        self.assertTrue(wait_until(lambda: not sp.is_speaking()), "job did not finish")
        return sp

    def test_synthesis_and_playback_complete_and_are_reported_separately_and_honestly(self):
        e = self.kokoro()
        sp = self.run_job(e, volume=0.5)
        r = sp.report()
        self.assertEqual(sp.exit_code(), 0)
        self.assertTrue(r["synthesis_ok"] and r["playback_started"] and r["playback_ok"])
        self.assertEqual(r["sample_rate"], 24000)
        self.assertAlmostEqual(r["audio_seconds"], 0.2, places=2)
        sink = FakeSink.instances[0]
        self.assertEqual(sink.opened_sr, 24000)
        self.assertEqual(sink.total_samples(), int(24000 * 0.2))
        self.assertAlmostEqual(float(np.max(sink.blocks[0])), 0.25, places=5)          # 0.5 audio x 0.5 volume
        self.assertEqual(sink.close_args[-1], True if sink.aborted else sink.close_args[-1])
        self.assertTrue(sink.closed)

    def test_rate_maps_to_model_speed_clamped_and_the_language_is_passed(self):
        e = self.kokoro()
        self.run_job(e, rate=1.5)
        self.assertEqual((self.model.calls[0]["speed"], self.model.calls[0]["lang"], self.model.calls[0]["voice"]), (1.5, "en-us", "af_heart"))

    def test_long_text_is_synthesised_in_chunks_in_order(self):
        text = " ".join(f"This is sentence number {i}, and it is a fairly ordinary sentence." for i in range(12))
        e = self.kokoro()
        self.run_job(e, text=text)
        self.assertGreater(len(self.model.calls), 2)
        self.assertEqual(" ".join(c["text"] for c in self.model.calls).split(), text.split())
        self.assertTrue(all(len(c["text"]) <= KE.CHUNK_CHARS for c in self.model.calls))

    def test_split_text_edge_cases(self):
        self.assertEqual(KE.split_text(""), [])
        self.assertEqual(KE.split_text("   \n  "), [])
        self.assertEqual(KE.split_text("One. Two! Three?"), ["One. Two! Three?"])
        long = "word " * 200
        parts = KE.split_text(long, 50)
        self.assertTrue(all(len(p) <= 50 for p in parts))
        self.assertEqual(" ".join(parts).split(), long.split())

    def test_phases_go_loading_generating_playing_then_idle(self):
        self.model = FakeModel(delay=0.05, seconds=0.2)
        e = self.kokoro()
        e.runtime._factory = lambda m, v: (time.sleep(0.15), self.model)[1]
        self.sink_kw = {"write_delay": 0.02}
        sp = e.speaker()
        sp.speak("Hello there. This is a second sentence.", 1.0, 0.8, "af_heart")
        seen = []
        while sp.is_speaking():
            if not seen or seen[-1] != sp.phase:
                seen.append(sp.phase)
            time.sleep(0.002)
        self.assertEqual([p for p in seen if p != "idle"], ["loading", "generating", "playing"])
        self.assertEqual(sp.phase, "idle")

    def test_unsupported_voice_is_refused_at_once_with_a_plain_message_and_nothing_starts(self):
        e = self.kokoro()
        with self.assertRaises(KE.KokoroError) as cm:
            e.speaker().speak("hi", 1.0, 0.8, "af_missing")
        self.assertIn("af_missing", str(cm.exception))
        self.assertEqual((kokoro_threads(), FakeSink.instances), ([], []))

    def test_a_voice_the_loaded_model_does_not_have_is_an_error_not_silence(self):
        self.model = FakeModel(voices=("af_heart",))
        e = self.kokoro()
        sp = self.run_job(e, voice="am_onyx")
        self.assertEqual(sp.exit_code(), 1)
        self.assertIn("does not have the voice", sp.last_error)

    def test_synthesis_error_is_failure_and_nothing_is_reported_as_played(self):
        self.model = FakeModel(fail=RuntimeError("onnx exploded"))
        sp = self.run_job(self.kokoro())
        r = sp.report()
        self.assertEqual(sp.exit_code(), 1)
        self.assertFalse(r["synthesis_ok"] or r["playback_ok"])
        self.assertIn("could not generate speech", sp.last_error)
        self.assertEqual(FakeSink.instances, [])

    def test_unusable_audio_is_an_error(self):
        for bad in (np.zeros(0, dtype=np.float32), np.array([0.1, float("nan")], dtype=np.float32)):
            self.model = FakeModel(bad_audio=bad)
            sp = self.run_job(self.kokoro())
            self.assertEqual(sp.exit_code(), 1, bad)
            self.assertIn("no usable audio", sp.last_error)

    def test_audio_device_failure_is_reported_with_synthesis_and_playback_kept_apart(self):
        self.sink_kw = {"fail_open": KE.KokoroError("Jarvis could not open the audio output device (no device). Check the output device in Windows sound settings.")}
        sp = self.run_job(self.kokoro())
        r = sp.report()
        self.assertEqual(sp.exit_code(), 1)
        self.assertFalse(r["playback_ok"])
        self.assertIn("audio output device", sp.last_error)
        self.sink_kw = {"fail_write": KE.KokoroError("Audio playback failed (device removed).")}
        sp = self.run_job(self.kokoro())
        self.assertEqual(sp.exit_code(), 1)
        self.assertFalse(sp.report()["playback_ok"])
        self.assertTrue(all(s.closed for s in FakeSink.instances))              # the device is released on failure

    def test_unexpected_exception_in_the_sink_is_caught_and_released(self):
        self.sink_kw = {"fail_write": ZeroDivisionError("weird")}
        sp = self.run_job(self.kokoro())
        self.assertEqual(sp.exit_code(), 1)
        self.assertIn("unexpectedly", sp.last_error)
        self.assertTrue(FakeSink.instances[0].closed)

    def test_stop_cuts_playback_at_once_aborts_the_device_and_ends_the_job(self):
        self.sink_kw = {"write_delay": 5.0}
        e = self.kokoro()
        sp = e.speaker()
        sp.speak("A fairly long sentence for the sink to hold.", 1.0, 0.8, "af_heart")
        self.assertTrue(wait_until(lambda: sp.phase == "playing"))
        t0 = time.monotonic()
        sp.stop()
        self.assertLess(time.monotonic() - t0, 0.5)                       # stop() itself does not wait
        self.assertTrue(wait_until(lambda: not sp.is_speaking(), 3.0))
        self.assertTrue(FakeSink.instances[0].aborted)
        self.assertTrue(FakeSink.instances[0].closed)
        self.assertIsNone(sp.exit_code())                                  # cancelled: neither success nor failure
        self.assertEqual(sp.last_error, "")
        self.assertTrue(wait_until(lambda: kokoro_threads() == []))

    def test_stop_during_a_slow_synthesis_discards_it_and_never_opens_the_audio_device(self):
        self.model = FakeModel(delay=0.3)
        e = self.kokoro()
        sp = e.speaker()
        sp.speak("One sentence. Another sentence. And a third.", 1.0, 0.8, "af_heart")
        self.assertTrue(wait_until(lambda: len(self.model.calls) >= 1))
        sp.stop()
        self.assertTrue(wait_until(lambda: not sp.is_speaking(), 3.0))
        self.assertEqual(FakeSink.instances, [])
        self.assertTrue(wait_until(lambda: kokoro_threads() == [], 3.0))
        self.assertLessEqual(len(self.model.calls), 2)                    # pending sentences were cancelled

    def test_stop_during_model_loading_cancels_before_any_synthesis(self):
        gate = threading.Event()
        e = self.kokoro()
        e.runtime._factory = lambda m, v: (gate.wait(2), self.model)[1]
        sp = e.speaker()
        sp.speak("hi there", 1.0, 0.8, "af_heart")
        self.assertEqual(sp.phase, "loading")
        sp.stop()
        gate.set()
        self.assertTrue(wait_until(lambda: not sp.is_speaking(), 3.0))
        self.assertEqual((self.model.calls, FakeSink.instances), ([], []))

    def test_a_new_speak_cancels_the_old_one_and_only_the_new_one_finishes_normally(self):
        self.sink_kw = {"write_delay": 5.0}
        e = self.kokoro()
        sp = e.speaker()
        sp.speak("first thing to say", 1.0, 0.8, "af_heart")
        self.assertTrue(wait_until(lambda: sp.phase == "playing"))
        first = FakeSink.instances[0]
        self.sink_kw = {}
        sp.speak("second thing", 1.0, 0.8, "am_onyx")
        self.assertTrue(wait_until(lambda: not sp.is_speaking(), 4.0))
        self.assertTrue(first.aborted)
        self.assertEqual(sp.exit_code(), 0)
        self.assertEqual(self.model.calls[-1]["voice"], "am_onyx")

    def test_one_model_copy_is_shared_and_cannot_be_unloaded_while_in_use(self):
        loads = []
        e = self.kokoro()
        e.runtime._factory = lambda m, v: (loads.append(1), self.model)[1]
        self.sink_kw = {"write_delay": 0.3}
        sp = e.speaker()
        sp.speak("hello there", 1.0, 0.8, "af_heart")
        self.assertTrue(wait_until(lambda: sp.phase == "playing"))
        self.assertFalse(e.runtime.unload())                               # refused: playback is using it
        self.assertTrue(e.runtime.loaded())
        self.assertTrue(wait_until(lambda: not sp.is_speaking(), 4.0))
        sp.speak("again", 1.0, 0.8, "af_heart")
        self.assertTrue(wait_until(lambda: not sp.is_speaking(), 4.0))
        self.assertEqual(len(loads), 1)
        self.assertTrue(e.runtime.unload())
        self.assertFalse(e.runtime.loaded())

    def test_close_waits_for_workers_releases_everything_and_refuses_new_speech(self):
        self.sink_kw = {"write_delay": 5.0}
        e = self.kokoro()
        sp = e.speaker()
        sp.speak("a sentence that is still playing", 1.0, 0.8, "af_heart")
        self.assertTrue(wait_until(lambda: sp.phase == "playing"))
        e.close()
        self.assertEqual(kokoro_threads(), [])
        self.assertTrue(all(s.closed for s in FakeSink.instances))
        self.assertFalse(e.runtime.loaded())
        with self.assertRaises(KE.KokoroError):
            sp.speak("too late", 1.0, 0.8, "af_heart")
        self.assertEqual(kokoro_threads(), [])

    def test_repeated_speak_and_stop_leaks_no_threads_or_open_devices(self):
        self.model = FakeModel(delay=0.01)
        self.sink_kw = {"write_delay": 0.01}
        e = self.kokoro()
        sp = e.speaker()
        for _ in range(15):
            sp.speak("Some words to say. And some more words.", 1.0, 0.8, "af_heart")
            time.sleep(0.02)
            sp.stop()
        e.close()
        self.assertEqual(kokoro_threads(), [])
        self.assertTrue(all(s.closed for s in FakeSink.instances))

    def test_empty_text_plays_nothing_and_does_not_claim_playback(self):
        sp = self.run_job(self.kokoro(), text="  ")
        self.assertEqual(sp.exit_code(), 0)
        self.assertFalse(sp.report()["playback_started"])
        self.assertFalse(sp.report()["playback_ok"])
        self.assertEqual(FakeSink.instances, [])

    def test_a_failed_job_stops_its_synthesis_thread_promptly(self):
        self.sink_kw = {"fail_write": KE.KokoroError("Audio playback failed (device removed).")}
        text = " ".join(f"This is sentence number {i}, and it is a fairly ordinary sentence." for i in range(30))
        e = self.kokoro()
        sp = e.speaker()
        t0 = time.monotonic()
        sp.speak(text, 1.0, 0.8, "af_heart")
        self.assertTrue(wait_until(lambda: not sp.is_speaking(), 3.0))
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(sp.exit_code(), 1)
        self.assertLess(len(self.model.calls), len(KE.split_text(text)))             # the remaining sentences were never synthesised
        self.assertTrue(wait_until(lambda: kokoro_threads() == [], 1.0))

    def test_shutdown_during_a_model_load_discards_the_model_and_leaves_no_thread(self):
        gate = threading.Event()
        e = self.kokoro()
        e.runtime._factory = lambda m, v: (gate.wait(3), self.model)[1]
        sp = e.speaker()
        sp.speak("hello there", 1.0, 0.8, "af_heart")
        self.assertEqual(sp.phase, "loading")
        threading.Timer(0.2, gate.set).start()
        e.close()
        self.assertFalse(e.runtime.loaded())
        self.assertEqual(kokoro_threads(), [])
        self.assertEqual(FakeSink.instances, [])
        with self.assertRaises(KE.KokoroError):
            e.runtime.acquire()


# =====================================================================================================================================================
class PreviewWithKokoro(Base):
    def preview(self, m, voice="kokoro:af_heart", **kw):
        pv = P.VoicePreview(m, poll=0.003, **kw)
        self.addCleanup(pv.close)
        states = []
        orig = pv._set

        def spy(state, message, v, gen=None):
            r = orig(state, message, v, gen)
            if not states or states[-1] != state:
                states.append(state)
            return r
        pv._set = spy
        pv.start(voice)
        self.assertTrue(wait_until(lambda: pv.state()["state"] in P.TERMINAL, 8.0), pv.state())
        return pv, states

    def test_preview_uses_the_real_engine_path_and_shows_loading_generating_playing_finished(self):
        self.model = FakeModel(delay=0.03, seconds=0.2)
        e = self.kokoro()
        e.runtime._factory = lambda m, v: (time.sleep(0.05), self.model)[1]
        self.sink_kw = {"write_delay": 0.03}
        m = self.mgr(kokoro=e)
        m.refresh()
        pv, states = self.preview(m)
        self.assertEqual(states, ["loading", "generating", "playing", "finished"])
        st = pv.state()
        self.assertEqual(self.model.calls[0]["voice"], "af_heart")
        self.assertEqual(self.model.calls[0]["text"], P.PREVIEW_TEXT)
        self.assertEqual(self.sapi_sp.said, [])                                     # SAPI was not used
        self.assertIn("generated", st["message"])
        self.assertIn("handed to the audio output device", st["message"])
        self.assertIn("cannot hear your speakers", st["message"])
        self.assertIn("measured on this run only", st["message"])
        self.assertNotRegex(VM.preview_text(st).lower(), r"(was|were|been) heard|you heard it|sounds? (natural|great|good)")

    def test_each_of_the_four_voices_previews_with_its_own_id(self):
        m = self.mgr()
        m.refresh()
        for v in PRESETS:
            self.model.calls.clear()
            pv, _ = self.preview(m, "kokoro:" + v)
            self.assertEqual(pv.state()["state"], "finished", v)
            self.assertEqual({c["voice"] for c in self.model.calls}, {v})

    def test_user_stop_during_playback_ends_in_stopped_and_aborts_the_device(self):
        self.sink_kw = {"write_delay": 5.0}
        m = self.mgr()
        m.refresh()
        pv = P.VoicePreview(m, poll=0.003)
        self.addCleanup(pv.close)
        pv.start("kokoro:af_heart")
        self.assertTrue(wait_until(lambda: pv.state()["state"] == "playing"))
        t0 = time.monotonic()
        pv.stop()
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(pv.state()["state"], "stopped")
        self.assertFalse(pv.state()["busy"])
        self.assertTrue(FakeSink.instances[0].aborted)
        self.assertTrue(wait_until(lambda: kokoro_threads() == []))

    def test_stop_while_loading_and_while_generating(self):
        gate = threading.Event()
        e = self.kokoro()
        e.runtime._factory = lambda m, v: (gate.wait(3), self.model)[1]
        m = self.mgr(kokoro=e)
        m.refresh()
        pv = P.VoicePreview(m, poll=0.003)
        self.addCleanup(pv.close)
        pv.start("kokoro:af_heart")
        self.assertTrue(wait_until(lambda: pv.state()["state"] == "loading"))
        pv.stop()
        gate.set()
        self.assertEqual(pv.state()["state"], "stopped")
        self.assertTrue(wait_until(lambda: kokoro_threads() == []))
        self.assertEqual(FakeSink.instances, [])

    def test_missing_model_gives_an_error_that_says_how_to_fix_it_and_switch_to_sapi(self):
        e = self.kokoro(install=False)
        m = self.mgr(kokoro=e)
        pv, states = self.preview(m, "kokoro:af_heart")
        self.assertEqual(pv.state()["state"], "error")
        self.assertNotIn("finished", states)
        self.assertEqual(self.sapi_sp.said, [])

    def test_synthesis_ok_but_playback_failure_is_an_error_not_a_success(self):
        self.sink_kw = {"fail_open": KE.KokoroError("Jarvis could not open the audio output device (busy).")}
        m = self.mgr()
        m.refresh()
        pv, states = self.preview(m)
        self.assertEqual(pv.state()["state"], "error")
        self.assertNotIn("finished", states)
        self.assertIn("audio output device", pv.state()["message"])

    def test_model_failure_during_preview_is_a_readable_error(self):
        e = self.kokoro()
        e.runtime._factory = lambda m, v: (_ for _ in ()).throw(RuntimeError("bad file"))
        m = self.mgr(kokoro=e)
        m.refresh()
        pv, _ = self.preview(m)
        self.assertEqual(pv.state()["state"], "error")
        self.assertIn("could not be loaded", pv.state()["message"])
        self.assertTrue(VM.preview_text(pv.state()).startswith("Could not play the sample."))

    def test_unsupported_voice_id_in_a_preview_is_refused(self):
        m = self.mgr()
        m.refresh()
        pv, _ = self.preview(m, "kokoro:af_not_real")
        self.assertEqual(pv.state()["state"], "error")
        self.assertIn("not installed", pv.state()["message"])

    def test_sapi_preview_still_works_exactly_as_before(self):
        m = self.mgr()
        m.refresh()
        pv, states = self.preview(m, "sapi:Alpha Voice")
        self.assertEqual(pv.state()["state"], "finished")
        self.assertEqual(self.sapi_sp.said[0][3], "Alpha Voice")
        self.assertNotIn("generating", states)
        self.assertEqual(FakeSink.instances, [])


class SelectionPersistenceAndOverrides(Base):
    def test_switching_engine_and_voice_is_saved_and_a_new_session_restores_it(self):
        m = self.mgr()
        m.refresh()
        m.apply("kokoro:am_onyx")
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").tts_voice, "kokoro:am_onyx")
        m2 = self.mgr()
        r = m2.resolve()
        self.assertEqual((r.engine_id, r.voice_name, r.fallback), ("kokoro", "am_onyx", False))
        self.assertEqual(m2.catalog()["selected_engine"], "kokoro")
        m2.select_engine("sapi")
        self.assertEqual(self.mgr().resolve().engine_id, "sapi")
        self.assertEqual(self.mgr().catalog()["selected_engine"], "sapi")
        m2.select_engine("kokoro")
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").tts_voice, "kokoro:af_heart")      # first listed voice

    def test_switching_engines_keeps_every_other_setting(self):
        self.save(rate=1.4, volume=0.3, wake_phrase="hey computer now", input_mode="off", mic_device="Fake Mic", vosk_model=None, speak_in_window=True)
        before = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        m = self.mgr()
        m.select_engine("kokoro")
        after = VoiceSettings.load(self.cfg.profile_dir / "voice.json")
        self.assertEqual({**vars(after), "tts_voice": None}, {**vars(before), "tts_voice": None})

    def test_selecting_a_not_ready_engine_is_refused_with_its_reason_and_nothing_changes(self):
        e = self.kokoro(install=False)
        m = self.mgr(kokoro=e)
        self.save(tts_voice="sapi:Alpha Voice")
        with self.assertRaises(ValueError) as cm:
            m.select_engine("kokoro")
        self.assertIn("not ready", str(cm.exception))
        self.assertIn("Install / repair", str(cm.exception))
        self.assertEqual(VoiceSettings.load(self.cfg.profile_dir / "voice.json").tts_voice, "sapi:Alpha Voice")
        with self.assertRaises(ValueError):
            m.select_engine("nonexistent")

    def test_personality_override_wins_only_for_its_mode_and_switching_mode_never_changes_the_saved_voice(self):
        m = self.mgr(persona=None)
        m.refresh()
        m.apply("kokoro:af_heart")
        m.apply("sapi:Bravo Voice", "professional")
        text0 = (self.cfg.profile_dir / "voice.json").read_text()
        for persona, want in ((None, ("kokoro", "af_heart", "global")), ("professional", ("sapi", "Bravo Voice", "persona")), ("friendly", ("kokoro", "af_heart", "global"))):
            r = m.resolve(persona)
            self.assertEqual((r.engine_id, r.voice_name, r.source), want, persona)
        self.assertEqual((self.cfg.profile_dir / "voice.json").read_text(), text0)
        m.clear_persona_voice("professional")
        self.assertEqual(m.resolve("professional").engine_id, "kokoro")

    def test_a_kokoro_override_with_kokoro_unavailable_falls_to_the_global_voice_and_says_so(self):
        e = self.kokoro()
        m = self.mgr(kokoro=e)
        m.refresh()
        m.apply("sapi:Alpha Voice")
        m.apply("kokoro:am_onyx", "professional")
        (KF.model_dir(self.data) / TINY[0].name).unlink()
        r = m.resolve("professional")
        self.assertEqual((r.engine_id, r.voice_name, r.fallback), ("sapi", "Alpha Voice", True))
        self.assertIn("not ready", r.note)
        self.assertIn("am_onyx", r.note)


class NoSilentSubstitution(Base):
    def test_kokoro_chosen_but_not_ready_means_silence_and_an_explicit_reason_never_sapi(self):
        e = self.kokoro()
        m = self.mgr(kokoro=e)
        m.refresh()
        m.apply("kokoro:af_heart")
        (KF.model_dir(self.data) / TINY[1].name).unlink()
        r = m.resolve()
        self.assertEqual((r.engine_id, r.source, r.fallback), (None, "unavailable", False))
        self.assertIn("will not speak with a different engine", r.note)
        self.assertIn("not ready", r.note)
        with self.assertRaises(E.EngineError):
            m.speaker().speak("a reply", 1.0, 0.8)
        self.assertEqual(self.sapi_sp.said, [])
        self.assertEqual(m.state()["resolution"]["source"], "unavailable")

    def test_spoken_replies_report_the_failure_instead_of_using_sapi(self):
        e = self.kokoro()
        self.save(speak_in_window=True, tts_voice="kokoro:af_heart")
        so = SpeechOut(self.cfg, speaker_factory=lambda: self.sapi_sp)       # a registry without Kokoro cannot be used here: use a real registry below
        so.voices.registry = E.EngineRegistry([E.SapiEngine(speaker=self.sapi_sp, platform="win32"), e])
        (KF.model_dir(self.data) / TINY[0].name).unlink()
        self.assertFalse(so.speak("Hello"))
        self.assertIn("not ready", so.last_error)
        self.assertEqual(self.sapi_sp.said, [])

    def test_a_saved_voice_missing_from_an_installed_kokoro_is_reported_and_not_replaced(self):
        e = self.kokoro(ids=("af_heart", "af_aoede", "am_onyx"))
        m = self.mgr(kokoro=e)
        self.save(tts_voice="kokoro:af_nicole")
        r = m.resolve()
        self.assertEqual((r.engine_id, r.source), (None, "unavailable"))
        self.assertIn("af_nicole", r.note)
        self.assertIn("not installed", r.note)
        self.assertEqual(self.sapi_sp.said, [])

    def test_recovery_text_is_offered_only_when_needed_and_names_both_ways_out(self):
        e = self.kokoro(install=False)
        m = self.mgr(kokoro=e)
        self.save(tts_voice="kokoro:af_heart")
        m.refresh()
        cat = m.catalog()
        cat["resolution"] = m.state()
        txt = VM.recovery_text(cat)
        self.assertIn("Install / repair", txt)
        self.assertIn("Use Windows speech", txt)
        self.assertIn("stays silent", txt)
        self.assertIn("NOT being spoken", VM.resolution_text(cat))
        self.save(tts_voice="sapi:Alpha Voice")
        cat["resolution"] = m.state()
        m.last_resolution = None
        cat["resolution"] = m.state()
        self.assertEqual(VM.recovery_text(cat), "")

    def test_sapi_keeps_its_old_fallback_to_the_engine_default_voice(self):
        m = self.mgr()
        m.refresh()
        self.save(tts_voice="sapi:Nobody")
        r = m.resolve()
        self.assertEqual((r.engine_id, r.voice_name, r.fallback), ("sapi", None, True))

    def test_speaking_through_the_resolving_speaker_uses_kokoro_and_not_sapi(self):
        m = self.mgr()
        m.refresh()
        m.apply("kokoro:am_onyx")
        sp = m.speaker()
        sp.speak("a normal reply", 1.2, 0.6)
        self.assertTrue(wait_until(lambda: not sp.is_speaking()))
        self.assertEqual(sp.exit_code(), 0)
        self.assertEqual((self.model.calls[0]["voice"], self.model.calls[0]["speed"]), ("am_onyx", 1.2))
        self.assertEqual(self.sapi_sp.said, [])
        sp.stop()


# =====================================================================================================================================================
class ControllerIntegration(AppCase):
    """The real AppController with the real default registry (SAPI + Kokoro) but FAKE Kokoro model / sink / network."""

    def make(self, **kw):
        self.cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        mic = mock.MagicMock()
        c = AppController(self.cfg, mic_factory=lambda: mic, **kw)
        self.addCleanup(c.shutdown, 3.0)
        eng = c.kokoro_engine()
        eng.store = eng.runtime.store = KF.KokoroModelStore(self.cfg.data_dir, TINY)
        eng._find_spec = lambda n: object()
        eng._providers_probe = lambda: ["CPUExecutionProvider"]                     # never import the real onnxruntime in a unit test
        eng.runtime._factory = lambda m, v: FakeModel()
        eng.sink_factory = lambda: FakeSink()
        self.mic = mic
        return c, eng

    def pump(self, c, pred, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            c.tasks.pump()
            if pred():
                return True
            time.sleep(0.005)
        return False

    def test_kokoro_is_registered_in_the_real_controller_and_visible_in_the_catalog(self):
        c, eng = self.make()
        cat = c.voice_catalog()
        self.assertEqual([e["id"] for e in cat["engines"]], ["sapi", "kokoro"])
        st = c.kokoro_state()
        self.assertTrue(st["present"])
        self.assertEqual(st["status"]["model"]["state"], "missing")
        self.assertFalse(eng.runtime.loaded())

    def test_install_needs_consent_runs_off_the_interface_thread_reports_progress_and_makes_the_voices_selectable(self):
        c, eng = self.make()
        pol = FakePolicy(chunk_delay=0.05)
        with mock.patch("jarvis.net.policy.NetworkPolicy.load", return_value=pol):
            self.assertFalse(c.kokoro_install(consent=False))
            self.assertIn("confirmation", c.kokoro["message"])
            self.assertEqual(pol.requests, [])
            t0 = time.monotonic()
            self.assertTrue(c.kokoro_install(consent=True))
            self.assertLess(time.monotonic() - t0, 0.3)                       # returned at once: the download runs on a worker
            self.assertTrue(c.kokoro["running"])
            self.assertFalse(c.kokoro_install(consent=True))                  # a second one is refused while one runs
            self.assertTrue(self.pump(c, lambda: not c.kokoro["running"]))
        self.assertTrue(c.kokoro["result"]["ok"], c.kokoro["result"])
        self.assertEqual(c.kokoro_state()["status"]["model"]["state"], "ready")
        self.assertTrue(self.pump(c, lambda: c.voice_catalog()["loaded"] and not c.voice_catalog()["refreshing"]))
        c.speech.voices.refresh()
        names = [v["name"] for v in c.voice_catalog()["categories"]["unspecified"] if v["engine"] == "kokoro"]
        self.assertEqual(names, list(PRESETS))
        self.assertFalse(pol.is_enabled("model_download"))

    def test_cancel_removes_partial_files_and_the_ui_can_try_again(self):
        c, eng = self.make()
        pol = FakePolicy(chunk_delay=0.4)
        with mock.patch("jarvis.net.policy.NetworkPolicy.load", return_value=pol):
            self.assertTrue(c.kokoro_install(consent=True))
            self.assertTrue(self.pump(c, lambda: bool(eng.store.partials())))
            self.assertTrue(c.kokoro_cancel())
            self.assertTrue(self.pump(c, lambda: not c.kokoro["running"]))
        self.assertTrue(c.kokoro["result"]["cancelled"])
        self.assertEqual((eng.store.partials(), eng.store.status()["state"]), ([], "missing"))
        self.assertIn("Cancelled", VM.kokoro_job_text(c.kokoro))

    def test_a_failed_download_is_a_plain_message(self):
        c, eng = self.make()
        pol = FakePolicy(files={})
        with mock.patch("jarvis.net.policy.NetworkPolicy.load", return_value=pol):
            c.kokoro_install(consent=True)
            self.assertTrue(self.pump(c, lambda: not c.kokoro["running"]))
        self.assertFalse(c.kokoro["result"]["ok"])
        self.assertIn("download failed", c.kokoro["result"]["message"])
        self.assertIn("Failed.", VM.kokoro_job_text(c.kokoro))

    def test_the_confirmed_entry_point_is_the_only_one_that_downloads(self):
        c, eng = self.make()
        pol = FakePolicy()
        with mock.patch("jarvis.net.policy.NetworkPolicy.load", return_value=pol):
            self.assertFalse(c.kokoro_install())                     # default: no consent, nothing happens
            self.assertEqual(pol.requests, [])
            self.assertTrue(c.kokoro_install_confirmed())
            self.assertTrue(self.pump(c, lambda: not c.kokoro["running"]))
        self.assertTrue(c.kokoro["result"]["ok"])

    def test_guest_sessions_download_and_change_nothing(self):
        c, eng = self.make(guest=True)
        pol = FakePolicy()
        with mock.patch("jarvis.net.policy.NetworkPolicy.load", return_value=pol):
            self.assertFalse(c.kokoro_install(consent=True))
        self.assertEqual(pol.requests, [])
        with self.assertRaises(ValueError):
            c.voice_select_engine("kokoro")

    def test_check_files_runs_on_a_worker_and_reports_changed_files(self):
        c, eng = self.make()
        eng.store.download(FakePolicy(), consent=True)
        self.assertTrue(c.kokoro_check())
        self.assertTrue(self.pump(c, lambda: not c.kokoro["running"]))
        self.assertTrue(c.kokoro["result"]["ok"], c.kokoro["result"])
        self.assertIn("does not prove", c.kokoro["result"]["message"])
        p = eng.store.dir / TINY[0].name
        b = bytearray(p.read_bytes())
        b[50] ^= 1
        p.write_bytes(bytes(b))
        c.kokoro_check()
        self.assertTrue(self.pump(c, lambda: not c.kokoro["running"]))
        self.assertFalse(c.kokoro["result"]["ok"])
        self.assertIn(TINY[0].name, c.kokoro["result"]["message"])

    def test_preview_and_selection_through_the_controller_never_touch_the_microphone_or_listening_mode(self):
        c, eng = self.make()
        eng.store.download(FakePolicy(), consent=True)
        before = c.speech.settings().input_mode
        c.voice_select_engine("kokoro")
        self.assertEqual(c.speech.settings().tts_voice, "kokoro:af_heart")
        t0 = time.monotonic()
        c.voice_preview_start("kokoro:am_onyx")
        self.assertLess(time.monotonic() - t0, 0.5)                          # returns immediately; synthesis is on a worker
        self.assertTrue(self.pump(c, lambda: c.voice_catalog()["preview"]["state"] in P.TERMINAL))
        self.assertEqual(c.voice_catalog()["preview"]["state"], "finished")
        self.assertEqual(c.speech.settings().input_mode, before)
        self.assertEqual(c.speech.settings().input_mode, "off")
        self.mic.assert_not_called()
        self.assertIsNone(c.voice)                                           # the hands-free service was never even created
        self.assertEqual(c.voice_input["result"], None)

    def test_interface_calls_stay_fast_while_the_model_is_loading(self):
        c, eng = self.make()
        eng.store.download(FakePolicy(), consent=True)
        gate = threading.Event()
        eng.runtime._factory = lambda m, v: (gate.wait(3), FakeModel())[1]
        c.voice_preview_start("kokoro:af_heart")
        self.assertTrue(self.pump(c, lambda: c.voice_catalog()["preview"]["state"] == "loading"))
        t0 = time.monotonic()
        for _ in range(50):
            c.voice_catalog()
            c.kokoro_state()
            c.voice_state() if False else None
        self.assertLess(time.monotonic() - t0, 1.0)
        c.voice_preview_stop()
        gate.set()
        self.assertEqual(c.voice_catalog()["preview"]["state"], "stopped")

    def test_unrelated_conversation_memory_and_project_data_are_untouched(self):
        c, eng = self.make()
        sentinels = {}
        for name in ("memory.sqlite", "persona.json", "permissions.json", "projects.json", "conversation.json"):
            p = self.cfg.profile_dir / name
            p.write_bytes(name.encode() * 50)
            sentinels[name] = hashlib.sha256(p.read_bytes()).hexdigest()
        with mock.patch("jarvis.net.policy.NetworkPolicy.load", return_value=FakePolicy()):
            c.kokoro_install(consent=True)
            self.assertTrue(self.pump(c, lambda: not c.kokoro["running"]))
        c.speech.voices.refresh()
        c.voice_select_engine("kokoro")
        c.voice_preview_start("kokoro:af_aoede")
        self.assertTrue(self.pump(c, lambda: c.voice_catalog()["preview"]["state"] in P.TERMINAL))
        for name, h in sentinels.items():
            self.assertEqual(hashlib.sha256((self.cfg.profile_dir / name).read_bytes()).hexdigest(), h, name)
        written = {p.name for p in self.cfg.data_dir.rglob("*") if p.is_file()} - set(sentinels)
        self.assertTrue(written <= {"voice.json", TINY[0].name, TINY[1].name, KF.MANIFEST}, written)

    def test_shutdown_leaves_no_kokoro_threads_and_nothing_starts_afterwards(self):
        c, eng = self.make()
        eng.store.download(FakePolicy(), consent=True)
        eng.sink_factory = lambda: FakeSink(write_delay=5.0)
        c.voice_select_engine("kokoro")
        c.voice_preview_start("kokoro:af_heart")
        self.assertTrue(self.pump(c, lambda: c.voice_catalog()["preview"]["state"] == "playing"))
        c.shutdown(3.0)
        self.assertEqual(kokoro_threads(), [])
        self.assertFalse(any(t.name == "jarvis-voice-preview" and t.is_alive() for t in threading.enumerate()))
        self.assertFalse(c.kokoro_install(consent=True))
        with self.assertRaises(E.EngineError):
            eng.speaker().speak("late", 1.0, 0.8, "af_heart")
        self.assertEqual(kokoro_threads(), [])


# =====================================================================================================================================================
class LocalFirstAndIsolation(Base):
    def test_speaking_and_listing_never_open_a_socket(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")):
            with mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
                m = self.mgr()
                m.refresh()
                m.apply("kokoro:af_heart")
                sp = m.speaker()
                sp.speak("hello", 1.0, 0.8)
                self.assertTrue(wait_until(lambda: not sp.is_speaking()))
                self.assertEqual(sp.exit_code(), 0)

    def test_no_api_keys_no_telemetry_and_no_network_modules_in_the_new_code(self):
        for f in (ROOT / "src/jarvis/voice/kokoro_engine.py", ROOT / "src/jarvis/models/kokoro_files.py"):
            t = f.read_text(encoding="utf-8")
            for bad in ("import urllib", "from urllib", "import http", "import socket", "import requests", "import ssl", "urlopen", "api_key", "apikey", "API_KEY",
                        "openai", "elevenlabs", "azure", "polly", "telemetry", "huggingface"):
                self.assertNotIn(bad, t, f"{f.name} mentions {bad}")
        t = (ROOT / "src/jarvis/models/kokoro_files.py").read_text(encoding="utf-8")
        self.assertIn('policy.open("model_download"', t)                      # the ONLY way it reaches the network is the Stage 5 policy, for model_download
        self.assertEqual(len(re.findall(r"policy\.open\(", t)), 1)

    def test_text_to_speech_code_never_references_microphone_recognition_or_hands_free(self):
        for f in (ROOT / "src/jarvis/voice/kokoro_engine.py", ROOT / "src/jarvis/models/kokoro_files.py"):
            t = f.read_text(encoding="utf-8")
            for bad in ("SoundDeviceMic", "InputStream", "vosk", "Vosk", "VoiceService", "input_mode", "wake_phrase", "mic_device", "set_input"):
                if f.name == "kokoro_engine.py" and bad == "vosk":
                    continue
                self.assertNotIn(bad, t.replace("does not listen", ""), f"{f.name} mentions {bad}")

    def test_the_microphone_library_is_never_used_by_the_engine_and_output_streams_are_output_only(self):
        t = (ROOT / "src/jarvis/voice/kokoro_engine.py").read_text(encoding="utf-8")
        self.assertIn("sd.OutputStream", t)
        self.assertNotRegex(t, r"sd\.(InputStream|Stream|rec|playrec)\(")

    def test_no_model_weights_or_third_party_runtime_are_bundled_or_listed_as_bundled(self):
        sys.path.insert(0, str(ROOT / "packaging"))
        man = importlib.import_module("manifest")
        everything = json.dumps([getattr(man, n) for n in dir(man) if n.isupper() and isinstance(getattr(man, n), (list, tuple, dict, set, frozenset))], default=sorted).lower()
        for bad in ("kokoro_onnx", "onnxruntime", "espeak", "phonemizer", "kokoro-v1.0", "voices-v1.0"):
            self.assertNotIn(bad, everything, bad)
        self.assertIn("jarvis.voice.kokoro_engine", man.HIDDEN_REQUIRED)
        self.assertIn("jarvis.models.kokoro_files", man.HIDDEN_REQUIRED)
        gi = (ROOT / ".gitignore").read_text()
        self.assertIn("*.onnx", gi)
        self.assertIn("voices-v*.bin", gi)

    def test_every_new_module_is_in_the_diagnostics_list_too(self):
        from jarvis.app import diagnostics
        for mod in ("jarvis.voice.kokoro_engine", "jarvis.models.kokoro_files"):
            self.assertIn(mod, diagnostics.REQUIRED_MODULES)
            importlib.import_module(mod)


class ViewModelAndWindow(Base):
    def cat(self, real=False, **kw):
        eng = KE.KokoroEngine(self.data, providers_probe=lambda: [], find_spec=(lambda n: object()) if kw.get("packages", True) else (lambda n: None)) if real else self.kokoro(**kw)
        self.addCleanup(eng.close)
        m = self.mgr(kokoro=eng)
        m.refresh()
        c = m.catalog()
        c["resolution"] = m.state()
        return c

    def test_engine_selector_lists_every_engine_with_its_state(self):
        opts = dict(VM.engine_options(self.cat()))
        self.assertEqual(opts["sapi"], "Windows built-in speech (SAPI) - ready")
        self.assertEqual(opts["kokoro"], "Kokoro (local neural voice) - ready")
        self.assertTrue(dict(VM.engine_options(self.cat(real=True)))["kokoro"].endswith("not ready"))

    def test_voice_lists_can_be_limited_to_one_engine(self):
        c = self.cat()
        self.assertEqual([i for i, _ in VM.voice_items(c, "unspecified", "kokoro")], ["kokoro:" + v for v in PRESETS])
        self.assertEqual(VM.voice_items(c, "unspecified", "sapi"), [])
        self.assertIn("(4)", dict(VM.category_options(c, "kokoro"))["unspecified"])
        self.assertNotIn("unspecified", dict(VM.category_options(c, "sapi")))

    def test_kokoro_status_text_covers_what_the_page_must_say(self):
        t = VM.kokoro_text(self.cat(real=True))
        for s in ("NOT READY", "Model files: not installed", "about 338 MB", "github.com", "works offline", "Install / repair", "does not listen", "Use Windows speech", "Licences:"):
            self.assertIn(s, t if s != "Install / repair" else t + self.cat(real=True)["engines"][1]["note"])
        t = VM.kokoro_text(self.cat())
        for s in ("READY", "installed", "af_heart, af_aoede, af_nicole, am_onyx", "all present", "model not loaded"):
            self.assertIn(s, t)
        t = VM.kokoro_text(self.cat(packages=False))
        self.assertIn("missing kokoro-onnx, sounddevice", t)
        self.assertIn("pip install kokoro-onnx sounddevice", t)

    def test_confirmation_text_states_size_source_licence_offline_and_cancel(self):
        t = VM.kokoro_confirm_text(self.cat(real=True))
        for s in ("about 338 MB", "github.com", "once", "no account, key or fee", "Apache-2.0", "GPL", "No publisher checksum", "cancel"):
            self.assertIn(s, t)

    def test_progress_and_results_text(self):
        self.assertIn("Downloading the Kokoro model: 10 of about 338 MB", VM.kokoro_job_text({"running": True, "op": "install", "current": "10 of about 338 MB"}))
        self.assertIn("Checking", VM.kokoro_job_text({"running": True, "op": "check"}))
        self.assertTrue(VM.kokoro_job_text({"result": {"ok": True, "message": "Downloaded: x. "}}).startswith("Done."))
        self.assertEqual(VM.kokoro_job_text({}), "")

    def test_preview_text_has_a_generating_state(self):
        self.assertIn("Generating", VM.preview_text({"state": "generating", "message": ""}))

    def window(self):
        from test_p34_readiness import _fake_qt
        p = mock.patch.dict(sys.modules, _fake_qt())
        p.start()
        self.addCleanup(p.stop)
        sys.modules.pop("jarvis.ui.window", None)
        self.addCleanup(lambda: sys.modules.pop("jarvis.ui.window", None))
        return importlib.import_module("jarvis.ui.window")

    def test_window_wiring_exists_with_stand_in_qt_only(self):
        W = self.window()
        src = (ROOT / "src/jarvis/ui/window.py").read_text(encoding="utf-8")
        for name in ("_vc_engine_changed", "_vc_use_sapi", "_vk_install", "_vk_cancel", "_vk_check"):
            self.assertTrue(hasattr(W.MainWindow, name), name)
        for w in ("vc_engine", "vc_to_sapi_btn", "vk_install_btn", "vk_cancel_btn", "vk_check_btn", "vk_progress", "vk_text", "vk_job", "vc_recovery"):
            self.assertIn("self." + w + " =", src, w)
        for call in re.findall(r"self\.ctl\.(\w+)\(", src):
            self.assertTrue(hasattr(AppController, call), call)

    def test_the_window_asks_before_downloading_and_never_imports_the_runtime_or_numpy(self):
        src = (ROOT / "src/jarvis/ui/window.py").read_text(encoding="utf-8")
        body = src[src.index("    def _vk_install"):src.index("    def _vk_cancel")]
        self.assertLess(body.index("QMessageBox.question"), body.index("kokoro_install_confirmed()"))
        self.assertEqual(src.count("kokoro_install_confirmed("), 1)
        self.assertEqual(src.count("consent=True"), 1)                      # still only the Import Brain page's own; Kokoro adds none
        for bad in ("kokoro_onnx", "onnxruntime", "import numpy", "kokoro_engine", "kokoro_files"):
            self.assertNotIn(bad, src)

    def test_the_new_labels_are_plain_text_not_rich_text(self):
        src = (ROOT / "src/jarvis/ui/window.py").read_text(encoding="utf-8")
        for w in ("vk_text", "vk_job", "vc_recovery"):
            self.assertIn(f"self.{w}.setTextFormat(Qt.TextFormat.PlainText)", src)


if __name__ == "__main__":
    unittest.main()
