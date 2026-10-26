"""File tools. Every path goes through SafePaths (allowed folders only, no system folders, no program/script files).
Every change is checked afterwards by reading the disk again; the result says what was actually found."""
from __future__ import annotations

import fnmatch
import os
import shutil
import tempfile
import time
from pathlib import Path

from jarvis.desktop.checkpoints import sha256_file
from jarvis.desktop import paths as _paths
from jarvis.desktop.paths import PathRejected
from jarvis.desktop.schema import Outcome

MAX_READ = 1_000_000
MAX_WRITE = 5_000_000
MAX_COPY_BYTES = 2_000_000_000
MAX_COPY_FILES = 20_000


def _decode(raw: bytes) -> tuple[str, str]:
    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("this looks like a binary file, not text")
    for enc in ("utf-8-sig", "utf-16") if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")) else ("utf-8",):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="strict"), "cp1252"


def _tree_size(p: Path) -> tuple[int, int]:
    total = files = 0
    for root, dirs, fs in os.walk(p):
        for f in fs:
            files += 1
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            if files > MAX_COPY_FILES or total > MAX_COPY_BYTES:
                return total, files
    return total, files


def _describe(p: Path) -> dict:
    if not p.exists():
        return {"exists": False}
    st = p.stat()
    d = {"exists": True, "is_dir": p.is_dir(), "size": st.st_size, "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}
    if p.is_file() and st.st_size <= 200_000_000:
        d["sha256"] = sha256_file(p)
    return d


def _atomic_write(env, p: Path, data: bytes) -> None:
    """Write via a NEW, uniquely named temp file created exclusively in the same folder, re-check the target, then replace.
    (The p15 code used the fixed name '<file>.jarvis-tmp', so a link planted at that name redirected the write.)"""
    fd, tmp_name = tempfile.mkstemp(prefix=".jarvis-", suffix=".tmp", dir=str(p.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if p.exists():
            try:
                shutil.copymode(p, tmp)
            except OSError:
                pass
        env.paths.revalidate(p)                  # still exactly the approved path, still no link, still not Jarvis' own data
        os.replace(tmp, p)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _walkable(env, d: Path) -> bool:
    """Search never descends into Jarvis' own data, links or junctions."""
    try:
        return not env.paths.is_protected(d) and _paths._link_problem(d) is None
    except PathRejected:
        return False


# ------------------------------------------------------------------ observe
def file_search(a, env) -> Outcome:
    root = env.paths.resolve(a["folder"], must_exist=True, allow_ancestor=True)
    if not root.is_dir():
        raise PathRejected(f"{root} is not a folder")
    pattern = a["pattern"] or "*"
    contains = (a["name_contains"] or "").lower()
    hits, scanned = [], 0
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        env.stop.check()
        if len(Path(dirpath).parts) - base_depth >= a["max_depth"]:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and _walkable(env, Path(dirpath) / d)]
        for name in dirnames + filenames:
            scanned += 1
            if fnmatch.fnmatch(name.lower(), pattern.lower()) and contains in name.lower():
                hits.append(str(Path(dirpath) / name))
                if len(hits) >= a["max_results"]:
                    break
        if len(hits) >= a["max_results"] or scanned > 200_000:
            break
    text = "\n".join(hits)
    return Outcome(True, f"{len(hits)} match(es) under {root} ({scanned} names checked)", verified=True,
                   evidence="listed from disk", data={"matches": hits}, untrusted_text=text)


def file_info(a, env) -> Outcome:
    p = env.paths.resolve(a["path"], allow_ancestor=True)
    d = _describe(p)
    return Outcome(True, f"{p}: {'exists' if d['exists'] else 'does not exist'}", verified=True, evidence="checked on disk",
                   data={"path": str(p), **d})


def read_file(a, env) -> Outcome:
    p = env.paths.resolve(a["path"], must_exist=True)
    if not p.is_file():
        raise PathRejected(f"{p} is not a file")
    size = p.stat().st_size
    if size > MAX_READ:
        raise ValueError(f"{p} is {size} bytes; Jarvis reads text files up to {MAX_READ} bytes")
    text, enc = _decode(p.read_bytes())
    shown = text[: a["max_chars"]]
    return Outcome(True, f"read {len(text)} characters from {p} ({enc})" + (" (truncated)" if len(shown) < len(text) else ""),
                   verified=True, evidence="read from disk", data={"path": str(p), "chars": len(text), "encoding": enc,
                                                                  "sha256": sha256_file(p)}, untrusted_text=shown)


# ------------------------------------------------------------------ change
def create_folder(a, env) -> Outcome:
    p = env.paths.resolve(a["path"], for_write=True)
    if p.exists():
        ok = p.is_dir()
        return Outcome(ok, f"{p} already exists" + ("" if ok else " and is a file"), verified=ok, evidence="checked on disk",
                       data={"path": str(p), "created": False})
    p.mkdir(parents=True)
    cid = env.checkpoints.record_created(p, "create_folder")
    ok = p.is_dir()
    return Outcome(ok, f"created folder {p}" if ok else f"folder {p} not found after creating it", verified=ok,
                   evidence="is_dir() after mkdir", data={"path": str(p), "created": True}, checkpoint=cid)


def write_risk(a, env) -> str:
    from jarvis.desktop import schema as S
    try:
        p = env.paths.resolve(a.get("path", ""))
    except PathRejected:
        return S.MODIFY
    return S.DESTRUCTIVE if p.exists() else S.MODIFY


def write_file(a, env) -> Outcome:
    p = env.paths.resolve(a["path"], for_write=True)
    data = a["content"].encode("utf-8")
    if len(data) > MAX_WRITE:
        raise ValueError(f"content is too large ({len(data)} bytes; max {MAX_WRITE})")
    cid = None
    if p.exists():
        if p.is_dir():
            raise ValueError(f"{p} is a folder")
        if not a["overwrite"]:
            raise FileExistsError(f"{p} already exists; set overwrite=true to replace it (a backup is made first)")
        cid = env.checkpoints.backup_file(p, "before write_file overwrite")
    if not p.parent.exists():
        p.parent.mkdir(parents=True)
    _atomic_write(env, p, data)
    if cid is None:
        cid = env.checkpoints.record_created(p, "write_file")
    back = p.read_bytes()                           # VERIFY: read the file back from disk
    ok = back == data
    return Outcome(ok, (f"wrote {len(data)} bytes to {p}" if ok else f"{p} does not contain what was written"),
                   verified=ok, evidence=f"read back {len(back)} bytes, sha256 {'matches' if ok else 'differs'}",
                   data={"path": str(p), "bytes": len(data), "sha256": sha256_file(p), "replaced_existing": a["overwrite"] and cid is not None},
                   checkpoint=cid)


def edit_file(a, env) -> Outcome:
    p = env.paths.resolve(a["path"], must_exist=True, for_write=True)
    if not p.is_file():
        raise PathRejected(f"{p} is not a file")
    raw = p.read_bytes()
    if len(raw) > MAX_WRITE:
        raise ValueError("file too large to edit")
    text, enc = _decode(raw)
    old, new = a["old_text"], a["new_text"]
    if not old:
        raise ValueError("old_text must not be empty")
    n = text.count(old)
    if n != a["occurrences"]:
        raise ValueError(f"expected old_text {a['occurrences']} time(s) but found it {n} time(s); nothing was changed")
    cid = env.checkpoints.backup_file(p, "before edit_file")
    updated = text.replace(old, new)
    out = updated.encode("utf-8-sig" if enc == "utf-8-sig" else enc)
    _atomic_write(env, p, out)
    check, _ = _decode(p.read_bytes())              # VERIFY
    ok = check == updated and (old in new or check.count(old) < n or old == new)
    return Outcome(ok, f"replaced {n} occurrence(s) in {p}" if ok else f"{p} does not show the edit after saving",
                   verified=ok, evidence="re-read file and compared with the intended text", data={"path": str(p), "replaced": n},
                   checkpoint=cid)


def copy_risk(a, env) -> str:
    from jarvis.desktop import schema as S
    try:
        return S.DESTRUCTIVE if env.paths.resolve(a.get("destination", "")).exists() else S.MODIFY
    except PathRejected:
        return S.MODIFY


def copy_path(a, env) -> Outcome:
    src = env.paths.resolve(a["source"], must_exist=True)
    dst = env.paths.resolve(a["destination"], for_write=src.is_file())
    if dst.is_dir() and src.is_file():
        dst = env.paths.resolve(str(dst / src.name), for_write=True)
    cid = None
    if dst.exists():
        if not a["overwrite"]:
            raise FileExistsError(f"{dst} already exists; set overwrite=true to replace it (a backup is made first)")
        cid = env.checkpoints.backup_file(dst, "before copy_path overwrite")
    if src.is_dir():
        size, count = _tree_size(src)
        if count > MAX_COPY_FILES or size > MAX_COPY_BYTES:
            raise ValueError("folder too large for Jarvis to copy (limit 20,000 files / 2 GB)")
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=True)
        ok = dst.is_dir() and _tree_size(dst)[1] == count
        ev = f"destination has {_tree_size(dst)[1]} of {count} files"
    else:
        shutil.copy2(src, dst)
        ok = dst.is_file() and sha256_file(dst) == sha256_file(src)
        ev = "sha256 of copy matches source" if ok else "copy differs from source"
    if cid is None:
        cid = env.checkpoints.record_created(dst, "copy_path")
    return Outcome(ok, f"copied {src} to {dst}" if ok else f"copy of {src} to {dst} could not be confirmed", verified=ok,
                   evidence=ev, data={"source": str(src), "destination": str(dst)}, checkpoint=cid)


def move_path(a, env) -> Outcome:
    src = env.paths.resolve(a["source"], must_exist=True, for_write=src_is_file(a, env))
    dst = env.paths.resolve(a["destination"], for_write=src.is_file())
    if dst.is_dir():
        dst = env.paths.resolve(str(dst / src.name), for_write=src.is_file())
    if dst.exists():
        raise FileExistsError(f"{dst} already exists; Jarvis does not move over existing items")
    before = sha256_file(src) if src.is_file() else None
    shutil.move(str(src), str(dst))
    cid = env.checkpoints.record_move(src, dst, "move_path")
    ok = dst.exists() and not src.exists() and (before is None or sha256_file(dst) == before)
    return Outcome(ok, f"moved {src} to {dst}" if ok else "move could not be confirmed", verified=ok,
                   evidence="source gone, destination present" + (", content hash unchanged" if before else ""),
                   data={"source": str(src), "destination": str(dst)}, checkpoint=cid)


def src_is_file(a, env) -> bool:
    try:
        return env.paths.resolve(a["source"]).is_file()
    except PathRejected:
        return False


def rename_path(a, env) -> Outcome:
    new = a["new_name"]
    if any(c in new for c in '\\/:*?"<>|') or new.strip() in ("", ".", ".."):
        raise PathRejected("the new name must be a plain file or folder name")
    src = env.paths.resolve(a["path"], must_exist=True)
    return move_path({"source": str(src), "destination": str(src.with_name(new))}, env)


def delete_path(a, env) -> Outcome:
    p = env.paths.resolve(a["path"], must_exist=True)
    if any(_same(p, r) for r in env.paths.roots):
        raise PathRejected("Jarvis does not delete a whole allowed folder")
    if p.is_dir():
        size, count = _tree_size(p)
        if count > MAX_COPY_FILES or size > MAX_COPY_BYTES:
            raise ValueError("folder too large to move to Jarvis' recycle folder")
    cid = env.checkpoints.trash(p, "delete_path")
    ok = not p.exists()
    return Outcome(ok, f"moved {p} to Jarvis' recycle folder (undo: jarvis desktop undo {cid})" if ok else f"{p} still exists",
                   verified=ok, evidence="path no longer exists; a recoverable copy is kept", data={"path": str(p)}, checkpoint=cid)


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def undo(a, env) -> Outcome:
    rec = env.checkpoints.get(a["checkpoint"])
    orig = env.paths.resolve(rec["original"])        # only checkpoints inside the allowed folders can be undone by a tool
    # every path the undo will touch (the original and, for a move, where the item went) is checked the same way
    msg = env.checkpoints.undo(a["checkpoint"], validate=lambda raw: env.paths.resolve(str(raw)))
    expect_present = rec["kind"] in ("backup", "trashed", "moved")
    ok = orig.exists() == expect_present
    return Outcome(ok, msg, verified=ok, evidence=f"{orig} {'exists' if orig.exists() else 'is absent'} after undo",
                   data={"path": str(orig)})
