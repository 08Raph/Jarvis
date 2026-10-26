"""Verifies, as far as can be checked without PyTorch, that core paths never touch the network."""
import ast
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from jarvis.net.policy import FEATURES, NetworkDisabled, NetworkPolicy
from jarvis.security import signing

NETWORK_MODULES = {"socket", "http", "ftplib", "smtplib", "telnetlib", "ssl", "requests", "httpx",
                   "urllib3", "aiohttp", "websocket", "websockets", "xmlrpc"}
ALLOWED_FILE = SRC / "jarvis" / "net" / "policy.py"


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            if node.module == "urllib":
                for a in node.names:
                    yield f"urllib.{a.name}"


class StaticNetworkScan(unittest.TestCase):
    def test_no_network_imports_outside_policy_module(self):
        offenders = []
        for py in SRC.rglob("*.py"):
            if py == ALLOWED_FILE:
                continue
            for mod in _imports(py):
                top = mod.split(".")[0]
                if top in NETWORK_MODULES or mod.startswith("urllib.request") or mod.startswith("urllib.error"):
                    offenders.append(f"{py.relative_to(ROOT)}: {mod}")
        self.assertEqual(offenders, [])

    def test_no_api_key_or_quota_logic_in_source(self):
        text = "\n".join(p.read_text(encoding="utf-8").lower() for p in SRC.rglob("*.py"))
        for needle in ("api_key", "apikey", "openai", "anthropic", "daily_limit", "message_quota", "messages_per_day"):
            self.assertNotIn(needle, text, needle)


class PolicyTests(unittest.TestCase):
    def test_everything_off_by_default(self):
        p = NetworkPolicy()
        self.assertEqual({f: p.is_enabled(f) for f in FEATURES}, {f: False for f in FEATURES})

    def test_disabled_feature_never_touches_a_socket(self):
        p = NetworkPolicy()
        with mock.patch("socket.socket", side_effect=AssertionError("socket opened")), \
             mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS lookup")):
            with self.assertRaises(NetworkDisabled):
                p.open("update_check", "https://example.com/manifest.json")

    def test_https_only_and_cloud_needs_cost_ack(self):
        p = NetworkPolicy()
        p.enable("update_check")
        with self.assertRaises(NetworkDisabled):
            p.open("update_check", "http://example.com/x")
        with self.assertRaises(NetworkDisabled):
            p.enable("cloud_ai")
        p.enable("cloud_ai", cost_acknowledged=True)
        p.disable("cloud_ai")
        self.assertFalse(p.cloud_ai_cost_acknowledged)

    def test_grant_once_restores_state(self):
        p = NetworkPolicy()
        with p.grant_once("model_download"):
            self.assertTrue(p.is_enabled("model_download"))
        self.assertFalse(p.is_enabled("model_download"))

    def test_corrupt_settings_fail_safe(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.json"
            for junk in ("{not json", "[]", '{"enabled": []}', ""):
                f.write_text(junk)
                p = NetworkPolicy.load(f)
                self.assertFalse(any(p.is_enabled(x) for x in FEATURES), junk)

    def test_unknown_feature_rejected(self):
        with self.assertRaises(ValueError):
            NetworkPolicy().enable("ai_phone_home")

    def test_save_load_and_hand_edit_cannot_skip_cost_ack(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.json"
            p = NetworkPolicy()
            p.enable("update_check")
            p.save(f)
            self.assertTrue(NetworkPolicy.load(f).is_enabled("update_check"))
            f.write_text('{"enabled": {"cloud_ai": true}}')
            self.assertFalse(NetworkPolicy.load(f).is_enabled("cloud_ai"))
            self.assertFalse(NetworkPolicy.load(Path(d) / "missing.json").is_enabled("update_check"))


class CorePathsWithSocketsBlocked(unittest.TestCase):
    """Runs the code paths that CAN run here with all socket use made fatal, and no API keys set."""

    def test_core_paths_make_no_network_calls(self):
        from jarvis.data.pipeline import prepare_dataset
        from jarvis.data.provenance import ProvenanceLedger
        from jarvis.eval.benchmarks import oracle_answerer, run_benchmarks
        from jarvis.inference.policy import AnswerGate
        from jarvis.memory.store import MemoryStore
        from jarvis.tokenizer.bpe import BPETokenizer
        from jarvis.tools.registry import build_default_registry

        env = {k: v for k, v in os.environ.items() if "API_KEY" not in k.upper()}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("socket.socket", side_effect=AssertionError("socket opened")), \
             mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS lookup")), \
             tempfile.TemporaryDirectory() as d:
            d = Path(d)
            tok = BPETokenizer.train("hello world " * 50, 300)
            self.assertEqual(tok.decode(tok.encode("hello")), "hello")
            mem = MemoryStore(d / "m.sqlite")
            reg = build_default_registry(mem)
            self.assertEqual(reg.run("calculate", {"expression": "6*7"}).output, "42")
            mem.close()
            self.assertTrue(AnswerGate().decide("ok", -1.0).answered)
            res = run_benchmarks(oracle_answerer())
            self.assertEqual(res["categories"]["instruction_following"]["score"], 1.0)
            words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
            import random
            rng = random.Random(1)
            (d / "c.txt").write_text("\n\n".join(" ".join(rng.choice(words) + str(i) for i in range(1)) +
                                                  " " + " ".join("".join(rng.choice("abcdefghij") for _ in range(6))
                                                                 for _ in range(30)) for _ in range(40)))
            led = ProvenanceLedger(d / "l.db")
            led.register(d / "c.txt", "c", "synthetic", "project-original")
            led.close()
            prepare_dataset(d / "l.db", d / "out", ngram=8)


class SigningTests(unittest.TestCase):
    def test_sign_verify_and_tamper(self):
        sk, pk = signing.generate_keypair()
        obj = {"version": 1, "models": [{"id": "a", "sha256": "00"}]}
        sig = signing.sign(obj, sk)
        self.assertTrue(signing.verify(obj, sig, pk))
        obj["models"][0]["sha256"] = "11"
        self.assertFalse(signing.verify(obj, sig, pk))
        _, other_pk = signing.generate_keypair()
        self.assertFalse(signing.verify({"version": 1, "models": [{"id": "a", "sha256": "00"}]}, sig, other_pk))
        self.assertFalse(signing.verify(obj, "not-base64!!", pk))


if __name__ == "__main__":
    unittest.main()
