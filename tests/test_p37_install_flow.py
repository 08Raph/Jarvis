"""Install-flow honesty tests (Stage 5, 8B authorization pass). Everything here uses FAKES from test_p20_setup (a scripted 'model', a fake network): it proves
the logic of the consent text, the integrity report and the identity check. It does NOT prove a real download, a real load or real inference."""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_p20_setup as P20
from jarvis.app import setup_engine as SE
from jarvis.eval import winvalidate
from jarvis.models import catalog

M8 = "qwen3-8b-q4_k_m"


def gguf_with(path, arch, name="Fake"):
    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kvs = {"general.architecture": arch, "general.name": name, "general.license": "apache-2.0"}
    body = b"".join(s(k) + struct.pack("<I", 8) + s(v) for k, v in kvs.items())
    Path(path).write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs)) + body + b"\0" * 1000)
    return path


class ConsentInformation(P20.Base):
    def test_every_model_row_tells_the_user_where_it_comes_from_what_it_needs_and_how_it_is_checked(self):
        rows = {r["id"]: r for r in self.engine().recommend()["models"]}
        r = rows[M8]
        self.assertEqual((r["repo"], r["quantization"], r["license"]), ("Qwen/Qwen3-8B-GGUF", "Q4_K_M", "Apache-2.0"))
        self.assertEqual(r["download_gb"], 5.03)
        self.assertGreater(r["needs_free_gb"], r["download_gb"])                 # room for the file plus a margin, as download() requires
        self.assertFalse(r["auto_recommended"])
        v = r["verification"]
        self.assertIn("same host", v)                                              # the checksum is NOT described as independent
        self.assertIn("refused", v)
        self.assertNotIn("independently verified", v.replace("not independently verified", ""))

    def test_the_default_recommendation_is_unchanged_by_the_8b_correction(self):
        for hw in (P20.CPU_ONLY, P20.RTX):
            self.assertNotEqual(self.engine(hw=hw).recommend()["recommended"], M8)

    def test_a_download_that_needs_more_space_than_free_is_refused_before_anything_is_fetched(self):
        eng = self.engine()
        store_download = mock.Mock()
        eng.store = mock.Mock(dir=self.d, download_gguf=store_download, active=mock.Mock(return_value=None))
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")
        with mock.patch.object(SE.shutil, "disk_usage", return_value=usage(10**12, 0, 6_000_000_000)):         # 6 GB free < 8B need (about 6.5 GB)
            res = eng.download(M8)
        self.assertEqual((res["ok"], res["kind"]), (False, "disk_space"))
        store_download.assert_not_called()


class IntegrityReport(P20.Base):
    def test_a_successful_download_reports_what_was_and_was_not_checked(self):
        eng = self.engine()
        meta = {"id": MID_OK}
        with mock.patch.object(eng.store, "download_gguf", return_value=meta), mock.patch.object(eng.store, "set_active"):
            res = eng.download(MID_OK)
        self.assertTrue(res["ok"])
        i = res["integrity"]
        self.assertTrue(i["size_checked"] and i["sha256_computed_locally"])
        self.assertFalse(i["independent_of_host"])
        self.assertIn("same host", i["expected_checksum_source"])


MID_OK = P20.MID


class IdentityCheck(P20.Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(winvalidate, "Sampler", P20.FakeSampler)
        p.start()
        self.addCleanup(p.stop)

    def install_arch(self, mid, arch):
        self.store.install_gguf(gguf_with(self.d / f"{arch}.gguf", arch, "Qwen3-8B"), mid, "Apache-2.0", "t")
        self.store.set_active(mid)

    def test_a_file_with_the_expected_architecture_passes_and_the_header_is_reported(self):
        self.install_arch(M8, "qwen3")
        v = self.engine().verify(M8)
        c = next(c for c in v["checks"] if c["name"] == "model identity (GGUF header)")
        self.assertTrue(c["ok"])
        self.assertIn("qwen3", c["detail"])
        self.assertIn("self-declared", c["detail"])           # not presented as proof of origin
        self.assertTrue(v["ok"])

    def test_a_valid_gguf_of_another_architecture_installed_under_a_qwen3_id_is_rejected_before_any_loading(self):
        self.install_arch(M8, "llama")
        eng = self.engine()
        with mock.patch.object(P20.FakeLlama, "__init__", side_effect=AssertionError("the model must not be loaded")):
            v = eng.verify(M8)
        self.assertFalse(v["ok"])
        c = next(c for c in v["checks"] if c["name"] == "model identity (GGUF header)")
        self.assertFalse(c["ok"])
        self.assertIn("not qwen3", c["detail"])
        self.assertIn("recovery", v)

    def test_an_install_without_a_recorded_header_is_reported_not_failed(self):
        self.install_arch(M8, "qwen3")
        mp = self.store.path(M8) / "meta.json"
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta.pop("gguf")
        mp.write_text(json.dumps(meta), encoding="utf-8")
        ok, detail = self.engine()._identity(M8)
        self.assertTrue(ok)
        self.assertIn("not checked", detail)


if __name__ == "__main__":
    unittest.main()
