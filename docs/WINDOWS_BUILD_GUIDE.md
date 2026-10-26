# Windows build guide and troubleshooting

**Status: the build has never been run on Windows.** Everything here describes what the scripts are written to do. The first person to run it will find
problems; the troubleshooting list below is built from reading the scripts, not from observed failures.

## What you need
| Need | Detail | Checked by |
|---|---|---|
| Windows 10/11, 64-bit | The `.exe` and installer can only be built on Windows | `scripts\check_build_env.py` (warns elsewhere) |
| 64-bit Python 3.11-3.13 | 3.12 recommended (the prebuilt local-AI wheel is most likely to exist for it) | `check_build_env.py`, `build_windows.ps1` |
| Inno Setup 6 | Only for the installer; `build_windows.ps1` stops early if it is missing | `build_windows.ps1` |
| About 8 GB free disk | PySide6 (and optionally PyTorch) make the build large | `check_build_env.py` (fails under 3 GB, warns under 8) |
| A short, plain folder | e.g. `C:\dev\project-jarvis`; not OneDrive, not a very long or non-ASCII path | `check_build_env.py` (warns) |
| Internet | pip downloads packages | **not tested** by any script; pip's own error is shown |

Package versions: `pyproject.toml` gives lower bounds (`numpy>=1.26`, `cryptography>=42`, `PySide6>=6.7,<7`, `pyinstaller>=6.6`, ...). No upper bounds or exact pins
are committed, because no combination has been verified on Windows; inventing pins would only look reproducible. Instead each build **records** what it resolved
(`requirements-lock.txt`, `build_info.json`, `dist\build_manifest.json`) and a later build can reproduce it with `-LockFile`.

## Steps
    cd C:\dev\project-jarvis
    python scripts\check_build_env.py                    # or: .\scripts\build_windows.ps1 -CheckOnly   (also looks for Inno Setup)
    .\scripts\build_windows.ps1 -Clean                  # add -SkipTorch for a Qwen-only build (smaller; see below)
    python scripts\check_release_assets.py --dist dist\ProjectJarvis --manifest dist\build_manifest.json

`build_windows.ps1` always empties `build\` and `dist\` first (so a leftover file of an older build cannot reach the installer); `-Clean` also deletes `.venv`.
It then: installs packages, writes `requirements-lock.txt` (`pip freeze --exclude-editable`), runs the unit tests, regenerates notices and `build_info.json`,
freezes with PyInstaller (`packaging\jarvis.spec`, asset list in `packaging\manifest.py`), checks the built folder, smoke-tests the frozen exes, bundles missing
Visual C++ DLLs, builds the installer, writes `SHA256SUMS.txt` and `dist\build_manifest.json`, then verifies the folder against that manifest.

### Reproducing an earlier build
Keep `requirements-lock.txt` from the earlier build, then `.\scripts\build_windows.ps1 -Clean -LockFile path\to\requirements-lock.txt`. pip then refuses any package
version that differs. Expect the SHA-256 of the executables to differ anyway (timestamps and absolute paths inside PyInstaller output are not normalised; set
`SOURCE_DATE_EPOCH` to fix the recorded build time). Compare package versions and the file list, not the installer hash.

## What `dist\build_manifest.json` contains
Version, channel, build time, git commit, Python version, build platform, versions of the key packages (`null` = not installed), the SHA-256 of
`requirements-lock.txt`, whether PyTorch was bundled, any model-weight-sized file found in the build, the SHA-256 of `jarvis.exe`, `jarvis-gui.exe` and every other
file, a tree hash over all of them, the installer's SHA-256, and any text file that contains the build machine's folder or home path. It is a record, not a signature,
and says nothing about whether the build works. `build-manifest` exits non-zero (and the script stops) if weights or machine paths are found.
Binary files are not scanned for build paths (PyInstaller can embed some); that remains unchecked.

## Decisions for the owner
1. **PyTorch in the app.** The default build installs CPU PyTorch and PyInstaller bundles it, because `jarvis chat` for the separate original-model research checkpoints imports it.
   The Qwen assistant (llama.cpp) does not need it. A Qwen-only build (`-SkipTorch`) is smaller and has fewer things that can fail to freeze; the research engine then reports
   "torch is not installed". `build_manifest.json` says which you shipped (`torch_bundled`). Recommendation for a preview: `-SkipTorch`. Not changed here because it changes what the exe can do.
2. **Model weights are never bundled.** The build fails if a weight-sized file is found. The app downloads Qwen3-1.7B (Apache-2.0) only when the user asks, or the user adds a `.gguf`
   they are licensed to use. Check the licence of any other model before distributing it (`docs\MODEL_DISTRIBUTION.md`).
3. **`config.sample.toml`** is shipped but Jarvis 0.6.0 does not read a config file. The file now says so. Either implement reading it or stop shipping it.

## Troubleshooting (from the scripts, not from observed failures)
| Symptom | Likely cause | What to do |
|---|---|---|
| `check_build_env.py` FAIL python version | Python outside 3.11-3.13 | Install 3.12 from python.org; reopen PowerShell |
| FAIL 64-bit | 32-bit Python | Install the 64-bit build |
| WARN path / OneDrive | Long, non-ASCII or synced folder | Move to `C:\dev\...` |
| `BUILD BLOCKED: mandatory package(s) not installed` | pip install did not finish | Read pip's error above it; rerun |
| llama-cpp-python install fails | No wheel for this Python | Use Python 3.12; or Visual Studio Build Tools + CMake and `-RuntimeFromSource` (`docs\LOCAL_MODEL.md`) |
| Unit tests fail | The suite has only run on Linux | `python -m unittest discover -s tests -v` and send each failing name; do not release a `-SkipTests` build |
| `check_vc_runtime.py` ERROR | A Visual C++ DLL missing or 32-bit | Install the x64 Visual C++ Redistributable on the build PC and rebuild |
| `jarvis-gui.exe --selftest` fails | Qt plugins not frozen | Read `%TEMP%\jarvis-gui-selftest.txt` |
| `jarvis.exe runtime --require` fails | The AI runtime is missing from the frozen app | The build is unusable; see the spec's WARNING lines |
| `BUILD BLOCKED ... contains the build machine's folder` | A text file picked up your path | Find the named file; do not ship it |
| `WARNING: model weights are inside the build` | A large model file was in the folder | Remove it; weights are installed by the user |
| The window does not open on another PC | Qt cannot load (Visual C++ runtime), or a blocked folder | A message box names it; `%LOCALAPPDATA%\ProjectJarvis\crash.log` has details |
