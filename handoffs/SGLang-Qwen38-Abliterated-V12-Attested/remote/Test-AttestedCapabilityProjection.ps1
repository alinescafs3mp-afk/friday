[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'AttestedBundle.Common.ps1')

function Assert-Accepted([object[]]$Observed) {
    if (-not (Test-ExactAttestedProxyCapabilitySet -Observed $Observed)) {
        throw "Exact proxy capability set was rejected: $([string]::Join(',', @($Observed)))"
    }
}

function Assert-Rejected {
    param(
        [AllowNull()]
        [object[]]$Observed
    )

    if (Test-ExactAttestedProxyCapabilitySet -Observed $Observed) {
        throw "Unsafe proxy capability set was accepted: $([string]::Join(',', @($Observed)))"
    }
}

$composeSpelling = @('CHOWN', 'DAC_OVERRIDE', 'SETGID', 'SETUID')
$dockerRuntimeSpelling = @('CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_SETGID', 'CAP_SETUID')
Assert-Accepted $composeSpelling
Assert-Accepted $dockerRuntimeSpelling
Assert-Accepted @('SETUID', 'CHOWN', 'SETGID', 'DAC_OVERRIDE')
Assert-Accepted @('CAP_SETUID', 'CAP_CHOWN', 'CAP_SETGID', 'CAP_DAC_OVERRIDE')

Assert-Rejected @('CAP_CHOWN', 'DAC_OVERRIDE', 'SETGID', 'SETUID')
Assert-Rejected @('CHOWN', 'DAC_OVERRIDE', 'SETGID', 'SETUID', 'SYS_ADMIN')
Assert-Rejected @('CHOWN', 'DAC_OVERRIDE', 'SETGID')
Assert-Rejected @('chown', 'DAC_OVERRIDE', 'SETGID', 'SETUID')
Assert-Rejected @('cap_chown', 'CAP_DAC_OVERRIDE', 'CAP_SETGID', 'CAP_SETUID')
Assert-Rejected @('CAP_CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_SETGID', 'CAP_SETUID')
Assert-Rejected @('CHOWN', 'DAC_OVERRIDE', 'SETGID', 'SETGID')
Assert-Rejected @('CHOWN', 'DAC_OVERRIDE', 'SETGID', $null)
Assert-Rejected @('CHOWN', 'DAC_OVERRIDE', 'SETGID', 7)
Assert-Rejected @()
Assert-Rejected @(('X' * 33), 'DAC_OVERRIDE', 'SETGID', 'SETUID')

'attested proxy capability projection: PASS'
