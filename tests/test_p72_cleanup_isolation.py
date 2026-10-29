"""Windows temp-folder cleanup failures after a controller's task outlived shutdown() (CI run 40795644979, three P17 tests: PermissionError [WinError 32] on memory.sqlite).

WHAT THIS PROVES (Linux, real controller / task runner / assistant, FAKE model): the mechanism behind that failure and the guard against it.
  * shutdown(timeout) really does return WITHOUT closing the database while a task is still running (product behaviour, deliberate: never close a database a worker may use);
  * the AppCase safety net waits for the task and then closes the database, or fails NAMING the task that never ended.
WHAT IT CAN NOT PROVE: the Windows symptom itself. Linux lets you delete an open SQLite file, so the PermissionError cannot be reproduced here; why the task outlived
its timeout on that runner (whose unit-test step took 661 s against 268-359 s in every other run) was not captured and is not explained by these tests."""
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from p10_common import AppCase, fake_engine_factory  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
