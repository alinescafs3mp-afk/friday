[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Plan', 'Convert', 'Verify')]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ArtifactDirectory,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CalibrationFile,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CalibrationManifest,

    [Parameter()]
    [string]$OutputManifest,

    [Parameter()]
    [string]$AcceptedOutputManifest,

    [Parameter()]
    [string]$AcceptedConverterManifest,

    [Parameter()]
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$preferredImage = 'nvcr.io/nvidia/tensorrt-llm/release@sha256:7202108ab373557e0562f78ef3c0f65bdc70e18cc0b040c8d6805a5cde897a0d'
$alternativeBaseImage = 'lmsysorg/sglang@sha256:7a038aa31356fdd1a5b591fc756397bc2e9eb5ac91442c407f55cd2ae8bee738'
$alternativeBaseImageId = 'sha256:7a038aa31356fdd1a5b591fc756397bc2e9eb5ac91442c407f55cd2ae8bee738'
$alternativeImageId = 'sha256:b801dc95ca304701242aeeaaeaf64332d67134ba8e56c8c0e74ab2dc77569c7a'
$alternativeDockerfileSha256 = '89edbaa53184e725f8d1ff1071e1ab42663df64bd8e7a605b574e38ec12f1278'
$alternativeContextTreeSha256 = '6f2c066d818303396430a0667997a799a1903bf93c0faa32fadd171fcd43d220'
$sourceVolume = 'friday-secondary-source-gptoss20b'
$outputVolume = 'friday-secondary-modelopt-conversion-output'
$sourceRevision = '6cee5e81ee83917806bbde320786a8fb61efebee'
$digestPattern = '\A[0-9a-f]{64}\z'
$imageIdPattern = '\Asha256:[0-9a-f]{64}\z'

$artifactHashes = [ordered]@{
    'accelerate-1.12.0-py3-none-any.whl' = '3e2091cd341423207e2f084a6654b1efcd250dc326f2a37d6dde446e07cabb11'
    'cast_mxfp4_to_nvfp4.py' = 'cd4a14baf6e977581e016b8ceec3102b8304523b11f289692ec1826eb01c4018'
    'example_utils.py' = '981c036c2c6ec0dbac4f1fb8cce33493d2fcc958dc248054e8863d4ede4b8549'
    'hf_ptq.py' = '4606bcb6a9ace89a9c6c29a95bd9903be56e93a1c859e8ffbc16323d40f670d1'
    'nvidia_modelopt-0.45.0-py3-none-any.whl' = '04e1d787898e44e7281022f4772ee57bf59d1224cbcdd10d9487c2a110687a30'
    'transformers-5.9.0-py3-none-any.whl' = '1d19509bcff7028ebc6b277d71caa712e8353778463d38764237d14b42b52788'
}
$packageVersions = [ordered]@{
    'accelerate' = '1.12.0'
    'nvidia-modelopt' = '0.45.0'
    'transformers' = '5.9.0'
}
$wheelhouseHashes = [ordered]@{
    'accelerate-1.12.0-py3-none-any.whl' = '3e2091cd341423207e2f084a6654b1efcd250dc326f2a37d6dde446e07cabb11'
    'cryptography-50.0.0-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl' = '06a32a980526a6ab9a4b9bf8f7385800791e2bb960903cb6b530e4817509a3b7'
    'nvidia_modelopt-0.45.0-py3-none-any.whl' = '04e1d787898e44e7281022f4772ee57bf59d1224cbcdd10d9487c2a110687a30'
    'pillow-11.3.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl' = '67172f2944ebba3d4a7b54f2e95c786a3a50c21b88456329314caaa28cda70f6'
    'pyjwt-2.13.0-py3-none-any.whl' = '66adcc2aff09b3f1bbd95fc1e1577df8ac8723c978552fd43304c8a290ac5728'
    'pyparsing-3.3.2-py3-none-any.whl' = '850ba148bd908d7e2411587e247a1e4f0327839c40e2e5e6d05a007ecc69911d'
    'transformers-5.9.0-py3-none-any.whl' = '1d19509bcff7028ebc6b277d71caa712e8353778463d38764237d14b42b52788'
}

function Assert-ExactPropertyNames($Value, [string[]]$Expected, [string]$Name) {
    if ($null -eq $Value) {
        throw "$Name is absent."
    }
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $wanted = @($Expected | Sort-Object)
    if ($actual.Count -ne $wanted.Count -or @(Compare-Object $actual $wanted).Count -ne 0) {
        throw "$Name has an unexpected property set."
    }
}

function Assert-Sha256([string]$Value, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -cnotmatch $digestPattern) {
        throw "$Name is not an exact lowercase SHA-256."
    }
}

function Resolve-RegularFile([string]$Path, [string]$Name) {
    if ($Path.IndexOf(',') -ge 0 -or $Path.IndexOf("`r") -ge 0 -or $Path.IndexOf("`n") -ge 0) {
        throw "$Name contains a character unsafe for a Docker bind mount."
    }
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Name must be one regular, non-reparse file."
    }
    return $item.FullName
}

function Resolve-RegularDirectory([string]$Path, [string]$Name) {
    if ($Path.IndexOf(',') -ge 0 -or $Path.IndexOf("`r") -ge 0 -or $Path.IndexOf("`n") -ge 0) {
        throw "$Name contains a character unsafe for a Docker bind mount."
    }
    $item = Get-Item -LiteralPath $Path
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Name must be one regular, non-reparse directory."
    }
    return $item.FullName
}

function Assert-ArtifactSet([string]$Directory) {
    $items = @(Get-ChildItem -LiteralPath $Directory -Force)
    $expectedNames = @($artifactHashes.Keys | Sort-Object)
    $actualNames = @($items.Name | Sort-Object)
    if ($items.Count -ne $expectedNames.Count -or @(Compare-Object $actualNames $expectedNames).Count -ne 0) {
        throw 'ArtifactDirectory differs from the exact six-file closure.'
    }
    foreach ($item in $items) {
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'ArtifactDirectory contains a non-regular or reparse entry.'
        }
        $actualHash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -cne [string]$artifactHashes[$item.Name]) {
            throw "Artifact hash mismatch: $($item.Name)."
        }
    }
}

function Read-AcceptedConverterManifest([string]$Path) {
    $resolved = Resolve-RegularFile $Path 'AcceptedConverterManifest'
    if ((Get-Item -LiteralPath $resolved).Length -gt 1048576) {
        throw 'AcceptedConverterManifest exceeds 1 MiB.'
    }
    try {
        $manifest = Get-Content -LiteralPath $resolved -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw 'AcceptedConverterManifest is unreadable JSON.'
    }
    Assert-ExactPropertyNames $manifest @(
        'schema', 'status', 'image_id', 'platform', 'base_image', 'base_image_id',
        'dockerfile_sha256', 'context_archive', 'context_tree_sha256', 'core_artifacts',
        'wheelhouse', 'removed_distributions', 'packages', 'package_freeze',
        'pip_check', 'preflight_attestation', 'build_network', 'note'
    ) 'AcceptedConverterManifest'
    if ([string]$manifest.schema -cne 'friday.secondary-modelopt-converter-image.v1' -or
        [string]$manifest.status -cne 'accepted' -or
        [string]$manifest.platform -cne 'linux/amd64' -or
        [string]$manifest.base_image -cne $alternativeBaseImage -or
        [string]$manifest.image_id -cne $alternativeImageId -or
        [string]$manifest.base_image_id -cne $alternativeBaseImageId -or
        [string]$manifest.dockerfile_sha256 -cne $alternativeDockerfileSha256 -or
        [string]$manifest.context_tree_sha256 -cne $alternativeContextTreeSha256 -or
        [string]$manifest.build_network -cne 'none') {
        throw 'AcceptedConverterManifest identity is not exact and accepted.'
    }
    Assert-ExactPropertyNames $manifest.context_archive @('name', 'sha256') 'context archive'
    if ([string]$manifest.context_archive.name -cne 'friday-secondary-converter-context-v2.tgz' -or
        [string]$manifest.context_archive.sha256 -cne 'd1b24adcccf52f20aee7c07a950783b01f4d62ad488d668e7713fedb9ec8dabe') {
        throw 'Accepted converter context archive identity differs.'
    }
    Assert-ExactPropertyNames $manifest.core_artifacts @($artifactHashes.Keys) 'converter core artifacts'
    foreach ($name in $artifactHashes.Keys) {
        if ([string]$manifest.core_artifacts.$name -cne [string]$artifactHashes[$name]) {
            throw "Accepted converter artifact differs: $name."
        }
    }
    Assert-ExactPropertyNames $manifest.wheelhouse @($wheelhouseHashes.Keys) 'converter wheelhouse'
    foreach ($name in $wheelhouseHashes.Keys) {
        if ([string]$manifest.wheelhouse.$name -cne [string]$wheelhouseHashes[$name]) {
            throw "Accepted converter wheel differs: $name."
        }
    }
    if (@($manifest.removed_distributions).Count -ne 2 -or
        [string]$manifest.removed_distributions[0] -cne 'sglang' -or
        [string]$manifest.removed_distributions[1] -cne 'nixl') {
        throw 'Accepted converter removed-distribution projection differs.'
    }
    Assert-ExactPropertyNames $manifest.packages @($packageVersions.Keys) 'converter packages'
    foreach ($name in $packageVersions.Keys) {
        if ([string]$manifest.packages.$name -cne [string]$packageVersions[$name]) {
            throw "Accepted converter package version differs: $name."
        }
    }
    Assert-ExactPropertyNames $manifest.package_freeze @('name', 'lines', 'sha256') 'package freeze'
    if ([string]$manifest.package_freeze.name -cne 'converter-pip-freeze.txt' -or
        [int]$manifest.package_freeze.lines -ne 313 -or
        [string]$manifest.package_freeze.sha256 -cne 'd989c6a1e4215176e96e06059e7dbd5d6883d5fb30bc1f79255343e56a56d4d3') {
        throw 'Accepted converter package freeze identity differs.'
    }
    Assert-ExactPropertyNames $manifest.pip_check @('name', 'status', 'sha256') 'pip check'
    if ([string]$manifest.pip_check.name -cne 'converter-pip-check.txt' -or
        [string]$manifest.pip_check.status -cne 'passed' -or
        [string]$manifest.pip_check.sha256 -cne '9261363b733079a641c2e4cc9bc46ffa1d8336945a87f807b6cf68847dbc9b09') {
        throw 'Accepted converter pip-check identity differs.'
    }
    Assert-ExactPropertyNames $manifest.preflight_attestation @('name', 'sha256') 'preflight attestation'
    if ([string]$manifest.preflight_attestation.name -cne 'converter-preflight.json' -or
        [string]$manifest.preflight_attestation.sha256 -cne 'ee895ea6a82eddfe0ee8d649f26d1017ee588a3a0df45d2a8537a60b39ed498b') {
        throw 'Accepted converter preflight attestation identity differs.'
    }
    return [ordered]@{
        path = $resolved
        image = [string]$manifest.image_id
        image_id = [string]$manifest.image_id
        base_image = [string]$manifest.base_image
        base_image_id = [string]$manifest.base_image_id
        raw_sha256 = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant()
        packages_mode = 'preinstalled'
    }
}

function Test-DockerVolumeExists([string]$Name) {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & docker volume inspect $Name 1> $null 2> $null
        $inspectExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($inspectExitCode -eq 0) {
        return $true
    }
    if ($inspectExitCode -eq 1) {
        return $false
    }
    throw 'Docker volume existence check failed unexpectedly.'
}

function Assert-LocalImage($Converter) {
    $inspectionJson = & docker image inspect --format '{{json .}}' $Converter.image
    if ($LASTEXITCODE -ne 0) {
        throw 'Exact converter image is not present locally; this operator never pulls.'
    }
    $inspection = (($inspectionJson | ForEach-Object { [string]$_ }) -join "`n") | ConvertFrom-Json
    if ([string]$inspection.Os -cne 'linux' -or [string]$inspection.Architecture -cne 'amd64') {
        throw 'Converter image platform differs from linux/amd64.'
    }
    if ($Converter.packages_mode -ceq 'preinstalled') {
        if ([string]$inspection.Id -cne [string]$Converter.image_id) {
            throw 'Local derived converter ID differs from its accepted manifest.'
        }
        $baseInspectionJson = & docker image inspect --format '{{json .}}' $Converter.base_image
        if ($LASTEXITCODE -ne 0) {
            throw 'Accepted derived converter base image is not present locally.'
        }
        $baseInspection = (($baseInspectionJson | ForEach-Object { [string]$_ }) -join "`n") | ConvertFrom-Json
        if ([string]$baseInspection.Id -cne [string]$Converter.base_image_id -or
            [string]$baseInspection.Os -cne 'linux' -or
            [string]$baseInspection.Architecture -cne 'amd64') {
            throw 'Derived converter base image differs from its accepted manifest.'
        }
    }
}

function Write-NewUtf8File([string]$Path, [string]$Text) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $parent = Split-Path -Parent $resolved
    if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'OutputManifest parent directory does not exist.'
    }
    $parentItem = Get-Item -LiteralPath $parent
    if ($parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'OutputManifest parent directory must not be a reparse point.'
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text.Trim() + "`n")
    $stream = $null
    try {
        $stream = [IO.File]::Open($resolved, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
    return $resolved
}

if ($Mode -eq 'Convert' -and [string]::IsNullOrWhiteSpace($OutputManifest)) {
    throw 'Convert requires OutputManifest.'
}
if ($Mode -eq 'Verify' -and [string]::IsNullOrWhiteSpace($AcceptedOutputManifest)) {
    throw 'Verify requires AcceptedOutputManifest.'
}

$converter = if ([string]::IsNullOrWhiteSpace($AcceptedConverterManifest)) {
    [ordered]@{
        path = $null
        image = $preferredImage
        image_id = $null
        base_image = $null
        base_image_id = $null
        raw_sha256 = $null
        packages_mode = 'overlay'
    }
} else {
    Read-AcceptedConverterManifest $AcceptedConverterManifest
}

$plan = [ordered]@{
    schema = 'friday.secondary-modelopt-conversion-plan.v1'
    mode = $Mode
    apply = [bool]$Apply
    source_repository = 'openai/gpt-oss-20b'
    source_revision = $sourceRevision
    source_volume = $sourceVolume
    output_volume = $outputVolume
    conversion_image = $converter.image
    accepted_converter_manifest_sha256 = $converter.raw_sha256
    packages_mode = $converter.packages_mode
    qformat = 'nvfp4_mlp_only'
    kv_cache_qformat = 'none'
    calibration = [ordered]@{ rows = 256; sequence_length = 512; batch_size = 1 }
    use_sequential_device_map = $true
    gpu_max_mem_percentage = 0.70
    skip_generate = $true
    low_memory_mode = $false
    network = 'none'
    pulls = $false
    secrets = $false
    overwrite = $false
}
if (-not $Apply -or $Mode -eq 'Plan') {
    $plan | ConvertTo-Json -Depth 6
    return
}

$resolvedArtifacts = Resolve-RegularDirectory $ArtifactDirectory 'ArtifactDirectory'
$resolvedCalibration = Resolve-RegularFile $CalibrationFile 'CalibrationFile'
$resolvedCalibrationManifest = Resolve-RegularFile $CalibrationManifest 'CalibrationManifest'
Assert-ArtifactSet $resolvedArtifacts
if ($Mode -eq 'Convert' -and (Test-Path -LiteralPath ([IO.Path]::GetFullPath($OutputManifest)))) {
    throw 'Conversion refuses to overwrite OutputManifest.'
}
$resolvedAcceptedOutput = $null
if ($Mode -eq 'Verify') {
    $resolvedAcceptedOutput = Resolve-RegularFile $AcceptedOutputManifest 'AcceptedOutputManifest'
}

& docker version --format '{{.Server.Version}}' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker engine is unavailable.'
}
Assert-LocalImage $converter
if (-not (Test-DockerVolumeExists $sourceVolume)) {
    throw 'The exact verified source volume does not exist.'
}
if (-not (Test-DockerVolumeExists $outputVolume)) {
    throw 'The exact pre-created output volume does not exist.'
}

$toolPath = Resolve-RegularFile (Join-Path $PSScriptRoot 'modelopt_conversion_tool.py') 'conversion tool'
$common = @(
    'run', '--rm', '--pull', 'never', '--network', 'none', '--gpus', 'device=0',
    '--security-opt', 'no-new-privileges:true', '--cap-drop', 'ALL', '--pids-limit', '512',
    '--read-only', '--shm-size', '1g',
    '--tmpfs', '/tmp:size=8g,mode=1777',
    '--tmpfs', '/root/.cache:size=4g,mode=0700',
    '--tmpfs', '/run/friday-python:size=1g,mode=0700',
    '--mount', ('type=volume,source={0},target=/source,readonly' -f $sourceVolume),
    '--mount', ('type=bind,source={0},target=/artifacts,readonly' -f $resolvedArtifacts),
    '--mount', ('type=bind,source={0},target=/calibration/calibration.jsonl,readonly' -f $resolvedCalibration),
    '--mount', ('type=bind,source={0},target=/calibration/calibration.observed.json,readonly' -f $resolvedCalibrationManifest),
    '--mount', ('type=bind,source={0},target=/bundle/modelopt_conversion_tool.py,readonly' -f $toolPath),
    '--entrypoint', 'python3', $converter.image
)

if ($Mode -eq 'Convert') {
    $preflightArguments = @(
        $common[0..($common.Count - 4)] +
        @('--mount', ('type=volume,source={0},target=/output,readonly' -f $outputVolume)) +
        $common[($common.Count - 3)..($common.Count - 1)] +
        @('/bundle/modelopt_conversion_tool.py', 'validate-inputs', '--packages-mode', $converter.packages_mode)
    )
    & docker @preflightArguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Sealed conversion input preflight failed before output mutation.'
    }

    $conversionArguments = @(
        $common[0..($common.Count - 4)] +
        @('--mount', ('type=volume,source={0},target=/output' -f $outputVolume)) +
        $common[($common.Count - 3)..($common.Count - 1)] +
        @('/bundle/modelopt_conversion_tool.py', 'convert', '--packages-mode', $converter.packages_mode)
    )
    & docker @conversionArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Sealed conversion failed; partial output volume was retained and will not be overwritten.'
    }

    $observeArguments = @(
        $common[0..($common.Count - 4)] +
        @('--mount', ('type=volume,source={0},target=/output,readonly' -f $outputVolume)) +
        $common[($common.Count - 3)..($common.Count - 1)] +
        @('/bundle/modelopt_conversion_tool.py', 'observe-output', '--conversion-image', $converter.image)
    )
    if ($null -ne $converter.raw_sha256) {
        $observeArguments += @('--accepted-converter-manifest-sha256', $converter.raw_sha256)
    }
    $manifestJson = & docker @observeArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Converted output failed the closed provenance/metadata audit.'
    }
    $resolvedOutput = Write-NewUtf8File $OutputManifest (($manifestJson | ForEach-Object { [string]$_ }) -join "`n")
    [ordered]@{
        schema = 'friday.secondary-modelopt-conversion-operation.v1'
        status = 'completed_unaccepted'
        output_volume = $outputVolume
        output_manifest = $resolvedOutput
        raw_model_content_reported = $false
        secrets_used = $false
    } | ConvertTo-Json -Depth 4
    return
}

$verifyArguments = @(
    $common[0..($common.Count - 4)] +
    @(
        '--mount', ('type=volume,source={0},target=/output,readonly' -f $outputVolume),
        '--mount', ('type=bind,source={0},target=/bundle/accepted-output-manifest.json,readonly' -f $resolvedAcceptedOutput)
    ) +
    $common[($common.Count - 3)..($common.Count - 1)] +
    @(
        '/bundle/modelopt_conversion_tool.py', 'verify-output',
        '--conversion-image', $converter.image,
        '--manifest', '/bundle/accepted-output-manifest.json'
    )
)
if ($null -ne $converter.raw_sha256) {
    $verifyArguments += @('--accepted-converter-manifest-sha256', $converter.raw_sha256)
}
& docker @verifyArguments
if ($LASTEXITCODE -ne 0) {
    throw 'Output volume does not match the exact accepted conversion manifest.'
}
