# Capability matrix

Generated from `jarvis.operator.matrix` and checked against the capability registry by the tests.
**Nothing here has been run on a physical Windows PC.** Class 1 means tests with real files and a *fake* Windows layer.

| # | Class | Meaning |
|---|---|---|
| 1 | Implemented and verified by tests | |
| 2 | Implemented, Windows part not verified on a real PC | |
| 3 | Partial | |
| 4 | Not implemented | |
| 5 | Needs something outside Jarvis | |
| 6 | Not feasible offline / free | |
| 7 | Refused on purpose (unsafe) | |

183 registered capabilities.

## Class 1: Implemented and verified by tests

- **File search by name, type, size, date and content** — `files.empty_folders, files.inventory, files.large, files.missing_project_files, files.old, files.recent, files.search, files.temp, files.unusual`. Searches only the folders you allowed.
- **Duplicates, hashes, comparing files** — `files.compare, files.duplicates, files.hash, files.verify_hash`. Duplicates are only reported, never removed.
- **Organize / move / copy / rename / create folders with plan, undo** — `files.copy, files.create_folder, files.move, files.organize_apply, files.organize_plan, files.organize_undo, files.rename`. Never overwrites; a plan can be undone.
- **Delete and restore (recoverable)** — `files.delete, files.restore`. Goes to a Jarvis recovery folder, not the Windows Recycle Bin. Always asks.
- **ZIP create/list/verify/extract/compare with path-traversal protection** — `zip.compare, zip.create, zip.extract, zip.list, zip.split, zip.verify`. RAR, 7z and TAR are not handled (ZIP only).
- **Backups: full, incremental, verify, restore, compare, retention** — `backup.compare, backup.create, backup.list, backup.missing, backup.report, backup.restore, backup.verify`. Restores into a new folder; the original is never overwritten. No cloud sync.
- **Documents: read, create (docx/txt), compare, proofreading prepare** — `docs.compare, docs.create, docs.proofread_prepare, docs.read, draft.spam_check`. Document text is treated as data, never as instructions. Doc layout/merge/split of Word files and scanned-PDF OCR are not implemented.
- **PDF: search, extract pages, merge, split** — `pdf.extract_pages, pdf.merge, pdf.search, pdf.split`. Encrypted PDFs and scanned PDFs (OCR) are not handled.
- **Spreadsheets: summary, sort, dedupe, compare, chart** — `sheet.chart, sheet.compare, sheet.dedupe, sheet.sort, sheet.summary`. Writes a new file; formulas and macros are not run.
- **Images: info, resize, convert, compress, crop, thumbnails, compare, inventory** — `image.compare, image.compress, image.convert, image.crop, image.info, image.inventory, image.resize, image.thumbnail`. Writes a new file; the original is kept. No background removal or AI edit.
- **Programming: project inventory, TODOs, run tests, parse results** — `project.find_todos, project.inventory, tests.run`. Test runs always ask. Only unittest, pytest and npm test.
- **Tasks, reminders, planning, calendar (local)** — `calendar.add, calendar.list, tasks.add, tasks.complete, tasks.delete, tasks.list, tasks.plan`. Local only. No Google/Outlook calendar sync.
- **Workflows: save, run, schedule, event triggers, export/import, history** — `workflow.delete, workflow.duplicate, workflow.export, workflow.history, workflow.import, workflow.list, workflow.pause, workflow.run, workflow.save, workflow.show`. Every step passes the same gate; imports are untrusted and paused.
- **Self-check / diagnostics** — `diag.self_check`. Reports what Jarvis itself can and cannot do right now.

## Class 2: Implemented, Windows part not verified on a real PC

- **Window control: list, active, minimize/maximize/close, snap, resize, monitors, frozen windows** — `window.action, window.active, window.check_frozen, window.list, window.resize, window.snap, window.to_monitor`. Close always asks; Windows Security and Jarvis itself are protected. Uses the Windows API through ctypes.
- **Virtual desktops, Task View, Start, search, show desktop** — `desktop.close, desktop.new, desktop.next, desktop.prev, desktop.search, desktop.show_desktop, desktop.start_menu, desktop.task_view`. Sent as the real Windows shortcuts; the result cannot be confirmed from outside, so it is reported as 'sent'.
- **Power: lock, sleep, hibernate, restart, shutdown, sign out, cancel** — `power.cancel, power.hibernate, power.idle, power.lock, power.restart, power.shutdown, power.signout, power.sleep`. Every power action always asks right before it runs. Hibernate depends on the PC having it enabled.
- **Volume, mute, media keys, screenshots, screen recording** — `media.record_screen, media.screenshot, media.set_volume, media.volume`. Screen recording needs ffmpeg to be installed; not bundled.
- **Open files and folders** — `files.open, files.reveal`. Programs and scripts are refused.
- **Storage: drives, biggest folders, cleanup candidates, growth, drive health** — `storage.biggest_folders, storage.cleanup_candidates, storage.drives, storage.health, storage.report`. SMART detail beyond Windows' own health status is not read.
- **Running apps, installed apps, close/force-close/restart** — `apps.close, apps.force_close, apps.installed, apps.restart, apps.running`. Force close always asks. Installing, uninstalling and updating software is NOT implemented.
- **System monitoring: CPU, RAM, battery, uptime, processes, heavy apps, startup apps, devices, print queue, power plan** — `system.battery, system.cpu, system.devices, system.heavy, system.power_plan, system.print_queue, system.process_report, system.processes, system.ram, system.resources, system.service_control, system.services` and 2 more. Read-only. Per-app network bandwidth, GPU load and FPS are not available.
- **Windows services (start/stop)** — `system.service_control`. Critical services (Defender, firewall, Windows Update, RPC, ...) are refused.
- **Network: status, DNS, latency, throughput, diagnose, IP config** — `network.diagnose, network.dns, network.ip_config, network.latency, network.open_settings, network.status, network.throughput`. Does not change network settings, reset adapters, or connect to Wi-Fi.
- **Security: Defender, firewall, signatures, risky files, credential-looking files, app access, warnings explained** — `security.app_access, security.credential_files, security.defender, security.explain, security.firewall, security.open_credentials, security.report, security.risky_files, security.signature`. Read-only. It does not scan, quarantine or change security settings, and cannot read stored passwords.
- **Windows Update status, drivers, event log, crashes, error codes** — `events.crashes, events.driver_pages, events.drivers, events.explain_code, events.open_update, events.recent_errors, events.update_status`. Opens vendor/driver pages; it never installs drivers or updates.
- **Git: status, log, diff, branches, stage, commit, push/pull, tags** — `git.branches, git.commit, git.conflicts, git.create_branch, git.destructive, git.diff, git.fetch, git.log, git.pull, git.push, git.stage, git.status` and 2 more. Needs git installed. Destructive git commands are refused.
- **Gaming mode: start/stop, close list, launchers, Steam library, saves, requirements** — `games.backup_saves, games.launcher, games.list, games.requirements, games.saves, gaming.set_close_list, gaming.start, gaming.status, gaming.stop`. No FPS, no driver changes, no overlays. Game saves are copied, never changed.

## Class 3: Partial

- **Mouse and keyboard at coordinates, typing into other apps, drag and drop** — `media.key`. Only a fixed list of media keys is sent. Free clicking/typing is the existing Desktop Control (docs/DESKTOP_CONTROL.md), not this Operator.
- **Settings: open pages, wallpaper, brightness** — `settings.brightness, settings.open, settings.wallpaper`. Bluetooth, Wi-Fi, resolution, Night light and similar are opened, not toggled; you press the switch.
- **Printers** — `system.print_queue`. Queue is shown. Printing, clearing and choosing printers are not done.
- **Email drafts** — `draft.email`. Opens a draft in your mail app for YOU to send. Reading mailboxes, sending and attachments are not implemented.

## Class 4: Not implemented

- **Software install / uninstall / update / winget**. Not implemented. Jarvis will not run installers.
- **Browser automation: tabs, forms, bookmarks, downloads, logins**. Not implemented. Jarvis can open sites you name (Quick Launch) and research through its normal search; it does not drive a browser or fill forms.
- **Messaging apps (Discord/WhatsApp/Telegram etc.)**. Not implemented.
- **Camera capture, face/gesture features**. Not implemented.

## Class 5: Needs something outside Jarvis

- **Audio/video conversion** — `media.convert`. Needs ffmpeg installed separately.

## Class 6: Not feasible offline / free

- **Cloud, paid or online-only abilities**. The Operator works offline and at no cost; nothing needs an account.

## Class 7: Refused on purpose (unsafe)

- **Passwords, payments, bank/credential entry, security-setting changes, admin elevation**. Refused on purpose. Jarvis never types passwords or payment details and never asks Windows for administrator rights.
- **Arbitrary shell/PowerShell/registry commands and macros**. Refused on purpose. Every external program is started from a fixed argument list inside Jarvis.

