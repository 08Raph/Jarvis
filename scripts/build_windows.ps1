# Build pipeline for Windows 10/11. NOT YET RUN (the authoring sandbox has no Windows, PyInstaller or Inno Setup).
# Prerequisites: 64-bit Python 3.11-3.13 (python.org installer, "py launcher" ticked) and Inno Setup 6 (jrsoftware.org).
# Usage: .\scripts\build_windows.ps1 [-CheckOnly] [-Clean] [-SkipTorch] [-SkipTests] [-WithVoice] [-TelemetryUrl https://your-server] [-SkipLocalRuntime] [-RuntimeFromSource] [-LockFile requirements-lock.txt] [-SigningKeyFile C:\offline\jarvis_signing_private.hex]
#   -CheckOnly  report what was found (Python, build environment, Inno Setup) and stop; changes nothing
#   -Clean      ALSO delete the .venv build environment (build\ and dist\ are always emptied first, so no file of an older build can be shipped)
#   -LockFile   install exactly the package versions of an earlier build (pip -c FILE) to reproduce it; the file is the requirements-lock.txt that build wrote
#   -SkipTorch  do not install PyTorch. The Qwen assistant does not need it; only the separate original-model research engine does. See docs\WINDOWS_BUILD_GUIDE.md
#   -SkipTests  diagnostic only: skip the unit tests (they have never run on Windows). Never release such a build.
param(
    [switch]$CheckOnly,
    [switch]$Clean,
    [switch]$SkipTorch,
    [switch]$SkipTests,
    [switch]$WithVoice,
    [string]$TelemetryUrl = "",
    [switch]$SkipLocalRuntime,
    [switch]$RuntimeFromSource,
    [string]$SigningKeyFile = "",
    [string]$LockFile = ""
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Run($cmd) {
    # Windows PowerShell 5.1 turns ANY line a native program writes to stderr (pip warnings, notices) into a terminating error under "Stop",
    # even when the program succeeds. The exit code is the real verdict, so relax it for the call only.
    Write-Host ">> $cmd"
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { Invoke-Expression $cmd } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "failed (exit $LASTEXITCODE): $cmd" }
}

# ---- Preflight: find a suitable 64-bit Python (3.11-3.13) ----
$pyExe = $null; $pyArgs = @()
$candidates = @( @("py", "-3.12"), @("py", "-3.13"), @("py", "-3.11"), @("python") )
foreach ($c in $candidates) {
    $exe = $c[0]; $rest = @(); if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $info = (& $exe @rest -c "import sys; print(sys.version_info[0], sys.version_info[1], sys.maxsize > 2**32)") 2>$null
        $p = ("$info").Trim().Split(" ")
        if ($LASTEXITCODE -eq 0 -and [int]$p[0] -eq 3 -and [int]$p[1] -ge 11 -and [int]$p[1] -le 13 -and $p[2] -eq "True") {
            $pyExe = $exe; $pyArgs = $rest; Write-Host "Python found: $exe $rest (3.$($p[1]), 64-bit)"; break
        }
    } catch { }
}
if (-not $pyExe) { Write-Host "ERROR: no 64-bit Python 3.11-3.13 found. Install it from python.org (tick 'py launcher'), then reopen PowerShell." -ForegroundColor Red; exit 1 }

# ---- Preflight: find Inno Setup 6 (its installer does NOT add iscc to PATH) ----
$iscc = $null
$isccCmd = Get-Command iscc -ErrorAction SilentlyContinue
if ($isccCmd) { $iscc = $isccCmd.Source }
if (-not $iscc) {
    $paths = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
    foreach ($k in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
                     "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
                     "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1")) {
        $loc = (Get-ItemProperty $k -ErrorAction SilentlyContinue).InstallLocation
        if ($loc) { $paths += (Join-Path $loc "ISCC.exe") }
    }
    foreach ($pth in $paths) { if (Test-Path $pth) { $iscc = $pth; break } }
}
if ($iscc) { Write-Host "Inno Setup found: $iscc" } else { Write-Host "WARNING: Inno Setup 6 (ISCC.exe) not found. Install it from jrsoftware.org; the exe can be built but not the installer." -ForegroundColor Yellow }
if ((Get-Location).Path -match '[^\x00-\x7F]') { Write-Host "WARNING: the project path contains non-ASCII characters; move it to something like C:\dev\project-jarvis if the build fails." -ForegroundColor Yellow }
# ---- Preflight: disk space, path, permissions, Python modules (reports measured / guessed / not-checked separately) ----
& $pyExe @pyArgs scripts\check_build_env.py
$envOk = ($LASTEXITCODE -eq 0)
if ($LockFile -ne "" -and -not (Test-Path $LockFile)) { Write-Host "ERROR: -LockFile $LockFile does not exist." -ForegroundColor Red; exit 1 }
if ($CheckOnly) { if ($envOk) { Write-Host "Check complete." } else { Write-Host "Check complete: fix the FAIL lines above." -ForegroundColor Red }; exit $(if ($envOk) { 0 } else { 1 }) }
if (-not $envOk) { Write-Host "ERROR: the build environment check failed (see the FAIL lines above). Nothing was changed." -ForegroundColor Red; exit 1 }
if (-not $iscc) { Write-Host "ERROR: stopping before a long build that cannot finish. Install Inno Setup 6 and rerun." -ForegroundColor Red; exit 1 }

# Always start from empty output folders: a file left from an older build (a model, a log, a test file) must never reach the installer.
foreach ($d in @("build", "dist")) { if (Test-Path $d) { Remove-Item $d -Recurse -Force } }
if ($Clean -and (Test-Path .venv)) { Remove-Item .venv -Recurse -Force }

if (-not (Test-Path .venv)) { & $pyExe @pyArgs -m venv .venv; if ($LASTEXITCODE -ne 0) { throw "could not create .venv" } }
$py = ".\.venv\Scripts\python.exe"
Run "$py -m pip install --upgrade pip"
$cArg = ""
if ($LockFile -ne "") { $cArg = '-c "' + (Resolve-Path $LockFile).Path + '"'; Write-Host "Reproducing an earlier build: package versions are constrained by $LockFile" }
if (-not $SkipTorch) {
    # CPU-only PyTorch avoids redistributing NVIDIA CUDA libraries and keeps the build smaller.
    Run "$py -m pip install torch --index-url https://download.pytorch.org/whl/cpu"
    Write-Host "NOTE: PyTorch will be bundled into the application (several hundred MB). The Qwen assistant does not use it; use -SkipTorch for a Qwen-only build." -ForegroundColor Yellow
}
Run "$py -m pip install $cArg -e `".[dev,build,desktop,ui]`""   # desktop = comtypes (UI Automation, MIT); ui = PySide6 (Qt, LGPL) for jarvis-gui.exe
if (-not $SkipLocalRuntime) {
    # The local AI runtime (llama.cpp via llama-cpp-python, MIT). Prebuilt CPU wheel = no C++ compiler needed.
    try {
        if ($RuntimeFromSource) { Run "$py -m pip install llama-cpp-python" }
        else { Run "$py -m pip install llama-cpp-python --only-binary=:all: --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu" }
    } catch {
        Write-Host "ERROR: could not install the local AI runtime. Use 64-bit Python 3.12 (wheels are most likely for it), or install the free Visual Studio Build Tools (C++ workload) plus CMake and rerun with -RuntimeFromSource. See docs\LOCAL_MODEL.md." -ForegroundColor Red
        throw
    }
}
if ($WithVoice) { Run "$py -m pip install $cArg -e `".[voice]`"" }
# Mandatory packages and files: stop with a clear message instead of building a half-working package.
Run "$py scripts\check_release_assets.py --packages"
# Record exact versions of this release build. --exclude-editable: an editable line holds this PC's folder and pip refuses it in a constraints file. Out-File: PowerShell 5.1's > would write UTF-16.
& $py -m pip freeze --exclude-editable | Out-File -Encoding ascii requirements-lock.txt

# 1. Tests must pass before anything is packaged.
if ($SkipTests) {
    Write-Host "WARNING: -SkipTests given. The test suite has NEVER been run on Windows; a failure there may be a Windows-only test problem. Do not release a build made this way." -ForegroundColor Yellow
} else {
    Run "$py -m unittest discover -s tests"      # the runner the suite was developed with
}

# 2. Notices, build info and requirements doc regenerated from the build environment.
Run "$py scripts\release_tools.py notices --out THIRD_PARTY_NOTICES.md"
Run "$py scripts\release_tools.py build-info --out build_info.json"      # honours SOURCE_DATE_EPOCH; records commit, Python, lock-file hash
Run "$py scripts\check_release_assets.py --source"
Run "$py scripts\release_tools.py requirements-doc --out docs\MODEL_REQUIREMENTS.md"

# 3. Freeze the application (one-folder build). Optional telemetry server address is baked in for this build only; users must
#    still opt in. The source file is emptied again afterwards so the repository never contains a server address.
$epFile = Join-Path (Get-Location) "src\jarvis\telemetry\endpoint.txt"
if ($TelemetryUrl -ne "" -and $TelemetryUrl -notmatch '^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$') { throw "-TelemetryUrl must be an https:// address" }
try {
    if ($TelemetryUrl -ne "") { [System.IO.File]::WriteAllText($epFile, $TelemetryUrl, (New-Object System.Text.UTF8Encoding($false))) }
    Run "$py -m PyInstaller packaging\jarvis.spec --noconfirm --clean"
} finally {
    [System.IO.File]::WriteAllText($epFile, "", (New-Object System.Text.UTF8Encoding($false)))
}

Run "$py scripts\check_release_assets.py --dist dist\ProjectJarvis"      # required files present, no secrets/weights/dev files

# 4. Smoke-test the frozen executable before building the installer.
if (-not (Test-Path .\dist\ProjectJarvis\jarvis.exe)) { throw "PyInstaller did not produce dist\ProjectJarvis\jarvis.exe" }
$smoke = Join-Path $env:TEMP ("jarvis-smoke-" + [guid]::NewGuid().ToString("N"))   # throwaway data folder, not your real one
Run ".\dist\ProjectJarvis\jarvis.exe version"
if (-not (Test-Path .\dist\ProjectJarvis\jarvis-gui.exe)) { throw "PyInstaller did not produce dist\ProjectJarvis\jarvis-gui.exe" }
# The window is started in Qt's offscreen mode, builds every page, closes, and writes PASS/FAIL to %TEMP%\jarvis-gui-selftest.txt.
# This proves the Qt plugins were frozen correctly; it does NOT prove the window looks right: open it yourself (docs\WINDOWS_TEST_CHECKLIST.md).
Remove-Item "$env:TEMP\jarvis-gui-selftest.txt" -ErrorAction SilentlyContinue
$g = Start-Process -FilePath .\dist\ProjectJarvis\jarvis-gui.exe -ArgumentList "--selftest" -Wait -PassThru
if ($g.ExitCode -ne 0 -or -not (Test-Path "$env:TEMP\jarvis-gui-selftest.txt") -or -not ((Get-Content "$env:TEMP\jarvis-gui-selftest.txt" -Raw).StartsWith("PASS"))) {
    throw "jarvis-gui.exe --selftest failed (see $env:TEMP\jarvis-gui-selftest.txt)"
}
if (-not $SkipLocalRuntime) { Run ".\dist\ProjectJarvis\jarvis.exe runtime --require" }   # the frozen exe must contain the runtime
Run ".\dist\ProjectJarvis\jarvis.exe --data-dir `"$smoke`" privacy"
Run ".\dist\ProjectJarvis\jarvis.exe --data-dir `"$smoke`" hardware"
Run "'' | .\dist\ProjectJarvis\jarvis.exe --data-dir `"$smoke`" setup"        # the guided setup, with empty answers
Run ".\dist\ProjectJarvis\jarvis.exe --data-dir `"$smoke`" backup"
Remove-Item $smoke -Recurse -Force -ErrorAction SilentlyContinue

# 4b. Other-PC portability: the app and the llama.cpp runtime are built with Visual C++. A clean Windows PC may not have
#     the Visual C++ Redistributable, so every VC++ DLL the frozen files import must be INSIDE the app folder. This reads the
#     import tables, copies only missing 64-bit files from this PC's native System32, and FAILS if any stays missing.
$sys32 = if ([Environment]::Is64BitProcess) { "$env:SystemRoot\System32" } else { "$env:SystemRoot\Sysnative" }   # never copy 32-bit DLLs
Run "$py scripts\check_vc_runtime.py dist\ProjectJarvis --fix-from `"$sys32`""

# 5. Installer.
$ver = (& $py -c "from jarvis.version import __version__; print(__version__)").Trim()   # the one authoritative version
# Inno Setup older than 6.3 does not know the "x64compatible" architecture name; use "x64" there.
$archName = "x64compatible"
try {
    $iv = (Get-Item $iscc).VersionInfo.FileVersion
    if ($iv -match '^(\d+)\.(\d+)') { if ([int]$Matches[1] -lt 6 -or ([int]$Matches[1] -eq 6 -and [int]$Matches[2] -lt 3)) { $archName = "x64" } }
    Write-Host "Inno Setup version: $iv (architecture name: $archName)"
} catch { Write-Host "note: could not read the Inno Setup version; assuming 6.3 or newer" -ForegroundColor Yellow }
Run "& `"$iscc`" /DAppVersion=$ver /DArchName=$archName packaging\installer.iss"
if (-not (Test-Path "dist\installer\ProjectJarvis-Setup-$ver.exe")) { throw "installer was not produced" }

# 6. Checksums (publish SHA256SUMS.txt next to the installer) and the build manifest (versions, every file's SHA-256, the installer's SHA-256).
Run "$py scripts\release_tools.py sha256sums dist\installer dist\installer\SHA256SUMS.txt"
Run "$py scripts\release_tools.py build-manifest --dist dist\ProjectJarvis --installer-dir dist\installer --out dist\build_manifest.json"
Run "$py scripts\check_release_assets.py --dist dist\ProjectJarvis --manifest dist\build_manifest.json"      # again, after the smoke tests: nothing was added or changed meanwhile
Write-Host "NOTE: this build was made on this PC only. It is not a clean-machine test; follow docs\WINDOWS_TEST_CHECKLIST.md section 10-11." -ForegroundColor Yellow

# 7. Optional: sign an update manifest. The private key stays offline; nothing is uploaded by this script.
if ($SigningKeyFile -ne "") {
    $installer = Get-ChildItem dist\installer -Filter "ProjectJarvis-Setup-*.exe" | Select-Object -First 1
    $sha = (Get-FileHash $installer.FullName -Algorithm SHA256).Hash.ToLower()
    $payload = @{ latest = $ver; installer_url = "https://REPLACE-WITH-YOUR-RELEASE-URL/$($installer.Name)";
                  installer_sha256 = $sha; notes = "" } | ConvertTo-Json -Compress
    # Pass the payload as a FILE: Windows PowerShell 5.1 strips double quotes from native-command arguments.
    $payloadFile = Join-Path $env:TEMP "jarvis-update-payload.json"
    [System.IO.File]::WriteAllText($payloadFile, $payload, (New-Object System.Text.UTF8Encoding($false)))
    Run "$py scripts\release_tools.py sign-manifest --payload-file `"$payloadFile`" --private-key-file `"$SigningKeyFile`" --out dist\installer\update-manifest.json"
}
Write-Host "Done. Output: dist\installer"
