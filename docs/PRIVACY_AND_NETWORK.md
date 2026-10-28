# Privacy and network behavior

**Local inference makes no network connections.** All network access in the code base goes through one module,
`jarvis/net/policy.py`. Every online feature is **off by default**, can be toggled with `jarvis privacy enable|disable <feature>`,
and is recorded in an in-memory audit log (feature, host, time, bytes sent; never the content).

| Feature | Purpose | Default | Status |
|---|---|---|---|
| model_download | fetch a model package you chose; also the one-time read of the model host's public metadata by the **System & Model Readiness** page (huggingface.co API only, no model file; asked first, switched on for that action only) | off (enable per action) | Implemented |
| update_check | fetch a signed version manifest; never installs | off | Implemented |
| web_research | Prompt 7 `web_fetch`/`web_search` tools: public https pages, Wikipedia, or your own SearXNG; each request is asked | off | Implemented; sends only the URL/search words you approve (secrets and passages of documents you read are refused); private addresses refused |
| web_search | web lookups for answers | off | **Not implemented**; cannot be enabled |
| telemetry | anonymous diagnostics/usage counts to the developer's server | **off** until you read the notice and type `I agree` (`jarvis telemetry enable`) | Implemented. Allowlisted schema, no free text, no chats/files; see TELEMETRY.md. The build must contain a server address or nothing can be enabled |
| crash_reporting | send crash reports | off | **Not implemented**; cannot be enabled |
| online_assistant | talk to a Jarvis Online server YOU named; sign-in required; messages are sent to that server | off until `online connect` + typed yes | Implemented; see ONLINE.md (server operator's terms apply, training use of chats is OFF unless you type `I consent`; guest mode blocks it entirely) |
| account_sync | sync memory to a cloud account | off | **Not implemented**; cannot be enabled |
| cloud_ai | optional hosted AI fallback | off | **Not implemented**; no provider code exists. Enabling in code would require explicit acknowledgement that a provider may bill you |

## Prompt 8 local features (no network, no internet feature)
| Feature | What is read | Where it goes | Controls |
|---|---|---|---|
| Screen capture | One window (or the screen) per request you approve; OCR text and accessibility elements | Held in memory for a few minutes (5 captures); the picture is deleted right after OCR; never written to the audit log or task history; never sent anywhere; text recently read from the screen is also refused as web-search / URL content | `jarvis desktop enable screen` (notice + `yes`); asked on every capture; sign-in, password, security, password-manager and anti-cheat windows refused; secret-looking lines withheld |
| Monitoring | CPU, memory, disk, GPU/temperature (if a source exists), process names and Jarvis' own usage, only when asked | Returned to the model as untrusted data; nothing stored except your settings file `monitoring.json` | `jarvis desktop enable monitoring` (notice + consent); `jarvis monitor config`; no background polling |
| Gaming mode | Running program names (fixed game list), whole-PC readings, FPS log files you recorded elsewhere, your profile notes | Profiles in your profile folder (`game_profiles/`); nothing sent | `jarvis desktop enable gaming` (notice + consent); `jarvis gaming config`; no input, no game memory, no anti-cheat contact |
| Modes | Nothing | `mode.json` in your profile folder | `jarvis mode set|clear`; never grants permissions |

Rules enforced in code: HTTPS only (HTTP only to localhost in tests); redirects are re-checked; a corrupted settings file leaves everything off;
prompts, files, screenshots, voice and memory are never sent anywhere.

## Prompt 9 local features (no network, no internet feature)
| Feature | What is stored | Where | Controls |
|---|---|---|---|
| Missions | The plan you approved (goal, steps, arguments), states and timestamps, and per step a short redacted message plus the tool's evidence line and check results. **Not** stored: file contents, screen/OCR text, web pages or other untrusted text returned by tools. Secret-looking text is redacted before it is written. | `<profile>\missions\<id>.json` | `jarvis mission show|history|export`; `jarvis mission history ID --delete`; `jarvis mission delete ID`; `jarvis memory forget-all --yes` removes all of it |
| Mission history | Append-only log of starts, ends, approvals and schedule events (redacted) | `<profile>\missions\history.jsonl` | `jarvis mission history [ID] [--delete]` |
| Schedules | Kind, time zone, next-run cursor, run counts, the schedule's own approval (plan hash, expiry, run limit) and a claim record per occurrence (prevents duplicate runs) | `<profile>\missions\schedules.json` | `jarvis schedule list|show|pause|resume|cancel|delete`; removed by `forget-all` |
| Remembered information used for planning | Only if you pass `--use-memory`: up to 5 matching memories are shown to the local model and stored with the draft so you can inspect and remove them | inside the mission file | `jarvis mission context ID [--add|--remove N]` |

Missions and schedules never contact a network, never enable a permission and are never used to train or retrain any model. The mission package contains no network or process-launching code (static test). Scheduled missions run only while Jarvis is open and checking. Missions are switched off in guest mode.

## Prompt 10 desktop window and diagnostics (no network, no new internet feature)
The window (`jarvis-gui`) makes no network request of its own; it calls the same code as the command line and adds no cloud service. It cannot change permissions, and every confirmation defaults to "Don't allow".
It stores only your text-size choice, in the Windows per-user registry (Qt `QSettings`, `HKEY_CURRENT_USER\Software\ProjectJarvis\Window`; never conversation text). The uninstaller does not remove that one value; delete the key by hand if you want it gone. The theme follows the Windows light/dark setting. `jarvis diagnostics` and the Status page build a report from local facts only (versions, dependency presence, model state, log tail) with your home folder and user name replaced and secret-looking text removed; conversations, memory and file contents are never included, and nothing is uploaded: you copy it yourself if you choose.
The installer and the app create no startup entry, service or scheduled task. Schedules run only while a Jarvis window is open and its "run due schedules" switch is on.

## Stored copies of what Jarvis read or ran (Prompt 11 audit)
Besides the memory database, a profile folder (`%LOCALAPPDATA%\ProjectJarvis`, or `profiles\<name>` inside it) can hold copies of your material. Each is local only and none is encrypted:
| Folder | What it holds | Removed by `jarvis memory forget-all` |
|---|---|---|
| `research_sources` | the full text of every document or web page Jarvis read for research (up to 2 million characters each) | yes (before Prompt 11 it was **not**) |
| `code_snapshots` | copies of project files taken for diffs | yes (before Prompt 11 it was **not**) |
| `code_runs` | output of code and tests Jarvis ran | yes (before Prompt 11 it was **not**) |
| `screenshots`, `desktop_audit*.jsonl`, `desktop_tasks.jsonl`, `app_index.json`, `missions` | screenshots you asked to keep, action logs, task history, app index, missions and schedules | yes |
| `code_backups`, `desktop_checkpoints` | full copies of files as they were before Jarvis changed them (these are your **undo points**) | no by default; yes with `--include-undo-points` (you can then no longer undo those changes) |
Windows' own file history, backups you made, and exports you saved yourself are not tracked. The window's "Forget everything" uses the same function.

## What has been verified (and what has not)
Verified by automated tests in this repository:
- No module outside `net/policy.py` imports a network library (static scan of every source file).
- The source contains no API-key, hosted-provider, or quota logic (static scan).
- Core paths that can run without PyTorch (tokenizer, tools, memory, benchmarks, gate, data pipeline) run with sockets and DNS made fatal and no `*API_KEY*` environment variables, without any attempt to connect.
- A disabled feature raises before any socket is touched.

NOT verified:
- The PyTorch inference path was never executed, so it has not been run under a network block. Our own `model/` and `inference/` code has no network imports, but PyTorch and the Python runtime are third-party code that was not audited.
- Behavior on Windows (firewall prompts, real HTTPS downloads) has not been tested.
- To check on your own PC: run the app with Windows Firewall set to block `jarvis.exe` outbound, or in Windows Sandbox with networking disabled (`packaging/clean_install_test.wsb`).

## Data on disk (all under %LOCALAPPDATA%\ProjectJarvis)
`memory.sqlite` (conversations, notes, action log, **unencrypted**), `models\`, `backups\`, `settings.json`, `logs\`.
Export or delete everything via the memory store API (`export_all`, `delete_all`); uninstall prompts before deleting this folder.


## Personal memory
Saved memories, preferences and optional summaries are stored in a local SQLite file per profile (see `docs/MEMORY.md`), are never sent anywhere, are not used for training, and the file is not encrypted (Windows file permissions only). The memory package contains no network code (static test).

## Data-flow audit (Prompt 6)
| Data | Leaves this PC? | Notes |
|---|---|---|
| Local personal memory, notes, summaries, personality, voice settings | **No**, ever | Static test: the memory package and the online client import no network code and no memory code respectively |
| Local chat (`jarvis chat`), `--guest` | No | Guest also refuses `online`, `backup` and `restore` |
| Online chat messages | Only to the server you named, after sign-in | Needed to answer you; the server keeps history until you run `delete-chats` / `delete-account` |
| Use of those messages for **training** | Only with your explicit `I consent`; OFF by default | Separate flag per message; consent is prospective and tied to the exact notice text |
| Telemetry | Only if you enabled it; allowlisted fields only | No free text; secret-looking values refused; skipped in guest mode |
| Desktop control (files, apps, UI, screenshots, OCR) | No | No network tool exists; screenshots, audit log and task history stay in the profile folder |
| Crash log | Never sent | Local file; secrets best-effort redacted; may contain paths with your Windows user name |

Limits: secret detection is best effort; secure erasure is not guaranteed on SSDs or all filesystems; nothing here was verified on Windows.

## Personality styles (Prompt 15)
`persona.json` holds style settings only and is never sent anywhere; style text goes only to the local model. Details and limits: `docs/PERSONALITY.md`.

### Prompt 17: hands-free microphone (opt-in)
* **Off by default.** `input_mode` is `off` until you turn hands-free ON on the Voice page or with `jarvis voice set input_mode wake_word`. A guest session never starts voice.
* **Visible.** The Voice page always states whether the microphone is OPEN or CLOSED, measured from the audio stream, and Windows' own microphone indicator shows while the stream is open. Jarvis does not bypass Windows microphone permission; if Windows blocks it, Jarvis reports Unavailable.
* **What is processed.** Short pieces of audio are held in memory only long enough for the local wake detector and the local recogniser (Vosk) to turn them into text. **Nothing is retained**: no audio is written to disk (tested by scanning the voice code and the profile folder), none is sent over the network (tested with sockets blocked), and no cloud recogniser exists. Recognised text is treated like typed text; it follows your memory and privacy settings.
* **Mute and disable.** Microphone mute closes the stream and is saved (no automatic unmute after a restart). Turning hands-free OFF closes it and stays off. Speaker mute only silences speech.
* **Windows-login startup** is separate, off by default, needs your explicit confirmation, writes one visible per-user entry, and can be removed here or in Windows Startup apps. It does not start the microphone.
* **Not guaranteed.** Wake detection can mishear (false wakes) and a speech model can mis-transcribe; spoken words get no extra trust: they cannot approve an action or change a permission.

### Prompt 16 additions
Reading replies aloud, the readiness table, `/normal` and `jarvis persona selfcheck` make no network request (tested with sockets and DNS blocked). (Prompt 17: the window can now open the microphone, but only after you turn hands-free voice ON; see below.) The self-check writes the real model's replies to `persona_validation.json` in your data folder (local, plain text, delete it any time); it runs as a guest session so your memories are not read.

