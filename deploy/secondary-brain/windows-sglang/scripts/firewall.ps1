[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('192.168.1.78')]
    [string]$PrimaryFridayHost = '192.168.1.78',

    [Parameter()]
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$localFridayHost = '192.168.1.35'
$managedRuleName = 'Friday.Secondary.SGLang.Allow.TrustedIPv4.TCP8443'
$legacyRuleDisplayName = 'Friday Secondary - SGLang from primary only'
$ruleDisplayName = 'Friday Secondary - SGLang from primary and local telemetry'
$applyGuardName = 'Friday.Secondary.SGLang.ApplyGuard.All.TCP8443'
$ipv4BlockName = 'Friday.Secondary.SGLang.Block.Complement.IPv4.TCP8443'
$ipv6BlockName = 'Friday.Secondary.SGLang.Block.All.IPv6.TCP8443'
$ipv4BlockAddresses = @(
    '0.0.0.0-192.168.1.34',
    '192.168.1.36-192.168.1.77',
    '192.168.1.79-255.255.255.255'
)
$ipv6BlockAddresses = @('::/0')
$blockSpecs = @(
    [pscustomobject]@{
        name = $ipv4BlockName
        display = 'Friday Secondary - block non-trusted IPv4 on TCP 8443'
        remote_addresses = $ipv4BlockAddresses
    },
    [pscustomobject]@{
        name = $ipv6BlockName
        display = 'Friday Secondary - block all IPv6 on TCP 8443'
        remote_addresses = $ipv6BlockAddresses
    }
)
$applyGuardSpec = [pscustomobject]@{
    name = $applyGuardName
    display = 'Friday Secondary - temporary all-source TCP 8443 apply guard'
    remote_addresses = @('Any')
}

$plan = [ordered]@{
    schema = 'friday.secondary-firewall-plan.v3'
    apply = [bool]$Apply
    allow_rule_name = $managedRuleName
    block_rule_names = @($ipv4BlockName, $ipv6BlockName)
    protocol = 'TCP'
    local_port = 8443
    allowed_remote_ipv4 = @($PrimaryFridayHost, $localFridayHost)
    ipv6_allowed = $false
    public_internet_exposure_allowed = $false
}
if (-not $Apply) {
    $plan | ConvertTo-Json -Depth 4
    return
}

$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Firewall configuration requires an elevated PowerShell session.'
}

. (Join-Path $PSScriptRoot 'firewall-classifier.ps1')

function Get-FridayCoreFirewallFilters {
    param([Parameter(Mandatory = $true)][object]$Rule)
    [pscustomobject]@{
        port = @($Rule | Get-NetFirewallPortFilter -ErrorAction Stop)
        application = @($Rule | Get-NetFirewallApplicationFilter -ErrorAction Stop)
        service = @($Rule | Get-NetFirewallServiceFilter -ErrorAction Stop)
    }
}

function Get-FridayManagedFirewallFilters {
    param([Parameter(Mandatory = $true)][object]$Rule)
    $core = Get-FridayCoreFirewallFilters $Rule
    [pscustomobject]@{
        port = @($core.port)
        application = @($core.application)
        service = @($core.service)
        address = @($Rule | Get-NetFirewallAddressFilter -ErrorAction Stop)
        security = @($Rule | Get-NetFirewallSecurityFilter -ErrorAction Stop)
        interface = @($Rule | Get-NetFirewallInterfaceFilter -ErrorAction Stop)
        interface_type = @($Rule | Get-NetFirewallInterfaceTypeFilter -ErrorAction Stop)
    }
}

function Test-FridayManagedAllowCandidate {
    param(
        [Parameter(Mandatory = $true)][object]$Rule,
        [Parameter()][switch]$LegacyPrimaryOnly
    )
    $filters = Get-FridayManagedFirewallFilters $Rule
    $arguments = @{
        Rule = $Rule
        PortFilters = $filters.port
        ApplicationFilters = $filters.application
        ServiceFilters = $filters.service
        AddressFilters = $filters.address
        SecurityFilters = $filters.security
        InterfaceFilters = $filters.interface
        InterfaceTypeFilters = $filters.interface_type
        PrimaryFridayHost = $PrimaryFridayHost
    }
    if (-not $LegacyPrimaryOnly) { $arguments.LocalFridayHost = $localFridayHost }
    Test-FridayManagedFirewallRuleExact @arguments
}

function Test-FridayManagedBlockCandidate {
    param(
        [Parameter(Mandatory = $true)][object]$Rule,
        [Parameter(Mandatory = $true)][object]$Spec
    )
    $filters = Get-FridayManagedFirewallFilters $Rule
    Test-FridayManagedBlockFirewallRuleExact -Rule $Rule -PortFilters $filters.port `
        -ApplicationFilters $filters.application -ServiceFilters $filters.service `
        -AddressFilters $filters.address -SecurityFilters $filters.security `
        -InterfaceFilters $filters.interface -InterfaceTypeFilters $filters.interface_type `
        -ExpectedRemoteAddresses $Spec.remote_addresses
}

function Assert-FridayFirewallProfilesEnabled {
    $firewallServices = @(Get-Service -Name MpsSvc -ErrorAction Stop)
    if ($firewallServices.Count -ne 1) {
        throw 'Windows Defender Firewall service state is missing or ambiguous.'
    }
    $firewallServiceStatus = Get-FridayStrictAtomProperty $firewallServices[0] 'Status'
    if (-not $firewallServiceStatus.valid -or $firewallServiceStatus.value -cne 'Running') {
        throw 'Windows Defender Firewall service must be running.'
    }
    $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
    if ($profiles.Count -ne 3) { throw 'Effective firewall profile set is missing or ambiguous.' }
    foreach ($profileName in @('Domain', 'Private', 'Public')) {
        $matching = @($profiles | Where-Object { [string]$_.Name -ceq $profileName })
        if ($matching.Count -ne 1) { throw 'Effective firewall profile set is missing or ambiguous.' }
        $enabled = Get-FridayStrictAtomProperty $matching[0] 'Enabled'
        $defaultInbound = Get-FridayStrictAtomProperty $matching[0] 'DefaultInboundAction'
        $allowInbound = Get-FridayStrictAtomProperty $matching[0] 'AllowInboundRules'
        $allowLocal = Get-FridayStrictAtomProperty $matching[0] 'AllowLocalFirewallRules'
        $disabledAliasesProperty = $matching[0].PSObject.Properties['DisabledInterfaceAliases']
        $disabledAliases = if ($null -eq $disabledAliasesProperty) {
            @('__missing__')
        } else {
            @($disabledAliasesProperty.Value | Where-Object {
                $null -ne $_ -and -not [string]::IsNullOrWhiteSpace([string]$_)
            })
        }
        if (-not $enabled.valid -or $enabled.value -cne 'True' -or
            -not $defaultInbound.valid -or $defaultInbound.value -cne 'Block' -or
            -not $allowInbound.valid -or $allowInbound.value -cne 'True' -or
            -not $allowLocal.valid -or $allowLocal.value -cne 'True' -or
            @($disabledAliases).Count -ne 0) {
            throw 'Every effective firewall profile must enforce blocks and local inbound rules.'
        }
    }
}

function Get-FridayAuthenticatedBypassConflicts {
    $conflicts = @()
    $allowRules = @(Get-NetFirewallRule -PolicyStore ActiveStore -TracePolicyStore `
        -Direction Inbound -Action Allow -Enabled True -ErrorAction Stop)
    foreach ($allowRule in $allowRules) {
        $portFilters = @($allowRule | Get-NetFirewallPortFilter -ErrorAction Stop)
        $securityFilters = @($allowRule | Get-NetFirewallSecurityFilter -ErrorAction Stop)
        $assessment = Get-FridayAuthenticatedBypassAssessment `
            -PortFilters $portFilters -SecurityFilters $securityFilters
        if ($assessment.conflict) { $conflicts += $allowRule }
    }
    return @($conflicts)
}

function Assert-FridayBlockCoverage {
    foreach ($spec in $blockSpecs) {
        $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -TracePolicyStore `
            -Name $spec.name -ErrorAction SilentlyContinue)
        if ($rules.Count -ne 1 -or -not (Test-FridayManagedBlockCandidate $rules[0] $spec)) {
            throw 'Managed complement block coverage is missing or differs from the exact contract.'
        }
    }
    if (-not (Test-FridayRemoteAddressComplementExact `
        $ipv4BlockAddresses $ipv6BlockAddresses)) {
        throw 'Managed complement address coverage is not exact.'
    }
}

function Assert-FridayFinalCoverage {
    Assert-FridayFirewallProfilesEnabled
    Assert-FridayBlockCoverage
    $allowReadback = @(Get-NetFirewallRule -PolicyStore ActiveStore -TracePolicyStore `
        -Name $managedRuleName -ErrorAction Stop)
    if ($allowReadback.Count -ne 1 -or
        -not (Test-FridayManagedAllowCandidate $allowReadback[0])) {
        throw 'The trusted allow is missing, ambiguous, or differs from the exact contract.'
    }
    if (@(Get-FridayAuthenticatedBypassConflicts).Count -ne 0) {
        throw 'An enabled authenticated-bypass allow can override TCP 8443 complement blocks.'
    }
}

function Remove-FridayCreatedTrustedAllowBestEffort {
    param([Parameter(Mandatory = $true)][bool]$CreatedByThisApply)
    if (-not $CreatedByThisApply) { return }
    try {
        $rollbackCandidates = @(Get-NetFirewallRule -PolicyStore PersistentStore `
            -TracePolicyStore -Name $managedRuleName -ErrorAction SilentlyContinue)
        if ($rollbackCandidates.Count -eq 1 -and
            (Test-FridayManagedAllowCandidate $rollbackCandidates[0])) {
            $rollbackCandidates[0] | Remove-NetFirewallRule -ErrorAction SilentlyContinue
        }
    } catch {
        # Exact complement blocks/Apply guard remain authoritative.
    }
}

# A block is ineffective if its profile is disabled, and authenticated bypass
# can override normal block precedence. Both facts are audited before mutation.
Assert-FridayFirewallProfilesEnabled
if (@(Get-FridayAuthenticatedBypassConflicts).Count -ne 0) {
    throw 'An enabled authenticated-bypass allow can override TCP 8443 complement blocks.'
}

# The all-source guard is safe tightening. It remains after any interrupted or
# failed Apply until both final complement blocks and the trusted allow verify.
$guardRules = @(Get-NetFirewallRule -PolicyStore PersistentStore -TracePolicyStore `
    -Name $applyGuardName -ErrorAction SilentlyContinue)
if ($guardRules.Count -eq 0) {
    New-NetFirewallRule -Name $applyGuardName -DisplayName $applyGuardSpec.display `
        -Direction Inbound -Action Block -Enabled True -Profile Any `
        -Protocol TCP -LocalPort 8443 -RemoteAddress Any `
        -EdgeTraversalPolicy Block -LocalOnlyMapping $false -LooseSourceMapping $false `
        -PolicyStore PersistentStore -ErrorAction Stop | Out-Null
} elseif ($guardRules.Count -ne 1 -or
    -not (Test-FridayManagedBlockCandidate $guardRules[0] $applyGuardSpec)) {
    throw 'The persistent all-source Apply guard is ambiguous or malformed.'
}
$guardReadback = @(Get-NetFirewallRule -PolicyStore ActiveStore -TracePolicyStore `
    -Name $applyGuardName -ErrorAction Stop)
if ($guardReadback.Count -ne 1 -or
    -not (Test-FridayManagedBlockCandidate $guardReadback[0] $applyGuardSpec)) {
    throw 'The all-source Apply guard failed exact effective-policy readback.'
}

# Under the verified guard, a malformed or narrow complement can be repaired
# without opening TCP 8443 during remove/create.
foreach ($spec in $blockSpecs) {
    $existing = @(Get-NetFirewallRule -PolicyStore PersistentStore -TracePolicyStore `
        -Name $spec.name -ErrorAction SilentlyContinue)
    if ($existing.Count -ne 1 -or -not (Test-FridayManagedBlockCandidate $existing[0] $spec)) {
        foreach ($candidate in $existing) {
            $candidate | Remove-NetFirewallRule -ErrorAction Stop
        }
        New-NetFirewallRule -Name $spec.name -DisplayName $spec.display `
            -Direction Inbound -Action Block -Enabled True -Profile Any `
            -Protocol TCP -LocalPort 8443 -RemoteAddress $spec.remote_addresses `
            -EdgeTraversalPolicy Block -LocalOnlyMapping $false -LooseSourceMapping $false `
            -PolicyStore PersistentStore -ErrorAction Stop | Out-Null
    }
}
Assert-FridayBlockCoverage

$activeAllows = @(Get-NetFirewallRule -PolicyStore ActiveStore -TracePolicyStore `
    -Direction Inbound -Action Allow -Enabled True -ErrorAction Stop)
$legacyNames = @{}
foreach ($activeAllow in $activeAllows) {
    $display = Get-FridayStrictTextProperty $activeAllow 'DisplayName'
    $name = Get-FridayStrictTextProperty $activeAllow 'Name'
    if ($display.valid -and $name.valid -and $display.value -ceq $legacyRuleDisplayName -and
        (Test-FridayManagedAllowCandidate $activeAllow -LegacyPrimaryOnly)) {
        $legacyNames[$name.value] = $true
    }
}
$persistentRules = @(Get-NetFirewallRule -PolicyStore PersistentStore -TracePolicyStore -ErrorAction Stop)
$legacyCandidates = @($persistentRules | Where-Object {
    $name = Get-FridayStrictTextProperty $_ 'Name'
    $name.valid -and $legacyNames.ContainsKey($name.value)
})
foreach ($candidate in $legacyCandidates) {
    if (-not (Test-FridayManagedAllowCandidate $candidate -LegacyPrimaryOnly)) {
        throw 'A legacy allow changed after exact classification.'
    }
}
$trustedCandidates = @($persistentRules | Where-Object {
    $name = Get-FridayStrictTextProperty $_ 'Name'
    $name.valid -and $name.value -ceq $managedRuleName
})
if ($trustedCandidates.Count -gt 1 -or
    ($trustedCandidates.Count -eq 1 -and -not (Test-FridayManagedAllowCandidate $trustedCandidates[0]))) {
    throw 'The stable trusted allow identity is ambiguous or malformed.'
}
foreach ($candidate in $legacyCandidates) {
    $candidate | Remove-NetFirewallRule -ErrorAction Stop
}
$createdTrustedAllow = $false
if ($trustedCandidates.Count -eq 0) {
    New-NetFirewallRule -Name $managedRuleName -DisplayName $ruleDisplayName `
        -Direction Inbound -Action Allow -Enabled True -Profile Any -Protocol TCP `
        -LocalPort 8443 -RemoteAddress @($PrimaryFridayHost, $localFridayHost) `
        -EdgeTraversalPolicy Block -LocalOnlyMapping $false -LooseSourceMapping $false `
        -PolicyStore PersistentStore -ErrorAction Stop | Out-Null
    $createdTrustedAllow = $true
}

try {
    # This audit deliberately runs while the all-source guard still closes the
    # endpoint. It is also the TOCTOU rescan after trusted-allow creation.
    Assert-FridayFinalCoverage
    $guardPersistentReadback = @(Get-NetFirewallRule -PolicyStore PersistentStore `
        -TracePolicyStore -Name $applyGuardName -ErrorAction Stop)
    $guardActiveReadback = @(Get-NetFirewallRule -PolicyStore ActiveStore `
        -TracePolicyStore -Name $applyGuardName -ErrorAction Stop)
    if ($guardPersistentReadback.Count -ne 1 -or $guardActiveReadback.Count -ne 1 -or
        -not (Test-FridayManagedBlockCandidate $guardPersistentReadback[0] $applyGuardSpec) -or
        -not (Test-FridayManagedBlockCandidate $guardActiveReadback[0] $applyGuardSpec)) {
        throw 'The Apply guard changed before final removal.'
    }
} catch {
    Remove-FridayCreatedTrustedAllowBestEffort $createdTrustedAllow
    throw 'Pre-removal firewall coverage audit failed; the Apply guard remains installed.'
}

try {
    $guardPersistentReadback[0] | Remove-NetFirewallRule -ErrorAction Stop
    $remainingActiveGuard = @(Get-NetFirewallRule -PolicyStore ActiveStore -TracePolicyStore `
        -Name $applyGuardName -ErrorAction SilentlyContinue)
    $remainingPersistentGuard = @(Get-NetFirewallRule -PolicyStore PersistentStore -TracePolicyStore `
        -Name $applyGuardName -ErrorAction SilentlyContinue)
    if ($remainingActiveGuard.Count -ne 0 -or $remainingPersistentGuard.Count -ne 0) {
        throw 'The Apply guard remained in effective policy after removal.'
    }
    # Success requires the same complete proof after the guard is gone.
    Assert-FridayFinalCoverage
} catch {
    # Restore closure first. Any exact complement continues to protect the
    # endpoint even if this best-effort recovery encounters a policy race.
    try {
        $recoveryGuard = @(Get-NetFirewallRule -PolicyStore PersistentStore `
            -TracePolicyStore -Name $applyGuardName -ErrorAction SilentlyContinue)
        if ($recoveryGuard.Count -eq 0) {
            New-NetFirewallRule -Name $applyGuardName -DisplayName $applyGuardSpec.display `
                -Direction Inbound -Action Block -Enabled True -Profile Any `
                -Protocol TCP -LocalPort 8443 -RemoteAddress Any `
                -EdgeTraversalPolicy Block -LocalOnlyMapping $false -LooseSourceMapping $false `
                -PolicyStore PersistentStore -ErrorAction Stop | Out-Null
        } elseif ($recoveryGuard.Count -ne 1 -or
            -not (Test-FridayManagedBlockCandidate $recoveryGuard[0] $applyGuardSpec)) {
            throw 'Apply guard recovery identity is ambiguous or malformed.'
        }
        $recoveredActiveGuard = @(Get-NetFirewallRule -PolicyStore ActiveStore `
            -TracePolicyStore -Name $applyGuardName -ErrorAction Stop)
        if ($recoveredActiveGuard.Count -ne 1 -or
            -not (Test-FridayManagedBlockCandidate $recoveredActiveGuard[0] $applyGuardSpec)) {
            throw 'Apply guard recovery did not reach effective policy.'
        }
    } catch {
        # The exact complement blocks are deliberately never removed here.
    }
    Remove-FridayCreatedTrustedAllowBestEffort $createdTrustedAllow
    throw 'Post-removal firewall coverage audit failed; closed-state recovery was attempted.'
}

$plan.status = 'configured_complement_blocks_exact_allow_and_no_authenticated_bypass'
$plan | ConvertTo-Json -Depth 4
