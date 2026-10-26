# Prompt 20 review - setup wizard, diagnostics, honest Windows status

**Status: PARTIAL. Source and logic delivered and tested on Linux with fakes. NO Windows EXE or installer was built, NO real model was downloaded or run,
NO GPU was used, the new wizard window has NEVER been displayed (PySide6 is not installable in the authoring sandbox).** The Stage-1 evidence run
on the owner's Windows PC has not been returned yet.

## Why no EXE/installer here
PyInstaller and Inno Setup produce Windows binaries only on Windows. This sandbox is Linux with no Wine. The repository already contains the pipeline
(`scripts/build_windows.ps1`, `packaging/jarvis.spec`, `packaging/installer.iss`) and a GitHub Actions workflow (`.github/workflows/build-windows.yml`,
windows-2022 runner, free for public repositories) that builds the installer, then silently installs, launches and uninstalls it on the runner. That workflow
has never run; the first green run is the first real evidence. This milestone changes the installer's final page so the default is "Launch Project JARVIS"
(the GUI wizard), not a console.

## What the wizard does (src/jarvis/app/setup_engine.py; window: src/jarvis/ui/setup_dialog.py)
1. **System**: Windows version vs the supported minimum, CPU threads, RAM, free disk, GPU (NVIDIA via nvidia-smi), AI runtime present?, and three separate
   GPU facts: detected / GPU-capable runtime present / speed-up verified. Blockers (runtime missing, tiny disk, old Windows) stop the wizard with a reason.
2. **Model**: the selector's recommendation plus every catalog model with licence, download size, estimated memory, fits/doesn't, and whether its metadata was
   confirmed. Unconfirmed models are never auto-recommended; a model that does not fit is never downloaded or silently replaced.
3. **Download**: only after the person presses the download button (the page shows size and licence first). Free-disk check, progress, cancel, 3 tries with
   back-off for network errors, no retry for checksum failures, partial files removed, publisher SHA-256 enforced by the existing store.
4. **Verify**: full model hash; load on CPU; real prompt; measure load time, first-token time, tokens/s, peak memory. If a GPU-capable runtime exists, repeat
   with all layers on the GPU; GPU mode is chosen only if it was >1.2x faster AND GPU activity was seen, otherwise CPU stays. GPU failure -> CPU with the error.
   The choice is saved (`acceleration.json`) and the app's own engine is then loaded exactly as normal use will load it and must agree.
5. **Finish**: summary (model, licence, mode, GPU verified?, CPU fallback, limitations). `setup_state.json` says completed only for a REAL run.
Reopen any time: Status page -> "Run AI setup again". Export: Status page -> "Export diagnostic report" (field list is inside the report).

Engine precedence for GPU layers (every part of the app, including self-test and missions, goes through `GGUFEngine`): explicit argument > `JARVIS_GPU_LAYERS` >
`acceleration.json` written by setup > 0 (CPU).

## Bundled vs downloaded
Bundled by the installer build: Python runtime, app, llama-cpp-python (CPU build), PySide6. Downloaded after consent: the model. NOT bundled: any model weights, any GPU
runtime. **A GPU-enabled llama runtime is not part of the installer**, so on the packaged app "GPU present, no GPU-capable runtime" is the expected result until a
GPU variant of the runtime is shipped (blocker; needs CUDA/Vulkan redistribution review, see LICENSING_AUDIT_P19.md).

## Cost and licence summary (engineering reading, not legal advice; unverified items marked)
| Component | Download free | Commercial use | Ongoing cost | Obligations | Account/internet |
|---|---|---|---|---|---|
| Qwen3 1.7B GGUF | yes | Apache-2.0 (publisher page read earlier) | none | keep licence/notice | internet once to download |
| Qwen3 4B / 8B | yes (expected) | Apache-2.0 expected, NOT confirmed here | none | same | same |
| llama.cpp / llama-cpp-python | yes | MIT (unverified here) | none | notice | none |
| PySide6 / Qt | yes | LGPL-3.0 route: conditions apply (unverified) | none | replaceable libs, notices | none |
| PyInstaller | yes | GPL-2.0 + bootloader exception (unverified) | none | n/a | none |
| Inno Setup | yes | free incl. commercial (terms unverified) | none | n/a | none |
| Vosk + model | yes | library Apache-2.0; each model has its own licence (unverified) | none | per model | none |
| Windows SAPI voices | included in Windows | OS component | none | do not redistribute | none |
| Code signing | NOT free | unsigned installers trigger SmartScreen warnings | certificate cost | - | - |
No paid API, subscription, credits, account or cloud fallback is required anywhere in the core path. The installer is NOT signed.

## Voice and personality
Unchanged by this milestone. The voice requirements (8+/10 naturalness, distinct masculine/feminine identities, a Voice & Speech page with rate/pitch/accent/preview,
local neural STT/TTS) are NOT implemented: no neural TTS engine is bundled, no audio was generated or listened to, no score is given. Next voice milestone: evaluate
free commercially-usable local TTS engines by actual listening on the target PC.

## Tests
Linux, `PYTHONPATH=src python -I -m unittest discover -s tests`: **1709 tests, OK, 6 skipped (152 s)**. New: `test_p20.py` (13), `test_p20_setup.py` (39), `test_p20_app.py` (16).
Simulated: every model, download, GPU and hardware fact in these tests. Real Windows: none. Real model: none.
Negative controls: **22 of 22 deliberate bugs detected** (3 gaps found on the first pass - needs_setup with a damaged model, the app-engine agreement check, and a model-load failure recorded as a pass - were closed with new tests; `docs/PROMPT20_NEGATIVE_CONTROLS.json`).

## Unverified / not done
Windows EXE and installer build; wizard window display; real download; real inference (CPU or GPU); GPU offload; offline run; full test suite on Windows;
voice quality; clean-machine install/uninstall; signing. Next milestone: run `build-windows` on GitHub Actions (or the build script on Windows), install the
produced setup, and let the wizard run on the target PC; return `setup_report.json` and the exported diagnostic report.


## Addendum: first real Windows build attempt (blocked before it could start)
* Archive verified: `project-jarvis-prompt20-stage2.zip` SHA-256 `1425923e...0f85` matched.
* The workflow (`.github/workflows/build-windows.yml`) runs on `windows-2022`, installs Inno Setup, runs `scripts/build_windows.ps1` (tests, PyInstaller one-folder build with the
  CPU llama runtime and Visual C++ runtime, headless GUI self-test, Inno installer), then silently installs/launches/uninstalls. Python is bundled; the customer needs none.
  It needs a GitHub repository holding the project (public repositories get free minutes; a private one uses the account's limited free minutes, Windows minutes count double).
  No secrets are needed (signing is skipped; the installer is unsigned).
* **What it did NOT do before this addendum:** open the wizard, download a model, run inference, test offline, or keep logs. Added now (never run): `jarvis setup-auto`
  (non-interactive wizard engine; downloads only with `--accept-download`; exit 0 only for a real completed setup), the headless GUI self-test now also builds the wizard window,
  and `scripts/ci_real_model_test.ps1` which on the runner: installs, launches the real `jarvis-gui.exe` on the runner desktop (screenshot + window titles must include the setup wizard),
  runs the real wizard engine with a real model download and real CPU inference, runs the self-test, checks the diagnostics for the Windows user name, blocks the app's network with a
  Windows Firewall rule (and proves the block by a failing download), repeats the self-test offline, uninstalls. Logs and JSON go to the `ProjectJarvis-ci-logs` artifact even on failure.
* **Blocker:** the session has no repository for this project. GitHub access in this session is limited to the repositories listed for it (08Raph/Learn, sightforge-updates, sightforge,
  Sight-Forge-Demo), none of which is Jarvis; creating a repository, or pushing the project into an unrelated one, would publish or store your commercial source code without your decision,
  so it was not done. **No workflow run exists, so no Windows build, installer, wizard display, model download, inference, offline or GPU result exists.**
* **GPU:** CI runners have no GPU. The packaged runtime is the CPU build; GPU acceleration cannot be verified in CI and a GPU-enabled runtime is not packaged (redistribution/licence review pending).
