[CmdletBinding()]
param(
    [ValidateSet('setup', 'check', 'sync', 'run')]
    [string]$Action = 'check',
    [string]$Distribution = 'Ubuntu-22.04',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs
)
$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$linuxSource = & wsl.exe -d $Distribution -- wslpath -a ($sourceRoot.Replace('\', '/'))
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the project path in WSL.' }
$linuxSource = ($linuxSource | Out-String).Trim()
& wsl.exe -d $Distribution -- bash "$linuxSource/scripts/wsl/run.sh" $Action $linuxSource @CommandArgs
exit $LASTEXITCODE
