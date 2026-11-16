@echo off
rem Gate Face: double-click to install or update. Right-click > Run as administrator for
rem firewall, auto-start and nightly backup. Existing settings (.env) and data are kept.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
echo.
if errorlevel 1 (
  echo Installation stopped with an error. Read the message above.
) else (
  echo Finished.
)
pause
