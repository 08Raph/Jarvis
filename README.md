# PROJECT JARVIS

A local Windows assistant. Two separate model tracks: (1) a **research pipeline** with its own tokenizer and transformer code (tiny, never trained, kept for experimentation), and (2) the **shipped chat model**, a third-party free open-weight model (default Qwen3-1.7B, Apache-2.0) that is downloaded on the user's request and run locally. The app does not claim to have created track 2's weights. It runs on the user's computer: no paid AI API, no message quotas, and
no network use during local inference. Local use needs no account. An optional Jarvis Online mode (sign-in required, off until you connect) is described in `docs/ONLINE.md`. See `docs/CONCEPTS.md` for what "original" does and does not mean.

## Status (Prompt 4 implemented in code; hardware parts untested)

| Area | State |
|---|---|
| Tokenizer, provenance ledger, data pipeline, estimator, memory, tools, benchmarks, gate, model card | Implemented and tested (see earlier milestones) |
| Network policy: every online feature off by default, single choke point, audit log | Implemented, tested |
| Hardware detection + model-tier compatibility | Implemented, tested on Linux only |
| Model packages, install/verify/select/remove/rollback, hash-checked download | Implemented, tested (download only against localhost) |
| Ed25519-signed catalogs and update manifests, opt-in update check | Implemented, tested; no real keys or URLs exist |
| Context budgeting + retrieval for long chats | Implemented, tested |
| Backups, schema-version guard, `restore`; first-run setup; CLI (`python -m jarvis`) | Implemented, tested |
| PyInstaller spec, Inno Setup script, build + clean-install scripts | Written; **not run or compiled** |
| GPT model, trainer, inference engine (research track) | Written, **never run** (needs PyTorch) |
| Local open-weight chat model: GGUF install/verify, guarded download, chat, streaming, hardware fit, selftest | Implemented; tested with a fake runtime only. **Real inference NOT tested**; run `jarvis selftest` on Windows (docs/LOCAL_MODEL.md) |
| Voice session logic, wake matching, speech filter, personality engine | Implemented, tested with fakes |
| Real microphone / Vosk / Windows speech backends | Written; **never run on real hardware** (see docs/VOICE.md) |
| Jarvis Online: server with sign-in, consent, **learning OFF by default (explicit opt-in)**, export/delete-chats/delete-account; client `jarvis online` (blocked in `--guest`) | Implemented, tested on localhost; **no AI model behind it**, TLS/internet untested (docs/ONLINE.md) |
| Desktop control: permission-gated tools for files, apps, windows, UI Automation, keyboard/mouse, screenshots/OCR, system info; act-observe-verify runner, emergency stop, audit | Core **tested on Linux with real files and a FAKE simulated desktop**; Windows APIs written but **not run on Windows** (docs/DESKTOP_CONTROL.md) |
| Optional telemetry (diagnostics/usage; opt-in, allowlisted, no chat content) + receiving endpoint/report | Implemented, tested locally; off by default and inert unless a build bakes in a server address (docs/TELEMETRY.md) |
| Adaptive personal memory: 8 categories with source/confidence/expiry, secrets refused, remember/correct/usual-style/like-last-time rules, verified-only procedures, profiles, guest mode, export/delete (`docs/MEMORY.md`) | Implemented; tested with real SQLite and a fake model. **Never tried with a real model or real users.** No model retraining from conversations, by design |
| Desktop UI, installer download inside the app | Not started |

**No model has been trained, no benchmark scores exist, nothing has been built or installed on Windows.**
Without an installed model the CLI says so and exits; it never fabricates answers.

## Commands

    python -m unittest discover -s tests            # 339 tests: 338 pass, 1 skipped without torch (Linux only; never run on Windows)
    python -m jarvis setup                          # guided first-run (what the Start-menu shortcut runs)
    python -m jarvis first-run                      # folders + safe defaults + hardware check (no network)
    python -m jarvis hardware
    python -m jarvis privacy [enable|disable FEATURE]
    python -m jarvis models install PKG --id ID --sha256 HASH   # or --accept-unverified
    python -m jarvis models list|verify|select|rollback|remove ID
    python -m jarvis chat ["text"]                  # local model (interactive if no text); needs runtime + model
    python -m jarvis models catalog|fetch|add-gguf ... ; python -m jarvis selftest ; python -m jarvis runtime --require
    python -m jarvis backup | restore FILE
    python -m jarvis voice show|set|devices|voices|say|run     # see docs/VOICE.md (voice hardware untested)
    python -m jarvis online connect|register|login|chat|learning|export|delete-account   # optional service, docs/ONLINE.md
    python -m jarvis telemetry status|schema|preview|enable|disable|send|delete-my-data   # optional, off by default
    python -m jarvis desktop status|enable|allow-folder|run|task|stop   # control this PC, docs/DESKTOP_CONTROL.md
    python -m jarvis agent "goal"                                       # local model operates the PC with your approval
    python -m jarvis memory list|add|edit|delete|pending|approve|export|forget-all|where|learning|summaries   # see docs/MEMORY.md
    python -m jarvis --profile NAME chat | python -m jarvis --guest chat    # separate profile / temporary session
    python -m jarvis personality show|set-mode|set-trait|approve
    python scripts/release_tools.py --help          # checksums, keys, signed manifests, model packages, notices
    python scripts/smoke_train.py                   # needs torch: train + resume + inference check

## Build and test on Windows 11 (free tools: Python 3.12 64-bit + Inno Setup 6)
Exact commands and the verification checklist are in `docs/WINDOWS_TEST_CHECKLIST.md`; background and limits in `docs/WINDOWS_DISTRIBUTION.md`.
Short version: `powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -CheckOnly`, then `build_installer.bat`, then install the result and run
`jarvis setup` and `jarvis selftest`. Nothing here has been run on Windows yet; the installer is not production-ready.

## Docs
`DESKTOP_CONTROL.md` (operating this PC: permissions, confirmations, verification), `MEMORY.md` (adaptive memory, profiles, guest mode), `WINDOWS_TEST_CHECKLIST.md` (what to run and verify on Windows), `TELEMETRY.md` (what is sent, consent, your database), `LOCAL_MODEL.md` (model choice, licenses, requirements, Windows 11 build), `ONLINE.md` (online service, consent, learning, free hosting reality), `WINDOWS_DISTRIBUTION.md` (build, install, signing, honest limits), `VOICE.md` (voice, privacy, licences, personality), `REVIEW_M3.md` (earlier review and Windows test steps), `DEPLOYMENT.md` (deployment model, trade-offs, status table),
`PRIVACY_AND_NETWORK.md` (online features and what is/isn't verified), `BUILD_AND_RELEASE.md`, `MODEL_REQUIREMENTS.md`,
`CONCEPTS.md`, `ROADMAP.md`, `DATA.md`, `COMPUTE_ESTIMATES.md`, plus `THIRD_PARTY_NOTICES.md`.
