"""Import Brain: catalog details, download preflight, consent, progress, cancel, cleanup, checksum honesty.

LABELS: the network is MOCKED (no host was contacted, no model was downloaded); the downloaded 'model' is a few-hundred-byte SYNTHETIC GGUF with no
weights; free disk space is a MOCKED reading where stated. These tests check Jarvis's own flow and cleanup. They do not show that a real multi-GB download
works, and they cannot show Windows behaviour."""
import hashlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p41_common as C                                    # noqa: E402
from test_p29_download_safety import FakeResp             # noqa: E402
from jarvis.app.setup_engine import SetupEngine           # noqa: E402
from jarvis.models import catalog, diskspace              # noqa: E402

MID = "qwen3-1.7b-q4_k_m"
REV = "a" * 40
HUGE = 10 ** 13


class Download(C.BrainCase):
    def setUp(self):
        super().setUp()
        self.body = C.build_gguf(self.files / "dl.gguf", arch="qwen3", size=0).read_bytes()
        self.sha = hashlib.sha256(self.body).hexdigest()
        self.setup = SetupEngine(self.cfg, self.store, self.policy, sleep=lambda s: None, hardware=C.hw(), runtime={"available": True, "gpu_offload_supported": False})
        self.mgr = self.make(C.hw(), setup=self.setup)
        for p in (mock.patch.object(diskspace, "free_bytes", return_value=HUGE),
                  mock.patch("jarvis.app.setup_engine.shutil.disk_usage", return_value=type("U", (), {"free": HUGE})())):
            p.start()
            self.addCleanup(p.stop)

    def info(self, **kw):
        d = {"name": "Qwen3-1.7B-Q4_K_M.gguf", "url": "https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf",
             "size_bytes": len(self.body), "sha256": self.sha, "revision": REV}
        d.update(kw)
        return d

    def run_dl(self, resp=None, info=None, **kw):
        with mock.patch("jarvis.models.catalog.resolve_file", return_value=info or self.info()), \
                mock.patch.object(self.policy, "open", return_value=resp or FakeResp(self.body)) as op:
            self.opened = op
            return self.mgr.download(MID, **kw)

    def leftovers(self):
        return sorted(p.name for p in self.store.dir.iterdir() if p.name.startswith("."))

    def test_without_consent_nothing_is_fetched(self):
        r = self.run_dl()
        self.assertEqual((r["ok"], r["kind"]), (False, "consent_required"))
        self.opened.assert_not_called()
        self.assertEqual(list(self.store.dir.iterdir()), [])

    def test_with_consent_it_installs_but_does_not_activate_and_records_provenance(self):
        self.store.set_active  # noqa: B018 - presence only
        r = self.run_dl(consent=True)
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["activated"])
        self.assertIsNone(self.store.active())
        self.assertIn("NOT active", r["message"])
        m = self.store.meta(MID)
        self.assertEqual((m["origin"], m["source_repo"], m["source_revision"]), ("catalog", "ggml-org/Qwen3-1.7B-GGUF", REV))
        self.assertTrue(m["verified_origin"])
        self.assertFalse(m["checksum_reference"]["independent_of_host"])           # same host: damage detection, not trust in the host
        self.assertEqual(m["package_sha256"], self.sha)
        self.assertEqual(self.leftovers(), [])
        b = self.mgr.installed_brains()[0]
        self.assertEqual((b["origin"], b["source_revision"]), ("catalog", REV))
        self.assertIn("same host", b["verification"])

    def test_the_integrity_report_does_not_overclaim(self):
        r = self.run_dl(consent=True)
        self.assertFalse(r["integrity"]["independent_of_host"])
        self.assertIn("same host", r["integrity"]["expected_checksum_source"])

    def test_active_brain_is_untouched_by_a_download(self):
        other = self.imp("o.gguf")
        self.mgr.activate(other)
        self.run_dl(consent=True)
        self.assertEqual(self.store.active(), other)
        self.assertEqual(self.host.model_id, other)

    def test_checksum_mismatch_is_rejected_and_cleaned_up(self):
        r = self.run_dl(info=self.info(sha256="0" * 64), consent=True)
        self.assertEqual((r["ok"], r["kind"]), (False, "integrity"))
        self.assertIn("SHA-256", r["message"])
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.leftovers(), [])

    def test_no_published_checksum_refuses_and_never_invents_one(self):
        r = self.run_dl(info=self.info(sha256=None), consent=True)
        self.assertEqual((r["ok"], r["kind"]), (False, "integrity"))
        self.assertIn("did not publish a checksum", r["message"])
        self.assertEqual(self.store.installed(), [])

    def test_a_valid_gguf_of_another_architecture_is_rejected(self):
        body = C.build_gguf(self.files / "x.gguf", arch="llama", size=0).read_bytes()
        r = self.run_dl(resp=FakeResp(body), info=self.info(size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest()), consent=True)
        self.assertEqual((r["ok"], r["kind"]), (False, "integrity"))
        self.assertIn("architecture", r["message"])
        self.assertEqual((self.store.installed(), self.leftovers()), ([], []))

    def test_truncated_download_is_rejected_and_cleaned(self):
        r = self.run_dl(resp=FakeResp(self.body[:-10]), consent=True)
        self.assertFalse(r["ok"])
        self.assertEqual((self.store.installed(), self.leftovers()), ([], []))

    def test_connection_dropping_mid_download_is_retried_then_reported_with_no_leftovers(self):
        class Dropping(FakeResp):
            def read(self, n=-1):
                raise ConnectionResetError("simulated drop")
        r = self.run_dl(resp=Dropping(self.body), consent=True)
        self.assertEqual((r["ok"], r["kind"]), (False, "network"))
        self.assertIn("Nothing was installed", r["message"])
        self.assertEqual((self.store.installed(), self.leftovers()), ([], []))

    def test_cancel_stops_the_download_and_removes_the_partial_file(self):
        class Flag:
            def __init__(self):
                self.n = 0

            def is_set(self):
                return self.n > 0
        flag = Flag()
        calls = []

        def progress(done, total):
            calls.append((done, total))
            flag.n += 1
        big = self.body + b"\0" * 5_000_000
        info = self.info(size_bytes=len(big), sha256=hashlib.sha256(big).hexdigest())
        r = self.run_dl(resp=FakeResp(big), info=info, consent=True, cancel=flag, progress=progress)
        self.assertEqual((r["ok"], r["kind"]), (False, "cancelled"))
        self.assertEqual((self.store.installed(), self.leftovers()), ([], []))
        self.assertTrue(calls)

    def test_progress_is_reported_and_ends_at_the_total(self):
        calls = []
        big = self.body + b"\0" * 3_000_000
        info = self.info(size_bytes=len(big), sha256=hashlib.sha256(big).hexdigest())
        r = self.run_dl(resp=FakeResp(big), info=info, consent=True, progress=lambda d, t: calls.append((d, t)))
        self.assertTrue(r["ok"], r)
        self.assertEqual(calls[-1], (len(big), len(big)))
        self.assertEqual([c[0] for c in calls], sorted(c[0] for c in calls))

    def test_not_enough_disk_refuses_before_any_network_use(self):
        usage = type("U", (), {"free": 500_000_000})()
        with mock.patch("jarvis.app.setup_engine.shutil.disk_usage", return_value=usage):
            r = self.run_dl(consent=True)
        self.assertEqual((r["ok"], r["kind"]), (False, "disk_space"))
        self.assertIn("Not enough free disk space", r["message"])
        self.opened.assert_not_called()

    def test_busy_while_downloading_blocks_other_operations_and_shows_the_state(self):
        seen = {}

        class Peek(FakeResp):
            def read(s, n=-1):
                seen.setdefault("busy", self.mgr.current_operation)
                seen.setdefault("other", self.mgr.deactivate())
                return super().read(n)
        self.run_dl(resp=Peek(self.body), consent=True)
        self.assertEqual(seen["busy"], "downloading a model")
        self.assertEqual(seen["other"]["kind"], "busy")


class Preflight(C.BrainCase):
    def test_preflight_shows_everything_and_downloads_nothing(self):
        with mock.patch.object(diskspace, "free_bytes", return_value=HUGE), mock.patch.object(self.policy, "open") as op:
            p = self.mgr.download_preflight("qwen3-8b-q4_k_m")
        op.assert_not_called()
        self.assertTrue(p["nothing_downloaded"] and not p["will_activate"])
        self.assertEqual((p["download_gb"], p["license"]), (5.03, "Apache-2.0"))
        self.assertIn("same host", p["verification"])
        self.assertIn("does not prove the host itself is trustworthy", p["verification"])
        self.assertTrue(any("large download" in w for w in p["warnings"]))

    def test_preflight_warns_when_disk_is_short(self):
        with mock.patch.object(diskspace, "free_bytes", return_value=1_000_000_000):
            p = self.mgr.download_preflight(MID)
        self.assertFalse(p["ok"])
        self.assertTrue(any("Not enough free disk space" in w for w in p["warnings"]))

    def test_preflight_warns_when_the_pc_is_too_small_for_the_model(self):
        small = self.make(C.hw(ram=4.0, avail=3.0))
        with mock.patch.object(diskspace, "free_bytes", return_value=HUGE):
            p = small.download_preflight("qwen3-8b-q4_k_m")
        self.assertEqual(p["compat"]["level"], "unsupported")
        self.assertTrue(any("in total" in w for w in p["warnings"]))

    def test_unknown_model_is_a_clear_error(self):
        self.assertEqual(self.mgr.download_preflight("nope")["kind"], "unknown_model")


class CatalogDetails(C.BrainCase):
    def test_every_entry_has_the_required_details_and_labels_its_basis(self):
        rows = self.mgr.catalog()
        self.assertEqual(len(rows), len(catalog.BUILTIN))
        for r in rows:
            for k in ("id", "name", "source", "architecture", "format", "quantization", "size", "license", "memory_need_gb", "compatibility", "install_status", "verification"):
                self.assertIn(k, r, (r["id"], k))
            self.assertEqual(r["memory_need_gb"]["basis"], "estimate")
            self.assertIn("basis", r["size"])
            self.assertEqual(r["install_status"], "not installed")
            self.assertIn("nothing verified", r["verification"])
            self.assertIn("not recorded", r["source"]["revision"])

    def test_unconfirmed_metadata_is_called_unconfirmed(self):
        r = next(x for x in self.mgr.catalog() if not x["metadata_confirmed"])
        self.assertIn("NOT confirmed", r["license"]["basis"])
        self.assertIn("not confirmed", r["size"]["basis"])

    def test_the_8b_is_listed_but_never_auto_recommended(self):
        r = next(x for x in self.mgr.catalog() if x["id"] == "qwen3-8b-q4_k_m")
        self.assertFalse(r["auto_recommend"])

    def test_catalog_rows_render_without_errors(self):
        from jarvis.brains import viewmodel as V
        rows = V.catalog_rows(self.mgr.catalog())
        self.assertEqual(len(rows), len(catalog.BUILTIN))
        for c in self.mgr.catalog():
            self.assertIn("[estimate]", V.catalog_detail(c))


if __name__ == "__main__":
    unittest.main()
