"""Install, verify, select, upgrade/rollback and remove model packages; guarded download."""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from jarvis.models import gguf as gguf_mod
from jarvis.models.package import PackageError, sha256_file, validate_and_extract
from jarvis.net.policy import NetworkPolicy
from jarvis.security import signing

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class IntegrityError(RuntimeError):
    pass


def load_catalog(manifest: dict, signature_b64: str, public_key: bytes) -> list[dict]:
    """Return catalog entries only if the manifest signature verifies."""
    if not signing.verify(manifest, signature_b64, public_key):
        raise IntegrityError("catalog signature is invalid; refusing to trust it")
    return list(manifest.get("models", []))


class ModelStore:
    def __init__(self, models_dir: str | Path, policy: NetworkPolicy | None = None) -> None:
        self.dir = Path(models_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy or NetworkPolicy()

    # helpers -----------------------------------------------------------------
    @staticmethod
    def _check_id(model_id: str) -> None:
        if not _ID.match(model_id):
            raise ValueError("model id must be lowercase letters, digits, '.', '_' or '-'")

    def path(self, model_id: str) -> Path:
        self._check_id(model_id)
        return self.dir / model_id

    def _prev(self, model_id: str) -> Path:
        return self.dir / f".prev-{model_id}"

    # install -----------------------------------------------------------------
    def install_package(self, pkg_path: str | Path, model_id: str, expected_sha256: str | None = None,
                        confirm_unlisted: bool = False) -> dict:
        """Install a package. Without a trusted expected hash the caller must pass
        confirm_unlisted=True (the user accepted an unverified origin); inner file hashes are still checked."""
        self._check_id(model_id)
        digest = sha256_file(pkg_path)
        if expected_sha256 is None:
            if not confirm_unlisted:
                raise IntegrityError(f"package SHA-256 is {digest}; no trusted hash was supplied, "
                                     "so the user must explicitly confirm installing it")
        elif digest != expected_sha256.lower():
            raise IntegrityError("package SHA-256 does not match the expected value; not installed")
        tmp = self.dir / f".tmp-{uuid.uuid4().hex}"
        try:
            manifest = validate_and_extract(pkg_path, tmp)
            meta = {"id": model_id, "package_sha256": digest, "license": manifest.get("license"),
                    "files": manifest["files"], "verified_origin": expected_sha256 is not None,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                    "size_bytes": sum((tmp / n).stat().st_size for n in manifest["files"])}
            (tmp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            final = self.path(model_id)
            if final.exists():  # keep the previous version for rollback
                prev = self._prev(model_id)
                if prev.exists():
                    shutil.rmtree(prev)
                os.replace(final, prev)
            os.replace(tmp, final)
            return meta
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def install_gguf(self, gguf_path: str | Path, model_id: str, license_spdx: str, license_text: str,
                     source_url: str = "", expected_sha256: str | None = None, move: bool = False,
                     extra_meta: dict | None = None) -> dict:
        """Install a single-file GGUF model. Requires license information (never installs weights with unknown terms).
        The file must have a valid GGUF header. Without expected_sha256 the install is recorded as unverified origin."""
        self._check_id(model_id)
        if not license_spdx.strip() or not license_text.strip():
            raise IntegrityError("a license name and license text/pointer are required to install a model")
        header = gguf_mod.read_header(gguf_path)          # raises GGUFError for non-GGUF or corrupt files
        digest = sha256_file(gguf_path)
        if expected_sha256 is not None and digest != expected_sha256.lower():
            raise IntegrityError("model file SHA-256 does not match the expected value; not installed")
        tmp = self.dir / f".tmp-{uuid.uuid4().hex}"
        try:
            tmp.mkdir(parents=True)
            dst = tmp / "model.gguf"
            if move:
                os.replace(gguf_path, dst)
            else:
                shutil.copyfile(gguf_path, dst)
            (tmp / "LICENSE.txt").write_text(f"License: {license_spdx}\nSource: {source_url}\n\n{license_text}\n", encoding="utf-8")
            files = {n: sha256_file(tmp / n) for n in ("model.gguf", "LICENSE.txt")}
            meta = {"id": model_id, "kind": "gguf", "package_sha256": digest, "license": license_spdx, "files": files,
                    "sizes": {n: (tmp / n).stat().st_size for n in files}, "source_url": source_url,
                    "verified_origin": expected_sha256 is not None, "gguf": gguf_mod.summarize(header),
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                    "size_bytes": sum((tmp / n).stat().st_size for n in files), **(extra_meta or {})}
            (tmp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            final = self.path(model_id)
            if final.exists():
                prev = self._prev(model_id)
                if prev.exists():
                    shutil.rmtree(prev)
                os.replace(final, prev)
            os.replace(tmp, final)
            return meta
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def kind(self, model_id: str) -> str:
        mp = self.path(model_id) / "meta.json"
        return json.loads(mp.read_text(encoding="utf-8")).get("kind", "jarvis") if mp.is_file() else "jarvis"

    def rollback(self, model_id: str) -> bool:
        prev, cur = self._prev(model_id), self.path(model_id)
        if not prev.exists():
            return False
        swap = self.dir / f".swap-{uuid.uuid4().hex}"
        if cur.exists():
            os.replace(cur, swap)
        os.replace(prev, cur)
        if swap.exists():
            os.replace(swap, prev)
        return True

    # inspect / select / remove ----------------------------------------------------
    def installed(self) -> list[dict]:
        out = []
        for d in sorted(self.dir.iterdir()):
            m = d / "meta.json"
            if d.is_dir() and not d.name.startswith(".") and m.is_file():
                out.append(json.loads(m.read_text(encoding="utf-8")))
        return out

    def verify(self, model_id: str, quick: bool = False) -> tuple[bool, list[str]]:
        """Full check re-hashes every file. quick=True (used before each chat of a multi-GB GGUF) only checks that the
        files exist with the recorded sizes and a valid GGUF signature; `jarvis models verify` does the full hash."""
        d = self.path(model_id)
        mp = d / "meta.json"
        if not mp.is_file():
            return False, ["not installed"]
        meta = json.loads(mp.read_text(encoding="utf-8"))
        problems = []
        for name, expect in meta["files"].items():
            f = d / name
            if not f.is_file():
                problems.append(f"{name}: missing")
            elif quick and name in meta.get("sizes", {}):
                if f.stat().st_size != meta["sizes"][name]:
                    problems.append(f"{name}: size changed")
                elif name.endswith(".gguf"):
                    with open(f, "rb") as fh:
                        if fh.read(4) != gguf_mod.MAGIC:
                            problems.append(f"{name}: not a GGUF file")
            elif sha256_file(f) != expect:
                problems.append(f"{name}: hash mismatch")
        return (not problems), problems

    def remove(self, model_id: str) -> bool:
        d = self.path(model_id)
        existed = d.exists() or self._prev(model_id).exists()
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self._prev(model_id), ignore_errors=True)
        if self.active() == model_id:
            (self.dir / "active.json").unlink(missing_ok=True)
        return existed

    def set_active(self, model_id: str) -> None:
        ok, problems = self.verify(model_id)
        if not ok:
            raise IntegrityError(f"cannot select {model_id}: {problems}")
        (self.dir / "active.json").write_text(json.dumps({"id": model_id}), encoding="utf-8")

    def active(self) -> str | None:
        f = self.dir / "active.json"
        if not f.is_file():
            return None
        mid = json.loads(f.read_text(encoding="utf-8")).get("id")
        return mid if mid and (self.dir / mid / "meta.json").is_file() else None

    def disk_usage_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.dir.rglob("*") if p.is_file())

    def download_gguf(self, model, progress: Callable[[int, int], None] | None = None, trust_unpinned: bool = False,
                      base: str = "https://huggingface.co", hosts: tuple[str, ...] | None = None) -> dict:
        """Fetch a catalog GGUF model from its publisher. Requires 'model_download' enabled (user consent). Integrity:
        the SHA-256 published by the host is checked; if the host gives none, trust_unpinned must be True (user confirmed).
        The download is capped at 1.5x the size shown on the model page."""
        from jarvis.models.catalog import HF_HOSTS, resolve_file
        hosts = hosts or HF_HOSTS
        info = resolve_file(self.policy, model, base, hosts)
        limit = int(model.approx_size_bytes * 1.5)
        if info["size_bytes"] and info["size_bytes"] > limit:
            raise IntegrityError("the file on the host is much larger than expected; aborted")
        if info["sha256"] is None and not trust_unpinned:
            raise IntegrityError("the host did not publish a checksum for this file; refusing without explicit confirmation")
        part = self.dir / f".part-{uuid.uuid4().hex}"
        try:
            total = 0
            with self.policy.open("model_download", info["url"], timeout=60, allowed_host_suffixes=hosts) as resp, open(part, "wb") as out:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    total += len(chunk)
                    if total > limit:
                        raise IntegrityError("download exceeded the allowed size; aborted")
                    out.write(chunk)
                    if progress:
                        progress(total, info["size_bytes"] or model.approx_size_bytes)
            if info["size_bytes"] and total != info["size_bytes"]:
                raise IntegrityError(f"download incomplete ({total} of {info['size_bytes']} bytes); nothing installed")
            lic = (f"This model is distributed by its publisher under {model.license_spdx}. Full text: {model.license_url}\n"
                   f"Model page: https://huggingface.co/{model.repo}")
            return self.install_gguf(part, model.id, model.license_spdx, lic, info["url"], info["sha256"], move=True,
                                     extra_meta={"catalog_id": model.id, "no_think_suffix": model.no_think_suffix,
                                                 "context_tokens": model.context_tokens})
        finally:
            part.unlink(missing_ok=True)

    # download (explicit user action; policy must have model_download enabled) --------
    def download(self, entry: dict, progress: Callable[[int, int], None] | None = None) -> dict:
        """entry: {id, url, sha256, size_bytes}. Raises NetworkDisabled unless the user enabled
        'model_download' (e.g. via policy.grant_once after a consent dialog)."""
        self._check_id(entry["id"])
        limit = int(entry["size_bytes"])
        part = self.dir / f".part-{uuid.uuid4().hex}"
        try:
            with self.policy.open("model_download", entry["url"]) as resp, open(part, "wb") as out:
                total = 0
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    total += len(chunk)
                    if total > limit:
                        raise IntegrityError("download is larger than the size declared in the catalog; aborted")
                    out.write(chunk)
                    if progress:
                        progress(total, limit)
            return self.install_package(part, entry["id"], expected_sha256=entry["sha256"])
        finally:
            part.unlink(missing_ok=True)
