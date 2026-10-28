# Windows first-run guide: environment and runtime check

**Status: PREPARED, NEVER RUN ON WINDOWS.** Nothing in this guide has been executed on a Windows PC by its author. The PowerShell syntax is unchecked (no PowerShell was available to the author). Real model inference, the GUI, voice and the installer are **untested**. This guide stops **before** any model is downloaded.

It covers one thing: getting a clean Python environment, the project, and the `llama-cpp-python` runtime onto your PC and checking that the runtime loads. Run **one stage at a time**. After each stage, check the stop condition. Do not go on if it is not met.

This is a source package, not a release or an installer.

---

## A. Prerequisites

| Item | Requirement | Where it comes from |
|---|---|---|
| Windows | Windows 11, **64-bit** (x64/AMD64). ARM64 and 32-bit are not supported by this procedure. | The runtime wheel is expected to be `win_amd64`; nothing else was considered. |
| Python | **64-bit CPython 3.12 recommended.** The project declares 3.11 or newer (`pyproject.toml`) and its docs name 3.11-3.13; only 3.12 is what the build scripts and CI target. Python 3.14 is untested; do not use it. | `pyproject.toml`, `docs/LOCAL_MODEL.md` |
| Python launcher | The `py` launcher (ticked by default in the python.org installer). | The commands below use `py -3.12`. |
| CPU | **AVX2 recommended.** Whether the prebuilt runtime needs AVX2 is **not verified**. If your CPU reports no AVX2, stop at Stage A and ask. | `src/jarvis/hardware.py` |
| RAM | **8 GB or more.** The code's hard minimum for the default model is 4 GB and it calls anything under 7 GB "tight"; older docs say 7 GB or 8 GB. Use the conservative 8 GB. | `assess_gguf` in `hardware.py`, `docs/LOCAL_MODEL.md`, `docs/STAGE7_VALIDATION_RUNBOOK.md` |
| Disk | **At least 5 GB free on C:.** The model step later needs about 1.8 GB (the default model is about 1.28 GB) and the environment about 1 GB. | `hardware.py`, `docs/LOCAL_MODEL.md` |
| Network | Needed for `pip` in Stages D and E (it downloads Python packages from pypi.org and from the runtime's own index, abetlen.github.io). It is **not** needed for the runtime check. **No model weights are downloaded anywhere in this guide.** | |
| Cost | None. Everything here is free. | |

> **Not verified:** that `llama-cpp-python==0.3.36` has a prebuilt Windows wheel, that it works with your Python and CPU, that its DLLs load on your PC, and that any command below behaves as written on Windows. Treat every "Expect" line as what the scripts are *written* to do, not as something observed.

---

## B. Safe staged commands (PowerShell)

Open **Windows PowerShell** (not as administrator). Everything goes under `C:\jv`, which can be deleted afterwards.

### Stage 0: start a log you can safely share
```powershell
New-Item -ItemType Directory -Force C:\jv\report | Out-Null
Start-Transcript -Path C:\jv\report\transcript.txt
```
**Save:** nothing yet. **Stop if:** PowerShell reports an error creating the folder.
Keep this window open for all stages. The log records commands and their output.

### Stage A: check Windows, Python, CPU, RAM and disk (reads only)
```powershell
[Environment]::OSVersion.Version
(Get-CimInstance Win32_OperatingSystem).OSArchitecture
(Get-CimInstance Win32_Processor).Name
"RAM GB: " + [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)
"Free C: GB: " + [math]::Round((Get-PSDrive C).Free/1GB,1)
py -0
py -3.12 -c "import sys,struct,platform,ctypes; print(sys.version); print(struct.calcsize('P')*8,'bit',platform.machine()); print('AVX2:',bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40)))"
```
**Expect:** a Windows version number, `64-bit`, a processor name, `RAM GB` of 8 or more, `Free C: GB` of 5 or more, a Python 3.12 entry in the `py -0` list, and from the last command `64 bit AMD64` and `AVX2: True`. (`IsProcessorFeaturePresent(40)` is the Windows API the app itself uses for AVX2.)
**Stop if:** the architecture is not 64-bit / AMD64, `py -3.12` is "not found" (install 64-bit Python 3.12 from python.org, then repeat), RAM is under 8, free disk is under 5, or `AVX2: False`.
**Save:** the screen output (it is in the log).

### Stage B: verify the ZIP before extracting it
Download the ZIP first (its name and SHA-256 come from the chat message that gave it to you). Then:
```powershell
$ZIP = "$HOME\Downloads\ProjectJarvis-source-797573f-with-windows-guide.zip"
Test-Path $ZIP
(Get-FileHash $ZIP -Algorithm SHA256).Hash.ToLower()
```
**Expect:** `True`, then a 64-character hash that equals, character for character, the hash given in the chat with this ZIP.
**Stop if:** `False` (fix the file name or location), or the hash differs in any character (delete the file and re-download; do not extract it). If you were not given a hash, do not continue.

### Stage C: extract the source and create an isolated environment
```powershell
if (Test-Path C:\jv\src) { "STOP: C:\jv\src already exists" } else {
  Expand-Archive -Path $ZIP -DestinationPath C:\jv\src
  Get-ChildItem C:\jv\src
}
py -3.12 -m venv C:\jv\venv
$PY = "C:\jv\venv\Scripts\python.exe"
& $PY --version
```
**Expect:** the extracted archive has a single top folder `ProjectJarvis-source`, so the project lives in `C:\jv\src\ProjectJarvis-source` (that is the folder containing `pyproject.toml`). `& $PY --version` prints `Python 3.12.x`.
**Stop if:** it prints "STOP", or `C:\jv\src` does not contain `ProjectJarvis-source`, or the venv step fails.
The commands call the venv's Python directly, so you do not need `Activate.ps1` (which a default execution policy may block).

### Stage D: install the project (no model weights)
```powershell
cd C:\jv\src\ProjectJarvis-source
Test-Path .\pyproject.toml
& $PY -m pip install -e .
& $PY -m jarvis version
```
**Expect:** `True`; pip ends with `Successfully installed ... project-jarvis-0.6.0 ...` (it also installs numpy, cryptography and tzdata from pypi.org); `jarvis version` prints `0.6.0`. This step downloads Python packages only. It never downloads a model.
**Stop if:** `Test-Path` is `False` (you are in the wrong folder), pip prints an error (save the last 15 lines), or the version command fails.

### Stage E: attempt the wheel-only runtime install
This installs a package from a third-party index (the `llama-cpp-python` project's own site). Read the notes below before you run it.
```powershell
& $PY -m pip install "llama-cpp-python==0.3.36" --only-binary=:all: --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
"pip exit code: $LASTEXITCODE"
```
**Expect (if a compatible wheel exists, which is unverified):** pip downloads a `llama_cpp_python-0.3.36-...whl` file, and ends with `Successfully installed ... llama-cpp-python-0.3.36 ...` and `pip exit code: 0`.
**Stop if:** pip prints "No matching distribution found" or "Could not find a version that satisfies the requirement", or any other error, or the exit code is not 0. If the error names a *dependency* (such as numpy), save that line too.

**Read this:**
- `--only-binary=:all:` tells pip to accept a ready-made binary wheel only. It **prevents a silent source build**. Without it, pip would try to compile `llama-cpp-python` from its source archive (PyPI has only the source archive), which needs Visual Studio Build Tools and CMake.
- **A failure here means STOP.** Do not remove the flag, and do not install build tools "to make it work". Send the saved output back and decide the next step with me. Compiling is possible but is a deliberate choice with its own prerequisites (Visual Studio Build Tools C++ workload and CMake), not a default fallback.
- A wheel's existence for your Windows / Python / CPU combination was **not verified** by the author: the package index could not be read from the authoring environment. Nothing here proves it exists or works.

### Stage F: runtime check
```powershell
& $PY -m jarvis runtime --require
"exit code: $LASTEXITCODE"
```
**Expect (success):** JSON containing `"available": true`, `"version": "0.3.36"`, `"cpu_avx2": true` and probably `"gpu_offload_supported": false` (the CPU wheel has no GPU), then `exit code: 0`.
**Failure looks like:** `"available": false` with an `"error"` such as `ModuleNotFoundError: No module named 'llama_cpp'` (not installed in this Python) or a DLL load error (installed, but does not run on this PC), then `exit code: 3`. A DLL error may quote a folder path.
**Stop:** this is the end of the guide. Whatever the result, go to "Collect your report".

### Collect your report
```powershell
Stop-Transcript
$names = @($env:USERNAME, $env:COMPUTERNAME, $env:USERPROFILE) | Where-Object { $_ }
$t = Get-Content C:\jv\report\transcript.txt -Raw
foreach ($n in $names) { $t = $t.Replace($n, "<redacted>") }
Set-Content -Path C:\jv\report\REPORT_SANITIZED.txt -Value $t
notepad C:\jv\report\REPORT_SANITIZED.txt
```
Read `REPORT_SANITIZED.txt` in Notepad and paste its contents. The sanitizing step replaces your Windows user name, computer name and profile folder, but it is a **best effort**: look through it yourself and remove anything else personal before sharing. Do **not** paste the original `transcript.txt`, tokens, passwords, account details or anything from your documents.

---

## C. Safety and accuracy: what the checks mean

- **What `runtime --require` proves:** `import llama_cpp` works in this Python, and what version, AVX2 flag and GPU-offload flag it reports. Exit code 0 means the import worked.
- **What it does not prove:** that any model loads or generates text, that the speed is usable, that the GUI works, that voice works, or that the installer works.
- No model weights are downloaded, and none are included in the source package. Only the Python packages named above are fetched.
- Real model inference, the GUI, voice and the installer remain **untested**.

---

## D. Next step after the runtime check: NOT YET AUTHORIZED TO RUN

> **Do not run anything in this section** until you have shared the sanitized report from Stages A-F, it has been reviewed, and you have said explicitly that you want to proceed. These commands download a model.

Use a throw-away data folder so your real profile is untouched (`--data-dir` goes **before** the subcommand):
```powershell
$D = "C:\jv\data"
& $PY -m jarvis --data-dir $D models fetch qwen3-1.7b-q4_k_m   # DOWNLOADS about 1.28 GB; asks you to type "yes" first
& $PY -m jarvis --data-dir $D models verify qwen3-1.7b-q4_k_m  # re-hashes the installed file; prints {"ok": true}
& $PY -m jarvis --data-dir $D selftest                          # loads the model and generates a short reply
"exit code: $LASTEXITCODE"                                      # right after selftest
```
- `models fetch` prints the licence (Apache-2.0), the size and a fit verdict, then asks you to type `yes`. It stops if the PC is clearly too small, and refuses a file for which the host publishes no checksum.
- `models verify` shows the installed files match the recorded SHA-256 values. It does not show that the publisher is trustworthy (the checksum comes from the same host as the file).
- `selftest` passes only if the model loads and returns non-empty text from one short generation (`SELFTEST PASSED`, exit code 0). Read the `answer` field in the printed JSON yourself. It says nothing about quality, personality, tools, the GUI or voice.
- The full first-run procedure after this is `docs/STAGE7_FIRST_REAL_RUN_CHECKLIST.md`.

## E. Clean-up
Everything created by this guide is inside `C:\jv` (the log, the source, the environment and, later, the data folder). Delete that folder to remove it. Nothing outside it is modified.
