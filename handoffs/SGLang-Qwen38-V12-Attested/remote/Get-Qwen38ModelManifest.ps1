[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$ModelRoot = 'D:\jarvis\data\models\qwen3.8-27b-nvfp4-a2genesis-bfd9b312'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$root = (Get-Item -LiteralPath $ModelRoot -Force).FullName.TrimEnd('\')
if (-not (Test-Path -LiteralPath $root -PathType Container)) {
    throw 'Model root is not a directory.'
}
$items = @(Get-ChildItem -LiteralPath $root -Force -Recurse)
if (@($items | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }).Count -ne 0) {
    throw 'Model root contains a reparse point.'
}
$relativePaths = [string[]]@(
    $items |
        Where-Object { -not $_.PSIsContainer } |
        ForEach-Object { $_.FullName.Substring($root.Length + 1).Replace('\', '/') }
)
[Array]::Sort($relativePaths, [StringComparer]::Ordinal)

$rows = [Collections.Generic.List[object]]::new()
[int64]$totalBytes = 0
foreach ($relativePath in $relativePaths) {
    if ($relativePath.Contains('/') -or $relativePath.Contains('\')) {
        throw 'This manifest version permits top-level regular files only.'
    }
    $path = Join-Path -Path $root -ChildPath $relativePath
    $item = Get-Item -LiteralPath $path -Force
    $totalBytes += [int64]$item.Length
    $rows.Add([ordered]@{
        path = $relativePath
        size = [int64]$item.Length
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    })
}

[ordered]@{
    schema = 'friday.model-snapshot-manifest.v1'
    model_repository = 'a2genesis/Qwen3.8-27B-NVFP4'
    model_revision = 'bfd9b31207712e0850eec9da32261e8c5ee16af7'
    model_quantization = 'W4A16_NVFP4'
    snapshot_directory = 'qwen3.8-27b-nvfp4-a2genesis-bfd9b312'
    file_count = $rows.Count
    total_bytes = $totalBytes
    files = $rows
} | ConvertTo-Json -Depth 6
