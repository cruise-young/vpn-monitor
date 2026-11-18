$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'data\monitor.pid'
if (Test-Path -LiteralPath $pidFile) {
    $monitorPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($monitorPid -and (Get-Process -Id $monitorPid -ErrorAction SilentlyContinue)) {
        Write-Host "Monitor is already running (PID $monitorPid): http://127.0.0.1:18769/"
        exit 0
    }
}
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    $py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    if ($py) {
        $python = (& $py -3 -c "import os,sys; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))").Trim()
    }
}
if (-not $python) { $python = 'D:\Soft\Python310\pythonw.exe' }
if (-not (Test-Path -LiteralPath $python)) { throw "Python not found; install Python 3.8+ or set pythonw.exe on PATH." }
Start-Process -FilePath $python -ArgumentList ('"' + (Join-Path $PSScriptRoot 'vpn_monitor.py') + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
Write-Host 'Monitor started: http://127.0.0.1:18769/'
