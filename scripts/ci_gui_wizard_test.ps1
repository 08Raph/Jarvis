# Prompt 22 / Stage 4A - click-through test of the INSTALLED jarvis-gui.exe on a Windows desktop session (GitHub-hosted runner; not a customer PC).
# Interaction method: Microsoft UI Automation (System.Windows.Automation) finds the real controls by name; every button press is a REAL mouse click
# (SetCursorPos + mouse_event at the control's on-screen centre); text is typed with real key events (SendKeys). Nothing calls internal functions and
# nothing is simulated. Every scenario records ok/detail; screenshots (full desktop PNG) and the UI-Automation tree are saved at each key moment.
# Scenarios (each starts a fresh jarvis-gui.exe):
#   S1 wizard shows details + consent button; nothing downloads before consent; "Set up later" -> honest no-model app
#   S2 "Cancel" in the wizard -> not complete
#   S3 download with the network BLOCKED -> failure shown, never "finished"; unblock -> Retry -> Cancel mid-download -> Retry -> close the window mid-download
#   S4 full download via the wizard -> integrity + load + real inference -> "Launch Jarvis" -> main window answers a real question
#   S5 reopen after success: no wizard, answers again
#   S6 integrity failure: one byte of the installed model is changed -> setup must FAIL; restore -> Retry succeeds
# The script never hides a failure: each scenario is wrapped, a failed assertion is recorded with its message, the app is closed, and the next scenario runs.
param([Parameter(Mandatory = $true)][string]$Installer, [string]$LogDir = "ci-logs\gui")
$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogDir = (Resolve-Path $LogDir).Path
$app = Join-Path $env:LOCALAPPDATA "Programs\ProjectJarvis"
$data = Join-Path $env:LOCALAPPDATA "ProjectJarvis"
$gui = "$app\jarvis-gui.exe"
$transcript = "$LogDir\transcript.txt"
$results = [ordered]@{}
$script:shotNo = 0
$script:fwRules = @()
$script:uiaClicks = 0
$script:typed = @()

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Drawing, System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public class GuiNative {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
"@
[void][GuiNative]::SetProcessDPIAware()
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]

function Log($m) { $line = "{0:HH:mm:ss} {1}" -f (Get-Date), $m; Write-Host $line; Add-Content -Path $transcript -Value $line }
function Scenario($name, $block) {
    Log "=== SCENARIO $name"
    $script:notes = New-Object System.Collections.Generic.List[string]
    try { & $block; $results[$name] = @{ ok = $true; detail = ($script:notes -join " | ") } }
    catch { $results[$name] = @{ ok = $false; detail = ("ASSERTION/ERROR: " + $_.Exception.Message + " || notes: " + ($script:notes -join " | ")) } }
    Log ("=== {0}: {1}" -f $name, $(if ($results[$name].ok) { "PASS" } else { "FAIL - " + $results[$name].detail }))
}
function Note($m) { $script:notes.Add($m); Log "  note: $m" }
function Clip($t, [int]$n) { $x = (([string]$t) -replace '\s+', ' ').Trim(); if ($x.Length -gt $n) { return $x.Substring(0, $n) } else { return $x } }
function Assert($cond, $msg) { if (-not $cond) { throw $msg } }

function Shot($name) {
    $script:shotNo++
    $f = "{0}\{1:00}-{2}.png" -f $LogDir, $script:shotNo, $name
    try {
        $w = [System.Windows.Forms.SystemInformation]::PrimaryMonitorSize
        $bmp = New-Object System.Drawing.Bitmap($w.Width, $w.Height)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen(0, 0, 0, 0, $bmp.Size)
        $bmp.Save($f); $g.Dispose(); $bmp.Dispose()
        Log ("  screenshot {0} ({1} bytes)" -f (Split-Path $f -Leaf), (Get-Item $f).Length)
    } catch { Log "  screenshot failed: $($_.Exception.Message)" }
}

# ---------------------------------------------------------------- UI Automation helpers
function Top-Windows([int]$procId) {
    $c = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $procId)
    $col = $AE::RootElement.FindAll($TS::Children, $c)
    $out = @(); foreach ($e in $col) { $out += $e }
    # a modal dialog (the wizard, a message box) is OWNED by the main window and UI Automation lists it below its owner, not on the desktop
    $wc = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)
    foreach ($o in @($out)) { try { foreach ($c in $o.FindAll($TS::Children, $wc)) { $out += $c } } catch { } }
    return $out
}
function Find-Win([int]$procId, [string]$pattern) {
    foreach ($w in (Top-Windows $procId)) { try { if ($w.Current.Name -like $pattern) { return $w } } catch { } }
    return $null
}
function Wait-Win([int]$procId, [string]$pattern, [int]$sec) {
    $end = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $end) { $w = Find-Win $procId $pattern; if ($w) { return $w }; Start-Sleep -Milliseconds 500 }
    return $null
}
function Wait-Gone([int]$procId, [string]$pattern, [int]$sec) {
    $end = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $end) { if (-not (Find-Win $procId $pattern)) { return $true }; Start-Sleep -Milliseconds 500 }
    return $false
}
function All-Els($root) {
    $col = $root.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    $out = @(); foreach ($e in $col) { $out += $e }; return $out
}
function El-Text($el) {
    $p = $null
    try { if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$p)) { $v = $p.Current.Value; if ($v) { return [string]$v } } } catch { }
    $p = $null
    try { if ($el.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$p)) { $t = $p.DocumentRange.GetText(30000); if ($t) { return [string]$t } } } catch { }
    try { return [string]$el.Current.Name } catch { return "" }
}
function Visible-Texts($root) {
    # every element's name and text; offscreen/disabled flags included, so "Launch Jarvis exists but is hidden" cannot be mistaken for visible
    $lines = @()
    foreach ($e in (All-Els $root)) {
        try {
            $c = $e.Current
            $lines += ("{0}|{1}|off={2}|en={3}|{4}" -f $c.ControlType.ProgrammaticName.Replace("ControlType.", ""), ($c.Name -replace "\s+", " "), $c.IsOffscreen, $c.IsEnabled, ((El-Text $e) -replace "\s+", " ").Trim())
        } catch { }
    }
    return $lines
}
function Dump-Tree($root, $name) {
    $f = "{0}\tree-{1}.txt" -f $LogDir, $name
    try { Visible-Texts $root | Out-File $f -Encoding utf8 } catch { Log "  tree dump failed: $($_.Exception.Message)" }
}
function Find-Btn($root, [string]$regex) {
    foreach ($e in (All-Els $root)) {
        try {
            if ($e.Current.ControlType.ProgrammaticName -eq "ControlType.Button" -and $e.Current.Name -match $regex -and -not $e.Current.IsOffscreen) { return $e }
        } catch { }
    }
    return $null
}
function Find-Named($root, [string]$name) {
    foreach ($e in (All-Els $root)) { try { if ($e.Current.Name -eq $name) { return $e } } catch { } }
    return $null
}
function Text-Of($root, [string]$name) { $e = Find-Named $root $name; if ($e) { return (El-Text $e) } else { return $null } }
function All-Text($root) { return ((Visible-Texts $root) -join "`n") }
function Click-El($el, $win) {
    try { [void][GuiNative]::SetForegroundWindow([IntPtr]$win.Current.NativeWindowHandle) } catch { }
    Start-Sleep -Milliseconds 200
    $r = $el.Current.BoundingRectangle
    if ($r.IsEmpty -or $el.Current.IsOffscreen) { throw "control '$($el.Current.Name)' has no on-screen rectangle (not visible)" }
    $x = [int]($r.X + $r.Width / 2); $y = [int]($r.Y + $r.Height / 2)
    [void][GuiNative]::SetCursorPos($x, $y); Start-Sleep -Milliseconds 200
    [GuiNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 80; [GuiNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    $script:uiaClicks++
    Log ("  REAL mouse click at ({0},{1}) on '{2}'" -f $x, $y, $el.Current.Name)
}
function Click-Btn($win, [string]$regex, [int]$waitSec = 30) {
    $end = (Get-Date).AddSeconds($waitSec); $b = $null
    while ((Get-Date) -lt $end) {
        $b = Find-Btn $win $regex
        if ($b -and $b.Current.IsEnabled) { break }
        Start-Sleep -Milliseconds 500
    }
    Assert ($b -and $b.Current.IsEnabled) "button matching /$regex/ not found or not enabled within $waitSec s"
    Click-El $b $win
}
function Wait-Text($win, [string]$regex, [int]$sec) {
    $end = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $end) { $t = All-Text $win; if ($t -match $regex) { return $true }; Start-Sleep -Seconds 1 }
    return $false
}

# ---------------------------------------------------------------- app/process helpers
function Start-App {
    $p = Start-Process $gui -PassThru
    $null = $p.Handle
    Log "started jarvis-gui.exe pid $($p.Id)"
    return $p
}
function Stop-AppGracefully($p) {
    if ($p.HasExited) { Note "app had already exited (code $($p.ExitCode))"; return }
    $end = (Get-Date).AddSeconds(40); $answered = @()
    while (-not $p.HasExited -and (Get-Date) -lt $end) {
        $wins = Top-Windows $p.Id
        # a "Close Jarvis?" question or an information box may be open: answer it like a user would (Yes / OK) with a real click
        $box = $wins | Where-Object { try { $_.Current.Name -match "^(Close Jarvis\?|Project JARVIS)$" } catch { $false } } | Select-Object -First 1
        if ($box) { $b = Find-Btn $box "^(&?Yes|Yes|OK)$"; if ($b) { $answered += $box.Current.Name; try { Click-El $b $box } catch { } ; Start-Sleep -Seconds 1; continue } }
        foreach ($w in ($wins | Where-Object { try { $_.Current.Name -notmatch "^(Close Jarvis\?)$" } catch { $false } } | Select-Object -Last 1)) {
            try { $wp = $null; if ($w.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$wp)) { $wp.Close() } } catch { }
        }
        Start-Sleep -Seconds 2
    }
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Note "app did not exit within 40 s after close requests; force-stopped (a finding)"; Start-Sleep -Seconds 2 }
    else { Note ("app exited after the window close request (exit code {0}){1}" -f $p.ExitCode, $(if ($answered.Count) { "; dialogs answered: " + ($answered -join ", ") } else { "" })) }
}
function Get-Setup { try { return (Get-Content "$data\setup_state.json" -Raw -ErrorAction Stop | ConvertFrom-Json) } catch { return $null } }
function Big-Files { if (Test-Path $data) { return @(Get-ChildItem $data -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 1MB }) } else { return @() } }
function Reset-Data { Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 500 }
function Block-Net { foreach ($f in "jarvis-gui.exe", "jarvis.exe") { netsh advfirewall firewall add rule name="JarvisGuiBlock-$f" dir=out action=block program="$app\$f" enable=yes | Out-Null; $script:fwRules += "JarvisGuiBlock-$f" } }
function Unblock-Net { foreach ($r in $script:fwRules) { netsh advfirewall firewall delete rule name="$r" | Out-Null }; $script:fwRules = @() }
function Progress-Sample($win) {
    # step label text and progress-bar value as the UI shows them right now
    $step = $null; $bar = $null
    foreach ($e in (All-Els $win)) {
        try {
            $c = $e.Current
            if ($c.ControlType.ProgrammaticName -eq "ControlType.ProgressBar") { $rv = $null; if ($e.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$rv)) { $bar = $rv.Current.Value } }
            elseif ($c.ControlType.ProgrammaticName -eq "ControlType.Text" -and $c.Name -match "^(Download|Verify|System|Model|Cancelling)") { $step = $c.Name }
        } catch { }
    }
    return [pscustomobject]@{ step = $step; bar = $bar }
}
function Open-Wizard($p, $sec = 90) {
    $w = Wait-Win $p.Id "*setup" $sec
    if (-not $w) { $names = (Top-Windows $p.Id | ForEach-Object { try { $_.Current.Name } catch { "?" } }) -join " | "; Log "  windows of the process: $names"; Note "windows seen: $names"; Shot "no-wizard-found"; foreach ($t in (Top-Windows $p.Id)) { Dump-Tree $t ("nowizard-" + (($t.Current.Name -replace '[^A-Za-z0-9]', '_'))) } }
    Assert $w "no window titled '* setup' appeared within $sec s"
    Start-Sleep -Seconds 2
    return $w
}
function Wizard-Result($w) { return (Text-Of $w "Setup results") }
function Wait-Preview($w) {
    # the PC check finishes by filling in the model information
    Assert (Wait-Text $w "(download about|already installed)" 90) "the wizard never showed the model information"
}
function Ask($p, [string]$question, [string]$expectRegex, [int]$sec = 240) {
    $main = Wait-Win $p.Id "Project JARVIS 0*" 60
    Assert $main "main window did not appear"
    $inp = $null
    foreach ($e in (All-Els $main)) { try { if ($e.Current.Name -eq "Message" -and $e.Current.ControlType.ProgrammaticName -ne "ControlType.Text") { $inp = $e; break } } catch { } }
    Assert $inp "chat input 'Message' not found"
    Dump-Tree $main "main-before-typing"
    Click-El $inp $main
    Start-Sleep -Milliseconds 400
    [System.Windows.Forms.SendKeys]::SendWait($question)
    $script:typed += $question
    Start-Sleep -Milliseconds 600
    $typedBack = El-Text $inp
    Note ("typed with real key events; the box then holds: '{0}'" -f ($typedBack -replace "\s+", " ").Trim())
    if ($typedBack -notmatch [regex]::Escape($question.Substring(0, [Math]::Min(11, $question.Length)))) { Note "Message box text not readable through UI Automation; relying on the echo in the conversation instead"; Shot "typed-unreadable"; Dump-Tree $main "main-after-typing" }
    Click-Btn $main "^Send$" 10
    $ok = $false; $conv = $null
    $end = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $end) {
        $conv = Text-Of $main "Conversation"
        if ($conv -and $conv -match $expectRegex) { $ok = $true; break }
        Start-Sleep -Seconds 2
    }
    Dump-Tree $main "main-after-question"
    return [pscustomobject]@{ ok = $ok; conversation = $conv; main = $main }
}

# =================================================================== install (fresh)
Scenario "S0_install" {
    $p = Start-Process $Installer -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=$LogDir\install.log" -PassThru
    $null = $p.Handle; $p.WaitForExit()
    Assert ($p.ExitCode -eq 0) "installer exit code '$($p.ExitCode)'"
    Assert (Test-Path $gui) "jarvis-gui.exe missing after install"
    Reset-Data
    Note ("installed; jarvis-gui.exe {0} bytes" -f (Get-Item $gui).Length)
}

try {
# =================================================================== S1: details, consent, Set up later
Scenario "S1_details_consent_and_set_up_later" {
    Reset-Data
    $p = Start-App
    try {
        $w = Open-Wizard $p
        Wait-Preview $w
        Shot "s1-wizard-first-launch"
        Dump-Tree $w "s1-wizard"
        $all = All-Text $w
        $info = Text-Of $w "Model"        # combo box
        $infoLine = ($all -split "`n" | Where-Object { $_ -match "download about" } | Select-Object -First 1)
        Assert $infoLine "model information line not shown"
        Assert ($infoLine -match "download about 1\.28 GB") "model size not displayed as 1.28 GB: $infoLine"
        Assert ($infoLine -match "Apache-2\.0") "licence not displayed: $infoLine"
        Assert ($infoLine -match "Estimated memory needed: [0-9.]+ GB") "resource estimate not displayed: $infoLine"
        Assert ($infoLine -match "pretrained third-party model") "third-party disclosure missing: $infoLine"
        Note "model info shown: $(Clip $infoLine 300)"
        Assert ($all -match "Windows: ") "system section not shown"
        Assert ($all -match "GPU:") "GPU line not shown"
        $go = Find-Btn $w "^Download 1\.28 GB and set up$"
        Assert $go "consent button 'Download 1.28 GB and set up' not found"
        Assert $go.Current.IsEnabled "consent button is not enabled"
        # nothing may be downloaded, and nothing may be connected, before the button is pressed
        Start-Sleep -Seconds 10
        Assert ((Big-Files).Count -eq 0) "files > 1 MB exist before consent: $((Big-Files | ForEach-Object { $_.Name }) -join ',')"
        $ns = @(netstat -ano -p tcp | Where-Object { $_ -match "\s$($p.Id)\s*$" -and $_ -notmatch "(127\.0\.0\.1|\[::1\]|0\.0\.0\.0:0|LISTENING)" })
        Assert ($ns.Count -eq 0) "network connections exist before consent: $($ns -join ' ; ')"
        Note "10 s with the wizard open: no files over 1 MB, no non-local TCP connections"
        $st = Get-Setup; Assert (-not ($st -and $st.completed)) "setup already marked complete before anything happened"
        # Set up later
        Click-Btn $w "^Set up later$"
        Assert (Wait-Gone $p.Id "*setup" 30) "wizard did not close after 'Set up later'"
        $main = Wait-Win $p.Id "Project JARVIS 0*" 30
        Assert $main "main window not available after 'Set up later'"
        Shot "s1-main-after-set-up-later"
        $r = Ask $p "hello there" "(?i)(no model|not installed|no chat model|set up)" 60
        Note "chat without a model says: $((Clip $r.conversation 250))"
        Assert $r.ok "the chat did not give an honest 'no model' message"
        Assert ($r.conversation -notmatch "(?i)Paris|I am Jarvis") "the chat produced assistant-like text without a model"
        Shot "s1-main-chat-without-model"
        $st = Get-Setup
        Assert (-not ($st -and $st.completed)) "'Set up later' left setup marked complete"
        Assert ((Big-Files).Count -eq 0) "a model file exists after 'Set up later'"
    } finally { Stop-AppGracefully $p }
}

# =================================================================== S2: Cancel
Scenario "S2_cancel_button" {
    $p = Start-App
    try {
        $w = Open-Wizard $p
        Wait-Preview $w
        Click-Btn $w "^Cancel$"
        Assert (Wait-Gone $p.Id "*setup" 30) "wizard did not close after Cancel"
        $st = Get-Setup
        Assert (-not ($st -and $st.completed)) "Cancel left setup marked complete"
        Assert ((Big-Files).Count -eq 0) "Cancel left a model file"
        Assert (Wait-Win $p.Id "Project JARVIS 0*" 20) "main window missing after Cancel"
        Shot "s2-main-after-cancel"
    } finally { Stop-AppGracefully $p }
}

# =================================================================== S3: network failure, retry, cancel mid-download, close mid-download
Scenario "S3_failure_retry_cancel_close" {
    Block-Net
    $p = Start-App
    try {
        $w = Open-Wizard $p
        Wait-Preview $w
        Click-Btn $w "^Download 1\.28 GB and set up$"
        Note "consent given by clicking the download button, with the app's network access BLOCKED by a firewall rule"
        Assert (Wait-Text $w "did not finish" 240) "the wizard did not report a failure within 240 s with the network blocked"
        Shot "s3-network-failure"
        Dump-Tree $w "s3-failure"
        $all = All-Text $w
        $res = Wizard-Result $w
        Note "failure text: $((Clip $res 300))"
        Assert ($res -match "(?i)download failed|network|internet") "failure text does not mention the download/network problem"
        Assert ($all -notmatch "Setup finished") "wizard claims 'Setup finished' after a failed download"
        Assert (-not (Find-Btn $w "^Launch Jarvis$")) "'Launch Jarvis' is visible after a failed download"
        Assert ((Find-Btn $w "^Retry$")) "no Retry button after the failure"
        $st = Get-Setup; Assert (-not ($st -and $st.completed)) "failed download left setup marked complete"
        Assert ((Big-Files).Count -eq 0) "failed download left a model/part file"
        Unblock-Net
        Note "firewall rules removed; pressing Retry"
        # Retry, then Cancel while the download is running
        Click-Btn $w "^Retry$"
        $seen = New-Object System.Collections.Generic.List[string]; $started = $false
        $end = (Get-Date).AddSeconds(120)
        while ((Get-Date) -lt $end) {
            $s = Progress-Sample $w
            if ($s.step) { $seen.Add($s.step) }
            if ($s.step -match "^Download: ([0-9,]+) / ([0-9,]+) MB" -and [int]($Matches[1] -replace ",", "") -ge 100) { $started = $true; break }
            Start-Sleep -Milliseconds 700
        }
        Assert $started "download progress never reached 100 MB after Retry. Seen: $(($seen | Select-Object -Last 3) -join ' / ')"
        Shot "s3-downloading"
        Note "progress shown in the UI during download: $(($seen | Select-Object -Last 2) -join ' ... ')"
        Click-Btn $w "^Cancel$"
        Assert (Wait-Text $w "did not finish" 90) "after Cancel the wizard did not end the run"
        Shot "s3-after-cancel"
        $res = Wizard-Result $w
        Note "after cancel: $((Clip $res 200))"
        Assert ($res -match "(?i)cancel") "result does not say the setup was cancelled"
        Assert ((All-Text $w) -notmatch "Setup finished") "claims finished after Cancel"
        Assert (-not (Find-Btn $w "^Launch Jarvis$")) "'Launch Jarvis' visible after Cancel"
        Start-Sleep -Seconds 2
        $st = Get-Setup; Assert (-not ($st -and $st.completed)) "Cancel mid-download left setup complete"
        Assert ((Big-Files).Count -eq 0) "Cancel mid-download left a partial file: $((Big-Files | ForEach-Object { $_.Name + ' ' + $_.Length }) -join ',')"
        # Retry again; this time close the window itself (X) while downloading
        Click-Btn $w "^Retry$"
        $started = $false; $end = (Get-Date).AddSeconds(120)
        while ((Get-Date) -lt $end) {
            $s = Progress-Sample $w
            if ($s.step -match "^Download: ([0-9,]+) / ([0-9,]+) MB" -and [int]($Matches[1] -replace ",", "") -ge 100) { $started = $true; break }
            Start-Sleep -Milliseconds 700
        }
        Assert $started "second Retry did not reach 100 MB"
        $wp = $null
        Assert ($w.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$wp)) "wizard has no WindowPattern (cannot send a close request)"
        $wp.Close()
        Note "close request (window X) sent to the wizard mid-download"
        Assert (Wait-Gone $p.Id "*setup" 90) "wizard did not close after the close request (it should cancel and close)"
        Start-Sleep -Seconds 5
        Shot "s3-main-after-closing-wizard"
        $st = Get-Setup; Assert (-not ($st -and $st.completed)) "closing the wizard mid-download left setup marked complete"
        $bf = Big-Files
        Assert ($bf.Count -eq 0) "closing the wizard mid-download left files: $(($bf | ForEach-Object { $_.Name + ' ' + $_.Length }) -join ',')"
        Start-Sleep -Seconds 15
        Assert ((Big-Files).Count -eq 0) "a hidden download kept running after the wizard was closed"
        Note "after closing mid-download: no model, no partial file, setup not complete (also 20 s later)"
    } finally { Unblock-Net; Stop-AppGracefully $p }
}

# =================================================================== S4: full successful setup through the wizard, then a real question
Scenario "S4_full_setup_then_question" {
    $p = Start-App
    try {
        $w = Open-Wizard $p
        Wait-Preview $w
        Click-Btn $w "^Download 1\.28 GB and set up$"
        Note "consent given by a real click on the download button"
        $bars = New-Object System.Collections.Generic.List[double]; $steps = New-Object System.Collections.Generic.List[string]; $mbs = New-Object System.Collections.Generic.List[int]
        $end = (Get-Date).AddSeconds(420); $done = $false
        while ((Get-Date) -lt $end) {
            $s = Progress-Sample $w
            if ($s.step -and ($steps.Count -eq 0 -or $steps[$steps.Count - 1] -ne $s.step)) { $steps.Add($s.step) }
            if ($s.bar -ne $null) { $bars.Add([double]$s.bar) }
            if ($s.step -match "^Download: ([0-9,]+) /") { $mbs.Add([int]($Matches[1] -replace ",", "")) }
            if (Find-Btn $w "^Launch Jarvis$") { $done = $true; break }
            if ((All-Text $w) -match "did not finish") { break }
            Start-Sleep -Milliseconds 700
        }
        Shot "s4-after-setup"
        Dump-Tree $w "s4-wizard-end"
        Assert $done "wizard did not reach the 'Launch Jarvis' state; last steps: $(($steps | Select-Object -Last 4) -join ' / ')"
        $uniq = @($mbs | Select-Object -Unique)
        Assert ($uniq.Count -ge 3) "fewer than 3 distinct download progress values were observed in the UI ($($uniq.Count))"
        $inc = $true; for ($i = 1; $i -lt $mbs.Count; $i++) { if ($mbs[$i] -lt $mbs[$i - 1]) { $inc = $false } }
        Assert $inc "download progress shown in the UI went backwards"
        Note ("UI progress: {0} samples, {1} distinct MB values, last {2} MB; labels seen: {3}" -f $mbs.Count, $uniq.Count, ($mbs | Select-Object -Last 1), (($steps | Select-Object -First 6) -join ' > '))
        $all = All-Text $w; $res = Wizard-Result $w
        Assert ($all -match "Setup finished\. The model answered a real question on this PC") "completion text not shown"
        Assert ($res -match "\[ok\] model file integrity \(full SHA-256\)") "integrity check line missing/failed in the wizard result: $res"
        Assert ($res -notmatch "\[FAILED\]") "a FAILED check is shown in the wizard result"
        Assert ($res -match "\[ok\] the app's own engine answers") "the 'app's own engine answers' check is missing"
        Assert ($res -match "Mode: CPU") "mode CPU not shown"
        Note ("wizard result: " + (Clip $res 500))
        $st = Get-Setup
        Assert ($st -and $st.completed -and $st.real) "setup_state.json does not say completed+real: $($st | ConvertTo-Json -Compress)"
        Assert ($st.model_id -eq "qwen3-1.7b-q4_k_m") "unexpected model id $($st.model_id)"
        $gguf = Get-ChildItem $data -Recurse -Filter "model.gguf" | Select-Object -First 1
        Assert ($gguf -and $gguf.Length -gt 1GB) "model.gguf missing or too small"
        Note ("setup_state: completed={0} real={1} mode={2} gpu_verified={3}; model.gguf {4} bytes" -f $st.completed, $st.real, $st.mode, $st.gpu_verified, $gguf.Length)
        Click-Btn $w "^Launch Jarvis$"
        Assert (Wait-Gone $p.Id "*setup" 30) "wizard did not close after 'Launch Jarvis'"
        Shot "s4-main-after-launch"
        $r = Ask $p "What is the capital of France? Answer in one short sentence." "Paris" 240
        Shot "s4-main-answer"
        Assert $r.ok "the main window did not show an answer containing 'Paris'. Conversation: $((($r.conversation -replace '\s+', ' ')))"
        Note "main window conversation: $((Clip $r.conversation 300))"
    } finally { Stop-AppGracefully $p }
}

# =================================================================== S5: reopen after success
Scenario "S5_reopen_after_success" {
    $p = Start-App
    try {
        $main = Wait-Win $p.Id "Project JARVIS 0*" 90
        Assert $main "main window did not appear"
        Start-Sleep -Seconds 12
        Assert (-not (Find-Win $p.Id "*setup")) "the setup wizard appeared again although setup had completed"
        Shot "s5-reopen-no-wizard"
        $r = Ask $p "What is the capital of Italy? Answer in one short sentence." "Rome" 240
        Assert $r.ok "no answer containing 'Rome' after reopening. Conversation: $((($r.conversation -replace '\s+', ' ')))"
        Note "after reopening: no wizard; answered: $((Clip $r.conversation 250))"
    } finally { Stop-AppGracefully $p }
}

# =================================================================== S6: a damaged model must never be accepted; with no network it must FAIL, then Retry repairs it from the verified source
Scenario "S6_damaged_model_never_accepted" {
    $PINNED = "d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5"
    $gguf = Get-Item "$data\models\qwen3-1.7b-q4_k_m\model.gguf" -ErrorAction SilentlyContinue
    Assert $gguf "no installed model to tamper with"
    $off = [int64]($gguf.Length / 2)
    $fs = [System.IO.File]::Open($gguf.FullName, "Open", "ReadWrite", "None")
    $orig = New-Object byte[] 1
    try { [void]$fs.Seek($off, "Begin"); [void]$fs.Read($orig, 0, 1); [void]$fs.Seek($off, "Begin"); $fs.WriteByte([byte](($orig[0] + 1) % 256)) } finally { $fs.Close() }
    Note "changed 1 byte of the installed model at offset $off; setup_state deleted so the wizard runs again; network BLOCKED"
    Remove-Item "$data\setup_state.json" -Force
    Block-Net
    $p = Start-App
    try {
        $w = Open-Wizard $p
        Wait-Preview $w
        Shot "s6-wizard-tampered"
        Dump-Tree $w "s6-wizard"
        $btn = (Find-Btn $w "^(Check and finish setup|Download .* and set up)$")
        Assert $btn "no primary setup button"
        Note "primary button offered for a damaged model: '$($btn.Current.Name)'"
        Click-Btn $w "^(Check and finish setup|Download .* and set up)$"
        Assert (Wait-Text $w "did not finish" 240) "with a damaged model and no network the wizard did not end with a failure"
        Shot "s6-damaged-no-network"
        $res = Wizard-Result $w
        Note ("result: " + (Clip $res 300))
        Assert ((All-Text $w) -notmatch "Setup finished") "claims finished with a damaged model"
        Assert (-not (Find-Btn $w "^Launch Jarvis$")) "'Launch Jarvis' offered with a damaged model"
        $st = Get-Setup; Assert (-not ($st -and $st.completed)) "setup marked complete with a damaged model"
        Unblock-Net
        Click-Btn $w "^Retry$"
        $ok = $false; $end = (Get-Date).AddSeconds(420)
        while ((Get-Date) -lt $end) { if (Find-Btn $w "^Launch Jarvis$") { $ok = $true; break }; if ((All-Text $w) -match "did not finish") { Start-Sleep -Seconds 5; if (-not (Find-Btn $w "^Retry$")) { } }; Start-Sleep -Seconds 2 }
        Shot "s6-retry-result"
        Assert $ok "Retry with the network back did not complete setup"
        $h = (Get-FileHash "$data\models\qwen3-1.7b-q4_k_m\model.gguf" -Algorithm SHA256).Hash.ToLower()
        Assert ($h -eq $PINNED) "after Retry the model hash is $h, not the pinned publisher hash"
        $st = Get-Setup; Assert ($st -and $st.completed -and $st.real -and $st.model_id -eq "qwen3-1.7b-q4_k_m") "state not complete for the pinned model after Retry"
        Note "Retry re-fetched the model; its SHA-256 equals the pinned publisher hash $PINNED; setup then completed"
        Click-Btn $w "^Launch Jarvis$"
        Assert (Wait-Gone $p.Id "*setup" 30) "wizard did not close after Launch"
    } finally { Unblock-Net; Stop-AppGracefully $p }
}
} finally { Unblock-Net }

# =================================================================== summary
$results["_meta"] = @{ ok = $true; detail = ("real mouse clicks: {0}; typed strings: {1}; screenshots: {2}; method: UI Automation + mouse_event + SendKeys on the runner's interactive desktop" -f $script:uiaClicks, $script:typed.Count, $script:shotNo) }
$results | ConvertTo-Json -Depth 4 | Out-File "$LogDir\gui-results.json" -Encoding utf8
($results.GetEnumerator() | ForEach-Object { "{0} {1}: {2}" -f $(if ($_.Value.ok) { "PASS  " } else { "FAILED" }), $_.Key, (($_.Value.detail -replace "\s+", " ").Trim()) }) | Out-File "$LogDir\gui-summary.txt" -Encoding utf8
Get-Content "$LogDir\gui-summary.txt"
if ($results.Values | Where-Object { -not $_.ok }) { exit 1 }
exit 0
