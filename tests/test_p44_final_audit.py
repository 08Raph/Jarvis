"""Stage 5 final audit: regression tests for the defects found and fixed in the audit.

LABELS: SYNTHETIC GGUF files (real header layout, no weights); FakeEngine/FakeHost (NOT inference); SIMULATED hardware; MOCKED store/host failures.
Nothing here loads a real model, runs the real llama.cpp runtime, or uses PySide6/Windows."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import p41_common as C                                      # noqa: E402
import manifest                                              # noqa: E402
from jarvis.app import diagnostics                           # noqa: E402
from jarvis.brains import compat, viewmodel                  # noqa: E402
from jarvis.models import gguf                               # noqa: E402
from jarvis.models.gguf_vocab import KNOWN_PRE_TOKENIZERS, KNOWN_TOKENIZER_MODELS  # noqa: E402

GB = 10 ** 9


class _Assess(C.BrainCase):
    def assess(self, **kw):
        p = self.gguf("t.gguf", **kw)
        g = gguf.summarize(gguf.read_header(p))
        return compat.assess(g, p.stat().st_size, C.hw(), {"available": True, "gpu_offload_supported": False})

    def blocked(self, r):
        return [x["text"] for x in r["reasons"] if x["severity"] == "block"]


class Classification(_Assess):
    """Header-level rules, taken from the runtime source at the pinned commit (see docs/STAGE5_MODEL_SOURCE_VERIFICATION.md section 29)."""

    def test_plain_qwen3_file_is_expected_but_never_called_verified(self):
        r = self.assess()
        self.assertEqual(r["level"], "compatible")
        self.assertEqual(r["support"]["tier"], "expected")
        self.assertFalse(r["support"]["verified_on_this_pc"])
        self.assertIn("NOT verified", r["support"]["text"])

    def test_vocabulary_only_file_with_zero_tensors_is_unsupported(self):
        r = self.assess(tensors=0)
        self.assertEqual(r["level"], "unsupported")
        self.assertTrue(any("no model weights" in t for t in self.blocked(r)))

    def test_adapter_vision_projector_and_calibration_files_are_unsupported(self):
        for kind in ("adapter", "mmproj", "imatrix"):
            with self.subTest(kind=kind):
                r = self.assess(general_type=kind)
                self.assertEqual(r["level"], "unsupported")
                self.assertTrue(any(kind in t for t in self.blocked(r)))
        self.assertEqual(self.assess(general_type="model")["level"], "compatible")

    def test_one_part_of_a_split_model_is_unsupported(self):
        r = self.assess(split_count=3)
        self.assertEqual(r["level"], "unsupported")
        self.assertTrue(any("3 files" in t for t in self.blocked(r)))
        self.assertEqual(self.assess(split_count=1)["level"], "compatible")

    def test_unknown_or_missing_vocabulary_tokenizer_is_unsupported(self):
        for tok in ("totally-new-tokenizer", "none", "no_vocab"):
            with self.subTest(tok=tok):
                self.assertEqual(self.assess(tok=tok)["level"], "unsupported")

    def test_unknown_non_empty_bpe_pre_tokenizer_is_unsupported_but_missing_one_only_warns(self):
        r = self.assess(pre="pre-tokenizer-from-the-future")
        self.assertEqual(r["level"], "unsupported")
        self.assertTrue(any("pre-tokenizer" in t for t in self.blocked(r)))
        r = self.assess(pre=None)
        self.assertEqual(r["level"], "warnings")
        self.assertTrue(any("quality" in x["text"] and x["severity"] == "warn" for x in r["reasons"]))

    def test_file_that_states_no_tokenizer_at_all_only_warns(self):
        r = self.assess(tok=None, pre=None)
        self.assertEqual(r["level"], "warnings")

    def test_tokenizer_tables_contain_the_names_real_models_use(self):
        for t in ("gpt2", "llama", "t5", "rwkv"):
            self.assertIn(t, KNOWN_TOKENIZER_MODELS)
        for pre in ("qwen2", "llama-bpe", "deepseek-llm", "deepseek-coder", "deepseek-v3", "deepseek-r1-qwen"):
            self.assertIn(pre, KNOWN_PRE_TOKENIZERS)


class DeepSeek(_Assess):
    """DeepSeek findings come from the runtime source at the pinned commit, NOT from running any DeepSeek model."""

    def test_deepseek2_without_kv_lora_rank_is_unsupported(self):
        r = self.assess(arch="deepseek2", pre="deepseek-v3")
        self.assertEqual(r["level"], "unsupported")
        self.assertTrue(any("kv_lora_rank" in t for t in self.blocked(r)))

    def test_deepseek2_with_its_fields_is_not_called_verified_and_explains_size_and_cache(self):
        r = self.assess(arch="deepseek2", pre="deepseek-v3", kv_lora_rank=512, file_type=None)
        self.assertNotEqual(r["level"], "unsupported")
        self.assertFalse(r["support"]["verified_on_this_pc"])
        text = " ".join(x["text"] for x in r["reasons"])
        self.assertIn("multi-head latent attention", text)
        self.assertIn("hundreds of GB", text)
        self.assertEqual(r["estimate"]["kv_basis"], "rough")        # the standard KV formula would be wrong for this attention type

    def test_ordinary_architecture_keeps_the_header_based_cache_figure(self):
        self.assertEqual(self.assess()["estimate"]["kv_basis"], "header")

    def test_deepseek_v32_and_v4_are_warnings_not_support(self):
        for arch in ("deepseek32", "deepseek4"):
            with self.subTest(arch=arch):
                r = self.assess(arch=arch, pre="deepseek-v3")
                self.assertEqual(r["level"], "warnings")
                self.assertTrue(any(arch == "deepseek32" and "V3.2" in x["text"] or arch == "deepseek4" and "V4" in x["text"] for x in r["reasons"]))

    def test_deepseek_ocr_is_a_non_chat_model(self):
        self.assertEqual(self.assess(arch="deepseek2-ocr", pre="deepseek-v3")["level"], "unsupported")

    def test_r1_distill_is_recognised_as_a_qwen_model_with_a_note(self):
        r = self.assess(arch="qwen2", pre="deepseek-r1-qwen")
        self.assertEqual(r["level"], "compatible")
        self.assertTrue(any("R1 distilled" in x["text"] for x in r["reasons"]))

    def test_catalog_checks_do_not_demand_header_fields_the_catalog_does_not_have(self):
        rows = self.mgr.catalog()
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("support", row["compatibility"])
            self.assertFalse(row["compatibility"]["support"]["verified_on_this_pc"], row["id"])


class Switching(C.BrainCase):
    def two(self):
        a, b = self.imp("a.gguf", name="A"), self.imp("b.gguf", name="B")
        self.assertTrue(self.mgr.activate(a)["ok"])
        return a, b

    def test_failure_to_select_the_new_model_keeps_the_running_previous_brain(self):
        a, b = self.two()
        before = self.host.engine
        with mock.patch.object(self.store, "set_active", side_effect=RuntimeError("disk error")):
            r = self.mgr.activate(b)
        self.assertFalse(r["ok"])
        self.assertIs(self.host.engine, before)
        self.assertEqual(self.host.model_id, a)
        self.assertFalse(before.closed)
        self.assertEqual(self.store.active(), a)
        self.assertEqual(C.FakeEngine.loaded, 1, "the candidate must be released")
        self.assertNotIn(f"install:{b}", self.host.events)

    def test_failure_to_hand_the_engine_to_the_assistant_restores_pointer_and_previous_brain(self):
        a, b = self.two()
        real = self.host.install_engine

        def flaky(engine, model_id):
            if model_id == b:
                raise RuntimeError("host refused")
            real(engine, model_id)
        with mock.patch.object(self.host, "install_engine", side_effect=flaky):
            r = self.mgr.activate(b)
        self.assertFalse(r["ok"])
        self.assertEqual(self.store.active(), a)
        self.assertEqual(self.host.model_id, a)
        self.assertTrue(r["previous_restored"])
        self.assertEqual(C.FakeEngine.loaded, 1)

    def test_selection_file_is_replaced_atomically_and_a_damaged_one_means_no_brain(self):
        a = self.imp()
        self.store.set_active(a)
        self.assertFalse((self.store.dir / "active.json.tmp").exists())
        (self.store.dir / "active.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.store.active())
        (self.store.dir / "active.json").write_text(json.dumps({"id": "../x"}), encoding="utf-8")
        self.assertIsNone(self.store.active())
        (self.store.dir / "active.json").write_text(json.dumps([1]), encoding="utf-8")
        self.assertIsNone(self.store.active())


class ImportAndRemove(C.BrainCase):
    def test_file_that_changes_while_being_checked_is_not_added(self):
        p = self.gguf("grow.gguf")

        def hash_and_change(path, cancel=None, progress=None):
            with open(path, "ab") as f:
                f.write(b"more")
            return "0" * 64
        with mock.patch("jarvis.brains.service.hash_file", side_effect=hash_and_change):
            r = self.mgr.import_file(p)
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "changed")
        self.assertEqual(self.store.installed(), [])

    def test_disk_full_during_copy_import_is_reported_and_adds_nothing(self):
        p = self.gguf("big.gguf")
        with mock.patch.object(self.store, "install_gguf", side_effect=OSError(28, "No space left on device")):
            r = self.mgr.import_file(p, copy=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "io_error")
        self.assertIn("Nothing was added", r["message"])
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.store.staging_leftovers(), [])

    def test_removal_that_leaves_files_behind_is_not_reported_as_success(self):
        mid = self.imp()
        self.mgr.test_brain(mid)
        with mock.patch.object(self.store, "remove", return_value=True):          # the real one swallows rmtree errors
            r = self.mgr.remove(mid, confirmed=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "not_removed")
        self.assertIn(mid, [m["id"] for m in self.store.installed()])
        self.assertIn(mid, self.mgr.load_state().get("last_tests", {}), "the test record must survive a failed removal")

    def test_registration_removal_keeps_the_users_file_and_file_deletion_is_separate(self):
        p = self.gguf("mine.gguf")
        mid = self.mgr.import_file(p)["model_id"]
        self.assertIn("stays where it is", self.mgr.remove_preview(mid)["library_removal"])
        self.assertTrue(self.mgr.remove(mid, confirmed=True)["ok"])
        self.assertTrue(p.exists())


class Privacy(C.BrainCase):
    def test_error_text_is_scrubbed_of_home_folder_and_copied_prompt_text(self):
        secret_prompt = "SECRET-PROMPT-" + "x" * 120
        home = str(Path.home())

        def boom(model_dir):
            raise RuntimeError(f"cannot open {home}/models/a.gguf: text=\"{secret_prompt}\"")
        mgr = self.make(C.hw(), factory=boom)
        mid = self.imp()
        r = mgr.test_brain(mid)
        self.assertFalse(r["ok"])
        self.assertNotIn(home + "/", r["message"])
        self.assertNotIn(secret_prompt, r["message"])
        saved = json.dumps(mgr.load_state())
        self.assertNotIn(secret_prompt, saved)
        self.assertNotIn(home + "/", saved)


class Evidence(C.BrainCase):
    def test_a_test_with_an_injected_engine_is_recorded_as_not_real_and_never_makes_a_brain_verified(self):
        mid = self.imp()
        self.assertTrue(self.mgr.test_brain(mid)["ok"])
        rec = self.mgr.load_state()["last_tests"][mid]
        self.assertFalse(rec["real"])
        b = next(x for x in self.mgr.installed_brains() if x["id"] == mid)
        self.assertNotEqual(b["support"]["tier"], "verified")
        self.assertFalse(b["support"]["verified_on_this_pc"])

    def test_only_a_successful_test_from_the_real_runtime_counts_as_verified_on_this_pc(self):
        mid = self.imp()
        st = self.mgr.load_state()
        st["last_tests"] = {mid: {"ok": True, "real": True, "at": "2026-10-27T00:00:00+00:00"}}
        self.mgr._save_state(st)                                           # SIMULATED record: no real runtime ran here
        b = next(x for x in self.mgr.installed_brains() if x["id"] == mid)
        self.assertEqual(b["support"]["tier"], "verified")
        self.assertIn("Support: Verified on this PC", viewmodel.installed_detail(b))
        st["last_tests"][mid]["ok"] = False
        self.mgr._save_state(st)
        b = next(x for x in self.mgr.installed_brains() if x["id"] == mid)
        self.assertNotEqual(b["support"]["tier"], "verified")

    def test_catalog_detail_shows_the_support_line(self):
        row = self.mgr.catalog()[0]
        self.assertIn("NOT verified", viewmodel.catalog_detail(row) if hasattr(viewmodel, "catalog_detail") else row["compatibility"]["support"]["text"])


class Packaging(unittest.TestCase):
    def test_brain_modules_are_in_both_module_lists_and_import_without_qt(self):
        import importlib
        want = {"jarvis.brains.service", "jarvis.brains.viewmodel", "jarvis.brains.compat", "jarvis.brains.archs", "jarvis.models.gguf_vocab"}
        self.assertTrue(want <= set(manifest.HIDDEN_REQUIRED))
        self.assertEqual(set(diagnostics.REQUIRED_MODULES), set(manifest.HIDDEN_REQUIRED))
        for m in want:
            importlib.import_module(m)


if __name__ == "__main__":
    unittest.main()
