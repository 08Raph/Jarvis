"""Preflight for the Windows build: reports what this machine can and cannot build, BEFORE any long step starts.

  python scripts/check_build_env.py            human-readable table, exit 1 if anything FAILs
  python scripts/check_build_env.py --json     the same as JSON

Every line carries a state, so a real measurement is never confused with a guess or a check that did not run:
  ok       measured here and fine            warn   measured here, a problem is likely
  fail     measured here, the build cannot work   not_run  NOT checked (the reason is given); treat as unknown
Standard library only. Changes nothing except a short-lived probe file inside the build folder. Never touches the network.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_PY, MAX_PY = (3, 11), (3, 13)       # the range the build script and the docs promise; 3.12 has the most prebuilt wheels
MIN_FREE_GB_FAIL, MIN_FREE_GB_WARN = 3.0, 8.0
MAX_PATH_WARN = 80                      # PyInstaller + Qt create deep trees; Windows still limits many tools to 260 characters
STATES = ("ok", "warn", "fail", "not_run")


def _r(name: str, state: str, detail: str, how: str = "measured") -> dict:
    assert state in STATES
    return {"check": name, "state": state, "detail": detail, "how": how}


def run_checks(root: Path = ROOT, *, version_info=None, is_64bit: bool | None = None, platform_name: str | None = None,
               free_bytes: int | None = None, which=shutil.which, environ=None) -> list[dict]:
    """All inputs can be replaced so tests can simulate other machines (such tests are labelled simulated)."""
    root = Path(root)
    env = os.environ if environ is None else environ
    vi = tuple(version_info) if version_info is not None else tuple(sys.version_info[:3])
    plat = platform_name if platform_name is not None else sys.platform
    out: list[dict] = []

    ok_py = MIN_PY <= vi[:2] <= MAX_PY
    out.append(_r("python version", "ok" if ok_py else "fail",
                  f"{vi[0]}.{vi[1]}.{vi[2] if len(vi) > 2 else 0}" + ("" if ok_py else f"; the build supports {MIN_PY[0]}.{MIN_PY[1]} to {MAX_PY[0]}.{MAX_PY[1]} "
                  "(3.12 recommended: the local-AI runtime wheels are most likely to exist for it)")))
    b64 = (sys.maxsize > 2**32) if is_64bit is None else is_64bit
    out.append(_r("python is 64-bit", "ok" if b64 else "fail", "64-bit" if b64 else "32-bit Python cannot run the local model or Qt; install the 64-bit python.org build"))
    if plat == "win32":
        out.append(_r("operating system", "ok", "Windows"))
    else:
        out.append(_r("operating system", "warn", f"{plat}: the .exe and installer can only be built on Windows; source checks can still run here"))

    for need in ("pyproject.toml", "packaging/jarvis.spec", "packaging/manifest.py", "packaging/installer.iss", "scripts/build_windows.ps1", "src/jarvis/version.py"):
        if not (root / need).is_file():
            out.append(_r("project files", "fail", f"missing {need}: run this from an intact copy of the project"))
            break
    else:
        out.append(_r("project files", "ok", "build inputs present"))

    p = str(root)
    out.append(_r("path length", "ok" if len(p) <= MAX_PATH_WARN else "warn",
                  f"{len(p)} characters" + ("" if len(p) <= MAX_PATH_WARN else f"; over {MAX_PATH_WARN}: move the project to something short like C:\\dev\\jarvis")))
    out.append(_r("path characters", "ok" if p.isascii() else "warn", "plain ASCII" if p.isascii() else "non-ASCII characters in the path can break native build tools; use a plain folder"))
    synced = [w for w in ("onedrive", "dropbox", "google drive", "icloud") if w in p.lower()]
    out.append(_r("path is not a synced folder", "warn" if synced else "ok",
                  ("inside a folder name containing " + ", ".join(synced) + "; sync clients lock files during a build. Use a local folder") if synced
                  else "no sync-client folder name in the path (a guess from the name only)", "inferred"))

    try:
        free = free_bytes if free_bytes is not None else shutil.disk_usage(root).free
        gb = free / 1e9
        st = "fail" if gb < MIN_FREE_GB_FAIL else ("warn" if gb < MIN_FREE_GB_WARN else "ok")
        out.append(_r("free disk space", st, f"{gb:.1f} GB free" + ("" if st == "ok" else f"; a build with PySide6 (and torch) needs roughly {MIN_FREE_GB_WARN:.0f} GB")))
    except OSError as e:
        out.append(_r("free disk space", "not_run", f"could not read: {type(e).__name__}"))

    probe = root / f".build_probe_{os.getpid()}"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        out.append(_r("project folder is writable", "ok", "wrote and removed a probe file"))
    except OSError as e:
        out.append(_r("project folder is writable", "fail", f"cannot write in {root} ({e.strerror or e}); the build creates build\\ and dist\\ here"))

    for mod, why in (("venv", "creates the build environment"), ("ensurepip", "installs pip into it")):
        import importlib.util
        has = importlib.util.find_spec(mod) is not None
        out.append(_r(f"python module '{mod}'", "ok" if has else "fail", "available" if has else f"missing ({why}); reinstall Python with the standard options"))

    out.append(_r("git", "ok" if which("git") else "warn", "found" if which("git") else "not on PATH: build_info.json will record the commit as 'unknown' (the build still works)"))
    out.append(_r("internet for pip", "not_run", "not tested here; the build downloads packages from PyPI (and the CPU local-AI wheel index) and fails with pip's own message if offline", "not checked"))
    sde = env.get("SOURCE_DATE_EPOCH", "")
    out.append(_r("SOURCE_DATE_EPOCH", "ok", f"set ({sde}): build time will be this value" if sde.isdigit() else "not set: build time is the wall clock", "measured"))
    return out


def render(results: list[dict]) -> str:
    w = max(len(r["check"]) for r in results)
    lines = [f"{r['state'].upper():8} {r['check']:<{w}}  {r['detail']}" + ("" if r["how"] == "measured" else f"  [{r['how']}]") for r in results]
    bad = [r for r in results if r["state"] == "fail"]
    lines.append("")
    lines.append(f"BUILD BLOCKED: {len(bad)} check(s) failed." if bad else "No blocking problem found. (A pass here does not mean the build will succeed; it only rules out common causes.)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    res = run_checks(Path(a.root))
    print(json.dumps(res, indent=2) if a.json else render(res))
    return 1 if any(r["state"] == "fail" for r in res) else 0


if __name__ == "__main__":
    sys.exit(main())
