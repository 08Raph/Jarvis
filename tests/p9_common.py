"""Shared helpers for the Prompt 9 tests (not a test module).

LABELS: REAL = real files, the real executor/policy/confirmation/audit, the real mission store and scheduler logic (with an injected clock).
TEST TOOLS = tiny tools registered ONLY in tests (counters, controlled failures/timeouts) so retries, timeouts and ambiguity can be exercised exactly;
they go through the real executor like any tool. FAKE MODEL = scripted replies. Nothing here ran on Windows or with the real Qwen model."""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from p8_common import P8, ScriptedModel, call  # noqa: E402,F401
from jarvis.desktop import schema as S          # noqa: E402
from jarvis.desktop.executor import Executor    # noqa: E402
from jarvis.desktop.schema import Arg, Outcome, ToolSpec  # noqa: E402
from jarvis.extensions import all_specs         # noqa: E402
from jarvis.missions import model as M          # noqa: E402
from jarvis.missions.runner import MissionRunner  # noqa: E402
from jarvis.missions.schedule import Scheduler  # noqa: E402
from jarvis.missions.store import MissionStore  # noqa: E402


class Crash(BaseException):
    """Simulates the whole process dying in the middle of a tool (not catchable by 'except Exception')."""


class Clock:
    """Fake wall clock (aware UTC)."""
    def __init__(self, iso="2026-06-01T12:00:00Z"):
        self.t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def set(self, iso):
        self.t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def add(self, **kw):
        from datetime import timedelta
        self.t += timedelta(**kw)


class Counters(dict):
    def __missing__(self, k):
        return 0


def test_tools(env_hooks):
    """TEST TOOLS. `env_hooks` is a dict the test fills: callables per tool name."""
    calls = Counters()

    def mk(name, risk, handler, timeout=5.0, args=()):
        def h(a, env):
            calls[name] += 1
            return handler(a, env, calls[name])
        return ToolSpec(name, f"test tool {name}", "system", risk, args, h, timeout=timeout)

    def ok_(a, env, n):
        return Outcome(True, f"ok #{n}", verified=True, evidence="test evidence")

    def flaky(a, env, n):
        if env_hooks.get("on_call"):
            env_hooks["on_call"](env, n)
        if n <= env_hooks.get("fail_times", 1):
            raise RuntimeError("temporary failure")
        return Outcome(True, f"worked on try {n}", verified=True, evidence="test evidence")

    def always_fail(a, env, n):
        raise RuntimeError("permanent failure")

    def unverified(a, env, n):
        return Outcome(True, "did something", verified=None)

    def slow(a, env, n):
        time.sleep(env_hooks.get("sleep", 1.0))
        return Outcome(True, "late", verified=True, evidence="e")

    def crash(a, env, n):
        raise Crash()

    def hook(a, env, n):
        f = env_hooks.get("hook")
        if f:
            f(env, n)
        return Outcome(True, f"hook #{n}", verified=True, evidence="e")

    def leak(a, env, n):
        return Outcome(True, "key ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", verified=True, evidence="e",
                       data={"x": 1}, untrusted_text="SCREEN-CONTENT-SHOULD-NOT-BE-STORED password hunter2")

    def state(a, env, n):
        return Outcome(True, f"state={env_hooks.get('state', 'off')}", verified=True, evidence="read state", data={"state": env_hooks.get("state", "off")})

    def effect_then_crash(a, env, n):
        env_hooks["effects"] = env_hooks.get("effects", 0) + 1       # the side effect HAPPENED ...
        raise Crash()                                                # ... and the process died before anything was recorded

    return calls, [mk("t_net", S.NETWORK, flaky), mk("t_exec", S.EXECUTE, flaky), mk("t_input", S.INPUT, flaky), mk("t_destroy", S.DESTRUCTIVE, flaky),
                   mk("t_effect_crash", S.MODIFY, effect_then_crash), mk("t_ok", S.OBSERVE, ok_), mk("t_flaky_obs", S.OBSERVE, flaky), mk("t_flaky_mod", S.MODIFY, flaky), mk("t_fail_mod", S.MODIFY, always_fail),
                   mk("t_unverified", S.MODIFY, unverified), mk("t_slow_obs", S.OBSERVE, slow, timeout=0.3), mk("t_slow_mod", S.MODIFY, slow, timeout=0.3),
                   mk("t_crash", S.MODIFY, crash), mk("t_hook", S.MODIFY, hook), mk("t_leak", S.MODIFY, leak), mk("t_state", S.OBSERVE, state),
                   mk("t_fail_obs", S.OBSERVE, always_fail)]


class P9(P8):
    def contract(self, *names):
        """SIMULATED trusted registration: declare these TEST tools idempotent in jarvis.desktop.retry for the length of one test (name AND handler are
        taken from the registered test tool, as a real entry would be). The policy still checks the tool's risk and metadata."""
        from types import MappingProxyType
        from unittest import mock
        from jarvis.desktop import retry
        specs = {**{sp.name: sp for sp in self.extra}, **self.ex.specs}          # the spec as registered NOW (a test may have wrapped a handler)
        ids = {n: retry.handler_id(specs[n]) if n in specs else ("tests", n) for n in names}
        p1 = mock.patch.object(retry, "IDEMPOTENT_TOOLS", MappingProxyType({**retry.IDEMPOTENT_TOOLS, **{n: "test tool declared idempotent" for n in names}}))
        p2 = mock.patch.object(retry, "AUDITED_HANDLERS", MappingProxyType({**retry.AUDITED_HANDLERS, **ids}))
        for pt in (p1, p2):
            pt.start()
            self.addCleanup(pt.stop)

    def make9(self, enable=("files_read", "files_write", "system"), consent=(), backend=None, **kw):
        self.hooks = {}
        self.calls, self.extra = test_tools(self.hooks)
        self.make8(enable=enable, consent=consent, backend=backend, **kw)
        self.reload9()
        self.store = MissionStore(self.profile)
        self.runner = MissionRunner(self.ex, self.store, self.ctx, clock=time.monotonic)
        return self.ex

    def reload9(self):
        self.ex = Executor(self.env, self.audit, self._confirm, isolation="inline", extra_specs=all_specs(self.profile) + self.extra)
        return self.ex

    def new_runner(self):
        """A 'restarted' Jarvis: new executor, new store object, new runner (same files)."""
        self.reload9()
        self.store = MissionStore(self.profile)
        self.runner = MissionRunner(self.ex, self.store, self.ctx)
        return self.runner

    def mission(self, steps, goal="test goal", approve=True, **limits):
        plan = {"goal": goal, "steps": steps}
        if limits:
            plan["limits"] = limits
        d = M.normalize(plan, self.ex.specs)
        m = M.new_mission(d)
        self.store.save(m)
        if approve and self.runner.is_consequential(m):
            self.approve(m["id"])
        return self.store.load(m["id"])

    def approve(self, mid):
        m = self.store.load(mid)
        m["approval"] = {"at": M.now(), "plan_hash": m["plan_hash"], "uses_left": 1, "by": "user"}
        M.set_state(m, "approved", "approved")
        self.store.save(m)

    def run_m(self, mid, **kw):
        return self.runner.run(mid, **kw)

    def st(self, m, sid):
        return M.current_execution(m)["steps"][sid]["status"]
