[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'AttestedBundle.Common.ps1')

function New-SyntheticContainer(
    [string]$Source,
    [string]$Destination = '/root/.cache',
    [string]$Type = 'bind',
    [bool]$ReadWrite = $true
) {
    return [pscustomobject]@{
        Mounts = @(
            [pscustomobject]@{
                Destination = $Destination
                Type = $Type
                Source = $Source
                RW = $ReadWrite
            }
        )
    }
}

function Assert-Accepted([string]$Observed, [string]$Expected) {
    Assert-BindMount (New-SyntheticContainer $Observed) '/root/.cache' $Expected $false
}

function Assert-Rejected([string]$Observed, [string]$Expected) {
    $rejected = $false
    try {
        Assert-BindMount (New-SyntheticContainer $Observed) '/root/.cache' $Expected $false
    }
    catch {
        $rejected = $true
    }
    if (-not $rejected) {
        throw "Unsafe bind source was accepted: $Observed"
    }
}

function Assert-InvalidExpected([string]$Expected) {
    $rejected = $false
    try {
        $null = Test-ExactAttestedBindSource $Expected $Expected
    }
    catch {
        $rejected = $true
    }
    if (-not $rejected) {
        throw "Unsafe expected bind source was accepted: $Expected"
    }
}

$cachePath = 'D:\jarvis\cache\sglang-qwen38-v12-attested'
$dockerDesktopCachePath = '/run/desktop/mnt/host/d/jarvis/cache/sglang-qwen38-v12-attested'
Assert-Accepted $cachePath $cachePath
Assert-Accepted 'D:/jarvis/cache/sglang-qwen38-v12-attested' $cachePath
Assert-Accepted $dockerDesktopCachePath $cachePath

$modelPath = 'D:\jarvis\data\models\qwen3.8-27b-nvfp4-a2genesis-bfd9b312'
Assert-Accepted $modelPath $modelPath
Assert-Accepted 'D:/jarvis/data/models/qwen3.8-27b-nvfp4-a2genesis-bfd9b312' $modelPath
Assert-Accepted '/run/desktop/mnt/host/d/jarvis/data/models/qwen3.8-27b-nvfp4-a2genesis-bfd9b312' `
    $modelPath

foreach ($mutation in @(
    'D:\jarvis/cache\sglang-qwen38-v12-attested',
    'D:/jarvis\cache/sglang-qwen38-v12-attested',
    'd:\jarvis\cache\sglang-qwen38-v12-attested',
    'd:/jarvis/cache/sglang-qwen38-v12-attested',
    'E:\jarvis\cache\sglang-qwen38-v12-attested',
    'D:\Jarvis\cache\sglang-qwen38-v12-attested',
    'D:\jarvis\cache\..\cache\sglang-qwen38-v12-attested',
    'D:\jarvis\cache\sglang-qwen38-v12-attested\',
    '\\?\D:\jarvis\cache\sglang-qwen38-v12-attested',
    '/run/desktop/mnt/host/c/jarvis/cache/sglang-qwen38-v12-attested',
    '/run/desktop/mnt/host/D/jarvis/cache/sglang-qwen38-v12-attested',
    '/Run/desktop/mnt/host/d/jarvis/cache/sglang-qwen38-v12-attested',
    '/run/desktop/mnt/host/d/Jarvis/cache/sglang-qwen38-v12-attested',
    '/run/desktop/mnt/host/d/jarvis/cache/../cache/sglang-qwen38-v12-attested',
    '/run/desktop/mnt/host/d/jarvis/cache//sglang-qwen38-v12-attested',
    '/run/desktop/mnt/host/d/jarvis/cache/sglang-qwen38-v12-attested/',
    '/run/desktop/mnt/host/d/jarvis/cache/sglang-qwen38-v12-attested-extra'
)) {
    Assert-Rejected $mutation $cachePath
}

foreach ($unsafeExpected in @(
    'd:\jarvis\cache\sglang-qwen38-v12-attested',
    'D:/jarvis/cache/sglang-qwen38-v12-attested',
    'D:\jarvis\cache\.\sglang-qwen38-v12-attested',
    'D:\jarvis\cache\..\cache\sglang-qwen38-v12-attested',
    'D:\jarvis\\cache\sglang-qwen38-v12-attested',
    'D:\jarvis\cache\sglang qwen38',
    ('D:\' + ('a' * 238)),
    ('D:\' + [string]::Join('\', @(1..33 | ForEach-Object { "s$_" })))
)) {
    Assert-InvalidExpected $unsafeExpected
}
Assert-Rejected ('x' * 513) $cachePath

$wrongType = New-SyntheticContainer $cachePath -Type 'volume'
$wrongAccess = New-SyntheticContainer $cachePath -ReadWrite $false
$wrongDestination = New-SyntheticContainer $cachePath -Destination '/root/.cache-near-miss'
foreach ($container in @($wrongType, $wrongAccess, $wrongDestination)) {
    $rejected = $false
    try {
        Assert-BindMount $container '/root/.cache' $cachePath $false
    }
    catch {
        $rejected = $true
    }
    if (-not $rejected) {
        throw 'A bind mount identity mutation was accepted'
    }
}

'attested bind mount projection: PASS'
