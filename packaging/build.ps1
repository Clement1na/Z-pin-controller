# Build separate hardware-only user and full debug onedir deliveries.
# ASCII-only for Windows PowerShell 5.1. User docs live in README.md/DEBUG.md.
param(
    [string]$Python = "D:\miniconda3\envs\zpin\python.exe",
    [string]$PreparedStage = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path -LiteralPath $Python)) { throw "Python not found: $Python" }
$buildId = [guid]::NewGuid().ToString("N")
$stageParent = if ($PreparedStage) { [IO.Path]::GetFullPath($PreparedStage) } else {
    Join-Path $root ("build\package-stage-" + $buildId)
}
function Assert-WorkspacePath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Delivery path outside workspace: $full"
    }
    if (Test-Path -LiteralPath $full) {
        if ((Get-Item -LiteralPath $full).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing linked delivery path: $full"
        }
    }
}
$userTarget = Join-Path $root 'dist\ZpinControl'
$debugTarget = Join-Path $root 'dist\ZpinControlDebug'
$deliveries = @(
    @{ Target=$userTarget; Stage=(Join-Path $stageParent 'ZpinControl');
       Previous=(Join-Path $root ("build\previous-delivery-"+$buildId));
       Failed=(Join-Path $root ("build\failed-user-delivery-"+$buildId));
       Exes=@('ZpinControlUI.exe'); Docs='README.md'; Installed=$false; HadPrevious=$false },
    @{ Target=$debugTarget; Stage=(Join-Path $stageParent 'ZpinControlDebug');
       Previous=(Join-Path $root ("build\previous-debug-delivery-"+$buildId));
       Failed=(Join-Path $root ("build\failed-debug-delivery-"+$buildId));
       Exes=@('ZpinControl.exe','ZpinControlDebug.exe'); Docs='DEBUG.md'; Installed=$false; HadPrevious=$false }
)
function Assert-DeliveryClosed {
    $paths = @((Join-Path $userTarget 'ZpinControl.exe'), (Join-Path $userTarget 'ZpinControlUI.exe'),
               (Join-Path $debugTarget 'ZpinControl.exe'), (Join-Path $debugTarget 'ZpinControlDebug.exe'))
    foreach ($process in @(Get-Process -Name ZpinControl,ZpinControlUI,ZpinControlDebug -ErrorAction SilentlyContinue)) {
        if ($paths -contains $process.Path) {
            throw "Close the Z-pin application safely before promotion (PID $($process.Id)); existing delivery was not changed."
        }
    }
}
Assert-WorkspacePath $stageParent
if (-not $stageParent.StartsWith(([IO.Path]::GetFullPath((Join-Path $root 'build'))+'\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Staging must be inside project build directory: $stageParent"
}
foreach ($delivery in $deliveries) {
    foreach ($path in @($delivery.Target,$delivery.Stage,$delivery.Previous,$delivery.Failed)) { Assert-WorkspacePath $path }
}
if (-not $PreparedStage) {
    # Never clean the live dist folder.
    & $Python -m PyInstaller --noconfirm --clean --distpath $stageParent --workpath build `
        (Join-Path $PSScriptRoot 'ZpinControl.spec')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE); existing deliveries unchanged. Staging: $stageParent" }
}
foreach ($delivery in $deliveries) {
    foreach ($name in ($delivery.Exes + @('_internal\base_library.zip','_internal\_tkinter.pyd','_internal\tcl86t.dll','_internal\tk86t.dll'))) {
        if (-not (Test-Path -LiteralPath (Join-Path $delivery.Stage $name) -PathType Leaf)) {
            throw "Incomplete staged delivery: $($delivery.Stage)\$name; existing deliveries unchanged."
        }
    }
    if (-not @(Get-ChildItem -LiteralPath (Join-Path $delivery.Stage '_internal') -Filter 'python3*.dll' -File).Count) {
        throw "Staged Python runtime missing: $($delivery.Stage)"
    }
}
Assert-DeliveryClosed
# Initial debug delivery inherits the existing machine. Later builds preserve its own state.
$debugSource = if (Test-Path -LiteralPath $debugTarget) { $debugTarget } else { $userTarget }
$readmeName = (-join ([char[]]@(0x4f7f,0x7528,0x8bf4,0x660e))) + '.md'
foreach ($delivery in $deliveries) {
    $sourceRoot = if ($delivery.Target -eq $userTarget) { $userTarget } else { $debugSource }
    foreach ($name in @('config.toml','config.local.toml','profiles','runs')) {
        $source = Join-Path $sourceRoot $name
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination $delivery.Stage -Recurse -Force
            Write-Host "preserved $($delivery.Target): $name"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $delivery.Stage 'config.toml'))) {
        Copy-Item -LiteralPath (Join-Path $root 'master\config.toml') -Destination $delivery.Stage
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $delivery.Docs) -Destination (Join-Path $delivery.Stage $readmeName)
    $firmware = Join-Path $delivery.Stage 'firmware\insert_head'
    New-Item -ItemType Directory -Path $firmware -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $root 'firmware\insert_head\insert_head.ino') -Destination $firmware
}
Assert-DeliveryClosed
New-Item -ItemType Directory -Path (Join-Path $root 'dist') -Force | Out-Null
try {
    foreach ($delivery in $deliveries) {
        $delivery.HadPrevious = Test-Path -LiteralPath $delivery.Target
        if ($delivery.HadPrevious) { Move-Item -LiteralPath $delivery.Target -Destination $delivery.Previous }
        Move-Item -LiteralPath $delivery.Stage -Destination $delivery.Target
        $delivery.Installed = $true
    }
} catch {
    $restoreOrder = @($deliveries)
    [array]::Reverse($restoreOrder)
    foreach ($delivery in $restoreOrder) {
        if ($delivery.Installed -and (Test-Path -LiteralPath $delivery.Target)) {
            Move-Item -LiteralPath $delivery.Target -Destination $delivery.Failed
        }
        if ($delivery.HadPrevious -and (Test-Path -LiteralPath $delivery.Previous) -and -not (Test-Path -LiteralPath $delivery.Target)) {
            Move-Item -LiteralPath $delivery.Previous -Destination $delivery.Target
        }
    }
    throw
}
foreach ($delivery in $deliveries) {
    Write-Host "Built: $($delivery.Target)"
    if ($delivery.HadPrevious) { Write-Host "previous delivery retained: $($delivery.Previous)" }
}
# Both complete folders moved out; this parent is empty.
Remove-Item -LiteralPath $stageParent
Write-Host 'User: dist\ZpinControl\ZpinControlUI.exe (three hardware modes)'
Write-Host 'Debug: dist\ZpinControlDebug\ZpinControlDebug.exe (original full GUI)'
Write-Host 'CLI check: dist\ZpinControlDebug\ZpinControl.exe --smoke-test'
