# Voice and personality (Prompt 4)

## Honest status

| Piece | State |
|---|---|
| Voice settings (validation, persistence, safe defaults) | Implemented, **tested** |
| Wake-phrase matching (fuzzy, sensitivity, start-of-utterance only) | Implemented, **tested on text** |
| Session state machine (wake, capture, speak, follow-up, barge-in, mute, sleep, push-to-talk, mic-failure backoff) | Implemented, **tested with fake mic/recognizer/speaker** |
| Speech filter (logs, tracebacks, code, paths, URLs, long identifiers never spoken), pronunciation dictionary, response length | Implemented, **tested** |
| Personality engine (7 traits, 5 modes, template layer, approval flow, invariants) | Implemented, **tested** |
| `SoundDeviceMic`, `VoskRecognizer`, `SapiSpeaker` | Written; argument handling **tested with fakes only**. **Never run against a real microphone, Vosk, or Windows speech** |
| Wake-word accuracy, false-accept/false-reject rates, latency, VAD quality in real rooms, barge-in/echo behaviour | **Not measured** |
| Console keys in `voice run` (m mute, p push-to-talk, s sleep, w wake, c cancel, q quit) and a plain-text microphone indicator (`[MIC OPEN]` / `[MIC CLOSED]`) | Implemented; logic **tested with fakes**. The Windows key reader (`msvcrt`) has **never run** |
| Desktop Voice page (voice choice by type, Preview/Stop/Apply, microphone check, wake phrase, hold-to-talk, mute) | Built (Stage 6); **never run with PySide6 or on Windows**. A global hotkey for push-to-talk is not built |
| Kokoro local neural voice (second speech engine, Stage 6.5): engine selector, four voice ids, consented model install, preview phases | Built; **tested with fakes only**; the real model, real audio, PySide6 and Windows were **never** used. See `docs/STAGE6_5_KOKORO.md` |
| A trained language model behind the replies | Does not exist; `voice run` answers honestly that no model is installed |

## How it works

- **Input**: `sounddevice` (16 kHz mono) -> frames -> `VoiceSession`. Wake detection uses a Vosk recogniser restricted to a small
  grammar of your wake phrases, then fuzzy-matches the text (`sensitivity` 0 = exact, 1 = lenient). Default phrase "hey jarvis";
  alternatives are configurable. A phrase must have at least two words and must start the utterance.
- **Capture** ends on silence (energy VAD), on a maximum length, or on recogniser finalisation. Push-to-talk ends on key release.
- **Output**: Windows built-in speech (System.Speech) via PowerShell. Text is sent on stdin and settings via environment variables,
  never in the command line.
- **Follow-up window**: after a reply, speech is accepted without the wake word for `follow_up_seconds` (default 8).
- **Interruption**: `stop` / `cancel` / `never mind` while speaking silences immediately (kills the speech process). Barge-in modes:
  `off`, `wake_word_only` (default), `speech`. There is **no echo cancellation**, so `speech` mode can make Jarvis interrupt itself
  on speakers; use headphones if you enable it.
- **Do not speak over the user**: if you are talking when a reply is ready, the reply is dropped and your speech is captured instead.
- **Never read aloud**: log lines, tracebacks, code blocks, file paths, URLs, long hex/identifier strings. Errors from the assistant
  are replaced by a generic spoken message; details stay in the log.
- **Modes**: `voice_and_text`, `voice_only`, `text_only`; response length `brief` / `normal` / `detailed`; rate 0.5-2.0; volume 0-1;
  voice selection (`jarvis voice voices`); pronunciation dictionary (`jarvis voice pron-add SQL sequel`).

## Controls while `jarvis voice run --vosk-model <folder>` is running

| Key | Action |
|---|---|
| `m` | Mute / unmute. Muting closes the microphone stream; the line shows `[MIC CLOSED] muted`. Also blocks push-to-talk |
| `p` | Push-to-talk: press to start talking, press again to send (a console cannot see key release) |
| `s` / `w` | Sleep (stream closed) / wake |
| `c` | Cancel: stop speaking immediately |
| `h`, `q` | Help, quit (stream closed on exit) |

Limit: while the assistant is *working on a reply* (THINKING) the loop is busy, so a key press is applied only when the reply is ready; the
indicator says so. Saying "stop" or pressing `c` interrupts speech right away. A global hotkey and an on-screen indicator need the (not yet built) desktop UI.

## Privacy and microphone visibility

- Default `input_mode` is **off**: nothing opens the microphone until you run `jarvis voice set input_mode wake_word` (or `push_to_talk`)
  and start a session.
- The microphone stream is open only in LISTENING, CAPTURING, THINKING, SPEAKING and FOLLOW_UP. **Mute and sleep close the stream**
  (the Windows microphone-in-use icon should disappear; that is expected behaviour but unverified here). Muting also blocks push-to-talk.
- Every state change emits an event with `mic_open`; `voice run` prints them. A UI must show them (not built yet).
- No audio is recorded to disk and nothing is sent anywhere. Recognition and synthesis are local; the voice package imports no network
  code (enforced by the static scan in `tests/test_offline_guarantees.py`). Downloading a speech model is **never automatic**.
- Mic failure: the session closes the stream, silences speech, and retries after 1, 2, 5, 10, 30 s, then stops in an ERROR state with a plain message.
- Voice "sleep" closes the mic, so it cannot be woken by voice; use `wake` (button/CLI) or push-to-talk.

## Voice licences (read before packaging anything)

Nothing in this repository bundles a speech model or voice.

- **Windows voices** (System.Speech): use what the user's Windows already has. They are not redistributed by Jarvis. Good for dev and personal use.
- **Vosk library**: Apache-2.0. **Vosk models**: each has its own licence; `vosk-model-small-en-us` was listed as Apache-2.0 on a mirror
  page, which still has to be confirmed against the original source before you ship it. Not bundled.
- **openWakeWord**: the pretrained `hey_jarvis` model is CC BY-NC-SA 4.0 (non-commercial, share-alike). **Do not bundle or default to it** in a
  distributable product. Training your own wake model on data you have rights to is the clean path.
- **Piper / other neural voices**: licence is per voice. Check the specific voice's model card and training-data terms before redistributing.
- **Name**: "Jarvis" is associated with a fictional character and a trademarked franchise. This project does not use that character's voice,
  lines, or likeness and does not claim to be that character. Consider a different product name/default wake phrase before commercial release;
  that is a legal question for counsel.

## Voice style

The default phrasing is original, calm, formal British-English-inspired wording ("Certainly.", "Right away.", "Shall I go further?").
It does not imitate any actor or character, uses no catchphrases or "sir", and the assistant never claims to be a fictional character.
The default Windows voice is whatever the machine provides; choose a British English voice if installed.

## Personality

Seven traits (0-1): formality, humor, warmth, directness, verbosity, initiative, technical_depth. Five modes: Professional JARVIS (default),
Friendly Companion, Technical Engineer, Concise Operator, Humorous Assistant. `jarvis personality set-mode|set-trait|show|instructions`.

- It is a **template layer** around reply text (an acknowledgement before, an offer or light remark after). It never rewrites the content,
  so facts and numbers are untouched. Concise Operator adds nothing.
- **Critical messages are never styled**: errors, refusals, permission requests, security and privacy notices, uncertainty statements.
- `behavior_instructions()` ends with fixed invariants (style never overrides privacy/security/permissions/truthfulness; no claimed
  feelings; no claiming to be a character; state uncertainty). No trait value can remove them.
- No human feelings are claimed; "glad"/"pleased" remain courtesy phrases and tests check for first-person feeling claims.
- **No silent adaptation**: the system may `propose` a change with a reason, which sits in a pending list and changes nothing until you
  `approve` it. Model weights are never modified (the personality package imports no model, memory, network, or numeric code; tested).
- Humour is a small fixed set of light lines, only on ordinary answers.

## Commands

    python -m jarvis voice show | set KEY VALUE | devices | voices | say "text" | pron-list | pron-add W SAY | pron-remove W
    python -m jarvis voice run --vosk-model C:\path\to\vosk-model     # live session (pip install sounddevice vosk; Windows)
    python -m jarvis personality show | set-mode MODE | set-trait T V | pending | approve ID | reject ID | instructions

## Test this on Windows 11 (none of this has been done)

1. `pip install -e ".[voice]"`; download a Vosk English model yourself (check its licence); unzip it.
2. `python -m jarvis voice devices` and `voice voices`: confirm your mic and a voice appear.
3. `python -m jarvis voice say "Certainly. Voice output is working."`
4. `python -m jarvis voice set input_mode wake_word`, then `voice run --vosk-model <folder>`; say "hey jarvis", then a sentence.
   Record: did the state sequence LISTENING -> CAPTURING -> reply happen, how often it falsely wakes over 10 minutes of normal talk, how often it misses.
5. Say "stop" mid-reply; unplug the mic during a session; mute; confirm the Windows mic indicator goes off when muted.

## Personality styles and speech rate (Prompt 15)
The voice session accepts an optional `rate_factor` hint (clamped to 0.5-2.0 after multiplying by your rate; a failing hint is ignored). `jarvis voice run` passes the active style's rate (Calm 0.8, Concise 1.1). Tested with fake audio only. See `docs/PERSONALITY.md`.

## Prompt 17: automatic, always-listening hands-free voice in the window
**What it is.** The window (Voice page) can listen for "Hey Jarvis" all the time while it is open, answer by voice, and go back to listening, with no button per turn. It reuses the existing parts (EnergyVAD, Vosk wake and dictation recognizers, `VoiceSession`, SAPI speaker); one background thread owns the microphone, the recognizer and the speaker, so there is never a second listener.

**Separate capabilities (each reported on its own):**
| Capability | What it means |
|---|---|
| App-start voice initialization | When the window opens and you earlier turned hands-free ON, voice starts by itself (model loads on a worker thread, window stays responsive). If you never turned it ON, the microphone is not touched. |
| Windows-login startup | Optional, separate, off by default: starts only the window at sign-in (`jarvis startup show/enable --confirm/disable`, or the checkbox). It never turns the microphone on. |
| Microphone loop | Open stream, frames read continuously, closed on mute/disable/exit. |
| Wake detection | Vosk with a grammar limited to your wake phrases; fuzzy matching in `voice/wake.py`. |
| Local recognition | Vosk dictation after the wake phrase. |
| Model answer | The same local model/assistant as typed chat. |
| Speaking | Windows SAPI via PowerShell, local. |
| Barge-in | Default `wake_word_only`; `speech` mode is opt-in because there is no echo cancellation. |

**States (measured, not assumed):** Off, Starting, Listening, Processing, Speaking, Muted, Unavailable. "Listening" is shown only while the worker thread is alive, the microphone stream is open and the recognizer is loaded. If the microphone is unplugged, the model folder is wrong, PortAudio/Vosk is missing or another process holds the microphone, the state is Unavailable (or Starting while it retries) with the real reason and a **Retry** button.

**Protections.** Identical text twice within 2 s is processed once. Audio right after Jarvis speaks is ignored for 0.6 s (`post_speech_guard_seconds`). A transcript that is mostly Jarvis's own last reply is ignored. A wake phrase that appears inside Jarvis's own reply does not interrupt it or restart it. A reply that arrives after you muted, cancelled or disabled is dropped. If you start talking while the model is thinking, the answer is not spoken over you. These are tested with fake audio; real speakers and rooms may still cause echo.

**Mute and disable.** *Mute microphone* closes the stream (the Windows microphone indicator goes off) and is saved, so a restart does not unmute. *Mute speaker* is separate: Jarvis keeps listening and shows text. *Turn hands-free OFF* is saved and releases the microphone, thread and lock (`mic.lock`). `jarvis voice run` and the window use the same lock, so only one can own the microphone.

**Honest limits.** While the model is thinking, microphone audio is read and discarded: a spoken "stop" is not understood then (use Stop or Mute). Cancelling speech stops the speaker; cancelling a reply stops generation at its next piece; these are separate operations. The wake detector keeps no audio. No audio is saved or sent anywhere.

**Set up (Windows).** `pip install sounddevice vosk`; download a small English Vosk model yourself (Jarvis never downloads one), unzip it; `jarvis voice set vosk_model C:\path\to\model`; open the window, Voice page, "Turn hands-free ON". Never verified on real audio hardware: follow `docs/WINDOWS_TEST_CHECKLIST.md` section 6q.

## Home screen (HUD) and the always-on indicator
The window now opens on a **Home** page: a glowing core with rings, particles and a waveform, a large **MIC ON / MIC OFF** chip, panels (current activity, quick info, recent conversation, local-AI/privacy, resources), and the controls **Turn hands-free ON/OFF**, **Mute microphone** (also `Ctrl+Shift+M`) and **Stop speaking**. Code: `ui/hud_model.py` (states and animation maths, no Qt), `ui/hud.py` (the painter), `window.py` (`_home_page`).

**Always listening, without a button per turn.** This is the existing Prompt 17 behaviour: once hands-free is ON (one click, saved) the window starts it by itself on every launch (`voice_autostart`) and keeps listening for the wake phrase until you turn it OFF or mute. **First start:** on a profile that has never chosen, the window turns wake-word listening ON by itself (`listening_choice_made` in `voice.json` records any later choice). The microphone is opened only after a sound device exists and the Vosk model you installed has loaded; with none, nothing is opened and the HUD says so ("VOICE ERROR" / "MICROPHONE UNAVAILABLE" + the reason). Turning it OFF (or muting) is remembered and never overridden. Guest sessions never turn it on. Nothing is acted on before the wake phrase (the wake recogniser only accepts the wake grammar; after a reply there is a short follow-up window, `follow_up_seconds`, set to 0 to disable); no audio is saved; none leaves the PC.

**What each visual state is driven by (measured, never decorative):** voice off, starting, listening (mic stream open AND recogniser loaded), wake heard (a ~1.2 s flash after the wake event), hearing you, follow-up, thinking (the assistant is producing a reply, typed or spoken), speaking (the speaker reports it is speaking), muted, microphone unavailable (no device, in use, or the stream failed; retrying), voice error (e.g. missing model). The waveform follows the **real microphone level** only while the stream is open and listening; while thinking or speaking it is a pattern captioned "animation only - not audio" (Windows speech and Kokoro expose no playback amplitude); with the microphone closed it is flat. Animation pauses when the window is hidden or minimised, runs at ~15 fps when quiet and ~30 fps when active; `JARVIS_REDUCE_MOTION=1` slows it to 2 fps.

**Not verified:** none of this has been looked at on a real screen, with a real microphone or speaker, or on Windows 11. Tests: `tests/test_p70_hud.py` (real code with fake audio and stand-in widgets) and `tests/test_p71_hud_qt.py` (real Qt, offscreen, only where PySide6 is installed).

## Prompt 16: window speech, saved Vosk folder, `n` key
`speak_in_window` (default false) lets the window read replies aloud through the same SAPI speaker; `vosk_model` stores your Vosk folder so `jarvis voice run` needs no flag. Console key `n` silences speech and returns to the Normal style. Still never run on real audio hardware.


## Choosing a voice (Stage 6)

Open **Voice**. Voices are listed by what Windows reports, grouped **Feminine / Masculine / Not specified** (the engine's own gender field; a voice that does not say is
never guessed). Each shows name, language/accent (from its locale code) and engine. **Preview Voice** plays a short fixed sentence (it says "finished" only when the
engine reports it ran to the end; it cannot hear your speakers), **Stop** silences it, **Apply** saves it. The choice survives restarts and is independent of the
personality; tick "only for the current personality mode" to give one mode its own voice, and use the button beside it to go back to your default. If a saved voice is no longer installed Jarvis uses
the engine's default voice and tells you. Voices come from Windows' classic speech list; add voices in Windows Settings > Time & language > Speech.
The same page checks your microphone and speech recogniser **without opening the microphone**, lets you pick the microphone, switch wake phrase / push-to-talk, change the wake phrase and choose the Vosk model folder.
Details, sizes, licences and what is unverified: `docs/STAGE6_VOICE_SYSTEM.md` (development repository).

## Kokoro neural voice (Stage 6.5, optional)
A second speech engine next to Windows speech. It needs `pip install kokoro-onnx sounddevice` and a model download of about 338 MB that starts only after you confirm on
the Voice page; afterwards it works offline with no account, key or fee. It only speaks: the microphone, wake phrase and Vosk are not touched. If Kokoro cannot be
used, Jarvis stays silent and offers Install / repair or Use Windows speech; it never quietly speaks with another engine. Details, licences (espeak-ng is GPL, so nothing is
bundled), install steps and what is NOT verified: `docs/STAGE6_5_KOKORO.md`.
