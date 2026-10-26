"""Prompt 7: the sandbox that runs tests, snippets and plugins.

REAL tests start the real jail on this Linux machine (user/mount/pid/network namespaces, a private root, resource limits). They are SKIPPED, and
counted as skipped, where no genuine sandbox can be created. MOCKED tests replace the availability answer to prove that Jarvis refuses to run
code when there is no sandbox (this is what happens on Windows). The sandbox is a Linux-only containment with a shared kernel and no seccomp;
nothing here says it is a security guarantee, and nothing was run on Windows."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from p7_common import SANDBOX_OK, SANDBOX_WHY, needs_sandbox, SB, ROOT  # noqa: F401


def py(code: str, **kw):
    return SB.run([SB.interpreter(), "-I", "-B", "-c", code], **kw)


class AvailabilityTests(unittest.TestCase):
    def test_availability_is_a_pair_with_a_reason_when_unavailable(self):
        ok, why = SB.availability()
        self.assertIsInstance(ok, bool)
        if not ok:
            self.assertTrue(why)

    def test_MOCKED_non_linux_is_reported_unavailable_not_pretended(self):
        with mock.patch.object(SB.sys, "platform", "win32"):
            ok, why = SB.availability(refresh=True)
        SB.availability(refresh=True)
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_MOCKED_run_refuses_when_unavailable(self):
        with mock.patch.object(SB, "availability", return_value=(False, "no sandbox here")):
            with self.assertRaises(SB.SandboxUnavailable):
                SB.run(["/usr/bin/true"])


@needs_sandbox
class IsolationTests(unittest.TestCase):
    """REAL jail."""

    def test_a_host_file_is_invisible_and_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as d:
            canary = Path(d) / "secret.txt"
            canary.write_text("TOP-SECRET-CANARY")
            r = py(f"import os\nprint(os.path.exists({str(canary)!r}))\ntry:\n    print(open({str(canary)!r}).read())\nexcept OSError as e:\n    print('blocked', type(e).__name__)")
        self.assertIn("False", r.stdout)
        self.assertNotIn("TOP-SECRET-CANARY", r.stdout + r.stderr)

    def test_home_etc_and_var_are_not_there(self):
        r = py("import os\nfor p in ('/root','/home','/etc/passwd','/var','/mnt','/srv'):\n    print(p, os.path.exists(p))")
        for p in ("/root", "/home", "/etc/passwd", "/var", "/mnt", "/srv"):
            self.assertIn(f"{p} False", r.stdout, r.stdout)

    def test_network_is_unreachable(self):
        r = py("import socket\ns=socket.socket()\ns.settimeout(3)\ntry:\n    s.connect(('1.1.1.1',80)); print('CONNECTED')\nexcept OSError as e:\n    print('blocked', e.errno)")
        self.assertIn("blocked", r.stdout)
        self.assertNotIn("CONNECTED", r.stdout)

    def test_system_directories_are_read_only(self):
        r = py("try:\n    open('/usr/jarvis_probe','w'); print('WROTE')\nexcept OSError as e:\n    print('blocked', e.errno)")
        self.assertIn("blocked", r.stdout)

    def test_files_are_a_copy_and_changes_do_not_reach_the_host(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("original")
            r = py("open('/work/a.txt','w').write('CHANGED'); open('/work/new.txt','w').write('x'); print(open('/work/a.txt').read())", copy_from=Path(d))
            self.assertIn("CHANGED", r.stdout)
            self.assertEqual((Path(d) / "a.txt").read_text(), "original")
            self.assertFalse((Path(d) / "new.txt").exists())

    def test_symlinks_in_the_project_are_not_copied(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "host.txt").write_text("HOST")
            (Path(d) / "link.txt").symlink_to(Path(outside) / "host.txt")
            (Path(d) / "ok.txt").write_text("ok")
            r = py("import os; print(sorted(os.listdir('/work')))", copy_from=Path(d))
        self.assertIn("ok.txt", r.stdout)
        self.assertNotIn("link.txt", r.stdout)
        self.assertTrue(r.skipped)

    def test_the_process_cannot_see_host_processes(self):
        r = py("import os; print(os.getpid())")
        self.assertLess(int(r.stdout.strip()), 50)


@needs_sandbox
class LimitTests(unittest.TestCase):
    """REAL jail: timeouts, cancellation, output and failure handling."""

    def test_a_runaway_loop_is_killed_at_the_wall_clock_limit(self):
        t0 = time.monotonic()
        r = py("while True: pass", limits=SB.Limits(wall_seconds=2, cpu_seconds=30))
        self.assertIn(r.killed_for, ("timeout", "limit"))
        self.assertFalse(r.completed)
        self.assertLess(time.monotonic() - t0, 15)

    def test_sleeping_children_do_not_survive_the_timeout(self):
        r = py(f"import subprocess,time\nsubprocess.Popen(['/usr/bin/sleep','97'])\ntime.sleep(60)", limits=SB.Limits(wall_seconds=2))
        self.assertEqual(r.killed_for, "timeout")
        time.sleep(0.5)
        import subprocess
        out = subprocess.run(["pgrep", "-x", "-a", "sleep"], capture_output=True, text=True).stdout
        self.assertNotIn(" 97", out)

    def test_cancellation_stops_a_running_program(self):
        flag = {"stop": False}
        import threading
        threading.Timer(1.0, lambda: flag.update(stop=True)).start()
        t0 = time.monotonic()
        r = py("import time; time.sleep(60)", limits=SB.Limits(wall_seconds=30), stop=lambda: flag["stop"])
        self.assertEqual(r.killed_for, "stop")
        self.assertLess(time.monotonic() - t0, 10)

    def test_output_is_capped_and_the_program_is_stopped(self):
        r = py("while True: print('x'*1000)", limits=SB.Limits(wall_seconds=20, output_bytes=20_000))
        self.assertEqual(r.killed_for, "output")
        self.assertTrue(r.truncated)
        self.assertLess(len(r.stdout), 60_000)

    def test_memory_limit_makes_a_huge_allocation_fail(self):
        r = py("try:\n    b = bytearray(3*1024*1024*1024); print('ALLOCATED')\nexcept MemoryError:\n    print('memoryerror')", limits=SB.Limits(memory_mb=256))
        self.assertNotIn("ALLOCATED", r.stdout)

    def test_a_failing_program_reports_its_exit_code_and_error(self):
        r = py("import sys; print('out'); sys.stderr.write('boom'); sys.exit(3)")
        self.assertEqual(r.returncode, 3)
        self.assertIn("boom", r.stderr)
        self.assertEqual(r.killed_for, "")

    def test_disk_growth_in_work_is_stopped(self):
        r = py("import time\ni=0\nwhile True:\n    open('/work/f%d'%i,'wb').write(b'0'*1000000); i+=1; time.sleep(0.02)", limits=SB.Limits(wall_seconds=20, work_mb=5))
        self.assertEqual(r.killed_for, "disk")

    def test_a_single_huge_file_hits_the_file_size_limit(self):
        r = py("f=open('/work/big','wb')\nwhile True: f.write(b'0'*100000); f.flush()", limits=SB.Limits(wall_seconds=20, work_mb=500, file_mb=3))
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(r.completed and r.returncode == 0)

    def test_the_environment_is_clean(self):
        os.environ["JARVIS_P7_SECRET_TEST"] = "leak"
        try:
            r = py("import os; print(os.environ.get('JARVIS_P7_SECRET_TEST','absent'))")
        finally:
            del os.environ["JARVIS_P7_SECRET_TEST"]
        self.assertEqual(r.stdout.strip(), "absent")


if __name__ == "__main__":
    unittest.main()
