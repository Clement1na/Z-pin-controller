# Build the onedir deliverable into dist\ZpinControl\.
#
# ASCII-only on purpose: Windows PowerShell 5.1 decodes .ps1 as ANSI unless the
# file carries a UTF-8 BOM, so any non-ASCII text written here would turn into
# mojibake on the machines this is meant to run on. The operator-facing Chinese
# documentation lives in packaging\README.md instead.
#
# Runs under the default execution policy; do not reach for -ExecutionPolicy
# Bypass unless this machine's policy actually forbids running scripts.
#
# Usage:
#   powershell -File packaging\build.ps1
#   powershell -File packaging\build.ps1 -Python C:\py\python.exe

param(
    [string]$Python = "D:\miniconda3\envs\zpin\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Python)) {
    throw "Python not found: $Python  (pass -Python <path-to-python.exe>)"
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& $Python -m PyInstaller --noconfirm --clean `
    --distpath dist --workpath build `
    (Join-Path $PSScriptRoot "ZpinControl.spec")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$target = Join-Path $root "dist\ZpinControl"

# config.toml belongs NEXT TO the exe, not inside _internal\: the frozen build
# resolves it from sys.executable, and the operator has to edit it. Never
# overwrite one that is already there - it may hold a measured port and limits.
$config = Join-Path $target "config.toml"
if (Test-Path $config) {
    Write-Host "kept existing $config"
} else {
    Copy-Item (Join-Path $root "master\config.toml") $config
    Write-Host "wrote $config from the template"
}

Copy-Item (Join-Path $PSScriptRoot "README.md") (Join-Path $target "使用说明.md") -Force

Write-Host ""
Write-Host "Built: $target"
Write-Host ""
Write-Host "Acceptance test:"
Write-Host "  cd `"$target`""
Write-Host "  .\ZpinControl.exe --smoke-test"
Write-Host "  expect: COMPLETE: 1620/1620 placements / valves closed / exit code 0"
