# Prompt 17 review: Prompt 16 completion, automatic voice start, always-listening hands-free conversation

Status: **PARTIAL.** The hands-free pipeline (automatic initialization, always-listening loop, wake phrase, local recognition, answer through the selected personality, spoken reply, automatic return to listening, mute, disable, cancel, recovery, privacy and permission isolation) is **built and tested with fake audio and a scripted model on Linux**. The real microphone, a real Vosk model, real speakers (SAPI), the real Qwen model, the displayed Qt window and Windows 11 were **not available**, so none of those are verified. Nothing below claims otherwise.

Started from `project-jarvis-prompt16.zip` (893,379 bytes, SHA-256 `fe8aedb8e7cb5a4c53fa7eeb2aae610db6eb9a578688f6a9fcc6e83c288d16ef`, matched, left unchanged). Baseline on a clean extraction: **1501 tests, OK, 6 skipped**. Now: **1591 tests, OK, 6 skipped** (90 new, all in `tests/test_p17_handsfree.py`). Prompt 16 and earlier archives are untouched.

## 1. Prompt 16 baseline: what was done, what was not
| P16 item | State found | Prompt 17 action |
|---|---|---|
| Real Qwen + personality adherence | harness only; never run | still **BLOCKED** here (no weights, no llama-cpp). Harness unchanged; Windows steps in checklist 6q steps 3 and 10 |
| GUI rendering / controller wiring | controller tested; window never displayed | new Voice page added, still never displayed (no PySide6/display); static + controller tests only |
| Local STT / TTS | Vosk and SAPI code, hardware-untested; microphone only in a console command | integrated into the window's hands-free loop; **still unverified on hardware** |
| Hands-free turn-taking / interruption | `VoiceSession` existed; only console | hardened (see section 3) and wired to the window; barge-in tested **simulated** only |
| Offline behaviour | no network in tested paths | extended: a full hands-free turn is run with every socket blocked |
| Windows 11 / packaging | not done | not possible here; checklist 6q written |
Two Prompt 16 statements became wrong and were corrected on purpose: "this window never uses the microphone" (readiness row, feature row, Personality page text, privacy/troubleshooting docs) and the matching test assertion (see section 7).

## 2. What was built (reused vs new)
Reused unchanged: `EnergyVAD`, `VoskRecognizer` (grammar-limited wake + dictation), `SapiSpeaker`, `speakable`/`shorten`, `match_wake`, `Persona`, `LocalAssistant.reply_stream`, `InstanceLock`, `SpeechOut`, the controller and its confirmation bridge.
Changed: `voice/session.py` (duplicate guard, own-voice echo filter, post-speech guard, self-wake guard, drop reply after mute/cancel, `observe()`, speaker mute), `voice/backends.py` (`VoskRecognizer.preload()` with a real reason), `voice/settings.py` (`speaker_muted`, `post_speech_guard_seconds`, validation of `muted`), `app/speech.py` (public shared `speaker()`, speaker mute applies to typed-chat speech), `app/controller.py` (voice methods, voice turn on the same assistant, hooks so Stop / Return to Normal / emergency stop / shutdown cancel voice), `app/readiness.py` and `app/status.py` (honest microphone rows), `ui/window.py` (Voice page), `ui/app.py` (`voice_autostart()` once after the window is shown), `cli.py` (`jarvis startup ...`, `voice run` uses the shared microphone lock).
New: `app/voice_service.py` (the single worker thread that owns microphone, recognizer and speaker), `app/startup.py` (optional start-with-Windows), `tests/p17_common.py`, `tests/test_p17_handsfree.py`, this review, `PROMPT17_NEGATIVE_CONTROLS.json`, checklist 6q, doc updates.
Nothing under `desktop/` or `missions/` changed; the Prompt 13/14 retry policy (14-tool allowlist, 2 retries, 20 polls) is asserted again.

## 3. Hands-free behaviour actually implemented
* **Automatic initialization.** `voice_autostart()` runs once after the window is shown. If hands-free was turned ON earlier (saved in `voice.json`), a worker thread checks the microphone library and devices, loads the Vosk model (`preload()`), checks the speaker (missing speaker degrades to text-only with a visible note) and then starts listening. If voice was never turned on, **the microphone is not touched** (default is off). Idempotent: repeated calls leave one thread and one microphone stream. Guest sessions never start voice.
* **Honest status.** Off / Starting / Listening / Processing / Speaking / Muted / Unavailable. Listening is shown only while the worker thread is alive, the stream is open and the recognizer is loaded (tests break each condition). Failures show the real reason and a Retry button.
* **Conversation loop.** Wake phrase -> capture -> local recognition -> the same assistant, persona and safety path as typed chat -> reply spoken (personality speed, code/paths/links removed) -> back to Listening (or the follow-up window). No button per turn; push-to-talk remains an option.
* **Protections:** identical text within 2 s is processed once; audio for 0.6 s after speaking is ignored; text that is mostly Jarvis's last reply is ignored; a wake phrase inside Jarvis's own reply neither restarts nor interrupts it; a reply arriving after mute/cancel/disable is dropped; a user who starts talking while the model thinks is not talked over.
* **Controls.** Microphone mute closes the stream, is saved and survives restart. Speaker mute is separate. Turn hands-free OFF is saved and releases microphone, thread and `mic.lock`. Spoken "return to normal"/"back to normal"/"normal mode"/"style off"/"stop roleplay" become the exact `/normal` command (no model call). Stop / Return to Normal / emergency stop / closing the window silence speech directly (not queued) and abandon a reply being produced. `jarvis voice run` and the window share one lock file, so only one owns the microphone.
* **Windows-login startup (separate, optional).** Off by default; needs explicit confirmation (`--confirm` / a Yes-defaulting-to-No dialog); writes exactly one visible per-user Run value; disable removes exactly that value; never turns the microphone on. Tested with an injected in-memory registry; **the real registry was not touched**.
* **Limits, stated in the UI and docs.** While the model is thinking, audio is read and discarded, so a spoken "stop" is not understood then (use Stop or Mute). Cancelling speech does not cancel model inference (generation stops at its next piece) and neither closes the microphone; they are separate operations. No echo cancellation exists, so speaker echo on a real PC may still mis-trigger; `barge_in` stays `wake_word_only` by default.

## 4. Privacy and permissions
No raw audio is written (static scan of voice code and a scan of the profile folder after a conversation), no network module is imported by the voice code, a full turn runs with sockets blocked, the executor/missions are not importable from the voice code, a spoken "yes allow it" cannot answer a pending approval, spoken permission requests change no permission file, and emergency stop silences voice while remaining in force. Details: `docs/PRIVACY_AND_NETWORK.md`.

## 5. Tests (labels as required)
Commands: `PYTHONPATH=src python -I -m unittest discover -s tests` -> **Ran 1591, OK (skipped=6)**; new file alone 90 tests.
| Group | Label |
|---|---|
| Voice service lifecycle, conversation loop, mute/disable/cancel/recovery/failures (real worker thread, real `VoiceSession`) | **Simulated**: fake microphone, recognizer and speaker; scripted model |
| Session guards (duplicate, echo, post-speech guard, self-wake, barge-in, empty/false wake) | **Simulated**, deterministic frame stepping |
| Startup setting | Real logic, **injected fake registry** (not run on Windows) |
| Missing PortAudio / Vosk / model folder / SAPI in this sandbox | **Real execution** (this machine really lacks them) |
| Privacy scans, import isolation, window wiring, consent dialog ordering | **Static inspection** |
| No-network, no audio files written | Real execution (sockets blocked, directory scan) |
| Real microphone, Vosk, SAPI speech, Qwen, displayed window, Windows 11 | **Not run** |

## 6. Negative controls (`docs/PROMPT17_NEGATIVE_CONTROLS.json`, disposable copies only)
43 deliberate breakages (duplicate/echo/post-speech guards, self-wake, mute and cancel paths, status honesty x2, preload, mic lock, mute and disable persistence, spoken `/normal`, busy gate, shutdown, text-only degradation, device check, guest, autostart, Stop hooks, speaker mute x2, start-with-Windows consent/scope/removal, window confirmation, readiness, default-off, validation, raw-audio write, network import, executor import, idempotent start, ...). **First run: 38 of 42 detected.** Undetected and handled: M7 (session-level speaker-mute stop, covered only indirectly), M16 (cancel while the user is mid-utterance), M31 (window ignores the confirmation answer) -> tests added; M23 (autostart ignores the saved setting) is an **equivalent mutation** because `start()` repeats the same check; a stronger mutation M23b removing both checks is detected. **Final run: 42 of 43 detected; the one not detected is the equivalent M23.** The control (unmodified tree) passed.

## 7. Existing tests changed, and why
* `test_voice.py::test_wake_then_command_then_speech_then_followup`: now waits out the 0.6 s post-speech guard before the follow-up (the old behaviour listened to its own echo immediately).
* `test_voice.py::test_does_not_speak_over_user`: sets the session to THINKING first, because `_deliver` now ignores replies unless the turn is still in progress.
* `test_p16_completion.py::test_feature_row_for_voice_is_not_misleading`: the assertion "never opens the microphone" became "OFF by default", because Prompt 17 intentionally lets the window open the microphone once the user turns hands-free ON.
No assertion was removed or weakened otherwise. Two Prompt 15 window-scan tests (words "Settings" and "grant") flagged my new page text; the page text was reworded and the tests are unchanged.

## 8. Acceptance (PASS / PARTIAL / BLOCKED / FAIL)
| Area | Result | Basis |
|---|---|---|
| Prompt 16 completion | PARTIAL | everything buildable is done; real model, rendering, hardware and Windows remain blocked |
| Automatic voice initialization | PARTIAL | implemented and tested (simulated, worker thread, idempotent, off when disabled); never run with a real window or audio |
| Always-listening hands-free loop | PARTIAL | implemented and tested with fake audio; not run on real hardware |
| Wake-word detection | PARTIAL | existing Vosk grammar + fuzzy matching, guards for self-wake; accuracy and false-wake rate unmeasured |
| Local speech recognition | BLOCKED | no Vosk/model/PortAudio/microphone here |
| Local speech synthesis | BLOCKED | SAPI needs Windows; wiring tested with a fake speaker |
| Automatic return to listening | PASS (simulated) | repeated three-turn test, post-cancel, post-failure; real timing unmeasured |
| Speech cancellation and barge-in | PARTIAL | cancel/mute/stop paths tested; barge-in simulated; echo behaviour unknown |
| GUI integration and rendering | PARTIAL | controller + static checks; window never displayed |
| Real-model personality adherence | BLOCKED | no weights/runtime |
| Model tool calling | BLOCKED | not run; no tool path exists from voice by design |
| Offline operation | PARTIAL | no network used in any tested path; not verified with the real model |
| Privacy and permission isolation | PASS for what is deterministic | scans, isolation tests, negative controls |
| Windows 11 verification | BLOCKED | checklist 6q |
| Packaging and installation | not tested | no installer build here |
Overall: **not complete.** Hands-free voice cannot be called working until checklist 6q passes on Windows 11 with a microphone, a Vosk model, the real model and a displayed window.

## 9. Remaining blockers and next smallest step
Run `docs/WINDOWS_TEST_CHECKLIST.md` section 6q (18 steps) on Windows 11 and send back the notes, screenshots of the Voice page, `jarvis diagnostics`, `jarvis selftest` and `jarvis persona selfcheck` output. Steps 6-9 (start, wake, five questions, self-echo) decide whether the loop really works; step 14 decides failure handling; step 16 decides privacy.
