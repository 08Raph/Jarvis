# Model files: identity, source, licence and how to supply them

Model weights are a **separate, user-managed component**. They are NOT in the source archive. A build made with `-BundleModel` (the default of `build_installer.bat`)
ships exactly ONE weights file inside the installer, `bundled_models/qwen3-1.7b-q4_k_m/model.gguf`, which is copied into the user's own model folder on first start
(`src/jarvis/models/bundled.py`). `scripts/check_release_assets.py` fails a build that contains any other `*.gguf`, `*.pt` or `*.safetensors`, or that file with a size or
SHA-256 different from the pin in the program. The pin is an OBSERVED value, not confirmed by the publisher. See `docs/BUNDLED_EVERYTHING.md`.

## What model the app uses today
| | |
|---|---|
| Model | **Qwen3-1.7B**, 4-bit (`Q4_K_M`) GGUF, catalog id `qwen3-1.7b-q4_k_m` |
| Made by | The Qwen team (Alibaba Cloud). GGUF conversion by the llama.cpp organisation (`ggml-org/Qwen3-1.7B-GGUF`) |
| Licence | Apache-2.0 (checked on the publisher's page during development; re-check before redistributing) |
| Size | about 1.28 GB (the real size and SHA-256 are read from the publisher's metadata at download time) |
| Runtime | `llama-cpp-python` (llama.cpp, MIT), CPU. Needs about 4 GB free RAM |
| Role | **Pretrained interim model.** It is *not* the project's from-scratch pipeline and was not trained by this project. |

The from-scratch training pipeline (`jarvis/model`, `jarvis/train`) is separate code. No model trained with it exists yet, and nothing in the
window or the docs may describe Qwen as such. Conversation history and memory are stored as data and put into the prompt; they never retrain or change any weights.

## Ways to supply weights
1. **Download from the publisher** (asks first, needs internet once): `jarvis models fetch qwen3-1.7b-q4_k_m` or the setup guide `jarvis setup`.
   Only the file is fetched; chats are never sent. The download is size-bounded and checked against the publisher's SHA-256 when one is provided.
2. **Use a file you already have:** `jarvis models add-gguf C:\path\model.gguf --id my-model --license Apache-2.0` (a licence id is required; add `--sha256 <hash>` if the publisher gave one, otherwise `--accept-unverified`).
3. Check state any time: `jarvis models list`, `jarvis models verify <id>`, or the **Status** page of the window.

Models live in `%LOCALAPPDATA%\ProjectJarvis\models`, outside the program folder, so upgrades keep them.

## What you see when something is wrong (no silent fallback)
| Situation | State shown | What to do |
|---|---|---|
| No model installed | `no_model` | Install one (above). Chat is disabled; nothing is answered on its behalf. |
| `llama-cpp-python` missing from the build | `runtime_missing` | Use the official build or `pip install llama-cpp-python` (see `docs/LOCAL_MODEL.md`). |
| File damaged, truncated or checksum mismatch | `model_invalid` | Run `jarvis models verify <id>`, then fetch it again. |
| Not a chat model / wrong format | `not_a_chat_model` | Choose a chat GGUF. |
| Anything unexpected | `error` | Run `jarvis diagnostics` (redacted) and see `docs/TROUBLESHOOTING.md`. |

Jarvis never sends your prompt to a paid or hosted AI service. There is no such code path and no API-key setting.

## Offline versus internet
- **Works without internet once a model is installed** (design intent): chat, memory, desktop tools, missions, schedules, coding tools on local files, monitoring.
  *This has not been verified with the real model and runtime on Windows. Treat it as unproven until `jarvis selftest` passes on your PC.*
- **Needs internet only when you start it:** model download, `research` web fetches (off by default, per-feature permission), update check (off by default).
- Telemetry is opt-in and cannot send anything unless the build contained a server address (`docs/TELEMETRY.md`).

## If you want to bundle weights in your own distribution
Check, in this order: the licence permits redistribution (Apache-2.0 does, with notice and licence text), the archive size limits you must meet,
and that you ship the licence and attribution next to the file. The project does not do this for you.
