$ErrorActionPreference = "Stop"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Proxy virtual environment not found. Run .\setup_venv.ps1 first."
}

if (-not $env:ZET_PROJECT_ROOT) {
    $env:ZET_PROJECT_ROOT = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\Zet"))
}
if (-not (Test-Path -LiteralPath (Join-Path $env:ZET_PROJECT_ROOT "config.toml") -PathType Leaf)) {
    throw "ZET_PROJECT_ROOT does not contain config.toml: $env:ZET_PROJECT_ROOT"
}
if (-not $env:AI_QUEUE_ROOT) {
    $env:AI_QUEUE_ROOT = Join-Path $HOME "Dropbox\AI_Queue"
}
if (-not $env:FILE_PROXY_SYNC_GRACE_SECONDS) {
    $env:FILE_PROXY_SYNC_GRACE_SECONDS = "300"
}

Write-Host "Starting File Proxy..."
& $venvPython -m file_proxy.cli --root $env:AI_QUEUE_ROOT --registry-dir (Join-Path $PSScriptRoot "registries") run --sync-grace-seconds $env:FILE_PROXY_SYNC_GRACE_SECONDS
exit $LASTEXITCODE
