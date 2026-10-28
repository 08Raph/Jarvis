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

## Stage 5 final download-safety follow-up (not committed)
`add-gguf --expected-size-bytes`, safer replacement/rollback (older rollback copy kept until the new model is in place, restore on failure), chunked copy with free-space rechecks, `models list` shows rollback copies and crash leftovers. Linux-tested only. `docs/STAGE5_MODEL_SOURCE_VERIFICATION.md` section 16.

## Stage 5 Windows verification and benchmark readiness (not committed)
New scripts `verify_model_metadata.py`, `verify_downloaded_model.py`, `verify_wheel.py`, `resource_guard.py`, `summarize_stage5_results.py`; new `docs/STAGE5_WINDOWS_PLAYBOOK.md`; limits wording reconciled in the runbook, `LOCAL_MODEL.md` and the verification report (section 19). No runtime, catalog, default-model or pin change; nothing downloaded; nothing validated on Windows.
### Tests
- New: `tests/test_p33_windows_tools.py` (57 tests).

## Stage 5 pre-Windows audit (not committed)
Fixes in `scripts/verify_model_metadata.py`, `verify_downloaded_model.py`, `verify_wheel.py`, `resource_guard.py`, `summarize_stage5_results.py` and `docs/STAGE5_WINDOWS_PLAYBOOK.md` (details: verification report section 20). No runtime, catalog, default-model or pin change; nothing downloaded or installed; nothing validated on Windows.
### Tests
- `tests/test_p33_windows_tools.py` now has 79 tests (audit regressions, playbook-versus-CLI option check).

## Stage 5 in-app preflight: System & Model Readiness page (not committed)
New `src/jarvis/preflight/` (runner, shared model-metadata check, policy-gated fetcher, result history), controller methods `preflight_start/cancel/history/folder`, a new window page (`Ctrl+9`), `docs/IN_APP_PREFLIGHT.md` (shipped in the app), packaging manifest entries. `scripts/verify_model_metadata.py` now wraps the shared module. Nothing is downloaded or installed; catalog, default model and runtime pin unchanged. Untested in Qt, on Windows and in the EXE.
### Tests
- New: `tests/test_p34_readiness.py`.


## Stage 5 Windows EXE validation and preflight hardening (not committed)
`--selftest` rewritten (per-check report, genuine/simulated/not_run labels, exit codes 0/1/3, `RUNNING` marker, direct page refresh, offline preflight on the worker); shared packaged-files/modules check in `app/diagnostics.py` feeding the diagnostics report, a tenth preflight check ("App files and modules") and the self-test; "Show diagnostics" button; Bearer-token scrubbing fix; build script prints the self-test report on failure. No EXE was built or run; nothing downloaded or installed; catalog, default model and pin unchanged. Linux-tested only.
### Tests
- New: `tests/test_p35_validation.py` (51 tests).

## Stage 5 continuation without Windows (not committed)
`catalog.resolve_file` now refuses malformed host answers (wrong shapes, negative/boolean/non-numeric sizes) with one clean `LookupError`; `SetupEngine.download` no longer retries or blames the connection for a changed repository (`host_changed`); the desktop audit log digests the value set by `ui_act`; the `bench-intel` summary states that the pooled rate is not an intelligence rating and what it does not measure; verification report section 23 (Qwen3-8B page evidence, runtime blockers, storage estimate, reviews). Catalog, default model and runtime pin unchanged; nothing downloaded or installed. Linux-tested only.
### Tests
- New: `tests/test_p36_stage5_review.py` (20 tests).

## Qwen3-8B catalog correction (not committed)
Entry `qwen3-8b-q4_k_m` now points to `Qwen/Qwen3-8B-GGUF` / `Qwen3-8B-Q4_K_M.gguf` (exact-name pattern), 5.03 GB estimate, install on request only (`auto_recommend=False`, now enforced by the selector). Default/recommended model unchanged. Nothing downloaded or installed (host and PyPI unreachable here); runtime pin unchanged; Linux-tested only. Tests: `test_p36_stage5_review.py` (Qwen3_8B_CatalogEvidence), `test_p20_setup.py` updated.

## Install-flow honesty fixes (not committed)
Model rows in the setup wizard now state repository, quantisation, free space needed and exactly what the download check does and does not prove; `download()` returns an `integrity` report (checksum from the same host, not independent); `verify()` rejects a Qwen3 entry whose GGUF header declares another architecture before loading it. Nothing downloaded or installed; Linux/fake-tested only. Tests: `tests/test_p37_install_flow.py`.

## Stage 5 master-order pass: runtime pin and lifecycle fixes (not committed)
Runtime pinned to `llama-cpp-python==0.3.36` (pyproject extra, Windows build script, two existing CI files); the build downloads, inspects (`verify_wheel.py`) and installs the exact wheel file, optional `-RuntimeSha256` enforcement. `verify_wheel.py` now accepts the official `py3-none-win_amd64` tag (earlier docs wrongly expected `cp312`). Install refuses a GGUF of the wrong architecture before hashing/copying; `meta.json` records that the checksum reference came from the same host; `GGUFEngine.close()` releases model memory and setup verification keeps one model copy at a time; regression test that a missing local runtime never triggers a network call. Nothing downloaded or installed; Linux/fake-tested only. Tests: `test_p38_runtime_and_lifecycle.py`, `test_p39_no_silent_cloud.py`.

## Import Brain: model import, hardware-aware safety and transactional switching (not committed)
New `jarvis.brains` package (compatibility classifier with the runtime's own architecture table, memory estimates, `BrainManager` with import-in-place, download-without-activation, real-generation test, transactional activate with rollback, deactivate, return to previous, safe removal) and an "Import Brain" page. `Hardware.ram_available_gb`; `ModelStore.register_external/installed_report/gguf_file/remove(delete_linked_file)`; `SetupEngine.download(activate=False)`; `LocalAssistant.swap_engine/release_engine`; richer GGUF header summary; `brains` section in the diagnostics report; provenance (`source_repo`, `source_revision`) recorded for catalog downloads. Catalog, default recommendation and runtime pin unchanged; Qwen3-8B stays install-on-request. Tested on Linux with synthetic GGUF files, a scripted fake engine, simulated hardware and a mocked network; no real model, runtime, download or Windows run; the page was never launched. Tests: `test_p41_import_and_compat.py`, `test_p41_switching.py`, `test_p41_download_and_catalog.py`, `test_p42_brain_controller.py`, `test_p43_brain_security_and_ownership.py`; docs section 28.

## Stage 5 final audit: transactional switching order, import/removal honesty, stricter compatibility (not committed)
Fixed `activate()` ordering (selection pointer before engine swap, with restore), atomic/defensive `active.json`, import refused if the file changes while hashed, removal reported only when files are really gone, disk-full import errors, scrubbed error text, test records flagged `real`, three-tier support labels (only a real successful test is "Verified on this PC"). Compatibility now blocks vocabulary-only, adapter/mmproj/imatrix, split-part, unknown-tokenizer and unknown-pre-tokenizer files, handles latent-attention (DeepSeek-V2/V3 family) memory honestly, and classifies DeepSeek variants from the runtime source (docs section 29). Brain modules added to the packaging hidden-import list. 27 new tests (`tests/test_p44_final_audit.py`), 15 mutations killed. Stage 5 remains OPEN: no real model, runtime, PySide6 or Windows verification.

## Stage 6: voice system (not committed)
Modular speech engines (`voice/engines.py`; Windows SAPI is the one real engine), `VoiceManager` (discovery from engine-reported voices, feminine / masculine / not specified, saved global voice, optional per-personality-mode override, visible fallback), `VoicePreview` (loading/playing/finished/stopped/error with honest wording, stop, timeout, cleanup), read-only microphone and recogniser check, configurable speech-detection floor, controller methods and a Voice-page section (never run under PySide6). `SpeechOut` now routes through the manager so a voice change applies to the next sentence. SAPI voice list carries gender/age/enabled and the speech process exit code. 73 new tests (`tests/test_p45_voice_system.py`), all with fake audio. Nothing verified on Windows, with real audio, Vosk, or PySide6; Stage 5 blockers unchanged.

## Stage 6.5: Kokoro local neural voice (not committed)
Kokoro (`kokoro-onnx`, ONNX Runtime, CPU) added as a second engine through the Stage 6 registry; SAPI unchanged. New `voice/kokoro_engine.py` (engine, worker + synthesis threads, single shared model, output-only audio sink, sentence chunking) and `models/kokoro_files.py` (model-file state checks, consented cancellable download through the Stage 5 network policy, local hash record). Voices `af_heart`, `af_aoede`, `af_nicole`, `am_onyx` are offered only if the installed voices file lists them; ids are configurable; no accent/gender/quality is invented. Voice page: engine selector, Use Windows speech, Kokoro status and Install / repair / Cancel / Check controls. Preview gains a `generating` state and reports synthesis and playback separately (never "heard"). A chosen Kokoro voice that cannot be used is never replaced by another engine: nothing is spoken and two recovery choices are shown. Nothing is bundled (espeak-ng is GPL). New optional extra `kokoro`; `.gitignore` covers `*.onnx` and `voices-v*.bin`. Defects found by the tests and fixed: interface blocked during model load, network error reported as disk error, speak-after-shutdown, failed job leaving its synthesis thread waiting, playback claimed for empty text. The model is built on ONNX Runtime's CPU provider only.
**Not verified:** real Kokoro inference, real model download, real audio, PySide6, Windows (see `docs/STAGE6_5_KOKORO.md`).
### Tests
- New: `tests/test_p46_kokoro.py` (97 tests, all fakes).

## Stage 7: Full personality system
- 17 personalities (new: Friendly Companion, Humorous Assistant, Sexy; Hype Coach renamed), each with a versioned behaviour spec.
- Five persistent customization controls (offsets with per-mode identity limits), Personality page controls, `persona control` CLI.
- 15-scenario x 17-mode evaluation suite (`jarvis persona suite`); docs/STAGE7_PERSONALITY.md.

## Stage 7 audit
Verified e63ee22 and the ZIP. Fixes: contradictory legacy trait text vs spec/controls (D1), one bad control value discarding all settings (D2), missed distress/emergency phrasings (D3). Added `persona audit` real-model workflow, regression tests (test_p48, p49, p50). Real model, Qt and Windows still NOT run. See docs/STAGE7_AUDIT.md.
