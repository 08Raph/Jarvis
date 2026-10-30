# What is inside the installer (the "everything in the exe" build)

Status: implemented; built and install-tested on a hosted Windows runner with the model inside (run 40997760878). Not yet run by a person on a physical Windows 11 PC.

## What ships

| Part | Where it is | How it gets used |
|---|---|---|
| Jarvis program, window, all pages, tools, missions, memory, personalities, Import Brain, Account screens | `jarvis.exe`, `jarvis-gui.exe` and `_internal\` | Nothing to install separately; no Python needed |
| Local AI runtime (llama.cpp via llama-cpp-python 0.3.36) | inside the app folder | Checked in the build with `jarvis.exe runtime --require` |
| Voice libraries (sounddevice, vosk) | inside the app folder when built with `-WithVoice` (the default for `build_installer.bat` and for CI) | Microphone and speaker use is still your choice; always-listening default is unchanged |
| **Qwen3-1.7B Q4_K_M model** (1,282,439,264 bytes, Apache-2.0, Qwen team / Alibaba Cloud, GGUF by the llama.cpp organisation) | `bundled_models\qwen3-1.7b-q4_k_m\model.gguf` when built with `-BundleModel` | Copied into `%LOCALAPPDATA%\ProjectJarvis\models` on first start; see below |
| Local-only sign-in server | frozen into the app; Start-menu entry "local sign-in server (this PC only)" or `jarvis.exe local-server` | Runs only when you start it, on 127.0.0.1, port 8765 |

## What is deliberately NOT inside

* The speech-recognition (Vosk) model is inside only when the build was made with `-BundleVoiceModel` (the default for `build_installer.bat`); see "Speech model" below. Its licence line is recorded from the publisher's page in `docs/VERIFICATION_LEDGER.md`, not assumed.
* Any model other than the one above. Qwen3-8B stays a choice you make yourself.
* A public Jarvis Online service. None exists; the local server makes accounts that exist on this PC only.

## How the bundled model is installed

1. First start of the window (or `jarvis first-run`, or the setup guide) finds `bundled_models` next to the program.
2. It acts only if this profile has no installed model and has never been seeded.
3. Before copying, it checks the file against the size and SHA-256 **pinned inside the program** (not a value stored next to the file), the GGUF header and the `qwen3` architecture. A wrong or damaged file is refused and nothing is installed.
4. The copy goes into your own model folder (so upgrades and uninstalls of the program never delete it). If no brain is active yet, it becomes the active one. No network is used.
5. A marker `bundled_seed.json` records that this happened once. If you remove the model later, it is not put back.
6. The installed record says `origin: bundled-with-installer` and `verified_origin: false`: the SHA-256 is the value observed on a genuine download (run 40955846622), **not** confirmed by the publisher.

Needs about 1.3 GB free for the copy (the installer's own copy stays in the program folder, so about 2.6 GB in total are used until you uninstall).

## How to build it

* `build_installer.bat` on Windows: voice + model + server, one download of the model (about 1.3 GB), then the pinned size and SHA-256 are checked by `scripts\check_release_assets.py --dist ... --require-bundled-model`.
* Smaller build without the model: `powershell -File scripts\build_windows.ps1 -SkipTorch -WithVoice`.
* GitHub Actions: pushes build without the model. A manual run with the input `bundle_model_consent` = `yes-bundle-qwen3-1.7b-q4_k_m` builds the full installer (one download). Nothing is uploaded as a workflow artifact.
* The installer stores the model uncompressed (`nocompression`) so the build stays fast. Inno Setup refuses a single installer above about 2 GB without disk spanning; if the build ever hits that, the build fails loudly rather than shipping something broken.

## What is verified (and what is not)

| Claim | Evidence |
|---|---|
| Seeding verifies the pin, refuses a wrong/damaged/swapped file, never overwrites a model you have, never re-installs after you removed it, uses no network | F: 30 tests (tests/test_p78_bundled_everything.py) with a tiny synthetic GGUF; hash enforcement mutation-checked |
| The build refuses any other weights file and a bundled file with the wrong size or hash | F: tests on a synthetic dist folder |
| Local server binds 127.0.0.1 only, has no host option, serves health and terms over real HTTP | F/R: loopback test |
| Installer script, build script and CI wiring contain the right flags | S: text checks in the tests |
| The real 1.28 GB file builds into an installer, installs silently, is installed into the profile on first run (exact size, active brain, no network), the install folder holds only that pinned file, and a silent uninstall keeps your data | **R: hosted Windows runner, run 40997760878 at main after 185f680; installer 1,453,504,221 bytes, SHA-256 E0E024FB6B2009C0050AD38D68F699D92B9546004342CEB18574B903912F4EDB** (CI does not upload it, so this exact file is not downloadable) |
| The bundled model then answers in the installed app | **N for this build.** The earlier real-model run (40955846622) proved the same pinned file answers through the packaged worker; the full-installer check deliberately does not run inference |
| It works on a physical Windows 11 PC | **N** |

## Honest notes about the build runs

* Two full builds were made, both with your approval: the first reached a 1.45 GB installer and then stopped on a bug in my build-manifest step (fixed). During that run the old clean-install check also ran `chat "hello"` after the first run had installed the model, so that run incidentally did one real chat reply (exit 0; the text was not captured). The check is now model-aware and never runs inference on a full installer.
* Three build problems only the voice libraries exposed were fixed: certifi's public certificate list (allowed at one exact path, only if it holds no private key) and 32-bit / ARM64 DLLs from sounddevice (left out of the 64-bit app).
* Licences of the newly bundled voice libraries and their dependencies (sounddevice, vosk, requests, certifi and others) have not been legally reviewed.
* The installer is still unsigned.

## Speech model (small English Vosk model, about 40 MB)

* **What:** the folder `vosk-model-small-en-us-0.15` from its publisher (alphacephei.com), shipped at `bundled_models/vosk-model-small-en-us-0.15`.
* **First start:** the folder is copied to `%LOCALAPPDATA%\ProjectJarvis\voice_models\` and, only if no speech model is set yet, `vosk_model` in your voice settings points at that copy. Nothing else in the voice settings changes. It is done once per profile and is not put back if you clear the setting. A speech model you set yourself is never replaced.
* **Check:** the whole folder is compared with a digest and file count compiled into the program (`src/jarvis/voice/bundled_vosk.py`). The build stops on a mismatch, and while the pin is empty the program refuses to install the folder.
* **What this changes on a fresh profile:** with a speech model present, the window's first start can now actually turn wake-word listening on (it was already the default on a fresh profile, but it had no model to use). The microphone still opens only if a sound device exists, the mute switch and status stay honest, and raw audio is not kept.
* **Not claimed:** voice quality, accuracy and microphone behaviour on a real PC are not verified by CI. The pin is an observed value from a genuine download; the publisher does not sign the model.
