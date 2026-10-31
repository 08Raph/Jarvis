"""The honest capability matrix: every area of the Windows-operator specification, the class it really is in, the capabilities behind it and what is missing.

Classes (the spec's own list):
 1 implemented and verified by automated tests (logic that runs the same everywhere; the Windows layer is faked)
 2 implemented, but its Windows-only part has NOT been run on a real Windows PC (ctypes / PowerShell / Windows tools)
 3 partially implemented
 4 not implemented
 5 depends on something outside Jarvis (a cloud account, a vendor tool, hardware, an installed program)
 6 not feasible offline / without cost / without a vendor API
 7 deliberately refused (unsafe)
Nothing here has been run on a physical Windows PC. Class 1 means tests with real files and a fake Windows layer, nothing more."""
from __future__ import annotations

CLASSES = {1: "Implemented and verified by tests", 2: "Implemented, Windows part not verified on a real PC", 3: "Partial", 4: "Not implemented",
           5: "Needs something outside Jarvis", 6: "Not feasible offline / free", 7: "Refused on purpose (unsafe)"}

# (area, class, capability ids or prefix.* patterns, what is missing / the honest note)
ROWS: list[tuple[str, int, tuple[str, ...], str]] = [
    ("Window control: list, active, minimize/maximize/close, snap, resize, monitors, frozen windows", 2, ("window.*",), "Close always asks; Windows Security and Jarvis itself are protected. Uses the Windows API through ctypes."),
    ("Virtual desktops, Task View, Start, search, show desktop", 2, ("desktop.*",), "Sent as the real Windows shortcuts; the result cannot be confirmed from outside, so it is reported as 'sent'."),
    ("Mouse and keyboard at coordinates, typing into other apps, drag and drop", 3, ("media.key",), "Only a fixed list of media keys is sent. Free clicking/typing is the existing Desktop Control (docs/DESKTOP_CONTROL.md), not this Operator."),
    ("Power: lock, sleep, hibernate, restart, shutdown, sign out, cancel", 2, ("power.*",), "Every power action always asks right before it runs. Hibernate depends on the PC having it enabled."),
    ("Volume, mute, media keys, screenshots, screen recording", 2, ("media.volume", "media.set_volume", "media.screenshot", "media.record_screen"), "Screen recording needs ffmpeg to be installed; not bundled."),
    ("File search by name, type, size, date and content", 1, ("files.search", "files.large", "files.old", "files.recent", "files.empty_folders", "files.temp", "files.unusual", "files.inventory", "files.missing_project_files"), "Searches only the folders you allowed."),
    ("Duplicates, hashes, comparing files", 1, ("files.duplicates", "files.hash", "files.verify_hash", "files.compare"), "Duplicates are only reported, never removed."),
    ("Organize / move / copy / rename / create folders with plan, undo", 1, ("files.organize_plan", "files.organize_apply", "files.organize_undo", "files.create_folder", "files.copy", "files.rename", "files.move"), "Never overwrites; a plan can be undone."),
    ("Delete and restore (recoverable)", 1, ("files.delete", "files.restore"), "Goes to a Jarvis recovery folder, not the Windows Recycle Bin. Always asks."),
    ("Open files and folders", 2, ("files.open", "files.reveal"), "Programs and scripts are refused."),
    ("ZIP create/list/verify/extract/compare with path-traversal protection", 1, ("zip.*",), "RAR, 7z and TAR are not handled (ZIP only)."),
    ("Backups: full, incremental, verify, restore, compare, retention", 1, ("backup.*",), "Restores into a new folder; the original is never overwritten. No cloud sync."),
    ("Storage: drives, biggest folders, cleanup candidates, growth, drive health", 2, ("storage.*",), "SMART detail beyond Windows' own health status is not read."),
    ("Running apps, installed apps, close/force-close/restart", 2, ("apps.*",), "Force close always asks. Installing, uninstalling and updating software is NOT implemented."),
    ("Software install / uninstall / update / winget", 4, (), "Not implemented. Jarvis will not run installers."),
    ("System monitoring: CPU, RAM, battery, uptime, processes, heavy apps, startup apps, devices, print queue, power plan", 2, ("system.*",), "Read-only. Per-app network bandwidth, GPU load and FPS are not available."),
    ("Windows services (start/stop)", 2, ("system.service_control",), "Critical services (Defender, firewall, Windows Update, RPC, ...) are refused."),
    ("Settings: open pages, wallpaper, brightness", 3, ("settings.*",), "Bluetooth, Wi-Fi, resolution, Night light and similar are opened, not toggled; you press the switch."),
    ("Network: status, DNS, latency, throughput, diagnose, IP config", 2, ("network.*",), "Does not change network settings, reset adapters, or connect to Wi-Fi."),
    ("Printers", 3, ("system.print_queue",), "Queue is shown. Printing, clearing and choosing printers are not done."),
    ("Security: Defender, firewall, signatures, risky files, credential-looking files, app access, warnings explained", 2, ("security.*",), "Read-only. It does not scan, quarantine or change security settings, and cannot read stored passwords."),
    ("Windows Update status, drivers, event log, crashes, error codes", 2, ("events.*",), "Opens vendor/driver pages; it never installs drivers or updates."),
    ("Documents: read, create (docx/txt), compare, proofreading prepare", 1, ("docs.*", "draft.spam_check"), "Document text is treated as data, never as instructions. Doc layout/merge/split of Word files and scanned-PDF OCR are not implemented."),
    ("PDF: search, extract pages, merge, split", 1, ("pdf.*",), "Encrypted PDFs and scanned PDFs (OCR) are not handled."),
    ("Spreadsheets: summary, sort, dedupe, compare, chart", 1, ("sheet.*",), "Writes a new file; formulas and macros are not run."),
    ("Images: info, resize, convert, compress, crop, thumbnails, compare, inventory", 1, ("image.*",), "Writes a new file; the original is kept. No background removal or AI edit."),
    ("Audio/video conversion", 5, ("media.convert",), "Needs ffmpeg installed separately."),
    ("Programming: project inventory, TODOs, run tests, parse results", 1, ("project.*", "tests.run"), "Test runs always ask. Only unittest, pytest and npm test."),
    ("Git: status, log, diff, branches, stage, commit, push/pull, tags", 2, ("git.*",), "Needs git installed. Destructive git commands are refused."),
    ("Gaming mode: start/stop, close list, launchers, Steam library, saves, requirements", 2, ("gaming.*", "games.*"), "No FPS, no driver changes, no overlays. Game saves are copied, never changed."),
    ("Tasks, reminders, planning, calendar (local)", 1, ("tasks.*", "calendar.*"), "Local only. No Google/Outlook calendar sync."),
    ("Email drafts", 3, ("draft.email",), "Opens a draft in your mail app for YOU to send. Reading mailboxes, sending and attachments are not implemented."),
    ("Workflows: save, run, schedule, event triggers, export/import, history", 1, ("workflow.*",), "Every step passes the same gate; imports are untrusted and paused."),
    ("Self-check / diagnostics", 1, ("diag.*",), "Reports what Jarvis itself can and cannot do right now."),
    ("Browser automation: tabs, forms, bookmarks, downloads, logins", 4, (), "Not implemented. Jarvis can open sites you name (Quick Launch) and research through its normal search; it does not drive a browser or fill forms."),
    ("Messaging apps (Discord/WhatsApp/Telegram etc.)", 4, (), "Not implemented."),
    ("Camera capture, face/gesture features", 4, (), "Not implemented."),
    ("Passwords, payments, bank/credential entry, security-setting changes, admin elevation", 7, (), "Refused on purpose. Jarvis never types passwords or payment details and never asks Windows for administrator rights."),
    ("Arbitrary shell/PowerShell/registry commands and macros", 7, (), "Refused on purpose. Every external program is started from a fixed argument list inside Jarvis."),
    ("Cloud, paid or online-only abilities", 6, (), "The Operator works offline and at no cost; nothing needs an account."),
]


def _matches(pattern: str, cap_id: str) -> bool:
    return cap_id.startswith(pattern[:-1]) if pattern.endswith("*") else cap_id == pattern


def covered(registry) -> dict[str, list[int]]:
    """capability id -> the row numbers that list it."""
    out: dict[str, list[int]] = {k: [] for k in registry}
    for i, (_a, _c, pats, _n) in enumerate(ROWS):
        for p in pats:
            for k in registry:
                if _matches(p, k):
                    out[k].append(i)
    return out


def render(registry) -> str:
    rows = ["# Capability matrix", "", "Generated from `jarvis.operator.matrix` and checked against the capability registry by the tests.",
            "**Nothing here has been run on a physical Windows PC.** Class 1 means tests with real files and a *fake* Windows layer.", "",
            "| # | Class | Meaning |", "|---|---|---|"]
    rows += [f"| {k} | {v} | |" for k, v in CLASSES.items()]
    rows += ["", f"{len(registry)} registered capabilities.", ""]
    for cls in sorted(CLASSES):
        sel = [(i, r) for i, r in enumerate(ROWS) if r[1] == cls]
        if not sel:
            continue
        rows += [f"## Class {cls}: {CLASSES[cls]}", ""]
        for _i, (area, _c, pats, note) in sel:
            ids = sorted({k for k in registry for p in pats if _matches(p, k)})
            rows.append(f"- **{area}**" + (f" — `{', '.join(ids[:12])}`" + (f" and {len(ids) - 12} more" if len(ids) > 12 else "") if ids else "") + f". {note}")
        rows.append("")
    return "\n".join(rows)


if __name__ == "__main__":
    from jarvis.operator import core
    core.load_all()
    print(render(core.REGISTRY))
