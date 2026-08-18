[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$switchPath = Join-Path $PSScriptRoot 'Switch-Qwen38V12Attested.ps1'
if (-not (Test-Path -LiteralPath $switchPath -PathType Leaf)) {
    throw 'Attested switch script is absent.'
}
& $switchPath -PreflightOnly
