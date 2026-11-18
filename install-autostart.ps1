$ErrorActionPreference = 'Stop'
$startupPath = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupPath 'LocalMihomoTrafficMonitor.lnk'
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    if ($py) {
        $pythonw = (& $py -3 -c "import os,sys; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))").Trim()
    }
}
if (-not $pythonw) { $pythonw = 'D:\Soft\Python310\pythonw.exe' }
if (-not (Test-Path -LiteralPath $pythonw)) { throw "Python not found; install Python 3.8+ or set pythonw.exe on PATH." }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'vpn_monitor.py') + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Local Mihomo traffic statistics (application and domain)'
$shortcut.Save()
Write-Host "Installed login startup: $shortcutPath"
