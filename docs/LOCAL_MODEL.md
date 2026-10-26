# Local AI model (runs on each user's Windows PC)

## Honest status - read this first
| Item | State |
|---|---|
| Runtime wiring (llama.cpp through llama-cpp-python), GGUF validation, install/verify/rollback, guarded download, chat, streaming, personality system prompt, hardware fit check, `selftest` | Implemented and tested **with a fake runtime and synthetic files only** (219 tests) |
| **Real inference with a real model** | **NOT TESTED.** The authoring sandbox could not install llama-cpp-python (package index blocked) and could not reach the model host, so no weights were ever loaded. **Nobody should say "it works" until `jarvis selftest` passes on a real Windows PC** |
| Model download from huggingface.co (file name + SHA-256 are read from the host's own metadata over HTTPS) | Implemented; tested against a local imitation server. The real host's response format was assumed from memory of its public API and **not verified** |
| Speed on your CPU | **Unknown**. `jarvis selftest` measures it and writes `selftest.json` |
| Prebuilt Windows wheel for your Python version | Documented by the runtime's authors for CPU use; Python-version coverage **not verified**. Python 3.12 64-bit is the safest choice |

The earlier from-scratch research pipeline (own tokenizer/transformer) is **kept** but is a separate, tiny, untrained track. The chat model the app ships/downloads is a **third-party open-weight model**; Project JARVIS does not claim to have created or trained it.

## Model choice and license research
Requirements: free, open weights, small enough for ordinary PCs without a GPU, license that lets users run it and lets you distribute the app without paying.

| Model | License (what I verified) | Size / fit | Decision |
|---|---|---|---|
| **Qwen3-1.7B** (GGUF by ggml-org) | **Apache-2.0** - shown on the Hugging Face page of `ggml-org/Qwen3-1.7B-GGUF`; Q4_K_M file listed as 1.28 GB | Needs roughly 4 GB RAM or more (estimate) | **Chosen default** |
| SmolLM3-3B | Apache-2.0 (per the European Open Source AI Index page); GGUF repository and file names **not verified** | ~2x the size of the default | Candidate; not in the catalog |
| Phi-4-mini-instruct (3.8B) | MIT per search results; **not confirmed on the model page** | Larger | Candidate; not in the catalog |
| Llama 3.2 1B/3B | Meta's custom community license (conditions on redistribution, attribution/naming, and gated download). **Not checked in detail** | Small | Not chosen: more obligations than Apache-2.0/MIT |
| Qwen2.5-3B and some other sizes | Some Qwen2.5 sizes use a non-commercial research license | - | Avoid unless you check each size's license |

Why Apache-2.0: you may use and redistribute it commercially if you keep the license/notice. This app does **not** bundle weights: each user's PC downloads the model from its publisher, and the license pointer is stored next to it. If you ever bundle weights into your own installer you must ship the Apache-2.0 text and the publisher's notices.
Add other models with `jarvis models add-gguf` (license information is mandatory). You are responsible for checking their terms. Check the current page before shipping: licenses and repositories change.

## PC requirements (estimates; not measured)
- **Windows 11** (also written for Windows 10 1809+), 64-bit CPU. A GPU is **not** required: the default path is CPU-only. GPUs are not used by the shipped wheel.
- **RAM** for the default model: about 4 GB minimum, about 7 GB or more recommended (file size + ~1.5 GB for context + room for Windows). `jarvis models catalog` and the setup guide compute this for your PC and refuse to download if clearly too small.
- **Disk**: about 1.5 GB free for the default model.
- **Speed**: depends on your CPU and memory; small 4-bit models on modern CPUs are usable, older/low-power CPUs may be slow. Run `jarvis selftest` for real numbers.
- **Internet**: only to download the model once (explicit confirmation). Chatting is fully offline afterwards.

## Using it
    jarvis setup                              (guided: hardware check, download or choose a model)
    jarvis models catalog                     (models, licenses, fit for this PC)
    jarvis models fetch qwen3-1.7b-q4_k_m     (asks first; ~1.3 GB)
    jarvis models add-gguf C:\path\m.gguf --id mymodel --license Apache-2.0 --license-url https://... --sha256 <hash>
    jarvis selftest                           (REAL test: loads the model, generates, measures speed)
    jarvis chat                               (interactive; or: jarvis chat "question")
    jarvis runtime --require                  (is the AI runtime inside this installation?)
Manual download if the in-app download fails: open the model page on huggingface.co in your browser, download the `...Q4_K_M...gguf` file, then use `add-gguf` with `--sha256` from the page's file details.
The assistant answers from the model only. It cannot browse, open files or control apps through the model: the tool registry exists but model-driven tool use is **not implemented**. Model output can be wrong; there is no abstention gate for these models.

## Windows 11 build (free tools only)
1. Install **64-bit Python 3.12** from python.org (tick "py launcher") and **Inno Setup 6** from jrsoftware.org.
2. In PowerShell: `cd project-jarvis`, then `powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -CheckOnly`.
3. `build_installer.bat`. It installs dependencies and the AI runtime (`llama-cpp-python`, prebuilt CPU wheel, no compiler needed), runs the tests, freezes the app, checks the frozen exe with `jarvis.exe runtime --require`, builds the installer, and writes `dist\installer\ProjectJarvis-Setup-0.6.0.exe` plus `SHA256SUMS.txt`.
4. If the runtime wheel cannot be installed: use Python 3.12, or install the free Visual Studio Build Tools (C++ workload) and CMake and run `scripts\build_windows.ps1 -SkipTorch -RuntimeFromSource`.
5. Install on a clean machine or Windows Sandbox, run `jarvis setup`, accept the model download, then `jarvis selftest`. **Only if selftest prints SELFTEST PASSED** can you say the local AI works on that PC. Send me `selftest.json` and any errors.
