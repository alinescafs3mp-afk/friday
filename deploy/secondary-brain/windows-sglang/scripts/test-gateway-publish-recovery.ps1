[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'gateway-publish-recovery.ps1') -LibraryOnly

$script:caseCount = 0
$script:bindAddress = '192.168.1.35'

function Assert-FridayTrue {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][bool]$Value)
    if (-not $Value) { throw "Case '$Name' unexpectedly evaluated false." }
    $script:caseCount += 1
}

function Assert-FridayEqual {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter()][AllowNull()][object]$Expected,
        [Parameter()][AllowNull()][object]$Observed
    )
    if ($Expected -cne $Observed) {
        throw "Case '$Name' expected '$Expected', observed '$Observed'."
    }
    $script:caseCount += 1
}

function Assert-FridayThrows {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][scriptblock]$Action)
    $threw = $false
    try { $null = & $Action } catch { $threw = $true }
    if (-not $threw) { throw "Case '$Name' did not fail closed." }
    $script:caseCount += 1
}

function New-FridayExactPortMap {
    param([string]$HostIp = '192.168.1.35')
    return [pscustomobject][ordered]@{
        '8443/tcp' = @(
            [pscustomobject][ordered]@{ HostIp = $HostIp; HostPort = '8443' }
        )
    }
}

function New-FridayGatewayFixture {
    return [pscustomobject][ordered]@{
        Id = ('a' * 64)
        Name = '/friday-secondary-gateway'
        Config = [pscustomobject][ordered]@{
            Labels = [pscustomobject][ordered]@{
                'com.docker.compose.service' = 'gateway'
            }
        }
        HostConfig = [pscustomobject][ordered]@{
            PortBindings = New-FridayExactPortMap
        }
        State = [pscustomobject][ordered]@{
            Running = $true
            Status = 'running'
            Health = [pscustomobject][ordered]@{ Status = 'healthy' }
        }
        NetworkSettings = [pscustomobject][ordered]@{
            Ports = New-FridayExactPortMap
        }
    }
}

$listenerCalls = 0
$assessment = Get-FridayGatewayRecoveryAssessment `
    -Container (New-FridayGatewayFixture) `
    -ExpectedBindAddress $script:bindAddress `
    -TestListener { $script:listenerCalls += 1; $true }
Assert-FridayEqual 'exact publication is healthy' 'healthy' $assessment.state
Assert-FridayEqual 'healthy assessment probes listener once' 1 $script:listenerCalls

foreach ($missingPorts in @($null, @(), ([pscustomobject]@{}))) {
    $fixture = New-FridayGatewayFixture
    $fixture.NetworkSettings.Ports = $missingPorts
    $assessment = Get-FridayGatewayRecoveryAssessment `
        -Container $fixture `
        -ExpectedBindAddress $script:bindAddress `
        -TestListener { $false }
    Assert-FridayEqual 'empty effective publication is recoverable evidence' 'recover' $assessment.state
}

$fixture = New-FridayGatewayFixture
$fixture.NetworkSettings.Ports = '{"8443/tcp":[]}' | ConvertFrom-Json -ErrorAction Stop
$assessment = Get-FridayGatewayRecoveryAssessment `
    -Container $fixture `
    -ExpectedBindAddress $script:bindAddress `
    -TestListener { $false }
Assert-FridayEqual 'empty effective binding array is recoverable evidence' 'recover' $assessment.state

Assert-FridayThrows 'empty configured binding array fails closed' {
    $fixture = New-FridayGatewayFixture
    $fixture.HostConfig.PortBindings = [pscustomobject][ordered]@{ '8443/tcp' = @() }
    Get-FridayGatewayRecoveryAssessment $fixture $script:bindAddress { $false }
}

$fixture = New-FridayGatewayFixture
$fixture.NetworkSettings.Ports = [pscustomobject]@{}
$assessment = Get-FridayGatewayRecoveryAssessment `
    -Container $fixture `
    -ExpectedBindAddress $script:bindAddress `
    -TestListener { $true }
Assert-FridayEqual 'listener without publication is inconsistent' 'inconsistent' $assessment.state

$fixture = New-FridayGatewayFixture
$assessment = Get-FridayGatewayRecoveryAssessment `
    -Container $fixture `
    -ExpectedBindAddress $script:bindAddress `
    -TestListener { $false }
Assert-FridayEqual 'publication without listener is inconsistent' 'inconsistent' $assessment.state

$script:listenerCalls = 0
$fixture = New-FridayGatewayFixture
$fixture.State.Health.Status = 'starting'
$assessment = Get-FridayGatewayRecoveryAssessment `
    -Container $fixture `
    -ExpectedBindAddress $script:bindAddress `
    -TestListener { $script:listenerCalls += 1; $true }
Assert-FridayEqual 'starting gateway is only waited for' 'wait' $assessment.state
Assert-FridayEqual 'starting gateway is not probed' 0 $script:listenerCalls

Assert-FridayThrows 'wrong container name' {
    $fixture = New-FridayGatewayFixture
    $fixture.Name = '/friday-secondary-gateway-lookalike'
    Get-FridayGatewayRecoveryAssessment $fixture $script:bindAddress { $false }
}
Assert-FridayThrows 'wrong compose service ownership' {
    $fixture = New-FridayGatewayFixture
    $fixture.Config.Labels.'com.docker.compose.service' = 'engine'
    Get-FridayGatewayRecoveryAssessment $fixture $script:bindAddress { $false }
}
Assert-FridayThrows 'wrong configured bind IP' {
    $fixture = New-FridayGatewayFixture
    $fixture.HostConfig.PortBindings = New-FridayExactPortMap '192.168.1.36'
    Get-FridayGatewayRecoveryAssessment $fixture $script:bindAddress { $false }
}
Assert-FridayThrows 'extra configured binding' {
    $fixture = New-FridayGatewayFixture
    $fixture.HostConfig.PortBindings | Add-Member `
        -NotePropertyName '9443/tcp' `
        -NotePropertyValue @([pscustomobject]@{ HostIp = $script:bindAddress; HostPort = '9443' })
    Get-FridayGatewayRecoveryAssessment $fixture $script:bindAddress { $false }
}
Assert-FridayThrows 'extra binding row' {
    $fixture = New-FridayGatewayFixture
    $fixture.HostConfig.PortBindings.'8443/tcp' +=
        [pscustomobject]@{ HostIp = $script:bindAddress; HostPort = '8443' }
    Get-FridayGatewayRecoveryAssessment $fixture $script:bindAddress { $false }
}
Assert-FridayThrows 'malformed effective publication' {
    $fixture = New-FridayGatewayFixture
    $fixture.NetworkSettings.Ports = @([pscustomobject]@{ unsafe = $true })
    Get-FridayGatewayRecoveryAssessment $fixture $script:bindAddress { $false }
}
Assert-FridayThrows 'non-Boolean listener result' {
    Get-FridayGatewayRecoveryAssessment `
        (New-FridayGatewayFixture) $script:bindAddress { 'yes' }
}

foreach ($unsafeAddress in @(
    '192.168.001.35', '192.168.1.35 ', '127.0.0.1', '169.254.1.1',
    '8.8.8.8', '::1', 'not-an-address'
)) {
    Assert-FridayThrows ('unsafe bind address ' + $unsafeAddress) {
        Get-FridayCanonicalPrivateIPv4 $unsafeAddress
    }
}

$script:inspectionCalls = 0
$script:restartCalls = 0
$script:sleepCalls = 0
$result = Invoke-FridayGatewayRecoveryLoop `
    -ExpectedBindAddress $script:bindAddress `
    -ReadinessAttempts 4 `
    -PostRestartAttempts 2 `
    -RetryDelaySeconds 1 `
    -TestLanReady { $true } `
    -TestDockerReady { $true } `
    -GetContainer {
        $script:inspectionCalls += 1
        $fixture = New-FridayGatewayFixture
        if ($script:inspectionCalls -le 2) {
            $fixture.NetworkSettings.Ports = [pscustomobject]@{}
        }
        $fixture
    } `
    -TestListener { [bool]($script:inspectionCalls -gt 2) } `
    -RestartGateway { $script:restartCalls += 1; $true } `
    -Sleep { param([int]$Seconds) $script:sleepCalls += 1 }
Assert-FridayEqual 'confirmed failure recovers' 'recovered' $result.outcome
Assert-FridayEqual 'confirmed failure restarts exactly once' 1 $script:restartCalls
Assert-FridayEqual 'recovery records exactly one restart' 1 $result.restarts
Assert-FridayEqual 'two missing snapshots are required' 3 $script:inspectionCalls
Assert-FridayEqual 'confirmation delay is bounded' 1 $script:sleepCalls

$script:restartCalls = 0
$script:sleepCalls = 0
$result = Invoke-FridayGatewayRecoveryLoop `
    -ExpectedBindAddress $script:bindAddress `
    -ReadinessAttempts 2 `
    -PostRestartAttempts 1 `
    -RetryDelaySeconds 1 `
    -TestLanReady { $true } `
    -TestDockerReady { $true } `
    -GetContainer { New-FridayGatewayFixture } `
    -TestListener { $true } `
    -RestartGateway { $script:restartCalls += 1; $true } `
    -Sleep { param([int]$Seconds) $script:sleepCalls += 1 }
Assert-FridayEqual 'healthy endpoint is a no-op' 'already_healthy' $result.outcome
Assert-FridayEqual 'healthy endpoint is never restarted' 0 $script:restartCalls
Assert-FridayEqual 'healthy endpoint does not sleep' 0 $script:sleepCalls

$script:restartCalls = 0
Assert-FridayThrows 'inconsistent initial evidence never restarts' {
    Invoke-FridayGatewayRecoveryLoop `
        -ExpectedBindAddress $script:bindAddress `
        -ReadinessAttempts 2 `
        -PostRestartAttempts 1 `
        -RetryDelaySeconds 1 `
        -TestLanReady { $true } `
        -TestDockerReady { $true } `
        -GetContainer {
            $fixture = New-FridayGatewayFixture
            $fixture.NetworkSettings.Ports = [pscustomobject]@{}
            $fixture
        } `
        -TestListener { $true } `
        -RestartGateway { $script:restartCalls += 1; $true } `
        -Sleep { param([int]$Seconds) }
}
Assert-FridayEqual 'inconsistent evidence restart count' 0 $script:restartCalls

$script:restartCalls = 0
$script:dockerCalls = 0
$script:sleepCalls = 0
Assert-FridayThrows 'LAN wait is bounded' {
    Invoke-FridayGatewayRecoveryLoop `
        -ExpectedBindAddress $script:bindAddress `
        -ReadinessAttempts 3 `
        -PostRestartAttempts 1 `
        -RetryDelaySeconds 1 `
        -TestLanReady { $false } `
        -TestDockerReady { $script:dockerCalls += 1; $true } `
        -GetContainer { throw 'must not inspect' } `
        -TestListener { throw 'must not probe' } `
        -RestartGateway { $script:restartCalls += 1; $true } `
        -Sleep { param([int]$Seconds) $script:sleepCalls += 1 }
}
Assert-FridayEqual 'LAN wait never reaches Docker' 0 $script:dockerCalls
Assert-FridayEqual 'LAN wait never restarts' 0 $script:restartCalls
Assert-FridayEqual 'LAN wait sleep count is bounded' 2 $script:sleepCalls

$script:restartCalls = 0
$script:sleepCalls = 0
Assert-FridayThrows 'post-restart verification is bounded without a second restart' {
    Invoke-FridayGatewayRecoveryLoop `
        -ExpectedBindAddress $script:bindAddress `
        -ReadinessAttempts 2 `
        -PostRestartAttempts 2 `
        -RetryDelaySeconds 1 `
        -TestLanReady { $true } `
        -TestDockerReady { $true } `
        -GetContainer {
            $fixture = New-FridayGatewayFixture
            $fixture.NetworkSettings.Ports = [pscustomobject]@{}
            $fixture
        } `
        -TestListener { $false } `
        -RestartGateway { $script:restartCalls += 1; $true } `
        -Sleep { param([int]$Seconds) $script:sleepCalls += 1 }
}
Assert-FridayEqual 'failed verification still has one restart' 1 $script:restartCalls
Assert-FridayEqual 'all retry sleeps are bounded' 2 $script:sleepCalls

"gateway publish recovery projection: PASS ($script:caseCount cases)"
