"""Tests for scripts/check_vc_runtime.py using small SYNTHETIC PE files (not real Windows binaries)."""
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_vc_runtime", ROOT / "scripts" / "check_vc_runtime.py")
cv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cv)


def make_pe(imports, machine=0x8664, pe32plus=True) -> bytes:
    """Minimal PE with one section holding an import table that names `imports`."""
    opt_size = 240 if pe32plus else 224
    pe_off = 0x40
    head = bytearray(0x200)
    head[0:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, pe_off)
    head[pe_off:pe_off + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", head, pe_off + 4, machine, 1, 0, 0, 0, opt_size, 0x2022)
    opt = pe_off + 24
    struct.pack_into("<H", head, opt, 0x20B if pe32plus else 0x10B)
    dd = opt + (112 if pe32plus else 96)
    struct.pack_into("<I", head, dd - 4, 16)
    va, raw = 0x1000, 0x200
    n = len(imports)
    desc_size = 20 * (n + 1)
    struct.pack_into("<II", head, dd + 8, va, desc_size)
    sec = opt + opt_size
    body = bytearray(desc_size)
    names_at = desc_size
    for i, name in enumerate(imports):
        struct.pack_into("<IIIII", body, 20 * i, 0x2000, 0, 0, va + names_at, 0x2000)
        b = name.encode("ascii") + b"\0"
        body += b
        names_at += len(b)
    head[sec:sec + 8] = b".idata\0\0"
    struct.pack_into("<IIII", head, sec + 8, len(body), va, len(body), raw)
    return bytes(head) + bytes(body)


class PeParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_imports_and_machine(self):
        p = self.d / "a.dll"
        p.write_bytes(make_pe(["KERNEL32.dll", "VCRUNTIME140.dll", "MSVCP140.dll"]))
        self.assertEqual(cv.pe_imports(p), ["KERNEL32.dll", "VCRUNTIME140.dll", "MSVCP140.dll"])
        self.assertEqual(cv.pe_machine(p), cv.MACHINE_AMD64)

    def test_pe32_header_also_parses(self):
        p = self.d / "a.dll"
        p.write_bytes(make_pe(["VCRUNTIME140.dll"], machine=0x14C, pe32plus=False))
        self.assertEqual(cv.pe_imports(p), ["VCRUNTIME140.dll"])

    def test_non_pe_rejected(self):
        p = self.d / "x.dll"
        p.write_bytes(b"not a pe at all" * 20)
        with self.assertRaises(cv.NotPE):
            cv.pe_imports(p)

    def test_vc_name_pattern(self):
        for n in ("vcruntime140.dll", "VCRUNTIME140_1.dll", "msvcp140.dll", "MSVCP140_1.dll", "concrt140.dll", "vcomp140.dll"):
            self.assertTrue(cv.VC_RUNTIME.match(n), n)
        for n in ("kernel32.dll", "python312.dll", "api-ms-win-crt-runtime-l1-1-0.dll", "ucrtbase.dll"):
            self.assertFalse(cv.VC_RUNTIME.match(n), n)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Path(self.tmp.name) / "app"
        self.sys32 = Path(self.tmp.name) / "sys32"
        (self.app / "sub").mkdir(parents=True)
        self.sys32.mkdir()
        (self.app / "jarvis.exe").write_bytes(make_pe(["KERNEL32.dll", "VCRUNTIME140.dll"]))
        (self.app / "sub" / "llama.dll").write_bytes(make_pe(["MSVCP140.dll", "VCRUNTIME140_1.dll"]))
        self.log = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, fix=None):
        return cv.check(self.app, fix, out=self.log.append)

    def test_missing_dlls_fail_without_a_source(self):
        self.assertEqual(self.run_check(), 1)
        text = "\n".join(self.log)
        for n in ("vcruntime140.dll", "msvcp140.dll", "vcruntime140_1.dll"):
            self.assertIn(f"ERROR: {n}", text)

    def test_copies_only_64bit_files_from_source(self):
        for n in ("vcruntime140.dll", "msvcp140.dll", "vcruntime140_1.dll"):
            (self.sys32 / n).write_bytes(make_pe(["KERNEL32.dll"]))
        self.assertEqual(self.run_check(self.sys32), 0, self.log)
        self.assertTrue((self.app / "msvcp140.dll").is_file())
        self.assertEqual(cv.check(self.app, None, out=lambda m: None), 0)   # now satisfied without a source

    def test_32bit_source_is_refused_and_reported(self):
        (self.sys32 / "vcruntime140.dll").write_bytes(make_pe(["KERNEL32.dll"], machine=0x14C, pe32plus=False))
        (self.sys32 / "msvcp140.dll").write_bytes(make_pe(["KERNEL32.dll"]))
        (self.sys32 / "vcruntime140_1.dll").write_bytes(make_pe(["KERNEL32.dll"]))
        self.assertEqual(self.run_check(self.sys32), 1)
        self.assertFalse((self.app / "vcruntime140.dll").exists())
        self.assertIn("not a 64-bit file", "\n".join(self.log))

    def test_dll_missing_from_source_is_not_ignored(self):
        (self.sys32 / "vcruntime140.dll").write_bytes(make_pe(["KERNEL32.dll"]))
        self.assertEqual(self.run_check(self.sys32), 1)
        self.assertIn("msvcp140.dll", "\n".join(self.log))

    def test_32bit_file_inside_app_is_an_error(self):
        (self.app / "old.pyd").write_bytes(make_pe(["VCRUNTIME140.dll"], machine=0x14C, pe32plus=False))
        self.assertEqual(self.run_check(), 1)
        self.assertIn("not 64-bit", "\n".join(self.log))

    def test_no_vc_imports_at_all_is_suspicious_not_ok(self):
        (self.app / "jarvis.exe").write_bytes(make_pe(["KERNEL32.dll"]))
        (self.app / "sub" / "llama.dll").write_bytes(make_pe(["KERNEL32.dll"]))
        self.assertEqual(self.run_check(), 1)

    def test_missing_folder(self):
        self.assertEqual(cv.check(self.app / "nope", None, out=self.log.append), 1)


if __name__ == "__main__":
    unittest.main()
