"""Shared helpers for the Prompt 8 tests (not a test module).

LABELS: REAL = real files/processes/OCR engine/OS counters of THIS Linux machine. MOCKED/FAKE SOURCE = a stand-in replaces a data source
(a fake metrics source, a fake screen, a scripted model). Those prove Jarvis' decisions and honesty, not that Windows, a GPU, a game, a real
vision model or Qwen behave that way. Nothing was run on Windows, on a GPU or with the real model."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from p7_common import P7, ScriptedModel, call, needs_sandbox, SANDBOX_OK  # noqa: E402,F401
from jarvis.desktop.executor import Executor                              # noqa: E402
from jarvis.extensions import all_specs                                    # noqa: E402

NEW_CATS = ("monitoring", "gaming", "screen")


class P8(P7):
    def make8(self, enable=("files_read", "monitoring", "gaming", "screen", "windows"), consent=("monitoring", "gaming"), backend=None, **kw):
        self.make7(enable=enable, plugins=False, **kw) if backend is None else None
        if backend is not None:
            self.make(backend=backend, enable=enable, project_folders=[], **kw)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for c in consent:
            self.env.settings.consents[c] = stamp
        self.ex = Executor(self.env, self.audit, self._confirm, isolation="inline", extra_specs=all_specs(self.profile))
        return self.ex

    def reload8(self):
        self.ex = Executor(self.env, self.audit, self._confirm, isolation="inline", extra_specs=all_specs(self.profile))
        return self.ex


class FakeSource:
    """FAKE metrics source with the same methods as monitor.system.RealSource."""
    name = "fake source"

    def __init__(self, **over):
        self.cpu = over.get("cpu", (37.5, "fake /proc/stat"))
        self.mem = over.get("mem", {"available": True, "total_gb": 16.0, "available_gb": 10.0, "used_percent": 37.5, "source": "fake meminfo"})
        self.disks_ = over.get("disks", {"available": True, "drives": [{"drive": "C:\\", "total_gb": 500.0, "free_gb": 120.5, "used_percent": 75.9}], "source": "fake"})
        self.gpu_ = over.get("gpu")
        self.temps = over.get("temps")
        self.procs = over.get("procs")
        self.own_ = over.get("own")
        self.calls = 0

    def cpu_usage(self, interval=0.3):
        self.calls += 1
        if isinstance(self.cpu, Exception):
            raise self.cpu
        return self.cpu

    def cpu_info(self):
        return {"logical": 8, "name": "Fake CPU 9000", "mhz": 3600.0}

    def memory(self):
        if isinstance(self.mem, Exception):
            raise self.mem
        return self.mem

    def disks(self):
        if isinstance(self.disks_, Exception):
            raise self.disks_
        return self.disks_

    def gpu(self):
        from jarvis.monitor.system import Unavailable
        if self.gpu_ is None:
            raise Unavailable("no GPU information source found (fake)")
        return self.gpu_

    def temperatures(self):
        from jarvis.monitor.system import Unavailable
        if self.temps is None:
            raise Unavailable("no sensors (fake)")
        return self.temps

    def processes(self, limit, sort):
        from jarvis.monitor.system import Unavailable
        if self.procs is None:
            raise Unavailable("needs psutil (fake)")
        if isinstance(self.procs, Exception):
            raise self.procs
        return list(self.procs)[:limit]

    def own(self):
        from jarvis.monitor.system import Unavailable
        if self.own_ is None:
            raise Unavailable("no source (fake)")
        return self.own_
