[CmdletBinding()]
param(
    [string]$InputPath,
    [switch]$NoOpen,
    [string]$Distribution = 'Ubuntu-22.04'
)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (-not $InputPath) { $InputPath = Join-Path $projectRoot 'data/inputs' }
if (-not (Test-Path -LiteralPath $InputPath)) {
    Write-Host "Input not found: $InputPath"
    Write-Host 'Put MCAP files in HFlow\data\inputs, or drag one file/folder onto the CMD launcher.'
    exit 3
}
$InputPath = (Resolve-Path -LiteralPath $InputPath).Path
$reportRoot = Join-Path $projectRoot 'data/quality-reports'
New-Item -ItemType Directory -Force -Path $reportRoot | Out-Null
function Convert-ToWslPath([string]$Path) {
    $converted = & wsl.exe -d $Distribution -- wslpath -a ($Path.Replace('\', '/'))
    if ($LASTEXITCODE -ne 0) { throw "Cannot convert path: $Path" }
    return ($converted | Out-String).Trim()
}
$linuxProject = Convert-ToWslPath $projectRoot
$linuxInput = Convert-ToWslPath $InputPath
$linuxReports = Convert-ToWslPath $reportRoot
$linuxUserHome = & wsl.exe -d $Distribution -- printenv HOME
if ($LASTEXITCODE -ne 0) { throw 'Cannot find the WSL home directory.' }
$linuxUserHome = ($linuxUserHome | Out-String).Trim()
$pointer = Join-Path $reportRoot 'latest-report.txt'
$previousPointer = if (Test-Path -LiteralPath $pointer) { Get-Content -LiteralPath $pointer -Raw -Encoding UTF8 } else { '' }
& wsl.exe -d $Distribution -- bash "$linuxProject/scripts/wsl/run.sh" run $linuxProject python -m hflow.realman_qc --input $linuxInput --config "$linuxProject/configs/realman-qc.json" --runtime-root "$linuxUserHome/workspaces/ai_Quality_Inspection_Division/HFlow/data/quality" --output $linuxReports
$qcExitCode = $LASTEXITCODE
if (Test-Path -LiteralPath $pointer) {
    $currentPointer = Get-Content -LiteralPath $pointer -Raw -Encoding UTF8
    if ($currentPointer -ne $previousPointer) {
        $windowsReport = & wsl.exe -d $Distribution -- wslpath -w $currentPointer.Trim()
        if ($LASTEXITCODE -eq 0) {
            $windowsReport = ($windowsReport | Out-String).Trim()
            if (-not (Test-Path -LiteralPath $windowsReport)) { throw "Report path is not readable: $windowsReport" }
            Write-Host "Report saved: $windowsReport"
            if (-not $NoOpen) { Invoke-Item -LiteralPath $windowsReport }
        }
    }
}
switch ($qcExitCode) {
    0 { Write-Host 'Completed: technical checks passed.' }
    2 { Write-Host 'Completed: findings need review. Read the report; this is not a startup failure.' }
    default { Write-Host 'Inspection incomplete: review the error and any available report.' }
}
exit $qcExitCode
