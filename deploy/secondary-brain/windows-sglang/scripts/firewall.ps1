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
$managedRuleName = 'Friday.Secondary.SGLang.PrimaryOnly.TCP8443'
$ruleDisplayName = 'Friday Secondary - SGLang from primary only'

$plan = [ordered]@{
    schema = 'friday.secondary-firewall-plan.v2'
    apply = [bool]$Apply
    rule_name = $managedRuleName
    rule_display_name = $ruleDisplayName
    protocol = 'TCP'
    local_port = 8443
    remote_address = $PrimaryFridayHost
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
    param(
        [Parameter(Mandatory = $true)][object]$Rule,
        [Parameter(Mandatory = $true)][object]$Core
    )
    [pscustomobject]@{
        port = @($Core.port)
        application = @($Core.application)
        service = @($Core.service)
        address = @($Rule | Get-NetFirewallAddressFilter -ErrorAction Stop)
        security = @($Rule | Get-NetFirewallSecurityFilter -ErrorAction Stop)
    }
}

function Test-FridayManagedCandidate {
    param(
        [Parameter(Mandatory = $true)][object]$Rule,
        [Parameter(Mandatory = $true)][object]$Core
    )
    $filters = Get-FridayManagedFirewallFilters $Rule $Core
    Test-FridayManagedFirewallRuleExact `
        -Rule $Rule `
        -PortFilters $filters.port `
        -ApplicationFilters $filters.application `
        -ServiceFilters $filters.service `
        -AddressFilters $filters.address `
        -SecurityFilters $filters.security `
        -PrimaryFridayHost $PrimaryFridayHost
}

# Package-family inventory is an authority input, not a display-name heuristic.
# Failure to enumerate or validate it stops Apply before any firewall mutation.
$installedPackages = @(Get-AppxPackage -AllUsers -ErrorAction Stop)
$installedPackageFamilyNames = @()
foreach ($installedPackage in $installedPackages) {
    $identity = Get-FridayStrictTextProperty $installedPackage 'PackageFamilyName'
    if (-not $identity.valid) {
        throw 'Installed package-family inventory contains an unreadable identity.'
    }
    $installedPackageFamilyNames += $identity.value
}
$installedPackageFamilyNameSet = New-FridayVerifiedPackageFamilyNameSet $installedPackageFamilyNames

# Audit effective local and policy rules. A legacy Friday DisplayName is ignored
# only after full desired-rule verification; the name alone grants no exemption.
$effectiveRules = @(
    Get-NetFirewallRule `
        -PolicyStore ActiveStore `
        -TracePolicyStore `
        -Direction Inbound `
        -Action Allow `
        -Enabled True `
        -ErrorAction Stop
)
$conflicts = @()
$managedNamesToReplace = @{}
foreach ($otherRule in $effectiveRules) {
    $core = Get-FridayCoreFirewallFilters $otherRule
    $observedName = Get-FridayStrictTextProperty $otherRule 'Name'
    $observedDisplayName = Get-FridayStrictTextProperty $otherRule 'DisplayName'
    $isManagedName = $observedName.valid -and $observedName.value -ceq $managedRuleName
    $isLegacyDisplayName = $observedDisplayName.valid -and
        $observedDisplayName.value -ceq $ruleDisplayName
    if (($isManagedName -or $isLegacyDisplayName) -and
        (Test-FridayManagedCandidate $otherRule $core)) {
        $managedNamesToReplace[$observedName.value] = $true
        continue
    }
    $assessment = Get-FridayFirewallRuleAssessment `
        -Rule $otherRule `
        -PortFilters $core.port `
        -ApplicationFilters $core.application `
        -ServiceFilters $core.service `
        -InstalledPackageFamilyNames $installedPackageFamilyNameSet
    if ($assessment.conflict) {
        $conflicts += $otherRule
    }
}
if ($conflicts.Count -ne 0) {
    throw 'Another enabled effective inbound allow rule can reach TCP 8443; remove or narrow it before rollout.'
}

# Re-read and revalidate every local rule that will be replaced. No mutation
# occurs above this boundary.
$persistentRules = @(
    Get-NetFirewallRule -PolicyStore PersistentStore -TracePolicyStore -ErrorAction Stop
)
$mutationCandidates = @(
    $persistentRules |
        Where-Object {
            $candidateName = Get-FridayStrictTextProperty $_ 'Name'
            $candidateName.valid -and (
                $candidateName.value -ceq $managedRuleName -or
                $managedNamesToReplace.ContainsKey($candidateName.value)
            )
        }
)
foreach ($expectedName in $managedNamesToReplace.Keys) {
    if (@($mutationCandidates | Where-Object { [string]$_.Name -ceq $expectedName }).Count -ne 1) {
        throw 'A previously audited managed firewall rule changed before replacement.'
    }
}
foreach ($candidate in $mutationCandidates) {
    $core = Get-FridayCoreFirewallFilters $candidate
    if (-not (Test-FridayManagedCandidate $candidate $core)) {
        throw 'A managed firewall candidate failed exact pre-mutation revalidation.'
    }
}

foreach ($candidate in $mutationCandidates) {
    $candidate | Remove-NetFirewallRule -ErrorAction Stop
}
New-NetFirewallRule `
    -Name $managedRuleName `
    -DisplayName $ruleDisplayName `
    -Direction Inbound `
    -Action Allow `
    -Enabled True `
    -Profile Any `
    -Protocol TCP `
    -LocalPort 8443 `
    -RemoteAddress $PrimaryFridayHost `
    -EdgeTraversalPolicy Block `
    -PolicyStore PersistentStore `
    -ErrorAction Stop | Out-Null

$readback = @(
    Get-NetFirewallRule `
        -PolicyStore ActiveStore `
        -TracePolicyStore `
        -Name $managedRuleName `
        -ErrorAction Stop
)
if ($readback.Count -ne 1) {
    throw 'Managed firewall rule readback is missing or ambiguous.'
}
$readbackCore = Get-FridayCoreFirewallFilters $readback[0]
if (-not (Test-FridayManagedCandidate $readback[0] $readbackCore)) {
    throw 'Managed firewall rule readback differs from the exact desired rule.'
}

$finalAuditFailed = $false
try {
    $finalEffectiveRules = @(
        Get-NetFirewallRule `
            -PolicyStore ActiveStore `
            -TracePolicyStore `
            -Direction Inbound `
            -Action Allow `
            -Enabled True `
            -ErrorAction Stop
    )
    $finalConflicts = @()
    $finalManagedCount = 0
    foreach ($finalRule in $finalEffectiveRules) {
        $finalCore = Get-FridayCoreFirewallFilters $finalRule
        $finalName = Get-FridayStrictTextProperty $finalRule 'Name'
        if ($finalName.valid -and $finalName.value -ceq $managedRuleName) {
            $finalManagedCount += 1
            if (-not (Test-FridayManagedCandidate $finalRule $finalCore)) {
                $finalConflicts += $finalRule
            }
            continue
        }
        $finalAssessment = Get-FridayFirewallRuleAssessment `
            -Rule $finalRule `
            -PortFilters $finalCore.port `
            -ApplicationFilters $finalCore.application `
            -ServiceFilters $finalCore.service `
            -InstalledPackageFamilyNames $installedPackageFamilyNameSet
        if ($finalAssessment.conflict) {
            $finalConflicts += $finalRule
        }
    }
    if ($finalManagedCount -ne 1 -or $finalConflicts.Count -ne 0) {
        $finalAuditFailed = $true
    }
} catch {
    $finalAuditFailed = $true
}
if ($finalAuditFailed) {
    try {
        Get-NetFirewallRule `
            -PolicyStore PersistentStore `
            -Name $managedRuleName `
            -ErrorAction SilentlyContinue |
                Remove-NetFirewallRule -ErrorAction SilentlyContinue
    } catch {
        # Best effort only: the rollout remains failed and reports no success.
    }
    throw 'Effective firewall policy changed during Apply; the managed allow rule was rolled back where possible.'
}

$plan.status = 'configured_exact_rule_and_no_conflicting_effective_allow_observed'
$plan | ConvertTo-Json -Depth 4
