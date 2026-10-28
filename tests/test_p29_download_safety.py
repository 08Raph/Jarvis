"""Stage 5 D-9: download-size (115 %) and free-disk-space safeguards for model files.

MOCKED network responses and MOCKED free-space readings only: no network, no model weights, no real large files. These tests verify our
size/disk LOGIC and cleanup behaviour on Linux. They do NOT validate Windows behaviour (shutil.disk_usage semantics, antivirus, quotas) and do not
prove that a real 5-9 GB download behaves the same way."""
import errno
import getpass
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from p10_common import make_gguf  # noqa: E402
from jarvis.models import catalog, diskspace  # noqa: E402
from jarvis.models.diskspace import DiskSpaceError  # noqa: E402
from jarvis.models.manager import IntegrityError, ModelStore  # noqa: E402
from jarvis.net.policy import NetworkPolicy  # noqa: E402

HUGE = 10 ** 13           # a mocked "plenty of space" reading


class FakeResp:
    """Stand-in for the urllib response: headers + read(); counts how many bytes it handed out."""
    def __init__(self, body=None, headers=None, endless=0):
        self.body = io.BytesIO(body or b"")
        self.headers = headers if headers is not None else {}
        self.endless, self.served = endless, 0

    def read(self, n=-1):
        if self.endless:
            self.served += n
            return b"x" * n
        b = self.body.read(n)
        self.served += len(b)
        return b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def limit_for(want: int) -> int:
    """An estimate whose 115 % limit is the largest reachable value <= `want` (the limit steps by 1-2 bytes, so not every value is reachable)."""
    est = want * 100 // 115 + 2
    while est * 115 // 100 > want:
        est -= 1
    return est


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.models = self.d / "models"
        self.store = ModelStore(self.models, NetworkPolicy())
        self.free = mock.patch.object(diskspace, "free_bytes", return_value=HUGE)
        self.free_mock = self.free.start()
        self.addCleanup(self.free.stop)
        self.addCleanup(self.tmp.cleanup)

    def payload(self, size: int) -> bytes:
        """A valid synthetic GGUF of exactly `size` bytes."""
        base = make_gguf(self.d / "probe.gguf", pad=0).stat().st_size
        data = make_gguf(self.d / "p.gguf", pad=size - base).read_bytes()
        assert len(data) == size, (len(data), size)
        return data

    def model(self, estimate: int, mid="fake-q4"):
        return catalog.CatalogModel(mid, "Fake", "pub/fake-GGUF", r"(?i)q4_k_m.*\.gguf$", "Apache-2.0", "https://example.org/l",
                                    approx_size_bytes=estimate, min_ram_gb=1, context_tokens=2048)

    def fetch(self, model, resp, declared=None, sha="auto", body=None, **kw):
        if sha == "auto":
            sha = hashlib.sha256(body).hexdigest() if body is not None else None
        info = {"name": "Fake-Q4_K_M.gguf", "url": "https://huggingface.co/pub/fake-GGUF/resolve/main/Fake-Q4_K_M.gguf", "size_bytes": declared, "sha256": sha}
        with mock.patch("jarvis.models.catalog.resolve_file", return_value=info), mock.patch.object(self.store.policy, "open", return_value=resp) as op:
            self.opened = op
            return self.store.download_gguf(model, trust_unpinned=(sha is None), **kw)

    def leftovers(self):
        return sorted(p.name for p in self.models.iterdir() if p.name.startswith("."))


class SizePolicyUnits(unittest.TestCase):
    def test_limit_is_115_percent_rounded_down(self):
        self.assertEqual(diskspace.size_limit(1000), 1150)
        self.assertEqual(diskspace.size_limit(101), 116)          # 116.15 -> 116
        self.assertEqual(diskspace.size_limit(1), 1)              # 1.15 -> 1: never more generous than 115 %
        self.assertEqual(diskspace.size_limit(2_500_000_000), 2_875_000_000)
        self.assertEqual(diskspace.size_limit(9_000_000_000), 10_350_000_000)

    def test_a_smaller_host_declared_size_wins(self):
        self.assertEqual(diskspace.size_limit(1000, 1100), 1100)
        self.assertEqual(diskspace.size_limit(1000, 1500), 1150)
        self.assertEqual(diskspace.size_limit(1000, 0), 1150)
        self.assertEqual(diskspace.size_limit(1000, None), 1150)

    def test_content_length_parsing_is_defensive(self):
        P = diskspace.parse_content_length
        self.assertEqual(P("123"), 123)
        self.assertEqual(P(" 5 "), 5)
        for bad in (None, "", "abc", "-1", "1.5", "12 34", "9" * 40, object()):
            self.assertIsNone(P(bad), bad)

    def test_requirements_include_the_copy_and_a_margin(self):
        self.assertEqual(diskspace.need_for_copy(9_000_000_000), 9_000_000_000 + diskspace.META_OVERHEAD)
        self.assertEqual(diskspace.need_for_download(10_350_000_000), 10_350_000_000 + diskspace.META_OVERHEAD)
        self.assertEqual(diskspace.margin_for(1), diskspace.MIN_MARGIN)
        self.assertEqual(diskspace.margin_for(50_000_000_000), 5_000_000_000)       # 10 % once that exceeds 1 GB
        self.assertEqual(diskspace.required_free(9_000_000_000), 9_000_000_000 + 1_000_000_000)

    def test_free_space_unknown_fails_closed_without_leaking_the_path(self):
        with mock.patch("shutil.disk_usage", side_effect=OSError(r"cannot stat C:\Users\Jane Doe\AppData\x")):
            with self.assertRaises(DiskSpaceError) as cm:
                diskspace.free_bytes(Path("/nonexistent/anything"))
        self.assertNotIn("Jane", str(cm.exception))
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=-5)):
            with self.assertRaises(DiskSpaceError):
                diskspace.free_bytes(Path("/tmp"))

    def test_free_space_uses_the_nearest_existing_parent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsInstance(diskspace.free_bytes(Path(td) / "a" / "b" / "c"), int)


class SizeLimitEnforcement(Base):
    def test_1_download_below_the_limit_succeeds(self):
        body = self.payload(2000)
        est = limit_for(2100)
        self.assertGreater(diskspace.size_limit(est), 2000)         # limit above the 2000-byte file
        meta = self.fetch(self.model(est), FakeResp(body, {"Content-Length": "2000"}), declared=2000, body=body)
        self.assertTrue(meta["verified_origin"])
        self.assertTrue(self.store.verify("fake-q4")[0])
        self.assertEqual(self.leftovers(), [])

    def test_2_exactly_at_the_limit_is_accepted_and_one_byte_more_is_refused(self):
        est = limit_for(2000)
        L = diskspace.size_limit(est)
        self.assertEqual(L, est * 115 // 100)
        body = self.payload(L)                                      # exactly the limit: INCLUSIVE
        meta = self.fetch(self.model(est), FakeResp(body), declared=None, body=body)
        self.assertEqual(meta["sizes"]["model.gguf"], L)
        self.store.remove("fake-q4")
        over = self.payload(L + 1)                                  # limit + 1: refused
        with self.assertRaises(IntegrityError):
            self.fetch(self.model(est), FakeResp(over), declared=None, body=over)
        self.assertEqual(self.store.installed(), [])

    def test_3_a_streamed_download_is_stopped_before_accepting_more_data(self):
        est = limit_for(4 * (1 << 20))                              # 4 MiB limit
        resp = FakeResp(endless=1)                                  # an endless stream, no Content-Length
        written = []
        real_open = open

        def spy(path, mode="r", *a, **k):
            f = real_open(path, mode, *a, **k)
            if "b" in mode and str(path).split("/")[-1].startswith(".part-"):
                real_write = f.write
                f.write = lambda b: (written.append(len(b)), real_write(b))[1]
            return f
        with mock.patch("builtins.open", spy):
            with self.assertRaises(IntegrityError) as cm:
                self.fetch(self.model(est), resp, declared=None, sha=None)
        self.assertLessEqual(sum(written), diskspace.size_limit(est))           # never wrote past the limit
        self.assertLessEqual(resp.served, diskspace.size_limit(est) + (1 << 20))   # read at most one extra chunk, then stopped
        self.assertIn("115%", str(cm.exception))

    def test_4_misleading_or_absent_content_length_cannot_bypass_the_streaming_limit(self):
        est = limit_for(1 << 20)
        big = self.payload((1 << 20) + 5000)
        for headers in ({}, {"Content-Length": "10"}, {"Content-Length": "abc"}, {"Content-Length": "-4"}, {"Content-Length": ""}):
            with self.subTest(headers=headers):
                with self.assertRaises(IntegrityError):
                    self.fetch(self.model(est), FakeResp(big, headers), declared=None, body=big)
                self.assertEqual(self.store.installed(), [])
                self.assertEqual(self.leftovers(), [])

    def test_4b_an_announced_oversize_is_refused_before_any_body_is_read(self):
        est = limit_for(1 << 20)
        resp = FakeResp(b"y" * 10, {"Content-Length": str(10 * (1 << 20))})
        with self.assertRaises(IntegrityError):
            self.fetch(self.model(est), resp, declared=None, sha=None)
        self.assertEqual(resp.served, 0)
        self.assertEqual(self.leftovers(), [])

    def test_4c_a_host_listing_above_115_percent_is_refused_before_connecting(self):
        est = limit_for(1000)
        with self.assertRaises(IntegrityError):
            self.fetch(self.model(est), FakeResp(b""), declared=1001, sha=None)
        self.opened.assert_not_called()                              # no download connection was even opened

    def test_4d_a_file_that_outgrows_the_hosts_own_declared_size_is_stopped(self):
        est = limit_for(2100)
        body = self.payload(1900)
        grown = body + b"\0" * 40                                    # declared 1900 but the stream is 1940 (< 2000)
        with self.assertRaises(IntegrityError):
            self.fetch(self.model(est), FakeResp(grown), declared=1900, body=grown)
        self.assertEqual(self.store.installed(), [])

    def test_5_oversized_download_leaves_no_registration_and_no_partial_file(self):
        est = limit_for(1 << 20)
        with self.assertRaises(IntegrityError):
            self.fetch(self.model(est), FakeResp(endless=1), declared=None, sha=None)
        self.assertEqual(self.store.installed(), [])
        self.assertIsNone(self.store.active())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual([p.name for p in self.models.iterdir()], [])

    def test_catalog_estimates_stay_inside_the_rule_for_the_published_display_sizes(self):
        """Not a download: a sanity check that the 115 % rule is practical for the catalog entries against the page sizes recorded in the report
        (display figures, so +/- rounding): 1.7B 1.28 GB, 4B 2.5 GB, 8B 5.03 GB (Qwen repo). The estimates are NOT changed by this milestone."""
        shown = {"qwen3-1.7b-q4_k_m": 1.28e9, "qwen3-4b-q4_k_m": 2.55e9, "qwen3-8b-q4_k_m": 5.03e9}      # 4B: top of the "2.5 GB" display rounding
        for m in catalog.BUILTIN:
            self.assertLessEqual(shown[m.id], diskspace.size_limit(m.approx_size_bytes), m.id)


class DiskSpaceEnforcement(Base):
    def test_6_a_shortage_refuses_before_anything_is_written(self):
        body = self.payload(2000)
        est = limit_for(2100)
        self.free_mock.return_value = 5_000                          # far below the 1 GB margin
        with self.assertRaises(DiskSpaceError) as cm:
            self.fetch(self.model(est), FakeResp(body), declared=2000, body=body)
        self.opened.assert_not_called()                              # refused before the download connection was opened
        self.assertEqual([p.name for p in self.models.iterdir()], [])
        self.assertIn("needed", str(cm.exception))
        self.assertIn("available", str(cm.exception))

    def test_6b_exact_requirement_is_the_boundary(self):
        body = self.payload(2000)
        est = limit_for(2100)
        need = diskspace.required_free(diskspace.need_for_download(2000))        # effective limit = declared 2000
        self.free_mock.return_value = need - 1
        with self.assertRaises(DiskSpaceError):
            self.fetch(self.model(est), FakeResp(body), declared=2000, body=body)
        self.free_mock.return_value = need
        self.assertTrue(self.fetch(self.model(est), FakeResp(body), declared=2000, body=body)["verified_origin"])

    def test_7_add_gguf_accounts_for_the_staged_copy(self):
        src = make_gguf(self.d / "src.gguf", pad=5000)
        size = src.stat().st_size
        need = diskspace.required_free(diskspace.need_for_copy(size))
        self.assertGreater(need, size + diskspace.MIN_MARGIN - 1)                  # a full second copy plus the margin
        self.free_mock.return_value = need - 1
        with self.assertRaises(DiskSpaceError):
            self.store.install_gguf(src, "m1", "MIT", "text")
        self.assertEqual([p.name for p in self.models.iterdir()], [])
        self.free_mock.return_value = need
        self.assertEqual(self.store.install_gguf(src, "m1", "MIT", "text")["id"], "m1")
        self.assertTrue(src.exists())                                              # the source stays: both copies existed at the peak

    def test_7b_peak_for_the_14b_is_one_extra_copy_not_two(self):
        # the 14B Q4_K_M displays as ~9 GB: add-gguf needs ~9 GB MORE (source already on disk) + 1 GB margin; the 2x total (18 GB) includes the source
        self.assertEqual(diskspace.required_free(diskspace.need_for_copy(9_000_000_000)), 10_001_048_576)

    def test_7c_a_move_install_needs_no_copy_space(self):
        src = make_gguf(self.d / "src.gguf", pad=5000)
        self.free_mock.return_value = 0
        self.assertEqual(self.store.install_gguf(src, "m1", "MIT", "text", move=True)["id"], "m1")      # rename on the same volume
        self.assertFalse(src.exists())

    def test_8_a_disk_full_error_during_the_copy_leaves_no_false_success_and_keeps_the_old_model(self):
        old = make_gguf(self.d / "old.gguf", pad=1000)
        self.store.install_gguf(old, "m1", "MIT", "text")
        before = self.store.path("m1").joinpath("model.gguf").read_bytes()
        new = make_gguf(self.d / "new.gguf", pad=3000)
        with mock.patch.object(ModelStore, "_copy_checked", side_effect=OSError(errno.ENOSPC, "No space left on device", str(self.d / "secret-user-dir" / "x"))):
            with self.assertRaises(DiskSpaceError) as cm:
                self.store.install_gguf(new, "m1", "MIT", "text")
        self.assertNotIn("secret-user-dir", str(cm.exception))
        self.assertEqual(self.store.path("m1").joinpath("model.gguf").read_bytes(), before)       # still the old, valid model
        self.assertTrue(self.store.verify("m1")[0])
        self.assertEqual([p.name for p in self.models.iterdir() if p.name.startswith(".")], [])    # staging removed
        self.assertEqual([m["id"] for m in self.store.installed()], ["m1"])

    def test_8b_space_running_out_mid_download_stops_and_keeps_the_old_model(self):
        old = make_gguf(self.d / "old.gguf", pad=1000)
        self.store.install_gguf(old, "fake-q4", "MIT", "text")
        before = self.store.path("fake-q4").joinpath("model.gguf").read_bytes()
        big = self.payload(300_000)
        est = limit_for(400_000)
        self.assertGreater(diskspace.size_limit(est), 300_000)
        readings = iter([HUGE])                                       # the pre-start check sees plenty; every later reading (the recheck) sees almost nothing
        self.free_mock.side_effect = lambda *_: next(readings, 1000)
        with mock.patch.object(diskspace, "RECHECK_EVERY", 64 * 1024):
            with self.assertRaises(DiskSpaceError):
                self.fetch(self.model(est), FakeResp(big), declared=None, body=big)
        self.assertEqual(self.store.path("fake-q4").joinpath("model.gguf").read_bytes(), before)
        self.assertTrue(self.store.verify("fake-q4")[0])
        self.assertEqual(self.leftovers(), [])

    def test_package_download_also_checks_space_for_download_plus_unpack(self):
        self.free_mock.return_value = 10_000
        with mock.patch.object(self.store.policy, "open") as op:
            with self.assertRaises(DiskSpaceError):
                self.store.download({"id": "pkg", "url": "https://example.org/p", "sha256": "0" * 64, "size_bytes": 1000})
        op.assert_not_called()


class CleanupAndMessages(Base):
    def make_bystanders(self):
        self.models.mkdir(parents=True, exist_ok=True)
        files = {self.models / "notes.txt": b"mine", self.models / ".part-someone-elses": b"partial", self.models / ".tmp-other" / "f.bin": b"stage",
                 self.models / "other-model" / "model.gguf": b"keep", self.models / ".prev-other": None}
        for p, data in files.items():
            if data is None:
                p.mkdir(parents=True, exist_ok=True)
                (p / "x").write_bytes(b"1")
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return files

    def assert_bystanders_intact(self):
        self.assertEqual((self.models / "notes.txt").read_bytes(), b"mine")
        self.assertEqual((self.models / ".part-someone-elses").read_bytes(), b"partial")
        self.assertEqual((self.models / ".tmp-other" / "f.bin").read_bytes(), b"stage")
        self.assertEqual((self.models / "other-model" / "model.gguf").read_bytes(), b"keep")
        self.assertEqual((self.models / ".prev-other" / "x").read_bytes(), b"1")

    def test_9_cleanup_after_failures_never_touches_unrelated_files(self):
        self.make_bystanders()
        est = limit_for(1 << 20)
        with self.assertRaises(IntegrityError):                       # oversize
            self.fetch(self.model(est), FakeResp(endless=1), declared=None, sha=None)
        self.assert_bystanders_intact()
        self.free_mock.return_value = 1
        with self.assertRaises(DiskSpaceError):                       # shortage before start
            self.fetch(self.model(est), FakeResp(b"x"), declared=None, sha=None)
        self.assert_bystanders_intact()
        self.free_mock.return_value = HUGE
        src = make_gguf(self.d / "n.gguf", pad=2000)
        with mock.patch.object(ModelStore, "_copy_checked", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(DiskSpaceError):                   # disk full during copy
                self.store.install_gguf(src, "m2", "MIT", "t")
        self.assert_bystanders_intact()
        with self.assertRaises(IntegrityError):                       # wrong checksum
            self.store.install_gguf(src, "m3", "MIT", "t", expected_sha256="0" * 64)
        self.assert_bystanders_intact()

    def test_10_error_messages_do_not_expose_users_paths_or_secrets(self):
        secret = "sk-ABCDEF0123456789ABCDEF0123456789"
        texts = []
        est = limit_for(1 << 20)
        try:
            self.fetch(self.model(est), FakeResp(endless=1), declared=None, sha=None)
        except Exception as e:     # noqa: BLE001
            texts.append(str(e))
        self.free_mock.return_value = 123
        try:
            self.fetch(self.model(est), FakeResp(b"x"), declared=None, sha=None)
        except Exception as e:     # noqa: BLE001
            texts.append(str(e))
        src = make_gguf(self.d / "n.gguf", pad=2000)
        try:
            self.store.install_gguf(src, "m2", "MIT", "t")
        except Exception as e:     # noqa: BLE001
            texts.append(str(e))
        self.free_mock.return_value = HUGE
        with mock.patch.object(ModelStore, "_copy_checked", side_effect=OSError(errno.ENOSPC, f"full {secret}", str(Path.home() / "x"))):
            try:
                self.store.install_gguf(src, "m2", "MIT", "t")
            except Exception as e:     # noqa: BLE001
                texts.append(str(e))
        self.free.stop()
        try:
            with mock.patch("shutil.disk_usage", side_effect=OSError(f"{Path.home()} {secret}")):
                try:
                    self.store.install_gguf(src, "m4", "MIT", "t")
                except Exception as e:     # noqa: BLE001
                    texts.append(str(e))
        finally:
            self.free.start()
        self.assertGreaterEqual(len(texts), 5)
        user = getpass.getuser()
        for t in texts:
            self.assertNotIn(str(Path.home()), t)
            self.assertNotIn(str(self.d), t)
            self.assertNotIn(secret, t)
            if len(user) >= 4:
                self.assertNotIn(user, t)
        self.assertTrue(any("needed" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
