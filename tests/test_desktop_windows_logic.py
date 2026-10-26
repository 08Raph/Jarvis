"""Stage C: isolated tests for the Windows-facing logic of desktop control.

NOTHING IN THIS FILE RAN ON WINDOWS. Read each class docstring for what kind of test it is and what it CANNOT prove:

  REAL    : real files, real subprocesses, the real executor/policy on this Linux machine.
  MOCKED  : the Windows-only module is imported with sys.platform / ctypes.WinDLL / comtypes / PowerShell replaced by fakes
            that return scripted answers. This checks OUR logic (arithmetic, ordering of calls, filtering, error handling,
            parsing). It does NOT show that the real Win32 / UI Automation / WinRT / PowerShell calls work, that struct
            layouts are right (wintypes sizes differ between Linux and Windows), or that Windows answers the way the fake does.
  STATIC  : the source code is read (ast / text). It proves a property of the text, not of behaviour.

No real keyboard, mouse, UI-automation or destructive operation is executed anywhere here: input functions are never called,
and the fake Windows libraries are plain mocks.
"""
import ast
import contextlib
import ctypes
import importlib
import json
import os
import struct
import subprocess
import sys
import threading
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_desktop_control import Harness                                  # noqa: E402  (shared helper only)
from jarvis.desktop import apps as APPS                                   # noqa: E402
from jarvis.desktop import executor as EX                                 # noqa: E402
from jarvis.desktop import ocr as OCR                                     # noqa: E402
from jarvis.desktop import policy as P                                    # noqa: E402
from jarvis.desktop import schema as S                                    # noqa: E402
from jarvis.desktop import sysinfo                                        # noqa: E402
from jarvis.desktop.audit import AuditLog                                 # noqa: E402
from jarvis.desktop.backend import PosixDesktop, SimulatedDesktop         # noqa: E402
from jarvis.desktop.stop import EmergencyStop                             # noqa: E402

DESKTOP = ROOT / "src" / "jarvis" / "desktop"


@contextlib.contextmanager
def fake_windows(module: str):
    """Import jarvis.desktop.<module> as if on Windows, with ctypes.WinDLL replaced by mocks. MOCKED."""
    with mock.patch.object(sys, "platform", "win32"), \
            mock.patch.object(ctypes, "WinDLL", lambda n, use_last_error=False: mock.MagicMock(name=n), create=True), \
            mock.patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
        sys.modules.pop(f"jarvis.desktop.{module}", None)
        mod = importlib.import_module(f"jarvis.desktop.{module}")
        yield mod
    sys.modules.pop(f"jarvis.desktop.{module}", None)


# =============================================================================== win32.py (MOCKED + pure logic)
class Win32FocusAndCaptureTests(unittest.TestCase):
    """MOCKED user32/gdi32. Proves: focus() never sends input events; capture() releases the bitmap before GetDIBits and
    cleans up on failure; bgra_to_png converts channels correctly. Cannot prove the real APIs behave as the mocks do."""

    def test_focus_reports_a_refusal_and_never_sends_input(self):
        with fake_windows("win32") as w:
            w.user32.IsWindow.return_value = 1
            w.user32.IsIconic.return_value = 0
            w.user32.GetForegroundWindow.return_value = 999            # some other window stays in front
            self.assertFalse(w.focus(123, wait=0.1))
            w.user32.SendInput.assert_not_called()
            w.user32.keybd_event.assert_not_called()
            w.user32.mouse_event.assert_not_called()

    def test_focus_succeeds_only_when_the_window_really_is_in_front(self):
        with fake_windows("win32") as w:
            w.user32.IsWindow.return_value = 1
            w.user32.IsIconic.return_value = 0
            w.user32.GetForegroundWindow.side_effect = [5, 5, 123, 123]
            self.assertTrue(w.focus(123, wait=1.0))
            w.user32.SendInput.assert_not_called()
            w.user32.IsWindow.return_value = 0
            self.assertFalse(w.focus(123))

    def _capture_setup(self, w, dibits=3, bitmap=77, mem=66, screen=55):
        w.user32.GetSystemMetrics.side_effect = lambda i: {76: 0, 77: 0, 78: 4, 79: 3}[i]
        w.user32.GetDC.return_value = screen
        w.gdi32.CreateCompatibleDC.return_value = mem
        w.gdi32.CreateCompatibleBitmap.return_value = bitmap
        w.gdi32.SelectObject.return_value = 11
        w.gdi32.BitBlt.return_value = 1
        w.gdi32.GetDIBits.return_value = dibits

    def test_capture_deselects_the_bitmap_before_reading_it_and_cleans_up(self):
        with fake_windows("win32") as w:
            self._capture_setup(w)
            width, height, png = w.capture(None)
            self.assertEqual((width, height), (4, 3))
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            names = [c[0] for c in w.gdi32.mock_calls if c[0] in ("SelectObject", "GetDIBits", "DeleteObject", "DeleteDC")]
            self.assertEqual(names, ["SelectObject", "SelectObject", "GetDIBits", "DeleteObject", "DeleteDC"],
                             "the old bitmap must be selected back BEFORE GetDIBits, then objects are released")
            sel = [c for c in w.gdi32.mock_calls if c[0] == "SelectObject"]
            self.assertEqual(sel[1][1], (66, 11), "second SelectObject restores the original bitmap into the DC")
            w.user32.ReleaseDC.assert_called_once()

    def test_capture_failures_raise_and_still_release_everything(self):
        with fake_windows("win32") as w:
            self._capture_setup(w, dibits=0)
            with self.assertRaises(OSError):
                w.capture(None)
            w.gdi32.DeleteObject.assert_called_once_with(77)
            w.gdi32.DeleteDC.assert_called_once_with(66)
            w.user32.ReleaseDC.assert_called_once()
        with fake_windows("win32") as w:
            self._capture_setup(w, bitmap=0)                           # CreateCompatibleBitmap failed (too large)
            with self.assertRaises(OSError):
                w.capture(None)
            w.gdi32.DeleteDC.assert_called_once_with(66)
            w.user32.ReleaseDC.assert_called_once()
            w.gdi32.GetDIBits.assert_not_called()
        with fake_windows("win32") as w:
            self._capture_setup(w)
            w.user32.GetSystemMetrics.side_effect = lambda i: {76: 0, 77: 0, 78: 0, 79: 0}[i]
            with self.assertRaises(OSError):
                w.capture(None)                                        # zero-size area

    def test_a_window_capture_falls_back_to_the_screen_when_printwindow_fails(self):
        with fake_windows("win32") as w:
            self._capture_setup(w)
            w.user32.GetWindowRect.side_effect = lambda h, ref: setattr(ref._obj, "right", 4) or setattr(ref._obj, "bottom", 3)
            w.user32.PrintWindow.return_value = 0
            w.capture(123)
            w.gdi32.BitBlt.assert_called_once()

    def test_bgra_to_png_swaps_channels_correctly(self):
        with fake_windows("win32") as w:
            bgra = bytes([10, 20, 30, 255,  1, 2, 3, 255])              # two pixels: B,G,R,A
            png = w.bgra_to_png(2, 1, bgra)
        pos, idat, hdr = 8, b"", None
        while pos < len(png):
            ln, kind = struct.unpack(">I4s", png[pos:pos + 8])
            data = png[pos + 8:pos + 8 + ln]
            if kind == b"IHDR":
                hdr = struct.unpack(">IIBBBBB", data)
            elif kind == b"IDAT":
                idat += data
            pos += 12 + ln
        self.assertEqual(hdr[:2], (2, 1))
        raw = zlib.decompress(idat)
        self.assertEqual(raw, bytes([0, 30, 20, 10, 3, 2, 1]), "filter byte 0 then RGB for each pixel")


class Win32ProcessAndWindowTests(unittest.TestCase):
    """MOCKED. Filtering and error handling of the window / process helpers. Cannot prove real enumeration results."""

    def test_is_elevated_fails_closed(self):
        with fake_windows("win32") as w:
            w.shell32.IsUserAnAdmin.return_value = 0
            self.assertFalse(w.is_elevated())
            w.shell32.IsUserAnAdmin.return_value = 1
            self.assertTrue(w.is_elevated())
            w.shell32.IsUserAnAdmin.side_effect = OSError("no")
            self.assertTrue(w.is_elevated(), "unknown must count as elevated so Jarvis only observes")

    def test_processes_returns_nothing_when_the_snapshot_fails(self):
        with fake_windows("win32") as w:
            w.kernel32.CreateToolhelp32Snapshot.return_value = ctypes.c_void_p(-1).value
            self.assertEqual(w.processes(), {})

    def test_processes_lists_lower_cased_names(self):
        with fake_windows("win32") as w:
            w.kernel32.CreateToolhelp32Snapshot.return_value = 1234
            entries = [(42, "Notepad.EXE"), (7, "Code.exe")]
            it = iter(entries)

            def fill(snap, ref):
                try:
                    pid, name = next(it)
                except StopIteration:
                    return 0
                ref._obj.th32ProcessID, ref._obj.szExeFile = pid, name
                return 1
            w.kernel32.Process32FirstW.side_effect = fill
            w.kernel32.Process32NextW.side_effect = fill
            self.assertEqual(w.processes(), {42: "notepad.exe", 7: "code.exe"})
            w.kernel32.CloseHandle.assert_called_with(1234)

    def test_process_path_is_none_when_the_process_cannot_be_opened(self):
        with fake_windows("win32") as w:
            w.kernel32.OpenProcess.return_value = 0
            self.assertIsNone(w.process_path(4))
            w.kernel32.OpenProcess.return_value = 9
            w.kernel32.QueryFullProcessImageNameW.return_value = 0
            self.assertIsNone(w.process_path(4))
            w.kernel32.CloseHandle.assert_called_with(9)

    def test_top_windows_skips_hidden_untitled_owned_and_cloaked_windows(self):
        with fake_windows("win32") as w:
            visible = {1: 1, 2: 0, 3: 1, 4: 1, 5: 1}
            title_len = {1: 5, 2: 5, 3: 0, 4: 5, 5: 5}
            owner = {1: 0, 2: 0, 3: 0, 4: 99, 5: 0}
            w.user32.IsWindowVisible.side_effect = lambda h: visible[h]
            w.user32.GetWindowTextLengthW.side_effect = lambda h: title_len[h]
            w.user32.GetWindow.side_effect = lambda h, kind: owner[h]
            w.user32.EnumWindows.side_effect = lambda cb, lp: [cb(h, 0) for h in (1, 2, 3, 4, 5)] and 1
            with mock.patch.object(w, "_cloaked", lambda h: h == 5), mock.patch.object(w, "window_info", lambda h: {"hwnd": h}):
                self.assertEqual(w.top_windows(), [{"hwnd": 1}])

    def test_window_info_derives_the_process_name_from_its_path(self):
        with fake_windows("win32") as w:
            w.user32.GetWindowTextLengthW.return_value = 0
            with mock.patch.object(w, "process_path", lambda pid: "C:\\Apps\\Tool\\Thing.EXE"):
                info = w.window_info(10)
            self.assertEqual((info["process"], info["process_path"]), ("thing.exe", "C:\\Apps\\Tool\\Thing.EXE"))
            with mock.patch.object(w, "process_path", lambda pid: None):
                info = w.window_info(10)
            self.assertEqual((info["process"], info["process_path"]), (None, None))


# =============================================================================== system metrics (MOCKED Windows branches)
def _fake_ctypes(kernel32=None, wininet=None):
    keep = {k: getattr(ctypes, k) for k in ("Structure", "c_ulong", "c_ulonglong", "c_int", "sizeof", "byref")}
    return types.SimpleNamespace(**keep, windll=types.SimpleNamespace(kernel32=kernel32 or mock.MagicMock(),
                                                                       wininet=wininet or mock.MagicMock()))


class SysInfoWindowsBranchTests(unittest.TestCase):
    """MOCKED Windows APIs. Proves our arithmetic and filtering; does NOT prove the real counters / drive types / adapters.
    (The Linux branches are tested by the existing SysInfoTests with real /proc data.)"""

    def test_cpu_usage_treats_kernel_time_as_including_idle(self):
        k32 = mock.MagicMock()
        samples = iter([(100, 1000, 500), (160, 1100, 560)])            # idle, kernel (incl. idle), user

        def times(i, k, u):
            idle, kern, user = next(samples)
            for ref, v in ((i, idle), (k, kern), (u, user)):
                ref._obj.dwLowDateTime, ref._obj.dwHighDateTime = v, 0
            return 1
        k32.GetSystemTimes.side_effect = times
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sysinfo, "_win", lambda: _fake_ctypes(k32)):
            r = sysinfo.cpu_usage(interval=0)
        self.assertEqual((r["available"], r["value"], r["source"]), (True, 62.5, "GetSystemTimes"))

    def test_cpu_usage_reports_unavailable_when_the_api_fails_or_no_time_passed(self):
        k32 = mock.MagicMock()
        k32.GetSystemTimes.return_value = 0
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sysinfo, "_win", lambda: _fake_ctypes(k32)):
            r = sysinfo.cpu_usage(interval=0)
        self.assertFalse(r["available"])
        self.assertIn("GetSystemTimes failed", r["why"])
        k32.GetSystemTimes.return_value = 1                             # all zeros -> no elapsed time
        k32.GetSystemTimes.side_effect = lambda i, k, u: 1
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sysinfo, "_win", lambda: _fake_ctypes(k32)):
            self.assertFalse(sysinfo.cpu_usage(interval=0)["available"])

    def test_memory_uses_physical_memory_and_reports_failure_honestly(self):
        k32 = mock.MagicMock()

        def status(ref):
            ref._obj.ullTotalPhys, ref._obj.ullAvailPhys = 16 * 2**30, 4 * 2**30
            return 1
        k32.GlobalMemoryStatusEx.side_effect = status
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sysinfo, "_win", lambda: _fake_ctypes(k32)):
            m = sysinfo.memory()
        self.assertEqual((m["total_gb"], m["available_gb"], m["used_percent"], m["source"]), (16.0, 4.0, 75.0, "GlobalMemoryStatusEx"))
        k32.GlobalMemoryStatusEx.side_effect = lambda ref: 0
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sysinfo, "_win", lambda: _fake_ctypes(k32)):
            self.assertFalse(sysinfo.memory()["available"])

    def test_storage_lists_only_fixed_drives_and_isolates_a_failing_drive(self):
        k32 = mock.MagicMock()
        k32.GetLogicalDrives.return_value = 0b111111                      # A..F
        k32.GetDriveTypeW.side_effect = lambda root: {"A:\\": 2, "B:\\": 1, "C:\\": 3, "D:\\": 3, "E:\\": 5, "F:\\": 4}[root]
        usage = types.SimpleNamespace(total=100 * 2**30, used=40 * 2**30, free=60 * 2**30)

        def du(d):
            if d == "D:\\":
                raise OSError("not ready")
            return usage
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sysinfo, "_win", lambda: _fake_ctypes(k32)), \
                mock.patch.object(sysinfo.shutil, "disk_usage", du):
            s = sysinfo.storage()
        self.assertEqual([d["drive"] for d in s["drives"]], ["C:\\", "D:\\"], "removable, optical and network drives are not listed")
        self.assertEqual(s["drives"][0]["used_percent"], 40.0)
        self.assertEqual(s["drives"][1]["available"], False)

    def test_network_on_windows_without_psutil_uses_the_wininet_flags_and_says_it_is_not_proof(self):
        wi = mock.MagicMock()

        def state(ref, reserved):
            ref._obj.value = 0x2 | 0x4
            return 1
        wi.InternetGetConnectedState.side_effect = state
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"psutil": None}), \
                mock.patch.object(sysinfo, "_win", lambda: _fake_ctypes(wininet=wi)):
            n = sysinfo.network()
        self.assertEqual((n["connected"], n["connection_types"]), (True, ["LAN", "proxy"]))
        self.assertIn("can be wrong", n["source"])

    def test_gpu_parses_nvidia_smi_including_not_available_fields(self):
        out = "NVIDIA GeForce RTX 3060, 12, 1024, 12288, 45\nNVIDIA Test, [N/A], [N/A], 8192, 50\n"
        cp = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch.object(sysinfo, "_nvidia_smi", lambda: "nvidia-smi"), mock.patch.object(sysinfo.subprocess, "run", lambda *a, **k: cp):
            g = sysinfo.gpu()
        self.assertEqual(g["source"], "nvidia-smi")
        self.assertEqual(g["gpus"][0]["utilization_percent"], 12.0)
        self.assertIsNone(g["gpus"][1]["utilization_percent"])
        self.assertIsNone(g["gpus"][1]["memory_used_mb"])

    def test_gpu_on_windows_without_nvidia_lists_names_only_and_says_metrics_are_unavailable(self):
        cp = subprocess.CompletedProcess([], 0, stdout="AMD Radeon(TM) Graphics\nMicrosoft Basic Render\n", stderr="")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sysinfo, "_nvidia_smi", lambda: None), \
                mock.patch.object(sysinfo.subprocess, "run", lambda *a, **k: cp):
            g = sysinfo.gpu()
        self.assertEqual([x["name"] for x in g["gpus"]], ["AMD Radeon(TM) Graphics", "Microsoft Basic Render"])
        self.assertNotIn("utilization_percent", json.dumps(g))
        self.assertIn("not available", g["metrics"])

    def test_every_text_mode_subprocess_call_tolerates_undecodable_output(self):
        """STATIC: subprocess.run(..., text=True) without errors= can raise UnicodeDecodeError on a legacy code page."""
        bad = []
        for f in DESKTOP.glob("*.py"):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("run", "check_output", "Popen"):
                    kws = {k.arg for k in node.keywords}
                    if "text" in kws and "errors" not in kws:
                        bad.append(f"{f.name}:{node.lineno}")
        self.assertEqual(bad, [])


# =============================================================================== app discovery
def make_lnk(base: str, suffix: str = "", unicode_base: str | None = None, id_list: bytes | None = None) -> bytes:
    """A minimal SYNTHETIC MS-SHLLINK file (not produced by Windows)."""
    flags = 0x2 | (0x1 if id_list is not None else 0)
    head = bytearray(0x4C)
    struct.pack_into("<I", head, 0, 0x4C)
    struct.pack_into("<I", head, 0x14, flags)
    out = bytes(head)
    if id_list is not None:
        out += struct.pack("<H", len(id_list)) + id_list
    base_b, suffix_b = base.encode("cp1252") + b"\0", suffix.encode("cp1252") + b"\0"
    hsize = 0x24 if unicode_base is not None else 0x1C
    lbp = hsize
    cps = lbp + len(base_b)
    body = base_b + suffix_b
    ubase = b""
    if unicode_base is not None:
        ubase = unicode_base.encode("utf-16-le") + b"\0\0"
    lbpu = hsize + len(body)
    total = hsize + len(body) + len(ubase)
    li = struct.pack("<7I", total, hsize, 1, 0, lbp, 0, cps)
    if unicode_base is not None:
        li += struct.pack("<I", lbpu) + struct.pack("<I", 0)
    return out + li + body + ubase


class AppDiscoveryTests(unittest.TestCase):
    """REAL parsing of SYNTHETIC shortcut files and REAL filesystem walking of a fake Start menu; MOCKED PowerShell.
    Cannot prove that real Windows shortcuts (advertised, MSI, Store) parse, or that Get-StartApps output looks like this."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lnk_target_reads_ansi_and_unicode_paths_and_the_suffix(self):
        f = self.d / "a.lnk"
        f.write_bytes(make_lnk("C:\\Program Files\\App", "\\app.exe"))
        self.assertEqual(APPS.lnk_target(f), "C:\\Program Files\\App\\app.exe")
        f.write_bytes(make_lnk("C:\\Prog", "\\x.exe", unicode_base="C:\\Programme\\Gr\u00fc\u00dfe"))
        self.assertTrue(APPS.lnk_target(f).startswith("C:\\Programme\\Gr\u00fc\u00dfe"))
        f.write_bytes(make_lnk("C:\\Apps\\x.exe", id_list=b"\x01" * 20))
        self.assertEqual(APPS.lnk_target(f), "C:\\Apps\\x.exe")

    def test_lnk_target_returns_none_for_damaged_virtual_or_missing_files(self):
        f = self.d / "bad.lnk"
        good = make_lnk("C:\\Apps\\x.exe")
        for blob in (b"", b"MZ" + b"\0" * 100, good[:0x50], good[:0x4C + 10], b"\x4c\0\0\0" + b"\xff" * 300):
            f.write_bytes(blob)
            self.assertIsNone(APPS.lnk_target(f), blob[:12])
        self.assertIsNone(APPS.lnk_target(self.d / "missing.lnk"))
        head = bytearray(make_lnk("x"))
        struct.pack_into("<I", head, 0x14, 0)                              # no LinkInfo: advertised / virtual shortcut
        f.write_bytes(bytes(head))
        self.assertIsNone(APPS.lnk_target(f))

    def test_start_menu_discovery_ids_names_and_uninstallers(self):
        pd, ad = self.d / "pd", self.d / "ad"
        for base, rel in ((pd, "Tools/Notepad Plus.lnk"), (pd, "Uninstall Thing.lnk"), (ad, "Games/My Game.lnk"), (ad, "Notes.txt")):
            p = base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(make_lnk("C:\\x\\" + p.stem + ".exe") if p.suffix == ".lnk" else b"hi")
        with mock.patch.dict(os.environ, {"ProgramData": str(pd), "APPDATA": str(ad)}):
            apps = APPS.from_start_menu()
        self.assertEqual(sorted(a["id"] for a in apps), ["lnk:games/my game.lnk", "lnk:tools/notepad plus.lnk"])
        self.assertTrue(all(a["kind"] == "lnk" and a["target"] for a in apps))

    def _store(self, stdout=None, exc=None):
        def run(*a, **k):
            if exc:
                raise exc
            return subprocess.CompletedProcess(a, 0, stdout=stdout, stderr="")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(APPS.subprocess, "run", run):
            return APPS.from_store()

    def test_store_apps_keep_only_packaged_ids_and_decode_ascii_escaped_unicode(self):
        out = ('[{"Name":"Caf\\u00e9 Rechner","AppID":"Pub.Calc_8we!App"},{"Name":"Uninstall Foo","AppID":"P.F_1!x"},'
               '{"Name":"Plain Win32","AppID":"C:\\\\Windows\\\\x.exe"},{"Name":"Emoji \\ud83d\\ude00","AppID":"Pub.E_1!E"}]')
        apps = self._store(out)
        self.assertEqual([a["name"] for a in apps], ["Caf\u00e9 Rechner", "Emoji \U0001F600"])
        self.assertEqual(apps[0]["id"], "aumid:pub.calc_8we!app")
        self.assertEqual(self._store('{"Name":"Solo","AppID":"P.S_1!S"}')[0]["name"], "Solo")      # PowerShell collapses 1-item arrays

    def test_store_discovery_failures_give_an_empty_list_not_an_exception(self):
        self.assertEqual(self._store(""), [])
        self.assertEqual(self._store("not json"), [])
        self.assertEqual(self._store(exc=subprocess.TimeoutExpired("ps", 20)), [])
        self.assertEqual(self._store(exc=FileNotFoundError("no powershell")), [])
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(APPS.from_store(), [])

    def test_the_powershell_command_escapes_non_ascii_and_skips_profiles(self):
        """STATIC: the command text, not its behaviour on Windows."""
        self.assertIn("MatchEvaluator", APPS._STARTAPPS_PS)
        self.assertIn("\\x7F", APPS._STARTAPPS_PS)
        seen = {}

        def run(cmd, **k):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(APPS.subprocess, "run", run):
            APPS.from_store()
        self.assertIn("-NoProfile", seen["cmd"])
        self.assertIn("-NonInteractive", seen["cmd"])

    def test_allowlist_resolution_and_installer_detection(self):
        exe = self.d / "tool.exe"
        exe.write_text("x")
        apps = APPS.from_allowlist({"Tool": str(exe), "Missing": str(self.d / "nope.exe")})
        self.assertEqual([a["id"] for a in apps], ["exe:tool"])
        self.assertTrue(APPS.is_installer({"name": "Foo Setup", "target": None}))
        self.assertTrue(APPS.is_installer({"name": "Foo", "target": "c:\\x\\foo.msi"}))
        self.assertTrue(APPS.is_installer({"name": "Foo", "path": "c:\\dl\\install_foo.exe"}))
        self.assertFalse(APPS.is_installer({"name": "Notepad", "target": "c:\\windows\\notepad.exe"}))
        listing = [{"id": "lnk:a", "name": "Visual Studio Code"}, {"id": "lnk:b", "name": "Visual Studio 2022"}]
        self.assertEqual(len(APPS.match(listing, "visual studio")), 2)
        self.assertEqual([a["id"] for a in APPS.match(listing, "lnk:a")], ["lnk:a"])


# =============================================================================== OCR (MOCKED PowerShell)
class _FakePS:
    def exists(self):
        return True

    def __str__(self):
        return "powershell.exe"


class OcrTests(unittest.TestCase):
    """MOCKED powershell.exe / tesseract. Proves our JSON handling and fallbacks. Cannot prove Windows.Media.Ocr works,
    that the PNG decodes to a supported bitmap format, or that an OCR language is installed."""

    def _run(self, stdout="", rc=0, exc=None, tess=None, tess_out=None):
        calls = []

        def run(cmd, **k):
            calls.append(cmd)
            if exc and cmd[0] == "powershell.exe":
                raise exc
            if cmd[0] == "powershell.exe":
                return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="oops")
            return subprocess.CompletedProcess(cmd, 0, stdout=tess_out or "", stderr="")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(OCR, "_powershell", lambda: _FakePS()), \
                mock.patch.object(OCR.subprocess, "run", run), mock.patch.object(OCR.shutil, "which", lambda n: tess):
            return OCR.ocr_png(Path("x.png")), calls

    def test_windows_ocr_json_is_parsed_with_unicode_escapes_and_a_single_line_dict(self):
        out, calls = self._run('{"engine":"Windows.Media.Ocr","language":"de-DE","lines":{"text":"Gr\\u00fc\\u00dfe","rect":[1,2,3,4]}}')
        self.assertEqual(out["lines"], [{"text": "Gr\u00fc\u00dfe", "rect": [1, 2, 3, 4]}])
        self.assertIn("-NoProfile", calls[0])
        self.assertIn("-File", calls[0])

    def test_noise_before_the_json_line_is_ignored_and_empty_lines_become_a_list(self):
        out, _ = self._run('WARNING: something\n{"engine":"Windows.Media.Ocr","language":"en-US","lines":null}')
        self.assertEqual(out["lines"], [])

    def test_failures_fall_back_to_tesseract_or_say_exactly_why_nothing_worked(self):
        out, _ = self._run('{"error":"no OCR language is installed in Windows"}', rc=2, tess="tesseract", tess_out="hello\n\nworld\n")
        self.assertEqual((out["engine"], [l["text"] for l in out["lines"]]), ("tesseract", ["hello", "world"]))
        for kwargs in ({"stdout": "garbage"}, {"stdout": ""}, {"exc": subprocess.TimeoutExpired("ps", 30)}, {"exc": OSError("x")}):
            with self.assertRaises(OCR.OcrUnavailable) as cm:
                self._run(**kwargs)
            self.assertIn("Windows OCR", str(cm.exception))

    def test_non_windows_without_tesseract_is_a_clear_unavailable_error(self):
        with mock.patch.object(OCR.shutil, "which", lambda n: None), mock.patch.object(sys, "platform", "linux"):
            with self.assertRaises(OCR.OcrUnavailable):
                OCR.ocr_png(Path("x.png"))

    def test_the_powershell_script_emits_only_ascii_escaped_json(self):
        """STATIC: every ConvertTo-Json in the script goes through Out-AsciiJson."""
        text = (DESKTOP / "ocr_winrt.ps1").read_text(encoding="utf-8")
        self.assertEqual(text.count("ConvertTo-Json"), 1)
        self.assertIn("function Out-AsciiJson", text)
        self.assertEqual(text.count("Out-AsciiJson @{"), 3)
        self.assertTrue(text.isascii())


# =============================================================================== emergency stop
class StopMechanismTests(unittest.TestCase):
    """REAL for the event / STOP-file logic; MOCKED user32 for the Windows hotkey loop (cannot prove Ctrl+Alt+Pause is
    delivered by Windows, that the key exists on the keyboard, or that another program does not already own it)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.f = Path(self.tmp.name) / "STOP"

    def tearDown(self):
        self.tmp.cleanup()

    def test_stop_file_event_wait_and_clear(self):
        st = EmergencyStop(self.f)
        st.check()
        self.f.write_text("x")
        self.assertTrue(st.is_set())
        self.assertIn("file", st.reason)
        with self.assertRaises(Exception):
            st.wait(5)
        st.clear()
        self.assertFalse(self.f.exists() or st.is_set())
        st.stop("because")
        self.assertEqual(st.reason, "because")

    def test_wait_is_interrupted_promptly(self):
        import time
        st = EmergencyStop(None)
        threading.Timer(0.2, lambda: st.stop("now")).start()
        t0 = time.monotonic()
        with self.assertRaises(Exception):
            st.wait(10)
        self.assertLess(time.monotonic() - t0, 3)

    def _hotkey(self, register_ok, messages):
        user32 = mock.MagicMock()
        user32.RegisterHotKey.return_value = 1 if register_ok else 0
        feed = iter(messages)

        def get_message(ref, hwnd, lo, hi):
            try:
                m = next(feed)
            except StopIteration:
                return 0
            ref._obj.message = m
            return 1
        user32.GetMessageW.side_effect = get_message
        st = EmergencyStop(None)
        fake = types.SimpleNamespace(user32=user32)
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(ctypes, "windll", fake, create=True):
            started = st.start_hotkey()
            if started:
                st._hotkey_thread.join(2)
        return st, started, user32

    def test_hotkey_registers_ctrl_alt_pause_and_stops_when_the_message_arrives(self):
        st, started, user32 = self._hotkey(True, [0x0312])
        self.assertTrue(started)
        self.assertTrue(st.is_set())
        self.assertIn("Ctrl+Alt+Pause", st.reason)
        hwnd, ident, mods, vk = user32.RegisterHotKey.call_args[0]
        self.assertEqual((mods, vk), (0x1 | 0x2 | 0x4000, 0x13))

    def test_other_messages_do_not_stop_and_a_taken_hotkey_is_reported(self):
        st, started, _ = self._hotkey(True, [0x0100, 0x0200])
        self.assertTrue(started)
        self.assertFalse(st.is_set())
        st2, started2, _ = self._hotkey(False, [])
        self.assertFalse(started2)
        self.assertFalse(st2.is_set())

    def test_hotkey_is_not_started_off_windows(self):
        self.assertFalse(EmergencyStop(None).start_hotkey())


# =============================================================================== UI Automation wrapper (MOCKED)
class FakeEl:
    def __init__(self, name="", ctype=50000, aid="", enabled=True, pw=False, patterns=None, rect=(0, 0, 10, 10)):
        self.CurrentName, self.CurrentControlType, self.CurrentAutomationId = name, ctype, aid
        self.CurrentIsEnabled, self.CurrentIsOffscreen, self.CurrentIsPassword = enabled, False, pw
        self.CurrentBoundingRectangle = types.SimpleNamespace(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3])
        self.patterns, self.focused = patterns or {}, False

    def GetCurrentPattern(self, pid):
        return self.patterns.get(pid)

    def SetFocus(self):
        self.focused = True


class FakePattern:
    def __init__(self, **attrs):
        self.calls = []
        for k, v in attrs.items():
            setattr(self, k, v)

    def QueryInterface(self, iface):
        return self

    def __getattr__(self, name):
        if name in ("Invoke", "Toggle", "Select", "Expand", "Collapse", "SetValue"):
            return lambda *a: self.calls.append((name, a))
        raise AttributeError(name)


class UiaWrapperTests(unittest.TestCase):
    """MOCKED comtypes and elements. Proves selection, limits, password handling, pattern dispatch and per-thread COM
    clients. Cannot prove that real UIAutomationCore returns these properties, that comtypes generates its wrappers in the
    frozen app, or how slow a real window with thousands of elements is."""

    def setUp(self):
        self.ctx = fake_windows("uia")
        self.uia = self.ctx.__enter__()
        self.mod = types.SimpleNamespace()
        self.uia._client = lambda: (self.fake_uia, self.mod)
        self.mod_names = ("IUIAutomationValuePattern", "IUIAutomationTogglePattern", "IUIAutomationSelectionItemPattern",
                          "IUIAutomationExpandCollapsePattern", "IUIAutomationInvokePattern")
        for n in self.mod_names:
            setattr(self.mod, n, n)
        self.fake_uia = mock.MagicMock()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def _tree(self, els):
        arr = mock.MagicMock()
        arr.Length = len(els)
        arr.GetElement.side_effect = lambda i: els[i]
        self.fake_uia.ElementFromHandle.return_value.FindAll.return_value = arr
        return arr

    def test_find_filters_by_name_type_and_id_and_reports_indexes(self):
        self._tree([FakeEl("OK", 50000, "ok"), FakeEl("Cancel", 50000), FakeEl("Name box", 50004, "name"), FakeEl("ok again", 50000)])
        got = self.uia.find(1, name="ok", control_type="button")
        self.assertEqual([d["name"] for _, d in got], ["OK", "ok again"])
        self.assertEqual([d["index"] for _, d in got], [0, 1])
        self.assertEqual(len(self.uia.find(1, name="ok", exact=True)), 1)
        self.assertEqual(self.uia.find(1, automation_id="name")[0][1]["control_type"], "edit")

    def test_find_never_looks_at_more_than_5000_elements_and_honours_the_limit(self):
        arr = self._tree([FakeEl(f"e{i}") for i in range(6000)])
        self.assertEqual(len(self.uia.find(1, limit=3)), 3)
        self.uia.find(1, name="zzz")
        self.assertLessEqual(arr.GetElement.call_count, 3 + 5000)

    def test_an_element_that_vanishes_while_enumerating_is_skipped(self):
        class Gone(FakeEl):
            @property
            def CurrentName(self):
                raise OSError("element gone")

            @CurrentName.setter
            def CurrentName(self, v):
                pass
        self._tree([FakeEl("a"), Gone("gone"), FakeEl("b")])
        self.assertEqual([d["name"] for _, d in self.uia.find(1)], ["a", "b"])

    def test_password_fields_are_never_read_or_written(self):
        pw = FakeEl("Password", 50004, pw=True, patterns={self.uia.P_VALUE: FakePattern(CurrentValue="hunter2", CurrentIsReadOnly=False)})
        r = self.uia.read(pw)
        self.assertIsNone(r["value"])
        self.assertNotIn("hunter2", json.dumps(r))
        with self.assertRaises(PermissionError):
            self.uia.act(pw, "set_value", "x")
        self.assertEqual(pw.patterns[self.uia.P_VALUE].calls, [])

    def test_actions_use_the_right_pattern_and_refuse_unsupported_ones(self):
        inv = FakePattern()
        el = FakeEl("Go", patterns={self.uia.P_INVOKE: inv})
        self.assertEqual(self.uia.act(el, "invoke"), "Invoke")
        self.assertEqual(inv.calls, [("Invoke", ())])
        with self.assertRaises(ValueError):
            self.uia.act(el, "toggle")
        ro = FakeEl("RO", 50004, patterns={self.uia.P_VALUE: FakePattern(CurrentIsReadOnly=True)})
        with self.assertRaises(ValueError):
            self.uia.act(ro, "set_value", "x")
        rw = FakePattern(CurrentIsReadOnly=False)
        self.assertEqual(self.uia.act(FakeEl("RW", 50004, patterns={self.uia.P_VALUE: rw}), "set_value", "abc"), "Value")
        self.assertEqual(rw.calls, [("SetValue", ("abc",))])
        with self.assertRaises(ValueError):
            self.uia.act(el, "explode")

    def test_read_collects_value_toggle_selection_and_expand_state(self):
        el = FakeEl("Box", patterns={
            self.uia.P_VALUE: FakePattern(CurrentValue="v"), self.uia.P_TOGGLE: FakePattern(CurrentToggleState=1),
            self.uia.P_SELECTIONITEM: FakePattern(CurrentIsSelected=True), self.uia.P_EXPANDCOLLAPSE: FakePattern(CurrentExpandCollapseState=0)})
        self.assertEqual(self.uia.read(el), {"name": "Box", "value": "v", "toggle_state": "on", "selected": True, "expanded": False})

    def test_each_thread_gets_its_own_com_client(self):
        """The real _client (not the test double) must not share one COM object between threads."""
        created, ctr = [], {"n": 0}

        def create_object(cls, interface=None):
            ctr["n"] += 1
            created.append(threading.get_ident())
            return object()
        gen_mod = types.SimpleNamespace(CUIAutomation="C", IUIAutomation="I")
        fake_comtypes = types.ModuleType("comtypes")
        fake_comtypes.CoInitialize = lambda: None
        fake_client = types.ModuleType("comtypes.client")
        fake_client.GetModule = lambda dll: None
        fake_client.CreateObject = create_object
        fake_gen = types.ModuleType("comtypes.gen")
        fake_gen.UIAutomationClient = gen_mod
        fake_comtypes.client, fake_comtypes.gen = fake_client, fake_gen
        with fake_windows("uia") as real, mock.patch.dict(sys.modules, {"comtypes": fake_comtypes, "comtypes.client": fake_client,
                                                                          "comtypes.gen": fake_gen,
                                                                          "comtypes.gen.UIAutomationClient": gen_mod}):
            a1, a2 = real._client(), real._client()
            self.assertIs(a1, a2)
            box = {}
            t = threading.Thread(target=lambda: box.setdefault("c", real._client()))
            t.start()
            t.join()
            self.assertIsNot(box["c"], a1)
        self.assertEqual(ctr["n"], 2)
        self.assertEqual(len(set(created)), 2)


# =============================================================================== policy and executor error handling (REAL logic)
class FailClosedTests(Harness):
    """REAL policy / executor logic with a REAL (Linux) or FAKE (simulated) backend. Proves we refuse when the information
    needed for a safety check is missing. Says nothing about what the real Windows backend returns."""

    def _spec(self, name):
        from jarvis.desktop.tools import registry
        return registry()[name]

    def test_input_is_refused_when_the_process_list_or_the_target_owner_is_unknown(self):
        s = P.Settings()
        s.enabled["input"] = True
        spec = self._spec("type_text")
        ok = P.decide(spec, S.INPUT, s, P.Context(running_processes={"explorer.exe"}, target_process="notepad.exe"))
        self.assertTrue(ok.allowed)
        for ctx, word in ((P.Context(running_processes=set(), target_process="notepad.exe"), "list the running"),
                          (P.Context(running_processes=None, target_process="notepad.exe"), "list the running"),
                          (P.Context(running_processes={"explorer.exe"}, target_process=None), "which program owns"),
                          (P.Context(running_processes={"explorer.exe"}, target_process=""), "which program owns")):
            d = P.decide(spec, S.INPUT, s, ctx)
            self.assertFalse(d.allowed)
            self.assertIn(word, d.reason)

    def test_existing_refusals_still_apply(self):
        s = P.Settings()
        s.enabled["input"] = True
        spec = self._spec("type_text")
        self.assertFalse(P.decide(spec, S.INPUT, s, P.Context(running_processes={"x.exe"}, target_process="cmd.exe")).allowed)
        self.assertFalse(P.decide(spec, S.INPUT, s, P.Context(running_processes={"consent.exe"}, target_process="consent.exe")).allowed)
        self.assertFalse(P.decide(spec, S.INPUT, s, P.Context(running_processes={"vgc.exe", "game.exe"}, target_process="game.exe")).allowed)

    def test_a_backend_that_cannot_say_whether_it_is_elevated_only_observes(self):
        class Unsure(SimulatedDesktop):
            def elevated(self):
                raise OSError("cannot tell")
        self.make(backend=Unsure())
        r = self.run_tool("write_file", path="a.txt", content="x")
        self.assertEqual(r.status, "denied", r.message)
        self.assertIn("administrator", r.message)
        self.assertFalse((self.work / "a.txt").exists())
        self.assertEqual(self.run_tool("file_info", path="a.txt").status, "verified")        # observing still works

    def test_a_backend_that_cannot_list_processes_blocks_input_instead_of_crashing(self):
        class Blind(SimulatedDesktop):
            def process_names(self):
                raise OSError("snapshot failed")
        be = Blind()
        self.make(backend=be, enable=("apps", "input", "windows"))
        self.env.settings.trusted_apps = ["sim:notepad"]
        self.assertEqual(self.run_tool("launch_app", app="sim:notepad").status, "verified")
        r = self.run_tool("type_text", window="Sim Notepad", text="hello")
        self.assertEqual(r.status, "denied", r.message)
        self.assertIn("running programs", r.message)
        self.assertEqual(be.typed, [])

    def test_an_unexpected_internal_error_becomes_a_failed_result_and_nothing_runs(self):
        self.make()
        with mock.patch("jarvis.desktop.tools.bind_action", side_effect=RuntimeError("boom")):
            r = self.run_tool("write_file", path="a.txt", content="x")
        self.assertEqual(r.status, "failed")
        self.assertIn("nothing was done", r.message)
        self.assertFalse((self.work / "a.txt").exists())

    def test_if_the_audit_log_cannot_record_the_start_the_action_does_not_run(self):
        self.make()

        class Broken(AuditLog):
            def write(self, **rec):
                if rec.get("event") == "start":
                    raise OSError("disk full")
                return super().write(**rec)
        self.ex.audit = Broken(self.profile / "audit.jsonl")
        r = self.run_tool("write_file", path="a.txt", content="x")
        self.assertEqual(r.status, "failed", r.message)
        self.assertFalse((self.work / "a.txt").exists())

    def test_if_the_audit_log_cannot_record_the_end_the_result_is_kept_and_the_gap_is_stated(self):
        self.make()

        class Broken(AuditLog):
            def write(self, **rec):
                if rec.get("event") == "end":
                    raise OSError("disk full")
                return super().write(**rec)
        self.ex.audit = Broken(self.profile / "audit.jsonl")
        r = self.run_tool("write_file", path="a.txt", content="x")
        self.assertEqual(r.status, "verified")
        self.assertIn("audit log could not be written", r.message)


class LaunchBindingTests(Harness):
    """REAL executor + FAKE simulated desktop. Proves the confirmation names the exact program and a change forces a new
    question. Cannot prove how real Start-menu shortcuts or Store apps are resolved."""

    def test_the_question_shows_the_exact_program_and_the_pinned_id(self):
        self.make(enable=("apps",))
        r = self.run_tool("launch_app", app="Sim Notepad")
        self.assertEqual(r.status, "verified", r.message)
        req = self.asked[0]
        self.assertEqual(req.args["app"], "sim:notepad")
        self.assertIn("sim:notepad", req.description)
        self.assertIn("starts 'Sim Notepad'", req.description)
        self.assertEqual(r.args["app"], "sim:notepad")

    def test_a_shortcut_target_is_shown_to_the_user(self):
        be = SimulatedDesktop()
        be.catalog["lnk:tools/calc.lnk"] = {"id": "lnk:tools/calc.lnk", "name": "Calculator", "kind": "sim", "path": "C:\\Users\\me\\Start\\calc.lnk",
                                            "target": "C:\\Users\\me\\Downloads\\totally_not_malware.exe", "source": "Start menu shortcut"}
        self.make(backend=be, enable=("apps",))
        self.run_tool("launch_app", app="Calculator")
        self.assertIn("totally_not_malware.exe", self.asked[0].description)

    def test_ambiguous_or_unknown_apps_fail_before_any_question(self):
        self.make(enable=("apps",))
        for q in ("sim", "no such program"):
            r = self.run_tool("launch_app", app=q)
            self.assertEqual(r.status, "failed", (q, r.message))
        self.assertEqual(self.asked, [])

    def test_if_the_program_behind_the_name_changes_while_the_question_is_open_nothing_starts(self):
        be = SimulatedDesktop()
        self.make(backend=be, enable=("apps",))

        def swap(req):
            be.catalog["sim:notepad"]["target"] = "C:\\somewhere\\else.exe"
            return True
        self.answer = swap
        r = self.run_tool("launch_app", app="sim:notepad")
        self.assertEqual(r.status, "denied", r.message)
        self.assertEqual(be.windows(), [])

    def test_a_trusted_app_still_starts_without_a_question(self):
        self.make(enable=("apps",))
        self.env.settings.trusted_apps = ["sim:notepad"]
        self.assertEqual(self.run_tool("launch_app", app="Sim Notepad").status, "verified")
        self.assertEqual(self.asked, [])


# =============================================================================== worker process integration (REAL subprocesses)
class WorkerIntegrationTests(Harness):
    """REAL child processes standing in for the worker (a tiny python -c script). Proves the parent handles crashes, garbage,
    silence and odd results without crashing or claiming success. Runs on Linux only: process creation flags, the frozen
    jarvis.exe entry point, antivirus interference and startup time on Windows are NOT covered."""

    def _with_worker(self, script):
        return mock.patch.object(EX, "worker_command", lambda: [sys.executable, "-I", "-c", script])

    def _write(self):
        self.make(backend=PosixDesktop(), isolation="process")
        with self._with_worker(self.script):
            return self.run_tool("write_file", path="w.txt", content="x")

    def test_garbage_silence_crash_and_unknown_errors_are_failures_never_successes(self):
        cases = {
            "garbage": "print('this is not json')",
            "silent": "pass",
            "crash": "import sys; sys.stderr.write('Traceback...'); sys.exit(3)",
            "unknown error type": "print('{\"error\": {\"type\": \"Weird\", \"message\": \"boom\"}}')",
            "truncated json": "print('{\"outcome\": {\"ok\": tru')",
            "noise then error": "print('x'*100000); print('{\"error\": {\"type\": \"PermissionError\", \"message\": \"nope\"}}')",
        }
        for label, script in cases.items():
            self.script = script
            r = self._write()
            expect = "denied" if label == "noise then error" else "failed"
            self.assertEqual(r.status, expect, (label, r.message))
            self.assertFalse((self.work / "w.txt").exists(), label)

    def test_a_worker_result_without_evidence_is_not_verified(self):
        self.script = "print('{\"outcome\": {\"ok\": true, \"summary\": \"done\", \"verified\": true}}')"
        r = self._write()
        self.assertEqual(r.status, "unverified", r.message)

    def test_a_worker_that_hangs_is_killed_at_the_timeout_and_reported_unknown(self):
        import time
        self.make(backend=PosixDesktop(), isolation="process")
        self.ex.specs["wait"] = __import__("dataclasses").replace(self.ex.specs["wait"], timeout=0.6)
        t0 = time.monotonic()
        with self._with_worker("import time; time.sleep(60)"):
            r = self.run_tool("wait", seconds=1)
        self.assertEqual(r.status, "timeout")
        self.assertTrue(r.started)
        self.assertIn("check before retrying", r.message)
        self.assertLess(time.monotonic() - t0, 20)
        self.assertIsNone(self.ex._proc)

    def test_emergency_stop_kills_a_running_worker_and_reports_it_started(self):
        import time
        self.make(backend=PosixDesktop(), isolation="process")
        threading.Timer(0.5, lambda: self.env.stop.stop("pressed")).start()
        t0 = time.monotonic()
        with self._with_worker("import time; time.sleep(60)"):
            r = self.run_tool("wait", seconds=1)
        self.assertEqual((r.status, r.started), ("stopped", True))
        self.assertLess(time.monotonic() - t0, 20)

    def test_the_worker_request_carries_no_secrets_and_the_protected_folders(self):
        seen = {}

        def fake_popen(cmd, **k):
            class P:
                returncode = 0

                def communicate(self_inner, data):
                    seen["req"] = json.loads(data.decode())
                    return b'{"outcome": {"ok": true, "summary": "s", "verified": true, "evidence": "e"}}\n', b""

                def poll(self_inner):
                    return 0

                def kill(self_inner):
                    pass
            return P()
        self.make(backend=PosixDesktop(), isolation="process")
        with mock.patch.object(EX.subprocess, "Popen", fake_popen):
            self.run_tool("write_file", path="w.txt", content="PRIVATE-TEXT")
        req = seen["req"]
        self.assertIn(str(self.profile), req["protected"])
        self.assertTrue(req["args_digest"])
        self.assertEqual(set(req) - {"tool", "args", "settings", "profile_dir", "protected", "args_digest", "allowlist", "guest", "stop_file"}, set())


# =============================================================================== static guards
class StaticGuardTests(unittest.TestCase):
    """STATIC: reads source text. Proves properties of the code as written, not of its behaviour."""

    def _functions(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    @staticmethod
    def _names(node):
        out = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
        return out

    def test_focus_window_support_code_cannot_inject_input(self):
        fns = self._functions(DESKTOP / "win32.py")
        for banned in ("send_vk_sequence", "send_unicode", "click", "_send", "SendInput", "keybd_event", "mouse_event"):
            self.assertNotIn(banned, self._names(fns["focus"]), banned)

    def test_input_injection_is_reachable_only_from_the_three_input_backend_methods(self):
        callers = []
        banned = {"send_unicode", "send_vk_sequence", "SendInput", "keybd_event", "mouse_event", "click"}
        for f in DESKTOP.glob("*.py"):
            if f.name == "win32.py":
                continue
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # attribute CALLS only, so a method merely named "click" does not count
                    used = {c.func.attr for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)} & banned
                    if used:
                        callers.append((f.name, node.name))
        self.assertEqual(sorted(callers), [("backend.py", "click"), ("backend.py", "press"), ("backend.py", "type_text"),
                          ("tools.py", "h_mouse_click")],
                         "any new caller of an input primitive must be reviewed and added here on purpose")
        w = self._functions(DESKTOP / "win32.py")
        users = {n for n, fn in w.items() if self._names(fn) & {"_send", "SendInput"}}
        self.assertEqual(users, {"_send", "send_vk_sequence", "send_unicode", "click"})

    def test_packaging_includes_the_ocr_script_and_comtypes_and_the_extra_is_declared(self):
        spec = (ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8") + (ROOT / "packaging" / "manifest.py").read_text(encoding="utf-8")   # Prompt 10: lists moved to manifest.py
        self.assertIn("ocr_winrt.ps1", spec)
        self.assertIn('"comtypes"', spec)
        self.assertIn("comtypes", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertTrue((DESKTOP / "ocr_winrt.ps1").exists())

    def test_windows_only_modules_refuse_to_import_elsewhere(self):
        for name in ("win32", "uia"):
            sys.modules.pop(f"jarvis.desktop.{name}", None)
            with self.assertRaises(ImportError):
                importlib.import_module(f"jarvis.desktop.{name}")

    def test_no_shell_is_used_to_start_processes(self):
        for f in DESKTOP.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text, f.name)
            self.assertNotIn("os.system(", text, f.name)


if __name__ == "__main__":
    unittest.main()
