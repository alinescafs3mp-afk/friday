$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'AttestedBundle.Common.ps1')

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

'attested receipt depth-12 serialization: PASS'
