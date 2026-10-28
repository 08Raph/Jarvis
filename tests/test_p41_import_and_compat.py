"""Import Brain: importing local GGUF files, rejecting what cannot be a brain, registration persistence, and the hardware compatibility classes.

LABELS: SYNTHETIC GGUF files (real header layout, no weights; sizes are sparse padding). SIMULATED hardware (chosen numbers: proves the classification
logic, not any real PC). FakeEngine is not inference. Nothing here loads a real model."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p41_common as C                                    # noqa: E402
from jarvis.brains import compat                          # noqa: E402
from jarvis.brains.service import BrainManager, hash_file, Cancelled  # noqa: E402
from jarvis.models import gguf as gg                      # noqa: E402
from jarvis.models.manager import ModelStore               # noqa: E402

GB = 1_000_000_000


class LocalImport(C.BrainCase):
    def test_register_in_place_does_not_copy_and_keeps_the_original(self):
        f = self.gguf("m.gguf")
        r = self.mgr.import_file(f)
        self.assertTrue(r["ok"] and r["linked"] and not r["copied"])
        mid = r["model_id"]
        self.assertTrue(f.is_file())                                                   # the user's file is untouched
        self.assertFalse((self.store.path(mid) / "model.gguf").exists())               # nothing was copied
        self.assertLess(sum(p.stat().st_size for p in self.store.dir.rglob("*") if p.is_file()), 20_000)   # only a small record
        self.assertEqual(self.store.gguf_file(mid), f.resolve())
        self.assertEqual(r["sha256"], hash_file(f))

    def test_copy_mode_copies_and_does_not_claim_verification(self):
        f = self.gguf("m.gguf", size=3_000_000)
        r = self.mgr.import_file(f, copy=True)
        self.assertTrue(r["ok"] and r["copied"])
        self.assertTrue((self.store.path(r["model_id"]) / "model.gguf").is_file())
        self.assertFalse(self.store.meta(r["model_id"])["verified_origin"])           # a local file has no trusted reference

    def test_registration_persists_across_restart(self):
        mid = self.imp()
        store2 = ModelStore(self.cfg.models_dir, self.policy)                          # a new process would build these again
        mgr2 = BrainManager(self.cfg, store2, self.policy, host=C.FakeHost(), engine_factory=C.FakeEngine, hardware=C.hw(), runtime={"available": True})
        ids = [b["id"] for b in mgr2.installed_brains()]
        self.assertEqual(ids, [mid])
        self.assertTrue(mgr2.installed_brains()[0]["linked"])

    def test_the_engine_path_follows_the_registered_file(self):
        mid = self.imp()
        from jarvis.inference.gguf_engine import GGUFEngine

        class L:
            def __init__(self, **kw):
                self.path = kw["model_path"]
        eng = GGUFEngine(self.store.path(mid), llama_factory=L)
        self.assertEqual(Path(eng.llm.path), self.files / "a.gguf")

    def test_a_moved_or_changed_file_is_noticed_not_loaded(self):
        mid = self.imp()
        f = self.files / "a.gguf"
        with open(f, "ab") as fh:
            fh.write(b"x")                                                              # size changed
        b = self.mgr.installed_brains()[0]
        self.assertEqual(b["status"], "failed")
        self.assertIn("size changed", " ".join(b["integrity_problems"]))
        r = self.mgr.activate(mid)
        self.assertEqual((r["ok"], r["kind"]), (False, "integrity"))
        self.assertEqual(C.FakeEngine.loaded, 0)
        f.unlink()
        self.assertIn("missing or was moved", " ".join(self.mgr.installed_brains()[0]["integrity_problems"]))

    def test_unique_ids_for_the_same_name(self):
        a, b = self.imp("a.gguf"), self.imp("b.gguf")
        self.assertNotEqual(a, b)

    def test_cancel_during_the_checksum_leaves_nothing_behind(self):
        f = self.gguf("big.gguf")

        class Flag:
            n = 0

            def is_set(self):
                Flag.n += 1
                return Flag.n > 1
        r = self.mgr.import_file(f, cancel=Flag())
        self.assertEqual((r["ok"], r["kind"]), (False, "cancelled"))
        self.assertEqual(self.store.installed(), [])
        self.assertEqual([p.name for p in self.store.dir.iterdir()], [])               # no folder, no staging leftover
        self.assertTrue(f.is_file())

    def test_hash_file_honours_cancel(self):
        f = self.gguf("h.gguf")
        with self.assertRaises(Cancelled):
            hash_file(f, cancel=type("F", (), {"is_set": lambda s: True})())

    def test_nothing_in_the_file_is_executed(self):
        sentinel = self.d / "SENTINEL"
        evil = f"{{{{ __import__('os').system('touch {sentinel}') }}}} $(touch {sentinel}) `touch {sentinel}`"
        r = self.mgr.import_file(self.gguf("evil.gguf", chat_template=evil, name=f"x; touch {sentinel}"))
        self.assertTrue(r["ok"])
        self.assertFalse(sentinel.exists())
        self.assertNotIn("touch", str(r["gguf"].get("chat_template", "")))              # the template text is never returned, only its length
        self.assertTrue(r["gguf"]["has_chat_template"])
        self.assertTrue(r["model_id"].startswith("imp-") and ";" not in r["model_id"] and " " not in r["model_id"])

    def test_ids_cannot_escape_the_model_folder(self):
        for bad in ("../x", "a/b", "..", "A", "x" * 80, ""):
            with self.assertRaises(ValueError):
                self.store.register_external(self.gguf("p.gguf"), bad)
        self.assertEqual(os.listdir(self.store.dir), [])


class Rejections(C.BrainCase):
    def reject(self, path, kind):
        r = self.mgr.import_file(path)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["kind"], kind, r)
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(C.FakeEngine.created, [])
        return r

    def test_not_a_gguf(self):
        p = self.files / "notes.gguf"
        p.write_text("this is a text file pretending to be a model" * 100)
        self.assertIn("GGUF", self.reject(p, "not_gguf")["message"])

    def test_png_with_a_gguf_name(self):
        p = self.files / "x.gguf"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 5000)
        self.reject(p, "not_gguf")

    def test_truncated_header(self):
        src = self.gguf("t.gguf")
        p = self.files / "cut.gguf"
        p.write_bytes(src.read_bytes()[:40])
        self.reject(p, "not_gguf")

    def test_unsupported_gguf_version(self):
        self.reject(self.gguf("v.gguf", version=1), "not_gguf")

    def test_missing_directory_and_empty(self):
        self.reject(self.files / "nope.gguf", "not_found")
        self.reject(self.files, "not_a_file")
        e = self.files / "e.gguf"
        e.write_bytes(b"")
        self.reject(e, "empty")

    def test_valid_gguf_that_is_not_a_chat_model_is_refused_with_the_reason(self):
        for arch, word in (("bert", "embedding"), ("clip", "image"), ("wavtokenizer-dec", "speech")):
            r = self.reject(self.gguf(f"{arch}.gguf", arch=arch), "unsupported")
            self.assertIn("not a chat model", r["message"])
            self.assertIn(arch, r["message"])

    def test_architecture_the_runtime_does_not_know_is_refused_and_says_why(self):
        r = self.reject(self.gguf("z.gguf", arch="fancynet9"), "unsupported")
        self.assertIn("fancynet9", r["message"])
        self.assertIn("knows how to load", r["message"])

    def test_a_valid_header_alone_does_not_make_a_model_compatible(self):
        info = self.mgr.inspect_file(self.gguf("w.gguf", arch="fancynet9"))
        self.assertTrue(info["ok"])
        self.assertEqual(info["compat"]["level"], compat.UNSUPPORTED)
        self.assertFalse(info["compat"]["can_activate"])

    def test_model_too_big_for_this_pc_is_refused(self):
        small = self.make(C.hw(ram=4.0, avail=3.0))
        r = small.import_file(self.gguf("huge.gguf", size=9 * GB))
        self.assertEqual((r["ok"], r["kind"]), (False, "unsupported"))
        self.assertIn("4.0 GB in total", r["message"])

    def test_runtime_missing_blocks_with_a_clear_reason(self):
        m = self.make(C.hw(), runtime={"available": False, "error": "ImportError: no llama_cpp"})
        r = m.import_file(self.gguf("r.gguf"))
        self.assertEqual(r["kind"], "unsupported")
        self.assertIn("llama-cpp-python", r["message"])
        self.assertIn("no llama_cpp", r["message"])


class Hardware(unittest.TestCase):
    """SIMULATED hardware only."""
    G = {"architecture": "qwen3", "block_count": 36, "embedding_length": 4096, "head_count": 32, "head_count_kv": 8, "key_length": 128,
         "context_length": 40960, "quantization": "Q4_K_M", "has_chat_template": True, "tensor_count": 399,
         "tokenizer_model": "gpt2", "tokenizer_pre": "qwen2"}

    def a(self, hw, size=5 * GB, g=None, rt=None, **kw):
        return compat.assess(g or self.G, size, hw, rt if rt is not None else {"available": True, "gpu_offload_supported": False}, **kw)

    def test_comfortable_pc_is_compatible(self):
        r = self.a(C.hw(32, 24))
        self.assertEqual((r["level"], r["label"]), ("compatible", "Compatible"))
        self.assertTrue(r["can_activate"])

    def test_kv_cache_estimate_uses_the_header_shape(self):
        e = self.a(C.hw(32, 24), n_ctx=4096)["estimate"]
        self.assertEqual(e["kv_cache_gb"], round(2 * 36 * 4096 * 8 * 128 * 2 / 1e9, 2))      # 0.6 GB
        self.assertEqual((e["kv_basis"], e["kind"]), ("header", "ESTIMATE"))
        self.assertAlmostEqual(e["total_gb"], 5.0 + 0.6 + 1.0, places=1)
        bigger = self.a(C.hw(32, 24), n_ctx=8192)["estimate"]["kv_cache_gb"]
        self.assertAlmostEqual(bigger, 2 * e["kv_cache_gb"], places=1)

    def test_missing_shape_fields_are_flagged_as_a_rough_guess(self):
        r = self.a(C.hw(32, 24), g={"architecture": "qwen3", "quantization": "Q4_K_M", "has_chat_template": True})
        self.assertEqual(r["estimate"]["kv_basis"], "rough")
        self.assertEqual(r["level"], "warnings")
        self.assertTrue(any("rough guess" in x["text"] for x in r["reasons"]))

    def test_tight_pc_gets_warnings_with_reasons(self):
        r = self.a(C.hw(8, 7.0), size=5.6 * GB)                                              # needs ~7.2 GB of 8
        self.assertEqual(r["level"], "warnings")
        self.assertTrue(any("very little" in x["text"] for x in r["reasons"]))

    def test_little_free_memory_right_now_warns(self):
        r = self.a(C.hw(32, 4.0))
        self.assertEqual(r["level"], "warnings")
        self.assertTrue(any("free right now" in x["text"] for x in r["reasons"]))

    def test_more_than_total_ram_is_unsupported(self):
        r = self.a(C.hw(4, 3.0))
        self.assertEqual(r["level"], "unsupported")
        self.assertFalse(r["can_activate"])
        self.assertEqual(r["mode_estimate"], "none")
        self.assertIn("4.0 GB in total", " ".join(x["text"] for x in r["reasons"]))

    def test_unknown_ram_is_a_warning_not_a_pass(self):
        r = self.a(C.hw(None, None))
        self.assertEqual(r["level"], "warnings")

    def test_no_avx2_warns(self):
        self.assertTrue(any("AVX2" in x["text"] for x in self.a(C.hw(32, 24, avx2=False))["reasons"]))

    def test_gpu_detected_without_backend_is_only_a_note(self):
        r = self.a(C.hw(32, 24, gpu="Sim GPU", vram=24.0))
        self.assertEqual(r["mode_estimate"], "cpu")
        self.assertTrue(any("no GPU support" in x["text"] for x in r["reasons"]))

    def test_gpu_with_backend_and_enough_vram_estimates_gpu_and_unverified(self):
        r = self.a(C.hw(32, 24, gpu="Sim GPU", vram=24.0), rt={"available": True, "gpu_offload_supported": True})
        self.assertEqual(r["mode_estimate"], "gpu")
        self.assertTrue(any("not verified" in x["text"] for x in r["reasons"]))

    def test_gpu_too_small_falls_back_to_cpu_estimate(self):
        r = self.a(C.hw(32, 24, gpu="Sim GPU", vram=4.0), rt={"available": True, "gpu_offload_supported": True})
        self.assertEqual(r["mode_estimate"], "cpu")

    def test_no_chat_template_warns(self):
        g = dict(self.G, has_chat_template=False)
        self.assertEqual(self.a(C.hw(32, 24), g=g)["level"], "warnings")

    def test_moe_is_explained(self):
        g = dict(self.G, architecture="qwen3moe", expert_count=128)
        self.assertTrue(any("mixture-of-experts" in x["text"] for x in self.a(C.hw(32, 24), g=g)["reasons"]))

    def test_context_is_limited_to_the_models_own_maximum(self):
        r = self.a(C.hw(32, 24), g=dict(self.G, context_length=2048), n_ctx=8192)
        self.assertEqual(r["estimate"]["n_ctx"], 2048)

    def test_architecture_mismatch_with_the_entry_is_unsupported(self):
        r = self.a(C.hw(32, 24), architecture_expected="llama")
        self.assertEqual(r["level"], "unsupported")
        self.assertIn("not the model it claims", " ".join(x["text"] for x in r["reasons"]))

    def test_the_lists_are_consistent_with_the_runtimes_own_table(self):
        from jarvis.brains.archs import RUNTIME_KNOWN_ARCHITECTURES as K
        self.assertEqual(len(K), 153)
        self.assertFalse(compat.SUPPORTED_CHAT_ARCHITECTURES & set(compat.NON_CHAT_ARCHITECTURES))
        self.assertEqual(sorted(compat.SUPPORTED_CHAT_ARCHITECTURES - K), [])        # nothing is claimed that the runtime has no loader for
        self.assertEqual(sorted(set(compat.NON_CHAT_ARCHITECTURES) - K), [])
        self.assertIn("qwen3", compat.SUPPORTED_CHAT_ARCHITECTURES)

    def test_known_to_the_runtime_but_unclassified_is_a_warning_not_a_pass(self):
        r = self.a(C.hw(32, 24), g=dict(self.G, architecture="maple"))
        self.assertEqual(r["level"], "warnings")
        self.assertTrue(any("has not classified it as a chat model" in x["text"] for x in r["reasons"]))

    def test_non_chat_reason_is_specific(self):
        r = self.a(C.hw(32, 24), g=dict(self.G, architecture="qwen3vl"))
        self.assertEqual(r["level"], "unsupported")
        self.assertIn("no image input", " ".join(x["text"] for x in r["reasons"]))

    def test_estimates_are_labelled_as_not_measurements(self):
        self.assertTrue(self.a(C.hw(32, 24))["estimates_are_not_measurements"])


class HeaderFields(unittest.TestCase):
    def test_summary_reads_shape_and_quantization_from_a_synthetic_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            p = C.build_gguf(Path(t) / "s.gguf", arch="llama", layers=32, embd=4096, heads=32, kv_heads=8, key_len=128, ctx=8192, file_type=7, size=0)
            s = gg.summarize(gg.read_header(p))
        self.assertEqual((s["architecture"], s["block_count"], s["head_count_kv"], s["quantization"], s["context_length"]), ("llama", 32, 8, "Q8_0", 8192))
        self.assertNotIn("chat_template", s)


if __name__ == "__main__":
    unittest.main()
