"""Stage 5 continuation without Windows: host-metadata hardening, Qwen3-8B catalog evidence (data only; the catalog is NOT changed here), setup download
errors, audit privacy for ui_act, and how the benchmark summary tells the reader what its numbers are.

What is REAL: resolve_file's parsing, SetupEngine's error handling, AuditLog, the benchmark summary text.
What is FAKE: the model host (scripted JSON bodies; no socket is opened), the model store, and the benchmark engine.
The file lists below were read from Hugging Face web pages on 2026-10-27 through a fetch tool that returns summaries; they are the evidence behind the tests,
not a measurement made by this code. NOTHING here downloaded a model, ran a runtime, or ran on Windows."""
import dataclasses
import io
import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.app import setup_engine as SE  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.desktop.audit import AuditLog, scrub_args  # noqa: E402
from jarvis.models import catalog  # noqa: E402
from jarvis.models.catalog import get, resolve_file  # noqa: E402
from jarvis.net.policy import NetworkPolicy  # noqa: E402

SHA = "d" * 64
# File list of https://huggingface.co/Qwen/Qwen3-8B-GGUF/tree/main (page read 2026-10-27; sizes as displayed, rounded)
QWEN_8B_FILES = [".gitattributes", "LICENSE", "Qwen3-8B-Q4_K_M.gguf", "Qwen3-8B-Q5_0.gguf", "Qwen3-8B-Q5_K_M.gguf", "Qwen3-8B-Q6_K.gguf", "Qwen3-8B-Q8_0.gguf", "README.md", "params"]
# File list of ggml-org/Qwen3-8B-GGUF (docs/STAGE5_MODEL_SOURCE_VERIFICATION.md section 17.4): there is NO Q4_K_M file
GGML_8B_FILES = [".gitattributes", "README.md", ".src_sha", "convert.log", "Qwen3-8B-BF16.gguf", "Qwen3-8B-Q8_0.gguf", "dflash-Qwen3-8B-BF16.gguf",
                 "dflash-Qwen3-8B-Q8_0.gguf", "dspark-Qwen3-8B-BF16.gguf", "dspark-Qwen3-8B-Q8_0.gguf"]
EXACT_8B_REGEX = r"(?i)^Qwen3-8B-Q4_K_M\.gguf$"          # the regex proposed (NOT applied) in the verification report, section 13.2


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Policy:
    """Stands in for NetworkPolicy: returns a scripted body for any request."""

    def __init__(self, body):
        self.body, self.requests = body, []

    def open(self, feature, url, **kw):
        self.requests.append((feature, url, kw.get("allowed_host_suffixes")))
        return _Resp(self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode())


def sibling(name, size=2_500_000_000, sha=SHA, **over):
    d = {"rfilename": name, "size": size, "lfs": {"sha256": sha, "size": size}}
    d.update(over)
    return d


def resolve(body, model_id="qwen3-4b-q4_k_m", **replace):
    m = get(model_id)
    if replace:
        m = dataclasses.replace(m, **replace)
    return resolve_file(_Policy(body), m)


class HostMetadataIsUntrusted(unittest.TestCase):
    def test_a_well_formed_answer_is_parsed(self):
        r = resolve({"siblings": [sibling("Qwen3-4B-Q4_K_M.gguf")]})
        self.assertEqual((r["name"], r["size_bytes"], r["sha256"]), ("Qwen3-4B-Q4_K_M.gguf", 2_500_000_000, SHA))
        self.assertTrue(r["url"].startswith("https://huggingface.co/ggml-org/Qwen3-4B-GGUF/resolve/main/"))

    def test_digit_text_sizes_and_a_missing_size_are_accepted_as_before(self):
        self.assertEqual(resolve({"siblings": [sibling("Qwen3-4B-Q4_K_M.gguf", size="2500000000", lfs={"sha256": SHA, "size": "2500000000"})]})["size_bytes"], 2_500_000_000)
        r = resolve({"siblings": [{"rfilename": "Qwen3-4B-Q4_K_M.gguf", "lfs": {"sha256": SHA}}]})
        self.assertEqual(r["size_bytes"], 0)                                 # unknown: the caller bounds it by the catalog estimate

    def test_an_unusable_size_is_refused_with_one_clean_error(self):
        for bad in ("abc", "-5", "12.5", "1e9", -5, 2.5e9, True, [1], {"a": 1}, "٣٣"):
            with self.subTest(size=bad):
                with self.assertRaises(LookupError) as cm:
                    resolve({"siblings": [sibling("Qwen3-4B-Q4_K_M.gguf", size=bad, lfs={"sha256": SHA, "size": bad})]})
                self.assertIn("unusable size", str(cm.exception))
                self.assertNotIn("/", str(cm.exception).replace("nothing was downloaded", ""))        # no path or URL in the message

    def test_wrong_shapes_are_lookup_errors_never_raw_type_errors(self):
        shapes = {"top-level list": [1, 2], "top-level text": "x", "siblings text": {"siblings": "x"}, "siblings null": {"siblings": None},
                  "siblings dict": {"siblings": {"a": 1}}, "lfs list": {"siblings": [sibling("Qwen3-4B-Q4_K_M.gguf", lfs=[1])]},
                  "lfs text": {"siblings": [sibling("Qwen3-4B-Q4_K_M.gguf", lfs="x")]}}
        for name, body in shapes.items():
            with self.subTest(shape=name):
                with self.assertRaises(LookupError):
                    resolve(body)

    def test_odd_file_names_are_skipped_not_crashed_on(self):
        body = {"siblings": [{"rfilename": 123}, {"rfilename": None}, {"x": 1}, "text", None, sibling("Qwen3-4B-Q4_K_M.gguf")]}
        self.assertEqual(resolve(body)["name"], "Qwen3-4B-Q4_K_M.gguf")

    def test_zero_two_or_nested_matches_are_refused(self):
        with self.assertRaises(LookupError):
            resolve({"siblings": []})
        with self.assertRaises(LookupError):
            resolve({"siblings": [sibling("Qwen3-4B-Q4_K_M.gguf"), sibling("Qwen3-4B-Q4_K_M.gguf")]})
        with self.assertRaises(LookupError):
            resolve({"siblings": [sibling("sub/Qwen3-4B-Q4_K_M.gguf")]})

    def test_invalid_json_stays_a_value_error_the_command_line_already_handles(self):
        with self.assertRaises(ValueError):
            resolve(b"{not json")

    def test_a_checksum_that_is_not_64_hex_is_treated_as_missing_not_trusted(self):
        for bad in ("abc", 123, "g" * 64, "A" * 63):
            self.assertIsNone(resolve({"siblings": [sibling("Qwen3-4B-Q4_K_M.gguf", lfs={"sha256": bad, "size": 1})]})["sha256"], bad)
        self.assertEqual(resolve({"siblings": [sibling("Qwen3-4B-Q4_K_M.gguf", lfs={"sha256": SHA.upper(), "size": 1})]})["sha256"], SHA)

    def test_only_the_api_of_the_model_host_is_asked(self):
        p = _Policy({"siblings": [sibling("Qwen3-4B-Q4_K_M.gguf")]})
        resolve_file(p, get("qwen3-4b-q4_k_m"))
        feature, url, hosts = p.requests[0]
        self.assertEqual(feature, "model_download")
        self.assertEqual(url, "https://huggingface.co/api/models/ggml-org/Qwen3-4B-GGUF?blobs=true")
        self.assertEqual(hosts, catalog.HF_HOSTS)


class Qwen3_8B_CatalogEvidence(unittest.TestCase):
    """The corrected catalog entry, tested on DATA recorded from the publisher's pages and from the ggml-org repository listing (no network)."""

    def pick(self, regex, files):
        rx = re.compile(regex)
        return [f for f in files if rx.search(f) and "/" not in f]

    def test_the_catalog_entry_points_at_the_publishers_repository_and_the_exact_file(self):
        m = get("qwen3-8b-q4_k_m")
        self.assertEqual((m.repo, m.file_regex), ("Qwen/Qwen3-8B-GGUF", EXACT_8B_REGEX))
        self.assertEqual((m.license_spdx, m.approx_size_bytes, m.quant), ("Apache-2.0", int(5.03e9), "Q4_K_M"))
        self.assertFalse(m.metadata_status.startswith("from-publisher-knowledge"))
        self.assertIn("not the API", m.note)                    # the entry says how its facts were obtained

    def test_the_old_repository_has_no_q4_k_m_file_so_the_old_entry_could_never_work(self):
        self.assertEqual(self.pick(r"(?i)q4_k_m.*\.gguf$", GGML_8B_FILES), [])
        old = dataclasses.replace(get("qwen3-8b-q4_k_m"), repo="ggml-org/Qwen3-8B-GGUF", file_regex=r"(?i)q4_k_m.*\.gguf$")
        with self.assertRaises(LookupError) as cm:
            resolve_file(_Policy({"siblings": [sibling(f) for f in GGML_8B_FILES]}), old)
        self.assertIn("found 0", str(cm.exception))

    def test_the_official_repository_has_exactly_one_q4_k_m_file_and_the_catalog_entry_finds_only_it(self):
        self.assertEqual(self.pick(EXACT_8B_REGEX, QWEN_8B_FILES), ["Qwen3-8B-Q4_K_M.gguf"])
        r = resolve_file(_Policy({"siblings": [dict(sibling(f, size=5_030_000_000), lfs=None) for f in QWEN_8B_FILES]}), get("qwen3-8b-q4_k_m"))
        self.assertEqual((r["name"], r["size_bytes"]), ("Qwen3-8B-Q4_K_M.gguf", 5_030_000_000))
        self.assertIn("/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf", r["url"])
        self.assertIsNone(r["sha256"])          # a host answer without a checksum gives none; download_gguf then refuses unless the user confirms

    def test_the_8b_model_is_installable_on_request_but_never_chosen_automatically(self):
        from jarvis.models import selector
        from jarvis.config import Config as _C
        self.assertFalse(get("qwen3-8b-q4_k_m").auto_recommend)
        hw = namedtuple("HW", "ram_gb vram_gb free_disk_gb avx2 cpu_threads")(64.0, 24.0, 500.0, True, 16)
        for prefer in ("quality", "balanced", "speed"):
            for unconfirmed in (False, True):
                self.assertNotEqual(selector.select(hw, {"gpu_offload_supported": True}, prefer=prefer, include_unconfirmed=unconfirmed).model.id, "qwen3-8b-q4_k_m")
        self.assertEqual(selector.select(hw, {}, pinned="qwen3-8b-q4_k_m").model.id, "qwen3-8b-q4_k_m")        # an explicit choice is honoured

    def test_a_host_checksum_is_passed_on_and_a_file_without_one_is_refused_by_default(self):
        from jarvis.models.manager import ModelStore
        r = resolve_file(_Policy({"siblings": [sibling("Qwen3-8B-Q4_K_M.gguf", lfs={"sha256": SHA, "size": 5_030_000_000})]}), get("qwen3-8b-q4_k_m"))
        self.assertEqual(r["sha256"], SHA)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(catalog, "resolve_file", return_value={"name": "Qwen3-8B-Q4_K_M.gguf", "url": "https://huggingface.co/x", "size_bytes": 5_030_000_000, "sha256": None}):
            with self.assertRaises(Exception) as cm:
                ModelStore(Path(d), NetworkPolicy()).download_gguf(get("qwen3-8b-q4_k_m"))
        self.assertIn("checksum", str(cm.exception).lower())

    def test_the_loose_pattern_would_also_accept_look_alike_files_the_exact_one_refuses(self):
        later = QWEN_8B_FILES + ["dflash-Qwen3-8B-Q4_K_M.gguf", "Qwen3-8B-Q4_K_M.gguf.bak"]
        self.assertEqual(len(self.pick(r"(?i)q4_k_m.*\.gguf$", later)), 2)            # the loose pattern: the unmodified name AND a look-alike
        self.assertEqual(self.pick(EXACT_8B_REGEX, later), ["Qwen3-8B-Q4_K_M.gguf"])

    def test_a_15_percent_ceiling_on_the_rounded_page_size_is_what_would_apply(self):
        from jarvis.models import diskspace
        self.assertEqual(diskspace.size_limit(int(5.03e9)), int(5.03e9) * 115 // 100)


class SetupDownloadErrors(unittest.TestCase):
    def engine(self, exc):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = mock.Mock()
        store.dir = Path(self.tmp.name)
        store.active.return_value = None
        store.download_gguf.side_effect = exc
        sleeps = []
        eng = SE.SetupEngine(Config(data_dir=Path(self.tmp.name) / "d"), store, NetworkPolicy(), llama_factory=object, hardware=object(), runtime={}, sleep=sleeps.append)
        return eng, store, sleeps

    def run_download(self, exc):
        eng, store, sleeps = self.engine(exc)
        usage = namedtuple("usage", "total used free")
        with mock.patch.object(SE.shutil, "disk_usage", return_value=usage(10**12, 0, 10**12)):
            return eng.download("qwen3-4b-q4_k_m", attempts=3), store, sleeps

    def test_a_changed_or_malformed_host_answer_is_reported_once_without_retries_or_blaming_the_connection(self):
        res, store, sleeps = self.run_download(LookupError("expected exactly one matching file in ggml-org/Qwen3-8B-GGUF, found 0; the publisher may have changed the repository"))
        self.assertEqual((res["ok"], res["kind"]), (False, "host_changed"))
        self.assertEqual(store.download_gguf.call_count, 1)
        self.assertEqual(sleeps, [])
        self.assertIn("not a connection problem", res["message"])
        self.assertNotIn("Check the internet connection", res["message"])

    def test_real_network_trouble_is_still_retried_and_still_says_so(self):
        res, store, sleeps = self.run_download(OSError("connection reset"))
        self.assertEqual((res["ok"], res["kind"]), (False, "network"))
        self.assertEqual(store.download_gguf.call_count, 3)
        self.assertEqual(len(sleeps), 2)
        self.assertIn("Check the internet connection", res["message"])

    def test_integrity_and_disk_failures_are_unchanged(self):
        from jarvis.models.diskspace import DiskSpaceError
        from jarvis.models.manager import IntegrityError
        res, store, _ = self.run_download(IntegrityError("hash mismatch"))
        self.assertEqual((res["kind"], store.download_gguf.call_count), ("integrity", 1))
        res, store, _ = self.run_download(DiskSpaceError("not enough space"))
        self.assertEqual((res["kind"], store.download_gguf.call_count), ("disk_space", 1))


class AuditDoesNotKeepTypedValues(unittest.TestCase):
    SECRET = "correct horse battery staple 12"

    def test_the_value_set_by_ui_act_is_recorded_as_length_and_hash_like_typed_text(self):
        a = AuditLog(None)
        r = a.write(event="start", tool="ui_act", args={"name": "Password", "action": "set_value", "value": self.SECRET})
        self.assertNotIn(self.SECRET, json.dumps(r))
        self.assertRegex(r["args"]["value"], r"^<31 chars, sha256 [0-9a-f]{12}>$")
        self.assertEqual(r["args"]["name"], "Password")                            # the control's name stays readable: it is what makes the log useful

    def test_every_other_typed_or_written_field_is_still_digested(self):
        out = scrub_args({"text": self.SECRET, "keys": self.SECRET, "content": self.SECRET, "old_text": self.SECRET, "new_text": self.SECRET, "value": self.SECRET})
        self.assertNotIn(self.SECRET, json.dumps(out))

    def test_the_audit_file_on_disk_has_no_value_either(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "audit.jsonl"
            AuditLog(p).write(event="confirm_asked", tool="ui_act", args={"action": "set_value", "value": self.SECRET})
            self.assertNotIn(self.SECRET, p.read_text(encoding="utf-8"))


class BenchmarkSummaryStatesWhatItIs(unittest.TestCase):
    def test_the_text_says_the_pooled_rate_is_not_an_intelligence_rating_and_what_is_not_measured(self):
        from test_p23_intel_bench import GOLD, FakeEngine
        from jarvis.eval import intel_bench as IB
        res = IB.run("unused", sampled_runs=0, engine=FakeEngine(GOLD), only=["mem1"])
        t = res["summary_text"]
        self.assertIn("NOT an intelligence rating", t)
        self.assertIn("do not measure language quality", t)
        self.assertIn("Memory use is not recorded by this command", t)
        self.assertIn("[SIMULATED", t)


if __name__ == "__main__":
    unittest.main()
