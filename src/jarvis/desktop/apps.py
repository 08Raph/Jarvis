"""Find installed programs the way the Start menu does: Start-menu shortcuts (.lnk), Store/UWP apps (Get-StartApps), and
the user's own allow-list. Uninstallers are never listed. Installers are flagged so starting one always needs confirmation."""
from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

INSTALLER_NAME = re.compile(r"\b(setup|install(er)?|update(r)?)\b", re.I)
UNINSTALLER_NAME = re.compile(r"\b(uninstall|uninst|remove)\b", re.I)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Windows PowerShell writes pipes in the OEM code page, so non-ASCII names would be garbled. The JSON is therefore escaped to
# pure ASCII (\\uXXXX) inside PowerShell; json.loads turns the escapes back into the real characters. NOT RUN ON WINDOWS.
_STARTAPPS_PS = ("$j = Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress; "
                 "if ($j) { [regex]::Replace($j, '[^\\x00-\\x7F]', [System.Text.RegularExpressions.MatchEvaluator]"
                 "{ param($m) ('\\u{0:x4}' -f [int][char]$m.Value) }) }")


def lnk_target(path: Path) -> str | None:
    """Local target path of a Windows shortcut (MS-SHLLINK LinkInfo.LocalBasePath). None for advertised/virtual shortcuts."""
    try:
        b = Path(path).read_bytes()[:65536]
        if len(b) < 0x4C or struct.unpack_from("<I", b, 0)[0] != 0x4C:
            return None
        flags = struct.unpack_from("<I", b, 0x14)[0]
        off = 0x4C
        if flags & 0x1:                                     # HasLinkTargetIDList
            off += 2 + struct.unpack_from("<H", b, off)[0]
        if not flags & 0x2:                                 # HasLinkInfo
            return None
        li = off
        size, hsize, liflags, _vol, lbp, _cnr, cps = struct.unpack_from("<7I", b, li)
        if not liflags & 0x1:
            return None
        if hsize >= 0x24:
            lbpu = struct.unpack_from("<I", b, li + 0x1C)[0]
            end = b.find(b"\x00\x00", li + lbpu)
            while end != -1 and (end - (li + lbpu)) % 2:
                end = b.find(b"\x00\x00", end + 1)
            base = b[li + lbpu:end].decode("utf-16-le", "replace") if end > 0 else ""
        else:
            end = b.find(b"\x00", li + lbp)
            base = b[li + lbp:end].decode("mbcs" if sys.platform == "win32" else "cp1252", "replace")
        sfx_end = b.find(b"\x00", li + cps)
        suffix = b[li + cps:sfx_end].decode("cp1252", "replace") if cps and sfx_end > 0 else ""
        return (base + suffix) or None
    except (OSError, struct.error, ValueError):
        return None


def start_menu_dirs() -> list[Path]:
    out = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            out.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [p for p in out if p.is_dir()]


def from_start_menu() -> list[dict]:
    apps = []
    for root in start_menu_dirs():
        for lnk in root.rglob("*.lnk"):
            name = lnk.stem
            if UNINSTALLER_NAME.search(name):
                continue
            target = lnk_target(lnk)
            apps.append({"id": "lnk:" + str(lnk.relative_to(root)).replace("\\", "/").lower(), "name": name, "kind": "lnk",
                         "path": str(lnk), "target": target, "source": "Start menu shortcut"})
    return apps


def from_store() -> list[dict]:
    if sys.platform != "win32":
        return []
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        r = subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-Command",
                            _STARTAPPS_PS],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                           creationflags=NO_WINDOW)
        data = json.loads(r.stdout) if r.stdout.strip() else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        aumid, name = str(d.get("AppID") or ""), str(d.get("Name") or "")
        # Only packaged-app ids (Publisher.App_hash!AppId); desktop .lnk entries are already covered above.
        if "!" in aumid and name and not UNINSTALLER_NAME.search(name):
            out.append({"id": "aumid:" + aumid.lower(), "name": name, "kind": "aumid", "aumid": aumid, "target": None,
                        "source": "Windows Store / packaged app"})
    return out


def from_allowlist(allow: dict[str, str]) -> list[dict]:
    out = []
    for key, exe in (allow or {}).items():
        p = Path(exe)
        if not p.is_absolute() and sys.platform == "win32":
            p = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / exe
        if p.is_file():
            out.append({"id": "exe:" + key.lower(), "name": key, "kind": "exe", "path": str(p), "target": str(p),
                        "source": "your app allow-list (config)"})
    return out


def discover(allow: dict[str, str]) -> list[dict]:
    seen, out = set(), []
    try:
        from jarvis.desktop import games
        found_games = games.discover()          # Epic and Steam games and game shortcuts; read-only file reads
    except Exception:      # noqa: BLE001 - never let the games scan break the program scan
        found_games = []
    for a in from_allowlist(allow) + from_start_menu() + from_store() + found_games:
        if a["id"] not in seen:
            seen.add(a["id"])
            out.append(a)
    return sorted(out, key=lambda a: a["name"].lower())


def is_installer(app: dict) -> bool:
    t = (app.get("target") or app.get("path") or "").lower()
    return bool(INSTALLER_NAME.search(app.get("name", ""))) or t.endswith((".msi", ".msix", ".appx", ".appinstaller")) \
        or bool(re.search(r"(setup|install)[^\\/]*\.exe$", t))


def match(apps: list[dict], query: str) -> list[dict]:
    q = query.strip().lower()
    by_id = [a for a in apps if a["id"] == q]
    if by_id:
        return by_id
    exact = [a for a in apps if a["name"].lower() == q]
    if exact:
        return exact
    words = q.split()
    return [a for a in apps if all(w in a["name"].lower() for w in words)]
