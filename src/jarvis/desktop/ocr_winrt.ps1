# Windows built-in OCR (Windows.Media.Ocr) from Windows PowerShell 5.1. Reads a PNG, prints one JSON object.
# Uses the OCR languages installed in Windows (Settings > Time & language > Language). Nothing leaves this PC.
param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference = 'Stop'
# Windows PowerShell writes redirected output in the OEM code page; escape every non-ASCII character so the JSON is pure ASCII.
function Out-AsciiJson($obj) {
  $json = $obj | ConvertTo-Json -Compress -Depth 5
  [regex]::Replace($json, '[^\x00-\x7F]', [System.Text.RegularExpressions.MatchEvaluator]{ param($m) ('\u{0:x4}' -f [int][char]$m.Value) })
}
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $null = $task.Wait(-1); $task.Result }
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if ($null -eq $engine) { Out-AsciiJson @{ error = 'no OCR language is installed in Windows' }; exit 2 }
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $lines = @()
  foreach ($l in $result.Lines) {
    $xs = $l.Words | ForEach-Object { $_.BoundingRect.X }; $ys = $l.Words | ForEach-Object { $_.BoundingRect.Y }
    $rs = $l.Words | ForEach-Object { $_.BoundingRect.X + $_.BoundingRect.Width }; $bs = $l.Words | ForEach-Object { $_.BoundingRect.Y + $_.BoundingRect.Height }
    $lines += @{ text = $l.Text; rect = @([int]($xs | Measure-Object -Minimum).Minimum, [int]($ys | Measure-Object -Minimum).Minimum,
                                          [int]($rs | Measure-Object -Maximum).Maximum, [int]($bs | Measure-Object -Maximum).Maximum) }
  }
  Out-AsciiJson @{ engine = 'Windows.Media.Ocr'; language = $engine.RecognizerLanguage.LanguageTag; lines = $lines }
} catch {
  Out-AsciiJson @{ error = $_.Exception.Message }
  exit 1
}
