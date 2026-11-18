$dataPath = Join-Path $PSScriptRoot 'data'
if (Test-Path -LiteralPath $dataPath) { Set-Content -LiteralPath (Join-Path $dataPath 'stop.request') -Value 'stop' -Encoding ascii }
Write-Host 'Stop requested. The collector flushes records before exit (normally within 6 seconds).'
