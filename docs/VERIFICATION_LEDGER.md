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

## 5. The Windows `WinError 32` cleanup flake: run history

`PermissionError [WinError 32]` on `memory.sqlite` while deleting a test's temp folder: **3 of 18** `windows-ci` runs (40795644979, 40918820914, 40922037937). The other six failed runs failed for different, since-fixed reasons (HUD Qt tests under development, a docs test). Two runs have completed since the voice-reply shutdown fix (3f36530): 40925532946 (no WinError 32; red only for a docs test) and 40928361454 (green). A product-level ordering defect was found and fixed; that it causes the Windows error is **not proven**. A 17% base rate means two clean runs say almost nothing (two clean runs happen about 69% of the time with no fix at all). Append every later `windows-ci` result here; call it resolved only after a long clean streak (at least 15 consecutive Windows runs, which would be unlikely, about 7%, by chance at the old rate).
