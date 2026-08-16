@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_control_room.ps1"
exit /b %errorlevel%
