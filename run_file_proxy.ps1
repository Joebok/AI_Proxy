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
if (-not $env:AI_PROXY_HTTP_PORT) {
    $env:AI_PROXY_HTTP_PORT = "11433"
}

Write-Host "Starting File Proxy..."
$proxyArgs = @(
    "-m", "file_proxy.cli",
    "--root", $env:AI_QUEUE_ROOT,
    "--registry-dir", (Join-Path $PSScriptRoot "registries"),
    "run",
    "--sync-grace-seconds", $env:FILE_PROXY_SYNC_GRACE_SECONDS
)
if ($env:AI_PROXY_HTTP_PORT) {
    $proxyArgs += @(
        "--http-listen-host", $(if ($env:AI_PROXY_HTTP_HOST) { $env:AI_PROXY_HTTP_HOST } else { "127.0.0.1" }),
        "--http-listen-port", $env:AI_PROXY_HTTP_PORT,
        "--ollama-upstream", $(if ($env:AI_PROXY_OLLAMA_UPSTREAM) { $env:AI_PROXY_OLLAMA_UPSTREAM } else { "http://127.0.0.1:11434" }),
        "--http-continuation-grace-seconds", $(if ($env:AI_PROXY_HTTP_CONTINUATION_GRACE_SECONDS) { $env:AI_PROXY_HTTP_CONTINUATION_GRACE_SECONDS } else { "2" }),
        "--http-max-body-bytes", $(if ($env:AI_PROXY_HTTP_MAX_BODY_BYTES) { $env:AI_PROXY_HTTP_MAX_BODY_BYTES } else { "104857600" }),
        "--http-upstream-timeout-seconds", $(if ($env:AI_PROXY_HTTP_UPSTREAM_TIMEOUT_SECONDS) { $env:AI_PROXY_HTTP_UPSTREAM_TIMEOUT_SECONDS } else { "7500" })
    )
}
& $venvPython @proxyArgs
exit $LASTEXITCODE
