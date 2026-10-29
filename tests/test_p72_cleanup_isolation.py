"""Windows temp-folder cleanup failures after a controller's task outlived shutdown() (CI run 40795644979, three P17 tests: PermissionError [WinError 32] on memory.sqlite).

WHAT THIS PROVES (Linux, real controller / task runner / assistant, FAKE model): the mechanism behind that failure and the guard against it.
  * shutdown(timeout) really does return WITHOUT closing the database while a task is still running (product behaviour, deliberate: never close a database a worker may use);
  * the AppCase safety net waits for the task and then closes the database, or fails NAMING the task that never ended.
WHAT IT CAN NOT PROVE: the Windows symptom itself. Linux lets you delete an open SQLite file, so the PermissionError cannot be reproduced here; why the task outlived
its timeout on that runner (whose unit-test step took 661 s against 268-359 s in every other run) was not captured and is not explained by these tests."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from p10_common import AppCase, FakeLlama, fake_engine_factory  # noqa: E402
from p17_common import Rig  # noqa: E402
from jarvis.app.controller import AppController  # noqa: E402
from jarvis.assistant import LocalAssistant as Assistant  # noqa: E402


def _controller_with_a_stuck_task(case, release: threading.Event):
    case.install_model()
    c = AppController(case.cfg, simulate=True, engine_factory=fake_engine_factory)
    c._ensure_assistant()
    c.tasks.submit("stuck", lambda token: release.wait(30))
    return c


class Mechanism(AppCase):
    def test_shutdown_returns_without_closing_the_database_while_a_task_is_running(self):
        release = threading.Event()
        self.addCleanup(release.set)                                   # runs BEFORE the inherited safety net (cleanups are last-in-first-out)
        c = _controller_with_a_stuck_task(self, release)
        with mock.patch.object(Assistant, "close", autospec=True, side_effect=Assistant.close) as closer:
            self.assertEqual(c.shutdown(0.2), {"still_running": ["stuck"]})
            self.assertEqual(closer.call_count, 0, "a database a live worker may be using must not be closed")


class SafetyNet(unittest.TestCase):
    """The AppCase cleanup is exercised by running small inner test cases and inspecting their outcome."""

    def run_case(self, body, wait=30.0):
        class Inner(AppCase):
            CLEANUP_WAIT = wait

            def test_it(self):
                body(self)
        result = unittest.TestResult()
        with mock.patch.object(Assistant, "close", autospec=True, side_effect=Assistant.close) as closer:
            unittest.defaultTestLoader.loadTestsFromTestCase(Inner).run(result)
        return result, closer

    def test_a_task_that_outlives_shutdown_is_waited_for_and_the_database_is_then_closed(self):
        release = threading.Event()

        def body(case):
            c = _controller_with_a_stuck_task(case, release)
            threading.Timer(0.4, release.set).start()                  # the task ends shortly AFTER shutdown(0.1) gave up
            self.assertEqual(c.shutdown(0.1), {"still_running": ["stuck"]})
        result, closer = self.run_case(body)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertGreaterEqual(closer.call_count, 1, "the safety net never closed the database")

    def test_a_task_that_never_ends_fails_the_test_and_names_the_task(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def body(case):
            c = _controller_with_a_stuck_task(case, release)
            c.shutdown(0.1)
        result, closer = self.run_case(body, wait=0.3)
        self.assertFalse(result.wasSuccessful())
        text = "\n".join(t for _, t in result.errors + result.failures)
        self.assertIn("still running", text)
        self.assertIn("stuck", text)
        self.assertEqual(closer.call_count, 0, "must not close a database a live worker may be using")

    def test_a_test_that_failed_in_its_body_still_reports_that_failure(self):
        def body(case):
            case.fail("the body failed first")
        result, _ = self.run_case(body)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("the body failed first", result.failures[0][1])


class SafetyNetCoversTheBrainCaseBase(unittest.TestCase):
    """CI run 40918820914: test_p42's Ctl derives from BrainCase (plain unittest.TestCase), not AppCase, so it had no safety net and failed with WinError 32."""

    def run_case(self, body, wait):
        import test_p42_brain_controller as T42

        class Inner(T42.Ctl):
            CLEANUP_WAIT = wait

            def test_it(self):
                body(self)
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Inner).run(result)
        return result

    def test_a_task_that_ends_late_is_waited_for(self):
        release = threading.Event()

        def body(case):
            case.ctl.tasks.submit("stuck", lambda token: release.wait(30))
            threading.Timer(0.4, release.set).start()
            self.assertEqual(case.ctl.shutdown(0.1), {"still_running": ["stuck"]})
        result = self.run_case(body, wait=10.0)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)

    def test_a_task_that_never_ends_fails_the_test_and_names_it(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def body(case):
            case.ctl.tasks.submit("stuck", lambda token: release.wait(30))
            case.ctl.shutdown(0.1)
        result = self.run_case(body, wait=0.3)
        self.assertFalse(result.wasSuccessful())
        text = "\n".join(t for _, t in result.errors + result.failures)
        self.assertIn("still running", text)
        self.assertIn("stuck", text)


class AVoiceReplyInFlightAtShutdown(AppCase):
    """Product defect found from CI run 76eb1cc (the original P17 test failed again, on a normal-speed run, with the safety net active): a spoken reply runs on its own helper
    thread, 'jarvis-voice-turn'. The voice worker gives up waiting for it after 2 s, and controller.shutdown() did not know about it, so it reported {'still_running': []} and
    CLOSED the assistant's memory database while that thread was still inside the model. (FAKE audio, FAKE model: this shows the ordering fault, not a Windows lock.)"""

    def test_shutdown_does_not_close_the_database_under_a_reply_that_is_still_being_produced(self):
        gate = threading.Event()

        def slow(self_, messages, max_tokens, temperature, stream=False):
            gate.wait(20)
            return iter([{"choices": [{"delta": {"content": "late answer"}}]}])
        self.addCleanup(gate.set)                                      # runs before the Rig's own cleanup (last-in-first-out): the fake model is always released
        with mock.patch.object(FakeLlama, "create_chat_completion", slow):
            r = Rig(self)
            r.c.voice_autostart()
            self.assertTrue(r.wait_state("Listening"))
            r.ask("a question that takes a while")
            self.assertTrue(r.wait_state("Processing"))
            time.sleep(0.2)
            self.assertTrue(r.c.voice.turn_running())
            end = time.monotonic() + 10                                # the assistant is built lazily by the reply thread; on a slow runner "Processing" can show before it exists (CI run 40959418900)
            while r.c.assistant is None and time.monotonic() < end:
                time.sleep(0.05)
            asst = r.c.assistant
            self.assertIsNotNone(asst, "the reply thread never built the assistant")
            res = r.c.shutdown(1.0)
            self.assertIn("voice reply", res["still_running"])         # it used to be [] while the helper was still alive
            self.assertTrue(r.c.voice.turn_running())
            asst.memory.list()                                         # the database was NOT closed under it (a closed one raises ProgrammingError)
            gate.set()
            self.assertTrue(r.c.voice.wait_turn(15), "the reply's helper thread never ended after the model was released")
            self.assertFalse(r.c.voice.turn_running())

    def test_an_idle_voice_service_reports_no_reply_running(self):
        r = Rig(self)
        self.assertFalse(r.c.voice_state() is None)
        self.assertEqual(r.c.shutdown(1.0), {"still_running": []})


if __name__ == "__main__":
    unittest.main()
