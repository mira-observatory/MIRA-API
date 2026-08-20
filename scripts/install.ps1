$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$venvDir = Join-Path $projectDir ".venv"
$pythonCommand = if ($env:PYTHON) { $env:PYTHON } else { "python" }

$python = Get-Command $pythonCommand -ErrorAction SilentlyContinue
if (-not $python) {
    throw "No se encontro '$pythonCommand'. Instala Python 3.11 o superior."
}

$pythonVersion = & $python.Source -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
if ([version]$pythonVersion -lt [version]"3.11") {
    throw "Se requiere Python 3.11 o superior."
}

Write-Host "Creando el entorno virtual en $venvDir..."
& $python.Source -m venv $venvDir

$venvPython = Join-Path $venvDir "Scripts\python.exe"
Write-Host "Instalando MIRA API y las dependencias de desarrollo..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e "$projectDir[dev]"

$envFile = Join-Path $projectDir ".env"
$envExample = Join-Path $projectDir ".env.example"
if (-not (Test-Path $envFile)) {
    Copy-Item $envExample $envFile
    Write-Host "Se creo .env a partir de .env.example. Completa sus credenciales antes de ejecutar."
} else {
    Write-Host "Se conservo el archivo .env existente."
}

Write-Host "Instalacion terminada. Ejecuta: .\scripts\run.ps1"