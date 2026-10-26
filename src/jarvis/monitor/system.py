"""Read-only system monitoring.

Sources, in order: the operating system's own counters (through `desktop/sysinfo.py`, no extra software), then the optional `psutil` package
(per-process data, temperatures where the OS reports them). `nvidia-smi` (NVIDIA driver) is the only GPU usage/temperature source. If none of
them can answer, the reading is 'unavailable' with the reason; nothing is estimated or invented.

This module can only READ. It has no function that ends a process, changes a service or a startup entry, or writes a setting.
It does not poll in the background: it measures when asked, and repeated asks inside the minimum interval get the cached reading, marked as cached."""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

from jarvis.desktop import sysinfo
from jarvis.monitor.readings import Reading, checked, na, now_iso, ok

PARTS = ("cpu", "memory", "disk", "gpu", "temperature")
MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL = 1.0, 300.0, 5.0
MAX_WATCH_SAMPLES, MAX_WATCH_SECONDS = 60, 300.0
MAX_PROCESSES = 25
CONFIG_FILE = "monitoring.json"


class Unavailable(Exception):
    """The source cannot answer; the message says why (shown to the user)."""


# ------------------------------------------------------------------ configuration (written by the command line only)
class MonitorConfig:
    def __init__(self, interval_s: float = DEFAULT_INTERVAL, parts=PARTS):
        self.interval_s = interval_s
        self.parts = tuple(parts)

    @classmethod
    def load(cls, profile_dir: Path | None) -> "MonitorConfig":
        try:
            raw = json.loads((Path(profile_dir) / CONFIG_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        iv = raw.get("interval_s")
        iv = float(iv) if isinstance(iv, (int, float)) and not isinstance(iv, bool) and math.isfinite(iv) else DEFAULT_INTERVAL
        parts = [p for p in raw.get("parts", []) if p in PARTS] if isinstance(raw.get("parts"), list) else list(PARTS)
        return cls(min(max(iv, MIN_INTERVAL), MAX_INTERVAL), parts)

    def save(self, profile_dir: Path) -> None:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        (Path(profile_dir) / CONFIG_FILE).write_text(json.dumps({"interval_s": self.interval_s, "parts": list(self.parts)}), encoding="utf-8")


# ------------------------------------------------------------------ the real source
class RealSource:
    name = "this PC"

    def __init__(self):
        try:
            import psutil
            self.psutil = psutil
        except ImportError:
            self.psutil = None

    def cpu_usage(self, interval: float = 0.3) -> tuple[float, str]:
        r = sysinfo.cpu_usage(interval)
        if not r.get("available"):
            raise Unavailable(r.get("why", "no CPU usage source"))
        return r["value"], r["source"]

    def cpu_info(self) -> dict:
        info = {"logical": os.cpu_count(), "name": None, "mhz": None}
        try:
            info["name"] = sysinfo.cpu().get("name")
        except Exception:  # noqa: BLE001
            pass
        if self.psutil:
            try:
                f = self.psutil.cpu_freq()
                info["mhz"] = f.current if f and f.current else None
            except Exception:  # noqa: BLE001
                pass
        return info

    def memory(self) -> dict:
        r = sysinfo.memory()
        if not r.get("available"):
            raise Unavailable(r.get("why", "no memory source"))
        return r

    def disks(self) -> dict:
        r = sysinfo.storage()
        if not r.get("available"):
            raise Unavailable("no fixed drives could be listed")
        return r

    def gpu(self) -> dict:
        r = sysinfo.gpu()
        if not r.get("available"):
            raise Unavailable(r.get("why", "no GPU source"))
        return r

    def temperatures(self) -> list[tuple[str, float]]:
        if not self.psutil:
            raise Unavailable("temperatures need the optional 'psutil' package, and even with it Windows does not expose CPU temperatures through a supported counter; "
                              "a vendor tool (for example HWiNFO or LibreHardwareMonitor) is required")
        fn = getattr(self.psutil, "sensors_temperatures", None)
        if fn is None:
            raise Unavailable("this platform/psutil does not report temperatures (psutil provides them on Linux and FreeBSD only)")
        try:
            data = fn()
        except Exception as e:  # noqa: BLE001
            raise Unavailable(f"the temperature sensors could not be read ({type(e).__name__})") from e
        out = [(f"{chip}/{(t.label or 'sensor')}", t.current) for chip, ts in (data or {}).items() for t in ts]
        if not out:
            raise Unavailable("the operating system reports no temperature sensors on this PC")
        return out

    def processes(self, limit: int, sort: str) -> list[dict]:
        ps = self.psutil
        if not ps:
            raise Unavailable("process information needs the optional 'psutil' package (pip install psutil)")
        procs = []
        for p in ps.process_iter(["pid", "name"]):
            try:
                p.cpu_percent(None)
                procs.append(p)
            except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess):
                continue
        time.sleep(0.3)
        rows = []
        ncpu = os.cpu_count() or 1
        for p in procs:
            try:
                with p.oneshot():
                    rows.append({"pid": p.pid, "name": p.info.get("name") or "?", "cpu_percent": p.cpu_percent(None) / ncpu,
                                 "memory_mb": p.memory_info().rss / 2**20})
            except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess):
                continue
        rows.sort(key=lambda r: -r["cpu_percent" if sort == "cpu" else "memory_mb"])
        return rows[:limit]

    def own(self) -> dict:
        if self.psutil:
            p = self.psutil.Process()
            with p.oneshot():
                p.cpu_percent(None)
                time.sleep(0.2)
                return {"pid": p.pid, "cpu_percent": p.cpu_percent(None) / (os.cpu_count() or 1), "memory_mb": p.memory_info().rss / 2**20,
                        "threads": p.num_threads(), "children": len(p.children()), "source": "psutil"}
        if sys.platform != "win32":
            try:
                import resource
                ru = resource.getrusage(resource.RUSAGE_SELF)
                rss = ru.ru_maxrss / 1024 if sys.platform != "darwin" else ru.ru_maxrss / 2**20
                return {"pid": os.getpid(), "cpu_percent": None, "memory_mb": rss, "threads": None, "children": None,
                        "source": "getrusage (peak memory only; CPU needs psutil)"}
            except Exception as e:  # noqa: BLE001
                raise Unavailable(f"{type(e).__name__}") from e
        raise Unavailable("Jarvis' own usage needs the optional 'psutil' package on Windows")


# ------------------------------------------------------------------ collectors: always return Readings, never raise
def collect(part: str, src) -> list[Reading]:
    at = now_iso()
    try:
        return _COLLECT[part](src, at)
    except Unavailable as e:
        return [na(part, "", getattr(src, "name", "?"), str(e), at)]
    except Exception as e:  # noqa: BLE001 - a broken source must not break the tool
        return [na(part, "", getattr(src, "name", "?"), f"the source failed ({type(e).__name__}: {e})", at)]


def _cpu(src, at):
    val, source = src.cpu_usage()
    info = src.cpu_info()
    out = [checked("cpu_usage", val, "%", source, 0, 100, at=at, detail="average over a ~0.3 s sample")]
    out.append(ok("cpu_logical_processors", info["logical"], "count", "os.cpu_count", at=at) if info.get("logical") else na("cpu_logical_processors", "count", "os.cpu_count", "unknown", at))
    out.append(ok("cpu_model", info["name"], "", "operating system", at=at) if info.get("name") else na("cpu_model", "", "operating system", "the OS did not report a processor name", at))
    out.append(checked("cpu_frequency", info.get("mhz"), "MHz", "psutil", 1, 20000, at=at) if info.get("mhz") else
               na("cpu_frequency", "MHz", "psutil", "needs the optional psutil package and OS support", at))
    return out


def _memory(src, at):
    m = src.memory()
    s = m.get("source", "operating system")
    tot, av = m.get("total_gb"), m.get("available_gb")
    out = [checked("memory_total", tot, "GiB", s, 0.01, at=at), checked("memory_available", av, "GiB", s, 0, at=at),
           checked("memory_used", m.get("used_percent"), "%", s, 0, 100, at=at)]
    if isinstance(tot, (int, float)) and isinstance(av, (int, float)) and av > tot * 1.001:
        return [na("memory", "GiB", s, f"inconsistent source data (available {av} GiB is more than total {tot} GiB)", at)]
    return out


def _disk(src, at):
    d = src.disks()
    out = []
    for dr in d.get("drives", []):
        name = dr.get("drive", "?")
        if dr.get("available") is False:
            out.append(na(f"disk {name}", "GiB", d.get("source", "disk_usage"), dr.get("why", "unreadable"), at))
            continue
        out += [checked(f"disk {name} capacity", dr.get("total_gb"), "GiB", d.get("source", "disk_usage"), 0, at=at),
                checked(f"disk {name} free", dr.get("free_gb"), "GiB", d.get("source", "disk_usage"), 0, at=at)]
    return out or [na("disk", "GiB", "disk_usage", "no fixed drive reported", at)]


def _gpu(src, at):
    g = src.gpu()
    out = []
    for i, gp in enumerate(g.get("gpus", [])):
        pre = f"gpu{i}" if len(g["gpus"]) > 1 else "gpu"
        out.append(ok(f"{pre}_name", gp.get("name", "?"), "", g.get("source", "?"), at=at))
        for key, label, unit, hi in (("utilization_percent", "usage", "%", 100), ("memory_used_mb", "memory_used", "MB", None),
                                     ("memory_total_mb", "memory_total", "MB", None), ("temperature_c", "temperature", "°C", 150)):
            if key in gp and gp[key] is not None:
                out.append(checked(f"{pre}_{label}", gp[key], unit, g.get("source", "?"), -20 if key == "temperature_c" else 0, hi, at=at))
            else:
                out.append(na(f"{pre}_{label}", unit, g.get("source", "?"), g.get("metrics") or "this GPU/driver did not report it (only NVIDIA GPUs with nvidia-smi report usage and temperature here)", at))
    return out or [na("gpu", "", "gpu", "no GPU listed", at)]


def _temperature(src, at):
    return [checked(f"temperature {label}", v, "°C", "psutil", -40, 150, at=at) for label, v in src.temperatures()]


_COLLECT = {"cpu": _cpu, "memory": _memory, "disk": _disk, "gpu": _gpu, "temperature": _temperature}


def process_readings(src, limit: int = 10, sort: str = "cpu") -> tuple[list[Reading], list[dict]]:
    at = now_iso()
    limit = max(1, min(int(limit), MAX_PROCESSES))
    try:
        rows = src.processes(limit, sort)
    except Unavailable as e:
        return [na("processes", "", getattr(src, "name", "?"), str(e), at)], []
    except Exception as e:  # noqa: BLE001
        return [na("processes", "", getattr(src, "name", "?"), f"the source failed ({type(e).__name__})", at)], []
    clean, out = [], []
    for r in rows:
        c = checked("cpu", r.get("cpu_percent"), "%", "psutil", 0, 100.001, at=at)
        m = checked("memory", r.get("memory_mb"), "MB", "psutil", 0, at=at)
        if c.kind == "unavailable" or m.kind == "unavailable" or not isinstance(r.get("pid"), int):
            continue                                   # a row with invalid numbers is dropped, not repaired
        clean.append({"pid": r["pid"], "name": str(r.get("name", "?"))[:80], "cpu_percent": round(c.value, 1), "memory_mb": round(m.value, 1)})
        out.append(c)
    dropped = len(rows) - len(clean)
    note = f"; {dropped} row(s) with invalid data were dropped" if dropped else ""
    return [ok("processes_listed", len(clean), "count", "psutil", at=at, detail="names, pid, CPU and memory only; no command lines, paths or users are read" + note)], clean


def own_readings(src) -> list[Reading]:
    at = now_iso()
    try:
        o = src.own()
    except Unavailable as e:
        return [na("jarvis_self", "", getattr(src, "name", "?"), str(e), at)]
    except Exception as e:  # noqa: BLE001
        return [na("jarvis_self", "", getattr(src, "name", "?"), f"the source failed ({type(e).__name__})", at)]
    s = o.get("source", "?")
    return [checked("jarvis_cpu", o.get("cpu_percent"), "%", s, 0, 100.001, at=at) if o.get("cpu_percent") is not None else na("jarvis_cpu", "%", s, "not available from this source", at),
            checked("jarvis_memory", o.get("memory_mb"), "MB", s, 0, at=at),
            checked("jarvis_threads", o.get("threads"), "count", s, 0, at=at) if o.get("threads") is not None else na("jarvis_threads", "count", s, "not available from this source", at)]


# ------------------------------------------------------------------ the sampler
class Monitor:
    def __init__(self, src=None, config: MonitorConfig | None = None, clock=time.monotonic):
        self.src = src or RealSource()
        self.config = config or MonitorConfig()
        self.clock = clock
        self._cache: dict[str, tuple[float, list[Reading]]] = {}

    def snapshot(self, parts=None) -> tuple[list[Reading], dict]:
        """Readings for the parts the USER allowed (a part they did not choose is reported as such, not measured)."""
        want = [p for p in (parts or self.config.parts)]
        bad = [p for p in want if p not in PARTS]
        if bad:
            raise ValueError(f"unknown part(s) {bad}; choose from {list(PARTS)}")
        out, meta = [], {"cached": [], "not_selected": []}
        for p in want:
            if p not in self.config.parts:
                meta["not_selected"].append(p)
                out.append(na(p, "", "settings", "you did not choose this metric (jarvis monitor config --metrics ...)"))
                continue
            hit = self._cache.get(p)
            if hit and self.clock() - hit[0] < self.config.interval_s:
                out += hit[1]
                meta["cached"].append(p)
                continue
            rs = collect(p, self.src)
            self._cache[p] = (self.clock(), rs)
            out += rs
        return out, meta

    def watch(self, parts, interval: float, count: int, stop=None, sleep=time.sleep) -> list[list[Reading]]:
        """A bounded series. interval is clamped to the allowed range; count and total time are capped; `stop()` ends it early."""
        interval = min(max(float(interval), MIN_INTERVAL), MAX_INTERVAL)
        count = max(1, min(int(count), MAX_WATCH_SAMPLES))
        if interval * (count - 1) > MAX_WATCH_SECONDS:
            count = int(MAX_WATCH_SECONDS // interval) + 1
        series = []
        for i in range(count):
            if stop and stop():
                break
            rs = []
            for p in parts or self.config.parts:
                rs += collect(p, self.src) if p in self.config.parts else [na(p, "", "settings", "you did not choose this metric")]
            series.append(rs)
            if i < count - 1:
                end = self.clock() + interval
                while self.clock() < end:
                    if stop and stop():
                        return series
                    sleep(min(0.1, max(0.0, end - self.clock())))
        return series
