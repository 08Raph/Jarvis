# First real-model validation: Windows checklist

**Status: PREPARED. Nothing in this file has been run on a real model, on Windows, or with `llama-cpp-python` by the author.** The commands below were checked against the program's real
argument parser (a test parses every `python -m jarvis ...` line in this file); that proves the syntax exists, not that the run works on your PC.
Background and the full rubric: `STAGE7_REAL_MODEL_REVIEW_PROTOCOL.md`, `STAGE7_VALIDATION_RUNBOOK.md`. The four adult-only modes (Savage, Flirty, Sexy, Romantic) stay restricted and are **not** part of this run.

Four separate steps, never mixed: **A. install the runtime, B. select a model, C. run the audit, D. review the transcripts by hand.**
Use PowerShell in the repository folder (the folder that contains `pyproject.toml`). The PowerShell syntax here (`$E`, `$E\data`, `$LASTEXITCODE`) could not be executed by the author; the Python-side commands were. `$E` is a throw-away folder so none of your own data is involved.

## A. Python, the project, and the runtime (same interpreter for everything)
```
python --version                                                  # 3.11 to 3.13
python -c "import sys; print(sys.executable)"                    # note which Python this is; use only this one for every command below
pip install -e .                                                  # installs the PROJECT itself (from the repository folder). Without it every "python -m jarvis" below fails with "No module named jarvis"
python -m pip show llama-cpp-python                              # "Version: 0.3.36" is the pinned one; "not found" = the runtime is not installed yet
python -m jarvis runtime --require                               # must print "available": true, and exits 0
```
Instead of `pip install -e .` you can run from the repository folder with `$env:PYTHONPATH = "src"` set in that PowerShell window (this was the only way the commands were exercised on Linux).
`pip install` uses the network to fetch packages; it is the only step in this file that does, and it never fetches a model.
If `runtime --require` prints `available: false` or exits 3, **stop**. Install the runtime into THAT Python (Jarvis never installs it for you). `pip install -e ".[local]"` does NOT guarantee a prebuilt wheel: PyPI carries only a **source** archive of `llama-cpp-python`, so on Windows that command may try to compile it (which needs Visual Studio Build Tools and CMake). Instead use the wheel-only command, which must FAIL rather than silently compile when no compatible wheel exists:
`python -m pip install "llama-cpp-python==0.3.36" --only-binary=:all: --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu`
**It is UNVERIFIED that a compatible wheel exists for your Windows / Python / CPU combination, and this command has never been run on Windows by the author** (`STAGE5_MODEL_SOURCE_VERIFICATION.md` section 27.3: the wheel index and release assets could not be read). If pip reports "No matching distribution found" (or any error), **stop and decide before going further**; do not let pip build from source unnoticed. Building from source is possible only with Visual Studio Build Tools (C++ workload) and CMake installed; see "Windows 11 build" in `docs/LOCAL_MODEL.md`, and choose that route deliberately. Then run the check again. `pip show` succeeding in a different Python proves nothing: `python -m jarvis runtime` uses the interpreter you ran it with.
The check prints the error text if the import fails; it can contain a local path.

## B. A model that is already installed and selected
```
$E = "C:\jarvis-eval"; New-Item -ItemType Directory -Force $E\data, $E\out | Out-Null
python -m jarvis --data-dir $E\data models list                  # "installed" must contain your model and "active" must be its id
python -m jarvis --data-dir $E\data models verify <id>           # {"ok": true}
python -m jarvis --data-dir $E\data selftest                     # REAL load + generate test of the active model (needs step A)
```
`--data-dir` goes before the subcommand. `models list` prints ids, sizes, hashes and licence, not folder paths, and no secrets; it is safe to read, but still read it before pasting it anywhere.
If you only have a `.gguf` file and it is not installed yet, install your own copy (nothing is downloaded; your original file is copied, never moved or changed, and needs one extra full copy of free disk space). A licence id plus a licence file or URL is required:
`python -m jarvis --data-dir $E\data models add-gguf "<path-to-file>" --id <id> --license <SPDX id> --license-url <url> --sha256 <trusted hash>` (or `--accept-unverified` if you have no trusted hash).
Put the path in double quotes (`"C:\Models\my model.gguf"`); a path with spaces and no quotes is read as several arguments. Keep the data folder path free of spaces, or quote it everywhere.
Compatibility is not checked by the program ahead of time: use an instruction/chat-tuned GGUF that llama-cpp-python 0.3.36 can load (the project's tested default is Qwen3-1.7B Q4_K_M, never run here). `selftest` is the real compatibility test; an unsupported file fails there.
`models select <id>` changes the active model. The audit uses the ACTIVE model only. It never downloads, never switches models and has no cloud fallback.
Right after `selftest`, run `$LASTEXITCODE` (type exactly that and press Enter). `0` and the console line `SELFTEST PASSED` (printed on the error stream) are both required. Other codes: `2` no model selected; `3` integrity, runtime, load or generation failure, or an empty reply; `4` timeout, worker stopped (raise `--load-timeout` / `--generate-timeout`, default 600 / 180 s, range 5-7200, only if you expect a slow load); `5` the worker process ended abnormally (suspected native crash; see `worker_exit` and `worker_stderr_tail`); `6` the reply was not a valid answer; `130` cancelled with Ctrl+C. Do not continue on any non-zero code.
"Passed" means the model loaded in a separate supervised worker process and its reply to "What is 2+2?" passed an automatic check: at most 200 characters, the number 4 (`4`, `4.0` or "four"), no other number, no chat-template tokens and no `<think>` tags. Non-empty text alone fails with exit code 6. Still look at `answer`, `answer_check` and `failure` in the printed JSON. It is a limited smoke test: not proof of quality, reasoning, complete Windows compatibility, GUI/voice, or a PyInstaller build. The report `selftest.json` (schema 2) is written into `$E\data`: a `running` marker first, then atomic stage updates (`stage`, `stages`), ending in `status` `passed`, `failed`, `timeout`, `crashed` or `cancelled` with `exit_code`, `failure` {kind, stage, message}, `timeouts_seconds`, `runtime`, `load_seconds`, `generation_seconds`, `tokens_per_second`, `worker_exit` and `worker_stderr_tail` (the old `passed`, `model_id`, `model_sha256`, `time` and `answer` fields are kept). If a previous run was interrupted, the new report carries `previous_run`. The error text can contain a local path. No crash-type exit code has been observed with real llama.cpp or on Windows; that behaviour is untested.
**Stop here** if `selftest` fails to load the model, prints nothing, or exits non-zero (including 4, 5 or 6): fix that first. (The audit does not check the selftest result itself; if the model cannot be set up at all it exits with code 2 and writes no report, but do not rely on that.)

## C. The first, limited run
```
python -m jarvis --data-dir $E\data persona audit --styles normal calm --categories serious_sensitive serious_undetected_probe
```
Verified: this exact syntax is accepted. What it runs: styles `normal` and `calm` (both unrestricted); the two categories you named **plus** the always-added `defining` prompt = 6 replies, **plus** the control sweep
(5 controls x min/default/max x 2 styles = 30 more replies) = **36 replies in total**. A lighter variant that skips the sweep (6 replies) is the same command with `--no-controls` added; use it if you want the very smallest first run.
Do not run `persona suite` or an audit of all styles until you have reviewed this report. Press Ctrl-C to stop: completed transcripts are kept and the report is marked incomplete.

### Stop immediately, and keep the files, if
| You see | Meaning |
|---|---|
| exit code 2, "Cannot run the audit", no files | no model, damaged model or runtime missing: nothing ran, nothing was simulated |
| exit code 1, `INCOMPLETE` on the console | a case failed, was empty, aborted (3 failures in a row), or you interrupted it |
| exit code 3 | finished but the report could not be written; look for `*_PARTIAL.json` in `$E\data\eval` |
| replies that are empty, only a label, or garbage | counted as failed cases, never as answers; do not "fix" by rerunning silently |
Check the exit code right after the run: type `$LASTEXITCODE` and press Enter before running anything else (the next command overwrites it).

## D. Check the report before reading any reply
Files are in `$E\data\eval\` (`personality_audit_<time>.json` and `.md`). Open the `.json` and confirm, in this order:
1. `evidence_status` starts with **`REAL MODEL, COMPLETE`**. `UNVERIFIED`, `SIMULATED` or `INCOMPLETE` means: not evidence, stop. This label is a **check, not proof**: it only says a llama.cpp runtime was recorded and every requested case produced non-empty text. Also confirm yourself below.
2. `simulated_model` is `false`, `complete` is `true`, `missing_cases` is `[]`, `incomplete` is `[]`, `aborted` is `null`, `interrupted` is `false`, `restricted_not_evaluated` lists the four adult modes (expected), `human_reviewed` is `false`.
3. `model.model_id` is the id you selected; `model.file_name` / `file_bytes` match your file; `model.runtime.available` is `true` and has a version; `model.model_meta.sha256` equals the hash from `models verify`; `config` shows the temperature and max tokens you expect.
4. Cross-check against the machine, not only the file: you saw `selftest` load the model, generation took real time per reply (seconds, not instant), and the Python from step A is the one you used.
5. Skipped or not-run cases: the audit has none by design (`missing_cases` must be empty). The suite reports `not_run` cases separately; they are untested, not passed.
Only then read `serious_sensitive` (the safety override should answer in a plain, kind way with no style) and `serious_undetected_probe` for each style. The probe is **one phrasing and one sample**: it shows what each style does when the keyword detector did not fire. It is not a measure of detector accuracy, and two replies say nothing about personality quality overall.
Automated measurements (`automated_analysis`) and your own judgment (`human_scores`, filled in a **copy**) stay separate; do not edit the original files.

## E. Before you share anything
- Reports have file-system paths replaced with `<path>`, but that is pattern matching, not a guarantee. Search both files for your Windows user name, `C:\Users`, drive letters, the machine name, e-mail addresses and tokens/keys, and look at the model file name.
- The console prints full local paths ("Saved: ..."). Remove them from any pasted console text.
- Share copies; mark a hand-edited copy as edited. Never share the model file. The prompts are fixed synthetic text, so no private conversation should be in a report; if something personal ended up in a file, delete it and rerun.
- Record: commit hash (`git rev-parse HEAD`), the exact command, the exit code, the `evidence_status` line, and what you did not test.

## What this does NOT establish
Windows UI, voice, microphone, packaging, the four restricted modes, the full 13-style behaviour, the distress detector's accuracy, or personality quality. It is a first look at two styles on one model.
