"""System information. Every value says where it came from; anything this PC cannot report is marked unavailable with
the reason. Nothing is estimated or invented, and nothing here uses the network."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _na(why: str) -> dict:
    return {"available": False, "why": why}


def _win():
    import ctypes
    return ctypes


# ------------------------------------------------------------------ CPU
def cpu() -> dict:
    out = {"logical_processors": os.cpu_count(), "architecture": platform.machine() or None}
    name = None
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    elif Path("/proc/cpuinfo").exists():
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.lower().startswith("model name"):
                name = line.split(":", 1)[1].strip()
                break
    out["name"] = name or platform.processor() or None
    out["usage_percent"] = cpu_usage()
    return out


def cpu_usage(interval: float = 0.4) -> dict:
    try:
        if sys.platform == "win32":
            ctypes = _win()
            from ctypes import wintypes

            def times():
                i, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
                if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
                    raise OSError("GetSystemTimes failed")
                f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
                return f(i), f(k), f(u)          # kernel time INCLUDES idle time
            i1, k1, u1 = times()
            time.sleep(interval)
            i2, k2, u2 = times()
            total = (k2 - k1) + (u2 - u1)
            busy = total - (i2 - i1)
            src = "GetSystemTimes"
        elif Path("/proc/stat").exists():
            def times():
                v = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
                return v[3] + v[4], sum(v)
            idle1, t1 = times()
            time.sleep(interval)
            idle2, t2 = times()
            total, busy = t2 - t1, (t2 - t1) - (idle2 - idle1)
            src = "/proc/stat"
        else:
            return _na("no CPU usage source on this OS")
        if total <= 0:
            return _na("no CPU time elapsed during the sample")
        return {"available": True, "value": round(100.0 * busy / total, 1), "sampled_seconds": interval, "source": src}
    except Exception as e:  # noqa: BLE001
        return _na(f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------ memory
def memory() -> dict:
    try:
        if sys.platform == "win32":
            ctypes = _win()

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return _na("GlobalMemoryStatusEx failed")
            total, avail, src = m.ullTotalPhys, m.ullAvailPhys, "GlobalMemoryStatusEx"
        elif Path("/proc/meminfo").exists():
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) * 1024
            total, avail, src = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0)), "/proc/meminfo"
        else:
            return _na("no memory information source on this OS")
        return {"available": True, "total_gb": round(total / 2**30, 2), "available_gb": round(avail / 2**30, 2),
                "used_percent": round(100.0 * (total - avail) / total, 1), "source": src}
    except Exception as e:  # noqa: BLE001
        return _na(f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------ storage
def storage() -> dict:
    drives = []
    if sys.platform == "win32":
        ctypes = _win()
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                root = f"{chr(65 + i)}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:      # DRIVE_FIXED only (no network/optical)
                    drives.append(root)
    else:
        drives.append("/")
    out = []
    for d in drives:
        try:
            u = shutil.disk_usage(d)
            out.append({"drive": d, "total_gb": round(u.total / 2**30, 1), "free_gb": round(u.free / 2**30, 1),
                        "used_percent": round(100.0 * u.used / u.total, 1) if u.total else None})
        except OSError as e:
            out.append({"drive": d, "available": False, "why": str(e)})
    return {"available": bool(out), "drives": out, "source": "GetLogicalDrives + disk_usage" if sys.platform == "win32" else "disk_usage"}


# ------------------------------------------------------------------ network (state only; no traffic is sent)
def network() -> dict:
    try:
        import psutil  # optional, free (BSD)
        up = sorted(n for n, s in psutil.net_if_stats().items() if s.isup and not n.lower().startswith(("lo", "loopback")))
        return {"available": True, "interfaces_up": up, "any_interface_up": bool(up),
                "source": "psutil (interface state only; does not prove internet access; no traffic sent)"}
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        return _na(f"psutil: {e}")
    if sys.platform == "win32":
        try:
            ctypes = _win()
            flags = ctypes.c_ulong(0)
            con = bool(ctypes.windll.wininet.InternetGetConnectedState(ctypes.byref(flags), 0))
            kind = [n for bit, n in ((0x1, "modem"), (0x2, "LAN"), (0x4, "proxy"), (0x20, "RAS")) if flags.value & bit]
            return {"available": True, "connected": con, "connection_types": kind,
                    "source": "InternetGetConnectedState (Windows' own view; no traffic sent; it can be wrong behind captive portals)"}
        except Exception as e:  # noqa: BLE001
            return _na(f"InternetGetConnectedState: {e}")
    sysnet = Path("/sys/class/net")
    if sysnet.exists():
        up = sorted(p.name for p in sysnet.iterdir() if p.name != "lo" and (p / "operstate").exists()
                    and (p / "operstate").read_text().strip() == "up")
        return {"available": True, "interfaces_up": up, "any_interface_up": bool(up),
                "source": "/sys/class/net operstate (does not prove internet access)"}
    return _na("no network state source on this OS")


# ------------------------------------------------------------------ GPU
def _nvidia_smi() -> str | None:
    p = shutil.which("nvidia-smi")
    if p:
        return p
    if sys.platform == "win32":
        c = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        if c.exists():
            return str(c)
    return None


def gpu() -> dict:
    smi = _nvidia_smi()
    names = []
    if smi:
        try:
            r = subprocess.run([smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                gpus = []
                for line in r.stdout.strip().splitlines():
                    f = [x.strip() for x in line.split(",")]
                    num = lambda x: float(x) if x.replace(".", "", 1).isdigit() else None
                    gpus.append({"name": f[0], "utilization_percent": num(f[1]), "memory_used_mb": num(f[2]),
                                 "memory_total_mb": num(f[3]), "temperature_c": num(f[4])})
                return {"available": True, "gpus": gpus, "source": "nvidia-smi"}
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    if sys.platform == "win32":
        ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        try:
            r = subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-Command",
                                "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    if names:
        return {"available": True, "gpus": [{"name": n} for n in names], "source": "Win32_VideoController",
                "metrics": "usage/temperature not available: only NVIDIA GPUs with nvidia-smi report them here"}
    return _na("no GPU information source found (NVIDIA nvidia-smi not present" + (", and Windows did not list a video controller)" if sys.platform == "win32" else ")"))


def snapshot(parts: list[str] | None = None) -> dict:
    fns = {"cpu": cpu, "memory": memory, "storage": storage, "network": network, "gpu": gpu}
    want = parts or list(fns)
    out = {"os": f"{platform.system()} {platform.release()} ({platform.version()})"}
    for k in want:
        out[k] = fns[k]()
    return out
