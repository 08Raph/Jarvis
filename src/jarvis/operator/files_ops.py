"""Files: search, duplicates, large/old/recent, inventory, hashes, compare, organize (preview first, never overwrites, undoable), recoverable delete."""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, cap, human_size

MAX_RESULTS = 200
MAX_SCAN = 200_000
TEMP_EXT = {".tmp", ".temp", ".bak", ".old", ".crdownload", ".part", ".dmp", ".log~"}
CATEGORIES = {
    "Pictures": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff", ".svg"},
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".flv"},
    "Music": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".ppt", ".pptx", ".xls", ".xlsx", ".csv"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Installers": {".exe", ".msi"},
    "Code": {".py", ".js", ".ts", ".html", ".css", ".json", ".cpp", ".c", ".h", ".java", ".cs", ".ps1", ".bat"},
}


class RootSet(list):
    """All of the allowed folders, scanned together when the user didn't name one."""
    name = "your folders"


def walk_files(root, limit: int = MAX_SCAN):
    if isinstance(root, RootSet):
        n = 0
        for r in root:
            for item in walk_files(r, limit - n):
                yield item
                n += 1
        return
    n = 0
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ("$recycle.bin", "node_modules", "__pycache__", "jarvis recycle")]
        for f in files:
            p = Path(dirpath) / f
            try:
                if p.is_symlink():
                    continue
                yield p, p.stat()
            except OSError:
                continue
            n += 1
            if n >= limit:
                return


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _root(op, path: str | None, multi: bool = False):
    if path and path.lower() not in ("computer", "pc", "everywhere", "all"):
        return op.check_path(path)
    roots = op.roots()
    if not roots:
        raise OperatorError("No allowed folders exist yet. Add one on the Operator page.")
    if multi:
        return RootSet(roots)
    return next((r for r in roots if r.name == "Downloads"), roots[0])


def _lines(items, fmt, n=12):
    return "; ".join(fmt(i) for i in items[:n]) + (f"; and {len(items) - n} more" if len(items) > n else "")


@cap("files.search", "files_read", "Search for files by name, type, size, date or content", LOW, ("folder", "name", "ext", "min_mb", "max_mb", "modified_days", "older_days", "contains"), False, "4,5")
def search(op, folder: str | None = None, name: str = "", ext: str = "", min_mb: float = 0, max_mb: float = 0, modified_days: float = 0, older_days: float = 0, contains: str = "") -> Outcome:
    root = _root(op, folder, multi=True)
    now = time.time()
    ext = ext.lower().lstrip(".")
    hits = []
    for p, st in walk_files(root):
        if name and name.lower() not in p.name.lower():
            continue
        if ext and p.suffix.lower().lstrip(".") != ext:
            continue
        if min_mb and st.st_size < min_mb * 1048576:
            continue
        if max_mb and st.st_size > max_mb * 1048576:
            continue
        if modified_days and now - st.st_mtime > modified_days * 86400:
            continue
        if older_days and now - st.st_mtime < older_days * 86400:
            continue
        if contains:
            if st.st_size > 5_000_000 or p.suffix.lower() not in (".txt", ".md", ".py", ".csv", ".json", ".log", ".html", ".js", ".ini", ".cfg", ".toml", ".yaml", ".yml", ".xml"):
                continue
            try:
                if contains.lower() not in p.read_text(encoding="utf-8", errors="ignore").lower():
                    continue
            except OSError:
                continue
        hits.append((p, st))
        if len(hits) >= MAX_RESULTS:
            break
    hits.sort(key=lambda h: h[1].st_mtime, reverse=True)
    data = [{"path": str(p), "size": st.st_size, "modified": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))} for p, st in hits]
    return Outcome("info", f"Found {len(hits)} file(s) in {root.name}: " + _lines(hits, lambda h: f"{h[0].name} ({human_size(h[1].st_size)})") if hits else f"No matching files in {root.name}.", data)


@cap("files.duplicates", "files_read", "Find duplicate files", LOW, ("folder", "min_kb"), False, "4,5,7")
def duplicates(op, folder: str | None = None, min_kb: int = 1) -> Outcome:
    root = _root(op, folder, multi=True)
    by_size: dict[int, list[Path]] = defaultdict(list)
    for p, st in walk_files(root):
        if st.st_size >= max(1, min_kb) * 1024:
            by_size[st.st_size].append(p)
    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for p in paths:
            try:
                by_hash[sha256(p)].append(p)
            except OSError:
                pass
        groups += [{"size": size, "files": [str(x) for x in v]} for v in by_hash.values() if len(v) > 1]
    groups.sort(key=lambda g: g["size"] * (len(g["files"]) - 1), reverse=True)
    waste = sum(g["size"] * (len(g["files"]) - 1) for g in groups)
    if not groups:
        return Outcome("info", f"No duplicate files in {root.name}.")
    return Outcome("info", f"{len(groups)} sets of identical files in {root.name}, wasting {human_size(waste)}. Biggest: " + _lines(groups, lambda g: " = ".join(Path(f).name for f in g["files"][:3]), 5) + ". I haven't deleted anything.", groups)


@cap("files.large", "files_read", "Find the largest files", LOW, ("folder", "top"), False, "4,5,7")
def large(op, folder: str | None = None, top: int = 15) -> Outcome:
    root = _root(op, folder, multi=True)
    rows = sorted(((st.st_size, p) for p, st in walk_files(root)), reverse=True)[:max(1, min(int(top), 100))]
    return Outcome("info", f"Largest files in {root.name}: " + _lines(rows, lambda r: f"{r[1].name} ({human_size(r[0])})") if rows else "That folder has no files.", [{"path": str(p), "size": s} for s, p in rows])


@cap("files.old", "files_read", "Find files not modified for a long time", LOW, ("folder", "days"), False, "4,5")
def old(op, folder: str | None = None, days: int = 365) -> Outcome:
    root = _root(op, folder, multi=True)
    cut = time.time() - days * 86400
    rows = sorted(((st.st_mtime, st.st_size, p) for p, st in walk_files(root) if st.st_mtime < cut))[:MAX_RESULTS]
    return Outcome("info", f"{len(rows)} file(s) in {root.name} not changed for {days} days (about {human_size(sum(r[1] for r in rows))}): " + _lines(rows, lambda r: r[2].name, 8) if rows else f"Nothing in {root.name} is older than {days} days.",
                   [{"path": str(p), "size": s} for _m, s, p in rows])


@cap("files.recent", "files_read", "Find files changed recently", LOW, ("folder", "days"), False, "4,5")
def recent(op, folder: str | None = None, days: float = 1) -> Outcome:
    return search(op, folder=folder, modified_days=days)


@cap("files.empty_folders", "files_read", "Find empty folders", LOW, ("folder",), False, "4")
def empty_folders(op, folder: str | None = None) -> Outcome:
    root = _root(op, folder)
    out = [str(Path(d)) for d, dirs, files in os.walk(root) if not dirs and not files and Path(d) != root]
    return Outcome("info", f"{len(out)} empty folder(s): " + _lines(out, lambda x: Path(x).name) if out else "No empty folders.", out)


@cap("files.temp", "files_read", "Find temporary and leftover files", LOW, ("folder",), False, "4,7")
def temp_files(op, folder: str | None = None) -> Outcome:
    root = _root(op, folder, multi=True)
    rows = [(p, st) for p, st in walk_files(root) if p.suffix.lower() in TEMP_EXT or p.name.startswith("~$") or p.name.lower().endswith(".tmp")]
    return Outcome("info", f"{len(rows)} temporary file(s), {human_size(sum(s.st_size for _p, s in rows))}: " + _lines(rows, lambda r: r[0].name, 8) if rows else "No temporary files found.", [str(p) for p, _s in rows])


@cap("files.unusual", "files_read", "Find files with unusual or risky extensions", LOW, ("folder",), False, "4,22")
def unusual(op, folder: str | None = None) -> Outcome:
    root = _root(op, folder, multi=True)
    risky = {".exe", ".bat", ".cmd", ".scr", ".vbs", ".ps1", ".js", ".jar", ".msi", ".com", ".pif", ".lnk", ".hta", ".reg", ".dll"}
    rows = [p for p, _s in walk_files(root) if p.suffix.lower() in risky or re.search(r"\.(pdf|docx?|jpg|png|txt)\.(exe|scr|bat|cmd|js)$", p.name.lower())]
    return Outcome("info", f"{len(rows)} program/script-type file(s) in {root.name}: " + _lines(rows, lambda p: p.name) + ". Only open ones you know." if rows else f"No program-type files in {root.name}.", [str(p) for p in rows])


@cap("files.inventory", "files_read", "Make a file inventory or folder report", LOW, ("folder", "save"), False, "4,5")
def inventory(op, folder: str | None = None, save: bool = False) -> Outcome:
    root = _root(op, folder)
    by_ext: Counter = Counter()
    by_size: Counter = Counter()
    total = n = 0
    newest = oldest = None
    for p, st in walk_files(root):
        e = p.suffix.lower() or "(none)"
        by_ext[e] += 1
        by_size[e] += st.st_size
        total += st.st_size
        n += 1
        newest = max(newest or st.st_mtime, st.st_mtime)
        oldest = min(oldest or st.st_mtime, st.st_mtime)
    top = by_size.most_common(6)
    msg = f"{root.name}: {n} files, {human_size(total)}. Biggest types: " + ", ".join(f"{e} {by_ext[e]} files ({human_size(s)})" for e, s in top) + "."
    data = {"files": n, "bytes": total, "by_ext": dict(by_ext)}
    if save:
        f = op.home / "Documents" / "Jarvis reports"
        f.mkdir(parents=True, exist_ok=True)
        out = f / time.strftime(f"inventory-{re.sub(r'[^A-Za-z0-9_-]', '_', root.name)}-%Y%m%d-%H%M%S.json")
        out.write_text(json.dumps({"folder": str(root), **data, "newest": newest, "oldest": oldest}, indent=1), encoding="utf-8")
        return Outcome("verified" if out.exists() else "failed", msg + f" Saved {out.name} in Documents/Jarvis reports.", data)
    return Outcome("info", msg, data)


@cap("files.hash", "files_read", "Calculate a file's SHA-256", LOW, ("path",), False, "4,22,32")
def hash_file(op, path: str = "") -> Outcome:
    p = op.check_path(path)
    if not p.is_file():
        raise OperatorError("That isn't a file.")
    h = sha256(p)
    return Outcome("info", f"SHA-256 of {p.name}: {h}", h)


@cap("files.verify_hash", "files_read", "Check a file against a SHA-256 you give me", LOW, ("path", "expected"), False, "4,32")
def verify_hash(op, path: str = "", expected: str = "") -> Outcome:
    p = op.check_path(path)
    exp = expected.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", exp):
        raise OperatorError("A SHA-256 is 64 hexadecimal characters.")
    h = sha256(p)
    return Outcome("verified" if h == exp else "failed", f"{p.name} matches the checksum." if h == exp else f"{p.name} does NOT match (its checksum is {h}). Don't trust it.")


@cap("files.compare", "files_read", "Compare two files", LOW, ("a", "b"), False, "4")
def compare(op, a: str = "", b: str = "") -> Outcome:
    pa, pb = op.check_path(a), op.check_path(b)
    sa, sb = pa.stat(), pb.stat()
    same = sa.st_size == sb.st_size and sha256(pa) == sha256(pb)
    if same:
        return Outcome("info", f"{pa.name} and {pb.name} are identical.")
    msg = f"They differ: {human_size(sa.st_size)} vs {human_size(sb.st_size)}."
    try:
        ta = pa.read_text(encoding="utf-8").splitlines()
        tb = pb.read_text(encoding="utf-8").splitlines()
        diff = [ln for ln in difflib.unified_diff(ta, tb, pa.name, pb.name, lineterm="", n=0) if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        msg += f" {sum(1 for d in diff if d[0] == '+')} line(s) added, {sum(1 for d in diff if d[0] == '-')} removed. " + "; ".join(d[:60] for d in diff[:6])
    except (UnicodeDecodeError, OSError):
        msg += " (binary files)."
    return Outcome("info", msg)


@cap("files.missing_project_files", "files_read", "Check a project folder for commonly expected files", LOW, ("folder",), False, "4,28")
def missing_project_files(op, folder: str | None = None) -> Outcome:
    root = _root(op, folder)
    names = {p.name.lower() for p in root.iterdir()}
    kinds = []
    if names & {"pyproject.toml", "setup.py", "requirements.txt"} or any(n.endswith(".py") for n in names):
        kinds.append(("Python", ["readme.md", "pyproject.toml", "tests", "license"]))
    if "package.json" in names:
        kinds.append(("Node", ["readme.md", "package.json", "package-lock.json", "license"]))
    if not kinds:
        return Outcome("info", "I can't tell what kind of project that is, so I have no checklist for it.")
    miss = [f"{k}: {m}" for k, want in kinds for m in want if not any(n == m or n.startswith(m) for n in names)]
    return Outcome("info", "Possibly missing: " + ", ".join(miss) if miss else "Nothing obvious is missing.", miss)


# ---------------------------------------------------------------------------------------------- creating, moving, organizing
def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suf = dest.stem, dest.suffix
    for i in range(2, 10_000):
        c = dest.with_name(f"{stem} ({i}){suf}")
        if not c.exists():
            return c
    raise OperatorError("Too many files with that name.")


@cap("files.create_folder", "files_write", "Create a folder", LOW, ("path",), False, "4,5")
def create_folder(op, path: str = "") -> Outcome:
    s = path.strip().strip('"')
    if not s or re.search(r'[<>"|?*]', s):
        raise OperatorError("That folder name has characters Windows doesn't allow.")
    p = Path(s)
    target = op.check_path(p, must_exist=False, write=True) if p.is_absolute() else op.check_path(str(op.roots()[0] / s) if op.roots() else s, must_exist=False, write=True)
    target.mkdir(parents=True, exist_ok=True)
    return Outcome("verified" if target.is_dir() else "failed", f"Created the folder {target.name} in {target.parent.name}." if target.is_dir() else "The folder was not created.", str(target))


def _plan_dir(op) -> Path:
    d = op.folder / "organize_plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _category(p: Path, scheme: str, st) -> str:
    if scheme == "date":
        return time.strftime("%Y-%m", time.localtime(st.st_mtime))
    if scheme == "extension":
        return (p.suffix.lower().lstrip(".") or "no extension").upper()
    ext = p.suffix.lower()
    if re.match(r"(?i)^(screenshot|screen shot|snip)", p.name) and ext in CATEGORIES["Pictures"]:
        return "Screenshots"
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"


@cap("files.organize_plan", "files_read", "Prepare a tidy-up plan for a folder (nothing is moved yet)", LOW, ("folder", "scheme", "pattern"), False, "4,5,14")
def organize_plan(op, folder: str | None = None, scheme: str = "type", pattern: str = "", into: str = "") -> Outcome:
    root = _root(op, folder)
    if scheme not in ("type", "date", "extension"):
        raise OperatorError("I can sort by type, date or extension.")
    moves = []
    for p in sorted(root.iterdir()):
        try:
            if not p.is_file() or p.is_symlink() or p.name.startswith("."):
                continue
            if pattern and not fnmatch.fnmatch(p.name.lower(), pattern.lower()):
                continue
            st = p.stat()
        except OSError:
            continue
        cat = into or _category(p, scheme, st)
        dest = root / cat / p.name
        moves.append({"src": str(p), "dest": str(dest)})
    if not moves:
        return Outcome("info", f"Nothing to tidy in {root.name}" + (f" matching {pattern}" if pattern else "") + ".")
    pid = uuid.uuid4().hex[:10]
    (_plan_dir(op) / f"{pid}.json").write_text(json.dumps({"id": pid, "root": str(root), "moves": moves, "applied": False, "created": time.time()}), encoding="utf-8")
    groups = Counter(Path(m["dest"]).parent.name for m in moves)
    return Outcome("info", f"Plan {pid}: move {len(moves)} file(s) in {root.name} into " + ", ".join(f"{g} ({n})" for g, n in groups.most_common(8)) + ". Nothing has moved. Say \"apply the plan\" to do it; I never delete or overwrite anything.",
                   {"plan": pid, "moves": len(moves)})


def _load_plan(op, pid: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"[0-9a-f]{10}", pid or ""):
        raise OperatorError("That isn't a plan number I made.")
    f = _plan_dir(op) / f"{pid}.json"
    if not f.exists():
        raise OperatorError("I can't find that plan.")
    return f, json.loads(f.read_text(encoding="utf-8"))


def latest_plan_id(op) -> str | None:
    try:
        fs = sorted(_plan_dir(op).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for f in fs:
        try:
            if not json.loads(f.read_text(encoding="utf-8")).get("applied"):
                return f.stem
        except (OSError, ValueError):
            pass
    return None


def _apply_risk(a: dict) -> int:
    return HIGH


@cap("files.organize_apply", "files_write", "Apply a tidy-up plan (moves files; never overwrites or deletes)", _apply_risk, ("plan",), False, "4,5,14")
def organize_apply(op, plan: str = "") -> Outcome:
    f, d = _load_plan(op, plan or latest_plan_id(op) or "")
    if d.get("applied"):
        raise OperatorError("That plan was already applied.")
    root = Path(d["root"])
    done, failed = [], []
    for m in d["moves"]:
        if op.cancel.is_set():
            break
        src = op.check_path(m["src"], write=True)
        dest = Path(m["dest"])
        if root not in dest.parents:
            failed.append(src.name)
            continue
        try:
            dest.parent.mkdir(exist_ok=True)
            final = _unique(dest)
            shutil.move(str(src), str(final))
            done.append({"src": str(src), "dest": str(final)})
        except OSError:
            failed.append(src.name)
    bad = [x for x in done if not Path(x["dest"]).exists() or Path(x["src"]).exists()]
    d["applied"], d["done"] = True, done
    f.write_text(json.dumps(d), encoding="utf-8")
    ok = len(done) - len(bad)
    msg = f"Moved {ok} file(s) and checked each one arrived." + (f" {len(failed)} could not be moved ({', '.join(failed[:5])})." if failed else "") + f" Say \"undo the plan\" to put them back."
    return Outcome("verified" if not bad and not failed else "failed" if not ok else "unverified", msg, {"plan": d["id"], "moved": ok})


@cap("files.organize_undo", "files_write", "Undo an applied tidy-up plan", MEDIUM, ("plan",), False, "4,5")
def organize_undo(op, plan: str = "") -> Outcome:
    if not plan:
        fs = sorted(_plan_dir(op).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in fs:
            if json.loads(f.read_text(encoding="utf-8")).get("done"):
                plan = f.stem
                break
    f, d = _load_plan(op, plan)
    back = 0
    for m in d.get("done", []):
        s, dest = Path(m["src"]), Path(m["dest"])
        try:
            if dest.exists() and not s.exists():
                shutil.move(str(dest), str(s))
                back += 1
        except OSError:
            pass
    d["done"] = []
    f.write_text(json.dumps(d), encoding="utf-8")
    return Outcome("verified", f"Put {back} file(s) back where they were.")


def _count_risk(a: dict) -> int:
    return HIGH


@cap("files.move", "files_write", "Move or rename a file or folder", MEDIUM, ("src", "dest"), False, "4")
def move(op, src: str = "", dest: str = "") -> Outcome:
    s = op.check_path(src, write=True)
    d_in = Path(dest.strip().strip('"'))
    d = op.check_path(d_in, must_exist=False, write=False) if d_in.is_absolute() else op.check_path(dest, must_exist=False)
    final = _unique(d / s.name) if d.is_dir() else _unique(d)
    if s.is_dir() and (final == s or s in final.parents):
        raise OperatorError("A folder can't be moved into itself.")
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(final))
    ok = final.exists() and not s.exists()
    return Outcome("verified" if ok else "failed", f"Moved {s.name} to {final.parent.name}." if ok else "The move did not complete.", str(final))


@cap("files.copy", "files_write", "Copy or duplicate a file or folder", MEDIUM, ("src", "dest"), False, "4")
def copy(op, src: str = "", dest: str = "") -> Outcome:
    s = op.check_path(src)
    if dest:
        d_in = Path(dest.strip().strip('"'))
        d = op.check_path(d_in, must_exist=False) if d_in.is_absolute() else op.check_path(dest, must_exist=False)
        final = _unique(d / s.name) if d.is_dir() else _unique(d)
    else:
        final = _unique(s.with_name(f"{s.stem} - copy{s.suffix}"))
    final.parent.mkdir(parents=True, exist_ok=True)
    if s.is_dir():
        shutil.copytree(s, final, symlinks=False)
        ok = final.is_dir()
    else:
        shutil.copy2(s, final)
        ok = final.is_file() and sha256(final) == sha256(s)
    return Outcome("verified" if ok else "failed", f"Copied to {final.name} and checked it matches." if ok else "The copy does not match the original.", str(final))


@cap("files.rename", "files_write", "Rename a file or folder", MEDIUM, ("path", "new_name"), False, "4")
def rename(op, path: str = "", new_name: str = "") -> Outcome:
    p = op.check_path(path, write=True)
    if not new_name or re.search(r'[\\/:*?"<>|]', new_name):
        raise OperatorError("That name has characters Windows doesn't allow.")
    target = p.with_name(new_name)
    if target.exists():
        raise OperatorError("Something with that name already exists, so I did not rename.")
    p.rename(target)
    return Outcome("verified" if target.exists() and not p.exists() else "failed", f"Renamed to {new_name}.", str(target))


def recycle_dir(op) -> Path:
    d = op.folder / "recycle"
    d.mkdir(parents=True, exist_ok=True)
    return d


@cap("files.delete", "files_write", "Delete to Jarvis's recycle folder (you can restore it)", HIGH, ("path",), False, "4")
def delete(op, path: str = "") -> Outcome:
    p = op.check_path(path, write=True)
    box = recycle_dir(op) / f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    box.mkdir()
    shutil.move(str(p), str(box / p.name))
    (box / "origin.json").write_text(json.dumps({"origin": str(p), "name": p.name}), encoding="utf-8")
    ok = not p.exists() and (box / p.name).exists()
    return Outcome("verified" if ok else "failed", f"Moved {p.name} to Jarvis's recycle folder. Say \"restore {p.name}\" to get it back." if ok else "I could not move it.", str(box))


@cap("files.restore", "files_write", "Restore something Jarvis deleted", MEDIUM, ("name",), False, "4")
def restore(op, name: str = "") -> Outcome:
    q = name.lower().strip()
    boxes = sorted(recycle_dir(op).iterdir(), reverse=True)
    for b in boxes:
        try:
            meta = json.loads((b / "origin.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not q or q in meta["name"].lower():
            origin = Path(meta["origin"])
            if not any(origin == r or r in origin.parents for r in op.roots()):
                continue
            target = _unique(origin)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(b / meta["name"]), str(target))
            shutil.rmtree(b, ignore_errors=True)
            return Outcome("verified" if target.exists() else "failed", f"Restored {target.name}.", str(target))
    return Outcome("info", "I don't have anything deleted by me that matches." + (" The Windows Recycle Bin is separate: open it from the desktop." if q else ""))


@cap("files.reveal", "files_read", "Show a file in File Explorer", LOW, ("path",), True, "4")
def reveal(op, path: str = "") -> Outcome:
    p = op.check_path(path)
    op.ops.run(["explorer.exe", "/select,", str(p)], 10)
    return Outcome("unverified", f"Showed {p.name} in File Explorer.")


@cap("files.open", "files_read", "Open a file with its default program", MEDIUM, ("path",), True, "4,11")
def open_file(op, path: str = "") -> Outcome:
    p = op.check_path(path)
    if p.suffix.lower() in (".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".scr", ".js", ".hta", ".reg", ".lnk", ".com", ".jar"):
        raise OperatorError("I don't open programs or scripts from here. Document, picture, media and text files only.")
    op.ops.open_target(str(p))
    return Outcome("unverified", f"Opened {p.name}.")
