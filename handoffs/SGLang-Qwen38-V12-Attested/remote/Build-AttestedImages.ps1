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
$engineRef = 'jarvis-gpt/sglang-qwen38-v12-attested:model-da435c4b-launch-640a1ea4'
$proxyRef = 'jarvis-gpt/qwen38-v12-attested-proxy:policy-47e6b9c2'
$expectedFiles = [ordered]@{
    'Dockerfile.engine' = 'a7053e63f52639b9acb1de13f2804f776ff59c62b85be51a5f437cc277bfb457'
    'Dockerfile.proxy' = '9684c14b2f344480f585569ff15c43d74bed5aa7abbb1e27a42591f006e9f96b'
    'deployment-identity.v1.json' = '932bb2a3f53f24333258814e9861ea15481c754f2c9cedcb1b3bd6e16d2f5a62'
    'engine-witness-entrypoint.py' = '847384e58fecd8d1c89eb27e2bf230a3d4a68a09392849f7c584581fdff034ea'
    'model-volume-sealer.py' = '3de3307620168e5d78732ab3880cb1434b51967c76ab7872ded56893c2613b70'
    'qwen38-model-manifest.v1.json' = '062e59d8ed8e6ba8d44b9a1c5b4e0796ad8bf279308293ba2235081a0fced4ac'
    'launch-manifest.v1.json' = '4833c0bd4cd2751ccf84e103a775555d4e252f7fd80e274a18844f1d15d18656'
    'default.conf.template' = '47e6b9c2dadea4a1e9395b8f8305699033b52a09ecba14d82afcdf77e7d9f3ae'
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
    'com.friday.model-manifest-sha256' = 'da435c4b7556d8d5feed8551024914b0da0b48bb3fe85850536a0eb3b2489333'
    'com.friday.model-volume-schema' = 'friday.sealed-model-volume.v1'
    'com.friday.model-sealer-sha256' = '3de3307620168e5d78732ab3880cb1434b51967c76ab7872ded56893c2613b70'
    'com.friday.launch-manifest-sha256' = '640a1ea428b2526ff6f3b3e412c18fef8e48f1fa882b3a94f9859a190678f62b'
    'com.friday.proxy-policy-sha256' = '47e6b9c2dadea4a1e9395b8f8305699033b52a09ecba14d82afcdf77e7d9f3ae'
}
Assert-DerivedImage -Image $proxy -BaseImage $proxyBase -ExpectedLabels @{
    'org.opencontainers.image.base.digest' = 'sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'
    'com.friday.base-image-id' = $proxyBaseId
    'com.friday.proxy-policy-sha256' = '47e6b9c2dadea4a1e9395b8f8305699033b52a09ecba14d82afcdf77e7d9f3ae'
}

$receipt = [ordered]@{
    schema = 'friday.attested-image-build.v1'
    built_utc = [datetime]::UtcNow.ToString('o')
    engine = [ordered]@{
        image_ref = $engineRef
        image_id = [string]$engine.Id
        base_image_digest = $engineBaseDigest
        base_image_id = $engineBaseId
        model_snapshot_manifest_sha256 = 'da435c4b7556d8d5feed8551024914b0da0b48bb3fe85850536a0eb3b2489333'
        launch_manifest_sha256 = '640a1ea428b2526ff6f3b3e412c18fef8e48f1fa882b3a94f9859a190678f62b'
    }
    proxy = [ordered]@{
        image_ref = $proxyRef
        image_id = [string]$proxy.Id
        base_image_digest = $proxyBaseDigest
        base_image_id = $proxyBaseId
        proxy_policy_sha256 = '47e6b9c2dadea4a1e9395b8f8305699033b52a09ecba14d82afcdf77e7d9f3ae'
    }
    immutable_build_inputs = $expectedFiles
}
$receiptPath = Join-Path $root 'build-attestation.v1.json'
$temporaryPath = "$receiptPath.tmp"
$receiptJson = $receipt | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($temporaryPath, "$receiptJson`n", [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporaryPath -Destination $receiptPath -Force
$receipt | ConvertTo-Json -Depth 8 -Compress
