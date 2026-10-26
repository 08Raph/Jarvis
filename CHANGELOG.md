# Changelog

Earlier (p1-p14): untracked.

## p15 Prompts 13-20: retries, styles, models
`docs/PROMPT13_REVIEW.md` .. `PROMPT20_REVIEW.md`; setup wizard.

## p15 Prompt 12: Windows readiness (not run on Windows)
Build preflight and manifest, honest runtime status, startup fixes. `docs/PROMPT12_REVIEW.md`. Tests: 1250 -> 1277.

## p15 Prompt 11: audit
Not a release candidate: `docs/PROMPT11_FINAL_AUDIT.md`. 4 findings fixed.

## p15 Prompt 10: window, Windows packaging (prepared)
Source only; no Windows build or Qt run. `docs/PROMPT10_REVIEW.md`.

## p15 Prompt 9: missions, planning and scheduling
Not run on Windows 11 or the real Qwen model. Not security-approved. `docs/PROMPT9_REVIEW.md`.
- Added: `jarvis/missions/`, `jarvis mission|schedule`. Tests: 961 -> 1130.

## p15 Prompt 8: vision, Gaming mode, system monitoring and operating modes
Not run on Windows 11 or with the real Qwen model; no GPU, no real game, no real PresentMon log; no real vision model exists (OCR + accessibility only). Not security-approved, not production-ready. Details: `docs/PROMPT8_REVIEW.md`.
- Added: `jarvis/monitor/` (readings, read-only system monitoring, Gaming mode), `desktop/vision.py` (`observe_screen`, `find_on_screen`, `recheck_screen`, `vision_status`), `desktop/capture_guard.py`, `modes.py` (General/Coding/Research/Gaming/Productivity + `mode_info`), `consent.py`, `cli_p8.py` (`jarvis mode|monitor|gaming`), `extensions.py`; permission categories `monitoring` and `gaming` (off, need recorded consent); optional extra `monitor` (psutil).
- Changed: `policy.py`, `tools.py` (capture guard and secret withholding now also apply to `screenshot` / `read_screen_text`), `executor.py` (mode filter), `agent.py`, `cli_desktop.py` (consent notices), `dev/research.py` (screen text is private), `cli.py`.
- Tests: Prompt 7 baseline 811 OK (2 skipped) -> 961 OK (3 skipped). No existing assertion changed.
- Command: `cd project-jarvis && PYTHONPATH=src python -I -m unittest discover -s tests`

## p15 Prompt 7: coding, research, documents, plugins and skills
Not run on Windows or with the real Qwen model; no real internet request. Not security-approved, not production-ready. Details: `docs/PROMPT7_REVIEW.md`.
- Added: `jarvis/dev/` (sandbox, error analysis, coding tools, document/research tools, plugins, skills, CLI `jarvis plugin|skill|research`), permission categories `code_read/code_edit/code_run/research_online/plugins` (off by default), risks EXECUTE and NETWORK (always confirmed), `desktop allow-project`, network feature `web_research`, `assert_public_host`.
- Changed: `schema.py`, `policy.py`, `paths.py`, `tools.py`, `executor.py` (extension hooks only; built-in tools cannot be replaced), `cli_desktop.py`, `cli.py`, `net/policy.py`.
- Tests: Stage D baseline 621 OK (2 skipped) -> 811 OK (2 skipped). No existing assertion changed.
- Command: `cd project-jarvis && PYTHONPATH=src python -I -m unittest discover -s tests`

## p15 Stage D: agent integration review
Not run on Windows or with the real Qwen model. Not security-approved, not production-ready. Details: `docs/AGENT_INTEGRATION_REVIEW.md`.
- Changed: `agent.py` (model crash, empty/non-text replies, refusal streak, no re-asking of a declined action, report shows the stop reason), `tools.py` + `executor.py` (the target window of input/UI/focus actions is pinned and re-judged by the policy), `task.py` (inconclusive checks and unwritable history no longer lose the report).
- Added: `tests/test_desktop_agent_integration.py` (37 tests; one opt-in real-model test that is skipped here), `docs/AGENT_INTEGRATION_REVIEW.md`, checklist section 6g.
- Tests: Stage C baseline 584 OK (1 skipped) -> 621 OK (2 skipped; the extra skip is the opt-in real-model test). No existing assertion changed.
- Command: `cd project-jarvis && PYTHONPATH=src python -I -m unittest discover -s tests`

## p15 Stage C: Windows integration review
Not run on Windows. Not security-approved, not production-ready, not Windows-verified. Details: `docs/WINDOWS_INTEGRATION_REVIEW.md`.
- Changed: `win32.py` (focus no longer injects input; capture GDI order/handle checks; fail-closed `is_elevated`), `uia.py` (per-thread COM client), `apps.py`, `ocr.py`, `ocr_winrt.ps1`, `sysinfo.py` (encoding safety), `tools.py` (launch confirmation pins the real target), `policy.py` (input refused when the process list or target owner is unknown), `executor.py` (no crash on internal errors, audit-write handling, fail-closed elevation/process checks).
- Added: `tests/test_desktop_windows_logic.py` (66 tests, labelled MOCKED/REAL/STATIC), `docs/WINDOWS_INTEGRATION_REVIEW.md`, checklist section 6f.
- Tests: baseline 518 OK (1 skipped) -> 584 OK (1 skipped). No existing assertion changed.
- Command: `cd project-jarvis && PYTHONPATH=src python -I -m unittest discover -s tests`

## p15 security hardening (limited pass)
Scope: file tools and allowed-folder enforcement, confirmation binding, tool-call parsing, execution reporting.
Not touched: keyboard/mouse input, UI automation, game interaction, anti-cheat detection, screenshots/OCR, Windows backend modules.
Not run on Windows. Not security-approved, not production-ready. Details and limits: `docs/DESKTOP_HARDENING.md`.

### Security
- Jarvis's own data (profile/data/model folders: permissions, audit log, checkpoints, memory) is unreachable by file tools and cannot be allowed as a work folder.
- Links, junctions, mount points, unknown reparse points and multi-hard-link files are refused; uncertain checks fail closed.
- Atomic writes use unique exclusive temp files and re-validate the target before replacing it; `undo` validates every path it touches.
- Confirmations show, and execute, one pinned action (real resolved paths and destination, true risk) with a fingerprint; any change before running forces a fresh approval. Only an exact `True` or a matching `Approval` approves.
- The tool-call parser accepts exactly one complete, schema-valid call and rejects unfinished reasoning, truncation, multiple/quoted/odd calls, NaN/Infinity, duplicate keys and unknown tools/arguments.
- Float arguments reject NaN/Infinity.

### Reporting
- `verified` needs recorded evidence; timeouts, cancellations (before/during), denials, declines, failures and unknown results are reported distinctly; "file not found" is FAILED, not DENIED.
- The executor's report comes first; the model's summary is quoted, labelled unverified and cannot imitate the report.
- Task runner: timeout -> task status `unknown`; checks that could not run leave a step UNVERIFIED.

### Behaviour changes
- `preview` reports bad paths up front; two tool calls in one reply are rejected; symlinks inside allowed folders are refused.
- Two assertions in `tests/test_desktop_control.py` were updated for the first two changes.

### Tests
- New: `tests/test_desktop_hardening.py`.
