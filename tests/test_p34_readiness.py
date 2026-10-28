"""System & Model Readiness (in-app Stage 5 preflight): core runner, policy-gated fetcher, result history, controller, window logic, packaging resolution.

What is REAL here: the runner, history files, NetworkPolicy gating, the controller's background worker, the 'scripts and app share one implementation' link.
What is FAKE: the machine (a scripted Probe), the model host (scripted fetch functions; no socket is opened), and Qt (PySide6 is not installable in the authoring
sandbox, so the window's own methods are exercised against stand-in widgets). None of this is Qt rendering, Windows, or the packaged EXE."""
import ast
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "packaging"))

from p10_common import AppCase  # noqa: E402
from test_p33_windows_tools import MetadataRun, VM, api_record, by_id  # noqa: E402,F401
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.models import diskspace  # noqa: E402
from jarvis.net.policy import NetworkDisabled, NetworkPolicy  # noqa: E402
from jarvis.preflight import hf_fetch, history, metadata, runner  # noqa: E402

BY_REPO = {c["repo"]: c for c in metadata.CANDIDATES}
SIZES = MetadataRun.sizes


def good_fetch(mutate=None, seen=None):
    def f(repo):
        c = BY_REPO[repo]
        if seen is not None:
            seen.append(repo)
        d = api_record(c, size=SIZES[c["id"]])
        return mutate(c, d) if mutate else d
    return f


class FakeProbe(runner.Probe):
    def __init__(self, system="Windows", version="10.0.22631", machine="AMD64", py=(3, 12, 4), bits=64, ram=32.0, free=500_000_000_000, runtime=None, models=None, frozen=False):
        self._s = {"system": system, "release": "11" if system == "Windows" else "6.1", "version": version, "machine": machine}
        self._p = {"frozen": frozen, "implementation": "CPython", "version": ".".join(map(str, py)), "bits": bits}
        self._ram, self._free = ram, free
        self._rt = runtime if runtime is not None else {"available": True, "version": "0.3.99"}
        self._m = models or {"installed": [], "selected": None}

    def system(self):
        return self._s

    def python(self):
        return self._p

    def ram_gb(self):
        if isinstance(self._ram, Exception):
            raise self._ram
        return self._ram

    def free_bytes(self, path):
        if isinstance(self._free, Exception):
            raise self._free
        return self._free

    def runtime(self):
        if isinstance(self._rt, Exception):
            raise self._rt
        return self._rt

    def models(self, cfg):
        return self._m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.cfg = Config(data_dir=self.d / "data")
        self.cfg.data_dir.mkdir()

    def go(self, **kw):
        kw.setdefault("probe", FakeProbe())
        kw.setdefault("policy", NetworkPolicy())
        return runner.run_preflight(self.cfg, **kw)

    @staticmethod
    def status(rec, cid):
        return next(c for c in rec["checks"] if c["id"] == cid)


class RunnerOutcomes(Base):
    def test_everything_passes_only_when_every_check_passes(self):
        policy, seen_enabled = NetworkPolicy(), []
        base = good_fetch()

        def fetch(repo):
            seen_enabled.append(policy.is_enabled("model_download"))      # the one online action runs with the feature switched on...
            return base(repo)
        rec = self.go(include_online=True, online_consent=True, fetch=fetch, policy=policy)
        self.assertEqual(rec["verdict"]["code"], "PASSED", [(c["id"], c["status"], c["detail"]) for c in rec["checks"]])
        self.assertTrue(all(c["status"] in ("PASS", "INFO") for c in rec["checks"]))
        self.assertEqual(set(seen_enabled), {True})
        self.assertFalse(policy.is_enabled("model_download"))               # ...and it is switched off again afterwards
        m = self.status(rec, "metadata")
        self.assertEqual((m["status"], m["exit_code"], m["needs_internet"]), ("PASS", 0, True))
        self.assertEqual(len(rec["models"]), 5)
        self.assertIn("not proof of authenticity", m["detail"])
        self.assertTrue(any("No independent second source" in b for b in rec["blockers"]))

    def test_validation_failure_is_a_problem_with_the_reason_and_never_a_pass(self):
        def mutate(c, d):
            if c["id"] == "qwen3-8b-official":
                d["siblings"][2]["lfs"]["sha256"] = "1" * 64
            return d
        rec = self.go(include_online=True, online_consent=True, fetch=good_fetch(mutate))
        m = self.status(rec, "metadata")
        self.assertEqual((rec["verdict"]["code"], m["status"], m["exit_code"]), ("PROBLEMS", "PROBLEM", 4))
        self.assertIn("HASH_DIFFERS_FROM_RECORDED", m["detail"])
        row = next(r for r in rec["models"] if r["id"] == "qwen3-8b-official")
        self.assertEqual(row["status"], "PROBLEMS")

    def test_network_unavailable_is_incomplete_and_claims_nothing(self):
        def down(repo):
            raise metadata.ApiUnavailable("network error (URLError): the host could not be reached")
        rec = self.go(include_online=True, online_consent=True, fetch=down)
        m = self.status(rec, "metadata")
        self.assertEqual((rec["verdict"]["code"], m["status"], m["exit_code"]), ("INCOMPLETE", "UNAVAILABLE", 3))
        self.assertIn("Nothing is claimed", m["detail"])
        self.assertNotEqual(rec["verdict"]["code"], "PASSED")

    def test_offline_run_is_partial_and_the_online_step_says_not_run(self):
        touched = []
        policy = NetworkPolicy()
        policy.open = lambda *a, **k: touched.append(a)         # would be recorded if anything tried to reach the network
        rec = self.go(policy=policy, fetch=lambda r: touched.append(r))
        m = self.status(rec, "metadata")
        self.assertEqual((rec["verdict"]["code"], m["status"], m["exit_code"]), ("PARTIAL", "NOT_RUN", None))
        self.assertEqual(touched, [])
        self.assertEqual(rec["include_online"], False)

    def test_no_consent_no_run_and_nothing_is_created(self):
        with self.assertRaises(ValueError):
            self.go(include_online=True, online_consent=False, fetch=good_fetch())
        self.assertFalse((self.cfg.data_dir / "preflight").exists())

    def test_missing_runtime_is_unavailable_with_the_reason_and_no_install(self):
        rec = self.go(probe=FakeProbe(runtime={"available": False, "error": "ModuleNotFoundError: No module named 'llama_cpp'"}))
        r = self.status(rec, "runtime")
        self.assertEqual((r["status"], r["exit_code"]), ("UNAVAILABLE", 3))
        self.assertIn("never installs", r["detail"])
        self.assertIn("import", r["title"].lower())
        self.assertEqual(rec["statements"]["runtime_installed_by_this_run"], False)

    def test_runtime_import_success_is_not_called_a_model_load(self):
        r = self.status(self.go(), "runtime")
        self.assertEqual(r["status"], "PASS")
        self.assertIn("does NOT show that a model loads", r["detail"])

    def test_disk_memory_python_and_os_outcomes(self):
        low = self.status(self.go(probe=FakeProbe(free=24_999_999_999)), "disk")
        self.assertEqual(low["status"], "PROBLEM")
        self.assertEqual(self.status(self.go(probe=FakeProbe(free=25_000_000_000)), "disk")["status"], "PASS")
        unk = self.status(self.go(probe=FakeProbe(free=diskspace.DiskSpaceError("could not determine"))), "disk")
        self.assertEqual(unk["status"], "UNAVAILABLE")
        self.assertEqual(self.status(self.go(probe=FakeProbe(ram=8.0)), "memory")["status"], "PROBLEM")
        self.assertEqual(self.status(self.go(probe=FakeProbe(ram=None)), "memory")["status"], "UNAVAILABLE")
        self.assertEqual(self.status(self.go(probe=FakeProbe(bits=32)), "python")["status"], "PROBLEM")
        self.assertEqual(self.status(self.go(probe=FakeProbe(py=(3, 10, 0))), "python")["status"], "PROBLEM")
        self.assertIn("bundled inside the app", self.status(self.go(probe=FakeProbe(frozen=True)), "python")["detail"])
        self.assertEqual(self.status(self.go(), "os")["status"], "PASS")
        self.assertEqual(self.status(self.go(probe=FakeProbe(version="10.0.19045")), "os")["status"], "INFO")
        self.assertEqual(self.status(self.go(probe=FakeProbe(machine="ARM64")), "os")["status"], "PROBLEM")
        self.assertEqual(self.status(self.go(probe=FakeProbe(version="garbage")), "os")["status"], "UNAVAILABLE")
        linux = self.status(self.go(probe=FakeProbe(system="Linux")), "os")
        self.assertEqual(linux["status"], "INFO")
        self.assertIn("NOT Windows", linux["detail"])

    def test_a_crashing_check_is_an_error_not_a_pass_and_the_others_still_run(self):
        rec = self.go(probe=FakeProbe(ram=RuntimeError("boom with C:\\Users\\jane\\secret")))
        m = self.status(rec, "memory")
        self.assertEqual((m["status"], m["exit_code"]), ("ERROR", 1))
        self.assertNotIn("jane", json.dumps(rec))
        self.assertEqual(rec["verdict"]["code"], "PROBLEMS")
        self.assertEqual(self.status(rec, "disk")["status"], "PASS")       # later checks still ran

    def test_malformed_host_output_is_never_success(self):
        for bad in (lambda r: [1, 2], lambda r: "text", lambda r: {"id": 5, "siblings": "x", "tags": 3}):
            rec = self.go(include_online=True, online_consent=True, fetch=bad)
            with self.subTest(bad=bad):
                self.assertNotEqual(rec["verdict"]["code"], "PASSED")
                self.assertIn(self.status(rec, "metadata")["status"], ("ERROR", "PROBLEM", "UNAVAILABLE"))

    def test_http_style_failures_through_the_policy_gate_become_unavailable(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("a socket was opened")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror("no network")):
            rec = self.go(include_online=True, online_consent=True)
        m = self.status(rec, "metadata")
        self.assertEqual(m["status"], "UNAVAILABLE")
        self.assertEqual(rec["verdict"]["code"], "INCOMPLETE")

    def test_nothing_is_downloaded_installed_or_changed(self):
        store = self.cfg.models_dir
        store.mkdir()
        (store / "active.json").write_text('{"id": "x"}', encoding="utf-8")
        before = sorted((p.relative_to(self.d).as_posix(), p.read_bytes()) for p in self.d.rglob("*") if p.is_file())
        rec = self.go(include_online=True, online_consent=True, fetch=good_fetch())
        after = sorted((p.relative_to(self.d).as_posix(), p.read_bytes()) for p in self.d.rglob("*") if p.is_file() and "preflight" not in p.parts)
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in store.iterdir()), ["active.json"])
        self.assertFalse(any(rec["statements"].values()))
        self.assertEqual(set(rec["statements"]), {"model_downloaded", "model_loaded", "model_benchmarked", "runtime_installed_by_this_run", "default_model_changed",
                                                  "catalog_changed", "windows_exe_behaviour_validated"})

    def test_cancel_before_start_marks_everything_not_run(self):
        rec = self.go(should_stop=lambda: True)
        self.assertEqual(rec["verdict"]["code"], "CANCELLED")
        self.assertTrue(all(c["status"] == "NOT_RUN" for c in rec["checks"]))

    def test_cancel_in_the_middle_of_the_online_check_discards_it(self):
        calls = {"n": 0}
        base = good_fetch()

        def fetch(repo):
            calls["n"] += 1
            return base(repo)
        rec = self.go(include_online=True, online_consent=True, fetch=fetch, should_stop=lambda: calls["n"] >= 2)
        self.assertEqual(calls["n"], 2)                            # no third request was made
        self.assertEqual(rec["verdict"]["code"], "CANCELLED")
        self.assertEqual(self.status(rec, "metadata")["status"], "NOT_RUN")
        self.assertEqual(rec["models"], [])

    def test_progress_is_ordered_and_ends_complete(self):
        seen = []
        self.go(include_online=True, online_consent=True, fetch=good_fetch(), progress=lambda d, n, t: seen.append((d, n, t)))
        done = [d for d, n, t in seen]
        self.assertEqual(done, sorted(done))
        self.assertEqual(seen[-1][:2], (len(runner.STEPS), len(runner.STEPS)))
        self.assertTrue(any("huggingface" in t.lower() or "Reading the published" in t for _, _, t in seen))

    def test_relative_data_dir_and_foreign_working_directory(self):
        other = self.d / "elsewhere"
        other.mkdir()
        old = os.getcwd()
        os.chdir(other)
        try:
            cfg = Config(data_dir=Path("relative-data"))
            rec = runner.run_preflight(cfg, probe=FakeProbe(), policy=NetworkPolicy())
        finally:
            os.chdir(old)
        self.assertTrue((other / "relative-data" / "preflight" / rec["run_id"] / "preflight_record.json").is_file())
        self.assertFalse((ROOT / "relative-data").exists())

    def test_the_app_and_the_script_share_one_implementation(self):
        self.assertIs(VM.evaluate, metadata.evaluate)
        self.assertIs(VM.CANDIDATES, metadata.CANDIDATES)
        with tempfile.TemporaryDirectory() as t:
            doc, code = VM.run(Path(t), fetch=good_fetch(), now=lambda: "2026-10-27T00:00:00Z")
        rec = self.go(include_online=True, online_consent=True, fetch=good_fetch())
        self.assertEqual(code, 0)
        self.assertEqual([r["status"] for r in doc["results"]], [m["status"] for m in rec["models"]])
        self.assertEqual([r["published_sha256"] for r in doc["results"]], [m["published_sha256"] for m in rec["models"]])


class RealProbeSmoke(Base):
    """The real Probe on whatever machine runs the tests (Linux here): it must not crash, create model folders, or touch the network."""

    def test_real_probe_runs_and_creates_only_the_preflight_folder(self):
        rec = runner.run_preflight(self.cfg, policy=NetworkPolicy())
        self.assertTrue(all(c["status"] in ("PASS", "PROBLEM", "UNAVAILABLE", "NOT_RUN", "INFO") for c in rec["checks"]), [(c["id"], c["status"]) for c in rec["checks"]])
        self.assertNotIn("ERROR", {c["status"] for c in rec["checks"]})
        self.assertEqual(sorted(p.name for p in self.cfg.data_dir.iterdir()), ["preflight"])       # no models folder, no settings file
        self.assertNotEqual(rec["verdict"]["code"], "PASSED") if sys.platform != "win32" else None

    def test_a_damaged_model_list_is_information_not_a_failure(self):
        class Bad(FakeProbe):
            def models(self, cfg):
                raise ValueError("bad json")
        r = self.status(self.go(probe=Bad()), "models")
        self.assertEqual(r["status"], "INFO")


class PolicyGatedFetcher(unittest.TestCase):
    class Resp:
        def __init__(self, body=b'{"id": "x"}', url="https://huggingface.co/api/models/Qwen/Qwen3-8B-GGUF?blobs=true"):
            self.b, self.url = body, url

        def read(self, n):
            return self.b[:n]

        def geturl(self):
            return self.url

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    def test_only_the_api_of_the_model_host_is_requested_through_the_policy(self):
        pol = mock.Mock()
        pol.open.return_value = self.Resp()
        self.assertEqual(hf_fetch.make_fetcher(pol)("Qwen/Qwen3-8B-GGUF"), {"id": "x"})
        args, kw = pol.open.call_args
        self.assertEqual(args[0], "model_download")
        self.assertEqual(args[1], "https://huggingface.co/api/models/Qwen/Qwen3-8B-GGUF?blobs=true")
        self.assertNotIn("/resolve/", args[1])
        self.assertEqual(kw["allowed_host_suffixes"], ("huggingface.co",))
        self.assertLessEqual(kw["timeout"], 20.0)

    def test_failures_become_api_unavailable_without_leaking_details(self):
        import urllib.error                      # the test file may import it; src may not
        cases = [NetworkDisabled("disabled"), urllib.error.URLError("dns C:\\Users\\jane"), TimeoutError("slow"), OSError("x"), ValueError("v")]
        for exc in cases:
            pol = mock.Mock()
            pol.open.side_effect = exc
            with self.subTest(exc=type(exc).__name__), self.assertRaises(metadata.ApiUnavailable) as cm:
                hf_fetch.make_fetcher(pol)("Qwen/Qwen3-8B-GGUF")
            self.assertNotIn("jane", str(cm.exception))

    def test_a_redirect_away_from_the_api_oversize_and_bad_bodies_are_refused(self):
        for resp in (self.Resp(url="https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/m.gguf"), self.Resp(url="https://evil.example/api/models/x"),
                     self.Resp(url="http://huggingface.co/api/models/x"), self.Resp(body=b"x" * (metadata.MAX_API_BYTES + 10)), self.Resp(body=b"not json"), self.Resp(body=b"[1]")):
            pol = mock.Mock()
            pol.open.return_value = resp
            with self.subTest(url=resp.url, n=len(resp.b)), self.assertRaises(metadata.ApiUnavailable):
                hf_fetch.make_fetcher(pol)("Qwen/Qwen3-8B-GGUF")

    def test_invalid_repository_names_never_reach_the_policy(self):
        pol = mock.Mock()
        for bad in ("../x", "a/b/c", "http://evil/x"):
            with self.assertRaises(ValueError):
                hf_fetch.make_fetcher(pol)(bad)
        pol.open.assert_not_called()

    def test_the_real_policy_refuses_when_the_feature_is_off(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("socket")):
            with self.assertRaises(metadata.ApiUnavailable):
                hf_fetch.make_fetcher(NetworkPolicy())("Qwen/Qwen3-8B-GGUF")


class HistoryStore(Base):
    def test_a_saved_run_is_readable_scrubbed_and_listed(self):
        rec = self.go(probe=FakeProbe(ram=RuntimeError("password=hunter2hunter2 failed at C:\\Users\\jane\\x")))
        run = self.cfg.data_dir / "preflight" / rec["run_id"]
        self.assertEqual(sorted(p.name for p in run.iterdir()), ["preflight.log", "preflight_record.json"])
        text = (run / "preflight_record.json").read_text() + (run / "preflight.log").read_text()
        for secret in ("hunter2hunter2", "jane"):
            self.assertNotIn(secret, text)
        saved = json.loads((run / "preflight_record.json").read_text())
        for key in ("kind", "started_at_utc", "finished_at_utc", "app", "system", "checks", "verdict", "statements", "blockers", "log_file"):
            self.assertIn(key, saved)
        self.assertEqual(saved["app"]["version"], runner.V.__version__)
        self.assertEqual(saved["log_file"], "preflight.log")
        self.assertEqual([r["run_id"] for r in history.list_runs(self.cfg)], [rec["run_id"]])

    def test_published_hashes_and_revisions_survive_scrubbing(self):
        h, rev = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785", "a" * 40
        self.assertEqual(history.scrub({"published_sha256": h, "revision": rev, "list": [h]}), {"published_sha256": h, "revision": rev, "list": [h]})

    def test_secret_keys_and_text_are_removed(self):
        out = history.scrub({"api_key": "sk-abc", "Authorization": "Bearer abcdef1234", "note": "token: abcdef123456 ok", "nested": [{"password": "p"}], "fine": "visible"})
        self.assertEqual(out["api_key"], "<removed>")
        self.assertEqual(out["Authorization"], "<removed>")
        self.assertEqual(out["nested"][0]["password"], "<removed>")
        self.assertNotIn("abcdef123456", out["note"])
        self.assertEqual(out["fine"], "visible")

    def test_only_the_newest_runs_are_kept(self):
        base = history.root(self.cfg)
        base.mkdir(parents=True)
        for i in range(25):
            (base / f"20260101T0000{i:02d}Z").mkdir()
        (base / "not-a-run").mkdir()
        (base / "keepme.txt").write_text("x")
        removed = history.prune(self.cfg, 20)
        self.assertEqual(len(removed), 5)
        self.assertEqual(removed, sorted(removed))
        self.assertTrue((base / "not-a-run").exists() and (base / "keepme.txt").exists())
        self.assertEqual(len([d for d in base.iterdir() if history.RUN_ID.match(d.name)]), 20)

    def test_listing_and_pruning_ignore_links_odd_names_and_bad_records(self):
        base = history.root(self.cfg)
        (base / "20260102T000000Z").mkdir(parents=True)
        (base / "20260102T000000Z" / "preflight_record.json").write_text("{broken", encoding="utf-8")
        (base / "20260103T000000Z").mkdir()
        (base / "20260103T000000Z" / "preflight_record.json").write_text("x" * (history.MAX_RECORD_BYTES + 1), encoding="utf-8")
        outside = self.d / "outside"
        outside.mkdir()
        (outside / "preflight_record.json").write_text('{"verdict": {"code": "PASSED"}}', encoding="utf-8")
        try:
            (base / "20260104T000000Z").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not available")
        rows = history.list_runs(self.cfg)
        self.assertEqual({r["run_id"]: r["readable"] for r in rows}, {"20260102T000000Z": False, "20260103T000000Z": False})
        history.prune(self.cfg, 0)
        self.assertTrue((outside / "preflight_record.json").exists())             # a link is never followed or deleted through
        for bad in ("..", "../x", "20260101T000000Z/../x", "2026"):
            self.assertIsNone(history.RUN_ID.match(bad), bad)

    def test_a_guest_session_saves_nothing_and_leaves_no_temporary_folder(self):
        before = set(Path(tempfile.gettempdir()).glob("jarvis-preflight-*"))
        rec = self.go(guest=True)
        self.assertTrue(rec["guest_session_not_saved"])
        self.assertFalse((self.cfg.data_dir / "preflight").exists())
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("jarvis-preflight-*")), before)

    def test_an_unwritable_history_is_reported_not_hidden(self):
        with mock.patch.object(history, "save", side_effect=PermissionError("denied")):
            rec = self.go()
        self.assertIn("could not be saved", rec["history_error"])
        self.assertNotIn("denied", rec["history_error"])

    def test_run_ids_do_not_collide(self):
        a = self.go(now=lambda: __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc))
        b = self.go(now=lambda: __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc))
        self.assertNotEqual(a["run_id"], b["run_id"])
        self.assertTrue(history.RUN_ID.match(b["run_id"]))


class ControllerPreflight(AppCase):
    def ctl(self):
        topics = []
        c = AppController(self.cfg, simulate=True, on_update=topics.append)
        c.topics = topics
        self.addCleanup(c.shutdown, 1.0)
        return c

    @staticmethod
    def settle(c, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            c.tasks.pump()
            if not c.preflight["running"] and not c.tasks.running():
                c.tasks.pump()
                return
            time.sleep(0.01)
        raise AssertionError("preflight did not finish")

    def test_offline_run_reports_through_the_worker_and_the_interface_thread(self):
        c = self.ctl()
        threads = []
        real = runner.run_preflight

        def spy(cfg, **kw):
            threads.append(threading.current_thread().name)
            return real(cfg, probe=FakeProbe(), policy=NetworkPolicy(), **kw)
        with mock.patch.object(runner, "run_preflight", spy):
            self.assertTrue(c.preflight_start(False, False))
            self.assertTrue(c.preflight["running"])
            self.settle(c)
        self.assertEqual(threads, ["jarvis-preflight"])                     # work ran on the worker, never on the interface thread
        self.assertFalse(c.preflight["running"])
        self.assertEqual(c.preflight["result"]["verdict"]["code"], "PARTIAL")
        self.assertEqual(c.preflight["error"], "")
        self.assertIn("preflight", c.topics)
        self.assertEqual(len(c.preflight_history()), 1)

    def test_start_returns_at_once_while_the_work_is_blocked_and_a_second_start_is_refused(self):
        c = self.ctl()
        gate = threading.Event()

        def slow(cfg, **kw):
            gate.wait(10)
            return {"verdict": {"code": "PASSED", "text": "t"}, "checks": []}
        with mock.patch.object(runner, "run_preflight", slow):
            t0 = time.monotonic()
            self.assertTrue(c.preflight_start(False, False))
            self.assertLess(time.monotonic() - t0, 1.0)                     # the caller (interface thread) is not held up
            self.assertFalse(c.preflight_start(False, False))
            self.assertIn("already running", c.preflight["message"])
            gate.set()
            self.settle(c)

    def test_online_without_consent_does_not_start_and_touches_nothing(self):
        c = self.ctl()
        with mock.patch.object(runner, "run_preflight", side_effect=AssertionError("must not run")):
            self.assertFalse(c.preflight_start(True, False))
        self.assertFalse(c.preflight["running"])
        self.assertIn("confirmation", c.preflight["message"])
        self.assertFalse((self.cfg.data_dir / "preflight").exists())

    def test_cancel_reaches_the_runner(self):
        c = self.ctl()
        started, seen = threading.Event(), {}

        def waits(cfg, should_stop=None, **kw):
            started.set()
            end = time.time() + 10
            while not should_stop() and time.time() < end:
                time.sleep(0.01)
            seen["stopped"] = should_stop()
            return {"verdict": {"code": "CANCELLED", "text": "t"}, "checks": []}
        with mock.patch.object(runner, "run_preflight", waits):
            c.preflight_start(False, False)
            self.assertTrue(started.wait(5))
            c.preflight_cancel()
            self.settle(c)
        self.assertTrue(seen["stopped"])
        self.assertEqual(c.preflight["result"]["verdict"]["code"], "CANCELLED")

    def test_an_unexpected_exception_is_shown_as_an_error_never_as_a_result(self):
        c = self.ctl()
        with mock.patch.object(runner, "run_preflight", side_effect=RuntimeError("kaboom C:\\Users\\jane")):
            c.preflight_start(False, False)
            self.settle(c)
        self.assertIsNone(c.preflight["result"])
        self.assertIn("stopped unexpectedly", c.preflight["error"])
        self.assertNotIn("jane", c.preflight["error"])
        self.assertFalse(c.preflight["running"])

    def test_closing_the_app_asks_a_running_preflight_to_stop(self):
        c = AppController(self.cfg, simulate=True)
        flag = {}

        def waits(cfg, should_stop=None, **kw):
            while not should_stop():
                time.sleep(0.01)
            flag["stopped"] = True
            return {"verdict": {"code": "CANCELLED", "text": "t"}, "checks": []}
        with mock.patch.object(runner, "run_preflight", waits):
            c.preflight_start(False, False)
            time.sleep(0.1)
            c.shutdown(3.0)
        self.assertTrue(flag.get("stopped"))


# ------------------------------------------------------------------ the window's own logic against stand-in widgets (NOT Qt rendering)
class _Anything:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return mock.MagicMock()


def _fake_qt():
    mods = {}
    for name in ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"):
        m = mock.MagicMock(name=name)
        mods[name] = m
    core = mods["PySide6.QtCore"]
    core.Signal = lambda *a, **k: mock.MagicMock()
    core.Slot = lambda *a, **k: (lambda f: f)
    core.QObject = _Anything
    widgets = mods["PySide6.QtWidgets"]
    for cls in ("QMainWindow", "QPlainTextEdit", "QWidget", "QDialog"):
        setattr(widgets, cls, type(cls, (_Anything,), {}))
    mods["PySide6"].QtCore, mods["PySide6"].QtGui, mods["PySide6"].QtWidgets = core, mods["PySide6.QtGui"], widgets
    return mods


class FakeWidget:
    def __init__(self, checked=False):
        self.text, self.enabled, self.value, self.checked, self.plain, self.rows = "", True, 0, checked, "", None

    def setText(self, t):
        self.text = t

    def setEnabled(self, e):
        self.enabled = e

    def setValue(self, v):
        self.value = v

    def isChecked(self):
        return self.checked

    def setPlainText(self, t):
        self.plain = t


class WindowLogic(unittest.TestCase):
    """PySide6 is NOT available here, so `jarvis.ui.window` is imported against stand-in Qt modules and its readiness methods run on stand-in widgets.
    This checks the wiring and the text shown; it cannot show that anything draws, responds or looks right."""

    def setUp(self):
        patcher = mock.patch.dict(sys.modules, _fake_qt())
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("jarvis.ui.window", None)
        self.W = importlib.import_module("jarvis.ui.window")
        self.addCleanup(lambda: sys.modules.pop("jarvis.ui.window", None))
        self.W._fill = lambda table, rows: setattr(table, "rows", rows)
        self.win = object.__new__(self.W.MainWindow)
        w = self.win
        w.pf_run, w.pf_cancel, w.pf_online, w.pf_bar = FakeWidget(), FakeWidget(), FakeWidget(checked=True), FakeWidget()
        w.pf_current, w.pf_verdict, w.pf_table, w.pf_models, w.pf_blockers, w.pf_hist = (FakeWidget() for _ in range(6))
        self.ctl = mock.Mock()
        self.state = {"running": False, "fraction": 0.0, "current": "", "result": None, "error": "", "message": ""}
        self.ctl.preflight_state.return_value = self.state
        self.ctl.preflight_history.return_value = []
        w.ctl = self.ctl
        w.statusBar = lambda: mock.Mock()

    def dialog(self, choice):
        """A stand-in QMessageBox whose 'user' presses the named button."""
        outer = self

        class Box:
            Icon = mock.MagicMock()
            ButtonRole = mock.MagicMock()
            shown = []

            def __init__(self, *a):
                self.buttons = {}
                Box.shown.append(self)

            def addButton(self, text, role):
                self.buttons[text] = object()
                return self.buttons[text]

            def exec(self):
                pass

            def clickedButton(self):
                return self.buttons[choice] if choice else None

            def __getattr__(self, n):
                return lambda *a, **k: None
        self.W.QMessageBox = Box
        return Box

    def test_the_page_exists_is_named_and_has_a_valid_refresh_method(self):
        self.assertIn("System & Model Readiness", self.W.PAGES)
        self.assertEqual(self.W._slug("System & Model Readiness"), "system_model_readiness")
        for name in self.W.PAGES:
            self.assertTrue(hasattr(self.W.MainWindow, "_refresh_" + self.W._slug(name)), name)
        self.assertEqual(self.W._slug("Chat"), "chat")

    def test_cancel_in_the_dialog_starts_nothing(self):
        self.dialog("Cancel")
        self.win._pf_run()
        self.ctl.preflight_start.assert_not_called()
        self.dialog(None)
        self.win._pf_run()
        self.ctl.preflight_start.assert_not_called()

    def test_allow_starts_with_consent_and_offline_only_starts_without_it(self):
        self.dialog("Allow and run all checks")
        self.win._pf_run()
        self.ctl.preflight_start.assert_called_once_with(True, True)
        self.ctl.preflight_start.reset_mock()
        self.dialog("Run offline checks only")
        self.win._pf_run()
        self.ctl.preflight_start.assert_called_once_with(False, False)

    def test_unticked_online_box_runs_offline_without_any_dialog(self):
        box = self.dialog("Allow and run all checks")
        self.win.pf_online.checked = False
        self.win._pf_run()
        self.assertEqual(box.shown, [])
        self.ctl.preflight_start.assert_called_once_with(False, False)

    def test_the_dialog_defaults_to_the_safe_choice_and_says_what_it_does(self):
        calls = []

        class Box:
            Icon = mock.MagicMock()
            ButtonRole = mock.MagicMock()

            def __init__(self, *a):
                self.b = {}

            def addButton(self, text, role):
                self.b[text] = object()
                return self.b[text]

            def setText(self, t):
                calls.append(("text", t))

            def setDefaultButton(self, b):
                calls.append(("default", b is self.b["Cancel"]))

            def setEscapeButton(self, b):
                calls.append(("escape", b is self.b["Cancel"]))

            def clickedButton(self):
                return self.b["Cancel"]

            def __getattr__(self, n):
                return lambda *a, **k: None
        self.W.QMessageBox = Box
        self.win._pf_run()
        self.assertIn(("default", True), calls)
        self.assertIn(("escape", True), calls)
        text = " ".join(t for k, t in calls if k == "text")
        self.assertIn("huggingface.co", text)
        self.assertIn("No model is downloaded", text)

    def test_a_start_that_is_refused_is_explained(self):
        self.dialog("Allow and run all checks")
        self.ctl.preflight_start.return_value = False
        self.state["message"] = "A preflight is already running."
        shown = []
        self.win.statusBar = lambda: mock.Mock(showMessage=lambda *a: shown.append(a[0]))
        self.win._pf_run()
        self.assertEqual(shown, ["A preflight is already running."])

    def test_running_state_disables_run_enables_cancel_and_shows_progress(self):
        self.state.update(running=True, fraction=0.4, current="Checking: Free disk space")
        self.win._refresh_system_model_readiness()
        w = self.win
        self.assertEqual((w.pf_run.enabled, w.pf_cancel.enabled, w.pf_online.enabled), (False, True, False))
        self.assertEqual(w.pf_bar.value, 40)
        self.assertEqual(w.pf_current.text, "Running: Checking: Free disk space")

    def test_a_finished_result_is_shown_with_every_check_the_models_and_the_blockers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(data_dir=Path(tmp.name) / "d")
        probe_rec = runner.run_preflight(cfg, include_online=True, online_consent=True, fetch=good_fetch(), probe=FakeProbe(), policy=NetworkPolicy())
        self.state.update(result=probe_rec, fraction=1.0)
        self.ctl.preflight_history.return_value = history.list_runs(cfg)
        self.win._refresh_system_model_readiness()
        w = self.win
        self.assertTrue(w.pf_verdict.text.startswith("PASSED: "))
        self.assertEqual(len(w.pf_table.rows), len(probe_rec["checks"]))
        self.assertEqual({r[1] for r in w.pf_table.rows}, {"yes", "no"})
        self.assertIn(["Model metadata (online: contacts huggingface.co)", "yes", "OK"], [r[:3] for r in w.pf_table.rows])
        self.assertEqual(len(w.pf_models.rows), 5)
        self.assertTrue(all(len(str(r[3])) == 64 for r in w.pf_models.rows))
        self.assertIn("No independent second source", w.pf_blockers.plain)
        self.assertEqual(len(w.pf_hist.rows), 1)
        self.assertEqual((w.pf_run.enabled, w.pf_cancel.enabled), (True, False))

    def test_failure_and_error_states_are_shown_not_hidden(self):
        self.state.update(error="The preflight stopped unexpectedly: boom")
        self.win._refresh_system_model_readiness()
        self.assertIn("stopped unexpectedly", self.win.pf_current.text)
        self.assertEqual(self.win.pf_verdict.text, "")

    def test_text_from_outside_is_set_as_plain_text_and_the_window_has_no_process_or_network_code(self):
        src = (ROOT / "src" / "jarvis" / "ui" / "window.py").read_text(encoding="utf-8")
        self.assertIn("self.pf_verdict.setTextFormat(Qt.TextFormat.PlainText)", src)
        self.assertIn("self.pf_current.setTextFormat(Qt.TextFormat.PlainText)", src)
        for banned in ("subprocess", "os.system", "urllib", "shell=True"):
            self.assertNotIn(banned, src)


class PackagingAndSafety(unittest.TestCase):
    PKG = ROOT / "src" / "jarvis" / "preflight"

    def test_the_feature_lives_inside_the_jarvis_package_so_the_exe_contains_it(self):
        import manifest as MF
        for mod in ("runner", "metadata", "hf_fetch", "history"):
            self.assertTrue((self.PKG / f"{mod}.py").is_file())
            self.assertIn(f"jarvis.preflight.{mod}", MF.HIDDEN_REQUIRED)
        spec = (ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
        self.assertIn('collect_submodules("jarvis")', spec)
        self.assertIn("IN_APP_PREFLIGHT.md", MF.DOCS)
        self.assertTrue((ROOT / "docs" / "IN_APP_PREFLIGHT.md").is_file())
        self.assertIn("scripts", MF.FORBIDDEN_TOP_LEVEL)           # the scripts folder is NOT shipped, which is why the app must not depend on it

    def test_no_preflight_module_depends_on_the_scripts_folder_or_the_repository_layout(self):
        for f in self.PKG.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            for needle in ('"scripts"', "'scripts'", "parents[", "sys.path", "__file__", "spec_from_file_location"):
                self.assertNotIn(needle, text, f"{f.name} contains {needle!r}")

    def test_it_imports_and_runs_from_an_unrelated_working_directory_without_the_scripts_on_the_path(self):
        with tempfile.TemporaryDirectory() as t:
            code = "import sys; assert not any(p.rstrip('/').endswith('scripts') for p in sys.path); import jarvis.preflight.runner as r, jarvis.preflight.hf_fetch; print('ok')"
            res = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=t, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True, timeout=60)
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "ok"), res.stderr)

    def test_no_process_launching_shell_or_direct_network_code_in_the_package(self):
        banned_imports = {"subprocess", "socket", "http", "ssl", "requests", "urllib3", "ctypes"}
        for f in self.PKG.glob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], banned_imports, f"{f.name} imports {m}")
                    self.assertFalse(m.startswith("urllib.request") or m.startswith("urllib.error"), f"{f.name} imports {m}")
            text = f.read_text(encoding="utf-8")
            for needle in ("os.system", "shell=True", "Popen(", "eval(", "exec(", "os.startfile"):
                self.assertNotIn(needle, text, f"{f.name} contains {needle}")

    def test_the_package_never_asks_for_administrator_rights(self):
        for f in self.PKG.glob("*.py"):
            text = f.read_text(encoding="utf-8").lower()
            for needle in ("runas", "shellexecute", "isuseranadmin", "elevat"):
                self.assertNotIn(needle, text, f"{f.name} mentions {needle}")

    def test_the_default_model_catalog_and_pin_files_are_untouched_by_the_feature(self):
        for f in self.PKG.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            for needle in ("set_active(", "catalog.BUILTIN", "download_gguf", "install_gguf", "install_package", "pip install"):
                self.assertNotIn(needle, text, f"{f.name} contains {needle}")


if __name__ == "__main__":
    unittest.main()
