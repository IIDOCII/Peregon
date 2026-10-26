# Сборка портативной версии Peregon: dist\Peregon\ и dist\Peregon-<версия>-win64.zip.
# Запуск из любой папки: powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-NoZip]
param([switch]$NoZip)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# uv находит Python только с этой переменной (см. BUILD.md, «Окружение»).
if (-not $env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR = "$env:USERPROFILE\.local\share\uv\python" }
$env:PYTHONIOENCODING = "utf-8"

$arguments = @("run", "--project", $root, "python", (Join-Path $PSScriptRoot "build.py"))
if ($NoZip) { $arguments += "--no-zip" }
& uv @arguments
exit $LASTEXITCODE
