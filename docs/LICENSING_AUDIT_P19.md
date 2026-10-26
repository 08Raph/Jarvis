# Commercial licensing audit (Prompt 19) - engineering notes, NOT legal advice

Purpose: list what would ship or be fetched, the licence as far as it could be established, the obligation it creates, and what needs a qualified reviewer.
Method: versions and licences were read from installed package metadata where the package is installed in the build sandbox; for packages that are not installed
(PySide6, llama-cpp-python, vosk, comtypes, PyInstaller, torch) the licence comes from general knowledge of the upstream project and is **unverified here**.
`THIRD_PARTY_NOTICES.md` carries the per-package list; regenerate it on the build machine.

| Component | Role | Licence (basis) | Obligation / risk | Needs review? |
|---|---|---|---|---|
| Project JARVIS | the app | MIT (LICENSE file) | owner decides commercial terms | no |
| numpy 2.5.3 | maths | BSD-3 and others (read from metadata) | keep notices | no |
| cryptography | signature check | Apache-2.0 OR BSD-3 (metadata, earlier audit) | keep notices | no |
| PySide6 / Qt | GUI | LGPL-3.0 / GPL / commercial (**unverified here**) | LGPL: ship licence text, allow Qt library replacement (one-folder build), no static link; or buy a Qt licence | **yes** |
| llama-cpp-python / llama.cpp / ggml | local inference | MIT (**unverified here**) | keep notices | low |
| CUDA build of the runtime | GPU | NVIDIA CUDA EULA terms for redistributable DLLs (**unverified**) | only specific CUDA runtime files may be redistributed; simplest is to NOT bundle and let the user install a GPU build | **yes** |
| Qwen3 1.7B | default model | Apache-2.0 (publisher page read in earlier development) | not bundled; show licence at download | confirm at download |
| Qwen3 4B / 8B | optional models | Apache-2.0 expected (**not fetched**) | same | **yes, before listing as supported** |
| Vosk library / Vosk model | offline speech recognition | library Apache-2.0; each model has its own licence (**unverified**) | read the model licence before bundling or recommending | **yes** |
| Windows SAPI voices | speech output | part of Windows | uses the OS voices; do not redistribute | low |
| sounddevice / PortAudio | microphone | MIT (metadata) / MIT-style | keep notices | no |
| comtypes | UI Automation | MIT (**unverified here**) | keep notice | low |
| psutil, requests, openpyxl, python-docx, pypdf, pdfminer | optional/test | BSD / Apache / MIT (read from metadata) | not required at runtime except pypdf (optional) | no |
| PyInstaller | build tool | GPL-2.0 with bootloader exception (**unverified here**) | exception normally allows proprietary output; confirm | **yes** |
| Python runtime | bundled | PSF licence | include licence | no |
| Inno Setup / installer tooling | installer | not audited | check terms | **yes** |

Model weights are never bundled by this project; a commercial redistribution plan that DID bundle weights would need its own review of each licence's
attribution and use-restriction terms. Telemetry, accounts and paid APIs: none exist (see PRIVACY_AND_NETWORK.md).
Open question for counsel: obligations when the app downloads third-party model files on the user's behalf and when it is sold rather than given away.
