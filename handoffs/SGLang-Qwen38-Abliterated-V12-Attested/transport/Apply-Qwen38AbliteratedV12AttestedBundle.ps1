[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceRoot,

    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$liveParent = 'D:\jarvis-gpt'
$liveRoot = 'D:\jarvis-gpt\qwen38-abliterated-v12-attested-bundle'
$lockPath = 'D:\jarvis-gpt\sglang-qwen38-w4a16\switch.lock'
$stagePattern = '^D:\\jarvis-gpt\\qwen38-abliterated-v12-attested-sync\\expanded\\[0-9a-f]{64}-[0-9a-f]{32}$'
$manifestName = 'TRANSPORT-FILES.v1'
$applierName = 'Apply-Qwen38AbliteratedV12AttestedBundle.ps1'
$expectedNames = @(
    '.dockerignore',
    'AttestedBundle.Common.ps1',
    'Build-AttestedImages.ps1',
    'Cleanup-StoppedQwen38AbliteratedV12Attested.ps1',
    'CORE-SHA256SUMS',
    'Dockerfile.engine',
    'Dockerfile.proxy',
    'Get-Qwen38ModelManifest.ps1',
    'ORCHESTRATION-SHA256SUMS',
    'ORCHESTRATION.md',
    'Preflight-Qwen38AbliteratedV12Attested.ps1',
    'README.md',
    'Rollback-Qwen38AbliteratedV12Attested.ps1',
    'Switch-Qwen38AbliteratedV12Attested.ps1',
    'Test-AttestedBindMountProjection.ps1',
    'Test-AttestedCapabilityProjection.ps1',
    'Test-AttestedCleanupProjection.ps1',
    'Test-AttestedNetworkProjection.ps1',
    'Test-AttestedProxy.ps1',
    'Test-AttestedPublisherObservation.ps1',
    'Test-AttestedReceiptSerialization.ps1',
    'build-attestation.v1.json',
    'default.conf.template',
    'deployment-identity.v1.json',
    'deployment.lock.env.example',
    'docker-compose.attested.yml',
    'docker-compose.publish-8001.yml',
    'engine-witness-entrypoint.py',
    'launch-manifest.v1.json',
    'model-volume-sealer.py',
    'qwen38-model-manifest.v1.json'
)

function Get-ExactSha256([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is absent"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label is a reparse point"
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

if ($SourceRoot -cnotmatch $stagePattern -or
    -not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw 'SourceRoot is not an exact expanded transport stage'
}
$sourceItem = Get-Item -LiteralPath $SourceRoot -Force
if (($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Expanded transport stage is a reparse point'
}

$manifestPath = Join-Path $SourceRoot $manifestName
$lines = @(Get-Content -LiteralPath $manifestPath -Encoding ascii)
if ($lines.Count -ne $expectedNames.Count) {
    throw 'Transport manifest row count is not exact'
}
$expected = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($name in $expectedNames) {
    if (-not $expected.Add($name)) { throw 'Duplicate expected transport name' }
}
$records = [ordered]@{}
foreach ($line in $lines) {
    if ([string]$line -cnotmatch '^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$') {
        throw 'Transport manifest row is not canonical'
    }
    $hash = [string]$Matches[1]
    $name = [string]$Matches[2]
    if (-not $expected.Contains($name) -or $records.Contains($name)) {
        throw 'Transport manifest name is not exact'
    }
    $records[$name] = $hash
}
foreach ($name in $expectedNames) {
    if (-not $records.Contains($name)) { throw 'Transport manifest omits a payload' }
}

$stageItems = @(Get-ChildItem -LiteralPath $SourceRoot -Force)
if ($stageItems.Count -ne ($expectedNames.Count + 2)) {
    throw 'Expanded transport stage item count is not exact'
}
foreach ($item in $stageItems) {
    if ($item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ([string]$item.Name -cne $manifestName -and
            [string]$item.Name -cne $applierName -and
            -not $expected.Contains([string]$item.Name))) {
        throw 'Expanded transport stage contains a non-allowlisted item'
    }
}
foreach ($name in $expectedNames) {
    $actual = Get-ExactSha256 (Join-Path $SourceRoot $name) "staged $name"
    if ($actual -cne [string]$records[$name]) {
        throw "Staged payload hash changed: $name"
    }
}

if (-not (Test-Path -LiteralPath $liveParent -PathType Container)) {
    throw 'Live parent is absent'
}
$parentItem = Get-Item -LiteralPath $liveParent -Force
if (($parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Live parent is a reparse point'
}
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw 'Shared switch lock is absent'
}

$lock = $null
$temporaryRoot = $null
try {
    $lock = [IO.File]::Open(
        $lockPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
    if (Test-Path -LiteralPath $liveRoot) {
        throw 'Isolated live bundle root already exists; create-new transport refuses replacement'
    }

    if ($Execute) {
        $temporaryRoot = Join-Path $liveParent (
            '.qwen38-abliterated-v12-attested-bundle.' + [Guid]::NewGuid().ToString('N') + '.tmp'
        )
        [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
        foreach ($name in $expectedNames) {
            $sourcePath = Join-Path $SourceRoot $name
            $targetPath = Join-Path $temporaryRoot $name
            [IO.File]::Copy($sourcePath, $targetPath, $false)
            if ((Get-ExactSha256 $targetPath "copied $name") -cne [string]$records[$name]) {
                throw "Copied payload hash changed: $name"
            }
        }
        $copied = @(Get-ChildItem -LiteralPath $temporaryRoot -Force)
        if ($copied.Count -ne $expectedNames.Count) {
            throw 'Temporary bundle item count changed'
        }
        if (Test-Path -LiteralPath $liveRoot) {
            throw 'Live bundle root appeared before atomic publication'
        }
        [IO.Directory]::Move($temporaryRoot, $liveRoot)
        $temporaryRoot = $null
    }

    [pscustomobject][ordered]@{
        schema = 'friday.attested-bundle-create.v1'
        status = $(if ($Execute) { 'created' } else { 'preflight_ready' })
        mutation_authorized = [bool]$Execute
        live_root = $liveRoot
        payload_count = $expectedNames.Count
        shared_lock = $lockPath
        runtime_state_untouched = $true
    } | ConvertTo-Json -Compress -Depth 4
}
finally {
    if ($null -ne $temporaryRoot -and
        [string]$temporaryRoot -cmatch '^D:\\jarvis-gpt\\\.qwen38-abliterated-v12-attested-bundle\.[0-9a-f]{32}\.tmp$' -and
        (Test-Path -LiteralPath $temporaryRoot -PathType Container)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
    if ($null -ne $lock) { $lock.Dispose() }
}
