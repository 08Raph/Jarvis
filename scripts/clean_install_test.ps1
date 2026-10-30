# Clean-machine install/launch/uninstall check. NOT YET RUN.
# Intended to run INSIDE Windows Sandbox (Windows 11 Pro/Enterprise feature: a disposable clean Windows),
# via packaging\clean_install_test.wsb, but also works on any Windows VM.
# Usage: .\clean_install_test.ps1 -Installer C:\share\ProjectJarvis-Setup-0.6.0.exe
param([Parameter(Mandatory = $true)][string]$Installer)
$ErrorActionPreference = "Stop"
$results = [ordered]@{}
function Check($name, $block) {
    try { $v = & $block; $results[$name] = @{ ok = $true; detail = "$v" } }
    catch { $results[$name] = @{ ok = $false; detail = $_.Exception.Message } }
}
$app = Join-Path $env:LOCALAPPDATA "Programs\ProjectJarvis"
$data = Join-Path $env:LOCALAPPDATA "ProjectJarvis"
$pinBytes = 1282439264      # keep in step with PIN_BYTES in src/jarvis/models/bundled.py (a test compares them)

Check "silent_install" {
    $log = "$env:TEMP\jarvis-install.log"
    $p = Start-Process $Installer -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/LOG=$log" -PassThru
    $null = $p.Handle          # Windows PowerShell 5.1: without caching the handle, ExitCode can come back empty after WaitForExit
    $p.WaitForExit()
    $code = $p.ExitCode
    if ($code -ne 0) {
        $tail = if (Test-Path $log) { (Get-Content $log -Tail 25) -join " | " } else { "no install log was written (the installer may not have started)" }
        throw "installer exit code '$code' (installer: $Installer). Log tail: $tail"
    }
    "exit 0"
}
Check "exe_present" { if (-not (Test-Path "$app\jarvis.exe")) { throw "missing $app\jarvis.exe" }; "ok" }
Check "version" { & "$app\jarvis.exe" version }
Check "local_ai_runtime_present" {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $out = & "$app\jarvis.exe" runtime --require 2>&1 | Out-String
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "local AI runtime missing or failed to load: $out" }
    "runtime loads: $($out.Trim())"
}
Check "vc_runtime_bundled" {
    $f = Get-ChildItem $app -Recurse -Filter "vcruntime140.dll" | Select-Object -First 1
    if (-not $f) { throw "vcruntime140.dll is not inside the install folder; a PC without the Visual C++ Redistributable would fail" }
    "found $($f.FullName)"
}
Check "bare_launch_runs_setup_and_exits" {
    # Same as double-clicking jarvis.exe, but with stdin redirected so it cannot wait for a key.
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $null = '' | & "$app\jarvis.exe" 2>&1 | Out-String
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "bare launch exited with $code" }
    "exit 0"
}
Check "first_run_offline_defaults" {
    # Windows PowerShell 5.1 needs the multi-line output joined into ONE string before ConvertFrom-Json.
    $r = (& "$app\jarvis.exe" first-run | Out-String) | ConvertFrom-Json
    if ($r.network.PSObject.Properties.Value -contains $true) { throw "an online feature is enabled by default" }
    "all online features off; data dir $($r.data_dir)"
}
# A full installer (built with -BundleModel) contains bundled_models: the first run above then installs that model, from the installer's own file.
$bundled = Test-Path "$app\bundled_models\qwen3-1.7b-q4_k_m\model.gguf"
$bundledVoice = Test-Path "$app\bundled_models\vosk-model-small-en-us-0.15"
Check "bundled_model_installed_from_installer" {
    if (-not $bundled) { return "not applicable: this installer has no bundled model" }
    $m = Join-Path $data "models\qwen3-1.7b-q4_k_m\model.gguf"
    if (-not (Test-Path $m)) { throw "the bundled model was not installed into $data\models by the first run" }
    $len = (Get-Item $m).Length
    if ($len -ne $pinBytes) { throw "installed model is $len bytes, expected $pinBytes" }
    $a = Get-Content (Join-Path $data "models\active.json") -Raw | ConvertFrom-Json
    if ($a.id -ne "qwen3-1.7b-q4_k_m") { throw "the installed model was not made the active brain: $($a.id)" }
    "installed ($len bytes) and active; no network was needed"
}
Check "bundled_speech_model_set_up_from_installer" {
    if (-not $bundledVoice) { return "not applicable: this installer has no bundled speech model" }
    $v = Get-Content (Join-Path $data "voice.json") -Raw | ConvertFrom-Json
    if (-not $v.vosk_model) { throw "voice.json has no vosk_model after the first run" }
    if (-not (Test-Path (Join-Path $v.vosk_model "am"))) { throw "the speech model folder was not copied: $($v.vosk_model)" }
    "speech model copied to $($v.vosk_model) and set; no network was needed"
}
Check "chat_without_model_is_honest" {
    if ($bundled) { return "skipped on purpose: this installer carries a model, so chat would run real inference, which this check must not do" }
    # Windows PowerShell 5.1 turns native stderr into a terminating error under "Stop"; relax it for this call only.
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $null = & "$app\jarvis.exe" chat "hello" 2>&1 | Out-String
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 2) { throw "expected exit code 2, got $code" }
    "refused cleanly"
}
Check "no_model_bundled" {
    $big = @(Get-ChildItem $app -Recurse -Include model.pt,*.jarvismodel,*.gguf)
    if ($bundled) {
        $ok = Join-Path $app "bundled_models\qwen3-1.7b-q4_k_m\model.gguf"
        $extra = @($big | Where-Object { $_.FullName -ne $ok })
        if ($extra.Count -gt 0) { throw "unexpected weights in install dir: $($extra.FullName -join ', ')" }
        if (-not (Test-Path $ok) -or (Get-Item $ok).Length -ne $pinBytes) { throw "the bundled model file is missing or has the wrong size" }
        return "only the pinned bundled model"
    }
    if ($big) { throw "weights found in install dir" }; "none"
}
Check "silent_uninstall_keeps_data" {
    $u = Get-ChildItem $app -Filter "unins*.exe" | Select-Object -First 1
    Start-Process $u.FullName -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES" -Wait
    # Inno's uninstaller can hand over to a temporary copy of itself, so wait (up to 60 s) for the files to disappear.
    for ($i = 0; $i -lt 60 -and (Test-Path "$app\jarvis.exe"); $i++) { Start-Sleep -Seconds 1 }
    if (Test-Path "$app\jarvis.exe") { throw "app files remain" }
    if (-not (Test-Path $data)) { throw "user data was deleted by a silent uninstall" }
    "app removed, user data kept"
}
$results | ConvertTo-Json -Depth 4 | Tee-Object -FilePath "$env:TEMP\jarvis-clean-install-results.json"
if ($results.Values | Where-Object { -not $_.ok }) { exit 1 }
exit 0   # explicit: a stale $LASTEXITCODE from the last native command (for example "chat" expected to exit 2) must not decide the verdict
