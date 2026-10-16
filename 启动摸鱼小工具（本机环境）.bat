@echo off
setlocal
set "PROJ=%~dp0WDMS_Cloud_Project"
cd /d "%PROJ%"
powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%PROJ%\moyu_tools\launch.ps1" -UseExistingPython
if errorlevel 1 pause
