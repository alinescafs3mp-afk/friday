Set-StrictMode -Version 2.0

function New-FridayFirewallAssessment {
    param(
        [Parameter(Mandatory = $true)][bool]$Conflict,
        [Parameter(Mandatory = $true)][string]$Reason,
        [Parameter()][bool]$PackageExempt = $false
    )
    [pscustomobject][ordered]@{
        conflict = $Conflict
        reason = $Reason
        package_exempt = $PackageExempt
    }
}

function Get-FridayStrictTextProperty {
    param(
        [Parameter(Mandatory = $true)][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter()][switch]$AllowNull
    )
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return [pscustomobject]@{ valid = $false; value = ''; was_null = $false }
    }
    if ($null -eq $property.Value) {
        return [pscustomobject]@{ valid = [bool]$AllowNull; value = ''; was_null = $true }
    }
    if ($property.Value -isnot [string]) {
        return [pscustomobject]@{ valid = $false; value = ''; was_null = $false }
    }
    $value = [string]$property.Value
    if ($value -cne $value.Trim()) {
        return [pscustomobject]@{ valid = $false; value = ''; was_null = $false }
    }
    if (-not $AllowNull -and [string]::IsNullOrWhiteSpace($value)) {
        return [pscustomobject]@{ valid = $false; value = ''; was_null = $false }
    }
    [pscustomobject]@{ valid = $true; value = $value; was_null = $false }
}

function Get-FridayStrictAtomProperty {
    param(
        [Parameter(Mandatory = $true)][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        return [pscustomobject]@{ valid = $false; value = '' }
    }
    $raw = $property.Value
    if ($raw -is [string]) {
        $value = [string]$raw
        if ([string]::IsNullOrWhiteSpace($value) -or $value -cne $value.Trim()) {
            return [pscustomobject]@{ valid = $false; value = '' }
        }
        return [pscustomobject]@{ valid = $true; value = $value }
    }
    if ($raw -is [bool] -or $raw.GetType().IsEnum -or
        $raw -is [byte] -or $raw -is [sbyte] -or $raw -is [int16] -or
        $raw -is [uint16] -or $raw -is [int32] -or $raw -is [uint32] -or
        $raw -is [int64] -or $raw -is [uint64]) {
        $value = [Convert]::ToString($raw, [Globalization.CultureInfo]::InvariantCulture)
        return [pscustomobject]@{ valid = $true; value = $value }
    }
    [pscustomobject]@{ valid = $false; value = '' }
}

function Get-FridayStrictStringItems {
    param([Parameter(Mandatory = $true)][object]$Value)
    $rawItems = @()
    if ($Value -is [string]) {
        $rawItems = @([string]$Value)
    } elseif ($Value -is [array]) {
        foreach ($item in $Value) {
            if ($item -isnot [string]) {
                return [pscustomobject]@{ valid = $false; values = @() }
            }
            $rawItems += [string]$item
        }
    } else {
        return [pscustomobject]@{ valid = $false; values = @() }
    }
    $items = @()
    foreach ($rawItem in $rawItems) {
        foreach ($part in @($rawItem -split ',')) {
            $candidate = ([string]$part).Trim()
            if ([string]::IsNullOrWhiteSpace($candidate)) {
                return [pscustomobject]@{ valid = $false; values = @() }
            }
            $items += $candidate
        }
    }
    if ($items.Count -eq 0) {
        return [pscustomobject]@{ valid = $false; values = @() }
    }
    [pscustomobject]@{ valid = $true; values = @($items) }
}

function Get-FridayPortRelation8443 {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$PortFilters)
    if (@($PortFilters).Count -ne 1) {
        return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'port_filter_count' }
    }
    $protocol = Get-FridayStrictAtomProperty $PortFilters[0] 'Protocol'
    if (-not $protocol.valid) {
        return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'protocol_malformed' }
    }
    $protocolNumber = [uint32]0
    $isNumeric = [uint32]::TryParse(
        $protocol.value,
        [Globalization.NumberStyles]::None,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$protocolNumber
    )
    $isTcpCapable = $false
    if ($isNumeric) {
        if ($protocolNumber -eq 6 -or $protocolNumber -eq 256) {
            $isTcpCapable = $true
        } elseif ($protocolNumber -le 255) {
            return [pscustomobject]@{ state = 'no_overlap'; kind = 'non_tcp'; reason = 'protocol_excludes_tcp' }
        } else {
            return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'protocol_out_of_range' }
        }
    } elseif ([string]::Equals($protocol.value, 'TCP', [StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($protocol.value, 'Any', [StringComparison]::OrdinalIgnoreCase)) {
        $isTcpCapable = $true
    } elseif ([string]::Equals($protocol.value, 'UDP', [StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($protocol.value, 'ICMPv4', [StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($protocol.value, 'ICMPv6', [StringComparison]::OrdinalIgnoreCase)) {
        return [pscustomobject]@{ state = 'no_overlap'; kind = 'non_tcp'; reason = 'protocol_excludes_tcp' }
    } else {
        return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'protocol_unknown' }
    }
    if (-not $isTcpCapable) {
        return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'protocol_unknown' }
    }
    $portProperty = $PortFilters[0].PSObject.Properties['LocalPort']
    if ($null -eq $portProperty -or $null -eq $portProperty.Value) {
        return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'local_port_missing' }
    }
    $ports = Get-FridayStrictStringItems $portProperty.Value
    if (-not $ports.valid) {
        return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'local_port_malformed' }
    }
    $hasAny = $false
    $includes8443 = $false
    foreach ($token in $ports.values) {
        if ([string]::Equals($token, 'Any', [StringComparison]::OrdinalIgnoreCase)) {
            $hasAny = $true
            continue
        }
        if ($token -notmatch '\A(?<first>[0-9]{1,5})(?:-(?<last>[0-9]{1,5}))?\z') {
            return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'local_port_token_unknown' }
        }
        $first = [uint32]0
        $last = [uint32]0
        if (-not [uint32]::TryParse($Matches.first, [ref]$first) -or $first -gt 65535) {
            return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'local_port_out_of_range' }
        }
        $lastText = ''
        if ($Matches.ContainsKey('last')) {
            $lastText = [string]$Matches['last']
        }
        if ([string]::IsNullOrEmpty($lastText)) {
            $last = $first
        } elseif (-not [uint32]::TryParse($lastText, [ref]$last) -or
            $last -gt 65535 -or $first -gt $last) {
            return [pscustomobject]@{ state = 'unknown'; kind = 'unknown'; reason = 'local_port_range_malformed' }
        }
        if ($first -le 8443 -and 8443 -le $last) {
            $includes8443 = $true
        }
    }
    if ($hasAny) {
        return [pscustomobject]@{ state = 'overlap'; kind = 'any'; reason = 'any_local_port' }
    }
    if ($includes8443) {
        return [pscustomobject]@{ state = 'overlap'; kind = 'explicit'; reason = 'explicit_local_port' }
    }
    [pscustomobject]@{ state = 'no_overlap'; kind = 'numeric'; reason = 'local_port_excludes_8443' }
}

function Test-FridayPackageFamilyNameSyntax {
    param([Parameter(Mandatory = $true)][object]$Value)
    if ($Value -isnot [string]) { return $false }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text) -or $text -cne $text.Trim()) { return $false }
    $options = [Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    $match = [Text.RegularExpressions.Regex]::Match(
        $text,
        '\A(?<name>[A-Za-z0-9.-]{3,50})_[a-hj-km-np-tv-z0-9]{13}\z',
        $options
    )
    if (-not $match.Success) { return $false }
    $name = $match.Groups['name'].Value
    if ($name.EndsWith('.', [StringComparison]::Ordinal) -or
        $name.StartsWith('xn--', [StringComparison]::OrdinalIgnoreCase) -or
        $name.IndexOf('.xn--', [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        return $false
    }
    foreach ($reserved in @('con', 'prn', 'aux', 'nul', 'com1', 'com2', 'com3', 'com4', 'com5',
        'com6', 'com7', 'com8', 'com9', 'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5', 'lpt6',
        'lpt7', 'lpt8', 'lpt9')) {
        if ([string]::Equals($name, $reserved, [StringComparison]::OrdinalIgnoreCase) -or
            $name.StartsWith(($reserved + '.'), [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
    }
    return $true
}

function New-FridayVerifiedPackageFamilyNameSet {
    param([Parameter(Mandatory = $true)][object[]]$Values)
    if (@($Values).Count -eq 0) {
        throw 'Installed package-family inventory is empty.'
    }
    $set = New-Object 'System.Collections.Generic.HashSet[string]' -ArgumentList (
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($value in $Values) {
        if (-not (Test-FridayPackageFamilyNameSyntax $value)) {
            throw 'Installed package-family inventory contains a malformed identity.'
        }
        $set.Add([string]$value) | Out-Null
    }
    Write-Output -NoEnumerate $set
}

function Test-FridayCanonicalSpecificAppContainerSid {
    param([Parameter(Mandatory = $true)][object]$Value)
    if ($Value -isnot [string]) { return $false }
    $text = [string]$Value
    if ($text -cne $text.Trim()) { return $false }
    try {
        $sid = New-Object Security.Principal.SecurityIdentifier -ArgumentList $text
    } catch {
        return $false
    }
    if ($sid.Value -cne $text -or -not $text.StartsWith('S-1-15-2-', [StringComparison]::Ordinal)) {
        return $false
    }
    $ridParts = @($text.Substring('S-1-15-2-'.Length) -split '-')
    if ($ridParts.Count -ne 7) { return $false }
    foreach ($ridPart in $ridParts) {
        if ($ridPart -notmatch '\A(?:0|[1-9][0-9]*)\z') { return $false }
        $rid = [uint32]0
        if (-not [uint32]::TryParse(
            $ridPart,
            [Globalization.NumberStyles]::None,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$rid
        )) { return $false }
    }
    return $true
}

function Get-FridayFirewallRuleAssessment {
    param(
        [Parameter(Mandatory = $true)][object]$Rule,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$PortFilters,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$ApplicationFilters,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$ServiceFilters,
        [Parameter(Mandatory = $true)]
        [System.Collections.Generic.HashSet[string]]$InstalledPackageFamilyNames
    )
    $portRelation = Get-FridayPortRelation8443 $PortFilters
    if ($portRelation.state -eq 'no_overlap') {
        return (New-FridayFirewallAssessment -Conflict $false -Reason $portRelation.reason)
    }
    if ($portRelation.state -ne 'overlap') {
        return (New-FridayFirewallAssessment -Conflict $true -Reason $portRelation.reason)
    }
    if (@($ApplicationFilters).Count -ne 1) {
        return (New-FridayFirewallAssessment -Conflict $true -Reason 'application_filter_count')
    }
    if (@($ServiceFilters).Count -ne 1) {
        return (New-FridayFirewallAssessment -Conflict $true -Reason 'service_filter_count')
    }
    $program = Get-FridayStrictTextProperty $ApplicationFilters[0] 'Program'
    $package = Get-FridayStrictTextProperty $ApplicationFilters[0] 'Package' -AllowNull
    $service = Get-FridayStrictTextProperty $ServiceFilters[0] 'Service'
    $pfn = Get-FridayStrictTextProperty $Rule 'PackageFamilyName' -AllowNull
    $name = Get-FridayStrictTextProperty $Rule 'Name'
    if (-not $program.valid -or -not $package.valid -or -not $service.valid -or
        -not $pfn.valid -or -not $name.valid) {
        return (New-FridayFirewallAssessment -Conflict $true -Reason 'identity_filter_malformed')
    }
    $programAny = [string]::Equals($program.value, 'Any', [StringComparison]::Ordinal)
    $serviceAny = [string]::Equals($service.value, 'Any', [StringComparison]::Ordinal)
    $hasPackageSid = -not [string]::IsNullOrEmpty($package.value)
    $hasPfn = -not [string]::IsNullOrEmpty($pfn.value)
    if ($hasPackageSid) {
        if ($hasPfn -or -not $programAny -or -not $serviceAny -or
            -not (Test-FridayCanonicalSpecificAppContainerSid $package.value)) {
            return (New-FridayFirewallAssessment -Conflict $true -Reason 'legacy_package_identity_unverified')
        }
        return (New-FridayFirewallAssessment `
            -Conflict $false -Reason 'specific_appcontainer_sid' -PackageExempt $true)
    }
    if ($hasPfn) {
        $suffix = '-In-Allow-ServerCapability'
        if (-not $programAny -or -not $serviceAny -or
            -not (Test-FridayPackageFamilyNameSyntax $pfn.value) -or
            -not $InstalledPackageFamilyNames.Contains($pfn.value) -or
            -not $name.value.EndsWith($suffix, [StringComparison]::Ordinal)) {
            return (New-FridayFirewallAssessment -Conflict $true -Reason 'package_family_identity_unverified')
        }
        return (New-FridayFirewallAssessment `
            -Conflict $false -Reason 'installed_package_server_capability' -PackageExempt $true)
    }
    if ($name.value.EndsWith('-In-Allow-ServerCapability', [StringComparison]::Ordinal)) {
        return (New-FridayFirewallAssessment -Conflict $true -Reason 'package_family_identity_missing')
    }
    if ($portRelation.kind -eq 'explicit') {
        return (New-FridayFirewallAssessment -Conflict $true -Reason 'explicit_8443_allow')
    }
    $programBroad = $programAny -or
        [string]::Equals($program.value, 'System', [StringComparison]::OrdinalIgnoreCase) -or
        $program.value -match '(?i)(?:docker|com\.docker)'
    $serviceBroad = $serviceAny -or $service.value -match '(?i)(?:docker|hns|winnat)'
    if ($programBroad -and $serviceBroad) {
        return (New-FridayFirewallAssessment -Conflict $true -Reason 'unscoped_any_port_allow')
    }
    New-FridayFirewallAssessment -Conflict $false -Reason 'application_or_service_excludes_docker'
}

function Test-FridayManagedFirewallRuleExact {
    param(
        [Parameter(Mandatory = $true)][object]$Rule,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$PortFilters,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$ApplicationFilters,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$ServiceFilters,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$AddressFilters,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$SecurityFilters,
        [Parameter(Mandatory = $true)][string]$PrimaryFridayHost
    )
    if (@($PortFilters).Count -ne 1 -or @($ApplicationFilters).Count -ne 1 -or
        @($ServiceFilters).Count -ne 1 -or @($AddressFilters).Count -ne 1 -or
        @($SecurityFilters).Count -ne 1) { return $false }
    $ruleExpectations = [ordered]@{
        Direction = 'Inbound'
        Action = 'Allow'
        Enabled = 'True'
        Profile = 'Any'
        EdgeTraversalPolicy = 'Block'
        LocalOnlyMapping = 'False'
        LooseSourceMapping = 'False'
        PolicyStoreSourceType = 'Local'
        PolicyStoreSource = 'PersistentStore'
    }
    foreach ($propertyName in $ruleExpectations.Keys) {
        $observed = Get-FridayStrictAtomProperty $Rule $propertyName
        if (-not $observed.valid -or
            -not [string]::Equals(
                $observed.value,
                $ruleExpectations[$propertyName],
                [StringComparison]::OrdinalIgnoreCase
            )) {
            return $false
        }
    }
    $protocol = Get-FridayStrictAtomProperty $PortFilters[0] 'Protocol'
    $remotePort = Get-FridayStrictTextProperty $PortFilters[0] 'RemotePort'
    $program = Get-FridayStrictTextProperty $ApplicationFilters[0] 'Program'
    $package = Get-FridayStrictTextProperty $ApplicationFilters[0] 'Package' -AllowNull
    $service = Get-FridayStrictTextProperty $ServiceFilters[0] 'Service'
    $pfn = Get-FridayStrictTextProperty $Rule 'PackageFamilyName' -AllowNull
    if (-not $protocol.valid -or $protocol.value -notin @('6', 'TCP') -or
        -not $remotePort.valid -or $remotePort.value -cne 'Any' -or
        -not $program.valid -or $program.value -cne 'Any' -or
        -not $package.valid -or -not [string]::IsNullOrEmpty($package.value) -or
        -not $service.valid -or $service.value -cne 'Any' -or
        -not $pfn.valid -or -not [string]::IsNullOrEmpty($pfn.value)) { return $false }
    $securityExpectations = [ordered]@{
        Authentication = 'NotRequired'
        Encryption = 'NotRequired'
        OverrideBlockRules = 'False'
        LocalUser = 'Any'
        RemoteUser = 'Any'
        RemoteMachine = 'Any'
    }
    foreach ($propertyName in $securityExpectations.Keys) {
        $observed = Get-FridayStrictAtomProperty $SecurityFilters[0] $propertyName
        if (-not $observed.valid -or
            -not [string]::Equals(
                $observed.value,
                $securityExpectations[$propertyName],
                [StringComparison]::OrdinalIgnoreCase
            )) {
            return $false
        }
    }
    $localPortProperty = $PortFilters[0].PSObject.Properties['LocalPort']
    $localAddressProperty = $AddressFilters[0].PSObject.Properties['LocalAddress']
    $remoteAddressProperty = $AddressFilters[0].PSObject.Properties['RemoteAddress']
    if ($null -eq $localPortProperty -or $null -eq $localAddressProperty -or
        $null -eq $remoteAddressProperty) { return $false }
    $localPorts = Get-FridayStrictStringItems $localPortProperty.Value
    $localAddresses = Get-FridayStrictStringItems $localAddressProperty.Value
    $remoteAddresses = Get-FridayStrictStringItems $remoteAddressProperty.Value
    if (-not $localPorts.valid -or $localPorts.values.Count -ne 1 -or
        $localPorts.values[0] -cne '8443' -or
        -not $localAddresses.valid -or $localAddresses.values.Count -ne 1 -or
        $localAddresses.values[0] -cne 'Any' -or
        -not $remoteAddresses.valid -or $remoteAddresses.values.Count -ne 1 -or
        $remoteAddresses.values[0] -cne $PrimaryFridayHost) { return $false }
    return $true
}
