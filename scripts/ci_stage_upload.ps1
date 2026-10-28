<#
Copies ONLY vetted files into a staging folder; the workflow uploads that folder and nothing else.
Why staging: a guard that deletes or fails but leaves the upload step pointed at the original folder can be bypassed if the guard itself crashes.
Here the upload path contains only files that already passed every check, so a crash can only mean fewer files, never a rejected one.
Rejected files are named in an error annotation (path, reason, size); their content is never printed.
Usage: scripts\ci_stage_upload.ps1 -Kind logs|installer|bench -Out <staging folder>
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('logs', 'installer', 'bench')][string]$Kind,
  [Parameter(Mandatory = $true)][string]$Out
)
$ErrorActionPreference = 'Continue'
$denyExt  = '.gguf', '.safetensors', '.pt', '.pth', '.onnx', '.pem', '.key', '.pfx', '.p12', '.sqlite', '.sqlite3', '.db', '.db-wal', '.db-shm'
$denyName = '.env', 'jarvis_signing_private.hex', 'id_rsa', 'id_ed25519'
$textExt  = '.log', '.txt', '.json', '.err', '.md', '.csv', '.xml'
$secretRx = '-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,}|hf_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}'
$root = (Get-Location).Path
$BENCH = Join-Path 'ci-logs' 'intel'      # Join-Path, not a literal backslash, so the script also runs under PowerShell on Linux for testing
$INST  = Join-Path 'dist' 'installer'
if ($Kind -eq 'logs')   { $perFile = 20MB;  $total = 25MB;  $items = @(); if (Test-Path ci-logs) { $items += Get-ChildItem ci-logs -Recurse -Force -File }
                          foreach ($p in 'build_info.json') { if (Test-Path $p) { $items += Get-Item $p } }
                          if (Test-Path dist) { $items += Get-ChildItem dist -Filter '*.txt' -File }
                          $base = $root }
if ($Kind -eq 'bench')  { $perFile = 5MB;   $total = 5MB;   $items = @(); if (Test-Path $BENCH) { $items += Get-ChildItem $BENCH -Recurse -Force -File }
                          $base = (Resolve-Path $BENCH -ErrorAction SilentlyContinue).Path }
if ($Kind -eq 'installer') { $perFile = 300MB; $total = 300MB; $items = @(); if (Test-Path $INST) { $items += Get-ChildItem $INST -Force -File }
                          $base = (Resolve-Path $INST -ErrorAction SilentlyContinue).Path }
New-Item -ItemType Directory -Force $Out | Out-Null
$bad = 0; $sum = 0L; $kept = 0
foreach ($f in ($items | Sort-Object Length)) {
  $rel = $f.FullName.Substring($base.Length).TrimStart('\', '/')
  $why = $null; $ext = $f.Extension.ToLowerInvariant()
  if ($Kind -eq 'installer') {
    if ($f.Name -notlike 'ProjectJarvis-Setup-*.exe' -and $f.Name -ne 'SHA256SUMS.txt') { $why = 'not part of the installer upload set' }
  }
  if (-not $why -and ($denyExt -contains $ext)) { $why = "extension $ext is never uploaded" }
  if (-not $why -and ($denyName -contains $f.Name.ToLowerInvariant())) { $why = 'file name is on the secrets list' }
  if (-not $why -and $f.Length -gt $perFile) { $why = "larger than the per-file cap of $perFile bytes" }
  if (-not $why -and ($sum + $f.Length) -gt $total) { $why = "would exceed the upload-set cap of $total bytes" }
  if (-not $why -and $f.Length -ge 4) {
    try { $fs = [System.IO.File]::OpenRead($f.FullName); $b = New-Object byte[] 4; [void]$fs.Read($b, 0, 4); $fs.Close()
          if ([System.Text.Encoding]::ASCII.GetString($b) -eq 'GGUF') { $why = 'GGUF model header' }
          if (-not $why -and $Kind -eq 'installer' -and $ext -eq '.exe' -and [System.Text.Encoding]::ASCII.GetString($b, 0, 2) -ne 'MZ') { $why = 'installer is not a Windows executable' } }
    catch { $why = 'could not be read for checking' }
  }
  if (-not $why -and $Kind -ne 'installer' -and ($textExt -contains $ext)) {
    try { if ((Get-Content -LiteralPath $f.FullName -Raw -ErrorAction Stop) -match $secretRx) { $why = 'content matches a secret/token pattern' } }
    catch { $why = 'could not be read for checking' }
  }
  if ($why) { $bad++; Write-Host "::error title=upload guard::withheld $rel ($why, $($f.Length) bytes)"; continue }
  $dest = Join-Path $Out $rel; New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
  Copy-Item -LiteralPath $f.FullName -Destination $dest; $sum += $f.Length; $kept++
}
if ($Kind -eq 'installer') {
  $exe = @(Get-ChildItem $Out -Filter 'ProjectJarvis-Setup-*.exe' -File -ErrorAction SilentlyContinue)
  if ($exe.Count -ne 1) { Write-Host "::error title=upload guard::expected exactly one installer, staged $($exe.Count)"; $bad++ }
}
Write-Host "upload guard ($Kind): staged $kept file(s), $sum bytes, withheld $bad"
if ($bad -gt 0) { exit 1 }
exit 0
