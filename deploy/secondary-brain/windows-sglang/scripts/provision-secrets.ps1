[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$OpenSslPath = 'openssl.exe',

    [Parameter()]
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$bundleRoot = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
$secretRoot = Join-Path $bundleRoot 'secrets'
$tlsRoot = Join-Path $secretRoot 'tls'
$configPath = (Get-Item -LiteralPath (Join-Path $bundleRoot 'tls-openssl.cnf')).FullName
$gatewayKeyPath = Join-Path $secretRoot 'gateway-api-key'
$sglangKeyPath = Join-Path $secretRoot 'sglang-api-key'
$caKeyPath = Join-Path $tlsRoot 'ca.key'
$caCertPath = Join-Path $tlsRoot 'ca.crt'
$serverKeyPath = Join-Path $tlsRoot 'server.key'
$serverCertPath = Join-Path $tlsRoot 'server.crt'
$finalPaths = @(
    $gatewayKeyPath, $sglangKeyPath, $caKeyPath,
    $caCertPath, $serverKeyPath, $serverCertPath
)

function ConvertTo-NativeArgument([string]$Value) {
    if ([string]::IsNullOrEmpty($Value)) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }

    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes += 1
            continue
        }
        if ($character -eq [char]34) {
            [void]$builder.Append((('\' * (($backslashes * 2) + 1)) -join ''))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append((('\' * $backslashes) -join ''))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append((('\' * ($backslashes * 2)) -join ''))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-OpenSsl([string[]]$Arguments) {
    # Windows PowerShell 5 turns benign native stderr into a terminating error
    # under ErrorActionPreference=Stop.  Capture both streams directly so the
    # native exit code alone decides success and no command output can leak on
    # a provisioning failure.
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $OpenSslPath
    $startInfo.Arguments = (($Arguments | ForEach-Object {
        ConvertTo-NativeArgument ([string]$_)
    }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw 'OpenSSL process did not start.'
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderrTask.GetAwaiter().GetResult() | Out-Null
        if ($process.ExitCode -ne 0) {
            throw 'OpenSSL operation failed; output was suppressed because this is a secret provisioning boundary.'
        }
        return $stdout.Trim()
    } finally {
        $process.Dispose()
    }
}

function New-RandomHex([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-BearerFile([string]$Path) {
    $value = [IO.File]::ReadAllText($Path, [Text.Encoding]::ASCII)
    if ($value -notmatch '\A[0-9a-f]{64}\z') {
        throw 'Existing bearer file is not one exact 256-bit lowercase hex value.'
    }
    return $value
}

function Assert-CertificateSet(
    [string]$CaKey,
    [string]$CaCert,
    [string]$ServerKey,
    [string]$ServerCert
) {
    Invoke-OpenSsl @('verify', '-CAfile', $CaCert, $ServerCert) | Out-Null
    Invoke-OpenSsl @('x509', '-checkend', '2592000', '-noout', '-in', $ServerCert) | Out-Null
    $certificateText = Invoke-OpenSsl @('x509', '-in', $ServerCert, '-noout', '-text')
    if ($certificateText -notmatch 'IP Address:192\.168\.1\.35' -or
        $certificateText -notmatch 'IP Address:127\.0\.0\.1') {
        throw 'Server certificate lacks the exact laptop and loopback IP SANs.'
    }
    $serverPublicFromKey = Invoke-OpenSsl @('pkey', '-in', $ServerKey, '-pubout')
    $serverPublicFromCert = Invoke-OpenSsl @('x509', '-in', $ServerCert, '-pubkey', '-noout')
    if (-not [string]::Equals($serverPublicFromKey, $serverPublicFromCert, [StringComparison]::Ordinal)) {
        throw 'Server certificate and private key do not match.'
    }
    $caPublicFromKey = Invoke-OpenSsl @('pkey', '-in', $CaKey, '-pubout')
    $caPublicFromCert = Invoke-OpenSsl @('x509', '-in', $CaCert, '-pubkey', '-noout')
    if (-not [string]::Equals($caPublicFromKey, $caPublicFromCert, [StringComparison]::Ordinal)) {
        throw 'CA certificate and private key do not match.'
    }
}

function Set-RestrictiveSecretAcl() {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $currentSid = $currentIdentity.User
    $systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $targets = @((Get-Item -LiteralPath $secretRoot)) + @(
        Get-ChildItem -LiteralPath $secretRoot -Force -Recurse
    )
    foreach ($target in $targets) {
        $acl = Get-Acl -LiteralPath $target.FullName
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($existing in @($acl.Access)) {
            $acl.RemoveAccessRuleSpecific($existing)
        }
        $inheritance = if ($target.PSIsContainer) {
            [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        } else {
            [Security.AccessControl.InheritanceFlags]::None
        }
        foreach ($sid in @($currentSid, $systemSid)) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
            $acl.AddAccessRule($rule) | Out-Null
        }
        $acl.SetOwner($currentSid)
        Set-Acl -LiteralPath $target.FullName -AclObject $acl

        $observed = Get-Acl -LiteralPath $target.FullName
        $observedSids = @(
            $observed.Access | ForEach-Object {
                $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            }
        )
        $allowedSids = @($currentSid.Value, $systemSid.Value)
        if (-not $observed.AreAccessRulesProtected -or
            $observedSids.Count -ne 2 -or
            @($observedSids | Where-Object { $_ -notin $allowedSids }).Count -ne 0 -or
            @($observed.Access | Where-Object {
                $_.IsInherited -or
                $_.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne
                    [Security.AccessControl.FileSystemRights]::FullControl
            }).Count -ne 0) {
            throw 'Secret ACL readback differs from the exact owner/SYSTEM allowlist.'
        }
    }
}

$plan = [ordered]@{
    schema = 'friday.secondary-secret-provisioning.v1'
    apply = [bool]$Apply
    target = 'bundle-local ignored secrets directory'
    gateway_bearer_bits = 256
    sglang_bearer_bits = 256
    distinct_bearers_required = $true
    certificate_ip_sans = @('192.168.1.35', '127.0.0.1')
    private_material_exported = $false
}
if (-not $Apply) {
    $plan | ConvertTo-Json -Depth 4
    return
}

$openssl = Get-Command $OpenSslPath -CommandType Application -ErrorAction Stop
$OpenSslPath = $openssl.Source
[IO.Directory]::CreateDirectory($tlsRoot) | Out-Null
Set-RestrictiveSecretAcl
$presentCount = @($finalPaths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }).Count
if ($presentCount -ne 0 -and $presentCount -ne $finalPaths.Count) {
    throw 'Secret provisioning is partial; inspect it manually before retrying. No file was overwritten.'
}

$status = 'existing_set_verified'
if ($presentCount -eq 0) {
    $stagingRoot = Join-Path $secretRoot ('.provisioning-' + [Guid]::NewGuid().ToString('N'))
    $stagingTls = Join-Path $stagingRoot 'tls'
    [IO.Directory]::CreateDirectory($stagingTls) | Out-Null
    try {
        $gatewayBearer = New-RandomHex 32
        $sglangBearer = New-RandomHex 32
        if ([string]::Equals($gatewayBearer, $sglangBearer, [StringComparison]::Ordinal)) {
            throw 'Independent random bearer generation collided.'
        }
        [IO.File]::WriteAllText(
            (Join-Path $stagingRoot 'gateway-api-key'),
            $gatewayBearer,
            [Text.Encoding]::ASCII
        )
        [IO.File]::WriteAllText(
            (Join-Path $stagingRoot 'sglang-api-key'),
            $sglangBearer,
            [Text.Encoding]::ASCII
        )
        $stagingCaKey = Join-Path $stagingTls 'ca.key'
        $stagingCaCert = Join-Path $stagingTls 'ca.crt'
        $stagingServerKey = Join-Path $stagingTls 'server.key'
        $stagingServerCert = Join-Path $stagingTls 'server.crt'
        $stagingRequest = Join-Path $stagingTls 'server.csr'
        Invoke-OpenSsl @(
            'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256',
            '-out', $stagingCaKey
        ) | Out-Null
        Invoke-OpenSsl @(
            'req', '-new', '-x509', '-sha256', '-days', '3650',
            '-key', $stagingCaKey, '-subj', '/CN=Friday Secondary Private CA',
            '-config', $configPath, '-extensions', 'ca_ext', '-out', $stagingCaCert
        ) | Out-Null
        Invoke-OpenSsl @(
            'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256',
            '-out', $stagingServerKey
        ) | Out-Null
        Invoke-OpenSsl @(
            'req', '-new', '-sha256', '-key', $stagingServerKey,
            '-config', $configPath, '-out', $stagingRequest
        ) | Out-Null
        Invoke-OpenSsl @(
            'x509', '-req', '-sha256', '-days', '825', '-in', $stagingRequest,
            '-CA', $stagingCaCert, '-CAkey', $stagingCaKey, '-CAcreateserial',
            '-extfile', $configPath, '-extensions', 'server_ext', '-out', $stagingServerCert
        ) | Out-Null
        Assert-CertificateSet $stagingCaKey $stagingCaCert $stagingServerKey $stagingServerCert
        Move-Item -LiteralPath (Join-Path $stagingRoot 'gateway-api-key') -Destination $gatewayKeyPath
        Move-Item -LiteralPath (Join-Path $stagingRoot 'sglang-api-key') -Destination $sglangKeyPath
        Move-Item -LiteralPath $stagingCaKey -Destination $caKeyPath
        Move-Item -LiteralPath $stagingCaCert -Destination $caCertPath
        Move-Item -LiteralPath $stagingServerKey -Destination $serverKeyPath
        Move-Item -LiteralPath $stagingServerCert -Destination $serverCertPath
        $status = 'new_set_provisioned'
    } finally {
        if (Test-Path -LiteralPath $stagingRoot -PathType Container) {
            if (-not $stagingRoot.StartsWith(($secretRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::Ordinal) -or
                [IO.Path]::GetFileName($stagingRoot) -notmatch '\A\.provisioning-[0-9a-f]{32}\z') {
                throw 'Refusing unsafe staging cleanup path.'
            }
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force
        }
    }
}

$gateway = Assert-BearerFile $gatewayKeyPath
$sglang = Assert-BearerFile $sglangKeyPath
if ([string]::Equals($gateway, $sglang, [StringComparison]::Ordinal)) {
    throw 'Gateway and internal SGLang bearers are not distinct.'
}
Assert-CertificateSet $caKeyPath $caCertPath $serverKeyPath $serverCertPath
Set-RestrictiveSecretAcl
[ordered]@{
    schema = 'friday.secondary-secret-provisioning.v1'
    status = $status
    distinct_bearers_verified = $true
    certificate_ip_sans = @('192.168.1.35', '127.0.0.1')
    ca_certificate_sha256 = Get-FileSha256 $caCertPath
    server_certificate_sha256 = Get-FileSha256 $serverCertPath
    exportable_artifact = 'secrets/tls/ca.crt'
    private_material_exported = $false
    secret_values_reported = $false
} | ConvertTo-Json -Depth 4
