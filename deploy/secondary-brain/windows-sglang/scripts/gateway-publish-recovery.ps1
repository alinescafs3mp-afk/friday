[CmdletBinding()]
param(
    [Parameter()][string]$ExpectedBindAddress = '',
    [Parameter()][string]$DockerPath = 'docker.exe',
    [Parameter()][ValidateRange(2, 90)][int]$ReadinessAttempts = 60,
    [Parameter()][ValidateRange(1, 60)][int]$PostRestartAttempts = 24,
    [Parameter()][ValidateRange(1, 30)][int]$RetryDelaySeconds = 5,
    [Parameter()][ValidateRange(100, 5000)][int]$ListenerTimeoutMilliseconds = 1000,
    [Parameter()][switch]$InstallAtLogon,
    [Parameter(DontShow = $true)][switch]$LibraryOnly
)

Set-StrictMode -Version 2.0

$script:GatewayContainerName = 'friday-secondary-gateway'
$script:GatewayContainerPort = '8443/tcp'
$script:GatewayHostPort = '8443'
$script:RecoveryTaskName = 'Friday Secondary Gateway Publish Recovery'
$script:MissingConfirmations = 2

function Get-FridayRequiredProperty {
    param(
        [Parameter(Mandatory = $true)][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter()][switch]$AllowNull
    )

    if ($null -eq $InputObject -or $InputObject -is [array]) {
        throw "Required object for property '$Name' is malformed."
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property -or ($null -eq $property.Value -and -not $AllowNull)) {
        throw "Required property '$Name' is absent."
    }
    return $property.Value
}

function Get-FridayCanonicalPrivateIPv4 {
    param([Parameter(Mandatory = $true)][string]$Address)

    if ([string]::IsNullOrWhiteSpace($Address) -or $Address -cne $Address.Trim()) {
        throw 'Expected bind address must be one canonical private IPv4 address.'
    }
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$parsed) -or
        $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        $parsed.ToString() -cne $Address) {
        throw 'Expected bind address must be one canonical private IPv4 address.'
    }
    $octets = $parsed.GetAddressBytes()
    $isPrivate = $octets[0] -eq 10 -or
        ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31) -or
        ($octets[0] -eq 192 -and $octets[1] -eq 168)
    if (-not $isPrivate) {
        throw 'Expected bind address must be one canonical private IPv4 address.'
    }
    return $parsed
}

function Get-FridayExactPortMapState {
    param(
        [Parameter()][AllowNull()][object]$Map,
        [Parameter(Mandatory = $true)][string]$ExpectedBindAddress,
        [Parameter()][switch]$AllowProvablyMissing,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $null = Get-FridayCanonicalPrivateIPv4 $ExpectedBindAddress
    if ($null -eq $Map) {
        if ($AllowProvablyMissing) { return 'missing' }
        throw "$Label is absent."
    }
    if ($Map -is [array]) {
        if ($AllowProvablyMissing -and @($Map).Count -eq 0) { return 'missing' }
        throw "$Label is not a port-binding object."
    }

    $names = @($Map.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($names.Count -eq 0 -and $AllowProvablyMissing) { return 'missing' }
    if ($names.Count -ne 1 -or $names[0] -cne $script:GatewayContainerPort) {
        throw "$Label key set is not exact."
    }

    $bindingProperty = $Map.PSObject.Properties[$script:GatewayContainerPort]
    if ($null -eq $bindingProperty -or $null -eq $bindingProperty.Value) {
        if ($AllowProvablyMissing) { return 'missing' }
        throw "$Label binding is absent."
    }
    $rows = @($bindingProperty.Value)
    if ($rows.Count -ne 1 -or $null -eq $rows[0] -or $rows[0] -is [array]) {
        throw "$Label cardinality is not exact."
    }
    $rowNames = @($rows[0].PSObject.Properties | ForEach-Object { [string]$_.Name } | Sort-Object)
    if ($rowNames.Count -ne 2 -or
        $rowNames[0] -cne 'HostIp' -or $rowNames[1] -cne 'HostPort') {
        throw "$Label row shape is not exact."
    }
    $hostIp = Get-FridayRequiredProperty $rows[0] 'HostIp'
    $hostPort = Get-FridayRequiredProperty $rows[0] 'HostPort'
    if ($hostIp -isnot [string] -or $hostPort -isnot [string] -or
        [string]$hostIp -cne $ExpectedBindAddress -or
        [string]$hostPort -cne $script:GatewayHostPort) {
        throw "$Label endpoint is not exact."
    }
    return 'published'
}

function Get-FridayGatewayRecoveryAssessment {
    param(
        [Parameter(Mandatory = $true)][object]$Container,
        [Parameter(Mandatory = $true)][string]$ExpectedBindAddress,
        [Parameter(Mandatory = $true)][scriptblock]$TestListener
    )

    $null = Get-FridayCanonicalPrivateIPv4 $ExpectedBindAddress
    if ($Container -is [array]) { throw 'Gateway inspection is not one object.' }

    $id = Get-FridayRequiredProperty $Container 'Id'
    $name = Get-FridayRequiredProperty $Container 'Name'
    if ($id -isnot [string] -or [string]$id -cnotmatch '\A[0-9a-f]{64}\z' -or
        $name -isnot [string] -or [string]$name -cne ('/' + $script:GatewayContainerName)) {
        throw 'Gateway container identity is not exact.'
    }

    $config = Get-FridayRequiredProperty $Container 'Config'
    $labels = Get-FridayRequiredProperty $config 'Labels'
    $service = Get-FridayRequiredProperty $labels 'com.docker.compose.service'
    if ($service -isnot [string] -or [string]$service -cne 'gateway') {
        throw 'Gateway ownership label is not exact.'
    }

    $hostConfig = Get-FridayRequiredProperty $Container 'HostConfig'
    $configuredBindings = Get-FridayRequiredProperty $hostConfig 'PortBindings'
    $configuredState = Get-FridayExactPortMapState `
        -Map $configuredBindings `
        -ExpectedBindAddress $ExpectedBindAddress `
        -Label 'Configured gateway port binding'
    if ($configuredState -cne 'published') {
        throw 'Configured gateway port binding is not exact.'
    }

    $state = Get-FridayRequiredProperty $Container 'State'
    $running = Get-FridayRequiredProperty $state 'Running'
    $status = Get-FridayRequiredProperty $state 'Status'
    $health = Get-FridayRequiredProperty $state 'Health'
    $healthStatus = Get-FridayRequiredProperty $health 'Status'
    if ($running -isnot [bool] -or $status -isnot [string] -or $healthStatus -isnot [string]) {
        throw 'Gateway runtime state is malformed.'
    }
    if (-not [bool]$running -or [string]$status -cne 'running' -or
        [string]$healthStatus -cne 'healthy') {
        return [pscustomobject][ordered]@{ state = 'wait'; reason = 'gateway_not_healthy' }
    }

    $networkSettings = Get-FridayRequiredProperty $Container 'NetworkSettings'
    $portsProperty = $networkSettings.PSObject.Properties['Ports']
    if ($null -eq $portsProperty) {
        throw 'Effective gateway port publication property is absent.'
    }
    $effectiveState = Get-FridayExactPortMapState `
        -Map $portsProperty.Value `
        -ExpectedBindAddress $ExpectedBindAddress `
        -AllowProvablyMissing `
        -Label 'Effective gateway port publication'

    $listenerResults = @(& $TestListener)
    if ($listenerResults.Count -ne 1 -or $listenerResults[0] -isnot [bool]) {
        throw 'TCP listener probe did not return one Boolean result.'
    }
    $listenerPresent = [bool]$listenerResults[0]
    if ($effectiveState -ceq 'published' -and $listenerPresent) {
        return [pscustomobject][ordered]@{ state = 'healthy'; reason = 'publication_and_listener_present' }
    }
    if ($effectiveState -ceq 'missing' -and -not $listenerPresent) {
        return [pscustomobject][ordered]@{ state = 'recover'; reason = 'publication_and_listener_missing' }
    }
    return [pscustomobject][ordered]@{ state = 'inconsistent'; reason = 'publication_listener_disagree' }
}

function Invoke-FridayBooleanProbe {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Probe,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $results = @(& $Probe)
    if ($results.Count -ne 1 -or $results[0] -isnot [bool]) {
        throw "$Label did not return one Boolean result."
    }
    return [bool]$results[0]
}

function Invoke-FridayGatewayRecoveryLoop {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedBindAddress,
        [Parameter(Mandatory = $true)][ValidateRange(2, 90)][int]$ReadinessAttempts,
        [Parameter(Mandatory = $true)][ValidateRange(1, 60)][int]$PostRestartAttempts,
        [Parameter(Mandatory = $true)][ValidateRange(1, 30)][int]$RetryDelaySeconds,
        [Parameter(Mandatory = $true)][scriptblock]$TestLanReady,
        [Parameter(Mandatory = $true)][scriptblock]$TestDockerReady,
        [Parameter(Mandatory = $true)][scriptblock]$GetContainer,
        [Parameter(Mandatory = $true)][scriptblock]$TestListener,
        [Parameter(Mandatory = $true)][scriptblock]$RestartGateway,
        [Parameter(Mandatory = $true)][scriptblock]$Sleep
    )

    $null = Get-FridayCanonicalPrivateIPv4 $ExpectedBindAddress
    $missingConfirmations = 0
    $restartPerformed = $false
    for ($attempt = 1; $attempt -le $ReadinessAttempts; $attempt += 1) {
        $lanReady = Invoke-FridayBooleanProbe $TestLanReady 'LAN readiness probe'
        $dockerReady = $false
        if ($lanReady) {
            $dockerReady = Invoke-FridayBooleanProbe $TestDockerReady 'Docker readiness probe'
        }

        if (-not $lanReady -or -not $dockerReady) {
            $missingConfirmations = 0
        } else {
            $containerResults = @(& $GetContainer)
            if ($containerResults.Count -ne 1 -or $null -eq $containerResults[0] -or
                $containerResults[0] -is [array]) {
                throw 'Exact gateway container inspection did not return one object.'
            }
            $assessment = Get-FridayGatewayRecoveryAssessment `
                -Container $containerResults[0] `
                -ExpectedBindAddress $ExpectedBindAddress `
                -TestListener $TestListener
            if ([string]$assessment.state -ceq 'healthy') {
                return [pscustomobject][ordered]@{
                    outcome = 'already_healthy'
                    readiness_attempts = $attempt
                    post_restart_attempts = 0
                    restarts = 0
                }
            }
            if ([string]$assessment.state -ceq 'inconsistent') {
                throw 'Gateway publication evidence is inconsistent; refusing recovery.'
            }
            if ([string]$assessment.state -ceq 'recover') {
                $missingConfirmations += 1
                if ($missingConfirmations -ge $script:MissingConfirmations) {
                    $restartResults = @(& $RestartGateway)
                    if ($restartResults.Count -ne 1 -or $restartResults[0] -isnot [bool] -or
                        -not [bool]$restartResults[0]) {
                        throw 'Exact gateway restart did not report success.'
                    }
                    $restartPerformed = $true
                    break
                }
            } elseif ([string]$assessment.state -ceq 'wait') {
                $missingConfirmations = 0
            } else {
                throw 'Gateway assessment returned an unknown state.'
            }
        }

        if ($attempt -lt $ReadinessAttempts) {
            $null = & $Sleep $RetryDelaySeconds
        }
    }
    if (-not $restartPerformed) {
        throw 'Gateway recovery readiness window expired without an authorized action.'
    }

    for ($postAttempt = 1; $postAttempt -le $PostRestartAttempts; $postAttempt += 1) {
        $lanReady = Invoke-FridayBooleanProbe $TestLanReady 'LAN readiness probe'
        $dockerReady = $false
        if ($lanReady) {
            $dockerReady = Invoke-FridayBooleanProbe $TestDockerReady 'Docker readiness probe'
        }
        if ($lanReady -and $dockerReady) {
            $containerResults = @(& $GetContainer)
            if ($containerResults.Count -ne 1 -or $null -eq $containerResults[0] -or
                $containerResults[0] -is [array]) {
                throw 'Exact gateway container inspection did not return one object.'
            }
            $assessment = Get-FridayGatewayRecoveryAssessment `
                -Container $containerResults[0] `
                -ExpectedBindAddress $ExpectedBindAddress `
                -TestListener $TestListener
            if ([string]$assessment.state -ceq 'healthy') {
                return [pscustomobject][ordered]@{
                    outcome = 'recovered'
                    readiness_attempts = $attempt
                    post_restart_attempts = $postAttempt
                    restarts = 1
                }
            }
        }
        if ($postAttempt -lt $PostRestartAttempts) {
            $null = & $Sleep $RetryDelaySeconds
        }
    }
    throw 'Gateway publication did not recover inside the bounded verification window.'
}

function Resolve-FridayDockerExecutable {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    if ([string]::IsNullOrWhiteSpace($Candidate) -or $Candidate.IndexOf('"') -ge 0) {
        throw 'Docker executable path is malformed.'
    }
    $commands = @(Get-Command -Name $Candidate -CommandType Application -ErrorAction Stop)
    if ($commands.Count -ne 1) { throw 'Docker executable resolution is ambiguous.' }
    $resolved = [string]$commands[0].Source
    if ([string]::IsNullOrWhiteSpace($resolved) -or
        -not [string]::Equals([IO.Path]::GetFileName($resolved), 'docker.exe',
            [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw 'Resolved Docker executable is not exact.'
    }
    return [IO.Path]::GetFullPath($resolved)
}

function Invoke-FridayDockerText {
    param(
        [Parameter(Mandatory = $true)][string]$ResolvedDockerPath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    $lines = @(& $ResolvedDockerPath @Arguments 2>$null)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw $FailureMessage }
    return [string]::Join([Environment]::NewLine, @($lines | ForEach-Object { [string]$_ }))
}

function Test-FridayDockerReady {
    param([Parameter(Mandatory = $true)][string]$ResolvedDockerPath)

    try {
        $version = Invoke-FridayDockerText `
            -ResolvedDockerPath $ResolvedDockerPath `
            -Arguments @('version', '--format', '{{.Server.APIVersion}}') `
            -FailureMessage 'Docker server is not ready.'
        return [bool]($version -cmatch '\A[0-9]+\.[0-9]+\z')
    } catch {
        return $false
    }
}

function Get-FridayGatewayContainer {
    param([Parameter(Mandatory = $true)][string]$ResolvedDockerPath)

    $idsText = Invoke-FridayDockerText `
        -ResolvedDockerPath $ResolvedDockerPath `
        -Arguments @('ps', '--all', '--quiet', '--no-trunc', '--filter',
            ('name=^/' + $script:GatewayContainerName + '$')) `
        -FailureMessage 'Exact gateway container lookup failed.'
    $ids = @($idsText -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($ids.Count -ne 1 -or [string]$ids[0] -cnotmatch '\A[0-9a-f]{64}\z') {
        throw 'Exact gateway container lookup is absent or ambiguous.'
    }
    $inspectText = Invoke-FridayDockerText `
        -ResolvedDockerPath $ResolvedDockerPath `
        -Arguments @('inspect', [string]$ids[0]) `
        -FailureMessage 'Exact gateway container inspection failed.'
    try {
        $items = @($inspectText | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        throw 'Exact gateway container inspection returned malformed JSON.'
    }
    if ($items.Count -ne 1 -or $null -eq $items[0] -or
        [string]$items[0].Id -cne [string]$ids[0]) {
        throw 'Exact gateway container inspection is inconsistent.'
    }
    return $items[0]
}

function Restart-FridayGatewayContainer {
    param([Parameter(Mandatory = $true)][string]$ResolvedDockerPath)

    $result = Invoke-FridayDockerText `
        -ResolvedDockerPath $ResolvedDockerPath `
        -Arguments @('restart', '--time', '30', $script:GatewayContainerName) `
        -FailureMessage 'Exact gateway restart failed.'
    return [bool]($result.Trim() -ceq $script:GatewayContainerName)
}

function Test-FridayLanAddressReady {
    param([Parameter(Mandatory = $true)][string]$ExpectedBindAddress)

    $expected = Get-FridayCanonicalPrivateIPv4 $ExpectedBindAddress
    foreach ($adapter in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($adapter.OperationalStatus -ne [Net.NetworkInformation.OperationalStatus]::Up -or
            $adapter.NetworkInterfaceType -eq [Net.NetworkInformation.NetworkInterfaceType]::Loopback) {
            continue
        }
        foreach ($unicast in $adapter.GetIPProperties().UnicastAddresses) {
            if ($unicast.Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and
                $unicast.Address.Equals($expected)) {
                return $true
            }
        }
    }
    return $false
}

function Test-FridayTcpListener {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedBindAddress,
        [Parameter(Mandatory = $true)][ValidateRange(100, 5000)][int]$TimeoutMilliseconds
    )

    $address = Get-FridayCanonicalPrivateIPv4 $ExpectedBindAddress
    $client = $null
    $waitHandle = $null
    try {
        $client = New-Object Net.Sockets.TcpClient -ArgumentList (
            [Net.Sockets.AddressFamily]::InterNetwork
        )
        $async = $client.BeginConnect($address, [int]$script:GatewayHostPort, $null, $null)
        $waitHandle = $async.AsyncWaitHandle
        if (-not $waitHandle.WaitOne($TimeoutMilliseconds, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch [Net.Sockets.SocketException] {
        return $false
    } finally {
        if ($null -ne $waitHandle) { $waitHandle.Close() }
        if ($null -ne $client) { $client.Close() }
    }
}

function ConvertTo-FridayQuotedTaskArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.IndexOf('"') -ge 0 -or $Value.EndsWith('\', [StringComparison]::Ordinal)) {
        throw 'Scheduled-task argument cannot be quoted safely.'
    }
    return '"' + $Value + '"'
}

function Install-FridayGatewayRecoveryAtLogon {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string]$ExpectedBindAddress,
        [Parameter(Mandatory = $true)][string]$ResolvedDockerPath,
        [Parameter(Mandatory = $true)][ValidateRange(2, 90)][int]$ReadinessAttempts,
        [Parameter(Mandatory = $true)][ValidateRange(1, 60)][int]$PostRestartAttempts,
        [Parameter(Mandatory = $true)][ValidateRange(1, 30)][int]$RetryDelaySeconds,
        [Parameter(Mandatory = $true)][ValidateRange(100, 5000)][int]$ListenerTimeoutMilliseconds
    )

    $null = Get-FridayCanonicalPrivateIPv4 $ExpectedBindAddress
    $resolvedScript = [IO.Path]::GetFullPath($ScriptPath)
    if (-not (Test-Path -LiteralPath $resolvedScript -PathType Leaf) -or
        [IO.Path]::GetFileName($resolvedScript) -cne 'gateway-publish-recovery.ps1') {
        throw 'Recovery script path is not exact.'
    }
    $windowsPowerShell = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) {
        throw 'Windows PowerShell executable is unavailable.'
    }
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ([string]::IsNullOrWhiteSpace($currentUser)) { throw 'Current Windows identity is unavailable.' }

    $existing = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
        [string]::Equals([string]$_.TaskName, $script:RecoveryTaskName,
            [StringComparison]::OrdinalIgnoreCase)
    })
    if ($existing.Count -ne 0) {
        throw 'Recovery scheduled task already exists; refusing to overwrite it.'
    }

    $taskArguments = @(
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-File', (ConvertTo-FridayQuotedTaskArgument $resolvedScript),
        '-ExpectedBindAddress', (ConvertTo-FridayQuotedTaskArgument $ExpectedBindAddress),
        '-DockerPath', (ConvertTo-FridayQuotedTaskArgument $ResolvedDockerPath),
        '-ReadinessAttempts', [string]$ReadinessAttempts,
        '-PostRestartAttempts', [string]$PostRestartAttempts,
        '-RetryDelaySeconds', [string]$RetryDelaySeconds,
        '-ListenerTimeoutMilliseconds', [string]$ListenerTimeoutMilliseconds
    )
    $action = New-ScheduledTaskAction `
        -Execute $windowsPowerShell `
        -Argument ([string]::Join(' ', $taskArguments))
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentUser `
        -LogonType Interactive `
        -RunLevel Limited
    $maximumSeconds = (($ReadinessAttempts + $PostRestartAttempts) * $RetryDelaySeconds) + 180
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Seconds $maximumSeconds) `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    $null = Register-ScheduledTask `
        -TaskName $script:RecoveryTaskName `
        -Description 'One-shot fail-closed recovery for a missing Friday gateway Docker port publication.' `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -ErrorAction Stop
    return [pscustomobject][ordered]@{
        outcome = 'installed_at_logon'
        task_name = $script:RecoveryTaskName
        user = $currentUser
        bind_address = $ExpectedBindAddress
    }
}

function Invoke-FridayNativeGatewayRecovery {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedBindAddress,
        [Parameter(Mandatory = $true)][string]$ResolvedDockerPath,
        [Parameter(Mandatory = $true)][ValidateRange(2, 90)][int]$ReadinessAttempts,
        [Parameter(Mandatory = $true)][ValidateRange(1, 60)][int]$PostRestartAttempts,
        [Parameter(Mandatory = $true)][ValidateRange(1, 30)][int]$RetryDelaySeconds,
        [Parameter(Mandatory = $true)][ValidateRange(100, 5000)][int]$ListenerTimeoutMilliseconds
    )

    return Invoke-FridayGatewayRecoveryLoop `
        -ExpectedBindAddress $ExpectedBindAddress `
        -ReadinessAttempts $ReadinessAttempts `
        -PostRestartAttempts $PostRestartAttempts `
        -RetryDelaySeconds $RetryDelaySeconds `
        -TestLanReady { Test-FridayLanAddressReady $ExpectedBindAddress } `
        -TestDockerReady { Test-FridayDockerReady $ResolvedDockerPath } `
        -GetContainer { Get-FridayGatewayContainer $ResolvedDockerPath } `
        -TestListener {
            Test-FridayTcpListener $ExpectedBindAddress $ListenerTimeoutMilliseconds
        } `
        -RestartGateway { Restart-FridayGatewayContainer $ResolvedDockerPath } `
        -Sleep { param([int]$Seconds) Start-Sleep -Seconds $Seconds }
}

if (-not $LibraryOnly) {
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    try {
        $null = Get-FridayCanonicalPrivateIPv4 $ExpectedBindAddress
        $resolvedDocker = Resolve-FridayDockerExecutable $DockerPath
        if ($InstallAtLogon) {
            $result = Install-FridayGatewayRecoveryAtLogon `
                -ScriptPath $PSCommandPath `
                -ExpectedBindAddress $ExpectedBindAddress `
                -ResolvedDockerPath $resolvedDocker `
                -ReadinessAttempts $ReadinessAttempts `
                -PostRestartAttempts $PostRestartAttempts `
                -RetryDelaySeconds $RetryDelaySeconds `
                -ListenerTimeoutMilliseconds $ListenerTimeoutMilliseconds
        } else {
            $result = Invoke-FridayNativeGatewayRecovery `
                -ExpectedBindAddress $ExpectedBindAddress `
                -ResolvedDockerPath $resolvedDocker `
                -ReadinessAttempts $ReadinessAttempts `
                -PostRestartAttempts $PostRestartAttempts `
                -RetryDelaySeconds $RetryDelaySeconds `
                -ListenerTimeoutMilliseconds $ListenerTimeoutMilliseconds
        }
        $result | ConvertTo-Json -Compress
        exit 0
    } catch {
        [Console]::Error.WriteLine(('Friday gateway publication recovery failed closed: {0}' -f
            $_.Exception.Message))
        exit 20
    }
}
