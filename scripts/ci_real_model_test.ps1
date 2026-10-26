# Windows-runner evidence run (Prompt 20). NOT YET RUN. Installs the built installer, opens the REAL GUI (screenshot + window titles), runs the real setup
# wizard engine (real model download + real CPU inference), then repeats a self-test with this app's network access blocked by a Windows Firewall rule.
# Every step records ok/detail; nothing is hidden. This is a GitHub-hosted runner, NOT a customer PC.
param([Parameter(Mandatory = $true)][string]$Installer, [string]$LogDir = "ci-logs")
$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogDir = (Resolve-Path $LogDir).Path
$results = [ordered]@{}
function Check($name, $block) {
    try { $v = & $block; $results[$name] = @{ ok = $true; detail = "$v" } }
    catch { $results[$name] = @{ ok = $false; detail = $_.Exception.Message } }
    Write-Host ("[{0}] {1}: {2}" -f $(if ($results[$name].ok) { "ok" } else { "FAILED" }), $name, $results[$name].detail)
}
function Run-Logged($exe, $argList, $logName) {
    # Runs a native program, captures stdout+stderr to a log file, returns the exit code.
    $psi = Start-Process -FilePath $exe -ArgumentList $argList -NoNewWindow -PassThru -RedirectStandardOutput "$LogDir\$logName.out.txt" -RedirectStandardError "$LogDir\$logName.err.txt"
    $null = $psi.Handle; $psi.WaitForExit()
    if ($null -eq $psi.ExitCode) { throw "no exit code for $exe $argList" }
    return $psi.ExitCode
}
$app = Join-Path $env:LOCALAPPDATA "Programs\ProjectJarvis"
$data = Join-Path $env:LOCALAPPDATA "ProjectJarvis"
Add-Type -AssemblyName System.Drawing, System.Windows.Forms
Add-Type @"
using System; using System.Text; using System.Collections.Generic; using System.Runtime.InteropServices;
public class WinList {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc p, IntPtr l);
  [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  public static List<string> ForPid(uint want) {
    var res = new List<string>();
    EnumWindows((h, l) => { uint pid; GetWindowThreadProcessId(h, out pid); if (pid == want && IsWindowVisible(h)) { var sb = new StringBuilder(256); GetWindowText(h, sb, 256); if (sb.Length > 0) res.Add(sb.ToString()); } return true; }, IntPtr.Zero);
    return res;
  }
}
"@

Check "silent_install" {
    $p = Start-Process $Installer -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/LOG=$LogDir\install.log" -PassThru
    $null = $p.Handle; $p.WaitForExit(); $code = $p.ExitCode
    if ($code -ne 0) { throw "installer exit code '$code'" }
    "exit 0"
}
Check "artifact_facts" { @("jarvis.exe","jarvis-gui.exe") | ForEach-Object { $x = Get-Item "$app\$_"; "{0} {1} bytes sha256={2}" -f $_, $x.Length, (Get-FileHash $x.FullName -Algorithm SHA256).Hash } | Out-String }
Check "no_python_needed" { $savedPath = $env:PATH; try { $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"; $c = Run-Logged "$app\jarvis.exe" "version" "version" } finally { $env:PATH = $savedPath }; if ($c -ne 0) { throw "version exit $c" }; Get-Content "$LogDir\version.out.txt" -Raw }
Check "ai_runtime_loads" { $c = Run-Logged "$app\jarvis.exe" "runtime --require" "runtime"; if ($c -ne 0) { throw "runtime exit $c : $(Get-Content "$LogDir\runtime.out.txt" -Raw)" }; Get-Content "$LogDir\runtime.out.txt" -Raw }

# --- the real GUI on this runner's desktop: first launch should open the setup wizard by itself
Check "gui_launch_shows_wizard" {
    Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue
    $g = Start-Process "$app\jarvis-gui.exe" -PassThru
    Start-Sleep -Seconds 25
    if ($g.HasExited) { throw "jarvis-gui.exe exited with code $($g.ExitCode) within 25 s" }
    $titles = [WinList]::ForPid([uint32]$g.Id)
    try {
        $bmp = New-Object System.Drawing.Bitmap([System.Windows.Forms.SystemInformation]::PrimaryMonitorSize.Width, [System.Windows.Forms.SystemInformation]::PrimaryMonitorSize.Height)
        $gr = [System.Drawing.Graphics]::FromImage($bmp); $gr.CopyFromScreen(0, 0, 0, 0, $bmp.Size); $bmp.Save("$LogDir\gui_first_launch.png")
    } catch { "screenshot failed: $($_.Exception.Message)" | Out-File "$LogDir\screenshot_error.txt" }
    Stop-Process -Id $g.Id -Force
    if (-not ($titles | Where-Object { $_ -like "*setup*" })) { throw "no visible window titled like 'setup' for the GUI process. Titles seen: $($titles -join ' | ')" }
    "visible windows: $($titles -join ' | ')"
}

# --- the real setup wizard engine, headless: real model download (consent flag), real inference
Check "setup_real_model" {
    $c = Run-Logged "$app\jarvis.exe" "setup-auto --accept-download" "setup_auto"
    Copy-Item "$data\setup_report.json" "$LogDir\setup_report.json" -ErrorAction SilentlyContinue
    Copy-Item "$data\setup_state.json" "$LogDir\setup_state.json" -ErrorAction SilentlyContinue
    if ($c -ne 0) { throw "setup-auto exit $c : $(Get-Content "$LogDir\setup_auto.err.txt" -Raw)" }
    $st = Get-Content "$data\setup_state.json" -Raw | ConvertFrom-Json
    if (-not $st.completed -or -not $st.real) { throw "state says completed=$($st.completed) real=$($st.real)" }
    "completed real=$($st.real) model=$($st.model_id) mode=$($st.mode) gpu_verified=$($st.gpu_verified)"
}
Check "selftest_online" { $c = Run-Logged "$app\jarvis.exe" "selftest" "selftest_online"; Copy-Item "$data\selftest.json" "$LogDir\selftest_online.json" -ErrorAction SilentlyContinue; if ($c -ne 0) { throw "selftest exit $c" }; (Get-Content "$data\selftest.json" -Raw | ConvertFrom-Json | Select-Object model_id, load_seconds, generation_seconds, output_tokens, tokens_per_second, gpu_layers_requested | Out-String) }
Check "diagnostic_export_clean" {
    $c = Run-Logged "$app\jarvis.exe" "diagnostics" "diagnostics"
    $t = Get-Content "$LogDir\diagnostics.out.txt" -Raw
    if ($t -match [regex]::Escape($env:USERNAME) -and $env:USERNAME.Length -ge 3) { throw "Windows user name '$($env:USERNAME)' appears in diagnostics" }
    "no user name in diagnostics (user name length $($env:USERNAME.Length)); chars: $($t.Length)"
}

# --- offline: block this app's network access with firewall rules, prove the block works, then run inference again
Check "block_network_for_app" {
    foreach ($f in "jarvis.exe","jarvis-gui.exe") { netsh advfirewall firewall add rule name="JarvisBlock-$f" dir=out action=block program="$app\$f" enable=yes | Out-Null }
    $c = Run-Logged "$app\jarvis.exe" "models fetch qwen3-1.7b-q4_k_m --yes" "block_proof"
    $err = Get-Content "$LogDir\block_proof.err.txt" -Raw
    if ($c -eq 0) { throw "the app could still download with the firewall rule in place, so the offline test below would prove nothing" }
    if ($err -notmatch "Download failed") { throw "the download failed, but not with a network-level error (so the block is unproven; e.g. the app's own network switch may have refused instead). stderr: $err" }
    "download attempt failed with a network error as expected (exit $c): $($err.Trim())"
}
Check "selftest_offline" { $c = Run-Logged "$app\jarvis.exe" "selftest" "selftest_offline"; Copy-Item "$data\selftest.json" "$LogDir\selftest_offline.json" -ErrorAction SilentlyContinue; if ($c -ne 0) { throw "offline selftest exit $c : $(Get-Content "$LogDir\selftest_offline.err.txt" -Raw)" }; "model answered with the firewall block active" }
Check "unblock_network" { foreach ($f in "jarvis.exe","jarvis-gui.exe") { netsh advfirewall firewall delete rule name="JarvisBlock-$f" | Out-Null }; "rules removed" }
Check "silent_uninstall_keeps_data" {
    $u = Get-ChildItem $app -Filter "unins*.exe" | Select-Object -First 1
    Start-Process $u.FullName -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES" -Wait
    for ($i = 0; $i -lt 60 -and (Test-Path "$app\jarvis.exe"); $i++) { Start-Sleep -Seconds 1 }
    if (Test-Path "$app\jarvis.exe") { throw "app files remain" }
    if (-not (Test-Path $data)) { throw "user data deleted by silent uninstall" }
    "app removed, data kept"
}
$results | ConvertTo-Json -Depth 4 | Out-File "$LogDir\ci-real-model-results.json" -Encoding utf8
Get-Content "$LogDir\ci-real-model-results.json"
if ($results.Values | Where-Object { -not $_.ok }) { exit 1 }
exit 0   # explicit: a stale $LASTEXITCODE from the last native command (for example "chat" expected to exit 2) must not decide the verdict
