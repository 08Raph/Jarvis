"""Backups taken before risky file changes, and how to undo them. Deleting moves things into Jarvis' own recycle folder,
so every file change made by Jarvis can be reversed with `jarvis desktop undo ID` (until you empty that folder)."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class CheckpointError(Exception):
    pass


class Checkpoints:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _new(self) -> tuple[str, Path]:
        cid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        d = self.root / cid
        d.mkdir(parents=True, exist_ok=False)
        return cid, d

    def _write(self, d: Path, rec: dict) -> None:
        (d / "checkpoint.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")

    def backup_file(self, path: Path, reason: str) -> str:
        """Copy of a file (or folder) as it is now, before it is overwritten or edited."""
        cid, d = self._new()
        dst = d / "data"
        if path.is_dir():
            shutil.copytree(path, dst, symlinks=True)
        else:
            shutil.copy2(path, dst)
        self._write(d, {"id": cid, "kind": "backup", "original": str(path), "is_dir": path.is_dir(), "reason": reason,
                        "at": time.time()})
        return cid

    def record_created(self, path: Path, reason: str) -> str:
        cid, d = self._new()
        rec = {"id": cid, "kind": "created", "original": str(path), "is_dir": path.is_dir(), "reason": reason, "at": time.time()}
        if path.is_file():
            rec["sha256"] = sha256_file(path)
        self._write(d, rec)
        return cid

    def record_move(self, src: Path, dst: Path, reason: str) -> str:
        cid, d = self._new()
        self._write(d, {"id": cid, "kind": "moved", "original": str(src), "moved_to": str(dst), "reason": reason, "at": time.time()})
        return cid

    def trash(self, path: Path, reason: str) -> str:
        cid, d = self._new()
        shutil.move(str(path), str(d / "data"))
        self._write(d, {"id": cid, "kind": "trashed", "original": str(path), "reason": reason, "at": time.time()})
        return cid

    def get(self, cid: str) -> dict:
        if not cid or "/" in cid or "\\" in cid or ".." in cid:
            raise CheckpointError("bad checkpoint id")
        f = self.root / cid / "checkpoint.json"
        if not f.exists():
            raise CheckpointError(f"no checkpoint {cid}")
        return json.loads(f.read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        out = []
        if self.root.exists():
            for f in sorted(self.root.glob("*/checkpoint.json")):
                try:
                    out.append(json.loads(f.read_text(encoding="utf-8")))
                except ValueError:
                    pass
        return out

    def undo(self, cid: str, validate=None) -> str:
        """Reverse one checkpoint. Refuses rather than overwrite something the user changed since. `validate(path)` (used by the
        tool path) must return the checked real path for every location the undo will touch, or raise; a checkpoint record that
        points anywhere else is therefore never followed."""
        rec = self.get(cid)
        d = self.root / cid
        check = (lambda raw: Path(validate(str(raw)))) if validate else (lambda raw: Path(raw))
        orig = check(rec["original"])
        kind = rec["kind"]
        if rec.get("undone"):
            raise CheckpointError(f"checkpoint {cid} was already undone")
        if kind in ("backup", "trashed"):
            data = d / "data"
            if kind == "trashed" and orig.exists():
                raise CheckpointError(f"{orig} exists again; move it away first")
            if kind == "backup" and orig.exists():
                side = self.backup_file(orig, f"state before undo of {cid}")   # never lose the current version
                if orig.is_dir():
                    shutil.rmtree(orig)
                else:
                    orig.unlink()
                rec["current_version_saved_as"] = side
            orig.parent.mkdir(parents=True, exist_ok=True)
            if kind == "trashed":
                shutil.move(str(data), str(orig))
            elif rec.get("is_dir"):
                shutil.copytree(data, orig, symlinks=True)
            else:
                shutil.copy2(data, orig)
            msg = f"restored {orig}"
        elif kind == "created":
            if not orig.exists():
                msg = f"{orig} is already gone"
            else:
                if orig.is_file() and rec.get("sha256") and sha256_file(orig) != rec["sha256"]:
                    raise CheckpointError(f"{orig} was changed after Jarvis created it; not removing it")
                self.trash(orig, f"undo of {cid}")
                msg = f"moved {orig} to Jarvis' recycle folder"
        elif kind == "moved":
            src, dst = orig, check(rec["moved_to"])
            if src.exists():
                raise CheckpointError(f"{src} exists again; not moving back over it")
            if not dst.exists():
                raise CheckpointError(f"{dst} is no longer there")
            shutil.move(str(dst), str(src))
            msg = f"moved {dst} back to {src}"
        else:
            raise CheckpointError(f"unknown checkpoint kind {kind}")
        rec["undone"] = True
        self._write(d, rec)
        return msg
