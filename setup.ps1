param([switch]$NoAutostart)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    if ($py) { $python = (& $py -3 -c "import sys; print(sys.executable)").Trim() }
}
if (-not $python -and (Test-Path -LiteralPath 'D:\Soft\Python310\python.exe')) {
    $python = 'D:\Soft\Python310\python.exe'
}
if (-not $python -or -not (Test-Path -LiteralPath $python)) {
    throw 'Python not found. Install Python 3.8+ and enable "Add python.exe to PATH", then rerun this script.'
}
& $python (Join-Path $root 'setup.py')
if ($LASTEXITCODE -ne 0) { throw 'setup.py failed.' }
if (-not $NoAutostart) { & (Join-Path $root 'install-autostart.ps1') }
& (Join-Path $root 'start.ps1')
Write-Host ''
Write-Host 'Restart Clash Verge once, then open http://127.0.0.1:18769/'
