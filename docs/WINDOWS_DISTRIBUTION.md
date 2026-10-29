# Building and distributing the Windows installer

## What I could and could not do (honest status)

The authoring environment is Linux without Windows, PyInstaller, or Inno Setup, so **no .exe and no installer have been built or run**.
What is delivered is everything needed to build them in one command, plus automated build/test on a free Windows runner.
All Python logic and the static checks of the packaging files are tested (see the test report); the Windows steps below are **not yet executed**.

What a user gets once you build it:
- `ProjectJarvis-Setup-0.6.0.exe`: per-user install (no admin needed; admin optional), Windows 10 1809+ and Windows 11, 64-bit only, with Start-menu entry,
  optional desktop shortcut, and a "setup guide" that runs after install. No Python needed (bundled by PyInstaller).
- Data lives in `%LOCALAPPDATA%\ProjectJarvis`; upgrades keep it; a silent uninstall never deletes it; interactive uninstall asks.
- No paid API, subscription or account is needed. Every online feature is off by default. Telemetry is opt-in and only possible if YOU build in a server address (`-TelemetryUrl`); without one nothing can be sent (docs/TELEMETRY.md).

**Important limits you should know about:**
1. **The desktop window (`jarvis-gui.exe`, PySide6) has been driven by real key and click events on a hosted Windows CI runner (run 40431278616, an older commit), but has never been run by a person on a real PC.** It was written without Qt; the CI run covers the setup wizard and chat, not looks, scaling or accessibility. The build and installer are built and silently installed by `windows-ci` on every push; a clean consumer machine has never been tested. See `docs/VERIFICATION_LEDGER.md`.
   The release is a **folder** (`dist\ProjectJarvis`, one-folder PyInstaller build) wrapped by the Inno Setup installer, not a single exe.
2. **No model is bundled in the installer** (license and size reasons). The setup guide offers to download the free Qwen3-1.7B model (Apache-2.0) from huggingface.co with the user's consent, or to install a `.gguf` file the user already has. See `docs/LOCAL_MODEL.md`. **The download and real inference worked on a hosted Windows CI runner in run 40431278616 (an older commit; CPU only) but have not been re-run on the current build or on a person's PC**; run `jarvis selftest` on your Windows PC.
3. **The local runtime** (llama-cpp-python, prebuilt CPU wheel) is installed by the build script and checked in the frozen exe with `jarvis.exe runtime --require`. If no wheel exists for your Python version, use Python 3.12 or see LOCAL_MODEL.md.
4. **Voice is optional** (`-WithVoice` bundles sounddevice/vosk). The Vosk speech model is not bundled and must be licence-checked first (docs/VOICE.md).

## Build option A: free GitHub Actions (no Windows machine needed)
1. Push the project to a GitHub repository (public repos get free Windows runners; private repos have a free monthly allowance).
2. Actions tab -> `build-windows` -> Run workflow (or push a tag like `v0.6.0`).
3. Download the `ProjectJarvis-installer` artifact (installer + `SHA256SUMS.txt`).
The workflow runs the tests, freezes the exe, compiles the installer, then silently installs, launches, and uninstalls it on the runner.

## Build option B: your Windows 10/11 PC
Prerequisites (all free): 64-bit Python 3.11-3.13 from python.org (tick "py launcher"), Inno Setup 6 from jrsoftware.org.
The script finds Inno Setup itself (PATH, Program Files, or the registry); it does not need to be on PATH.

    cd project-jarvis
    powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -CheckOnly   (shows what it found; changes nothing)
    build_installer.bat                                                              (full build; add -Clean for a fresh start, -WithVoice to bundle voice libs)

Expected: pip installs, the unit tests pass (`python -m unittest discover -s tests`; one skipped without torch; **these tests have never been run on Windows**, so a Windows-only failure is possible: send me the output, and use `-SkipTests` only to keep diagnosing, never for a release build), PyInstaller prints `Building COLLECT ... completed successfully`,
`dist\ProjectJarvis\jarvis.exe version` prints `0.6.0`, `jarvis.exe privacy` shows all features false, Inno prints `Successful compile`,
the Visual C++ runtime check (`VC++ runtime bundled: ...` lines, or a copy/ERROR message), a smoke test of the frozen exe using a throwaway data folder (version, runtime, privacy, hardware, setup, backup), then `BUILD OK - see dist\installer` with `ProjectJarvis-Setup-0.6.0.exe` and `SHA256SUMS.txt`.
If any step differs, send me the output; that is the first real test of this pipeline.

## Test the installer on a clean machine
Windows Sandbox (Pro/Enterprise) or a fresh VM: copy the installer and `scripts\clean_install_test.ps1` in, then

    .\clean_install_test.ps1 -Installer .\ProjectJarvis-Setup-0.6.0.exe

Expected JSON: every check `"ok": true` (silent_install, exe_present, version, local_ai_runtime_present, vc_runtime_bundled, bare_launch_runs_setup_and_exits, first_run_offline_defaults, chat_without_model_is_honest, no_model_bundled, silent_uninstall_keeps_data).
Windows Sandbox has networking disabled, so it cannot test the model download; do that on a networked PC (see `docs/WINDOWS_TEST_CHECKLIST.md`).

## Making it safe to distribute
- Publish `SHA256SUMS.txt` next to the installer; users can check with `certutil -hashfile ProjectJarvis-Setup-0.6.0.exe SHA256`.
- **The exe is not code-signed**, so Windows SmartScreen will warn ("Windows protected your PC" -> More info -> Run anyway) until it builds reputation.
  Certificates cost money; free routes are limited (e.g. SignPath Foundation for open-source projects). Do not tell users to disable antivirus.
  PyInstaller executables sometimes trigger false positives; submitting the build to Microsoft Defender's false-positive form helps.
- `THIRD_PARTY_NOTICES.md` is regenerated at build time. Review licences (Qt/LGPL if you add PySide6, any model or voice) before release.
- Optional signed update manifests: keys via `release_tools.py gen-keys`; keep the private key offline. The app only checks for updates if the user enables it.
- Name: "Jarvis" has trademark risk; consider renaming before public release (legal question).


## Working on other people's PCs: what exists, and what is verified

Built into the code (logic unit-tested on Linux with fakes and synthetic files; NOT run on Windows):
- **Double-click.** `jarvis.exe` with no arguments starts the guided setup and, when it owns its console window, waits for Enter instead of
  vanishing. From cmd/PowerShell it never waits. A crash prints a plain message and writes `crash.log` in the data folder (local only, nothing is sent).
- **Visual C++ runtime.** Python, PyInstaller and llama.cpp are built with Visual C++, and a clean PC may lack the Redistributable.
  `scripts\check_vc_runtime.py` (run by the build script) reads the import tables of every `.exe/.dll/.pyd` in `dist\ProjectJarvis`, lists the
  VC++ runtime DLLs they need, copies a missing one from the build PC's native 64-bit `System32` only if it is a 64-bit file, and **fails the build**
  if any needed DLL is still missing, any file is 32-bit, or no VC++ import is found at all (a sign the scan itself failed). The import-table
  reader was tested only against small synthetic PE files, not against real Windows binaries.
  *Licensing:* app-local copies of these DLLs are what Microsoft's Visual C++ Redistributable terms are designed for, but I could not read the
  current licence text here: confirm it before you publish an installer. Fallback with no copying at all: tell users to install Microsoft's free
  "Visual C++ Redistributable (x64)".
- **CPU / AVX2.** `jarvis hardware` and `jarvis runtime` report `avx2` (Windows `IsProcessorFeaturePresent(40)`). **Whether the prebuilt
  llama-cpp-python CPU wheel requires AVX2 could NOT be verified** (its build settings were not reachable from the authoring sandbox). So a missing
  AVX2 only produces a warning; nothing is blocked. If a model load closes the program silently on an older CPU, that is the first suspect.
- **Older Inno Setup 6.x.** The script passes `/DArchName=x64` when ISCC is older than 6.3 (which lacks `x64compatible`). Running `iscc` by hand on an
  old Inno: add `/DArchName=x64`.
- **Frozen resources.** The telemetry endpoint file is also looked up under the PyInstaller bundle folder.

Still unverified and needing a real Windows run: the whole PowerShell pipeline (it was never parsed by PowerShell here), the unit tests on Windows,
PyInstaller output, Inno compile, the installer on clean Windows 10/11, llama-cpp-python wheel availability for your Python version, the Hugging Face
download, real inference, and antivirus/SmartScreen behaviour of the unsigned exe. Do not call the installer production-ready until
`docs/WINDOWS_TEST_CHECKLIST.md` has been completed on a clean machine.
