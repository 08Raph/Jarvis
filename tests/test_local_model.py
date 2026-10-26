"""Tests for the local open-weight model path. The runtime (llama.cpp) and real model weights were NOT available in the
authoring sandbox, so every test here uses a FAKE runtime or a synthetic GGUF header. They verify OUR logic (file validation,
integrity, download safety, prompt handling, honesty on failure), not real inference quality or speed."""
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis import cli
from jarvis.assistant import LocalAssistant, NoActiveModel
from jarvis.config import Config
from jarvis.hardware import Hardware, assess_gguf
from jarvis.inference import gguf_engine as ge
from jarvis.models import catalog, gguf
from jarvis.models.manager import IntegrityError, ModelStore
from jarvis.net.policy import NetworkDisabled, NetworkPolicy


def make_gguf(path, version=3, kvs=None, pad=1000):
    """Synthetic GGUF: header + metadata + padding. Mirrors the public spec as understood by the author."""
    def s(x): b = x.encode(); return struct.pack("<Q", len(b)) + b
    kvs = kvs if kvs is not None else {"general.architecture": "qwen3", "general.name": "Fake 1.7B",
                                      "general.license": "apache-2.0", "qwen3.context_length": 40960}
    body = b""
    for k, v in kvs.items():
        body += s(k) + (struct.pack("<I", 8) + s(v) if isinstance(v, str) else struct.pack("<I", 4) + struct.pack("<I", v))
    body += s("tokenizer.ggml.tokens") + struct.pack("<I", 9) + struct.pack("<I", 8) + struct.pack("<Q", 2) + s("a") + s("b")
    body += s("tokenizer.ggml.scores") + struct.pack("<I", 9) + struct.pack("<I", 6) + struct.pack("<Q", 3) + struct.pack("<fff", 1, 2, 3)
    n = len(kvs) + 2
    Path(path).write_bytes(b"GGUF" + struct.pack("<I", version) + struct.pack("<Q", 0) + struct.pack("<Q", n) + body + b"\0" * pad)
    return path


class FakeLlama:
    instances = []

    def __init__(self, model_path, n_ctx, n_threads, verbose):
        self.kw = dict(model_path=model_path, n_ctx=n_ctx, n_threads=n_threads)
        self.calls = []
        FakeLlama.instances.append(self)

    def tokenize(self, b, add_bos=False): return list(range(len(b.split())))

    def create_chat_completion(self, messages, max_tokens, temperature, stream=False):
        self.calls.append(messages)
        text = "<think>hidden reasoning</think>The answer is 4."
        if not stream:
            return {"choices": [{"message": {"content": text}}]}
        return iter({"choices": [{"delta": {"content": text[i:i + 3]}}]} for i in range(0, len(text), 3))


class GGUFReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_reads_metadata_and_skips_big_arrays(self):
        h = gguf.read_header(make_gguf(self.d / "a.gguf"))
        self.assertEqual(gguf.summarize(h), {"name": "Fake 1.7B", "architecture": "qwen3", "license": "apache-2.0",
                                              "context_length": 40960, "size_label": None})

    def test_rejects_bad_files(self):
        (self.d / "t.gguf").write_bytes(b"not a model at all")
        with self.assertRaises(gguf.GGUFError): gguf.read_header(self.d / "t.gguf")
        with self.assertRaises(gguf.GGUFError): gguf.read_header(make_gguf(self.d / "v.gguf", version=99))
        good = make_gguf(self.d / "g.gguf")
        (self.d / "trunc.gguf").write_bytes(good.read_bytes()[:30])
        with self.assertRaises(gguf.GGUFError): gguf.read_header(self.d / "trunc.gguf")
        (self.d / "huge.gguf").write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 10 ** 12))
        with self.assertRaises(gguf.GGUFError): gguf.read_header(self.d / "huge.gguf")
        (self.d / "str.gguf").write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 2 ** 40))
        with self.assertRaises(gguf.GGUFError): gguf.read_header(self.d / "str.gguf")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = Path(self.tmp.name)
        self.store = ModelStore(self.d / "models")
        self.f = make_gguf(self.d / "m.gguf")
        self.sha = hashlib.sha256(self.f.read_bytes()).hexdigest()
    def tearDown(self): self.tmp.cleanup()

    def test_install_select_verify_and_license_recorded(self):
        meta = self.store.install_gguf(self.f, "fake", "Apache-2.0", "License: see URL", "https://x.example", self.sha)
        self.assertEqual((meta["kind"], meta["verified_origin"]), ("gguf", True))
        self.store.set_active("fake")
        self.assertEqual(self.store.kind("fake"), "gguf")
        self.assertTrue(self.store.verify("fake")[0])
        self.assertIn("Apache-2.0", (self.store.path("fake") / "LICENSE.txt").read_text())
        self.assertTrue(self.f.exists())                       # copied, not moved, for user-supplied files

    def test_refuses_unknown_license_wrong_hash_and_non_gguf(self):
        with self.assertRaises(IntegrityError): self.store.install_gguf(self.f, "x", "", "text")
        with self.assertRaises(IntegrityError): self.store.install_gguf(self.f, "x", "MIT", "")
        with self.assertRaises(IntegrityError): self.store.install_gguf(self.f, "x", "MIT", "t", expected_sha256="0" * 64)
        bad = self.d / "bad.gguf"; bad.write_bytes(b"MZ not gguf")
        with self.assertRaises(gguf.GGUFError): self.store.install_gguf(bad, "x", "MIT", "t")
        self.assertEqual(self.store.installed(), [])
        self.assertEqual([p.name for p in (self.d / "models").iterdir()], [])   # no leftovers

    def test_quick_and_full_verify_detect_tampering(self):
        self.store.install_gguf(self.f, "fake", "MIT", "t", expected_sha256=self.sha)
        g = self.store.path("fake") / "model.gguf"
        data = bytearray(g.read_bytes()); data[-1] ^= 1; g.write_bytes(bytes(data))   # same size, one flipped byte
        self.assertTrue(self.store.verify("fake", quick=True)[0])                      # quick is size/signature only
        self.assertFalse(self.store.verify("fake")[0])                                  # full hash catches it
        g.write_bytes(g.read_bytes()[:-5])
        self.assertFalse(self.store.verify("fake", quick=True)[0])

    def test_upgrade_keeps_previous_for_rollback(self):
        self.store.install_gguf(self.f, "fake", "MIT", "t")
        f2 = make_gguf(self.d / "m2.gguf", pad=2000)
        self.store.install_gguf(f2, "fake", "MIT", "t")
        self.assertTrue(self.store.rollback("fake"))
        self.assertEqual(self.store.path("fake").joinpath("model.gguf").stat().st_size, self.f.stat().st_size)


class FakeHF(BaseHTTPRequestHandler):
    payload = b""
    sha = None
    redirect = False
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/api/models/"):
            sib = {"rfilename": "Fake-Q4_K_M.gguf", "size": len(FakeHF.payload)}
            if FakeHF.sha: sib["lfs"] = {"sha256": FakeHF.sha, "size": len(FakeHF.payload)}
            body = json.dumps({"siblings": [{"rfilename": "README.md"}, sib]}).encode()
        elif FakeHF.redirect:
            self.send_response(302); self.send_header("Location", "http://localhost:1/evil"); self.end_headers(); return
        else:
            body = FakeHF.payload
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = Path(self.tmp.name)
        f = make_gguf(self.d / "src.gguf", pad=5000)
        FakeHF.payload, FakeHF.sha, FakeHF.redirect = f.read_bytes(), hashlib.sha256(f.read_bytes()).hexdigest(), False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHF)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.pol = NetworkPolicy(allow_insecure_localhost=True)
        self.store = ModelStore(self.d / "models", self.pol)
        self.model = catalog.CatalogModel("fake-q4", "Fake", "pub/fake-GGUF", r"(?i)q4_k_m.*\.gguf$", "Apache-2.0",
                                          "https://example.org/l", approx_size_bytes=len(FakeHF.payload), min_ram_gb=1, context_tokens=2048)
    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.tmp.cleanup()

    def dl(self, **kw):
        with self.pol.grant_once("model_download"):
            return self.store.download_gguf(self.model, base=self.base, hosts=("127.0.0.1",), **kw)

    def test_download_verifies_host_published_checksum_and_installs(self):
        meta = self.dl()
        self.assertTrue(meta["verified_origin"])
        self.assertEqual(meta["context_tokens"], 2048)
        self.assertTrue(self.store.verify(meta["id"])[0])
        self.assertEqual([p.name for p in (self.d / "models").iterdir() if p.name.startswith(".")], [])   # .part cleaned/moved

    def test_wrong_checksum_installs_nothing(self):
        FakeHF.sha = "0" * 64
        with self.assertRaises(IntegrityError): self.dl()
        self.assertEqual(self.store.installed(), [])

    def test_missing_checksum_needs_explicit_trust(self):
        FakeHF.sha = None
        with self.assertRaises(IntegrityError): self.dl()
        self.assertEqual(self.dl(trust_unpinned=True)["verified_origin"], False)

    def test_oversized_download_aborted(self):
        self.model = catalog.CatalogModel(**{**self.model.__dict__, "approx_size_bytes": 100})
        with self.assertRaises(IntegrityError): self.dl()
        self.assertEqual(self.store.installed(), [])

    def test_disabled_by_default_and_host_allowlist_and_redirects(self):
        with self.assertRaises(NetworkDisabled):
            self.store.download_gguf(self.model, base=self.base, hosts=("127.0.0.1",))      # feature off: no socket
        with self.assertRaises(NetworkDisabled):
            with self.pol.grant_once("model_download"):
                self.store.download_gguf(self.model, base=self.base, hosts=("huggingface.co",))   # wrong host refused
        FakeHF.redirect = True
        with self.assertRaises(Exception):
            self.dl()                                                                         # redirect to another host blocked
        self.assertEqual(self.store.installed(), [])

    def test_repo_with_two_matches_is_refused(self):
        m = catalog.CatalogModel(**{**self.model.__dict__, "file_regex": r".*"})
        with self.assertRaises(LookupError):
            with self.pol.grant_once("model_download"):
                catalog.resolve_file(self.pol, m, self.base, ("127.0.0.1",))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = Path(self.tmp.name)
        make_gguf(self.d / "model.gguf")
        FakeLlama.instances.clear()
    def tearDown(self): self.tmp.cleanup()

    def test_chat_strips_thinking_and_adds_no_think_for_qwen3(self):
        e = ge.GGUFEngine(self.d, llama_factory=FakeLlama, no_think_suffix=True)
        out = e.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])
        self.assertEqual(out, "The answer is 4.")
        self.assertTrue(FakeLlama.instances[0].calls[0][-1]["content"].endswith("/no_think"))
        e2 = ge.GGUFEngine(self.d, llama_factory=FakeLlama, no_think_suffix=False)
        e2.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(FakeLlama.instances[1].calls[0][-1]["content"], "hi")

    def test_stream_hides_think_block_even_when_split_across_chunks(self):
        e = ge.GGUFEngine(self.d, llama_factory=FakeLlama)
        self.assertEqual("".join(e.stream([{"role": "user", "content": "hi"}])), "The answer is 4.")

    def test_history_is_trimmed_to_context_but_keeps_system_and_last_user(self):
        msgs = [{"role": "system", "content": "sys"}] + [{"role": "user" if i % 2 == 0 else "assistant", "content": "word " * 50} for i in range(20)]
        msgs.append({"role": "user", "content": "final question"})
        out = ge.fit_messages(msgs, 300, lambda t: len(t.split()))
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[-1]["content"], "final question")
        self.assertLess(len(out), len(msgs))

    def test_load_failure_and_missing_runtime_are_clear_errors(self):
        def boom(**kw): raise ValueError("bad file")
        with self.assertRaises(ge.RuntimeUnavailable) as cm: ge.GGUFEngine(self.d, llama_factory=boom)
        self.assertIn("could not be loaded", str(cm.exception))
        with self.assertRaises(ge.RuntimeUnavailable): ge.GGUFEngine(self.d / "nope", llama_factory=FakeLlama)
        if not ge.runtime_info()["available"]:
            with self.assertRaises(ge.RuntimeUnavailable) as cm: ge.GGUFEngine(self.d)
            self.assertIn("not available", str(cm.exception))


class HardwareTests(unittest.TestCase):
    def hw(self, ram, disk=50, gpu=None): return Hardware("Windows 11", 8, ram, disk, gpu)
    def test_verdicts(self):
        size = int(1.28e9)
        self.assertEqual(assess_gguf(self.hw(3.5), size, 4.0)["verdict"], "too_large")
        self.assertEqual(assess_gguf(self.hw(5.5), size, 4.0)["verdict"], "tight")
        self.assertEqual(assess_gguf(self.hw(16), size, 4.0)["verdict"], "ok")
        self.assertEqual(assess_gguf(self.hw(16, disk=0.5), size)["verdict"], "too_large")
        self.assertEqual(assess_gguf(self.hw(None), size)["verdict"], "unknown")
        self.assertIn("GPU is not required", " ".join(assess_gguf(self.hw(16), size)["reasons"]))


class AssistantAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()
        self.store = ModelStore(self.cfg.models_dir)
        FakeLlama.instances.clear()
    def tearDown(self): self.tmp.cleanup()

    def install(self):
        f = make_gguf(self.d / "m.gguf")
        self.store.install_gguf(f, "fake", "Apache-2.0", "t", extra_meta=None)
        self.store.set_active("fake")

    def test_no_model_and_wrong_kind_raise_honestly(self):
        with self.assertRaises(NoActiveModel): LocalAssistant(self.cfg)
        src = self.d / "m"; src.mkdir()
        (src / "model.pt").write_bytes(b"w"); (src / "tokenizer.json").write_text("{}"); (src / "LICENSE.txt").write_text("l")
        from jarvis.models.package import pack_model
        pack_model(src, self.d / "p.jarvismodel", "MIT")
        self.store.install_package(self.d / "p.jarvismodel", "scratch", confirm_unlisted=True)
        self.store.set_active("scratch")
        with self.assertRaises(NoActiveModel) as cm: LocalAssistant(self.cfg)
        self.assertIn("research checkpoint", str(cm.exception))

    def test_assistant_uses_personality_system_prompt_and_bounded_history(self):
        self.install()
        a = LocalAssistant(self.cfg, engine_factory=lambda p: ge.GGUFEngine(p, llama_factory=FakeLlama), max_history_messages=4)
        for i in range(6): a.reply(f"q{i}")
        sent = FakeLlama.instances[0].calls[-1]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("Personality is style only", sent[0]["content"])
        self.assertIn("never claim to have done so", sent[0]["content"])
        self.assertLessEqual(len(a.history), 4)

    def run_cli(self, *args, fake_runtime=False):
        mod = types.ModuleType("llama_cpp"); mod.Llama = FakeLlama; mod.__version__ = "fake"
        ctx = mock.patch.dict(sys.modules, {"llama_cpp": mod}) if fake_runtime else mock.patch.dict(sys.modules, {})
        import io, contextlib
        out, err = io.StringIO(), io.StringIO()
        with ctx, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if not fake_runtime: sys.modules.pop("llama_cpp", None)
            rc = cli.main(["--data-dir", str(self.cfg.data_dir), *args])
        return rc, out.getvalue(), err.getvalue()

    def test_cli_chat_with_no_model(self):
        rc, _, err = self.run_cli("chat", "hello")
        self.assertEqual(rc, 2); self.assertIn("cannot answer without a model", err)

    def test_cli_add_gguf_requires_license_and_checksum_choice(self):
        f = make_gguf(self.d / "m.gguf")
        self.assertEqual(self.run_cli("models", "add-gguf", str(f), "--id", "fake", "--license", "MIT")[0], 2)
        self.assertEqual(self.run_cli("models", "add-gguf", str(f), "--id", "fake", "--license", "MIT", "--license-url", "https://l.example")[0], 2)
        rc, out, _ = self.run_cli("models", "add-gguf", str(f), "--id", "fake", "--license", "MIT", "--license-url", "https://l.example", "--accept-unverified")
        self.assertEqual(rc, 0)

    def test_selftest_and_chat_with_fake_runtime_prove_plumbing_only(self):
        self.install()
        rc, out, err = self.run_cli("chat", "hi there", fake_runtime=True)
        self.assertEqual(rc, 0)
        self.assertIn("The answer is 4.", out)
        self.assertIn("not been self-tested", err)                       # honest until selftest passes
        rc, out, err = self.run_cli("selftest", fake_runtime=True)
        self.assertEqual(rc, 0)
        r = json.loads((self.cfg.data_dir / "selftest.json").read_text())
        self.assertTrue(r["passed"])
        rc, out, err = self.run_cli("chat", "again", fake_runtime=True)
        self.assertNotIn("not been self-tested", err)

    def test_selftest_without_runtime_fails_honestly_and_records_it(self):
        if ge.runtime_info()["available"]: self.skipTest("a real llama_cpp is installed here")
        self.install()
        rc, out, err = self.run_cli("selftest")
        self.assertEqual(rc, 3)
        self.assertIn("SELFTEST FAILED", err)
        self.assertFalse(json.loads((self.cfg.data_dir / "selftest.json").read_text())["passed"])
        rc, _, err = self.run_cli("chat", "hi")
        self.assertEqual(rc, 2); self.assertIn("not available", err)

    def test_selftest_state_resets_when_model_file_changes(self):
        self.install()
        self.run_cli("selftest", fake_runtime=True)
        f2 = make_gguf(self.d / "other.gguf", pad=4000)
        self.store.install_gguf(f2, "fake", "Apache-2.0", "t")
        self.store.set_active("fake")
        self.assertEqual(cli._selftest_state(self.cfg, self.store, "fake"), "not_run")

    def test_catalog_and_runtime_commands(self):
        rc, out, _ = self.run_cli("models", "catalog")
        c = json.loads(out)[0]
        self.assertEqual((rc, c["license"], c["id"]), (0, "Apache-2.0", "qwen3-1.7b-q4_k_m"))
        self.assertIn(c["fits_this_pc"]["verdict"], ("ok", "tight", "too_large", "unknown"))
        rc, out, _ = self.run_cli("runtime", "--require", fake_runtime=True)
        self.assertEqual(rc, 0)
        if not ge.runtime_info()["available"]:
            self.assertEqual(self.run_cli("runtime", "--require")[0], 3)


class CatalogFactsTests(unittest.TestCase):
    def test_builtin_models_are_permissively_licensed_and_not_bundled(self):
        for m in catalog.BUILTIN:
            self.assertIn(m.license_spdx, ("Apache-2.0", "MIT"))
            self.assertTrue(m.license_url.startswith("https://"))
        self.assertFalse(list(ROOT.rglob("*.gguf")))
        self.assertNotIn(".gguf", (ROOT / "packaging" / "installer.iss").read_text())


if __name__ == "__main__":
    unittest.main()
