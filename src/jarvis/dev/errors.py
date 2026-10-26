"""Reading error output. Pure text parsing, offline, no execution. It reports what is IN the text (exception type, message, stack
frames, failing test names, counts); the 'hints' are fixed generic advice and are labelled as such - they are not a diagnosis of
this particular program. All the text it reads is untrusted (a log can say anything)."""
from __future__ import annotations

import re

MAX_TEXT = 100_000

_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"\n]+)", line (?P<line>\d+)(?:, in (?P<func>[^\n]+))?\s*$', re.M)
_PY_EXC = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Fault)|StopIteration|AssertionError)(?:: (?P<msg>.*))?$", re.M)
_NODE_FRAME = re.compile(r"^\s*at (?:(?P<func>[^\s(][^(\n]*?) \()?(?P<file>[^():\n]+):(?P<line>\d+):(?P<col>\d+)\)?\s*$", re.M)
_GCC = re.compile(r"^(?P<file>[^\s:][^:\n]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>fatal error|error|warning):\s*(?P<msg>.+)$", re.M)
_MSVC = re.compile(r"^(?P<file>[^\n(]+)\((?P<line>\d+)(?:,(?P<col>\d+))?\)\s*:\s*(?P<sev>fatal error|error|warning)\s+(?P<code>[A-Z]+\d+)\s*:\s*(?P<msg>.+)$", re.M)
_UT_FAIL = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<test>\S+) \((?P<where>[^)\n]+)\)", re.M)
_UT_RAN = re.compile(r"^Ran (?P<n>\d+) tests? in (?P<secs>[\d.]+)s", re.M)
_UT_RESULT = re.compile(r"^(?P<res>OK|FAILED)(?: \((?P<detail>[^)\n]*)\))?\s*$", re.M)
_PT_FAIL = re.compile(r"^(?P<kind>FAILED|ERROR) (?P<test>\S+?)(?: - (?P<msg>.*))?$", re.M)
_PT_SUMMARY = re.compile(r"^=+ (?P<body>.*?(?:passed|failed|error|errors|no tests ran|skipped|deselected|xfailed|xpassed).*?) in (?P<secs>[\d.]+)s.*=+$", re.M)
_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected)")

# Fixed, generic, clearly labelled. Never claims to know the cause in THIS program.
HINTS = {
    "ModuleNotFoundError": "A module could not be imported. Typical causes: a dependency is not installed in the environment that ran this, a typo in the "
                           "module name, or a wrong working directory / import path. Jarvis will not install packages without your approval.",
    "ImportError": "An import failed. Typical causes: a missing or incompatible dependency version, a circular import, or a name that does not exist in that module.",
    "SyntaxError": "Python could not parse the file. Look at the reported line and the lines just before it (a missing bracket, quote or colon is often reported later).",
    "IndentationError": "Inconsistent indentation (tabs vs spaces, or a block with no body) at or just above the reported line.",
    "NameError": "A name is used before it is defined or is misspelled.",
    "AttributeError": "An object does not have the attribute used. Check the object's real type at that line (it may be None).",
    "TypeError": "A value of the wrong type, or a wrong number of arguments, was passed at the reported line.",
    "KeyError": "A dictionary key that is not present was looked up.",
    "IndexError": "A list/tuple index was outside its length.",
    "ValueError": "A value had the right type but an unacceptable content.",
    "FileNotFoundError": "A file or folder does not exist at that path from the program's working directory.",
    "PermissionError": "The operating system refused access to that file or resource.",
    "ZeroDivisionError": "A division or modulo by zero.",
    "RecursionError": "A function called itself without reaching a stop condition (or the input was too deep).",
    "AssertionError": "An assertion or a test comparison was false: the actual value differed from the expected one shown in the message.",
    "TimeoutError": "An operation did not finish in time.",
    "MemoryError": "The program ran out of memory.",
}


def _project_frame(frames: list[dict], project: str | None) -> dict | None:
    """The innermost frame that is in the user's own code (not the standard library or site-packages)."""
    def mine(f: str) -> bool:
        n = f.replace("\\", "/")
        if "/site-packages/" in n or "/dist-packages/" in n or "/lib/python" in n or n.startswith("<") or "/node_modules/" in n:
            return False
        if project:
            pn = project.replace("\\", "/").rstrip("/")
            return n.startswith(pn + "/") or not (n.startswith("/") or re.match(r"^[A-Za-z]:/", n))
        return True
    for fr in reversed(frames):
        if mine(fr["file"]):
            return fr
    return None


def parse_test_summary(runner: str, stdout: str, stderr: str, returncode: int | None) -> dict:
    """What a unittest/pytest run says about itself. `ok` is None when no summary could be found - never guessed."""
    text = (stderr or "") + "\n" + (stdout or "")
    out = {"runner": runner, "ran": None, "passed": None, "failed": 0, "errors": 0, "skipped": 0, "ok": None, "summary_line": "", "consistent_with_exit_code": None}
    if runner == "unittest":
        m = _UT_RAN.search(text)
        res = None
        for res in _UT_RESULT.finditer(text):
            pass
        if m and res:
            out["ran"] = int(m.group("n"))
            detail = res.group("detail") or ""
            for key, name in (("failures", "failed"), ("errors", "errors"), ("skipped", "skipped")):
                mm = re.search(rf"{key}=(\d+)", detail)
                if mm:
                    out[name] = int(mm.group(1))
            out["ok"] = res.group("res") == "OK"
            out["passed"] = max(0, out["ran"] - out["failed"] - out["errors"] - out["skipped"])
            out["summary_line"] = f"Ran {out['ran']} tests; {res.group(0).strip()}"
    elif runner == "pytest":
        last = None
        for last in _PT_SUMMARY.finditer(text):
            pass
        if last:
            body = last.group("body")
            counts = {}
            for n, w in _COUNT.findall(body):
                counts[w.rstrip("s") if w.startswith("error") else w] = counts.get(w.rstrip("s") if w.startswith("error") else w, 0) + int(n)
            out["passed"], out["failed"] = counts.get("passed", 0), counts.get("failed", 0)
            out["errors"], out["skipped"] = counts.get("error", 0), counts.get("skipped", 0)
            out["ran"] = out["passed"] + out["failed"] + out["errors"]
            out["ok"] = out["failed"] == 0 and out["errors"] == 0 and out["ran"] > 0
            out["summary_line"] = last.group(0).strip("= ").strip()
        elif "no tests ran" in text:
            out.update(ran=0, passed=0, ok=False, summary_line="no tests ran")
    if out["ok"] is not None and returncode is not None:
        out["consistent_with_exit_code"] = (returncode == 0) == bool(out["ok"])
    return out


def analyze(text: str, project: str | None = None) -> dict:
    """Structured facts from an error log / traceback / test output / compiler output."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    truncated = len(text) > MAX_TEXT
    text = text[:MAX_TEXT]
    res: dict = {"kinds": [], "exceptions": [], "frames": [], "failed_tests": [], "compiler_messages": [], "test_summary": None,
                 "innermost_project_frame": None, "hints": [], "truncated": truncated, "notes": []}
    # ---- Python tracebacks (chained exceptions give several)
    blocks = re.split(r"(?=^Traceback \(most recent call last\):)", text, flags=re.M)
    py_frames: list[dict] = []
    for b in blocks:
        if not b.startswith("Traceback"):
            continue
        fr = [{"file": m.group("file"), "line": int(m.group("line")), "function": (m.group("func") or "").strip()} for m in _PY_FRAME.finditer(b)]
        tail = [ln for ln in b.splitlines() if ln.strip() and not ln.startswith((" ", "\t")) and not ln.startswith("Traceback")]
        exc = None
        for ln in reversed(tail):
            m = _PY_EXC.match(ln.strip())
            if m:
                exc = {"type": m.group("type"), "message": (m.group("msg") or "")[:500]}
                break
        if fr or exc:
            py_frames += fr
            if exc:
                res["exceptions"].append(exc)
    if py_frames or res["exceptions"]:
        res["kinds"].append("python-traceback")
        res["frames"] += py_frames
    # a bare 'SyntaxError: ...' with a File line (no Traceback header)
    if not res["exceptions"]:
        for m in _PY_EXC.finditer(text):
            if m.group("type") in HINTS or m.group("type").endswith("Error"):
                res["exceptions"].append({"type": m.group("type"), "message": (m.group("msg") or "")[:500]})
        if res["exceptions"] and "python-traceback" not in res["kinds"]:
            res["kinds"].append("python-exception-line")
        if not py_frames:
            fr = [{"file": m.group("file"), "line": int(m.group("line")), "function": (m.group("func") or "").strip()} for m in _PY_FRAME.finditer(text)]
            res["frames"] += fr
    # ---- Node / JavaScript
    node = [{"file": m.group("file"), "line": int(m.group("line")), "function": (m.group("func") or "").strip(), "column": int(m.group("col"))}
            for m in _NODE_FRAME.finditer(text)]
    if node:
        res["kinds"].append("javascript-stack")
        res["frames"] += node
    # ---- compilers
    for rx, label in ((_GCC, "gcc-clang"), (_MSVC, "msvc")):
        found = [{"file": m.group("file").strip(), "line": int(m.group("line")), "severity": m.group("sev"), "message": m.group("msg")[:300]} for m in rx.finditer(text)]
        if found:
            res["kinds"].append(label)
            res["compiler_messages"] += found[:50]
    # ---- tests
    fails = [{"test": m.group("test"), "kind": m.group("kind").lower(), "where": m.group("where")} for m in _UT_FAIL.finditer(text)]
    if fails:
        res["kinds"].append("unittest")
    pt = [{"test": m.group("test"), "kind": m.group("kind").lower(), "message": (m.group("msg") or "")[:200]} for m in _PT_FAIL.finditer(text)
          if "::" in m.group("test") or m.group("test").endswith(".py")]
    if pt:
        res["kinds"].append("pytest")
    res["failed_tests"] = (fails + pt)[:100]
    for runner in ("unittest", "pytest"):
        s = parse_test_summary(runner, text, "", None)
        if s["ok"] is not None:
            res["test_summary"] = s
            if runner not in res["kinds"]:
                res["kinds"].append(runner)
            break
    res["innermost_project_frame"] = _project_frame(res["frames"], project)
    res["frames"] = res["frames"][:80]
    for e in res["exceptions"]:
        h = HINTS.get(e["type"].split(".")[-1])
        if h and h not in res["hints"]:
            res["hints"].append(h)
    mm = re.search(r"No module named '([^']+)'", text)
    if mm:
        res["notes"].append(f"The missing module is '{mm.group(1)}'. Its package name on PyPI may differ from the import name.")
    if not res["kinds"]:
        res["notes"].append("No recognisable traceback, test summary or compiler message was found in this text.")
    res["hints_are_generic"] = "Hints are generic advice for this kind of error, not a diagnosis of this program."
    return res
