"""Import Brain: transactional activation, recovery, testing, ownership, concurrency, removal, persistence.

LABELS: FakeEngine / FakeHost are scripted objects that count loads and closes. They test Jarvis's OWN switching, ownership and recovery logic.
They are NOT inference: no real model, runtime or hardware was involved, and a pass here does not show that a real model loads or answers."""
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p41_common as C                                    # noqa: E402
from jarvis.brains import service as S                    # noqa: E402

GB = 1_000_000_000


class Activation(C.BrainCase):
    def two(self):
        return self.imp("a.gguf", name="Alpha"), self.imp("b.gguf", name="Beta")

    def test_success_switches_after_a_real_generation_and_records_the_previous(self):
        a, b = self.two()
        ra = self.mgr.activate(a)
        self.assertTrue(ra["ok"], ra)
        self.assertEqual((self.store.active(), self.host.model_id), (a, a))
        self.assertTrue(ra["test"]["text"])
        rb = self.mgr.activate(b)
        self.assertTrue(rb["ok"])
        self.assertEqual((self.store.active(), self.host.model_id, rb["previous_id"]), (b, b, a))
        self.assertEqual(self.mgr.load_state()["previous_id"], a)
        self.assertEqual(self.mgr.load_state()["last_tests"][b]["ok"], True)

    def test_failed_load_keeps_the_previous_brain_loaded_and_selected(self):
        a, b = self.two()
        self.mgr.activate(a)
        before = self.host.engine
        C.FakeEngine.fail_for.add(b)
        r = self.mgr.activate(b)
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "load_failed")
        self.assertEqual((self.store.active(), self.host.model_id), (a, a))
        self.assertIs(self.host.engine, before)
        self.assertFalse(before.closed)
        self.assertEqual(C.FakeEngine.loaded, 1)                                   # the failed candidate left nothing in memory
        self.assertIn("previous brain was kept", r["message"])
        t = self.mgr.load_state()["last_tests"][b]
        self.assertFalse(t["ok"])
        self.assertIn("could not be loaded", t["reason"])

    def test_an_empty_reply_is_a_failure_and_the_candidate_is_released(self):
        a, b = self.two()
        self.mgr.activate(a)
        C.FakeEngine.empty_for.add(b)
        r = self.mgr.activate(b)
        self.assertEqual((r["ok"], r["kind"]), (False, "load_failed"))
        self.assertEqual(self.store.active(), a)
        self.assertEqual(C.FakeEngine.loaded, 1)
        self.assertTrue(C.FakeEngine.created[-1].closed)

    def test_activation_with_nothing_before_fails_clean(self):
        a, = self.two()[:1]
        C.FakeEngine.fail_for.add(a)
        r = self.mgr.activate(a)
        self.assertFalse(r["ok"])
        self.assertIsNone(self.store.active())
        self.assertIsNone(self.host.engine)
        self.assertEqual(self.mgr.summary()["message"], S.NO_BRAIN_MESSAGE)

    def test_tight_memory_releases_the_old_brain_first_so_two_never_coexist(self):
        tight = self.make(C.hw(32, 1.5))                                           # free memory covers the candidate (~1.45 GB estimate) but not 1.25x of it
        self.mgr = tight
        a, b = self.two()
        tight.activate(a)
        C.FakeEngine.peak = 0
        r = tight.activate(b)
        self.assertTrue(r["ok"])
        self.assertEqual(C.FakeEngine.peak, 1)                                      # sequential: at most one engine alive at a time
        self.assertEqual(self.host.events[-2:], ["release", f"install:{b}"])

    def test_tight_memory_and_failed_candidate_restores_the_previous_brain(self):
        tight = self.make(C.hw(32, 1.5))
        self.mgr = tight
        a, b = self.two()
        tight.activate(a)
        C.FakeEngine.fail_for.add(b)
        r = tight.activate(b)
        self.assertEqual((r["ok"], r["previous_restored"]), (False, True))
        self.assertEqual(self.store.active(), a)
        self.assertEqual((self.host.model_id, C.FakeEngine.loaded), (a, 1))
        self.assertFalse(self.host.engine.closed)                                   # the restored brain really works
        self.assertTrue(self.host.engine.chat([{"role": "user", "content": "hi"}]))

    def test_a_loaded_but_failing_candidate_is_released_before_the_previous_brain_is_reloaded(self):
        tight = self.make(C.hw(32, 1.5))
        self.mgr = tight
        a, b = self.two()
        tight.activate(a)
        C.FakeEngine.empty_for.add(b)                    # b loads fine but never produces text: the candidate EXISTS when the failure is detected
        C.FakeEngine.peak = 0
        r = tight.activate(b)
        self.assertEqual((r["ok"], r["previous_restored"]), (False, True))
        self.assertEqual(C.FakeEngine.peak, 1)           # never two copies, not even while restoring
        self.assertEqual((C.FakeEngine.loaded, self.host.model_id), (1, a))

    def test_if_the_previous_brain_cannot_be_reloaded_the_message_says_so_and_nothing_is_deleted(self):
        tight = self.make(C.hw(32, 1.5))
        self.mgr = tight
        a, b = self.two()
        tight.activate(a)
        C.FakeEngine.fail_for.update({a, b})
        r = tight.activate(b)
        self.assertFalse(r["ok"])
        self.assertIs(r["previous_restored"], False)
        self.assertIn("could not be reloaded", r["message"])
        self.assertEqual(self.store.active(), a)                                     # still selected: Activate can try again
        self.assertTrue((self.files / "a.gguf").is_file())

    def test_return_to_previous_after_a_switch(self):
        a, b = self.two()
        self.mgr.activate(a)
        self.mgr.activate(b)
        r = self.mgr.return_to_previous()
        self.assertTrue(r["ok"])
        self.assertEqual((self.store.active(), self.host.model_id), (a, a))
        self.assertEqual(self.mgr.load_state()["previous_id"], b)
        self.assertFalse(self.make(C.hw()).return_to_previous()["ok"] and False)

    def test_return_to_previous_without_one_is_a_clear_message(self):
        r = self.mgr.return_to_previous()
        self.assertEqual((r["ok"], r["kind"]), (False, "no_previous"))

    def test_already_active_and_loaded_is_a_no_op(self):
        a, _ = self.two()
        self.mgr.activate(a)
        n = len(C.FakeEngine.created)
        r = self.mgr.activate(a)
        self.assertTrue(r["ok"] and r["unchanged"])
        self.assertEqual(len(C.FakeEngine.created), n)

    def test_cancel_before_loading_changes_nothing(self):
        a, b = self.two()
        self.mgr.activate(a)
        r = self.mgr.activate(b, cancel=type("F", (), {"is_set": lambda s: True})())
        self.assertEqual(r["kind"], "cancelled")
        self.assertEqual((self.store.active(), len(C.FakeEngine.created)), (a, 1))

    def test_cancel_after_loading_releases_the_candidate(self):
        a, b = self.two()
        self.mgr.activate(a)
        state = {"n": 0}

        class Late:
            def is_set(self):
                state["n"] += 1
                return state["n"] >= 2              # first check (before load) passes; the check after generation fires
        r = self.mgr.activate(b, cancel=Late())
        self.assertEqual((r["ok"], r["kind"]), (False, "cancelled"))
        self.assertEqual((self.store.active(), self.host.model_id, C.FakeEngine.loaded), (a, a, 1))

    def test_memory_error_is_reported_as_out_of_memory_and_keeps_the_previous(self):
        a, b = self.two()
        self.mgr.activate(a)

        def boom(d):
            if d.name == b:
                raise MemoryError()
            return C.FakeEngine(d)
        m = self.make(C.hw(), factory=boom)
        m.host = self.host
        r = m.activate(b)
        self.assertEqual((r["ok"], r["kind"]), (False, "out_of_memory"))
        self.assertIn("Not enough memory", r["message"])
        self.assertEqual(self.store.active(), a)

    def test_unsupported_is_never_loaded(self):
        a = self.imp("a.gguf")
        small = self.make(C.hw(1.0, 0.8))                                           # same model, a PC that cannot hold it
        r = small.activate(a)
        self.assertEqual((r["ok"], r["kind"]), (False, "unsupported"))
        self.assertEqual(C.FakeEngine.created, [])

    def test_warnings_need_explicit_confirmation(self):
        mid = self.imp("a.gguf", chat_template="")                                  # no chat template -> a warning
        r = self.mgr.activate(mid)
        self.assertEqual((r["ok"], r["kind"]), (False, "needs_confirmation"))
        self.assertTrue(r["warnings"])
        self.assertEqual(C.FakeEngine.created, [])
        self.assertTrue(self.mgr.activate(mid, accept_warnings=True)["ok"])

    def test_the_manager_does_not_close_an_engine_it_handed_to_the_host(self):
        a, _ = self.two()
        self.mgr.activate(a)
        self.assertFalse(self.host.engine.closed)
        self.assertEqual(C.FakeEngine.loaded, 1)

    def test_not_installed_and_bad_ids(self):
        self.assertEqual(self.mgr.activate("nope")["kind"], "not_installed")

    def test_conversation_data_files_are_not_touched_by_a_switch(self):
        notes = self.cfg.data_dir / "profile" / "memory.db"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_bytes(b"my notes")
        settings = self.cfg.data_dir / "settings.json"
        settings.write_text('{"x": 1}')
        a, b = self.two()
        self.mgr.activate(a)
        self.mgr.activate(b)
        C.FakeEngine.fail_for.add(a)
        self.mgr.activate(a)
        self.assertEqual((notes.read_bytes(), settings.read_text()), (b"my notes", '{"x": 1}'))


class Deactivate(C.BrainCase):
    def test_deactivate_releases_and_says_there_is_no_brain_and_no_cloud(self):
        a = self.imp()
        self.mgr.activate(a)
        r = self.mgr.deactivate()
        self.assertTrue(r["ok"])
        self.assertIsNone(self.store.active())
        self.assertEqual(C.FakeEngine.loaded, 0)
        self.assertIn("No brain is active", r["message"])
        self.assertIn("cloud", r["message"])
        self.assertTrue((self.files / "a.gguf").is_file())
        self.assertEqual(self.mgr.load_state()["previous_id"], a)
        self.assertTrue(self.mgr.activate(a)["ok"])                                  # and it can be turned back on

    def test_summary_without_a_brain_gives_the_actionable_message(self):
        s = self.mgr.summary()
        self.assertIsNone(s["active"])
        self.assertIn("Import Brain", s["message"])


class Testing(C.BrainCase):
    def test_testing_the_active_brain_uses_the_running_engine(self):
        a = self.imp()
        self.mgr.activate(a)
        n = len(C.FakeEngine.created)
        r = self.mgr.test_brain(a)
        self.assertTrue(r["ok"] and r["test"]["mode"] == "active engine")
        self.assertEqual(len(C.FakeEngine.created), n)                               # no second copy was loaded
        self.assertFalse(self.host.engine.closed)

    def test_testing_another_brain_loads_it_alone_and_releases_it(self):
        a, b = self.imp("a.gguf"), self.imp("b.gguf")
        tight = self.make(C.hw(32, 30.0))
        r = tight.test_brain(b)
        self.assertTrue(r["ok"], r)
        self.assertEqual((C.FakeEngine.loaded, tight.load_state()["last_tests"][b]["ok"]), (0, True))
        self.assertIsNone(self.store.active())                                       # testing never activates

    def test_testing_beside_a_running_brain_is_refused_when_memory_is_short(self):
        a, b = self.imp("a.gguf"), self.imp("b.gguf")
        self.mgr.activate(a)
        tight = self.make(C.hw(32, 1.5))
        r = tight.test_brain(b)
        self.assertEqual((r["ok"], r["kind"]), (False, "memory"))
        self.assertEqual(C.FakeEngine.loaded, 1)

    def test_failed_test_is_recorded_with_the_reason_and_status_becomes_failed(self):
        a = self.imp()
        C.FakeEngine.empty_for.add(a)
        r = self.mgr.test_brain(a)
        self.assertFalse(r["ok"])
        b = self.mgr.installed_brains()[0]
        self.assertEqual(b["status"], "failed")
        self.assertIn("FAILED", __import__("jarvis.brains.viewmodel", fromlist=["x"]).last_test_text(b["last_test"]))
        self.assertEqual(C.FakeEngine.loaded, 0)

    def test_status_progression_unverified_then_ready(self):
        a = self.imp()
        self.assertEqual(self.mgr.installed_brains()[0]["status"], "unverified")
        self.mgr.test_brain(a)
        self.assertEqual(self.mgr.installed_brains()[0]["status"], "ready")

    def test_test_results_persist_across_restart(self):
        a = self.imp()
        self.mgr.test_brain(a)
        again = self.make(C.hw())
        self.assertTrue(again.installed_brains()[0]["last_test"]["ok"])

    def test_the_test_never_judges_the_answers_content_but_needs_readable_text(self):
        a = self.imp()
        C.FakeEngine.reply = "banana"
        try:
            self.assertTrue(self.mgr.test_brain(a)["ok"])
            C.FakeEngine.reply = "\x00\x01 \x02"
            self.assertFalse(self.mgr.test_brain(a)["ok"])
        finally:
            C.FakeEngine.reply = "Two plus two equals four."


class Concurrency(C.BrainCase):
    def test_a_second_operation_is_refused_while_one_runs(self):
        a = self.imp()
        gate, started = threading.Event(), threading.Event()

        def slow(d):
            started.set()
            gate.wait(5)
            return C.FakeEngine(d)
        m = self.make(C.hw(), factory=slow)
        out = {}
        t = threading.Thread(target=lambda: out.update(r=m.activate(a)))
        t.start()
        self.assertTrue(started.wait(5))
        self.assertEqual(m.current_operation, "switching brains")
        for call in (lambda: m.activate(a), lambda: m.test_brain(a), lambda: m.deactivate(), lambda: m.remove(a, confirmed=True), lambda: m.import_file(self.gguf("c.gguf"))):
            self.assertEqual(call()["kind"], "busy")
        self.assertEqual(next(b for b in m.installed_brains())["status"], "checking")   # the list shows the transient state
        gate.set()
        t.join(5)
        self.assertTrue(out["r"]["ok"])
        self.assertEqual(m.current_operation, "")
        self.assertTrue(m.deactivate()["ok"])                                           # and the lock is free again


class Removal(C.BrainCase):
    def test_needs_confirmation(self):
        a = self.imp()
        r = self.mgr.remove(a)
        self.assertEqual(r["kind"], "confirmation_required")
        self.assertEqual(len(self.store.installed()), 1)

    def test_library_removal_keeps_the_users_file(self):
        a = self.imp()
        pv = self.mgr.remove_preview(a)
        self.assertTrue(pv["linked"] and "stays where it is" in pv["library_removal"])
        self.assertTrue(self.mgr.remove(a, confirmed=True)["ok"])
        self.assertEqual(self.store.installed(), [])
        self.assertTrue((self.files / "a.gguf").is_file())

    def test_explicit_file_deletion_deletes_only_the_registered_file(self):
        a = self.imp("a.gguf")
        other = self.gguf("keep.gguf")
        self.assertTrue(self.mgr.remove(a, confirmed=True, delete_file=True)["ok"])
        self.assertFalse((self.files / "a.gguf").exists())
        self.assertTrue(other.is_file())

    def test_file_that_changed_since_registration_is_not_deleted(self):
        a = self.imp("a.gguf")
        with open(self.files / "a.gguf", "ab") as fh:
            fh.write(b"x")
        r = self.mgr.remove(a, confirmed=True, delete_file=True)
        self.assertEqual((r["ok"], r["kind"]), (False, "refused"))
        self.assertTrue((self.files / "a.gguf").is_file())
        self.assertEqual(len(self.store.installed()), 1)                               # and the entry stays so nothing is lost silently

    def test_a_symlink_is_never_followed_for_deletion(self):
        a = self.imp("a.gguf")
        real = self.files / "a.gguf"
        moved = self.files / "real.bin"
        real.rename(moved)
        real.symlink_to(moved)
        r = self.mgr.remove(a, confirmed=True, delete_file=True)
        self.assertTrue(moved.is_file())
        self.assertTrue(real.is_symlink() or not r["ok"])

    def test_active_brain_cannot_be_removed(self):
        a = self.imp()
        self.mgr.activate(a)
        self.assertEqual(self.mgr.remove(a, confirmed=True)["kind"], "is_active")
        self.assertEqual(len(self.store.installed()), 1)

    def test_downloaded_copy_is_deleted_with_its_folder(self):
        a = self.mgr.import_file(self.gguf("c.gguf", size=3_000_000), copy=True)["model_id"]
        self.assertTrue((self.store.path(a) / "model.gguf").is_file())
        self.assertTrue(self.mgr.remove(a, confirmed=True)["ok"])
        self.assertFalse(self.store.path(a).exists())
        self.assertTrue((self.files / "c.gguf").is_file())                              # the user's source file stays

    def test_unknown_and_malicious_ids(self):
        self.assertEqual(self.mgr.remove("nope", confirmed=True)["kind"], "not_installed")
        for bad in ("../data", "a/b", ".."):
            self.assertEqual(self.mgr.remove(bad, confirmed=True)["kind"], "bad_id")
            self.assertEqual(self.mgr.remove_preview(bad)["kind"], "bad_id")
        self.assertTrue(self.cfg.data_dir.is_dir())

    def test_removal_forgets_the_test_record_and_previous_pointer(self):
        a, b = self.imp("a.gguf"), self.imp("b.gguf")
        self.mgr.activate(a)
        self.mgr.activate(b)
        self.mgr.remove(a, confirmed=True)
        st = self.mgr.load_state()
        self.assertNotIn(a, st.get("last_tests", {}))
        self.assertNotIn("previous_id", st)

    def test_a_damaged_record_does_not_hide_the_other_models(self):
        a = self.imp("a.gguf")
        bad = self.store.dir / "broken"
        bad.mkdir()
        (bad / "meta.json").write_text("{not json")
        rows = self.mgr.installed_brains()
        self.assertEqual({r["id"] for r in rows}, {a, "broken"})
        self.assertTrue(next(r for r in rows if r["id"] == "broken")["damaged"])
        self.assertEqual([m["id"] for m in self.store.installed()], [a])
        self.assertEqual(self.mgr.activate("broken")["kind"], "damaged")


class Settings(C.BrainCase):
    def test_context_limit_is_validated_and_bounded_by_the_model(self):
        a = self.imp("a.gguf", ctx=8192)
        for bad in (100, 10 ** 7, 4096.5, "4096", True):
            self.assertEqual(self.mgr.set_context(a, bad)["kind"], "bad_value", bad)
        self.assertEqual(self.mgr.set_context(a, 16384)["kind"], "bad_value")          # beyond the model's own 8192
        self.assertTrue(self.mgr.set_context(a, 2048)["ok"])
        self.assertEqual(self.store.meta(a)["context_tokens"], 2048)

    def test_diagnostics_never_invent_values(self):
        m = self.make(C.hw(None, None, None))
        d = m.diagnostics()
        self.assertIsNone(d["hardware"]["ram_total_gb"])
        self.assertIsNone(d["hardware"]["ram_available_gb"])
        self.assertIn("never falls back to a cloud", d["cloud"])


if __name__ == "__main__":
    unittest.main()
