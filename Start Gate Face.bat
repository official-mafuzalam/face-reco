@echo off
rem Starts Gate Face in this window and opens it in the browser. Close the window to stop it.
cd /d "%~dp0"
start "" /b powershell.exe -NoProfile -Command "for ($i = 0; $i -lt 60; $i++) { try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', 8090); $c.Close(); Start-Process 'http://127.0.0.1:8090/'; break } catch { Start-Sleep 1 } }"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1"
pause
