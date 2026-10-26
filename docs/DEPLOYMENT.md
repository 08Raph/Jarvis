# Deployment model: local inference in a distributable EXE

## What the product is
A Windows application that runs the project's own model **on the user's computer**. There is no AI
server to pay for, no API key, no account, and no per-message billing. The model runs inside the app;
prompts never leave the machine during inference.

## How the model reaches the user: options and trade-offs

| Option | Pros | Cons |
|---|---|---|
| A. Bundle weights in the installer | Works offline from first launch; one download | Large installer; every model change needs a new installer; one size for everyone; weights may only be bundled if their license (and their training-data licenses) permit redistribution |
| **B. Installer without weights + integrity-checked model download (default)** | Small installer; pick a size that fits the PC; update or roll back the model independently | Needs internet once, only after explicit consent; the publisher must host files (hosting bandwidth is the publisher's cost, not an AI API cost) |
| **C. Import a model package from a local file (also supported)** | Fully offline / air-gapped; no hosting needed | The user must obtain the file and its trusted SHA-256 out of band |

**Chosen:** B plus C. Option A is reasonable later for the tiny tier only. Nothing is bundled today because
no model has been trained and no data/weight license has been cleared.

## Runtime packaging trade-offs
- PyInstaller + CPU-only PyTorch: simple, one toolchain with training. Expected to be large (hundreds of MB, **not measured**; nothing has been built).
- ONNX Runtime would be smaller but needs a model-export step. Not implemented.
- Unsigned EXEs trigger Windows SmartScreen and can attract antivirus false positives. A code-signing certificate
  costs money and is optional; the app does not need it to run. Signing the **update manifest** (free, Ed25519) is separate and implemented.
- No external AI program (such as a separate model server) is required, so the user installs one thing.

## What is implemented vs not (honest status)
| Capability | Status |
|---|---|
| Hardware detection + tier recommendation (`jarvis hardware`) | Implemented, tested on Linux only; Windows RAM path (ctypes) untested |
| Model packages (`.jarvismodel`), safe extraction, per-file hashes, required LICENSE.txt | Implemented, tested |
| Install, verify, select, remove, previous-version rollback (`jarvis models ...`) | Implemented, tested |
| Integrity-checked download (HTTPS only, size-capped, redirect-checked, hash-verified) | Implemented; tested against a localhost HTTP server, **never against a real HTTPS host** |
| Signed catalogs and update manifests (Ed25519) | Implemented, tested; **no real keys exist** and no catalog or update URL is published |
| Update check (opt-in, never installs) | Implemented, tested locally |
| Installer download/apply inside the app | Not implemented (user downloads and checks SHA-256 themselves) |
| Context-window budgeting + retrieval for long chats | Implemented, tested; needs an instruction-tuned model to be useful (not built) |
| Backups, schema-version guard, `jarvis restore` | Implemented, tested |
| PyInstaller spec, Inno Setup script, build and clean-install scripts | Written; **not run, not compiled, not tested** |
| Desktop UI, voice | Not started |

## Hardware and requirements
See `docs/MODEL_REQUIREMENTS.md` (generated). These are estimates; no model has been trained or measured, and speed on any PC is unknown.
The app does not promise that large models run well everywhere: `jarvis hardware` labels each tier ok / tight / too_large / unknown.

## No artificial limits, and honest limits
The app imposes no message, daily, weekly, or lifetime quotas (verified by a test that stores thousands of messages and by a source scan).
Real limits remain: the model reads at most its context window (tiers: 128 to 1024 tokens), plus disk, RAM, CPU time, and model quality.
Long sessions work by keeping every message in SQLite, retrieving relevant notes, and dropping the oldest turns from the prompt first.
Retrieval and memory do not make the base model smarter; they only decide what it gets to read.

## Upgrade, rollback, uninstall
- Application: installer upgrades in place (same AppId). User data lives in `%LOCALAPPDATA%\ProjectJarvis`, outside the install folder.
- Before replacing files the installer runs `jarvis backup` with the OLD build (if present).
- Database schema version is stored; an older build refuses a newer database instead of corrupting it. Recovery: `jarvis restore <backup>`.
- Models: a new version of the same id keeps the previous one; `jarvis models rollback <id>` swaps back.
- Rolling back the application = install the older installer (keep old installers). Inno Setup does not keep the previous app files.
- Uninstall removes the app. Models, memory and settings are kept unless the user answers Yes to a prompt; silent uninstall never deletes user data.
