@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-workbench.ps1"
set "workbench_exit=%errorlevel%"
pause
exit /b %workbench_exit%
