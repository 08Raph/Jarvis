# Verification ledger: what has actually been run, where, and at which commit

Written 2026-10-29 at commit c508666 after re-reading the CI record. **It corrects earlier statements in this repository and in earlier source-package READMEs that said real inference "has never been run".** That sentence was true of the `windows-ci` workflow, of the unit-test suite and of the authoring sandbox. It was not true of the project as a whole. Everything below is read from GitHub Actions run annotations (cited by run id); nothing is recalled from memory.

## 1. Real-model evidence that exists (hosted Windows runner, CPU only)

| Evidence | Run (workflow, date, commit) | What it shows | What it does NOT show |
|---|---|---|---|
| Packaged app, real model, scripted install | `build-windows` 40422256861 (2026-10-26, 82ffb6e) and 40431278616 (2026-10-27, bee5873) | Silent install of the built installer; `jarvis.exe` runs without Python; llama-cpp-python 0.3.36 loads; real Qwen3-1.7B Q4_K_M (1,282,439,264 bytes, SHA-256 `d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5`) downloaded and verified; `selftest` passed in both runs (load 1.3-1.6 s; answer "4"); in 40431278616 the real prompt "What is the capital of France?" was answered "The capital of France is Paris." both online and with the app's network blocked by a firewall rule | One-token selftest answer is a weak check; not the current code; not a person's PC |
| Real desktop window, driven by real key and click events | `build-windows` 40431278616, scenarios S0-S6 | Setup wizard shown on first start; consent click; real 1,282 MB download with progress; integrity check; load on CPU in 1.6 s; 25 tokens at 7.24 tok/s; main-window chat reply "The capital of France is Paris."; after reopening no wizard and "The capital of Italy is Rome."; cancel and close mid-download leave no partial file; a model with one byte changed is never accepted and is re-fetched | Looks, DPI scaling, dark mode, accessibility, a real desktop session (the runner is a CI server) |
| Speed and memory | same run, `perf` | 3 valid runs, 121 tokens each: sustained median 11.15 tok/s (min 11.12, max 11.19), time to first token 1.15 s, peak process memory 2,387 MB, 2 vCPU, no GPU | Any GPU (offload unsupported in this wheel), any other CPU |
| Intelligence benchmark `intel-v1` (48 cases) | `intel-bench` 40434024392, 40435571488 and others (2026-10-27) | Qwen3-1.7B greedy 34/48 = 0.708 (v1.1, 512 tokens, prompt v2); Qwen3-4B 38/48 = 0.792; Qwen3.5-4B 35/48 = 0.729 (details and caveats: `PROMPT24_MODEL_SELECTION_AUDIT.md`) | Qwen3-8B, the model chosen as the long-term target, was never measured. No model reached the 8/10 goal; scores are heuristic and small-sample |

## 2. What changed after that evidence

The runs above are on commits up to 06d196f (2026-10-27). HEAD is c508666: **68 commits later, 67 source files changed (about +10,700 lines)**, including `app/`, `ui/`, `voice/`, `personality/`, `models/` and `brains/`. Stage 5 continuation, Stage 6 voice, Kokoro, Stage 7 personality, the HUD and the cleanup fixes all came after. **The current code has not been run with a real model.** Re-running needs the ~1.28 GB download, which requires the typed consent value on `windows-ci` (or the `build-windows` workflow) and spends Actions minutes; it has not been authorised.

## 3. What the current commit's CI proves (run 40928361454, c508666)

Linux fast lane and the Windows unit suite pass; the PyInstaller exe and Inno Setup installer build; the frozen-worker gate passes against the real `jarvis.exe` including the runtime refusing a header-only synthetic GGUF; a silent install works and `llama-cpp-python 0.3.36` imports in the installed app. No model is loaded by that workflow unless the consent value is typed.

## 4. Still unverified anywhere

A person's own Windows 11 PC; any GPU; Qwen3-8B; a physical microphone, speakers, a Vosk model or Kokoro audio; the personality system, voice stack, HUD and Import Brain with a real model; real desktop-control actions (clicking other applications, UI Automation, the emergency stop); a clean consumer machine with SmartScreen and antivirus; upgrade installs; the Windows cleanup flake (below).

## 5. The Windows `WinError 32` cleanup flake: run history (updated 2026-10-29 after run 40936176943)

`PermissionError [WinError 32]` on `memory.sqlite` while deleting a test's temp folder: **3 of 18** `windows-ci` runs up to the voice-reply fix: 40795644979 (00:41 UTC), 40918820914 (17:10), 40922037937 (17:30), all on 2026-10-29. The other failed runs failed for different, since-fixed reasons (HUD Qt tests under development, a docs test), checked run by run in their annotations.

| Run | Commit | Created (UTC) | Result | `WinError 32` | Whole run |
|---|---|---|---|---|---|
| 40922037937 | 76eb1cc | 17:30 | failure (original P17 test) | **yes** | 12.0 min |
| 40925532946 | 3f36530 (voice-reply fix) | 17:52 | failure (docs test only) | no | 11.6 min |
| 40928361454 | c508666 | 18:10 | success | no | 12.0 min |
| 40936176943 | 60c8f61 | 19:00 | success | no | 11.3 min |
| 40947992903 | a260143 (docs and agent files) | ~20:29 | no `WinError 32` (result per annotations at the time) | no | - |

**Since the fix: 3 consecutive runs without `WinError 32`, over an observation window of about 1 h 20 min (17:52 to 19:12 UTC).** That is not evidence of a cure: between the first and second events there were **11 consecutive runs without it, about 16 hours, with no relevant fix in place** (a 17 % per-run rate gives a streak that long about 13 % of the time). The streak also does not cover a slow-runner period, which is when the failures happened (runs of 331 to 661 s against 270 to 350 s normally).

Revised bar for calling it resolved: **at least 20 consecutive Windows runs without the error (chance of that at the old rate: about 3 %), spread over several days and including at least some slow runs.** Until then the status is "not recurred in N runs", never "fixed". The product-level ordering defect (shutdown reporting nothing running and closing the database under a live voice reply) is real and fixed; that it causes the Windows error is not proven. Append each later `windows-ci` result to the table above.

**Status 2026-10-29 (after runs 40954058955 and 40955846622, both without `WinError 32`): 6 consecutive runs since the fix (40925532946, 40928361454, 40936176943, 40947992903, 40954058955, 40955846622), window about 3.5 h (17:52 to 21:25 UTC). Earlier count, kept for history: 4 consecutive runs without `WinError 32` since the fix (40925532946, 40928361454, 40936176943, 40947992903), window about 2.5 h (17:52 to 20:29 UTC). Threshold (20 runs, several days, some slow runs) NOT reached; status remains unresolved.**

## 6. The one authorised real-model run (2026-10-29): RESULT

`windows-ci` run **40955846622** (workflow_dispatch, commit **665e400883c402bf72769088c3a12886d3f2f459**, consent value typed once). Jobs: Linux unit tests **success** (123735632828); Windows build, frozen worker gate, install check **success** (123735633421). Read from the job's annotations, not from step colour.

The real-response checker **genuinely executed** and reported **PASS**: genuine GGUF (311 tensors, 28 blocks, qwen3, Q4_K_M, 1,282,439,264 bytes, matching the pinned size), SHA-256 `d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5` (equal to the pin), runtime llama-cpp-python 0.3.36 in the packaged exe (CPU, AVX2, no GPU offload), and `jarvis chat` answered "The capital of France is Paris." in 12.31 s. The selftest in the same step passed (load 1.57 s, answer "4").

**What this proves:** the current code at 665e400, packaged and installed on a hosted Windows runner, loads a genuine Qwen3-1.7B Q4_K_M with the pinned runtime and answers one question correctly through the application.
**What it does not prove:** the SHA-256 is still only an observed pin (not publisher-confirmed); one reply to one prompt on a 2-vCPU CPU runner; no quality, speed (the selftest's 1.15 tok/s is a one-token figure, not throughput), GPU, Qwen3-8B, GUI, voice, personality, tools, offline behaviour of this commit, or a person's PC. No retry or second download was made.
