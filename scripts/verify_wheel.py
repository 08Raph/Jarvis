"""Stage 5: inspect a llama-cpp-python wheel file that a person has ALREADY downloaded, and record exactly what it is. Nothing is installed or imported.

Checks and records (failing closed; a problem is listed, never silently accepted):
  * file name -> distribution, version, Python tag, ABI tag, platform tag; required here: win_amd64 and either the project's generic py3-none tag or a cp312 tag (cp312-cp312, cp312-abi3, cp312-none);
  * SHA-256 computed from the wheel's bytes, and (if --expected-sha256 is given) whether it equals the value a person took from the package index or PyPI;
  * the zip is read, never extracted: unsafe member names (absolute, '..'), unreadable members and an unreasonable unpacked size are problems;
  * <dist>.dist-info/METADATA: Name, Version, License / License-Expression, Requires-Python, Requires-Dist, and the licence files shipped in the wheel;
  * native libraries (.dll/.pyd): name, size, SHA-256; their import tables (which other DLLs they need) read with scripts/check_vc_runtime.py, and the CPU machine type;
    imports of Visual C++ runtime DLLs are listed so the installer can bundle them.
It cannot show that the wheel installs or that a model loads: IMPORT SUCCESS AND MODEL LOAD ARE DIFFERENT, separate steps (see docs/STAGE5_WINDOWS_PLAYBOOK.md part C).

Usage: python scripts/verify_wheel.py --wheel C:\\jv\\wheels\\llama_cpp_python-<ver>-py3-none-win_amd64.whl [--expected-sha256 <hex> --expected-sha256-source "index link"] --out C:\\jv\\report
Exit codes: 0 no problems; 4 problems (see the list).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path, PurePosixPath

HEX64 = re.compile(r"[0-9a-f]{64}")
WHEEL = re.compile(r"^(?P<dist>[A-Za-z0-9_.]+)-(?P<version>[^-]+)(?:-(?P<build>\d[^-]*))?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$")
MAX_UNPACKED = 2_000_000_000
MAX_NATIVE = 600_000_000          # one DLL/PYD larger than this is a problem, and is never read into memory
MAX_METADATA = 5_000_000


class TooBig(Exception):
    pass


def read_capped(z: zipfile.ZipFile, info: zipfile.ZipInfo, cap: int) -> bytes:
    """Read a member but never more than `cap` bytes, whatever its header claims (a zip header can lie about its size)."""
    with z.open(info) as f:
        data = f.read(cap + 1)
    if len(data) > cap:
        raise TooBig
    return data
NATIVE = (".dll", ".pyd")
LICENSE_NAMES = re.compile(r"(^|/)(licen[sc]e|copying|notice)[^/]*$", re.I)


def _vc():
    spec = importlib.util.spec_from_file_location("check_vc_runtime", Path(__file__).with_name("check_vc_runtime.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256_bytes_stream(fobj, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    for b in iter(lambda: fobj.read(chunk), b""):
        h.update(b)
    return h.hexdigest()


def inspect(wheel: Path, expected_sha256: str | None = None, expected_source: str | None = None, want_py: str = "cp312", want_plat: str = "win_amd64") -> dict:
    res = {"kind": "wheel-inspection", "inspected_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "file_name": wheel.name,
           "problems": [], "notes": ["nothing was installed, imported or extracted; a wheel that inspects cleanly may still fail to install or to load a model"]}
    p = res["problems"]
    m = WHEEL.match(wheel.name)
    if not m:
        p.append("FILE_NAME_NOT_A_WHEEL_NAME")
    else:
        res["tags"] = {k: m.group(k) for k in ("dist", "version", "py", "abi", "plat")}
        if m.group("dist").lower().replace("-", "_") != "llama_cpp_python":
            p.append("NOT_LLAMA_CPP_PYTHON")
        # The project's own release wheels are tagged py3-none-<platform> (pyproject.toml: wheel.py-api = "py3"; .github/workflows/build-and-release.yaml), a
        # generic wheel that pip accepts on any Python 3 of that platform. A version-specific wheel (cp312-cp312 / cp312-abi3 / cp312-none) is accepted too.
        generic = m.group("py") in ("py3", "py312") and m.group("abi") == "none"
        if generic:
            res["notes"].append("generic py3-none wheel (the tag the project's own release workflow produces): it carries no Python-version-specific code, so Python 3.12 compatibility rests on the library calling the C library through ctypes, not on this tag")
        else:
            if m.group("py") != want_py:
                p.append(f"PYTHON_TAG_NOT_{want_py.upper()}")
            if m.group("abi") not in (want_py, "abi3", "none"):
                p.append("ABI_TAG_UNEXPECTED")
        if m.group("plat") != want_plat:
            p.append(f"PLATFORM_NOT_{want_plat.upper()}")
    try:
        with open(wheel, "rb") as f:
            res["sha256_computed_from_bytes"] = sha256_bytes_stream(f)
        res["size_bytes"] = wheel.stat().st_size
    except OSError:
        p.append("WHEEL_UNREADABLE")
        res["status"] = "PROBLEMS"
        return res
    if expected_sha256 is not None:
        exp = expected_sha256.strip().lower()
        res["expected_sha256"], res["expected_sha256_source"] = exp, expected_source or "(not stated)"
        if not HEX64.fullmatch(exp):
            p.append("EXPECTED_HASH_MALFORMED")
        elif exp != res["sha256_computed_from_bytes"]:
            p.append("HASH_MISMATCH")
        if not expected_source:
            res["notes"].append("the source of the expected hash was not stated: a hash without a named source is not evidence")
    else:
        res["notes"].append("no expected hash was supplied: the computed hash below is only a record, not a verification")
    try:
        z = zipfile.ZipFile(wheel)
    except (zipfile.BadZipFile, OSError):
        p.append("NOT_A_READABLE_ZIP")
        res["status"] = "PROBLEMS"
        return res
    with z:
        infos = z.infolist()
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            p.append("UNPACKED_SIZE_UNREASONABLE")
        unsafe = [i.filename for i in infos if i.filename.startswith(("/", "\\")) or ".." in PurePosixPath(i.filename).parts or ":" in i.filename.split("/")[0]]
        if unsafe:
            p.append("UNSAFE_MEMBER_NAMES")
        names = [i.filename for i in infos]
        res["member_count"] = len(names)
        if len(set(names)) != len(names):
            p.append("DUPLICATE_MEMBERS")
        meta_files = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(meta_files) != 1:
            p.append("METADATA_MISSING_OR_AMBIGUOUS")
        else:
            try:
                msg = Parser().parsestr(read_capped(z, z.getinfo(meta_files[0]), MAX_METADATA).decode("utf-8", "replace"))
                res["metadata"] = {"name": msg.get("Name"), "version": msg.get("Version"), "license": msg.get("License"), "license_expression": msg.get("License-Expression"),
                                   "requires_python": msg.get("Requires-Python"), "requires_dist": msg.get_all("Requires-Dist") or [],
                                   "classifiers_python": [c for c in (msg.get_all("Classifier") or []) if c.startswith("Programming Language :: Python")]}
                if m and msg.get("Version") != m.group("version"):
                    p.append("METADATA_VERSION_DIFFERS_FROM_FILE_NAME")
                if msg.get("Name") and re.sub(r"[-_.]+", "_", msg.get("Name")).lower() != "llama_cpp_python":
                    p.append("METADATA_NAME_NOT_LLAMA_CPP_PYTHON")
                elif not msg.get("Name"):
                    p.append("METADATA_NAME_MISSING")
            except Exception:       # noqa: BLE001 - truncated, encrypted, oversized or malformed members are a recorded problem, never a traceback
                p.append("METADATA_UNREADABLE")
        lic = [n for n in names if LICENSE_NAMES.search(n)]
        res["licence_files"] = lic
        if not lic:
            p.append("NO_LICENCE_FILE_IN_WHEEL")
        natives = []
        vc = _vc()
        with tempfile.TemporaryDirectory() as td:
            for i in infos:
                if i.filename.lower().endswith(NATIVE) and i.filename not in unsafe:
                    entry = {"name": i.filename, "size_bytes": i.file_size}
                    try:
                        data = read_capped(z, i, MAX_NATIVE)        # the real bytes, capped; the header size is not trusted
                        entry["sha256"] = hashlib.sha256(data).hexdigest()
                        tmp = Path(td) / "probe.bin"
                        tmp.write_bytes(data)       # a throw-away copy, only so the PE reader can open it; deleted with the temp folder
                        entry["machine"] = hex(vc.pe_machine(tmp))
                        entry["imports"] = sorted(vc.pe_imports(tmp), key=str.lower)
                        entry["vc_runtime_imports"] = [d for d in entry["imports"] if vc.VC_RUNTIME.match(d)]
                        if entry["machine"] != hex(vc.MACHINE_AMD64):
                            p.append(f"NOT_AMD64: {i.filename}")
                    except Exception as exc:        # noqa: BLE001 - recorded, not hidden
                        entry["inspect_error"] = "TooBig" if isinstance(exc, TooBig) else type(exc).__name__
                        p.append(f"NATIVE_FILE_NOT_INSPECTABLE: {i.filename}")
                    natives.append(entry)
        res["native_libraries"] = natives
        if not natives:
            p.append("NO_NATIVE_LIBRARIES_FOUND")
        res["vc_runtime_dlls_needed"] = sorted({d.lower() for n in natives for d in n.get("vc_runtime_imports", [])})
    res["status"] = "NO_PROBLEMS_FOUND" if not p else "PROBLEMS"
    return res


def zlib_error():
    import zlib
    return zlib.error


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wheel", required=True, type=Path)
    ap.add_argument("--expected-sha256")
    ap.add_argument("--expected-sha256-source", help="where the expected hash came from (index link fragment, PyPI JSON, ...)")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    res = inspect(a.wheel, a.expected_sha256, a.expected_sha256_source)
    print(json.dumps({k: v for k, v in res.items() if k != "native_libraries"}, indent=2))
    for n in res.get("native_libraries", []):
        print(f"native: {n['name']} {n['size_bytes']} bytes sha256 {n.get('sha256')} imports {n.get('imports')}")
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "wheel_inspection.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    return 0 if res["status"] == "NO_PROBLEMS_FOUND" else 4


if __name__ == "__main__":
    sys.exit(main())
