"""Backups: dated copies with a checksum manifest, incremental option, verify, restore into a NEW folder, compare, retention, reports."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, cap, human_size
from jarvis.operator.files_ops import sha256, walk_files


def backups_root(op, dest: str | None = None) -> Path:
    if dest:
        return op.check_path(dest, must_exist=False, write=False) if Path(dest).is_absolute() else op.check_path(dest, must_exist=False)
    d = op.home / "Documents" / "Jarvis backups"
    return d


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:40] or "backup"


def sets_for(root: Path, label: str | None = None) -> list[Path]:
    if not root.is_dir():
        return []
    out = [p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists() and (not label or p.name.startswith(_slug(label) + "-"))]
    return sorted(out, key=lambda p: p.name)


def _manifest(p: Path) -> dict:
    return json.loads((p / "manifest.json").read_text(encoding="utf-8"))


@cap("backup.create", "backup", "Back up a folder or files (dated, checked)", MEDIUM, ("source", "dest", "incremental", "keep"), False, "8")
def create(op, source: str = "", dest: str | None = None, incremental: bool = False, keep: int = 0) -> Outcome:
    src = op.check_path(source)
    root = backups_root(op, dest)
    root.mkdir(parents=True, exist_ok=True)
    label = _slug(src.name)
    files = [(p, st) for p, st in walk_files(src)] if src.is_dir() else [(src, src.stat())]
    base = src if src.is_dir() else src.parent
    total = sum(st.st_size for _p, st in files)
    free = shutil.disk_usage(root).free
    if free < total * 1.1 + 50_000_000:
        raise OperatorError(f"There isn't enough space on that drive: the backup needs about {human_size(total)} and {human_size(free)} is free.")
    prev = sets_for(root, label)
    prev_files = _manifest(prev[-1])["files"] if (incremental and prev) else {}
    name = f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    out = root / name
    out.mkdir()
    entries: dict[str, dict] = {}
    copied = reused = 0
    for p, st in files:
        rel = p.relative_to(base).as_posix()
        h = sha256(p)
        old = prev_files.get(rel)
        if old and old["sha"] == h:
            entries[rel] = {"sha": h, "size": st.st_size, "in": old["in"]}
            reused += 1
            continue
        t = out / "data" / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, t)
        entries[rel] = {"sha": h, "size": st.st_size, "in": name}
        copied += 1
    (out / "manifest.json").write_text(json.dumps({"name": name, "source": str(src), "created": time.time(), "incremental": bool(prev_files), "files": entries}), encoding="utf-8")
    v = verify(op, backup=str(out), _quiet=True)
    msg = f"Backed up {len(entries)} file(s) ({copied} copied" + (f", {reused} unchanged and linked to the earlier backup" if reused else "") + f") to {name}."
    if keep and keep > 0:
        msg += " " + _retain(root, label, int(keep))
    return Outcome("verified" if v.ok else "failed", msg + (" Verified every file." if v.ok else " But verification FAILED: " + v.message), str(out))


def _retain(root: Path, label: str, keep: int) -> str:
    sets = sets_for(root, label)
    removed = 0
    needed = set()
    for p in sets[-keep:]:
        needed |= {e["in"] for e in _manifest(p)["files"].values()}
    for p in sets[:-keep]:
        if p.name not in needed:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    return f"Kept the {keep} newest; removed {removed} older one(s)." if removed else f"Kept the {keep} newest (older ones that newer backups still depend on stay)."


@cap("backup.list", "backup", "List backups", LOW, ("dest",), False, "8")
def list_backups(op, dest: str | None = None) -> Outcome:
    root = backups_root(op, dest)
    rows = []
    for p in sets_for(root):
        m = _manifest(p)
        rows.append({"name": p.name, "files": len(m["files"]), "source": m.get("source"), "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("created", 0)))})
    return Outcome("info", f"{len(rows)} backup(s): " + "; ".join(f"{r['name']} ({r['files']} files, {r['when']})" for r in rows[-10:]) if rows else "There are no backups yet.", rows)


@cap("backup.verify", "backup", "Verify a backup's integrity", LOW, ("backup", "dest"), False, "8")
def verify(op, backup: str = "", dest: str | None = None, _quiet: bool = False) -> Outcome:
    root = backups_root(op, dest)
    p = Path(backup) if backup and Path(backup).is_absolute() else None
    if p is None:
        sets = sets_for(root)
        p = (root / backup) if backup else (sets[-1] if sets else None)
    if p is None or not (p / "manifest.json").exists():
        raise OperatorError("I can't find that backup.")
    m = _manifest(p)
    bad, missing = [], []
    for rel, e in m["files"].items():
        f = p.parent / e["in"] / "data" / rel
        if not f.exists():
            missing.append(rel)
        elif sha256(f) != e["sha"]:
            bad.append(rel)
    if bad or missing:
        return Outcome("failed", f"{p.name}: {len(bad)} damaged and {len(missing)} missing file(s): " + ", ".join((bad + missing)[:6]))
    return Outcome("verified", f"{p.name}: all {len(m['files'])} files are present and match their checksums.")


@cap("backup.restore", "backup", "Restore files from a backup into a new folder", MEDIUM, ("backup", "dest", "only"), False, "8")
def restore(op, backup: str = "", dest: str | None = None, only: str = "", backups: str | None = None) -> Outcome:
    root = backups_root(op, backups)
    sets = sets_for(root)
    p = (root / backup) if backup and not Path(backup).is_absolute() else (Path(backup) if backup else (sets[-1] if sets else None))
    if p is None or not (p / "manifest.json").exists():
        raise OperatorError("I can't find that backup.")
    m = _manifest(p)
    out_root = op.check_path(dest, must_exist=False, write=True) if dest else (op.roots()[0] / f"Restored from {p.name}")
    n = 1
    out = out_root
    while out.exists() and any(out.iterdir()):
        n += 1
        out = out_root.with_name(f"{out_root.name} ({n})")
    out.mkdir(parents=True, exist_ok=True)
    restored = 0
    for rel, e in m["files"].items():
        if only and only.lower() not in rel.lower():
            continue
        s = p.parent / e["in"] / "data" / rel
        t = out / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, t)
        if sha256(t) != e["sha"]:
            return Outcome("failed", f"{rel} did not match its checksum after restoring.")
        restored += 1
    return Outcome("verified" if restored else "info", f"Restored {restored} file(s) into {out.name}. Your originals were not touched." if restored else "Nothing in that backup matched.", str(out))


@cap("backup.compare", "backup", "Compare two backups, or a backup with the folder it came from", LOW, ("a", "b"), False, "8")
def compare(op, a: str = "", b: str = "", dest: str | None = None) -> Outcome:
    root = backups_root(op, dest)
    sets = sets_for(root)
    pa = (root / a) if a else (sets[-2] if len(sets) > 1 else None)
    pb = (root / b) if b else (sets[-1] if sets else None)
    if not pa or not pb or not (pa / "manifest.json").exists() or not (pb / "manifest.json").exists():
        raise OperatorError("I need two backups to compare.")
    fa, fb = _manifest(pa)["files"], _manifest(pb)["files"]
    added, removed = sorted(set(fb) - set(fa)), sorted(set(fa) - set(fb))
    changed = sorted(k for k in set(fa) & set(fb) if fa[k]["sha"] != fb[k]["sha"])
    return Outcome("info", f"{pb.name} vs {pa.name}: {len(added)} new, {len(removed)} gone, {len(changed)} changed." + (" " + "; ".join((added + changed)[:6]) if added or changed else ""), {"new": added, "gone": removed, "changed": changed})


@cap("backup.missing", "backup", "Find files in the folder that the latest backup doesn't have", LOW, ("source", "dest"), False, "8")
def missing(op, source: str = "", dest: str | None = None) -> Outcome:
    src = op.check_path(source)
    sets = sets_for(backups_root(op, dest), _slug(src.name))
    if not sets:
        return Outcome("info", "There is no backup of that folder yet.")
    have = _manifest(sets[-1])["files"]
    base = src if src.is_dir() else src.parent
    gone = [p.relative_to(base).as_posix() for p, _st in walk_files(src) if p.relative_to(base).as_posix() not in have or sha256(p) != have[p.relative_to(base).as_posix()]["sha"]] if src.is_dir() else []
    return Outcome("info", f"{len(gone)} file(s) are new or changed since {sets[-1].name}: " + "; ".join(gone[:8]) if gone else f"The latest backup ({sets[-1].name}) is up to date.", gone)


@cap("backup.report", "backup", "Write a backup report", LOW, ("dest",), False, "8")
def report(op, dest: str | None = None) -> Outcome:
    root = backups_root(op, dest)
    lines = [f"Backup report {time.strftime('%Y-%m-%d %H:%M')}", f"Location: {root}", ""]
    for p in sets_for(root):
        m = _manifest(p)
        v = verify(op, backup=str(p), _quiet=True)
        lines.append(f"{p.name}: {len(m['files'])} files, {'OK' if v.ok else 'PROBLEM - ' + v.message}")
    f = op.home / "Documents" / "Jarvis reports"
    f.mkdir(parents=True, exist_ok=True)
    out = f / time.strftime("backup-report-%Y%m%d-%H%M%S.txt")
    out.write_text("\n".join(lines), encoding="utf-8")
    return Outcome("verified" if out.exists() else "failed", f"Saved {out.name} in Documents/Jarvis reports ({len(lines) - 3} backup set(s) checked).", str(out))
