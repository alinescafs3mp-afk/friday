$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'AttestedBundle.Common.ps1')

# Exercise the exact .NET surface used by Invoke-SixWayProbe in a fresh native
# PowerShell process, without issuing any network request.
Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
$httpClient = [Net.Http.HttpClient]::new()
try {
    $httpClient.DefaultRequestHeaders.Authorization =
        [Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', 'projection-token')
    $content = [Net.Http.StringContent]::new(
        '{"projection":true}',
        [Text.Encoding]::UTF8,
        'application/json'
    )
    $content.Dispose()
    $tasks = @()
    for ($index = 1; $index -le 6; $index += 1) {
        $tasks += [Threading.Tasks.Task]::Delay(0)
    }
    $all = [Threading.Tasks.Task]::WhenAll([Threading.Tasks.Task[]]$tasks)
    if (-not $all.Wait(1000)) {
        throw 'Six-way HTTP client projection timed out'
    }
} finally {
    $httpClient.Dispose()
}

# Load only the production convergence helper through the native PowerShell AST.
# The projection replaces its GPU reader and journal sink; it never calls Docker,
# nvidia-smi, or the candidate endpoint.
$switchPath = Join-Path $PSScriptRoot 'Switch-Qwen38V12Attested.ps1'
$switchTokens = $null
$switchErrors = $null
$switchAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $switchPath,
    [ref]$switchTokens,
    [ref]$switchErrors
)
if ($switchErrors.Count -ne 0) {
    throw 'Switch source does not parse for GPU headroom projection'
}
$headroomFunctions = @($switchAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        [string]$node.Name -ceq 'Wait-PostSixWayGpuHeadroom'
}, $true))
if ($headroomFunctions.Count -ne 1) {
    throw 'Post-six-way GPU headroom helper definition is not exact'
}
. ([scriptblock]::Create([string]$headroomFunctions[0].Extent.Text))

$script:gpuProjectionReadings = @()
$script:gpuProjectionPersistentFreeMiB = $null
$script:gpuProjectionProbeCount = 0
$script:gpuProjectionJournal = @()

function Set-GpuProjectionReadings([object[]]$Readings) {
    $script:gpuProjectionReadings = @($Readings)
    $script:gpuProjectionPersistentFreeMiB = $null
    $script:gpuProjectionProbeCount = 0
    $script:gpuProjectionJournal = @()
}

function Get-GpuMemory {
    $script:gpuProjectionProbeCount += 1
    if ($null -ne $script:gpuProjectionPersistentFreeMiB) {
        $value = [int]$script:gpuProjectionPersistentFreeMiB
    }
    else {
        if ($script:gpuProjectionReadings.Count -eq 0) {
            throw 'GPU projection exhausted its exact readings'
        }
        $value = $script:gpuProjectionReadings[0]
        $script:gpuProjectionReadings = @($script:gpuProjectionReadings | Select-Object -Skip 1)
    }
    if ($value -is [Exception]) { throw $value }
    return [pscustomobject]@{
        TotalMiB = 49140
        UsedMiB = 49140 - [int]$value
        FreeMiB = [int]$value
    }
}

function Write-Journal([string]$State, [hashtable]$Data = @{}) {
    $script:gpuProjectionJournal += [pscustomobject]@{
        State = $State
        Data = $Data
    }
}

Set-GpuProjectionReadings @(1200, 1400, 1536)
$verifiedGpu = Wait-PostSixWayGpuHeadroom 2 1
if ([int]$verifiedGpu.FreeMiB -ne 1536 -or $script:gpuProjectionProbeCount -ne 3 -or
    $script:gpuProjectionJournal.Count -ne 1) {
    throw 'Post-six-way GPU headroom did not converge on the exact third valid reading'
}
$verifiedRecord = $script:gpuProjectionJournal[0]
$verifiedKeys = [string]::Join(',', @($verifiedRecord.Data.Keys | Sort-Object))
if ([string]$verifiedRecord.State -cne 'post_six_way_gpu_headroom_verified' -or
    $verifiedKeys -cne 'attempts,free_mib,minimum_free_mib,request_count,timeout_seconds' -or
    [int]$verifiedRecord.Data.attempts -ne 3 -or
    [int]$verifiedRecord.Data.free_mib -ne 1536 -or
    [int]$verifiedRecord.Data.minimum_free_mib -ne 1536 -or
    [int]$verifiedRecord.Data.request_count -ne 6 -or
    [int]$verifiedRecord.Data.timeout_seconds -ne 2) {
    throw 'Verified post-six-way GPU journal evidence is not exact or body-free'
}

foreach ($probeFailure in @(
    [ComponentModel.Win32Exception]::new('synthetic nvidia-smi command failure'),
    [FormatException]::new('synthetic nvidia-smi schema failure')
)) {
    Set-GpuProjectionReadings @($probeFailure, 1536)
    $probeFailed = $false
    $probeTimer = [Diagnostics.Stopwatch]::StartNew()
    try { $null = Wait-PostSixWayGpuHeadroom 30 2000 }
    catch {
        $probeFailed = $true
        if ($_.Exception.GetType().FullName -cne $probeFailure.GetType().FullName) {
            throw 'Post-six-way GPU headroom did not propagate the probe error exactly'
        }
    }
    finally { $probeTimer.Stop() }
    $probeState = [string]$script:gpuProjectionJournal[0].State
    if (-not $probeFailed -or $script:gpuProjectionProbeCount -ne 1 -or
        $probeTimer.Elapsed.TotalSeconds -ge 1 -or $script:gpuProjectionJournal.Count -ne 1 -or
        $probeState -cne 'post_six_way_gpu_headroom_probe_failed') {
        throw 'Post-six-way GPU command/schema failure was retried or not journaled'
    }
}

Set-GpuProjectionReadings @()
$script:gpuProjectionPersistentFreeMiB = 1400
$timeoutTimer = [Diagnostics.Stopwatch]::StartNew()
$timedOut = $false
try { $null = Wait-PostSixWayGpuHeadroom 1 400 }
catch {
    $timedOut = $true
    if ([string]$_.Exception.Message -cne 'Post-six-way candidate VRAM headroom did not converge') {
        throw
    }
}
finally { $timeoutTimer.Stop() }
if (-not $timedOut -or $timeoutTimer.Elapsed.TotalSeconds -ge 3 -or
    $script:gpuProjectionProbeCount -lt 2 -or $script:gpuProjectionJournal.Count -ne 1) {
    throw 'Post-six-way persistent low-free projection was not bounded'
}
$timeoutRecord = $script:gpuProjectionJournal[0]
$timeoutKeys = [string]::Join(',', @($timeoutRecord.Data.Keys | Sort-Object))
if ([string]$timeoutRecord.State -cne 'post_six_way_gpu_headroom_timeout' -or
    $timeoutKeys -cne 'attempts,free_mib,minimum_free_mib,timeout_seconds' -or
    [int]$timeoutRecord.Data.free_mib -ne 1400 -or
    [int]$timeoutRecord.Data.minimum_free_mib -ne 1536 -or
    [int]$timeoutRecord.Data.timeout_seconds -ne 1) {
    throw 'Timed-out post-six-way GPU journal evidence is not exact or body-free'
}

function New-ExactPublishReceipt {
    return [pscustomobject][ordered]@{
        id = 'd' * 64
        name = $script:Attested.PublishNetworkName
        driver = 'bridge'
        scope = 'local'
        internal = $false
        attachable = $false
        ingress = $false
        config_only = $false
        labels = [pscustomobject](Get-ExpectedPublishNetworkLabels)
    }
}

$record = [pscustomobject][ordered]@{
    at_utc = '2026-08-20T20:00:00.0000000Z'
    state = 'receipt_serialization_projection'
    output = [pscustomobject][ordered]@{
        publish_network = New-ExactPublishReceipt
        retained = $true
    }
}
$json = $record | ConvertTo-Json -Compress -Depth 12
$parsed = $json | ConvertFrom-Json
Assert-ExactProperties $parsed @('at_utc', 'state', 'output') 'serialized receipt record'
Assert-ExactProperties $parsed.output @('publish_network', 'retained') `
    'serialized receipt output'
Assert-PublishNetworkReceipt $parsed.output.publish_network $null
if (-not [bool]$parsed.output.retained) {
    throw 'Serialized receipt retention flag changed'
}

$expectedSerializerCounts = [ordered]@{
    'Switch-Qwen38V12Attested.ps1' = 3
    'Rollback-Qwen38V12Attested.ps1' = 4
}
foreach ($entry in $expectedSerializerCounts.GetEnumerator()) {
    $path = Join-Path $PSScriptRoot ([string]$entry.Key)
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $path,
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors.Count -ne 0) {
        throw "Receipt serializer source does not parse: $($entry.Key)"
    }
    $source = Get-Content -Raw -LiteralPath $path -Encoding utf8
    $serializers = @([regex]::Matches($source, 'ConvertTo-Json -Compress(?: -Depth [0-9]+)?'))
    if ($serializers.Count -ne [int]$entry.Value) {
        throw "Receipt serializer count changed: $($entry.Key)"
    }
    foreach ($serializer in $serializers) {
        if ([string]$serializer.Value -cne 'ConvertTo-Json -Compress -Depth 12') {
            throw "Receipt serializer lost explicit depth 12: $($entry.Key)"
        }
    }
}

'attested receipt serialization, six-way HTTP, and GPU convergence projections: PASS'
