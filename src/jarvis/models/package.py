""".jarvismodel packages: a zip holding model files plus a manifest of per-file hashes.

A package MUST contain LICENSE.txt: weights are never packaged without stating the terms
under which they may be redistributed.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

FORMAT = 1
ALLOWED_NAMES = frozenset({"model.pt", "tokenizer.json", "metrics.json", "run_config.json",
                           "dataset_report.json", "MODEL_CARD.md", "LICENSE.txt", "NOTICES.txt"})
REQUIRED_NAMES = frozenset({"model.pt", "tokenizer.json", "LICENSE.txt"})


class PackageError(ValueError):
    pass


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


def validate_and_extract(pkg_path: str | Path, dest: str | Path, max_bytes: int = 8_000_000_000) -> dict:
    """Safely extract a package into an empty directory. Returns its manifest.

    Rejects path traversal, unexpected names, size bombs, missing files, and hash mismatches.
    """
    dest = Path(dest)
    try:
        z = zipfile.ZipFile(pkg_path)
    except zipfile.BadZipFile as exc:
        raise PackageError("not a valid package") from exc
    with z:
        infos = {i.filename: i for i in z.infolist()}
        if "manifest.json" not in infos:
            raise PackageError("package has no manifest.json")
        if infos["manifest.json"].file_size > 1_000_000:
            raise PackageError("manifest too large")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise PackageError("unsupported package format")
        files = manifest.get("files", {})
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
