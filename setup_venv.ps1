$ErrorActionPreference = "Stop"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "Creating proxy virtual environment..."
    & python3 -m venv (Join-Path $PSScriptRoot ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the virtual environment."
    }
}

Write-Host "Installing File Proxy and development dependencies..."
& $venvPython -m pip install --editable "$PSScriptRoot[dev]"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install File Proxy."
}

Write-Host "Verifying installation..."
& $venvPython -m pytest
if ($LASTEXITCODE -ne 0) {
    throw "File Proxy tests failed."
}
