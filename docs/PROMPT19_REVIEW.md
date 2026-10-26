# Prompt 19 review - honest acceptance report

**Verdict: PARTIAL. No category reached 8/10. Real local-model inference, GPU use, GUI, voice, Windows operation, installer and offline behaviour were NOT verified:
this sandbox is Linux with no GPU, no PySide6, no llama-cpp-python, no model weights, no microphone, and cannot reach huggingface.co or PyPI (curl returned code 000;
the fetch tool refused the model-metadata URLs). The blocked downloads were not retried.**

## What was done (real work, all in this archive)
1. Checkpoint: `project-jarvis-prompt17.zip` SHA-256 `1997ff16...3907e` matched; extracted clean; baseline suite run before any change: **1591 tests, OK, 6 skipped (150 s)**.
2. **Memory retrieval measured, then improved** (`eval/memory_eval.py`, `memory/retrieval.py`). 40 memories, 48 fixed queries (37 answerable, 11 unanswerable), split by position into dev (odd) and test (even), written before the change.
3. **Hardware-adaptive selection** (`models/selector.py`, `jarvis models plan`): tiers, three separate GPU states, memory estimates, no silent model substitution, honest compatibility records and matrix.
4. **GPU plumbing** (`GGUFEngine(n_gpu_layers=...)`, `runtime_info()["gpu_offload_supported"]`): tested with a fake factory only.
5. **Catalog** grew from 1 to 3 models (Qwen3 1.7B/4B/8B). Only the 1.7B entry's metadata was read from the publisher earlier; the 4B/8B entries are flagged `from-publisher-knowledge-NOT-fetched`.
6. **Staged real-model validation harness** (`eval/realmodel.py`): seven separate states; a run with an injected factory is flagged simulated and can never report real validation.
7. **Fifteen-area benchmark** (`eval/suite19.py`): 11 model areas (35 fixed tasks with keys) + 4 component areas. Without a real model the model areas report `not_measured`; the oracle only proves the scorers.
8. **Document handling tested with real generated files** (txt/csv/md/html/docx/pdf) and refusals (corrupt, bomb, entity docx, xlsx/pptx/doc, image-only PDF).
9. Licensing audit (`docs/LICENSING_AUDIT_P19.md`), hardware doc (`docs/HARDWARE_COMPATIBILITY.md`), negative controls (`docs/PROMPT19_NEGATIVE_CONTROLS.json`).

## Measured results (real, on this PC, no language model involved)
Memory retrieval, `PersonalMemory.search`, default threshold 0.25:

| split | | hit@1 | hit@3 | MRR | false-positive rate (unanswerable) |
|---|---|---|---|---|---|
| all 48 | Prompt 17 code | 0.703 | 0.838 | 0.761 | 0.545 |
| all 48 | after | 0.838 | 0.973 | 0.896 | 0.545 |
| dev (24, tuned on) | before -> after | 0.684 -> 0.842 | 0.842 -> 0.947 | 0.763 -> 0.895 | 0.400 -> 0.800 |
| test (24, held out) | before -> after | 0.722 -> 0.833 | 0.833 -> 1.000 | 0.759 -> 0.898 | 0.667 -> 0.333 |

Honest reading:
* The improvement is real on the held-out half too, but the dataset is small, synthetic and written by the author; do not read 0.97 as a real-world rate.
* The near-synonym table (`_GROUPS`) was written knowing the kinds of questions in the set (boss/manager, wake/get up, ...). That makes the `semantic` gains **optimistic**.
  The one `semantic` query still missed ("What time do I get up?") shows the limit: this is a word-matching retriever, not a meaning-matching one.
* **False positives are not fixed**: unanswerable questions that share a word with a memory still return a memory (overall 0.545, unchanged). A stricter threshold removes
  them but costs recall (tested: min_score 0.45 -> test false positives 0.0, hit@1 unchanged 0.833, hit@3 0.944; dev hit@1 falls 0.842 -> 0.684). The default stays 0.25.
* Per tag (all 48, after): lex hit@1 0.72 / hit@3 1.0; morph 1.0; typo 1.0 (n=2); semantic 0.875 (n=8); conflict 1.0 (n=2); superseded values returned: 0.
* Not measured: end-to-end recall inside a conversation with a real model; embedding-based retrieval (no embedder bundled).

## Not measured (and why)
Real inference, tokens/s, time to first token, load time, peak RAM/VRAM, GPU speedup, tool-call reliability of Qwen, any of the 11 model benchmark areas, wake-word accuracy,
latency of voice, GUI rendering, Windows desktop control, installer, offline behaviour with the network disconnected. All need the target Windows PC. Steps: `docs/HARDWARE_COMPATIBILITY.md`
and `docs/WINDOWS_TEST_CHECKLIST.md`.

## Tests
Baseline 1591 OK (6 skipped). Final: **1640 tests, OK, 6 skipped (147 s)**. New in `tests/test_p19.py`: 49 (plus 0 changed existing tests) (memory eval, engine plumbing, selector, harness, benchmark, documents, CLI).
Negative controls (apply a deliberate bug to a disposable copy, expect a failing test): **21 of 21 deliberate bugs detected** (2 gaps found on the first pass, N15 zip-bomb and N19 acceleration message, were closed with stronger assertions and re-run; results in `docs/PROMPT19_NEGATIVE_CONTROLS.json`).

## Scorecard (0-10; none verified at 8; "confidence" is in the score, not in the code)
Scoring standard: 7 = strong with important weaknesses; 8 = validated against meaningful acceptance criteria. An untested implementation cannot receive a verified 8.

| # | Category | Score | Conf. | Verified | Simulated / unverified | Biggest work to reach 8 | Test that would demonstrate it |
|---|---|---|---|---|---|---|---|
| 1 | Intelligence & reasoning | 2 | Low | scorers, harness | whether Qwen reasons: never run | run the 11-area suite on a real model and compare 1.7B/4B/8B | `suite19.run_all(real_answer)` |
| 2 | Knowledge & factual accuracy | 2 | Low | scorers | model facts unmeasured | same | factual_accuracy + abstention areas |
| 3 | Coding | 2 | Low | code-test scorer; edit/rollback tools (fake model) | real model code quality | real-model coding tasks with unit tests | coding_debugging area |
| 4 | Research & web | 3 | Low | document reading with real files | no search provider verified | configure/verify a real provider; citation checks on a real model | provider smoke test + source tasks |
| 5 | Memory & personalization | 5 | Medium | retrieval measured (above); persistence, export, delete, supersede tested | usefulness in real chat; semantic retrieval | embedder or larger bench + abstention fix | `memory_eval` with a larger, independently written set |
| 6 | Planning & task completion | 3 | Low | mission machinery (fake) | real-model plans | real-model missions | planning area + mission suite |
| 7 | Windows operation | 2 | Low | policy/executor logic, mocks | nothing on real Windows | Windows task suite | checklist part 7+ |
| 8 | Voice | 2 | Low | simulated hands-free stack (P17) | no real mic/Vosk/SAPI | real-hardware wake/latency tests | checklist voice section |
| 9 | Personality | 3 | Low | style/safety separation tests | no real-model style evaluation | evaluate 14 styles on a real model | personality consistency area |
| 10 | GUI & UX | 2 | Low | source scans | never launched | launch on Windows, screenshot review | checklist GUI part |
| 11 | Reliability & recovery | 4 | Medium | many failure-injection tests (simulated), harness stage failures | real OOM/crash/disk-full | run injected failures against the real stack on Windows | reliability list in checklist |
| 12 | Privacy & security | 4 | Medium | negative tests, no-network guards, document bomb/entity refusals | self-audit only; no independent review | external review + fuzzing | independent audit |
| 13 | Offline | 3 | Low | offline code paths, no hidden network | never run with network off on a real model | disconnect network and run the validation harness | harness with network disabled |
| 14 | Performance on target hardware | 1 | High | nothing measured; selector gives estimates only | everything | measure the 4070 SUPER: load, TTFT, tok/s, peak memory | harness + benchmark JSON |
| 15 | Packaging & commercial | 1 | High | build scripts exist; licensing table written | no installer ever built; legal review pending | build and install on a clean Windows 11 | installer checklist |

Overall: about 3/10 (unchanged from the Prompt 17 audit; this work improved memory retrieval and removed hardware/model-selection blind spots, but the dominant unknowns remain unmeasured).

## The fifteen final questions
1. **Categories at >= 8:** none.
2. **Improved:** memory (retrieval measured and better), hardware/model selection (new, tested), benchmark & validation tooling (new), document-format evidence.
3. **Still unverified:** everything listed under "Not measured".
4. **Real model inference verified?** No. 5. **GPU acceleration verified?** No (detection/backend/verified states exist; none exercised against a real GPU).
6. **GUI launched?** No. 7. **Voice on real hardware?** No. 8. **Installer on clean Windows?** No. 9. **Offline with network off?** No (the code makes no hidden network calls; not demonstrated on a model).
10. **Licensing reviewed?** Engineering inventory only (`LICENSING_AUDIT_P19.md`); Qt/LGPL, CUDA redistribution, Vosk models, PyInstaller, installer tooling need qualified review.
11. **Ready for everyday users?** No. 12. **Ready for paying customers?** No.
13. **Five blockers:** (a) no real-model run, so quality is unknown; (b) no GPU-enabled runtime, so the RTX 4070 SUPER is unused; (c) no Windows run of GUI/voice/desktop control; (d) no installer; (e) licensing/legal review and independent security review outstanding.
14. **Retrieval weakness to fix next:** false positives on unanswerable questions.
15. **Next milestone:** on the target PC, install the GPU build of llama-cpp-python, fetch Qwen3 1.7B, run `realmodel.validate` CPU and GPU, then `suite19.run_all`; store the JSON.
