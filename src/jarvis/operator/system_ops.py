"""System monitoring: processes, resources, uptime, battery, startup entries, services, power plan, devices."""
from __future__ import annotations

import json
import time

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, Unavailable, cap, human_size

PROTECTED_PROCESSES = {"system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
                       "explorer.exe", "dwm.exe", "fontdrvhost.exe", "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe", "securityhealthsystray.exe",
                       "sihost.exe", "ctfmon.exe", "taskhostw.exe", "runtimebroker.exe", "searchindexer.exe", "audiodg.exe", "spoolsv.exe", "lsm.exe", "wudfhost.exe",
                       "conhost.exe", "dllhost.exe", "init", "systemd", "kthreadd"}
PROTECTED_SERVICES = {"windefend", "mpssvc", "wscsvc", "securityhealthservice", "eventlog", "rpcss", "rpceptmapper", "dcomlaunch", "lsm", "samss", "wuauserv",
                      "bits", "cryptsvc", "trustedinstaller", "winmgmt", "sens", "profsvc", "gpsvc", "schedule", "power", "plugplay", "dnscache", "nlasvc", "netprofm", "lanmanserver", "lanmanworkstation"}


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        raise Unavailable("This needs the psutil package, which isn't in this copy of Jarvis.") from None


def snapshot_processes(top: int = 15, sample: float = 0.4) -> list[dict]:
    ps = _psutil()
    procs = []
    for p in ps.process_iter(["pid", "name", "memory_info"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except (ps.NoSuchProcess, ps.AccessDenied):
            pass
    time.sleep(sample)
    rows = []
    n = max(1, ps.cpu_count() or 1)
    for p in procs:
        try:
            rows.append({"pid": p.pid, "name": p.info["name"] or "?", "cpu": round(p.cpu_percent(None) / n, 1), "ram": int(p.info["memory_info"].rss if p.info["memory_info"] else 0)})
        except (ps.NoSuchProcess, ps.AccessDenied):
            pass
    return rows


@cap("system.resources", "system_read", "Report CPU, RAM, disk and battery use", LOW, (), False, "6,16")
def resources(op) -> Outcome:
    ps = _psutil()
    cpu = ps.cpu_percent(interval=0.4)
    vm = ps.virtual_memory()
    disks = []
    for part in ps.disk_partitions(all=False):
        try:
            u = ps.disk_usage(part.mountpoint)
            disks.append(f"{part.mountpoint} {human_size(u.free)} free of {human_size(u.total)}")
        except OSError:
            pass
    b = ps.sensors_battery() if hasattr(ps, "sensors_battery") else None
    msg = f"CPU {cpu:.0f}% busy. RAM {vm.percent:.0f}% used ({human_size(vm.used)} of {human_size(vm.total)}). " + ("Drives: " + "; ".join(disks[:6]) + ". " if disks else "")
    if b is not None:
        msg += f"Battery {b.percent:.0f}%{', charging' if b.power_plugged else ''}."
    return Outcome("info", msg.strip(), {"cpu": cpu, "ram_percent": vm.percent, "ram_used": vm.used, "ram_total": vm.total})


@cap("system.ram", "system_read", "Report RAM use", LOW, (), False, "6")
def ram(op) -> Outcome:
    vm = _psutil().virtual_memory()
    return Outcome("info", f"You are using {human_size(vm.used)} of {human_size(vm.total)} RAM ({vm.percent:.0f}%); {human_size(vm.available)} is available.", {"percent": vm.percent})


@cap("system.cpu", "system_read", "Report CPU use", LOW, (), False, "6")
def cpu(op) -> Outcome:
    ps = _psutil()
    v = ps.cpu_percent(interval=0.5)
    return Outcome("info", f"The CPU is {v:.0f}% busy across {ps.cpu_count() or '?'} threads.", {"cpu": v})


@cap("system.battery", "system_read", "Report battery level and charging", LOW, (), False, "3,6")
def battery(op) -> Outcome:
    ps = _psutil()
    b = ps.sensors_battery() if hasattr(ps, "sensors_battery") else None
    if b is None:
        return Outcome("info", "This PC doesn't report a battery (it is probably a desktop).")
    left = "" if b.secsleft in (None, -1, -2) else f", about {int(b.secsleft // 3600)} h {int(b.secsleft % 3600 // 60)} min left"
    return Outcome("info", f"Battery {b.percent:.0f}%, {'charging' if b.power_plugged else 'on battery'}{left}.", {"percent": b.percent, "plugged": b.power_plugged})


@cap("system.uptime", "system_read", "Report how long the PC has been on", LOW, (), False, "3,6")
def uptime(op) -> Outcome:
    s = int(time.time() - _psutil().boot_time())
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    return Outcome("info", f"The PC has been on for {d} days, {h} hours and {r // 60} minutes." if d else f"The PC has been on for {h} hours and {r // 60} minutes.", s)


@cap("system.processes", "system_read", "List the busiest processes", LOW, ("top", "by"), False, "6,16")
def processes(op, top: int = 10, by: str = "cpu") -> Outcome:
    rows = snapshot_processes(sample=0.5)
    key = "ram" if by == "ram" else "cpu"
    rows.sort(key=lambda r: r[key], reverse=True)
    rows = rows[:max(1, min(int(top), 40))]
    msg = f"Busiest by {'memory' if key == 'ram' else 'CPU'}: " + "; ".join(f"{r['name']} (pid {r['pid']}, {r['cpu']}% CPU, {human_size(r['ram'])})" for r in rows)
    return Outcome("info", msg, rows)


@cap("system.heavy", "system_read", "Find resource-heavy applications", LOW, (), False, "6,9,16")
def heavy(op) -> Outcome:
    rows = snapshot_processes(sample=0.6)
    hot = [r for r in rows if r["cpu"] >= 25 or r["ram"] >= 1.5 * 1024 ** 3]
    hot.sort(key=lambda r: (r["cpu"], r["ram"]), reverse=True)
    if not hot:
        return Outcome("info", "Nothing is using an unusual amount of CPU (25% or more) or memory (1.5 GB or more) right now.")
    return Outcome("info", "Using a lot: " + "; ".join(f"{r['name']} ({r['cpu']}% CPU, {human_size(r['ram'])})" for r in hot[:8]) + ". I won't close system processes; ask me to close an app by name if you want.", hot)


@cap("system.process_report", "system_read", "Write a process report to a text file", LOW, (), False, "16")
def process_report(op) -> Outcome:
    rows = sorted(snapshot_processes(sample=0.5), key=lambda r: r["ram"], reverse=True)[:60]
    folder = op.home / "Documents" / "Jarvis reports"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / time.strftime("processes-%Y%m%d-%H%M%S.txt")
    f.write_text("\n".join(f"{r['pid']:>7}  {r['name'][:40]:<40} cpu {r['cpu']:>5}%  ram {human_size(r['ram'])}" for r in rows), encoding="utf-8")
    return Outcome("verified" if f.exists() else "failed", f"Saved the report: {f.name} in Documents/Jarvis reports.", str(f))


@cap("system.startup_apps", "system_read", "List programs that start with Windows", LOW, (), True, "9,15,22")
def startup_apps(op) -> Outcome:
    import os
    out: list[dict] = []
    try:
        import winreg
        for hive, name in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                    i = 0
                    while True:
                        try:
                            n, v, _t = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        out.append({"name": n, "command": str(v)[:200], "source": name})
                        i += 1
            except OSError:
                pass
    except ImportError:
        raise Unavailable("Startup entries can only be read on Windows.") from None
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        from pathlib import Path
        folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        if folder.is_dir():
            out += [{"name": p.stem, "command": str(p), "source": "Startup folder"} for p in folder.iterdir() if p.is_file() and p.name.lower() != "desktop.ini"]
    msg = f"{len(out)} startup entries: " + "; ".join(f"{e['name']}" for e in out[:25]) if out else "No startup entries were found."
    return Outcome("info", msg, out)


@cap("system.services", "system_read", "List Windows services and their state", LOW, ("state", "name"), True, "16")
def services(op, state: str = "running", name: str = "") -> Outcome:
    script = "Get-Service | Select-Object Name,DisplayName,Status | ConvertTo-Json -Compress"
    raw = op.ops.powershell(script, 30)
    try:
        data = json.loads(raw)
    except ValueError:
        raise Unavailable("Windows returned a service list I couldn't read.") from None
    data = data if isinstance(data, list) else [data]
    stat = {1: "stopped", 4: "running"}
    rows = [{"name": d.get("Name"), "display": d.get("DisplayName"), "state": stat.get(d.get("Status"), str(d.get("Status")).lower())} for d in data]
    if name:
        rows = [r for r in rows if name.lower() in (r["name"] or "").lower() or name.lower() in (r["display"] or "").lower()]
    elif state in ("running", "stopped"):
        rows = [r for r in rows if r["state"] == state]
    return Outcome("info", f"{len(rows)} services" + (": " + "; ".join(f"{r['display'] or r['name']} ({r['state']})" for r in rows[:15]) if rows else "."), rows)


def _svc_name(name: str) -> str:
    import re
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name or ""):
        raise OperatorError("That doesn't look like a service name.")
    if name.lower() in PROTECTED_SERVICES or name.lower().startswith(("defender", "sec", "mpssvc", "win")):
        raise OperatorError("I won't touch that service: stopping it could disable Windows security or break Windows.")
    return name


@cap("system.service_control", "services", "Start, stop or restart a Windows service", HIGH, ("name", "action"), True, "16")
def service_control(op, name: str = "", action: str = "restart") -> Outcome:
    if action not in ("start", "stop", "restart"):
        raise OperatorError("A service can be started, stopped or restarted.")
    n = _svc_name(name)
    cmd = {"start": "Start-Service", "stop": "Stop-Service", "restart": "Restart-Service"}[action]
    op.ops.powershell(f"{cmd} -Name '{n}' -ErrorAction Stop", 40)
    raw = op.ops.powershell(f"(Get-Service -Name '{n}').Status", 15).strip()
    want = "Stopped" if action == "stop" else "Running"
    ok = raw.lower() in (want.lower(), "4" if want == "Running" else "1")
    return Outcome("verified" if ok else "failed", f"The service {n} is now {raw.lower()}." if ok else f"I asked Windows to {action} {n} but its state is {raw}. It may need administrator rights, which I never take.")


@cap("system.power_plan", "system_read", "Show the active power plan", LOW, (), True, "3")
def power_plan(op) -> Outcome:
    code, out, _e = op.ops.run(["powercfg.exe", "/getactivescheme"], 10)
    return Outcome("info", out.strip() or "I couldn't read the power plan.", out.strip())


@cap("system.devices", "system_read", "List cameras, audio devices, USB devices or printers", LOW, ("kind",), True, "21")
def devices(op, kind: str = "usb") -> Outcome:
    scripts = {"usb": "Get-PnpDevice -Class USB -PresentOnly | Select-Object -ExpandProperty FriendlyName",
               "audio": "Get-PnpDevice -Class AudioEndpoint -PresentOnly | Select-Object -ExpandProperty FriendlyName",
               "camera": "Get-PnpDevice -Class Camera -PresentOnly | Select-Object -ExpandProperty FriendlyName",
               "printers": "Get-Printer | Select-Object -ExpandProperty Name",
               "scanners": "Get-PnpDevice -Class Image -PresentOnly | Select-Object -ExpandProperty FriendlyName"}
    if kind not in scripts:
        raise OperatorError("I can list usb, audio, camera, printers or scanners.")
    names = [ln.strip() for ln in op.ops.powershell(scripts[kind], 25).splitlines() if ln.strip()]
    return Outcome("info", f"{len(names)} {kind} device(s): " + "; ".join(names[:15]) if names else f"No {kind} devices were found.", names)


@cap("system.print_queue", "system_read", "Show the print queue", LOW, (), True, "21")
def print_queue(op) -> Outcome:
    raw = op.ops.powershell("Get-Printer | ForEach-Object { $n=$_.Name; Get-PrintJob -PrinterName $n | Select-Object @{n='Printer';e={$n}},DocumentName,JobStatus } | ConvertTo-Json -Compress", 30).strip()
    if not raw:
        return Outcome("info", "The print queue is empty.")
    data = json.loads(raw)
    data = data if isinstance(data, list) else [data]
    return Outcome("info", f"{len(data)} print job(s): " + "; ".join(f"{d.get('DocumentName')} on {d.get('Printer')} ({d.get('JobStatus')})" for d in data[:10]), data)
