"""Code plugins: reviewed, explicitly activated, run ONLY inside the sandbox.

What a plugin is: a small folder with `manifest.json` and one Python file. It declares its name, version, inputs, outputs, license,
dependencies, tests and the permissions it WANTS. What it can get: the only grantable permission is `compute` - it receives its JSON
arguments, runs in the sandbox (no network, no access to your files, throw-away copy of its own folder, time/memory/output limits) and
returns text. File access, network access, desktop control and system changes are not grantable at all (they are review BLOCKERS).

Who decides: only YOU, by the command line (`jarvis plugin install / review / activate`). The tools Jarvis' model can call cannot install, review,
activate, deactivate or edit a plugin, change its permissions, or touch the state file (it lives in the protected profile folder). A plugin runs as
tool `plugin_<name>` through the normal executor: validated arguments, your confirmation each time, audit log, timeout, emergency stop.

What review is and is not: a static look (manifest, license, dependencies, imports, risky calls, network declarations). It FINDS things. It does not
prove a plugin is safe; the sandbox is the real boundary and the activation is bound to the exact code hash you reviewed - any change to the code
deactivates it. The result of a run is the plugin's own claim; Jarvis does not mark it verified."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from jarvis.desktop import schema as S
from jarvis.desktop.paths import PathRejected
from jarvis.desktop.schema import Arg, Outcome, ToolSpec
from jarvis.dev import sandbox as SB

GRANTABLE = ("compute",)
ALLOWED_FILES = {".py", ".json", ".md", ".txt"}
MAX_FILES, MAX_BYTES = 20, 200_000
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
VER_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
NETWORK_MODS = {"socket", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "ftplib", "smtplib", "telnetlib", "ssl", "websocket",
                "websockets", "xmlrpc", "imaplib", "poplib", "socketserver", "asyncio", "webbrowser"}
PROCESS_MODS = {"subprocess", "multiprocessing", "ctypes", "cffi", "pty", "pexpect", "signal", "resource"}
FILE_MODS = {"shutil", "tempfile", "glob", "pathlib"}
DYNAMIC_MODS = {"importlib", "pickle", "marshal", "runpy", "code", "codeop", "imp"}
DYNAMIC_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint", "input"}
PROTECTED_WORDS = re.compile(r"jarvis|desktop_permissions|settings\.json|audit|\.env\b|id_rsa|policy|plugins?/state|\bSTOP\b", re.I)
PERMISSIVE = {"mit", "bsd-2-clause", "bsd-3-clause", "apache-2.0", "isc", "0bsd", "unlicense", "cc0-1.0", "python-2.0", "psf-2.0", "mpl-2.0", "zlib"}
COPYLEFT = re.compile(r"^(a|l)?gpl", re.I)
MANIFEST_KEYS = {"name", "version", "description", "license", "entry", "function", "inputs", "outputs", "permissions", "network_hosts", "dependencies", "tests"}
RUNNER = '''import importlib.util, json, sys
sys.path.insert(0, "/work")
meta = json.load(open("/work/_jarvis_call.json"))
spec = importlib.util.spec_from_file_location("jarvis_plugin_main", "/work/" + meta["entry"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
res = getattr(mod, meta["function"])(meta["args"])
print("\\n<<<JARVIS-PLUGIN-RESULT>>>")
print(json.dumps({"result": res}, default=str))
'''
MARK = "<<<JARVIS-PLUGIN-RESULT>>>"


class PluginError(ValueError):
    """Anything wrong with a plugin or a plugin operation; the message is shown to the user as is."""


# ------------------------------------------------------------------ manifest
def parse_manifest(d) -> dict:
    if not isinstance(d, dict):
        raise PluginError("manifest.json must be a JSON object")
    extra = set(d) - MANIFEST_KEYS
    if extra:
        raise PluginError(f"unknown manifest field(s): {sorted(extra)}")
    for k in ("name", "version", "description", "entry", "inputs", "outputs"):
        if k not in d:
            raise PluginError(f"manifest is missing '{k}'")
    if not isinstance(d["name"], str) or not NAME_RE.match(d["name"]):
        raise PluginError("name must be 2-31 characters: a lowercase letter, then lowercase letters, digits or _")
    if not isinstance(d["version"], str) or not VER_RE.match(d["version"]):
        raise PluginError("version must look like 1.0.0")
    if not isinstance(d["description"], str) or not (1 <= len(d["description"]) <= 200):
        raise PluginError("description must be 1-200 characters")
    if not isinstance(d["entry"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", d["entry"]):
        raise PluginError("entry must be a file name such as plugin.py")
    fn = d.get("function", "run")
    if not isinstance(fn, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", fn):
        raise PluginError("function must be a plain identifier")
    if not isinstance(d["outputs"], str) or len(d["outputs"]) > 200:
        raise PluginError("outputs must be a short text description")
    inputs = d["inputs"]
    if not isinstance(inputs, dict) or len(inputs) > 10:
        raise PluginError("inputs must be an object with at most 10 entries")
    for k, v in inputs.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", k):
            raise PluginError(f"input name '{k}' is not allowed")
        if not isinstance(v, dict) or v.get("type") not in ("str", "int", "float", "bool"):
            raise PluginError(f"input '{k}' needs a type of str, int, float or bool")
        if set(v) - {"type", "description", "required", "default", "max_len", "min", "max"}:
            raise PluginError(f"input '{k}' has unknown field(s)")
        if v.get("required", True) is False and "default" not in v:
            raise PluginError(f"optional input '{k}' needs a default")
    for k, kind in (("permissions", list), ("network_hosts", list), ("dependencies", list), ("tests", list)):
        if not isinstance(d.get(k, []), kind):
            raise PluginError(f"'{k}' must be a list")
    if not all(isinstance(x, str) for x in d.get("permissions", []) + d.get("network_hosts", []) + d.get("dependencies", [])):
        raise PluginError("permissions, network_hosts and dependencies must be lists of text")
    if len(d.get("tests", [])) > 10 or not all(isinstance(t, dict) and set(t) <= {"args", "expect_contains", "expect_ok"} for t in d.get("tests", [])):
        raise PluginError("tests: at most 10 entries, each {args, expect_contains, expect_ok}")
    if d.get("license") is not None and not isinstance(d.get("license"), str):
        raise PluginError("license must be text (an SPDX name such as MIT)")
    out = dict(d)
    out.setdefault("function", "run")
    out.setdefault("permissions", [])
    out.setdefault("network_hosts", [])
    out.setdefault("dependencies", [])
    out.setdefault("tests", [])
    return out


def code_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in Path(folder).rglob("*") if x.is_file() and not x.is_symlink()):
        h.update(p.relative_to(folder).as_posix().encode() + b"\x00")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def _scan_folder(folder: Path) -> list[str]:
    blockers, total, n = [], 0, 0
    for p in folder.rglob("*"):
        if p.is_symlink():
            blockers.append(f"{p.name} is a symbolic link; plugins may not contain links")
        elif p.is_file():
            n += 1
            total += p.stat().st_size
            if p.suffix.lower() not in ALLOWED_FILES:
                blockers.append(f"{p.name}: file type '{p.suffix}' is not allowed in a plugin (allowed: {', '.join(sorted(ALLOWED_FILES))})")
    if n > MAX_FILES:
        blockers.append(f"{n} files; the limit is {MAX_FILES}")
    if total > MAX_BYTES:
        blockers.append(f"{total} bytes; the limit is {MAX_BYTES}")
    return blockers


# ------------------------------------------------------------------ review
def review(folder: str | Path) -> dict:
    """Static review. Returns {ok, blockers, warnings, facts, hash}. `ok` means "nothing blocks activation", NOT "safe"."""
    folder = Path(folder)
    blockers: list[str] = []
    warnings: list[str] = []
    facts: dict = {}
    try:
        man = parse_manifest(json.loads((folder / "manifest.json").read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        return {"ok": False, "blockers": [f"manifest.json: {e}"], "warnings": [], "facts": {}, "hash": "", "manifest": None}
    blockers += _scan_folder(folder)
    facts.update(name=man["name"], version=man["version"], permissions_requested=man["permissions"], dependencies=man["dependencies"])
    # ---- permissions / network
    bad = [p for p in man["permissions"] if p not in GRANTABLE]
    if bad:
        blockers.append(f"asks for permission(s) {bad} that plugins cannot be given (only {list(GRANTABLE)} exists: plugins get no files, network, desktop or system access)")
    if man["network_hosts"]:
        blockers.append(f"declares network access to {man['network_hosts']}; the sandbox has no network and plugins cannot be granted any")
    # ---- license
    lic = (man.get("license") or "").strip()
    if not lic:
        warnings.append("no license declared: without one you may have no right to use or share this code")
    elif lic.lower() in PERMISSIVE:
        facts["license"] = lic
    elif COPYLEFT.match(lic):
        facts["license"] = lic
        warnings.append(f"license {lic} is copyleft: using is fine, but sharing a combined work has conditions")
    else:
        facts["license"] = lic
        warnings.append(f"license '{lic}' is not one Jarvis recognises: read it yourself")
    # ---- dependencies
    declared = {}
    for dep in man["dependencies"]:
        m = re.fullmatch(r"([A-Za-z][A-Za-z0-9_.\-]*)(?:==([0-9][\w.]*))?", dep)
        if not m:
            blockers.append(f"dependency '{dep}' is not written as name or name==version")
            continue
        imp = m.group(1).lower().replace("-", "_")
        declared[imp] = m.group(2)
        if not m.group(2):
            warnings.append(f"dependency '{dep}' is not pinned to a version: a later release could behave differently")
        if importlib.util.find_spec(imp) is None:
            blockers.append(f"dependency '{dep}' is not installed on this PC. Jarvis never installs packages by itself; install it yourself if you trust it")
    # ---- code
    entry = folder / man["entry"]
    local = {p.stem for p in folder.glob("*.py")}
    if not entry.is_file():
        blockers.append(f"entry file {man['entry']} is missing")
    imports, risky = set(), []
    for py in sorted(folder.glob("*.py")):
        if py.is_symlink():          # already a blocker (_scan_folder); never follow a link out of the plugin folder, and a dangling one must not crash the review
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError, OSError) as e:
            blockers.append(f"{py.name} cannot be parsed ({type(e).__name__}); it would not run")
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [("." * node.level) + (node.module or "")] if node.level == 0 else []
                if node.level:
                    continue
            for mname in mods:
                top = mname.split(".")[0]
                imports.add(top)
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
                if name in DYNAMIC_CALLS:
                    risky.append(f"{py.name}:{node.lineno} calls {name}() (runs code built at run time)")
                if name == "open" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str) and any(c in node.args[1].value for c in "wax+"):
                    risky.append(f"{py.name}:{node.lineno} opens a file for writing")
                if name in ("system", "popen", "remove", "unlink", "rmtree", "rmdir", "chmod", "chown", "kill", "fork", "execv", "execve", "spawn", "startfile"):
                    risky.append(f"{py.name}:{node.lineno} calls {name}()")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and PROTECTED_WORDS.search(node.value) and len(node.value) < 200:
                risky.append(f"{py.name}:{node.lineno} mentions a protected name ('{node.value[:50]}')")
    stdlib = set(sys.stdlib_module_names)
    third = sorted(i for i in imports if i not in stdlib and i not in local and i not in ("",))
    undeclared = [t for t in third if t.lower() not in declared]
    if undeclared:
        blockers.append(f"imports {undeclared} which are neither standard library nor declared in dependencies")
    facts["third_party_imports"] = third
    for label, mods, why in (("network", NETWORK_MODS, "the sandbox has no network, so this will fail at run time"),
                             ("process/low-level", PROCESS_MODS, "starting processes or low-level access is contained by the sandbox but is a red flag"),
                             ("dynamic code", DYNAMIC_MODS, "loads or builds code at run time, which static review cannot see"),
                             ("file", FILE_MODS, "files exist only in a throw-away copy inside the sandbox")):
        hit = sorted(imports & mods)
        if hit:
            (warnings if label != "file" else warnings).append(f"imports {label} module(s) {hit}: {why}")
    warnings += risky[:15]
    facts["imports"] = sorted(imports)
    h = code_hash(folder)
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings, "facts": facts, "hash": h, "manifest": man}


# ------------------------------------------------------------------ store
class Store:
    """All plugin state. Only the command line (and tests) call the mutating methods; no tool handler holds a Store."""

    def __init__(self, profile_dir: Path):
        self.root = Path(profile_dir) / "plugins"
        self.state_file = self.root / "state.json"

    # -- state
    def _load(self) -> dict:
        try:
            s = json.loads(self.state_file.read_text(encoding="utf-8"))
            return s if isinstance(s, dict) and isinstance(s.get("plugins"), dict) else {"plugins": {}}
        except (OSError, ValueError):
            return {"plugins": {}}

    def _save(self, s: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, self.state_file)

    def folder(self, name: str, version: str) -> Path:
        if not NAME_RE.match(name) or not VER_RE.match(version):
            raise PluginError("bad plugin name or version")
        return self.root / name / version

    def versions(self, name: str) -> list[str]:
        d = self.root / name
        return sorted((v.name for v in d.iterdir() if v.is_dir() and VER_RE.match(v.name)), key=lambda x: tuple(map(int, x.split(".")))) if d.is_dir() else []

    # -- install (copies; inactive)
    def install(self, source: str | Path) -> dict:
        src = Path(source)
        if not src.is_dir():
            raise PluginError(f"{src} is not a folder")
        rev = review(src)
        if rev["manifest"] is None:
            raise PluginError(rev["blockers"][0])
        man = rev["manifest"]
        dest = self.folder(man["name"], man["version"])
        if dest.exists():
            raise PluginError(f"{man['name']} {man['version']} is already installed; bump the version to install changed code (existing versions are never overwritten)")
        scan = _scan_folder(src)
        if scan:
            raise PluginError("not installed: " + "; ".join(scan))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, symlinks=False)
        rev = review(dest)               # review the COPY: that is what will run
        st = self._load()
        p = st["plugins"].setdefault(man["name"], {"active": None, "approved": {}, "history": [], "tests": {}})
        p.setdefault("tests", {})
        self._save(st)
        newer = set(man["permissions"])
        act = p.get("active")
        if act and act in p["approved"]:
            more = newer - set(p["approved"][act].get("permissions", []))
            if more:
                rev["warnings"].append(f"requests permission(s) {sorted(more)} that the active version {act} was not given; activating this version is a fresh decision")
        return rev

    def review_installed(self, name: str, version: str) -> dict:
        f = self.folder(name, version)
        if not f.is_dir():
            raise PluginError(f"{name} {version} is not installed")
        return review(f)

    # -- tests (recorded; the only source of a 'passed' label)
    def run_tests(self, name: str, version: str, stop=None) -> dict:
        f = self.folder(name, version)
        rev = review(f)
        man = rev["manifest"]
        if man is None:
            raise PluginError(rev["blockers"][0])
        results = []
        for i, t in enumerate(man["tests"], 1):
            out = execute(f, man, t.get("args", {}), stop=stop)
            ok = out["ran"] and (out["returncode"] == 0) == bool(t.get("expect_ok", True))
            if ok and "expect_contains" in t:
                ok = t["expect_contains"] in out["text"]
            results.append({"n": i, "ok": bool(ok), "note": out["problem"] or ("" if ok else "output did not match")})
        rec = {"hash": rev["hash"], "total": len(results), "passed": sum(r["ok"] for r in results), "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": results}
        st = self._load()
        st["plugins"].setdefault(name, {"active": None, "approved": {}, "history": [], "tests": {}}).setdefault("tests", {})[version] = rec
        self._save(st)
        return rec

    def test_status(self, name: str, version: str) -> str:
        man = None
        try:
            f = self.folder(name, version)
            h, man = code_hash(f), parse_manifest(json.loads((f / "manifest.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, PluginError):
            return "unknown (plugin unreadable)"
        if not man["tests"]:
            return "UNTESTED (the plugin declares no tests)"
        rec = self._load()["plugins"].get(name, {}).get("tests", {}).get(version)
        if not rec:
            return f"UNTESTED ({len(man['tests'])} test(s) declared, none run)"
        if rec["hash"] != h:
            return "STALE (the code changed since the last test run)"
        return f"{rec['passed']}/{rec['total']} declared tests passed in the sandbox (these are the author's tests: they show it ran as the author expected, not that it is correct or safe)"

    # -- activation (explicit; bound to a hash)
    def activate(self, name: str, version: str, hash_prefix: str, accept_warnings: bool = False, stop=None) -> dict:
        f = self.folder(name, version)
        if not f.is_dir():
            raise PluginError(f"{name} {version} is not installed")
        rev = review(f)
        if not rev["ok"]:
            raise PluginError("not activated; blockers: " + "; ".join(rev["blockers"]))
        if len(hash_prefix) < 12 or not rev["hash"].startswith(hash_prefix.lower()):
            raise PluginError(f"not activated: the code hash you gave does not match. Review it and pass the first 12+ characters of its hash ({rev['hash'][:12]}...)")
        if rev["warnings"] and not accept_warnings:
            raise PluginError("not activated: the review has warnings that you must accept explicitly (--accept-warnings): " + "; ".join(rev["warnings"][:8]))
        ok, why = SB.availability()
        if not ok:
            raise PluginError("not activated: plugins run only in the sandbox and no genuine sandbox exists here. " + why)
        man = rev["manifest"]
        if man["tests"]:
            rec = self.run_tests(name, version, stop=stop)
            if rec["passed"] != rec["total"]:
                raise PluginError(f"not activated: {rec['total'] - rec['passed']} of its own {rec['total']} declared tests failed in the sandbox")
        st = self._load()
        p = st["plugins"].setdefault(name, {"active": None, "approved": {}, "history": [], "tests": {}})
        p["approved"][version] = {"hash": rev["hash"], "permissions": man["permissions"], "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "warnings_accepted": bool(rev["warnings"])}
        if p.get("active") and p["active"] != version:
            p["history"].append(p["active"])
        p["active"] = version
        self._save(st)
        return rev

    def deactivate(self, name: str) -> None:
        st = self._load()
        p = st["plugins"].get(name)
        if not p or not p.get("active"):
            raise PluginError(f"{name} is not active")
        p["history"].append(p["active"])
        p["active"] = None
        self._save(st)

    def rollback(self, name: str) -> str:
        """Go back to the most recent EARLIER version that was approved and whose code still matches what was approved."""
        st = self._load()
        p = st["plugins"].get(name)
        if not p:
            raise PluginError(f"{name} is unknown")
        cur = p.get("active")
        for v in reversed(p.get("history", [])):
            if v == cur:
                continue
            appr = p["approved"].get(v)
            f = self.folder(name, v)
            if appr and f.is_dir() and code_hash(f) == appr["hash"]:
                if cur:
                    p["history"].append(cur)
                p["active"] = v
                self._save(st)
                return v
        raise PluginError(f"no earlier approved version of {name} whose code is unchanged; nothing was changed")

    def status(self) -> list[dict]:
        out = []
        st = self._load()["plugins"]
        for name in sorted(set(st) | ({d.name for d in self.root.iterdir() if d.is_dir()} if self.root.is_dir() else set())):
            p = st.get(name, {})
            out.append({"name": name, "versions": self.versions(name), "active": p.get("active"), "tests": {v: self.test_status(name, v) for v in self.versions(name)}})
        return out

    # -- what the executor gets
    def active_specs(self) -> tuple[list[ToolSpec], list[str]]:
        """ToolSpecs for active plugins whose code still matches what was approved. Anything else is skipped and reported (fail closed)."""
        specs, problems = [], []
        for name, p in sorted(self._load()["plugins"].items()):
            v = p.get("active")
            if not v:
                continue
            try:
                f = self.folder(name, v)
                appr = p["approved"][v]
                if code_hash(f) != appr["hash"]:
                    problems.append(f"{name} {v}: the code changed after you approved it; it is NOT loaded (review and activate again)")
                    continue
                man = parse_manifest(json.loads((f / "manifest.json").read_text(encoding="utf-8")))
                if set(man["permissions"]) - set(GRANTABLE):
                    problems.append(f"{name} {v}: asks for permissions that cannot be granted; NOT loaded")
                    continue
                specs.append(_spec_for(self, name, v, man, appr["hash"]))
            except (KeyError, OSError, ValueError, PluginError) as e:
                problems.append(f"{name}: not loaded ({type(e).__name__}: {e})")
        return specs, problems


# ------------------------------------------------------------------ running
def execute(folder: Path, man: dict, args: dict, stop=None, wall: float = 20.0) -> dict:
    """Run the plugin once in the sandbox. Never raises for a plugin failure; says exactly what happened."""
    res_ = {"ran": False, "returncode": None, "text": "", "problem": "", "killed_for": "", "seconds": 0.0}
    try:
        r = SB.run([SB.interpreter(), "-I", "-B", "/work/_jarvis_runner.py"], copy_from=Path(folder),
                   files={"_jarvis_runner.py": RUNNER, "_jarvis_call.json": json.dumps({"entry": man["entry"], "function": man["function"], "args": args})},
                   limits=SB.Limits(wall_seconds=wall, cpu_seconds=int(wall) + 5, memory_mb=512, output_bytes=100_000), stop=stop)
    except SB.SandboxUnavailable as e:
        res_["problem"] = f"no sandbox: {e}"
        return res_
    res_.update(ran=True, returncode=r.returncode, killed_for=r.killed_for, seconds=r.seconds)
    if r.killed_for:
        res_["problem"] = f"stopped ({r.killed_for}) after {r.seconds:.0f}s"
        res_["text"] = r.stdout[-2000:]
        return res_
    if r.returncode != 0:
        tail = (r.stderr or r.stdout)[-1500:]
        m = re.search(r"No module named '([^']+)'", tail)
        res_["problem"] = (f"a dependency is missing inside the sandbox: '{m.group(1)}'" if m else f"the plugin failed (exit code {r.returncode})")
        res_["text"] = tail
        return res_
    if MARK not in r.stdout:
        res_["problem"] = "the plugin ended without returning a result"
        res_["text"] = r.stdout[-1500:]
        return res_
    try:
        val = json.loads(r.stdout.rsplit(MARK, 1)[1].strip().splitlines()[0])["result"]
    except (ValueError, KeyError, IndexError):
        res_["problem"] = "the plugin's result was not valid JSON"
        return res_
    res_["text"] = val if isinstance(val, str) else json.dumps(val, indent=1, default=str)
    return res_


def _spec_for(store: Store, name: str, version: str, man: dict, approved_hash: str) -> ToolSpec:
    folder = store.folder(name, version)
    args = []
    for k, v in man["inputs"].items():
        t = v["type"]
        args.append(Arg(k, t, str(v.get("description", ""))[:200], required=v.get("required", True), default=v.get("default"),
                        max_len=int(min(v.get("max_len", 2000), 20_000)), min=v.get("min"), max=v.get("max"), no_newlines=False))

    def check_hash():
        if code_hash(folder) != approved_hash:
            raise PathRejected(f"plugin {name} {version}: the code changed since you approved it; nothing ran")
        ok, why = SB.availability()
        if not ok:
            raise PathRejected(f"plugin {name} cannot run without a genuine sandbox. {why}")

    def bind(a, env):
        check_hash()
        shown = json.dumps(a, default=str)[:300]
        return a, [f"runs plugin '{name}' {version} (code {approved_hash[:12]}) inside the sandbox: no network, no access to your files, 20 s limit",
                   f"arguments: {shown}", "the plugin's answer is its own claim: Jarvis does not verify it"], [approved_hash]

    def handler(a, env) -> Outcome:
        check_hash()
        r = execute(folder, man, a, stop=env.stop.is_set)
        if r["killed_for"] == "stop":
            env.stop.check()
        data = {"plugin": name, "version": version, "code_hash": approved_hash, "returncode": r["returncode"], "seconds": round(r["seconds"], 1), "killed_for": r["killed_for"]}
        if r["problem"]:
            return Outcome(False, f"plugin {name}: {r['problem']}", verified=None if r["killed_for"] else True,
                           evidence="" if r["killed_for"] else "the plugin was run in the sandbox and failed", data=data, untrusted_text=r["text"])
        return Outcome(True, f"plugin {name} returned a result (the plugin's own claim; not verified by Jarvis)", verified=None, evidence="", data=data, untrusted_text=r["text"][:20_000])

    return ToolSpec(f"plugin_{name}", f"PLUGIN {name} {version}: {man['description']} [{store.test_status(name, version)}]", "plugins", S.EXECUTE, tuple(args), handler,
                    timeout=60, untrusted_output=True, bind=bind)
