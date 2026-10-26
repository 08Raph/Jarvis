# Prompt 12 review: Windows readiness, real-world validation, blocker resolution

**Recommendation: BLOCKED pending environment (Windows 11 + the real model).** The source is in better shape to be tested on Windows than the Prompt 11
archive, but this session had no Windows, no PySide6, no PyInstaller, no Inno Setup, no PowerShell, no `llama-cpp-python`, no Qwen weights, no audio device and no PyPI access.
Therefore: **no Windows build, no GUI run, no real inference, no voice, no installer and no clean-machine install was performed or verified.** Nothing here is a release-readiness claim.

Qwen3-1.7B is a pretrained open-weights model used as the interim assistant engine. It was not trained from scratch by this project. The separate original-model research/training pipeline (`jarvis.model`, `jarvis.train`) is a different thing and does not answer chat requests.

## 1. Baseline
- Input: `project-jarvis-final-candidate.zip`, 752,126 bytes, SHA-256 `47ea501c21f8a126a02f1a8211cb075443ab3fd102b87398591989ac61730fea` (matched before extraction). The original archive and a full checkpoint copy were kept untouched.
- Baseline suite on a fresh extraction: `PYTHONPATH=src python -I -m unittest discover -s tests` -> 1250 tests, OK, 6 skipped (Prompt 11's claim reproduced).
- Read: `PROMPT11_FINAL_AUDIT.md`, `WINDOWS_TEST_CHECKLIST.md`, the changelog, `build_windows.ps1`, `jarvis.spec`, `manifest.py`, `installer.iss` (checked by `check_release_assets.py`), `release_tools.py`, `pyproject.toml`, the startup path (`launcher`, `ui/app`, `app/lifecycle`, `app/status`, `app/diagnostics`), the model loader and the permission/mission code paths named in section 6.

## 2. What was and was not available
| Needed for | Available here? |
|---|---|
| Windows 11 | No (Linux) |
| PySide6 / Qt | No |
| PyInstaller, Inno Setup, PowerShell | No (so the `.ps1` script has never been parsed or run by anything but a text check) |
| llama-cpp-python, a Qwen GGUF | No |
| PyPI (to check Windows wheel availability for Python 3.11-3.13) | No: DNS failed. Wheel availability is **unverified** |
| Audio device / PortAudio | No |

## 3. Windows blockers: confirmed defects vs. Windows-only risks
### 3a. Confirmed defects (reproduced or demonstrable from the code here) and fixes
| ID | Defect | Fix | Test (labelled in `tests/test_p12_windows_readiness.py`) |
|---|---|---|---|
| W-1 | Status said the model was **ready** whenever `llama_cpp` existed on disk. On Windows a package can be present and still fail to load (missing Visual C++ DLL, CPU without AVX2), so the window would say "ready" and fail on the first message. | `status.import_problem()` actually attempts the import once (cached) and reports `runtime_missing` with a Visual C++ hint; `runtime_checks()` exposes the measurement; diagnostics carry a legend separating "found on disk" from "loads". | `test_SIMULATED_present_but_unloadable_runtime_is_not_reported_ready` (the failing import is simulated) |
| W-2 | `jarvis-gui.exe` ignored `JARVIS_PROFILE`, while every console command honours it: the window and the console could open different memories. | `gui_main` applies the same rule; an invalid value is explained and nothing starts. | `test_REAL_window_uses_the_same_profile_rule_as_the_command_line`, `..._invalid_profile_...` |
| W-3 | If the crash log could not be written the message said "saved on this PC only: None". | Says the details could not be saved (both the console and the window path). | `test_REAL_crash_message_does_not_pretend_a_log_was_saved` |
| W-4 | If Qt was present but failed to load, the window path raised a raw `ImportError` caught only by the generic crash handler. | A plain message naming the Visual C++ runtime; return code 3, command-line features unaffected. | `test_SIMULATED_qt_failing_to_load_gives_a_plain_message` |
| W-5 | A damaged model `meta.json` raised a raw `JSONDecodeError` from the engine. | `RuntimeUnavailable` with a recovery hint; the file is not modified. | `test_REAL_damaged_meta_json_is_a_clear_runtime_error_and_file_is_kept` |
| W-6 | The build wrote `requirements-lock.txt` with `pip freeze`, which includes an editable line holding the build PC's folder, and pip refuses editable lines in constraints files, so the lock could neither be reused nor shared cleanly. | `pip freeze --exclude-editable`; new `-LockFile` option installs with `-c`. | static text test only (PowerShell not run) |
| W-7 | `build\` and `dist\` were emptied only with `-Clean`, so a file left from an older build could reach the installer and its `SHA256SUMS.txt`. | Always emptied; `-Clean` now means "also delete `.venv`". | static text test only |
| W-8 | Preflight covered Python and Inno Setup only; no disk, path, writability or module checks; no build manifest; `build_info.json` had no package versions; machine paths in shipped text files were not checked. | `scripts/check_build_env.py` (states: ok / warn / fail / not_run), `release_tools.py build-manifest`, `check_release_assets.py --manifest`, machine-path and model-weight scans, package versions in `build_info.json`. | the `BuildEnvironmentCheck` and `BuildManifest` test classes |
| W-9 | `config.sample.toml` ships in the installer but Jarvis 0.6.0 reads no config file (`load_config_file` has no production caller). A user editing a config would see no effect. | The file now says so and warns about backslashes in TOML paths. Not implemented: reading it would be a new feature. | `test_REAL_sample_config_no_longer_pretends_to_be_read` |
| W-10 | Checklist referred to an old archive name. | Updated; the checklist was reorganised (section 8). | n/a |
| W-11 | My own edit briefly broke a pre-existing static check (unbalanced parentheses in a PowerShell comment); the full suite caught it before packaging. | Comment reworded. | `test_distribution.py` (existing) |

One existing test was changed on purpose: `test_p10_app.test_runtime_missing_vs_ready` simulated a present runtime by patching `has_module`; with W-1 "present" is no longer enough,
so the test now also patches `import_problem` to "loads". The rule became stricter, not looser; the "absent -> runtime_missing" half is unchanged.

### 3b. Risks that only Windows can confirm (none of these was verified)
- Python 3.11-3.13 wheels for PySide6, numpy, cryptography, pyinstaller, comtypes, psutil, sounddevice, vosk and the CPU llama-cpp-python index (PyPI unreachable here). No upper bounds or pins are committed on purpose.
- PySide6 start-up, Qt plugin freezing, high-DPI, dark mode, focus rings (window code never run).
- Reparse points, junctions, OneDrive placeholders and ADS handling in `desktop/paths.py` (simulated with fake `lstat` results only).
- SQLite on Windows: file locking with antivirus/indexers, `os.replace` over a file another handle holds open (mission store, settings), WAL cleanup on exit.
- `OpenProcess` liveness check used by the instance lock and mission locks; stale-lock take-over.
- Worker threads/processes and application exit: the `_desktop-worker` subprocess, cancellation, no leftover `jarvis-gui.exe`.
- Subprocess creation (`CREATE_NO_WINDOW`), `powershell.exe` OCR script run with `-ExecutionPolicy Bypass` (Jarvis' own shipped script only; not an OS-protection bypass), `tesseract` discovery.
- `comtypes` UI Automation in a frozen app (generated type libraries), input injection, emergency stop.
- llama.cpp loading a model from a path with non-ASCII characters (user names), memory limits, AVX2 requirement.
- PyInstaller hidden imports and data collection; the installer, shortcuts, upgrade, uninstall; SmartScreen (unsigned); Visual C++ runtime on a clean PC.

## 4. Reproducible build
Changes (all unrun on Windows):
- `scripts/check_build_env.py`: Python 3.11-3.13 and 64-bit, project files, path length/characters/sync-folder names, free disk (fail < 3 GB, warn < 8 GB), writable project folder, `venv`/`ensurepip`, git, `SOURCE_DATE_EPOCH`; "internet for pip" is always `not_run` because it is not tested. Called by `build_windows.ps1` (also in `-CheckOnly`) before anything is created.
- `build_windows.ps1`: always-clean outputs, `-LockFile`, `pip freeze --exclude-editable`, torch note, build manifest + verification after the smoke tests, reminder that the build is not a clean-machine test.
- `release_tools.py build-manifest` -> `dist\build_manifest.json`: version, build time, commit, Python, build platform, key package versions, lock-file hash, `torch_bundled`, weight-sized files in the build, SHA-256 of both executables and **every file**, tree hash, installer SHA-256, text files holding the build machine's path. Exits non-zero (stopping the build) on weights or machine paths.
- `check_release_assets.py --dist ... --manifest ...` recomputes all hashes; a changed, missing, extra file or a hand-edited per-file entry (tree hash) is reported.
- Not done: exact dependency pins or a committed lock (nothing was verified on Windows); scanning binaries for build paths; signing; making PyInstaller output byte-reproducible. `docs/WINDOWS_BUILD_GUIDE.md` explains all of this and lists owner decisions (notably whether to bundle PyTorch: recommend `-SkipTorch` for a Qwen-only preview).
- No credentials: the build embeds none; the telemetry endpoint file is emptied again by the script (existing behaviour, re-read, not re-run).

## 5. Start-up and diagnostics
Failure -> what the user now sees (all from code paths exercised on Linux, simulated where Windows-specific):
| Failure | Result |
|---|---|
| PySide6 absent | message box/stderr "needs PySide6", exit 3 (existing) |
| PySide6 present but fails to load | **new** plain message about the Visual C++ runtime, exit 3 |
| Model absent / file invalid / not a chat model | `model_status` states with an action (existing) |
| llama-cpp absent | `runtime_missing` (existing) |
| llama-cpp present but cannot load | **new** `runtime_missing` with the import error text and Visual C++/AVX2 hint |
| Damaged `meta.json` | **new** clear message, file kept |
| Unwritable / invalid data folder | `StartupError` with a hint (existing, `lifecycle.check_data_dir`) |
| Damaged `memory.sqlite` | `MemoryDatabaseError` with `jarvis restore` hint (Prompt 11) |
| Invalid `JARVIS_PROFILE` | message in window and console |
| Crash log cannot be written | says so |
| Voice packages absent | status row `needs_setup` (existing); the window has no voice controls |

Diagnostics (`jarvis diagnostics`, Status page) already redact secrets, replace the home folder and omit conversation/memory/file text. Added: `runtime_checks` (measured import result, `null` = not installed so nothing measured) and a `legend` stating that `optional_dependencies` means "found on disk, not imported" and that model "ready" is a quick file check with no inference. Feature rows still carry their evidence labels (Linux simulation / fake model / never run).

## 6. Permissions and failure handling: what was checked
Read in the code and covered by existing tests unless marked new. No safeguard was weakened.
| Requirement | Finding |
|---|---|
| Safest defaults | Only the `system` category is on by default; monitoring and gaming need recorded consent; modes only hide tools (Prompt 11 review; existing policy tests) |
| Confirmation matches the executed action | The text shown and the arguments that run come from one bound object with a fingerprint including file state; a changed fingerprint cancels the run (existing tests) |
| Denial/cancel/timeout never approve | Only the exact boolean `True`, or an `Approval` for this fingerprint, approves; strings, numbers, dicts, `None`, other fingerprints do not; a failing callback is a refusal; no view/timeout/shutdown in the window = NO. **New:** `test_REAL_only_exact_true_or_matching_approval_counts` |
| Malformed model output | One strict `<tool_call>` parser; every call passes validate -> policy -> confirm; tool output returns wrapped as data (Prompt 11 probes; existing injection tests with a scripted model - real Qwen unmeasured) |
| Failed/timed-out not success | A timeout of a changing step ends as `unknown` and the mission pauses; `failed` stays failed; success needs tool evidence or an independent check |
| No silent retry of destructive actions | Declined, refused and timed-out steps are never retried (existing tests). **Residual:** a mission plan can carry `retries` + `retry_safe: true` on a changing tool and a tool-reported `failed` is then retried within that limit. This is disclosed in `mission review` and bound into the approved plan hash, and each attempt passes policy/confirmation again, so it is not silent, but a reviewer could miss it. I did not change it: existing tests deliberately use it for idempotent writes. Recommendation: refuse `retry_safe` for destructive/input/install/execute/network tools at plan validation |
| Emergency stop and workers | The `STOP` file is checked before every step; a stopped step that had started is `unknown`; shutdown answers pending confirmations NO and closes the assistant only when no worker is running (Prompt 10). Windows behaviour unverified |
| GUI visibility vs. permission | Permissions are stored in the profile and enforced by the executor; the window has no switch that grants anything (Prompt 11 review) |

## 7. Tests
- New: `tests/test_p12_windows_readiness.py`, 27 tests. Labels in the names: `REAL` = real files/code on Linux, `SIMULATED` = another machine or Windows behaviour faked, `STATIC` = text check of a file that could not be run (the `.ps1`). Changed: one assertion in `test_p10_app.py` (section 3a). Changelog and docs updated.
- Negative controls (disposable copies; the fix reverted or the safeguard disabled; result = tests that failed): runtime measurement reverted: 4; GUI startup reverted: 3; launcher reverted: 1; `meta.json` handling reverted: 1; sample config reverted: 1; build script reverted: 1; manifest verification disabled: 2; machine-path scan disabled: 1; disk check forced ok: 1; Python range unchecked: 1; "internet" forced to ok: 1; unmodified control: 0 failures.
- Not given negative controls: the PowerShell logic itself (cannot be run), diagnostics redaction (existing tests).
- Commands: `cd project-jarvis && PYTHONPATH=src python -I -m unittest discover -s tests`; targeted: `... -p test_p12_windows_readiness.py`. Totals: see section 10.

## 8. Windows checklist
`docs/WINDOWS_TEST_CHECKLIST.md` now begins with **section A, an ordered 12-part procedure** (environment, dependencies, GUI, model and real inference, memory, permissions, missions/schedules, voice, diagnostics/errors, packaging, install/upgrade/uninstall/clean machine, security/privacy), each with prerequisites, exact commands, expected results and what to record. The older per-prompt lists are kept below it as reference. No box is ticked.

## 9. Model validation status
**BLOCKED.** No weights, no runtime. I did not download from any source, create fake weights, use a cloud model, or count scripted-model tests as inference. Setup is in checklist part 4 and `docs/LOCAL_MODEL.md`; the opt-in real-model tests (`JARVIS_REAL_MODEL_TEST=1`, `JARVIS_REAL_MODEL_PATH`) are the first real evidence to collect. Scripted tests (`FakeLlama`, `FAKE_MODEL` in names) prove only the surrounding code.

## 10. Results and decision
Full suite from the working tree: `PYTHONPATH=src python -I -m unittest discover -s tests` -> 1277 tests (1250 + 27 new), OK, 6 skipped (the opt-in real-model / hardware tests). The fresh-extraction run of the final archive is recorded in section 12.

| Area | Status |
|---|---|
| Source and automated-test quality | PARTIAL (Linux only, scripted model) |
| Windows build script and installer | NOT TESTED (never run; improved and statically checked only) |
| GUI | NOT TESTED |
| Real model | BLOCKED |
| Voice | BLOCKED |
| Windows desktop operation | NOT TESTED |
| Clean-machine install | NOT TESTED |
| Overall | **Blocked pending environment or dependencies** |

Why not "Ready for Windows testing": the build script has never been parsed by PowerShell and its dependency set has never been resolved on Windows, so a first run may fail before producing anything.
It is still worth running: the failure will be at a named step with a clear message (preflight, pip, tests, PyInstaller, smoke tests). Start with checklist section A, parts 1-3, using the source tree (`pip install -e`), which does not need the build script at all.

## 11. Unresolved
1. Everything in section 3b. 2. Real Qwen behaviour (tool calls, injection, planning). 3. `retry_safe` on changing tools (section 6). 4. PyTorch bundling decision. 5. No upper-bounded or locked dependency set. 6. `config.sample.toml` unread. 7. Legacy `tools/registry.py` still unused. 8. Binary files not scanned for build paths. 9. The unsigned installer will trigger SmartScreen.

## 12. Final archive
`project-jarvis-prompt12.zip`. Its size and SHA-256 cannot be written inside itself; they are in the delivery message. Earlier archives (`project-jarvis-p15-prompt10.zip`, `project-jarvis-final-candidate.zip`) were not modified.
