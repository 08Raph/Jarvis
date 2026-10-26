# Prompt 10 review: window, Windows packaging and release engineering

## 0. Status in one paragraph
**This deliverable is source-only plus packaging preparation.** It is NOT a tested Windows build and NOT a tested installer. No `.exe`, installer, or screenshot exists,
because this authoring environment is Linux with no Windows, no PySide6 (Qt), no PyInstaller, no Inno Setup, no PyPI access and no Qwen weights.
The window code was written and checked for syntax and undefined names only; **it has never been started**. Nothing here is security-approved or production-ready.

| Concern | Status |
|---|---|
| UI implementation | **Written, never run.** Controller (everything except drawing) is tested; the Qt window is not |
| Source-test success | Yes, on Linux (section 5) |
| Packaging preparation | Done: spec, manifest, checker, build script, installer script, workflow. **Never executed** |
| Actual Windows build | **NOT done** |
| Release readiness | **Not ready.** See the matrix (section 7) |

## 1. Baseline (before any change)
Prompt 9 archive `project-jarvis-p15-prompt9.zip` extracted fresh: `PYTHONPATH=src python -I -m unittest discover -s tests` gave **1130 tests, OK, 4 skipped**
(the opt-in real-model tests, the PyTorch formula test, the P9 real-model planning test). Findings from inspection: there was **no GUI at all** (`ui/` was empty; `[ui]` extra only declared),
the product was a console program; packaging scripts existed (PyInstaller one-folder spec, Inno Setup script, PowerShell build script, GitHub workflow) but had never been run.
Checkpoint of the untouched source: `/home/claude/p10_checkpoint` (not overwritten; earlier zips untouched).

## 2. UI changes (no screenshots)
Screenshots **could not be generated**: Qt is not installed and cannot be installed here. I will not draw a mock-up and call it a screenshot.
What the code is designed to show (to be confirmed by a human on Windows, checklist 6k):
- Sidebar navigation (Chat, Missions, Schedules, Memory, Safety, Status), shortcuts Ctrl+1..6, accessible names on controls, visible focus rings, light/dark following Windows, text scale 100/125/150 %.
- Chat: six visibly different message kinds (You, Jarvis, Action, Problem, Needs your approval, Info). A reply streams from the real engine on a worker thread; Stop cancels it. With no model the transcript says why and what to run; **no reply is ever invented**.
- Missions: list with state (draft, awaiting approval, approved, running, paused, completed, failed, cancelled), plan review with its hash, approve (a Yes/No question defaulting to No, bound to the hash you were shown; refused if the plan changed meanwhile), reject, pause, cancel, start. Progress lines come only from the runner's real callbacks.
- Schedules: create, approve, pause, delete; an opt-in checkbox (off) runs due schedules every 30 s only while the window is open (no background service).
- Memory: review, edit, delete, export, "forget everything" (confirmed first).
- Safety: permissions shown **read-only** (changing them stays in the console, so the window cannot grant itself access); modes (hide tools only); Emergency stop / Clear stop; action history.
- Status: model state, feature table with the evidence for each (e.g. "tested on Linux with a simulated desktop; NOT verified on Windows 11"), optional dependencies, redacted diagnostics, log tail.
- Voice, screen/OCR, gaming/monitoring, coding and research are **status rows only** in the window ("use the command line"); there are no decorative controls for them.
- Every confirmation is a dialog whose default is "Don't allow"; closing it, an error in it, no answer in 5 minutes, or closing the window means NO.

## 3. Files changed and architecture
Two layers so that nearly everything is testable without Qt:
- `src/jarvis/app/` (no GUI import): `lifecycle` (data-dir checks, config errors, one-window lock), `worker` (cancellable background tasks, honest shutdown), `transcript`, `status` (model/feature status), `diagnostics` (redacted), `controller` (all actions; calls the existing APIs).
- `src/jarvis/ui/`: `theme.py` (pure Python, contrast-verified), `window.py` (PySide6), `app.py` (start-up, `--selftest`).
- `src/jarvis/missions/service.py` shared by the CLI and the window; `memory/cli_memory.forget_all_profile` extracted. CLI behaviour unchanged (existing tests pass).
- `src/jarvis/version.py`: single version source (`0.6.0`, channel `preview`); `pyproject.toml` equality enforced by tests.
- `cli.py`: new `gui` and `diagnostics` subcommands. `jarvis gui` without PySide6 prints an explanation (exit 3).
- Bug found by the new tests and fixed: the assistant's database was opened on a worker thread and closed on the interface thread; `close()` swallowed sqlite's thread error and leaked the connection. Fix: `MemoryStore(check_same_thread)` / `PersonalMemory.open(any_thread=True)` used only by the window, the controller closes the assistant only when no worker is still running, and a failed model load closes the memory it opened.
- Existing tests: 3 assertions in `test_distribution.py`, 1 in `test_portability.py` and 1 in `test_desktop_windows_logic.py` searched `jarvis.spec` for literal names; the lists moved to `packaging/manifest.py`, so they now search spec + manifest. Intent unchanged. No other existing assertion changed.
- Packaging: `packaging/manifest.py` (single asset list), `jarvis.spec` (two executables via `MERGE`, mandatory-package and file checks that stop with "BUILD BLOCKED"), `entry_gui.py`, `installer.iss` (GUI shortcut, notice page, clearer uninstall text), `scripts/check_release_assets.py`, `release_tools.py build-info`, `build_windows.ps1` (installs `.[ui]`, gates, GUI self-test), workflow, `docs/INSTALLER_NOTICE.txt`.

## 4. Build and test commands
Tests (Linux or Windows, from the project root):
```
PYTHONPATH=src python -I -m unittest discover -s tests          (PowerShell: $env:PYTHONPATH="src"; python -I -m unittest discover -s tests)
set JARVIS_QT_TEST=1  &&  python -I -m unittest discover -s tests -p test_p10_ui_static.py     (needs PySide6; builds the real window offscreen)
python scripts/check_release_assets.py --source
```
Windows release build (not yet run by anyone): `.\scripts\build_windows.ps1 -Clean` (add `-WithVoice` for sounddevice/vosk, `-SkipTorch` for CI).

## 5. Test results
Full suite on the working tree (Linux, Python 3.13): **1230 tests, OK, 6 skipped** (baseline 1130 / 4 skipped; +100 new tests in `test_p10_app.py`, `test_p10_controller.py`, `test_p10_ui_static.py`, `test_p10_packaging.py`).
Skipped (honest list): (1) opt-in real-model tool calling (stage D), (2) opt-in real-model new tools (P8), (3) opt-in real Qwen planning (P9) - all need `JARVIS_REAL_MODEL_TEST=1` and weights; (4) PyTorch parameter formula - torch not installed; (5) `test_unwritable_dir_reported` - skipped because the sandbox runs as root, which can write anywhere; (6) `QtSmoke.test_offscreen_selftest` - needs PySide6 and `JARVIS_QT_TEST=1`.
What the new tests cover: data-dir/config errors, instance lock (live, stale, corrupt), worker cleanup, transcript kinds, model status for missing/corrupt/wrong-type/runtime-missing weights, no network/API-key code in the app layer, diagnostics redaction, controller chat/stop/confirmation fail-closed/permissions read-only/emergency stop/memory/missions/schedule-side planning, shutdown and repeated-launch data safety, theme contrast, syntax and undefined names of the Qt files, Python 3.11 f-string portability, version consistency, manifest and checker (including failing cases), installer static rules, build-script gate order, CLI entry points, upgrade data preservation, doc honesty. Mocks are labelled in each file header; FakeLlama is a scripted fake, not inference.

## 6. Packaging: included and excluded
Included (folder `dist\ProjectJarvis`, **one-folder build, not a single exe**): `jarvis.exe` (console), `jarvis-gui.exe` (window), Python runtime, PySide6 and only the Qt modules in use, `llama_cpp`/`sounddevice`/`vosk`/`comtypes` if installed at build time, `LICENSE`, `THIRD_PARTY_NOTICES.md`, `CHANGELOG.md`, `config.sample.toml`, `build_info.json`, `ocr_winrt.ps1`, empty `endpoint.txt`, nine user-facing docs (not the maintainer review notes or checklists).
Excluded and checked by `check_release_assets.py`: model weights (`*.gguf`, `*.pt`, `*.safetensors`), databases, keys/`.pem`, `.env`, `tests/`, `server/`, `scripts/`, `src/`, crash logs, the `STOP` file, audit logs, `requirements-lock.txt`; Qt WebEngine, QML/Quick, Multimedia, Charts, 3D, Sql, Bluetooth and others (`QT_EXCLUDES`).
User data lives in `%LOCALAPPDATA%\ProjectJarvis`, refused if it points inside the program folder. Upgrades replace only program files; the installer takes a memory backup first.
The build stops with "BUILD BLOCKED" when PySide6, numpy or cryptography are missing, when a manifest file or document is missing, when versions disagree, or when the built folder lacks a required file or contains a forbidden one.

## 7. Release verification matrix
| Area | Status | Evidence / what is missing |
|---|---|---|
| Source tests | **PASSED on Linux** | section 5; mocks labelled in each test file |
| Clean-extraction tests | **PASSED on Linux** | the full suite is run on a fresh extraction of this exact archive and the result is given in the delivery message (a result cannot be written inside the archive it describes) |
| Actual Windows build | **NOT DONE** | no Windows, PyInstaller or PySide6 here |
| Actual Windows launch | **NOT DONE** | `jarvis-gui.exe` never started anywhere |
| Qt window rendering | **NOT DONE** | never instantiated; static checks only |
| Model loading and local inference | **NOT DONE** | no Qwen weights or llama runtime; tested only with a scripted fake model |
| Voice and microphone | **NOT DONE** | CLI voice tested on Linux with fakes; no window controls |
| Desktop operations and permission enforcement | **Linux simulation only** | policy/confirmation tests pass; real Windows backends untested |
| Installer, upgrade, uninstall | **NOT DONE** | script reviewed statically; data-preservation logic simulated on Linux |
| Clean-machine / VM testing | **NOT DONE** | `clean_install_test.ps1` / `.wsb` exist, never run |
| Code signing | **NOT DONE** | unsigned; SmartScreen will warn |

## 8. Model distribution and licensing
See `docs/MODEL_DISTRIBUTION.md`. Interim model: Qwen3-1.7B Q4_K_M GGUF (Apache-2.0, Qwen team; GGUF by `ggml-org`), downloaded from the publisher after consent, never bundled, kept distinct from the from-scratch pipeline (no model from it exists). Missing, damaged or wrong-type weights give `no_model` / `model_invalid` / `not_a_chat_model` / `runtime_missing` with a next step, and **no external or paid fallback exists** (a test scans the app and UI code for network clients and API keys). The Apache-2.0 status was read from the publisher page earlier in the project and not re-checked here (no network). Offline inference is **unproven**.
Licences to review before any public release: PySide6/Qt is LGPL-3.0 (one-folder build keeps Qt DLLs replaceable; the notice must ship; get legal review), PyInstaller bootloader (GPL with exception), llama.cpp (MIT), Vosk models (each has its own licence).

## 9. Exact steps to produce and validate the Windows release
1. Windows 11 x64, Python 3.12 64-bit, Inno Setup 6, Git. `git clone` or extract the zip to an ASCII path such as `C:\dev\project-jarvis`.
2. `.\scripts\build_windows.ps1 -CheckOnly`, then `.\scripts\build_windows.ps1 -Clean` (optionally set `SOURCE_DATE_EPOCH` for comparable timestamps). Keep the full log.
3. Fix whatever "BUILD BLOCKED" names; the first real build will surface problems nobody has seen (Qt plugin paths, llama wheel availability, antivirus quarantine).
4. Work through `docs/WINDOWS_TEST_CHECKLIST.md` section 6k in order, then sections 6a-6j for voice, desktop control, vision, gaming and missions.
5. Install on a clean VM or Windows Sandbox; test upgrade and both uninstall answers.
6. Only after all rows of the matrix have real results, update the matrix and `docs/RELEASE_NOTES.md`. Until then the status stays "preview, unverified".

## 10. Known issues, security and dependencies
- Qt window never run; layout, focus order, high-DPI and threading hand-off (`_Bridge`) may fail on first launch.
- Permissions cannot be changed from the window by design; that is inconvenient but removes a way for the UI to bypass the policy.
- Schedules do not run unless a window or `schedule watch` is open. No background service, no startup entry.
- An uninterruptible tool step keeps running after Stop or window close; the window says so.
- The window's `QSettings` value (text size) remains in the registry after uninstall.
- `THIRD_PARTY_NOTICES.md` in this archive was generated here and lists PySide6, llama-cpp-python and others as "not installed"; the build regenerates it from the build environment.
- Installer unsigned; no auto-update installer (update check is separate and off by default).
- No security review or penetration test of any kind has been done.
- Prompt 11 has not been started.
