"""Windows event logs, error codes, Windows Update and drivers (read-only), Jarvis self-check."""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections import Counter

from jarvis.operator.core import CATEGORIES, LOW, Outcome, OperatorError, Unavailable, cap

NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
ERROR_CODES = {
    "0x80070005": "Access denied: the program or you don't have permission for that file or setting.",
    "0x80070002": "File not found: a file Windows or the program expected is missing.",
    "0x80070003": "Path not found: a folder it expected is missing.",
    "0x80070057": "Invalid parameter: the program passed Windows something it couldn't use.",
    "0x80004005": "Unspecified error: a general failure; check the event log around that time.",
    "0x8007000e": "Out of memory: close programs or add RAM.",
    "0x80070020": "Sharing violation: another program has the file open.",
    "0x800f0922": "A Windows update could not be installed (often low space in the System Reserved partition or a pending restart).",
    "0x80073712": "A Windows component store file is missing or damaged (try DISM/SFC from an administrator prompt yourself).",
    "0xc0000005": "Access violation: the program crashed reading memory it shouldn't. Update or reinstall that program.",
    "0xc000007b": "Bad image format: a 32-bit/64-bit mismatch. Reinstall the program and the Visual C++ runtimes.",
    "0xc0000135": "A required DLL (often .NET or Visual C++) is missing. Install the runtime the program needs.",
    "0x80240fff": "Windows Update hit an unspecified error; retry later or restart.",
}
WIN32_CODES = {2: "The system cannot find the file specified.", 3: "The system cannot find the path specified.", 5: "Access is denied.", 32: "The file is in use by another process.",
               87: "The parameter is incorrect.", 1223: "The operation was cancelled by the user.", 1326: "The user name or password is incorrect.", 10061: "The connection was refused.",
               10060: "The connection timed out.", 11001: "The host name could not be found."}


def parse_events(xml_text: str) -> list[dict]:
    rows = []
    for chunk in re.findall(r"<Event .*?</Event>", xml_text, re.S):
        try:
            e = ET.fromstring(chunk)
        except ET.ParseError:
            continue
        sysn = e.find("e:System", NS)
        if sysn is None:
            continue
        prov = (sysn.find("e:Provider", NS).attrib.get("Name") if sysn.find("e:Provider", NS) is not None else "?")
        eid = (sysn.findtext("e:EventID", "", NS) or "").strip()
        tc = sysn.find("e:TimeCreated", NS)
        lvl = int(sysn.findtext("e:Level", "4", NS) or 4)
        data = [(d.text or "").strip() for d in e.findall(".//e:EventData/e:Data", NS)]
        rows.append({"provider": prov, "id": eid, "time": (tc.attrib.get("SystemTime", "")[:19].replace("T", " ") if tc is not None else ""), "level": lvl, "data": data[:6]})
    return rows


def summarize(rows: list[dict]) -> str:
    if not rows:
        return "No errors or warnings in that period."
    c = Counter((r["provider"], r["id"]) for r in rows)
    crashes = [r for r in rows if r["id"] in ("1000", "1002", "1026") and r["provider"] in ("Application Error", "Application Hang", ".NET Runtime")]
    apps = Counter((r["data"][0] if r["data"] else "?") for r in crashes)
    msg = f"{len(rows)} error/warning event(s). Most repeated: " + "; ".join(f"{p} #{i} x{n}" for (p, i), n in c.most_common(4)) + "."
    if apps:
        msg += " Crashes/hangs: " + ", ".join(f"{a} x{n}" for a, n in apps.most_common(4)) + "."
    if rows:
        msg += f" Latest: {rows[0]['time']} {rows[0]['provider']}."
    return msg


@cap("events.recent_errors", "events", "Summarize recent system and application errors", LOW, ("log", "hours", "count"), True, "17,47")
def recent_errors(op, log: str = "System", hours: int = 24, count: int = 60) -> Outcome:
    if log not in ("System", "Application"):
        raise OperatorError("I can read the System or Application log.")
    ms = max(1, min(int(hours), 24 * 30)) * 3600 * 1000
    code, out, err = op.ops.run(["wevtutil.exe", "qe", log, f"/c:{max(1, min(int(count), 200))}", "/rd:true", "/f:xml",
                                 f"/q:*[System[(Level=1 or Level=2 or Level=3) and TimeCreated[timediff(@SystemTime) <= {ms}]]]"], 40)
    if code != 0:
        raise Unavailable("Windows wouldn't let me read that log (" + (err.strip()[:100] or "access denied") + "). The Security log needs administrator rights, which I never take.")
    rows = parse_events(out)
    return Outcome("info", f"{log} log, last {hours} h: " + summarize(rows), rows)


@cap("events.crashes", "events", "List application crashes and hangs", LOW, ("hours",), True, "9,17,47")
def crashes(op, hours: int = 72) -> Outcome:
    res = recent_errors(op, "Application", hours, 200)
    rows = [r for r in res.data if r["id"] in ("1000", "1002", "1026")]
    if not rows:
        return Outcome("info", f"No application crashes or hangs in the last {hours} hours.")
    apps = Counter((r["data"][0] if r["data"] else "?") for r in rows)
    return Outcome("info", f"{len(rows)} crash/hang event(s): " + ", ".join(f"{a} x{n}" for a, n in apps.most_common(6)) + f". Latest {rows[0]['time']}.", rows)


@cap("events.explain_code", "events", "Explain a Windows error code", LOW, ("code",), False, "17,47")
def explain_code(op, code: str = "") -> Outcome:
    c = code.strip().lower()
    if re.fullmatch(r"0x[0-9a-f]{1,8}", c):
        key = "0x" + c[2:].zfill(8)
        if key in ERROR_CODES:
            return Outcome("info", f"{code}: {ERROR_CODES[key]}")
        n = int(c, 16)
        if (n >> 16) == 0x8007 and (n & 0xFFFF) in WIN32_CODES:
            return Outcome("info", f"{code}: {WIN32_CODES[n & 0xFFFF]}")
    elif c.isdigit() and int(c) in WIN32_CODES:
        return Outcome("info", f"Error {c}: {WIN32_CODES[int(c)]}")
    return Outcome("info", f"I don't have {code} in my small list, and I won't guess. Search the exact code with the program name, or tell me where it appeared.")


@cap("events.update_status", "events", "Show Windows Update history and installed fixes", LOW, (), True, "18")
def update_status(op) -> Outcome:
    raw = op.ops.powershell("Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 8 HotFixID,InstalledOn | ConvertTo-Json -Compress", 40).strip()
    import json
    data = json.loads(raw) if raw else []
    data = data if isinstance(data, list) else [data]
    return Outcome("info", "Latest installed updates: " + ", ".join(str(d.get("HotFixID")) for d in data) + ". For pending updates open Windows Update (I can do that).", data)


@cap("events.open_update", "events", "Open Windows Update", LOW, (), True, "18")
def open_update(op) -> Outcome:
    op.ops.open_target("ms-settings:windowsupdate")
    return Outcome("unverified", "Opened Windows Update. I don't install updates or drivers myself.")


@cap("events.drivers", "events", "List installed driver versions", LOW, ("filter",), True, "18")
def drivers(op, filter: str = "") -> Outcome:
    import json
    raw = op.ops.powershell("Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceName } | Select-Object DeviceName,DriverVersion,DriverDate,Manufacturer | ConvertTo-Json -Compress", 60).strip()
    data = json.loads(raw) if raw else []
    data = data if isinstance(data, list) else [data]
    if filter:
        data = [d for d in data if filter.lower() in str(d.get("DeviceName")).lower() or filter.lower() in str(d.get("Manufacturer")).lower()]
    return Outcome("info", f"{len(data)} driver(s)" + (": " + "; ".join(f"{d.get('DeviceName')} {d.get('DriverVersion')}" for d in data[:10]) if data else ".") + " I can't tell which are outdated without a trusted source; use the manufacturer's page.", data)


@cap("events.driver_pages", "events", "Open an official driver download page", LOW, ("vendor",), True, "18")
def driver_pages(op, vendor: str = "") -> Outcome:
    pages = {"nvidia": "https://www.nvidia.com/Download/index.aspx", "amd": "https://www.amd.com/en/support", "intel": "https://www.intel.com/content/www/us/en/download-center/home.html",
             "windows": "ms-settings:windowsupdate"}
    v = vendor.lower().strip()
    if v not in pages:
        raise OperatorError("I can open the official pages for nvidia, amd, intel or windows update.")
    op.ops.open_target(pages[v])
    return Outcome("unverified", f"Opened the official {vendor} page. Download and install drivers yourself from there.")


@cap("diag.self_check", "system_read", "Check Jarvis's own parts honestly", LOW, (), False, "49")
def self_check(op) -> Outcome:
    from jarvis.operator.core import REGISTRY
    items = []
    items.append(("Computer control", "on" if op.is_on() else "off"))
    items.append(("Emergency stop", "ON (blocking everything)" if op.stop_active() else "clear"))
    items.append(("Audit log writable", "yes" if _writable(op.folder) else "NO"))
    for mod, label in (("psutil", "system readings"), ("PIL", "image tools"), ("docx", "Word files"), ("openpyxl", "Excel files"), ("reportlab", "PDF creation"), ("pypdf", "PDF reading")):
        try:
            __import__(mod)
            items.append((label, "available"))
        except ImportError:
            items.append((label, "NOT installed (those features are unavailable)"))
    items.append(("Windows control layer", "available" if op.ops.is_windows else "not on Windows: window, power and media actions are unavailable here"))
    items.append(("Capabilities registered", str(len(REGISTRY))))
    try:
        free = __import__("shutil").disk_usage(op.folder if op.folder.exists() else op.home).free
        items.append(("Free space for logs", f"{free // 1048576} MB"))
    except OSError:
        pass
    bad = [k for k, v in items if "NOT" in v or v == "NO"]
    return Outcome("info", "; ".join(f"{k}: {v}" for k, v in items) + ("." if not bad else f". Problems: {', '.join(bad)}."), dict(items))


def _writable(folder) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        t = folder / ".w"
        t.write_text("x")
        t.unlink()
        return True
    except OSError:
        return False
