[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('bootstrap', 'full')]
    [string]$Phase,

    [Parameter(Mandatory = $true)]
    [string]$SourceRoot,

    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$liveRoot = 'D:\jarvis-gpt\qwen38-v12-attested-bundle'
$lockPath = 'D:\jarvis-gpt\sglang-qwen38-w4a16\switch.lock'
$manifestName = 'TRANSPORT-FILES.v1'
$applierName = 'Apply-Qwen38V12AttestedBundle.ps1'
$stagePattern = '^D:\\jarvis-gpt\\qwen38-v12-attested-sync\\expanded\\[0-9a-f]{64}-[0-9a-f]{32}$'
$expectedRoles = [ordered]@{
    'AttestedBundle.Common.ps1' = 'bootstrap'
    'Cleanup-StoppedQwen38V12Attested.ps1' = 'bootstrap'
    'docker-compose.publish-8001.yml' = 'bootstrap'
    'CORE-SHA256SUMS' = 'full'
    'ORCHESTRATION-SHA256SUMS' = 'full'
    'ORCHESTRATION.md' = 'full'
    'README.md' = 'full'
    'Rollback-Qwen38V12Attested.ps1' = 'full'
    'Switch-Qwen38V12Attested.ps1' = 'full'
    'Test-AttestedCleanupProjection.ps1' = 'full'
    'Test-AttestedNetworkProjection.ps1' = 'full'
    'Test-AttestedReceiptSerialization.ps1' = 'full'
}
$bootstrapPublicationOrder = @(
    'docker-compose.publish-8001.yml',
    'AttestedBundle.Common.ps1',
    'Cleanup-StoppedQwen38V12Attested.ps1'
)
$fullPublicationOrder = @(
    'docker-compose.publish-8001.yml',
    'AttestedBundle.Common.ps1',
    'Cleanup-StoppedQwen38V12Attested.ps1',
    'ORCHESTRATION.md',
    'README.md',
    'Rollback-Qwen38V12Attested.ps1',
    'Switch-Qwen38V12Attested.ps1',
    'Test-AttestedCleanupProjection.ps1',
    'Test-AttestedNetworkProjection.ps1',
    'Test-AttestedReceiptSerialization.ps1',
    'CORE-SHA256SUMS',
    'ORCHESTRATION-SHA256SUMS'
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

function Read-TransportManifest([string]$Path) {
    $records = [ordered]@{}
    $lines = @(Get-Content -LiteralPath $Path -Encoding ascii)
    if ($lines.Count -ne $expectedRoles.Count) {
        throw 'Transport manifest row count is not exact'
    }
    foreach ($line in $lines) {
        if ([string]$line -cnotmatch `
            '^(bootstrap|full) ([0-9a-f]{64}|ABSENT) ([0-9a-f]{64}) ([A-Za-z0-9._-]+)$') {
            throw 'Transport manifest row is not canonical'
        }
        $role = [string]$Matches[1]
        $oldHash = [string]$Matches[2]
        $newHash = [string]$Matches[3]
        $name = [string]$Matches[4]
        if (-not $expectedRoles.Contains($name) -or
            [string]$expectedRoles[$name] -cne $role -or
            $records.Contains($name)) {
            throw 'Transport manifest name or phase is not exact'
        }
        $records[$name] = [pscustomobject][ordered]@{
            name = $name
            role = $role
            old_sha256 = $(if ($oldHash -ceq 'ABSENT') { $null } else { $oldHash })
            new_sha256 = $newHash
        }
    }
    foreach ($entry in $expectedRoles.GetEnumerator()) {
        if (-not $records.Contains([string]$entry.Key)) {
            throw 'Transport manifest omits an allowlisted target'
        }
    }
    return $records
}

function Test-ExactNullableSha256(
    [AllowNull()][object]$Left,
    [AllowNull()][object]$Right
) {
    if ($null -eq $Left -or $null -eq $Right) {
        return ($null -eq $Left -and $null -eq $Right)
    }
    return ([string]$Left -ceq [string]$Right)
}

if ($SourceRoot -cnotmatch $stagePattern) {
    throw 'SourceRoot is not an exact code-owned expanded-stage path'
}
if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw 'Expanded source root is absent'
}
$sourceRootItem = Get-Item -LiteralPath $SourceRoot -Force
if (($sourceRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Expanded source root is a reparse point'
}
if (-not (Test-Path -LiteralPath $liveRoot -PathType Container)) {
    throw 'Live attested bundle root is absent'
}
$liveRootItem = Get-Item -LiteralPath $liveRoot -Force
if (($liveRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Live attested bundle root is a reparse point'
}
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw 'Shared switch lock is absent'
}

$manifestPath = Join-Path $SourceRoot $manifestName
$null = Get-ExactSha256 $manifestPath 'transport manifest'
$records = Read-TransportManifest $manifestPath
$publicationOrder = $(
    if ($Phase -ceq 'bootstrap') { $bootstrapPublicationOrder }
    else { $fullPublicationOrder }
)
$selected = @(
    foreach ($name in $publicationOrder) {
        $records[[string]$name]
    }
)
$expectedStageNames = @($selected.name) + @($applierName, $manifestName)
$stageItems = @(Get-ChildItem -LiteralPath $SourceRoot -Force)
if ($stageItems.Count -ne $expectedStageNames.Count) {
    throw 'Expanded stage item count is not exact'
}
foreach ($item in $stageItems) {
    if ($item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $expectedStageNames -cnotcontains [string]$item.Name) {
        throw 'Expanded stage contains a non-allowlisted item'
    }
}

$projections = @()
foreach ($record in $selected) {
    $sourcePath = Join-Path $SourceRoot ([string]$record.name)
    $sourceHash = Get-ExactSha256 $sourcePath "staged $($record.name)"
    if ($sourceHash -cne [string]$record.new_sha256) {
        throw "Staged source hash is not frozen: $($record.name)"
    }

    $targetPath = Join-Path $liveRoot ([string]$record.name)
    $targetExists = Test-Path -LiteralPath $targetPath -PathType Leaf
    $targetHash = $null
    if ($targetExists) {
        $targetHash = Get-ExactSha256 $targetPath "live $($record.name)"
        if ($targetHash -cne [string]$record.new_sha256 -and
            ($null -eq $record.old_sha256 -or
                $targetHash -cne [string]$record.old_sha256)) {
            throw "Live target is neither the exact old nor frozen new byte set: $($record.name)"
        }
    }
    elseif ($null -ne $record.old_sha256) {
        throw "Required old live target is absent: $($record.name)"
    }
    $projections += [pscustomobject][ordered]@{
        name = [string]$record.name
        old_sha256 = $record.old_sha256
        new_sha256 = [string]$record.new_sha256
        before_sha256 = $targetHash
        action = $(if ($targetHash -ceq [string]$record.new_sha256) { 'retain' } else { 'replace' })
    }
}

$lock = $null
try {
    $lock = [IO.File]::Open(
        $lockPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )

    # Revalidate the entire target projection while holding the same cooperative
    # lock used by switch, rollback, and cleanup before the first replacement.
    foreach ($projection in $projections) {
        $targetPath = Join-Path $liveRoot ([string]$projection.name)
        $actual = $null
        if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
            $actual = Get-ExactSha256 $targetPath "locked live $($projection.name)"
        }
        if (-not (Test-ExactNullableSha256 $actual $projection.before_sha256)) {
            throw "Live target changed between CAS projection and lock: $($projection.name)"
        }
    }

    if ($Execute) {
        foreach ($projection in $projections) {
            $targetPath = Join-Path $liveRoot ([string]$projection.name)
            $currentHash = $null
            if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
                $currentHash = Get-ExactSha256 $targetPath "current live $($projection.name)"
            }
            if ($currentHash -ceq [string]$projection.new_sha256) {
                continue
            }
            if (-not (Test-ExactNullableSha256 $currentHash $projection.old_sha256)) {
                throw "Live target lost its exact CAS predecessor: $($projection.name)"
            }

            $sourcePath = Join-Path $SourceRoot ([string]$projection.name)
            $temporaryPath = Join-Path $liveRoot (
                '.{0}.sync-{1}.tmp' -f $projection.name, [Guid]::NewGuid().ToString('N')
            )
            try {
                [IO.File]::Copy($sourcePath, $temporaryPath, $false)
                if ((Get-ExactSha256 $temporaryPath 'same-directory sync temporary file') -cne
                    [string]$projection.new_sha256) {
                    throw "Copied temporary hash changed: $($projection.name)"
                }
                $currentHash = $null
                if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
                    $currentHash = Get-ExactSha256 $targetPath "immediate CAS live $($projection.name)"
                }
                if (-not (Test-ExactNullableSha256 $currentHash $projection.old_sha256)) {
                    throw "Live target changed before atomic replacement: $($projection.name)"
                }
                if ($null -eq $projection.old_sha256) {
                    [IO.File]::Move($temporaryPath, $targetPath)
                }
                else {
                    [IO.File]::Replace($temporaryPath, $targetPath, $null, $true)
                }
            }
            finally {
                if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
                    Remove-Item -LiteralPath $temporaryPath -Force
                }
            }
            if ((Get-ExactSha256 $targetPath "updated live $($projection.name)") -cne
                [string]$projection.new_sha256) {
                throw "Atomic replacement did not land frozen bytes: $($projection.name)"
            }
        }
    }

    foreach ($projection in $projections) {
        $targetPath = Join-Path $liveRoot ([string]$projection.name)
        $finalHash = $null
        if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
            $finalHash = Get-ExactSha256 $targetPath "final live $($projection.name)"
        }
        $projection | Add-Member -NotePropertyName after_sha256 -NotePropertyValue $finalHash
        if ($Execute -and $finalHash -cne [string]$projection.new_sha256) {
            throw "Final live target is not frozen: $($projection.name)"
        }
    }

    [pscustomobject][ordered]@{
        schema = 'friday.attested-bundle-sync.v1'
        phase = $Phase
        mutation_authorized = [bool]$Execute
        status = $(if ($Execute) { 'applied' } else { 'preflight_ready' })
        live_root = $liveRoot
        shared_lock = $lockPath
        target_count = $projections.Count
        targets = $projections
        runtime_state_untouched = $true
        network_volume_image_untouched = $true
    } | ConvertTo-Json -Compress -Depth 12
}
finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
