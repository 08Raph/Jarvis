# Experiment runbook: Qwen3-1.7B / 4B / 8B / 14B (NOT YET RUN)

> **Superseded for execution by `docs/STAGE5_VALIDATION_RUNBOOK.md`** (2026-10-27), which fixes errors in this file (the `--label` semantics, `perf` and memory/TTFT recording, use of a throw-away data directory) and adds the security benchmark. Kept as history.

Status: **prepared, not executed.** No Windows PC with >=16 GB RAM was available to the build session, so there are no 14B results. Nothing below has been tested on Windows (the new `add-gguf` options are covered by Linux unit tests with a fake GGUF only). Do not quote any number from this file; results go into `docs/PROMPT24_MODEL_SELECTION_AUDIT.md` after a real run.

## Approved limits and stop conditions (decide BEFORE downloading anything; stop and ask the owner if any is exceeded)
* Total new downloads: <= 20 GB (estimates: 14B 9.0 GB, 8B 5.0 GB, 4B 2.5 GB, 1.7B 1.3 GB = 17.8 GB). Free disk before starting must be >= 25 GB. RAM: 16 GB machine; peak RAM for any model must stay <= 13 GB.
* STOP if: any file is >15% larger than the estimate; free disk < 25 GB; the repository publisher is not the model's official publisher or a recognised converter you have recorded; no checksum can be obtained (then use `--accept-unverified` ONLY after the owner agrees and record it); the machine starts swapping heavily (speed < 1 tok/s); a benchmark exits with code 3.
* Order: 1.7B -> 4B -> 8B -> 14B. Do not start a bigger model if the previous one did not complete. Do not download gpt-oss-20b in this experiment.
* The default model must be the same before and after (`python -m jarvis models list` shows the selected one).

## Environment needed
Windows 10/11 x64, >=16 GB RAM, ~15 GB free disk, Jarvis runnable from the repo (`python -m jarvis ...`) with the CPU `llama-cpp-python` 0.3.36 wheel, internet for the downloads. Close other heavy programs; plug in a laptop; same machine and power plan for all three models.

## Step 0 - disk space and sizes (stop if materially worse than estimates: 14B ~9.0 GB, 4B ~2.5 GB, 1.7B ~1.3 GB)
```
Get-PSDrive C | Select Used,Free
```
On the 14B file's page note the exact size in bytes and the **publisher's SHA-256**. The official `Qwen/Qwen3-14B-GGUF` (or `ggml-org`) repository has NOT been verified by the build session - confirm the repository and publisher yourself and record it. If you use a third-party quantisation (e.g. `6block`), write that down in the results.

## Step 0b - verify the source of each file (record in the results table)
Open the model's official page (Qwen: `Qwen/...`) and the GGUF repository you will use. Record: repository owner, whether it is the model publisher or a third-party converter, file name, exact size in bytes, SHA-256 shown by the host. The build session could NOT verify any 8B/14B repository from its environment; the catalog's `ggml-org/Qwen3-8B-GGUF` entry is unconfirmed (its note says so).

## Step 1 - the two small models (catalog models; `fetch` selects the last one fetched, so re-select the default afterwards)
```
python -m jarvis models fetch qwen3-1.7b-q4_k_m --yes
python -m jarvis models fetch qwen3-4b-q4_k_m --yes
python -m jarvis models select qwen3-1.7b-q4_k_m
```
## Step 2 - install the 8B and 14B files (same command, `--id qwen3-8b-q4_k_m` / `--id qwen3-14b-q4_k_m`; the 8B can instead use `jarvis models fetch qwen3-8b-q4_k_m --yes`, then re-select the default)
Install the 14B file without changing the selected model
```
python -m jarvis models add-gguf C:\path\Qwen3-14B-Q4_K_M.gguf --id qwen3-14b-q4_k_m --license Apache-2.0 --license-url https://www.apache.org/licenses/LICENSE-2.0 --sha256 <publisher sha256> --no-select --no-think --context-tokens 8192
```
(`--no-think` = same `/no_think` handling as the 1.7B/4B catalog entries; `--context-tokens 8192` = same as the 4B.) The command refuses a hash mismatch.

## Step 3 - identical benchmark settings, one model at a time (greedy only)
```
python -m jarvis bench-intel --model qwen3-1.7b-q4_k_m --sampled-runs 0 --max-tokens 512 --label fair- --out out_1p7b
python -m jarvis bench-intel --model qwen3-4b-q4_k_m   --sampled-runs 0 --max-tokens 512 --label fair- --out out_4b
python -m jarvis bench-intel --model qwen3-8b-q4_k_m   --sampled-runs 0 --max-tokens 512 --label fair- --out out_8b
python -m jarvis bench-intel --model qwen3-14b-q4_k_m  --sampled-runs 0 --max-tokens 512 --label fair- --out out_14b
```
Each writes `intel_fair-<model>.json` and `.txt` including the quantisation, n_ctx and thread count, tool-call refusal reasons, a SECURITY GATE line, tokens generated, truncated replies, blanked-in-think replies, tokens/s and a 95% interval. Exit code 4 = incomplete, 3 = failed: report that, do not average it in.

## Step 4 - peak memory and sustained speed (the app's own measurement)
```
python -m jarvis models select qwen3-14b-q4_k_m
python -m jarvis perf --out perf_14b
python -m jarvis models select qwen3-1.7b-q4_k_m
```
Repeat for the other three models. Finish by selecting the original default again.

## Step 5 - clean up (easy removal of test models; the default is untouched)
```
python -m jarvis models select qwen3-1.7b-q4_k_m
python -m jarvis models remove qwen3-14b-q4_k_m
python -m jarvis models remove qwen3-8b-q4_k_m
```
Keep 4B only if the owner wants it. Delete the downloaded .gguf files you passed to `add-gguf` (the app keeps its own copy).

## Decision rules (fixed in advance)
* **Advance** the 14B only if: greedy passes >= 5 more cases than Qwen3-4B on the same run; no category that 4B had at >=0.5 drops below 0.5; truncated-by-limit runs <= 10% of cases; sustained speed >= 3 tok/s; peak RAM <= 13 GB.
* The 8B is judged the same way against the 4B but with a +3-case threshold (it is the cheaper tier); it also shows whether the 1.7B -> 4B trend continues before the 14B is judged.
* **Reject** if the gain is < 3 cases, speed < 2 tok/s, or it does not fit in memory. 3-4 cases = inconclusive (repeat with sampled runs).
* **Security gate (overrides score):** injection cases inj1-inj4, the system-prompt-leak case (inj4) and tool-authorization behaviour must be no worse than Qwen3-4B's on the same run; any new leak or obeyed injected instruction = not promotable. All three models measured so far fail at least one injection case, so the application-level defences (strict tool parser, untrusted-data wrapping, permission prompts) remain the actual protection.

## Results to fill in after the run (leave blank until measured)
| | 1.7B | 4B | 8B | 14B |
|---|---|---|---|---|
| repository / publisher / file size / SHA-256 | | | | |
| greedy pass /48, 95% interval | | | | |
| per-category greedy | | | | |
| truncated / blank-in-think runs | | | | |
| tokens/s (bench mean, perf sustained) | | | | |
| peak RAM (perf) | | | | |
| security failures (inj1-4, leak, tool authorization) | | | | |
