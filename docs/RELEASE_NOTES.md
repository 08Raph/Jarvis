# Release notes - Project JARVIS 0.6.0 (preview)

**Status: preview. Source-level work only. No Windows executable or installer has been built or run by the authors of this change.**
Not security-approved. Not production-ready.

## What is new in 0.6.0
- A desktop window (`jarvis-gui`, PySide6): Chat, Missions, Schedules, Memory, Safety and Status pages. Light and dark themes, 100/125/150% text size,
  keyboard shortcuts (Ctrl+1 to Ctrl+6), visible focus rings.
- The window calls the same code as the command line. It cannot change permissions: the Safety page shows them read-only, and every confirmation
  dialog defaults to "Don't allow" and is refused automatically if nobody answers within five minutes.
- Start-up checks: data folder, one running window per profile, honest model status, redacted `jarvis diagnostics`.
- One authoritative version (`src/jarvis/version.py`), `build_info.json`, a shared asset manifest and a build checker that fails clearly.
- Windows build prepared: PyInstaller spec for `jarvis.exe` (console) and `jarvis-gui.exe` (window), Inno Setup script, build script, CI workflow.

## Release-ready versus experimental
| Area | Level | Evidence |
|---|---|---|
| Source tests, memory, missions, controller logic | tested on Linux | automated tests |
| Window (Qt) | **experimental** | compiled and name-checked only; never started in this environment |
| Windows build and installer | **prepared, not built** | no Windows available |
| Local model chat with Qwen3-1.7B | **unverified** | no real weights or runtime available |
| Voice, desktop control, OCR, gaming, monitoring | **experimental** | Linux simulation only |

See `docs/PROMPT10_REVIEW.md` for the release verification matrix.

## Supported systems (intended, unverified)
Windows 11 or Windows 10 1809+, 64-bit; 8 GB RAM recommended, 4 GB minimum for the 1.7B model; about 3 GB free disk; no GPU needed.

## Known limitations
- Schedules run only while Jarvis is open and checking; there is no background service.
- Permissions are changed with the command line (`jarvis desktop enable|disable ...`), not in the window.
- Voice and microphone, screen/OCR, gaming and monitoring pages are status only in the window; use the command line.
- No code signing: Windows SmartScreen will warn about the installer.
