# Stage 7 validation and personality-quality audit

Starting point verified: branch `main`, commit `e63ee22`, clean tree, remote `origin/main` equal to it, and the delivered Stage 7 ZIP byte-identical
to `git archive` of that commit. (GitHub reports the repository moved to `08Raph/Jarvis`; pushes still work.)

## What this audit could and could not test
| Item | Status |
|---|---|
| Full `python -m unittest discover -s tests` | RUN (numbers in the final report / CHANGELOG) |
| Real-model personality evaluation | **NOT RUN.** No `llama_cpp`, no GGUF weights in the sandbox; Hugging Face is unreachable (documented in STAGE5_MODEL_SOURCE_VERIFICATION.md). Nothing was fabricated. |
| Model comparison | **NOT RUN** (same reason). A comparison tool exists (`audit.compare`), it ranks nothing. |
| Real PySide6 Personality page | **NOT RUN.** PySide6 is not installable here (pip finds no matching distribution). Page wiring was checked against stand-in Qt classes (`tests/test_p50_...`), which cannot show drawing, clicks or appearance. |
| Windows, SAPI, Kokoro audio | **NOT RUN.** |

## Defects found by inspection and tests, and fixes
- **D1 contradictory prompt.** The older fixed trait text ("Register: formal", "Humour: minimal", "Tone: neutral and businesslike") was sent
  next to the Stage 7 spec and contradicted it and the user's controls (Normal's default even carried Professional JARVIS wording). The five
  controls are now stated once, by the spec; the older profile contributes only initiative, technical depth and the invariants. A profile the user
  customised with the older `personality` commands still reaches the model (converted to levels).
- **D2 one bad value wiped everything (generalised at the gate: per-field recovery, see the runbook section 9).** A single out-of-range customization value discarded the whole `persona.json` (style, intensities, language).
  At the pre-Stage-8 gate this became per-field recovery: each invalid field is reset on its own with a visible note, and the adult gate fails closed.
- **D3 distress phrasing missed.** "I've been feeling hopeless", "I feel like a burden", "I can't stop crying", "my chest hurts", "he hit me",
  "I took a bunch of pills" and similar did not trigger the safety override, so a roleplay style would have carried on. Patterns extended; benign
  look-alikes are tested not to fire. This is still a keyword heuristic and **will miss other phrasings**; it is not a clinical classifier.
- **ResourceWarnings.** Traced with `-X tracemalloc=14`: all 83 sampled unclosed-SQLite warnings originate in tests that create in-memory
  `PersonalMemory.guest()` / `MemoryStore()` objects and never close them (test_p24_disclosure, test_p26_final_pass, test_personal_memory, test_core,
  test_p25_disclosure_paths, ...). No allocation without a test frame was found; production paths (`LocalAssistant.close`, controller memory helpers)
  close their stores. They are test hygiene, not a product leak. The tests were left unchanged.

## Real-model workflow (run this on a machine with a model)
```
jarvis models fetch <id>        # or: jarvis models add-gguf <file> --id ... (see STAGE5_VALIDATION_RUNBOOK.md)
jarvis persona audit            # 17 personalities x 10 prompt categories + 1 defining prompt each + control sweeps (min/default/max)
jarvis persona suite            # the 15-scenario program-behaviour suite against the real model
```
Output: `<data>/eval/personality_audit_<time>.json` (model identity and runtime, configuration, every prompt and transcript, automated measures,
similarity and repeated-opener flags, **blank** human scores) and a `.md` transcript for reading. Run it once per candidate model; compare with
`audit.compare` and add tokens/second, memory use and the human rubric (0-3 per dimension, defined in `audit.RUBRIC`). Guest session: no personal
memory is read and no settings are saved. Candidates documented in this repo (Qwen3 1.7B/4B/8B/14B, Apache-2.0) are unevaluated for personality quality.

## Windows validation checklist (not performed)
1. Launch; open Personality. 2. Combo lists 17 modes; Savage, Flirty, Sexy, Romantic show "locked". 3. Selecting a locked mode and Apply shows an error, no change.
4. Tick the 18+ box: the four unlock; untick: current adult mode returns to Normal. 5. Apply each mode: indicator changes with no restart; chat history intact.
6. Five spin boxes show -2..2; type a value and Tab: it persists after closing and reopening. 7. Corrupt `persona.json` (edit a control to 9): app starts, note shown,
other settings kept. 8. Corrupt it fully: Normal, note shown. 9. Voice page: SAPI and Kokoro selections unchanged by personality; Calm is audibly slower,
Concise faster; speed never leaves 50-200 %. 10. Send "I feel hopeless" in Savage: reply is plain and serious, mode returns to Normal. 11. Say "I'm 15" with an
adult mode on: switched off with notice. 12. Tab order and screen-reader names (all spin boxes have accessible names).

## Unresolved
Everything marked NOT RUN above. Personality distinctness, naturalness, and whether the controls change a real model's behaviour are **unknown**.
Fake-model results only show that the program hands the model the right instructions.
