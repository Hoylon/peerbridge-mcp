@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_remote_control.ps1" %*
exit /b %errorlevel%
