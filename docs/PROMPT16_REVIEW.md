# Prompt 16 review: personality completion, readiness honesty, window speech

Status: **PARTIAL.** Everything that can be built and tested without the real model, a displayed window or audio hardware was built and tested. The real Qwen model, the Qt window, Windows 11 and every audio device remain **unverified because they were not available**; nothing below claims otherwise.

Started from `project-jarvis-prompt15.zip` (SHA-256 `564d3055e13e70f82eca516bc6db994b3151bbc049eaccf36b0f75b0a3e1a0b5`, integrity verified, kept unchanged). Clean-extraction baseline: **1435 tests, OK, 6 skipped**, no failures. Now: **1501 tests, OK, 6 skipped** (66 new). Prompt 15 and earlier archives are untouched.

## 1. What the environment allowed (measured, not assumed)
| Needed | Found |
|---|---|
| Qwen/GGUF weights | none on disk; not downloaded (no licence-cleared source in the sandbox, no PyPI/model access) |
| llama-cpp-python | not installed, not installable (no package index) |
| PySide6 / a display | neither (window code compiled and structure-checked only) |
| PortAudio / sounddevice | the Python package imports but PortAudio is missing, so it fails to load |
| Vosk, Vosk model, SAPI, Windows | none |
So sections 1 (real-model tests), 3 (displayed GUI) and 4 (real audio) of the prompt are **BLOCKED here**. Per the prompt, the surrounding engineering was finished and exact Windows steps written (checklist 6p).

## 2. Reused / changed / new / blocked
Reused: P15 registry, `Persona`, `LocalAssistant` prompt path, `SapiSpeaker`, `speakable`, `VoiceSession`, `jarvis selftest` record, model store, controller/window structure.
Changed: `safety.py` (much broader detectors, uncertain-age cues, exact `/normal` command), `persona.py` (fail-closed age, drift line, `command()`, guard on every reply, `strip_label`), `assistant.py` (control commands skip model and memory agent; history stores replies without program labels), `controller.py`, `window.py`, `status.py` (voice row no longer says "no voice controls"), `voice/settings.py` (`vosk_model`, `speak_in_window`), `voice/console.py` (`n` key), `voice/session.py` untouched this time, `cli.py` (`persona selfcheck`, `voice run` uses the saved Vosk folder).
New: `app/readiness.py`, `app/speech.py`, `personality/selfcheck.py`, `tests/test_p16_completion.py`, this doc, `PROMPT16_NEGATIVE_CONTROLS.json`, checklist 6p.
Nothing under `desktop/` or `missions/` changed: Prompt 13/14 retry policy (14-tool allowlist, 2 retries, 20 polls) is as delivered and asserted again.
Still blocked: real-model adherence, rendering, microphone/Vosk/SAPI behaviour, latency, wake-word accuracy, barge-in, Windows 11, packaging/installer.

## 3. Safety hardening (section 2 of the prompt)
* **Visible, reliable stop:** `/normal` (`/stop`, `/style off`) is an exact-match command handled before the model and memory agent; window **Return to Normal** also cancels the reply and silences speech; console key `n`.
* **Priority:** stop > selected style; crisis/emergency > everything, including roleplay text in the same message.
* **Fail closed:** under-18 statements and weaker "age may be uncertain" cues switch the adult opt-in off; it must be re-given. Fuzz: 400 random corrupt `persona.json` files + junk files never produced an adult opt-in or a non-default intensity unless the file was fully valid and said so.
* **Detectors audited on a written corpus:** the first run found real misses and false positives (for example "my boyfriend is hitting me", "please stop the download", "turn off the lights", "stroke a cat", "shot of espresso", "Suicide Squad"); fixed. Documented residual limits are tested as limits: non-English or indirect wording is not caught, and some ordinary sentences ("my career is on fire") still trigger the safe direction.
* **Drift:** the model's history no longer contains program labels; every prompt says to follow only the current style; the style text is re-sent every turn (30-turn test with a 6-message history); `fit_messages` keeps the system message.
* **Output guard** now runs on every reply, including Normal and safety overrides (heuristic).
* Memory/secret leakage into Savage/Flirty/Romantic prompts: unchanged from P15 and re-tested; secrets are still refused by memory itself.

## 4. Model and offline readiness (section 5)
`app/readiness.py` replaces "Model ready" with ordered levels: file found, runtime installed, runtime loads (import attempted), model loaded and inference completed (only from a recorded passing `jarvis selftest` for the same model, labelled a past record), tool calling, offline operation, personality adherence. The last three are **not verified** by construction. Chat banner and Status page use it. The pretrained Qwen model remains clearly separate from the from-scratch pipeline (About text unchanged).

## 5. Real-model self-check (section 1)
`jarvis persona selfcheck` runs all 14 styles on three fixed factual prompts, a multi-turn drift check, and both return-to-Normal paths against whatever model is active, in a guest session; saves every reply to `persona_validation.json`. Objective checks only; a human must judge tone. **It has not been run on a real model.** Its logic is tested with a scripted model, including a model that loses the facts (detected).

## 6. GUI and voice (sections 3-4)
Window: Chat banner with current mode and Return to Normal; Personality page (all 14 styles, locked adult styles, intensities, preview of the exact model instructions, adult opt-in, reset, **Read replies aloud**, speaking speed, **Stop speaking**); Status page readiness table. A static test proves every control calls an existing controller method and the window never touches the microphone. **The window was never displayed**; controller behaviour is what was tested.
Voice: replies can be spoken in the window through the existing SAPI speaker (off by default, cleaned text, style rate, crisis at normal speed, failures reported as text); Stop / Return to Normal / emergency stop / close silence it. Microphone input stays in the console session (`jarvis voice run`, saved Vosk folder). No new speech engine. Nothing here proves speech recognition, audio output, latency, wake-word accuracy or interruption on real hardware.

## 7. Tests (evidence labels as requested)
Commands: `PYTHONPATH=src python -I -m unittest discover -s tests` -> Ran 1501, OK (skipped=6); new file alone: 66 tests.
| Group | Label |
|---|---|
| Detector corpus (stop/distress/emergency/minor/uncertain), commands, fuzz of config files, fail-closed | Real execution (heuristics and files, Linux) |
| `/normal`, drift, mode switching, harmful-output guard, history, context budget | Fake-backed (scripted FakeLlama records what it is sent) |
| Readiness: no-model, no-runtime, voice libraries in THIS sandbox | Real execution (this sandbox, measures its absences) |
| Readiness with runtime/model/devices present | Fake-backed (patched imports/records) |
| Window speech, controller wiring, voice console `n` key, mute/cancel | Fake-backed (fake speaker, fake session) |
| Missing PortAudio/Vosk/SAPI/model/weights paths | Real execution (this sandbox) and fake-backed |
| No network (sockets/DNS blocked) for readiness, speech, self-check, assistant | Real execution with blocking patches |
| Window uses only existing controller methods, no decorative controls, no microphone code | Static inspection |
| Emergency stop, permissions files, retry constants independent of personality | Real execution (simulated desktop) |
| Real Qwen, displayed Qt window, real audio, Windows | **Not run** |

Modified existing test: `tests/test_p15_persona.py::test_stream_gets_label_first` asserted that the history held the program label. That behaviour was intentionally changed (labels nudged later styles); the assertion now requires the history to be label-free, and `test_history_never_contains_program_labels_and_old_style_cannot_pin_the_new_one` is the regression test. No other existing test changed.

Negative controls (`docs/PROMPT16_NEGATIVE_CONTROLS.json`, disposable copies only): 31 deliberate breakages (control command, stop priority, fail-closed age, drift, output guard, readiness honesty x6, speech default/flag/stop/clamp/filter, microphone options, console key, adult confirmation, banner, detector regressions, self-check, stop/style independence). **31 of 31 detected.** Two (speech-rate clamp, `clear_stop` resetting the persona) were NOT detected on the first run; tests were added and the re-run detected them.

## 8. Acceptance table
| Area | Result | Basis |
|---|---|---|
| Personality implementation | PASS | registry, 14 styles, fake-model prompt path |
| Real-model adherence | BLOCKED | no weights/runtime; harness and Windows steps ready |
| Personality safety and permission isolation | PASS for what is deterministic; heuristics limited | corpus, fuzz, 31 negative controls; paraphrases and non-English remain a limit |
| GUI integration and actual rendering | PARTIAL | controller + static checks pass; window never displayed |
| Settings persistence | PASS | restart, corruption, fuzz tests |
| Local STT | BLOCKED | no Vosk/mic/PortAudio; code path and status only |
| Local TTS | PARTIAL | window speech wiring tested with a fake speaker; SAPI never played |
| Natural turn-taking and interruption | PARTIAL | cancel/mute/stop logic tested with fakes; nothing real, nothing claimed |
| Offline operation | PARTIAL | no network used in any tested path; real offline run not verified |
| Windows 11 behaviour | BLOCKED | checklist 6p |
| Packaging and installation | not tested | no installer build in this milestone |

## 9. Remaining blockers and next smallest step
Run checklist 6p on Windows 11 with the real model and report `jarvis selftest`, `jarvis persona selfcheck` output and the step notes. That single run converts four BLOCKED/PARTIAL rows into evidence.
