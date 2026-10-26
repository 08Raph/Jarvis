"""Coding tools. They plug into the SAME validated registry and permission-controlled executor as the desktop tools, so every call is
schema-checked, permission-checked, pinned, confirmed (when it changes or runs anything), audited, timed out and cancellable.

  inspect (offline, read-only)  : repo_overview  read_lines  search_code  analyze_error  git_info  diff_snapshot  verify_fix
  change (you approve each)     : edit_code  write_code  undo_code_edit  snapshot_project
  run (you approve each)        : run_tests  run_code     -> ONLY inside the sandbox; refused when no genuine sandbox exists

Files are read and written only inside PROJECT folders (a list separate from the ordinary allowed folders; `jarvis desktop
allow-project`). Secrets files (.env, keys) are never read. Nothing here runs a shell or a model-written command line: the only
programs started are `git` (fixed, read-only sub-commands, hardened flags) and the sandbox's Python with a fixed runner."""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from jarvis.desktop import paths as _paths
from jarvis.desktop import schema as S
from jarvis.desktop.files import MAX_READ, _decode
from jarvis.desktop.paths import PathMissing, PathRejected
from jarvis.desktop.schema import Arg, Outcome, ToolSpec
from jarvis.dev import errors as E
from jarvis.dev import sandbox as SB

SKIP_DIRS = SB._SKIP_DIRS
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json",
                   "secrets.json", "secrets.yaml", "secrets.yml", "service-account.json", ".git-credentials", "known_hosts"}
SENSITIVE_SUFFIX = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".jks", ".ppk"}
MAX_EDIT = 2_000_000
MAX_SNAPSHOT_FILES = 5000
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024


# ------------------------------------------------------------------ helpers
def sensitive(p: Path) -> str | None:
    n = p.name.lower()
    if n in SENSITIVE_NAMES or n.startswith(".env.") or p.suffix.lower() in SENSITIVE_SUFFIX:
        return f"'{p.name}' looks like a secrets file (keys, tokens, passwords); Jarvis never reads or changes those"
    return None


def _project(env, raw: str) -> Path:
    p = env.code_paths.resolve(raw, must_exist=True)
    if not p.is_dir():
        raise PathRejected(f"{p} is not a folder")
    return p


def _file(env, raw: str, *, for_write: bool = False, must_exist: bool = True) -> Path:
    p = env.code_paths.resolve(raw, must_exist=must_exist, for_write=for_write)
    why = sensitive(p)
    if why:
        raise PathRejected(why)
    return p


def _walk(env, root: Path, max_files: int = 20000):
    """Files below root in a stable order; never enters skipped folders, links, Jarvis' own data or secrets."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        env.stop.check()
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and _walkable(env, Path(dirpath) / d))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if sensitive(p) or _paths._link_problem(p):
                continue
            n += 1
            if n > max_files:
                return
            yield p


def _walkable(env, d: Path) -> bool:
    try:
        return not env.code_paths.is_protected(d) and _paths._link_problem(d) is None
    except PathRejected:
        return False


def _text_of(p: Path, limit: int = MAX_READ) -> tuple[str, str]:
    size = p.stat().st_size
    if size > limit:
        raise ValueError(f"{p} is {size} bytes; the limit here is {limit}")
    return _decode(p.read_bytes())


def manifest(env, root: Path) -> dict[str, str]:
    """relative path -> sha256 for every readable project file (skipped folders and secrets excluded)."""
    out, total = {}, 0
    for p in _walk(env, root, MAX_SNAPSHOT_FILES + 1):
        if len(out) >= MAX_SNAPSHOT_FILES:
            raise ValueError(f"the project has more than {MAX_SNAPSHOT_FILES} files; Jarvis will not snapshot or compare something that large")
        total += p.stat().st_size
        if total > MAX_SNAPSHOT_BYTES:
            raise ValueError("the project is larger than 100 MB; Jarvis will not snapshot or compare something that large")
        out[p.relative_to(root).as_posix()] = _sha(p)
    return out


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _state_dir(env, name: str) -> Path:
    d = env.profile_dir / name           # inside the PROTECTED profile folder: no file/code tool can reach it
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(env, p: Path, data: bytes) -> None:
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
        env.code_paths.revalidate(p)
        os.replace(tmp, p)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ inspect
def h_repo_overview(a, env) -> Outcome:
    root = _project(env, a["path"])
    exts: dict[str, int] = {}
    top, key, tests = [], [], []
    files = 0
    for p in _walk(env, root):
        files += 1
        rel = p.relative_to(root)
        exts[p.suffix.lower() or "(none)"] = exts.get(p.suffix.lower() or "(none)", 0) + 1
        if len(rel.parts) == 1 and len(top) < 60:
            top.append(rel.name)
        n = p.name.lower()
        if len(rel.parts) <= 2 and (n.startswith(("readme", "license", "makefile", "dockerfile", "requirements")) or n in (
                "pyproject.toml", "setup.py", "setup.cfg", "package.json", "cargo.toml", "go.mod", "pom.xml", "build.gradle", "cmakelists.txt",
                "tox.ini", "pytest.ini", "conftest.py", "tsconfig.json")):
            key.append(rel.as_posix())
        if re.match(r"(test_.*\.py|.*_test\.py|.*\.test\.[jt]sx?|.*\.spec\.[jt]sx?)$", n):
            tests.append(rel.as_posix())
    meta: dict = {}
    pp = root / "pyproject.toml"
    if pp.is_file() and pp.stat().st_size < 200_000:
        try:
            t = tomllib.loads(pp.read_text(encoding="utf-8"))
            pr = t.get("project", {})
            meta = {"name": pr.get("name"), "version": pr.get("version"), "requires_python": pr.get("requires-python"),
                    "dependencies": [re.split(r"[<>=!~\s;\[]", str(d), maxsplit=1)[0] for d in pr.get("dependencies", [])][:40],
                    "has_pytest_config": "pytest" in json.dumps(t.get("tool", {}))}
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
            meta = {"note": "pyproject.toml could not be parsed"}
    hint = ("pytest" if (meta.get("has_pytest_config") or (root / "pytest.ini").exists()) else "unittest") if tests else None
    top_ext = sorted(exts.items(), key=lambda kv: -kv[1])[:10]
    lines = [f"project: {root}", f"files (skipping .git, caches, virtual environments, secrets): {files}",
             "file types: " + ", ".join(f"{e}={n}" for e, n in top_ext), "top level: " + ", ".join(top),
             "key files: " + (", ".join(key) or "(none found)"), f"test files: {len(tests)}" + (f" e.g. {', '.join(tests[:5])}" if tests else ""),
             f"likely test runner: {hint or 'none detected'}  (a guess from file names; Jarvis has not run anything)",
             f"version control: {'git repository' if (root / '.git').exists() else 'no .git folder'}"]
    if meta:
        lines.append("pyproject: " + json.dumps(meta))
    return Outcome(True, f"overview of {root}: {files} files, {len(tests)} test files", verified=True, evidence="listed from disk now",
                   data={"path": str(root), "files": files, "file_types": dict(top_ext), "key_files": key, "test_files": len(tests),
                         "test_runner_guess": hint, "git": (root / ".git").exists(), "pyproject": meta}, untrusted_text="\n".join(lines))


def h_read_lines(a, env) -> Outcome:
    p = _file(env, a["path"])
    if not p.is_file():
        raise PathRejected(f"{p} is not a file")
    text, enc = _text_of(p, 2_000_000)
    lines = text.splitlines()
    start, count = a["start"], a["count"]
    chunk = lines[start - 1:start - 1 + count]
    width = len(str(start + max(len(chunk) - 1, 0)))
    body = "\n".join(f"{start + i:>{width}} | {ln}" for i, ln in enumerate(chunk))
    end = start + len(chunk) - 1
    return Outcome(True, f"{p.name}: lines {start}-{end} of {len(lines)}" if chunk else f"{p.name} has only {len(lines)} lines (nothing at line {start})",
                   verified=True, evidence="read from disk now", data={"path": str(p), "total_lines": len(lines), "sha256": _sha(p), "encoding": enc},
                   untrusted_text=body)


def h_search_code(a, env) -> Outcome:
    root = _project(env, a["folder"])
    q = a["query"]
    needle = q if a["case_sensitive"] else q.lower()
    glob = a["glob"]
    import fnmatch
    hits, scanned, t0 = [], 0, time.monotonic()
    for p in _walk(env, root):
        if time.monotonic() - t0 > 20:
            break
        if not fnmatch.fnmatch(p.name.lower(), glob.lower()):
            continue
        try:
            if p.stat().st_size > 1_000_000:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        scanned += 1
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            if needle in (ln if a["case_sensitive"] else ln.lower()):
                hits.append(f"{p.relative_to(root).as_posix()}:{i}: {ln.strip()[:200]}")
                if len(hits) >= a["max_results"]:
                    break
        if len(hits) >= a["max_results"]:
            break
    return Outcome(True, f"{len(hits)} match(es) for the text '{q[:60]}' in {scanned} file(s) (plain text search, not a regular expression)",
                   verified=True, evidence="searched the files on disk now", data={"matches": len(hits), "files_searched": scanned},
                   untrusted_text="\n".join(hits))


def h_analyze_error(a, env) -> Outcome:
    text, project = a["text"], None
    if a["path"]:
        p = _file(env, a["path"])
        text, _ = _text_of(p, 2_000_000)
        project = str(env.code_paths.resolve(a["project"], must_exist=True)) if a["project"] else None
    elif not text:
        raise S.ArgError("give either 'text' (pasted output) or 'path' (a log file inside a project folder)")
    elif a["project"]:
        project = str(env.code_paths.resolve(a["project"], must_exist=True))
    res = E.analyze(text, project)
    lines = []
    for e in res["exceptions"]:
        lines.append(f"exception: {e['type']}: {e['message']}")
    f = res["innermost_project_frame"]
    if f:
        lines.append(f"innermost frame in project code: {f['file']}:{f['line']} in {f['function']}")
    for t in res["failed_tests"][:20]:
        lines.append(f"{t['kind']}: {t['test']}")
    for m in res["compiler_messages"][:10]:
        lines.append(f"{m['severity']}: {m['file']}:{m['line']}: {m['message']}")
    if res["test_summary"]:
        lines.append("test summary: " + res["test_summary"]["summary_line"])
    ok = bool(res["kinds"])
    return Outcome(ok, ("found: " + ", ".join(res["kinds"])) if ok else "no recognisable error output in this text", verified=True,
                   evidence="parsed the text with fixed rules (no execution); hints are generic", data=res, untrusted_text="\n".join(lines))


_GIT = {"status": ["status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
        "log": ["log", "-n", "15", "--no-textconv", "--date=short", "--format=%h %ad %an: %s"],
        "diff_stat": ["diff", "--stat", "--no-ext-diff", "--no-textconv"],
        "branch": ["branch", "--show-current"]}


def h_git_info(a, env) -> Outcome:
    root = _project(env, a["path"])
    git = shutil.which("git")
    if not git:
        return Outcome(False, "git is not installed or not on PATH, so repository history is unavailable (everything else still works)",
                       verified=True, evidence="looked for git on PATH")
    cmd = [git, "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-c", "protocol.ext.allow=never", "-C", str(root), "--no-pager", *_GIT[a["what"]]]
    envv = {k: v for k, v in os.environ.items() if k in ("PATH", "SystemRoot", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG")}
    envv.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"})
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, env=envv, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        return Outcome(False, f"git did not run ({type(e).__name__})", verified=True, evidence="git was started and failed")
    out = (r.stdout or "")[:20_000]
    if r.returncode != 0:
        return Outcome(False, f"git {a['what']} failed: {(r.stderr or '').strip()[:300]}", verified=True, evidence=f"git exit code {r.returncode}")
    return Outcome(True, f"git {a['what']} for {root}", verified=True, evidence="output of a read-only git command run now",
                   data={"what": a["what"]}, untrusted_text=out)


def h_diff_snapshot(a, env) -> Outcome:
    root = _project(env, a["path"])
    sid = a["snapshot"]
    d = _state_dir(env, "code_snapshots") / sid
    if not re.fullmatch(r"[0-9A-Za-z_-]{6,40}", sid) or not (d / "manifest.json").is_file():
        raise PathMissing(f"no snapshot '{sid}'")
    rec = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    if rec.get("project") != str(root):
        raise PathRejected(f"snapshot {sid} was taken of {rec.get('project')}, not {root}")
    then, now = rec["files"], manifest(env, root)
    added = sorted(set(now) - set(then))
    removed = sorted(set(then) - set(now))
    changed = sorted(k for k in set(now) & set(then) if now[k] != then[k])
    lines = [f"added: {x}" for x in added] + [f"removed: {x}" for x in removed] + [f"changed: {x}" for x in changed]
    return Outcome(True, f"since snapshot {sid}: {len(added)} added, {len(removed)} removed, {len(changed)} changed", verified=True,
                   evidence="compared file hashes on disk now with the snapshot's manifest",
                   data={"added": added[:200], "removed": removed[:200], "changed": changed[:200]}, untrusted_text="\n".join(lines[:300]))


# ------------------------------------------------------------------ change (always asked)
def _bind_path(a, env, *, must_exist=True):
    p = _file(env, a["path"], for_write=True, must_exist=must_exist)
    if p.exists() and not p.is_file():
        raise PathRejected(f"{p} is not a file")
    return p


def _diff_effects(old: str, new: str, name: str) -> list[str]:
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"{name} (now)", f"{name} (after)", lineterm="", n=2))
    out = [ln[:160] for ln in d[:60]]
    if len(d) > 60:
        out.append(f"... {len(d) - 60} more diff line(s)")
    return out


def _bind_edit(a, env):
    p = _bind_path(a, env)
    if p.stat().st_size > MAX_EDIT:
        raise PathRejected(f"{p} is too large to edit here")
    text, _ = _decode(p.read_bytes())
    old = a["old_text"]
    if not old:
        raise PathMissing("old_text must not be empty")
    n = text.count(old)
    if n != a["occurrences"]:
        raise PathMissing(f"expected the text {a['occurrences']} time(s) in {p.name} but found it {n} time(s); nothing was changed")
    updated = text.replace(old, a["new_text"])
    pinned = dict(a, path=str(p))
    return pinned, [f"edits {p} ({n} replacement(s); a backup is made first and can be restored with undo_code_edit)"] + _diff_effects(text, updated, p.name), [_sha(p)]


def _edit_risk(a, env):
    return S.DESTRUCTIVE


def _backup(env, p: Path, reason: str) -> str:
    bid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = _state_dir(env, "code_backups") / bid
    d.mkdir(parents=True)
    existed = p.exists()
    if existed:
        shutil.copy2(p, d / "before")
    (d / "record.json").write_text(json.dumps({"id": bid, "path": str(p), "existed": existed, "reason": reason, "before_sha256": _sha(p) if existed else None,
                                              "after_sha256": None, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    return bid


def _finish_backup(env, bid: str, p: Path) -> None:
    f = _state_dir(env, "code_backups") / bid / "record.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["after_sha256"] = _sha(p)
    f.write_text(json.dumps(rec), encoding="utf-8")


def _syntax_note(p: Path, text: str) -> tuple[str, bool | None]:
    if p.suffix.lower() == ".py":
        try:
            ast.parse(text)
            return "; Python syntax check of the new file: ok", True
        except SyntaxError as e:
            return f"; WARNING: the new file does NOT parse as Python (line {e.lineno}: {e.msg})", False
    return "", None


def h_edit_code(a, env) -> Outcome:
    p = _bind_path(a, env)
    text, enc = _decode(p.read_bytes())
    n = text.count(a["old_text"])
    if n != a["occurrences"]:
        raise PathMissing(f"expected the text {a['occurrences']} time(s) but found it {n} time(s); nothing was changed")
    bid = _backup(env, p, "before edit_code")
    updated = text.replace(a["old_text"], a["new_text"])
    _atomic_write(env, p, updated.encode("utf-8-sig" if enc == "utf-8-sig" else enc))
    _finish_backup(env, bid, p)
    back, _ = _decode(p.read_bytes())
    ok = back == updated
    note, syntax_ok = _syntax_note(p, back)
    return Outcome(ok, (f"edited {p.name}: {n} replacement(s)" if ok else f"{p.name} does not show the edit after saving") + note,
                   verified=ok, evidence=f"re-read the file and compared it with the intended text; backup {bid}",
                   data={"path": str(p), "backup": bid, "replaced": n, "syntax_ok": syntax_ok}, checkpoint=None)


def _write_risk(a, env):
    try:
        return S.DESTRUCTIVE if env.code_paths.resolve(a.get("path", ""), for_write=True).exists() else S.MODIFY
    except PathRejected:
        return S.MODIFY


def _bind_write(a, env):
    p = _bind_path(a, env, must_exist=False)
    exists = p.exists()
    if exists and not a["overwrite"]:
        raise PathRejected(f"{p} already exists; set overwrite=true to replace it (a backup is made first)")
    data = a["content"].encode("utf-8")
    if len(data) > MAX_EDIT:
        raise PathRejected("content is too large")
    old = _decode(p.read_bytes())[0] if exists else ""
    eff = [("replaces the existing file (a backup is made first): " if exists else "creates the file: ") + str(p)]
    eff += _diff_effects(old, a["content"], p.name) if exists else [f"new file with {len(a['content'].splitlines())} line(s), sha256 {hashlib.sha256(data).hexdigest()[:16]}"]
    return dict(a, path=str(p)), eff, [_sha(p) if exists else None]


def h_write_code(a, env) -> Outcome:
    p = _bind_path(a, env, must_exist=False)
    exists = p.exists()
    if exists and not a["overwrite"]:
        raise PathRejected(f"{p} already exists; set overwrite=true to replace it")
    bid = _backup(env, p, "before write_code")
    if not p.parent.exists():
        env.code_paths.resolve(str(p.parent), for_write=True)
        p.parent.mkdir(parents=True)
    data = a["content"].encode("utf-8")
    _atomic_write(env, p, data)
    _finish_backup(env, bid, p)
    ok = p.read_bytes() == data
    note, syntax_ok = _syntax_note(p, a["content"])
    return Outcome(ok, (f"wrote {len(data)} bytes to {p.name}" if ok else f"{p.name} does not contain what was written") + note, verified=ok,
                   evidence=f"read the file back and compared; backup {bid}", data={"path": str(p), "backup": bid, "syntax_ok": syntax_ok, "replaced_existing": exists})


def _bind_undo(a, env):
    bid = a["backup"]
    if not re.fullmatch(r"[0-9A-Za-z_-]{6,40}", bid):
        raise PathRejected("not a valid backup id")
    f = _state_dir(env, "code_backups") / bid / "record.json"
    if not f.is_file():
        raise PathMissing(f"no code backup '{bid}'")
    rec = json.loads(f.read_text(encoding="utf-8"))
    p = env.code_paths.resolve(rec["path"], for_write=True)
    cur = _sha(p) if p.exists() else None
    if rec.get("after_sha256") and cur != rec["after_sha256"]:
        raise PathRejected(f"{p.name} was changed after that edit, so restoring would throw those later changes away; nothing was done")
    return a, [("restores the previous contents of " if rec["existed"] else "removes the file Jarvis created: ") + str(p)], [rec["id"], cur]


def h_undo_code_edit(a, env) -> Outcome:
    d = _state_dir(env, "code_backups") / a["backup"]
    rec = json.loads((d / "record.json").read_text(encoding="utf-8"))
    p = env.code_paths.resolve(rec["path"], for_write=True)
    if rec.get("after_sha256") and (_sha(p) if p.exists() else None) != rec["after_sha256"]:
        raise PathRejected("the file changed after that edit; nothing was done")
    if rec["existed"]:
        _atomic_write(env, p, (d / "before").read_bytes())
        ok = _sha(p) == rec["before_sha256"]
    else:
        p.unlink()
        ok = not p.exists()
    return Outcome(ok, f"restored {p.name}" if ok else f"{p.name} is not as it was before the edit", verified=ok,
                   evidence="compared the file's hash with the backup's recorded hash", data={"path": str(p)})


def h_snapshot_project(a, env) -> Outcome:
    root = _project(env, a["path"])
    man = manifest(env, root)
    sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = _state_dir(env, "code_snapshots") / sid
    (d / "files").mkdir(parents=True)
    for rel in man:
        dst = d / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dst)
    (d / "manifest.json").write_text(json.dumps({"id": sid, "project": str(root), "label": a["label"], "files": man,
                                                 "created": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    ok = all(_sha(d / "files" / rel) == h for rel, h in man.items())
    return Outcome(ok, f"snapshot {sid}: {len(man)} file(s) copied into Jarvis' own backup folder" if ok else "the snapshot copy does not match the files",
                   verified=ok, evidence="re-hashed every copied file", data={"snapshot": sid, "files": len(man)})


# ------------------------------------------------------------------ run (sandbox only)
def _sandbox_or_refuse() -> None:
    ok, why = SB.availability()
    if not ok:
        raise PathRejected("Jarvis will not run code without a genuine sandbox. " + why + " What still works: reading and searching code, analysing "
                           "error output you paste, and editing files. Run the tests yourself and paste the output into analyze_error.")


def _bind_run_tests(a, env):
    root = _project(env, a["project"])
    _sandbox_or_refuse()
    target = a["target"]
    if target and not re.fullmatch(r"[A-Za-z0-9_./:\[\]\-]{1,200}", target):
        raise PathRejected("the test target may only contain letters, digits and _ . / : [ ] -")
    if target.startswith("-") or ".." in target:
        raise PathRejected("the test target must not start with '-' or contain '..'")
    man = manifest(env, root)
    ph = hashlib.sha256(json.dumps(man, sort_keys=True).encode()).hexdigest()
    pinned = dict(a, project=str(root))
    eff = [f"runs the {a['runner']} test runner on a COPY of {root} ({len(man)} files) inside the sandbox: no network, no access to your other files, "
           f"stopped after {a['timeout']} s", f"command: {a['runner']} " + (target or "(all tests found)"), "the copy is deleted afterwards; nothing is written to your project"]
    return pinned, eff, [ph]


def _runner_argv(runner: str, target: str) -> list[str]:
    py = SB.interpreter()
    if runner == "pytest":
        return [py, "-B", "-s", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no", *( [target] if target else [])]
    return [py, "-B", "-s", "-m", "unittest", *([target] if target else ["discover"]), "-v"] if target else [py, "-B", "-s", "-m", "unittest", "discover", "-v"]


def _sandbox_pythonpath(root: Path) -> str:
    parts = ["/work"]
    if (root / "src").is_dir():
        parts.insert(0, "/work/src")
    return os.pathsep.join(parts)


def h_run_tests(a, env) -> Outcome:
    root = _project(env, a["project"])
    _sandbox_or_refuse()
    man = manifest(env, root)
    ph = hashlib.sha256(json.dumps(man, sort_keys=True).encode()).hexdigest()
    lim = SB.Limits(wall_seconds=a["timeout"], cpu_seconds=int(a["timeout"]) + 5)
    argv = _runner_argv(a["runner"], a["target"])
    res = SB.run(argv, copy_from=root, limits=lim, stop=env.stop.is_set, env={"PYTHONPATH": _sandbox_pythonpath(root)})
    if res.killed_for == "stop":
        env.stop.check()
    summary = E.parse_test_summary(a["runner"], res.stdout, res.stderr, res.returncode)
    tail = (res.stdout[-4000:] + ("\n--- stderr ---\n" + res.stderr[-4000:] if res.stderr.strip() else ""))
    rec = {"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6], "project": str(root), "runner": a["runner"], "target": a["target"],
           "ok": bool(summary["ok"]) and res.completed and res.returncode == 0, "decisive": summary["ok"] is not None and res.completed,
           "summary": summary, "project_hash": ph, "manifest": man, "killed_for": res.killed_for, "returncode": res.returncode,
           "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "seq": time.time_ns()}
    d = _state_dir(env, "code_runs")
    (d / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    data = {"run": rec["id"], "summary": summary, "returncode": res.returncode, "killed_for": res.killed_for, "seconds": round(res.seconds, 1),
            "backend": res.backend, "skipped_links": len(res.skipped)}
    if res.killed_for:
        return Outcome(False, f"the test run was stopped ({res.killed_for}) after {res.seconds:.0f}s; whatever it was doing is unfinished and its result is UNKNOWN",
                       verified=None, evidence="the sandbox killed the process tree", data=data, untrusted_text=tail)
    if summary["ok"] is None:
        return Outcome(res.returncode == 0, f"the runner exited with code {res.returncode} but printed no test summary Jarvis could read, so the result is not confirmed",
                       verified=None, evidence="", data=data, untrusted_text=tail)
    verified = summary["consistent_with_exit_code"] is True
    ok = bool(summary["ok"]) and res.returncode == 0
    return Outcome(ok, f"{summary['summary_line']}" + ("" if ok else " - tests FAILED") , verified=verified or None,
                   evidence=f"parsed the runner's own summary and exit code {res.returncode} (run {rec['id']}, in the sandbox, on a copy)" if verified else "",
                   data=data, untrusted_text=tail)


def _bind_run_code(a, env):
    _sandbox_or_refuse()
    code = a["code"]
    eff = [f"runs this Python code inside the sandbox (no network, no access to your files, stopped after {a['timeout']} s, memory and output limited):"]
    for i, ln in enumerate(code.splitlines()[:80], 1):
        eff.append(f"{i:>3} | {ln[:200]}")
    if len(code.splitlines()) > 80:
        eff.append(f"... {len(code.splitlines()) - 80} more line(s), {len(code)} characters in all, sha256 {hashlib.sha256(code.encode()).hexdigest()[:16]}")
    return a, eff, [hashlib.sha256(code.encode()).hexdigest()]


def h_run_code(a, env) -> Outcome:
    _sandbox_or_refuse()
    res = SB.run([SB.interpreter(), "-I", "-B", "/work/snippet.py"], files={"snippet.py": a["code"]},
                 limits=SB.Limits(wall_seconds=a["timeout"], cpu_seconds=int(a["timeout"]) + 5), stop=env.stop.is_set)
    if res.killed_for == "stop":
        env.stop.check()
    text = res.stdout + (("\n--- stderr ---\n" + res.stderr) if res.stderr.strip() else "")
    data = {"returncode": res.returncode, "killed_for": res.killed_for, "seconds": round(res.seconds, 1), "backend": res.backend, "output_truncated": res.truncated}
    if res.killed_for:
        return Outcome(False, f"the snippet was stopped ({res.killed_for}) after {res.seconds:.0f}s", verified=None, evidence="the sandbox killed the process tree",
                       data=data, untrusted_text=text[-6000:])
    ok = res.returncode == 0
    return Outcome(ok, f"the snippet exited with code {res.returncode}", verified=True,
                   evidence=f"ran in the sandbox; exit code {res.returncode} (this says the program ran, not that its output is correct)", data=data, untrusted_text=text[-6000:])


def h_verify_fix(a, env) -> Outcome:
    root = _project(env, a["project"])
    d = env.profile_dir / "code_runs"             # looked at only: a read must not create the folder
    runs = []
    for f in (sorted(d.glob("*.json")) if d.is_dir() else []):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("project") == str(root) and r.get("runner") == a["runner"] and r.get("target") == a["target"] and r.get("decisive"):
            runs.append(r)
    runs.sort(key=lambda r: r.get("seq", 0))
    if not runs:
        return Outcome(False, "NOT VERIFIED: no completed test run is recorded for this project/runner/target. Run the tests first (run_tests).", verified=True,
                       evidence="searched Jarvis' record of test runs", data={"verdict": "no_runs"})
    last = runs[-1]
    failing = [r for r in runs if not r["ok"]]
    if last["ok"] and not failing:
        return Outcome(False, "NOT VERIFIED AS A FIX: the tests pass, but Jarvis never saw them FAIL, so there is no proof that anything was broken or that a "
                              "change fixed it. To prove a fix: reproduce the failure first, change the code, run again.", verified=True,
                       evidence="every recorded run passed", data={"verdict": "not_reproduced"})
    if not last["ok"]:
        return Outcome(False, f"STILL FAILING: the latest run (#{last['id']}) failed: {last['summary'].get('summary_line')}", verified=True,
                       evidence="latest recorded run failed", data={"verdict": "still_failing", "run": last["id"]})
    base = failing[-1]
    then, now = base["manifest"], last["manifest"]
    changed = sorted(k for k in set(then) | set(now) if then.get(k) != now.get(k))
    if not changed:
        return Outcome(False, "NOT VERIFIED AS A FIX: the earlier run failed and the latest passed, but no project file differs between them (flaky test or environment).",
                       verified=True, evidence="compared the file hashes of the two runs", data={"verdict": "no_change"})
    is_test = lambda k: bool(re.search(r"(^|/)(tests?|spec)(/|$)|(^|/)(test_[^/]*|[^/]*_test\.py|conftest\.py)$|\.(test|spec)\.[jt]sx?$", k))
    src = [k for k in changed if not is_test(k)]
    if not src:
        return Outcome(False, "NOT VERIFIED AS A FIX: only test files changed between the failing and the passing run (" + ", ".join(changed[:5]) +
                       "). Changing a test until it passes is not a fix of the program.", verified=True, evidence="compared the file hashes of the two runs",
                       data={"verdict": "tests_only", "changed": changed[:50]})
    note = "" if len(src) == len(changed) else f" (test files also changed: {', '.join(k for k in changed if is_test(k))[:200]} - review them)"
    return Outcome(True, f"VERIFIED FIX: run #{base['id']} failed ({base['summary'].get('summary_line')}); after changing {', '.join(src[:5])} run #{last['id']} "
                         f"passed ({last['summary'].get('summary_line')}){note}", verified=True,
                   evidence="two recorded sandbox runs of the same tests: fail, then pass, with source files changed in between",
                   data={"verdict": "verified_fix", "failing_run": base["id"], "passing_run": last["id"], "changed": changed[:50]})


# ------------------------------------------------------------------ registry
PROJ = Arg("path", "str", "a project folder (added with `jarvis desktop allow-project`)", max_len=500, no_newlines=True)


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("repo_overview", "summarise a project: file types, key files, tests, likely test runner (offline, read-only)", "code_read", S.OBSERVE,
                 (PROJ,), h_repo_overview, timeout=60, untrusted_output=True),
        ToolSpec("read_lines", "read numbered lines of a source file in a project folder (never secrets files)", "code_read", S.OBSERVE,
                 (Arg("path", "str", max_len=500, no_newlines=True), Arg("start", "int", required=False, default=1, min=1, max=10_000_000),
                  Arg("count", "int", required=False, default=200, min=1, max=1000)), h_read_lines, untrusted_output=True),
        ToolSpec("search_code", "find a piece of text in a project's files (plain text, case-insensitive by default)", "code_read", S.OBSERVE,
                 (Arg("query", "str", max_len=200, no_newlines=True), Arg("folder", "str", max_len=500, no_newlines=True),
                  Arg("glob", "str", required=False, default="*", max_len=60, no_newlines=True),
                  Arg("max_results", "int", required=False, default=50, min=1, max=300),
                  Arg("case_sensitive", "bool", required=False, default=False)), h_search_code, timeout=40, untrusted_output=True),
        ToolSpec("analyze_error", "parse an error log, traceback, test output or compiler output into structured facts (offline; nothing is run)", "code_read",
                 S.OBSERVE, (Arg("text", "str", required=False, default="", max_len=100_000), Arg("path", "str", required=False, default="", max_len=500, no_newlines=True),
                             Arg("project", "str", required=False, default="", max_len=500, no_newlines=True)), h_analyze_error, untrusted_output=True),
        ToolSpec("git_info", "read-only git facts about a project: status, log, diff_stat, branch", "code_read", S.OBSERVE,
                 (PROJ, Arg("what", "str", choices=tuple(_GIT))), h_git_info, timeout=30, untrusted_output=True),
        ToolSpec("diff_snapshot", "compare a project with a snapshot made earlier by snapshot_project", "code_read", S.OBSERVE,
                 (PROJ, Arg("snapshot", "str", max_len=60, no_newlines=True)), h_diff_snapshot, timeout=60, untrusted_output=True),
        ToolSpec("verify_fix", "decide from Jarvis' own recorded test runs whether a fix is proven (fail, then change, then pass)", "code_read", S.OBSERVE,
                 (Arg("project", "str", max_len=500, no_newlines=True), Arg("runner", "str", choices=("unittest", "pytest")),
                  Arg("target", "str", required=False, default="", max_len=200, no_newlines=True)), h_verify_fix, timeout=30),
        ToolSpec("snapshot_project", "copy a project's files into Jarvis' own backup folder (a checkpoint before significant edits)", "code_edit", S.MODIFY,
                 (PROJ, Arg("label", "str", required=False, default="", max_len=100, no_newlines=True)), h_snapshot_project, timeout=120),
        ToolSpec("edit_code", "replace exact text in a source file in a project folder (shows the diff, backup first)", "code_edit", S.DESTRUCTIVE,
                 (Arg("path", "str", max_len=500, no_newlines=True), Arg("old_text", "str", max_len=100_000), Arg("new_text", "str", max_len=100_000),
                  Arg("occurrences", "int", required=False, default=1, min=1, max=1000)), h_edit_code, risk_fn=_edit_risk, bind=_bind_edit),
        ToolSpec("write_code", "create a source file (or replace one with overwrite=true) in a project folder; backup first", "code_edit", S.MODIFY,
                 (Arg("path", "str", max_len=500, no_newlines=True), Arg("content", "str", max_len=MAX_EDIT),
                  Arg("overwrite", "bool", required=False, default=False)), h_write_code, risk_fn=_write_risk, bind=_bind_write),
        ToolSpec("undo_code_edit", "restore a file from the backup an edit_code/write_code made (refuses if the file changed since)", "code_edit", S.DESTRUCTIVE,
                 (Arg("backup", "str", max_len=60, no_newlines=True),), h_undo_code_edit, risk_fn=_edit_risk, bind=_bind_undo),
        ToolSpec("run_tests", "run unittest or pytest on a COPY of a project inside the sandbox (no network); reports the runner's own summary", "code_run", S.EXECUTE,
                 (Arg("project", "str", max_len=500, no_newlines=True), Arg("runner", "str", choices=("unittest", "pytest")),
                  Arg("target", "str", required=False, default="", max_len=200, no_newlines=True),
                  Arg("timeout", "float", required=False, default=120.0, min=5, max=300)), h_run_tests, timeout=420, untrusted_output=True,
                 bind=_bind_run_tests),
        ToolSpec("run_code", "run a short Python snippet inside the sandbox (no network, no access to your files)", "code_run", S.EXECUTE,
                 (Arg("code", "str", max_len=20_000), Arg("timeout", "float", required=False, default=10.0, min=1, max=60)), h_run_code, timeout=90,
                 untrusted_output=True, bind=_bind_run_code),
    ]
