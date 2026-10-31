"""Find installed GAMES and desktop shortcuts the Start-menu scan misses: Epic Games Launcher games (Fortnite and others), Steam games, .url shortcuts and
shortcuts on the Desktop. Read-only: it only reads small text files (Epic manifests, Steam library files and appmanifests, shortcut files). Nothing is started here.

Every entry has the same shape as the entries in ``jarvis.desktop.apps``: ``{"id", "name", "kind", "source", ...}``. Games are started through their launcher's own protocol
link (``steam://rungameid/<id>``, ``com.epicgames.launcher://apps/...``), and ONLY those two schemes are ever accepted (see ``allowed_uri``). NOT RUN ON A REAL PC by
the authoring sandbox: the file formats are tested with fake files that follow the layout these launchers document."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

ALLOWED_URI_SCHEMES = ("steam://", "com.epicgames.launcher://")
SKIP_NAMES = re.compile(r"(redistributable|steamworks common|proton\b|steam linux runtime|uninstall|crash ?report|easyanticheat|battleye)", re.I)
MAX_FILE = 262_144


def allowed_uri(uri: str) -> bool:
    u = str(uri or "").strip().lower()
    return u.startswith(ALLOWED_URI_SCHEMES) and len(u) < 600 and not any(c in u for c in ("\n", "\r", "\x00", " ", '"', "'", "|", "&&"))


def _read(p: Path) -> str:
    try:
        with open(p, "rb") as f:
            return f.read(MAX_FILE).decode("utf-8", "replace")
    except OSError:
        return ""


# ------------------------------------------------------------------------------ Epic Games
def epic_manifest_dirs() -> list[Path]:
    pd = os.environ.get("ProgramData") or r"C:\ProgramData"
    return [Path(pd) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"]


def epic_uri(app_name: str, namespace: str = "", catalog_id: str = "") -> str:
    """The link Epic's own shortcuts use: namespace:catalog item:app name (URL-encoded colons); the app name alone is the fallback."""
    if namespace and catalog_id:
        key = quote(f"{namespace}:{catalog_id}:{app_name}", safe="")
    else:
        key = quote(app_name, safe="")
    return f"com.epicgames.launcher://apps/{key}?action=launch&silent=true"


def from_epic(dirs: list[Path] | None = None) -> list[dict]:
    out = []
    for d in dirs if dirs is not None else epic_manifest_dirs():
        try:
            files = sorted(Path(d).glob("*.item"))
        except OSError:
            continue
        for f in files:
            try:
                m = json.loads(_read(f))
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            name, app = str(m.get("DisplayName") or ""), str(m.get("AppName") or "")
            if not name or not app or m.get("bIsIncompleteInstall") or SKIP_NAMES.search(name):
                continue
            if m.get("bIsApplication") is False:           # DLC and add-ons are not programs
                continue
            uri = epic_uri(app, str(m.get("CatalogNamespace") or ""), str(m.get("CatalogItemId") or ""))
            out.append({"id": "epic:" + app.lower(), "name": name, "kind": "uri", "uri": uri, "target": str(m.get("InstallLocation") or "") or None,
                        "source": "Epic Games Launcher"})
    return out


# ------------------------------------------------------------------------------ Steam
def steam_roots() -> list[Path]:
    roots = []
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"), (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        v = winreg.QueryValueEx(k, "SteamPath" if hive == winreg.HKEY_CURRENT_USER else "InstallPath")[0]
                        roots.append(Path(v))
                except OSError:
                    pass
        except ImportError:
            pass
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Steam")
    seen, out = set(), []
    for r in roots:
        k = str(r).lower()
        if k not in seen and r.is_dir():
            seen.add(k)
            out.append(r)
    return out


def steam_libraries(root: Path) -> list[Path]:
    libs = [root / "steamapps"]
    txt = _read(root / "steamapps" / "libraryfolders.vdf")
    for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
        libs.append(Path(m.group(1).replace("\\\\", "\\")) / "steamapps")
    seen, out = set(), []
    for lib in libs:
        k = str(lib).lower()
        if k not in seen and lib.is_dir():
            seen.add(k)
            out.append(lib)
    return out


def from_steam(roots: list[Path] | None = None) -> list[dict]:
    out, seen = [], set()
    for root in roots if roots is not None else steam_roots():
        for lib in steam_libraries(Path(root)):
            try:
                manifests = sorted(lib.glob("appmanifest_*.acf"))
            except OSError:
                continue
            for f in manifests:
                txt = _read(f)
                aid = re.search(r'"appid"\s+"(\d+)"', txt)
                name = re.search(r'"name"\s+"([^"]+)"', txt)
                if not aid or not name or SKIP_NAMES.search(name.group(1)) or aid.group(1) in seen:
                    continue
                seen.add(aid.group(1))
                out.append({"id": "steam:" + aid.group(1), "name": name.group(1), "kind": "uri", "uri": f"steam://rungameid/{aid.group(1)}",
                            "target": None, "source": "Steam"})
    return out


# ------------------------------------------------------------------------------ shortcut files
def desktop_dirs() -> list[Path]:
    out = []
    for env, sub in (("USERPROFILE", "Desktop"), ("PUBLIC", "Desktop"), ("OneDrive", "Desktop")):
        base = os.environ.get(env)
        if base:
            out.append(Path(base) / sub)
    return [p for p in out if p.is_dir()]


def url_shortcut_target(path: Path) -> str | None:
    m = re.search(r"(?im)^URL=(.+)$", _read(path))
    return m.group(1).strip() if m else None


def from_shortcut_files(start_menu: list[Path] | None = None, desktops: list[Path] | None = None) -> list[dict]:
    """``.url`` shortcuts (Steam and Epic put their game shortcuts here) and ``.lnk`` shortcuts on the Desktop."""
    from jarvis.desktop import apps as appdisc
    out = []
    for root in start_menu if start_menu is not None else appdisc.start_menu_dirs():
        try:
            urls = list(Path(root).rglob("*.url"))
        except OSError:
            continue
        for p in urls:
            uri = url_shortcut_target(p)
            if uri and allowed_uri(uri) and not SKIP_NAMES.search(p.stem):
                out.append({"id": "url:" + p.stem.lower(), "name": p.stem, "kind": "uri", "uri": uri, "target": None, "source": "Start menu game shortcut"})
    for d in desktops if desktops is not None else desktop_dirs():
        try:
            items = sorted(Path(d).iterdir())
        except OSError:
            continue
        for p in items:
            suffix = p.suffix.lower()
            if suffix == ".lnk" and not appdisc.UNINSTALLER_NAME.search(p.stem):
                out.append({"id": "desktop:" + p.stem.lower(), "name": p.stem, "kind": "lnk", "path": str(p), "target": appdisc.lnk_target(p),
                            "source": "Desktop shortcut"})
            elif suffix == ".url":
                uri = url_shortcut_target(p)
                if uri and allowed_uri(uri) and not SKIP_NAMES.search(p.stem):
                    out.append({"id": "desktopurl:" + p.stem.lower(), "name": p.stem, "kind": "uri", "uri": uri, "target": None, "source": "Desktop game shortcut"})
    return out


def discover() -> list[dict]:
    """All game and shortcut entries, de-duplicated by id."""
    seen, out = set(), []
    for fn in (from_epic, from_steam, from_shortcut_files):
        try:
            found = fn()
        except Exception:      # noqa: BLE001 - one unreadable launcher must not hide the others
            found = []
        for a in found:
            if a["id"] not in seen:
                seen.add(a["id"])
                out.append(a)
    return out
