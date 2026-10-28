# Stage 6.5: Local Kokoro neural voice (2026-10-27; Linux sandbox only)

**Status: implemented and tested with FAKES. The real Kokoro model was never downloaded, loaded or run. No real audio device was used and nothing was heard.
Nothing was run on Windows and PySide6 was never imported.** The four voices are selectable only if the installed voices file lists them; whether they sound
good, natural or right for you is **unknown until you listen**. Stage 5's real-model and Windows blockers are NOT resolved by this stage.

## 0. What is verified and what is not
| Item | State |
|---|---|
| Kokoro registered as a second engine in the Stage 6 registry, next to Windows SAPI (SAPI unchanged) | Implemented, **tested** (fakes) |
| Four voice ids `af_heart`, `af_aoede`, `af_nicole`, `am_onyx` offered when the installed voices file lists them; more ids configurable (`voice_ids=`) | Implemented, **tested** against synthetic voices files |
| Engine + voice choice saved in `voice.json`, restored in a new session, switchable without restarting Jarvis, independent of personality, per-personality override kept | Implemented, **tested** |
| Unavailable Kokoro / missing voice: explicit "unavailable" state, nothing spoken, two recovery choices; never a silent SAPI substitution | Implemented, **tested** |
| Preview with phases loading -> generating -> playing -> finished / stopped / error; synthesis and playback reported separately; Stop; cancel; cleanup | Implemented, **tested** (fake model, fake audio output) |
| Model install: consent, size shown, progress, cancel, partial-file cleanup, size/structure checks, no re-download of a valid install | Implemented, **tested** with a fake network |
| Download URLs of the two model files | **Not opened** (no route to the host from the sandbox). Taken from the kokoro-onnx project's documentation |
| Real Kokoro inference, real model loading, real file sizes/hashes | **NOT run / NOT verified** (`kokoro-onnx` is not installed in the sandbox; `onnxruntime` 1.29.0 happens to be installed and lists `AzureExecutionProvider` and `CPUExecutionProvider`, which is why the CPU provider is requested explicitly) |
| Real audio playback (`sounddevice`/PortAudio) | **NOT tested** |
| Voice page with real PySide6 | **NOT tested** (stand-in Qt modules + source reading only) |
| Windows | **NOT tested** |
| Latency, real-time factor, memory use, CPU load, voice quality | **NOT measured** (the app measures and shows model-load and time-to-first-audio on each real run; nothing is promised) |

## 1. Assessment made before changing anything
The Stage 6 abstraction fit without a parallel subsystem: `TTSEngine` / `EngineRegistry` (engines.py), `VoiceManager` (discovery, saved choice, resolution at the
moment of speaking), `VoicePreview` (worker thread, generation guard), `ResolvingSpeaker` (the one speaker the rest of the app uses), voice ids saved as
`engine:name`. Kokoro is one more engine class. The existing hands-free service, SAPI, Vosk and microphone code were not edited.

Decisions:
* **Backend: `kokoro-onnx`** (ONNX Runtime, CPU). Chosen because it is small, runs locally and offline after install and has a documented Python API. Not chosen: the PyTorch `kokoro` package (heavy), hosted demos/APIs (not local, would break the local-first rules).
* **Nothing is bundled.** The runtime (`pip install kokoro-onnx sounddevice`, optional extra `kokoro`) is installed by the user; the model files are downloaded from the Voice page only after a confirmation. Reason: licences (section 3) and size.
* **No silent fallback.** A Kokoro voice that cannot be used is not replaced by another engine (`fallback_ok = False` on the engine). SAPI keeps its Stage 6 behaviour (default voice, with a visible note).
* **Model store lives in `jarvis/models/`**, not `jarvis/voice/`: an existing privacy guard (`test_no_raw_audio_is_written_anywhere`) forbids any file writing inside `voice/`, and that guard was kept strict instead of weakened.

## 2. Files
Added:
* `src/jarvis/voice/kokoro_engine.py` : `KokoroEngine`, `KokoroSpeaker` (worker thread + synthesis thread + bounded queue), `KokoroRuntime` (the single loaded model), `SoundDeviceSink` (output-only audio), `split_text`.
* `src/jarvis/models/kokoro_files.py` : file table, state checks (missing / incomplete / damaged / ready), consented cancellable download, local hash record, deep verify.
* `tests/test_p46_kokoro.py` : 97 tests (all fakes, see its header).
* `docs/STAGE6_5_KOKORO.md` : this file.

Modified:
* `src/jarvis/voice/engines.py` : `fallback_ok`, `TTSEngine.close()`, `default_registry(platform, kokoro_data_dir)`.
* `src/jarvis/voice/manager.py` : "unavailable" resolution, `select_engine`, engines closed at shutdown, `selected_engine` in the catalog.
* `src/jarvis/voice/preview.py` : `generating` state, per-phase time limit, honest end-of-sample text for engines that report synthesis and playback separately.
* `src/jarvis/voice/viewmodel.py` : engine selector, Kokoro status / confirmation / job / recovery text, per-engine voice lists.
* `src/jarvis/app/controller.py` : `voice_select_engine`, `kokoro_state`, `kokoro_install(consent)`, `kokoro_cancel`, `kokoro_check`.
* `src/jarvis/app/speech.py` : registers Kokoro with the user data folder.
* `src/jarvis/ui/window.py` : Engine selector, "Use Windows speech", Kokoro panel (status, Install / repair, Cancel, Check, progress), recovery text.
* `packaging/manifest.py`, `src/jarvis/app/diagnostics.py` : the two new modules added to both module lists (kept equal). Kokoro runtime and weights are **not** in any bundled list.
* `pyproject.toml` : optional extra `kokoro = ["kokoro-onnx>=0.4.0", "sounddevice>=0.4.6"]` (the lower bound is a minimum guess; not install-tested here).
* `.gitignore` : `*.onnx`, `voices-v*.bin`.
* `tests/test_p45_voice_system.py` : two new window state-attribute names added to an exclusion set (they are not handlers).
* `docs/VOICE.md`, `CHANGELOG.md`, `THIRD_PARTY_NOTICES.md`.

## 3. Implementation and licences (what was actually read, and what was not)
| Component | Licence | Evidence / caveat |
|---|---|---|
| `kokoro-onnx` (PyPI 0.6.1 page; Python >=3.10,<3.14; optional `gpu` extra) | MIT | Read on its PyPI page during this stage. |
| Kokoro-82M model weights | Apache-2.0 | As stated by the model's publisher in the sources read; the licence file itself was not downloaded here. |
| espeak-ng (text-to-phoneme step, installed through kokoro-onnx's dependencies) | GPL-3.0 | **This is why Jarvis does not bundle the runtime.** Whether and how a closed installer could include it is a licence question for you; it is not decided here. |
| `phonemizer-fork` / `espeakng-loader` / `onnxruntime` / `sounddevice` | not verified in this stage | Their licences were NOT individually read here. Do not bundle them without checking. |

Permission to **use** the model locally, permission to **distribute** its weights, and permission to **redistribute** the Python dependencies are three different
questions. This stage relies only on the first: Jarvis ships none of the weights or dependencies, and the app tells you where they come from and under which licences.

Model files (kokoro-onnx "v1.0" release): `kokoro-v1.0.onnx` (about 310 MB; smaller quantised variants exist but are not used) and `voices-v1.0.bin` (about 28 MB).
The sizes in the code are **approximations** from the project's documentation and only warn you and cap a runaway download (115 % of the estimate). **No publisher
SHA-256 was found, so none is compared and none is invented.** Instead: the host's announced length must match what arrived, both sizes must be plausible, the voices
file must be a readable archive listing voices, the model must not be a web page, and a SHA-256 of the received bytes is recorded locally so a later change is
detected ("Check model files"). That detects truncation and later damage; it does **not** prove the file is the publisher's original.

Whether the four requested voices exist in the exact model file: they appear in the voice list the Kokoro project documents for v1.0 (read earlier in this
session), but the real `voices-v1.0.bin` could not be downloaded, so this is **unverified**. The app reads the installed file's index and offers a preset only if it
is really there; a missing one is named on the page and is not selectable.

## 4. Install procedure (for you, on Windows)
1. Install the runtime into the Python environment that runs Jarvis: `pip install kokoro-onnx sounddevice` (or `pip install .[kokoro]`). (`sounddevice` is the same optional package the microphone code already uses.)
2. Open the Voice page. The Kokoro panel says what is missing. Press **Install / repair Kokoro model...**, read the confirmation (size, source, licences) and confirm. Progress is shown; **Cancel download** removes the partial files.
3. Files go to `<Jarvis data folder>\voice_models\kokoro\` (not into any database or settings file). You can also place `kokoro-v1.0.onnx` and `voices-v1.0.bin` there yourself.
4. Choose engine **Kokoro**, pick a voice, **Preview Voice**, then **Apply** to save it.
5. To go back: **Use Windows speech**. Kokoro files stay installed; delete the folder to remove them.

## 5. Behaviour
* **Engine selector** lists every engine with its state. Choosing an engine only changes which voices are listed; nothing is saved until **Apply**. **Use Windows speech** saves the first Windows voice as your default.
* **Voices** shown as the voice id only. The voices file gives no accent, gender or quality, so none is shown and none is guessed from the id.
* **Preview** uses the real engine path: `loading` (model load, first use only) -> `generating` -> `playing` -> `finished`. "Finished" means: Kokoro generated N seconds of audio and **all of it was handed to the output device without an error**, with the measured model-load and first-audio times of that run. It never says the sample was heard.
* **Stop** aborts the output stream at once and cancels pending sentences. A sentence already being computed cannot be interrupted (ONNX Runtime has no cancel); it finishes in the background and is discarded.
* **Threads / devices**: speech job thread + synthesis thread per utterance, both joined at shutdown; the output device is closed on success, failure and cancel; after shutdown nothing can start (`speak()` is refused, a model load still in progress is discarded). Model loading uses its own lock, so the interface never waits for it.
* **One model copy**, kept loaded after first use; unloading is refused while a job is using it.
* **Unavailable / missing**: the page says why (missing packages with the pip command, missing/incomplete/damaged files, a preset missing from the file). If your chosen Kokoro voice cannot be used, replies are **not spoken** and the page offers Install / repair or Use Windows speech. A per-mode Kokoro override that cannot be used falls back to your default voice with a visible note.
* **Hardware**: CPU only. Jarvis builds the model with ONNX Runtime's CPU provider only (`default_model_factory`: its own session when the installed kokoro-onnx has `from_session`, else `ONNX_PROVIDER=CPUExecutionProvider`), so a GPU or cloud provider that is installed is never used. The page shows which providers the installation offers (read-only). This construction was tested with fake `onnxruntime`/`kokoro_onnx` modules only; it has NOT been run against the real `kokoro-onnx`. Out-of-memory and load failures give plain messages and a way back to Windows speech. No speed is promised.
* **Microphone untouched**: the engine and model code never reference the microphone, Vosk, wake phrase or hands-free service (checked by a source-scanning test); selecting, previewing or installing leaves `input_mode` and the microphone alone (tested through the real controller).
* **Local-first**: no API key, account, fee, telemetry or cloud fallback. The only network use is the consented download, through the Stage 5 `NetworkPolicy` (`model_download`, https, allow-listed hosts github.com / githubusercontent.com, granted for that one action and then restored). Generating speech opens no socket (tested by making sockets raise).

## 6. Tests
Commands (from the project folder; the sandbox's pytest lives in a separate tool environment, so its site-packages is put on PYTHONPATH):
```
PYTHONPATH=src:/root/.local/share/uv/tools/pytest/lib/python3.13/site-packages python -m pytest tests/test_p46_kokoro.py -q -p no:cacheprovider
PYTHONPATH=src:/root/.local/share/uv/tools/pytest/lib/python3.13/site-packages python -m pytest tests -q -p no:cacheprovider
```
On a normal machine: `python -m pytest tests/test_p46_kokoro.py` after `pip install -e .[dev]`.

Results: see the final report of this stage (numbers are filled in there from the real runs). Mutation check (scratchpad script, not shipped): 41 deliberate breakages of the new rules were prepared and applied one at a time; 39 matched the code. 34 were caught at once and 5 survived; tests were added for those 5, and a second pass over them (plus the 2 that had not matched because of a pattern typo) caught all 7.

Defects found by the tests while building this stage and fixed: the interface would have blocked while the model loaded (a status call waited on the load lock); a network error during download was reported as a disk-write error; a speak request after shutdown could create a fresh speaker; a failed job left its synthesis thread waiting; "playback ok" was claimed for empty text; and the new tests themselves imported the real `onnxruntime` through the provider probe, leaving 3 open file handles that made unrelated sandbox tests (`test_p7_*`) fail intermittently in full runs. The probe is now injectable and the tests never import the real library (4 of 4 combined runs clean afterwards, versus 1 failure in 3 before).

## 7. Not done / limits
* Real inference, real playback, real PySide6 page, Windows: **not verified** (see section 0). A checklist for the first run on Windows: install the runtime, press Install / repair on a normal connection, preview all four voices, listen, press Stop mid-sample, unplug the output device during a preview, switch to Windows speech and back, restart Jarvis and confirm the choice is kept, confirm the microphone indicator never changes.
* The download URLs and file names are from documentation and were never opened here. If the project moves them, the download fails with a plain message and nothing is installed; the manual placement in section 4 still works.
* No publisher checksum exists in this implementation (none found). If you obtain a trusted one, it can be added to the file table.
* The `kokoro-onnx` version bound and the exact way it selects its ONNX provider were not verified by running it.
* "Stop" cannot interrupt a sentence that is mid-computation (discarded when done).
* Language is fixed to `en-us` for synthesis (the four presets are English voices per the Kokoro documentation read; not verified against the file). Other languages are not offered.
