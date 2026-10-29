# One consented real-inference run: exact plan (authorised once on 2026-10-29; see the ledger for whether it has run)

Prepared 2026-10-29. On 2026-10-29 the owner authorised **exactly one** run with the consent value below, after the minimal workflow edit in section 5 was applied and reviewed. No second run, retry or other model download is authorised. Whether the run has happened, and its result, is recorded only in `VERIFICATION_LEDGER.md`.

## 1. What the run must prove (and what it cannot)

| Claim | How the run proves it | Synthetic/mock outcome |
|---|---|---|
| A genuine model file was installed | `models fetch` (host metadata SHA-256) + `models verify`, then `ci_real_response_check.py`: `tensor_count >= 50`, size >= 500 MB and within 10 % of the catalogue (~1.28 GB), architecture present, name not "Synthetic", SHA-256 equal to the pinned value | The project's own fixture (1 tensor, 52 MB, "Synthetic 1B") fails four of these; a header-only file fails to load |
| The real runtime loaded it | `jarvis selftest` in the frozen exe: worker process loads `GGUFEngine` (llama-cpp-python 0.3.36), report needs `load_seconds > 0`, `output_tokens > 0`, all stages recorded, judged by `ci_check_selftest_report.py` | A fake runtime cannot exist inside the packaged exe; the unit-test fakes are not used by this step |
| The current application answers | `jarvis chat "What is the capital of France? Answer in one short sentence."` through `LocalAssistant -> GGUFEngine -> llama.cpp`; reply must be non-empty, contain "Paris", exit 0, with the `[local model: qwen3-1.7b-q4_k_m` banner and no traceback / "returned no text" | An empty, wrong, hung or crashing reply fails; a stand-in command is rejected |

**Not proven by this run (say so in any report):** answer quality beyond one question, speed on any other CPU, any GPU, Qwen3-8B, the GUI, voice, the personality system, tool use, offline behaviour of the current code, a person's PC. It is one reply to one prompt on a 2-vCPU hosted runner.

## 2. Facts to record when it runs

| Item | Value |
|---|---|
| Model | Qwen3 1.7B Q4_K_M, `ggml-org/Qwen3-1.7B-GGUF` (catalogue id `qwen3-1.7b-q4_k_m`), Apache-2.0, third-party pretrained |
| Download size | about 1.28 GB (**1,282,439,264 bytes**, confirmed from the publisher's file listing through the Hugging Face connector on 2026-10-29; the run pins this size) |
| SHA-256 | **observed** in runs 40422256861 and 40431278616: `d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5`. Not independently confirmed against the publisher (the publisher's hash could not be obtained from any trusted source). The run pins it as a consistency check only: a mismatch fails, a match proves this is the same file as before, not that the publisher issued it. Pass it as `--expect-sha256` to pin identity; a mismatch means "a different file", not necessarily "a bad one" |
| Runtime | llama-cpp-python **0.3.36**, CPU (AVX2), no GPU offload |
| Runner | GitHub-hosted `windows-2022`, 2 vCPU, ~8.6 GB RAM |
| Duration | about 11 min for the usual Windows job (run 40936176943: 11.3 min) plus download and selftest/chat; **estimate 15-25 min**. Earlier real-model runs took 12-16 min in total |
| Cost category | the repository is public (checked 2026-10-29), so hosted-runner minutes are free; if it is made private, Windows minutes bill at a multiplier. No paid service is involved. Bandwidth: one 1.28 GB download from huggingface.co, kept only on the runner |
| Storage | nothing is uploaded as an artifact by `windows-ci` |

## 3. The command (when you authorise it)

`windows-ci` runs the tip of the branch you give it, and `gh` cannot target a bare commit id. So: confirm `main` is still at the commit you want checked (`git ls-remote origin main`), then

    gh workflow run windows-ci.yml --repo 08Raph/Jarvis --ref main -f real_inference_consent=yes-download-qwen3-1.7b-q4_k_m

or in the browser: Actions > windows-ci > Run workflow > type exactly `yes-download-qwen3-1.7b-q4_k_m` in the consent box. Leaving the box empty downloads nothing. Do **not** dispatch `build-windows` for this: it has no consent gate (its GUI step always downloads the model) and it uploads an installer artifact.

## 4. Success and failure

* **PASS** needs all of: the usual jobs green; step `real` green; its summary says `PASS` for the selftest and (after section 5 is applied) for the real response check; the JSON in the log shows `tensor_count`, `model_bytes`, `model_sha256`, `reply_head`.
* **BLOCKED** (exit 3): download failed, no model file, runtime missing. Nothing about inference is learnt.
* **FAIL** (exit 1): the model or application was there and was wrong. Treat as a finding: keep the log, do not retry silently.
* Judge from the run's annotations and step summary, not the step colour (the step is `continue-on-error`).

## 5. The workflow edit (applied; the only change)

One block in the `real` step of `windows-ci.yml`, before `Remove-Item $D`. The gate, timeout, `continue-on-error` and permissions are unchanged. When the selftest passes, the application-level check runs and its verdict becomes the step's exit code; otherwise the check records "NOT EXECUTED" with the reason (so a skipped check is never read as a pass).

```diff
+          if ($verdict -eq 0) {
+            ...ci_real_response_check.py --data-dir $D --model-id $id --cmd $X --expect-bytes 1282439264 --expect-sha256 <pinned> --expect-runtime 0.3.36 --commit $env:GITHUB_SHA --annotate --summary $env:GITHUB_STEP_SUMMARY
+            $verdict = $LASTEXITCODE
+          } else {
+            ...ci_real_response_check.py --data-dir $D --model-id $id --annotate --summary $env:GITHUB_STEP_SUMMARY --not-executed "the selftest did not pass (verdict $verdict)"
+          }
```

Results are read from the run's check-run annotations (`Real selftest.json`, `Real response through the application: PASS|FAIL|BLOCKED|NOT EXECUTED`), because raw logs are not readable from the authoring environment. The checker rejects the project's synthetic fixture, a stand-in command and a wrong size, hash or runtime. Text checks of the edit are in `tests/test_p74_real_response_check.py` (class `TheWorkflowEdit`). The PowerShell itself can only be proven by running it; no PowerShell was available to the author, so the first execution is the real test.

## 6. After a PASS

Record run id, commit, model sha256, `reply_head`, timings in `docs/VERIFICATION_LEDGER.md`; it then proves the current code at that commit on a hosted CPU runner, which is still not a person's PC, a GPU, or Qwen3-8B. Qwen3-8B stays unmeasured and is not made a default by this run.
