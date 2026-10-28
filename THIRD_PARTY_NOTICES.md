# Third-party notices

Project JARVIS 0.6.0 is released under the MIT License (see LICENSE).

No model weights and no training datasets are bundled in the application or installer. The app can download the free chat model Qwen3-1.7B (Apache-2.0, by the Qwen team / Alibaba Cloud; GGUF conversion by the llama.cpp organisation) from its publisher on the user's request; the model keeps its own license, shown with the model. Any model package carries its own LICENSE.txt, which you must read before installing or redistributing it.

## Runtime and build dependencies

Versions and licenses below were read from the packages installed in the build environment when this file was generated. Regenerate it on the build machine (`python scripts/release_tools.py notices`).

- **numpy 2.5.3**: numerical arrays. License metadata: BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0. Note: BSD-style; keep copyright notice.
- **cryptography 50.0.1**: signature verification. License metadata: Apache-2.0 OR BSD-3-Clause. Note: Apache-2.0 OR BSD-3-Clause; keep notices.
- **torch**: model training/inference (optional extra). NOT INSTALLED when this file was generated, so version and license were not read. Note: BSD-3-Clause; a CUDA build also redistributes NVIDIA libraries under NVIDIA's own license terms: prefer the CPU-only build for distribution and verify.
- **PySide6**: desktop window (jarvis-gui.exe; optional extra [ui], bundled in the Windows build). NOT INSTALLED when this file was generated, so version and license were not read. Note: LGPL-3.0 / GPL / commercial (Qt): LGPL use needs the license text, notices, and the ability for users to replace Qt libraries (one-folder build helps). Verify with counsel.
- **llama-cpp-python**: local AI runtime (llama.cpp bindings). NOT INSTALLED when this file was generated, so version and license were not read. Note: MIT; bundles llama.cpp (MIT) and ggml. Keep notices.
- **sounddevice 0.5.6**: microphone access (optional voice extra). License metadata: MIT. Note: MIT; bundles/uses PortAudio (MIT-style). Verify before bundling.
- **vosk**: offline speech recognition (optional voice extra). NOT INSTALLED when this file was generated, so version and license were not read. Note: Apache-2.0 for the library; each Vosk MODEL has its own licence: check it before redistributing any model.
- **comtypes**: Windows UI Automation access for desktop control (Windows only). NOT INSTALLED when this file was generated, so version and license were not read. Note: MIT; keep notice.
- **tzdata**: IANA time-zone database for Windows (scheduling in named zones). License: Apache-2.0. Bundled in Windows builds.
- **pypdf**: reads text PDFs the user asks about. License: BSD-3-Clause. Bundled in Windows builds; keep notice.
- **psutil 7.2.2**: optional network interface state for system_info (not bundled unless installed). License metadata: BSD-3-Clause. Note: BSD-3-Clause; keep notice.
- **pyinstaller**: build tool (not shipped, but its bootloader is embedded). NOT INSTALLED when this file was generated, so version and license were not read. Note: GPL-2.0 with bootloader exception; verify.

Optional, NOT bundled (installed by the user; see docs/STAGE6_5_KOKORO.md): **kokoro-onnx** (MIT, read on its PyPI page), the **Kokoro-82M** model files (Apache-2.0 as stated by the publisher; downloaded only after the user confirms), and the packages kokoro-onnx installs, including **espeak-ng** (GPL-3.0). The licences of its other dependencies (phonemizer-fork, espeakng-loader, onnxruntime) were not individually verified; check them before ever bundling any of it.

Python itself is distributed under the PSF License. This summary is informational, not legal advice.
