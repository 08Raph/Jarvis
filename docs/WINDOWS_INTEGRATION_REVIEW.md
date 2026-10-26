# Windows integration review (Prompt 6, Stage C)

**Nothing in this review was run on Windows.** The project has not been tested on real Windows 11. This document is not a
security approval, not a production-readiness statement and not a Windows verification. Passing tests here show that our
decision logic behaves as written when the operating system is replaced by fakes; they do not show that Windows behaves the way
the fakes do.

Scope: the Windows backend (`win32.py`), application discovery (`apps.py`), system metrics (`sysinfo.py`), UI Automation
(`uia.py`), OCR (`ocr.py`, `ocr_winrt.ps1`), the emergency stop (`stop.py`), the worker-process integration (`executor.py`) and the
packaging that carries them (`packaging/jarvis.spec`). No real keyboard/mouse/UI action, destructive or elevated operation was
executed, and none was enabled.

## 1. Findings that were fixed in code (all unverified on Windows)

| # | Finding | Risk | Change | How it was checked here |
|---|---|---|---|---|
| F1 | `focus()` fell back to a synthetic Alt key press to win the foreground lock. `focus_window` is not an input tool, so it could send input without the input permission. | Input without the input permission | Fallback removed. `focus()` returns `False` and the tool reports that Windows refused. | MOCKED: `SendInput`/`keybd_event`/`mouse_event` are never called; STATIC: `focus` references no input primitive; negative control (re-adding a send) fails 3 tests |
| F2 | `capture()` called `GetDIBits` while the bitmap was still selected into the device context (undefined by the Win32 documentation), did not check `CreateCompatibleDC`/`CreateCompatibleBitmap`, and leaked handles on failure. | Wrong/black screenshots, handle leaks | Bitmap is selected out before `GetDIBits`; each handle is checked; everything is released on every path. | MOCKED call order and cleanup. Cannot prove the real GDI result |
| F3 | Child processes (`Get-StartApps`, OCR, `nvidia-smi`, CIM query) were decoded with `text=True` only: on a legacy code page a non-ASCII app name raised `UnicodeDecodeError` or was garbled. | Crash or wrong names | PowerShell output is ASCII-escaped (`\uXXXX`) and every text-mode call passes `encoding="utf-8", errors="replace"`. | MOCKED parsing of escaped JSON incl. surrogate pairs; STATIC scan for `text=True` without `errors=` |
| F4 | One UI Automation COM client was shared by every thread. COM objects belong to the apartment of the thread that made them, and the inline executor runs each tool on a fresh thread. | Intermittent COM errors | Per-thread client with `CoInitialize` in each thread. | MOCKED: two threads get two clients. Cannot prove the real COM behaviour |
| F5 | The launch confirmation showed the name the model typed, not the program that would start. A shortcut could point anywhere. | User approves something other than what runs | `launch_app` is pinned at confirmation time: the question shows the app id, kind, how it was found and the real target path; any change before running forces a new question. Unknown/ambiguous apps fail before any question. | REAL executor with the simulated backend |
| F6 | When Jarvis could not list running programs or could not tell which program owns the target window, the anti-cheat/protected-program checks silently saw an empty set and allowed input. | A safety check passing because information is missing | Input/UI actions are refused when either is unknown. | REAL policy and executor; negative control fails 2 tests |
| F7 | `is_elevated()` and the executor's elevation/process-list calls could raise, which either crashed the step or looked like "not elevated". | Fail open | Unknown elevation counts as elevated (observe only); an exception in `run()` becomes a `failed` result with "nothing was done"; an audit-log write failure before the start stops the action, after the end it is stated in the message. | REAL executor |

## 2. Findings that were NOT fixed (open risks)

| # | Finding | Why it was left | Where to look on Windows |
|---|---|---|---|
| O1 | `load_config` is never called, so a `config.toml` `[apps]` allow-list is not applied. | Behaviour change outside this stage | Checklist 6f.9 |
| O2 | `launch()` verifies success by loose title/process matching; a different window with a similar name can satisfy it. | Needs real-app testing to tune | 6f.6 |
| O3 | UI Automation `find` walks up to 5000 elements; on big windows (browsers, IDEs) this may exceed the tool timeout. | Needs real measurements | 6f.3 |
| O4 | Killing the worker leaves grandchildren if the worker started any. Launched applications are deliberately not killed. | By design for apps; worker has none today | 6f.8 |
| O5 | The `BitBlt` fallback captures whatever is on top, including overlapping windows. | Inherent to the fallback | 6f.4 |
| O6 | The stop hotkey is Ctrl+Alt+Pause. Keyboards without a Pause key cannot use it. The STOP file and `jarvis desktop stop` still work. | Needs a design decision | 6f.7 |
| O7 | `ctypes` calls do not declare `argtypes`/`restype`. On 64-bit Windows a handle returned as a C `int` can be truncated. | Real-hardware testing needed before a wide change | 6f.1, 6f.4 |
| O8 | The OCR bitmap format is not forced to `Bgra8`; some decoders may produce another format. | Cannot test the WinRT path here | 6f.5 |
| O9 | The frozen build's comtypes wrapper generation (`GetModule("UIAutomationCore.dll")`) has never run. | Needs an installed build | 6f.3 |
| O10 | `powershell.exe -ExecutionPolicy Bypass` for the OCR script may interact badly with Constrained Language Mode / AppLocker. | Environment dependent | 6f.5 |
| O11 | `paths.py` accepts OneDrive/cloud reparse tags by a list that was written from documentation, not observed. | Needs a real OneDrive | 6f.10 |
| O12 | Window/UI targets for input were not bound the way file actions are (hardening pass). | Window pinned in Stage D; UI elements inside the window are still chosen at run time | see `AGENT_INTEGRATION_REVIEW.md` |
| O13 | Real Qwen tool calling through the strict parser is untested. | Needs the model | n/a |

## 3. What the new tests are, and are not

`tests/test_desktop_windows_logic.py` has 66 tests. Each class says in its docstring which of these it is:

- **MOCKED**: `jarvis.desktop.win32`/`uia` are imported on Linux with `sys.platform`, `ctypes.WinDLL`, `ctypes.WINFUNCTYPE`
  replaced; `sysinfo`, `apps`, `ocr` get fake Windows API answers or fake PowerShell output. They prove call order, arithmetic,
  filtering, parsing and error handling. They **cannot** prove that the real Win32, UI Automation, WinRT or PowerShell behave as
  the fake does, that structure layouts are right (`wintypes.DWORD`/`LONG` are 8 bytes on this Linux, 4 on Windows, so struct tests
  are invalid here), or anything about timing.
- **REAL**: the real executor, policy, parser and real child processes standing in for the worker, on Linux. They prove the
  parent survives garbage, silence, crashes, hangs and the emergency stop. They do not cover process creation flags, the frozen
  `jarvis.exe`, antivirus or Windows start-up time.
- **STATIC**: source inspection (no input primitive reachable from `focus`; input primitives called only from the known backend
  and tool-handler functions; no `shell=True`; packaging includes the OCR script and comtypes). They prove the text of the code only.
- The `.lnk` files are **synthetic**, built by the test; real Windows shortcuts (advertised, MSI, Store, network) are not covered.

Negative controls were run in a scratch copy: removing the F6 policy check fails 2 tests; re-adding an input send to `focus` fails 3.

## 4. Review of the documented Windows checklist items

| Item | State |
|---|---|
| Junctions, symlinks | Logic tested with REAL Linux symlinks and a MOCKED reparse-point `lstat` (hardening pass). Real junctions: untested (6f.10) |
| Hard links | REAL on Linux (link count). NTFS behaviour untested |
| OneDrive placeholders | Accepted by a guessed reparse-tag list (O11). Untested |
| Path normalisation (8.3, case, `:` streams, reserved names, trailing dot) | Logic present; never run on NTFS (6f.10) |
| Process isolation | REAL subprocess tests on Linux; Windows untested |
| Emergency stop | Event/STOP-file logic REAL; hotkey loop MOCKED; real key delivery untested (6f.7) |
| Elevated run | Fail-closed logic tested; real UAC token untested (6f.11) |

## 5. Verification record

Baseline before Stage C: 518 tests, OK, 1 skipped. After Stage C: 584 tests (518 + 66 new), OK, 1 skipped. No existing assertion was
changed in Stage C. See `CHANGELOG.md` for commands.
