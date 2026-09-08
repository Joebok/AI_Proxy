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
if not defined AI_PROXY_HTTP_HOST set "AI_PROXY_HTTP_HOST=127.0.0.1"
if not defined AI_PROXY_HTTP_PORT set "AI_PROXY_HTTP_PORT=11433"
if not defined AI_PROXY_OLLAMA_UPSTREAM set "AI_PROXY_OLLAMA_UPSTREAM=http://127.0.0.1:11434"
if not defined AI_PROXY_HTTP_CONTINUATION_GRACE_SECONDS set "AI_PROXY_HTTP_CONTINUATION_GRACE_SECONDS=2"
if not defined AI_PROXY_HTTP_MAX_BODY_BYTES set "AI_PROXY_HTTP_MAX_BODY_BYTES=104857600"
if not defined AI_PROXY_HTTP_UPSTREAM_TIMEOUT_SECONDS set "AI_PROXY_HTTP_UPSTREAM_TIMEOUT_SECONDS=7500"
set "HTTP_ARGS="
set "RUNTIME_ARGS="
if defined AI_PROXY_RUNTIME_CONFIG set RUNTIME_ARGS=--runtime-config "%AI_PROXY_RUNTIME_CONFIG%"
if defined AI_PROXY_HTTP_PORT (
  set "HTTP_ARGS=--http-listen-host %AI_PROXY_HTTP_HOST% --http-listen-port %AI_PROXY_HTTP_PORT% --ollama-upstream %AI_PROXY_OLLAMA_UPSTREAM% --http-continuation-grace-seconds %AI_PROXY_HTTP_CONTINUATION_GRACE_SECONDS% --http-max-body-bytes %AI_PROXY_HTTP_MAX_BODY_BYTES% --http-upstream-timeout-seconds %AI_PROXY_HTTP_UPSTREAM_TIMEOUT_SECONDS%"
)
if defined AI_PROXY_COMFYUI_PORT (
  if not defined AI_PROXY_COMFYUI_UPSTREAM set "AI_PROXY_COMFYUI_UPSTREAM=http://127.0.0.1:8188"
  set "HTTP_ARGS=%HTTP_ARGS% --comfyui-listen-port %AI_PROXY_COMFYUI_PORT% --comfyui-upstream %AI_PROXY_COMFYUI_UPSTREAM%"
)
echo Starting File Proxy...
"%VENV_PYTHON%" -m file_proxy.cli --root "%AI_QUEUE_ROOT%" --registry-dir "%~dp0registries" %RUNTIME_ARGS% tui --sync-grace-seconds "%FILE_PROXY_SYNC_GRACE_SECONDS%" %HTTP_ARGS%
