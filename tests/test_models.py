import http.server
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jarvis.models.manager import IntegrityError, ModelStore, load_catalog
from jarvis.models.package import PackageError, pack_model, sha256_file
from jarvis.net.policy import NetworkDisabled, NetworkPolicy
from jarvis.security import signing


def make_model_dir(root: Path, tag: bytes = b"v1") -> Path:
    d = root / f"src-{tag.decode()}"
    d.mkdir()
    (d / "model.pt").write_bytes(b"weights-" + tag)
    (d / "tokenizer.json").write_text("{}")
    (d / "LICENSE.txt").write_text("test license")
    return d


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class ModelStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ModelStore(self.root / "models")

    def tearDown(self):
        self.tmp.cleanup()

    def pkg(self, tag=b"v1"):
        out = self.root / f"{tag.decode()}.jarvismodel"
        sha = pack_model(make_model_dir(self.root, tag), out, "MIT")
        return out, sha

    def test_pack_requires_license_file(self):
        d = self.root / "nolicense"
        d.mkdir()
        (d / "model.pt").write_bytes(b"x")
        (d / "tokenizer.json").write_text("{}")
        with self.assertRaises(PackageError):
            pack_model(d, self.root / "x.jarvismodel", "MIT")

    def test_install_verify_select_remove(self):
        p, sha = self.pkg()
        meta = self.store.install_package(p, "tiny", expected_sha256=sha)
        self.assertTrue(meta["verified_origin"])
        self.assertEqual([m["id"] for m in self.store.installed()], ["tiny"])
        self.assertEqual(self.store.verify("tiny"), (True, []))
        self.store.set_active("tiny")
        self.assertEqual(self.store.active(), "tiny")
        self.assertGreater(self.store.disk_usage_bytes(), 0)
        (self.store.path("tiny") / "model.pt").write_bytes(b"tampered")
        ok, problems = self.store.verify("tiny")
        self.assertFalse(ok)
        self.assertIn("model.pt: hash mismatch", problems)
        with self.assertRaises(IntegrityError):
            self.store.set_active("tiny")
        self.assertTrue(self.store.remove("tiny"))
        self.assertEqual(self.store.installed(), [])
        self.assertIsNone(self.store.active())

    def test_wrong_hash_rejected_and_nothing_left_behind(self):
        p, _ = self.pkg()
        with self.assertRaises(IntegrityError):
            self.store.install_package(p, "tiny", expected_sha256="0" * 64)
        self.assertEqual(list((self.root / "models").iterdir()), [])

    def test_unlisted_package_needs_explicit_confirmation(self):
        p, _ = self.pkg()
        with self.assertRaises(IntegrityError):
            self.store.install_package(p, "tiny")
        meta = self.store.install_package(p, "tiny", confirm_unlisted=True)
        self.assertFalse(meta["verified_origin"])

    def test_malicious_packages_rejected(self):
        evil = self.root / "evil.jarvismodel"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("manifest.json", json.dumps({"format": 1, "license": "MIT", "files": {"../evil": "0"}}))
            z.writestr("../evil", "x")
        with self.assertRaises(PackageError):
            self.store.install_package(evil, "evil", confirm_unlisted=True)
        # inner hash mismatch
        p, _ = self.pkg()
        bad = self.root / "bad.jarvismodel"
        with zipfile.ZipFile(p) as zin, zipfile.ZipFile(bad, "w") as zout:
            for n in zin.namelist():
                zout.writestr(n, b"corrupted" if n == "model.pt" else zin.read(n))
        with self.assertRaises(PackageError):
            self.store.install_package(bad, "bad", confirm_unlisted=True)
        # size bomb
        from jarvis.models.package import validate_and_extract
        with self.assertRaises(PackageError):
            validate_and_extract(p, self.root / "bomb", max_bytes=5)
        self.assertEqual(self.store.installed(), [])

    def test_bad_ids_rejected(self):
        p, sha = self.pkg()
        for bad in ["../x", "A", "", "a/b", "x" * 100]:
            with self.assertRaises(ValueError):
                self.store.install_package(p, bad, expected_sha256=sha)

    def test_upgrade_and_rollback(self):
        p1, s1 = self.pkg(b"v1")
        p2, s2 = self.pkg(b"v2")
        self.store.install_package(p1, "lm", expected_sha256=s1)
        self.store.install_package(p2, "lm", expected_sha256=s2)
        self.assertEqual((self.store.path("lm") / "model.pt").read_bytes(), b"weights-v2")
        self.assertTrue(self.store.rollback("lm"))
        self.assertEqual((self.store.path("lm") / "model.pt").read_bytes(), b"weights-v1")
        self.assertEqual(len(self.store.installed()), 1)
        self.assertTrue(self.store.remove("lm"))
        self.assertEqual(list((self.root / "models").iterdir()), [])
        self.assertFalse(self.store.rollback("lm"))

    def test_signed_catalog(self):
        sk, pk = signing.generate_keypair()
        manifest = {"version": 1, "models": [{"id": "tiny", "sha256": "ab"}]}
        sig = signing.sign(manifest, sk)
        self.assertEqual(load_catalog(manifest, sig, pk)[0]["id"], "tiny")
        manifest["models"][0]["sha256"] = "cd"
        with self.assertRaises(IntegrityError):
            load_catalog(manifest, sig, pk)


class DownloadTests(unittest.TestCase):
    """Real HTTP downloads from a localhost server (the only network use in the test suite)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.www = cls.root / "www"
        cls.www.mkdir()
        cls.sha = pack_model(make_model_dir(cls.root), cls.www / "tiny.jarvismodel", "MIT")
        cls.size = (cls.www / "tiny.jarvismodel").stat().st_size
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(cls.www)))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def entry(self, **kw):
        e = {"id": "tiny", "url": f"http://127.0.0.1:{self.port}/tiny.jarvismodel", "sha256": self.sha,
             "size_bytes": self.size}
        e.update(kw)
        return e

    def store(self, enable=True):
        pol = NetworkPolicy(allow_insecure_localhost=True)
        if enable:
            pol.enable("model_download")
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return ModelStore(Path(d.name), pol), pol

    def test_download_succeeds_and_is_audited(self):
        s, pol = self.store()
        seen = []
        meta = s.download(self.entry(), progress=lambda a, b: seen.append(a))
        self.assertEqual(meta["id"], "tiny")
        self.assertTrue(s.verify("tiny")[0])
        self.assertEqual(seen[-1], self.size)
        self.assertEqual([a["feature"] for a in pol.audit], ["model_download"])
        self.assertEqual(pol.audit[0]["bytes_sent"], 0)

    def test_download_refused_when_feature_disabled(self):
        s, pol = self.store(enable=False)
        with self.assertRaises(NetworkDisabled):
            s.download(self.entry())
        self.assertEqual(pol.audit, [])
        with pol.grant_once("model_download"):
            s.download(self.entry())
        self.assertFalse(pol.is_enabled("model_download"))

    def test_redirect_to_disallowed_target_is_blocked(self):
        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://example.invalid/evil.jarvismodel")  # http, non-local
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        s, pol = self.store()
        with self.assertRaises(NetworkDisabled):
            s.download(self.entry(url=f"http://127.0.0.1:{srv.server_address[1]}/x"))
        self.assertEqual([a["host"] for a in pol.audit], ["127.0.0.1"])  # the bad hop was never audited as fetched
        self.assertEqual(s.installed(), [])

    def test_wrong_hash_and_oversize_rejected(self):
        s, _ = self.store()
        with self.assertRaises(IntegrityError):
            s.download(self.entry(sha256="0" * 64))
        with self.assertRaises(IntegrityError):
            s.download(self.entry(size_bytes=10))
        self.assertEqual(s.installed(), [])
        self.assertEqual([p.name for p in s.dir.iterdir()], [])


if __name__ == "__main__":
    unittest.main()
