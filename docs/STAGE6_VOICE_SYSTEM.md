# Stage 6: Voice system (2026-10-27; Linux sandbox only)

**Status: implemented and tested with FAKES. Nothing here was run on Windows, with PySide6, a real microphone, a real speaker, Vosk or Windows speech.
No sound was produced and nothing was heard.** Stage 5's real-model and Windows blockers are NOT resolved by this stage.

## 1. What was added on top of the existing voice code
| Area | Files | What it does |
|---|---|---|
| Engines | `voice/engines.py` | Small `TTSEngine` interface + registry. One real engine: **Windows SAPI** (`SapiEngine`). `FactoryEngine` wraps any speaker object (tests / future embedding). A new engine = one class + `registry.add(...)`. Engine code is never loaded from a user folder (that would run untrusted files). |
| Voice list and choice | `voice/manager.py` | Asks the engines which voices exist (never invents any), groups them **feminine / masculine / not specified**, saves the choice in `voice.json`, resolves the voice at the moment of speaking, falls back visibly. |
| Preview | `voice/preview.py` | Worker thread: `loading -> playing -> finished / stopped / error`. Stop, timeout (30 s), cleanup, stale-worker guard. |
| Input check | `voice/inputcheck.py` | Read-only check: lists microphones, looks for the Vosk package and model folder. **Never opens the microphone.** |
| Page text | `voice/viewmodel.py` | All wording and list building for the Voice page, with no Qt (testable). |
| Controller | `app/controller.py` | `voice_catalog`, `voice_catalog_refresh`, `voice_apply`, `voice_clear_persona`, `voice_preview_start/stop`, `voice_input_check`, `voice_input_set`, `voice_set_model_folder`, `voice_switch_mode`. |
| Window | `ui/window.py` | Voice page: Voice section (type, voice, details, Preview Voice, Stop, Apply, loading bar, per-mode option), Microphone section (device, check, mode, hold-to-talk, wake phrase, model folder). |
| Settings | `voice/settings.py` | New fields `voice_by_persona` (optional per-mode voice ids) and `vad_min_rms` (speech-detection floor). Old files load unchanged. |
| SAPI backend | `voice/backends.py` | Voice list now includes the engine's own `Gender`, `Age`, `Enabled`; `exit_code()` so a failed speech process is not reported as played. |

Already existing and reused unchanged in behaviour: Vosk recogniser, `SoundDeviceMic`, wake-phrase matching ("hey jarvis" default, configurable), energy VAD, hands-free
service (start, stop, mute, push-to-talk, cancel, barge-in options, mic lock, visible state), speakable-text filter, speaker mute, rate and volume.

Defect found by the full suite while building this and fixed: a "restart voice" task queued by a microphone / wake-phrase / mode change could run AFTER the application had begun closing and start a new microphone worker. `VoiceService.retire()` (called first in `AppController.shutdown`) now makes `start()` a no-op from then on; a regression test covers it.

## 2. Voices
* The list is whatever Windows reports (System.Speech `GetInstalledVoices`). **This sandbox is Linux, so no real voice list was ever read. I cannot tell you which feminine or masculine voices exist on your PC.** The page shows them after you open it; the CLI shows them with `jarvis voice voices` (grouped).
* **Category** = the engine's own `Gender` field (Male -> masculine, Female -> feminine). A voice that reports neither (Neutral / NotSet / empty) is listed under "Not specified"; its category is never guessed from its name.
* **Language / accent** = the voice's locale code (for example `en-GB`) turned into words by a small table (`English (United Kingdom)`); an unknown code is shown as the code.
* Limit: System.Speech lists the classic desktop voices. Newer "natural" voices that Windows 11 stores separately may not appear (not verified here).
* **Fallback**: if the saved voice is gone, the engine's default voice is used and the page says so ("The voice 'X' is not installed... Using the default voice of Windows built-in speech instead"). If no engine works, Jarvis answers as text and says why. If a mode's own voice is gone, the global voice is used.
* Voice choice is **independent of personality**. One global voice (`tts_voice`); an OPTIONAL override per personality mode (`voice_by_persona`). Switching mode never changes the saved voice; a mode uses its own voice only if you set one for that mode. Stage 7 personality work was not started.
* Restart: the choice lives in `voice.json` (atomic write) and is read at the moment of speaking.

## 3. Preview
"Finished" means only that the engine ran the sample to its end and (when it reports one) exited with code 0. Jarvis cannot hear the speakers, and says so in the message.
Errors reported as errors: engine missing, voice not installed, speaker muted in Jarvis, start failure, non-zero exit code, 30-second timeout. Starting a preview stops whatever Jarvis was saying (one audio output).

## 4. Speech input
Local recognition: Vosk (offline). Microphone: `sounddevice`/PortAudio. Both are optional (`pip install .[voice]`) and not installed in this sandbox. The page can: choose a microphone, run the read-only check, switch wake phrase / push-to-talk, hold-to-talk, edit the wake phrase (two or more words), choose the model folder, turn hands-free on/off, mute, cancel.
**Listening never starts by itself**: `input_mode` is `off` by default; the check, the settings editor and the mode switch cannot turn it on (`voice_switch_mode` refuses while off); only "Turn hands-free ON" (or an earlier saved ON) does. The state, microphone open/closed indicator and reasons are shown on the page.

## 5. Dependencies, size, licence, internet
| Item | Needed for | Size / licence / internet |
|---|---|---|
| Windows SAPI (System.Speech via PowerShell) | speech output | Part of Windows. Nothing bundled or downloaded. Voices carry Microsoft's own licences. Offline. |
| `sounddevice` (PortAudio) | microphone | Already an optional extra; not installed here. Offline. |
| `vosk` + a model folder you download | speech recognition | Publisher's model page (alphacephei.com/vosk/models, read 2026-10-27): small US English model listed at **40M**, **Apache 2.0**; other models vary (some GPL, AGPL, non-commercial). The library's own licence was not stated on that page. Download is by you, never automatic. Offline once downloaded. |
No paid API, subscription, account or cloud fallback exists in the voice code (a static test scans for network modules and cloud vendor names). No large download was made.

## 6. Test results (see the final report for counts)
`tests/test_p45_voice_system.py` (73 tests): discovery and categories, saved choice and restart, per-mode override, fallback, preview success/failure/timeout/stop/muted/stale, speech output errors and cleanup, microphone and recogniser checks, listening-never-auto-starts, start/mute/stop/cancel through the real controller with fake audio, unrelated data unchanged (hashes), no network / no cloud, window wiring against stand-in Qt.
Mutation check: 24 deliberate breakages of the new rules. 20 were caught at once. Of the 4 survivors, 2 exposed real test gaps (gender mapping, stale-preview guard; tests added, mutants then caught), 1 had been aimed at the wrong line (re-aimed, caught) and 1 is an equivalent mutant (the old voice-name argument to the speaker is ignored by design, so changing it changes nothing).

## 7. NOT verified (needs real hardware / Windows)
Any real voice listing, real audio from a speaker, voice quality, whether Windows lists feminine/masculine voices as expected, SAPI exit codes on a failed voice, Vosk loading and accuracy, wake-word false accepts/rejects, VAD thresholds in real rooms (`vad_min_rms` default is untuned), microphone permission prompts, the mic-in-use icon, echo/barge-in behaviour, hold-to-talk button feel, the Voice page layout and threading under PySide6, the packaged EXE.
Windows checklist: open Voice page -> voices appear grouped; pick one per group, Preview, Stop mid-sample, Apply, restart Jarvis and confirm the voice persists; unplug/disable the audio output and confirm an error not a success; remove a voice's language pack and confirm the fallback message; set a mode's own voice and switch modes; check the microphone (nothing recorded); turn hands-free on, say "Hey Jarvis", hold-to-talk, mute, cancel.
