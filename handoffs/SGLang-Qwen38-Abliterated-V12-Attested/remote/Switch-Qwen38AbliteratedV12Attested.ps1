[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'AttestedBundle.Common.ps1')
Add-Type -AssemblyName System.Net.Http -ErrorAction Stop

if ($Execute -and $PreflightOnly) {
    throw 'Choose either -Execute or -PreflightOnly.'
}

$journalPath = Join-Path $PSScriptRoot ("switch-attested-{0}.jsonl" -f [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
$lock = $null
$apiKey = $null
$mutationStarted = $false
$switchSucceeded = $false
$stage = 'initial'
$receipt = $null
$stableEngine = $null
$stableProxy = $null
$stableEngineId = ''
$stableProxyId = ''
$candidateEngineId = ''
$candidateProxyId = ''
$engineConfig = $null
$proxyConfig = $null
$publishNetworkReceipt = $null
$publishNetworkStatus = ''
$attestedNetworkStatus = ''

function Write-Journal([string]$State, [hashtable]$Data = @{}) {
    $record = [ordered]@{ at_utc = [DateTime]::UtcNow.ToString('o'); state = $State }
    foreach ($entry in $Data.GetEnumerator()) { $record[$entry.Key] = $entry.Value }
    try {
        ($record | ConvertTo-Json -Compress -Depth 12) | Add-Content `
            -LiteralPath $journalPath -Encoding utf8
    }
    catch {
        if (-not $mutationStarted) { throw }
    }
}

function Save-State {
    if ($null -eq $publishNetworkReceipt) {
        throw 'Publish network receipt is absent while sealing rollback state'
    }
    $value = [ordered]@{
        schema = 'friday.attested-switch-state.v2'
        profile_id = $script:Attested.ProfileId
        stable_engine_id = $stableEngineId
        stable_proxy_id = $stableProxyId
        stable_engine_image_id = $script:Attested.StableEngineImageId
        stable_proxy_image_id = $script:Attested.StableProxyImageId
        stable_engine_restart = Get-RestartSpec $stableEngine
        stable_proxy_restart = Get-RestartSpec $stableProxy
        candidate_engine_id = $(if ($candidateEngineId) { $candidateEngineId } else { $null })
        candidate_proxy_id = $(if ($candidateProxyId) { $candidateProxyId } else { $null })
        candidate_engine_image_id = [string]$receipt.engine.image_id
        candidate_proxy_image_id = [string]$receipt.proxy.image_id
        publish_network = $publishNetworkReceipt
        key_sha256 = Get-KeyHash $apiKey
        written_at_utc = [DateTime]::UtcNow.ToString('o')
    }
    Write-AtomicJson $value $script:Attested.StatePath
}

function Get-HttpStatus(
    [string]$Method,
    [string]$Path,
    [hashtable]$Headers = @{}
) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Method $Method `
            -Uri ("http://127.0.0.1:8001" + $Path) -Headers $Headers -TimeoutSec 10
        return [int]$response.StatusCode
    }
    catch {
        if ($null -ne $_.Exception.Response) {
            return [int]$_.Exception.Response.StatusCode
        }
        throw 'Negative-path request failed without an HTTP response'
    }
}

function Get-WrongCaseAuthorization([hashtable]$Headers) {
    $authorization = [string]$Headers.Authorization
    if (-not $authorization.StartsWith('Bearer ', [StringComparison]::Ordinal)) {
        throw 'Authorization header shape is not exact'
    }
    $characters = $authorization.ToCharArray()
    for ($index = 7; $index -lt $characters.Count; $index++) {
        if ([char]::IsLower($characters[$index])) {
            $characters[$index] = [char]::ToUpperInvariant($characters[$index])
            return (-join $characters)
        }
        if ([char]::IsUpper($characters[$index])) {
            $characters[$index] = [char]::ToLowerInvariant($characters[$index])
            return (-join $characters)
        }
    }
    throw 'Current API key has no letter for the wrong-case authorization gate'
}

function Assert-ProxyNegativePaths([hashtable]$Headers) {
    $wrongCaseHeaders = @{ Authorization = (Get-WrongCaseAuthorization $Headers) }
    if ((Get-HttpStatus 'GET' '/health') -ne 200 -or
        (Get-HttpStatus 'POST' '/health') -ne 405 -or
        (Get-HttpStatus 'GET' '/v1/models') -ne 401 -or
        (Get-HttpStatus 'GET' '/v1/models' @{ Authorization = 'Bearer definitely-wrong-key' }) -ne 401 -or
        (Get-HttpStatus 'POST' '/v1/models' $Headers) -ne 405 -or
        (Get-HttpStatus 'GET' '/v1/models/' $Headers) -ne 404 -or
        (Get-HttpStatus 'POST' '/v1/chat/completions') -ne 401 -or
        (Get-HttpStatus 'GET' '/v1/chat/completions' $Headers) -ne 405 -or
        (Get-HttpStatus 'GET' '/metrics') -ne 401 -or
        (Get-HttpStatus 'POST' '/metrics' $Headers) -ne 405 -or
        (Get-HttpStatus 'GET' '/server_info') -ne 401 -or
        (Get-HttpStatus 'POST' '/server_info' $Headers) -ne 405 -or
        (Get-HttpStatus 'GET' '/server_info/') -ne 404 -or
        (Get-HttpStatus 'GET' '/openapi.json' $Headers) -ne 404 -or
        (Get-HttpStatus 'GET' '/v1/files' $Headers) -ne 404 -or
        (Get-HttpStatus 'GET' '/_friday/v1/deployment-witness') -ne 401 -or
        (Get-HttpStatus 'GET' '/_friday/v1/deployment-witness' @{ Authorization = 'Bearer definitely-wrong-key' }) -ne 401 -or
        (Get-HttpStatus 'GET' '/_friday/v1/deployment-witness' $wrongCaseHeaders) -ne 401 -or
        (Get-HttpStatus 'POST' '/_friday/v1/deployment-witness' $Headers) -ne 405 -or
        (Get-HttpStatus 'GET' '/_friday/v1/deployment-witness/extra' $Headers) -ne 404) {
        throw 'Closed proxy allowlist negative-path matrix failed'
    }
}

function Wait-OldWitnessAbsent([hashtable]$Headers, [int]$TimeoutSeconds = 90) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $status = $null
        try {
            $status = Get-HttpStatus 'GET' '/_friday/v1/deployment-witness' $Headers
        }
        catch {
            # nginx may need a moment to bind after the exact proxy container starts.
            Start-Sleep -Seconds 1
            continue
        }
        if ($status -eq 404) { return }
        if ($status -notin @(200, 502, 503, 504)) {
            throw 'Witness disappearance probe returned an unexpected HTTP status'
        }
        Start-Sleep -Seconds 1
    }
    throw 'Old deployment witness was never observed absent during engine restart'
}

function Invoke-Chat([hashtable]$Headers, [hashtable]$Body, [int]$TimeoutSeconds, [string]$Gate) {
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 20 -Compress))
        $response = Invoke-WebRequest -UseBasicParsing -Method Post `
            -Uri 'http://127.0.0.1:8001/v1/chat/completions' -Headers $Headers `
            -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec $TimeoutSeconds
        return [string]$response.Content | ConvertFrom-Json
    }
    catch {
        $status = 0
        if ($null -ne $_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        Write-Journal 'request_failed' @{ gate = $Gate; status_code = $status }
        throw "Candidate request failed at $Gate"
    }
}

function Assert-ComposeConfig([object]$Value, [object]$BuildReceipt, [string]$KeyHash) {
    $services = @($Value.services.PSObject.Properties.Name | Sort-Object)
    if ([string]::Join(',', $services) -cne 'engine,proxy') {
        throw 'Attested Compose must render exactly two sibling services'
    }
    $networks = @($Value.networks.PSObject.Properties.Name | Sort-Object)
    $volumes = @($Value.volumes.PSObject.Properties.Name | Sort-Object)
    if ([string]::Join(',', $networks) -cne 'attested' -or
        [string]::Join(',', $volumes) -cne 'deployment-witness,model-snapshot' -or
        [string]$Value.networks.attested.name -cne $script:Attested.AttestedNetworkName -or
        -not [bool]$Value.networks.attested.internal -or
        [string]$Value.volumes.'model-snapshot'.name -cne $script:Attested.ModelVolumeName -or
        -not [bool]$Value.volumes.'model-snapshot'.external) {
        throw 'Attested Compose network or sealed external volume contract changed'
    }
    $script:engineConfig = $Value.services.engine
    $script:proxyConfig = $Value.services.proxy
    $engineNetworks = @($engineConfig.networks.PSObject.Properties.Name | Sort-Object)
    $proxyNetworks = @($proxyConfig.networks.PSObject.Properties.Name | Sort-Object)
    if ([string]$engineConfig.container_name -cne $script:Attested.CandidateEngineName -or
        [string]$proxyConfig.container_name -cne $script:Attested.CandidateProxyName -or
        [string]$engineConfig.image -cne [string]$BuildReceipt.engine.image_id -or
        [string]$proxyConfig.image -cne [string]$BuildReceipt.proxy.image_id -or
        [string]$engineConfig.pull_policy -cne 'never' -or
        [string]$proxyConfig.pull_policy -cne 'never' -or
        [string]$engineConfig.restart -cne 'unless-stopped' -or
        [string]$proxyConfig.restart -cne 'unless-stopped' -or
        [string]$engineConfig.labels.'com.friday.deployment.engine-image-id' -cne [string]$BuildReceipt.engine.image_id -or
        [string]$engineConfig.labels.'com.friday.deployment.proxy-image-id' -cne [string]$BuildReceipt.proxy.image_id -or
        [string]$proxyConfig.labels.'com.friday.deployment.proxy-image-id' -cne [string]$BuildReceipt.proxy.image_id -or
        [string]$proxyConfig.labels.'com.friday.proxy.openai-key-sha256' -cne $KeyHash -or
        [string]$proxyConfig.environment.SGLANG_UPSTREAM -cne 'engine' -or
        [string]::Join(',', $engineNetworks) -cne 'attested' -or
        [string]::Join(',', $proxyNetworks) -cne 'attested') {
        throw 'Rendered attested Compose identities are not exact'
    }
    Assert-ExactCommand ([pscustomobject]@{ Config = [pscustomobject]@{ Cmd = @($engineConfig.command) } }) `
        $script:ExpectedGraphCommand 'rendered candidate engine'
    $enginePorts = @(if ($null -ne $engineConfig.PSObject.Properties['ports']) { $engineConfig.ports })
    $proxyPorts = @(if ($null -ne $proxyConfig.PSObject.Properties['ports']) { $proxyConfig.ports })
    if ($enginePorts.Count -ne 0 -or $proxyPorts.Count -ne 0) {
        throw 'Base candidate Compose must not publish a host port'
    }
    $modelMounts = @($engineConfig.volumes | Where-Object {
        [string]$_.target -ceq '/models/qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5'
    })
    if ($modelMounts.Count -ne 1 -or [string]$modelMounts[0].type -cne 'volume' -or
        [string]$modelMounts[0].source -cne 'model-snapshot' -or
        -not [bool]$modelMounts[0].read_only) {
        throw 'Rendered candidate model mount is not the exact sealed read-only volume'
    }
    foreach ($forbidden in @('privileged', 'pid', 'network_mode')) {
        if ($null -ne $engineConfig.PSObject.Properties[$forbidden] -or
            $null -ne $proxyConfig.PSObject.Properties[$forbidden]) {
            throw 'Attested Compose isolation was broadened'
        }
    }
    if ([string]$engineConfig.security_opt[0] -cne 'no-new-privileges:true' -or
        [string]$proxyConfig.security_opt[0] -cne 'no-new-privileges:true' -or
        [string]::Join(',', @($proxyConfig.cap_drop)) -cne 'ALL' -or
        [string]::Join(',', @($proxyConfig.cap_add | Sort-Object)) -cne 'CHOWN,DAC_OVERRIDE,SETGID,SETUID') {
        throw 'Attested container hardening changed'
    }
}

function Assert-PublishedComposeConfig([object]$Value, [object]$BuildReceipt, [string]$KeyHash) {
    $services = @($Value.services.PSObject.Properties.Name | Sort-Object)
    $networks = @($Value.networks.PSObject.Properties.Name | Sort-Object)
    $volumes = @($Value.volumes.PSObject.Properties.Name | Sort-Object)
    if ([string]::Join(',', $services) -cne 'engine,proxy' -or
        [string]::Join(',', $networks) -cne 'attested,publish' -or
        [string]::Join(',', $volumes) -cne 'deployment-witness,model-snapshot' -or
        [string]$Value.networks.attested.name -cne $script:Attested.AttestedNetworkName -or
        -not [bool]$Value.networks.attested.internal -or
        [string]$Value.networks.publish.name -cne $script:Attested.PublishNetworkName -or
        -not [bool]$Value.networks.publish.external) {
        throw 'Published Compose network graph is not exact'
    }
    $engine = $Value.services.engine
    $proxy = $Value.services.proxy
    $engineNetworks = @($engine.networks.PSObject.Properties.Name | Sort-Object)
    $proxyNetworks = @($proxy.networks.PSObject.Properties.Name | Sort-Object)
    $engineAttestedEndpoint = $engine.networks.PSObject.Properties['attested'].Value
    $attestedEndpoint = $proxy.networks.PSObject.Properties['attested'].Value
    $publishEndpoint = $proxy.networks.PSObject.Properties['publish'].Value
    $attestedEndpointProperties = @($attestedEndpoint.PSObject.Properties | ForEach-Object {
        [string]$_.Name
    } | Sort-Object)
    $publishEndpointProperties = @($publishEndpoint.PSObject.Properties | ForEach-Object {
        [string]$_.Name
    } | Sort-Object)
    if ([string]$engine.container_name -cne $script:Attested.CandidateEngineName -or
        [string]$proxy.container_name -cne $script:Attested.CandidateProxyName -or
        [string]$engine.image -cne [string]$BuildReceipt.engine.image_id -or
        [string]$proxy.image -cne [string]$BuildReceipt.proxy.image_id -or
        [string]$proxy.labels.'com.friday.proxy.openai-key-sha256' -cne $KeyHash -or
        [string]::Join(',', $engineNetworks) -cne 'attested' -or
        $null -ne $engineAttestedEndpoint -or
        [string]::Join(',', $proxyNetworks) -cne 'attested,publish' -or
        $attestedEndpointProperties.Count -ne 0 -or
        [string]::Join(',', $publishEndpointProperties) -cne 'gw_priority' -or
        [int]$publishEndpoint.gw_priority -ne 1) {
        throw 'Published Compose container topology is not exact'
    }
    $enginePorts = @(if ($null -ne $engine.PSObject.Properties['ports']) { $engine.ports })
    $ports = @($proxy.ports)
    if ($enginePorts.Count -ne 0 -or $ports.Count -ne 1) {
        throw 'Published Compose port cardinality is not exact'
    }
    Assert-ExactProperties $ports[0] @(
        'mode', 'host_ip', 'target', 'published', 'protocol'
    ) 'published Compose proxy port'
    if ([string]$ports[0].mode -cne 'ingress' -or
        [string]$ports[0].host_ip -cne '0.0.0.0' -or
        [int]$ports[0].published -ne 8001 -or [int]$ports[0].target -ne 8080 -or
        [string]$ports[0].protocol -cne 'tcp') {
        throw 'Published Compose port is not exact 0.0.0.0:8001 to proxy 8080/tcp'
    }
    foreach ($forbidden in @('privileged', 'pid', 'network_mode')) {
        if ($null -ne $engine.PSObject.Properties[$forbidden] -or
            $null -ne $proxy.PSObject.Properties[$forbidden]) {
            throw 'Published Compose isolation was broadened'
        }
    }
    if ([string]$engine.security_opt[0] -cne 'no-new-privileges:true' -or
        [string]$proxy.security_opt[0] -cne 'no-new-privileges:true' -or
        [string]::Join(',', @($proxy.cap_drop)) -cne 'ALL' -or
        [string]::Join(',', @($proxy.cap_add | Sort-Object)) -cne 'CHOWN,DAC_OVERRIDE,SETGID,SETUID') {
        throw 'Published Compose container hardening changed'
    }
}

function Assert-CurrentEndpoint([hashtable]$Headers) {
    $models = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/v1/models' -Headers $Headers -TimeoutSec 10
    $payload = [string]$models.Content | ConvertFrom-Json
    if (@($payload.data).Count -ne 1 -or [string]$payload.data[0].id -cne 'dispatcher') {
        throw 'Stable dispatcher model inventory is not exact'
    }
    $info = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/server_info' -Headers $Headers -TimeoutSec 10
    $stableInfo = [string]$info.Content | ConvertFrom-Json
    Assert-StableServerInfo $stableInfo
    $null = Get-EndpointMetrics $Headers
    $stableWitnessResponse = Invoke-WebRequest -UseBasicParsing `
        -Uri 'http://127.0.0.1:8001/_friday/v1/deployment-witness' `
        -Headers $Headers -TimeoutSec 10
    Assert-StableDeploymentWitness `
        ([string]$stableWitnessResponse.Content | ConvertFrom-Json) $stableInfo
}

function Restore-Stable {
    Write-Journal 'automatic_rollback_started' @{ failed_stage = $stage }
    $candidateProxy = Get-Container $script:Attested.CandidateProxyName
    $candidateEngine = Get-Container $script:Attested.CandidateEngineName
    if ($null -ne $candidateProxy) {
        if (-not $candidateProxyId -or [string]$candidateProxy.Id -cne $candidateProxyId -or
            [string]$candidateProxy.Image -cne [string]$receipt.proxy.image_id) {
            throw 'Refusing to stop a non-attested proxy during automatic rollback'
        }
        Stop-ExactContainer $candidateProxy 45
    }
    if ($null -ne $candidateEngine) {
        if (-not $candidateEngineId -or [string]$candidateEngine.Id -cne $candidateEngineId -or
            [string]$candidateEngine.Image -cne [string]$receipt.engine.image_id) {
            throw 'Refusing to stop a non-attested engine during automatic rollback'
        }
        if ([bool]$candidateEngine.State.Running) {
            try { Wait-EngineIdle $script:Attested.CandidateEngineName 90 }
            catch { Write-Journal 'candidate_drain_failed' @{ continuing_exact_stop = $true } }
        }
        Stop-ExactContainer $candidateEngine 90
    }
    $candidateProxy = Get-Container $script:Attested.CandidateProxyName
    $publishNetwork = Get-DockerNetwork $script:Attested.PublishNetworkName
    if ($null -eq $publishNetwork) { throw 'Durable publish network disappeared during rollback' }
    Assert-PublishNetworkReceipt $publishNetworkReceipt $publishNetwork
    $allowedPublishAttachments = @(if ($null -ne $candidateProxy) { $candidateProxy })
    Assert-NetworkContainerProjection $publishNetwork $allowedPublishAttachments @() `
        'durable publish network after candidate stop'
    $stableEngineNow = Get-Container $script:Attested.StableEngineName
    $stableProxyNow = Get-Container $script:Attested.StableProxyName
    if ($null -eq $stableEngineNow -or $null -eq $stableProxyNow -or
        [string]$stableEngineNow.Id -cne $stableEngineId -or [string]$stableProxyNow.Id -cne $stableProxyId) {
        throw 'Preserved stable container identities changed during rollback'
    }
    Assert-StableGraph $stableEngineNow $stableProxyNow (Get-KeyHash $apiKey)
    if (-not [bool]$stableEngineNow.State.Running) {
        $null = Wait-GpuRelease 180
    }
    Set-RestartPolicy $stableEngineId (Get-RestartSpec $stableEngine)
    Set-RestartPolicy $stableProxyId (Get-RestartSpec $stableProxy)
    $stableEngineNow = Get-Container $script:Attested.StableEngineName
    $stableProxyNow = Get-Container $script:Attested.StableProxyName
    if ([string]$stableEngineNow.Id -cne $stableEngineId -or
        [string]$stableProxyNow.Id -cne $stableProxyId -or
        (Get-RestartSpec $stableEngineNow) -cne (Get-RestartSpec $stableEngine) -or
        (Get-RestartSpec $stableProxyNow) -cne (Get-RestartSpec $stableProxy)) {
        throw 'Stable restart policies were not restored exactly'
    }
    if (-not [bool]$stableEngineNow.State.Running) {
        & docker start $stableEngineId | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not restart preserved stable engine' }
    }
    $stableEngineNow = Wait-Healthy $script:Attested.StableEngineName 900
    $stableProxyNow = Get-Container $script:Attested.StableProxyName
    if (-not [bool]$stableProxyNow.State.Running) {
        & docker start $stableProxyId | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not restart preserved stable proxy' }
    }
    $stableProxyNow = Wait-Healthy $script:Attested.StableProxyName 180
    Assert-StableGraph $stableEngineNow $stableProxyNow (Get-KeyHash $apiKey)
    $headers = @{ Authorization = "Bearer $apiKey" }
    Assert-CurrentEndpoint $headers
    Assert-SolePublisher $script:Attested.StableProxyName
    Assert-Sidecars
    Write-Journal 'automatic_rollback_complete' @{
        stable_engine_id = $stableEngineId.Substring(0, 12)
        stable_proxy_id = $stableProxyId.Substring(0, 12)
        publish_network = $publishNetworkReceipt
        publish_network_retained = $true
    }
}

function Invoke-SixWayProbe([string]$Secret) {
    $client = [Net.Http.HttpClient]::new()
    try {
        $client.Timeout = [TimeSpan]::FromSeconds(180)
        $client.DefaultRequestHeaders.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $Secret)
        $tasks = @()
        for ($index = 1; $index -le 6; $index += 1) {
            $body = [ordered]@{
                model = 'dispatcher'
                messages = @(@{ role = 'user'; content = "Reply with one word: READY$index" })
                max_tokens = 24
                temperature = 0.0
                seed = 205000 + $index
                stream = $false
                chat_template_kwargs = @{ enable_thinking = $false }
            } | ConvertTo-Json -Depth 10 -Compress
            $content = [Net.Http.StringContent]::new($body, [Text.Encoding]::UTF8, 'application/json')
            $tasks += $client.PostAsync('http://127.0.0.1:8001/v1/chat/completions', $content)
        }
        $all = [Threading.Tasks.Task]::WhenAll([Threading.Tasks.Task[]]$tasks)
        if (-not $all.Wait(180000)) { throw 'Six-way request probe timed out' }
        foreach ($response in $tasks | ForEach-Object { $_.Result }) {
            if ([int]$response.StatusCode -ne 200) { throw 'Six-way request probe returned non-200' }
            $payload = $response.Content.ReadAsStringAsync().Result | ConvertFrom-Json
            if ([string]$payload.model -cne 'dispatcher' -or @($payload.choices).Count -ne 1 -or
                [string]::IsNullOrWhiteSpace([string]$payload.choices[0].message.content)) {
                throw 'Six-way response contract is invalid'
            }
        }
    }
    finally {
        $client.Dispose()
    }
}

try {
    $stage = 'lock'
    $lockDirectory = Split-Path -Parent $script:Attested.LockPath
    if (-not (Test-Path -LiteralPath $lockDirectory -PathType Container)) {
        throw 'Shared switch-lock directory is absent'
    }
    $lock = [IO.File]::Open($script:Attested.LockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    Write-Journal 'preflight_started'

    $stage = 'build_receipt'
    $receipt = Assert-BuildReceipt
    Assert-ModelSnapshot
    $modelVolumeStatus = Assert-ModelVolumePreflight $receipt

    $stage = 'stable_identity'
    $stableEngine = Get-Container $script:Attested.StableEngineName
    $stableProxy = Get-Container $script:Attested.StableProxyName
    if ($null -eq $stableEngine -or $null -eq $stableProxy -or
        -not [bool]$stableEngine.State.Running -or -not [bool]$stableProxy.State.Running -or
        [string]$stableEngine.State.Health.Status -cne 'healthy' -or
        [string]$stableProxy.State.Health.Status -cne 'healthy') {
        throw 'Exact stable graph pair is not healthy'
    }
    $stableEngineId = [string]$stableEngine.Id
    $stableProxyId = [string]$stableProxy.Id
    $apiKey = Get-EnvValue $stableProxy 'JARVIS_LLM_API_KEY'
    if ($apiKey -cnotmatch '^[A-Za-z0-9._~-]{32,256}$') { throw 'Dispatcher API key shape is not safe' }
    $keyHash = Get-KeyHash $apiKey
    Assert-StableGraph $stableEngine $stableProxy $keyHash
    Assert-SolePublisher $script:Attested.StableProxyName
    foreach ($name in @($script:Attested.CandidateEngineName, $script:Attested.CandidateProxyName)) {
        if ($null -ne (Get-Container $name)) { throw 'A sibling candidate container already exists' }
    }
    Assert-Sidecars

    $stage = 'network_preflight'
    $attestedNetworkStatus = Get-AttestedNetworkPreflight
    $publishPreflight = Get-PublishNetworkPreflight
    $publishNetworkStatus = [string]$publishPreflight.Status
    $publishNetworkReceipt = $publishPreflight.Receipt

    $stage = 'compose_render'
    $env:JARVIS_LLM_API_KEY = $apiKey
    $env:JARVIS_LLM_API_KEY_SHA256 = $keyHash
    $env:JARVIS_QWEN38_ATTESTED_CACHE_HOST_PATH = $script:Attested.CachePath.Replace('\', '/')
    $env:JARVIS_OPENAI_BIND_ADDRESS = '0.0.0.0'
    $rendered = Invoke-Compose $receipt @('config', '--format', 'json')
    $compose = [string]::Join([Environment]::NewLine, $rendered) | ConvertFrom-Json
    Assert-ComposeConfig $compose $receipt $keyHash
    $publishedRendered = Invoke-Compose $receipt @('config', '--format', 'json') -Publish8001
    $published = [string]::Join([Environment]::NewLine, $publishedRendered) | ConvertFrom-Json
    Assert-PublishedComposeConfig $published $receipt $keyHash

    $stage = 'stable_endpoint'
    $headers = @{ Authorization = "Bearer $apiKey" }
    Assert-CurrentEndpoint $headers
    Write-Journal 'preflight_clear' @{
        stable_engine_id = $stableEngineId.Substring(0, 12)
        stable_proxy_id = $stableProxyId.Substring(0, 12)
        candidate_engine_image_id = [string]$receipt.engine.image_id
        candidate_proxy_image_id = [string]$receipt.proxy.image_id
        attested_network = $attestedNetworkStatus
        publish_network = $publishNetworkStatus
        publish_network_receipt = $publishNetworkReceipt
    }
    if (-not $Execute) {
        [pscustomobject]@{
            status = 'preflight_clear'
            mutation_authorized = $false
            stable = 'qwen38-v12-attested'
            candidate = 'qwen38-abliterated-v12-attested'
            profile_id = $script:Attested.ProfileId
            context_length = 40960
            max_running_requests = 6
            decode_cuda_graphs = 'full-bs1-6'
            sealed_model_volume = $modelVolumeStatus
            attested_network = $attestedNetworkStatus
            publish_network = $publishNetworkStatus
            publish_network_receipt = $publishNetworkReceipt
            publish_network_expected = [ordered]@{
                name = $script:Attested.PublishNetworkName
                driver = 'bridge'
                internal = $false
                attachable = $false
                labels = Get-ExpectedPublishNetworkLabels
            }
            stable_untouched = $true
            backend_bridge_untouched = $true
        } | ConvertTo-Json -Compress -Depth 12
        return
    }

    $stage = 'sealed_model_volume'
    try {
        Ensure-AttestedModelVolume $receipt
    }
    catch {
        $cleanup = [string]$_.Exception.Data['new_model_volume_cleanup']
        if ($cleanup -notin @('not_applicable', 'removed_exact_new_volume', 'failed_closed')) {
            $cleanup = 'failed_closed'
        }
        Write-Journal 'sealed_model_volume_failed' @{ new_volume_cleanup = $cleanup }
        throw
    }
    Write-Journal 'sealed_model_volume_verified' @{ volume = $script:Attested.ModelVolumeName }

    $stage = 'durable_publish_network'
    $publishNetworkReceipt = Ensure-PublishNetwork
    $publishNetworkStatus = 'verified_durable'
    Write-Journal 'durable_publish_network_verified' @{
        publish_network = $publishNetworkReceipt
        retained_on_rollback = $true
    }

    $stage = 'state_seal'
    Save-State

    $stage = 'stable_drain'
    Wait-EndpointIdle $headers 120
    Write-Journal 'stable_endpoint_drained'
    $mutationStarted = $true

    $stage = 'stable_proxy_stop'
    Stop-ExactContainer $stableProxy 45
    Wait-EngineIdle $script:Attested.StableEngineName 120

    $stage = 'stable_engine_stop'
    Stop-ExactContainer $stableEngine 90
    $null = Wait-GpuRelease 180
    Write-Journal 'stable_preserved_stopped' @{
        engine_id = $stableEngineId.Substring(0, 12)
        proxy_id = $stableProxyId.Substring(0, 12)
    }

    $stage = 'candidate_engine_start'
    try {
        $null = Invoke-Compose $receipt @('up', '-d', '--no-deps', '--pull', 'never', 'engine')
    }
    finally {
        $created = Get-Container $script:Attested.CandidateEngineName
        if ($null -ne $created) { $candidateEngineId = [string]$created.Id; Save-State }
    }
    $candidateEngine = Wait-Healthy $script:Attested.CandidateEngineName 1200
    if ([string]$candidateEngine.Id -cne $candidateEngineId -or
        (Get-RestartSpec $candidateEngine) -cne 'unless-stopped') {
        throw 'Candidate engine identity changed during startup'
    }
    Assert-CandidateContainers $candidateEngine $null $receipt $keyHash $publishNetworkReceipt
    Assert-FatalFree $candidateEngine
    $null = Assert-GpuHeadroom

    $stage = 'candidate_proxy_start'
    try {
        $null = Invoke-Compose $receipt @('up', '-d', '--no-deps', '--pull', 'never', 'proxy') -Publish8001
    }
    finally {
        $created = Get-Container $script:Attested.CandidateProxyName
        if ($null -ne $created) { $candidateProxyId = [string]$created.Id; Save-State }
    }
    $candidateProxy = Wait-Healthy $script:Attested.CandidateProxyName 180
    if ([string]$candidateProxy.Id -cne $candidateProxyId -or
        (Get-RestartSpec $candidateProxy) -cne 'unless-stopped') {
        throw 'Candidate proxy identity changed during startup'
    }
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash $publishNetworkReceipt
    Wait-SolePublisher $script:Attested.CandidateProxyName 120
    $candidateProxy = Get-Container $script:Attested.CandidateProxyName
    if ($null -eq $candidateProxy -or [string]$candidateProxy.Id -cne $candidateProxyId -or
        -not [bool]$candidateProxy.State.Running) {
        throw 'Candidate proxy identity changed after publisher registration'
    }
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash $publishNetworkReceipt
    Assert-CandidateProxyPortPublication $candidateProxy

    $stage = 'quick_health_models_witness'
    if ((Get-HttpStatus 'GET' '/health') -ne 200) {
        throw 'Candidate health route did not return 200'
    }
    $models = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/v1/models' `
        -Headers $headers -TimeoutSec 10
    $modelsPayload = [string]$models.Content | ConvertFrom-Json
    if (@($modelsPayload.data).Count -ne 1 -or [string]$modelsPayload.data[0].id -cne 'dispatcher') {
        throw 'Candidate model inventory is not exact'
    }
    $serverInfoResponse = Invoke-WebRequest -UseBasicParsing `
        -Uri 'http://127.0.0.1:8001/server_info' -Headers $headers -TimeoutSec 10
    $quickServerInfo = [string]$serverInfoResponse.Content | ConvertFrom-Json
    Assert-ServerInfo $quickServerInfo
    $quickWitnessResponse = Invoke-WebRequest -UseBasicParsing `
        -Uri 'http://127.0.0.1:8001/_friday/v1/deployment-witness' `
        -Headers $headers -TimeoutSec 10
    $quickWitness = [string]$quickWitnessResponse.Content | ConvertFrom-Json
    Assert-DeploymentWitness $quickWitness $quickServerInfo $receipt

    $stage = 'quick_chat_smoke'
    $chatBody = @{
        model = 'dispatcher'
        messages = @(@{ role = 'user'; content = 'Reply briefly with primary-ok.' })
        max_tokens = 32
        temperature = 0.0
        stream = $false
        chat_template_kwargs = @{ enable_thinking = $false }
    }
    $chatResponse = Invoke-Chat $headers $chatBody 120 'quick_chat_smoke'
    if ([string]$chatResponse.model -cne 'dispatcher' -or
        [string]::IsNullOrWhiteSpace([string]$chatResponse.choices[0].message.content) -or
        -not [string]::IsNullOrWhiteSpace([string]$chatResponse.choices[0].message.reasoning_content)) {
        throw 'Candidate chat smoke failed'
    }

    $stage = 'quick_tool_smoke'
    $toolBody = @{
        model = 'dispatcher'
        messages = @(@{
            role = 'user'
            content = 'Call get_weather exactly once for Moscow. Do not answer directly.'
        })
        tools = @(@{
            type = 'function'
            function = @{
                name = 'get_weather'
                description = 'Return current weather for one city.'
                parameters = @{
                    type = 'object'
                    properties = @{ city = @{ type = 'string' } }
                    required = @('city')
                    additionalProperties = $false
                }
            }
        })
        tool_choice = @{ type = 'function'; function = @{ name = 'get_weather' } }
        max_tokens = 128
        temperature = 0.0
        stream = $false
        chat_template_kwargs = @{ enable_thinking = $false }
    }
    $toolResponse = Invoke-Chat $headers $toolBody 120 'quick_tool_smoke'
    $toolCalls = @($toolResponse.choices[0].message.tool_calls)
    if ([string]$toolResponse.model -cne 'dispatcher' -or $toolCalls.Count -ne 1 -or
        [string]$toolCalls[0].type -cne 'function' -or
        [string]$toolCalls[0].function.name -cne 'get_weather') {
        throw 'Candidate native tool-call shape failed'
    }
    $toolArguments = [string]$toolCalls[0].function.arguments | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace([string]$toolArguments.city)) {
        throw 'Candidate native tool-call arguments failed'
    }
    Wait-EndpointIdle $headers 60
    $quickMetrics = Get-EndpointMetrics $headers
    if ($quickMetrics.Running -ne 0 -or $quickMetrics.Queued -ne 0) {
        throw 'Candidate is not idle after quick smokes'
    }
    $gpu = Get-GpuMemory
    $longTokens = 0
    $witness2 = $quickWitness

    # The migration request intentionally authorizes only fast health/models/chat/tool
    # smokes.  Keep the inherited extended battery visible for later certification,
    # but do not run it during this primary replacement.
    if ($false) {
    $stage = 'proxy_negative_paths'
    Assert-ProxyNegativePaths $headers

    $stage = 'identity_witness'
    $models = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/v1/models' -Headers $headers -TimeoutSec 10
    $modelsPayload = [string]$models.Content | ConvertFrom-Json
    if (@($modelsPayload.data).Count -ne 1 -or [string]$modelsPayload.data[0].id -cne 'dispatcher') {
        throw 'Candidate model inventory is not exact'
    }
    $serverInfoResponse = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/server_info' -Headers $headers -TimeoutSec 10
    $serverInfo = [string]$serverInfoResponse.Content | ConvertFrom-Json
    Assert-ServerInfo $serverInfo
    $witnessResponse1 = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/_friday/v1/deployment-witness' -Headers $headers -TimeoutSec 10
    $witnessRaw1 = [string]$witnessResponse1.Content
    $witnessResponse2 = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/_friday/v1/deployment-witness' -Headers $headers -TimeoutSec 10
    $witnessRaw2 = [string]$witnessResponse2.Content
    if ([Text.Encoding]::UTF8.GetByteCount($witnessRaw1) -gt 8192 -or $witnessRaw1 -cne $witnessRaw2) {
        throw 'Deployment witness is oversized or changed within one process'
    }
    $witness = $witnessRaw1 | ConvertFrom-Json
    Assert-DeploymentWitness $witness $serverInfo $receipt
    $metrics = Get-EndpointMetrics $headers
    if ($metrics.Running -ne 0 -or $metrics.Queued -ne 0) { throw 'Candidate is not idle before acceptance' }

    $stage = 'identity_witness_gpu_observation_non_acceptance'
    $identityWitnessGpu = Get-GpuMemory
    Write-Journal 'identity_witness_gpu_headroom_observed_non_acceptance' @{
        free_mib = [int]$identityWitnessGpu.FreeMiB
        minimum_free_mib = $script:Attested.MinimumCandidateFreeMiB
        strict_headroom_stage = 'candidate_engine_start'
        acceptance = $false
    }

    $stage = 'text_schema'
    $schemaBody = @{
        model = 'dispatcher'
        messages = @(@{ role = 'user'; content = 'Return status ok and count 2 in the required JSON object.' })
        max_tokens = 48
        temperature = 0.0
        stream = $false
        chat_template_kwargs = @{ enable_thinking = $false }
        response_format = @{
            type = 'json_schema'
            json_schema = @{
                name = 'attested_health_probe'
                strict = $true
                schema = @{
                    type = 'object'
                    properties = @{
                        status = @{ type = 'string'; enum = @('ok') }
                        count = @{ type = 'integer'; enum = @(2) }
                    }
                    required = @('status', 'count')
                    additionalProperties = $false
                }
            }
        }
    }
    $schemaResponse = Invoke-Chat $headers $schemaBody 120 'text_schema'
    $schemaValue = [string]$schemaResponse.choices[0].message.content | ConvertFrom-Json
    if ([string]$schemaResponse.model -cne 'dispatcher' -or [string]$schemaValue.status -cne 'ok' -or
        [int]$schemaValue.count -ne 2 -or @($schemaValue.PSObject.Properties).Count -ne 2) {
        throw 'Text/JSON-schema acceptance failed'
    }

    $stage = 'image'
    $probePng = 'iVBORw0KGgoAAAANSUhEUgAAABwAAAAcCAIAAAD9b0jDAAAAI0lEQVR42u3MsQ0AAAjAoP7/tD7hJgkzTZ1LKpVKpVKp9Ee6DsoNHtgm0ZUAAAAASUVORK5CYII='
    $imageBody = @{
        model = 'dispatcher'
        messages = @(@{
            role = 'user'
            content = @(
                @{ type = 'text'; text = 'State one visible property of this synthetic test image.' },
                @{ type = 'image_url'; image_url = @{ url = "data:image/png;base64,$probePng" } }
            )
        })
        max_tokens = 48
        temperature = 0.0
        stream = $false
        chat_template_kwargs = @{ enable_thinking = $false }
    }
    $imageResponse = Invoke-Chat $headers $imageBody 180 'image'
    if ([string]$imageResponse.model -cne 'dispatcher' -or
        [string]::IsNullOrWhiteSpace([string]$imageResponse.choices[0].message.content) -or
        -not [string]::IsNullOrWhiteSpace([string]$imageResponse.choices[0].message.reasoning_content)) {
        throw 'Synthetic image acceptance failed'
    }

    $stage = 'soak_settle'
    Start-Sleep -Seconds 60

    $stage = 'soak_candidate_gates'
    $candidateEngine = Wait-Healthy $script:Attested.CandidateEngineName 30
    $candidateProxy = Wait-Healthy $script:Attested.CandidateProxyName 30
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash $publishNetworkReceipt
    Assert-FatalFree $candidateEngine

    $stage = 'soak_witness_gate'
    $witnessAfter = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/_friday/v1/deployment-witness' -Headers $headers -TimeoutSec 10
    if ([string]$witnessAfter.Content -cne $witnessRaw1) { throw 'Process witness changed during soak' }

    $stage = 'soak_external_gates'
    Assert-Sidecars
    Assert-SolePublisher $script:Attested.CandidateProxyName
    Assert-CandidateProxyPortPublication $candidateProxy

    $stage = 'soak_gpu_observation_non_acceptance'
    $postSoakGpu = Get-GpuMemory
    Write-Journal 'post_soak_gpu_headroom_observed_non_acceptance' @{
        free_mib = [int]$postSoakGpu.FreeMiB
        minimum_free_mib = $script:Attested.MinimumCandidateFreeMiB
        strict_headroom_stage = 'candidate_engine_start'
        acceptance = $false
    }

    $stage = 'six_way_probe'
    Invoke-SixWayProbe $apiKey

    $stage = 'six_way_drain'
    Wait-EndpointIdle $headers 180
    $postSixWayGpu = Get-GpuMemory
    Write-Journal 'post_six_way_gpu_headroom_observed_during_cumulative_stress' @{
        free_mib = [int]$postSixWayGpu.FreeMiB
        minimum_free_mib = $script:Attested.MinimumCandidateFreeMiB
        request_count = 6
        strict_headroom_stage = 'epoch_restart_health'
    }

    $stage = 'long_context'
    $longBody = @{
        model = 'dispatcher'
        messages = @(@{ role = 'user'; content = ((-join (' z' * 34000)) + ' End marker. Explain in at least 100 words that the marker was present.') })
        max_tokens = 192
        temperature = 0.2
        stream = $false
        chat_template_kwargs = @{ enable_thinking = $false }
    }
    $longResponse = Invoke-Chat $headers $longBody 300 'long_context'
    $longTokens = [int]$longResponse.usage.prompt_tokens
    if ($longTokens -lt 32000 -or $longTokens -gt 40000 -or
        [int]$longResponse.usage.completion_tokens -lt 32 -or
        [string]::IsNullOrWhiteSpace([string]$longResponse.choices[0].message.content)) {
        throw '40K context acceptance did not exercise the required window'
    }

    $stage = 'long_context_drain'
    Wait-EndpointIdle $headers 180
    $candidateEngine = Wait-Healthy $script:Attested.CandidateEngineName 30
    $candidateProxy = Wait-Healthy $script:Attested.CandidateProxyName 30
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash $publishNetworkReceipt
    Assert-FatalFree $candidateEngine
    $postLongGpu = Get-GpuMemory
    Write-Journal 'post_long_context_gpu_headroom_observed_before_epoch_restart' @{
        free_mib = [int]$postLongGpu.FreeMiB
        minimum_free_mib = $script:Attested.MinimumCandidateFreeMiB
        request_count = 1
        strict_headroom_stage = 'epoch_restart_health'
    }

    $stage = 'epoch_restart_drain'
    Wait-EndpointIdle $headers 120
    Stop-ExactContainer $candidateProxy 45
    Wait-EngineIdle $script:Attested.CandidateEngineName 120
    Stop-ExactContainer $candidateEngine 90
    $null = Wait-GpuRelease 180

    $stage = 'epoch_restart_engine'
    & docker start $candidateEngineId | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restart exact candidate engine for epoch rehearsal' }
    $candidateEngine = Get-Container $script:Attested.CandidateEngineName
    if ($null -eq $candidateEngine -or [string]$candidateEngine.Id -cne $candidateEngineId) {
        throw 'Candidate engine identity changed during epoch rehearsal'
    }

    # The witness volume is shared only with the closed proxy.  Start that same
    # exact proxy briefly while the wrapper re-hashes the model and require a
    # 404 before the new witness appears.  This proves the old epoch is not
    # carried across the exec boundary.
    $stage = 'epoch_old_witness_disappearance'
    & docker start $candidateProxyId | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start exact proxy for witness disappearance proof' }
    Wait-OldWitnessAbsent $headers 90
    $candidateProxy = Get-Container $script:Attested.CandidateProxyName
    if ($null -eq $candidateProxy -or [string]$candidateProxy.Id -cne $candidateProxyId) {
        throw 'Candidate proxy identity changed during epoch rehearsal'
    }
    Stop-ExactContainer $candidateProxy 45

    $stage = 'epoch_restart_health'
    $candidateEngine = Wait-Healthy $script:Attested.CandidateEngineName 1200
    Assert-CandidateContainers $candidateEngine $null $receipt $keyHash $publishNetworkReceipt
    Assert-FatalFree $candidateEngine
    $gpu = Assert-GpuHeadroom
    & docker start $candidateProxyId | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restart exact candidate proxy after epoch rehearsal' }
    $candidateProxy = Wait-Healthy $script:Attested.CandidateProxyName 180
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash $publishNetworkReceipt
    Wait-SolePublisher $script:Attested.CandidateProxyName 120
    $candidateProxy = Get-Container $script:Attested.CandidateProxyName
    if ($null -eq $candidateProxy -or [string]$candidateProxy.Id -cne $candidateProxyId -or
        -not [bool]$candidateProxy.State.Running) {
        throw 'Candidate proxy identity changed after epoch publisher registration'
    }
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash $publishNetworkReceipt
    Assert-CandidateProxyPortPublication $candidateProxy
    Assert-ProxyNegativePaths $headers

    $stage = 'epoch_rotation_proof'
    $serverInfoAfter = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/server_info' -Headers $headers -TimeoutSec 10
    $serverInfo2 = [string]$serverInfoAfter.Content | ConvertFrom-Json
    Assert-ServerInfo $serverInfo2
    $witnessAfterRestart = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/_friday/v1/deployment-witness' -Headers $headers -TimeoutSec 10
    $witnessRaw2 = [string]$witnessAfterRestart.Content
    if ($witnessRaw2 -ceq $witnessRaw1) {
        throw 'Engine restart did not rotate the canonical process witness'
    }
    $witness2 = $witnessRaw2 | ConvertFrom-Json
    Assert-DeploymentWitness $witness2 $serverInfo2 $receipt
    if ([string]$witness2.engine_start_nonce -ceq [string]$witness.engine_start_nonce) {
        throw 'Engine restart reused the previous process nonce'
    }
    $metricsAfterRestart = Get-EndpointMetrics $headers
    if ($metricsAfterRestart.Running -ne 0 -or $metricsAfterRestart.Queued -ne 0) {
        throw 'Restarted candidate is not idle before the final smoke'
    }

    $stage = 'epoch_restart_text_smoke'
    $schemaResponse2 = Invoke-Chat $headers $schemaBody 120 'epoch_restart_text_smoke'
    $schemaValue2 = [string]$schemaResponse2.choices[0].message.content | ConvertFrom-Json
    if ([string]$schemaResponse2.model -cne 'dispatcher' -or [string]$schemaValue2.status -cne 'ok' -or
        [int]$schemaValue2.count -ne 2 -or @($schemaValue2.PSObject.Properties).Count -ne 2) {
        throw 'Post-restart text/JSON-schema smoke failed'
    }

    $stage = 'epoch_restart_post_smoke_settle'
    Start-Sleep -Seconds 30

    $stage = 'epoch_restart_post_smoke_candidate_gates'
    $candidateEngine = Wait-Healthy $script:Attested.CandidateEngineName 30
    $candidateProxy = Wait-Healthy $script:Attested.CandidateProxyName 30
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash $publishNetworkReceipt
    Assert-CandidateProxyPortPublication $candidateProxy
    Assert-FatalFree $candidateEngine

    $stage = 'epoch_restart_post_smoke_gpu_observation'
    $postRestartSmokeGpu = Get-GpuMemory
    Write-Journal 'post_epoch_restart_smoke_gpu_headroom_observed' @{
        free_mib = [int]$postRestartSmokeGpu.FreeMiB
        minimum_free_mib = $script:Attested.MinimumCandidateFreeMiB
        request_count = 1
        strict_headroom_stage = 'epoch_restart_health'
    }

    $stage = 'epoch_restart_post_smoke_external_gates'
    Assert-Sidecars
    Assert-SolePublisher $script:Attested.CandidateProxyName
    }

    $stage = 'arm_candidate'
    $expectedEngineRestart = 'unless-stopped'
    $expectedProxyRestart = 'unless-stopped'
    Set-RestartPolicy $candidateEngineId $expectedEngineRestart
    Set-RestartPolicy $candidateProxyId $expectedProxyRestart
    $candidateEngine = Wait-Healthy $script:Attested.CandidateEngineName 30
    $candidateProxy = Wait-Healthy $script:Attested.CandidateProxyName 30
    if ((Get-RestartSpec $candidateEngine) -cne $expectedEngineRestart -or
        (Get-RestartSpec $candidateProxy) -cne $expectedProxyRestart) {
        throw 'Candidate restart policies were not armed exactly'
    }
    Assert-CandidateContainers $candidateEngine $candidateProxy $receipt $keyHash $publishNetworkReceipt
    Assert-SolePublisher $script:Attested.CandidateProxyName
    Assert-CandidateProxyPortPublication $candidateProxy
    $switchSucceeded = $true
    Write-Journal 'ready' @{
        engine_id = $candidateEngineId.Substring(0, 12)
        proxy_id = $candidateProxyId.Substring(0, 12)
        gpu_free_mib = $gpu.FreeMiB
        witness_nonce_sha256 = Get-KeyHash ([string]$witness2.engine_start_nonce)
        quick_smokes = @('health', 'models', 'chat', 'tool')
        extended_acceptance_run = $false
        armed_restart_policy = 'unless-stopped'
        publish_network = $publishNetworkReceipt
        publish_network_retained_on_rollback = $true
    }
    [pscustomobject]@{
        status = 'ready'
        active = 'qwen38-abliterated-v12-attested'
        profile_id = $script:Attested.ProfileId
        context_length = 40960
        max_running_requests = 6
        decode_cuda_graphs = 'full-bs1-6'
        gpu_free_mib = $gpu.FreeMiB
        stable_preserved = $true
        quick_smokes = @('health', 'models', 'chat', 'tool')
        extended_acceptance_run = $false
        armed_restart_policy = 'unless-stopped'
        publish_network = $publishNetworkReceipt
        publish_network_retained_on_rollback = $true
        backend_bridge_untouched = $true
        rollback = (Join-Path $PSScriptRoot 'Rollback-Qwen38AbliteratedV12Attested.ps1')
        journal = $journalPath
    } | ConvertTo-Json -Compress -Depth 12
}
catch {
    try {
        Write-Journal 'failed' @{
            stage = $stage
            error_type = $_.Exception.GetType().FullName
            publish_network_retained = ($null -ne $publishNetworkReceipt)
            publish_network = $publishNetworkReceipt
        }
    }
    catch {}
    if ($mutationStarted -and -not $switchSucceeded) {
        try { Restore-Stable }
        catch {
            try { Write-Journal 'automatic_rollback_failed' @{ error_type = $_.Exception.GetType().FullName } } catch {}
            throw 'Attested switch failed and exact automatic rollback also failed; inspect the sanitized journal.'
        }
    }
    throw 'Attested switch failed; stable was untouched or automatically restored. Inspect the sanitized journal.'
}
finally {
    Clear-AttestedEnvironment
    $apiKey = $null
    if ($null -ne $lock) { $lock.Dispose() }
}
