"""ZIP and other archives, with the usual safety rules: no path traversal, no links, no overwriting, size limits."""
from __future__ import annotations

import tarfile
import time
import zipfile
from pathlib import Path

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, Unavailable, cap, human_size
from jarvis.operator.files_ops import _unique, sha256

MAX_EXTRACT_BYTES = 8 * 1024 ** 3
MAX_MEMBERS = 100_000
MAX_RATIO = 200
DANGEROUS = {".exe", ".bat", ".cmd", ".scr", ".vbs", ".ps1", ".msi", ".js", ".hta", ".lnk", ".com"}


def safe_member(name: str, dest: Path) -> Path:
    """The destination for an archive member, or OperatorError if it tries to escape the folder, use a drive, device or stream name."""
    n = name.replace("\\", "/")
    if n.startswith("/") or ":" in n.split("/")[0] or "\x00" in n:
        raise OperatorError(f"The archive contains an unsafe path ({name[:60]}), so I did not extract it.")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or any(":" in p for p in parts):
        raise OperatorError(f"The archive tries to write outside the folder ({name[:60]}), so I did not extract it.")
    target = (dest / Path(*parts)).resolve() if parts else dest
    if dest.resolve() not in target.parents and target != dest.resolve():
        raise OperatorError("The archive tries to write outside the folder, so I did not extract it.")
    return target


def _zip_is_link(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


@cap("zip.create", "archives", "Create a ZIP archive", MEDIUM, ("sources", "dest"), False, "6,8")
def zip_create(op, sources=None, dest: str = "") -> Outcome:
    src_list = [sources] if isinstance(sources, str) else list(sources or [])
    if not src_list:
        raise OperatorError("Tell me what to zip.")
    srcs = [op.check_path(s) for s in src_list]
    first = srcs[0]
    d_in = Path(dest.strip().strip('"')) if dest else first.with_name(first.stem + ".zip")
    target = _unique(op.check_path(d_in, must_exist=False, write=False) if d_in.is_absolute() else first.with_name(first.stem + ".zip"))
    n = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for s in srcs:
            if s.is_dir():
                for p in sorted(s.rglob("*")):
                    if p.is_file() and not p.is_symlink() and p != target:
                        z.write(p, (Path(s.name) / p.relative_to(s)).as_posix())
                        n += 1
            else:
                z.write(s, s.name)
                n += 1
    with zipfile.ZipFile(target) as z:
        bad = z.testzip()
        count = len(z.namelist())
    ok = bad is None and count >= n
    return Outcome("verified" if ok else "failed", f"Created {target.name} with {count} file(s), {human_size(target.stat().st_size)}, and tested it." if ok else "The archive failed its own test.", str(target))


@cap("zip.list", "archives", "List what is inside an archive without extracting", LOW, ("archive",), False, "6")
def zip_list(op, archive: str = "") -> Outcome:
    p = op.check_path(archive)
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            infos = z.infolist()
            names = [(i.filename, i.file_size) for i in infos]
    elif tarfile.is_tarfile(p):
        with tarfile.open(p) as t:
            names = [(m.name, m.size) for m in t.getmembers()]
    else:
        raise Unavailable("I read ZIP and TAR archives. RAR and 7z need an extra tool that isn't installed with Jarvis.")
    total = sum(s for _n, s in names)
    return Outcome("info", f"{p.name} holds {len(names)} item(s), {human_size(total)} unpacked: " + "; ".join(n for n, _s in names[:12]) + ("; ..." if len(names) > 12 else ""), names)


@cap("zip.verify", "archives", "Check an archive for damage", LOW, ("archive",), False, "6")
def zip_verify(op, archive: str = "") -> Outcome:
    p = op.check_path(archive)
    if not zipfile.is_zipfile(p):
        return Outcome("failed", f"{p.name} is not a readable ZIP archive (damaged, or another format).")
    with zipfile.ZipFile(p) as z:
        bad = z.testzip()
    return Outcome("verified" if bad is None else "failed", f"{p.name} passed its integrity test." if bad is None else f"{p.name} is damaged: {bad} failed its checksum.")


@cap("zip.extract", "archives", "Extract an archive safely into a new folder", MEDIUM, ("archive", "dest", "members"), False, "6")
def zip_extract(op, archive: str = "", dest: str = "", members=None) -> Outcome:
    p = op.check_path(archive)
    d_in = Path(dest.strip().strip('"')) if dest else p.with_name(p.stem)
    out = _unique(op.check_path(d_in, must_exist=False, write=True)) if (d_in.is_absolute() or not dest) else _unique(op.check_path(dest, must_exist=False, write=True))
    wanted = set(members) if members else None
    written = 0
    total = 0
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            infos = [i for i in z.infolist() if not wanted or i.filename in wanted]
            if len(infos) > MAX_MEMBERS:
                raise OperatorError("That archive has too many files.")
            for i in infos:
                safe_member(i.filename, out)            # raises before anything is written
                if _zip_is_link(i):
                    raise OperatorError("The archive contains a symbolic link, which I never extract.")
                total += i.file_size
                if i.compress_size and i.file_size / max(1, i.compress_size) > MAX_RATIO and i.file_size > 50_000_000:
                    raise OperatorError("That archive expands suspiciously (a possible zip bomb), so I stopped.")
            if total > MAX_EXTRACT_BYTES:
                raise OperatorError(f"It would unpack to {human_size(total)}, more than I allow.")
            out.mkdir(parents=True)
            for i in infos:
                t = safe_member(i.filename, out)
                if i.is_dir():
                    t.mkdir(parents=True, exist_ok=True)
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as src, open(t, "xb") as dst:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            break
                        dst.write(b)
                written += 1
    elif tarfile.is_tarfile(p):
        with tarfile.open(p) as t:
            ms = [m for m in t.getmembers() if not wanted or m.name in wanted]
            for m in ms:
                safe_member(m.name, out)
                if m.issym() or m.islnk() or m.isdev():
                    raise OperatorError("The archive contains links or device files, which I never extract.")
            out.mkdir(parents=True)
            for m in ms:
                if m.isfile():
                    tgt = safe_member(m.name, out)
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    with t.extractfile(m) as src, open(tgt, "xb") as dst:
                        dst.write(src.read())
                    written += 1
                elif m.isdir():
                    safe_member(m.name, out).mkdir(parents=True, exist_ok=True)
    else:
        raise Unavailable("I extract ZIP and TAR archives. RAR and 7z need an extra tool that isn't installed with Jarvis.")
    risky = [q.name for q in out.rglob("*") if q.suffix.lower() in DANGEROUS][:5]
    msg = f"Extracted {written} file(s) into {out.name}. Nothing was overwritten." + (f" It contains programs/scripts ({', '.join(risky)}); I did not run them." if risky else "")
    return Outcome("verified" if out.exists() else "failed", msg, str(out))


@cap("zip.compare", "archives", "Compare the contents of two archives", LOW, ("a", "b"), False, "6")
def zip_compare(op, a: str = "", b: str = "") -> Outcome:
    def crcs(path):
        with zipfile.ZipFile(path) as z:
            return {i.filename: (i.file_size, i.CRC) for i in z.infolist() if not i.is_dir()}
    pa, pb = op.check_path(a), op.check_path(b)
    ca, cb = crcs(pa), crcs(pb)
    only_a, only_b = sorted(set(ca) - set(cb)), sorted(set(cb) - set(ca))
    changed = sorted(n for n in set(ca) & set(cb) if ca[n] != cb[n])
    if not (only_a or only_b or changed):
        return Outcome("info", f"{pa.name} and {pb.name} hold the same files.")
    return Outcome("info", f"Only in {pa.name}: {len(only_a)}; only in {pb.name}: {len(only_b)}; changed: {len(changed)}. " + "; ".join((only_a + only_b + changed)[:8]), {"a": only_a, "b": only_b, "changed": changed})


@cap("zip.split", "archives", "Split a file into parts", MEDIUM, ("path", "part_mb"), False, "6")
def zip_split(op, path: str = "", part_mb: int = 100) -> Outcome:
    p = op.check_path(path)
    size = max(1, int(part_mb)) * 1048576
    if not p.is_file():
        raise OperatorError("Pick a file to split.")
    parts = []
    with open(p, "rb") as f:
        i = 1
        while True:
            chunk = f.read(size)
            if not chunk:
                break
            q = p.with_name(f"{p.name}.{i:03d}")
            if q.exists():
                raise OperatorError("Parts with that name already exist.")
            q.write_bytes(chunk)
            parts.append(q)
            i += 1
    ok = sum(q.stat().st_size for q in parts) == p.stat().st_size
    return Outcome("verified" if ok else "failed", f"Split {p.name} into {len(parts)} part(s) (join them by copying them back together in order)." if ok else "The parts don't add up.", [str(q) for q in parts])
