# First real-model validation: Windows checklist

**Status: PREPARED. Nothing in this file has been run on a real model, on Windows, or with `llama-cpp-python` by the author.** The commands below were checked against the program's real
argument parser (a test parses every `python -m jarvis ...` line in this file); that proves the syntax exists, not that the run works on your PC.
Background and the full rubric: `STAGE7_REAL_MODEL_REVIEW_PROTOCOL.md`, `STAGE7_VALIDATION_RUNBOOK.md`. The four adult-only modes (Savage, Flirty, Sexy, Romantic) stay restricted and are **not** part of this run.

Four separate steps, never mixed: **A. install the runtime, B. select a model, C. run the audit, D. review the transcripts by hand.**
Use PowerShell in the repository folder. `$E` is a throw-away folder so none of your own data is involved.

## A. Python and the runtime (same interpreter for everything)
```
python --version                                                  # 3.11 to 3.13
python -c "import sys; print(sys.executable)"                    # note which Python this is; use only this one for every command below
python -m pip show llama-cpp-python                              # "Version: 0.3.36" is the pinned one; "not found" = step A is not done
python -m jarvis runtime --require                               # must print "available": true, and exits 0
```
If `runtime --require` prints `available: false` or exits 3, **stop**. Install the runtime into THAT Python (the project's own extra is `pip install -e ".[local]"`; Jarvis never installs it for you),
then run the check again. `pip show` succeeding in a different Python proves nothing: `python -m jarvis runtime` uses the interpreter you ran it with.
The check prints the error text if the import fails; it can contain a local path.

## B. A model that is already installed and selected
```
$E = "C:\jarvis-eval"; New-Item -ItemType Directory -Force $E\data, $E\out | Out-Null
python -m jarvis --data-dir $E\data models list                  # "installed" must contain your model and "active" must be its id
python -m jarvis --data-dir $E\data models verify <id>           # {"ok": true}
python -m jarvis --data-dir $E\data selftest                     # REAL load + generate test of the active model (needs step A)
```
`--data-dir` goes before the subcommand. `models list` prints ids, sizes, hashes and licence, not folder paths, and no secrets; it is safe to read, but still read it before pasting it anywhere.
If you only have a `.gguf` file and it is not installed yet, install your own copy (no download, licence text or URL required):
`python -m jarvis --data-dir $E\data models add-gguf <path-to-file> --id <id> --license <SPDX id> --license-url <url> --sha256 <trusted hash>` (or `--accept-unverified` if you have no trusted hash).
`models select <id>` changes the active model. The audit uses the ACTIVE model only. It never downloads, never switches models and has no cloud fallback.
**Stop here** if `selftest` fails to load the model, prints nothing, or exits non-zero: fix that first; an audit on a model that does not load is exit code 2 and writes no report.

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
Check the exit code right after the run: `$LASTEXITCODE`.

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
