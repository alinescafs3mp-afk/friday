[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$ObservedPath,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$AcceptedPath,

    [Parameter()]
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$expectedObservedSha256 = '7b850221e7e11ac0063971d7baaf627c96eae5441368f1907cc070106832b0f3'
$expectedAcceptedSha256 = '0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f'

function Get-BytesSha256([byte[]]$Bytes) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function New-CanonicalReceipt([string]$Status) {
    if ($Status -cnotin @('observed_unaccepted', 'accepted')) {
        throw 'Receipt status is outside the closed promotion vocabulary.'
    }
    $receipt = [ordered]@{
        docker = [ordered]@{
            client_api_version = '1.55'
            client_version = '29.7.2'
            compose_version = '5.4.0'
            desktop_file_version = '4.87.0.236836'
            desktop_product_version = '4.87.0.236836'
            server_api_version = '1.55'
            server_architecture = 'x86_64'
            server_os = 'linux'
            server_version = '29.7.2'
        }
        gpu = [ordered]@{
            compute_capability = '12.0'
            driver_version = '610.88'
            memory_total_mib = 16303
            name = 'NVIDIA GeForce RTX 5080 Laptop GPU'
            uuid = 'GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07'
        }
        schema = 'friday.secondary-hardware-runtime.v1'
        status = $Status
        windows = [ordered]@{
            build = '26200'
            caption = 'Майкрософт Windows 11 Pro'
            version = '10.0.26200'
        }
        wsl = [ordered]@{
            direct3d_version = '1.611.1-81528511'
            dxcore_version = '10.0.26100.1-240331-1435.ge-release'
            kernel_version = '6.6.114.1-1'
            msrdc_version = '1.2.6676'
            version = '2.7.3.0'
            windows_component_version = '10.0.26200.9168'
            wslg_version = '1.0.73'
        }
    }
    return ($receipt | ConvertTo-Json -Depth 6 -Compress) + "`n"
}

function Read-BoundedObservedReceipt([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $before = Get-Item -LiteralPath $resolved -Force
    if ($before.PSIsContainer -or
        ($before.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $before.Length -lt 1 -or
        $before.Length -gt 16384) {
        throw 'Observed receipt is not a bounded non-reparse regular file.'
    }
    $bytes = [IO.File]::ReadAllBytes($resolved)
    $after = Get-Item -LiteralPath $resolved -Force
    if ($bytes.Length -ne $before.Length -or
        $after.Length -ne $before.Length -or
        $after.LastWriteTimeUtc.Ticks -ne $before.LastWriteTimeUtc.Ticks) {
        throw 'Observed receipt changed during verification.'
    }
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xef -and $bytes[1] -eq 0xbb -and $bytes[2] -eq 0xbf) {
        throw 'Observed receipt has a forbidden UTF-8 BOM.'
    }
    $strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
    return [ordered]@{
        bytes = $bytes
        text = $strictUtf8.GetString($bytes)
        resolved_path = $resolved
    }
}

function Write-NewUtf8File([string]$Path, [string]$Text) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $parent = Split-Path -Parent $resolved
    if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'Accepted receipt parent directory does not exist.'
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
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

$observed = Read-BoundedObservedReceipt $ObservedPath
$canonicalObserved = New-CanonicalReceipt 'observed_unaccepted'
$observedSha256 = Get-BytesSha256 $observed.bytes
if ([string]$observed.text -cne $canonicalObserved -or $observedSha256 -cne $expectedObservedSha256) {
    throw 'Observed receipt differs from the exact canonical hardware/runtime contract.'
}

$canonicalAccepted = New-CanonicalReceipt 'accepted'
$acceptedBytes = [Text.UTF8Encoding]::new($false).GetBytes($canonicalAccepted)
$acceptedSha256 = Get-BytesSha256 $acceptedBytes
if ($acceptedSha256 -cne $expectedAcceptedSha256) {
    throw 'Accepted receipt serialization differs from the canonical contract.'
}

$resolvedAcceptedPath = [IO.Path]::GetFullPath($AcceptedPath)
$applied = $false
if ($Apply) {
    $resolvedAcceptedPath = Write-NewUtf8File $AcceptedPath $canonicalAccepted
    $applied = $true
}

[ordered]@{
    schema = 'friday.secondary-hardware-runtime-promotion.v1'
    status = $(if ($applied) { 'accepted_receipt_created' } else { 'plan_only' })
    observed_path = $observed.resolved_path
    observed_sha256 = $observedSha256
    accepted_path = $resolvedAcceptedPath
    accepted_sha256 = $acceptedSha256
    applied = $applied
    overwritten = $false
} | ConvertTo-Json -Depth 4
