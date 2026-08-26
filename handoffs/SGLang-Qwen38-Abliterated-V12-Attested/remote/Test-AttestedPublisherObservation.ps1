[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'AttestedBundle.Common.ps1')

$candidate = $script:Attested.CandidateProxyName

if (-not (Test-SolePublisherObservation @($candidate) $candidate)) {
    throw 'Exact candidate publisher was not accepted'
}
if (Test-SolePublisherObservation @() $candidate) {
    throw 'Empty publisher observation was not treated as pending'
}

function Assert-Rejected([object[]]$Observed) {
    $rejected = $false
    try {
        $null = Test-SolePublisherObservation $Observed $candidate
    }
    catch {
        $rejected = $true
    }
    if (-not $rejected) {
        throw 'Unsafe publisher observation was accepted'
    }
}

Assert-Rejected @($script:Attested.StableProxyName)
Assert-Rejected @($candidate, $script:Attested.StableProxyName)
Assert-Rejected @($candidate, $candidate)
Assert-Rejected @('jarvis-gpt-sglang-qwen38-abliterated-v12-attested-api-near')
Assert-Rejected @(7)

$unownedExpectedRejected = $false
try {
    $null = Test-SolePublisherObservation @('unowned') 'unowned'
}
catch {
    $unownedExpectedRejected = $true
}
if (-not $unownedExpectedRejected) {
    throw 'Unowned expected publisher was accepted'
}

'attested publisher observation: PASS'
