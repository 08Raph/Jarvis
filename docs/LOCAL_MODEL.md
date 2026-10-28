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

## Download size and free-disk safeguards (Stage 5, D-9)
Implemented in `src/jarvis/models/diskspace.py` and `ModelStore` (`download_gguf`, `install_gguf`, `download`); tested on Linux with mocked network and disk readings (`tests/test_p29_download_safety.py`). **Not validated on Windows.**
* **Size stop:** a catalog download is stopped as soon as it exceeds **115 % of the catalog estimate** (`estimate * 115 // 100`, rounded down; exactly that many bytes is accepted, one more is refused), or more than the size the host itself lists, whichever is smaller. It is enforced while streaming, so a missing, wrong or changing `Content-Length` cannot bypass it; a header that already announces more than the limit is refused before the body is read. The partial `.part-<id>` file is always removed and nothing is registered.
* **Disk check:** before a download starts, and about every 256 MiB while it runs, free space on the model folder's volume must cover the remaining bytes (up to the limit) plus a margin of `max(1 GB, 10 %)`. `add-gguf` (which copies) needs one extra full copy of the file plus the margin, checked before the copy; a move (`fetch`) needs no copy space. The signed-package path checks download plus unpacked size, and a local `models install <package>` checks the package's declared unpacked size (an estimate; real bytes are still capped and hash-checked while unpacking). If free space cannot be read, the operation is refused. A "disk full" error while writing becomes a clear message, the staging folder is removed and an already installed model with the same id is left untouched.
* **`add-gguf` rules (review, Stage 5):** the file must be a regular file (a link to one is followed and only read); `--sha256` must be exactly 64 hex characters (spaces/case ignored) and is compared with the exact bytes installed, including after the copy; your source file is only read, never changed or deleted; on any failure the staging copy is removed and an already installed model with the same id keeps working. Optional `--expected-size-bytes N`: the publisher's size of the file, read from the model page (digits only, 1 to 10^13; zero, negative, decimal or text values are refused). A file larger than 115 % of N (`N*115//100`, rounded down; exactly that many bytes is accepted) is refused **before it is hashed or copied**; it never replaces the checksum, free-space, regular-file or rollback checks. Without it the command prints that the size limit was **not enforced** and `meta.json` records `size_limit_enforced: false` - Jarvis never guesses a size.
* **Messages** state required and available gigabytes only (no paths, user names or secrets).
* **Limits:** a free-space reading is a snapshot; nothing reserves space. On Windows CPython reports the volume's total free bytes, which may overstate what a quota-limited account can use. Antivirus scanning, other programs filling the disk, and a model folder on a different volume than the temp area are not modelled. The experiment's manual limits (>= 25 GB free before starting, <= 20 GB of downloads in total) remain human checks in `docs/STAGE5_VALIDATION_RUNBOOK.md`.

## Replacement and rollback (Stage 5; Linux-tested with mocked failures; Windows NOT verified)
* **What happens when an id is installed again** (`add-gguf`, `fetch`, signed package): the new files are staged in a private `.tmp-<id>` folder inside the model folder and verified; the current model is renamed to the rollback slot `.prev-<id>`; the staged folder is renamed to the model's name. The rollback copy of the version before that is moved aside first and deleted **only after** the new model is in place. If any step fails, the renames already done are undone, so the previous model and its rollback copy are still there; if even the undo fails, nothing is deleted and the message says to run `jarvis models rollback <id>`.
* **Guarantees:** each `os.replace` on the same volume is atomic on POSIX; the sequence of renames is **not** atomic (a crash or power loss between two renames can leave the model in `.prev-<id>`; `jarvis models rollback <id>` brings it back). On Windows, renames of folders can fail while an antivirus scanner or another program holds a file open; the code then undoes what it did, but this was not tested on Windows. A copy is not atomic on any system: the half-written copy lives only in the `.tmp-` folder and is removed on failure.
* **Disk space:** the old model is *renamed* to the rollback slot, not copied, so replacing needs free space for the incoming file only (plus the margin); the rollback copy keeps using the disk the old model already used. Right after a replacement both versions are on disk (about two models' worth); the version before them is deleted. The copy rechecks free space about every 256 MiB.
* **Nothing is deleted to make room.** Jarvis never deletes the current model or a rollback copy to free space. Only the version two back is deleted, and only after the new model is installed and its hash recorded.
* **Damaged packages:** a corrupt, encrypted, unsupported or oddly built `.jarvismodel` file ends in a one-line `error:` message; nothing is installed and the installed model and its rollback copy are untouched.
* **Finding rollback copies and leftovers:** `jarvis models list` shows `rollback_copies` (id and size) and `leftover_staging` (`.tmp-*`, `.part-*`, `.swap-*` folders left by a crash, with sizes). It only lists; it never deletes.
* **Removing a rollback copy safely (manual):** run `jarvis models verify <id>`; if it reports ok and the model works, the `.prev-<id>` folder inside the model folder is obsolete and can be deleted in the file manager. `jarvis models remove <id>` removes the model **and** its rollback copy. Do not delete a `.swap-*` or `.tmp-*` folder before checking that the model it belongs to is installed and verifies; after a crash a `.swap-*` folder can hold the only copy.

## What the code enforces and what you must do yourself
| Safeguard | Enforced by code? |
|---|---|
| Per-file download-size stop (catalog `fetch` / setup wizard: 115 % of the catalog estimate, or the host's declared size if smaller) | **Yes**, while streaming (Linux-tested, mocked network) |
| `add-gguf` size limit | **Only if you pass `--expected-size-bytes N`**; otherwise not enforced (the command says so) |
| Free-space check for one operation (fetch, `add-gguf` copy, signed package) incl. margin; 256 MiB rechecks during downloads and `add-gguf` copies (not during package unpacking, which uses a declared-size estimate) | **Yes**, per operation only |
| 15 % size check for a model downloaded in a browser | **Manual** unless you pass `--expected-size-bytes` with the page's exact size |
| Experiment-wide minimum 25 GB free before the experiment | **Manual in Jarvis itself.** Optional `scripts/verify_model_metadata.py --check-disk` and `scripts/resource_guard.py` check it once, at start (Linux-tested only) |
| Experiment-wide download budget of 20 GB | **Manual** (no running total is kept). `verify_model_metadata.py` only compares the *planned* sizes from the API with 20 GB |
| Peak RAM stop at 13 GB | **Manual in Jarvis itself**; enforced only for commands run through `scripts/resource_guard.py` (Linux-tested only) |
| Stop if swapping occurs | **Manual**; `resource_guard.py` adds a heuristic (low available RAM / page-file growth for 3 samples) for guarded commands only |
| Comparing the official SHA-256 and reading the licence on the publisher's page | **Manual** (code compares the file to the hash you type; a person must confirm that hash is the official one) |
