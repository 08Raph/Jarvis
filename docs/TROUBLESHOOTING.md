# Troubleshooting

First step for anything: `jarvis diagnostics` (or the Status page). It prints versions, model state, dependencies and the log tail with your user
name and home folder replaced and secrets removed. It makes no network request.

| Symptom | Likely cause | What to do |
|---|---|---|
| Window does not open, message about PySide6 | Running from source without the UI extra | `pip install -e ".[ui]"`; the official build includes it |
| "Project JARVIS is already running" | Another window uses this profile | Switch to it. If none is open, a stale lock is detected and replaced automatically |
| "Data folder cannot be used" | Folder is read-only, a file, or inside the program folder | Pick another with `--data-dir`, or fix permissions |
| Chat is disabled, status `no_model` | No model installed | `docs/MODEL_DISTRIBUTION.md` |
| Status `runtime_missing` | `llama-cpp-python` not in this build | `docs/LOCAL_MODEL.md` |
| Status `model_invalid` | Damaged download | `jarvis models verify <id>`, then fetch again |
| Closing the window says work is still running | A tool call cannot be interrupted safely | Wait, or use Emergency stop on the Safety page, then close |
| An action is refused | Policy: the permission category is off | Intended. Change with `jarvis desktop enable <category>` after reading `docs/DESKTOP_CONTROL.md` |
| Schedule did not fire | Jarvis was not open and checking | Schedules only run while it is checking; `jarvis schedule run-due` runs missed ones |
| SmartScreen warning on the installer | Build is not code-signed | Compare the SHA-256 in `SHA256SUMS.txt` with the one published |
| Window crashed at start | See `crash.log` in the data folder | Include the redacted `jarvis diagnostics` in a bug report |

Your data is in `%LOCALAPPDATA%\ProjectJarvis`. Back it up with `jarvis backup`.
| Banner says the model file and runtime were found but "not confirmed" | `jarvis selftest` has not passed for this model on this PC | Run `jarvis selftest`; see the Status page readiness table |
| Savage/Flirty/Romantic locked or switched off by themselves | Adult opt-in is off, or a message said you are under 18 / made your age uncertain | Tick the adult checkbox (or `jarvis persona adult on --confirm-adult`) if you are an adult |
| A style keeps stopping | A keyword stop check fired (e.g. "too much") | Use the style again; `docs/PERSONALITY.md` lists known false positives |
| `/normal` seems to do nothing | The message must be exactly `/normal` with nothing else | Send only `/normal`, or click Return to Normal |
| "Speech output failed" | Windows speech engine or PowerShell problem | Turn off "Read replies aloud"; run `jarvis voice voices` in a console to see the error |
| Microphone library "not found" on the Status page | sounddevice/PortAudio missing | `pip install sounddevice`; `docs/VOICE.md`. Hands-free voice shows Unavailable with this reason until it is fixed |
| Voice page says Unavailable: no Vosk model | `vosk_model` is not set | Download a Vosk model yourself, then `jarvis voice set vosk_model FOLDER` and press Retry |
| Voice page says Unavailable: no microphone / blocked | No input device, or Windows microphone privacy is off for desktop apps | Plug in/enable a microphone; turn the privacy setting on yourself; press Retry. Jarvis never bypasses it |
| "Another Jarvis process is already using the microphone" | `jarvis voice run` or a second window holds `mic.lock` | Close it, or delete a stale `mic.lock` only if no Jarvis is running; Retry |
| Jarvis answers itself or repeats | Speaker echo into the microphone | Use headphones; keep barge-in on `wake_word_only`; raise `post_speech_guard_seconds` (0-3) in `voice.json` |
| It says Listening but never wakes | Wake phrase misheard | Say it clearly; try `jarvis voice set sensitivity 0.7`; check the microphone in Windows Sound settings |
| Spoken "stop" does nothing while "Processing" | Audio is discarded while the model thinks | Use the Stop button or Mute |
| Voice stays muted after restart | Mute is saved on purpose | Click Unmute on the Voice page |

