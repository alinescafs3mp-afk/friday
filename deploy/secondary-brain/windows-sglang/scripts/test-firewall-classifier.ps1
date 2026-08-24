[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'firewall-classifier.ps1')

$script:caseCount = 0

function Assert-FridayAssessment {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$ExpectedConflict,
        [Parameter(Mandatory = $true)][object]$Rule,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$PortFilters,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$ApplicationFilters,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$ServiceFilters,
        [Parameter(Mandatory = $true)]
        [System.Collections.Generic.HashSet[string]]$InstalledPackageFamilyNames
    )
    $observed = Get-FridayFirewallRuleAssessment `
        -Rule $Rule `
        -PortFilters $PortFilters `
        -ApplicationFilters $ApplicationFilters `
        -ServiceFilters $ServiceFilters `
        -InstalledPackageFamilyNames $InstalledPackageFamilyNames
    if ([bool]$observed.conflict -ne $ExpectedConflict) {
        throw ('Case {0} expected conflict={1}, got conflict={2}, reason={3}.' -f
            $Name, $ExpectedConflict, $observed.conflict, $observed.reason)
    }
    $script:caseCount += 1
}

function Assert-FridayFalse {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][bool]$Value)
    if ($Value) { throw ('Case {0} unexpectedly evaluated true.' -f $Name) }
    $script:caseCount += 1
}

function Assert-FridayTrue {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][bool]$Value)
    if (-not $Value) { throw ('Case {0} unexpectedly evaluated false.' -f $Name) }
    $script:caseCount += 1
}

function Assert-FridayThrows {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][scriptblock]$Action)
    $threw = $false
    try { & $Action | Out-Null } catch { $threw = $true }
    if (-not $threw) { throw ('Case {0} did not fail closed.' -f $Name) }
    $script:caseCount += 1
}

function New-FridayRuleFixture {
    param(
        [string]$Name = 'fixture-rule',
        [AllowNull()][object]$PackageFamilyName = $null
    )
    [pscustomobject]@{ Name = $Name; PackageFamilyName = $PackageFamilyName }
}

function New-FridayPortFixture {
    param([object]$Protocol = 'Any', [object]$LocalPort = 'Any')
    $localPortValue = if ($LocalPort -is [array]) {
        @($LocalPort | ForEach-Object { [string]$_ })
    } else {
        [string]$LocalPort
    }
    [pscustomobject]@{
        Protocol = $Protocol
        LocalPort = $localPortValue
        RemotePort = 'Any'
        DynamicTarget = 'Any'
        LocalOnlyMapping = $false
        LooseSourceMapping = $false
    }
}

function New-FridayApplicationFixture {
    param([object]$Program = 'Any', [AllowNull()][object]$Package = $null)
    [pscustomobject]@{ Program = $Program; Package = $Package }
}

function New-FridayServiceFixture { param([object]$Service = 'Any') [pscustomobject]@{ Service = $Service } }

$knownPfn = 'Microsoft.WindowsStore_8wekyb3d8bbwe'
$installed = New-FridayVerifiedPackageFamilyNameSet @($knownPfn)
$anyPort = New-FridayPortFixture
$anyApplication = New-FridayApplicationFixture
$anyService = New-FridayServiceFixture

Assert-FridayAssessment `
    -Name 'installed_pfn_server_capability_is_exempt' `
    -ExpectedConflict $false `
    -Rule (New-FridayRuleFixture -Name ('x' + '-In-Allow-ServerCapability') -PackageFamilyName $knownPfn) `
    -PortFilters @($anyPort) -ApplicationFilters @($anyApplication) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'uninstalled_pfn_fails_closed' `
    -ExpectedConflict $true `
    -Rule (New-FridayRuleFixture -Name ('x' + '-In-Allow-ServerCapability') `
        -PackageFamilyName 'Microsoft.WindowsCamera_8wekyb3d8bbwe') `
    -PortFilters @($anyPort) -ApplicationFilters @($anyApplication) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'wrong_server_capability_suffix_fails_closed' `
    -ExpectedConflict $true `
    -Rule (New-FridayRuleFixture -Name 'lookalike-rule' -PackageFamilyName $knownPfn) `
    -PortFilters @($anyPort) -ApplicationFilters @($anyApplication) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'server_capability_without_pfn_fails_closed' `
    -ExpectedConflict $true `
    -Rule (New-FridayRuleFixture -Name ('x' + '-In-Allow-ServerCapability')) `
    -PortFilters @($anyPort) -ApplicationFilters @($anyApplication) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

$missingPfnRule = [pscustomobject]@{ Name = 'missing-pfn-property' }
Assert-FridayAssessment `
    -Name 'missing_pfn_property_fails_closed' `
    -ExpectedConflict $true -Rule $missingPfnRule `
    -PortFilters @($anyPort) -ApplicationFilters @($anyApplication) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

$specificSid = 'S-1-15-2-1-2-3-4-5-6-7'
Assert-FridayAssessment `
    -Name 'canonical_specific_appcontainer_sid_is_exempt' `
    -ExpectedConflict $false -Rule (New-FridayRuleFixture) `
    -PortFilters @($anyPort) `
    -ApplicationFilters @((New-FridayApplicationFixture -Package $specificSid)) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

foreach ($unsafeSid in @('Any', 'S-1-15-2-1', 'S-1-15-3-1',
    'S-1-15-2-1-2-3-4-5-6', 'S-1-15-2-1-2-3-4-5-6-4294967296')) {
    Assert-FridayAssessment `
        -Name ('unsafe_package_sid_' + $unsafeSid) `
        -ExpectedConflict $true -Rule (New-FridayRuleFixture) `
        -PortFilters @($anyPort) `
        -ApplicationFilters @((New-FridayApplicationFixture -Package $unsafeSid)) `
        -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed
}

Assert-FridayAssessment `
    -Name 'explicit_8443_is_conflict' `
    -ExpectedConflict $true -Rule (New-FridayRuleFixture) `
    -PortFilters @((New-FridayPortFixture -Protocol TCP -LocalPort 8443)) `
    -ApplicationFilters @($anyApplication) -ServiceFilters @($anyService) `
    -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'range_including_8443_is_conflict' `
    -ExpectedConflict $true -Rule (New-FridayRuleFixture) `
    -PortFilters @((New-FridayPortFixture -Protocol 6 -LocalPort '8000-9000')) `
    -ApplicationFilters @($anyApplication) -ServiceFilters @($anyService) `
    -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'numeric_port_excluding_8443_is_safe' `
    -ExpectedConflict $false -Rule (New-FridayRuleFixture) `
    -PortFilters @((New-FridayPortFixture -Protocol TCP -LocalPort '80,443')) `
    -ApplicationFilters @($anyApplication) -ServiceFilters @($anyService) `
    -InstalledPackageFamilyNames $installed

foreach ($badPort in @('RPC', '9000-8000', '65536', '80,,443')) {
    Assert-FridayAssessment `
        -Name ('malformed_port_' + $badPort) `
        -ExpectedConflict $true -Rule (New-FridayRuleFixture) `
        -PortFilters @((New-FridayPortFixture -Protocol TCP -LocalPort $badPort)) `
        -ApplicationFilters @($anyApplication) -ServiceFilters @($anyService) `
        -InstalledPackageFamilyNames $installed
}

Assert-FridayAssessment `
    -Name 'udp_any_port_excludes_tcp' `
    -ExpectedConflict $false -Rule (New-FridayRuleFixture) `
    -PortFilters @((New-FridayPortFixture -Protocol UDP)) `
    -ApplicationFilters @() -ServiceFilters @() -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'unknown_protocol_fails_closed' `
    -ExpectedConflict $true -Rule (New-FridayRuleFixture) `
    -PortFilters @((New-FridayPortFixture -Protocol StrangeProtocol)) `
    -ApplicationFilters @($anyApplication) -ServiceFilters @($anyService) `
    -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'missing_application_filter_fails_closed' `
    -ExpectedConflict $true -Rule (New-FridayRuleFixture) `
    -PortFilters @($anyPort) -ApplicationFilters @() -ServiceFilters @($anyService) `
    -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'multiple_service_filters_fail_closed' `
    -ExpectedConflict $true -Rule (New-FridayRuleFixture) `
    -PortFilters @($anyPort) -ApplicationFilters @($anyApplication) `
    -ServiceFilters @($anyService, $anyService) -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'multiple_port_filters_fail_closed' `
    -ExpectedConflict $true -Rule (New-FridayRuleFixture) `
    -PortFilters @($anyPort, $anyPort) -ApplicationFilters @($anyApplication) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'package_identity_with_program_path_fails_closed' `
    -ExpectedConflict $true `
    -Rule (New-FridayRuleFixture -Name ('x' + '-In-Allow-ServerCapability') -PackageFamilyName $knownPfn) `
    -PortFilters @($anyPort) `
    -ApplicationFilters @((New-FridayApplicationFixture -Program 'C:\Windows\System32\notepad.exe')) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'unscoped_any_any_any_is_conflict' `
    -ExpectedConflict $true -Rule (New-FridayRuleFixture) `
    -PortFilters @($anyPort) -ApplicationFilters @($anyApplication) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

Assert-FridayAssessment `
    -Name 'explicit_unrelated_program_excludes_docker' `
    -ExpectedConflict $false -Rule (New-FridayRuleFixture) `
    -PortFilters @($anyPort) `
    -ApplicationFilters @((New-FridayApplicationFixture -Program 'C:\Windows\System32\notepad.exe')) `
    -ServiceFilters @($anyService) -InstalledPackageFamilyNames $installed

Assert-FridayThrows -Name 'empty_installed_inventory_fails_closed' -Action {
    New-FridayVerifiedPackageFamilyNameSet @()
}
Assert-FridayThrows -Name 'malformed_installed_pfn_fails_closed' -Action {
    New-FridayVerifiedPackageFamilyNameSet @('forged_package')
}

$managedRule = [pscustomobject]@{
    Name = 'legacy-guid'
    DisplayName = 'Friday Secondary - SGLang from primary only'
    PackageFamilyName = $null
    Direction = 'Inbound'
    Action = 'Allow'
    Enabled = $true
    Profile = 'Any'
    EdgeTraversalPolicy = 'Block'
    LocalOnlyMapping = $false
    LooseSourceMapping = $false
    PolicyStoreSourceType = 'Local'
    PolicyStoreSource = 'PersistentStore'
    Owner = $null
}
$managedPort = New-FridayPortFixture -Protocol TCP -LocalPort 8443
$managedAddress = [pscustomobject]@{ LocalAddress = 'Any'; RemoteAddress = '192.168.1.78' }
$managedSecurity = [pscustomobject]@{
    Authentication = 'NotRequired'
    Encryption = 'NotRequired'
    OverrideBlockRules = $false
    LocalUser = 'Any'
    RemoteUser = 'Any'
    RemoteMachine = 'Any'
}
$managedInterface = [pscustomobject]@{ InterfaceAlias = @('Any') }
$managedInterfaceType = [pscustomobject]@{ InterfaceType = 'Any' }
$managedArguments = @{
    Rule = $managedRule
    PortFilters = @($managedPort)
    ApplicationFilters = @($anyApplication)
    ServiceFilters = @($anyService)
    AddressFilters = @($managedAddress)
    SecurityFilters = @($managedSecurity)
    InterfaceFilters = @($managedInterface)
    InterfaceTypeFilters = @($managedInterfaceType)
    PrimaryFridayHost = '192.168.1.78'
}
Assert-FridayTrue -Name 'exact_legacy_managed_rule_is_replaceable' `
    -Value (Test-FridayManagedFirewallRuleExact @managedArguments)
$managedAddress.RemoteAddress = 'Any'
Assert-FridayFalse -Name 'display_name_does_not_exempt_broad_remote_address' `
    -Value (Test-FridayManagedFirewallRuleExact @managedArguments)
$managedAddress.RemoteAddress = @('192.168.1.35', '192.168.1.78')
$managedArguments.LocalFridayHost = '192.168.1.35'
Assert-FridayTrue -Name 'trusted_allow_exact_set_is_accepted_in_any_order' `
    -Value (Test-FridayManagedFirewallRuleExact @managedArguments)
$managedInterface.InterfaceAlias = @('Ethernet')
Assert-FridayFalse -Name 'narrowed_interface_alias_fails_exact_allow' `
    -Value (Test-FridayManagedFirewallRuleExact @managedArguments)
$managedInterface.InterfaceAlias = @('Any')
$managedPort.DynamicTarget = 'ProximityApps'
Assert-FridayFalse -Name 'dynamic_target_fails_exact_allow' `
    -Value (Test-FridayManagedFirewallRuleExact @managedArguments)
$managedPort.DynamicTarget = 'Any'

$ipv4Complement = @(
    '0.0.0.0-192.168.1.34',
    '192.168.1.36-192.168.1.77',
    '192.168.1.79-255.255.255.255'
)
Assert-FridayTrue -Name 'exact_ipv4_ipv6_complement_is_complete' `
    -Value (Test-FridayRemoteAddressComplementExact $ipv4Complement @('::/0'))
Assert-FridayFalse -Name 'missing_complement_range_fails_closed' `
    -Value (Test-FridayRemoteAddressComplementExact $ipv4Complement[0..1] @('::/0'))
Assert-FridayFalse -Name 'missing_ipv6_block_fails_closed' `
    -Value (Test-FridayRemoteAddressComplementExact $ipv4Complement @())

$managedBlockRule = [pscustomobject]@{
    Name = 'Friday.Secondary.SGLang.Block.Complement.IPv4.TCP8443'
    DisplayName = 'managed complement'
    PackageFamilyName = $null
    Owner = $null
    Direction = 'Inbound'
    Action = 'Block'
    Enabled = $true
    Profile = 'Any'
    EdgeTraversalPolicy = 'Block'
    LocalOnlyMapping = $false
    LooseSourceMapping = $false
    PolicyStoreSourceType = 'Local'
    PolicyStoreSource = 'PersistentStore'
}
$managedBlockAddress = [pscustomobject]@{
    LocalAddress = 'Any'
    RemoteAddress = $ipv4Complement
}
$managedBlockArguments = @{
    Rule = $managedBlockRule
    PortFilters = @($managedPort)
    ApplicationFilters = @($anyApplication)
    ServiceFilters = @($anyService)
    AddressFilters = @($managedBlockAddress)
    SecurityFilters = @($managedSecurity)
    InterfaceFilters = @($managedInterface)
    InterfaceTypeFilters = @($managedInterfaceType)
    ExpectedRemoteAddresses = $ipv4Complement
}
Assert-FridayTrue -Name 'exact_managed_complement_block_is_accepted' `
    -Value (Test-FridayManagedBlockFirewallRuleExact @managedBlockArguments)
$managedBlockRule.Owner = 'S-1-5-21-1'
Assert-FridayFalse -Name 'owned_block_fails_exact_contract' `
    -Value (Test-FridayManagedBlockFirewallRuleExact @managedBlockArguments)
$managedBlockRule.Owner = $null
$managedBlockAddress.RemoteAddress = $ipv4Complement[0..1]
Assert-FridayFalse -Name 'narrowed_managed_block_fails_exact_contract' `
    -Value (Test-FridayManagedBlockFirewallRuleExact @managedBlockArguments)

$normalSecurity = [pscustomobject]@{ OverrideBlockRules = $false }
$bypassSecurity = [pscustomobject]@{ OverrideBlockRules = $true }
$normalMalformed = Get-FridayAuthenticatedBypassAssessment `
    -PortFilters @((New-FridayPortFixture -Protocol TCP -LocalPort 'unknown-token')) `
    -SecurityFilters @($normalSecurity)
Assert-FridayFalse -Name 'normal_allow_skips_irrelevant_port_parsing' -Value $normalMalformed.conflict
$bypassAny = Get-FridayAuthenticatedBypassAssessment -PortFilters @($anyPort) `
    -SecurityFilters @($bypassSecurity)
Assert-FridayTrue -Name 'authenticated_any_port_bypass_conflicts' -Value $bypassAny.conflict
$bypassNumeric = Get-FridayAuthenticatedBypassAssessment `
    -PortFilters @((New-FridayPortFixture -Protocol TCP -LocalPort 80)) `
    -SecurityFilters @($bypassSecurity)
Assert-FridayFalse -Name 'authenticated_numeric_nonoverlap_is_safe' -Value $bypassNumeric.conflict
$bypassUnknown = Get-FridayAuthenticatedBypassAssessment `
    -PortFilters @((New-FridayPortFixture -Protocol TCP -LocalPort RPC)) `
    -SecurityFilters @($bypassSecurity)
Assert-FridayTrue -Name 'authenticated_unknown_port_is_indeterminate' -Value $bypassUnknown.conflict
$missingSecurity = Get-FridayAuthenticatedBypassAssessment -PortFilters @($anyPort) -SecurityFilters @()
Assert-FridayTrue -Name 'missing_bypass_security_filter_fails_closed' -Value $missingSecurity.conflict

[pscustomobject][ordered]@{
    schema = 'friday.secondary-firewall-classifier-test.v1'
    status = 'passed'
    cases = $script:caseCount
} | ConvertTo-Json -Depth 3
