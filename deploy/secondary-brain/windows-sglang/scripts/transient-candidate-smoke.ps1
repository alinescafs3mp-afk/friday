[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$OldBundleRoot = 'C:\ProgramData\FridaySecondary\bundle',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$CandidateBundleRoot = 'C:\ProgramData\FridaySecondary\bundle-ablit-79f64a52',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$ProjectName = 'friday-secondary-brain',

    [Parameter()]
    [ValidateRange(60, 1800)]
    [int]$HealthTimeoutSec = 900
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$endpoint = 'https://192.168.1.35:8443/v1'
$candidateVolume = 'friday-secondary-source-gptoss20b-ablit-79f64a52'
$rollbackVolume = 'friday-secondary-source-gptoss20b'
$candidateEvidence = Join-Path $CandidateBundleRoot 'evidence\smoke.ablit.candidate.json'

if (Test-Path -LiteralPath $candidateEvidence) {
    throw 'Candidate smoke evidence path already exists.'
}

function Invoke-ComposeUp([string]$Root, [string]$EnvName) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & docker compose -p $ProjectName `
            --env-file (Join-Path $Root $EnvName) `
            -f (Join-Path $Root 'compose.yml') `
            up -d --force-recreate 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        throw ('Compose activation failed with exit code ' + $exitCode + '.')
    }
}

function Wait-ComposeHealthy {
    $deadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSec)
    while ([DateTime]::UtcNow -lt $deadline) {
        $raw = & docker inspect friday-secondary-gptoss20b friday-secondary-gateway 2>$null
        if ($LASTEXITCODE -eq 0) {
            $rows = $raw | ConvertFrom-Json
            if (
                $rows.Count -eq 2 -and
                ($rows | Where-Object { $_.State.Health.Status -ne 'healthy' }).Count -eq 0
            ) {
                return
            }
        }
        Start-Sleep -Seconds 5
    }
    throw 'Compose health deadline expired.'
}

function Assert-SourceVolume([string]$ExpectedVolume) {
    $engine = (docker inspect friday-secondary-gptoss20b | ConvertFrom-Json)[0]
    if ($LASTEXITCODE -ne 0) {
        throw 'SGLang container inspection failed.'
    }
    $sourceMount = $engine.Mounts | Where-Object { $_.Destination -eq '/source' }
    if ($null -eq $sourceMount -or $sourceMount.Name -cne $ExpectedVolume) {
        throw 'SGLang source volume identity is wrong.'
    }
}

$candidateFailure = $null
$candidateReport = $null
try {
    Invoke-ComposeUp $CandidateBundleRoot '.env.stage'
    Wait-ComposeHealthy
    Assert-SourceVolume $candidateVolume
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $smokeOutput = & python (Join-Path $CandidateBundleRoot 'scripts\candidate_smoke.py') `
            --base-url $endpoint `
            --api-key-file (Join-Path $CandidateBundleRoot 'secrets\gateway-api-key') `
            --ca-file (Join-Path $CandidateBundleRoot 'secrets\tls\ca.crt') `
            --profile-manifest (Join-Path $CandidateBundleRoot 'evidence\profile.ablit.full.candidate.json') `
            --timeout-sec 180 `
            --output $candidateEvidence 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        try {
            $failure = $smokeOutput | Select-Object -Last 1 | ConvertFrom-Json
            $detail = [string]$failure.error
        } catch {
            $detail = 'content-free diagnostic unavailable'
        }
        if ($detail -notmatch '^[A-Za-z0-9 /_.:-]{1,240}$') {
            $detail = 'content-free diagnostic unavailable'
        }
        throw ('Candidate smoke failed: ' + $detail)
    }
    $candidateReport = $smokeOutput | Select-Object -Last 1 | ConvertFrom-Json
} catch {
    $candidateFailure = $_.Exception.Message
}

$rollbackFailure = $null
$rollbackReport = $null
try {
    Invoke-ComposeUp $OldBundleRoot '.env'
    Wait-ComposeHealthy
    Assert-SourceVolume $rollbackVolume
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $probeOutput = & python (Join-Path $OldBundleRoot 'scripts\probe_endpoint.py') `
            --base-url $endpoint `
            --api-key-file (Join-Path $OldBundleRoot 'secrets\gateway-api-key') `
            --ca-file (Join-Path $OldBundleRoot 'secrets\tls\ca.crt') `
            --profile-manifest (Join-Path $OldBundleRoot 'evidence\profile.accepted.json') `
            --timeout-sec 180 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        $detail = ($probeOutput | Select-Object -Last 1 | Out-String).Trim()
        if ($detail -notmatch '^probe failed: [A-Za-z0-9 /_.:-]{1,240}$') {
            $detail = 'content-free diagnostic unavailable'
        }
        throw ('Rollback probe failed: ' + $detail)
    }
    $rollbackReport = $probeOutput | Select-Object -Last 1 | ConvertFrom-Json
} catch {
    $rollbackFailure = $_.Exception.Message
}

if ($null -ne $rollbackFailure) {
    throw ('Rollback verification failed: ' + $rollbackFailure)
}
if ($null -ne $candidateFailure) {
    throw ('Candidate verification failed after successful rollback: ' + $candidateFailure)
}

[ordered]@{
    schema = 'friday.secondary-transient-candidate-smoke.v1'
    status = 'passed'
    candidate = $candidateReport
    rollback = [ordered]@{
        status = $rollbackReport.status
        candidate_profile_id = $rollbackReport.candidate_profile_id
        candidate_profile_sha256 = $rollbackReport.candidate_profile_sha256
        served_model_alias = $rollbackReport.served_model_alias
    }
} | ConvertTo-Json -Compress -Depth 8
