$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'AttestedBundle.Common.ps1')

function New-Receipt {
    return [pscustomobject]@{
        engine = [pscustomobject]@{ image_id = $script:Attested.CandidateEngineImageId }
        proxy = [pscustomobject]@{ image_id = $script:Attested.CandidateProxyImageId }
    }
}

function New-PublishReceipt {
    return [pscustomobject]@{
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

function New-PublishNetwork([string]$Id = ('d' * 64)) {
    return [pscustomobject]@{
        Name = $script:Attested.PublishNetworkName
        Id = $Id
        Driver = 'bridge'
        Scope = 'local'
        EnableIPv4 = $true
        EnableIPv6 = $false
        Internal = $false
        Attachable = $false
        Ingress = $false
        ConfigOnly = $false
        ConfigFrom = [pscustomobject]@{ Network = '' }
        Options = [pscustomobject][ordered]@{
            'com.docker.network.enable_ipv4' = 'true'
            'com.docker.network.enable_ipv6' = 'false'
        }
        Labels = [pscustomobject](Get-ExpectedPublishNetworkLabels)
        Containers = [pscustomobject]@{}
    }
}

function New-State([ValidateSet('v1', 'v2')][string]$Version) {
    $state = [ordered]@{
        schema = "friday.attested-switch-state.$Version"
        profile_id = $script:Attested.ProfileId
        stable_engine_id = 'a' * 64
        stable_proxy_id = 'b' * 64
        stable_engine_image_id = $script:Attested.StableEngineImageId
        stable_proxy_image_id = $script:Attested.StableProxyImageId
        stable_engine_restart = 'unless-stopped'
        stable_proxy_restart = 'unless-stopped'
        candidate_engine_id = 'c' * 64
        candidate_proxy_id = 'd' * 64
        candidate_engine_image_id = $script:Attested.CandidateEngineImageId
        candidate_proxy_image_id = $script:Attested.CandidateProxyImageId
        key_sha256 = 'e' * 64
        written_at_utc = '2026-08-20T20:00:00.0000000Z'
    }
    if ($Version -ceq 'v2') { $state['publish_network'] = (New-PublishReceipt) }
    return [pscustomobject]$state
}

function Copy-Value([object]$Value) {
    return $Value | ConvertTo-Json -Depth 20 | ConvertFrom-Json
}

function Assert-Rejected([string]$Label, [object]$State) {
    try {
        $null = Assert-CandidateCleanupState $State (New-Receipt)
    }
    catch {
        return
    }
    throw "Unsafe cleanup state mutation was accepted: $Label"
}

function Assert-FinalNetworkRejected(
    [string]$Label,
    [object]$SealedReceipt,
    [object]$Network
) {
    try {
        $null = Get-CleanupFinalPublishNetworkReceipt `
            'friday.attested-switch-state.v2' $SealedReceipt $Network
    }
    catch {
        return
    }
    throw "Unsafe final cleanup network mutation was accepted: $Label"
}

if ((Assert-CandidateCleanupState (New-State 'v1') (New-Receipt)) -cne
    'friday.attested-switch-state.v1') {
    throw 'Exact legacy cleanup state was rejected'
}
if ((Assert-CandidateCleanupState (New-State 'v2') (New-Receipt)) -cne
    'friday.attested-switch-state.v2') {
    throw 'Exact current cleanup state was rejected'
}

$unknown = Copy-Value (New-State 'v1')
$unknown.schema = 'friday.attested-switch-state.v3'
Assert-Rejected 'unknown state schema' $unknown

$extra = Copy-Value (New-State 'v1')
$extra | Add-Member -NotePropertyName unexpected -NotePropertyValue $true
Assert-Rejected 'extra state property' $extra

$badId = Copy-Value (New-State 'v1')
$badId.candidate_proxy_id = 'near-id'
Assert-Rejected 'unbound candidate ID' $badId

$empty = Copy-Value (New-State 'v1')
$empty.candidate_engine_id = $null
$empty.candidate_proxy_id = $null
Assert-Rejected 'no candidate IDs' $empty

$wrongImage = Copy-Value (New-State 'v1')
$wrongImage.candidate_engine_image_id = 'sha256:' + ('f' * 64)
Assert-Rejected 'wrong candidate image' $wrongImage

$unsafeNetwork = Copy-Value (New-State 'v2')
$unsafeNetwork.publish_network.internal = $true
Assert-Rejected 'internal publish receipt' $unsafeNetwork

$wrongLabel = Copy-Value (New-State 'v2')
$wrongLabel.publish_network.labels.'com.friday.network.role' = 'general-egress'
Assert-Rejected 'wrong publish ownership label' $wrongLabel

$sealedPublish = New-PublishReceipt
$exactPublish = New-PublishNetwork
$preservedPublish = Get-CleanupFinalPublishNetworkReceipt `
    'friday.attested-switch-state.v2' $sealedPublish $exactPublish
if ([string]$preservedPublish.id -cne [string]$sealedPublish.id) {
    throw 'Final v2 cleanup did not preserve its sealed publish network receipt'
}
$differentIdPublish = New-PublishNetwork ('f' * 64)
Assert-FinalNetworkRejected 'post-removal different live network ID' `
    $sealedPublish $differentIdPublish
$adoptedV1Publish = Get-CleanupFinalPublishNetworkReceipt `
    'friday.attested-switch-state.v1' $null $differentIdPublish
if ([string]$adoptedV1Publish.id -cne [string]$differentIdPublish.Id) {
    throw 'Final v1 cleanup did not adopt the exact live publish network'
}

'attested stopped-candidate cleanup projection: PASS'
