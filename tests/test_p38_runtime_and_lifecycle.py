"""Stage 5 master-order pass: runtime wheel tags, runtime pin consistency, and the early model-identity check at install time.

SYNTHETIC wheels and MOCKED network responses only (fakes from test_p29/test_p33/test_p37). They test our logic. They do not show that the real
llama-cpp-python 0.3.36 wheel exists, installs or loads on Windows, and they do not involve any real model."""
import dataclasses
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_p29_download_safety as P29
import test_p33_windows_tools as T33
from jarvis.models import catalog
from jarvis.models.manager import IntegrityError
from test_p37_install_flow import gguf_with

PIN = "0.3.36"


class WheelTags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def problems(self, name):
        return T33.VW.inspect(T33.make_wheel(self.d, name=name))["problems"]

    def test_the_projects_own_generic_windows_wheel_is_accepted_and_the_reason_is_recorded(self):
        r = T33.VW.inspect(T33.make_wheel(self.d, name=f"llama_cpp_python-{PIN}-py3-none-win_amd64.whl"))
        self.assertEqual(r["problems"], [])
        self.assertEqual(r["status"], "NO_PROBLEMS_FOUND")
        self.assertTrue(any("generic py3-none" in n for n in r["notes"]))

    def test_version_specific_cp312_tags_are_still_accepted(self):
        for tag in ("cp312-cp312", "cp312-abi3", "cp312-none"):
            with self.subTest(tag=tag):
                self.assertEqual(self.problems(f"llama_cpp_python-{PIN}-{tag}-win_amd64.whl"), [])

    def test_a_generic_wheel_for_the_wrong_platform_or_with_a_wrong_python_or_abi_is_still_a_problem(self):
        cases = {"py3-none-win32": "PLATFORM_NOT_WIN_AMD64", "py3-none-any": "PLATFORM_NOT_WIN_AMD64", "py3-none-manylinux_2_17_x86_64": "PLATFORM_NOT_WIN_AMD64",
                 "py2-none-win_amd64": "PYTHON_TAG_NOT_CP312", "cp311-none-win_amd64": "PYTHON_TAG_NOT_CP312", "py3-cp39-win_amd64": "PYTHON_TAG_NOT_CP312",
                 "cp312-cp39-win_amd64": "ABI_TAG_UNEXPECTED"}
        for tag, code in cases.items():
            with self.subTest(tag=tag):
                name = f"llama_cpp_python-{PIN}-{tag}.whl"
                self.assertIn(code, self.problems(name))

    def test_a_generic_wheel_still_has_to_pass_every_content_check(self):
        n = f"llama_cpp_python-{PIN}-py3-none-win_amd64.whl"
        self.assertIn("NO_LICENCE_FILE_IN_WHEEL", T33.VW.inspect(T33.make_wheel(self.d, name=n, licence=False))["problems"])
        self.assertIn("NO_NATIVE_LIBRARIES_FOUND", T33.VW.inspect(T33.make_wheel(self.d, name=n, dll=False))["problems"])
        h = T33.VW.inspect(T33.make_wheel(self.d, name=n), "0" * 64, "test")
        self.assertIn("HASH_MISMATCH", h["problems"])


class RuntimePinIsConsistent(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_every_place_that_installs_the_runtime_uses_the_same_exact_version(self):
        self.assertIn(f'local = ["llama-cpp-python=={PIN}"]', self.read("pyproject.toml"))
        self.assertIn(f'$RuntimeVersion = "{PIN}"', self.read("scripts/build_windows.ps1"))
        for wf in (".github/workflows/intel-bench.yml", ".github/workflows/model-probe.yml"):
            txt = self.read(wf)
            self.assertIn(f"llama-cpp-python=={PIN}", txt, wf)
            self.assertNotRegex(txt, r"pip install llama-cpp-python(?!==)", wf)         # no unpinned install line left

    def test_the_windows_build_downloads_inspects_records_and_only_then_installs_the_wheel_and_never_installs_unpinned(self):
        b = self.read("scripts/build_windows.ps1")
        self.assertNotRegex(b, r"pip install llama-cpp-python(?!==)")
        self.assertLess(b.index("pip download llama-cpp-python==$RuntimeVersion"), b.index("verify_wheel.py"))
        self.assertLess(b.index("verify_wheel.py"), b.index('pip install `"$($whl.FullName)`"'))
        self.assertIn("-RuntimeSha256", b)
        self.assertIn("it was NOT installed", b)                       # a supplied hash that does not match stops the build before installation
        self.assertIn("--only-binary=:all:", b)

    def test_the_pin_matches_what_the_code_was_written_against(self):
        self.assertIn(PIN, self.read("tests/test_p25_disclosure_paths.py"))


class ArchitectureAtInstall(P29.Base):
    def qwen_model(self, size, **kw):
        return dataclasses.replace(self.model(size, mid="fake-qwen3-q4"), architecture="qwen3", **kw)

    def body_with(self, arch):
        p = gguf_with(self.d / f"{arch}.gguf", arch, "Fake")
        return p.read_bytes()

    def test_a_valid_gguf_of_another_architecture_is_refused_before_anything_is_installed(self):
        body = self.body_with("llama")
        m = self.qwen_model(len(body))
        with self.assertRaises(IntegrityError) as cm:
            self.fetch(m, P29.FakeResp(body), declared=len(body), body=body)
        self.assertIn("architecture", str(cm.exception))
        self.assertIn("nothing was installed", str(cm.exception))
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.leftovers(), [])                        # the downloaded partial file is gone

    def test_the_expected_architecture_installs_and_the_checksum_reference_is_recorded_honestly(self):
        body = self.body_with("qwen3")
        m = self.qwen_model(len(body))
        meta = self.fetch(m, P29.FakeResp(body), declared=len(body), body=body)
        rec = json.loads((self.store.path(meta["id"]) / "meta.json").read_text(encoding="utf-8"))
        ref = rec["checksum_reference"]
        self.assertFalse(ref["independent_of_host"])
        self.assertIn("same host", ref["source"])
        self.assertEqual(ref["host_declared_size_bytes"], len(body))
        self.assertTrue(rec["verified_origin"])
        self.assertEqual(rec["gguf"]["architecture"], "qwen3")

    def test_an_entry_without_an_expected_architecture_is_not_restricted(self):
        body = self.body_with("llama")
        m = self.model(len(body))
        self.assertEqual(m.architecture, "")
        meta = self.fetch(m, P29.FakeResp(body), declared=len(body), body=body)
        self.assertEqual(meta["id"], m.id)

    def test_every_built_in_qwen3_entry_names_the_architecture_the_llama_cpp_converter_gives_qwen3(self):
        for m in catalog.BUILTIN:
            with self.subTest(model=m.id):
                self.assertEqual(m.architecture, "qwen3")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------------------------------------------------------------
# Model memory is released explicitly (llama-cpp-python's Llama.close()). FAKE model objects that count loads and closes.
import test_p20_setup as P20
from unittest import mock
from jarvis.app import setup_engine as SE
from jarvis.eval import winvalidate
from jarvis.inference.gguf_engine import GGUFEngine, InferenceError


class CountingLlama(P20.FakeLlama):
    """FAKE MODEL that records how many copies are alive. Not real inference."""
    alive = 0
    peak = 0
    closes = 0

    def __init__(self, **kw):
        super().__init__(**kw)
        type(self).alive += 1
        type(self).peak = max(type(self).peak, type(self).alive)
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            type(self).alive -= 1
            type(self).closes += 1


class ModelMemoryRelease(P20.Base):
    def setUp(self):
        super().setUp()
        CountingLlama.alive = CountingLlama.peak = CountingLlama.closes = 0
        p = mock.patch.object(winvalidate, "Sampler", P20.FakeSampler)
        p.start()
        self.addCleanup(p.stop)
        self.install()

    def test_close_frees_the_model_is_idempotent_and_generation_afterwards_is_a_clear_error(self):
        eng = GGUFEngine(self.store.path(P20.MID), llama_factory=CountingLlama)
        self.assertEqual(CountingLlama.alive, 1)
        eng.close()
        eng.close()
        self.assertEqual((CountingLlama.alive, CountingLlama.closes), (0, 1))
        with self.assertRaises(InferenceError) as cm:
            eng.chat([{"role": "user", "content": "hi"}])
        self.assertIn("shut down", str(cm.exception))
        with self.assertRaises(InferenceError):
            list(eng.stream([{"role": "user", "content": "hi"}]))

    def test_an_engine_whose_model_object_cannot_close_or_raises_on_close_still_shuts_down_quietly(self):
        class NoClose(P20.FakeLlama):
            pass

        class BadClose(P20.FakeLlama):
            def close(self):
                raise RuntimeError("boom")
        for factory in (NoClose, BadClose):
            eng = GGUFEngine(self.store.path(P20.MID), llama_factory=factory)
            eng.close()
            self.assertIsNone(eng.llm)

    def test_the_assistant_closes_an_engine_it_created_but_never_one_that_was_handed_to_it(self):
        from jarvis.assistant import LocalAssistant
        owned = LocalAssistant(self.cfg, engine_factory=lambda path: GGUFEngine(path, llama_factory=CountingLlama), guest=True)
        self.assertEqual(CountingLlama.alive, 1)
        owned.close()
        self.assertEqual((CountingLlama.alive, CountingLlama.closes), (0, 1))
        shared = GGUFEngine(self.store.path(P20.MID), llama_factory=CountingLlama)          # the benchmarks share ONE engine across many assistants
        for _ in range(3):
            LocalAssistant(self.cfg, engine=shared, guest=True).close()
        self.assertEqual(CountingLlama.alive, 1)                                             # still loaded: the caller owns it
        self.assertEqual(shared.chat([{"role": "user", "content": "hi"}]), "ready")           # and still usable
        shared.close()
        self.assertEqual(CountingLlama.alive, 0)

    def test_setup_verification_never_holds_more_than_one_copy_of_the_model_in_memory(self):
        eng = SE.SetupEngine(self.cfg, self.store, self.policy, llama_factory=CountingLlama, hardware=P20.RTX,
                             runtime={"available": True, "gpu_offload_supported": True}, sleep=lambda s: None)
        v = eng.verify(P20.MID)
        self.assertTrue(v["ok"])
        self.assertTrue(v["gpu"]["tried"])                      # the CPU, the GPU and the app's own engine were all loaded in turn
        self.assertGreaterEqual(CountingLlama.closes, 3)
        self.assertEqual(CountingLlama.peak, 1)
        self.assertEqual(CountingLlama.alive, 0)

    def test_a_generation_that_fails_during_verification_leaves_no_model_loaded(self):
        class Exploding(CountingLlama):
            def create_chat_completion(self, *a, **k):
                raise RuntimeError("generation failed")
        eng = SE.SetupEngine(self.cfg, self.store, self.policy, llama_factory=Exploding, hardware=P20.CPU_ONLY,
                             runtime={"available": True, "gpu_offload_supported": False}, sleep=lambda s: None)
        v = eng.verify(P20.MID)
        self.assertFalse(v["ok"])
        self.assertEqual(Exploding.alive, 0)
