@echo off
cd /d "%~dp0"
set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
  echo Proxy virtual environment not found. Run setup_venv.bat first.
  exit /b 2
)
if not defined ZET_PROJECT_ROOT (
  for %%I in ("%~dp0..\Zet") do set "ZET_PROJECT_ROOT=%%~fI"
)
if not exist "%ZET_PROJECT_ROOT%\config.toml" (
  echo ZET_PROJECT_ROOT does not contain config.toml: %ZET_PROJECT_ROOT%
  exit /b 2
)
if not defined AI_QUEUE_ROOT set "AI_QUEUE_ROOT=%USERPROFILE%\Dropbox\AI_Queue"
if not defined FILE_PROXY_SYNC_GRACE_SECONDS set "FILE_PROXY_SYNC_GRACE_SECONDS=300"
echo Starting File Proxy...
"%VENV_PYTHON%" -m file_proxy.cli --root "%AI_QUEUE_ROOT%" --registry-dir "%~dp0registries" run --sync-grace-seconds "%FILE_PROXY_SYNC_GRACE_SECONDS%"
