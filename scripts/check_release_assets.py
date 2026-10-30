"""Fail-fast checks for the Windows release build. Nothing here builds anything and nothing is faked: a missing mandatory file, package or version
mismatch is reported as BUILD BLOCKED and the exit code is 1.

  python scripts/check_release_assets.py --source            files named in packaging/manifest.py exist; one version everywhere; installer rules
  python scripts/check_release_assets.py --packages          packages the release needs are importable in THIS Python
  python scripts/check_release_assets.py --dist dist\\ProjectJarvis   the BUILT folder has the required files and none of the forbidden ones
  python scripts/check_release_assets.py --dist dist\\ProjectJarvis --manifest dist\\build_manifest.json   ...and every file still matches the build manifest
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "src"))
import manifest as MF  # noqa: E402


def read_version(root: Path = ROOT) -> str:
    t = (root / "src" / "jarvis" / "version.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', t, re.M)
    if not m:
        raise ValueError("src/jarvis/version.py has no __version__")
    return m.group(1)


def check_source(root: Path = ROOT, generated_ok: tuple = ("build_info.json",)) -> list[str]:
    problems: list[str] = []
    for src, _dest in MF.DATAS:
        if src in generated_ok:
            continue
        if not (root / src).is_file():
            problems.append(f"mandatory file missing: {src}")
    for d in MF.DOCS:
        if not (root / "docs" / d).is_file():
            problems.append(f"documentation shipped with the app is missing: docs/{d}")
    for need in ("packaging/jarvis.spec", "packaging/installer.iss", "packaging/entry_cli.py", "packaging/entry_gui.py", "scripts/build_windows.ps1"):
        if not (root / need).is_file():
            problems.append(f"packaging file missing: {need}")
    # one authoritative version
    try:
        ver = read_version(root)
    except (OSError, ValueError) as e:
        return problems + [f"cannot read the version: {e}"]
    py = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', py, re.M)
    if not m or m.group(1) != ver:
        problems.append(f"pyproject.toml version ({m.group(1) if m else 'none'}) does not match src/jarvis/version.py ({ver})")
    iss = (root / "packaging" / "installer.iss").read_text(encoding="utf-8") if (root / "packaging" / "installer.iss").is_file() else ""
    for stale in re.findall(r"\b\d+\.\d+\.\d+\b", re.sub(r"(?m)^;.*$", "", iss)):
        if stale not in (ver, "0.0.0", "6.3.0", "10.0.17763"):
            problems.append(f"installer.iss hard-codes version-like text {stale}; the version must come from /DAppVersion")
    if '#define AppVersion "0.0.0"' not in iss:
        problems.append("installer.iss must default AppVersion to 0.0.0 and take the real value from /DAppVersion (the build script passes version.py's)")
    # installer rules that protect the user
    low = iss.lower()
    for bad in ("[registry]", "userstartup", "commonstartup", "\\run\"", "runonce", "schtasks", "[service", "sc.exe"):
        if bad in low:
            problems.append(f"installer.iss contains '{bad}': it would create startup persistence or system changes that were not requested")
    if "privilegesrequired=lowest" not in low:
        problems.append("installer.iss must install per-user (PrivilegesRequired=lowest); administrator rights are not needed")
    if "{localappdata}\\projectjarvis" not in low:
        problems.append("installer.iss must keep user data in {localappdata}\\ProjectJarvis, outside the program folder")
    for m2 in re.finditer(r"DelTree\(([^,]+),", iss):
        if "DataDir()" not in m2.group(1):
            problems.append(f"installer.iss deletes something other than the data folder: DelTree({m2.group(1)})")
    spec = (root / "packaging" / "jarvis.spec").read_text(encoding="utf-8") if (root / "packaging" / "jarvis.spec").is_file() else ""
    if "manifest" not in spec:
        problems.append("jarvis.spec does not read packaging/manifest.py, so the asset list could drift")
    return problems


def missing_packages(names=None) -> list[str]:
    out = []
    for n in names or MF.MANDATORY_PACKAGES_FOR_RELEASE:
        try:
            found = importlib.util.find_spec(n) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append(n)
    return out


TEXT_EXT = {".json", ".md", ".txt", ".toml", ".ps1", ".cfg", ".ini", ".xml", ".csv", ".spec", ".iss"}


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def hash_tree(dist: Path) -> dict[str, str]:
    """{relative posix path: sha256} for every file under dist."""
    dist = Path(dist)
    return {p.relative_to(dist).as_posix(): sha256_of(p) for p in sorted(dist.rglob("*")) if p.is_file()}


def scan_machine_paths(dist: Path, tokens: list[str], max_bytes: int = 5_000_000) -> list[str]:
    """Text files (by extension) in the build that contain the build machine's folder or home folder. Binary files are NOT scanned (PyInstaller
    embeds build paths in some of them); this catches the files WE write, which is what we control."""
    dist = Path(dist)
    # Also the forms a path takes inside JSON ("C:\\\\Users\\\\x" - doubled backslashes) and with forward slashes; a Windows path written by json.dump is otherwise missed.
    toks = [t for t in {*tokens, *(t.replace("\\", "/") for t in tokens), *(t.replace("\\", "\\\\") for t in tokens)} if t and len(t) > 3]
    hits = []
    for p in sorted(dist.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
            continue
        try:
            if p.stat().st_size > max_bytes:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(t in text for t in toks):
            hits.append(p.relative_to(dist).as_posix())
    return hits


def verify_build_manifest(manifest_path: Path, dist: Path) -> list[str]:
    """Recompute every hash and compare with build_manifest.json. Reports changed, missing and unlisted files; it proves the folder is the one that
    was described, not that the build is correct."""
    try:
        m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        listed = m["files"]
        if not isinstance(listed, dict) or m.get("schema") != 1:
            raise ValueError("unexpected layout")
    except (OSError, ValueError, KeyError) as e:
        return [f"build manifest unreadable: {type(e).__name__}: {e}"]
    now = hash_tree(Path(dist))
    problems = [f"listed in the manifest but missing: {k}" for k in sorted(set(listed) - set(now))]
    problems += [f"present but not in the manifest: {k}" for k in sorted(set(now) - set(listed))]
    problems += [f"changed since the manifest was written: {k}" for k in sorted(set(now) & set(listed)) if now[k] != listed[k]]
    tree = hashlib.sha256("\n".join(f"{k} {now[k]}" for k in sorted(now)).encode()).hexdigest()
    if not problems and m.get("tree_sha256") != tree:
        problems.append("tree_sha256 in the manifest does not match the files")
    return problems


def _check_bundled_model(dist: Path, require: bool) -> list[str]:
    """The only model file allowed in the build is the pinned one, at the one allowed path. Wrong size or hash, an extra file or a missing one (when required) blocks the build."""
    from jarvis.models import bundled as B
    problems: list[str] = []
    f = dist / MF.BUNDLED_MODEL_RELPATH
    root = dist / B.BUNDLE_DIRNAME
    if root.exists():
        for p in root.rglob("*"):
            if p.is_file() and p.relative_to(dist).as_posix() != MF.BUNDLED_MODEL_RELPATH:
                problems.append(f"unexpected file in {B.BUNDLE_DIRNAME}: {p.relative_to(dist).as_posix()} (only {MF.BUNDLED_MODEL_RELPATH} may ship)")
    if not f.is_file():
        if require:
            problems.append(f"the bundled model is required but missing: {MF.BUNDLED_MODEL_RELPATH}")
        return problems
    size = f.stat().st_size
    if size != B.PIN_BYTES:
        return problems + [f"bundled model is {size} bytes; the pinned size is {B.PIN_BYTES}"]
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != B.PIN_SHA256:
        problems.append(f"bundled model SHA-256 {h.hexdigest()} does not match the pinned {B.PIN_SHA256}")
    return problems


def check_dist(dist: Path, machine_tokens: list[str] | None = None, require_bundled_model: bool = False) -> list[str]:
    dist = Path(dist)
    if not dist.is_dir():
        return [f"built folder not found: {dist}"]
    # PyInstaller 6 puts data files in "_internal"; the executables themselves must be at the top. The installer also copies LICENSE and the notices to the top.
    problems = [f"required file missing from the build: {r}" for r in MF.REQUIRED_IN_DIST
                if not (dist / r).exists() and (r.endswith(".exe") or not (dist / "_internal" / r).exists())]
    for p in dist.rglob("*"):
        rel = p.relative_to(dist)
        if rel.as_posix() == MF.BUNDLED_MODEL_RELPATH:       # the single allowed weights file; validated (size and SHA-256) below
            continue
        for pat in MF.FORBIDDEN_PATTERNS:
            if fnmatch.fnmatch(p.name.lower(), pat.lower()):
                problems.append(f"forbidden file in the build: {rel} (matches {pat})")
                break
    problems += _check_bundled_model(dist, require_bundled_model)
    for name in MF.FORBIDDEN_TOP_LEVEL:                      # project folders that must not be shipped (third-party packages may have their own tests/)
        if (dist / name).exists():
            problems.append(f"forbidden item at the top of the build: {name}")
    for exe in ("jarvis.exe", "jarvis-gui.exe"):
        f = dist / exe
        if f.is_file() and f.stat().st_size < 100_000:
            problems.append(f"{exe} is only {f.stat().st_size} bytes - this is not a real PyInstaller executable")
    for rel in scan_machine_paths(dist, machine_tokens or []):
        problems.append(f"{rel} contains the build machine's folder or home path (personal/machine-specific data must not ship)")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="store_true")
    ap.add_argument("--packages", action="store_true")
    ap.add_argument("--dist")
    ap.add_argument("--require-bundled-model", action="store_true", help="with --dist: the pinned model file must be in the build")
    ap.add_argument("--manifest", help="with --dist: also verify every file against this build_manifest.json")
    a = ap.parse_args(argv)
    if not (a.source or a.packages or a.dist):
        ap.error("choose --source, --packages or --dist")
    problems: list[str] = []
    if a.source:
        problems += check_source()
    if a.packages:
        problems += [f"required Python package not installed: {n} (pip install -e \".[ui,build,desktop]\")" for n in missing_packages()]
    if a.dist:
        problems += check_dist(Path(a.dist), [str(ROOT), str(Path.home())], require_bundled_model=a.require_bundled_model)
        if a.manifest:
            problems += verify_build_manifest(Path(a.manifest), Path(a.dist))
    if problems:
        print("BUILD BLOCKED:")
        for p in problems:
            print("  - " + p)
        return 1
    print("release asset checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
