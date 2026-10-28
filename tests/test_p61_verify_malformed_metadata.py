"""Damaged model metadata must be a clear verification failure, never an unhandled traceback, and `selftest` must never try to load a model
whose verification failed. Synthetic files in a temporary folder only: no model weights, no network, no real runtime. These tests run the
program's own code on the machine running the tests; they do not show how it behaves on Windows or with a real GGUF model."""
import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import cli
from jarvis.models.manager import ModelStore

MODEL_ID = "synthetic"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class VerifyMalformedMetadata(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.data_dir = self.root / "data"
        self.store = ModelStore(self.data_dir / "models")
        self.dir = self.store.dir / MODEL_ID
        self.dir.mkdir(parents=True)
        self.weights = b"GGUF" + b"\x00" * 64        # synthetic bytes, not a real model
        (self.dir / "model.gguf").write_bytes(self.weights)
        self.write_meta({"id": MODEL_ID, "kind": "gguf", "files": {"model.gguf": _sha(self.weights)},
                         "sizes": {"model.gguf": len(self.weights)}})

    def write_meta(self, meta):
        (self.dir / "meta.json").write_text(meta if isinstance(meta, str) else json.dumps(meta), encoding="utf-8")

    def meta(self):
        return json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))

    def snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.dir.iterdir())}

    def run_selftest(self):
        """Run `jarvis selftest` against the temporary data folder; any attempt to build an engine is a test failure."""
        (self.store.dir / "active.json").write_text(json.dumps({"id": MODEL_ID}), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("jarvis.inference.gguf_engine.GGUFEngine", side_effect=AssertionError("selftest tried to load the model")) as eng, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.data_dir), "selftest"])
        eng.assert_not_called()
        return rc, err.getvalue()

    # --- verify ---------------------------------------------------------------------------------------------
    def test_valid_metadata_with_matching_hashes_passes_full_and_quick(self):
        self.assertEqual(self.store.verify(MODEL_ID), (True, []))
        self.assertEqual(self.store.verify(MODEL_ID, quick=True), (True, []))

    def test_matching_metadata_but_changed_file_still_fails(self):
        (self.dir / "model.gguf").write_bytes(self.weights + b"x")
        ok, problems = self.store.verify(MODEL_ID)
        self.assertFalse(ok)
        self.assertEqual(problems, ["model.gguf: hash mismatch"])

    def test_missing_files_key_is_a_clear_failure(self):
        meta = self.meta(); del meta["files"]; self.write_meta(meta)
        ok, problems = self.store.verify(MODEL_ID)
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)
        self.assertIn("files", problems[0])

    def test_files_with_the_wrong_type_is_a_clear_failure(self):
        for bad in (None, [], ["model.gguf"], "model.gguf", 7, {}, {"model.gguf": 5}, {"model.gguf": None}):
            with self.subTest(files=bad):
                meta = self.meta(); meta["files"] = bad; self.write_meta(json.dumps(meta))
                ok, problems = self.store.verify(MODEL_ID)
                self.assertFalse(ok)
                self.assertIn("files", problems[0])

    def test_sizes_with_the_wrong_type_is_a_clear_failure(self):
        for bad in ([], "x", 7, None):
            with self.subTest(sizes=bad):
                meta = self.meta(); meta["sizes"] = bad; self.write_meta(meta)
                ok, problems = self.store.verify(MODEL_ID, quick=True)
                self.assertFalse(ok)
                self.assertIn("sizes", problems[0])

    def test_malformed_json_and_non_object_json_are_clear_failures(self):
        for text in ("{not json", "", "[1, 2]", "null", '"text"'):
            with self.subTest(meta=text):
                self.write_meta(text)
                self.assertEqual(self.store.verify(MODEL_ID), (False, ["meta.json cannot be read"]))

    def test_missing_model_metadata_is_a_clear_failure(self):
        (self.dir / "meta.json").unlink()
        self.assertEqual(self.store.verify(MODEL_ID), (False, ["not installed"]))
        self.assertEqual(self.store.verify("never-installed"), (False, ["not installed"]))

    def test_verify_cli_reports_failure_without_a_traceback_and_changes_nothing(self):
        meta = self.meta(); del meta["files"]; self.write_meta(meta)
        before = self.snapshot()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--data-dir", str(self.data_dir), "models", "verify", MODEL_ID])
        self.assertEqual(rc, 3)
        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertNotIn("Traceback", out.getvalue() + err.getvalue())
        self.assertEqual(self.snapshot(), before)          # the damaged metadata and the model file are left exactly as they were

    def test_registered_external_model_is_unaffected(self):
        from test_local_model import make_gguf      # the existing synthetic GGUF header builder (a few hundred bytes, not a model)
        ext = make_gguf(self.root / "mine.gguf")
        self.store.register_external(ext, "linked-model", sha256=_sha(ext.read_bytes()), license_spdx="MIT")
        self.assertEqual(self.store.verify("linked-model"), (True, []))

    # --- selftest -------------------------------------------------------------------------------------------
    def test_selftest_refuses_to_load_after_verification_fails(self):
        cases = {"missing files": lambda m: m.pop("files"), "files wrong type": lambda m: m.update(files=["model.gguf"]),
                 "hash mismatch": lambda m: m.update(files={"model.gguf": "0" * 64})}
        for name, damage in cases.items():
            with self.subTest(case=name):
                meta = {"id": MODEL_ID, "kind": "gguf", "files": {"model.gguf": _sha(self.weights)}, "sizes": {}}
                damage(meta); self.write_meta(meta)
                before = self.snapshot()
                rc, err = self.run_selftest()
                self.assertEqual(rc, 3)
                self.assertIn("failed its integrity check", err)
                self.assertNotIn("Traceback", err)
                self.assertFalse((self.data_dir / "selftest.json").exists())      # nothing was claimed about this model
                self.assertEqual(self.snapshot(), before)

    def test_selftest_refuses_to_load_when_metadata_is_unreadable(self):
        for text in ("{not json", "[1, 2]"):
            with self.subTest(meta=text):
                self.write_meta(text)
                before = self.snapshot()
                rc, err = self.run_selftest()
                self.assertEqual(rc, 3)
                self.assertIn("failed its integrity check", err)
                self.assertFalse((self.data_dir / "selftest.json").exists())
                self.assertEqual(self.snapshot(), before)

    def test_selftest_with_missing_metadata_does_not_load_a_model(self):
        (self.dir / "meta.json").unlink()
        rc, err = self.run_selftest()
        self.assertEqual(rc, 2)
        self.assertIn("No chat model selected", err)
        self.assertFalse((self.data_dir / "selftest.json").exists())


if __name__ == "__main__":
    unittest.main()
