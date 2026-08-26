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

$journalPath = Join-Path $PSScriptRoot ("rollback-attested-{0}.jsonl" -f [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
$lock = $null
$apiKey = $null
$stage = 'initial'
$publishNetworkReceipt = $null

function Write-Journal([string]$State, [hashtable]$Data = @{}) {
    $record = [ordered]@{ at_utc = [DateTime]::UtcNow.ToString('o'); state = $State }
    foreach ($entry in $Data.GetEnumerator()) { $record[$entry.Key] = $entry.Value }
    ($record | ConvertTo-Json -Compress -Depth 12) | Add-Content `
        -LiteralPath $journalPath -Encoding utf8
}

function Assert-State([object]$State, [object]$Receipt) {
    Assert-ExactProperties $State @(
        'schema', 'profile_id', 'stable_engine_id', 'stable_proxy_id',
        'stable_engine_image_id', 'stable_proxy_image_id', 'stable_engine_restart',
        'stable_proxy_restart', 'candidate_engine_id', 'candidate_proxy_id',
        'candidate_engine_image_id', 'candidate_proxy_image_id', 'publish_network', 'key_sha256',
        'written_at_utc'
    ) 'rollback state'
    if ([string]$State.schema -cne 'friday.attested-switch-state.v2' -or
        [string]$State.profile_id -cne $script:Attested.ProfileId -or
        [string]$State.stable_engine_id -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$State.stable_proxy_id -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$State.stable_engine_image_id -cne $script:Attested.StableEngineImageId -or
        [string]$State.stable_proxy_image_id -cne $script:Attested.StableProxyImageId -or
        [string]$State.candidate_engine_image_id -cne [string]$Receipt.engine.image_id -or
        [string]$State.candidate_proxy_image_id -cne [string]$Receipt.proxy.image_id -or
        [string]$State.key_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]::IsNullOrWhiteSpace([string]$State.written_at_utc)) {
        throw 'Rollback state immutable identity is invalid'
    }
    $publishNetwork = Get-DockerNetwork $script:Attested.PublishNetworkName
    if ($null -eq $publishNetwork) { throw 'Recorded durable publish network is absent' }
    Assert-PublishNetworkReceipt $State.publish_network $publishNetwork
    foreach ($field in @('candidate_engine_id', 'candidate_proxy_id')) {
        $value = $State.$field
        if ($null -ne $value -and [string]$value -cnotmatch '^[0-9a-f]{64}$') {
            throw "Rollback state contains an invalid $field"
        }
    }
    foreach ($policy in @([string]$State.stable_engine_restart, [string]$State.stable_proxy_restart)) {
        if ($policy -cnotmatch '^(?:no|always|unless-stopped|on-failure(?::[1-9][0-9]*)?)$') {
            throw 'Rollback restart policy is not allowlisted'
        }
    }
}

function Assert-RestoredEndpoint([hashtable]$Headers) {
    $models = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/v1/models' -Headers $Headers -TimeoutSec 10
    $payload = [string]$models.Content | ConvertFrom-Json
    if (@($payload.data).Count -ne 1 -or [string]$payload.data[0].id -cne 'dispatcher') {
        throw 'Restored model inventory is not exact'
    }
    $serverInfo = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/server_info' -Headers $Headers -TimeoutSec 10
    $stableInfo = [string]$serverInfo.Content | ConvertFrom-Json
    Assert-StableServerInfo $stableInfo
    $metrics = Get-EndpointMetrics $Headers
    if ($metrics.Running -ne 0 -or $metrics.Queued -ne 0) {
        throw 'Restored endpoint is not idle'
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
    $lock = [IO.File]::Open($script:Attested.LockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    Write-Journal 'rollback_preflight_started'

    $stage = 'build_receipt'
    $receipt = Assert-BuildReceipt
    $state = Get-ExactJson $script:Attested.StatePath 'rollback state'
    Assert-State $state $receipt
    $publishNetworkReceipt = $state.publish_network

    $stage = 'identity_validation'
    $stableEngine = Get-Container $script:Attested.StableEngineName
    $stableProxy = Get-Container $script:Attested.StableProxyName
    if ($null -eq $stableEngine -or $null -eq $stableProxy -or
        [string]$stableEngine.Id -cne [string]$state.stable_engine_id -or
        [string]$stableProxy.Id -cne [string]$state.stable_proxy_id) {
        throw 'Preserved stable pair no longer matches rollback state'
    }
    $apiKey = Get-EnvValue $stableProxy 'JARVIS_LLM_API_KEY'
    if ($apiKey -cnotmatch '^[A-Za-z0-9._~-]{32,256}$' -or
        (Get-KeyHash $apiKey) -cne [string]$state.key_sha256) {
        throw 'Preserved API key no longer matches rollback state'
    }
    $keyHash = Get-KeyHash $apiKey
    Assert-StableGraph $stableEngine $stableProxy $keyHash
    Assert-Sidecars

    $candidateEngine = Get-Container $script:Attested.CandidateEngineName
    $candidateProxy = Get-Container $script:Attested.CandidateProxyName
    if ($null -ne $candidateEngine) {
        if ($null -eq $state.candidate_engine_id -or
            [string]$candidateEngine.Id -cne [string]$state.candidate_engine_id) {
            throw 'Candidate engine no longer matches rollback state'
        }
        Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash `
            $publishNetworkReceipt
    }
    elseif ($null -ne $state.candidate_engine_id) {
        throw 'Recorded candidate engine is absent'
    }
    if ($null -ne $candidateProxy -and
        ($null -eq $state.candidate_proxy_id -or [string]$candidateProxy.Id -cne [string]$state.candidate_proxy_id)) {
        throw 'Candidate proxy no longer matches rollback state'
    }
    if ($null -eq $candidateProxy -and $null -ne $state.candidate_proxy_id) {
        throw 'Recorded candidate proxy is absent'
    }
    if ($null -ne $candidateProxy -and $null -eq $candidateEngine) {
        throw 'Recorded candidate proxy exists without its exact engine sibling'
    }

    $publishers = @(& docker ps --filter 'publish=8001' --format '{{.Names}}')
    if ($LASTEXITCODE -ne 0 -or $publishers.Count -gt 1 -or
        @($publishers | Where-Object { [string]$_ -cnotin @(
            $script:Attested.CandidateProxyName, $script:Attested.StableProxyName
        ) }).Count -ne 0) {
        throw 'Port 8001 publisher set is unsafe for rollback'
    }
    if ($null -ne $candidateProxy -and [bool]$candidateProxy.State.Running -and
        ($publishers.Count -ne 1 -or [string]$publishers[0] -cne $script:Attested.CandidateProxyName)) {
        throw 'Running candidate proxy is not the sole port 8001 publisher'
    }

    Write-Journal 'rollback_preflight_clear' @{
        stable_engine_id = ([string]$stableEngine.Id).Substring(0, 12)
        stable_proxy_id = ([string]$stableProxy.Id).Substring(0, 12)
        candidate_engine_present = ($null -ne $candidateEngine)
        candidate_proxy_present = ($null -ne $candidateProxy)
        publish_network = $publishNetworkReceipt
        publish_network_retained = $true
    }
    if (-not $Execute) {
        [pscustomobject]@{
            status = 'rollback_preflight_clear'
            mutation_authorized = $false
            active_publisher = $(if ($publishers.Count -eq 1) { [string]$publishers[0] } else { '' })
            stable_preserved = $true
            candidate_engine_present = ($null -ne $candidateEngine)
            candidate_proxy_present = ($null -ne $candidateProxy)
            publish_network = $publishNetworkReceipt
            publish_network_retained = $true
            backend_bridge_untouched = $true
        } | ConvertTo-Json -Compress -Depth 12
        return
    }

    $env:JARVIS_LLM_API_KEY = $apiKey
    $headers = @{ Authorization = "Bearer $apiKey" }
    $alreadyRestored = (
        [bool]$stableEngine.State.Running -and [bool]$stableProxy.State.Running -and
        [string]$stableEngine.State.Health.Status -ceq 'healthy' -and
        [string]$stableProxy.State.Health.Status -ceq 'healthy' -and
        (Get-RestartSpec $stableEngine) -ceq [string]$state.stable_engine_restart -and
        (Get-RestartSpec $stableProxy) -ceq [string]$state.stable_proxy_restart -and
        $publishers.Count -eq 1 -and [string]$publishers[0] -ceq $script:Attested.StableProxyName -and
        ($null -eq $candidateEngine -or (
            -not [bool]$candidateEngine.State.Running -and (Get-RestartSpec $candidateEngine) -ceq 'no'
        )) -and
        ($null -eq $candidateProxy -or (
            -not [bool]$candidateProxy.State.Running -and (Get-RestartSpec $candidateProxy) -ceq 'no'
        ))
    )
    if ($alreadyRestored) {
        Assert-RestoredEndpoint $headers
        Assert-SolePublisher $script:Attested.StableProxyName
        Write-Journal 'rollback_already_complete'
        [pscustomobject]@{
            status = 'rollback_already_complete'
            active = 'qwen38-v12-attested'
            stable_preserved = $true
            candidate_stopped = $true
            publish_network = $publishNetworkReceipt
            publish_network_retained = $true
            backend_bridge_untouched = $true
            journal = $journalPath
        } | ConvertTo-Json -Compress -Depth 12
        return
    }

    if ($null -ne $candidateProxy) {
        if ([bool]$candidateProxy.State.Running) {
            $stage = 'candidate_endpoint_drain'
            try {
                Wait-EndpointIdle $headers 120
                Write-Journal 'candidate_endpoint_drained'
            }
            catch {
                Write-Journal 'candidate_endpoint_drain_failed' @{ continuing_exact_stop = $true }
            }
        }
        $stage = 'candidate_proxy_stop'
        Stop-ExactContainer $candidateProxy 45
    }

    $candidateEngine = Get-Container $script:Attested.CandidateEngineName
    if ($null -ne $candidateEngine) {
        if ([bool]$candidateEngine.State.Running) {
            $stage = 'candidate_engine_drain'
            try {
                Wait-EngineIdle $script:Attested.CandidateEngineName 120
                Write-Journal 'candidate_engine_drained'
            }
            catch {
                Write-Journal 'candidate_engine_drain_failed' @{ continuing_exact_stop = $true }
            }
        }
        $stage = 'candidate_engine_stop'
        Stop-ExactContainer $candidateEngine 90
    }
    foreach ($name in @($script:Attested.CandidateProxyName, $script:Attested.CandidateEngineName)) {
        $candidate = Get-Container $name
        if ($null -ne $candidate -and ([bool]$candidate.State.Running -or (Get-RestartSpec $candidate) -cne 'no')) {
            throw 'Candidate did not reach exact disarmed stopped state'
        }
    }
    $candidateProxy = Get-Container $script:Attested.CandidateProxyName
    $publishNetwork = Get-DockerNetwork $script:Attested.PublishNetworkName
    if ($null -eq $publishNetwork) { throw 'Durable publish network disappeared during rollback' }
    Assert-PublishNetworkReceipt $publishNetworkReceipt $publishNetwork
    $allowedPublishAttachments = @(if ($null -ne $candidateProxy) { $candidateProxy })
    Assert-NetworkContainerProjection $publishNetwork $allowedPublishAttachments @() `
        'durable publish network after explicit rollback stop'

    $stage = 'stable_restart_policy'
    $stableEngine = Get-Container $script:Attested.StableEngineName
    $stableProxy = Get-Container $script:Attested.StableProxyName
    Assert-StableGraph $stableEngine $stableProxy $keyHash
    if (-not [bool]$stableEngine.State.Running) {
        $stage = 'gpu_release'
        $null = Wait-GpuRelease 180
        $stage = 'stable_restart_policy'
    }
    Set-RestartPolicy ([string]$state.stable_engine_id) ([string]$state.stable_engine_restart)
    Set-RestartPolicy ([string]$state.stable_proxy_id) ([string]$state.stable_proxy_restart)
    $stableEngine = Get-Container $script:Attested.StableEngineName
    $stableProxy = Get-Container $script:Attested.StableProxyName
    Assert-StableGraph $stableEngine $stableProxy $keyHash
    if ((Get-RestartSpec $stableEngine) -cne [string]$state.stable_engine_restart -or
        (Get-RestartSpec $stableProxy) -cne [string]$state.stable_proxy_restart) {
        throw 'Stable restart policies were not restored exactly'
    }

    $stage = 'stable_engine_start'
    if (-not [bool]$stableEngine.State.Running) {
        & docker start ([string]$state.stable_engine_id) | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not start preserved stable engine' }
    }
    $stableEngine = Wait-Healthy $script:Attested.StableEngineName 900

    $stage = 'stable_proxy_start'
    $stableProxy = Get-Container $script:Attested.StableProxyName
    if (-not [bool]$stableProxy.State.Running) {
        & docker start ([string]$state.stable_proxy_id) | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not start preserved stable proxy' }
    }
    $stableProxy = Wait-Healthy $script:Attested.StableProxyName 180

    $stage = 'restored_acceptance'
    Assert-StableGraph $stableEngine $stableProxy $keyHash
    Assert-SolePublisher $script:Attested.StableProxyName
    Assert-RestoredEndpoint $headers
    Assert-Sidecars
    Write-Journal 'rollback_complete' @{
        engine_id = ([string]$stableEngine.Id).Substring(0, 12)
        proxy_id = ([string]$stableProxy.Id).Substring(0, 12)
        publish_network = $publishNetworkReceipt
        publish_network_retained = $true
    }
    [pscustomobject]@{
        status = 'rollback_complete'
        active = 'qwen38-v12-attested'
        stable_engine_id = ([string]$stableEngine.Id).Substring(0, 12)
        stable_proxy_id = ([string]$stableProxy.Id).Substring(0, 12)
        candidate_stopped = $true
        sole_port_8001 = $true
        publish_network = $publishNetworkReceipt
        publish_network_retained = $true
        backend_bridge_untouched = $true
        journal = $journalPath
    } | ConvertTo-Json -Compress -Depth 12
}
catch {
    try {
        Write-Journal 'rollback_failed' @{
            stage = $stage
            error_type = $_.Exception.GetType().FullName
            publish_network_retained = ($null -ne $publishNetworkReceipt)
            publish_network = $publishNetworkReceipt
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
