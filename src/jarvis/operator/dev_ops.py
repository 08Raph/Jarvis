"""Programming help: project inspection, git (read freely, write only with confirmation, destructive operations refused), running approved tests."""
from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, Unavailable, cap
from jarvis.operator.files_ops import walk_files

LANG = {".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".cs": "C#", ".cpp": "C++", ".c": "C", ".h": "C/C++ header",
        ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".html": "HTML", ".css": "CSS", ".ps1": "PowerShell", ".bat": "Batch", ".sql": "SQL", ".kt": "Kotlin", ".swift": "Swift"}
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,100}$")
TEST_RUNNERS = {"unittest": ["python", "-m", "unittest"], "pytest": ["python", "-m", "pytest", "-q"], "npm": ["npm", "test", "--silent"]}


def _repo(op, folder: str | None) -> Path:
    p = op.check_path(folder) if folder else (op.roots()[0] if op.roots() else None)
    if p is None:
        raise OperatorError("Tell me which project folder.")
    return p


def _git(op, repo: Path, *args: str, timeout: float = 30.0) -> str:
    code, out, err = op.ops.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *args], timeout)
    if code != 0:
        raise OperatorError((err.strip().splitlines() or ["git reported a problem"])[0][:200])
    return out


@cap("project.inventory", "dev_read", "Describe a project: languages, structure, TODOs", LOW, ("folder",), False, "28")
def project_inventory(op, folder: str | None = None) -> Outcome:
    root = _repo(op, folder)
    langs: Counter = Counter()
    lines = Counter()
    todos = []
    for p, st in walk_files(root, 20000):
        lang = LANG.get(p.suffix.lower())
        if not lang:
            continue
        langs[lang] += 1
        if st.st_size < 400_000:
            try:
                for i, ln in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    lines[lang] += 1
                    if re.search(r"\b(TODO|FIXME|XXX|NotImplemented)\b", ln) and len(todos) < 200:
                        todos.append(f"{p.name}:{i}")
            except OSError:
                pass
    top = [d.name for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith(".")][:10]
    msg = f"{root.name}: " + ", ".join(f"{l} {n} files/{lines[l]} lines" for l, n in langs.most_common(5)) + f". Folders: {', '.join(top)}. {len(todos)} TODO/FIXME mark(s)" + (": " + ", ".join(todos[:6]) if todos else "") + "."
    return Outcome("info" if langs else "info", msg if langs else f"I see no source code in {root.name}.", {"languages": dict(langs), "todos": todos})


@cap("project.find_todos", "dev_read", "List TODO/FIXME comments", LOW, ("folder",), False, "28")
def find_todos(op, folder: str | None = None) -> Outcome:
    res = project_inventory(op, folder)
    t = res.data["todos"] if res.data else []
    return Outcome("info", f"{len(t)} TODO/FIXME: " + ", ".join(t[:15]) if t else "No TODO or FIXME comments.", t)


@cap("git.status", "dev_read", "Show git status, branch and whether the tree is clean", LOW, ("folder",), False, "30")
def git_status(op, folder: str | None = None) -> Outcome:
    r = _repo(op, folder)
    out = _git(op, r, "status", "--porcelain=v1", "-b")
    lines = out.splitlines()
    branch = lines[0][3:] if lines else "?"
    changes = lines[1:]
    return Outcome("info", f"On {branch}. " + ("The working tree is clean." if not changes else f"{len(changes)} change(s): " + "; ".join(c.strip() for c in changes[:10])), changes)


@cap("git.branches", "dev_read", "List git branches", LOW, ("folder",), False, "30")
def git_branches(op, folder: str | None = None) -> Outcome:
    out = _git(op, _repo(op, folder), "branch", "--list", "--no-color")
    names = [ln.strip() for ln in out.splitlines()]
    return Outcome("info", "Branches: " + ", ".join(names), names)


@cap("git.log", "dev_read", "Show recent commits", LOW, ("folder", "count"), False, "30")
def git_log(op, folder: str | None = None, count: int = 8) -> Outcome:
    out = _git(op, _repo(op, folder), "log", f"-{max(1, min(int(count), 50))}", "--pretty=format:%h %ad %s", "--date=short")
    return Outcome("info", out.replace("\n", "; ")[:900] or "No commits yet.", out.splitlines())


@cap("git.diff", "dev_read", "Summarize uncommitted changes", LOW, ("folder",), False, "30")
def git_diff(op, folder: str | None = None) -> Outcome:
    r = _repo(op, folder)
    stat = _git(op, r, "diff", "--stat", "--no-color").strip()
    return Outcome("info", ("Uncommitted changes: " + stat.replace("\n", "; ")[:700]) if stat else "No uncommitted changes to tracked files.", stat)


@cap("git.fetch", "dev_read", "Fetch updates from the remote (changes nothing locally)", LOW, ("folder",), False, "30")
def git_fetch(op, folder: str | None = None) -> Outcome:
    _git(op, _repo(op, folder), "fetch", "--quiet", timeout=60)
    return Outcome("verified", "Fetched the latest from the remote. Your files were not changed.")


@cap("git.conflicts", "dev_read", "Find files with merge conflicts", LOW, ("folder",), False, "30")
def git_conflicts(op, folder: str | None = None) -> Outcome:
    out = _git(op, _repo(op, folder), "diff", "--name-only", "--diff-filter=U").split()
    return Outcome("info", "Conflicted files: " + ", ".join(out) if out else "No merge conflicts.", out)


@cap("git.create_branch", "dev_write", "Create and switch to a new git branch", MEDIUM, ("folder", "name"), False, "30")
def git_create_branch(op, folder: str | None = None, name: str = "") -> Outcome:
    if not REF_RE.match(name or "") or name.startswith("-") or ".." in name:
        raise OperatorError("That isn't a valid branch name.")
    r = _repo(op, folder)
    _git(op, r, "switch", "-c", name)
    cur = _git(op, r, "branch", "--show-current").strip()
    return Outcome("verified" if cur == name else "failed", f"Created and switched to {name}." if cur == name else f"The branch is {cur}, not {name}.")


@cap("git.switch", "dev_write", "Switch git branch", MEDIUM, ("folder", "name"), False, "30")
def git_switch(op, folder: str | None = None, name: str = "") -> Outcome:
    if not REF_RE.match(name or "") or name.startswith("-"):
        raise OperatorError("That isn't a valid branch name.")
    r = _repo(op, folder)
    _git(op, r, "switch", name)
    cur = _git(op, r, "branch", "--show-current").strip()
    return Outcome("verified" if cur == name else "failed", f"Switched to {name}." if cur == name else f"Still on {cur}.")


@cap("git.stage", "dev_write", "Stage changes for a commit", MEDIUM, ("folder", "paths"), False, "30")
def git_stage(op, folder: str | None = None, paths=None) -> Outcome:
    r = _repo(op, folder)
    items = [paths] if isinstance(paths, str) else list(paths or ["."])
    for it in items:
        if str(it).startswith("-"):
            raise OperatorError("That isn't a path.")
    _git(op, r, "add", "--", *map(str, items))
    return Outcome("verified", "Staged: " + ", ".join(map(str, items)) + ".")


@cap("git.commit", "dev_write", "Commit the staged changes", HIGH, ("folder", "message"), False, "30")
def git_commit(op, folder: str | None = None, message: str = "") -> Outcome:
    m = (message or "").strip()
    if not m or len(m) > 300:
        raise OperatorError("A commit needs a short message.")
    r = _repo(op, folder)
    before = _git(op, r, "rev-parse", "HEAD").strip() if _has_head(op, r) else ""
    _git(op, r, "commit", "-m", m)
    after = _git(op, r, "rev-parse", "HEAD").strip()
    return Outcome("verified" if after != before else "failed", f"Committed {after[:7]}." if after != before else "Nothing was committed.")


def _has_head(op, r) -> bool:
    code, _o, _e = op.ops.run(["git", "-C", str(r), "rev-parse", "--verify", "HEAD"], 10)
    return code == 0


@cap("git.pull", "dev_write", "Pull updates (fast-forward only)", HIGH, ("folder",), False, "30")
def git_pull(op, folder: str | None = None) -> Outcome:
    out = _git(op, _repo(op, folder), "pull", "--ff-only", timeout=90)
    return Outcome("verified", out.strip().splitlines()[-1][:160] if out.strip() else "Up to date.")


@cap("git.push", "dev_write", "Push commits (never forced)", HIGH, ("folder",), False, "30")
def git_push(op, folder: str | None = None) -> Outcome:
    r = _repo(op, folder)
    _git(op, r, "push", timeout=120)
    ahead = _git(op, r, "status", "-sb").splitlines()[0]
    return Outcome("verified" if "ahead" not in ahead else "failed", "Pushed." if "ahead" not in ahead else f"Still ahead of the remote: {ahead}")


@cap("git.tag", "dev_write", "Create a tag", HIGH, ("folder", "name"), False, "30")
def git_tag(op, folder: str | None = None, name: str = "") -> Outcome:
    if not REF_RE.match(name or "") or name.startswith("-"):
        raise OperatorError("That isn't a valid tag name.")
    r = _repo(op, folder)
    _git(op, r, "tag", name)
    return Outcome("verified" if name in _git(op, r, "tag", "--list", name) else "failed", f"Created tag {name}.")


@cap("git.destructive", "dev_write", "Force-push, hard reset, delete branches or rewrite history (refused)", HIGH, ("what",), False, "30")
def git_destructive(op, what: str = "") -> Outcome:
    raise OperatorError("I don't do force-pushes, hard resets, branch deletion or history rewriting. They can destroy work that can't be recovered. Run those yourself in a terminal where you can see exactly what they do.")


# ---------------------------------------------------------------------------------------------------------- tests
def parse_test_output(text: str, code: int) -> dict:
    """What the output PROVES. A run only counts as passed when the runner said so AND exited 0."""
    ran = re.search(r"Ran (\d+) tests? in", text)
    py = re.search(r"(\d+) passed", text)
    failed_u = re.findall(r"^(?:FAIL|ERROR): (\S+)", text, re.M)
    failed_p = re.findall(r"^FAILED (\S+)", text, re.M)
    skipped = re.search(r"skipped=(\d+)", text) or re.search(r"(\d+) skipped", text)
    total = int(ran.group(1)) if ran else (int(py.group(1)) if py else None)
    ok_line = bool(re.search(r"^OK\b", text, re.M)) or bool(py and not failed_p and not re.search(r"\b\d+ (failed|error)", text))
    return {"ran": total, "passed": bool(code == 0 and ok_line and total is not None), "failing": failed_u + failed_p, "skipped": int(skipped.group(1)) if skipped else 0, "exit": code}


@cap("tests.run", "dev_write", "Run a project's tests (unittest, pytest or npm test) and report honestly", HIGH, ("folder", "runner", "target"), False, "31")
def run_tests(op, folder: str | None = None, runner: str = "unittest", target: str = "") -> Outcome:
    if runner not in TEST_RUNNERS:
        raise OperatorError("I can run unittest, pytest or npm test.")
    r = _repo(op, folder)
    argv = list(TEST_RUNNERS[runner])
    if runner == "unittest" and not target:
        argv += ["discover"]
    if target:
        if not re.fullmatch(r"[A-Za-z0-9_.\-/:\\\[\]]{1,120}", target) or target.startswith("-"):
            raise OperatorError("That isn't a test name I can run.")
        argv += [target]
    try:
        code, out, err = op.ops.run_in(r, argv, 900)
    except Unavailable as e:
        if "longer than" in str(e):
            return Outcome("failed", "The tests took longer than 15 minutes, so I stopped them. That is NOT a pass.")
        raise
    info = parse_test_output(out + "\n" + err, code)
    if info["ran"] is None:
        return Outcome("failed", f"I couldn't read a test result (exit code {code}). I won't call that a pass. Last output: " + (err or out).strip()[-200:], info)
    msg = f"Ran {info['ran']} test(s): " + ("all passed" if info["passed"] else f"{len(info['failing'])} failing" + (": " + ", ".join(info["failing"][:6]) if info["failing"] else f" (exit code {code})")) + (f"; {info['skipped']} skipped" if info["skipped"] else "") + "."
    return Outcome("verified" if info["passed"] else "failed", msg, info)
