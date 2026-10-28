""".jarvismodel packages: a zip holding model files plus a manifest of per-file hashes.

A package MUST contain LICENSE.txt: weights are never packaged without stating the terms
under which they may be redistributed.
"""
from __future__ import annotations

import hashlib
import json
import struct
import zipfile
import zlib
from pathlib import Path

FORMAT = 1
ALLOWED_NAMES = frozenset({"model.pt", "tokenizer.json", "metrics.json", "run_config.json",
                           "dataset_report.json", "MODEL_CARD.md", "LICENSE.txt", "NOTICES.txt"})
REQUIRED_NAMES = frozenset({"model.pt", "tokenizer.json", "LICENSE.txt"})


class PackageError(ValueError):
    pass


# What the zip library and the JSON parser raise for damaged, hostile or unsupported archives. They are all reported as PackageError so a bad
# package gives a clean one-line error (and no traceback) instead of a raw library exception.
_ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError, RecursionError, OverflowError,
               struct.error, zlib.error)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pack_model(model_dir: str | Path, out_path: str | Path, license_id: str) -> str:
    """Create a package from a trained model folder. Returns the package SHA-256."""
    d = Path(model_dir)
    names = sorted(n for n in ALLOWED_NAMES if (d / n).is_file())
    missing = REQUIRED_NAMES - set(names)
    if missing:
        raise PackageError(f"model folder is missing required files: {sorted(missing)}")
    manifest = {"format": FORMAT, "license": license_id, "files": {n: sha256_file(d / n) for n in names}}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        for n in names:
            z.write(d / n, n)
    return sha256_file(out_path)


def declared_unpacked_bytes(pkg_path: str | Path, max_bytes: int = 8_000_000_000) -> int:
    """Total unpacked size DECLARED in the package's zip directory (every entry counted, duplicates included), for the up-front free-space check.
    The declared sizes are untrusted - extraction still counts the real bytes against ``max_bytes`` and verifies every file hash - so this is an
    estimate, never a limit. Raises PackageError (fail closed) if the package cannot be read as a zip or declares more than ``max_bytes``."""
    try:
        with zipfile.ZipFile(pkg_path) as z:
            total = sum(int(i.file_size) for i in z.infolist())
    except (*_ZIP_ERRORS, OSError, ValueError) as exc:
        raise PackageError("not a valid package") from exc
    if total > max_bytes:
        raise PackageError("package expands beyond the allowed size")
    return total


def validate_and_extract(pkg_path: str | Path, dest: str | Path, max_bytes: int = 8_000_000_000) -> dict:
    """Safely extract a package into an empty directory. Returns its manifest.

    Rejects path traversal, unexpected names, duplicate entries, size bombs, missing files, hash mismatches, and damaged or unsupported archives
    (all as PackageError; OSError such as a full disk is passed through for the caller to translate).
    """
    try:
        return _validate_and_extract(pkg_path, dest, max_bytes)
    except _ZIP_ERRORS:
        raise PackageError("the package is damaged or uses an unsupported zip feature") from None


def _validate_and_extract(pkg_path: str | Path, dest: str | Path, max_bytes: int) -> dict:
    dest = Path(dest)
    try:
        z = zipfile.ZipFile(pkg_path)
    except zipfile.BadZipFile as exc:
        raise PackageError("not a valid package") from exc
    with z:
        infos = {i.filename: i for i in z.infolist()}
        if len(infos) != len(z.infolist()):
            raise PackageError("package has duplicate entries")
        if "manifest.json" not in infos:
            raise PackageError("package has no manifest.json")
        if infos["manifest.json"].file_size > 1_000_000:
            raise PackageError("manifest too large")
        try:
            manifest = json.loads(z.read("manifest.json"))
        except (ValueError, RecursionError):
            raise PackageError("manifest is not valid JSON") from None
        if not isinstance(manifest, dict):
            raise PackageError("manifest must be a JSON object")
        if manifest.get("format") != FORMAT:
            raise PackageError("unsupported package format")
        files = manifest.get("files", {})
        if not isinstance(files, dict):
            raise PackageError("manifest file list is invalid")
        for name in infos:
            if name != "manifest.json" and name not in ALLOWED_NAMES:
                raise PackageError(f"unexpected file in package: {name!r}")
        if set(files) != {n for n in infos if n != "manifest.json"}:
            raise PackageError("manifest does not match package contents")
        if REQUIRED_NAMES - set(files):
            raise PackageError(f"package is missing required files: {sorted(REQUIRED_NAMES - set(files))}")
        if sum(i.file_size for i in infos.values()) > max_bytes:
            raise PackageError("package expands beyond the allowed size")
        dest.mkdir(parents=True, exist_ok=True)
        for name, expect in files.items():
            target = dest / name  # names are from a fixed allowlist, so no traversal is possible
            written = 0
            with z.open(name) as src, open(target, "wb") as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    written += len(chunk)
                    if written > max_bytes:
                        raise PackageError("package expands beyond the allowed size")
                    out.write(chunk)
            if sha256_file(target) != expect:
                raise PackageError(f"hash mismatch for {name}")
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest
