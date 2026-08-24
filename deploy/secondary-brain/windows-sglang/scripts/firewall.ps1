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
$otherRules = @(
    Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
        Where-Object { [string]$_.DisplayName -cne $ruleName }
)
foreach ($otherRule in $otherRules) {
    $portFilters = @($otherRule | Get-NetFirewallPortFilter)
    $applicationFilters = @($otherRule | Get-NetFirewallApplicationFilter)
    foreach ($portFilter in $portFilters) {
        $portMatches = @([string]$portFilter.LocalPort -split ',') -contains '8443'
        $broadDockerProgram = @(
            $applicationFilters |
                Where-Object {
                    [string]$_.Program -match '(?i)(?:docker|com\.docker)' -and
                    [string]$portFilter.LocalPort -ceq 'Any'
                }
        ).Count -ne 0
        if ([string]$portFilter.Protocol -in @('6', 'TCP') -and ($portMatches -or $broadDockerProgram)) {
            $conflicts += $otherRule
        }
    }
}
if ($conflicts.Count -ne 0) {
    throw 'Another enabled inbound allow rule can reach TCP 8443; remove or narrow it before rollout.'
}
$plan.status = 'configured_no_conflicting_allow_rule_observed'
$plan | ConvertTo-Json -Depth 4
