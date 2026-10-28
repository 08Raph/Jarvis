# Stage 5 Windows session playbook (PREPARED 2026-10-27; NOTHING IN IT HAS BEEN RUN ON WINDOWS)

**Status of this file.** It orders, on one page, the steps a person runs on a real Windows 11 x64 PC (CPython 3.12). Every command below is *documented, not executed* by the author.
The helper scripts were tested on Linux with fake network, fake disk and synthetic wheels/PE files (`tests/test_p33_windows_tools.py`). That is **not** Windows evidence.
The 2026-10-27 pre-Windows audit (verification report §20) compared every command with the real `--help` output and the scripts; it did not run any of them on Windows.
Stage 5 stays **OPEN** until real-model comparisons, security benchmarks and this Windows session have actually been done and recorded.
Background and the detailed per-model procedure: `docs/STAGE5_VALIDATION_RUNBOOK.md` (sections 3-6); source/licence facts: `docs/STAGE5_MODEL_SOURCE_VERIFICATION.md`; storage and enforcement table: `docs/LOCAL_MODEL.md`.

**No PowerShell for the first steps:** the Jarvis window now has a **System & Model Readiness** page that runs the checks of preflight and Part A (OS, Python, disk, RAM, runtime import, and the model-metadata read) with one button; see `docs/IN_APP_PREFLIGHT.md`. It uses the same code as `verify_model_metadata.py`. Everything after Part A (downloading, hashing a real file, inspecting a wheel, benchmarks under the guard, the summary) still needs the commands below; none of that is in the EXE yet.

## 0. Limits and who enforces them (single table; the same numbers appear in the runbook and LOCAL_MODEL.md)
| Limit | Value | Enforced by code? | Where |
|---|---|---|---|
| Free disk before the experiment | >= 25 GB | **Checked** by `verify_model_metadata.py --check-disk` and `resource_guard.py --min-free-gb` (default 25) at *start*; nothing re-checks later except D-9 per operation. Otherwise manual (`Get-PSDrive C`). | scripts |
| Total new downloads | <= 20 GB | **Planned size** computed from API sizes by `verify_model_metadata.py` (OVER_BUDGET -> exit 4). **No running total is kept** while downloading; adding up what you actually fetched is manual. | script / manual |
| Per-file size stop | 115 % of the estimate/declared size | **Yes** in `models fetch` while streaming; in `add-gguf` only with `--expected-size-bytes`; `verify_downloaded_model.py` checks it for a file you already have. | code |
| Free space for one operation | operation's peak need + margin | **Yes**: `models fetch`, `add-gguf`, signed package (D-9) | code |
| Peak RAM | <= 13 GB (read as 13 x 10^9 bytes, summed resident set of the command and its children, sampled once a second) | **Only for commands started through `resource_guard.py`**, and only as well as a 1 s sample can see (a spike between samples is missed; a command that cannot be read is stopped as MEASUREMENT_FAILED). Anything else is manual (Task Manager > Details > Peak working set). | script |
| Sustained swapping / severe slowdown | stop | **Heuristic only** in the guard: available RAM < 1 GB, or page-file use up > 0.5 GB, for 3 samples in a row. psutil on Windows gives page-file *use*, not paging rates. Judging "severe slowdown" (e.g. sustained < 1 tok/s) stays manual. | script / manual |
| Wall time | `bench-intel` 150 min, `bench-security` 90 min | `--max-minutes` inside the benchmarks; `--max-seconds` in the guard as an outer bound | code |
| Hash/owner/licence being the *official* ones | - | **Manual**: a person confirms; code only compares bytes with what is typed or published | manual |

## 1. Preflight (PowerShell, from the repository root; record every output in the session notes)
Use a working folder whose path has **no spaces** (the commands below do not quote `$E`). No `.ps1` script is used, so the execution policy is not involved. Whether any step needs administrator rights is **unknown** - run as a normal user and record it if one fails.
```
cd <path to the Jarvis repository>               # the folder that contains pyproject.toml
$E = "C:\jarvis-eval"; New-Item -ItemType Directory -Force $E\data, $E\out, $E\meta, $E\dl, $E\wheels | Out-Null
Get-PSDrive C | Select Used,Free                 # STOP if Free < 25 GB (bytes)
git rev-parse HEAD; git status --short           # the commit under test; record any local change
python --version                                 # must be CPython 3.12.x
python -c "import struct,sys; print(struct.calcsize('P')*8, sys.version)"      # must start with 64
python -m venv $E\venv                           # one fresh venv for the whole session
$PY = "$E\venv\Scripts\python.exe"
& $PY -m pip install --upgrade pip
& $PY -m pip install -e ".[monitor]"             # Jarvis + psutil (needed by resource_guard.py). Do NOT add the [local] extra: it would make pip try to BUILD llama-cpp-python from source
& $PY -m pip list > $E\meta\pip_list_before_runtime.txt
$env:PYTHONPATH = "src"; & $PY -W ignore -m unittest discover -s tests; echo "exit $LASTEXITCODE"      # record Ran / OK / skipped / exit; Windows results may differ from the Linux 2041 / skipped=6
```
After **every** command in this playbook run `echo "exit $LASTEXITCODE"` and write the number down; the stop rules below depend on it.
Record also: power plan (`powercfg /getactivescheme`), plugged in or on battery, antivirus state, background programs, RAM size, CPU model. No script records these.
Never use the real data folder: every `jarvis` command below carries `--data-dir $E\data` **before** the subcommand.

## 2. Part A - model metadata (network, no weights)
```
& $PY scripts\verify_model_metadata.py --out $E\meta --check-disk C:\; echo "exit $LASTEXITCODE"
```
* Exit 0 = the host's own metadata is complete, matches the recorded values and the planned total is within 20 GB. **This is the host's *published* SHA-256, not a hash of bytes, and not independent of Hugging Face.**
* Exit 3 = API unreachable: nothing is claimed; rerun later or copy the saved `raw\` folder and `--from-raw` it. Exit 4 = a problem code is listed (owner changed, file renamed/missing/duplicated, size or hash missing/different, licence not Apache-2.0, gated, over budget, disk). **Do not proceed to downloading until a person has read every problem.**
* The Qwen3-8B catalog entry is known to be defective (`ggml-org/Qwen3-8B-GGUF` has no Q4_K_M). The script checks `Qwen/Qwen3-8B-GGUF` / `Qwen3-8B-Q4_K_M.gguf`. Using it needs the owner's decision; the catalog is **not** changed by this tooling.
* Also read, by hand, the model card and licence text for each repository and compare with `model_metadata.md`; record organisation status of the owner.

## 3. Part B - install one verified model into a throw-away store (ONLY after explicit owner authorisation to download)
1. Download the single file in the browser (or `curl.exe -L -o`) into `$E\dl\`. Add up bytes downloaded so far against 20 GB yourself.
2. `& $PY scripts\verify_downloaded_model.py --file $E\dl\<file>.gguf --metadata $E\meta\model_metadata.json --candidate <id> --human-sha256 <value read on the model page> --data-dir $E\data --out $E\meta` (`<id>` is the `id` column of `model_metadata.md`, for example `qwen3-8b-official`; `<value read on the model page>` is the SHA-256 shown on the file's Hugging Face page, copied by a person - omit the option if it was not read, and the record then says no human comparison was made)
   * Exit 0 (PASS): size equals the published size, within 115 %, SHA-256 **computed from the bytes** equals the published hash (and the typed one), GGUF header parses, room for the copy. It prints the exact `add-gguf` command with every value single-quoted for PowerShell, the model id set to the candidate id, and your `--data-dir`. PASS does not mean the model runs.
   * Exit 4: STOP. Delete the file, keep the JSON, do not "fix" the metadata to match.
3. Paste the printed command with `python` replaced by `& $PY` (it ends in `--sha256 <computed> --expected-size-bytes <size> --no-select`; `--no-select` leaves the default model alone). Then `& $PY -m jarvis --data-dir $E\data models verify <id>` (must say ok) and `& $PY -m jarvis --data-dir $E\data models list` (shows rollback copies and leftover staging folders).
4. Re-installing the same id keeps the previous copy as `.prev-<id>`; `& $PY -m jarvis --data-dir $E\data models rollback <id>` restores it. Test the rollback once and record whether it worked. A failed install must leave no `.part-`/`.tmp-`/`.swap-` folder; if `models list` shows leftovers, record it and delete them by hand.
5. The default model and any existing store are untouched because `--data-dir $E\data` is a separate folder; confirm by looking at the real data folder's `models\` timestamp **without opening anything in it**.

## 4. Part C - llama-cpp-python on Windows 11 / CPython 3.12 x64 (import is NOT model load)
Official sources only: PyPI (`https://pypi.org/pypi/llama-cpp-python/json`) and the project's own index (`https://abetlen.github.io/llama-cpp-python/whl/cpu`, from its README). The runtime is now pinned to `llama-cpp-python==0.3.36` (section 27 of the verification report). The project's own release workflow builds Windows wheels tagged **`py3-none-win_amd64`** (not `cp312`), so the expected file is `llama_cpp_python-0.3.36-py3-none-win_amd64.whl`; **whether that file exists on the CPU index is the first thing this part finds out** (D-8 stays BLOCKED until a real wheel is inspected and installed on Windows).
1. Find a wheel without installing:
   `& $PY -m pip download llama-cpp-python==0.3.36 --only-binary=:all: --no-deps --python-version 3.12 --implementation cp --abi cp312 --platform win_amd64 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu -d $E\wheels`
   If pip says "No matching distribution", **that is the result**: record it; the runtime stays unverified (D-8 BLOCKED). Do not fall back to building from source in this milestone. Record the exact file name pip printed.
2. Get the expected hash from the source, not from the file: open the index page `https://abetlen.github.io/llama-cpp-python/whl/cpu/llama-cpp-python/` (layout assumed from the project's `scripts/releases-to-pep-503.sh` and index workflow - **the page itself has not been seen by the author**) and read the `#sha256=` fragment of the link for your file name, e.g.
   `(Invoke-WebRequest -UseBasicParsing https://abetlen.github.io/llama-cpp-python/whl/cpu/llama-cpp-python/).Links | Where-Object { $_.href -match 'cp312.*win_amd64' } | Select-Object -ExpandProperty href`
   If the link has no `#sha256=`, there is no expected hash: run step 3 without the two `--expected-*` options and the record says "not a verification". Then:
   `& $PY scripts\verify_wheel.py --wheel $E\wheels\<wheel file name> --expected-sha256 <hex> --expected-sha256-source "<the link you read it from>" --out $E\meta; echo "exit $LASTEXITCODE"`
   `Get-FileHash $E\wheels\<wheel file name> -Algorithm SHA256` is an independent second computation of the same hash (PowerShell's own).
   It reports file-name tags, the hash computed from bytes (and whether it equals the typed one), METADATA (name, version, license, Requires-Python, Requires-Dist), licence files inside the wheel (names only - **read the licence text yourself**), each native DLL/PYD with size, SHA-256, CPU type and imported DLLs, and which Visual C++ runtime DLLs are needed. Exit 4 = problems. It never extracts, installs or imports, and it does not check that `Requires-Python` admits 3.12 (read that line).
3. Install exactly the inspected file (first step that changes the venv; it still pulls the wheel's dependencies from PyPI): `& $PY -m pip install $E\wheels\<wheel file name> 2>&1 | Tee-Object $E\meta\pip_install_runtime.txt; echo "exit $LASTEXITCODE"` then `& $PY -m pip list > $E\meta\pip_list_after_runtime.txt` and `& $PY -m pip show -f llama-cpp-python > $E\meta\pip_show_runtime.txt`. Record success/failure and the full output.
4. **Import check (does NOT prove a model loads):** `& $PY -m jarvis --data-dir $E\data runtime --require; echo "exit $LASTEXITCODE"` (non-zero = the import or a DLL load failed; the message names it). Record `available`, `version`, `cpu_avx2`, `gpu_offload_supported`. List the installed DLLs' imports: `& $PY scripts\check_vc_runtime.py $E\venv\Lib\site-packages\llama_cpp` (a "missing" Visual C++ runtime DLL there is a finding, not an error in the script).
5. **Model-load check (separate; needs the model installed in Part B):** `& $PY scripts\ci_model_probe.py $E\data <id>` - loads the GGUF, asks two short questions, prints facts (load seconds, architecture, chat template). Only a pass here supports "the runtime can load this model on this PC". Run it under the guard (Part D's pattern) if the model is 8B or larger.
6. Clean-machine check (Windows Sandbox or a second PC, `scripts\clean_install_test.ps1`; read that script first) repeats 3-5 without the Visual C++ runtime already installed.

## 5. Part D - benchmarks, always under the guard
Same settings for every candidate (taken from the runbook, section 3.2): context as configured per model (record it), `--max-tokens 512` (perf: 128), greedy decision run, `--sampled-runs 2` only for repeatability, one warm-up run for `perf`, three measured runs, same power plan, plugged in, nothing else running. Prompt template and questions are fixed by the suite hash printed in each JSON.
```
& $PY scripts\resource_guard.py --log $E\out\guard_intel_<id>.json --free-path C:\ --max-seconds 10800 -- $PY -m jarvis --data-dir $E\data bench-intel --model <id> --sampled-runs 0 --max-tokens 512 --max-minutes 150 --label intel-<id> --out $E\out
& $PY scripts\resource_guard.py --log $E\out\guard_sec_<id>.json  --free-path C:\ --max-seconds 7200  -- $PY -m jarvis --data-dir $E\data bench-security --model <id> --sampled-runs 2 --max-tokens 512 --max-minutes 90 --label sec-<id> --out $E\out
& $PY -m jarvis --data-dir $E\data models select <id>
& $PY scripts\resource_guard.py --log $E\out\guard_perf_<id>.json  --free-path C:\ --max-seconds 3600  -- $PY -m jarvis --data-dir $E\data perf --runs 3 --warmup 1 --max-tokens 128 --out $E\out\perf-<id>
& $PY scripts\summarize_stage5_results.py --in $E\out --in $E\meta --out $E\summary
```
Add `; echo "exit $LASTEXITCODE"` to each line. The guard needs no network. `--` separates the guard's options from the command (assumed to reach the guard unchanged in PowerShell 5.1 and 7 - **unverified on Windows**; if the guard prints its usage line and exits 8, put the whole command in a `cmd /c` call and tell the owner).
Guard exit codes: the benchmark's own (0 complete, 3 failed, 4 subset/incomplete/invalid) when it finished by itself; **7 = the guard stopped it; 8 = it refused to start** (a command that itself exits 7 or 8 looks the same: open the guard's JSON log - `started`, `stop_reason`, `command_exit_code`, `finished` - and note a log with `finished: false` means the guard itself died). A stopped run is a result ("did not fit / unsafe"), not something to retry with other settings without the owner.
**Four result families, never merged:** (A) functional correctness - pass counts with Wilson interval, per category, exact vs rubric checks; (B) language quality - **not measured** by anything here (rubric checks are keyword heuristics; judge by hand and say so); (C) tool safety - invalid/wrong tool requests, and the `bench-security` raw/app gates; (D) performance - load time, time to first token, sustained and overall tokens/s, end-to-end latency per case, peak memory. The summary has no overall score, and simulated, incomplete, subset, failed or invalid runs are listed under EXCLUDED. Differences in suite hash, token limit, sampling, thinking mode, OS/CPU/threads or runtime version make runs "not comparable".

## 6. Stop conditions (stop, record, tell the owner; do not continue to the next step)
| Condition | Stop rule | Enforced by |
|---|---|---|
| Free disk < 25 GB at start | do not start | `verify_model_metadata --check-disk`, `resource_guard` (start only); else manual |
| Planned or actual downloads > 20 GB | do not download more | plan: script; actual running total: **manual** |
| Any file > 15 % above its expected size, size or SHA-256 mismatch | delete the file, do not install | `models fetch` stream stop; `verify_downloaded_model`; `add-gguf --expected-size-bytes` |
| Owner/repo/file name/licence differs from the record, gated or private repo, missing hash or size | do not download | `verify_model_metadata` (exit 4); the licence *text* is manual |
| Peak process-tree RAM > 13 GB | kill the run | `resource_guard` (only guarded commands) |
| Sustained low available RAM or page-file growth, or sustained < 1 tok/s | kill the run | guard heuristic; the tok/s part is manual |
| Runtime crash, DLL load error, or two timeouts in a row | stop that model; record; no retry loops | manual (guard exit 7/8 or benchmark exit 3) |
| `add-gguf`/`fetch` fails, leaves staging folders, or `models rollback` does not restore the previous model | stop all installs; record `models list` output | manual inspection |
| A tool tries to contact any host other than Hugging Face during a benchmark | stop | manual (firewall log) |
| Any command exits 3 or 4 | report it; never average an incomplete run in | manual |

## 7. After the session
Collect `$E\meta`, `$E\out`, `$E\summary`, session notes (power plan, plugged in, background load, antivirus), `jarvis diagnostics`. Delete `$E\dl` and `$E\wheels` only after the hashes and JSON are saved. Statements about Windows may be made **only** from these recorded outputs.

## 8. Known limits of this tooling (from the pre-Windows audit; none is fixed by a claim)
* The scripts trust local input files: `model_metadata.json` is trusted as the record of what the host published, and a person can edit it. The tools prove consistency with that record, not that the record is the official one.
* The "published SHA-256" and sizes come from one host. No independent second source exists yet; `Get-FileHash` and a second reading of the model page are the only cross-checks and are manual.
* `resource_guard.py` cannot make a hard kill clean (see its header): after a stop, inspect `models list` and the output folder. It cannot see a memory spike shorter than its sampling interval. It does not watch disk use or tokens/s.
* The Windows behaviour of every script - `psutil` readings, `TerminateProcess` of the process tree, `--` passing, path handling, the Hugging Face API from your network - is **unobserved**.
