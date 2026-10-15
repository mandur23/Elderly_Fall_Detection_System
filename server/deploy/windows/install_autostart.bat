@echo off
rem Registers the fall detection server to start at Windows sign-in. Options: -WithUpdate -Firewall -Remove
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
set "RC=%errorlevel%"
pause
exit /b %RC%
