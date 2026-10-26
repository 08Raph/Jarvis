import io
import contextlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import cli
from jarvis.config import Config
from jarvis.net.policy import NetworkPolicy
from jarvis.telemetry import consent as consent_mod
from jarvis.telemetry.client import OUTBOX_CAP, Telemetry, TelemetryError, get_endpoint
from jarvis.telemetry.consent import Consent, notice_text
from jarvis.telemetry.schema import EVENTS, SchemaError, build_event, validate_event
from jarvis_server.__main__ import make_handler
from jarvis_server.app import Service
from jarvis_server.db import DB
from jarvis_server.telemetry_report import report

IID = "a" * 32
GOOD = {"app_version": "0.4.0", "os": "Windows 11 10.0.22631", "ram_gb_bucket": 8, "cpu_threads_bucket": 8,
        "gpu_present": False, "packaged": True}


class SchemaTests(unittest.TestCase):
    def test_valid_events(self):
        self.assertEqual(build_event("app_start", IID, GOOD)["event"], "app_start")
        build_event("selftest_result", IID, {"model": "qwen3-1.7b-q4_k_m", "passed": True, "load_seconds": 3.2, "tokens_per_second": 11.5})
        build_event("feature_used", IID, {"feature": "chat", "count": 3})
        build_event("error", IID, {"component": "chat", "code": "unknown"})

    def test_rejects_unknown_fields_missing_fields_and_bad_envelope(self):
        for bad in ({**GOOD, "hostname": "BOBS-PC"}, {k: v for k, v in GOOD.items() if k != "os"}):
            with self.assertRaises(SchemaError): build_event("app_start", IID, bad)
        ev = build_event("app_start", IID, GOOD)
        for mutate in (lambda e: e.update(extra=1), lambda e: e.update(event="chat_text"), lambda e: e.update(t="2026-10-25T02:13:44Z"),
                       lambda e: e.update(install_id="BOB"), lambda e: e.update(schema=2), lambda e: e.update(data=[])):
            e = json.loads(json.dumps(ev)); mutate(e)
            with self.assertRaises(SchemaError): validate_event(e)

    def test_no_field_can_carry_free_text(self):
        """Every string-capable field must reject prompts, paths, emails and long text - by construction, not by luck."""
        probes = ["please summarise my private document", "C:\\Users\\bob\\notes.txt", "/home/bob/x", "bob@example.com", "x" * 200,
                  "Windows 11 but also my name is Bob", "model with spaces"]
        for event, (_, fields, _) in EVENTS.items():
            for name, check in fields.items():
                for p in probes:
                    self.assertFalse(check(p), f"{event}.{name} accepted free text {p!r}")

    def test_numeric_edge_cases(self):
        for v in (True, math.nan, math.inf, -1, 10 ** 9, "5"):
            with self.assertRaises(SchemaError): build_event("selftest_result", IID, {"model": "m", "passed": True, "load_seconds": v})
        with self.assertRaises(SchemaError): build_event("feature_used", IID, {"feature": "chat", "count": True})
        with self.assertRaises(SchemaError): build_event("app_start", IID, {**GOOD, "ram_gb_bucket": 7})   # only bucket values

    def test_timestamp_has_hour_precision_only(self):
        self.assertRegex(build_event("app_start", IID, GOOD)["t"], r"T\d\d:00:00Z$")


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.c = Consent(Path(self.tmp.name) / "t.json")
    def tearDown(self): self.tmp.cleanup()

    def test_off_by_default_and_requires_a_category(self):
        self.assertFalse(self.c.state()["enabled"])
        self.assertIsNone(self.c.install_id())
        with self.assertRaises(ValueError): self.c.enable(False, False)

    def test_enable_disable_and_independent_categories(self):
        s = self.c.enable(True, False)
        self.assertTrue(s["diagnostics"]); self.assertFalse(s["usage"]); self.assertRegex(s["install_id"], r"^[0-9a-f]{32}$")
        iid = s["install_id"]
        self.assertEqual(self.c.enable(True, True)["install_id"], iid)        # same id while consent continues
        self.c.disable()
        self.assertFalse(self.c.state()["enabled"])

    def test_reset_id_and_notice_version_forces_reconsent(self):
        a = self.c.enable(True, True)["install_id"]
        self.assertNotEqual(self.c.reset_id(), a)
        with mock.patch.object(consent_mod, "NOTICE_VERSION", 2):
            st = self.c.state()
            self.assertFalse(st["enabled"]); self.assertTrue(st["needs_reconsent"])

    def test_corrupt_file_means_off(self):
        self.c.path.write_text("{garbage")
        self.assertFalse(self.c.state()["enabled"])

    def test_notice_lists_everything_and_says_what_is_never_sent(self):
        n = notice_text()
        for needle in ("chats", "IP address", "jarvis telemetry disable", "delete-my-data", "app_start", "feature_used", "[OPERATOR:"):
            self.assertIn(needle, n)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self.tmp.name) / "d"); self.cfg.data_dir.mkdir()
        self.pol = NetworkPolicy(allow_insecure_localhost=True)
    def tearDown(self): self.tmp.cleanup()
    def tel(self, endpoint="http://127.0.0.1:1"): return Telemetry(self.cfg, self.pol, endpoint=endpoint)

    def test_records_nothing_without_consent(self):
        t = self.tel()
        self.assertFalse(t.record("feature_used", feature="chat", count=1))
        self.assertFalse(t.outbox.exists())

    def test_categories_are_enforced_separately(self):
        t = self.tel(); t.consent.enable(diagnostics=False, usage=True)
        self.assertFalse(t.record("error", component="chat", code="unknown"))
        self.assertTrue(t.record("feature_used", feature="chat", count=1))
        t.consent.enable(diagnostics=True, usage=False)
        self.assertFalse(t.record("feature_used", feature="chat", count=1))

    def test_invalid_data_is_dropped_never_queued(self):
        t = self.tel(); t.consent.enable(True, True)
        self.assertFalse(t.record("error", component="chat", code="my prompt was: hello"))
        self.assertFalse(t.record("error", component="chat", code="unknown", prompt="hi"))
        self.assertEqual(t.preview(), [])

    def test_usage_counts_merge_and_queue_is_capped_and_damage_is_ignored(self):
        t = self.tel(); t.consent.enable(True, True)
        for _ in range(3): t.record("feature_used", feature="chat", count=1)
        self.assertEqual([e["data"]["count"] for e in t.preview()], [3])
        t.outbox.write_text(t.outbox.read_text() + "not json\n" + '{"schema":1,"event":"app_start"}\n')
        self.assertEqual(len(t.preview()), 1)
        for i in range(OUTBOX_CAP + 50): t.record("model_installed", model=f"m{i}", source="file")
        self.assertLessEqual(len(t.preview()), OUTBOX_CAP)

    def test_disable_clears_nothing_automatically_but_stops_recording(self):
        t = self.tel(); t.consent.enable(True, True); t.record("app_start", **GOOD)
        t.consent.disable()
        self.assertFalse(t.record("app_start", **GOOD))
        with self.assertRaises(TelemetryError): t.send()

    def test_send_needs_endpoint_and_consent_and_unreachable_keeps_events(self):
        t = self.tel(endpoint=""); t.consent.enable(True, True); t.record("app_start", **GOOD)
        with self.assertRaises(TelemetryError): t.send()
        t2 = self.tel(); self.pol.enable("telemetry")
        with self.assertRaises(TelemetryError): t2.send()
        self.assertEqual(len(t2.preview()), 1)            # kept for a later attempt

    def test_flag_off_means_no_socket(self):
        t = self.tel(); t.consent.enable(True, True); t.record("app_start", **GOOD)
        with self.assertRaises(TelemetryError) as cm: t.send()       # policy 'telemetry' flag is off
        self.assertIn("disabled", str(cm.exception))

    def test_shipped_default_has_no_server(self):
        self.assertEqual(get_endpoint(), "")                         # the repo build phones nowhere


class ServerSideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "s.db")
        self.svc = Service(self.db, None, terms_text="t")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.svc, False))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.cfg = Config(data_dir=Path(self.tmp.name) / "d"); self.cfg.data_dir.mkdir()
        self.pol = NetworkPolicy(allow_insecure_localhost=True)
        self.t = Telemetry(self.cfg, self.pol, endpoint=self.url)
        self.t.consent.enable(True, True); self.pol.enable("telemetry")
    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.db.close(); self.tmp.cleanup()

    def test_end_to_end_preview_equals_payload_and_queue_empties(self):
        self.t.record("app_start", **GOOD)
        self.t.record("feature_used", feature="chat", count=2)
        self.t.record("selftest_result", model="qwen3-1.7b-q4_k_m", passed=True, tokens_per_second=9.5)
        preview = self.t.preview()
        r = self.t.send()
        self.assertEqual((r["sent"], r["accepted"], r["rejected"]), (3, 3, 0))
        self.assertEqual(self.t.preview(), [])
        stored = [dict(x) for x in self.db.all("SELECT * FROM telemetry")]
        self.assertEqual(len(stored), 3)
        self.assertEqual({s["install_id"] for s in stored}, {preview[0]["install_id"]})
        blob = json.dumps(stored)
        self.assertNotIn("127.0.0.1", blob)                          # no IP address stored

    def test_server_rejects_bad_events_individually(self):
        good = build_event("app_start", IID, GOOD)
        bad = json.loads(json.dumps(good)); bad["data"]["note"] = "my prompt"
        s, r = self.svc.handle("POST", "/v1/telemetry", {}, json.dumps({"events": [good, bad, "junk", 5]}).encode(), "9.9.9.9")
        self.assertEqual((s, r), (200, {"accepted": 1, "rejected": 3}))
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM telemetry")["c"], 1)
        self.assertEqual(self.svc.handle("POST", "/v1/telemetry", {}, b'{"events": []}', "9.9.9.9")[0], 400)
        self.assertEqual(self.svc.handle("POST", "/v1/telemetry", {}, json.dumps({"events": [good] * 101}).encode(), "9.9.9.9")[0], 400)

    def test_rate_limit_delete_retention_and_disable_flag(self):
        good = build_event("app_start", IID, GOOD)
        body = json.dumps({"events": [good]}).encode()
        codes = [self.svc.handle("POST", "/v1/telemetry", {}, body, "5.5.5.5")[0] for _ in range(62)]
        self.assertEqual(codes[:60], [200] * 60); self.assertEqual(codes[60:], [429, 429])
        s, r = self.svc.handle("POST", "/v1/telemetry/delete", {}, json.dumps({"install_id": IID}).encode(), "6.6.6.6")
        self.assertEqual(s, 200); self.assertEqual(r["deleted_events"], 60)
        self.assertEqual(self.svc.handle("POST", "/v1/telemetry/delete", {}, b'{"install_id":"nope"}', "6.6.6.6")[0], 400)
        self.svc.handle("POST", "/v1/telemetry", {}, body, "7.7.7.7")
        self.db.q("UPDATE telemetry SET received_at = 0")
        self.assertEqual(self.svc.purge_telemetry(), 1)
        off = Service(self.db, None, terms_text="t", telemetry_enabled=False)
        self.assertEqual(off.handle("POST", "/v1/telemetry", {}, body, "8.8.8.8")[0], 404)

    def test_delete_my_data_through_client_and_report(self):
        self.t.record("app_start", **GOOD); self.t.record("selftest_result", model="qwen3-1.7b-q4_k_m", passed=False)
        self.t.send()
        rep = report(self.db)
        self.assertEqual(rep["installs_seen"], 1)
        self.assertEqual(rep["events_total"], 2)
        self.assertEqual(rep["ram_buckets"], [{"ram_gb": 8, "n": 1}])
        self.assertEqual(self.t.delete_remote()["deleted_events"], 2)
        self.assertEqual(report(self.db)["events_total"], 0)

    def test_server_telemetry_needs_no_account_and_stores_no_account_link(self):
        cols = [r["name"] for r in self.db.all("PRAGMA table_info(telemetry)")]
        self.assertNotIn("user_id", cols); self.assertNotIn("ip", cols)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = self.tmp.name
    def tearDown(self): self.tmp.cleanup()

    def cli(self, *a, inp=""):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        p = subprocess.run([sys.executable, "-W", "ignore", "-m", "jarvis", "--data-dir", self.d, *a], input=inp,
                           capture_output=True, text=True, env=env, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_default_build_cannot_enable_and_privacy_flag_is_not_a_backdoor(self):
        rc, _, err = self.cli("telemetry", "enable", inp="I agree\n")
        self.assertEqual(rc, 2); self.assertIn("no telemetry server configured", err)
        rc, _, err = self.cli("privacy", "enable", "telemetry")
        self.assertEqual(rc, 2); self.assertIn("jarvis telemetry enable", err)
        st = json.loads(self.cli("telemetry", "status")[1])
        self.assertFalse(st["enabled"]); self.assertFalse(st["server_configured_in_this_build"])
        self.assertEqual(json.loads(self.cli("privacy")[1])["online_features"]["telemetry"], False)

    def test_full_flow_against_local_server(self):
        db = DB(Path(self.d) / "srv.db"); svc = Service(db, None, terms_text="t")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc, False))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            base = ("telemetry", "--dev-insecure-localhost", "--endpoint", url)
            self.assertEqual(self.cli(*base, "enable", inp="no thanks\n")[0], 1)        # anything but "I agree" = off
            self.assertFalse(json.loads(self.cli(*base, "status")[1])["enabled"])
            rc, out, _ = self.cli(*base, "enable", "--diagnostics-only", inp="I agree\n")
            self.assertEqual(rc, 0); self.assertIn("What is never sent".upper().replace("WHAT IS NEVER SENT", "WHAT IS NEVER SENT"), out.upper())
            st = json.loads(self.cli(*base, "status")[1])
            self.assertTrue(st["diagnostics"] and not st["usage"] and st["network_flag_on"])
            # queue an event through the real API, preview it, send it
            t = Telemetry(Config(data_dir=Path(self.d)), NetworkPolicy(), endpoint=url)
            t.record("error", component="chat", code="unknown")
            t.record("feature_used", feature="chat", count=1)                          # usage was NOT consented: dropped
            prev = json.loads(self.cli(*base, "preview")[1])
            self.assertEqual([e["event"] for e in prev], ["error"])
            self.assertEqual(json.loads(self.cli(*base, "send")[1])["accepted"], 1)
            self.assertEqual(db.one("SELECT COUNT(*) c FROM telemetry")["c"], 1)
            self.assertEqual(self.cli(*base, "disable")[0], 0)
            self.assertFalse(json.loads(self.cli(*base, "status")[1])["enabled"])
            self.assertEqual(self.cli(*base, "delete-my-data")[0], 0)
            self.assertEqual(db.one("SELECT COUNT(*) c FROM telemetry")["c"], 0)
        finally:
            httpd.shutdown(); httpd.server_close(); db.close()


class NoContentLeakTests(unittest.TestCase):
    """Run a real chat command with telemetry ON and a recognisable secret prompt; the secret must appear nowhere."""
    def test_prompt_and_reply_never_reach_queue(self):
        from test_local_model import FakeLlama, make_gguf
        from jarvis.models.manager import ModelStore
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(data_dir=Path(d) / "data"); cfg.data_dir.mkdir()
            store = ModelStore(cfg.models_dir)
            store.install_gguf(make_gguf(Path(d) / "m.gguf"), "my-private-model-name", "MIT", "t")
            store.set_active("my-private-model-name")
            t = Telemetry(cfg, NetworkPolicy(), endpoint="http://127.0.0.1:1"); t.consent.enable(True, True)
            mod = types.ModuleType("llama_cpp"); mod.Llama = FakeLlama; mod.__version__ = "0.3.2"
            with mock.patch.dict(sys.modules, {"llama_cpp": mod}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                cli.main(["--data-dir", str(cfg.data_dir), "chat", "SECRET-PROMPT-123 my passport number"])
                cli.main(["--data-dir", str(cfg.data_dir), "selftest"])
            queued = (cfg.data_dir / "telemetry_outbox.jsonl").read_text()
            self.assertIn("feature_used", queued)
            self.assertIn("selftest_result", queued)
            for secret in ("SECRET-PROMPT", "passport", "The answer is 4", "my-private-model-name", d):
                self.assertNotIn(secret, queued)
            self.assertIn('"model":"custom"', queued)                  # user-chosen model names are not sent


if __name__ == "__main__":
    unittest.main()
