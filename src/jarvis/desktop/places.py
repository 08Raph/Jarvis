"""Things besides programs that "open ..." can reach: your own folders (Downloads, Documents, ...), Windows Settings pages, and ordinary files you name from those folders.

Safety rules, all enforced here and again by the launcher: only folders inside your user profile; only Windows Settings links that start with ``ms-settings:``; files only from
your personal folders, never programs or scripts (see ``DENY_SUFFIXES``), never Jarvis' own data, and never more than a bounded search. Nothing is read: a file is only handed to
Windows, which opens it with the program you normally use for that type."""
from __future__ import annotations

import os
import time
from pathlib import Path

DENY_SUFFIXES = {".exe", ".com", ".scr", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp", ".msix", ".appx", ".reg", ".hta",
                 ".jar", ".lnk", ".url", ".pif", ".cpl", ".dll", ".sys", ".py", ".pyw", ".sh", ".gadget", ".application", ".appref-ms", ".chm", ".iso", ".vhd", ".vhdx"}
FOLDER_NAMES = {"downloads": "Downloads", "download": "Downloads", "documents": "Documents", "my documents": "Documents", "docs": "Documents", "desktop": "Desktop",
                "pictures": "Pictures", "photos": "Pictures", "my pictures": "Pictures", "videos": "Videos", "my videos": "Videos", "music": "Music", "my music": "Music"}
SETTINGS = {"settings": "ms-settings:", "windows settings": "ms-settings:", "wifi": "ms-settings:network-wifi", "wi-fi": "ms-settings:network-wifi", "network settings": "ms-settings:network",
            "bluetooth": "ms-settings:bluetooth", "bluetooth settings": "ms-settings:bluetooth", "display settings": "ms-settings:display", "display": "ms-settings:display",
            "sound settings": "ms-settings:sound", "sound": "ms-settings:sound", "windows update": "ms-settings:windowsupdate", "update settings": "ms-settings:windowsupdate",
            "apps settings": "ms-settings:appsfeatures", "installed apps": "ms-settings:appsfeatures", "default apps": "ms-settings:defaultapps", "privacy settings": "ms-settings:privacy",
            "microphone settings": "ms-settings:privacy-microphone", "camera settings": "ms-settings:privacy-webcam", "personalization": "ms-settings:personalization",
            "background settings": "ms-settings:personalization-background", "storage settings": "ms-settings:storagesense", "battery settings": "ms-settings:batterysaver",
            "power settings": "ms-settings:powersleep", "night light": "ms-settings:nightlight", "mouse settings": "ms-settings:mousetouchpad", "keyboard settings": "ms-settings:easeofaccess-keyboard",
            "startup apps": "ms-settings:startupapps", "notifications settings": "ms-settings:notifications", "game mode": "ms-settings:gaming-gamemode", "xbox game bar": "ms-settings:gaming-gamebar"}
SKIP_DIRS = {"appdata", "node_modules", ".git", "__pycache__", "projectjarvis", "$recycle.bin", "windows", "program files", "program files (x86)"}
MAX_SCAN_ENTRIES = 30_000
MAX_SECONDS = 4.0


def allowed_settings_uri(uri: str) -> bool:
    u = str(uri or "").strip().lower()
    return u.startswith("ms-settings:") and len(u) < 80 and all(c.isalnum() or c in ":-_" for c in u)


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def personal_roots() -> list[Path]:
    """Existing personal folders (also their OneDrive copies); the only places a file may be opened from."""
    home, out = _home(), []
    bases = [home] + [Path(v) for k, v in os.environ.items() if k.upper() in ("ONEDRIVE", "ONEDRIVECONSUMER", "ONEDRIVECOMMERCIAL") and v]
    for base in bases:
        for name in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"):
            p = base / name
            if p.is_dir() and p not in out:
                out.append(p)
    return out


def is_inside_personal_roots(path: Path, roots: list[Path] | None = None) -> bool:
    try:
        rp = Path(path).resolve()
    except OSError:
        return False
    for r in roots if roots is not None else personal_roots():
        try:
            rp.relative_to(r.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def folder_entry(query: str, roots: list[Path] | None = None) -> dict | None:
    q = " ".join(str(query).lower().replace("folder", "").split())
    canon = FOLDER_NAMES.get(q)
    if not canon:
        return None
    for r in roots if roots is not None else personal_roots():
        if r.name == canon:
            return {"id": "folder:" + canon.lower(), "name": canon + " folder", "kind": "folder", "path": str(r), "source": "your folders"}
    return None


def settings_entry(query: str) -> dict | None:
    q = " ".join(str(query).lower().split())
    uri = SETTINGS.get(q) or SETTINGS.get(q + " settings")
    return {"id": "settings:" + q, "name": q.title(), "kind": "settings", "uri": uri, "source": "Windows Settings"} if uri else None


def file_allowed(path: Path, roots: list[Path] | None = None) -> bool:
    p = Path(path)
    return p.suffix.lower() not in DENY_SUFFIXES and p.suffix != "" and is_inside_personal_roots(p, roots) and p.is_file() and not p.is_symlink()


def find_files(query: str, roots: list[Path] | None = None, limit: int = 6, max_depth: int = 4) -> list[dict]:
    """Files whose name contains every word of the query, newest first. Bounded in entries, depth and time; skips program folders and Jarvis' own data."""
    words = [w for w in query.lower().replace(".", " ").split() if w]
    if not words:
        return []
    t0, seen, hits = time.monotonic(), 0, []
    for root in roots if roots is not None else personal_roots():
        stack = [(Path(root), 0)]
        while stack:
            d, depth = stack.pop()
            try:
                it = list(os.scandir(d))
            except OSError:
                continue
            for e in it:
                seen += 1
                if seen > MAX_SCAN_ENTRIES or time.monotonic() - t0 > MAX_SECONDS:
                    return _rank(hits, limit)
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if depth < max_depth and e.name.lower() not in SKIP_DIRS:
                            stack.append((Path(e.path), depth + 1))
                        continue
                    name = e.name.lower()
                    p = Path(e.path)
                    if all(w in name for w in words) and file_allowed(p, roots if roots is not None else None):
                        hits.append((e.stat().st_mtime, p))
                except OSError:
                    continue
    return _rank(hits, limit)


def _rank(hits, limit):
    out = []
    for _m, p in sorted(hits, key=lambda x: -x[0])[:limit]:
        out.append({"id": "file:" + str(p).lower(), "name": p.name, "kind": "file", "path": str(p), "source": "your files"})
    return out
