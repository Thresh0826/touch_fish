@echo off
setlocal
cd /d "%~dp0WDMS_Cloud_Project"
powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0WDMS_Cloud_Project\moyu_tools\launch.ps1"
if errorlevel 1 pause
