@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [PYTHON_ENV_MISSING] Please create .venv and install requirements-windows.txt.
  exit /b 10
)
".venv\Scripts\python.exe" "main.py" --mode real
set "TASK_EXIT_CODE=%ERRORLEVEL%"
exit /b %TASK_EXIT_CODE%
