# Prompt 8: vision, Gaming mode, system monitoring and operating modes

**Not run on Windows 11. Not run with the real Qwen model. No GPU, no real game, no real PresentMon/CapFrameX log and no real vision model were used.**
This is not a security approval, not a production-readiness statement and not a Windows, real-model or hardware verification.
**Implementation complete** (the code exists and its automated tests pass on this Linux machine); **real Windows 11, real-model and hardware verification: pending**
(see section 8 and `docs/WINDOWS_TEST_CHECKLIST.md` section 6i). Prompt 6 desktop control and Prompt 7 tools are still not Windows-verified either.
Everything here sits on top of the interim pretrained Qwen model and supporting tools (OCR, accessibility, operating-system counters). None of it is the original
from-scratch model, which does not exist yet; nothing here retrains any weights, and OCR or scripted logic is **not** a trained vision model.

## 1. What was implemented
| Area | What | Files |
|---|---|---|
| Monitoring (read-only) | CPU, memory, disk, GPU (NVIDIA `nvidia-smi` only), temperature (psutil sensors only), busiest processes (names, pid, CPU %, memory), Jarvis' own usage. Every reading has a value, unit, **kind** (measured / derived / imported / estimated / unavailable), source and UTC time. Invalid values (NaN, out of range, wrong type) become *unavailable*, never clamped. No background polling; fresh readings are reused for a configurable 1-300 s; `monitor_watch` is bounded to 60 samples / 300 s and can be stopped. Never ends a process, never changes a setting; no command lines or paths are read. | `monitor/readings.py`, `monitor/system.py`, `monitor/tools.py` |
| Vision | `observe_screen`, `find_on_screen`, `recheck_screen`, `vision_status`. One capture per approved request (asked **every** time), analysed with OCR (Windows OCR / Tesseract) and UI Automation elements; picture held only in a private temp folder while OCR runs, then deleted (never in the screenshots folder); results carry source, capture time, window, OCR confidence and box where the engine gives them; labelled **inferences** are kept apart from observations; `find` answers FOUND / PROBABLE / NOT SEEN (never "absent"); `recheck` compares a second capture with an earlier one. | `desktop/vision.py` |
| Capture guard | Refuses sign-in / password / verification-code / banking windows (title), password managers, UAC and security programs, anti-cheat software and anti-cheat games, windows with a visible password box, and a whole-screen capture while any such window is open. Secret-looking lines (keys, tokens, card numbers, `password: ...`, one-time codes) are withheld. Also applied to the older `screenshot` and `read_screen_text` tools. | `desktop/capture_guard.py` |
| Gaming mode | `game_status`, `game_metrics`, `game_fps_import`, `game_compare`, `game_profile_list/get/save`. Known-game detection by program name (fixed list, anti-cheat flag); whole-PC readings; **FPS and frame times only from logs you recorded with another tool** (PresentMon / CapFrameX CSV, parsed with limits and a hash); comparison that reports a difference only when >= 3 separate runs per setting differ by more than twice their own spread; per-game profiles (notes + manual suggestions, never applied). Cheat / aimbot / injection / anti-cheat-bypass wording is refused. | `monitor/gaming.py` |
| Modes | General, Coding, Research, Gaming, Productivity: a fixed table (tools, restrictions, behaviour) enforced by the executor (denied before validation), hidden from the model's tool list, announced in its system prompt, inspectable with `jarvis mode show` or the read-only `mode_info` tool. | `modes.py`, `executor.py`, `agent.py` |
| Consent | New permission categories `monitoring` and `gaming` (off by default) need a **recorded consent** as well as the switch; `jarvis desktop enable screen|monitoring|gaming` shows a notice and needs a typed `yes` (or `--yes`, still printing the notice); disabling withdraws consent. | `consent.py`, `policy.py`, `cli_desktop.py` |
| CLI | `jarvis mode ...`, `jarvis monitor status|config`, `jarvis gaming status|config`. Settings the model cannot reach. | `cli_p8.py`, `cli.py` |
| Wiring | One place registers all extension tools. | `extensions.py` |

## 2. Files changed
New: `src/jarvis/{extensions,modes,consent,cli_p8}.py`, `src/jarvis/monitor/{__init__,readings,system,tools,gaming}.py`, `src/jarvis/desktop/{vision,capture_guard}.py`,
`tests/{p8_common,test_p8_monitoring,test_p8_vision,test_p8_gaming,test_p8_modes,test_p8_integration}.py`, this file.
Edited: `desktop/policy.py` (categories, consent check, always-ask screen tools, gaming changes always asked), `desktop/tools.py` (`Env.ext`, capture guard in `screenshot`/`read_screen_text`, secret withholding), `desktop/executor.py` (mode filter, spec view), `desktop/agent.py` (mode-filtered tool list, mode in the system prompt), `desktop/cli_desktop.py` (consent prompts, status shows consents and mode), `dev/research.py` (text read from the screen counts as private for the outgoing-request guard), `cli.py`, `pyproject.toml` (optional `monitor = psutil`), `docs/WINDOWS_TEST_CHECKLIST.md` (6i), `docs/PRIVACY_AND_NETWORK.md`, `CHANGELOG.md`.
No existing test assertion was changed. Built-in tool list is unchanged (a test freezes it).

## 3. Tests: exact command and results
Command (from the project root, also from a clean extraction of the final archive):
`PYTHONPATH=src python -I -m unittest discover -s tests`
Baseline before Prompt 8 (Prompt 7 archive): 811 tests OK, 2 skipped.
Final, from a clean extraction of the final archive: **Ran 961 tests, OK, skipped=3** (about 96 s)
Skipped (3): the Prompt 6/7 opt-in real-model test (Stage D), the PyTorch formula test, and the new opt-in real-model test for the Prompt 8 tools (`JARVIS_REAL_MODEL_TEST=1`, `JARVIS_REAL_MODEL_PATH=...`). Nothing was reported as passed for them.
New tests: monitoring 34, vision 33, gaming 33, modes 20, integration/CLI/privacy/regression/docs 30 (one skipped).
Negative controls (each security rule removed on a scratch copy, tests must then fail): consent check, capture guard, mode filter, cheat-wording filter, reproducibility rule, always-ask for capture, secret withholding, reading validation: **all 8 made tests fail**, and the clean copy passes. The scratch copy was discarded.

## 4. What works in this environment (Linux, no Windows, no GPU)
- REAL: CPU, memory, disk, process and own-usage readings from the OS and psutil; Tesseract OCR on pictures the tests draw; frame-time maths on CSV files the tests write; the real executor, policy, consent, confirmation, audit, emergency stop, CLI.
- Honestly unavailable here: GPU (no `nvidia-smi`), temperatures (no sensors), Windows OCR, UI Automation, FPS (no log).

## 5. What was mocked or simulated (labelled in each test file)
- The desktop (windows, accessibility elements, a "game is running") is the in-memory simulator; "screen" pictures are drawn by the tests.
- GPU values in a few tests come from a fake source and are labelled so; they prove validation, not any GPU.
- The language model is a scripted list of replies. Those tests prove the tools can be called through the agent loop and that Jarvis refuses what it must; they say nothing about Qwen.
- Tesseract absence/crash, a backend without screenshots or accessibility are simulated with small test classes.

## 6. Questions you asked
- **Is a real vision model available?** **No.** There is no image-understanding model. Jarvis reads text with OCR and controls with Windows accessibility. It cannot describe pictures, icons, colours, charts or layout. `vision_status`, every observation and the consent notice say so.
- **Were real gaming metrics obtained?** Whole-PC CPU/memory only, on this Linux machine. **No real FPS or frame-time data from a real game** (no PresentMon / CapFrameX log); FPS numbers in tests come from CSV files the tests wrote. GPU was unavailable.
- **Did the real Qwen model use the new tools?** **No / not tested.** The weights and runtime are not in this environment. The opt-in test `RealModelNewToolsTests` exists and was skipped. Real-model validation is pending.
- **Windows 11:** nothing here was run on Windows.

## 7. Behaviour you should know about
- Screen capture is asked on every request, even though it only observes. There is no continuous capture. The old `screenshot` tool still saves a PNG in the screenshots folder when approved (its Prompt 6 behaviour); the new tools never save the picture.
- "No mode selected" (the default) behaves exactly like earlier versions. A mode only hides tools; permissions, consents and confirmations are untouched. Switching a mode, monitor setting or gaming setting never enables anything (tested).
- Gaming-mode tools cannot send input or touch other processes (static test). `game_profile_save` is always asked and changes no system or game setting.
- `FPS improved` is reported only for >= 3 runs per setting with a difference above twice the runs' own spread, and the text says this does not prove the setting caused it.

## 8. Not yet verified (pending)
Windows 11: Windows OCR via PowerShell, UI Automation elements, window enumeration, process names for game detection, capture of a real window, `nvidia-smi` on an NVIDIA machine, a real PresentMon/CapFrameX CSV (column names differ between versions), the console prompts. Real model: whether Qwen3-1.7B chooses these tools sensibly with a longer tool list. Hardware: GPU readings. Checklist: `docs/WINDOWS_TEST_CHECKLIST.md` section 6i.

## 9. Known limitations, bugs and security concerns
- OCR is approximate: low-contrast, small, stylised or non-Latin text is missed or misread; `NOT SEEN` is never proof of absence. Tesseract here gives line confidence; Windows OCR gives none, and the result says so.
- The secret detector and sensitive-title list are heuristics. A secret shown in an unrecognised window can still be read by OCR once you approve the capture; you approve each capture and see the target window first.
- Game detection uses a short fixed list of program names (a few are weak matches such as `javaw.exe`); unknown games are not recognised, and a game with a non-standard name will not be protected by the capture guard unless anti-cheat software is also running.
- Only NVIDIA GPUs (via `nvidia-smi`) can be read; temperatures need psutil sensors (often absent on Windows). AMD / Intel GPU metrics are not supported.
- Frame-time columns are guessed from names (`FrameTime`, `MsBetweenPresents`, `MsBetweenDisplayChange`); a log in another format is refused, not guessed.
- Monitoring readings of process names are data from outside; they are returned to the model as untrusted data.
- The capture cache lives in memory (5 captures, 10 minutes); a crash loses it, nothing is written to disk.
- The consent for `screen` is a typed confirmation at `enable` time and a per-capture question; it is not stored in the permission file (only `monitoring` and `gaming` consents are).
- No sandboxing was added for these tools; they only read. No dependency is required; `psutil` (optional extra `monitor`), Tesseract and Windows PowerShell OCR are used if present.
- Not a security approval: no external review, no fuzzing of the OCR / CSV parsers beyond the bad-input tests.

## 10. Next recommended milestone
Run section 6i of the Windows checklist on a real Windows 11 PC (with one real PresentMon recording and, if available, an NVIDIA GPU), fix what it finds, then run the opt-in real-model test and record how often Qwen chooses valid calls with the longer tool list. Prompt 9 has not been started.

## 11. Package
`/home/claude/project-jarvis-p15-prompt8.zip` (earlier checkpoints, including `project-jarvis-p15-prompt7.zip`, were not touched). An archive cannot contain its own checksum, so its exact size and full SHA-256 are given in the delivery message and can be recomputed with `sha256sum`.
