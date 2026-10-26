"""Prompt 19 tests. Real: memory retrieval measurements, selector logic, document reading with generated real files.
Simulated (labelled): anything that uses a fake llama factory - it proves plumbing, NOT model quality or GPU speed."""
import hashlib
import io
import json
import tempfile
import unittest
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis.dev import research
from jarvis.eval import memory_eval, realmodel, suite19
from jarvis.hardware import Hardware
from jarvis.inference import gguf_engine as ge
from jarvis.memory import retrieval
from jarvis.models import catalog, selector


class FakeKw:
    """FAKE MODEL that records the keyword arguments it was created with."""
    last = None

    def __init__(self, **kw):
        FakeKw.last = kw
        self.reply = "ready"

    def tokenize(self, b, add_bos=False):
        return list(range(len(b.split())))

    def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
        txt = "TOOL: get_time" if "TOOL:" in messages[0]["content"] else self.reply
        return {"choices": [{"message": {"content": txt}}]}


def model_dir(tmp, magic=b"GGUF", meta=None):
    d = Path(tmp)
    (d / "model.gguf").write_bytes(magic + b"\x00" * 64)
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


class MemoryRetrievalEval(unittest.TestCase):
    def test_dataset_is_fixed_and_split_evenly(self):
        self.assertEqual(len(memory_eval.QUERIES), 48)
        dev = [q for q in memory_eval.QUERIES if q["split"] == "dev"]
        self.assertEqual(len(dev), 24)
        self.assertEqual({q["tag"] for q in memory_eval.QUERIES}, {"lex", "morph", "typo", "semantic", "conflict", "hardneg", "none"})

    def test_deterministic(self):
        a, b = memory_eval.run(), memory_eval.run()
        self.assertEqual(a["rows"], b["rows"])

    def test_floor_on_held_out_test_split(self):
        r = memory_eval.run("test")           # measured on the real code, no model involved
        self.assertGreaterEqual(r["hit@3"], 0.9)
        self.assertGreaterEqual(r["hit@1"], 0.75)
        self.assertEqual(r["superseded_returned"], 0)

    def test_corrections_beat_old_values(self):
        r = memory_eval.run()
        for row in r["rows"]:
            if row["tag"] == "conflict":
                self.assertTrue(row["hit1"], row)

    def test_known_weakness_is_recorded_not_hidden(self):
        # Unanswerable questions that share words with a memory still return something: lexical retrieval cannot abstain well.
        r = memory_eval.run()
        self.assertGreater(r["false_positive_rate"], 0.0)

    def test_stricter_threshold_trades_recall_for_fewer_false_positives(self):
        loose, strict = memory_eval.run(min_score=0.25), memory_eval.run(min_score=0.55)
        self.assertLessEqual(strict["false_positive_rate"], loose["false_positive_rate"])
        self.assertLess(strict["hit@1"], loose["hit@1"])

    def test_retrieval_primitives(self):
        self.assertEqual(retrieval.stem("studies"), retrieval.stem("study"))
        self.assertEqual(retrieval.stem("designer"), retrieval.stem("designs"))
        s = retrieval.lexical_scores("pasport renewal", ["Passport renewal is due", "I like tea"])
        self.assertGreater(s[0], 0.5)
        self.assertEqual(s[1], 0.0)
        self.assertEqual(retrieval.lexical_scores("", ["x"]), [0.0])
        self.assertEqual(retrieval.lexical_scores("zzzzqqq", ["hello world"]), [0.0])


class EnginePlumbing(unittest.TestCase):
    def test_gpu_layers_only_passed_when_requested(self):
        with tempfile.TemporaryDirectory() as t:
            d = model_dir(t)
            ge.GGUFEngine(d, llama_factory=FakeKw)
            self.assertNotIn("n_gpu_layers", FakeKw.last)
            e = ge.GGUFEngine(d, llama_factory=FakeKw, n_gpu_layers=-1)
            self.assertEqual(FakeKw.last["n_gpu_layers"], -1)
            self.assertEqual(e.gpu_layers_requested, -1)

    def test_load_failure_is_reported_not_swallowed(self):
        def boom(**kw):
            raise MemoryError("out of memory")
        with tempfile.TemporaryDirectory() as t, self.assertRaises(ge.RuntimeUnavailable) as c:
            ge.GGUFEngine(model_dir(t), llama_factory=boom)
        self.assertIn("MemoryError", str(c.exception))

    def test_runtime_info_without_runtime_is_honest(self):
        info = ge.runtime_info()
        if not info["available"]:
            self.assertIn("error", info)
            self.assertNotIn("gpu_offload_supported", info)


class Selector(unittest.TestCase):
    CPU_ONLY = Hardware("Windows 11", 8, 8.0, 100.0)
    RTX = Hardware("Windows 11", 16, 32.0, 300.0, "NVIDIA GeForce RTX 4070 SUPER", 12.0)

    def test_three_acceleration_states_are_separate(self):
        a = selector.acceleration_state(self.RTX, {"gpu_offload_supported": False})
        self.assertTrue(a["gpu_detected"]); self.assertFalse(a["backend_available"]); self.assertFalse(a["acceleration_verified"])
        b = selector.acceleration_state(self.RTX, {"gpu_offload_supported": True})
        self.assertTrue(b["backend_available"]); self.assertFalse(b["acceleration_verified"])
        self.assertIn("NOT verified", b["message"])
        self.assertIn("no GPU backend", a["message"])
        n = selector.acceleration_state(self.CPU_ONLY, {})
        self.assertFalse(n["gpu_detected"]); self.assertIn("No supported GPU", n["message"])
        c = selector.acceleration_state(self.RTX, {"gpu_offload_supported": True}, 5.0, 30.0)
        self.assertTrue(c["acceleration_verified"])
        d = selector.acceleration_state(self.RTX, {"gpu_offload_supported": True}, 5.0, 5.5)
        self.assertFalse(d["acceleration_verified"])

    def test_gpu_without_backend_is_not_counted_in_tier(self):
        self.assertEqual(selector.hardware_tier(32, 12, False), "performance")      # by RAM
        self.assertEqual(selector.hardware_tier(8, 12, False), "entry")             # VRAM ignored without a backend
        self.assertEqual(selector.hardware_tier(8, 12, True), "performance")
        self.assertEqual(selector.hardware_tier(64, 24, True), "high-performance")
        self.assertEqual(selector.hardware_tier(None, None, False), "entry")

    def test_small_machine_gets_small_model(self):
        c = selector.select(self.CPU_ONLY, {})
        self.assertTrue(c.fits); self.assertEqual(c.model.id, "qwen3-1.7b-q4_k_m"); self.assertEqual(c.mode, "cpu")

    def test_pinned_model_that_does_not_fit_is_never_replaced(self):
        c = selector.select(self.CPU_ONLY, {}, pinned="qwen3-8b-q4_k_m")
        self.assertFalse(c.fits)
        self.assertEqual(c.model.id, "qwen3-8b-q4_k_m")
        self.assertIn("NOT replaced", c.reasons[0])
        self.assertIn("qwen3-1.7b-q4_k_m", c.alternatives)

    def test_unconfirmed_models_are_never_auto_recommended(self):
        c = selector.select(self.RTX, {"gpu_offload_supported": True}, prefer="quality")
        self.assertEqual(c.model.id, "qwen3-1.7b-q4_k_m")
        c2 = selector.select(self.RTX, {"gpu_offload_supported": True}, prefer="quality", include_unconfirmed=True)
        self.assertNotEqual(c2.model.id, "qwen3-1.7b-q4_k_m")
        self.assertEqual(selector.select(self.RTX, {}, pinned="qwen3-4b-q4_k_m").model.id, "qwen3-4b-q4_k_m")   # explicit choice still allowed

    def test_unknown_pinned_model(self):
        c = selector.select(self.CPU_ONLY, {}, pinned="nope")
        self.assertFalse(c.fits); self.assertIsNone(c.model)

    def test_gpu_chosen_only_with_backend(self):
        self.assertEqual(selector.select(self.RTX, {"gpu_offload_supported": True}).mode, "gpu")
        self.assertEqual(selector.select(self.RTX, {"gpu_offload_supported": False}).mode, "cpu")

    def test_quality_vs_speed_preference(self):
        q = selector.select(self.RTX, {"gpu_offload_supported": True}, prefer="quality", include_unconfirmed=True)
        s = selector.select(self.RTX, {"gpu_offload_supported": True}, prefer="speed", include_unconfirmed=True)
        self.assertGreater(q.model.approx_size_bytes, s.model.approx_size_bytes)

    def test_no_disk_space(self):
        c = selector.select(Hardware("W", 8, 64.0, 0.5), {})
        self.assertFalse(c.fits)

    def test_unknown_ram_does_not_crash(self):
        c = selector.select(Hardware("W", 4, None, None), {})
        self.assertFalse(c.fits)

    def test_records_do_not_claim_verification(self):
        for m in catalog.BUILTIN:
            r = selector.compatibility_record(m)
            self.assertEqual(r["verified_hardware"], [])
            self.assertTrue(r["license"])
        unconfirmed = [m for m in catalog.BUILTIN if m.metadata_status.startswith("from-publisher-knowledge")]
        self.assertTrue(unconfirmed)
        for m in unconfirmed:
            self.assertIn("NOT confirmed", m.note)

    def test_matrix_has_no_fabricated_results(self):
        rows = selector.matrix_rows()
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["quality_result"], "not measured")
            self.assertEqual(r["performance_result"], "not measured")
            self.assertIn("NOT RUN", r["status"])
        self.assertFalse([r for r in rows if r["hardware_tier"] == "entry" and r["inference_mode"] == "gpu"])


class RealModelHarness(unittest.TestCase):
    def test_missing_model_stops_at_first_stage(self):
        with tempfile.TemporaryDirectory() as t:
            r = realmodel.validate(t)
            self.assertEqual(r["stages"]["model_downloaded"]["state"], "failed")
            self.assertEqual(r["stages"]["model_loaded"]["state"], "not_run")
            self.assertFalse(r["real_validation_passed"])

    def test_bad_magic_fails_integrity(self):
        with tempfile.TemporaryDirectory() as t:
            r = realmodel.validate(model_dir(t, magic=b"JUNK"), llama_factory=FakeKw)
            self.assertEqual(r["stages"]["file_integrity_checked"]["state"], "failed")
            self.assertEqual(r["stages"]["model_loaded"]["state"], "not_run")

    def test_checksum_mismatch_fails_integrity(self):
        with tempfile.TemporaryDirectory() as t:
            r = realmodel.validate(model_dir(t, meta={"sha256": "0" * 64}), llama_factory=FakeKw)
            self.assertEqual(r["stages"]["file_integrity_checked"]["state"], "failed")
            self.assertIn("mismatch", r["stages"]["file_integrity_checked"]["detail"])

    def test_matching_checksum_passes(self):
        with tempfile.TemporaryDirectory() as t:
            d = model_dir(t)
            sha = hashlib.sha256((d / "model.gguf").read_bytes()).hexdigest()
            (d / "meta.json").write_text(json.dumps({"sha256": sha}))
            r = realmodel.validate(d, llama_factory=FakeKw)
            self.assertEqual(r["stages"]["file_integrity_checked"]["state"], "passed")

    def test_simulated_run_can_never_be_real_validation(self):
        with tempfile.TemporaryDirectory() as t:
            r = realmodel.validate(model_dir(t), llama_factory=FakeKw, answer_check=lambda e: (True, "ok"))
            self.assertTrue(r["all_stages_passed"])
            self.assertTrue(r["simulated"])
            self.assertFalse(r["real_inference_passed"])
            self.assertFalse(r["real_validation_passed"])
            self.assertIn("SIMULATED", realmodel.format_report(r))

    def test_without_runtime_real_run_fails_at_runtime_stage(self):
        if ge.runtime_info()["available"]:
            self.skipTest("llama-cpp-python is installed here")
        with tempfile.TemporaryDirectory() as t:
            r = realmodel.validate(model_dir(t))
            self.assertEqual(r["stages"]["runtime_imported"]["state"], "failed")
            self.assertFalse(r["real_validation_passed"])

    def test_load_failure_recorded(self):
        def boom(**kw):
            raise OSError("bad file")
        with tempfile.TemporaryDirectory() as t:
            r = realmodel.validate(model_dir(t), llama_factory=boom)
            self.assertEqual(r["stages"]["model_loaded"]["state"], "failed")
            self.assertEqual(r["stages"]["inference_completed"]["state"], "not_run")

    def test_empty_reply_fails_inference(self):
        class Empty(FakeKw):
            def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
                return {"choices": [{"message": {"content": "  "}}]}
        with tempfile.TemporaryDirectory() as t:
            r = realmodel.validate(model_dir(t), llama_factory=Empty)
            self.assertEqual(r["stages"]["inference_completed"]["state"], "failed")

    def test_wrong_tool_fails_tool_stage(self):
        class NoTool(FakeKw):
            def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
                return {"choices": [{"message": {"content": "TOOL: delete_everything" if "TOOL:" in messages[0]["content"] else "ready"}}]}
        with tempfile.TemporaryDirectory() as t:
            r = realmodel.validate(model_dir(t), llama_factory=NoTool)
            self.assertEqual(r["stages"]["tool_calling_completed"]["state"], "failed")
            self.assertEqual(r["stages"]["end_to_end_task_completed"]["state"], "not_run")

    def test_no_end_to_end_task_means_not_run_not_passed(self):
        with tempfile.TemporaryDirectory() as t:
            r = realmodel.validate(model_dir(t), llama_factory=FakeKw)
            self.assertEqual(r["stages"]["end_to_end_task_completed"]["state"], "not_run")
            self.assertFalse(r["all_stages_passed"])


class Suite19(unittest.TestCase):
    def test_fifteen_areas_accounted_for(self):
        self.assertEqual(len(suite19.MODEL_AREAS) + len(suite19.COMPONENT_AREAS), 15)
        res = suite19.run_all(None)
        for a in suite19.MODEL_AREAS + suite19.COMPONENT_AREAS:
            self.assertIn(a, res)

    def test_no_model_means_not_measured(self):
        for a, v in suite19.run_model_areas(None).items():
            self.assertEqual(v["status"], "not_measured", a)
            self.assertNotIn("score", v)

    def test_oracle_proves_scorers_accept_correct_answers(self):
        for a, v in suite19.run_model_areas(suite19.oracle(), "scorer_check").items():
            self.assertEqual(v["correct"], v["n"], a)

    def test_wrong_answers_are_rejected(self):
        res = suite19.run_model_areas(lambda p: "banana", "t")
        for a, v in res.items():
            self.assertEqual(v["correct"], 0, a)

    def test_blanket_i_dont_know_does_not_win_everything(self):
        res = suite19.run_model_areas(lambda p: "I don't know.", "t")
        self.assertEqual(res["uncertainty_abstention"]["score"], 1.0)
        self.assertEqual(res["factual_accuracy"]["score"], 0.0)
        self.assertEqual(res["general_reasoning"]["score"], 0.0)

    def test_crashing_answerer_is_recorded_as_failure(self):
        def boom(p):
            raise RuntimeError("x")
        res = suite19.run_model_areas(boom, "t")
        self.assertEqual(res["planning"]["correct"], 0)
        self.assertIn("error", res["planning"]["items"][0])

    def test_long_context_prompts_are_actually_long(self):
        self.assertGreater(len(suite19.TASKS["long_context"][0]["prompt"]), 8000)

    def test_components_are_measured_without_a_model(self):
        c = suite19.run_components()
        self.assertEqual(c["memory_retrieval"]["status"], "measured")
        self.assertIn("NOT measured", c["latency_resource"]["note"])


def _pdf(text: str) -> bytes:
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"]
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET"
    objs += [f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()


class RealDocuments(unittest.TestCase):
    """Real generated files through the real reader. Only formats listed here are claimed to work."""
    def test_txt_csv_md_html(self):
        self.assertIn("alpha", research.extract(b"alpha beta", "a.txt")[0])
        self.assertIn("1,2", research.extract(b"a,b\n1,2\n", "t.csv")[0])
        self.assertIn("Title", research.extract(b"# Title\ntext", "n.md")[0])
        self.assertIn("Hello", research.extract(b"<html><body><p>Hello</p><script>x=1</script></body></html>", "p.html")[0])
        self.assertNotIn("x=1", research.extract(b"<html><body><p>Hello</p><script>x=1</script></body></html>", "p.html")[0])

    def test_docx_generated_by_python_docx(self):
        try:
            docx = __import__("docx")
        except ImportError:
            self.skipTest("python-docx not installed (test-only dependency: pip install python-docx)")
        d = docx.Document(); d.add_paragraph("Quarterly budget is 4200 dollars."); d.add_paragraph("Second paragraph.")
        b = io.BytesIO(); d.save(b)
        text = research.extract(b.getvalue(), "r.docx")[0]
        self.assertIn("4200", text); self.assertIn("Second paragraph", text)

    def test_pdf_with_text(self):
        t = research.extract(_pdf("Invoice total 987 units shipped to Ottawa"), "i.pdf")[0]
        self.assertIn("987", t); self.assertIn("[page 1]", t)

    def test_pdf_without_text_is_refused_not_invented(self):
        with self.assertRaises(research.Unreadable):
            research.extract(_pdf(""), "scan.pdf")

    def test_corrupt_files_are_refused_with_reasons(self):
        for raw, name in ((b"PK\x03\x04junk", "x.docx"), (b"%PDF-1.4 garbage", "x.pdf"), (b"\x00\x01\x02" * 100, "x.bin")):
            with self.assertRaises(research.Unreadable):
                research.extract(raw, name)

    def test_zip_bomb_docx_is_refused(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", "<w:document>" + "a" * 60_000_000 + "</w:document>")
        with self.assertRaises(research.Unreadable) as c:
            research.extract(b.getvalue(), "bomb.docx")
        self.assertIn("suspiciously large", str(c.exception))

    def test_entity_declaration_docx_is_refused(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x "y">]><d/>')
        with self.assertRaises(research.Unreadable):
            research.extract(b.getvalue(), "e.docx")

    def test_unsupported_office_formats_are_refused_not_faked(self):
        try:
            openpyxl = __import__("openpyxl")
        except ImportError:
            self.skipTest("openpyxl not installed (test-only dependency: pip install openpyxl)")
        wb = openpyxl.Workbook(); wb.active["A1"] = "secret"
        b = io.BytesIO(); wb.save(b)
        for name in ("sheet.xlsx", "deck.pptx", "old.doc"):
            with self.assertRaises(research.Unreadable) as c:
                research.extract(b.getvalue(), name)
            self.assertIn("not supported", str(c.exception))


class CliPlan(unittest.TestCase):
    def run_cli(self, *argv):
        import contextlib
        from jarvis import cli
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(buf):
            rc = cli.main(["--data-dir", t, "models", "plan", *argv])
        return rc, json.loads(buf.getvalue())

    def test_plan_reports_three_acceleration_facts_and_no_verified_hardware(self):
        rc, out = self.run_cli()
        self.assertIn(rc, (0, 2))
        for k in ("gpu_detected", "backend_available", "acceleration_verified"):
            self.assertIn(k, out["acceleration"])
        self.assertFalse(out["acceleration"]["acceleration_verified"])
        self.assertTrue(all(r["verified_hardware"] == [] for r in out["records"]))

    def test_pinned_model_that_cannot_fit_exits_nonzero_and_is_not_replaced(self):
        from jarvis import hardware
        orig = hardware.detect_hardware
        hardware.detect_hardware = lambda *a, **k: Hardware("Windows 11", 4, 4.0, 50.0)
        try:
            rc, out = self.run_cli("--model", "qwen3-8b-q4_k_m")
        finally:
            hardware.detect_hardware = orig
        self.assertEqual(rc, 2)
        self.assertEqual(out["model"], "qwen3-8b-q4_k_m")
        self.assertFalse(out["fits"])


if __name__ == "__main__":
    unittest.main()
