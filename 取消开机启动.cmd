@echo off
powershell.exe -NoProfile -Command "$p=Join-Path ([Environment]::GetFolderPath('Startup')) 'LocalMihomoTrafficMonitor.lnk'; if(Test-Path -LiteralPath $p){Remove-Item -LiteralPath $p}"
