[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('192.168.1.78')]
    [string]$PrimaryFridayHost = '192.168.1.78',

    [Parameter()]
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ruleName = 'Friday Secondary - SGLang from primary only'

$plan = [ordered]@{
    schema = 'friday.secondary-firewall-plan.v1'
    apply = [bool]$Apply
    rule = $ruleName
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
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule `
    -DisplayName $ruleName `
    -Direction Inbound `
    -Action Allow `
    -Enabled True `
    -Profile Any `
    -Protocol TCP `
    -LocalPort 8443 `
    -RemoteAddress $PrimaryFridayHost `
    -EdgeTraversalPolicy Block | Out-Null

$conflicts = @()

function Test-PortSetIncludes8443([string]$Value) {
    foreach ($part in @($Value -split ',')) {
        $candidate = $part.Trim()
        if ($candidate -ceq '8443') {
            return $true
        }
        if ($candidate -match '\A(?<first>[0-9]{1,5})-(?<last>[0-9]{1,5})\z' -and
            [int]$Matches.first -le 8443 -and 8443 -le [int]$Matches.last) {
            return $true
        }
    }
    return $false
}

$otherRules = @(
    Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
        Where-Object { [string]$_.DisplayName -cne $ruleName }
)
foreach ($otherRule in $otherRules) {
    $portFilters = @($otherRule | Get-NetFirewallPortFilter)
    $applicationFilters = @($otherRule | Get-NetFirewallApplicationFilter)
    $serviceFilters = @($otherRule | Get-NetFirewallServiceFilter)
    foreach ($portFilter in $portFilters) {
        $localPort = [string]$portFilter.LocalPort
        $portMatches = Test-PortSetIncludes8443 $localPort
        $broadProgram = @(
            $applicationFilters |
                Where-Object {
                    [string]$_.Program -ceq 'Any' -or
                    [string]$_.Program -match '(?i)(?:docker|com\.docker)'
                }
        ).Count -ne 0
        $broadService = @(
            $serviceFilters | Where-Object { [string]$_.Service -ceq 'Any' }
        ).Count -ne 0
        $anyPortConflict = $localPort -ceq 'Any' -and $broadProgram -and $broadService
        if ([string]$portFilter.Protocol -in @('6', 'TCP', '256', 'Any') -and
            ($portMatches -or $anyPortConflict)) {
            $conflicts += $otherRule
        }
    }
}
if ($conflicts.Count -ne 0) {
    throw 'Another enabled inbound allow rule can reach TCP 8443; remove or narrow it before rollout.'
}
$plan.status = 'configured_no_conflicting_allow_rule_observed'
$plan | ConvertTo-Json -Depth 4
