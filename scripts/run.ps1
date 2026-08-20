$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$python = Join-Path $projectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "El entorno virtual no existe. Ejecuta .\scripts\install.ps1 primero."
}

$envFile = Join-Path $projectDir ".env"
if (-not (Test-Path $envFile)) {
    throw "Falta .env. Ejecuta .\scripts\install.ps1 y completa sus valores."
}

$hostValue = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
$portValue = if ($env:PORT) { $env:PORT } else { "8000" }

Push-Location $projectDir
try {
    & $python -m uvicorn mira_api.main:app --host $hostValue --port $portValue --reload @args
} finally {
    Pop-Location
}