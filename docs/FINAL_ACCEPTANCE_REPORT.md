# Final acceptance report (milestones 2-5), written 2026-10-29 on branch work/milestones-2-5

Baseline: commit 8f5fab3 (code identical to CI-green 665e400), archive ProjectJarvis-source-8f5fab3.zip, SHA-256 528a168e76a86faeb71491335c67b4b8ff4067dda2e3a6f69ee121d7117e671c. No rubric-based completion percentage is given because no rubric has been agreed; the matrix below is the status.

**Verdict: NOT release-ready.** The core is built and well tested with fakes, one real-model reply is proven on a hosted runner, and nothing has been run on a person's PC.

Evidence levels: **R** real execution (real code, no fakes; where it ran is stated), **F** automated tests with fakes or mocks, **S** source inspection only, **N** not verified.

## 1. Inference and runtime

| Requirement | Level | Evidence and limits |
|---|---|---|
| Genuine model loads and answers through the packaged app | R (hosted Windows CPU runner) | windows-ci run 40955846622 at 665e400: 311-tensor Qwen3-1.7B Q4_K_M, 1,282,439,264 bytes, runtime 0.3.36, reply "The capital of France is Paris." in 12.31 s. One prompt, one reply. Pretrained Qwen3; no Jarvis model has been trained |
| Model provenance | **Unresolved** | The SHA-256 d2387ca2... is an observed value pinned for consistency across runs. It is NOT confirmed against the publisher; the size matches the publisher's listing. Authenticity is not claimed. The app's own download check compares against the host's metadata, which detects corruption, not a malicious host |
| Missing model: clear message, no network | R (Linux, this session) | fresh profile: chat prints "No model is installed or selected...", exit 2; selftest exit 2; models verify with a garbage file: "not installed", exit 3 |
| Missing runtime, corrupt or synthetic weights | F plus R (CI) | tests p39, p41, p65; frozen-worker gate in CI refuses a header-only GGUF with the real exe |
| No silent cloud fallback | F, S | test_p39_no_silent_cloud (engine unavailable means a stop with exit 2, not online); all HTTP goes through net/policy.py (the only socket/urllib import in src); every feature is OFF by default |
| Network-disabled operation | R (older runs) | build-windows 40431278616: reply with the app's network blocked by a firewall rule (older commit). Not re-run on current code |
| Timeouts, cancellation, shutdown | F | 18 timeout, 51 cancel and 13 shutdown tests; selftest worker is a separate process with load/generate timeouts. Not exercised against a real model for cancellation on the current commit |
| UI status distinguishes loaded / unavailable / error / simulated | F, S | tests label fakes; the readiness page states "verified / not verified"; real-Qt tests are SKIPPED (PySide6 absent here) |

## 2. Product features

| Area | Level | Notes |
|---|---|---|
| Startup, shutdown, settings, fresh profile | F; R for CLI paths | fresh-profile CLI runs above; GUI window shown in CI on an older commit only |
| SQLite persistence, memory add / refuse secret / correct / export / delete | **R (Linux, this session)** plus F | Real CLI run: a normal preference saved; an API-key-like line and a password line refused; export lists memories, conversations, notes and logs; forget-all --yes empties the profile and its message states that backups, undo points and your own export files are not removed (honest). Secret check is best effort |
| Wake word, mute/disable, mic state, shutdown | F only | Always-listening default ON on a fresh profile (owner decision 2026-10-29) is implemented in AppController.voice_autostart; mic opens only with a sound device and a Vosk model; explicit OFF remembered; no raw audio written (test scans for writes). **No physical microphone, speaker, Vosk model or Kokoro run exists** |
| Speech output (SAPI/Kokoro) | F only | N on real hardware |
| Desktop tools, confirmations, permissions, audit, act-observe-verify | F plus S | fake backend drives the real policy and executor (38 confirm, 42 permission, 31 audit tests); no real clicks in other apps have been run. Spoken "yes" cannot answer a pending confirmation (test). App launch uses an indexed absolute path without a shell |
| Personality styles and modes (general, coding, research, gaming, productivity) | F | style/mode tests; wording quality with a real model is unmeasured except a small intel benchmark on an older commit |
| Privacy, retention, secrets | R (partly), F, S | secret refusal above; telemetry and online features OFF by default; no developer paths or key-shaped strings found in src or packaging by scan |
| Accessibility basics | F, S, N | accessible names set in code; real screen-reader/keyboard/DPI checks need a PC |
| Qwen3-8B | N | never measured; not a default; not recommended |

## 3. Accounts and sign-in (finding, corrected)

An earlier assumption that no account system exists was wrong. **Jarvis Online exists as an optional, opt-in feature**: a command-line client (jarvis online ...) and a standard-library server under server/jarvis_server (accounts, scrypt password hashes, sessions, consent records, export, delete, learning consent OFF by default). Facts: tested only on localhost over plain HTTP; no AI model is behind it (it answers 503); the Windows token encryption (DPAPI) is written but never run on Windows; no server address is built in; there is **no GUI sign-in screen**; there is **no admin panel and no ban or appeal feature**. Local inference does not depend on it. The admin-visibility and ban-appeal policy you described is a future design requirement only and is not implemented. No cloud authentication work was started in this session.

## 4. Security and privacy review (targeted)

| Check | Result |
|---|---|
| Dangerous API patterns (shell=True, eval, exec, pickle, unsafe archive extraction, disabled TLS verification) | None found in src |
| Process launching | subprocess is used with argument lists; app launch uses an index path, no shell; the OCR helper runs a PowerShell script shipped inside the install folder (same trust as the exe). Tesseract is found through PATH (low risk, noted) |
| Tokens | stored per user: DPAPI on Windows (not run there), owner-only file elsewhere; never written in plain text on Windows if encryption fails |
| Export files hold personal data | **Fixed:** exports from the memory, mission and window export paths were created world-readable on POSIX. They now go through jarvis.security.private_file.write_private_text (owner-only). Regression tests in tests/test_p75_private_export.py (3; mutation-checked). On Windows the file inherits the profile folder's permissions; no ACL change |
| Installer | per-user install, no admin; uninstall asks before deleting data; unsigned |
| Update mechanism | the update check verifies a signature on its metadata (S; the update path was not otherwise reviewed or executed) |
| Open risks | SQLite database is not encrypted at rest (relies on the Windows account); SHA-256 provenance above; no third-party dependency audit (pip-audit) was possible offline |

## 5. Windows release preparation

Implemented: PyInstaller spec with mandatory-file and mandatory-package build blocks, Inno Setup per-user installer, SHA256SUMS, frozen-worker gate, silent install check and a clean-install sandbox script, all exercised in windows-ci. Not done: signing (no purchase made), upgrade-over-install (needs a second version), clean consumer machine, SmartScreen/antivirus behaviour, physical PC acceptance. **CI green does not mean the installer is ready.**

Release-readiness checklist (all must be ticked with evidence): [ ] installer built from a tagged CI-green commit; [ ] installer SHA-256 recorded; [ ] clean install on a new Windows 11 account; [ ] first-run model download with consent works on a real PC; [ ] real reply, Stop, close during reply; [ ] mic/speaker acceptance (Part 6 of the runbook); [ ] desktop-control acceptance (Part 7); [ ] uninstall keeps or removes data as chosen; [ ] Windows cleanup flake resolved (20 clean runs over days); [ ] Prompts 1-5 reviewed; [ ] signing decision made by the owner.

## 6. Minimal manual Windows 11 sequence (about 45 minutes)

Use docs/WINDOWS_ACCEPTANCE_RUNBOOK.md for detail. Order: 1) build with build_installer.bat (or use a CI installer you approve) and note the SHA-256; 2) install in a new standard account, start Jarvis, screenshot; 3) accept the model download, see a reply to "What is 2+2?"; 4) press Stop on a long answer, close during a reply, reopen; 5) add a memory, restart, export, then Forget everything; 6) disconnect the network and chat; 7) with mic and speakers: wake phrase x10, mute with Ctrl+Shift+M, unplug the mic; 8) desktop control in C:\jarvis-test\work: answer No then Yes; 9) uninstall answering No then Yes to data removal; 10) send the evidence list at the end of the runbook.

## 7. Unresolved items

WinError 32 flake: 6 consecutive clean runs since the fix (about 3.5 h), target 20 over days including slow runs; not resolved. No physical PC acceptance. Installer unsigned. PySide6 real-Qt tests skipped here (they run in CI on Windows only if PySide6 is installed there; check before relying on them). Qwen3-8B unmeasured. Prompts 1-5 not supplied and unreviewed. Model hash provenance unconfirmed.
