"""Import Brain: no silent cloud, no network outside the explicit download, nothing executed from model files, diagnostics privacy, engine ownership.

LABELS: static source inspection (regex over the source), MOCKED network/socket (any use raises), FakeEngine (not inference), SYNTHETIC GGUF files.
The Windows branch of the available-memory reader cannot run on Linux and is NOT tested here."""
import json
import re
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p41_common as C                                    # noqa: E402
from jarvis.assistant import LocalAssistant, NoActiveModel  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BRAINS = ROOT / "src/jarvis/brains"


class NoNetwork(C.BrainCase):
    def test_brains_package_imports_no_network_library(self):
        bad = re.compile(r"^\s*(import|from)\s+(urllib|requests|socket|http|httpx|aiohttp|ftplib|smtplib|openai|anthropic)\b", re.M)
        for f in BRAINS.glob("*.py"):
            self.assertIsNone(bad.search(f.read_text(encoding="utf-8")), f.name)

    def test_no_cloud_provider_or_api_key_anywhere_in_the_brain_or_inference_code(self):
        pat = re.compile(r"openai|anthropic|api[_-]?key|azure|bedrock|vertex|gpt-4|claude-", re.I)
        for d in (BRAINS, ROOT / "src/jarvis/inference"):
            for f in d.glob("*.py"):
                self.assertIsNone(pat.search(f.read_text(encoding="utf-8")), f.name)

    def test_import_test_activate_deactivate_remove_never_touch_the_network(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")), \
                mock.patch.object(self.policy, "open", side_effect=AssertionError("policy.open used")):
            mid = self.imp()
            self.assertTrue(self.mgr.test_brain(mid)["ok"])
            self.assertTrue(self.mgr.activate(mid)["ok"])
            self.assertTrue(self.mgr.deactivate()["ok"])
            self.assertTrue(self.mgr.remove(mid, confirmed=True)["ok"])
            self.mgr.catalog()
            self.mgr.download_preflight("qwen3-1.7b-q4_k_m")
            self.mgr.diagnostics()

    def test_failed_activation_does_not_reach_for_anything_else(self):
        mid = self.imp()
        C.FakeEngine.fail_for.add(mid)
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")):
            r = self.mgr.activate(mid)
        self.assertFalse(r["ok"])
        self.assertNotIn("cloud", r["message"].lower().replace("never uses a cloud", ""))

    def test_assistant_without_an_engine_raises_a_clear_error_instead_of_answering_from_anywhere(self):
        a = LocalAssistant.__new__(LocalAssistant)
        a.engine = None
        with self.assertRaises(NoActiveModel) as cm:
            a._eng()
        self.assertIn("does not use a cloud", str(cm.exception))


class ModelFilesAreUntrusted(C.BrainCase):
    def test_hostile_metadata_is_data_only(self):
        sentinel = self.d / "PWNED"
        r = self.mgr.import_file(self.gguf("h.gguf", name=f"<script>alert(1)</script>$(touch {sentinel})", chat_template="{% for x in range(10**9) %}{% endfor %}"))
        self.assertTrue(r["ok"])
        self.assertFalse(sentinel.exists())
        row = self.mgr.installed_brains()[0]
        self.assertIn("<script>", row["name"])                      # kept verbatim as data; the window shows it as plain text (see test_p42)

    def test_a_directory_symlink_or_device_is_not_a_model_path(self):
        link = self.files / "dir.gguf"
        link.symlink_to(self.files, target_is_directory=True)
        self.assertEqual(self.mgr.import_file(link)["kind"], "not_a_file")
        self.assertEqual(self.mgr.import_file("/dev/null")["kind"], "not_a_file")

    def test_huge_string_lengths_in_a_header_are_rejected_not_allocated(self):
        import struct
        p = self.files / "bomb.gguf"
        p.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 2 ** 60) + b"x" * 64)
        r = self.mgr.import_file(p)
        self.assertEqual((r["ok"], r["kind"]), (False, "not_gguf"))

    def test_registering_never_modifies_the_users_file(self):
        f = self.gguf("m.gguf")
        before = (f.read_bytes(), f.stat().st_mtime_ns)
        self.mgr.import_file(f)
        self.mgr.test_brain(next(iter(self.mgr.installed_brains()))["id"])
        self.assertEqual((f.read_bytes(), f.stat().st_mtime_ns), before)

    def test_brain_activation_changes_no_permission_or_tool_setting_files(self):
        guard = self.cfg.data_dir / "settings.json"
        guard.write_text(json.dumps({"tools": {"files": False}, "permissions": {"desktop": "off"}}))
        audit = self.cfg.data_dir / "audit.log"
        audit.write_text("line\n")
        snap = (guard.read_bytes(), audit.read_bytes())
        a, b = self.imp("a.gguf"), self.imp("b.gguf")
        self.mgr.activate(a)
        self.mgr.activate(b)
        self.mgr.deactivate()
        self.assertEqual((guard.read_bytes(), audit.read_bytes()), snap)


class DiagnosticsPrivacy(C.BrainCase):
    def test_report_lists_brains_without_paths_or_replies(self):
        from jarvis.app import diagnostics
        mid = self.imp()
        self.mgr.test_brain(mid)
        d = diagnostics.collect(self.cfg)
        b = d["brains"]
        self.assertEqual((b["installed"], b["models"][0]["last_test"], b["models"][0]["registered_in_place"]), (1, "passed", True))
        text = diagnostics.render_text(d)
        self.assertNotIn(str(self.files), text)
        self.assertNotIn(C.FakeEngine.reply, text)
        self.assertTrue(any("Brains:" in x for x in diagnostics.INCLUDED_FIELDS))

    def test_a_damaged_library_is_reported_not_raised(self):
        from jarvis.app import diagnostics
        bad = self.store.dir / "broken"
        bad.mkdir()
        (bad / "meta.json").write_text("nope")
        self.assertEqual(diagnostics.collect(self.cfg)["brains"]["damaged_entries"], ["broken"])


class Ownership(C.BrainCase):
    def assistant(self, engine=None):
        a = LocalAssistant(self.cfg, engine=engine or C.FakeEngine(self.d / "x"), model_id="x", guest=True)
        self.addCleanup(a.close)
        return a

    def test_an_injected_engine_is_not_closed_by_release_or_close(self):
        e = C.FakeEngine(self.d / "shared")
        a = self.assistant(e)
        a.release_engine()
        self.assertFalse(e.closed)                                    # it belongs to whoever created it
        self.assertIsNone(a.engine)

    def test_swap_engine_takes_ownership_and_closes_the_old_owned_engine_once(self):
        a = self.assistant()
        old = a.engine
        a.swap_engine(C.FakeEngine(self.d / "n1"), "n1")              # the old one was injected: not owned, so not closed
        self.assertFalse(old.closed)
        mid = a.engine
        new = C.FakeEngine(self.d / "n2")
        a.swap_engine(new, "n2")                                       # now the old one IS owned
        self.assertTrue(mid.closed)
        self.assertFalse(new.closed)
        a.close()
        self.assertTrue(new.closed)
        a.close()                                                      # closing twice is harmless
        self.assertEqual(C.FakeEngine.loaded, 1)                       # only the never-owned initial engine is left alive

    def test_swap_with_the_same_engine_does_not_close_it(self):
        a = self.assistant()
        e = C.FakeEngine(self.d / "z")
        a.swap_engine(e, "z")
        a.swap_engine(e, "z")
        self.assertFalse(e.closed)

    def test_history_survives_swap_and_release(self):
        a = self.assistant()
        a.history.append({"role": "user", "content": "hi"})
        a.swap_engine(C.FakeEngine(self.d / "q"), "q")
        a.release_engine()
        self.assertEqual(a.history, [{"role": "user", "content": "hi"}])
        with self.assertRaises(NoActiveModel):
            a.reply("anything")


class AvailableMemory(unittest.TestCase):
    def test_reader_returns_none_when_it_cannot_read(self):
        from jarvis import hardware
        with mock.patch("builtins.open", side_effect=OSError("denied")), mock.patch.object(hardware.os, "name", "posix"):
            self.assertIsNone(hardware._ram_available_gb())

    def test_linux_value_is_plausible_and_not_above_total(self):
        from jarvis import hardware
        h = hardware.detect_hardware()
        if h.ram_available_gb is None:
            self.skipTest("MemAvailable not readable on this system")
        self.assertLessEqual(h.ram_available_gb, h.ram_gb + 0.01)
        self.assertGreater(h.ram_available_gb, 0)

    def test_parses_meminfo(self):
        from jarvis import hardware
        m = mock.mock_open(read_data="MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n")
        with mock.patch("builtins.open", m), mock.patch.object(hardware.os, "name", "posix"):
            self.assertAlmostEqual(hardware._ram_available_gb(), 8000000 * 1024 / 1e9, places=3)


if __name__ == "__main__":
    unittest.main()
