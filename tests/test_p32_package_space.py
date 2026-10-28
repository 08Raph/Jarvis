"""Stage 5 model-verification milestone: up-front free-space check for installing a local signed-package (.jarvismodel) file.

Linux only, mocked disk readings, no network. The estimate is the sum of the sizes DECLARED in the zip directory (untrusted, but extraction still
counts real bytes against the package size cap and verifies every file hash); it does not prove Windows behaviour."""
import errno
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_models import make_model_dir  # noqa: E402
from jarvis.models import diskspace, manager, package  # noqa: E402
from jarvis.models.diskspace import DiskSpaceError  # noqa: E402
from jarvis.models.manager import IntegrityError, ModelStore  # noqa: E402
from jarvis.models.package import PackageError, pack_model  # noqa: E402
from jarvis.net.policy import NetworkPolicy  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.models = self.d / "models"
        self.store = ModelStore(self.models, NetworkPolicy())
        p = mock.patch.object(diskspace, "free_bytes", return_value=10 ** 13)
        self.free = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def pkg(self, tag=b"v1"):
        out = self.d / f"{tag.decode()}.jarvismodel"
        sha = pack_model(make_model_dir(self.d, tag), out, "MIT")
        return out, sha

    def hidden(self):
        return sorted(p.name for p in self.models.iterdir() if p.name.startswith("."))

    @staticmethod
    def declared(path):
        with zipfile.ZipFile(path) as z:
            return sum(i.file_size for i in z.infolist())


class DeclaredSize(Base):
    def test_the_estimate_is_the_sum_of_the_declared_member_sizes(self):
        p, _ = self.pkg()
        self.assertEqual(package.declared_unpacked_bytes(p), self.declared(p))
        self.assertGreater(self.declared(p), 0)

    def test_an_unreadable_package_has_no_estimate(self):
        bad = self.d / "bad.jarvismodel"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(PackageError):
            package.declared_unpacked_bytes(bad)
        with self.assertRaises(PackageError):
            package.declared_unpacked_bytes(self.d / "missing.jarvismodel")

    def test_huge_declared_sizes_cannot_overflow_or_wrap_the_estimate(self):
        class E:
            def __init__(self, n):
                self.file_size = n

        class FakeZip:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def infolist(self):
                return [E(2 ** 64 - 1), E(2 ** 64 - 1)]          # zip64 allows sizes up to 2**64 - 1
        with mock.patch.object(package.zipfile, "ZipFile", FakeZip):
            with self.assertRaises(PackageError):
                package.declared_unpacked_bytes("whatever.jarvismodel")

    def test_a_package_that_declares_more_than_the_allowed_size_is_refused(self):
        p, _ = self.pkg()
        with self.assertRaises(PackageError):
            package.declared_unpacked_bytes(p, max_bytes=5)


class InstallSpace(Base):
    def test_not_enough_free_space_is_refused_before_anything_is_extracted_and_models_are_preserved(self):
        p1, s1 = self.pkg(b"v1")
        p2, s2 = self.pkg(b"v2")
        p3, s3 = self.pkg(b"v3")
        self.store.install_package(p1, "tiny", expected_sha256=s1)
        self.store.install_package(p2, "tiny", expected_sha256=s2)
        cur, prev = (self.models / "tiny" / "model.pt").read_bytes(), (self.models / ".prev-tiny" / "model.pt").read_bytes()
        need = diskspace.required_free(self.declared(p3) + diskspace.META_OVERHEAD)
        self.free.return_value = need - 1
        with mock.patch.object(manager, "validate_and_extract", side_effect=AssertionError("must not extract")):
            with self.assertRaises(DiskSpaceError) as cm:
                self.store.install_package(p3, "tiny", expected_sha256=s3)
        self.assertIn("needed", str(cm.exception))
        self.assertIn("available", str(cm.exception))
        self.assertNotIn(str(self.d), str(cm.exception))
        self.assertEqual((self.models / "tiny" / "model.pt").read_bytes(), cur)
        self.assertEqual((self.models / ".prev-tiny" / "model.pt").read_bytes(), prev)
        self.assertEqual(self.hidden(), [".prev-tiny"])
        self.assertTrue(self.store.verify("tiny")[0])

    def test_exactly_enough_space_installs(self):
        p, s = self.pkg()
        self.free.return_value = diskspace.required_free(self.declared(p) + diskspace.META_OVERHEAD)
        self.store.install_package(p, "tiny", expected_sha256=s)
        self.assertTrue(self.store.verify("tiny")[0])

    def test_free_space_that_cannot_be_read_refuses_the_install(self):
        p, s = self.pkg()
        self.free.side_effect = DiskSpaceError("could not determine the free disk space for the model folder")
        with mock.patch.object(manager, "validate_and_extract", side_effect=AssertionError("must not extract")):
            with self.assertRaises(DiskSpaceError):
                self.store.install_package(p, "tiny", expected_sha256=s)
        self.assertEqual(self.store.installed(), [])

    def test_the_hash_and_trust_checks_still_come_first_and_are_not_weakened(self):
        p, s = self.pkg()
        self.free.side_effect = AssertionError("free space must not be read before the package is trusted")
        with self.assertRaises(IntegrityError) as cm:
            self.store.install_package(p, "tiny", expected_sha256="0" * 64)
        self.assertIn("does not match", str(cm.exception))
        with self.assertRaises(IntegrityError):
            self.store.install_package(p, "tiny")                      # no trusted hash and no confirmation
        self.assertEqual(self.store.installed(), [])

    def test_an_unreadable_package_fails_safely_with_nothing_created(self):
        bad = self.d / "bad.jarvismodel"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(PackageError):
            self.store.install_package(bad, "tiny", confirm_unlisted=True)
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.hidden(), [])

    def test_a_tampered_member_fails_extraction_cleans_up_and_keeps_the_installed_model(self):
        p1, s1 = self.pkg(b"v1")
        self.store.install_package(p1, "tiny", expected_sha256=s1)
        cur = (self.models / "tiny" / "model.pt").read_bytes()
        p2, _ = self.pkg(b"v2")
        tampered = self.d / "tampered.jarvismodel"
        with zipfile.ZipFile(p2) as zin, zipfile.ZipFile(tampered, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                zout.writestr(info.filename, b"EVIL" if info.filename == "model.pt" else data)
        with self.assertRaises(PackageError) as cm:
            self.store.install_package(tampered, "tiny", confirm_unlisted=True)
        self.assertIn("hash mismatch", str(cm.exception))
        self.assertEqual((self.models / "tiny" / "model.pt").read_bytes(), cur)
        self.assertEqual(self.hidden(), [])
        self.assertTrue(self.store.verify("tiny")[0])

    def test_a_package_that_lies_about_its_size_is_still_capped_by_the_real_byte_count(self):
        p, _ = self.pkg()
        with self.assertRaises(PackageError):
            package.validate_and_extract(p, self.d / "bomb", max_bytes=5)
        self.assertFalse(any(self.models.iterdir()) if self.models.exists() else False)


def _central_headers(raw):
    i = 0
    while True:
        i = raw.find(b"PK\x01\x02", i)
        if i < 0:
            return
        yield i
        i += 4


class MalformedPackages(Base):
    """Review of the package path (Stage 5 readiness): damaged or hostile archives must end in a clean PackageError, never a raw library
    exception, must leave nothing behind and must not touch an installed model or its rollback copy."""

    def rewrite(self, manifest_fn=None, extra=()):
        src, _ = self.pkg(b"v9")
        out = self.d / "x.jarvismodel"
        import warnings
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for i in zin.infolist():
                data = zin.read(i.filename)
                zout.writestr(i.filename, manifest_fn(data) if (i.filename == "manifest.json" and manifest_fn) else data)
            for n, dta in extra:
                zout.writestr(n, dta)
        return out

    def patch_headers(self, fn, big=False):
        src = self.d / "b.jarvismodel"
        md = make_model_dir(self.d, b"v8")
        if big:
            (md / "model.pt").write_bytes(bytes(range(256)) * 400)
        pack_model(md, src, "MIT")
        raw = bytearray(src.read_bytes())
        fn(raw)
        out = self.d / "p.jarvismodel"
        out.write_bytes(bytes(raw))
        return out

    def assert_clean_failure(self, pkg):
        p1, s1 = self.pkg(b"v1")
        p2, s2 = self.pkg(b"v2")
        self.store.install_package(p1, "tiny", expected_sha256=s1)
        self.store.install_package(p2, "tiny", expected_sha256=s2)
        cur, prev = (self.models / "tiny" / "model.pt").read_bytes(), (self.models / ".prev-tiny" / "model.pt").read_bytes()
        with self.assertRaises(PackageError) as cm:
            self.store.install_package(pkg, "tiny", confirm_unlisted=True)
        self.assertNotIn(str(self.d), str(cm.exception))
        self.assertEqual((self.models / "tiny" / "model.pt").read_bytes(), cur)
        self.assertEqual((self.models / ".prev-tiny" / "model.pt").read_bytes(), prev)
        self.assertEqual(self.hidden(), [".prev-tiny"])
        self.assertTrue(self.store.verify("tiny")[0])

    def test_a_manifest_that_is_not_a_json_object(self):
        self.assert_clean_failure(self.rewrite(lambda m: b"[]"))

    def test_a_deeply_nested_manifest_does_not_crash_the_parser(self):
        self.assert_clean_failure(self.rewrite(lambda m: b"[" * 200000 + b"]" * 200000))

    def test_a_manifest_that_is_not_valid_text(self):
        self.assert_clean_failure(self.rewrite(lambda m: b"\xff\xfe{"))

    def test_a_files_entry_that_is_not_a_mapping(self):
        self.assert_clean_failure(self.rewrite(lambda m: json.dumps({"format": 1, "files": ["model.pt"]}).encode()))

    def test_duplicate_member_names_are_rejected_whichever_copy_is_valid(self):
        good = self.rewrite()
        with zipfile.ZipFile(good) as z:
            real = z.read("model.pt")
        for first, second in ((b"junk", real), (real, b"junk")):
            import warnings
            out = self.d / "dup.jarvismodel"
            with zipfile.ZipFile(good) as zin, zipfile.ZipFile(out, "w") as zout, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for i in zin.infolist():
                    if i.filename == "model.pt":
                        zout.writestr("model.pt", first)
                        zout.writestr("model.pt", second)
                    else:
                        zout.writestr(i.filename, zin.read(i.filename))
            with self.subTest(order="junk-first" if first == b"junk" else "valid-first"):
                with self.assertRaises(PackageError) as cm:
                    package.validate_and_extract(out, self.d / "dupdest")
                self.assertIn("duplicate", str(cm.exception))

    def test_a_damaged_compressed_stream(self):
        def damage(raw):
            with zipfile.ZipFile(self.d / "b.jarvismodel") as z:
                i = z.getinfo("model.pt")
            off = i.header_offset + 30 + len(i.filename) + 20
            for k in range(40):
                raw[off + k] ^= 0xFF
        self.assert_clean_failure(self.patch_headers(damage, big=True))

    def test_an_unsupported_compression_method(self):
        def f(raw):
            for i in _central_headers(raw):
                raw[i + 10:i + 12] = (99).to_bytes(2, "little")
        self.assert_clean_failure(self.patch_headers(f))

    def test_an_encrypted_entry(self):
        def f(raw):
            for i in _central_headers(raw):
                raw[i + 8] |= 1
        self.assert_clean_failure(self.patch_headers(f))

    def test_a_declared_size_smaller_than_the_real_data_cannot_write_extra_bytes(self):
        def f(raw):
            for i in _central_headers(raw):
                n = int.from_bytes(raw[i + 28:i + 30], "little")
                if bytes(raw[i + 46:i + 46 + n]) == b"model.pt":
                    raw[i + 24:i + 28] = (10).to_bytes(4, "little")
        self.assert_clean_failure(self.patch_headers(f, big=True))

    def test_random_damage_to_the_zip_directory_only_ever_gives_a_package_error(self):
        import random
        rng = random.Random(1234)
        p, _ = self.pkg(b"v7")
        base = p.read_bytes()
        odd = []
        for n in range(300):
            raw = bytearray(base)
            for _ in range(rng.randint(1, 6)):
                raw[rng.randrange(max(len(raw) - 400, 0), len(raw))] = rng.randrange(256)
            out = self.d / "fuzz.jarvismodel"
            out.write_bytes(bytes(raw))
            try:
                package.declared_unpacked_bytes(out)
            except PackageError:
                pass
            except Exception as exc:        # noqa: BLE001 - anything else is the defect this test looks for
                odd.append(type(exc).__name__)
        self.assertEqual(odd, [])


class CliInstall(Base):
    def test_cli_install_with_too_little_space_exits_3_with_numbers_and_no_path(self):
        import contextlib
        import io
        from jarvis import cli
        p, s = self.pkg()
        self.free.return_value = 1000
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(manager, "validate_and_extract", side_effect=AssertionError("must not extract")):
            rc = cli.main(["--data-dir", str(self.d / "data"), "models", "install", str(p), "--id", "tiny", "--sha256", s])
        self.assertEqual(rc, 3)
        self.assertIn("needed", err.getvalue())
        self.assertNotIn(str(self.d), err.getvalue())
        self.assertEqual([x.name for x in (self.d / "data" / "models").iterdir()], [])


if __name__ == "__main__":
    unittest.main()
