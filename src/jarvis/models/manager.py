"""Install, verify, select, upgrade/rollback and remove model packages; guarded download."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from jarvis.models import diskspace
from jarvis.models import gguf as gguf_mod
from jarvis.models.errors import IntegrityError  # noqa: F401 - re-exported: callers import it from here
from jarvis.models.package import PackageError, declared_unpacked_bytes, sha256_file, validate_and_extract
from jarvis.net.policy import NetworkPolicy
from jarvis.security import signing

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"[0-9a-f]{64}")
COPY_CHUNK = 8 * 1024 * 1024        # add-gguf copies in chunks so free space can be rechecked while a multi-GB file is copied


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
        # Free space for the unpacked files (declared sizes: an estimate; real bytes are still capped and hash-checked while unpacking). The package
        # file itself is already on disk. The previous model is renamed to the rollback slot, not copied, so it needs no extra space.
        diskspace.ensure_free(self.dir, declared_unpacked_bytes(pkg_path) + diskspace.META_OVERHEAD, "unpack this model package")
        tmp = self.dir / f".tmp-{uuid.uuid4().hex}"
        try:
            manifest = validate_and_extract(pkg_path, tmp)
            meta = {"id": model_id, "package_sha256": digest, "license": manifest.get("license"),
                    "files": manifest["files"], "verified_origin": expected_sha256 is not None,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                    "size_bytes": sum((tmp / n).stat().st_size for n in manifest["files"])}
            (tmp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            self._swap_in(tmp, model_id)
            return meta
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            full = diskspace.translate_oserror(e, "unpack the model package")
            if full is not None:
                raise full from None
            raise
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _swap_in(self, tmp: Path, model_id: str) -> None:
        """Put the staged folder ``tmp`` in place as the model ``model_id``, keeping the model it replaces as the single rollback copy.
        Order (all renames inside the model folder, i.e. the same volume; no data is copied and no extra space is needed):
          1. an older rollback copy (if any) is moved to a private ``.swap-<uuid>`` name - NOT deleted yet;
          2. the current model is moved to the rollback slot;
          3. the staged folder becomes the model;
          4. only now, with the new model in place, is the older rollback copy (two versions back) deleted.
        If any of 1-3 fails, every rename already done is undone, so the model that worked before still works and the older rollback copy is
        still there. If the undo itself fails, nothing is deleted and the error says how to recover. Individual renames are atomic on POSIX; the
        sequence as a whole is not atomic and a crash between renames can leave the model in the rollback slot (``jarvis models rollback``)."""
        final, prev = self.path(model_id), self._prev(model_id)
        old_prev = None
        moved = False
        try:
            if final.exists():
                if prev.exists():
                    old_prev = self.dir / f".swap-{uuid.uuid4().hex}"
                    os.replace(prev, old_prev)
                os.replace(final, prev)
                moved = True
            os.replace(tmp, final)
        except BaseException:
            try:
                if moved and not final.exists():
                    os.replace(prev, final)
                if old_prev is not None and not prev.exists():
                    os.replace(old_prev, prev)
            except OSError:
                raise IntegrityError("the new model could not be put in place and the previous model could not be restored automatically; "
                                     f"nothing was deleted. Run `jarvis models rollback {model_id}` to bring the previous version back") from None
            raise
        if old_prev is not None:
            shutil.rmtree(old_prev, ignore_errors=True)       # a folder this call created: the version two back, obsolete now that the new model is in place

    def install_gguf(self, gguf_path: str | Path, model_id: str, license_spdx: str, license_text: str,
                     source_url: str = "", expected_sha256: str | None = None, move: bool = False,
                     extra_meta: dict | None = None, expected_size_bytes: int | None = None, expected_architecture: str | None = None) -> dict:
        """Install a single-file GGUF model. Requires license information (never installs weights with unknown terms).
        The file must have a valid GGUF header. Without expected_sha256 the install is recorded as unverified origin.
        expected_size_bytes (optional): the publisher's size of the file. If given, a source larger than 115 % of it (diskspace.size_limit, integer
        maths, exactly the limit is accepted) is refused BEFORE it is hashed or copied. It is an extra check only: it never replaces the checksum,
        free-space, regular-file or rollback protections. Omitted = no size limit is applied and the record says so (size_limit_enforced False)."""
        self._check_id(model_id)
        size_limit = None
        if expected_size_bytes is not None:
            if type(expected_size_bytes) is not int or not 1 <= expected_size_bytes <= diskspace.MAX_EXPECTED_SIZE:
                raise ValueError("expected_size_bytes must be a whole number of bytes, greater than zero")
            size_limit = diskspace.size_limit(expected_size_bytes)
        if not license_spdx.strip() or not license_text.strip():
            raise IntegrityError("a license name and license text/pointer are required to install a model")
        if expected_sha256 is not None:
            expected_sha256 = str(expected_sha256).strip().lower()
            if not _SHA256.fullmatch(expected_sha256):       # reject a malformed checksum up front, before reading a multi-GB file
                raise IntegrityError("the expected SHA-256 must be exactly 64 hexadecimal characters; nothing was installed")
        try:
            st = os.stat(gguf_path)                  # follows a symlink to its target
        except OSError:
            raise IntegrityError("the model file could not be read; nothing was installed") from None
        if not stat.S_ISREG(st.st_mode):             # a directory, pipe or device could hang or read unbounded data
            raise IntegrityError("the model path is not a regular file; nothing was installed")
        if size_limit is not None and st.st_size > size_limit:       # before hashing or copying a possibly huge wrong file
            raise IntegrityError(f"the model file ({st.st_size} bytes) is larger than 115% of the expected size ({expected_size_bytes} bytes; "
                                 f"limit {size_limit} bytes); nothing was installed")
        header = gguf_mod.read_header(gguf_path)          # raises GGUFError for non-GGUF or corrupt files
        if expected_architecture:        # a valid GGUF of another kind of model is refused BEFORE it is hashed or copied (the header is self-declared: this catches a wrong file, not a forged one)
            declared = gguf_mod.summarize(header).get("architecture")
            if declared != expected_architecture:
                raise IntegrityError(f"the file declares architecture {str(declared)[:40]!r}, not {expected_architecture!r}; it is not the model this entry names, nothing was installed")
        digest = sha256_file(gguf_path)
        if expected_sha256 is not None and digest != expected_sha256:
            raise IntegrityError("model file SHA-256 does not match the expected value; not installed")
        if not move:      # add-gguf: a full second copy is staged next to the other models while the source file stays; refuse BEFORE writing if it cannot fit
            diskspace.ensure_free(self.dir, diskspace.need_for_copy(Path(gguf_path).stat().st_size), "copy this model into the model folder")
        tmp = self.dir / f".tmp-{uuid.uuid4().hex}"
        try:
            tmp.mkdir(parents=True)
            dst = tmp / "model.gguf"
            if move:
                os.replace(gguf_path, dst)
            else:
                try:
                    self._copy_checked(gguf_path, dst, st.st_size, size_limit)
                except OSError as e:
                    full = diskspace.translate_oserror(e, "copy the model")
                    if full is not None:
                        raise full from None
                    raise
            (tmp / "LICENSE.txt").write_text(f"License: {license_spdx}\nSource: {source_url}\n\n{license_text}\n", encoding="utf-8")
            files = {n: sha256_file(tmp / n) for n in ("model.gguf", "LICENSE.txt")}
            if files["model.gguf"] != digest:         # the source changed (or was still being written) between checking it and copying it
                raise IntegrityError("the model file changed while it was being installed; nothing was installed")
            meta = {"id": model_id, "kind": "gguf", "package_sha256": digest, "license": license_spdx, "files": files,
                    "sizes": {n: (tmp / n).stat().st_size for n in files}, "source_url": source_url,
                    "verified_origin": expected_sha256 is not None, "gguf": gguf_mod.summarize(header),
                    "expected_size_bytes": expected_size_bytes, "size_limit_enforced": size_limit is not None,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                    "size_bytes": sum((tmp / n).stat().st_size for n in files), **(extra_meta or {})}
            (tmp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            self._swap_in(tmp, model_id)
            return meta
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _copy_checked(self, src, dst, total: int, limit: int | None = None) -> None:
        """Copy ``src`` to ``dst`` in chunks, rechecking free space about every RECHECK_EVERY bytes (DiskSpaceError if the volume is filling up) and,
        when ``limit`` is given, refusing a source that grows past it while being copied. The caller removes ``dst``'s folder on any error."""
        copied, next_check = 0, diskspace.RECHECK_EVERY
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                chunk = fi.read(COPY_CHUNK)
                if not chunk:
                    return
                copied += len(chunk)
                if limit is not None and copied > limit:
                    raise IntegrityError("the model file grew beyond the size limit while it was being copied; nothing was installed")
                fo.write(chunk)
                if copied >= next_check:
                    diskspace.ensure_free(self.dir, max(total - copied, 0), "finish copying this model into the model folder")
                    next_check = copied + diskspace.RECHECK_EVERY

    @staticmethod
    def _tree_bytes(p: Path) -> int:
        """Total size of the regular files under p (symbolic links are never followed)."""
        if p.is_symlink():
            return 0
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())

    def rollback_copies(self) -> list[dict]:
        """Read-only list of the rollback copies (``.prev-<id>``), with their size. Nothing is deleted. A rollback copy is only obsolete once the
        model with the same id is installed AND ``jarvis models verify`` passes for it; see docs/LOCAL_MODEL.md for the manual removal steps."""
        return [{"id": p.name[len(".prev-"):], "size_bytes": self._tree_bytes(p)} for p in sorted(self.dir.glob(".prev-*")) if p.is_dir() and not p.is_symlink()]

    def staging_leftovers(self) -> list[dict]:
        """Read-only list of staging leftovers (``.tmp-*``, ``.part-*``, ``.swap-*``) with their size. These normally disappear by themselves; one that
        stays was left by a crash or power loss. They are NEVER deleted automatically (a ``.swap-*`` or ``.tmp-*`` folder can hold the only copy of a model)."""
        out = []
        for pre in (".tmp-", ".part-", ".swap-"):
            out += [{"name": p.name, "size_bytes": self._tree_bytes(p)} for p in sorted(self.dir.glob(pre + "*")) if not p.is_symlink()]
        return out

    def kind(self, model_id: str) -> str:
        mp = self.path(model_id) / "meta.json"
        return json.loads(mp.read_text(encoding="utf-8")).get("kind", "jarvis") if mp.is_file() else "jarvis"

    def rollback(self, model_id: str) -> bool:
        prev, cur = self._prev(model_id), self.path(model_id)
        if not prev.exists():
            return False
        swap = self.dir / f".swap-{uuid.uuid4().hex}"
        swapped = False
        if cur.exists():
            os.replace(cur, swap)
            swapped = True
        try:
            os.replace(prev, cur)
        except BaseException:
            if swapped and not cur.exists():          # undo: the model that worked before stays in place
                os.replace(swap, cur)
            raise
        if swapped:
            os.replace(swap, prev)
        return True

    # inspect / select / remove ----------------------------------------------------
    def installed(self) -> list[dict]:
        """Every readable model record. A folder whose meta.json is damaged is skipped here (it must not hide the other models);
        installed_report() lists those separately so the user can see and remove them."""
        return self.installed_report()["models"]

    def installed_report(self) -> dict:
        models, damaged = [], []
        for d in sorted(self.dir.iterdir()):
            m = d / "meta.json"
            if d.is_dir() and not d.name.startswith(".") and m.is_file():
                try:
                    meta = json.loads(m.read_text(encoding="utf-8"))
                    if not isinstance(meta, dict) or not isinstance(meta.get("files", {}), dict):
                        raise ValueError("not a model record")
                    models.append(meta)
                except (OSError, ValueError):
                    damaged.append({"id": d.name, "reason": "its meta.json cannot be read"})
        return {"models": models, "damaged": damaged}

    def gguf_file(self, model_id: str) -> Path:
        """The model's GGUF file: inside the model folder, or (for a registered model) the user's own file at its original place."""
        return gguf_mod.model_file(self.path(model_id))

    def meta(self, model_id: str) -> dict:
        try:
            m = json.loads((self.path(model_id) / "meta.json").read_text(encoding="utf-8"))
            return m if isinstance(m, dict) else {}
        except (OSError, ValueError):
            return {}

    def is_linked(self, model_id: str) -> bool:
        return isinstance(self.meta(model_id).get("linked"), dict)

    def register_external(self, gguf_path: str | Path, model_id: str, *, sha256: str | None = None, license_spdx: str = "unknown",
                          source: str = "local file", extra_meta: dict | None = None) -> dict:
        """Register a GGUF file where it already is: NOTHING is copied or moved, only a small record (meta.json) is written. The file is
        checked to be a regular file with a valid GGUF header. The record keeps the path, size and modification time so a later change or
        a moved file is noticed. ``sha256`` is the digest the caller computed from this exact file (None = not computed). The license is
        'unknown' unless the user says otherwise: nothing here claims a license."""
        self._check_id(model_id)
        try:
            st = os.stat(gguf_path)
        except OSError:
            raise IntegrityError("the model file could not be read; nothing was registered") from None
        if not stat.S_ISREG(st.st_mode):
            raise IntegrityError("the model path is not a regular file; nothing was registered")
        header = gguf_mod.read_header(gguf_path)
        if sha256 is not None and not _SHA256.fullmatch(str(sha256).lower()):
            raise IntegrityError("the SHA-256 must be exactly 64 hexadecimal characters")
        absolute = os.path.abspath(str(gguf_path))
        d = self.path(model_id)
        if d.exists():
            raise IntegrityError("a model with this id already exists; remove it first or choose another name")
        tmp = self.dir / f".tmp-{uuid.uuid4().hex}"
        try:
            tmp.mkdir(parents=True)
            meta = {"id": model_id, "kind": "gguf", "files": {}, "sizes": {}, "package_sha256": (sha256 or "").lower() or None,
                    "linked": {"path": absolute, "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns},
                    "license": license_spdx, "source_url": source, "verified_origin": False, "gguf": gguf_mod.summarize(header),
                    "origin": "imported", "size_bytes": st.st_size, "installed_at": datetime.now(timezone.utc).isoformat(),
                    **(extra_meta or {})}
            (tmp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            os.replace(tmp, d)
            return meta
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def verify(self, model_id: str, quick: bool = False) -> tuple[bool, list[str]]:
        """Full check re-hashes every file. quick=True (used before each chat of a multi-GB GGUF) only checks that the
        files exist with the recorded sizes and a valid GGUF signature; `jarvis models verify` does the full hash."""
        d = self.path(model_id)
        mp = d / "meta.json"
        if not mp.is_file():
            return False, ["not installed"]
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                raise ValueError
        except (OSError, ValueError):
            return False, ["meta.json cannot be read"]
        problems = []
        link = meta.get("linked")
        if isinstance(link, dict):          # a registered file lives outside the model folder: check it where it is
            f = gguf_mod.model_file(d)
            if not f.is_file():
                return False, ["the registered model file is missing or was moved"]
            if f.stat().st_size != link.get("size_bytes"):
                problems.append("the registered file's size changed since it was registered")
            else:
                try:
                    with open(f, "rb") as fh:
                        if fh.read(4) != gguf_mod.MAGIC:
                            problems.append("the registered file is no longer a GGUF file")
                except OSError:
                    problems.append("the registered file cannot be read")
                if not quick and not problems and meta.get("package_sha256") and sha256_file(f) != meta["package_sha256"]:
                    problems.append("the registered file's SHA-256 changed since it was registered")
            return (not problems), problems
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

    def remove(self, model_id: str, delete_linked_file: bool = False) -> bool:
        """Remove a model from the library. A copied/downloaded model's files live in its folder and go with it. For a REGISTERED model only the
        record is removed (the user's own file is left alone) unless ``delete_linked_file`` is True; then the file is deleted only if it is still
        a regular, non-symlink .gguf file whose size matches the record (the caller has asked the user to confirm)."""
        d = self.path(model_id)
        if delete_linked_file and self.is_linked(model_id):
            link = self.meta(model_id).get("linked") or {}
            f = Path(str(link.get("path", "")))
            if f.is_file() and not f.is_symlink() and f.suffix.lower() == ".gguf" and f.stat().st_size == link.get("size_bytes"):
                f.unlink()
            elif f.exists():
                raise IntegrityError("the registered file no longer looks like the one that was registered; it was NOT deleted and the model was NOT removed")
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
        tmp = self.dir / "active.json.tmp"
        tmp.write_text(json.dumps({"id": model_id}), encoding="utf-8")
        tmp.replace(self.dir / "active.json")      # atomic: a crash never leaves a half-written selection

    def active(self) -> str | None:
        f = self.dir / "active.json"
        if not f.is_file():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None                      # unreadable/damaged selection file = no brain selected (never a crash)
        mid = data.get("id") if isinstance(data, dict) else None
        return mid if isinstance(mid, str) and mid and not ("/" in mid or "\\" in mid or ".." in mid) and (self.dir / mid / "meta.json").is_file() else None

    def disk_usage_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.dir.rglob("*") if p.is_file())

    def download_gguf(self, model, progress: Callable[[int, int], None] | None = None, trust_unpinned: bool = False,
                      base: str = "https://huggingface.co", hosts: tuple[str, ...] | None = None) -> dict:
        """Fetch a catalog GGUF model from its publisher. Requires 'model_download' enabled (user consent). Integrity:
        the SHA-256 published by the host is checked; if the host gives none, trust_unpinned must be True (user confirmed).
        Size policy (diskspace.size_limit): the download is stopped as soon as MORE than 115 % of the catalog estimate has arrived
        (integer maths, rounded down; exactly the limit is accepted), or more than the size the host itself declared. It is enforced while
        streaming, so a missing, wrong or changing Content-Length cannot bypass it. Disk policy: free space is checked before the first byte is
        written and again about every 256 MiB (diskspace.ensure_free); the partial file is always removed on any failure."""
        from jarvis.models.catalog import HF_HOSTS, resolve_file
        hosts = hosts or HF_HOSTS
        info = resolve_file(self.policy, model, base, hosts)
        declared = info["size_bytes"] or None
        limit = diskspace.size_limit(model.approx_size_bytes, declared)
        if declared and declared > diskspace.size_limit(model.approx_size_bytes):
            raise IntegrityError("the file on the host is more than 115% of the expected size; aborted")
        if info["sha256"] is None and not trust_unpinned:
            raise IntegrityError("the host did not publish a checksum for this file; refusing without explicit confirmation")
        diskspace.ensure_free(self.dir, diskspace.need_for_download(limit), "download this model")      # before anything is written
        part = self.dir / f".part-{uuid.uuid4().hex}"
        try:
            total, next_check = 0, diskspace.RECHECK_EVERY
            with self.policy.open("model_download", info["url"], timeout=60, allowed_host_suffixes=hosts) as resp, open(part, "wb") as out:
                header = None
                try:
                    header = diskspace.parse_content_length(resp.headers.get("Content-Length"))
                except Exception:     # noqa: BLE001 - no usable headers: rely on the streaming limit alone
                    header = None
                if header is not None and header > limit:
                    raise IntegrityError("the server announced a file larger than the allowed size; aborted before downloading")
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    if total + len(chunk) > limit:
                        raise IntegrityError("download exceeded the allowed size (115% of the expected size); aborted and removed")
                    out.write(chunk)
                    total += len(chunk)
                    if total >= next_check:
                        next_check = total + diskspace.RECHECK_EVERY
                        diskspace.ensure_free(self.dir, max(limit - total, 0), "finish downloading this model")
                    if progress:
                        progress(total, info["size_bytes"] or model.approx_size_bytes)
            if info["size_bytes"] and total != info["size_bytes"]:
                raise IntegrityError(f"download incomplete ({total} of {info['size_bytes']} bytes); nothing installed")
            lic = (f"This model is distributed by its publisher under {model.license_spdx}. Full text: {model.license_url}\n"
                   f"Model page: https://huggingface.co/{model.repo}")
            return self.install_gguf(part, model.id, model.license_spdx, lic, info["url"], info["sha256"], move=True,
                                     extra_meta={"catalog_id": model.id, "no_think_suffix": model.no_think_suffix,
                                                 "context_tokens": model.context_tokens,
                                                 # what the integrity check was measured against: the hash was computed here from the bytes, but the reference value came from the
                                                 # same host that served the file, so it shows the file was received intact, not that the host itself is trustworthy
                                                 "checksum_reference": {"source": "huggingface.co API (same host as the file)", "independent_of_host": False,
                                                                        "host_declared_size_bytes": info["size_bytes"] or None},
                                                 "origin": "catalog", "source_repo": model.repo, "source_revision": info.get("revision"),
                                                 "source_file": info.get("name")},
                                     expected_architecture=model.architecture or None)
        except OSError as e:           # "disk full" while creating or writing the partial file: a clear, path-free message (other OSErrors pass through)
            full = diskspace.translate_oserror(e, "download the model")
            if full is not None:
                raise full from None
            raise
        finally:
            part.unlink(missing_ok=True)

    # download (explicit user action; policy must have model_download enabled) --------
    def download(self, entry: dict, progress: Callable[[int, int], None] | None = None) -> dict:
        """entry: {id, url, sha256, size_bytes}. Raises NetworkDisabled unless the user enabled
        'model_download' (e.g. via policy.grant_once after a consent dialog)."""
        self._check_id(entry["id"])
        limit = int(entry["size_bytes"])      # signed-catalog packages: the declared size is exact, so it is already stricter than +15 %
        # the package is downloaded (limit bytes) and then extracted next to it (up to ~limit bytes more): check the whole peak before writing
        diskspace.ensure_free(self.dir, 2 * limit + diskspace.META_OVERHEAD, "download and unpack this model package")
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
