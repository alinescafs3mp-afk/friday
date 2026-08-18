Set-StrictMode -Version Latest

$script:Attested = [ordered]@{
    ProfileId = 'qwen38-27b-nvfp4-sglang:dispatcher:v12.14'
    Root = $PSScriptRoot
    ComposePath = Join-Path $PSScriptRoot 'docker-compose.attested.yml'
    PublishPath = Join-Path $PSScriptRoot 'docker-compose.publish-8001.yml'
    BuildReceiptPath = Join-Path $PSScriptRoot 'build-attestation.v1.json'
    IdentityPath = Join-Path $PSScriptRoot 'deployment-identity.v1.json'
    ModelManifestPath = Join-Path $PSScriptRoot 'qwen38-model-manifest.v1.json'
    LaunchManifestPath = Join-Path $PSScriptRoot 'launch-manifest.v1.json'
    ProxyPolicyPath = Join-Path $PSScriptRoot 'default.conf.template'
    ModelPath = 'D:\jarvis\data\models\qwen3.8-27b-nvfp4-a2genesis-bfd9b312'
    ModelVolumeName = 'jarvis-gpt-qwen38-v12-attested-model-da435c4b7556d8d5'
    CachePath = 'D:\jarvis\cache\sglang-qwen38-v12-attested'
    LockPath = 'D:\jarvis-gpt\sglang-qwen38-w4a16\switch.lock'
    StatePath = Join-Path $PSScriptRoot 'rollback-state-attested.json'
    StableEngineName = 'jarvis-gpt-sglang-qwen38-perf-graph'
    StableProxyName = 'jarvis-gpt-sglang-qwen38-perf-graph-api'
    CandidateEngineName = 'jarvis-gpt-sglang-qwen38-v12-attested'
    CandidateProxyName = 'jarvis-gpt-sglang-qwen38-v12-attested-api'
    ComposeProject = 'jarvis-gpt-qwen38-v12-attested'
    ComposeProfile = 'qwen38-v12-attested-go'
    EngineService = 'engine'
    ProxyService = 'proxy'
    StableEngineImageRef = 'lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124'
    StableEngineImageId = 'sha256:317b75ce527f3b6ee482e9437c753e98f4df6e6b17a335f8681af5d86a8a9de8'
    StableProxyImageRef = 'nginx:1.28.3-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'
    StableProxyImageId = 'sha256:dc73b49f5124cf2ee538dfbdfbd121f0b4ccdcb20fea30f3a81bd477c02e2bb5'
    CandidateEngineImageRef = 'jarvis-gpt/sglang-qwen38-v12-attested:model-da435c4b-launch-640a1ea4'
    CandidateEngineImageId = 'sha256:7f27e2885eca5041860a8c28c0bc3304b43b9fce072f298da043393866aa5887'
    CandidateProxyImageRef = 'jarvis-gpt/qwen38-v12-attested-proxy:policy-47e6b9c2'
    CandidateProxyImageId = 'sha256:2bf585895ba4ede01899f4b17db5c690dd893d77c3e1da9ac4dfb2482e22c091'
    ModelRevision = 'bfd9b31207712e0850eec9da32261e8c5ee16af7'
    ModelManifestSha256 = 'da435c4b7556d8d5feed8551024914b0da0b48bb3fe85850536a0eb3b2489333'
    LaunchManifestSha256 = '640a1ea428b2526ff6f3b3e412c18fef8e48f1fa882b3a94f9859a190678f62b'
    ProxyPolicySha256 = '47e6b9c2dadea4a1e9395b8f8305699033b52a09ecba14d82afcdf77e7d9f3ae'
    ComposeSha256 = '0797dbb8708c7454ce4a1477b644a78e2b44efcfe729374f88e6a5288469da7f'
    PublishSha256 = '9403b256555fd105f3c17395dd1049fac894e140891ef8ff9ccb86767934fcae'
    MinimumGpuReleaseMiB = 26000
    MinimumCandidateFreeMiB = 1536
}

$script:ExpectedGraphCommand = @(
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
    if ($null -eq $Value -or $Value -is [array]) {
        throw "$Label is not one object"
    }
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
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
        [string]$Volume.Labels.'com.friday.model.repository' -cne 'a2genesis/Qwen3.8-27B-NVFP4' -or
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
    $expected = '{"file_count":17,"manifest_sha256":"' + $script:Attested.ModelManifestSha256 +
        '","schema":"friday.model-volume-sealer-result.v1","status":"' + $status +
        '","total_bytes":21952105742}'
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
                '--label', 'com.friday.model.repository=a2genesis/Qwen3.8-27B-NVFP4',
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

function Assert-BindMount([object]$Container, [string]$Destination, [string]$Source, [bool]$ReadOnly) {
    $matches = @($Container.Mounts | Where-Object { [string]$_.Destination -ceq $Destination })
    if ($matches.Count -ne 1 -or [string]$matches[0].Type -cne 'bind' -or
        [string]$matches[0].Source.Replace('/', '\') -cne $Source -or
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
        [string]$receipt.engine.base_image_digest -cne $script:Attested.StableEngineImageRef -or
        [string]$receipt.engine.base_image_id -cne $script:Attested.StableEngineImageId -or
        [string]$receipt.engine.model_snapshot_manifest_sha256 -cne $script:Attested.ModelManifestSha256 -or
        [string]$receipt.engine.launch_manifest_sha256 -cne $script:Attested.LaunchManifestSha256 -or
        [string]$receipt.proxy.base_image_digest -cne $script:Attested.StableProxyImageRef -or
        [string]$receipt.proxy.base_image_id -cne $script:Attested.StableProxyImageId -or
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
        [string]$engine.Config.Labels.'com.friday.base-image-id' -cne $script:Attested.StableEngineImageId -or
        [string]$engine.Config.Labels.'com.friday.profile-id' -cne $script:Attested.ProfileId -or
        [string]$engine.Config.Labels.'com.friday.model-manifest-sha256' -cne $script:Attested.ModelManifestSha256 -or
        [string]$engine.Config.Labels.'com.friday.launch-manifest-sha256' -cne $script:Attested.LaunchManifestSha256 -or
        [string]$engine.Config.Labels.'com.friday.proxy-policy-sha256' -cne $script:Attested.ProxyPolicySha256 -or
        [string]$proxy.Config.Labels.'com.friday.base-image-id' -cne $script:Attested.StableProxyImageId -or
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
        [string]$manifest.model_repository -cne 'a2genesis/Qwen3.8-27B-NVFP4' -or
        [string]$manifest.model_revision -cne $script:Attested.ModelRevision -or
        [string]$manifest.model_quantization -cne 'W4A16_NVFP4' -or
        [string]$manifest.snapshot_directory -cne 'qwen3.8-27b-nvfp4-a2genesis-bfd9b312' -or
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
        [string]$Engine.Config.Labels.'com.jarvis-gpt.perf-mode' -cne 'graph' -or
        [string]$Engine.Config.Labels.'com.jarvis-gpt.model.revision' -cne $script:Attested.ModelRevision -or
        [string]$Proxy.Config.Labels.'com.jarvis-gpt.perf-mode' -cne 'graph' -or
        [string]$Proxy.Config.Labels.'com.jarvis-gpt.proxy.openai-key-sha256' -cne $KeyHash -or
        (Get-EnvValue $Proxy 'SGLANG_UPSTREAM') -cne 'sglang-qwen38-perf-graph') {
        throw 'Preserved stable graph identity is not exact'
    }
    Assert-ExactCommand $Engine $script:ExpectedGraphCommand 'stable graph engine'
    Assert-BindMount $Engine '/models/qwen3.8-27b-nvfp4-a2genesis-bfd9b312' $script:Attested.ModelPath $true
}

function Assert-CandidateContainers([object]$Engine, [object]$Proxy, [object]$Receipt, [string]$KeyHash) {
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
    Assert-VolumeMount $Engine '/models/qwen3.8-27b-nvfp4-a2genesis-bfd9b312' `
        $script:Attested.ModelVolumeName $true
    Assert-BindMount $Engine '/root/.cache' $script:Attested.CachePath $false
    Assert-AttestedModelVolume (Get-AttestedModelVolume) ([string]$Engine.Id)
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
            [string]::Join(',', @($Proxy.HostConfig.CapAdd | Sort-Object)) -cne 'CHOWN,DAC_OVERRIDE,SETGID,SETUID') {
            throw 'Candidate proxy identity is not exact'
        }
    }
    $engineNetworks = @($Engine.NetworkSettings.Networks.PSObject.Properties.Name)
    if ($engineNetworks.Count -ne 1 -or [string]$engineNetworks[0] -cne 'jarvis-gpt-qwen38-v12-attested-net') {
        throw 'Candidate engine network is not the exact internal sibling network'
    }
    if ($null -ne $Proxy) {
        $proxyNetworks = @($Proxy.NetworkSettings.Networks.PSObject.Properties.Name)
        if ($proxyNetworks.Count -ne 1 -or [string]$proxyNetworks[0] -cne 'jarvis-gpt-qwen38-v12-attested-net') {
            throw 'Candidate proxy network is not the exact internal sibling network'
        }
        $witnessMount = @($Proxy.Mounts | Where-Object { [string]$_.Destination -ceq '/run/friday-witness' })
        if ($witnessMount.Count -ne 1 -or [string]$witnessMount[0].Type -cne 'volume' -or [bool]$witnessMount[0].RW) {
            throw 'Candidate proxy witness mount is not exact read-only volume'
        }
    }
}

function Assert-ServerInfo([object]$Info) {
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
        [string]$Info.model_path -cne '/models/qwen3.8-27b-nvfp4-a2genesis-bfd9b312' -or
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
        [string]$Witness.engine_base_image_digest -cne $script:Attested.StableEngineImageRef -or
        [string]$Witness.engine_base_image_id -cne $script:Attested.StableEngineImageId -or
        [string]$Witness.runtime_source_revision -cne 'c4271c3fe1262fc2adbd162c33b25de5255251c5' -or
        [string]$Witness.runtime_reported_version -cne '0.0.0.dev0+qwen38.27b.g561c8f3' -or
        [string]$Witness.model_repository -cne 'a2genesis/Qwen3.8-27B-NVFP4' -or
        [string]$Witness.model_revision -cne $script:Attested.ModelRevision -or
        [string]$Witness.model_snapshot_manifest_sha256 -cne $script:Attested.ModelManifestSha256 -or
        [string]$Witness.model_quantization -cne 'W4A16_NVFP4' -or
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
