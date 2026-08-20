$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'AttestedBundle.Common.ps1')

function New-Endpoint([string]$NetworkId, [string]$Address, [int]$GwPriority) {
    return [pscustomobject]@{
        NetworkID = $NetworkId
        IPAddress = $Address
        GwPriority = $GwPriority
    }
}

function New-Container(
    [string]$Id,
    [string]$Name,
    [object]$Networks
) {
    return [pscustomobject]@{
        Id = $Id
        Name = "/$Name"
        State = [pscustomobject]@{ Running = $true }
        NetworkSettings = [pscustomobject]@{ Networks = [pscustomobject]$Networks }
    }
}

function New-Network(
    [string]$Name,
    [string]$Id,
    [bool]$Internal,
    [object]$Labels,
    [object]$Containers
) {
    return [pscustomobject]@{
        Name = $Name
        Id = $Id
        Driver = 'bridge'
        Scope = 'local'
        EnableIPv4 = $true
        EnableIPv6 = $false
        Internal = $Internal
        Attachable = $false
        Ingress = $false
        ConfigOnly = $false
        ConfigFrom = [pscustomobject]@{ Network = '' }
        Options = [pscustomobject][ordered]@{
            'com.docker.network.enable_ipv4' = 'true'
            'com.docker.network.enable_ipv6' = 'false'
        }
        Labels = $Labels
        Containers = [pscustomobject]$Containers
    }
}

function New-ExactTopology {
    $engineId = 'a' * 64
    $proxyId = 'b' * 64
    $attestedId = 'c' * 64
    $publishId = 'd' * 64
    $engineName = $script:Attested.CandidateEngineName
    $proxyName = $script:Attested.CandidateProxyName
    $engineNetworks = [ordered]@{
        $script:Attested.AttestedNetworkName = New-Endpoint $attestedId '172.31.0.2' 0
    }
    $proxyNetworks = [ordered]@{
        $script:Attested.AttestedNetworkName = New-Endpoint $attestedId '172.31.0.3' 0
        $script:Attested.PublishNetworkName = New-Endpoint $publishId '172.32.0.2' 1
    }
    $engine = New-Container $engineId $engineName $engineNetworks
    $proxy = New-Container $proxyId $proxyName $proxyNetworks
    $attestedLabels = [pscustomobject][ordered]@{
        'com.docker.compose.config-hash' = $script:Attested.AttestedNetworkConfigHash
        'com.docker.compose.network' = 'attested'
        'com.docker.compose.project' = $script:Attested.ComposeProject
        'com.docker.compose.version' = $script:Attested.ComposeVersion
    }
    $publishLabels = [pscustomobject](Get-ExpectedPublishNetworkLabels)
    $attestedContainers = [ordered]@{
        $engineId = [pscustomobject]@{ Name = $engineName; IPv4Address = '172.31.0.2/16' }
        $proxyId = [pscustomobject]@{ Name = $proxyName; IPv4Address = '172.31.0.3/16' }
    }
    $publishContainers = [ordered]@{
        $proxyId = [pscustomobject]@{ Name = $proxyName; IPv4Address = '172.32.0.2/16' }
    }
    return [pscustomobject]@{
        Engine = $engine
        Proxy = $proxy
        Attested = New-Network $script:Attested.AttestedNetworkName $attestedId $true `
            $attestedLabels $attestedContainers
        Publish = New-Network $script:Attested.PublishNetworkName $publishId $false `
            $publishLabels $publishContainers
    }
}

function Assert-Topology([object]$Graph) {
    Assert-CandidateNetworkTopology $Graph.Engine $Graph.Proxy $Graph.Attested $Graph.Publish
}

function Assert-Rejected([string]$Label, [scriptblock]$Mutation) {
    $graph = New-ExactTopology
    & $Mutation $graph
    try {
        Assert-Topology $graph
    }
    catch {
        return
    }
    throw "Unsafe network topology mutation was accepted: $Label"
}

$exact = New-ExactTopology
Assert-Topology $exact
$publishReceipt = Get-PublishNetworkReceipt $exact.Publish
Assert-PublishNetworkReceipt $publishReceipt $exact.Publish
$emptyPublish = New-ExactTopology
$emptyPublish.Publish.Containers = [pscustomobject]@{}
Assert-NetworkContainerProjection $emptyPublish.Publish @() @() 'empty publish projection'
Assert-NoHostPortBindings ([pscustomobject]@{
    HostConfig = [pscustomobject]@{ PortBindings = [pscustomobject]@{} }
}) 'unpublished projection'

Assert-Rejected 'missing publish network' {
    param($graph)
    $graph.Proxy.NetworkSettings.Networks.PSObject.Properties.Remove(
        $script:Attested.PublishNetworkName
    )
}
Assert-Rejected 'extra proxy network' {
    param($graph)
    $graph.Proxy.NetworkSettings.Networks | Add-Member -NotePropertyName 'foreign-net' `
        -NotePropertyValue (New-Endpoint ('e' * 64) '172.33.0.2' 0)
}
Assert-Rejected 'swapped network identities' {
    param($graph)
    $graph.Proxy.NetworkSettings.Networks.($script:Attested.AttestedNetworkName).NetworkID = `
        $graph.Publish.Id
    $graph.Proxy.NetworkSettings.Networks.($script:Attested.PublishNetworkName).NetworkID = `
        $graph.Attested.Id
}
Assert-Rejected 'attested network is not internal' {
    param($graph)
    $graph.Attested.Internal = $false
}
Assert-Rejected 'publish network became internal' {
    param($graph)
    $graph.Publish.Internal = $true
}
Assert-Rejected 'engine attached to publish ingress' {
    param($graph)
    $graph.Engine.NetworkSettings.Networks | Add-Member `
        -NotePropertyName $script:Attested.PublishNetworkName `
        -NotePropertyValue (New-Endpoint $graph.Publish.Id '172.32.0.3' 1)
}
Assert-Rejected 'publish gateway priority lost' {
    param($graph)
    $graph.Proxy.NetworkSettings.Networks.($script:Attested.PublishNetworkName).GwPriority = 0
}
Assert-Rejected 'publish network driver changed' {
    param($graph)
    $graph.Publish.Driver = 'overlay'
}
Assert-Rejected 'publish ownership label changed' {
    param($graph)
    $graph.Publish.Labels.'com.friday.network.role' = 'general-egress'
}
Assert-Rejected 'foreign publish attachment' {
    param($graph)
    $graph.Publish.Containers | Add-Member -NotePropertyName ('e' * 64) `
        -NotePropertyValue ([pscustomobject]@{ Name = 'foreign'; IPv4Address = '172.32.0.3/16' })
}

'attested network topology projection: PASS'
