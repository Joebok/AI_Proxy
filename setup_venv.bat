@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating proxy virtual environment...
  python3 -m venv .venv
  if errorlevel 1 exit /b 1
)

echo Installing File Proxy and development dependencies...
".venv\Scripts\python.exe" -m pip install --editable ".[dev]"
if errorlevel 1 exit /b 1

echo Verifying installation...
".venv\Scripts\python.exe" -m pytest
