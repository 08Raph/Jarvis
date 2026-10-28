"""Stage 5 D-9 independent review: regression tests for defects found while reviewing the download/install code.

Defects covered (each test fails against the pre-review code, see the review notes in docs/STAGE5_MODEL_SOURCE_VERIFICATION.md section 15):
  R1 a malformed expected SHA-256 was only rejected as a 'mismatch' after hashing a multi-GB file;
  R2 a source file that changed between hashing and copying could be installed although its hash no longer matched the verified value;
  R3 a failed final swap could leave the previously valid model parked in the rollback slot (not usable under its id);
  R4 a pipe/device/directory as the model path was not rejected up front;
  R5 'disk full' while creating the partial file leaked the full path in the exception text.
Mocked network and disk readings only; no models; Linux only (nothing here validates Windows)."""
import contextlib
import errno
import hashlib
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_p29_download_safety as P29  # noqa: E402
import test_p20_setup as P20  # noqa: E402
from p10_common import make_gguf  # noqa: E402
from jarvis import cli  # noqa: E402
from jarvis.app import setup_engine as SE  # noqa: E402
from jarvis.models import diskspace  # noqa: E402
from jarvis.models.diskspace import DiskSpaceError  # noqa: E402
from jarvis.models.manager import IntegrityError, ModelStore  # noqa: E402
from jarvis.net.policy import NetworkPolicy  # noqa: E402


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class AddGgufIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.models = self.d / "models"
        self.store = ModelStore(self.models, NetworkPolicy())
        p = mock.patch.object(diskspace, "free_bytes", return_value=10 ** 13)
        self.free = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.src = make_gguf(self.d / "src.gguf", pad=3000)

    def hidden(self):
        return sorted(p.name for p in self.models.iterdir() if p.name.startswith("."))

    def test_R1_malformed_or_missing_checksums_are_rejected_before_the_file_is_hashed(self):
        for bad in ("", "abc", "g" * 64, "a" * 63, "a" * 65, "sha256:" + "a" * 64, "a" * 32 + " " + "a" * 31):
            with self.subTest(bad=bad), mock.patch("jarvis.models.manager.sha256_file", side_effect=AssertionError("must not hash first")):
                with self.assertRaises(IntegrityError) as cm:
                    self.store.install_gguf(self.src, "m1", "MIT", "t", expected_sha256=bad)
                self.assertIn("64 hexadecimal", str(cm.exception))
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.hidden(), [])

    def test_a_wrong_but_well_formed_checksum_is_a_mismatch_and_a_correct_one_ignores_case_and_edge_spaces(self):
        with self.assertRaises(IntegrityError) as cm:
            self.store.install_gguf(self.src, "m1", "MIT", "t", expected_sha256="0" * 64)
        self.assertIn("does not match", str(cm.exception))
        self.assertEqual(self.store.installed(), [])
        meta = self.store.install_gguf(self.src, "m1", "MIT", "t", expected_sha256="  " + sha(self.src).upper() + " \n")
        self.assertTrue(meta["verified_origin"])
        self.assertEqual(meta["package_sha256"], sha(self.src))

    def test_the_cli_requires_a_checksum_or_an_explicit_override(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.d / "data"), "models", "add-gguf", str(self.src), "--id", "m1", "--license", "MIT", "--license-url", "https://x"])
        self.assertEqual(rc, 2)
        self.assertIn("--sha256", err.getvalue())
        self.assertFalse((self.d / "data" / "models" / "m1").exists())

    def test_the_source_file_is_never_modified_or_removed(self):
        before = (self.src.read_bytes(), self.src.stat().st_mtime_ns)
        self.store.install_gguf(self.src, "m1", "MIT", "t", expected_sha256=sha(self.src))
        self.assertEqual((self.src.read_bytes(), self.src.stat().st_mtime_ns), before)
        with self.assertRaises(IntegrityError):
            self.store.install_gguf(self.src, "m2", "MIT", "t", expected_sha256="1" * 64)
        self.assertEqual((self.src.read_bytes(), self.src.stat().st_mtime_ns), before)

    def test_R2_a_source_that_changes_during_the_copy_is_not_installed_and_the_old_model_survives(self):
        old = make_gguf(self.d / "old.gguf", pad=1000)
        self.store.install_gguf(old, "m1", "MIT", "t")
        before = self.store.path("m1").joinpath("model.gguf").read_bytes()
        real = ModelStore._copy_checked

        def racing_copy(self_, a, b, *k, **kw):
            r = real(self_, a, b, *k, **kw)
            with open(b, "ab") as f:          # the file grew after it had been hashed (e.g. a browser still writing it)
                f.write(b"\0" * 10)
            return r
        with mock.patch.object(ModelStore, "_copy_checked", racing_copy):
            with self.assertRaises(IntegrityError) as cm:
                self.store.install_gguf(self.src, "m1", "MIT", "t", expected_sha256=sha(self.src))
        self.assertIn("changed", str(cm.exception))
        self.assertEqual(self.store.path("m1").joinpath("model.gguf").read_bytes(), before)
        self.assertTrue(self.store.verify("m1")[0])
        self.assertEqual(self.hidden(), [])

    def test_R3_a_failed_swap_puts_the_previous_model_back(self):
        old = make_gguf(self.d / "old.gguf", pad=1000)
        self.store.install_gguf(old, "m1", "MIT", "t")
        before = self.store.path("m1").joinpath("model.gguf").read_bytes()
        real, n = os.replace, [0]

        def flaky(a, b, *k, **kw):
            n[0] += 1
            if n[0] == 2:                      # 1st: final -> .prev (ok); 2nd: new -> final (fails, e.g. antivirus lock on Windows)
                raise PermissionError("locked")
            return real(a, b, *k, **kw)
        with mock.patch("os.replace", flaky):
            with self.assertRaises(PermissionError):
                self.store.install_gguf(self.src, "m1", "MIT", "t")
        self.assertEqual(self.store.path("m1").joinpath("model.gguf").read_bytes(), before)       # usable again under its own id
        self.assertTrue(self.store.verify("m1")[0])
        self.assertEqual([m["id"] for m in self.store.installed()], ["m1"])
        self.assertEqual(self.hidden(), [])

    def test_R4_only_regular_files_are_accepted(self):
        d = self.d / "adir.gguf"
        d.mkdir()
        with self.assertRaises(IntegrityError):
            self.store.install_gguf(d, "m1", "MIT", "t")
        if hasattr(os, "mkfifo"):
            fifo = self.d / "pipe.gguf"
            os.mkfifo(fifo)
            done = []
            t = threading.Thread(target=lambda: done.append(self._try(fifo)), daemon=True)
            t.start()
            t.join(5)
            self.assertFalse(t.is_alive(), "a named pipe must be rejected without blocking")
            self.assertIn("IntegrityError", done)
        dangling = self.d / "dangling.gguf"
        dangling.symlink_to(self.d / "nope.gguf")
        with self.assertRaises(IntegrityError):
            self.store.install_gguf(dangling, "m1", "MIT", "t")
        notgguf = self.d / "notes.txt"
        notgguf.write_bytes(b"hello world, not a model" * 10)
        link = self.d / "link-to-notes.gguf"
        link.symlink_to(notgguf)
        with self.assertRaises(Exception) as cm:
            self.store.install_gguf(link, "m1", "MIT", "t")
        self.assertEqual(notgguf.read_bytes(), b"hello world, not a model" * 10)               # nothing written through the link
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.hidden(), [])
        self.assertTrue(cm.exception is not None)

    def _try(self, p):
        try:
            self.store.install_gguf(p, "m1", "MIT", "t")
        except Exception as e:     # noqa: BLE001
            return type(e).__name__
        return "none"

    def test_a_symlink_to_a_real_gguf_installs_a_copy_and_leaves_both_link_and_target_alone(self):
        link = self.d / "link.gguf"
        link.symlink_to(self.src)
        before = self.src.read_bytes()
        meta = self.store.install_gguf(link, "m1", "MIT", "t", expected_sha256=sha(self.src))
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.src.read_bytes(), before)
        dst = self.store.path("m1") / "model.gguf"
        self.assertFalse(dst.is_symlink())
        self.assertEqual(dst.read_bytes(), before)
        self.assertEqual(meta["package_sha256"], sha(self.src))

    def test_insufficient_space_is_rejected_before_any_copy_in_the_cli_with_a_path_free_message(self):
        data = self.d / "data"
        self.free.return_value = 1000
        err = io.StringIO()
        with mock.patch.object(ModelStore, "_copy_checked", side_effect=AssertionError("copy must not start")), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(data), "models", "add-gguf", str(self.src), "--id", "m1", "--license", "MIT", "--license-url", "https://x",
                           "--sha256", sha(self.src), "--no-select"])
        self.assertEqual(rc, 3)
        text = err.getvalue()
        self.assertIn("needed", text)
        self.assertIn("available", text)
        self.assertNotIn(str(self.d), text)
        self.assertNotIn(str(Path.home()), text)
        self.assertEqual([p.name for p in (data / "models").iterdir()], [])

    def test_ids_cannot_escape_the_model_folder(self):
        for bad in ("../x", "a/b", "..", ".hidden", "a\\b", "A"):
            with self.assertRaises(ValueError):
                self.store.install_gguf(self.src, bad, "MIT", "t")
            with self.assertRaises(ValueError):
                self.store.remove(bad)
        self.assertFalse((self.d / "x").exists())


class DownloadCancellationAndSetup(P29.Base):
    def test_cancellation_and_interrupts_remove_the_partial_file_and_register_nothing(self):
        class Cancelled(Exception):
            pass
        body = self.payload(2000)
        est = P29.limit_for(2100)
        for exc in (Cancelled("stop"), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):
                old = make_gguf(self.d / "old.gguf", pad=1000)
                self.store.install_gguf(old, "fake-q4", "MIT", "t")
                before = self.store.path("fake-q4").joinpath("model.gguf").read_bytes()

                def prog(done, total, e=exc):
                    raise e
                with self.assertRaises(type(exc)):
                    self.fetch(self.model(est), P29.FakeResp(body), declared=2000, body=body, progress=prog)
                self.assertEqual(self.leftovers(), [])
                self.assertEqual(self.store.path("fake-q4").joinpath("model.gguf").read_bytes(), before)
                self.assertTrue(self.store.verify("fake-q4")[0])
                self.store.remove("fake-q4")

    def test_R5_disk_full_when_creating_the_partial_file_is_a_path_free_disk_space_error(self):
        est = P29.limit_for(2100)
        real_open = open

        def full_open(path, mode="r", *a, **k):
            if "b" in mode and "w" in mode and Path(str(path)).name.startswith(".part-"):
                raise OSError(errno.ENOSPC, "No space left on device", str(path))
            return real_open(path, mode, *a, **k)
        body = self.payload(2000)
        with mock.patch("builtins.open", full_open):
            with self.assertRaises(DiskSpaceError) as cm:
                self.fetch(self.model(est), P29.FakeResp(body), declared=2000, body=body)
        self.assertNotIn(str(self.d), str(cm.exception))
        self.assertNotIn(".part-", str(cm.exception))
        self.assertEqual(self.store.installed(), [])
        self.assertEqual(self.leftovers(), [])

    def test_other_oserrors_during_download_are_not_disguised_as_disk_errors(self):
        est = P29.limit_for(2100)
        with mock.patch.object(self.store.policy, "open", side_effect=ConnectionResetError("reset")), \
                mock.patch("jarvis.models.catalog.resolve_file", return_value={"name": "f", "url": "https://huggingface.co/x", "size_bytes": 2000, "sha256": "a" * 64}):
            with self.assertRaises(ConnectionResetError):
                self.store.download_gguf(self.model(est))

    def test_signed_package_download_stays_exact_size_capped_and_cleans_up(self):
        resp = P29.FakeResp(b"x" * 5000)
        with mock.patch.object(self.store.policy, "open", return_value=resp):
            with self.assertRaises(IntegrityError) as cm:
                self.store.download({"id": "pkg", "url": "https://example.org/p", "sha256": "0" * 64, "size_bytes": 1000})
        self.assertIn("larger than the size declared", str(cm.exception))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.store.installed(), [])


class SetupEngineConsistency(P20.Base):
    def test_a_disk_space_failure_is_reported_as_disk_space_not_as_a_rejected_file(self):
        eng = self.engine()
        with mock.patch.object(self.store, "download_gguf", side_effect=DiskSpaceError("not enough free disk space to download this model: 10.00 GB needed, 1.00 GB available. Nothing was written.")):
            r = eng.download("qwen3-1.7b-q4_k_m")
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "disk_space")
        self.assertIn("needed", r["message"])

    def test_it_is_not_retried(self):
        eng = self.engine()
        calls = []
        with mock.patch.object(self.store, "download_gguf", side_effect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(DiskSpaceError("x needed y available"))):
            eng.download("qwen3-1.7b-q4_k_m")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
