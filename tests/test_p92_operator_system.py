"""Operator: windows, power, media, system, network, security, events, settings, git and test output. All Windows calls go to FakeOps."""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from operator_fakes import OpCase  # noqa: E402


class Windows(OpCase):
    def test_list_active_and_snap(self):
        self.assertIn("Notepad", self.op.run("window.list").message)
        self.assertIn("Notepad", self.op.run("window.active").message)
        out = self.op.run("window.snap", window="discord", where="left")
        self.assertTrue(out.ok, out.message)
        self.assertEqual(self.fake.placed[-1][0], 2)
        self.assertEqual(self.fake.placed[-1][1], 0)

    def test_minimize_is_verified_by_looking_again(self):
        out = self.op.run("window.action", window="notepad", action="minimize")
        self.assertEqual(out.status, "verified", out.message)

    def test_closing_a_window_asks_and_declining_closes_nothing(self):
        self.answer = False
        out = self.op.run("window.action", window="notepad", action="close")
        self.assertEqual(out.status, "declined")
        self.assertEqual(len(self.fake.win), 3)

    def test_protected_windows_are_refused(self):
        out = self.op.run("window.action", window="Windows Security", action="close")
        self.assertIn(out.status, ("refused", "declined"))
        self.assertEqual(len(self.fake.win), 3)

    def test_unknown_window(self):
        self.assertFalse(self.op.run("window.action", window="nonexistent", action="minimize").ok)

    def test_frozen_check(self):
        self.fake.hung.add(2)
        self.assertIn("Discord", self.op.run("window.check_frozen").message)


class PowerAndMedia(OpCase):
    def test_every_power_action_always_asks_and_declining_does_nothing(self):
        self.answer = False
        for c in ("power.shutdown", "power.restart", "power.sleep", "power.hibernate", "power.signout"):
            self.assertEqual(self.op.run(c).status, "declined", c)
        self.assertEqual(self.fake.power_log, [])
        self.assertTrue(all(r == "high" for _, r, _ in self.asked))

    def test_approved_shutdown_reaches_ops_and_cancel_works(self):
        self.assertTrue(self.op.run("power.shutdown").ok)
        self.assertEqual(self.fake.power_log[-1], "shutdown")
        self.op.run("power.cancel")
        self.assertEqual(self.fake.power_log[-1], "cancel")

    def test_power_off_category_blocks(self):
        self.op.set_category("power", False)
        self.assertEqual(self.op.run("power.sleep").status, "denied")
        self.assertEqual(self.fake.power_log, [])

    def test_volume_and_keys(self):
        self.op.set_category("media", True)
        self.assertTrue(self.op.run("media.volume", direction="up", steps=3).ok)
        self.assertTrue(self.op.run("media.key", key="play_pause").ok)
        self.assertFalse(self.op.run("media.key", key="alt_f4").ok)


class SystemInfo(OpCase):
    def test_reads_work_and_are_low_risk(self):
        for c in ("system.cpu", "system.ram", "system.uptime", "system.resources", "system.processes", "system.heavy", "system.battery"):
            out = self.op.run(c)
            self.assertNotIn(out.status, ("denied", "refused", "off"), c)
        self.assertEqual(self.asked, [])

    def test_service_control_asks_and_blocks_critical_services(self):
        self.op.set_category("services", True)
        out = self.op.run("system.service_control", name="WinDefend", action="stop")
        self.assertIn(out.status, ("refused", "declined"))
        self.assertFalse([r for r in self.fake.runs if r and r[0].lower() in ("sc", "sc.exe", "net")])


class Network(OpCase):
    def test_status_dns_and_diagnose_do_not_crash_offline(self):
        self.op.set_category("network", True)
        self.assertIsNotNone(self.op.run("network.status").message)
        self.assertIsNotNone(self.op.run("network.diagnose").message)

    def test_bad_host_is_refused(self):
        self.op.set_category("network", True)
        self.assertEqual(self.op.run("network.latency", host="bad host; rm -rf", port=80).status, "refused")


class Security(OpCase):
    def test_defender_and_firewall_use_powershell_read_only(self):
        self.op.set_category("security_read", True)
        self.fake.ps_answers = [("Get-MpComputerStatus", '{"AntivirusEnabled":true,"RealTimeProtectionEnabled":false}')]
        out = self.op.run("security.defender")
        self.assertTrue(out.ok)
        self.assertTrue(all("Set-" not in r[1] for r in self.fake.runs if r[0] == "powershell"))

    def test_credential_and_risky_file_scans_do_not_show_secrets(self):
        self.op.set_category("security_read", True)
        self.write("passwords.txt", "password=hunter2supersecret")
        self.write("evil.exe")
        out = self.op.run("security.credential_files")
        self.assertIn("passwords.txt", out.message)
        self.assertNotIn("hunter2", out.message)
        self.assertIn("evil.exe", self.op.run("security.risky_files").message)

    def test_explain_warning(self):
        self.assertTrue(self.op.run("security.explain", warning="SmartScreen prevented an unrecognized app").ok)


class EventsAndSettings(OpCase):
    def test_error_code_explanation_is_offline(self):
        out = self.op.run("events.explain_code", code="0x80070005")
        self.assertIn("denied", out.message.lower())

    def test_settings_open_only_known_pages(self):
        self.assertTrue(self.op.run("settings.open", page="bluetooth").ok)
        self.assertTrue(self.fake.opened[-1].startswith("ms-settings:"))
        n = len(self.fake.opened)
        self.assertEqual(self.op.run("settings.open", page="x; calc").status, "refused")
        self.assertEqual(len(self.fake.opened), n)

    def test_wallpaper_only_from_allowed_folders(self):
        img = self.dl / "w.png"
        from PIL import Image
        Image.new("RGB", (8, 8)).save(img)
        self.assertTrue(self.op.run("settings.wallpaper", path=str(img)).ok)
        self.assertIsNotNone(self.fake.wallpaper)
        self.assertEqual(self.op.run("settings.wallpaper", path="C:/Windows/x.png").status, "refused")

    def test_brightness_range(self):
        self.assertFalse(self.op.run("settings.brightness", percent=500).ok)


class Git(OpCase):
    def setUp(self):
        super().setUp()
        self.repo = self.home / "Documents" / "repo"
        self.repo.mkdir()
        def g(*a):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (self.repo / "a.txt").write_text("1")
        g("add", ".")
        g("commit", "-qm", "first")
        (self.repo / "a.txt").write_text("2")
        self.op.ops = self.op.ops  # real git is used: FakeOps only fakes Windows
        import jarvis.operator.ops as o
        self.fake.run = lambda argv, timeout=20.0: (lambda r: (r.returncode, r.stdout, r.stderr))(subprocess.run(argv, capture_output=True, text=True))
        self.fake.run_in = lambda cwd, argv, timeout=60.0: (lambda r: (r.returncode, r.stdout, r.stderr))(subprocess.run(argv, cwd=cwd, capture_output=True, text=True))

    def test_read_only_git(self):
        self.op.set_category("dev_read", True)
        self.assertIn("a.txt", self.op.run("git.status", folder=str(self.repo)).message)
        self.assertIn("first", self.op.run("git.log", folder=str(self.repo)).message)
        self.assertTrue(self.op.run("git.diff", folder=str(self.repo)).ok)

    def test_commit_asks_and_works(self):
        self.op.run("git.stage", folder=str(self.repo), paths=["a.txt"])
        self.answer = False
        self.assertEqual(self.op.run("git.commit", folder=str(self.repo), message="second").status, "declined")
        self.answer = True
        out = self.op.run("git.commit", folder=str(self.repo), message="second")
        self.assertTrue(out.ok, out.message)

    def test_destructive_git_is_never_run(self):
        out = self.op.run("git.destructive", what="reset --hard")
        self.assertIn(out.status, ("refused", "declined"))
        self.assertEqual((self.repo / "a.txt").read_text(), "2")


class TestOutput(unittest.TestCase):
    def test_parse_test_output(self):
        from jarvis.operator import dev_ops
        f = getattr(dev_ops, "parse_test_output", None)
        self.assertIsNotNone(f)
        r = f("Ran 12 tests in 0.5s\n\nFAILED (failures=2)", 1)
        self.assertFalse(r["passed"])
        self.assertEqual(r["ran"], 12)
        self.assertTrue(f("Ran 3 tests in 0.1s\n\nOK", 0)["passed"])
        self.assertFalse(f("Ran 3 tests in 0.1s\n\nOK", 1)["passed"])
        self.assertFalse(f("", 0)["passed"])


if __name__ == "__main__":
    unittest.main()
