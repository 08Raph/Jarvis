"""Stage 5 final download-safety follow-up: add-gguf expected-size protection and model replacement / rollback storage.

Linux only, mocked disk readings, no network, no model weights. Nothing here validates Windows: the rename/copy guarantees tested are the
ones this code gets from os.replace / shutil on the Linux filesystem used for the run (see docs/LOCAL_MODEL.md, 'Replacement and rollback')."""
import contextlib
import errno
import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from p10_common import make_gguf  # noqa: E402
from test_models import make_model_dir  # noqa: E402
from jarvis import cli  # noqa: E402
from jarvis.models import diskspace, manager  # noqa: E402
from jarvis.models.diskspace import DiskSpaceError  # noqa: E402
from jarvis.models.manager import IntegrityError, ModelStore  # noqa: E402
from jarvis.models.package import pack_model  # noqa: E402
from jarvis.net.policy import NetworkPolicy  # noqa: E402


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def boundary_pair(start=2000):
    """(size, expected_ok, expected_over): the limit for expected_ok is EXACTLY `size`; the limit for expected_over is exactly size-1."""
    for size in range(start, start + 400):
        ok = [e for e in range(1, size + 1) if diskspace.size_limit(e) == size]
        over = [e for e in range(1, size + 1) if diskspace.size_limit(e) == size - 1]
        if ok and over:
            return size, ok[0], over[0]
    raise AssertionError("no boundary pair found")


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

    def gguf(self, name, pad):
        return make_gguf(self.d / name, pad=pad)

    def hidden(self):
        return sorted(p.name for p in self.models.iterdir() if p.name.startswith("."))

    def final_bytes(self, mid="m1"):
        return (self.models / mid / "model.gguf").read_bytes()

    def prev_bytes(self, mid="m1"):
        return (self.models / f".prev-{mid}" / "model.gguf").read_bytes()


class ExpectedSizeValidation(unittest.TestCase):
    def test_strict_parser_accepts_only_plain_positive_decimal_integers_in_range(self):
        self.assertEqual(diskspace.parse_expected_size("5030000000"), 5_030_000_000)
        for bad in ("", " ", "0", "00", "-1", "+5", "1.5", "1e9", "1_000", "0x10", " 5", "5 ", "5\n", "abc", "٥", "9" * 30, str(10 ** 13 + 1), None, 5, 5.0, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                diskspace.parse_expected_size(bad)

    def test_api_rejects_invalid_values_before_touching_the_file(self):
        with tempfile.TemporaryDirectory() as t:
            store = ModelStore(Path(t) / "m", NetworkPolicy())
            src = make_gguf(Path(t) / "a.gguf", pad=100)
            for bad in (0, -1, True, False, "5", 1.5, 10 ** 13 + 1):
                with self.subTest(bad=bad), mock.patch("jarvis.models.manager.sha256_file", side_effect=AssertionError("must not hash")):
                    with self.assertRaises(ValueError):
                        store.install_gguf(src, "m1", "MIT", "t", expected_size_bytes=bad)
            self.assertEqual(store.installed(), [])


class AddGgufExpectedSize(Base):
    def test_a_file_within_the_limit_installs_and_the_record_says_the_limit_was_enforced(self):
        src = self.gguf("a.gguf", 3000)
        size = src.stat().st_size
        meta = self.store.install_gguf(src, "m1", "MIT", "t", expected_sha256=sha(src), expected_size_bytes=size)
        self.assertTrue(meta["size_limit_enforced"])
        self.assertEqual(meta["expected_size_bytes"], size)
        self.assertTrue(self.store.verify("m1")[0])

    def test_without_the_option_behaviour_is_unchanged_and_the_record_says_no_limit_was_enforced(self):
        src = self.gguf("a.gguf", 3000)
        meta = self.store.install_gguf(src, "m1", "MIT", "t", expected_sha256=sha(src))
        self.assertFalse(meta["size_limit_enforced"])
        self.assertIsNone(meta["expected_size_bytes"])

    def test_exact_boundary_is_accepted_and_one_byte_over_is_rejected(self):
        size, ok, over = boundary_pair()
        src = self.gguf("b.gguf", 0)
        pad = size - src.stat().st_size
        self.assertGreater(pad, 0, "boundary file must be larger than the bare header")
        src = self.gguf("b.gguf", pad)
        self.assertEqual(src.stat().st_size, size)
        self.assertEqual(diskspace.size_limit(ok), size)
        self.assertEqual(diskspace.size_limit(over), size - 1)
        with self.assertRaises(IntegrityError) as cm:
            self.store.install_gguf(src, "m1", "MIT", "t", expected_sha256=sha(src), expected_size_bytes=over)
        self.assertIn("115", str(cm.exception))
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.hidden(), [])
        self.store.install_gguf(src, "m1", "MIT", "t", expected_sha256=sha(src), expected_size_bytes=ok)
        self.assertEqual([m["id"] for m in self.store.installed()], ["m1"])

    def test_an_oversized_file_is_rejected_before_it_is_hashed_or_copied_and_nothing_changes(self):
        src = self.gguf("big.gguf", 5000)
        before = (src.read_bytes(), src.stat().st_mtime_ns)
        old = self.gguf("old.gguf", 1000)
        self.store.install_gguf(old, "m1", "MIT", "t")
        old_bytes = self.final_bytes()
        with mock.patch("jarvis.models.manager.sha256_file", side_effect=AssertionError("hashed first")), \
                mock.patch.object(ModelStore, "_copy_checked", side_effect=AssertionError("copied first")):
            with self.assertRaises(IntegrityError) as cm:
                self.store.install_gguf(src, "m1", "MIT", "t", expected_sha256="0" * 64, expected_size_bytes=1000)
        self.assertIn("larger than", str(cm.exception))
        self.assertEqual(self.final_bytes(), old_bytes)
        self.assertEqual(self.hidden(), [])
        self.assertEqual((src.read_bytes(), src.stat().st_mtime_ns), before)

    def test_the_option_does_not_bypass_the_other_checks(self):
        src = self.gguf("a.gguf", 3000)
        size = src.stat().st_size
        with self.assertRaises(IntegrityError):                                    # wrong checksum
            self.store.install_gguf(src, "m1", "MIT", "t", expected_sha256="1" * 64, expected_size_bytes=size)
        with self.assertRaises(IntegrityError):                                    # malformed checksum
            self.store.install_gguf(src, "m1", "MIT", "t", expected_sha256="zz", expected_size_bytes=size)
        dirpath = self.d / "adir.gguf"
        dirpath.mkdir()
        with self.assertRaises(IntegrityError):                                    # not a regular file
            self.store.install_gguf(dirpath, "m1", "MIT", "t", expected_size_bytes=size)
        with self.assertRaises(ValueError):                                        # id validation
            self.store.install_gguf(src, "../x", "MIT", "t", expected_size_bytes=size)
        self.free.return_value = 1000
        with mock.patch.object(ModelStore, "_copy_checked", side_effect=AssertionError("must not copy")):
            with self.assertRaises(DiskSpaceError):                                # free-space check still runs
                self.store.install_gguf(src, "m1", "MIT", "t", expected_sha256=sha(src), expected_size_bytes=size)
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.hidden(), [])

    def test_a_failed_install_with_the_option_restores_the_previous_model_and_keeps_the_source(self):
        old = self.gguf("old.gguf", 1000)
        self.store.install_gguf(old, "m1", "MIT", "t")
        old_bytes = self.final_bytes()
        src = self.gguf("new.gguf", 2000)
        before = src.read_bytes()
        real, n = os.replace, [0]

        def flaky(a, b, *k, **kw):
            n[0] += 1
            if str(a).split(os.sep)[-1].startswith(".tmp-"):          # the final rename of the staged model
                raise PermissionError("locked")
            return real(a, b, *k, **kw)
        with mock.patch("os.replace", flaky):
            with self.assertRaises(PermissionError):
                self.store.install_gguf(src, "m1", "MIT", "t", expected_sha256=sha(src), expected_size_bytes=src.stat().st_size)
        self.assertEqual(self.final_bytes(), old_bytes)
        self.assertTrue(self.store.verify("m1")[0])
        self.assertEqual(src.read_bytes(), before)
        self.assertEqual(self.hidden(), [])


class CliExpectedSize(Base):
    def run_cli(self, *extra, src=None):
        src = src or self.gguf("c.gguf", 3000)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--data-dir", str(self.d / "data"), "models", "add-gguf", str(src), "--id", "m1", "--license", "MIT",
                               "--license-url", "https://x", "--sha256", sha(src), "--no-select", *extra])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_omitting_the_option_installs_and_says_the_size_limit_was_not_enforced(self):
        rc, out, err = self.run_cli()
        self.assertEqual(rc, 0, err)
        self.assertIn("NOT enforced", out)
        self.assertIn("--expected-size-bytes", out)

    def test_a_valid_size_installs_and_says_it_was_enforced(self):
        src = self.gguf("c.gguf", 3000)
        rc, out, err = self.run_cli("--expected-size-bytes", str(src.stat().st_size), src=src)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("NOT enforced", out)
        self.assertIn("115", out)

    def test_invalid_values_are_usage_errors_and_nothing_is_created(self):
        for bad in ("0", "-5", "abc", "1.5", "1e9", "", "99999999999999999999"):
            with self.subTest(bad=bad):
                rc, out, err = self.run_cli("--expected-size-bytes", bad)
                self.assertEqual(rc, 2)
                self.assertFalse((self.d / "data" / "models" / "m1").exists())

    def test_an_oversized_file_exits_3_with_a_path_free_message_and_installs_nothing(self):
        src = self.gguf("c.gguf", 5000)
        rc, out, err = self.run_cli("--expected-size-bytes", "1000", src=src)
        self.assertEqual(rc, 3)
        self.assertIn("115", err)
        self.assertNotIn(str(self.d), err)
        self.assertFalse((self.d / "data" / "models" / "m1").exists())


class Replacement(Base):
    def install(self, name, pad, mid="m1"):
        return self.store.install_gguf(self.gguf(name, pad), mid, "MIT", "t")

    def test_successful_replacement_registers_the_new_model_and_keeps_exactly_one_rollback_copy(self):
        self.install("a.gguf", 1000)
        a = self.final_bytes()
        self.install("b.gguf", 2000)
        b = self.final_bytes()
        self.install("c.gguf", 3000)
        c = self.final_bytes()
        self.assertEqual(len({a, b, c}), 3)
        self.assertEqual(self.prev_bytes(), b)                       # one generation back; the two-generations-old copy is gone
        self.assertTrue(self.store.verify("m1")[0])
        self.assertEqual([m["id"] for m in self.store.installed()], ["m1"])
        self.assertEqual(self.hidden(), [".prev-m1"])

    def test_a_failed_final_rename_restores_both_the_model_and_the_older_rollback_copy(self):
        self.install("a.gguf", 1000)
        self.install("b.gguf", 2000)
        a, b = self.prev_bytes(), self.final_bytes()
        src = self.gguf("c.gguf", 3000)
        real = os.replace

        def flaky(x, y, *k, **kw):
            if str(x).split(os.sep)[-1].startswith(".tmp-"):
                raise PermissionError("locked")
            return real(x, y, *k, **kw)
        with mock.patch("os.replace", flaky):
            with self.assertRaises(PermissionError):
                self.store.install_gguf(src, "m1", "MIT", "t")
        self.assertEqual(self.final_bytes(), b)
        self.assertEqual(self.prev_bytes(), a)                      # the older rollback copy is NOT deleted by a failed replacement
        self.assertTrue(self.store.verify("m1")[0])
        self.assertEqual(self.hidden(), [".prev-m1"])

    def test_a_failure_while_moving_the_current_model_aside_changes_nothing(self):
        self.install("a.gguf", 1000)
        self.install("b.gguf", 2000)
        a, b = self.prev_bytes(), self.final_bytes()
        real, n = os.replace, [0]

        def flaky(x, y, *k, **kw):
            if str(y).split(os.sep)[-1].startswith(".prev-") and str(x).split(os.sep)[-1] == "m1" and not n[0]:     # only "current model -> rollback slot" fails, once
                n[0] = 1
                raise OSError(errno.EACCES, "denied")
            return real(x, y, *k, **kw)
        with mock.patch("os.replace", flaky):
            with self.assertRaises(OSError):
                self.store.install_gguf(self.gguf("c.gguf", 3000), "m1", "MIT", "t")
        self.assertEqual((self.final_bytes(), self.prev_bytes()), (b, a))
        self.assertEqual(self.hidden(), [".prev-m1"])

    def test_if_even_the_restore_fails_the_message_says_how_to_recover_and_has_no_path(self):
        self.install("a.gguf", 1000)
        b = self.final_bytes()
        real = os.replace

        def flaky(x, y, *k, **kw):
            sx, sy = str(x).split(os.sep)[-1], str(y).split(os.sep)[-1]
            if sx.startswith(".tmp-") or (sx.startswith(".prev-") and sy == "m1"):
                raise PermissionError("locked")
            return real(x, y, *k, **kw)
        with mock.patch("os.replace", flaky):
            with self.assertRaises(IntegrityError) as cm:
                self.store.install_gguf(self.gguf("c.gguf", 3000), "m1", "MIT", "t")
        self.assertIn("rollback", str(cm.exception))
        self.assertNotIn(str(self.d), str(cm.exception))
        self.assertEqual(self.prev_bytes(), b)                      # nothing was deleted: the previous model is still in the rollback slot
        self.store.rollback("m1")
        self.assertEqual(self.final_bytes(), b)

    def test_not_enough_free_space_refuses_before_any_change_and_the_old_model_and_rollback_stay(self):
        self.install("a.gguf", 1000)
        self.install("b.gguf", 2000)
        a, b = self.prev_bytes(), self.final_bytes()
        src = self.gguf("c.gguf", 3000)
        need = diskspace.required_free(diskspace.need_for_copy(src.stat().st_size))
        self.free.return_value = need - 1
        with mock.patch.object(ModelStore, "_copy_checked", side_effect=AssertionError("no copy")):
            with self.assertRaises(DiskSpaceError) as cm:
                self.store.install_gguf(src, "m1", "MIT", "t")
        self.assertIn("needed", str(cm.exception))
        self.assertEqual((self.final_bytes(), self.prev_bytes()), (b, a))
        self.assertEqual(self.hidden(), [".prev-m1"])
        self.free.return_value = need                                  # exactly enough: the replacement needs the incoming file only
        self.store.install_gguf(src, "m1", "MIT", "t")                 # (the old model is renamed to the rollback slot, not copied)
        self.assertEqual(self.prev_bytes(), b)

    def test_space_is_rechecked_during_a_long_copy_and_a_shortage_stops_it_cleanly(self):
        self.install("a.gguf", 1000)
        a = self.final_bytes()
        src = self.gguf("big.gguf", 40_000)
        readings = iter([10 ** 13])
        self.free.side_effect = lambda *_: next(readings, 10)          # plenty at the start, almost nothing after the first reading
        with mock.patch.object(manager, "COPY_CHUNK", 4096), mock.patch.object(diskspace, "RECHECK_EVERY", 8192):
            with self.assertRaises(DiskSpaceError):
                self.store.install_gguf(src, "m1", "MIT", "t")
        self.assertEqual(self.final_bytes(), a)
        self.assertEqual(self.hidden(), [])
        self.assertTrue(self.store.verify("m1")[0])

    def test_disk_full_during_the_copy_is_a_path_free_error_and_the_old_model_is_intact(self):
        self.install("a.gguf", 1000)
        a = self.final_bytes()
        src = self.gguf("big.gguf", 40_000)
        real_open = open

        class Full:
            def __init__(self, f):
                self.f, self.n = f, 0

            def write(self, b):
                self.n += 1
                if self.n > 1:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return self.f.write(b)

            def __getattr__(self, k):
                return getattr(self.f, k)

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return self.f.__exit__(*e)

        def fake_open(path, mode="r", *aa, **kk):
            f = real_open(path, mode, *aa, **kk)
            return Full(f) if "w" in mode and "b" in mode and Path(str(path)).name == "model.gguf" else f
        with mock.patch.object(manager, "COPY_CHUNK", 4096), mock.patch("builtins.open", fake_open):
            with self.assertRaises(DiskSpaceError) as cm:
                self.store.install_gguf(src, "m1", "MIT", "t")
        self.assertNotIn(str(self.d), str(cm.exception))
        self.assertEqual(self.final_bytes(), a)
        self.assertEqual(self.hidden(), [])

    def test_cleanup_removes_only_what_the_operation_created(self):
        self.models.mkdir(exist_ok=True)
        mine_not = [self.models / ".tmp-user-notes", self.models / ".swap-old-user-dir", self.models / ".part-someone-else"]
        for p in mine_not:
            p.mkdir()
            (p / "keep.txt").write_text("precious")
        stray = self.models / "README-user.txt"
        stray.write_text("x")
        self.install("a.gguf", 1000)
        with self.assertRaises(IntegrityError):
            self.store.install_gguf(self.gguf("b.gguf", 2000), "m1", "MIT", "t", expected_sha256="0" * 64)
        self.free.return_value = 10
        with self.assertRaises(DiskSpaceError):
            self.store.install_gguf(self.gguf("c.gguf", 2000), "m1", "MIT", "t")
        for p in mine_not:
            self.assertEqual((p / "keep.txt").read_text(), "precious")
        self.assertEqual(stray.read_text(), "x")

    def test_signed_package_replacement_has_the_same_rollback_guarantees(self):
        pk = []
        for tag in (b"v1", b"v2", b"v3"):
            out = self.d / f"{tag.decode()}.jarvismodel"
            pack_model(make_model_dir(self.d, tag), out, "MIT")
            pk.append(out)
        self.store.install_package(pk[0], "tiny", confirm_unlisted=True)
        self.store.install_package(pk[1], "tiny", confirm_unlisted=True)
        cur, prev = (self.models / "tiny" / "model.pt").read_bytes(), (self.models / ".prev-tiny" / "model.pt").read_bytes()
        real = os.replace

        def flaky(x, y, *k, **kw):
            if str(x).split(os.sep)[-1].startswith(".tmp-"):
                raise PermissionError("locked")
            return real(x, y, *k, **kw)
        with mock.patch("os.replace", flaky):
            with self.assertRaises(PermissionError):
                self.store.install_package(pk[2], "tiny", confirm_unlisted=True)
        self.assertEqual((self.models / "tiny" / "model.pt").read_bytes(), cur)
        self.assertEqual((self.models / ".prev-tiny" / "model.pt").read_bytes(), prev)
        self.assertEqual(sorted(p.name for p in self.models.iterdir() if p.name.startswith(".")), [".prev-tiny"])
        self.assertTrue(self.store.verify("tiny")[0])
        self.store.install_package(pk[2], "tiny", confirm_unlisted=True)         # and a normal replacement afterwards still works
        self.assertEqual((self.models / ".prev-tiny" / "model.pt").read_bytes(), cur)

    def test_signed_package_disk_full_while_unpacking_is_a_path_free_disk_error(self):
        out = self.d / "v1.jarvismodel"
        pack_model(make_model_dir(self.d, b"v1"), out, "MIT")
        with mock.patch("jarvis.models.manager.validate_and_extract", side_effect=OSError(errno.ENOSPC, "No space left", str(self.d))):
            with self.assertRaises(DiskSpaceError) as cm:
                self.store.install_package(out, "tiny", confirm_unlisted=True)
        self.assertNotIn(str(self.d), str(cm.exception))
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.hidden(), [])

    def test_manual_rollback_survives_a_failure_half_way(self):
        self.install("a.gguf", 1000)
        self.install("b.gguf", 2000)
        a, b = self.prev_bytes(), self.final_bytes()
        real, n = os.replace, [0]

        def flaky(x, y, *k, **kw):
            n[0] += 1
            if n[0] == 2:                                           # 1st: current -> swap, 2nd: previous -> current (fails)
                raise PermissionError("locked")
            return real(x, y, *k, **kw)
        with mock.patch("os.replace", flaky):
            with self.assertRaises(PermissionError):
                self.store.rollback("m1")
        self.assertEqual((self.final_bytes(), self.prev_bytes()), (b, a))
        self.assertEqual(self.hidden(), [".prev-m1"])
        self.assertTrue(self.store.rollback("m1"))                  # and a normal rollback swaps them
        self.assertEqual((self.final_bytes(), self.prev_bytes()), (a, b))


class IdentifyingRollbackAndLeftovers(Base):
    def test_rollback_copies_and_leftover_staging_are_listed_read_only_with_sizes(self):
        self.store.install_gguf(self.gguf("a.gguf", 1000), "m1", "MIT", "t")
        self.store.install_gguf(self.gguf("b.gguf", 2000), "m1", "MIT", "t")
        crashed = self.models / ".tmp-deadbeef"
        crashed.mkdir()
        (crashed / "model.gguf").write_bytes(b"x" * 123)
        (self.models / ".part-cafe").write_bytes(b"y" * 77)
        rc = self.store.rollback_copies()
        self.assertEqual([r["id"] for r in rc], ["m1"])
        self.assertEqual(rc[0]["size_bytes"], sum(p.stat().st_size for p in (self.models / ".prev-m1").rglob("*") if p.is_file()))
        left = {x["name"]: x["size_bytes"] for x in self.store.staging_leftovers()}
        self.assertEqual(left, {".tmp-deadbeef": 123, ".part-cafe": 77})
        self.assertTrue(crashed.exists())                           # listing never deletes anything
        self.assertEqual(self.store.verify("m1")[0], True)

    def test_models_list_reports_them(self):
        self.store.install_gguf(self.gguf("a.gguf", 1000), "m1", "MIT", "t")
        self.store.install_gguf(self.gguf("b.gguf", 2000), "m1", "MIT", "t")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--data-dir", str(self.d / "data"), "models", "list"])
        self.assertEqual(rc, 0)
        self.assertIn("rollback_copies", out.getvalue())
        self.assertIn("leftover_staging", out.getvalue())


if __name__ == "__main__":
    unittest.main()
