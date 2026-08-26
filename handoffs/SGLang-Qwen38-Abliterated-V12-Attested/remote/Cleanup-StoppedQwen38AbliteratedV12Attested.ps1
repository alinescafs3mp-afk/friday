[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'AttestedBundle.Common.ps1')

if ($Execute -and $PreflightOnly) {
    throw 'Choose either -Execute or -PreflightOnly.'
}

$journalPath = Join-Path $PSScriptRoot (
    'cleanup-stopped-attested-{0}.jsonl' -f [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
)
$lock = $null
$apiKey = $null
$stage = 'initial'
$publishNetworkReceipt = $null

function Write-Journal([string]$State, [hashtable]$Data = @{}) {
    $record = [ordered]@{ at_utc = [DateTime]::UtcNow.ToString('o'); state = $State }
    foreach ($entry in $Data.GetEnumerator()) { $record[$entry.Key] = $entry.Value }
    ($record | ConvertTo-Json -Compress -Depth 12) | Add-Content -LiteralPath $journalPath -Encoding utf8
}

function Assert-StableCleanupEndpoint([hashtable]$Headers) {
    $models = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/v1/models' `
        -Headers $Headers -TimeoutSec 10
    $payload = [string]$models.Content | ConvertFrom-Json
    if (@($payload.data).Count -ne 1 -or [string]$payload.data[0].id -cne 'dispatcher') {
        throw 'Stable cleanup model inventory is not exact'
    }
    $serverInfo = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/server_info' `
        -Headers $Headers -TimeoutSec 10
    $stableInfo = [string]$serverInfo.Content | ConvertFrom-Json
    Assert-StableServerInfo $stableInfo
    $metrics = Get-EndpointMetrics $Headers
    if ($metrics.Running -ne 0 -or $metrics.Queued -ne 0) {
        throw 'Stable cleanup endpoint is not idle'
    }
    $stableWitness = Invoke-WebRequest -UseBasicParsing `
        -Uri 'http://127.0.0.1:8001/_friday/v1/deployment-witness' `
        -Headers $Headers -TimeoutSec 10
    Assert-StableDeploymentWitness ([string]$stableWitness.Content | ConvertFrom-Json) $stableInfo
}

try {
    $stage = 'lock'
    $lockDirectory = Split-Path -Parent $script:Attested.LockPath
    if (-not (Test-Path -LiteralPath $lockDirectory -PathType Container)) {
        throw 'Shared switch-lock directory is absent'
    }
    $lock = [IO.File]::Open(
        $script:Attested.LockPath,
        [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
    Write-Journal 'cleanup_preflight_started'

    $stage = 'state_identity'
    $receipt = Assert-BuildReceipt
    $state = Get-ExactJson $script:Attested.StatePath 'candidate cleanup state'
    $stateSchema = Assert-CandidateCleanupState $state $receipt

    $stage = 'stable_identity'
    $stableEngine = Get-Container $script:Attested.StableEngineName
    $stableProxy = Get-Container $script:Attested.StableProxyName
    if ($null -eq $stableEngine -or $null -eq $stableProxy -or
        [string]$stableEngine.Id -cne [string]$state.stable_engine_id -or
        [string]$stableProxy.Id -cne [string]$state.stable_proxy_id -or
        -not [bool]$stableEngine.State.Running -or -not [bool]$stableProxy.State.Running -or
        [string]$stableEngine.State.Health.Status -cne 'healthy' -or
        [string]$stableProxy.State.Health.Status -cne 'healthy') {
        throw 'Cleanup requires the exact healthy restored stable pair'
    }
    $apiKey = Get-EnvValue $stableProxy 'JARVIS_LLM_API_KEY'
    if ($apiKey -cnotmatch '^[A-Za-z0-9._~-]{32,256}$' -or
        (Get-KeyHash $apiKey) -cne [string]$state.key_sha256) {
        throw 'Cleanup stable API key no longer matches sealed state'
    }
    $keyHash = Get-KeyHash $apiKey
    $headers = @{ Authorization = "Bearer $apiKey" }
    Assert-StableGraph $stableEngine $stableProxy $keyHash
    Assert-SolePublisher $script:Attested.StableProxyName
    Assert-StableCleanupEndpoint $headers
    Assert-Sidecars

    $stage = 'candidate_identity'
    $candidateEngine = Get-Container $script:Attested.CandidateEngineName
    $candidateProxy = Get-Container $script:Attested.CandidateProxyName
    if ($null -ne $candidateEngine -and
        [string]$candidateEngine.Id -cne [string]$state.candidate_engine_id) {
        throw 'Stopped cleanup engine does not match sealed candidate ID'
    }
    if ($null -ne $candidateProxy -and
        [string]$candidateProxy.Id -cne [string]$state.candidate_proxy_id) {
        throw 'Stopped cleanup proxy does not match sealed candidate ID'
    }
    if ($null -ne $candidateProxy -and $null -eq $candidateEngine) {
        throw 'Cleanup proxy exists without its sealed engine sibling'
    }
    foreach ($candidate in @($candidateProxy, $candidateEngine)) {
        if ($null -ne $candidate -and
            ([bool]$candidate.State.Running -or (Get-RestartSpec $candidate) -cne 'no')) {
            throw 'Cleanup accepts only disarmed stopped candidate containers'
        }
    }
    if ($null -ne $candidateEngine) {
        if ($stateSchema -ceq 'friday.attested-switch-state.v1') {
            Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash -LegacyInternalOnly
        }
        else {
            $publishNetwork = Get-DockerNetwork $script:Attested.PublishNetworkName
            if ($null -eq $publishNetwork) { throw 'Sealed v2 publish network is absent' }
            Assert-PublishNetworkReceipt $state.publish_network $publishNetwork
            $publishNetworkReceipt = $state.publish_network
            Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash `
                $publishNetworkReceipt
        }
    }
    else {
        Assert-AttestedModelVolume (Get-AttestedModelVolume)
    }

    $attestedNetwork = Get-DockerNetwork $script:Attested.AttestedNetworkName
    if ($null -eq $attestedNetwork) { throw 'Attested internal network is absent during cleanup' }
    Assert-AttestedInternalNetworkIdentity $attestedNetwork
    $allowedInternal = @()
    if ($null -ne $candidateEngine) { $allowedInternal += $candidateEngine }
    if ($null -ne $candidateProxy) { $allowedInternal += $candidateProxy }
    Assert-NetworkContainerProjection $attestedNetwork $allowedInternal @() `
        'cleanup attested internal network'

    $publishNetwork = Get-DockerNetwork $script:Attested.PublishNetworkName
    if ($stateSchema -ceq 'friday.attested-switch-state.v1') {
        if ($null -ne $publishNetwork) {
            Assert-PublishNetworkIdentity $publishNetwork
            Assert-NetworkContainerProjection $publishNetwork @() @() `
                'legacy cleanup pre-existing publish network'
            $publishNetworkReceipt = Get-PublishNetworkReceipt $publishNetwork
        }
    }
    else {
        if ($null -eq $publishNetwork) { throw 'Sealed v2 publish network is absent' }
        Assert-PublishNetworkReceipt $state.publish_network $publishNetwork
        $allowedPublish = @(if ($null -ne $candidateProxy) { $candidateProxy })
        Assert-NetworkContainerProjection $publishNetwork $allowedPublish @() `
            'v2 cleanup publish network'
        $publishNetworkReceipt = $state.publish_network
    }

    Write-Journal 'cleanup_preflight_clear' @{
        state_schema = $stateSchema
        candidate_engine_present = ($null -ne $candidateEngine)
        candidate_proxy_present = ($null -ne $candidateProxy)
        publish_network = $publishNetworkReceipt
        publish_network_permanent = $true
    }
    if (-not $Execute) {
        [pscustomobject]@{
            status = 'cleanup_preflight_clear'
            mutation_authorized = $false
            state_schema = $stateSchema
            candidate_engine_present = ($null -ne $candidateEngine)
            candidate_proxy_present = ($null -ne $candidateProxy)
            removal_order = 'proxy_then_engine'
            publish_network = $publishNetworkReceipt
            publish_network_permanent = $true
            stable_untouched = $true
            backend_bridge_untouched = $true
        } | ConvertTo-Json -Compress -Depth 12
        return
    }

    if ($null -ne $candidateProxy) {
        $stage = 'remove_bound_stopped_proxy'
        Remove-ExactStoppedContainer $candidateProxy ([string]$state.candidate_proxy_id) `
            'bound stopped candidate proxy'
        Write-Journal 'bound_stopped_proxy_removed' @{
            candidate_proxy_id = ([string]$state.candidate_proxy_id).Substring(0, 12)
        }
    }
    if ($null -ne $candidateEngine) {
        $stage = 'remove_bound_stopped_engine'
        Remove-ExactStoppedContainer $candidateEngine ([string]$state.candidate_engine_id) `
            'bound stopped candidate engine'
        Write-Journal 'bound_stopped_engine_removed' @{
            candidate_engine_id = ([string]$state.candidate_engine_id).Substring(0, 12)
        }
    }
    if ($null -ne (Get-Container $script:Attested.CandidateProxyName) -or
        $null -ne (Get-Container $script:Attested.CandidateEngineName)) {
        throw 'Candidate cleanup did not remove exactly both bound names'
    }

    $stage = 'permanent_network_reuse'
    $attestedNetwork = Get-DockerNetwork $script:Attested.AttestedNetworkName
    if ($null -eq $attestedNetwork) { throw 'Cleanup removed the attested internal network' }
    Assert-AttestedInternalNetworkIdentity $attestedNetwork
    Assert-NetworkContainerProjection $attestedNetwork @() @() `
        'cleanup unattached attested internal network'
    Assert-AttestedModelVolume (Get-AttestedModelVolume)
    if ($stateSchema -ceq 'friday.attested-switch-state.v1') {
        $null = Ensure-PublishNetwork
    }
    $publishNetwork = Get-DockerNetwork $script:Attested.PublishNetworkName
    $sealedPublishNetworkReceipt = $(
        if ($stateSchema -ceq 'friday.attested-switch-state.v2') {
            $state.publish_network
        }
        else {
            $null
        }
    )
    $publishNetworkReceipt = Get-CleanupFinalPublishNetworkReceipt $stateSchema `
        $sealedPublishNetworkReceipt $publishNetwork

    $stage = 'stable_recheck'
    $stableEngine = Get-Container $script:Attested.StableEngineName
    $stableProxy = Get-Container $script:Attested.StableProxyName
    if ($null -eq $stableEngine -or $null -eq $stableProxy -or
        [string]$stableEngine.Id -cne [string]$state.stable_engine_id -or
        [string]$stableProxy.Id -cne [string]$state.stable_proxy_id -or
        -not [bool]$stableEngine.State.Running -or -not [bool]$stableProxy.State.Running -or
        [string]$stableEngine.State.Health.Status -cne 'healthy' -or
        [string]$stableProxy.State.Health.Status -cne 'healthy') {
        throw 'Stable pair changed during stopped-candidate cleanup'
    }
    Assert-StableGraph $stableEngine $stableProxy $keyHash
    Assert-SolePublisher $script:Attested.StableProxyName
    Assert-StableCleanupEndpoint $headers
    Assert-Sidecars

    Write-Journal 'cleanup_complete' @{
        state_schema = $stateSchema
        publish_network = $publishNetworkReceipt
        publish_network_permanent = $true
        removed_only_bound_candidate_ids = $true
    }
    [pscustomobject]@{
        status = 'cleanup_complete'
        state_schema = $stateSchema
        candidate_proxy_removed = $true
        candidate_engine_removed = $true
        publish_network = $publishNetworkReceipt
        publish_network_permanent = $true
        attested_network_unattached = $true
        publish_network_unattached = $true
        stable_untouched = $true
        backend_bridge_untouched = $true
        journal = $journalPath
    } | ConvertTo-Json -Compress -Depth 12
}
catch {
    try {
        Write-Journal 'cleanup_failed' @{
            stage = $stage
            error_type = $_.Exception.GetType().FullName
            publish_network = $publishNetworkReceipt
            publish_network_permanent = $true
        }
    }
    catch {}
    throw
}
finally {
    Clear-AttestedEnvironment
    $apiKey = $null
    if ($null -ne $lock) { $lock.Dispose() }
}
