# System & Model Readiness - the safe preflight, run from the Jarvis window

**Status: written on Linux and tested there with mocks. This page has NOT been opened in a real Jarvis window or in the packaged Windows EXE, and nothing here has been validated on Windows.** The first Windows run is the test (see "What you will see on first use").

## How to run it (no PowerShell, no typing)
1. Start Jarvis (`jarvis-gui.exe`, or the Start-menu shortcut).
2. Click **System & Model Readiness** in the list on the left (or press `Ctrl+9`).
3. Leave the box "Also check the model host's published information" ticked to include the online check, or untick it to run only the offline checks.
4. Click **Run Safe Preflight**. If the online check is ticked, a window asks whether Jarvis may contact huggingface.co once. The safe default is **Cancel**; choose *Allow and run all checks*, or *Run offline checks only*.
5. Watch the progress bar and the line under it. **Cancel** stops after the current step (a request already sent to the host finishes first, up to 20 seconds).
6. Read the result line and the table. If a line says PROBLEM, UNAVAILABLE or ERROR, click **Show diagnostics**: it opens the Status page, whose diagnostic report (hardware, runtime, the app's own files, the newest readiness result) you can read, copy or save; it contains no conversations or files. Every check says OK, PROBLEM, UNAVAILABLE, NOT RUN, ERROR or INFO. The overall result is **PASSED only if every check passed**.

## What it checks
| Check | Needs internet | What it does | Possible results |
|---|---|---|---|
| Operating system | no | Reads the Windows version. | OK on 64-bit Windows 11; INFO on Windows 10 or on a non-Windows PC (then nothing is Windows 11 evidence); PROBLEM on a non-64-bit-Intel/AMD PC |
| Python runtime | no | Reads the interpreter inside the app (version, 64-bit). | OK / PROBLEM |
| App files and modules | no | Looks for the files and program modules this copy of Jarvis needs (17 files, 10 modules). Files are looked up; modules are found on disk but **not imported** here. In a source checkout it says so. | OK / PROBLEM (names the missing items; reinstall Jarvis, your data is not touched) |
| Data folder writable | no | Creates and removes a tiny test file in its own results folder. | OK / PROBLEM |
| Free disk space | no | Reads free space on the drive that holds your Jarvis data. At least **25 GB** is needed before the model experiment. | OK / PROBLEM / UNAVAILABLE |
| Memory (RAM) | no | Reads installed RAM; **16 GB or more** is recommended for the larger models. | OK / PROBLEM / UNAVAILABLE |
| Local AI runtime (import only) | no | Tries to import `llama-cpp-python`. Importing is **not** loading a model. | OK / UNAVAILABLE (not installed or fails to import) |
| Installed models | no | Lists the models already installed. Does not verify, load or change them. | INFO |
| Network permission | no | Shows whether model-host access is currently on (it is off by default). | INFO |
| Model metadata | **yes** | Reads the public description of the five candidate model files from huggingface.co: owner, file name, size, published SHA-256, revision, licence. Compares it with what the project recorded and with the 20 GB budget. Never requests a model file. | OK / PROBLEM / UNAVAILABLE (host unreachable) / NOT RUN |

Nothing is downloaded, no AI runtime is installed, no model is loaded or benchmarked, the default model and the model list are not changed, and no administrator rights are requested. The online step goes through the same network gate as every other online feature in Jarvis (`NetworkPolicy`), enabled for that one action only and switched off again.

## How to read the result
* **PASSED**: every check passed. This covers this PC's basics and the host's *published* metadata. It does not prove the models are authentic (the sizes and checksums come from a single source, huggingface.co, and none was computed from a downloaded file), and it says nothing about whether any model will run or how well.
* **PARTIAL**: the checks that ran passed, but the online check was not run.
* **INCOMPLETE**: something could not run (no internet, or the AI runtime is missing). This is **not** a pass.
* **PROBLEMS**: a check failed or crashed. Do not download any model until it is resolved.
* **CANCELLED**: you stopped it; nothing is concluded.
* The "Model files" table shows the host-published size and SHA-256 for each file. The saved `model_metadata.json` is a local, editable copy and is **not** proof of authenticity; run the check again to see current values.
* "Not yet verified" lists what this page cannot establish.

## Where results are kept
`<your Jarvis data folder>\preflight\<date-time>\` (use **Open results folder**): `preflight_record.json` (time, app version, Windows and Python information, every check with its result code and text, the model table, the blockers), `preflight.log` (the steps in order), and for an online run `model_metadata.json`, `model_metadata.md` and `raw\` (the host's responses). The newest 20 runs are kept. The home folder, your Windows user name and secret-looking text are removed before saving. A guest session does not save results. Nothing is sent anywhere by saving. The result code stored with each check follows the same convention as the maintainer scripts (0 passed, 3 unavailable, 4 problem, 1 the check crashed); no separate program is started, so it is not a process exit status.

## Limits - which are enforced where
| Limit | What this page does |
|---|---|
| 25 GB free before the experiment | **Checked** (once, now). |
| 20 GB total downloads | Only **compared with the host's published sizes**; no download happens here, and no running total is kept later. |
| 115 % per-file size ceiling | Not used on this page; **enforced by the download code** when a model is later downloaded or added. |
| Free space for each download/copy | Not used on this page; enforced by the download code. |
| 13 GB peak memory | **Not enforced by the app.** Watch Task Manager during any model test. |

## What is still not possible inside the EXE
These steps remain outside this page; none was built in this milestone, and each needs your decision or authorisation first:
* Downloading a model and checking the real file's size and SHA-256 (`scripts/verify_downloaded_model.py` is not shipped in the EXE).
* Inspecting or installing an AI-runtime wheel (`scripts/verify_wheel.py`; the app never installs the runtime).
* Running the model benchmarks under a memory guard (`scripts/resource_guard.py`, `summarize_stage5_results.py`).
* Any independent second source for the model checksums.
The maintainer procedure for those is `docs/STAGE5_WINDOWS_PLAYBOOK.md`.

## The built-in self-test (`jarvis-gui.exe --selftest`)
Used by the build and for a first look at a new build. It has no window of its own and writes two files to your temp folder: `jarvis-gui-selftest.txt` (first line `PASS` or `FAIL ...`, then one line per check) and `jarvis-gui-selftest.json`. Exit code: **0** every check passed, **1** at least one check failed, **3** Qt (PySide6) could not be loaded at all. A file that says `RUNNING` means it did not finish.
Each line is labelled **genuine** (really executed: files looked up, program modules really imported, real Qt widgets built on Qt's *offscreen* platform, every page's refresh called directly, and the offline part of Run Safe Preflight started on the background worker and waited for), **simulated** (the controller runs in simulate mode, so any chat reply is a stand-in) or **not_run** (loading a model, and the online check, are never done by the self-test). Findings about the PC itself (little disk space, no AI runtime) are listed as information, not as failures of the app. It uses a temporary data folder and your own Jarvis data is not touched; it downloads and installs nothing and needs no administrator rights.
**It cannot show** that anything looks right, responds to the mouse, or works with your screen, scaling or dark mode: offscreen rendering has none of those. Nobody has run it on Windows yet.

## What you will see on first use (to be confirmed on Windows)
If a part of this page looks wrong or does not open, that is a defect to report; include the self-test files above and the diagnostic report from the Status page.
