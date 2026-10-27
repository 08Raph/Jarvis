# Prompt 22 — Stage 4: real wizard click-through and multi-token performance

Repository `08Raph/Jarvis` (private), branch `main`. Stage 3 history is unchanged in `docs/PROMPT21_REVIEW.md` (commit `852f082`, archive SHA-256 `50b1fa3c…def9`).
Stage 4 commits: `732838a` (perf command, GUI test, dialog fix), `9be5918`, `9f365e2`, `7ecfb6b` (test-script fixes found by the runs), `bee5873` (workflow reporting). The docs-only commit that adds this file follows `bee5873`.

## Evidence runs (GitHub Actions, windows-2022, no GPU, 2 vCPU, 8.6 GB RAM)
| Run | Commit | Mode | Result |
|---|---|---|---|
| 40425740040 | 732838a | full (build, Windows unit tests, Stage 3 steps, Stage 4) | Stage 4 steps ran; GUI script FAILED 5 of 7 scenarios (script bugs, see below). The step showed "success" only because it is `continue-on-error`; the final gate turned the run red. |
| 40428959295, 40430038751 | 9be5918, 9f365e2 | diagnostic (installer reused from 40425740040) | progressively fewer failures; S1 and S6 were script bugs |
| 40430665918 | 7ecfb6b | diagnostic | **all scenarios S0–S6 PASS**, perf complete |
| **40431278616** | **bee5873** | **full** | **success: build, Windows unit tests (1740 run, 0 failed, 87 skipped), Stage 3 steps, GUI scenarios S0–S6 PASS, perf COMPLETE** — https://github.com/08Raph/Jarvis/actions/runs/40431278616 |

## 4A — Genuine GUI interaction: YES (on a CI runner desktop)
Method (`scripts/ci_gui_wizard_test.ps1`): Microsoft UI Automation finds the controls of the installed `jarvis-gui.exe`; every button press is a **real mouse click** (`SetCursorPos` + `mouse_event` at the control's on-screen centre); text is entered with **real key events** (`SendKeys`); screenshots are taken of the live desktop. No internal function is called and `setup-auto` is not used. Limit: this is a GitHub-hosted runner (Administrator, Windows Server 2022), not a customer PC or Windows 11. This session cannot view screenshot images; it read UI Automation text and tree dumps, the file sizes and SHA-256 of the 15 PNGs, which are in the `ProjectJarvis-ci-logs` artifact (`ci-logs/gui/`) for anyone to open.

| Scenario | Result | What it proved |
|---|---|---|
| S1 details, consent, Set up later | PASS | Wizard shows "download about 1.28 GB", licence Apache-2.0 with URL, "Estimated memory needed 2.96 GB", "pretrained third-party model". With the wizard open for 10 s: no file > 1 MB and no outbound TCP connection. Clicking "Set up later": no model, setup not complete; chat says "No chat model is installed" and does not pretend to answer. |
| S2 Cancel | PASS | Not marked complete, no files, app closes cleanly. |
| S3 failure / retry / cancel / close | PASS | Network blocked by a firewall rule: wizard reports "Setup did not finish (download)… failed after 3 tries (WinError 10013)", no "Setup finished", no Launch button, nothing written. After unblocking, Retry downloads; progress shown in the UI; Cancel mid-download removes the partial file; Retry again; closing the window (X) mid-download cancels the download, leaves no model/partial file, and nothing keeps downloading 20 s later. |
| S4 full setup then a question | PASS | Real click on "Download 1.28 GB and set up": UI progress with ≥6 distinct increasing MB values; integrity (full SHA-256), runtime, load, generation, "the app's own engine answers" all `[ok]`; `setup_state` completed/real/cpu, `gpu_verified=false`; "Launch Jarvis" → main window; typed question answered "The capital of France is Paris." |
| S5 reopen | PASS | No wizard on reopen; answers "capital of Italy" → "Rome". |
| S6 damaged model | PASS | One byte of the installed model changed. Wizard offers "Check and finish setup"; with the network blocked it fails, never reports success, no Launch. After unblocking, Retry re-fetches the model; its SHA-256 equals the pinned publisher hash `d2387ca2…bc7b5`; setup then completes. |

Defect found and fixed (genuine): closing the wizard (X/Esc) mid-download previously closed the dialog while the worker kept downloading and could later activate the model invisibly. `SetupDialog.reject` now cancels the worker first (verified by S3; static regression test added).
Script bugs found by the runs (not product defects): dialogs owned by the main window are not children of the desktop in UI Automation; a substring on an 11-character string; hashing a backup copy (`_prev`) of the damaged model instead of the active file; the shutdown helper now answers a "Close Jarvis?" prompt.
Not covered by an end-to-end test: a download that completes but fails the publisher checksum (the publisher's file cannot be altered); that case is covered by existing unit tests with fake hosts only.

## 4B — Multi-token performance (`jarvis perf`)
Command (installed app): `jarvis.exe perf --out <folder> --runs 3 --warmup 1 --max-tokens 128`. Workflow step "Stage 4B"; output `ci-logs/perf/perf.json` and `perf_summary.txt`. Prompt "Explain how the water cycle works in about 150 words." (25 content tokens, 38 with chat template), temperature 0, context reset before every run; load time, warm-up, TTFT, sustained rate ((n−1)/(last−first token)), overall rate and peak working set are recorded separately; model identity/size/SHA-256/quantisation, runtime version, OS, CPU, threads, mode are in the JSON.
Final run 40431278616 (real Qwen3 1.7B Q4_K_M, CPU, 1 thread used by the app): load 1.475 s; TTFT median 1.15 s; 121 generated tokens per run; **sustained 11.15 tokens/s** (11.12–11.19), overall 10.08 tokens/s; peak process memory 2,386.9 MB; warm-up (32 tokens, 4.0 s) excluded.
Across the three runs that completed, the runner hardware differed and so did speed: sustained 8.70, 20.31 and 11.15 tokens/s. **A single CI number is not a benchmark of customer PCs**; compare only with the recorded CPU. Stage 3's 1-token "0.6–1 tok/s" self-test figure is not a speed.

## 4C — Tests (kept distinct)
- Linux unit tests (local): 1740 run, 0 failed, 6 skipped (sandbox facts: no speech engine / GPU listing / llama runtime, etc.). Stage 4 added 15 tests (1725 → 1740) (perf arithmetic with a fake runtime, GUI-wiring static checks); none skipped.
- Windows unit tests (run 40431278616): 1740 run, 0 failed, 87 skipped — same skip set as Stage 3 (POSIX-only assertions, Linux-sandbox facts, elevated-process desktop-control tests, symlink privilege); no new skips.
- Fake-provider tests: inside both suites; the perf unit tests use a fake runtime and are marked simulated.
- Real inference, real GUI interaction, offline (firewall) validation and performance: only the Windows workflow steps above. Stage 3 steps (installer, setup-auto, real prompt, firewall-blocked offline, uninstall) still pass in the same run.

## Costs
GitHub-hosted Windows minutes only (about 6 workflow runs; the diagnostic runs skip the build). No paid service; the model is downloaded from its publisher a few times per run (≈1.28 GB each). Repository remains private.

## Remaining limitations / not tested
- Windows 11, clean customer PC, SmartScreen/code signing, upgrade-over-install: NOT TESTED.
- GPU: NOT TESTED (runner has none; packaged runtime is the CPU build).
- Voice: NOT TESTED (no audio generated or evaluated).
- Screenshot contents were not visually inspected by this session.
- The runner runs as Administrator; some real desktop-control actions cannot be exercised there.
- Performance varies by runner CPU; no cross-machine reproducibility is claimed.

## Next milestone
Manual click-through on a clean Windows 11 PC (install, wizard, download, question, reopen) using this script's checklist, plus a signed-installer/SmartScreen check; then the GPU path on real hardware.
