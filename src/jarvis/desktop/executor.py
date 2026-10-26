"""The tool executor: the only code that turns a requested tool call into an action. It is separate from the model.

For every call: emergency-stop check -> known tool? -> strict argument validation -> platform check -> least-privilege check
(refuses to change anything while running as administrator) -> permission policy -> explicit confirmation when required ->
audit "start" -> run with a timeout (in a separate worker process by default on Windows, so a hung or crashed tool can be
killed) -> audit "end". The status reflects what was OBSERVED, never what was intended."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from jarvis.desktop import schema as S
from jarvis.desktop import tools as T
from jarvis.desktop.audit import AuditLog
from jarvis.desktop.paths import PathMissing, PathRejected
from jarvis.desktop.policy import Context, Decision, decide
from jarvis.desktop.stop import Stopped

STATUSES = ("verified", "unverified", "failed", "denied", "declined", "invalid", "unsupported", "timeout", "stopped")


@dataclass
class ConfirmRequest:
    """What the user is asked to approve. `args` is a private COPY of the exact validated arguments that will run (editing it
    changes nothing); `fingerprint` identifies this exact action, its risk and the state of the files it touches."""
    tool: str
    risk: str
    category: str
    description: str
    args: dict
    fingerprint: str = ""


@dataclass(frozen=True)
class Approval:
    """An answer bound to one specific action. A confirmation callback may return plain True, or Approval(True, req.fingerprint).
    Anything else (strings, numbers, dicts, None, an Approval for another fingerprint) is a refusal."""
    approved: bool
    fingerprint: str


def _check_approval(answer, fingerprint: str) -> tuple[bool, str]:
    """(approved, why_not). Only the exact boolean True, or an Approval for THIS fingerprint, approves."""
    if answer is True:
        return True, ""
    if isinstance(answer, Approval):
        if answer.approved is True and isinstance(answer.fingerprint, str) and hmac.compare_digest(answer.fingerprint, fingerprint):
            return True, ""
        return False, "the approval was not for this exact action"
    if answer is False or answer is None:
        return False, ""
    return False, "the approval answer was malformed"


@dataclass
class StepResult:
    tool: str
    args: dict
    status: str
    risk: str = ""
    message: str = ""
    outcome: S.Outcome | None = None
    seconds: float = 0.0
    confirmed: bool | None = None
    started: bool = False            # True once the action itself was begun; after that a stop/timeout means "result unknown"

    @property
    def done(self) -> bool:
        return self.status in ("verified", "unverified")

    def to_dict(self) -> dict:
        from jarvis.desktop.audit import scrub_args
        return {"tool": self.tool, "args": scrub_args(self.args), "status": self.status, "risk": self.risk, "message": self.message,
                "seconds": round(self.seconds, 3), "confirmed": self.confirmed, "started": self.started,
                "outcome": ({k: v for k, v in self.outcome.to_dict().items() if k != "untrusted_text"} if self.outcome else None)}


def _escape(s: str) -> str:
    """Make invisible, control and direction-override characters visible, so shown text cannot disguise what it is."""
    out = []
    for ch in s:
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _show(v) -> str:
    if isinstance(v, str):
        if len(v) > 160:
            digest = hashlib.sha256(v.encode("utf-8", "surrogatepass")).hexdigest()[:16]
            return _escape(v[:160]) + f"... ({len(v)} characters in all, sha256 {digest})"
        return _escape(v)
    if isinstance(v, list):
        return "[" + ", ".join(_show(x) for x in v) + "]"
    return str(v)


def describe(spec: S.ToolSpec, bound: "T.BoundAction") -> str:
    """The text the user approves. It is built from the SAME bound arguments that will execute (real resolved paths, the
    real destination, the real risk), never from the model's raw wording."""
    lines = [f"{spec.name}: {spec.description}", f"risk: {bound.risk}"]
    for k, v in bound.args.items():
        if v is None or v == "" or v == []:
            continue
        lines.append(f"  {k} = {_show(v)}")
    for eff in bound.effects:
        lines.append("  -> " + _escape(eff))
    if bound.risk == S.DESTRUCTIVE:
        lines.append("This changes or removes something that exists. Jarvis keeps a backup where it can (undo with `jarvis desktop undo ID`).")
    if bound.risk == S.EXECUTE:
        lines.append("This RUNS code. It runs in the Jarvis sandbox (no network, no access to your files outside a private copy) and is "
                     "stopped at its time limit. Jarvis refuses to run it if the sandbox is not available.")
    if bound.risk == S.NETWORK:
        lines.append("This contacts the internet. Only what is shown above is sent. Jarvis never sends your files, code or logs.")
    if bound.risk == S.INSTALL:
        lines.append("This opens an installer. Windows may ask for administrator permission; Jarvis never answers that prompt for you.")
    return "\n".join(lines)


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "_desktop-worker"]
    return [sys.executable, "-m", "jarvis", "_desktop-worker"]


class Executor:
    def __init__(self, env: T.Env, audit: AuditLog, confirm: Callable[[ConfirmRequest], bool] | None,
                 isolation: str | None = None, data_dir=None, profile: str | None = None, extra_specs=None):
        self.env, self.audit, self.confirm_cb = env, audit, confirm
        if isolation is None:
            isolation = "process" if sys.platform == "win32" and not env.backend.simulated else "inline"
        if isolation == "process" and env.backend.simulated:
            raise ValueError("the simulated desktop lives in this process; use isolation='inline'")
        self.isolation = isolation
        self.data_dir, self.profile = data_dir, profile
        self.specs = T.registry()
        self._builtin = set(self.specs)
        for spec in (extra_specs or {}).values() if isinstance(extra_specs, dict) else (extra_specs or ()):
            self._add_spec(spec)
        self._proc: subprocess.Popen | None = None
        self.env.ext["specs"] = self.specs        # read-only view for mode_info

    def _mode_block(self, spec) -> str | None:
        from jarvis import modes
        return modes.blocked(self.env.profile_dir, spec)

    def _add_spec(self, spec: S.ToolSpec) -> None:
        """Tools from extensions go through exactly the same validation, policy, confirmation, audit and timeout path. They may not
        replace a built-in tool, use a permission category that does not exist, or use an unknown risk."""
        from jarvis.desktop.policy import CATEGORIES
        if spec.name in self.specs:
            raise ValueError(f"a tool called '{spec.name}' already exists; extensions cannot replace tools")
        if spec.category not in CATEGORIES:
            raise ValueError(f"tool '{spec.name}' uses an unknown permission category '{spec.category}'")
        if spec.risk not in S.RISKS or spec.risk in (S.ADMIN, S.EXTERNAL):
            raise ValueError(f"tool '{spec.name}' declares a risk that extensions may not use ({spec.risk})")
        if not callable(spec.handler):
            raise ValueError(f"tool '{spec.name}' has no handler")
        self.specs[spec.name] = spec

    # ------------------------------------------------------------------ public
    def preview(self, name: str, args: dict, ctx: Context) -> dict:
        """What WOULD happen, without doing it: validation, risk, permission and whether confirmation is needed. The
        description is exactly what a confirmation would show."""
        spec = self.specs.get(name)
        if spec is None:
            return {"tool": name, "ok": False, "problem": "unknown tool"}
        why = self._mode_block(spec)
        if why:
            return {"tool": name, "ok": False, "problem": why}
        try:
            a = spec.validate(args)
        except S.ArgError as e:
            return {"tool": name, "ok": False, "problem": str(e)}
        try:
            bound = T.bind_action(spec, a, self.env)
        except PathRejected as e:
            return {"tool": name, "ok": False, "risk": T.risk_of(spec, a, self.env), "problem": str(e)}
        d = self._decision(spec, bound.args, bound.risk, ctx, probe=True)
        return {"tool": name, "ok": d.allowed, "risk": bound.risk, "needs_confirmation": d.confirm, "problem": d.reason,
                "description": describe(spec, bound), "fingerprint": bound.fingerprint}

    def run(self, name: str, args: dict | None, ctx: Context) -> StepResult:
        t0 = time.monotonic()
        a0 = args if isinstance(args, dict) else {"__not_an_object__": args}
        try:
            res = self._run(name, a0, ctx)
        except Exception as e:  # noqa: BLE001 - run() never raises: an unexpected error BEFORE the action starts means nothing was done
            res = StepResult(name, a0, "failed", message=f"internal error before the action started ({type(e).__name__}: {e}); "
                                                          "nothing was done")
        res.seconds = time.monotonic() - t0
        try:
            self.audit.write(event="end", tool=res.tool, args=res.args, status=res.status, risk=res.risk, summary=res.message,
                             verified=res.outcome.verified if res.outcome else None,
                             evidence=res.outcome.evidence if res.outcome else "", checkpoint=res.outcome.checkpoint if res.outcome else None,
                             backend=self.env.backend.name, seconds=round(res.seconds, 3))
        except Exception as e:  # noqa: BLE001 - the result is still reported, but the gap is stated, never hidden
            res.message += f" (WARNING: the audit log could not be written: {type(e).__name__})"
        return res

    def kill_running(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            p.kill()

    # ------------------------------------------------------------------ internals
    def _decision(self, spec, a, risk, ctx, probe=False) -> Decision:
        if risk != S.OBSERVE:
            try:
                elevated = self.env.backend.elevated()
            except Exception:  # noqa: BLE001 - cannot tell: assume the worst
                elevated = True
            if elevated:
                return Decision(False, False, "Jarvis is running as administrator (or could not tell that it is not). To keep "
                                              "least privilege it will only observe; start Jarvis normally (not 'Run as "
                                              "administrator') to let it act.")
        if spec.category in ("input", "ui"):
            try:
                ctx.running_processes = self.env.backend.process_names()
            except Exception:  # noqa: BLE001 - an empty list makes the policy refuse input/UI actions
                ctx.running_processes = set()
        if T.needs_target(spec):
            w = T.target_window(self.env, a)
            ctx.target_process, ctx.target_title = w.get("process"), w.get("title")
        else:
            ctx.target_process = ctx.target_title = None
        if spec.name == "launch_app":
            try:
                ctx.app_id = T._resolve_app(self.env, a)["id"]
            except Exception:  # noqa: BLE001
                ctx.app_id = None
        return decide(spec, risk, self.env.settings, ctx)

    def _run(self, name, args, ctx) -> StepResult:
        try:
            self.env.stop.check()
        except Stopped as e:
            return StepResult(name, args, "stopped", message=str(e))
        spec = self.specs.get(name)
        if spec is None:
            return StepResult(name, args, "invalid", message=f"there is no tool called '{name}'")
        why = self._mode_block(spec)
        if why:
            return StepResult(name, args, "denied", message=why)
        try:
            a = spec.validate(args)
        except S.ArgError as e:
            return StepResult(name, args, "invalid", message=str(e))
        if spec.windows_only and sys.platform != "win32":
            return StepResult(name, a, "unsupported", message=f"{name} only works on Windows")
        risk = T.risk_of(spec, a, self.env)
        d = self._decision(spec, a, risk, ctx)
        if not d.allowed:
            return StepResult(name, a, "denied", risk, d.reason)
        # Pin the action: from here on ONE set of arguments (real resolved paths, real destination, real risk) is shown,
        # approved and executed.
        try:
            bound = T.bind_action(spec, a, self.env)
        except PathMissing as e:
            return StepResult(name, a, "failed", risk, str(e))
        except PathRejected as e:
            return StepResult(name, a, "denied", risk, str(e))
        a = bound.args
        if bound.risk != risk or T.needs_target(spec):
            # the target window is now pinned: judge the policy again against the window that will really be used
            risk = bound.risk
            d = self._decision(spec, a, risk, ctx)
            if not d.allowed:
                return StepResult(name, a, "denied", risk, d.reason)
        confirmed = None
        if d.confirm:
            req = ConfirmRequest(name, risk, spec.category, describe(spec, bound), copy.deepcopy(a), bound.fingerprint)
            self.audit.write(event="confirm_asked", tool=name, args=a, risk=risk, fingerprint=bound.fingerprint)
            try:
                answer = self.confirm_cb(req) if self.confirm_cb else False
            except Exception as e:  # noqa: BLE001 - a broken prompt is a refusal, never an approval
                self.audit.write(event="confirm_answer", tool=name, approved=False, problem=f"confirmation failed: {type(e).__name__}")
                return StepResult(name, a, "declined", risk, f"the confirmation step failed ({type(e).__name__}), so nothing was done",
                                  confirmed=False)
            confirmed, why = _check_approval(answer, bound.fingerprint)
            self.audit.write(event="confirm_answer", tool=name, approved=confirmed, problem=why)
            if not confirmed:
                return StepResult(name, a, "declined", risk,
                                  "you did not confirm, so nothing was done" + (f" ({why})" if why else ""), confirmed=False)
            if risk == S.INPUT:
                ctx.input_approved_for.add((ctx.target_process or "?").lower())
        try:
            self.env.stop.check()                 # the user may have pressed stop while the question was open
        except Stopped as e:
            return StepResult(name, a, "stopped", risk, str(e), confirmed=confirmed)
        # Look again right before running: if anything about the action changed since it was shown (target, destination,
        # contents, risk), nothing runs and a fresh approval is required.
        try:
            again = T.bind_action(spec, a, self.env)
        except PathRejected as e:
            return StepResult(name, a, "denied", risk, f"the target changed before it could run ({e}); nothing was done",
                              confirmed=confirmed)
        if again.fingerprint != bound.fingerprint:
            return StepResult(name, a, "denied", risk,
                              "the action changed after it was checked (its target, contents or risk are no longer what was shown); "
                              "nothing was done. Ask again to get a fresh approval.", confirmed=confirmed)
        self.audit.write(event="start", tool=name, args=a, risk=risk, isolation=self.isolation, fingerprint=bound.fingerprint)
        try:
            out = self._execute(spec, a, bound)
        except _Timeout as e:
            return StepResult(name, a, "timeout", risk, str(e), confirmed=confirmed, started=True)
        except Stopped as e:
            return StepResult(name, a, "stopped", risk, f"{e}; the action was interrupted and its result is unknown",
                              confirmed=confirmed, started=True)
        except PathMissing as e:
            return StepResult(name, a, "failed", risk, str(e), confirmed=confirmed, started=True)
        except PathRejected as e:
            return StepResult(name, a, "denied", risk, str(e), confirmed=confirmed, started=True)
        except PermissionError as e:
            return StepResult(name, a, "denied", risk, str(e), confirmed=confirmed, started=True)
        except S.ArgError as e:
            return StepResult(name, a, "invalid", risk, str(e), confirmed=confirmed, started=True)
        except Exception as e:  # noqa: BLE001 - every real failure is reported as such
            from jarvis.desktop.backend import Unsupported
            st = "unsupported" if isinstance(e, Unsupported) else "failed"
            return StepResult(name, a, st, risk, f"{type(e).__name__}: {e}", confirmed=confirmed, started=True)
        if ctx.agent and spec.untrusted_output and out.untrusted_text:
            ctx.tainted = True        # the model has now seen outside text: from here on every change is confirmed
        note = ""
        if not out.ok or out.verified is False:
            status = "failed"
        elif out.verified is True and (out.evidence or "").strip():
            status = "verified"       # only an executor-side check that recorded its evidence counts
        else:
            status = "unverified"
            if out.verified is True:
                note = " (the tool reported success but recorded no evidence, so it is not counted as verified)"
        return StepResult(name, a, status, risk, out.summary + note, out, confirmed=confirmed, started=True)

    def _execute(self, spec: S.ToolSpec, a: dict, bound=None) -> S.Outcome:
        # The isolated worker only knows the built-in desktop tools. Extension tools run in this process on a thread, but
        # anything that executes code does so in the sandbox (a separate, killable process tree) and polls the emergency stop.
        if self.isolation == "process" and spec.name in self._builtin:
            return self._in_worker(spec, a, bound)
        box: dict = {}

        def target():
            try:
                box["out"] = spec.handler(a, self.env)
            except BaseException as e:  # noqa: BLE001
                box["err"] = e
        th = threading.Thread(target=target, name=f"tool-{spec.name}", daemon=True)
        th.start()
        end = time.monotonic() + spec.timeout
        while th.is_alive():
            th.join(0.05)
            if self.env.stop.is_set() and th.is_alive():
                raise Stopped(self.env.stop.reason or "stopped")
            if time.monotonic() > end and th.is_alive():
                raise _Timeout(f"{spec.name} did not finish within {spec.timeout:.0f}s; it may still complete in the background, "
                               "so its result is UNKNOWN - check before retrying")
        if "err" in box:
            raise box["err"]
        return box["out"]

    def _in_worker(self, spec, a, bound=None) -> S.Outcome:
        req = {"tool": spec.name, "args": a, "settings": self.env.settings.to_dict(), "profile_dir": str(self.env.profile_dir),
               "protected": [str(x) for x in self.env.protected], "args_digest": bound.args_digest if bound else T.args_digest(spec.name, a),
               "allowlist": self.env.app_allowlist, "guest": self.env.guest,
               "stop_file": str(self.env.stop.stop_file) if self.env.stop.stop_file else None}
        envv = dict(os.environ)
        if not getattr(sys, "frozen", False):
            src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            envv["PYTHONPATH"] = src + os.pathsep + envv.get("PYTHONPATH", "")
        p = subprocess.Popen(worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=envv,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._proc = p
        out_box: dict = {}

        def comm():
            try:
                out_box["o"], out_box["e"] = p.communicate(json.dumps(req).encode("utf-8"))
            except Exception as e:  # noqa: BLE001
                out_box["x"] = e
        th = threading.Thread(target=comm, daemon=True)
        th.start()
        end = time.monotonic() + spec.timeout
        try:
            while th.is_alive():
                th.join(0.05)
                if self.env.stop.is_set():
                    p.kill()
                    raise Stopped(self.env.stop.reason or "stopped")
                if time.monotonic() > end:
                    p.kill()
                    raise _Timeout(f"{spec.name} did not finish within {spec.timeout:.0f}s; the worker was stopped. Part of the "
                                   "action may already have happened - check before retrying")
        finally:
            self._proc = None
        if "x" in out_box:
            raise RuntimeError(f"worker communication failed: {out_box['x']}")
        raw = (out_box.get("o") or b"").decode("utf-8", "replace").strip().splitlines()
        if not raw:
            raise RuntimeError(f"tool worker exited with code {p.returncode} and no result: {(out_box.get('e') or b'').decode('utf-8','replace')[-300:]}")
        msg = json.loads(raw[-1])
        if "error" in msg:
            kind, text = msg["error"]["type"], msg["error"]["message"]
            exc = {"PathRejected": PathRejected, "PathMissing": PathMissing, "PermissionError": PermissionError, "ArgError": S.ArgError,
                   "Unsupported": __import__("jarvis.desktop.backend", fromlist=["Unsupported"]).Unsupported}.get(kind)
            raise (exc(text) if exc else RuntimeError(f"{kind}: {text}"))
        return S.Outcome.from_dict(msg["outcome"])


class _Timeout(Exception):
    pass


def worker_main() -> int:
    """Entry point of the isolated tool process: one request on stdin, one JSON result on stdout. It re-checks the tool name,
    the arguments and that the permission category is on (defence in depth); confirmation already happened in the parent."""
    from pathlib import Path
    from jarvis.desktop.backend import default_backend
    from jarvis.desktop.policy import Settings
    from jarvis.desktop.stop import EmergencyStop
    try:
        req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        spec = T.registry().get(req.get("tool"))
        if spec is None:
            raise S.ArgError("unknown tool")
        a = spec.validate(req.get("args"))
        extra = tuple(str(x) for x in (req.get("protected") or []) if isinstance(x, str))
        from jarvis.desktop.paths import default_protected
        settings = Settings.from_dict(req.get("settings"), protected=[*default_protected(Path(req["profile_dir"])), *extra])
        if not settings.enabled.get(spec.category):
            raise PermissionError(f"permission '{spec.category}' is off")
        env = T.Env(default_backend(), settings, Path(req["profile_dir"]),
                    EmergencyStop(Path(req["stop_file"]) if req.get("stop_file") else None),
                    app_allowlist=req.get("allowlist") or {}, guest=bool(req.get("guest")), extra_protected=extra)
        # Defence in depth: the worker re-pins the action itself and runs only if it is exactly what the parent approved.
        bound = T.bind_action(spec, a, env)
        digest = req.get("args_digest")
        if (not isinstance(digest, str) or not digest or not hmac.compare_digest(digest, bound.args_digest)
                or bound.args != a):
            raise PermissionError("the action does not match what was approved (digest mismatch); nothing was done")
        out = spec.handler(bound.args, env)
        sys.stdout.write(json.dumps({"outcome": out.to_dict()}, default=str) + "\n")
        return 0
    except BaseException as e:  # noqa: BLE001
        sys.stdout.write(json.dumps({"error": {"type": type(e).__name__, "message": str(e)}}) + "\n")
        return 1
