Set-StrictMode -Version Latest

$script:Attested = [ordered]@{
    ProfileId = 'qwen38-27b-nvfp4-sglang:dispatcher:v12.15'
    Root = $PSScriptRoot
    ComposePath = Join-Path $PSScriptRoot 'docker-compose.attested.yml'
    PublishPath = Join-Path $PSScriptRoot 'docker-compose.publish-8001.yml'
    BuildReceiptPath = Join-Path $PSScriptRoot 'build-attestation.v1.json'
    IdentityPath = Join-Path $PSScriptRoot 'deployment-identity.v1.json'
    ModelManifestPath = Join-Path $PSScriptRoot 'qwen38-model-manifest.v1.json'
    LaunchManifestPath = Join-Path $PSScriptRoot 'launch-manifest.v1.json'
    ProxyPolicyPath = Join-Path $PSScriptRoot 'default.conf.template'
    ModelPath = 'D:\jarvis\data\models\qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5'
    ModelVolumeName = 'jarvis-gpt-qwen38-abliterated-v12-attested-model-e5fa0d366c3bcf65'
    CachePath = 'D:\jarvis\cache\sglang-qwen38-abliterated-v12-attested'
    StableProfileId = 'qwen38-27b-nvfp4-sglang:dispatcher:v12.14'
    StableModelRevision = 'bfd9b31207712e0850eec9da32261e8c5ee16af7'
    StableModelManifestSha256 = 'da435c4b7556d8d5feed8551024914b0da0b48bb3fe85850536a0eb3b2489333'
    StableLaunchManifestSha256 = '640a1ea428b2526ff6f3b3e412c18fef8e48f1fa882b3a94f9859a190678f62b'
    StableModelMountPath = '/models/qwen3.8-27b-nvfp4-a2genesis-bfd9b312'
    StableModelVolumeName = 'jarvis-gpt-qwen38-v12-attested-model-da435c4b7556d8d5'
    StableWitnessVolumeName = 'jarvis-gpt-qwen38-v12-attested-witness'
    LockPath = 'D:\jarvis-gpt\sglang-qwen38-w4a16\switch.lock'
    StatePath = Join-Path $PSScriptRoot 'rollback-state-attested.json'
    StableEngineName = 'jarvis-gpt-sglang-qwen38-v12-attested'
    StableProxyName = 'jarvis-gpt-sglang-qwen38-v12-attested-api'
    CandidateEngineName = 'jarvis-gpt-sglang-qwen38-abliterated-v12-attested'
    CandidateProxyName = 'jarvis-gpt-sglang-qwen38-abliterated-v12-attested-api'
    ComposeProject = 'jarvis-gpt-qwen38-abliterated-v12-attested'
    ComposeProfile = 'qwen38-abliterated-v12-attested-go'
    EngineService = 'engine'
    ProxyService = 'proxy'
    AttestedNetworkName = 'jarvis-gpt-qwen38-abliterated-v12-attested-net'
    PublishNetworkName = 'jarvis-gpt-qwen38-abliterated-v12-attested-publish-net'
    PublishNetworkSchema = 'friday.attested-publish-network.v1'
    PublishNetworkOwner = 'jarvis-gpt-qwen38-abliterated-v12-attested'
    PublishNetworkRole = 'proxy-host-8001-publication'
    AttestedNetworkConfigHash = '7520b09604728ed84b2cfeb0bd6c2482c61350e01fa3754590039009e3178fb7'
    ComposeVersion = '5.3.0'
    StableEngineImageRef = 'sha256:4a38144134d84d6f78c1844314f209c48ef69c4bd8bf7da1e5c400f9abda6f26'
    StableEngineImageId = 'sha256:4a38144134d84d6f78c1844314f209c48ef69c4bd8bf7da1e5c400f9abda6f26'
    StableProxyImageRef = 'sha256:37ae13a39a5d8a0780b0b0f226065753c0d929c31956be27f7f375f79cdef750'
    StableProxyImageId = 'sha256:37ae13a39a5d8a0780b0b0f226065753c0d929c31956be27f7f375f79cdef750'
    EngineBaseImageRef = 'lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124'
    EngineBaseImageId = 'sha256:317b75ce527f3b6ee482e9437c753e98f4df6e6b17a335f8681af5d86a8a9de8'
    ProxyBaseImageRef = 'nginx:1.28.3-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'
    ProxyBaseImageId = 'sha256:dc73b49f5124cf2ee538dfbdfbd121f0b4ccdcb20fea30f3a81bd477c02e2bb5'
    CandidateEngineImageRef = 'jarvis-gpt/sglang-qwen38-abliterated-v12-attested:model-e5fa0d36-launch-ed18fc43'
    CandidateEngineImageId = 'sha256:62ae2bb57a54a1dfcc33c05cdfd200cc69705ac94ad503cd4ec00a409804acaf'
    CandidateProxyImageRef = 'jarvis-gpt/qwen38-abliterated-v12-attested-proxy:policy-d51c092c'
    CandidateProxyImageId = 'sha256:2227ed08bc4360eea50b1bba31b0f07d5652ba63344a0ab0f135aec63fb680de'
    ModelRevision = '43aa7ff5eef05ab50a3bfa6aca581085312c7a04'
    ModelManifestSha256 = 'e5fa0d366c3bcf6546f9f3d0cb418b8e2530e2701a5a1506367f88fd08d1d1a4'
    LaunchManifestSha256 = 'ed18fc43f7a865dc0d01c568f22200fb71eebdcc2cef354f859860c966f3a19a'
    ProxyPolicySha256 = 'd51c092ca2ef566f092ef9d55320e302c2d10b710d319d27a6d982aba018dcfe'
    ComposeSha256 = '58ff8c6752d2183fbbfdba4a6975ad72428d0f0c614874b347fa98445ecada57'
    PublishSha256 = '6364fcb2c8c45703387fcfe956f444b777b30c404dcaf77f563594dc6a72bc82'
    MinimumGpuReleaseMiB = 26000
    MinimumCandidateFreeMiB = 1536
}

$script:ExpectedGraphCommand = @(
    '--model-path', '/models/qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5',
    '--served-model-name', 'dispatcher',
    '--context-length', '40960',
    '--mem-fraction-static', '0.90',
    '--kv-cache-dtype', 'fp8_e4m3',
    '--max-running-requests', '6',
    '--max-total-tokens', '40960',
    '--chunked-prefill-size', '2048',
    '--mamba-ssm-dtype', 'bfloat16',
    '--max-mamba-cache-size', '6',
    '--disable-radix-cache',
    '--cuda-graph-backend-decode', 'full',
    '--cuda-graph-max-bs-decode', '6',
    '--cuda-graph-bs-decode', '1', '2', '3', '4', '5', '6',
    '--cuda-graph-backend-prefill', 'disabled',
    '--attention-backend', 'flashinfer',
    '--reasoning-parser', 'qwen3',
    '--tool-call-parser', 'qwen3_coder',
    '--mm-feature-transport', 'cpu',
    '--enable-metrics',
    '--limit-mm-data-per-request', '{"image":4,"video":0,"audio":0}',
    '--host', '0.0.0.0',
    '--port', '30000'
)

$script:ExpectedStableGraphCommand = @(
    '--model-path', '/models/qwen3.8-27b-nvfp4-a2genesis-bfd9b312',
    '--served-model-name', 'dispatcher',
    '--context-length', '40960',
    '--mem-fraction-static', '0.90',
    '--kv-cache-dtype', 'fp8_e4m3',
    '--max-running-requests', '6',
    '--max-total-tokens', '40960',
    '--chunked-prefill-size', '2048',
    '--mamba-ssm-dtype', 'bfloat16',
    '--max-mamba-cache-size', '6',
    '--disable-radix-cache',
    '--cuda-graph-backend-decode', 'full',
    '--cuda-graph-max-bs-decode', '6',
    '--cuda-graph-bs-decode', '1', '2', '3', '4', '5', '6',
    '--cuda-graph-backend-prefill', 'disabled',
    '--attention-backend', 'flashinfer',
    '--reasoning-parser', 'qwen3',
    '--tool-call-parser', 'qwen3_coder',
    '--mm-feature-transport', 'cpu',
    '--enable-metrics',
    '--limit-mm-data-per-request', '{"image":4,"video":0,"audio":0}',
    '--host', '0.0.0.0',
    '--port', '30000'
)

function Assert-ExactProperties([object]$Value, [string[]]$Expected, [string]$Label) {
    if ($null -eq $Value -or $Value -is [array] -or
        [string]$Value.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
        throw "$Label is not one object"
    }
    $actual = @(
        $Value.PSObject.Properties |
            ForEach-Object { [string]$_.Name } |
            Sort-Object
    )
    $wanted = @($Expected | Sort-Object)
    if ([string]::Join(',', $actual) -cne [string]::Join(',', $wanted)) {
        throw "$Label schema is not exact"
    }
}

function Get-ExactJson([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is absent"
    }
    try {
        $value = Get-Content -Raw -LiteralPath $Path -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        throw "$Label is not valid JSON"
    }
    if ($null -eq $value -or $value -is [array]) {
        throw "$Label is not one JSON object"
    }
    return $value
}

function Get-KeyHash([string]$Value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)) | ForEach-Object { $_.ToString('x2') })
    }
    finally {
        $sha.Dispose()
    }
}

function Get-Container([string]$Name) {
    $ids = @(& docker ps -aq --no-trunc --filter "name=^/$Name$")
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker container lookup failed'
    }
    if ($ids.Count -eq 0) {
        return $null
    }
    if ($ids.Count -ne 1 -or [string]$ids[0] -cnotmatch '^[0-9a-f]{64}$') {
        throw "Container name is ambiguous: $Name"
    }
    $raw = @(& docker inspect ([string]$ids[0]))
    if ($LASTEXITCODE -ne 0 -or $raw.Count -eq 0) {
        throw "Docker inspection failed: $Name"
    }
    $items = @($raw | Out-String | ConvertFrom-Json)
    if ($items.Count -ne 1) {
        throw "Docker inspection is ambiguous: $Name"
    }
    return $items[0]
}

function Get-Image([string]$Reference) {
    $raw = @(& docker image inspect $Reference 2>$null)
    if ($LASTEXITCODE -ne 0 -or $raw.Count -eq 0) {
        throw "Required image is absent: $Reference"
    }
    $items = @($raw | Out-String | ConvertFrom-Json)
    if ($items.Count -ne 1) {
        throw "Image inspection is ambiguous: $Reference"
    }
    return $items[0]
}

function Get-DockerNetwork([string]$Name) {
    if ($Name -cnotin @($script:Attested.AttestedNetworkName, $script:Attested.PublishNetworkName)) {
        throw 'Docker network lookup is not code-owned'
    }
    $ids = @(& docker network ls --quiet --no-trunc --filter "name=^$([regex]::Escape($Name))$")
    if ($LASTEXITCODE -ne 0) { throw 'Docker network lookup failed' }
    if ($ids.Count -eq 0) { return $null }
    if ($ids.Count -ne 1 -or [string]$ids[0] -cnotmatch '^[0-9a-f]{64}$') {
        throw "Docker network lookup is ambiguous: $Name"
    }
    $raw = @(& docker network inspect ([string]$ids[0]))
    if ($LASTEXITCODE -ne 0 -or $raw.Count -eq 0) {
        throw "Docker network inspection failed: $Name"
    }
    $items = @($raw | Out-String | ConvertFrom-Json)
    if ($items.Count -ne 1 -or [string]$items[0].Name -cne $Name -or
        [string]$items[0].Id -cne [string]$ids[0]) {
        throw "Docker network inspection is ambiguous: $Name"
    }
    return $items[0]
}

function Assert-NetworkBaseIdentity(
    [object]$Network,
    [string]$Name,
    [bool]$Internal,
    [bool]$RequireDriverIpOptions,
    [string]$Label
) {
    if ($null -eq $Network) { throw "$Label is absent" }
    foreach ($property in @(
        'EnableIPv4', 'EnableIPv6', 'Internal', 'Attachable', 'Ingress', 'ConfigOnly'
    )) {
        if ($null -eq $Network.PSObject.Properties[$property] -or
            $Network.$property -isnot [bool]) {
            throw "$Label $property projection is not an exact Boolean"
        }
    }
    if ([string]$Network.Name -cne $Name -or
        [string]$Network.Id -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$Network.Driver -cne 'bridge' -or [string]$Network.Scope -cne 'local' -or
        -not [bool]$Network.EnableIPv4 -or [bool]$Network.EnableIPv6 -or
        [bool]$Network.Internal -ne $Internal -or [bool]$Network.Attachable -or
        [bool]$Network.Ingress -or [bool]$Network.ConfigOnly) {
        throw "$Label identity, driver, or isolation changed"
    }
    Assert-ExactProperties $Network.ConfigFrom @('Network') "$Label config-from"
    if (-not [string]::IsNullOrEmpty([string]$Network.ConfigFrom.Network)) {
        throw "$Label unexpectedly inherits another network config"
    }
    if ($RequireDriverIpOptions) {
        Assert-ExactProperties $Network.Options @(
            'com.docker.network.enable_ipv4', 'com.docker.network.enable_ipv6'
        ) "$Label options"
        if ([string]$Network.Options.'com.docker.network.enable_ipv4' -cne 'true' -or
            [string]$Network.Options.'com.docker.network.enable_ipv6' -cne 'false') {
            throw "$Label IPv4/IPv6 driver options changed"
        }
    }
    else {
        Assert-ExactProperties $Network.Options @() "$Label options"
    }
}

function Assert-AttestedInternalNetworkIdentity([object]$Network) {
    Assert-NetworkBaseIdentity $Network $script:Attested.AttestedNetworkName $true $true `
        'attested internal network'
    Assert-ExactProperties $Network.Labels @(
        'com.docker.compose.config-hash', 'com.docker.compose.network',
        'com.docker.compose.project', 'com.docker.compose.version'
    ) 'attested internal network labels'
    if ([string]$Network.Labels.'com.docker.compose.config-hash' -cne
            $script:Attested.AttestedNetworkConfigHash -or
        [string]$Network.Labels.'com.docker.compose.network' -cne 'attested' -or
        [string]$Network.Labels.'com.docker.compose.project' -cne $script:Attested.ComposeProject -or
        [string]$Network.Labels.'com.docker.compose.version' -cne $script:Attested.ComposeVersion) {
        throw 'Attested internal network ownership labels changed'
    }
}

function Get-ExpectedPublishNetworkLabels {
    return [ordered]@{
        'com.friday.network.schema' = $script:Attested.PublishNetworkSchema
        'com.friday.deployment.profile-id' = $script:Attested.ProfileId
        'com.friday.network.owner' = $script:Attested.PublishNetworkOwner
        'com.friday.network.role' = $script:Attested.PublishNetworkRole
    }
}

function Assert-PublishNetworkIdentity([object]$Network) {
    Assert-NetworkBaseIdentity $Network $script:Attested.PublishNetworkName $false $false `
        'attested publish network'
    $expected = Get-ExpectedPublishNetworkLabels
    Assert-ExactProperties $Network.Labels @($expected.Keys) 'attested publish network labels'
    foreach ($entry in $expected.GetEnumerator()) {
        if ([string]$Network.Labels.($entry.Key) -cne [string]$entry.Value) {
            throw 'Attested publish network ownership labels changed'
        }
    }
}

function Get-PublishNetworkReceipt([object]$Network) {
    Assert-PublishNetworkIdentity $Network
    return [pscustomobject][ordered]@{
        id = [string]$Network.Id
        name = [string]$Network.Name
        driver = [string]$Network.Driver
        scope = [string]$Network.Scope
        internal = [bool]$Network.Internal
        attachable = [bool]$Network.Attachable
        ingress = [bool]$Network.Ingress
        config_only = [bool]$Network.ConfigOnly
        labels = [pscustomobject](Get-ExpectedPublishNetworkLabels)
    }
}

function Assert-PublishNetworkReceipt([object]$Receipt, [AllowNull()][object]$Network) {
    Assert-ExactProperties $Receipt @(
        'id', 'name', 'driver', 'scope', 'internal', 'attachable', 'ingress',
        'config_only', 'labels'
    ) 'publish network receipt'
    $expectedLabels = Get-ExpectedPublishNetworkLabels
    Assert-ExactProperties $Receipt.labels @($expectedLabels.Keys) 'publish network receipt labels'
    if ([string]$Receipt.id -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$Receipt.name -cne $script:Attested.PublishNetworkName -or
        [string]$Receipt.driver -cne 'bridge' -or [string]$Receipt.scope -cne 'local' -or
        $Receipt.internal -isnot [bool] -or [bool]$Receipt.internal -or
        $Receipt.attachable -isnot [bool] -or [bool]$Receipt.attachable -or
        $Receipt.ingress -isnot [bool] -or [bool]$Receipt.ingress -or
        $Receipt.config_only -isnot [bool] -or [bool]$Receipt.config_only) {
        throw 'Publish network receipt identity is not exact'
    }
    foreach ($entry in $expectedLabels.GetEnumerator()) {
        if ([string]$Receipt.labels.($entry.Key) -cne [string]$entry.Value) {
            throw 'Publish network receipt labels are not exact'
        }
    }
    if ($null -ne $Network) {
        Assert-PublishNetworkIdentity $Network
        if ([string]$Network.Id -cne [string]$Receipt.id) {
            throw 'Publish network object changed from the sealed receipt'
        }
    }
}

function Get-CleanupFinalPublishNetworkReceipt(
    [string]$StateSchema,
    [AllowNull()][object]$SealedReceipt,
    [object]$PublishNetwork
) {
    if ($StateSchema -cnotin @(
        'friday.attested-switch-state.v1', 'friday.attested-switch-state.v2'
    )) {
        throw 'Final cleanup publish network state schema is not allowlisted'
    }
    if ($null -eq $PublishNetwork) {
        throw 'Final cleanup durable publish network is absent'
    }
    Assert-PublishNetworkIdentity $PublishNetwork
    Assert-NetworkContainerProjection $PublishNetwork @() @() `
        'cleanup unattached permanent publish network'
    if ($StateSchema -ceq 'friday.attested-switch-state.v2') {
        if ($null -eq $SealedReceipt) {
            throw 'Final v2 cleanup publish network receipt is absent'
        }
        Assert-PublishNetworkReceipt $SealedReceipt $PublishNetwork
        return $SealedReceipt
    }
    if ($null -ne $SealedReceipt) {
        throw 'Legacy v1 cleanup unexpectedly supplied a publish network receipt'
    }
    return Get-PublishNetworkReceipt $PublishNetwork
}

function Assert-NetworkContainerProjection(
    [object]$Network,
    [AllowNull()][object[]]$AllowedContainers,
    [AllowNull()][object[]]$RequiredContainers,
    [string]$Label
) {
    $allowed = @{}
    foreach ($container in @($AllowedContainers)) {
        if ($null -eq $container -or [string]$container.Id -cnotmatch '^[0-9a-f]{64}$' -or
            [string]::IsNullOrWhiteSpace([string]$container.Name)) {
            throw "$Label allowed container identity is invalid"
        }
        $allowed[[string]$container.Id] = [string]$container.Name.TrimStart('/')
    }
    $required = @($RequiredContainers | ForEach-Object { [string]$_.Id })
    if ($null -eq $Network.Containers) {
        $actual = @()
    }
    else {
        $actual = @($Network.Containers.PSObject.Properties | ForEach-Object {
            [string]$_.Name
        })
    }
    foreach ($id in $actual) {
        if (-not $allowed.ContainsKey([string]$id)) {
            throw "$Label has a foreign container attachment"
        }
        $attachment = $Network.Containers.PSObject.Properties[[string]$id].Value
        if ([string]$attachment.Name -cne [string]$allowed[[string]$id]) {
            throw "$Label container attachment name changed"
        }
    }
    foreach ($id in $required) {
        if ([string]$id -cnotin $actual) {
            throw "$Label omits a required running container attachment"
        }
        $attachment = $Network.Containers.PSObject.Properties[[string]$id].Value
        if ([string]$attachment.IPv4Address -cnotmatch '^(?:[0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$') {
            throw "$Label running container has no IPv4 attachment"
        }
    }
}

function Assert-ContainerNetworkEndpoint(
    [object]$Container,
    [string]$NetworkName,
    [string]$NetworkId,
    [int]$GwPriority
) {
    $property = $Container.NetworkSettings.Networks.PSObject.Properties[$NetworkName]
    if ($null -eq $property) { throw "Container network endpoint is absent: $NetworkName" }
    $endpoint = $property.Value
    if ($null -eq $endpoint.PSObject.Properties['GwPriority'] -or
        [string]$endpoint.NetworkID -cne $NetworkId -or [int]$endpoint.GwPriority -ne $GwPriority) {
        throw "Container network endpoint identity or gateway priority changed: $NetworkName"
    }
    if ([bool]$Container.State.Running -and
        [string]$endpoint.IPAddress -cnotmatch '^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$') {
        throw "Running container network endpoint has no IPv4 address: $NetworkName"
    }
}

function Assert-CandidateNetworkTopology(
    [object]$Engine,
    [AllowNull()][object]$Proxy,
    [object]$AttestedNetwork,
    [object]$PublishNetwork
) {
    Assert-AttestedInternalNetworkIdentity $AttestedNetwork
    Assert-PublishNetworkIdentity $PublishNetwork
    $engineNetworks = @($Engine.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
    if ($engineNetworks.Count -ne 1 -or
        [string]$engineNetworks[0] -cne $script:Attested.AttestedNetworkName) {
        throw 'Candidate engine must remain only on the exact internal attested network'
    }
    Assert-ContainerNetworkEndpoint $Engine $script:Attested.AttestedNetworkName `
        ([string]$AttestedNetwork.Id) 0

    $attestedAllowed = @($Engine)
    $attestedRequired = @(if ([bool]$Engine.State.Running) { $Engine })
    $publishAllowed = @()
    $publishRequired = @()
    if ($null -ne $Proxy) {
        $proxyNetworks = @($Proxy.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
        $expectedProxyNetworks = @(
            $script:Attested.AttestedNetworkName,
            $script:Attested.PublishNetworkName
        ) | Sort-Object
        if ($proxyNetworks.Count -ne 2 -or
            [string]::Join(',', $proxyNetworks) -cne [string]::Join(',', $expectedProxyNetworks)) {
            throw 'Candidate proxy must use exactly the internal and publish networks'
        }
        Assert-ContainerNetworkEndpoint $Proxy $script:Attested.AttestedNetworkName `
            ([string]$AttestedNetwork.Id) 0
        Assert-ContainerNetworkEndpoint $Proxy $script:Attested.PublishNetworkName `
            ([string]$PublishNetwork.Id) 1
        $attestedAllowed += $Proxy
        $publishAllowed = @($Proxy)
        if ([bool]$Proxy.State.Running) {
            $attestedRequired += $Proxy
            $publishRequired = @($Proxy)
        }
    }
    Assert-NetworkContainerProjection $AttestedNetwork $attestedAllowed $attestedRequired `
        'attested internal network'
    Assert-NetworkContainerProjection $PublishNetwork $publishAllowed $publishRequired `
        'attested publish network'
}

function Assert-LegacyCandidateNetworkTopology(
    [object]$Engine,
    [AllowNull()][object]$Proxy,
    [object]$AttestedNetwork
) {
    Assert-AttestedInternalNetworkIdentity $AttestedNetwork
    $engineNetworks = @($Engine.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
    if ($engineNetworks.Count -ne 1 -or
        [string]$engineNetworks[0] -cne $script:Attested.AttestedNetworkName) {
        throw 'Legacy candidate engine network is not exact internal-only topology'
    }
    Assert-ContainerNetworkEndpoint $Engine $script:Attested.AttestedNetworkName `
        ([string]$AttestedNetwork.Id) 0
    $allowed = @($Engine)
    $required = @(if ([bool]$Engine.State.Running) { $Engine })
    if ($null -ne $Proxy) {
        $proxyNetworks = @($Proxy.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
        if ($proxyNetworks.Count -ne 1 -or
            [string]$proxyNetworks[0] -cne $script:Attested.AttestedNetworkName) {
            throw 'Legacy candidate proxy network is not exact internal-only topology'
        }
        Assert-ContainerNetworkEndpoint $Proxy $script:Attested.AttestedNetworkName `
            ([string]$AttestedNetwork.Id) 0
        $allowed += $Proxy
        if ([bool]$Proxy.State.Running) { $required += $Proxy }
    }
    Assert-NetworkContainerProjection $AttestedNetwork $allowed $required `
        'legacy attested internal network'
}

function Get-PublishNetworkPreflight {
    $network = Get-DockerNetwork $script:Attested.PublishNetworkName
    if ($null -eq $network) {
        return [pscustomobject]@{ Status = 'absent_provision_on_execute'; Receipt = $null }
    }
    Assert-PublishNetworkIdentity $network
    Assert-NetworkContainerProjection $network @() @() 'attested publish network preflight'
    return [pscustomobject]@{
        Status = 'verified_existing_unattached'
        Receipt = Get-PublishNetworkReceipt $network
    }
}

function Get-AttestedNetworkPreflight {
    $network = Get-DockerNetwork $script:Attested.AttestedNetworkName
    if ($null -eq $network) { return 'absent_compose_provision_on_execute' }
    Assert-AttestedInternalNetworkIdentity $network
    Assert-NetworkContainerProjection $network @() @() 'attested internal network preflight'
    return 'verified_existing_unattached'
}

function Ensure-PublishNetwork {
    $network = Get-DockerNetwork $script:Attested.PublishNetworkName
    if ($null -eq $network) {
        $labels = Get-ExpectedPublishNetworkLabels
        $arguments = @(
            'network', 'create', '--driver', 'bridge', '--ipv4=true', '--ipv6=false',
            '--attachable=false', '--internal=false'
        )
        foreach ($entry in $labels.GetEnumerator()) {
            $arguments += @('--label', "$($entry.Key)=$($entry.Value)")
        }
        $arguments += $script:Attested.PublishNetworkName
        $preference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& docker @arguments 2>&1)
            $exit = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $preference
        }
        if ($exit -ne 0 -or $output.Count -ne 1 -or
            [string]$output[0] -cnotmatch '^[0-9a-f]{64}$') {
            throw 'Exact durable publish network creation failed'
        }
        $network = Get-DockerNetwork $script:Attested.PublishNetworkName
        if ($null -eq $network -or [string]$network.Id -cne [string]$output[0]) {
            throw 'Created publish network identity is not exact'
        }
    }
    Assert-PublishNetworkIdentity $network
    Assert-NetworkContainerProjection $network @() @() 'attested publish network provisioning'
    return Get-PublishNetworkReceipt $network
}

function Get-AttestedModelVolume {
    $names = @(& docker volume ls --quiet --filter "name=^$([regex]::Escape($script:Attested.ModelVolumeName))$")
    if ($LASTEXITCODE -ne 0) { throw 'Docker volume lookup failed' }
    if ($names.Count -eq 0) { return $null }
    if ($names.Count -ne 1 -or [string]$names[0] -cne $script:Attested.ModelVolumeName) {
        throw 'Attested model volume lookup is ambiguous'
    }
    $raw = @(& docker volume inspect $script:Attested.ModelVolumeName)
    if ($LASTEXITCODE -ne 0 -or $raw.Count -eq 0) { throw 'Attested model volume inspection failed' }
    $items = @($raw | Out-String | ConvertFrom-Json)
    if ($items.Count -ne 1) { throw 'Attested model volume inspection is ambiguous' }
    return $items[0]
}

function Assert-AttestedModelVolume([object]$Volume, [string]$ExpectedAttachmentId = '') {
    if ($null -eq $Volume -or [string]$Volume.Name -cne $script:Attested.ModelVolumeName -or
        [string]$Volume.Driver -cne 'local' -or [string]$Volume.Scope -cne 'local') {
        throw 'Attested model volume identity is not exact'
    }
    Assert-ExactProperties $Volume.Labels @(
        'com.friday.model.volume-schema', 'com.friday.deployment.profile-id',
        'com.friday.model.manifest-sha256', 'com.friday.model.repository',
        'com.friday.model.revision'
    ) 'attested model volume labels'
    if ([string]$Volume.Labels.'com.friday.model.volume-schema' -cne 'friday.sealed-model-volume.v1' -or
        [string]$Volume.Labels.'com.friday.deployment.profile-id' -cne $script:Attested.ProfileId -or
        [string]$Volume.Labels.'com.friday.model.manifest-sha256' -cne $script:Attested.ModelManifestSha256 -or
        [string]$Volume.Labels.'com.friday.model.repository' -cne 'Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4' -or
        [string]$Volume.Labels.'com.friday.model.revision' -cne $script:Attested.ModelRevision) {
        throw 'Attested model volume labels changed'
    }
    $options = @(if ($null -ne $Volume.Options) { $Volume.Options.PSObject.Properties.Name })
    if ($options.Count -ne 0) { throw 'Attested model volume driver options are not empty' }
    $attached = @(& docker ps -aq --no-trunc --filter "volume=$($script:Attested.ModelVolumeName)")
    if ($LASTEXITCODE -ne 0) { throw 'Attested model volume attachment lookup failed' }
    if ([string]::IsNullOrEmpty($ExpectedAttachmentId)) {
        if ($attached.Count -ne 0) {
            throw 'Attested model volume is unexpectedly attached before candidate startup'
        }
    }
    elseif ($ExpectedAttachmentId -cnotmatch '^[0-9a-f]{64}$' -or $attached.Count -ne 1 -or
        [string]$attached[0] -cne $ExpectedAttachmentId) {
        throw 'Attested model volume attachment identity is not exact'
    }
}

function Invoke-ModelVolumeSealer(
    [object]$Receipt,
    [ValidateSet('seal', 'verify')][string]$Mode
) {
    $arguments = @(
        'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--entrypoint', 'python3'
    )
    if ($Mode -ceq 'seal') {
        $arguments += @(
            '--mount', "type=bind,source=$($script:Attested.ModelPath),target=/source-model,readonly",
            '--mount', "type=volume,source=$($script:Attested.ModelVolumeName),target=/sealed-model"
        )
    }
    else {
        $arguments += @(
            '--mount', "type=volume,source=$($script:Attested.ModelVolumeName),target=/sealed-model,readonly"
        )
    }
    $arguments += @(
        [string]$Receipt.engine.image_id,
        '/usr/local/bin/friday-model-volume-sealer',
        $Mode
    )
    $preference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& docker @arguments 2>&1)
        $exit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $preference
    }
    $status = if ($Mode -ceq 'seal') { 'sealed' } else { 'verified' }
    $expected = '{"file_count":18,"manifest_sha256":"' + $script:Attested.ModelManifestSha256 +
        '","schema":"friday.model-volume-sealer-result.v1","status":"' + $status +
        '","total_bytes":20613780167}'
    if ($exit -ne 0 -or $output.Count -ne 1 -or [string]$output[0] -cne $expected) {
        throw "Model volume $Mode did not return the exact attested result"
    }
    $result = [string]$output[0] | ConvertFrom-Json
    Assert-ExactProperties $result @(
        'file_count', 'manifest_sha256', 'schema', 'status', 'total_bytes'
    ) "model volume $Mode result"
    if ($result.file_count -isnot [int] -and $result.file_count -isnot [long]) {
        throw "Model volume $Mode file count type is invalid"
    }
    if ($result.total_bytes -isnot [int] -and $result.total_bytes -isnot [long]) {
        throw "Model volume $Mode byte count type is invalid"
    }
}

function Assert-ModelVolumePreflight([object]$Receipt) {
    $volume = Get-AttestedModelVolume
    if ($null -eq $volume) { return 'absent_provision_on_execute' }
    Assert-AttestedModelVolume $volume
    Invoke-ModelVolumeSealer $Receipt 'verify'
    Assert-AttestedModelVolume (Get-AttestedModelVolume)
    return 'verified_existing'
}

function Ensure-AttestedModelVolume([object]$Receipt) {
    $createdHere = $false
    $volume = Get-AttestedModelVolume
    try {
        if ($null -eq $volume) {
            $arguments = @(
                'volume', 'create', '--driver', 'local',
                '--label', 'com.friday.model.volume-schema=friday.sealed-model-volume.v1',
                '--label', "com.friday.deployment.profile-id=$($script:Attested.ProfileId)",
                '--label', "com.friday.model.manifest-sha256=$($script:Attested.ModelManifestSha256)",
                '--label', 'com.friday.model.repository=Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4',
                '--label', "com.friday.model.revision=$($script:Attested.ModelRevision)",
                $script:Attested.ModelVolumeName
            )
            $preference = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                $output = @(& docker @arguments 2>&1)
                $exit = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $preference
            }
            if ($exit -ne 0 -or $output.Count -ne 1 -or
                [string]$output[0] -cne $script:Attested.ModelVolumeName) {
                throw 'Exact attested model volume creation failed'
            }
            $createdHere = $true
            $volume = Get-AttestedModelVolume
            Assert-AttestedModelVolume $volume
            Invoke-ModelVolumeSealer $Receipt 'seal'
        }
        else {
            Assert-AttestedModelVolume $volume
        }
        Invoke-ModelVolumeSealer $Receipt 'verify'
        Assert-AttestedModelVolume (Get-AttestedModelVolume)
    }
    catch {
        $failure = $_
        $cleanup = 'not_applicable'
        if ($createdHere) {
            $cleanup = 'failed_closed'
            try {
                $created = Get-AttestedModelVolume
                Assert-AttestedModelVolume $created
                $preference = $ErrorActionPreference
                $ErrorActionPreference = 'Continue'
                try {
                    $removed = @(& docker volume rm $script:Attested.ModelVolumeName 2>&1)
                    $removeExit = $LASTEXITCODE
                }
                finally {
                    $ErrorActionPreference = $preference
                }
                if ($removeExit -ne 0 -or $removed.Count -ne 1 -or
                    [string]$removed[0] -cne $script:Attested.ModelVolumeName -or
                    $null -ne (Get-AttestedModelVolume)) {
                    throw 'New attested model volume cleanup did not complete exactly'
                }
                $cleanup = 'removed_exact_new_volume'
            }
            catch {
                $cleanup = 'failed_closed'
            }
        }
        $failure.Exception.Data['new_model_volume_cleanup'] = $cleanup
        throw $failure
    }
}

function Get-EnvValue([object]$Container, [string]$Name) {
    $prefix = "$Name="
    $matches = @($Container.Config.Env | Where-Object { [string]$_ -clike "$prefix*" })
    if ($matches.Count -ne 1) {
        throw "Container environment binding is not exact: $Name"
    }
    return [string]$matches[0].Substring($prefix.Length)
}

function Get-RestartSpec([object]$Container) {
    $name = [string]$Container.HostConfig.RestartPolicy.Name
    $maximum = [int]$Container.HostConfig.RestartPolicy.MaximumRetryCount
    if ($name -eq 'on-failure' -and $maximum -gt 0) {
        return "on-failure:$maximum"
    }
    if ($name -in @('no', 'always', 'unless-stopped', 'on-failure')) {
        return $name
    }
    throw 'Container restart policy is not allowlisted'
}

function Assert-ExactCommand([object]$Container, [object[]]$Expected, [string]$Label) {
    $actual = @($Container.Config.Cmd)
    if ($actual.Count -ne $Expected.Count) {
        throw "$Label command length is not exact"
    }
    for ($index = 0; $index -lt $Expected.Count; $index += 1) {
        if ([string]$actual[$index] -cne [string]$Expected[$index]) {
            throw "$Label command differs at argument $index"
        }
    }
}

function Get-ExactDockerDesktopBindSource([string]$WindowsPath) {
    if ([string]::IsNullOrEmpty($WindowsPath) -or $WindowsPath.Length -gt 240) {
        throw 'Attested Windows bind source is not bounded'
    }
    $match = [regex]::Match(
        $WindowsPath,
        '\A(?<drive>[A-Z]):\\(?<tail>[A-Za-z0-9._-]+(?:\\[A-Za-z0-9._-]+)*)\z',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $match.Success) {
        throw 'Attested Windows bind source is not canonical'
    }
    $segments = @($match.Groups['tail'].Value -csplit '\\')
    if ($segments.Count -gt 32 -or
        @($segments | Where-Object { [string]$_ -ceq '.' -or [string]$_ -ceq '..' }).Count -ne 0) {
        throw 'Attested Windows bind source has unsafe segments'
    }
    $drive = $match.Groups['drive'].Value.ToLowerInvariant()
    $tail = $match.Groups['tail'].Value.Replace('\', '/')
    return ('/run/desktop/mnt/host/{0}/{1}' -f $drive, $tail)
}

function Test-ExactAttestedBindSource([string]$Observed, [string]$WindowsPath) {
    $dockerDesktopPath = Get-ExactDockerDesktopBindSource $WindowsPath
    $dockerWindowsPath = $WindowsPath.Replace('\', '/')
    if ([string]::IsNullOrEmpty($Observed) -or $Observed.Length -gt 512) {
        return $false
    }
    return ([string]::Equals($Observed, $WindowsPath, [StringComparison]::Ordinal) -or
        [string]::Equals($Observed, $dockerWindowsPath, [StringComparison]::Ordinal) -or
        [string]::Equals($Observed, $dockerDesktopPath, [StringComparison]::Ordinal))
}

function Test-ExactAttestedProxyCapabilitySet([AllowNull()][object[]]$Observed) {
    $values = @($Observed)
    if ($values.Count -ne 4) {
        return $false
    }
    $actual = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($entry in $values) {
        if ($null -eq $entry -or $entry -isnot [string]) {
            return $false
        }
        $value = [string]$entry
        if ($value.Length -lt 1 -or $value.Length -gt 32 -or -not $actual.Add($value)) {
            return $false
        }
    }

    $composeSpelling = [string[]]@('CHOWN', 'DAC_OVERRIDE', 'SETGID', 'SETUID')
    $dockerRuntimeSpelling = [string[]]@('CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_SETGID', 'CAP_SETUID')
    $composeMatch = $true
    $dockerRuntimeMatch = $true
    for ($index = 0; $index -lt 4; $index++) {
        if (-not $actual.Contains($composeSpelling[$index])) {
            $composeMatch = $false
        }
        if (-not $actual.Contains($dockerRuntimeSpelling[$index])) {
            $dockerRuntimeMatch = $false
        }
    }
    return ($composeMatch -or $dockerRuntimeMatch)
}

function Assert-BindMount([object]$Container, [string]$Destination, [string]$Source, [bool]$ReadOnly) {
    $matches = @($Container.Mounts | Where-Object { [string]$_.Destination -ceq $Destination })
    if ($matches.Count -ne 1 -or [string]$matches[0].Type -cne 'bind' -or
        -not (Test-ExactAttestedBindSource ([string]$matches[0].Source) $Source) -or
        [bool]$matches[0].RW -eq $ReadOnly) {
        throw "Container bind mount is not exact at $Destination"
    }
}

function Assert-VolumeMount([object]$Container, [string]$Destination, [string]$Name, [bool]$ReadOnly) {
    $matches = @($Container.Mounts | Where-Object { [string]$_.Destination -ceq $Destination })
    if ($matches.Count -ne 1 -or [string]$matches[0].Type -cne 'volume' -or
        [string]$matches[0].Name -cne $Name -or [bool]$matches[0].RW -eq $ReadOnly) {
        throw "Container volume mount is not exact at $Destination"
    }
}

function Wait-Healthy([string]$Name, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $container = Get-Container $Name
        if ($null -eq $container) {
            Start-Sleep -Seconds 3
            continue
        }
        if ([bool]$container.State.OOMKilled) {
            throw "$Name was OOM-killed"
        }
        if (-not [bool]$container.State.Running) {
            throw "$Name exited before becoming healthy"
        }
        if ([string]$container.State.Health.Status -ceq 'healthy') {
            return $container
        }
        if ([string]$container.State.Health.Status -ceq 'unhealthy') {
            throw "$Name became unhealthy"
        }
        Start-Sleep -Seconds 5
    }
    throw "$Name did not become healthy within $TimeoutSeconds seconds"
}

function Assert-BuildReceipt {
    $receipt = Get-ExactJson $script:Attested.BuildReceiptPath 'build attestation'
    Assert-ExactProperties $receipt @('schema', 'built_utc', 'engine', 'proxy', 'immutable_build_inputs') 'build attestation'
    Assert-ExactProperties $receipt.engine @(
        'image_ref', 'image_id', 'base_image_digest', 'base_image_id',
        'model_snapshot_manifest_sha256', 'launch_manifest_sha256'
    ) 'engine build attestation'
    Assert-ExactProperties $receipt.proxy @(
        'image_ref', 'image_id', 'base_image_digest', 'base_image_id', 'proxy_policy_sha256'
    ) 'proxy build attestation'
    if ([string]$receipt.schema -cne 'friday.attested-image-build.v1' -or
        [string]$receipt.engine.image_id -cne $script:Attested.CandidateEngineImageId -or
        [string]$receipt.proxy.image_id -cne $script:Attested.CandidateProxyImageId -or
        [string]$receipt.engine.image_ref -cne $script:Attested.CandidateEngineImageRef -or
        [string]$receipt.proxy.image_ref -cne $script:Attested.CandidateProxyImageRef -or
        [string]$receipt.engine.base_image_digest -cne $script:Attested.EngineBaseImageRef -or
        [string]$receipt.engine.base_image_id -cne $script:Attested.EngineBaseImageId -or
        [string]$receipt.engine.model_snapshot_manifest_sha256 -cne $script:Attested.ModelManifestSha256 -or
        [string]$receipt.engine.launch_manifest_sha256 -cne $script:Attested.LaunchManifestSha256 -or
        [string]$receipt.proxy.base_image_digest -cne $script:Attested.ProxyBaseImageRef -or
        [string]$receipt.proxy.base_image_id -cne $script:Attested.ProxyBaseImageId -or
        [string]$receipt.proxy.proxy_policy_sha256 -cne $script:Attested.ProxyPolicySha256) {
        throw 'Build attestation identity is not exact'
    }
    $inputs = $receipt.immutable_build_inputs
    if ($null -eq $inputs -or $inputs -is [array]) {
        throw 'Build input digest map is invalid'
    }
    foreach ($property in $inputs.PSObject.Properties) {
        if ([string]$property.Name -notmatch '^[A-Za-z0-9._-]+$' -or
            [string]$property.Value -cnotmatch '^[0-9a-f]{64}$') {
            throw 'Build input digest binding is invalid'
        }
        $path = Join-Path $script:Attested.Root ([string]$property.Name)
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$property.Value) {
            throw "Immutable build input changed: $($property.Name)"
        }
    }
    foreach ($required in @(
        'Dockerfile.engine', 'Dockerfile.proxy', 'deployment-identity.v1.json',
        'engine-witness-entrypoint.py', 'model-volume-sealer.py', 'qwen38-model-manifest.v1.json',
        'launch-manifest.v1.json', 'default.conf.template', '.dockerignore'
    )) {
        if ($null -eq $inputs.PSObject.Properties[$required]) {
            throw "Build attestation omits immutable input: $required"
        }
    }
    if ((Get-FileHash -LiteralPath $script:Attested.ProxyPolicyPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
        $script:Attested.ProxyPolicySha256) {
        throw 'Proxy allowlist policy digest changed'
    }
    Assert-OrchestrationInputs

    $engine = Get-Image ([string]$receipt.engine.image_ref)
    $proxy = Get-Image ([string]$receipt.proxy.image_ref)
    if ([string]$engine.Id -cne [string]$receipt.engine.image_id -or
        [string]$proxy.Id -cne [string]$receipt.proxy.image_id -or
        [string]$engine.Config.Labels.'com.friday.base-image-id' -cne $script:Attested.EngineBaseImageId -or
        [string]$engine.Config.Labels.'com.friday.profile-id' -cne $script:Attested.ProfileId -or
        [string]$engine.Config.Labels.'com.friday.model-manifest-sha256' -cne $script:Attested.ModelManifestSha256 -or
        [string]$engine.Config.Labels.'com.friday.launch-manifest-sha256' -cne $script:Attested.LaunchManifestSha256 -or
        [string]$engine.Config.Labels.'com.friday.proxy-policy-sha256' -cne $script:Attested.ProxyPolicySha256 -or
        [string]$proxy.Config.Labels.'com.friday.base-image-id' -cne $script:Attested.ProxyBaseImageId -or
        [string]$proxy.Config.Labels.'com.friday.profile-id' -cne $script:Attested.ProfileId -or
        [string]$proxy.Config.Labels.'com.friday.proxy-policy-sha256' -cne $script:Attested.ProxyPolicySha256) {
        throw 'Built image identity or labels changed'
    }
    return $receipt
}

function Assert-OrchestrationInputs {
    if (-not (Test-Path -LiteralPath $script:Attested.ComposePath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $script:Attested.PublishPath -PathType Leaf) -or
        (Get-FileHash -LiteralPath $script:Attested.ComposePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            $script:Attested.ComposeSha256 -or
        (Get-FileHash -LiteralPath $script:Attested.PublishPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            $script:Attested.PublishSha256) {
        throw 'Attested Compose input digest changed'
    }
}

function Assert-ModelSnapshot {
    $manifest = Get-ExactJson $script:Attested.ModelManifestPath 'model snapshot manifest'
    Assert-ExactProperties $manifest @(
        'schema', 'model_repository', 'model_revision', 'model_quantization',
        'snapshot_directory', 'file_count', 'total_bytes', 'files'
    ) 'model snapshot manifest'
    if ([string]$manifest.schema -cne 'friday.model-snapshot-manifest.v1' -or
        [string]$manifest.model_repository -cne 'Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4' -or
        [string]$manifest.model_revision -cne $script:Attested.ModelRevision -or
        [string]$manifest.model_quantization -cne 'W4A4_NVFP4_FP8_KV' -or
        [string]$manifest.snapshot_directory -cne 'qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5' -or
        -not (Test-Path -LiteralPath $script:Attested.ModelPath -PathType Container)) {
        throw 'Model snapshot identity is not exact'
    }
    $rows = @($manifest.files)
    if ($rows.Count -ne [int]$manifest.file_count -or $rows.Count -le 0) {
        throw 'Model snapshot manifest file count is invalid'
    }
    $observedFiles = @(Get-ChildItem -LiteralPath $script:Attested.ModelPath -Recurse -File -Force)
    if ($observedFiles.Count -ne $rows.Count) {
        throw 'Model snapshot file count changed'
    }
    $expectedNames = @()
    $expectedBytes = [int64]0
    foreach ($row in $rows) {
        Assert-ExactProperties $row @('path', 'size', 'sha256') 'model manifest row'
        $relative = [string]$row.path
        if ($relative -notmatch '^[A-Za-z0-9._/-]+$' -or $relative.Contains('..') -or
            [string]$row.sha256 -cnotmatch '^[0-9a-f]{64}$' -or [int64]$row.size -lt 0) {
            throw 'Model manifest row is unsafe'
        }
        $expectedNames += $relative
        $expectedBytes += [int64]$row.size
        $path = Join-Path $script:Attested.ModelPath $relative.Replace('/', '\')
        $file = Get-Item -LiteralPath $path -Force
        if (-not $file.PSIsContainer -and -not ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -and
            [int64]$file.Length -eq [int64]$row.size -and
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ceq [string]$row.sha256) {
            continue
        }
        throw 'Model snapshot file identity changed'
    }
    $observedNames = @($observedFiles | ForEach-Object {
        $_.FullName.Substring($script:Attested.ModelPath.Length).TrimStart('\').Replace('\', '/')
    } | Sort-Object)
    if ([string]::Join(',', @($expectedNames | Sort-Object)) -cne [string]::Join(',', $observedNames) -or
        $expectedBytes -ne [int64]$manifest.total_bytes) {
        throw 'Model snapshot set or byte total changed'
    }
}

function Assert-Sidecars {
    foreach ($name in @(
        'jarvis-gpt-embeddings', 'jarvis-gpt-embeddings-api',
        'jarvis-gpt-reranker', 'jarvis-gpt-reranker-api'
    )) {
        $container = Get-Container $name
        if ($null -eq $container -or -not [bool]$container.State.Running -or
            ($null -ne $container.State.Health -and [string]$container.State.Health.Status -cne 'healthy')) {
            throw "Required sidecar is not healthy: $name"
        }
    }
}

function Assert-SolePublisher([string]$ExpectedName) {
    $publishers = @(& docker ps --filter 'publish=8001' --format '{{.Names}}')
    if ($LASTEXITCODE -ne 0 -or $publishers.Count -ne 1 -or [string]$publishers[0] -cne $ExpectedName) {
        throw "Port 8001 does not have the exact sole publisher: $ExpectedName"
    }
}

function Test-SolePublisherObservation(
    [AllowNull()][object[]]$Observed,
    [string]$ExpectedName
) {
    if ($ExpectedName -cnotin @(
        $script:Attested.StableProxyName,
        $script:Attested.CandidateProxyName
    )) {
        throw 'Expected publisher is not code-owned'
    }
    $publishers = @($Observed)
    if ($publishers.Count -eq 0) {
        return $false
    }
    if ($publishers.Count -eq 1 -and $publishers[0] -is [string] -and
        [string]$publishers[0] -ceq $ExpectedName) {
        return $true
    }
    throw "Port 8001 publisher set is unsafe while waiting for: $ExpectedName"
}

function Wait-SolePublisher([string]$ExpectedName, [int]$TimeoutSeconds = 30) {
    if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 120) {
        throw 'Publisher wait timeout is outside the code-owned bound'
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ($true) {
        $publishers = @(& docker ps --filter 'publish=8001' --format '{{.Names}}')
        if ($LASTEXITCODE -ne 0) {
            throw 'Port 8001 publisher observation failed'
        }
        if (Test-SolePublisherObservation $publishers $ExpectedName) {
            return
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "Port 8001 publisher did not appear within the bounded wait: $ExpectedName"
        }
        Start-Sleep -Milliseconds 250
    }
}

function Get-MetricValue([string]$Body, [string]$Name, [switch]$ExactDispatcherLabels) {
    $values = @()
    $expectedLabels = 'engine_type="unified",model_name="dispatcher",moe_ep_rank="0",pp_rank="0",tp_rank="0"'
    $pattern = '^' + [regex]::Escape($Name) + '(?:\{(?<labels>[^{}]*)\})?\s+(?<value>[-+0-9.eE]+)(?:\s+[0-9]+)?$'
    foreach ($raw in ($Body -split "`n")) {
        $line = $raw.Trim()
        if ($line -match $pattern) {
            if ($ExactDispatcherLabels -and [string]$Matches.labels -cne $expectedLabels) {
                throw "Metric label set is not exact: $Name"
            }
            $values += [double]::Parse([string]$Matches.value, [Globalization.CultureInfo]::InvariantCulture)
        }
    }
    if ($values.Count -ne 1 -or [double]::IsNaN($values[0]) -or [double]::IsInfinity($values[0]) -or
        $values[0] -lt 0 -or [Math]::Truncate($values[0]) -ne $values[0]) {
        throw "Metric is absent, duplicated, or invalid: $Name"
    }
    return [double]$values[0]
}

function Get-EndpointMetrics([hashtable]$Headers) {
    $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8001/metrics' -Headers $Headers -TimeoutSec 10
    $body = [string]$response.Content
    if ([Text.Encoding]::UTF8.GetByteCount($body) -gt 131072) {
        throw 'Metrics response exceeds the attested 128 KiB bound'
    }
    return [pscustomobject]@{
        Body = $body
        Running = Get-MetricValue $body 'sglang:num_running_reqs' -ExactDispatcherLabels
        Queued = Get-MetricValue $body 'sglang:num_queue_reqs' -ExactDispatcherLabels
    }
}

function Wait-EndpointIdle([hashtable]$Headers, [int]$TimeoutSeconds = 120) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $clear = 0
    while ([DateTime]::UtcNow -lt $deadline) {
        $metrics = Get-EndpointMetrics $Headers
        if ($metrics.Running -eq 0 -and $metrics.Queued -eq 0) {
            $clear += 1
            if ($clear -ge 3) { return }
        }
        else { $clear = 0 }
        Start-Sleep -Seconds 2
    }
    throw 'Dispatcher endpoint did not drain'
}

function Wait-EngineIdle([string]$Name, [int]$TimeoutSeconds = 120) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $clear = 0
    while ([DateTime]::UtcNow -lt $deadline) {
        $lines = @(& docker exec $Name curl -fsS 'http://127.0.0.1:30000/metrics')
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not inspect engine metrics while draining'
        }
        $body = [string]::Join([Environment]::NewLine, $lines)
        $running = Get-MetricValue $body 'sglang:num_running_reqs' -ExactDispatcherLabels
        $queued = Get-MetricValue $body 'sglang:num_queue_reqs' -ExactDispatcherLabels
        if ($running -eq 0 -and $queued -eq 0) {
            $clear += 1
            if ($clear -ge 2) { return }
        }
        else { $clear = 0 }
        Start-Sleep -Seconds 2
    }
    throw 'Dispatcher engine did not drain'
}

function Get-GpuMemory {
    $rows = @(& nvidia-smi '-i' '0' '--query-gpu=memory.total,memory.used,memory.free' '--format=csv,noheader,nounits')
    if ($LASTEXITCODE -ne 0 -or $rows.Count -ne 1) {
        throw 'Expected exactly one queryable GPU'
    }
    $values = @(([string]$rows[0]) -split ',')
    if ($values.Count -ne 3 -or @($values | Where-Object { [string]$_ -notmatch '^\s*[0-9]+\s*$' }).Count -ne 0) {
        throw 'GPU memory response is invalid'
    }
    return [pscustomobject]@{
        TotalMiB = [int]$values[0].Trim()
        UsedMiB = [int]$values[1].Trim()
        FreeMiB = [int]$values[2].Trim()
    }
}

function Wait-GpuRelease([int]$TimeoutSeconds = 120) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $last = 0
    while ([DateTime]::UtcNow -lt $deadline) {
        $gpu = Get-GpuMemory
        $last = $gpu.FreeMiB
        if ($last -ge $script:Attested.MinimumGpuReleaseMiB) { return $gpu }
        Start-Sleep -Seconds 3
    }
    throw "GPU release gate failed; free MiB=$last"
}

function Assert-GpuHeadroom {
    $gpu = Get-GpuMemory
    if ($gpu.FreeMiB -lt $script:Attested.MinimumCandidateFreeMiB) {
        throw 'Candidate VRAM headroom is below the attested floor'
    }
    return $gpu
}

function Assert-StableGraph([object]$Engine, [object]$Proxy, [string]$KeyHash) {
    if ($null -eq $Engine -or $null -eq $Proxy -or
        [string]$Engine.Name.TrimStart('/') -cne $script:Attested.StableEngineName -or
        [string]$Proxy.Name.TrimStart('/') -cne $script:Attested.StableProxyName -or
        [string]$Engine.Image -cne $script:Attested.StableEngineImageId -or
        [string]$Engine.Config.Image -cne $script:Attested.StableEngineImageRef -or
        [string]$Proxy.Image -cne $script:Attested.StableProxyImageId -or
        [string]$Proxy.Config.Image -cne $script:Attested.StableProxyImageRef -or
        [string]$Engine.Config.Labels.'com.friday.profile-id' -cne $script:Attested.StableProfileId -or
        [string]$Engine.Config.Labels.'com.friday.deployment.profile-id' -cne $script:Attested.StableProfileId -or
        [string]$Engine.Config.Labels.'com.friday.model-manifest-sha256' -cne $script:Attested.StableModelManifestSha256 -or
        [string]$Engine.Config.Labels.'com.friday.launch-manifest-sha256' -cne $script:Attested.StableLaunchManifestSha256 -or
        [string]$Proxy.Config.Labels.'com.friday.profile-id' -cne $script:Attested.StableProfileId -or
        [string]$Proxy.Config.Labels.'com.friday.deployment.profile-id' -cne $script:Attested.StableProfileId -or
        [string]$Proxy.Config.Labels.'com.friday.proxy.openai-key-sha256' -cne $KeyHash -or
        [string]$Proxy.Config.Labels.'com.friday.proxy-policy-sha256' -cne $script:Attested.ProxyPolicySha256 -or
        (Get-EnvValue $Proxy 'SGLANG_UPSTREAM') -cne 'engine') {
        throw 'Preserved stable v12.14 graph identity is not exact'
    }
    Assert-ExactCommand $Engine $script:ExpectedStableGraphCommand 'stable v12.14 graph engine'
    Assert-VolumeMount $Engine $script:Attested.StableModelMountPath `
        $script:Attested.StableModelVolumeName $true
    Assert-VolumeMount $Engine '/run/friday-witness' $script:Attested.StableWitnessVolumeName $false
    Assert-VolumeMount $Proxy '/run/friday-witness' $script:Attested.StableWitnessVolumeName $true
}

function Assert-NoHostPortBindings([object]$Container, [string]$Label) {
    $bindings = @(if ($null -ne $Container.HostConfig.PortBindings) {
        $Container.HostConfig.PortBindings.PSObject.Properties | ForEach-Object {
            [string]$_.Name
        }
    })
    if ($bindings.Count -ne 0) { throw "$Label unexpectedly publishes a host port" }
}

function Assert-ExactProxyPortBindingMap([object]$Bindings, [string]$Label) {
    if ($null -eq $Bindings) { throw "$Label is absent" }
    $names = @($Bindings.PSObject.Properties.Name)
    if ($names.Count -ne 1 -or [string]$names[0] -cne '8080/tcp') {
        throw "$Label key set is not exact"
    }
    $rows = @($Bindings.PSObject.Properties['8080/tcp'].Value)
    if ($rows.Count -ne 1) { throw "$Label cardinality is not exact" }
    Assert-ExactProperties $rows[0] @('HostIp', 'HostPort') "$Label row"
    if ([string]$rows[0].HostIp -cne '0.0.0.0' -or [string]$rows[0].HostPort -cne '8001') {
        throw "$Label is not exact 0.0.0.0:8001 to 8080/tcp"
    }
}

function Assert-CandidateProxyPortConfiguration([object]$Proxy) {
    $exposed = @($Proxy.Config.ExposedPorts.PSObject.Properties.Name | Sort-Object)
    if ($exposed.Count -ne 2 -or
        [string]::Join(',', $exposed) -cne '80/tcp,8080/tcp') {
        throw 'Candidate proxy exposed-port set is not exact'
    }
    Assert-ExactProxyPortBindingMap $Proxy.HostConfig.PortBindings `
        'candidate proxy configured port bindings'
}

function Assert-CandidateProxyPortPublication([object]$Proxy) {
    Assert-CandidateProxyPortConfiguration $Proxy
    Assert-ExactProxyPortBindingMap $Proxy.NetworkSettings.Ports `
        'candidate proxy effective port bindings'
    $publish = $Proxy.NetworkSettings.Networks.PSObject.Properties[
        $script:Attested.PublishNetworkName
    ]
    if ($null -eq $publish -or
        [string]$publish.Value.IPAddress -cnotmatch '^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$') {
        throw 'Candidate proxy publish endpoint has no exact IPv4 projection'
    }
}

function Assert-CandidateContainers(
    [object]$Engine,
    [AllowNull()][object]$Proxy,
    [object]$Receipt,
    [string]$KeyHash,
    [AllowNull()][object]$PublishNetworkReceipt = $null,
    [switch]$LegacyInternalOnly
) {
    if ($null -eq $Engine -or
        [string]$Engine.Name.TrimStart('/') -cne $script:Attested.CandidateEngineName -or
        [string]$Engine.Image -cne [string]$Receipt.engine.image_id -or
        [string]$Engine.Config.Image -cne [string]$Receipt.engine.image_id -or
        [string]$Engine.Config.Labels.'com.friday.deployment.profile-id' -cne $script:Attested.ProfileId -or
        [string]$Engine.Config.Labels.'com.friday.deployment.engine-image-id' -cne [string]$Receipt.engine.image_id -or
        [string]$Engine.Config.Labels.'com.friday.deployment.proxy-image-id' -cne [string]$Receipt.proxy.image_id -or
        (Get-EnvValue $Engine 'FRIDAY_EXPECTED_ENGINE_IMAGE_ID') -cne [string]$Receipt.engine.image_id -or
        (Get-EnvValue $Engine 'FRIDAY_EXPECTED_PROXY_IMAGE_ID') -cne [string]$Receipt.proxy.image_id -or
        [bool]$Engine.HostConfig.Privileged -or -not [string]::IsNullOrWhiteSpace([string]$Engine.HostConfig.PidMode) -or
        [string]::Join(',', @($Engine.HostConfig.SecurityOpt | Sort-Object)) -cne
            'label=disable,no-new-privileges:true') {
        throw 'Candidate engine identity is not exact'
    }
    Assert-ExactCommand $Engine $script:ExpectedGraphCommand 'attested candidate engine'
    Assert-VolumeMount $Engine '/models/qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5' `
        $script:Attested.ModelVolumeName $true
    Assert-BindMount $Engine '/root/.cache' $script:Attested.CachePath $false
    Assert-AttestedModelVolume (Get-AttestedModelVolume) ([string]$Engine.Id)
    Assert-NoHostPortBindings $Engine 'candidate engine'
    if ($null -ne $Proxy) {
        if ([string]$Proxy.Name.TrimStart('/') -cne $script:Attested.CandidateProxyName -or
            [string]$Proxy.Image -cne [string]$Receipt.proxy.image_id -or
            [string]$Proxy.Config.Image -cne [string]$Receipt.proxy.image_id -or
            [string]$Proxy.Config.Labels.'com.friday.deployment.profile-id' -cne $script:Attested.ProfileId -or
            [string]$Proxy.Config.Labels.'com.friday.deployment.proxy-image-id' -cne [string]$Receipt.proxy.image_id -or
            [string]$Proxy.Config.Labels.'com.friday.proxy.openai-key-sha256' -cne $KeyHash -or
            (Get-EnvValue $Proxy 'SGLANG_UPSTREAM') -cne 'engine' -or
            [bool]$Proxy.HostConfig.Privileged -or -not [string]::IsNullOrWhiteSpace([string]$Proxy.HostConfig.PidMode) -or
            @($Proxy.HostConfig.SecurityOpt).Count -ne 1 -or
            [string]$Proxy.HostConfig.SecurityOpt[0] -cne 'no-new-privileges:true' -or
            [string]::Join(',', @($Proxy.HostConfig.CapDrop | Sort-Object)) -cne 'ALL' -or
            -not (Test-ExactAttestedProxyCapabilitySet -Observed @($Proxy.HostConfig.CapAdd))) {
            throw 'Candidate proxy identity is not exact'
        }
        Assert-CandidateProxyPortConfiguration $Proxy
    }
    $attestedNetwork = Get-DockerNetwork $script:Attested.AttestedNetworkName
    if ($null -eq $attestedNetwork) { throw 'Candidate internal network is absent' }
    if ($LegacyInternalOnly) {
        Assert-LegacyCandidateNetworkTopology $Engine $Proxy $attestedNetwork
    }
    else {
        $publishNetwork = Get-DockerNetwork $script:Attested.PublishNetworkName
        if ($null -eq $publishNetwork) { throw 'Candidate publish network is absent' }
        if ($null -eq $PublishNetworkReceipt) {
            throw 'Candidate publish network receipt is absent'
        }
        Assert-PublishNetworkReceipt $PublishNetworkReceipt $publishNetwork
        Assert-CandidateNetworkTopology $Engine $Proxy $attestedNetwork $publishNetwork
    }
    if ($null -ne $Proxy) {
        $witnessMount = @($Proxy.Mounts | Where-Object { [string]$_.Destination -ceq '/run/friday-witness' })
        if ($witnessMount.Count -ne 1 -or [string]$witnessMount[0].Type -cne 'volume' -or [bool]$witnessMount[0].RW) {
            throw 'Candidate proxy witness mount is not exact read-only volume'
        }
    }
}

function Assert-ServerInfo(
    [object]$Info,
    [string]$ExpectedModelPath = '/models/qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5'
) {
    $integerBindings = [ordered]@{
        context_length = 40960
        max_running_requests = 6
        max_total_tokens = 40960
        max_total_num_tokens = 40960
        chunked_prefill_size = 2048
        max_mamba_cache_size = 6
        cuda_graph_max_bs_decode = 6
    }
    foreach ($entry in $integerBindings.GetEnumerator()) {
        if ([int64]$Info.($entry.Key) -ne [int64]$entry.Value) {
            throw "Server-info binding changed: $($entry.Key)"
        }
    }
    if ([string]$Info.status -cne 'ready' -or
        [string]$Info.version -cne '0.0.0.dev0+qwen38.27b.g561c8f3' -or
        [string]$Info.model_path -cne $ExpectedModelPath -or
        [string]$Info.served_model_name -cne 'dispatcher' -or
        [string]$Info.weight_version -cne 'default' -or
        [double]$Info.mem_fraction_static -ne 0.90 -or
        [string]$Info.kv_cache_dtype -cne 'fp8_e4m3' -or
        [string]$Info.mamba_ssm_dtype -cne 'bfloat16' -or
        -not [bool]$Info.disable_radix_cache -or [bool]$Info.disable_cuda_graph -or
        [string]$Info.cuda_graph_backend_decode -cne 'full' -or
        [string]$Info.cuda_graph_backend_prefill -cne 'disabled' -or
        [string]$Info.attention_backend -cne 'flashinfer' -or
        [string]$Info.reasoning_parser -cne 'qwen3' -or
        [string]$Info.tool_call_parser -cne 'qwen3_coder' -or
        [string]$Info.mm_feature_transport -cne 'cpu' -or
        -not [bool]$Info.enable_metrics -or
        -not [string]::IsNullOrWhiteSpace([string]$Info.speculative_algorithm) -or
        -not [string]::IsNullOrWhiteSpace([string]$Info.speculative_draft_model_path) -or
        $null -ne $Info.speculative_num_steps -or
        [int64]$Info.random_seed -lt 1 -or [int64]$Info.random_seed -gt 1073741823) {
        throw 'Effective SGLang server-info projection is not exact'
    }
    $batches = @($Info.cuda_graph_bs_decode | ForEach-Object { [int]$_ })
    if ([string]::Join(',', $batches) -cne '1,2,3,4,5,6') {
        throw 'Decode CUDA graph batches are not exact 1..6'
    }
    $limits = $Info.limit_mm_data_per_request
    if ([int]$limits.image -ne 4 -or [int]$limits.video -ne 0 -or [int]$limits.audio -ne 0) {
        throw 'Multimodal request limits changed'
    }
    $states = @($Info.internal_states | Where-Object { $null -ne $_ })
    if ($states.Count -ne 1 -or
        [int]$states[0].effective_max_running_requests_per_dp -ne 6 -or
        [int64]$states[0].memory_usage.token_capacity -ne 40960) {
        throw 'Effective scheduler capacity is not exact 40960/6'
    }
    $decode = $Info.cuda_graph_config.decode
    $prefill = $Info.cuda_graph_config.prefill
    if ([string]$decode.backend -cne 'full' -or [int]$decode.max_bs -ne 6 -or
        [string]$prefill.backend -cne 'disabled' -or
        [string]::Join(',', @($decode.bs | ForEach-Object { [int]$_ })) -cne '1,2,3,4,5,6') {
        throw 'Canonical CUDA graph configuration changed'
    }
}

function Assert-StableServerInfo([object]$Info) {
    Assert-ServerInfo $Info $script:Attested.StableModelMountPath
}

function Assert-StableDeploymentWitness([object]$Witness, [object]$Info) {
    $keys = @(
        'schema', 'profile_id', 'engine_start_nonce', 'engine_random_seed',
        'engine_image_id', 'engine_base_image_digest', 'engine_base_image_id',
        'runtime_source_revision', 'runtime_reported_version', 'model_repository',
        'model_revision', 'model_snapshot_manifest_sha256', 'model_quantization',
        'served_model_alias', 'launch_manifest_sha256', 'proxy_image_id',
        'proxy_policy_sha256'
    )
    Assert-ExactProperties $Witness $keys 'stable deployment witness'
    if ([string]$Witness.schema -cne 'friday.sglang-deployment-witness.v1' -or
        [string]$Witness.profile_id -cne $script:Attested.StableProfileId -or
        [string]$Witness.engine_start_nonce -cnotmatch '^[0-9a-f]{64}$' -or
        [int64]$Witness.engine_random_seed -ne [int64]$Info.random_seed -or
        [string]$Witness.engine_image_id -cne $script:Attested.StableEngineImageId -or
        [string]$Witness.engine_base_image_digest -cne $script:Attested.EngineBaseImageRef -or
        [string]$Witness.engine_base_image_id -cne $script:Attested.EngineBaseImageId -or
        [string]$Witness.runtime_source_revision -cne 'c4271c3fe1262fc2adbd162c33b25de5255251c5' -or
        [string]$Witness.runtime_reported_version -cne '0.0.0.dev0+qwen38.27b.g561c8f3' -or
        [string]$Witness.model_repository -cne 'a2genesis/Qwen3.8-27B-NVFP4' -or
        [string]$Witness.model_revision -cne $script:Attested.StableModelRevision -or
        [string]$Witness.model_snapshot_manifest_sha256 -cne $script:Attested.StableModelManifestSha256 -or
        [string]$Witness.model_quantization -cne 'W4A16_NVFP4' -or
        [string]$Witness.served_model_alias -cne 'dispatcher' -or
        [string]$Witness.launch_manifest_sha256 -cne $script:Attested.StableLaunchManifestSha256 -or
        [string]$Witness.proxy_image_id -cne $script:Attested.StableProxyImageId -or
        [string]$Witness.proxy_policy_sha256 -cne $script:Attested.ProxyPolicySha256) {
        throw 'Stable deployment witness is not exact v12.14 identity'
    }
}

function Assert-DeploymentWitness([object]$Witness, [object]$Info, [object]$Receipt) {
    $keys = @(
        'schema', 'profile_id', 'engine_start_nonce', 'engine_random_seed',
        'engine_image_id', 'engine_base_image_digest', 'engine_base_image_id',
        'runtime_source_revision', 'runtime_reported_version', 'model_repository',
        'model_revision', 'model_snapshot_manifest_sha256', 'model_quantization',
        'served_model_alias', 'launch_manifest_sha256', 'proxy_image_id',
        'proxy_policy_sha256'
    )
    Assert-ExactProperties $Witness $keys 'deployment witness'
    if ([string]$Witness.schema -cne 'friday.sglang-deployment-witness.v1' -or
        [string]$Witness.profile_id -cne $script:Attested.ProfileId -or
        [string]$Witness.engine_start_nonce -cnotmatch '^[0-9a-f]{64}$' -or
        [int64]$Witness.engine_random_seed -ne [int64]$Info.random_seed -or
        [string]$Witness.engine_image_id -cne [string]$Receipt.engine.image_id -or
        [string]$Witness.engine_base_image_digest -cne $script:Attested.EngineBaseImageRef -or
        [string]$Witness.engine_base_image_id -cne $script:Attested.EngineBaseImageId -or
        [string]$Witness.runtime_source_revision -cne 'c4271c3fe1262fc2adbd162c33b25de5255251c5' -or
        [string]$Witness.runtime_reported_version -cne '0.0.0.dev0+qwen38.27b.g561c8f3' -or
        [string]$Witness.model_repository -cne 'Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4' -or
        [string]$Witness.model_revision -cne $script:Attested.ModelRevision -or
        [string]$Witness.model_snapshot_manifest_sha256 -cne $script:Attested.ModelManifestSha256 -or
        [string]$Witness.model_quantization -cne 'W4A4_NVFP4_FP8_KV' -or
        [string]$Witness.served_model_alias -cne 'dispatcher' -or
        [string]$Witness.launch_manifest_sha256 -cne $script:Attested.LaunchManifestSha256 -or
        [string]$Witness.proxy_image_id -cne [string]$Receipt.proxy.image_id -or
        [string]$Witness.proxy_policy_sha256 -cne $script:Attested.ProxyPolicySha256) {
        throw 'Deployment witness does not bind the exact process graph'
    }
}

function Assert-FatalFree([object]$Engine) {
    $since = [DateTime]::Parse([string]$Engine.State.StartedAt).ToUniversalTime().AddSeconds(-1).ToString('o')
    $preference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& docker logs --since $since $script:Attested.CandidateEngineName 2>&1)
        $exit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $preference
    }
    if ($exit -ne 0) { throw 'Could not inspect candidate logs' }
    $body = [string]::Join([Environment]::NewLine, $lines)
    foreach ($fatal in @(
        'Scheduler hit an exception', 'No live scheduler processes',
        'Loaded weights leave no GPU memory', 'CUDA out of memory',
        'torch.OutOfMemoryError', 'CUDA error: invalid resource handle'
    )) {
        if ($body.Contains($fatal)) { throw 'Candidate logs contain a fatal runtime signature' }
    }
}

function Invoke-Compose([object]$Receipt, [string[]]$Tail, [switch]$Publish8001) {
    Assert-OrchestrationInputs
    $files = @('-f', $script:Attested.ComposePath)
    if ($Publish8001) { $files += @('-f', $script:Attested.PublishPath) }
    $arguments = @(
        'compose', '-p', $script:Attested.ComposeProject
    ) + $files + @('--profile', $script:Attested.ComposeProfile) + $Tail
    $env:FRIDAY_ENGINE_IMAGE_ID = [string]$Receipt.engine.image_id
    $env:FRIDAY_PROXY_IMAGE_ID = [string]$Receipt.proxy.image_id
    $preference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& docker @arguments 2>&1)
        $exit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $preference
    }
    if ($exit -ne 0) {
        throw 'docker compose operation failed'
    }
    return $output
}

function Clear-AttestedEnvironment {
    foreach ($name in @(
        'FRIDAY_ENGINE_IMAGE_REF', 'FRIDAY_ENGINE_IMAGE_ID', 'FRIDAY_PROXY_IMAGE_REF',
        'FRIDAY_PROXY_IMAGE_ID', 'JARVIS_LLM_API_KEY', 'JARVIS_LLM_API_KEY_SHA256',
        'JARVIS_QWEN38_ATTESTED_CACHE_HOST_PATH',
        'JARVIS_OPENAI_BIND_ADDRESS'
    )) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
}

function Assert-CandidateCleanupState([object]$State, [object]$Receipt) {
    $schema = [string]$State.schema
    $properties = @(
        'schema', 'profile_id', 'stable_engine_id', 'stable_proxy_id',
        'stable_engine_image_id', 'stable_proxy_image_id', 'stable_engine_restart',
        'stable_proxy_restart', 'candidate_engine_id', 'candidate_proxy_id',
        'candidate_engine_image_id', 'candidate_proxy_image_id', 'key_sha256',
        'written_at_utc'
    )
    if ($schema -ceq 'friday.attested-switch-state.v2') { $properties += 'publish_network' }
    elseif ($schema -cne 'friday.attested-switch-state.v1') {
        throw 'Candidate cleanup state schema is not allowlisted'
    }
    Assert-ExactProperties $State $properties 'candidate cleanup state'
    if ([string]$State.profile_id -cne $script:Attested.ProfileId -or
        [string]$State.stable_engine_id -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$State.stable_proxy_id -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$State.stable_engine_image_id -cne $script:Attested.StableEngineImageId -or
        [string]$State.stable_proxy_image_id -cne $script:Attested.StableProxyImageId -or
        [string]$State.candidate_engine_image_id -cne [string]$Receipt.engine.image_id -or
        [string]$State.candidate_proxy_image_id -cne [string]$Receipt.proxy.image_id -or
        [string]$State.key_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]::IsNullOrWhiteSpace([string]$State.written_at_utc)) {
        throw 'Candidate cleanup state immutable identity is invalid'
    }
    foreach ($field in @('candidate_engine_id', 'candidate_proxy_id')) {
        $value = $State.$field
        if ($null -ne $value -and [string]$value -cnotmatch '^[0-9a-f]{64}$') {
            throw "Candidate cleanup state contains an invalid $field"
        }
    }
    if ($null -eq $State.candidate_engine_id -and $null -eq $State.candidate_proxy_id) {
        throw 'Candidate cleanup state binds no candidate container'
    }
    foreach ($policy in @([string]$State.stable_engine_restart, [string]$State.stable_proxy_restart)) {
        if ($policy -cnotmatch '^(?:no|always|unless-stopped|on-failure(?::[1-9][0-9]*)?)$') {
            throw 'Candidate cleanup restart policy is not allowlisted'
        }
    }
    if ($schema -ceq 'friday.attested-switch-state.v2') {
        Assert-PublishNetworkReceipt $State.publish_network $null
    }
    return $schema
}

function Remove-ExactStoppedContainer([object]$Container, [string]$ExpectedId, [string]$Label) {
    if ($null -eq $Container -or [string]$Container.Id -cne $ExpectedId -or
        [bool]$Container.State.Running -or (Get-RestartSpec $Container) -cne 'no') {
        throw "$Label is not the exact disarmed stopped container"
    }
    $name = [string]$Container.Name.TrimStart('/')
    $preference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& docker container rm $ExpectedId 2>&1)
        $exit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $preference
    }
    if ($exit -ne 0 -or $output.Count -ne 1 -or [string]$output[0] -cne $ExpectedId -or
        $null -ne (Get-Container $name)) {
        throw "$Label exact removal did not complete"
    }
}

function Write-AtomicJson([object]$Value, [string]$Path) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    $json = $Value | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText($temporary, "$json`n", [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Set-RestartPolicy([string]$ContainerId, [string]$Policy) {
    & docker update "--restart=$Policy" $ContainerId | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not update exact container restart policy' }
}

function Stop-ExactContainer([object]$Container, [int]$TimeoutSeconds) {
    Set-RestartPolicy ([string]$Container.Id) 'no'
    if ([bool]$Container.State.Running) {
        & docker stop --time $TimeoutSeconds ([string]$Container.Id) | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop exact container identity' }
    }
}
