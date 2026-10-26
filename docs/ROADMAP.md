# Intelligence roadmap

Each stage has an exit test. Do not start a stage until the previous exit test is met on real hardware.

| # | Stage | Status | Exit test |
|---|---|---|---|
| 1 | Tokenizer + tiny LM proof of concept | Code written; tokenizer tested; model **not yet run** | `scripts/smoke_train.py` passes on a torch machine; val loss falls; resume works |
| 2 | LM pretraining on an approved corpus | Pipeline written (ingest, clean, dedupe, split, decontaminate, resumable trainer); no corpus acquired | Dataset report with no warnings; test perplexity recorded in model card; overfit warnings reviewed |
| 3 | Instruction tuning on permitted data | Not started | Needs a licensed instruction dataset or project-written examples; instruction-following benchmark improves over stage 2 on held-out items |
| 4 | Preference/behavior tuning | Not started; only if compute allows | Held-out preference win-rate against stage 3, plus no regression in over-refusal |
| 5 | Tool-use training and evaluation | Tool layer and tool-selection benchmark exist; no tool-use training | Tool-selection score on a larger held-out set, measured with the confirmation flow on |
| 6 | Memory and retrieval | SQLite memory exists; retrieval not built | Retrieval hit-rate on a labelled set; answers cite stored items |
| 7 | Task planning and verification | Not started | Multi-step tasks completed with verified outcomes; failures reported truthfully |

## Honest feasibility

- Stage 1 to 2 on a laptop: feasible for a ~1M parameter toy (see `COMPUTE_ESTIMATES.md`).
- A ~110M parameter model on ~2.5B tokens: about 19 h on one RTX 4090 and ~120 h on an RTX 3060
  under stated assumptions, plus the hard part, which is acquiring and cleaning billions of
  licensed tokens. Even then it will be a modest model.
- Stages 3 to 4 need data you must source and license. Do not distill from another assistant's outputs
  unless its terms allow training on them.

## Uncertainty behavior

`inference/policy.py` abstains or asks for clarification when mean token log-probability is below a threshold,
and the app should offer a tool when one applies. The threshold is a placeholder until calibrated with
`calibrate_threshold()` on held-out labelled answers; log-probability is an imperfect confidence proxy.
