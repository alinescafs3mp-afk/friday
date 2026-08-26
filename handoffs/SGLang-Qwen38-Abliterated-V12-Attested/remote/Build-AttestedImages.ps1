[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$BundleRoot = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$engineBaseDigest = 'lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124'
$engineBaseId = 'sha256:317b75ce527f3b6ee482e9437c753e98f4df6e6b17a335f8681af5d86a8a9de8'
$proxyBaseDigest = 'nginx:1.28.3-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'
$proxyBaseId = 'sha256:dc73b49f5124cf2ee538dfbdfbd121f0b4ccdcb20fea30f3a81bd477c02e2bb5'
$engineRef = 'jarvis-gpt/sglang-qwen38-abliterated-v12-attested:model-e5fa0d36-launch-ed18fc43'
$proxyRef = 'jarvis-gpt/qwen38-abliterated-v12-attested-proxy:policy-d51c092c'
$expectedFiles = [ordered]@{
    'Dockerfile.engine' = '11820de04301cff4fa47a2409af29aa84ac294f8984c1921b0d4c1f2c77c9c3c'
    'Dockerfile.proxy' = '9118707bc1f6dba63093455c1d268fcd9d4abe78ff6ea07760f14875ef900c86'
    'deployment-identity.v1.json' = '06b494dd72ff0d8f63ce4946b69f52ae1aaef5993d5975975dc088077766d52a'
    'engine-witness-entrypoint.py' = '847384e58fecd8d1c89eb27e2bf230a3d4a68a09392849f7c584581fdff034ea'
    'model-volume-sealer.py' = '546a88c98bbbd882fe20bb5dfc3671f38ee07f379f2e5baeea097b6e9985da63'
    'qwen38-model-manifest.v1.json' = 'fa28bd82e8480a492d21c02b90487dceb2bc4a41be5c718eaf66c9cabe486f01'
    'launch-manifest.v1.json' = '9f0ab1c1f0a9d85d13de1e94b25134b5971b9c2f767892fed45ec4d57b6cb1fa'
    'default.conf.template' = 'd51c092ca2ef566f092ef9d55320e302c2d10b710d319d27a6d982aba018dcfe'
    '.dockerignore' = 'd2de255f68e551d97eb5ed7424d85904c087b36a5a347a3c65624b185fb21ad3'
}

$root = (Get-Item -LiteralPath $BundleRoot -Force).FullName
foreach ($entry in $expectedFiles.GetEnumerator()) {
    $path = Join-Path -Path $root -ChildPath $entry.Key
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing immutable build input: $($entry.Key)"
    }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -cne $entry.Value) {
        throw "Immutable build input digest mismatch: $($entry.Key)"
    }
}

function Get-Image([string]$Reference) {
    $raw = @(docker image inspect $Reference 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "Required local image is missing: $Reference"
    }
    return @($raw | Out-String | ConvertFrom-Json)[0]
}

function Assert-BaseImage([string]$Reference, [string]$ExpectedId) {
    $image = Get-Image -Reference $Reference
    if ([string]$image.Id -cne $ExpectedId) {
        throw "Base image ID mismatch: $Reference"
    }
    if (@($image.RepoDigests) -notcontains $Reference.Replace('nginx:1.28.3-alpine@', 'nginx@')) {
        if ($Reference -ne $engineBaseDigest -or @($image.RepoDigests) -notcontains $Reference) {
            throw "Base image repo digest mismatch: $Reference"
        }
    }
    return $image
}

function Assert-DerivedImage(
    [object]$Image,
    [object]$BaseImage,
    [hashtable]$ExpectedLabels
) {
    if ([string]$Image.Id -notmatch '^sha256:[0-9a-f]{64}$') {
        throw 'Derived image has an invalid ID.'
    }
    $baseLayers = @($BaseImage.RootFS.Layers)
    $derivedLayers = @($Image.RootFS.Layers)
    if ($derivedLayers.Count -lt $baseLayers.Count) {
        throw 'Derived image lost base filesystem layers.'
    }
    for ($index = 0; $index -lt $baseLayers.Count; $index++) {
        if ([string]$derivedLayers[$index] -cne [string]$baseLayers[$index]) {
            throw 'Derived image base filesystem ancestry mismatch.'
        }
    }
    foreach ($entry in $ExpectedLabels.GetEnumerator()) {
        if ([string]$Image.Config.Labels.($entry.Key) -cne [string]$entry.Value) {
            throw "Derived image label mismatch: $($entry.Key)"
        }
    }
}

$engineBase = Assert-BaseImage -Reference $engineBaseDigest -ExpectedId $engineBaseId
$proxyBase = Assert-BaseImage -Reference $proxyBaseDigest -ExpectedId $proxyBaseId

$env:DOCKER_BUILDKIT = '1'
& docker build --pull=false --no-cache --file (Join-Path $root 'Dockerfile.engine') --tag $engineRef $root
if ($LASTEXITCODE -ne 0) { throw 'Engine image build failed.' }
& docker build --pull=false --no-cache --file (Join-Path $root 'Dockerfile.proxy') --tag $proxyRef $root
if ($LASTEXITCODE -ne 0) { throw 'Proxy image build failed.' }

$engine = Get-Image -Reference $engineRef
$proxy = Get-Image -Reference $proxyRef
Assert-DerivedImage -Image $engine -BaseImage $engineBase -ExpectedLabels @{
    'org.opencontainers.image.base.digest' = 'sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124'
    'com.friday.base-image-id' = $engineBaseId
    'com.friday.model-manifest-sha256' = 'e5fa0d366c3bcf6546f9f3d0cb418b8e2530e2701a5a1506367f88fd08d1d1a4'
    'com.friday.model-volume-schema' = 'friday.sealed-model-volume.v1'
    'com.friday.model-sealer-sha256' = '546a88c98bbbd882fe20bb5dfc3671f38ee07f379f2e5baeea097b6e9985da63'
    'com.friday.launch-manifest-sha256' = 'ed18fc43f7a865dc0d01c568f22200fb71eebdcc2cef354f859860c966f3a19a'
    'com.friday.proxy-policy-sha256' = 'd51c092ca2ef566f092ef9d55320e302c2d10b710d319d27a6d982aba018dcfe'
}
Assert-DerivedImage -Image $proxy -BaseImage $proxyBase -ExpectedLabels @{
    'org.opencontainers.image.base.digest' = 'sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'
    'com.friday.base-image-id' = $proxyBaseId
    'com.friday.proxy-policy-sha256' = 'd51c092ca2ef566f092ef9d55320e302c2d10b710d319d27a6d982aba018dcfe'
}

$receipt = [ordered]@{
    schema = 'friday.attested-image-build.v1'
    built_utc = [datetime]::UtcNow.ToString('o')
    engine = [ordered]@{
        image_ref = $engineRef
        image_id = [string]$engine.Id
        base_image_digest = $engineBaseDigest
        base_image_id = $engineBaseId
        model_snapshot_manifest_sha256 = 'e5fa0d366c3bcf6546f9f3d0cb418b8e2530e2701a5a1506367f88fd08d1d1a4'
        launch_manifest_sha256 = 'ed18fc43f7a865dc0d01c568f22200fb71eebdcc2cef354f859860c966f3a19a'
    }
    proxy = [ordered]@{
        image_ref = $proxyRef
        image_id = [string]$proxy.Id
        base_image_digest = $proxyBaseDigest
        base_image_id = $proxyBaseId
        proxy_policy_sha256 = 'd51c092ca2ef566f092ef9d55320e302c2d10b710d319d27a6d982aba018dcfe'
    }
    immutable_build_inputs = $expectedFiles
}
$receiptPath = Join-Path $root 'build-attestation.v1.json'
$temporaryPath = "$receiptPath.tmp"
$receiptJson = $receipt | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($temporaryPath, "$receiptJson`n", [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporaryPath -Destination $receiptPath -Force
$receipt | ConvertTo-Json -Depth 8 -Compress
