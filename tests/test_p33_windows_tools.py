"""Stage 5 Windows-readiness tooling: scripts/verify_model_metadata.py, verify_downloaded_model.py, verify_wheel.py, resource_guard.py,
summarize_stage5_results.py.

Linux only. Network replaced by fixtures, disk readings mocked, wheels and PE files SYNTHETIC. These tests show the tools' logic and fail-closed behaviour; they say
nothing about Hugging Face today, about any real wheel, or about Windows."""
import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VM = load("verify_model_metadata")
VD = load("verify_downloaded_model")
VW = load("verify_wheel")
RG = load("resource_guard")
SR = load("summarize_stage5_results")
from jarvis.models import diskspace  # noqa: E402
from p10_common import make_gguf  # noqa: E402
from test_vc_runtime import make_pe  # noqa: E402
from test_p23_intel_bench import GOLD, FakeEngine, IB  # noqa: E402

WHEN = "2026-10-27T12:00:00Z"


def api_record(cand, size=5_030_000_000, **over):
    """What the host's API returns for a healthy repository."""
    rec = {"id": cand["repo"], "author": cand["repo"].split("/")[0], "sha": "a" * 40, "lastModified": "2026-01-01T00:00:00.000Z", "tags": ["gguf", "license:apache-2.0"],
           "siblings": [{"rfilename": "README.md"}, {"rfilename": "LICENSE"},
                        {"rfilename": cand["file"], "size": size, "lfs": {"sha256": cand["recorded_sha256"], "size": size, "pointerSize": 135}}]}
    rec.update(over)
    return rec


def by_id(i):
    return next(dict(c) for c in VM.CANDIDATES if c["id"] == i)


class MetadataEvaluate(unittest.TestCase):
    def ev(self, data, cid="qwen3-8b-official"):
        return VM.evaluate(by_id(cid), data, None, WHEN)

    def test_healthy_record_is_consistent_and_says_the_hash_is_only_published(self):
        c = by_id("qwen3-8b-official")
        r = self.ev(api_record(c))
        self.assertEqual((r["status"], r["problems"]), ("METADATA_CONSISTENT", []))
        self.assertEqual(r["size_bytes"], 5_030_000_000)
        self.assertEqual(r["size_limit_115_percent_bytes"], 5_030_000_000 * 115 // 100)
        self.assertEqual(r["published_sha256"], c["recorded_sha256"])
        self.assertIn("NOT computed from file bytes", r["hash_kind"])
        self.assertEqual(r["revision"], "a" * 40)
        self.assertEqual(r["retrieved_at_utc"], WHEN)

    def test_every_deviation_is_a_named_problem_never_silently_accepted(self):
        c = by_id("qwen3-8b-official")
        base = api_record(c)

        def sib(**kw):
            d = api_record(c)
            f = d["siblings"][2]
            for k, v in kw.items():
                if k.startswith("lfs_"):
                    f["lfs"][k[4:]] = v
                else:
                    f[k] = v
            return d
        cases = {
            "OWNER_MISMATCH": api_record(c, author="someone-else"),
            "REPO_ID_MISMATCH": api_record(c, id="Qwen/Renamed-GGUF"),
            "REPO_GATED": api_record(c, gated="manual"),
            "REPO_PRIVATE": api_record(c, private=True),
            "REPO_DISABLED": api_record(c, disabled=True),
            "REVISION_MISSING": {k: v for k, v in base.items() if k != "sha"},
            "LICENSE_MISSING": api_record(c, tags=["gguf"]),
            "LICENSE_NOT_APACHE_2_0": api_record(c, tags=["license:mit"]),
            "FILE_MISSING": api_record(c, siblings=[{"rfilename": "other.gguf", "size": 1}]),
            "FILE_DUPLICATE": api_record(c, siblings=base["siblings"] + [base["siblings"][2]]),
            "SIZE_INVALID": sib(size=0, lfs_size=0),
            "SIZE_MISSING": sib(size=None, lfs_size=None),
            "SIZE_CONFLICT": sib(size=5_030_000_001),
            "HASH_MISSING": sib(lfs_sha256=None),
            "HASH_MALFORMED": sib(lfs_sha256="zz" * 32),
            "HASH_DIFFERS_FROM_RECORDED": sib(lfs_sha256="0" * 64),
        }
        for code, data in cases.items():
            with self.subTest(code=code):
                r = self.ev(data)
                self.assertIn(code, r["problems"])
                self.assertEqual(r["status"], "PROBLEMS")

    def test_odd_types_do_not_crash_and_are_problems(self):
        c = by_id("qwen3-8b-official")
        for size in ("5", True, -3, 1.5, [1]):
            d = api_record(c)
            d["siblings"][2]["size"] = size
            d["siblings"][2]["lfs"]["size"] = size
            with self.subTest(size=size):
                self.assertEqual(self.ev(d)["status"], "PROBLEMS")
        junk = {"id": c["repo"], "author": 5, "siblings": [None, 3, "x", {"rfilename": None}], "tags": [1, None], "cardData": "x", "sha": 7}
        r = self.ev(junk)
        self.assertEqual(r["status"], "PROBLEMS")

    def test_similar_files_are_noted_and_a_lookalike_never_satisfies_the_exact_name(self):
        c = by_id("qwen3-8b-official")
        d = api_record(c)
        d["siblings"][2]["rfilename"] = "Qwen3-8B-Q4_K_M-imatrix.gguf"
        r = self.ev(d)
        self.assertIn("FILE_MISSING", r["problems"])
        self.assertEqual(r["other_q4_k_m_files"], ["Qwen3-8B-Q4_K_M-imatrix.gguf"])

    def test_api_unavailable_is_its_own_status(self):
        r = VM.evaluate(by_id("qwen3-8b-official"), None, "URLError: down", WHEN)
        self.assertEqual((r["status"], r["problems"]), ("API_UNAVAILABLE", ["API_UNAVAILABLE"]))

    def test_a_repository_without_a_licence_file_is_noted_not_hidden(self):
        c = by_id("qwen3-4b-ggml-org")
        d = api_record(c)
        d["siblings"] = [s for s in d["siblings"] if s["rfilename"] != "LICENSE"]
        r = self.ev(d, "qwen3-4b-ggml-org")
        self.assertFalse(r["license_file_in_repo"])
        self.assertTrue(any("no LICENSE file" in n for n in r["notes"]))

    def test_recorded_hashes_match_the_verification_report(self):
        doc = (ROOT / "docs" / "STAGE5_MODEL_SOURCE_VERIFICATION.md").read_text(encoding="utf-8")
        for c in VM.CANDIDATES:
            self.assertIn(c["recorded_sha256"], doc, c["id"])


class MetadataRun(unittest.TestCase):
    sizes = {"qwen3-1.7b-ggml-org": 1_280_000_000, "qwen3-4b-ggml-org": 2_500_000_000, "qwen3-4b-official": 2_500_000_000,
             "qwen3-8b-official": 5_030_000_000, "qwen3-14b-official": 9_000_000_000}

    def fake_fetch(self, mutate=None):
        by_repo = {c["repo"]: c for c in VM.CANDIDATES}

        def f(repo):
            c = by_repo[repo]
            d = api_record(c, size=self.sizes[c["id"]])
            return mutate(c, d) if mutate else d
        return f

    def run_it(self, fetch, **kw):
        with tempfile.TemporaryDirectory() as t:
            doc, code = VM.run(Path(t), fetch=fetch, now=lambda: WHEN, **kw)
            files = sorted(p.name for p in Path(t).iterdir())
            raw = sorted(p.name for p in (Path(t) / "raw").iterdir())
            md = (Path(t) / "model_metadata.md").read_text(encoding="utf-8")
        return doc, code, files, raw, md

    def test_all_consistent_and_within_budget_exits_0_and_writes_the_record(self):
        doc, code, files, raw, md = self.run_it(self.fake_fetch())
        self.assertEqual(code, 0, doc["overall"])
        self.assertEqual(files, ["model_metadata.json", "model_metadata.md", "raw"])
        self.assertEqual(len(raw), 6)          # five API responses plus the retrieval timestamp
        self.assertEqual(doc["mode"], "live-api")
        self.assertEqual(doc["budget"]["status"], "WITHIN_BUDGET")
        self.assertEqual(doc["budget"]["total_bytes"], 1_280_000_000 + 2_500_000_000 + 5_030_000_000 + 9_000_000_000)
        self.assertIn("NOT computed from file bytes", doc["hash_kind"])
        self.assertIn("Nothing was downloaded", md)

    def test_the_control_file_is_outside_the_plan_and_breaks_the_budget_when_included(self):
        doc, code, *_ = self.run_it(self.fake_fetch(), include_control=True)
        self.assertEqual(doc["budget"]["total_bytes"], 20_310_000_000)
        self.assertEqual((doc["budget"]["status"], code), ("OVER_BUDGET", 4))

    def test_one_problem_in_a_planned_file_exits_4_and_names_it(self):
        def mutate(c, d):
            if c["id"] == "qwen3-8b-official":
                d["siblings"][2]["lfs"]["sha256"] = "1" * 64
            return d
        doc, code, *_ = self.run_it(self.fake_fetch(mutate))
        self.assertEqual(code, 4)
        self.assertIn("qwen3-8b-official: HASH_DIFFERS_FROM_RECORDED", doc["overall"])

    def test_a_problem_only_in_the_excluded_control_does_not_fail_the_plan(self):
        def mutate(c, d):
            if c["id"] == "qwen3-4b-official":
                d["siblings"][2]["lfs"]["sha256"] = "1" * 64
            return d
        doc, code, *_ = self.run_it(self.fake_fetch(mutate))
        self.assertEqual(code, 0)

    def test_unavailable_api_exits_3_and_claims_nothing(self):
        def boom(repo):
            raise VM.ApiUnavailable("URLError: no route")
        doc, code, *_ = self.run_it(boom)
        self.assertEqual(code, 3)
        self.assertIn("nothing is claimed", doc["overall"])
        self.assertTrue(all(r["status"] == "API_UNAVAILABLE" for r in doc["results"]))

    def test_a_missing_size_makes_the_budget_unknown_and_fails(self):
        def mutate(c, d):
            if c["id"] == "qwen3-14b-official":
                d["siblings"][2].pop("size")
                d["siblings"][2]["lfs"].pop("size")
            return d
        doc, code, *_ = self.run_it(self.fake_fetch(mutate))
        self.assertEqual(code, 4)
        self.assertTrue(doc["budget"]["status"].startswith("UNKNOWN"))

    def test_disk_preflight_requires_25_gb_and_room_for_the_largest_two_copies(self):
        for free, expected in ((24_999_999_999, 4), (40_000_000_000, 0)):
            doc, code, *_ = self.run_it(self.fake_fetch(), check_disk="X:", free_fn=lambda p, f=free: f)
            with self.subTest(free=free):
                self.assertEqual(code, expected, doc["overall"])
        doc, code, *_ = self.run_it(self.fake_fetch(), check_disk="X:", free_fn=lambda p: 40_000_000_000)
        peak = doc["disk"]["peak_need_for_largest_with_margin_bytes"]
        self.assertGreater(peak, 2 * 9_000_000_000)
        doc, code, *_ = self.run_it(self.fake_fetch(), check_disk="X:", free_fn=lambda p: peak - 1 if peak - 1 >= 25_000_000_000 else 25_000_000_000)
        self.assertEqual(code, 4 if peak > 25_000_000_000 else 0)

    def test_unreadable_free_space_is_unknown_and_fails_closed(self):
        def bad(p):
            raise diskspace.DiskSpaceError("could not determine")
        doc, code, *_ = self.run_it(self.fake_fetch(), check_disk="X:", free_fn=bad)
        self.assertEqual((doc["disk"]["status"], code), ("UNKNOWN", 4))

    def test_offline_replay_of_saved_raw_responses_gives_the_same_verdict(self):
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            VM.run(Path(t1), fetch=self.fake_fetch(), now=lambda: WHEN)
            doc, code = VM.run(Path(t2), from_raw=Path(t1) / "raw", now=lambda: WHEN)
        self.assertEqual(code, 0)

    def test_replay_with_a_missing_raw_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            doc, code = VM.run(Path(t1), from_raw=Path(t2), now=lambda: WHEN)
        self.assertEqual(code, 3)


class MetadataNetworkSafety(unittest.TestCase):
    def test_only_api_urls_for_valid_repo_names_are_built(self):
        self.assertEqual(VM.api_url("Qwen/Qwen3-8B-GGUF"), "https://huggingface.co/api/models/Qwen/Qwen3-8B-GGUF?blobs=true")
        for bad in ("", "a", "a/b/c", "../x", "a/../b", "a b/c", "a/b?x=1", "http://evil/x", "a/b#f", "/a/b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                VM.api_url(bad)
        for c in VM.CANDIDATES:
            self.assertNotIn("resolve", VM.api_url(c["repo"]))

    def test_redirects_to_other_hosts_or_paths_are_refused(self):
        h = VM._SameHostOnly()
        for url in ("https://evil.example/api/models/x", "http://huggingface.co/api/models/x", "https://huggingface.co/Qwen/x/resolve/main/f.gguf",
                    "https://cdn-lfs.huggingface.co/api/x"):
            with self.subTest(url=url), self.assertRaises(Exception):
                h.redirect_request(mock.Mock(), None, 302, "Found", {}, url)

    def test_network_errors_and_bad_bodies_become_api_unavailable(self):
        class R:
            def __init__(self, b):
                self.b = b

            def read(self, n):
                return self.b[:n]

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False
        for body in (b"not json", b"[1]", b"x" * (VM.MAX_API_BYTES + 5)):
            opener = mock.Mock()
            opener.open.return_value = R(body)
            with self.subTest(body=body[:10]), mock.patch.object(VM.urllib.request, "build_opener", return_value=opener), self.assertRaises(VM.ApiUnavailable):
                VM.fetch_api("Qwen/Qwen3-8B-GGUF")
        opener = mock.Mock()
        opener.open.side_effect = VM.urllib.error.URLError("down")
        with mock.patch.object(VM.urllib.request, "build_opener", return_value=opener), self.assertRaises(VM.ApiUnavailable):
            VM.fetch_api("Qwen/Qwen3-8B-GGUF")
        opener.open.side_effect = None
        opener.open.return_value = R(b'{"id": "x"}')
        with mock.patch.object(VM.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(VM.fetch_api("Qwen/Qwen3-8B-GGUF"), {"id": "x"})
        req = opener.open.call_args[0][0]
        self.assertTrue(req.full_url.startswith("https://huggingface.co/api/models/"))


class DownloadedFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.f = make_gguf(self.d / "Qwen3-8B-Q4_K_M.gguf", pad=3000)
        self.sha = VD.sha256_of(self.f)
        self.size = self.f.stat().st_size
        self.meta = {"results": [self.record()]}

    def record(self, **kw):
        r = {"id": "m8", "file": "Qwen3-8B-Q4_K_M.gguf", "status": "METADATA_CONSISTENT", "problems": [], "size_bytes": self.size, "published_sha256": self.sha}
        r.update(kw)
        return r

    def v(self, **kw):
        args = dict(file=self.f, metadata=self.meta, candidate="m8")
        args.update(kw)
        return VD.verify(**args)

    def failed(self, res):
        return [c["check"] for c in res["checks"] if not c["ok"]]

    def test_matching_file_passes_and_the_hash_is_computed_from_the_bytes(self):
        r = self.v()
        self.assertEqual(r["status"], "PASS")
        self.assertTrue(r["hash_computed_from_bytes"])
        self.assertEqual(r["computed_sha256"], self.sha)
        self.assertIn("NO human comparison", r["meaning"])
        self.assertIn(f"--expected-size-bytes {self.size}", r["add_gguf_command"])
        self.assertIn(f"--sha256 {self.sha}", r["add_gguf_command"])
        self.assertNotIn(str(self.d), json.dumps(r))

    def test_a_human_hash_that_agrees_is_recorded_and_one_that_differs_fails(self):
        r = self.v(human_sha256="  " + self.sha.upper() + "\n")
        self.assertEqual(r["status"], "PASS")
        self.assertTrue(r["human_page_hash_compared"])
        self.assertIn("computed_hash_equals_hash_read_on_model_page", [c["check"] for c in r["checks"]])
        self.assertEqual(self.failed(self.v(human_sha256="1" * 64)), ["computed_hash_equals_hash_read_on_model_page"])
        self.assertEqual(self.failed(self.v(human_sha256="xyz")), ["human_hash_well_formed"])

    def test_size_differences_fail_before_the_file_is_hashed(self):
        for delta in (1, -1):
            self.meta["results"] = [self.record(size_bytes=self.size + delta)]
            with self.subTest(delta=delta):
                r = self.v(hash_fn=mock.Mock(side_effect=AssertionError("must not hash")))
                self.assertEqual(r["status"], "FAIL")
                self.assertFalse(r["hash_computed_from_bytes"])

    def test_a_file_above_115_percent_of_the_published_size_fails(self):
        self.meta["results"] = [self.record(size_bytes=self.size * 100 // 130)]
        self.assertEqual(self.failed(self.v(hash_fn=mock.Mock(side_effect=AssertionError("no")))), ["size_within_115_percent"])

    def test_wrong_hash_wrong_name_unknown_candidate_and_bad_metadata_all_fail(self):
        self.meta["results"] = [self.record(published_sha256="0" * 64)]
        self.assertEqual(self.failed(self.v()), ["computed_hash_equals_published_hash"])
        self.meta["results"] = [self.record(file="Other.gguf")]
        self.assertEqual(self.failed(self.v()), ["file_name_matches_metadata"])
        self.assertEqual(self.failed(self.v(candidate="nope")), ["candidate_in_metadata"])
        for bad in (self.record(status="PROBLEMS", problems=["HASH_MISSING"]), self.record(status="API_UNAVAILABLE")):
            self.meta["results"] = [bad]
            self.assertEqual(self.failed(self.v()), ["metadata_consistent"])
        for bad in (self.record(size_bytes=0), self.record(size_bytes="5"), self.record(published_sha256="zz")):
            self.meta["results"] = [bad]
            self.assertEqual(self.failed(self.v()), ["metadata_has_size_and_hash"])

    def test_links_folders_and_non_gguf_files_fail(self):
        link = self.d / "Qwen3-8B-Q4_K_M.gguf.lnk"
        ln = self.d / "sub"
        ln.mkdir()
        target = ln / "Qwen3-8B-Q4_K_M.gguf"
        target.symlink_to(self.f)
        self.assertEqual(self.failed(self.v(file=target)), ["regular_file_not_a_link"])
        d = self.d / "dir" / "Qwen3-8B-Q4_K_M.gguf"
        d.mkdir(parents=True)
        self.assertEqual(self.failed(self.v(file=d)), ["regular_file_not_a_link"])
        junk = self.d / "j" / "Qwen3-8B-Q4_K_M.gguf"
        junk.parent.mkdir()
        junk.write_bytes(b"not a gguf at all" * 50)
        self.meta["results"] = [self.record(size_bytes=junk.stat().st_size, published_sha256=VD.sha256_of(junk))]
        self.assertEqual(self.failed(self.v(file=junk)), ["gguf_header_valid"])
        self.assertEqual(self.failed(self.v(file=self.d / "missing.gguf")), ["file_name_matches_metadata"])

    def test_room_for_the_add_gguf_copy_is_checked(self):
        need = diskspace.required_free(diskspace.need_for_copy(self.size))
        self.assertEqual(self.v(data_dir=self.d, free_fn=lambda p: need)["status"], "PASS")
        self.assertEqual(self.failed(self.v(data_dir=self.d, free_fn=lambda p: need - 1)), ["room_for_add_gguf_copy"])

        def bad(p):
            raise diskspace.DiskSpaceError("cannot read")
        self.assertEqual(self.failed(self.v(data_dir=self.d, free_fn=bad)), ["room_for_add_gguf_copy"])

    def test_cli_exit_codes_and_saved_json(self):
        mp = self.d / "meta.json"
        mp.write_text(json.dumps(self.meta), encoding="utf-8")
        out = self.d / "out"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = VD.main(["--file", str(self.f), "--metadata", str(mp), "--candidate", "m8", "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertIn(str(self.f), buf.getvalue())        # the console command has the real path
        saved = (out / "download_verification_m8.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.d), saved)               # the saved record does not
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(VD.main(["--file", str(self.f), "--metadata", str(mp), "--candidate", "zzz"]), 4)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(VD.main(["--file", str(self.f), "--metadata", str(self.d / "nope.json"), "--candidate", "m8"]), 4)
        mp.write_text("[]", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(VD.main(["--file", str(self.f), "--metadata", str(mp), "--candidate", "m8"]), 4)


def make_wheel(path, name="llama_cpp_python-0.3.36-cp312-cp312-win_amd64.whl", members=None, version="0.3.36", licence=True, dll=True, extra=()):
    members = dict(members or {})
    members["llama_cpp/__init__.py"] = b"# synthetic"
    members["llama_cpp_python-%s.dist-info/METADATA" % version] = (
        f"Metadata-Version: 2.1\nName: llama_cpp_python\nVersion: {version}\nLicense: MIT\nRequires-Python: >=3.8\nRequires-Dist: typing-extensions>=4.5.0\n"
        "Requires-Dist: numpy>=1.20.0\nClassifier: Programming Language :: Python :: 3.12\n\nbody").encode()
    if licence:
        members["llama_cpp_python-%s.dist-info/licenses/LICENSE.md" % version] = b"MIT License ..."
    if dll:
        members["llama_cpp/lib/llama.dll"] = make_pe(["KERNEL32.dll", "VCRUNTIME140.dll", "MSVCP140.dll", "ggml.dll"])
    p = Path(path) / name
    with zipfile.ZipFile(p, "w") as z:
        for n, b in members.items():
            z.writestr(n, b)
        for n, b in extra:
            z.writestr(n, b)
    return p


class WheelInspection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_well_formed_synthetic_wheel_has_no_problems_and_records_everything(self):
        w = make_wheel(self.d)
        sha = VW.sha256_bytes_stream(open(w, "rb"))
        r = VW.inspect(w, sha, "synthetic test value")
        self.assertEqual((r["status"], r["problems"]), ("NO_PROBLEMS_FOUND", []))
        self.assertEqual(r["sha256_computed_from_bytes"], sha)
        self.assertEqual(r["tags"], {"dist": "llama_cpp_python", "version": "0.3.36", "py": "cp312", "abi": "cp312", "plat": "win_amd64"})
        self.assertEqual(r["metadata"]["requires_python"], ">=3.8")
        self.assertIn("typing-extensions>=4.5.0", r["metadata"]["requires_dist"])
        self.assertTrue(any("LICENSE" in n for n in r["licence_files"]))
        dll = r["native_libraries"][0]
        self.assertEqual(dll["machine"], "0x8664")
        self.assertIn("VCRUNTIME140.dll", dll["imports"])
        self.assertEqual(r["vc_runtime_dlls_needed"], ["msvcp140.dll", "vcruntime140.dll"])
        self.assertTrue(any("nothing was installed" in n for n in r["notes"]))

    def test_no_expected_hash_is_a_record_not_a_verification(self):
        r = VW.inspect(make_wheel(self.d))
        self.assertTrue(any("not a verification" in n for n in r["notes"]))
        r = VW.inspect(make_wheel(self.d), "a" * 64)
        self.assertIn("HASH_MISMATCH", r["problems"])
        self.assertTrue(any("without a named source" in n for n in r["notes"]))
        self.assertIn("EXPECTED_HASH_MALFORMED", VW.inspect(make_wheel(self.d), "xyz")["problems"])

    def test_wrong_tags_names_and_platforms_are_problems(self):
        cases = {"llama_cpp_python-0.3.36-cp311-cp311-win_amd64.whl": "PYTHON_TAG_NOT_CP312", "llama_cpp_python-0.3.36-cp312-cp312-win32.whl": "PLATFORM_NOT_WIN_AMD64",
                 "llama_cpp_python-0.3.36-cp312-cp312-manylinux_2_17_x86_64.whl": "PLATFORM_NOT_WIN_AMD64", "other_pkg-0.3.36-cp312-cp312-win_amd64.whl": "NOT_LLAMA_CPP_PYTHON",
                 "llama_cpp_python-0.3.36-cp312-cp39-win_amd64.whl": "ABI_TAG_UNEXPECTED", "notawheel.zip": "FILE_NAME_NOT_A_WHEEL_NAME"}
        for name, code in cases.items():
            with self.subTest(name=name):
                self.assertIn(code, VW.inspect(make_wheel(self.d, name=name))["problems"])
        self.assertNotIn("ABI_TAG_UNEXPECTED", VW.inspect(make_wheel(self.d, name="llama_cpp_python-0.3.36-cp312-abi3-win_amd64.whl"))["problems"])

    def test_structure_problems(self):
        self.assertIn("NO_LICENCE_FILE_IN_WHEEL", VW.inspect(make_wheel(self.d, licence=False))["problems"])
        self.assertIn("NO_NATIVE_LIBRARIES_FOUND", VW.inspect(make_wheel(self.d, dll=False))["problems"])
        self.assertIn("METADATA_VERSION_DIFFERS_FROM_FILE_NAME", VW.inspect(make_wheel(self.d, version="0.3.99", name="llama_cpp_python-0.3.36-cp312-cp312-win_amd64.whl"))["problems"])
        r = VW.inspect(make_wheel(self.d, extra=[("../evil.txt", b"x"), ("/abs.txt", b"x")]))
        self.assertIn("UNSAFE_MEMBER_NAMES", r["problems"])
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.assertIn("DUPLICATE_MEMBERS", VW.inspect(make_wheel(self.d, extra=[("llama_cpp/__init__.py", b"again")]))["problems"])

    def test_native_file_problems(self):
        r = VW.inspect(make_wheel(self.d, members={"llama_cpp/lib/llama.dll": make_pe(["KERNEL32.dll"], machine=0x14C, pe32plus=False)}, dll=False))
        self.assertTrue(any(p.startswith("NOT_AMD64") for p in r["problems"]))
        r = VW.inspect(make_wheel(self.d, members={"llama_cpp/lib/llama.dll": b"MZ not really a dll"}, dll=False))
        self.assertTrue(any(p.startswith("NATIVE_FILE_NOT_INSPECTABLE") for p in r["problems"]))

    def test_unreadable_inputs_are_problems_not_crashes(self):
        bad = self.d / "llama_cpp_python-0.3.36-cp312-cp312-win_amd64.whl"
        bad.write_bytes(b"not a zip")
        self.assertIn("NOT_A_READABLE_ZIP", VW.inspect(bad)["problems"])
        self.assertIn("WHEEL_UNREADABLE", VW.inspect(self.d / "missing-1-cp312-cp312-win_amd64.whl")["problems"])

    def test_nothing_is_extracted(self):
        before = sorted(p.name for p in self.d.iterdir())
        w = make_wheel(self.d)
        VW.inspect(w)
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), sorted(before + [w.name]))

    def test_cli_writes_the_report_and_returns_4_on_problems(self):
        w = make_wheel(self.d)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(VW.main(["--wheel", str(w), "--out", str(self.d / "o")]), 0)
            self.assertEqual(VW.main(["--wheel", str(w), "--expected-sha256", "b" * 64]), 4)
        self.assertTrue((self.d / "o" / "wheel_inspection.json").is_file())


class FakeProvider(RG.PsutilProvider):
    def __init__(self, rss=lambda n: 1000, avail=lambda n: 10 ** 12, swap=lambda n: 0, free=10 ** 12):
        super().__init__()
        self.f_rss, self.f_avail, self.f_swap, self.free, self.n = rss, avail, swap, free, 0

    def tree_rss(self, pid):
        self.n += 1
        return self.f_rss(self.n)

    def available(self):
        return self.f_avail(self.n)

    def swap_used(self):
        return self.f_swap(self.n)

    def free_disk(self, path):
        return self.free


SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.log = self.d / "g.json"

    def go(self, cmd=SLEEPER, provider=None, **kw):
        kw.setdefault("min_free_gb", None)
        kw.setdefault("interval", 0.02)
        code = RG.run_guard(cmd, self.log, provider or FakeProvider(), **kw)
        return code, json.loads(self.log.read_text(encoding="utf-8"))

    def test_the_command_exit_code_passes_through(self):
        for rc in (0, 3, 4):
            code, rec = self.go([sys.executable, "-c", f"import sys; sys.exit({rc})"])
            with self.subTest(rc=rc):
                self.assertEqual((code, rec["command_exit_code"], rec["stop_reason"]), (rc, rc, None))

    def test_peak_memory_over_the_limit_stops_the_command(self):
        code, rec = self.go(provider=FakeProvider(rss=lambda n: 14_000_000_000 if n >= 3 else 1000), max_rss_gb=13)
        self.assertEqual((code, rec["stop_reason"]), (7, "PEAK_MEMORY_LIMIT_EXCEEDED"))
        self.assertEqual(rec["peak_tree_rss_bytes"], 14_000_000_000)
        self.assertLess(rec["seconds"], 20)

    def test_exactly_the_limit_is_allowed(self):
        code, rec = self.go([sys.executable, "-c", "pass"], provider=FakeProvider(rss=lambda n: 13_000_000_000), max_rss_gb=13)
        self.assertEqual(code, 0)

    def test_sustained_low_available_memory_stops_but_a_blip_does_not(self):
        code, rec = self.go(provider=FakeProvider(avail=lambda n: 100 if n >= 2 else 10 ** 12), sustain=3)
        self.assertEqual((code, rec["stop_reason"]), (7, "LOW_AVAILABLE_MEMORY_SUSTAINED"))
        blip = FakeProvider(avail=lambda n: 100 if n in (2, 3) else 10 ** 12)
        code, rec = self.go([sys.executable, "-c", "import time; time.sleep(0.4)"], provider=blip, sustain=3)
        self.assertEqual((code, rec["stop_reason"]), (0, None))

    def test_sustained_swap_growth_stops(self):
        code, rec = self.go(provider=FakeProvider(swap=lambda n: 1_000_000_000 if n > 1 else 0), swap_growth_gb=0.5, sustain=3)
        self.assertEqual((code, rec["stop_reason"]), (7, "SWAP_GROWTH_SUSTAINED"))

    def test_time_limit_stops(self):
        code, rec = self.go(max_seconds=0.3)
        self.assertEqual((code, rec["stop_reason"]), (7, "TIME_LIMIT_EXCEEDED"))

    def test_too_little_free_disk_refuses_to_start_the_command(self):
        popen = mock.Mock(side_effect=AssertionError("must not start"))
        code, rec = self.go(provider=FakeProvider(free=24_999_999_999), min_free_gb=25, free_path="X:", popen=popen)
        self.assertEqual((code, rec["started"]), (8, False))
        self.assertIn("below the required", rec["refused"])
        code, rec = self.go([sys.executable, "-c", "pass"], provider=FakeProvider(free=25_000_000_000), min_free_gb=25, free_path="X:")
        self.assertEqual(code, 0)
        code, rec = self.go(provider=FakeProvider(), min_free_gb=25, free_path=None, popen=popen)
        self.assertEqual(code, 8)

    def test_fail_closed_without_psutil_or_with_bad_arguments(self):
        with mock.patch.dict(sys.modules, {"psutil": None}):
            code = RG.run_guard(SLEEPER, self.log, None, min_free_gb=None)
        self.assertEqual(code, 8)
        self.assertIn("psutil", json.loads(self.log.read_text())["refused"])
        for kw in ({"sustain": 0}, {"interval": 0}, {"max_rss_gb": 0}):
            self.assertEqual(RG.run_guard(SLEEPER, self.log, FakeProvider(), min_free_gb=None, **kw), 8)
        self.assertEqual(RG.run_guard([], self.log, FakeProvider(), min_free_gb=None), 8)
        self.assertEqual(RG.run_guard(["/definitely/not/a/program"], self.log, FakeProvider(), min_free_gb=None), 8)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(RG.main(["--log", str(self.log)]), 8)

    def test_an_unreadable_limit_means_no_guard_no_run(self):
        class Broken(FakeProvider):
            def swap_used(self):
                raise OSError("denied")
        self.assertEqual(RG.run_guard(SLEEPER, self.log, Broken(), min_free_gb=None), 8)

    def test_the_log_does_not_contain_the_arguments(self):
        code, rec = self.go([sys.executable, "-c", "pass  # SECRET-TOKEN-123"])
        self.assertNotIn("SECRET-TOKEN-123", self.log.read_text())
        self.assertIn("arguments", rec["command"])

    def test_an_interrupted_guard_kills_the_command(self):
        killed = []

        class P(FakeProvider):
            def kill_tree(self, pid):
                killed.append(pid)
                super().kill_tree(pid)

        def sleeper(_):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            RG.run_guard(SLEEPER, self.log, P(), min_free_gb=None, interval=0.02, sleep=sleeper)
        self.assertEqual(len(killed), 1)
        self.assertEqual(json.loads(self.log.read_text())["stop_reason"], "GUARD_INTERRUPTED")

    def test_real_psutil_measures_a_real_child_and_stops_it(self):
        child = [sys.executable, "-c", "x = bytearray(250_000_000); import time; time.sleep(60)"]
        code, rec = self.go(child, provider=RG.PsutilProvider(), max_rss_gb=0.1)
        self.assertEqual((code, rec["stop_reason"]), (7, "PEAK_MEMORY_LIMIT_EXCEEDED"))
        self.assertGreater(rec["peak_tree_rss_bytes"], 100_000_000)


class Summary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def real_intel(self, label="m"):
        res = IB.run("unused", out=self.d / "raw", sampled_runs=0, engine=FakeEngine(GOLD), label=label)
        return res

    def as_real(self, res, **over):
        r = json.loads(json.dumps(res))
        r["simulated"] = False
        r["environment"]["runtime"] = {"available": True, "version": "0.0-test"}      # a real run records the runtime version; the Linux fake engine cannot
        r.update(over)
        return r

    def test_simulated_runs_are_excluded_and_never_counted(self):
        sim = self.real_intel("sim")
        o = SR.summarize([("intel_sim.json", sim)], [])
        self.assertEqual((o["usable_results"], len(o["excluded"])), (0, 1))
        self.assertIn("SIMULATED", o["excluded"][0]["reason"])

    def test_complete_results_fill_the_separate_sections_and_there_is_no_overall_score(self):
        a = self.as_real(self.real_intel("a"))
        o = SR.summarize([("intel_a.json", a)], [])
        self.assertEqual(o["usable_results"], 1)
        self.assertEqual(o["A_functional_correctness"][0]["greedy_passed"], a["summary"]["greedy_passed"])
        self.assertEqual(o["B_language_quality"]["status"], "NOT MEASURED")
        self.assertEqual(o["C_tool_safety"][0]["tool_runs"], a["summary"]["tools"]["runs"])
        self.assertFalse(any(k in o for k in ("score", "overall_score", "rating", "intelligence")))
        md = SR.to_md(o)
        self.assertIn("No overall score", md)
        self.assertIn("not an intelligence rating", json.dumps(o))

    def test_incomplete_subset_failed_and_invalid_runs_are_excluded_with_the_reason(self):
        for status in ("incomplete", "subset", "failed", "invalid"):
            r = self.as_real(self.real_intel(status), status=status)
            o = SR.summarize([("x.json", r)], [])
            with self.subTest(status=status):
                self.assertEqual(o["usable_results"], 0)
                self.assertIn(status, o["excluded"][0]["reason"])

    def test_differing_settings_hardware_or_suite_are_flagged_not_comparable(self):
        a, b = self.as_real(self.real_intel("a")), self.as_real(self.real_intel("b"))
        self.assertTrue(SR.summarize([("a.json", a), ("b.json", b)], [])["comparability"]["verdict"].startswith("COMPARABLE on the recorded fields"))
        b["suite_sha256"] = "different"
        b["settings"]["max_tokens_default"] = 256
        b["environment"]["cpu_threads"] = 99
        o = SR.summarize([("a.json", a), ("b.json", b)], [])
        diffs = o["comparability"]["differences"]["bench-intel"]
        self.assertEqual(sorted(diffs), ["cpu_threads", "max_tokens", "suite_sha256"])
        self.assertTrue(o["comparability"]["verdict"].startswith("NOT COMPARABLE"))
        self.assertIn("power plan", o["comparability"]["unrecorded_by_any_file"])

    def test_security_perf_and_guard_files_are_separated(self):
        sec = {"kind": "security-benchmark", "suite": "sec-v1.1", "status": "complete", "simulated": False, "label": "s", "model": {"id": "m"},
               "summary": {"raw_gate": "FAIL", "app_gate": "PASS", "raw_failures_total": 2, "app_failures_total": 0, "crashes": 0}}
        perf = {"kind": "inference-performance", "status": "complete", "model": {"id": "m"}, "execution": {"n_ctx": 8192, "mode": "cpu"}, "load": {}, "runs": [],
                "summary": {"valid_runs": 3, "measured_runs": 3, "load_seconds": 4.2, "peak_memory_mb": 6100.0,
                            "time_to_first_token_seconds": {"median": 1.1}, "sustained_tokens_per_second": {"median": 7.5}, "overall_tokens_per_second": {"median": 6.9}}}
        guard = {"kind": "resource-guard", "started": True, "stop_reason": None, "peak_tree_rss_bytes": 7_000_000_000, "command_exit_code": 0, "guard_exit_code": 0}
        o = SR.summarize([("sec_s.json", sec), ("perf.json", perf), ("guard_x.json", guard)], [])
        self.assertEqual(o["C_tool_safety"][0]["raw_gate"], "FAIL")
        self.assertEqual(o["D_performance"][0]["peak_process_memory_mb"], 6100.0)
        self.assertEqual(o["guards"][0]["peak_tree_rss_bytes"], 7_000_000_000)
        self.assertEqual(o["usable_results"], 2)
        md = SR.to_md(o)
        self.assertIn("raw_gate=FAIL", md)

    def test_unreadable_files_are_reported_and_exit_4_when_nothing_is_usable(self):
        (self.d / "bad.json").write_text("{not json", encoding="utf-8")
        out = self.d / "out"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = SR.main(["--in", str(self.d), "--out", str(out)])
        self.assertEqual(rc, 4)
        o = json.loads((out / "stage5_summary.json").read_text())
        self.assertEqual(o["unreadable_files"], ["bad.json"])

    def test_cli_end_to_end_with_a_usable_result(self):
        a = self.as_real(self.real_intel("a"))
        (self.d / "in").mkdir()
        (self.d / "in" / "intel_a.json").write_text(json.dumps(a), encoding="utf-8")
        out = self.d / "out"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(SR.main(["--in", str(self.d / "in"), "--out", str(out)]), 0)
        self.assertTrue((out / "stage5_summary.md").is_file())


class AuditRegressions(unittest.TestCase):
    """Defects found by the pre-Windows audit. The tests were written together with the fixes; they were NOT run against the pre-fix code."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    # --- metadata
    def test_dot_segments_are_not_repository_names(self):
        for bad in ("../x", "./x", "a/..", "a/.", ".hidden/x", "a/.hidden"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                VM.api_url(bad)

    def test_conflicting_licences_and_malformed_revisions_are_problems(self):
        c = by_id("qwen3-8b-official")
        r = VM.evaluate(c, api_record(c, tags=["license:apache-2.0", "license:other"]), None, WHEN)
        self.assertIn("LICENSE_CONFLICT", r["problems"])
        r = VM.evaluate(c, api_record(c, cardData={"license": "mit"}), None, WHEN)
        self.assertIn("LICENSE_CONFLICT", r["problems"])          # card says mit, tag says apache-2.0
        r = VM.evaluate(c, api_record(c, sha="main"), None, WHEN)
        self.assertIn("REVISION_MALFORMED", r["problems"])

    def test_offline_replay_never_presents_the_replay_time_as_the_retrieval_time(self):
        by_repo = {x["repo"]: x for x in VM.CANDIDATES}
        sizes = MetadataRun.sizes
        fetch = lambda repo: api_record(by_repo[repo], size=sizes[by_repo[repo]["id"]])        # noqa: E731
        live, replay, orphan = self.d / "live", self.d / "replay", self.d / "orphan"
        VM.run(live, fetch=fetch, now=lambda: "2026-01-01T00:00:00Z")
        doc, code = VM.run(replay, from_raw=live / "raw", now=lambda: "2030-05-05T00:00:00Z")
        self.assertEqual((code, doc["mode"], doc["retrieved_at_utc"]), (0, "offline-replay", "2026-01-01T00:00:00Z"))
        self.assertIn("OFFLINE REPLAY", doc["results"][0]["retrieval_method"])
        (orphan / "raw").mkdir(parents=True)
        for f in (live / "raw").glob("*.api.json"):
            (orphan / "raw" / f.name).write_text(f.read_text())
        doc, code = VM.run(self.d / "o2", from_raw=orphan / "raw", now=lambda: "2030-05-05T00:00:00Z")
        self.assertTrue(doc["retrieved_at_utc"].startswith("UNKNOWN"))
        self.assertNotIn("2030", json.dumps(doc["results"][0]["retrieved_at_utc"]))

    # --- downloaded file
    def test_generated_command_is_powershell_safe_and_complete(self):
        weird = r"C:\Users\Jo's $env:TEMP\`dl (1)\m.gguf"
        cmd = VD.add_gguf_command("qwen3-8b-official", "a" * 64, 5, weird, r"C:\jarvis eval\data")
        self.assertIn("'C:\\Users\\Jo''s $env:TEMP\\`dl (1)\\m.gguf'", cmd)       # single-quoted, ' doubled, $ and ` stay literal
        self.assertIn("--id qwen3-8b-official", cmd)
        self.assertIn("'C:\\jarvis eval\\data'", cmd)
        self.assertNotIn('"', cmd)
        self.assertNotRegex(cmd.replace(weird.replace("'", "''"), ""), r"<ID>")
        self.assertEqual(VD.add_gguf_command("Qwen_X.Y", "b" * 64, 1).split("--id ")[1].split()[0], "qwen_x-y")       # a valid ModelStore id

    def test_a_pass_records_which_metadata_it_was_checked_against(self):
        t = DownloadedFile("test_cli_exit_codes_and_saved_json")
        t.setUp()
        t.addCleanup = lambda *a, **k: None
        t.meta["results"] = [t.record(repo="Qwen/Qwen3-8B-GGUF", revision="a" * 40, retrieved_at_utc=WHEN, license_tag="apache-2.0")]
        r = t.v()
        self.assertEqual(r["status"], "PASS")
        self.assertEqual((r["metadata_record"]["repo"], r["metadata_record"]["revision"]), ("Qwen/Qwen3-8B-GGUF", "a" * 40))
        t.tmp.cleanup()

    # --- wheel
    def test_a_lying_zip_header_cannot_make_the_inspector_read_a_huge_member(self):
        w = make_wheel(self.d)
        orig = VW.MAX_NATIVE
        try:
            VW.MAX_NATIVE = 100          # the synthetic DLL is bigger than this
            r = VW.inspect(w)
        finally:
            VW.MAX_NATIVE = orig
        self.assertTrue(any(p.startswith("NATIVE_FILE_NOT_INSPECTABLE") for p in r["problems"]))
        self.assertEqual(r["native_libraries"][0]["inspect_error"], "TooBig")
        self.assertEqual(len(VW.read_capped(zipfile.ZipFile(w), zipfile.ZipFile(w).getinfo("llama_cpp/__init__.py"), 1000)), len(b"# synthetic"))

    def test_metadata_name_must_be_llama_cpp_python_and_damage_is_not_a_traceback(self):
        w = self.d / "x"
        w.mkdir()
        r = VW.inspect(make_wheel(w, members={}, version="0.3.36"))
        self.assertNotIn("METADATA_NAME_NOT_LLAMA_CPP_PYTHON", r["problems"])
        evil = w / "e"
        evil.mkdir()
        p = make_wheel(evil)
        with zipfile.ZipFile(p) as zin, zipfile.ZipFile(evil / "new.whl", "w") as zout:
            for i in zin.infolist():
                data = zin.read(i)
                if i.filename.endswith("METADATA"):
                    data = data.replace(b"Name: llama_cpp_python", b"Name: totally_other")
                zout.writestr(i.filename, data)
        os.replace(evil / "new.whl", p)
        self.assertIn("METADATA_NAME_NOT_LLAMA_CPP_PYTHON", VW.inspect(p)["problems"])
        data = bytearray(p.read_bytes())
        i = data.find(b"typing-extensions")      # not compressed (ZIP_STORED): damage inside a member leaves a CRC error on read
        data[i:i + 4] = b"XXXX"
        p.write_bytes(bytes(data))
        r = VW.inspect(p)         # must return a record, not raise
        self.assertEqual(r["status"], "PROBLEMS")
        self.assertIn("METADATA_UNREADABLE", r["problems"])

    # --- guard
    def test_non_finite_or_non_positive_limits_are_refused_not_ignored(self):
        log = self.d / "g.json"
        popen = mock.Mock(side_effect=AssertionError("must not start"))
        for kw in ({"max_rss_gb": float("nan")}, {"max_rss_gb": float("inf")}, {"min_available_gb": 0}, {"swap_growth_gb": -1}, {"max_seconds": float("nan")},
                   {"max_seconds": 0}, {"interval": float("nan")}, {"min_free_gb": float("nan"), "free_path": "x"}, {"min_free_gb": -5, "free_path": "x"}):
            with self.subTest(kw=kw):
                kw.setdefault("min_free_gb", None)
                self.assertEqual(RG.run_guard(SLEEPER, log, FakeProvider(), popen=popen, **kw), 8)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(RG.main(["--max-rss-gb", "nan", "--log", str(log), "--", sys.executable, "-c", "pass"]), 8)

    def test_a_command_that_cannot_be_measured_is_stopped_not_run_unguarded(self):
        log = self.d / "g.json"
        code = RG.run_guard(SLEEPER, log, FakeProvider(rss=lambda n: None), min_free_gb=None, interval=0.02, sustain=3)
        rec = json.loads(log.read_text())
        self.assertEqual((code, rec["stop_reason"]), (7, "MEASUREMENT_FAILED"))

    def test_the_log_exists_while_the_command_runs_and_is_marked_finished_at_the_end(self):
        log = self.d / "g.json"
        seen = []

        def spy(_):
            seen.append(json.loads(log.read_text()))
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            RG.run_guard(SLEEPER, log, FakeProvider(), min_free_gb=None, interval=0.02, sleep=spy)
        self.assertFalse(seen[0]["finished"])
        self.assertTrue(seen[0]["started"])
        code = RG.run_guard([sys.executable, "-c", "pass"], log, FakeProvider(), min_free_gb=None, interval=0.02)
        self.assertTrue(json.loads(log.read_text())["finished"])

    def test_an_unwritable_log_does_not_change_the_commands_exit_code(self):
        blocker = self.d / "file"
        blocker.write_text("x")
        code = RG.run_guard([sys.executable, "-c", "import sys; sys.exit(4)"], blocker / "g.json", FakeProvider(), min_free_gb=None, interval=0.02)
        self.assertEqual(code, 4)

    def test_psutil_provider_reports_unreadable_as_none_not_zero(self):
        class FakePs:
            class Error(Exception):
                pass

            def Process(self, pid):
                raise self.Error()
        pp = RG.PsutilProvider.__new__(RG.PsutilProvider)
        pp.ps = FakePs()
        self.assertIsNone(pp.tree_rss(1))

    def test_the_guard_documents_what_it_does_not_guarantee(self):
        for needle in ("WHAT THIS DOES NOT GUARANTEE", "hard kill", "SYSTEM-WIDE", "ONCE", "Do NOT wrap `models fetch`"):
            self.assertIn(needle, RG.__doc__)

    # --- summarizer
    def real(self, label, **over):
        r = json.loads(json.dumps(IB.run("unused", out=self.d / ("raw" + label), sampled_runs=0, engine=FakeEngine(GOLD), label=label)))
        r["simulated"] = False
        r["environment"]["runtime"] = {"available": True, "version": "0.0-test"}
        r["model"] = {"id": label, "sha256": "c" * 64, "file_size_bytes": 10}
        r.update(over)
        return r

    def test_missing_metadata_in_every_file_is_not_agreement(self):
        a, b = self.real("a"), self.real("b")
        for r in (a, b):
            r["environment"].pop("processor")
            r["environment"]["cpu_threads"] = None
            r["environment"]["runtime"] = {"available": False}
        o = SR.summarize([("a.json", a), ("b.json", b)], [])
        self.assertTrue(o["comparability"]["verdict"].startswith("NOT COMPARABLE"))
        self.assertEqual(sorted(o["comparability"]["missing_fields"]["bench-intel"]), ["cpu_threads", "processor", "runtime_version"])
        self.assertIn("MISSING in", SR.to_md(o))

    def test_one_result_alone_is_not_called_comparable(self):
        o = SR.summarize([("a.json", self.real("a"))], [])
        self.assertTrue(o["comparability"]["verdict"].startswith("NOTHING TO COMPARE"))

    def test_perf_runs_are_compared_too(self):
        def perf(**env):
            return {"kind": "inference-performance", "status": "complete", "model": {"id": "m", "sha256": "c" * 64},
                    "settings": {"max_tokens": 128, "warmup_runs": 1, "measured_runs": 3, "prompt": "p", "system": "s"},
                    "execution": {"n_ctx": 8192, "mode": "cpu"}, "load": {}, "runs": [],
                    "environment": {"os": "W", "cpu_name": "c", "cpu_logical_threads": 8, "ram_gb": 16, "runtime": {"version": "1"}, **env},
                    "summary": {"valid_runs": 3, "measured_runs": 3, "load_seconds": 1.0}}
        o = SR.summarize([("p1.json", perf()), ("p2.json", perf())], [])
        self.assertTrue(o["comparability"]["verdict"].startswith("COMPARABLE"))
        o = SR.summarize([("p1.json", perf()), ("p2.json", perf(ram_gb=32))], [])
        self.assertIn("ram_gb", o["comparability"]["differences"]["perf"])
        o = SR.summarize([("p1.json", perf()), ("p2.json", perf(cpu_name=None))], [])
        self.assertIn("cpu_name", o["comparability"]["missing_fields"]["perf"])

    def test_results_are_traced_to_a_passed_download_verification(self):
        h = "d" * 64
        good = self.real("good")
        good["model"]["sha256"] = h
        unknown = self.real("unk")
        nohash = self.real("none")
        nohash["model"]["sha256"] = None
        ver = {"kind": "downloaded-file-verification", "candidate": "qwen3-8b-official", "status": "PASS", "computed_sha256": h, "hash_computed_from_bytes": True,
               "human_page_hash_compared": False}
        failed = dict(ver, status="FAIL", computed_sha256="c" * 64)
        o = SR.summarize([("good.json", good), ("unk.json", unknown), ("none.json", nohash), ("download_verification_x.json", ver), ("download_verification_y.json", failed)], [])
        tr = {r["result"]: r["trace"] for r in o["E_model_file_provenance"]["per_result"] if r["result"] in ("good.json", "unk.json", "none.json")}
        self.assertTrue(tr["good.json"].startswith("matches a PASSED"))
        self.assertTrue(tr["unk.json"].startswith("hash NOT found"))          # its hash only appears in a FAILED verification
        self.assertTrue(tr["none.json"].startswith("NO model hash recorded"))

    def test_unfinished_guard_logs_are_flagged_and_a_broken_link_does_not_crash_loading(self):
        o = SR.summarize([("guard_x.json", {"kind": "resource-guard", "finished": False, "started": True})], [])
        self.assertFalse(o["guards"][0]["finished"])
        self.assertIn("WARNING", SR.to_md(o))
        (self.d / "in").mkdir()
        try:
            (self.d / "in" / "dangling.json").symlink_to(self.d / "nowhere.json")
        except OSError:
            self.skipTest("symlinks not available")
        docs, bad = SR.load([self.d / "in"])
        self.assertEqual((docs, bad), ([], ["dangling.json"]))


class PlaybookCommands(unittest.TestCase):
    """The playbook's commands must name real scripts and real options. This compares TEXT with argparse on Linux; it does not run anything on Windows."""
    text = (ROOT / "docs" / "STAGE5_WINDOWS_PLAYBOOK.md").read_text(encoding="utf-8")

    @staticmethod
    def help_of(*argv):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        r = subprocess.run([sys.executable, "-W", "ignore", *argv, "--help"], capture_output=True, text=True, env=env, timeout=60, cwd=str(ROOT))
        return r.stdout + r.stderr

    def test_every_script_option_used_in_the_playbook_exists(self):
        seen = 0
        for m in re.finditer(r"scripts\\(\w+)\.py([^\n`]*)", self.text):
            name, rest = m.group(1), m.group(2)
            if not (ROOT / "scripts" / f"{name}.py").is_file():
                self.fail(f"playbook names a script that does not exist: {name}")
            rest = rest.split(" -- ")[0]
            opts = re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", rest)
            if not opts or name == "check_vc_runtime":
                continue
            helptext = self.help_of("scripts/" + name + ".py")
            for o in opts:
                seen += 1
                self.assertIn(o, helptext, f"{name}.py has no option {o}")
        self.assertGreater(seen, 15)

    def test_every_jarvis_option_used_in_the_playbook_exists(self):
        seen = 0
        for m in re.finditer(r"-m jarvis --data-dir \S+ ([a-z-]+)( [a-z-]+)?([^\n`;]*)", self.text):
            sub, sub2, rest = m.group(1), (m.group(2) or "").strip(), m.group(3)
            argv = ["-m", "jarvis", sub] + ([sub2] if sub == "models" and re.fullmatch(r"[a-z-]+", sub2 or "") and not sub2.startswith("<") else [])
            helptext = self.help_of(*argv)
            self.assertNotIn("invalid choice", helptext, f"jarvis {' '.join(argv[2:])}")
            for o in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", rest.split(" -- ")[0]):
                seen += 1
                self.assertIn(o, helptext, f"jarvis {' '.join(argv[2:])} has no option {o}")
        self.assertGreater(seen, 10)

    def test_the_generated_add_gguf_command_uses_real_options_and_the_playbook_says_to_run_it(self):
        helptext = self.help_of("-m", "jarvis", "models", "add-gguf")
        cmd = VD.add_gguf_command("qwen3-8b-official", "a" * 64, 5, "C:/x.gguf", "C:/d")
        for o in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", cmd):
            if o != "--data-dir":           # global option, placed before the subcommand
                self.assertIn(o, helptext)
        self.assertIn("--no-select", self.text)
        self.assertIn("Do NOT add the [local] extra", self.text)

    def test_the_playbook_does_not_install_the_runtime_extra_or_use_no_index_for_the_wheel(self):
        self.assertNotIn("--no-index", self.text)
        code = "\n".join(re.sub(r"\s#.*$", "", ln) for ln in self.text.splitlines())        # ignore trailing comments, which warn against it
        self.assertNotRegex(code, r"pip install[^\n]*\[[^\]]*local")


class Consistency(unittest.TestCase):
    def test_the_documented_limits_match_the_code_constants(self):
        self.assertEqual(VM.DOWNLOAD_BUDGET_BYTES, 20_000_000_000)
        self.assertEqual(VM.MIN_FREE_BYTES, 25_000_000_000)
        self.assertEqual(RG.GB, 1_000_000_000)
        doc = (ROOT / "docs" / "STAGE5_WINDOWS_PLAYBOOK.md").read_text(encoding="utf-8")
        for needle in ("25 GB", "20 GB", "13 GB", "resource_guard.py"):
            self.assertIn(needle, doc)

    def test_the_tools_never_touch_the_catalog_or_the_download_code(self):
        for name in ("verify_model_metadata", "verify_downloaded_model", "verify_wheel", "resource_guard", "summarize_stage5_results"):
            src = (ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("download_gguf", src, name)
            self.assertNotIn("catalog.BUILTIN", src, name)
            self.assertIsNone(re.search(r"urlopen\(|requests\.|/resolve/\{", src.replace('"""', "")), name)


if __name__ == "__main__":
    unittest.main()
