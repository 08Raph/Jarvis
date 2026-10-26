"""Check that every Microsoft Visual C++ runtime DLL needed by the frozen app is INSIDE the app folder.

Why: python.org Python, PyInstaller and llama.cpp are built with Visual C++. A clean Windows PC may not have the
Visual C++ Redistributable installed, and then the app would not start. This script reads the import tables of every
.exe/.dll/.pyd in the app folder (pure Python, no extra packages), lists the VC++ runtime DLLs they import, and
- reports the ones that are missing from the folder;
- with --fix-from <folder> copies each missing one from that folder (use the build PC's native 64-bit System32) but ONLY if
  it is a 64-bit (AMD64) file; it never copies a 32-bit DLL and never ignores a DLL that stays missing (exit code 1).
It does not look at the Windows UCRT (api-ms-win-crt-*): that ships with Windows 10 1809+ and Windows 11.

Usage: python scripts/check_vc_runtime.py dist\\ProjectJarvis [--fix-from C:\\Windows\\System32]
Redistribution terms are Microsoft's: see docs/WINDOWS_DISTRIBUTION.md (verify them before publishing an installer).
"""
from __future__ import annotations

import argparse
import re
import shutil
import struct
import sys
from pathlib import Path

VC_RUNTIME = re.compile(r"^(vcruntime\d+(_\d+)?|msvcp\d+(_\d+)?|concrt\d+|vcomp\d+|vccorlib\d+)\.dll$", re.I)
MACHINE_AMD64 = 0x8664
PE_SUFFIXES = {".exe", ".dll", ".pyd"}


class NotPE(ValueError):
    pass


def _header(data: bytes):
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise NotPE("no MZ header")
    (pe_off,) = struct.unpack_from("<I", data, 0x3C)
    if pe_off + 24 > len(data) or data[pe_off:pe_off + 4] != b"PE\0\0":
        raise NotPE("no PE signature")
    machine, nsec, _, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", data, pe_off + 4)
    opt = pe_off + 24
    (magic,) = struct.unpack_from("<H", data, opt)
    if magic not in (0x10B, 0x20B):
        raise NotPE("unknown optional header")
    return machine, nsec, opt, opt_size, magic


def pe_machine(path: Path) -> int:
    return _header(Path(path).read_bytes()[:4096])[0]


def pe_imports(path: Path) -> list[str]:
    """Names of the DLLs a PE file imports (normal import table only)."""
    data = Path(path).read_bytes()
    machine, nsec, opt, opt_size, magic = _header(data)
    dd = opt + (112 if magic == 0x20B else 96)          # start of the data directories
    n_dirs = struct.unpack_from("<I", data, dd - 4)[0]
    if n_dirs < 2:
        return []
    imp_rva, imp_size = struct.unpack_from("<II", data, dd + 8)
    if imp_rva == 0:
        return []
    sec0 = opt + opt_size
    sections = []
    for i in range(nsec):
        _, vsize, va, rawsize, rawptr = struct.unpack_from("<8sIIII", data, sec0 + 40 * i)
        sections.append((va, max(vsize, rawsize), rawptr))

    def to_off(rva: int) -> int:
        for va, size, raw in sections:
            if va <= rva < va + size:
                return raw + (rva - va)
        raise NotPE("RVA outside sections")

    names = []
    pos = to_off(imp_rva)
    for _ in range(4096):
        if pos + 20 > len(data):
            break
        oft, _, _, name_rva, ft = struct.unpack_from("<IIIII", data, pos)
        if name_rva == 0 and ft == 0 and oft == 0:
            break
        s = to_off(name_rva)
        e = data.index(b"\0", s)
        names.append(data[s:e].decode("ascii", "replace"))
        pos += 20
    return names


def scan(app_dir: Path) -> dict:
    """Return {'required': {dll: [importers]}, 'present': {dll: path}, 'wrong_arch': [paths], 'unreadable': [paths]}."""
    app_dir = Path(app_dir)
    required: dict[str, list[str]] = {}
    present: dict[str, Path] = {}
    wrong_arch, unreadable = [], []
    for p in sorted(app_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in PE_SUFFIXES:
            continue
        rel = p.relative_to(app_dir).as_posix()
        try:
            machine = pe_machine(p)
            imports = pe_imports(p)
        except (NotPE, struct.error, ValueError, OSError):
            unreadable.append(rel)
            continue
        if machine != MACHINE_AMD64:
            wrong_arch.append(rel)
        if VC_RUNTIME.match(p.name):
            present[p.name.lower()] = p
        for name in imports:
            if VC_RUNTIME.match(name):
                required.setdefault(name.lower(), []).append(rel)
    return {"required": required, "present": present, "wrong_arch": wrong_arch, "unreadable": unreadable}


def check(app_dir: Path, fix_from: Path | None = None, out=print) -> int:
    app_dir = Path(app_dir)
    if not app_dir.is_dir():
        out(f"ERROR: {app_dir} is not a folder")
        return 1
    r = scan(app_dir)
    rc = 0
    if r["wrong_arch"]:
        out("ERROR: these files are not 64-bit (AMD64) and cannot run in a 64-bit app: " + ", ".join(r["wrong_arch"]))
        rc = 1
    missing = sorted(d for d in r["required"] if d not in r["present"])
    for dll in sorted(r["required"]):
        if dll in r["present"]:
            out(f"VC++ runtime bundled: {dll}")
    for dll in missing:
        users = ", ".join(r["required"][dll][:3])
        src = Path(fix_from) / dll if fix_from else None
        if src is not None and src.is_file():
            try:
                if pe_machine(src) != MACHINE_AMD64:
                    out(f"ERROR: {src} is not a 64-bit file; NOT copied. {dll} is still missing (needed by {users}).")
                    rc = 1
                    continue
            except (NotPE, OSError, struct.error):
                out(f"ERROR: {src} is not a readable Windows DLL; NOT copied. {dll} is still missing.")
                rc = 1
                continue
            shutil.copy2(src, app_dir / dll)
            out(f"VC++ runtime copied into the app folder from {src.parent}: {dll} (needed by {users})")
        else:
            out(f"ERROR: {dll} is needed by {users} but is not in the app folder"
                + (f" or in {fix_from}." if fix_from else ".")
                + " Install the free 'Microsoft Visual C++ Redistributable (x64)' on the build PC and rebuild.")
            rc = 1
    if not r["required"]:
        out("WARNING: no Visual C++ runtime imports were found at all. That is unexpected for a Python/llama.cpp app; "
            "the scan may have failed. Do not assume the build is portable.")
        rc = 1
    if r["unreadable"]:
        out(f"note: {len(r['unreadable'])} .exe/.dll/.pyd files could not be parsed and were not checked")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("app_dir")
    ap.add_argument("--fix-from", help="folder to copy missing 64-bit VC++ DLLs from (e.g. the 64-bit System32)")
    a = ap.parse_args(argv)
    return check(Path(a.app_dir), Path(a.fix_from) if a.fix_from else None)


if __name__ == "__main__":
    sys.exit(main())
