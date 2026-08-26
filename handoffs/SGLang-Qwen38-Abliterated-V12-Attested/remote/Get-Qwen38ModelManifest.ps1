[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$ModelRoot = 'D:\jarvis\data\models\qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5'
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
    model_repository = 'Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4'
    model_revision = '43aa7ff5eef05ab50a3bfa6aca581085312c7a04'
    model_quantization = 'W4A4_NVFP4_FP8_KV'
    snapshot_directory = 'qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5'
    file_count = $rows.Count
    total_bytes = $totalBytes
    files = $rows
} | ConvertTo-Json -Depth 6
