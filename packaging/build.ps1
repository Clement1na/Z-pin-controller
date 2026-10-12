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
$buildId = [guid]::NewGuid().ToString("N")
$stageParent = Join-Path $root ("build\package-stage-" + $buildId)
$staged = Join-Path $stageParent "ZpinControl"
$previous = Join-Path $root ("build\previous-delivery-" + $buildId)

function Assert-WorkspacePath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Delivery path outside workspace: $full"
    }
    if (Test-Path -LiteralPath $full) {
        $item = Get-Item -LiteralPath $full
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing to replace a linked delivery directory: $full"
        }
    }
}

function Assert-DeliveryClosed {
    $executables = @((Join-Path $target 'ZpinControl.exe'), (Join-Path $target 'ZpinControlUI.exe'))
    foreach ($process in @(Get-Process -Name ZpinControl,ZpinControlUI -ErrorAction SilentlyContinue)) {
        if ($executables -contains $process.Path) {
            throw "Close the Z-pin application before packaging (PID $($process.Id)); existing delivery was not changed."
        }
    }
}

# Never let PyInstaller clean the active delivery. A failed COLLECT can delete
# base_library.zip before encountering a locked DLL, leaving both EXEs broken.
Assert-DeliveryClosed
Assert-WorkspacePath $stageParent
& $Python -m PyInstaller --noconfirm --clean `
    --distpath $stageParent --workpath build `
    (Join-Path $PSScriptRoot "ZpinControl.spec")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE; existing delivery was not changed. Staging: $stageParent"
}

foreach ($name in @('ZpinControl.exe', 'ZpinControlUI.exe', '_internal\base_library.zip',
                     '_internal\_tkinter.pyd', '_internal\tcl86t.dll', '_internal\tk86t.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $staged $name) -PathType Leaf)) {
        throw "Incomplete staged delivery: $name; existing delivery was not changed."
    }
}
if (-not @(Get-ChildItem -LiteralPath (Join-Path $staged '_internal') -Filter 'python3*.dll' -File).Count) {
    throw "Staged Python runtime missing; existing delivery was not changed."
}

# Copy the latest user state only after building, then promote complete folders.
# Keep the previous folder for recovery rather than recursively deleting it.
Assert-DeliveryClosed
foreach ($name in @('config.toml', 'config.local.toml', 'profiles', 'runs')) {
    $source = Join-Path $target $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $staged -Recurse -Force
        Write-Host "preserved $name"
    }
}
$config = Join-Path $staged 'config.toml'
if (-not (Test-Path -LiteralPath $config)) {
    Copy-Item -LiteralPath (Join-Path $root 'master\config.toml') -Destination $config
}
$readmeName = (-join ([char[]]@(0x4f7f, 0x7528, 0x8bf4, 0x660e))) + '.md'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $staged $readmeName)
# Ship the matching sketch for later flashing; never flash from the builder.
$firmwareDir = Join-Path $staged 'firmware\insert_head'
New-Item -ItemType Directory -Path $firmwareDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'firmware\insert_head\insert_head.ino') -Destination $firmwareDir

Assert-DeliveryClosed
foreach ($path in @($target, $staged, $previous)) { Assert-WorkspacePath $path }
New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
$hadPrevious = Test-Path -LiteralPath $target
if ($hadPrevious) {
    Move-Item -LiteralPath $target -Destination $previous
}
try {
    Move-Item -LiteralPath $staged -Destination $target
} catch {
    if ($hadPrevious -and -not (Test-Path -LiteralPath $target)) {
        Move-Item -LiteralPath $previous -Destination $target
    }
    throw
}
if ($hadPrevious) { Write-Host "previous delivery retained: $previous" }
# COLLECT's output has moved out; this parent is now empty (no recursive delete).
Remove-Item -LiteralPath $stageParent

Write-Host ""
Write-Host "Built: $target"
Write-Host ""
Write-Host "Acceptance test (console):"
Write-Host "  cd `"$target`""
Write-Host "  .\ZpinControl.exe --smoke-test"
Write-Host "  expect: 12 placements in 3 rows, valves closed, exit code 0"
Write-Host ""
Write-Host "Acceptance test (windowed):"
Write-Host "  double-click ZpinControlUI.exe"
Write-Host "  expect: window opens with NO console behind it; click the smoke-test"
Write-Host "          button and the same 12 placements appear in the log pane"
