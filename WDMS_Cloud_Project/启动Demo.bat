@echo off
setlocal
cd /d "%~dp0"
set "DEMO_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%DEMO_PY%" set "DEMO_PY=%USERPROFILE%\wdms_real_test\WDMS_Automation\.venv\Scripts\python.exe"
if not exist "%DEMO_PY%" (
  echo Python environment was not found. Run scripts\WDMS_Demo.py with your project Python.
  pause
  exit /b 1
)
"%DEMO_PY%" scripts\WDMS_Demo.py
if errorlevel 1 pause
