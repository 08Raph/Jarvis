# Stage 7: Full personality system

## What a personality is
A personality is **guidance text for the local language model plus a speaking-speed hint**. It is not a different model, has no
independent intelligence, and cannot grant, approve or skip any permission. Jarvis does **not** rewrite model output to fake a style
(no inserted catchphrases or jokes). The only program edits to a reply are the existing structural roleplay label and the
harmful-phrase guard. A small model may follow a spec only loosely; the Personality page says so.

## Architecture
| File | Role |
|---|---|
| `personality/styles.py` | The single registry of 17 modes (`STYLES`), age flag, memory rule, label, speech-rate hint, safety-critical style rules |
| `personality/spec.py` | Versioned `BehaviorSpec` per mode (`SPEC_VERSION = 1`) and `spec_guidance()` |
| `personality/persona.py` | Persisted settings (`persona.json`), per-turn safety overrides, prompt assembly. The fixed `SAFETY_BLOCK` is always last |
| `personality/evalsuite.py` | 15-scenario x 17-mode evaluation harness |
| `assistant.py` | Unchanged flow: `observe()` -> prompt -> model -> `finish()` |

Prompt order for a turn: style rules, behaviour spec (with effective levels), invariants, safety block. A safety override
(distress, emergency) replaces the whole personality text for that turn only.

## The 17 modes
Normal, Professional JARVIS, Friendly Companion, Technical Engineer, Concise Operator, Humorous Assistant, Savage*, Flirty*, Sexy*,
Debate, Hype Coach, Romantic*, Conspiracy (fiction/discussion), Dramatic Expert Explainer, Therapist-Style, Language Tutor, Calm.
`*` = 18+, locked until the adult opt-in. Each spec defines tone, vocabulary, rhythm, explanation style, problem-solving style, and
behaviour on a mistake, frustration, uncertainty, disagreement and serious topics, plus a "never" list (`personality/spec.py`).

## Settings
`persona.json` (versioned, atomic write). Five customization controls, humor, warmth, directness, formality, verbosity, are stored
as an **offset -2..+2 from the active mode's default** and clamped to the mode's identity limits (for example Concise verbosity <= 2,
Humorous humor >= 3, Calm humor <= 2). Changes apply from the next message; history and memory are untouched; no restart.
Missing file, corrupt JSON, unknown style, out-of-range value, unsupported future version, or an adult mode without opt-in all fall
back to Normal with adult opt-in off and offsets 0 (with a visible note). Files from earlier stages load with offsets 0.
Where: Personality page (UI), `jarvis persona control <name> <-2..2>`, `AppController.persona_control`.

## Safety, memory, permissions
Unchanged and tested: age gate (a minor/uncertain-age cue switches adult modes off), distress/emergency override, stop commands,
edgy/romantic modes receive no saved notes, personality package imports no executor/tools. Chat text cannot change settings.

## Voice
Verified interfaces: the shared speaker takes (text, rate multiplier, volume, voice) for both SAPI and Kokoro (speed). A personality
changes **only the speaking-speed multiplier** (`speech_rate`, e.g. Calm 0.8, Concise 1.1). No whisper, laughter or emotion effect
exists in either engine interface, so none is claimed. Voice selection, preview, mute, Stop, push-to-talk, wake word, hands-free and the
explicit no-silent-fallback rule are unchanged. New mode ids (`companion`, `humorous`, `sexy`) are valid per-mode voice-override keys.

## Tests
- Unit/behaviour (fake model, fake audio): `tests/test_p47_personality_suite.py` (specs, limits, settings corruption, age gate,
  switching, memory, controller, CLI, 17 x 15 matrix plus checks that the suite catches deliberately broken pipelines).
- Baseline: `python -m unittest discover -s tests`.
- Real model (run on your PC): `jarvis persona suite [--styles ...] [--scenarios ...]` saves transcripts under `<data>/eval/`.

## Automatically verified vs needs a human
Automatic: spec reached the model for each scenario, effective levels match settings, safety text present and last, overrides fire,
labels, memory exclusion, history across a switch, settings immune to chat text, persistence and corruption handling.
Human: whether replies actually sound witty, warm, savage, calm; whether a given small model follows the spec; charm and
human-likeness; how adult modes read.

## Limitations / not verified
No real model inference, real audio (SAPI/Kokoro), PySide6 UI or Windows run happened in the build sandbox. The two voice-failure
and memory scenarios in the CLI suite are marked not-run unless a harness is supplied. Windows compatibility is Stage 8 and is not claimed.
