"""Drives, storage use, biggest folders, growth over time, cleanup candidates, drive health."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from jarvis.operator.core import LOW, Outcome, OperatorError, Unavailable, cap, human_size
from jarvis.operator.files_ops import walk_files


def _psutil():
    from jarvis.operator.system_ops import _psutil as p
    return p()


@cap("storage.drives", "storage", "List drives with free space", LOW, (), False, "7")
def drives(op) -> Outcome:
    ps = _psutil()
    rows = []
    for part in ps.disk_partitions(all=False):
        try:
            u = ps.disk_usage(part.mountpoint)
        except OSError:
            continue
        removable = "removable" in (part.opts or "").lower() or part.fstype == "" and os.name == "nt"
        rows.append({"mount": part.mountpoint, "fs": part.fstype, "total": u.total, "free": u.free, "percent": u.percent, "removable": bool(removable)})
    msg = f"{len(rows)} drive(s): " + "; ".join(f"{r['mount']} {human_size(r['free'])} free of {human_size(r['total'])} ({r['percent']:.0f}% used{', removable' if r['removable'] else ''})" for r in rows)
    return Outcome("info", msg if rows else "I can't see any drives.", rows)


def folder_size(path: Path, limit: int = 300_000) -> int:
    return sum(st.st_size for _p, st in walk_files(path, limit))


@cap("storage.biggest_folders", "storage", "Show what is taking the most space inside a folder", LOW, ("folder", "top"), False, "7")
def biggest_folders(op, folder: str | None = None, top: int = 10) -> Outcome:
    root = op.check_path(folder) if folder else (op.roots()[0] if op.roots() else None)
    if root is None:
        raise OperatorError("No allowed folders exist yet.")
    rows = []
    loose = 0
    for p in root.iterdir():
        try:
            if p.is_dir() and not p.is_symlink():
                rows.append((folder_size(p), p))
            elif p.is_file():
                loose += p.stat().st_size
        except OSError:
            pass
    rows.sort(reverse=True)
    rows = rows[:max(1, int(top))]
    msg = f"Biggest folders in {root.name}: " + "; ".join(f"{p.name} {human_size(s)}" for s, p in rows) + (f"; loose files {human_size(loose)}" if loose else "")
    return Outcome("info", msg if rows else f"{root.name} has no subfolders.", [{"path": str(p), "size": s} for s, p in rows])


@cap("storage.report", "storage", "Write a storage report and note growth since last time", LOW, (), False, "7")
def report(op) -> Outcome:
    d = drives(op)
    hist_f = op.folder / "storage_history.json"
    try:
        hist = json.loads(hist_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = []
    now = {"t": time.time(), "drives": {r["mount"]: r["free"] for r in d.data}}
    growth = ""
    if hist:
        prev = hist[-1]
        parts = []
        for m, free in now["drives"].items():
            if m in prev["drives"]:
                diff = prev["drives"][m] - free
                if abs(diff) > 50_000_000:
                    parts.append(f"{m} {'lost' if diff > 0 else 'gained'} {human_size(abs(diff))} of free space since {time.strftime('%Y-%m-%d', time.localtime(prev['t']))}")
        growth = "; ".join(parts)
    hist = (hist + [now])[-60:]
    try:
        hist_f.write_text(json.dumps(hist), encoding="utf-8")
    except OSError:
        pass
    low = [r for r in d.data if r["free"] < 10 * 1024 ** 3 or r["percent"] > 90]
    msg = d.message + (" " + growth + "." if growth else "") + (" Running low: " + ", ".join(r["mount"] for r in low) + "." if low else "")
    return Outcome("info", msg, d.data)


@cap("storage.cleanup_candidates", "storage", "Suggest what could be cleaned up (nothing is deleted)", LOW, ("folder",), False, "7")
def cleanup_candidates(op, folder: str | None = None) -> Outcome:
    root = op.check_path(folder) if folder else None
    scan = [root] if root else [r for r in op.roots() if r.name in ("Downloads", "Desktop")]
    installers = old = temp = 0
    n_inst = n_old = n_temp = 0
    cut = time.time() - 365 * 86400
    inst_cut = time.time() - 30 * 86400
    for r in scan:
        for p, st in walk_files(r):
            e = p.suffix.lower()
            if e in (".exe", ".msi") and st.st_mtime < inst_cut:
                installers += st.st_size
                n_inst += 1
            elif e in (".tmp", ".crdownload", ".part", ".dmp", ".bak"):
                temp += st.st_size
                n_temp += 1
            elif st.st_mtime < cut and st.st_size > 100 * 1048576:
                old += st.st_size
                n_old += 1
    tmp_dir = os.environ.get("TEMP") or os.environ.get("TMP")
    tmp = folder_size(Path(tmp_dir), 50_000) if tmp_dir and Path(tmp_dir).is_dir() else 0
    parts = []
    if n_inst:
        parts.append(f"{n_inst} old installer(s) ({human_size(installers)})")
    if n_temp:
        parts.append(f"{n_temp} temporary file(s) ({human_size(temp)})")
    if n_old:
        parts.append(f"{n_old} large file(s) untouched for a year ({human_size(old)})")
    if tmp > 100 * 1048576:
        parts.append(f"the Windows temp folder holds {human_size(tmp)} (use Windows Storage settings to clear it)")
    return Outcome("info", "Could be cleaned: " + "; ".join(parts) + ". I haven't deleted anything; tell me which to move to the recycle folder." if parts else "I don't see obvious clutter in Downloads or Desktop.", parts)


@cap("storage.health", "storage", "Check drive health reported by Windows", LOW, (), True, "7")
def health(op) -> Outcome:
    raw = op.ops.powershell("Get-PhysicalDisk | Select-Object FriendlyName,MediaType,HealthStatus,OperationalStatus,Size | ConvertTo-Json -Compress", 30).strip()
    if not raw:
        return Outcome("info", "Windows didn't report any physical disks.")
    data = json.loads(raw)
    data = data if isinstance(data, list) else [data]
    msg = "; ".join(f"{d.get('FriendlyName')}: {d.get('HealthStatus')} ({d.get('OperationalStatus')})" for d in data)
    bad = [d for d in data if str(d.get("HealthStatus")).lower() not in ("healthy", "0")]
    return Outcome("info", msg + (". At least one disk is not reported healthy: back up now." if bad else ". These are Windows' own health flags; detailed SMART attributes need a vendor tool."), data)
