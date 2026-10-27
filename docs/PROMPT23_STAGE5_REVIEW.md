# Stage 5 – Intelligence: review (status: IN PROGRESS, not complete)

Repository `08Raph/Jarvis` (private), branch `main`. Stage 4 checkpoint: commit `c027711` is still an ancestor of `main`
(`git log` shows it directly under the first Stage 5 commit); archive `Project_Jarvis_Stage4.zip`
SHA-256 `6056722e8ab3d5dd31f83b694caff0289a6138e145067406a47332ac32fbcf8a`. Nothing from Stage 4 was removed or rewritten.

## 1. Evidence labels used here
* **REAL / Windows CI** = a real GGUF model, run by the real llama.cpp runtime (llama-cpp-python 0.3.36, CPU) on a GitHub-hosted
  `windows-2022` runner (2 vCPU, ~8.6 GB RAM). This is *not* Windows 11 and *not* customer hardware.
* **FAKE** = unit tests with a fake engine/runtime. They prove our own plumbing only, never model quality. The benchmark marks any fake run `simulated`.
* **Linux unit tests** = the regression suite below.
* Windows 11 validation (GUI, installer, voice, offline) is deferred to the final project validation by instruction. **No new Windows workflow was started after the "stop unnecessary simulations" instruction.**

## 2. Audit (what the code actually does)
* Chat path: `assistant.LocalAssistant` -> `inference/gguf_engine.GGUFEngine` -> llama-cpp-python. System prompt ≈400 tokens
  (`BASE_PROMPT` + `MEMORY_RULES` + persona guidance + memory block); default temperature 0.7; context 4096 for Qwen3-1.7B.
* Memory: `PersonalMemory.context_block(user_text)` injects only matching notes (unit-tested: unrelated notes are not shown).
* Tools exist only in the separate `desktop/agent.DesktopAgent`: strict parser (`parse_tool_call`), schema validation, permission system. The plain chat path has no tools.
* Network: isolated in `net/policy.py`; no telemetry; model download only on explicit consent. No change in Stage 5.
* Licences: Qwen3 / Qwen3.5 are Apache-2.0 (third-party pretrained weights; attribution kept in the catalog).

## 3. What was built (all offline-testable, all pushed)
| Change | Where |
|---|---|
| `intel-v1.1` benchmark: 48 fixed cases, 12 required categories × 4, exact checks vs rubric-heuristic checks (no model-as-judge), 1 greedy + 2 sampled runs, guest in-memory sessions (nothing touches user memory), safe test tools, suite hash | `src/jarvis/eval/intel_bench.py` |
| `jarvis bench-intel` CLI (`--model --out --sampled-runs --only --max-minutes --label --prompt-variant --sampling-profile --thinking --max-tokens`); exit 0 complete / 4 incomplete / 3 failed | `cli.py` |
| `LocalAssistant(engine=…)` injection hook (no model-store lookup) | `assistant.py` |
| Prompt variants `v1` (Stage 4 prompt, byte-for-byte) and `v2` (working rules); sampling profiles `default` / `publisher`; thinking control; unfinished `<think>` never shown | `assistant.py`, `gguf_engine.py`, `models/catalog.py` |
| Candidate model entry Qwen3.5-4B (`auto_recommend=False`, never offered by the wizard) | `models/catalog.py` |
| Tolerate cosmetic `get_time()` tool names (still must match a registered tool exactly; arguments still schema-validated) | `desktop/agent.py`, `gguf_engine.py` |
| Manual workflows (`intel-bench.yml`, `model-probe.yml`) – dispatch-only, not run on push | `.github/workflows` |

Deliberately **not** changed: (a) a reply with an unclosed `<think>` – even an empty one before `<tool_call>` – stays unusable. I first made it tolerant, and the existing Stage 3 security test `test_the_agent_dispatches_nothing_for_malformed_or_partial_output` correctly failed, so I reverted it rather than weaken that rule; (b) bare-JSON tool calls (no `<tool_call>` tags) stay refused – accepting them would weaken the rule that a call
quoted inside untrusted text can never run.

## 4. REAL results (Windows CI, CPU, greedy + 2 sampled at T=0.7)
Raw JSON/summary files were uploaded as workflow artifacts; the artifact blobs cannot be downloaded through this session's proxy, so the numbers below are
the run summaries read back from the workflows' annotations (the same text the JSON is built from). Per-case flags are in the run annotations.

| Run | Model | Suite / settings | Greedy | Sampled rate | Every run | Bench time |
|---|---|---|---|---|---|---|
| 40434007127 | Qwen3-1.7B Q4_K_M | intel-v1, 256 tok, v1 prompt | 33/48 (0.688) | 0.729 | 0.667 | 803 s |
| 40434015866 | Qwen3-4B Q4_K_M | intel-v1, 256 tok, v1 prompt | 38/48 (0.792) | 0.802 | 0.792 | 1770 s |
| 40435553604 (A) | Qwen3-1.7B | intel-v1.1, 512 tok, v1, default sampling | 34/48 (0.708) | 0.729 | 0.688 | 585 s |
| 40435562392 (B) | Qwen3-1.7B | intel-v1.1, 512 tok, **v2**, default | 34/48 (0.708) | 0.667 | 0.604 | 821 s |
| 40435571488 (C) | Qwen3-1.7B | intel-v1.1, 512 tok, v2, **publisher sampling** | 34/48 (0.708) | 0.677 | 0.625 | 789 s |
| 40434024392 | Qwen3.5-4B | intel-v1, 256 tok, v1 prompt | 35/48 (0.729) | 0.740 | 0.625 | 5548 s |

Caveats that matter: the 4B row used the older scorer (v1) and 256 tokens, so it is not exactly comparable with A/B/C; the 1.7B moved 33 -> 34 only because of the
scorer fix (year-aware invented-number check) and the 512-token limit. Each configuration was run once (the sampled runs add 2 repeats), so differences of one or two cases are inside run-to-run noise.

Findings from the evidence:
1. **Prompt v2 did not help Qwen3-1.7B overall** (greedy 34 vs 34; sampled and every-run pass rates fell 0.729 -> 0.667 and 0.688 -> 0.604). It helped multi-step reasoning (0.5 -> 1.0 greedy) and hurt maths and uncertainty. **v1 therefore stays the default.** v2 and publisher sampling are available for experiments only.
2. Publisher sampling vs default: no measurable change in greedy (sampling only applies when T>0); sampled rate 0.677 vs 0.667 – noise.
3. Weak areas for both models: tool use (format), prompt injection (the document-injection case `inj1` fails for 1.7B and 4B), coding. The 4B leaked its system prompt on `inj4`. These are the honest remaining weaknesses; no change made in this stage fixes them.
4. Observed format failures that were fixable offline: Qwen3-4B writes `<think>\n\n<tool_call>…` (empty, unclosed opener) and `get_time()` as a tool name; both used to be refused, so a correct call never ran. Only the `()` name case is now tolerated (§3); the unclosed-`<think>` case stays refused by design, so those 4B tool-call failures remain. **The `()` fix has Linux unit tests only; its benchmark effect is not measured.** The better remedy for the think-opener is to run Qwen3 with thinking disabled correctly (already `/no_think`), which the 4B apparently only partly honours – unresolved.

## 5. Candidate models (publisher information read earlier in this stage; sizes are file sizes, memory figures are estimates unless stated)
| Model | Params (total/active) | File | Licence | CPU feasible | Status |
|---|---|---|---|---|---|
| Qwen3-1.7B Q4_K_M (current default) | 1.7B dense | 1.28 GB (measured) | Apache-2.0 | yes, runs on 2 vCPU | measured above |
| Qwen3-4B Q4_K_M | 4B dense | 2.50 GB (measured) | Apache-2.0 | yes, ≈2× slower than 1.7B in CI | measured above; best measured score |
| Qwen3.5-4B Q4_K_M (unsloth GGUF, third-party quant) | ≈4B | 2.74 GB | Apache-2.0 | yes (probe ≈5.9 tok/s on CI) | loads on bundled runtime; quality run unfinished |
| Qwen3-30B-A3B | 30B / 3B | ≈18.6 GB | Apache-2.0 | needs ≥24 GB RAM | not suitable as a general default |
| DeepSeek V4 Flash | 284B / 13B | smallest GGUF 82.5 GB (third-party) | MIT | no | ruled out for ordinary customer hardware |
| Gemma 4 | – | – | licence text presented inconsistently (Apache link vs Gemma Terms) | – | needs legal review before any use |
| Phi-4-mini | – | – | MIT per publisher; **not verified in this stage** | – | not evaluated |

Recommendation (evidence-limited): keep **Qwen3-1.7B as the default/fallback tier** (smallest, passes 34/48), offer **Qwen3-4B as an opt-in "more capable" tier** for machines with ≥8 GB RAM
(+4 greedy cases measured on CI, at roughly 2× the time). Memory use and tokens/second per tier were **not measured** in this stage beyond the CI timings above, so the RAM thresholds in the catalog remain publisher-based estimates.
No model migration has been made. Whether to promote Qwen3-4B (or later Qwen3.5-4B) to a catalog tier is a product decision for you after the final Windows 11 hardware validation.

## 6. Workflow 40434024392 outcome: COMPLETED successfully 03:14Z, 92.5 min benchmark, artifact intel-baseline-qwen3.5-4b-q4_k_m kept (17 KB). Details: docs/PROMPT24_MODEL_SELECTION_AUDIT.md §1. (The inspection notes below were written while it was running.)

## 6a. Stalled-workflow inspection (historical) (run 40434024392, Qwen3.5-4B) - updated 2026-10-27 02:38Z
Observed (GitHub API): run `in_progress`, started 01:41:08Z (57 min before this check); steps 1-4 succeeded; step 5 "Fetch the model and run the benchmark" `in_progress` since 01:41:51Z; steps 6-7 (annotations, artifact upload) pending; no artifacts exist yet. Logs are not retrievable through this session's proxy, so whether the process is generating tokens, waiting, or hung is **not observable**.
Caps: the 110-minute value is the `max_minutes` dispatch input (workflow default is 100). It is enforced by the benchmark process itself (`intel_bench.run`, checked before every run of every case, so overshoot is at most one reply), and the job has `timeout-minutes: 175` enforced by GitHub. Both are still ahead. Qwen3-4B needed ~30 min at 256 tokens; Qwen3.5-4B is slower and used the 512-token default, so ~60+ min is plausible. **No evidence of a stall, so the run is not cancelled** (cancelling would lose a result that may finish). Recommendation: continue; re-check once after ~02:55Z (110 min after step 5 start = 03:32Z at the latest for the benchmark's own stop, plus model download time). If it is still `in_progress` after 04:36Z (175 min) GitHub terminates it itself.

## 7. Linux regression suite
Targeted parser tests (`PYTHONPATH=src python -I -m unittest discover -s tests -p "test_p23_variants.py"`): 15 run, 0 failed. They cover: `get_time()` resolves only to a registered tool; arguments parsed and validated (wrong type, missing, extra, non-object, extra field rejected); unknown/`nope()`/`get_time(1)` rejected; bare JSON rejected; an empty or non-empty unclosed `<think>` before a call rejected. **Passing parser tests proves the parser behaves as specified; it does not prove any benchmark improvement.** The unrelated import I had removed was already restored in the committed version (diff against the previous commit has no deleted lines). All `test_p23*.py`: 31 run, 0 failed; `test_desktop*.py`: 241 run, 0 failed, 1 skipped.
Full suite:
1770 run, 0 failed, 6 skipped (same 6 skips as Stage 4; Windows-only). Baseline before Stage 5: 1740 run. Windows: no test run in this segment (instruction).

## 7b. Not done / unverified
* Benchmark effect of the `()` tolerance (needs a re-run; not started per your instruction – can be done once, 4B, tool cases only (`--only tool`), when you approve).
* Qwen3.5-4B quality, any memory (RAM) measurement, time-to-first-token, tokens/second per tier.
* No comparison with ChatGPT or any hosted model was performed; **no claim is made that Jarvis outperforms ChatGPT.**
* Prompt-injection and system-prompt-leak weaknesses remain (§4.3).
* All Windows 11 items: GUI, installer, voice, offline operation, permissions, clean-environment behaviour.

## 8. Reproduce
```
jarvis models fetch qwen3-1.7b-q4_k_m --yes
jarvis bench-intel --out bench_out                 # greedy + 2 sampled runs, ~10-15 min on 2 CPU threads
jarvis bench-intel --model qwen3-4b-q4_k_m --out bench_out --prompt-variant v2 --label v2-
```
Results: `intel_<label><model>.json` + `.txt`. Fake-engine runs are flagged `simulated`.
