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

$target = Join-Path $root "dist\ZpinControl"

# PyInstaller runs with --noconfirm, which DELETES dist\ZpinControl before it
# rebuilds. config.toml survives that below because it can be re-copied from the
# template, but config.local.toml has no template: the wizard writes it from what
# was measured on THIS machine (ports, limits, datum). Losing it means re-running
# the whole wizard against live hardware. Park it outside the tree first, and put
# it back even if the build fails.
$localConfig = Join-Path $target "config.local.toml"
$parked = Join-Path $env:TEMP "zpin-config-local-parked.toml"
$hasLocal = Test-Path $localConfig
if ($hasLocal) {
    Copy-Item $localConfig $parked -Force
    Write-Host "parked $localConfig"
}

try {
    & $Python -m PyInstaller --noconfirm --clean `
        --distpath dist --workpath build `
        (Join-Path $PSScriptRoot "ZpinControl.spec")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    if ($hasLocal -and (Test-Path $parked)) {
        Copy-Item $parked $localConfig -Force
        Remove-Item $parked -Force
        Write-Host "restored $localConfig"
    }
}

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
Write-Host "  expect: 已完成：18 个落点，共 3 排（每排 6 个） / 阀门已关闭 / 退出码 0"
