"""Stage 5: a missing or unloadable local model must never be 'fixed' by a silent cloud call.

The local runtime is made unavailable by blocking the llama_cpp import (a FAKE condition, deterministic whether or not the package is installed here) and every
socket / DNS lookup is made fatal. The online assistant is deliberately connected and enabled, so a fallback WOULD be possible if the code had one.
This proves the code path on Linux; it is not a test of a real model, a real runtime or Windows."""
import argparse
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from p10_common import make_gguf
from jarvis import cli
from jarvis.assistant import LocalAssistant
from jarvis.config import Config
from jarvis.inference.gguf_engine import GGUFEngine, RuntimeUnavailable
from jarvis.models.manager import ModelStore
from jarvis.net.policy import NetworkPolicy


class NoSilentCloudFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()
        policy = NetworkPolicy()
        policy.enable("online_assistant")                                  # the online service IS connected and allowed
        policy.save(self.cfg.settings_path)
        (self.cfg.data_dir / "online.json").write_text(json.dumps({"url": "https://example.invalid"}), encoding="utf-8")
        self.store = ModelStore(self.cfg.models_dir, NetworkPolicy.load(self.cfg.settings_path))
        self.store.install_gguf(make_gguf(self.d / "m.gguf"), "qwen3-8b-q4_k_m", "Apache-2.0", "test licence text")
        self.store.set_active("qwen3-8b-q4_k_m")
        for p in (mock.patch.dict(sys.modules, {"llama_cpp": None}),
                  mock.patch("socket.socket", side_effect=AssertionError("a socket was opened")),
                  mock.patch("socket.getaddrinfo", side_effect=AssertionError("a DNS lookup was made")),
                  mock.patch.object(NetworkPolicy, "open", side_effect=AssertionError("NetworkPolicy.open was called"))):
            p.start()
            self.addCleanup(p.stop)

    def test_the_engine_reports_the_runtime_as_unavailable_with_a_recovery_hint(self):
        with self.assertRaises(RuntimeUnavailable) as cm:
            GGUFEngine(self.store.path("qwen3-8b-q4_k_m"))
        self.assertIn("not available", str(cm.exception))

    def test_the_assistant_cannot_be_created_without_the_local_runtime_and_does_not_reach_for_the_network(self):
        with self.assertRaises(RuntimeUnavailable):
            LocalAssistant(self.cfg, guest=True)

    def test_the_chat_command_stops_with_a_clear_message_and_exit_code_2_instead_of_going_online(self):
        err, out = io.StringIO(), io.StringIO()
        args = argparse.Namespace(guest=True, max_new_tokens=16, temperature=0.0, message=None)
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = cli._chat_gguf(args, self.cfg, self.store, "qwen3-8b-q4_k_m")
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue(), "")                                  # no answer text from anywhere
        self.assertIn("local AI runtime", err.getvalue())
        self.assertNotIn("online", err.getvalue().lower().replace("not available in this installation", ""))


if __name__ == "__main__":
    unittest.main()
