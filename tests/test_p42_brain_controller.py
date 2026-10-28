"""Import Brain: controller integration (worker threads, responsiveness, chat/brain interaction, preserved conversation) and static UI wiring.

LABELS: FakeEngine = scripted object, NOT inference. The window is checked only by importing it against stand-in Qt modules and by reading its source
(PySide6 is not installable here): this shows the wiring and the text, NOT that anything draws, responds or looks right. The Import Brain page has never
been launched."""
import importlib
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p41_common as C                                    # noqa: E402
from jarvis.app.controller import AppController           # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Ctl(C.BrainCase):
    def setUp(self):
        super().setUp()
        self.ctl = AppController(self.cfg, engine_factory=C.FakeEngine, guest=False)
        self.ctl.brain_manager()._hw = C.hw()
        self.ctl.brain_manager()._rt = {"available": True, "gpu_offload_supported": False}
        self.addCleanup(lambda: self.ctl.shutdown(2.0))

    def wait(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.ctl.tasks.pump()
            if cond():
                return True
            time.sleep(0.005)
        return False

    def idle(self):
        return self.wait(lambda: not self.ctl.brains["running"] and not self.ctl.brains["refreshing"])


class Operations(Ctl):
    def test_import_inspect_activate_run_on_workers_and_deliver_results(self):
        self.assertTrue(self.ctl.brain_run("inspect", path=str(self.gguf("a.gguf"))))
        self.assertTrue(self.idle())
        self.assertTrue(self.ctl.brains["result"]["ok"])
        self.assertEqual(self.ctl.brains["result"]["op"], "inspect")
        self.assertEqual(self.ctl.brain_manager().store.installed(), [])                  # inspecting registers nothing
        self.assertTrue(self.ctl.brain_run("import", path=str(self.gguf("a.gguf"))))
        self.assertTrue(self.idle())
        mid = self.ctl.brains["result"]["model_id"]
        self.assertTrue(self.ctl.brain_run("activate", model_id=mid))
        self.assertTrue(self.idle())
        self.assertTrue(self.ctl.brains["result"]["ok"], self.ctl.brains["result"])
        d = self.ctl.brains["data"]
        self.assertEqual(d["summary"]["active_id"], mid)

    def test_the_interface_thread_stays_free_during_a_slow_load(self):
        mid = self.ctl.brain_manager().import_file(self.gguf("a.gguf"))["model_id"]
        gate, started = threading.Event(), threading.Event()

        def slow(d):
            started.set()
            gate.wait(5)
            return C.FakeEngine(d)
        self.ctl.brain_manager()._factory = slow
        t0 = time.monotonic()
        self.assertTrue(self.ctl.brain_run("activate", model_id=mid))
        self.assertLess(time.monotonic() - t0, 0.5)                          # the call returned at once; the load runs elsewhere
        self.assertTrue(started.wait(5))
        t0 = time.monotonic()
        st = self.ctl.brain_state()                                           # interface-thread calls still answer immediately
        self.ctl.tasks.pump()
        self.assertTrue(st["running"] and st["op"] == "activate")
        self.assertLess(time.monotonic() - t0, 0.2)
        gate.set()
        self.assertTrue(self.idle())
        self.assertTrue(self.ctl.brains["result"]["ok"])

    def test_chat_is_refused_while_switching_and_a_second_operation_is_refused(self):
        mid = self.ctl.brain_manager().import_file(self.gguf("a.gguf"))["model_id"]
        gate, started = threading.Event(), threading.Event()
        self.ctl.brain_manager()._factory = lambda d: (started.set(), gate.wait(5), C.FakeEngine(d))[2]
        self.ctl.brain_run("activate", model_id=mid)
        self.assertTrue(started.wait(5))
        self.assertFalse(self.ctl.send("hello"))
        self.assertIn("switching or testing", self.ctl.transcript.entries[-1].text)
        self.assertFalse(self.ctl.brain_run("test", model_id=mid))
        self.assertIn("still running", self.ctl.brains["message"])
        gate.set()
        self.assertTrue(self.idle())

    def test_switching_is_refused_while_a_reply_is_being_produced(self):
        mid = self.ctl.brain_manager().import_file(self.gguf("a.gguf"))["model_id"]
        self.ctl.busy = True
        for op in ("activate", "deactivate", "previous", "test"):
            self.assertFalse(self.ctl.brain_run(op, model_id=mid), op)
            self.assertIn("answering a message", self.ctl.brains["message"])
        self.ctl.busy = False

    def test_cancel_stops_a_running_import(self):
        f = self.gguf("big.gguf", size=400_000_000)
        self.assertTrue(self.ctl.brain_run("import", path=str(f)))
        self.ctl.brain_cancel()
        self.assertTrue(self.idle(), "import did not finish")
        r = self.ctl.brains["result"]
        self.assertTrue(r["ok"] or r["kind"] == "cancelled")                 # cancelled if the checksum was still running; done if it had finished
        if not r["ok"]:
            self.assertEqual(self.ctl.brain_manager().store.installed(), [])

    def test_context_limit_is_set_on_a_worker_and_bad_values_are_refused(self):
        mid = self.ctl.brain_manager().import_file(self.gguf("a.gguf", ctx=8192))["model_id"]
        self.assertTrue(self.ctl.brain_run("context", model_id=mid, n_ctx=2048))
        self.assertTrue(self.idle())
        self.assertTrue(self.ctl.brains["result"]["ok"])
        self.assertEqual(self.ctl.brain_manager().store.meta(mid)["context_tokens"], 2048)
        self.assertTrue(self.ctl.brain_run("context", model_id=mid, n_ctx=64))
        self.assertTrue(self.idle())
        self.assertEqual(self.ctl.brains["result"]["kind"], "bad_value")
        self.assertEqual(self.ctl.brain_manager().store.meta(mid)["context_tokens"], 2048)

    def test_unknown_operation_does_not_start(self):
        self.assertFalse(self.ctl.brain_run("format-disk"))
        self.assertFalse(self.ctl.brains["running"])

    def test_nothing_runs_until_asked(self):
        self.assertEqual(self.ctl.tasks.running(), [])
        self.assertIsNone(self.ctl.brains["data"])

    def test_refresh_reads_the_library_and_hardware_off_the_interface_thread(self):
        self.ctl.brain_manager().import_file(self.gguf("a.gguf"))
        names = []
        orig = self.ctl.brain_manager().catalog
        self.ctl.brain_manager().catalog = lambda: (names.append(threading.current_thread().name), orig())[1]
        self.assertTrue(self.ctl.brain_refresh())
        self.assertTrue(self.idle())
        self.assertTrue(names and names[0] != threading.current_thread().name)
        self.assertEqual(len(self.ctl.brains["data"]["installed"]), 1)


class ChatInteraction(Ctl):
    def test_a_switch_keeps_the_conversation_memory_and_persona(self):
        mgr = self.ctl.brain_manager()
        a, b = (mgr.import_file(self.gguf(f"{n}.gguf", name=n.upper()))["model_id"] for n in "ab")
        self.ctl.brain_run("activate", model_id=a)
        self.assertTrue(self.idle())
        self.assertTrue(self.ctl.send("remember that my cat is called Miso"))
        self.assertTrue(self.wait(lambda: not self.ctl.busy))
        asst = self.ctl.assistant
        self.assertIsNotNone(asst)
        hist, mem, persona, eng_a = list(asst.history), asst.memory, asst.persona, asst.engine
        self.ctl.brain_run("activate", model_id=b)
        self.assertTrue(self.idle())
        self.assertIs(self.ctl.assistant, asst)                                # the same assistant object: nothing was rebuilt
        self.assertEqual((asst.model_id, list(asst.history)[:len(hist)]), (b, hist))
        self.assertIs(asst.memory, mem)
        self.assertIs(asst.persona, persona)
        self.assertIsNot(asst.engine, eng_a)
        self.assertEqual(C.FakeEngine.loaded, 1 if eng_a.closed else 2)               # the old engine was closed (or, with ample memory, kept until the new one proved itself)
        self.assertTrue(eng_a.closed)
        self.assertTrue(self.ctl.send("and what is my cat called?"))
        self.assertTrue(self.wait(lambda: not self.ctl.busy))
        self.assertEqual(self.ctl.transcript.entries[-1].kind, "assistant")
        self.assertEqual(self.ctl.transcript.entries[-1].text, C.FakeEngine.reply)

    def test_failed_switch_leaves_chat_working_on_the_old_brain(self):
        mgr = self.ctl.brain_manager()
        a, b = (mgr.import_file(self.gguf(f"{n}.gguf", name=n.upper()))["model_id"] for n in "ab")
        self.ctl.brain_run("activate", model_id=a)
        self.assertTrue(self.idle())
        self.assertTrue(self.ctl.send("hi"))
        self.assertTrue(self.wait(lambda: not self.ctl.busy))
        C.FakeEngine.fail_for.add(b)
        self.ctl.brain_run("activate", model_id=b)
        self.assertTrue(self.idle())
        self.assertFalse(self.ctl.brains["result"]["ok"])
        self.assertEqual(self.ctl.assistant.model_id, a)
        self.assertTrue(self.ctl.send("still there?"))
        self.assertTrue(self.wait(lambda: not self.ctl.busy))
        self.assertEqual(self.ctl.transcript.entries[-1].text, C.FakeEngine.reply)

    def test_deactivated_chat_says_there_is_no_brain_and_never_uses_a_cloud(self):
        mgr = self.ctl.brain_manager()
        a = mgr.import_file(self.gguf("a.gguf"))["model_id"]
        self.ctl.brain_run("activate", model_id=a)
        self.assertTrue(self.idle())
        self.ctl.send("hi")
        self.assertTrue(self.wait(lambda: not self.ctl.busy))
        self.ctl.brain_run("deactivate")
        self.assertTrue(self.idle())
        self.assertEqual(C.FakeEngine.loaded, 0)
        self.ctl.send("anyone there?")
        self.assertTrue(self.wait(lambda: not self.ctl.busy))
        last = self.ctl.transcript.entries[-1]
        self.assertEqual(last.kind, "error")
        self.assertIn("No brain is active", last.text)
        self.assertNotIn("http", last.text.lower())

    def test_activating_before_the_first_message_hands_over_the_tested_engine(self):
        mgr = self.ctl.brain_manager()
        a = mgr.import_file(self.gguf("a.gguf"))["model_id"]
        self.ctl.brain_run("activate", model_id=a)
        self.assertTrue(self.idle())
        n = len(C.FakeEngine.created)
        self.assertIsNone(self.ctl.assistant)
        self.ctl.send("first message")
        self.assertTrue(self.wait(lambda: not self.ctl.busy))
        self.assertEqual(len(C.FakeEngine.created), n)                         # no second load: the tested engine became the chat engine
        self.assertEqual(C.FakeEngine.loaded, 1)

    def test_shutdown_closes_the_engine_it_owns(self):
        mgr = self.ctl.brain_manager()
        a = mgr.import_file(self.gguf("a.gguf"))["model_id"]
        self.ctl.brain_run("activate", model_id=a)
        self.assertTrue(self.idle())
        self.ctl.shutdown(2.0)
        self.assertEqual(C.FakeEngine.loaded, 0)


class StaticWiring(unittest.TestCase):
    """Reads the source / imports against stand-in Qt. Nothing is drawn."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "src/jarvis/ui/window.py").read_text(encoding="utf-8")

    def window(self):
        from test_p34_readiness import _fake_qt
        p = mock.patch.dict(sys.modules, _fake_qt())
        p.start()
        self.addCleanup(p.stop)
        sys.modules.pop("jarvis.ui.window", None)
        self.addCleanup(lambda: sys.modules.pop("jarvis.ui.window", None))
        return importlib.import_module("jarvis.ui.window")

    def test_the_page_is_registered_and_every_page_has_a_refresh_method(self):
        W = self.window()
        self.assertIn("Import Brain", W.PAGES)
        for n in W.PAGES:
            self.assertTrue(hasattr(W.MainWindow, "_refresh_" + W._slug(n)), n)

    def test_all_seven_sections_exist(self):
        for tab in ("Active Brain", "Import Model", "Browse Models", "Installed Brains", "Test Brain", "Hardware and Diagnostics", "Remove Model"):
            self.assertIn(f'addTab(t, "{tab}")', self.src, tab)

    def test_every_handler_wired_to_a_button_exists(self):
        W = self.window()
        state = {"_ib_ids", "_ib_cat_ids", "_ib_handled", "_ib_dialog_open"}          # plain attributes set when the page is built
        for name in set(re.findall(r"self\.(_ib_\w+)", self.src)) - state:
            self.assertTrue(hasattr(W.MainWindow, name), name)

    def test_the_page_calls_only_controller_methods_that_exist(self):
        for name in set(re.findall(r"self\.ctl\.(brain_\w+)", self.src)):
            self.assertTrue(hasattr(AppController, name), name)

    def test_destructive_and_download_dialogs_default_to_cancel(self):
        body = self.src.split("def _ib_confirm", 1)[1].split("def _ib_start", 1)[0]
        self.assertIn("setDefaultButton(no)", body)
        self.assertIn("setEscapeButton(no)", body)

    def test_text_from_models_and_files_is_never_rendered_as_markup(self):
        page = self.src.split("def _import_brain_page", 1)[1].split("def _set_scale", 1)[0]
        for label in ("ib_active", "ib_status", "ib_test_target", "ib_remove_info"):
            self.assertRegex(page, rf"self\.{label}\.setTextFormat\(Qt\.TextFormat\.PlainText\)")
        self.assertNotIn("setHtml", page)
        self.assertNotIn("RichText", page)

    def test_download_and_removal_need_a_confirmation_step_before_the_consent_flag_is_sent(self):
        page = self.src.split("def _ib_on_result", 1)[1].split("def _ib_inspect_text", 1)[0]
        self.assertRegex(page, r"if self\._ib_confirm\([^\n]*\"Download\"\):\s*\n\s*self\.ctl\.brain_run\(\"download\"[^\n]*consent=True")
        self.assertRegex(page, r"elif self\._ib_confirm\([^\n]*\"Remove\"\):\s*\n\s*self\.ctl\.brain_run\(\"remove\"[^\n]*confirmed=True")
        # and no other place sends the consent / confirmation flags
        self.assertEqual(len(re.findall(r"consent=True", self.src)), 1)
        self.assertEqual(len(re.findall(r"brain_run\([^)]*confirmed=True", self.src)), 1)

    def test_nothing_is_read_at_start_up_only_when_the_page_is_open(self):
        self.assertIn("self.stack.currentIndex() == PAGES.index(IMPORT_BRAIN)", self.src)

    def test_viewmodel_text_is_complete_for_a_populated_library(self):
        from jarvis.brains import viewmodel as V
        self.assertIn("No brain is active", V.active_text({"active": None, "message": "No brain is active. x"}))
        self.assertIn("Reading", V.active_text(None))
        self.assertEqual(V.size_text(5_030_000_000), "5.03 GB")
        self.assertEqual(V.last_test_text(None), "never tested")
        self.assertIn("FAILED", V.last_test_text({"ok": False, "at": "2026-01-01T00:00:00", "reason": "x"}))


if __name__ == "__main__":
    unittest.main()
