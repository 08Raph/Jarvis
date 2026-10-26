# Prompt 15 review: personality styles, safety overrides, voice hook

Status: **PARTIAL.** The personality system, its settings, safety overrides and its separation from permissions are implemented and tested on Linux with a fake model. Nothing was run on Windows 11, with the real Qwen model, with the Qt window, or with any audio hardware. Not security-approved, not a release candidate.

Started from `project-jarvis-prompt14.zip` (SHA-256 `c77e4f16af10d726e7eeba57e1d1f6c7c13912d91d6907da2db90ade3657076d`, integrity verified, left unchanged). Baseline before any change: 1379 tests, OK, 6 skipped. After: 1435 tests (56 new), OK, 6 skipped.

## 1. Feature status found at the start
| Requested | Found | Done now |
|---|---|---|
| Central personality engine, Normal default | trait profiles + template layer existed; default was "Professional JARVIS" | New style registry (`personality/styles.py`) and `Persona` (`persona.py`); Normal is the default; trait profile still applies inside Normal |
| Savage, flirty, debate, coach, romantic, conspiracy, explainer, therapist, tutor, calm | none | all 10 implemented as prompt-level styles |
| Technical / Professional / Concise | existed (`engine.MODES`) | reused through `trait_mode`, not duplicated |
| Settings, persistence, validation | trait store only | `persona.json`, validated, atomic write, fail-safe load |
| GUI selector, indicator, return-to-Normal, intensities, preview, reset | no personality UI | new Personality page + Chat banner (code only, never run) |
| Permission separation | modes/executor already separate | enforced by test (imports, behaviour, files) |
| Local STT / TTS | Vosk STT, SAPI TTS, session, barge-in, cancel, PTT, mute existed (CLI only, hardware-untested) | **no new engines**; style speech-rate hook added |
| Natural turn-taking / hands-free | wake word + follow-up + barge-in existed | unchanged; not verified |
| Vehicle commands | **no vehicle integration exists** | none invented (a test asserts none exists) |

## 2. Modes implemented (14 ids)
normal (default), savage*, flirty*, romantic* (*adult opt-in), debate, coach, conspiracy, explainer, therapist, tutor, calm, professional, technical, concise. Intensity settings: roast 1-3, profanity 0-3 (independent), flirt 1-3, argument 1-3, motivation 1-3; tutor language/level; calm minutes; hands-busy.
Not added as separate modes: "Grok Doc" (the Dramatic Expert Explainer replaces it as requested).

## 3. Reused vs new
Reused: `PersonalityProfile`/`INVARIANTS`/trait modes, `VoiceSession`, `speakable`, controller/window structure, `LocalAssistant` prompt assembly, secret-rejecting memory.
New: `personality/styles.py` (registry + guidance), `personality/persona.py` (settings, per-turn `observe`, prompt `_compose`, `finish`), `personality/safety.py` (signals, output guard), CLI `jarvis persona`, controller `persona_*`, window Personality page, `VoiceSession(rate_factor=...)`, tests, docs.

## 4. Files changed
Added: `src/jarvis/personality/{styles,persona,safety}.py`, `tests/test_p15_persona.py`, `docs/PERSONALITY.md`, `docs/PROMPT15_REVIEW.md`, `docs/PROMPT15_NEGATIVE_CONTROLS.json`.
Edited: `personality/__init__.py`, `assistant.py` (persona turn, memory policy, label/guard, stream), `app/controller.py`, `cli.py`, `ui/window.py`, `voice/session.py`, `CHANGELOG.md`, `docs/{VOICE,PRIVACY_AND_NETWORK,WINDOWS_TEST_CHECKLIST}.md`. Earlier archives untouched. Nothing in `desktop/` or `missions/` changed, so the Prompt 13/14 retry allowlist and policy are as delivered (also asserted by a test).

## 5. Dependencies and provenance
No new dependency, model or download. Style text is original. No paid API, no cloud fallback; a test runs a reply and a stream with sockets and DNS blocked.

## 6. Design rules and how they are enforced
* Style is guidance only. `personality/*` imports nothing from desktop/missions/dev/extensions/modes/consent and those layers import nothing from it (AST test). The executor returns identical results for the same calls in all 14 styles after hostile "approvals are off" roleplay messages (declined / verified / verified, 2 confirmations asked each time). Permission/mode files are byte-identical after every persona operation.
* Fixed `SAFETY_BLOCK` ends every non-minimal prompt; saved settings cannot inject text (language field is a strict pattern).
* Invalid or corrupt `persona.json` (12 cases tested) gives Normal, gentle intensities, adult opt-in off; never a stronger style.
* Per message, before the model sees it: stop phrases return to Normal and persist; distress/emergency replace the style for that reply (no label, no memory) and end roleplay styles; an under-18 statement turns adult opt-in off (age not stored).
* Savage / Flirty / Romantic: saved personal notes are left out of the prompt entirely (so they cannot be used as ammunition). Cost: those styles do not use your notes.
* Roleplay labels (e.g. "[Fiction: conspiracy comedy]") are added by the program. A last-line regex guard replaces "kill yourself"-type output in non-Normal styles, including mid-stream.
* Voice: rate hint is clamped to 0.5-2.0, a broken hint is ignored, microphone remains off by default, cancel still silences speech (fake audio).

## 7. Tests and results
Commands (from the project root):
`PYTHONPATH=src python -I -m unittest discover -s tests` -> **Ran 1435 tests, OK (skipped=6)**.
`PYTHONPATH=src python -I -m unittest discover -s tests -p test_p15_persona.py` -> 56 tests OK.
Repeated from a fresh extraction of the final archive: see the delivery message for the final result.

Real vs simulated
* REAL: persona code, files, assistant prompt assembly, controller, CLI, executor/policy (simulated desktop backend).
* FAKE MODEL: scripted replies recording the messages the model received. This shows what is sent and how replies are post-processed, **not** that a model follows a style.
* FAKE AUDIO: fake mic/recognizer/speaker. No real audio test passed or was claimed.
* NOT RUN: Qt window (only compile + AST/structure checks), Windows, installer, real Qwen.

Negative controls (`docs/PROMPT15_NEGATIVE_CONTROLS.json`, disposable copies only): 23 deliberate breakages (adult gate, default style, no validation, aggressive fallback, stop/crisis/emergency/minor handling, memory policy, safety block, output/stream guard, label, config injection, ranges, guest persistence, rate clamp, import-separation both directions, driving cue, stop phrase). 22 detected. One (flip only the guest `persist` flag) is not detected because the path is already `None` for guests (a redundant defence); the stronger guest mutation is detected.

## 8. Limits
* Real-model adherence untested. A 1.7B model may ignore or exaggerate styles; the heuristics and guard are not guarantees. Keyword detectors for stop/distress/emergency/minor can miss paraphrases or misfire; they are tuned to err towards dropping the style.
* Tutor cannot assess pronunciation (no scoring tool); the prompt says so. Calm cannot "know" the user is driving beyond what they say.
* Preview shows the instructions, not a generated sample (a sample needs the model; hard-coded demo replies were deliberately not used).
* No streaming TTS, no instant or "human-like" voice, no new wake-word/hands-free work, no new STT/TTS engine: **not implemented**, so not claimed.
* Medical/vehicle styles give general information only and say so; they are not safe-by-construction.
* Persona is read at assistant start by the CLI chat; the window shares one persona object with its assistant, so changes apply immediately there.

## 9. Acceptance table
| Area | Result | Basis |
|---|---|---|
| Personality system | PASS (Linux, fake model) | registry, engine, tests, negative controls |
| Default Normal behaviour | PASS | fresh profile, bad config, reset, guest |
| Savage mode and other modes | PARTIAL | prompts/safety verified; model adherence untested |
| Settings and persistence | PASS | validation, restart, atomic write, corrupt files |
| Permission and executor isolation | PASS | import/behaviour/file tests; allowlist unchanged |
| Local speech recognition | PARTIAL | existing Vosk path, unchanged, hardware-untested |
| Local speech synthesis | PARTIAL | existing SAPI path + rate hook, hardware-untested |
| Natural turn-taking and interruption | PARTIAL | existing barge-in/cancel logic tested with fake audio; nothing new, nothing real |
| GUI integration | PARTIAL | controller tested; window compiled only, never displayed |
| Offline operation and privacy | PASS for what ran (no network used in tests); real-model offline operation unverified |
| Windows 11 verification | BLOCKED | no Windows; steps in checklist 6o |

## 10. Remaining Windows 11 steps
`docs/WINDOWS_TEST_CHECKLIST.md` section 6o (nine items: first launch Normal, opt-in and persistence, real-model behaviour per style, stop/crisis behaviour, corrupt config, conspiracy label, permission non-bypass, voice rate/cancel/mic indicator, visual and accessibility check).
